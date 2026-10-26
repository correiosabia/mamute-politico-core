"""Votos depois das 17h, secao por secao, a partir dos arquivos de urna (CS-163).

Duas etapas, cada uma grava um JSONL por UF em `--saida/<eleicao>/` e pula o
que ja esta la (a coleta e longa e pode ser retomada):

1. `bu`: o boletim de urna de todas as secoes (abertura, encerramento,
   local de votacao, comparecimento e votos de presidente).
2. `log`: o log das secoes que encerraram depois do limiar (horario de
   Brasilia) mais uma amostra das outras, para medir o que o limiar deixa de
   fora. Do log sai a hora de cada voto.

Fontes:

- 2026 (1o e 2o turno): `resultados.tse.jus.br/oficial/<ciclo>/arquivo-urna/<pleito>`,
  um `aux.json` por secao com os arquivos de cada urna (hash).
- 2022 (2o turno): o pacote por UF do Dados Abertos (um zip de ate 20 GB),
  lido por faixa de bytes so nos arquivos que interessam.

Exterior fica de fora: vota no horario local de cada pais.

Uso:
python -m mamute_scrappers.tse_crawler.votacao_horario bu --eleicao 2026-2t --saida /dados/votacao-horario
python -m mamute_scrappers.tse_crawler.votacao_horario log --eleicao 2026-2t --saida /dados/votacao-horario
python -m mamute_scrappers.tse_crawler.votacao_horario carregar --eleicao 2026-2t --saida /dados/votacao-horario \
    --cadastro eleitorado_local_votacao_2026_BRASIL.csv

`carregar` agrega (ver `votacao_horario_agregacao.py`) e troca a eleicao
inteira nas tabelas `votacao_horario_agregado` e `votacao_horario_local`. O
cadastro de locais e o de 2026; em 2022 ele so da nome e coordenada do
municipio, e os locais nao sao gravados (o numero do local mudou desde entao).
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import struct
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from mamute_scrappers.tse_crawler.resultados_parsing import UFS
from mamute_scrappers.tse_crawler.urna_parsing import (
    classificar_votos,
    extrair_log,
    fuso_do_municipio,
    parse_bu,
    votos_do_log,
)

logger = logging.getLogger(__name__)

BASE_RESULTADOS = "https://resultados.tse.jus.br/oficial"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MamutePolitico/1.0)"}
TIMEOUT = 60
ST_DESCARTADOS = ("exclu", "cancel", "substitu")


@dataclass(frozen=True)
class Eleicao:
    chave: str
    cd_eleicao: int
    dia: date
    # Fonte 2026: ciclo + pleito. Fonte 2022: modelo da URL do pacote por UF.
    ciclo: Optional[str] = None
    pleito: Optional[int] = None
    pacote: Optional[str] = None


ELEICOES: Dict[str, Eleicao] = {
    "2026-2t": Eleicao("2026-2t", 6258, date(2026, 10, 25), ciclo="ele2026", pleito=3221),
    # O pacote por UF do 1o turno ja saiu (22/10/2026); o do 2o turno ainda nao.
    "2026-1t": Eleicao(
        "2026-1t",
        6257,
        date(2026, 10, 4),
        pacote="https://cdn.tse.jus.br/estatistica/sead/eleicoes/eleicoes2026/arqurnatot/"
        "bu_imgbu_logjez_rdv_vscmr_2026_1t_{UF}.zip",
    ),
    "2022-2t": Eleicao(
        "2022-2t",
        545,
        date(2022, 10, 30),
        pacote="https://cdn.tse.jus.br/estatistica/sead/eleicoes/eleicoes2022/arqurnatot/"
        "bu_imgbu_logjez_rdv_vscmr_2022_2t_{UF}.zip",
    ),
}


@dataclass
class SecaoBu:
    uf: str
    municipio: int
    zona: int
    local: int
    secao: int
    abertura: datetime
    encerramento: datetime
    comparecimento: int
    votos: Dict[str, int]
    # Onde estao os logs: caminhos "hash/arquivo" (2026) ou nomes no pacote (2022).
    logs: List[str] = field(default_factory=list)

    def para_json(self) -> dict:
        d = asdict(self)
        d["abertura"] = self.abertura.isoformat()
        d["encerramento"] = self.encerramento.isoformat()
        return d

    @classmethod
    def de_json(cls, d: dict) -> "SecaoBu":
        d = dict(d)
        d["abertura"] = datetime.fromisoformat(d["abertura"])
        d["encerramento"] = datetime.fromisoformat(d["encerramento"])
        return cls(**d)


# --- listas e nomes -----------------------------------------------------------


def secoes_do_config(cs: dict) -> List[Tuple[str, int, int, int]]:
    """(uf, municipio, zona, secao) do `<uf>-p<pleito>-cs.json`."""
    saida = []
    for abr in cs.get("abr", []):
        uf = abr["cd"].lower()
        for mu in abr.get("mu", []):
            for zon in mu.get("zon", []):
                for sec in zon.get("sec", []):
                    saida.append((uf, int(mu["cd"]), int(zon["cd"]), int(sec["ns"])))
    return saida


def arquivos_da_secao(aux: dict) -> Tuple[Optional[str], List[str]]:
    """BU (da ultima urna valida) e todos os logs validos de uma secao.

    Secao com urna de contingencia tem mais de um hash; os votos se dividem
    entre os logs, por isso todos entram.
    """
    bu: Optional[str] = None
    logs: List[str] = []
    for h in aux.get("hashes", []):
        st = (h.get("st") or "").lower()
        if any(d in st for d in ST_DESCARTADOS):
            continue
        for arq in h.get("arq", []):
            caminho = f"{h['hash']}/{arq['nm']}"
            if arq.get("tp") == "bu":
                bu = caminho
            elif arq.get("tp") == "log":
                logs.append(caminho)
    return bu, logs


# 2022: "o00407-0101500020088.bu" / ".logjez"; 2026: "o03220ac0101500020088-bu.dat" / "-log.jez".
_NOME_PACOTE = re.compile(r"^o\d+(?:-|[a-z]{2})(\d{5})(\d{4})(\d{4})[.-](\w+)(?:\.\w+)?$")
_TIPO_PACOTE = {"logjez": "log"}


def secao_do_nome_pacote(nome: str) -> Optional[Tuple[int, int, int, str]]:
    """(municipio, zona, secao, tipo) de um arquivo do pacote por UF."""
    m = _NOME_PACOTE.match(nome.rsplit("/", 1)[-1])
    if not m:
        return None
    tipo = m.group(4)
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), _TIPO_PACOTE.get(tipo, tipo)


# --- zip remoto por faixa de bytes ---------------------------------------------


@dataclass(frozen=True)
class MembroZip:
    nome: str
    offset: int
    tamanho: int
    compressao: int


Faixa = Callable[[int, int], bytes]
_FOLGA_CABECALHO = 30 + 512


def ler_membro_zip(membro: MembroZip, faixa: Faixa) -> bytes:
    """Le um arquivo de dentro de um zip remoto com um so pedido de faixa."""
    bloco = faixa(membro.offset, membro.offset + _FOLGA_CABECALHO + membro.tamanho)
    assinatura, *_, tam_nome, tam_extra = struct.unpack("<IHHHHHIIIHH", bloco[:30])
    if assinatura != 0x04034B50:
        raise ValueError(f"cabecalho local invalido em {membro.nome}")
    ini = 30 + tam_nome + tam_extra
    if ini + membro.tamanho > len(bloco):
        bloco += faixa(membro.offset + len(bloco), membro.offset + ini + membro.tamanho - 1)
    dados = bloco[ini : ini + membro.tamanho]
    if membro.compressao == 0:
        return dados
    if membro.compressao == 8:
        return zlib.decompress(dados, -15)
    raise ValueError(f"compressao {membro.compressao} nao suportada")


class ArquivoRemoto:
    """Arquivo HTTP com seek, para o `zipfile` ler so o diretorio central."""

    def __init__(self, url: str, sessao: requests.Session):
        self.url, self.sessao, self.pos = url, sessao, 0
        r = sessao.head(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        self.tamanho = int(r.headers["Content-Length"])

    def faixa(self, inicio: int, fim: int) -> bytes:
        fim = min(fim, self.tamanho - 1)
        r = self.sessao.get(self.url, headers={**HEADERS, "Range": f"bytes={inicio}-{fim}"}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.tamanho + offset
        return self.pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.tamanho - self.pos
        if n == 0 or self.pos >= self.tamanho:
            return b""
        dados = self.faixa(self.pos, self.pos + n - 1)
        self.pos += len(dados)
        return dados


def membros_do_pacote(remoto: ArquivoRemoto) -> Dict[str, MembroZip]:
    import zipfile

    with zipfile.ZipFile(remoto) as z:
        return {
            i.filename: MembroZip(i.filename, i.header_offset, i.compress_size, i.compress_type)
            for i in z.infolist()
        }


# --- fuso e selecao ------------------------------------------------------------


def fusos_por_municipio(secoes: Iterable[SecaoBu]) -> Dict[Tuple[str, int], int]:
    aberturas: Dict[Tuple[str, int], List[datetime]] = {}
    for s in secoes:
        aberturas.setdefault((s.uf, s.municipio), []).append(s.abertura)
    return {k: fuso_do_municipio(v) for k, v in aberturas.items()}


def selecionar_para_log(
    secoes: Sequence[SecaoBu],
    fusos: Dict[Tuple[str, int], int],
    *,
    limiar: Tuple[int, int] = (17, 1),
    amostra: float = 0.01,
    semente: int = 163,
) -> List[Tuple[SecaoBu, str]]:
    """Secoes cujo log sera lido: as que encerraram no limiar ou depois (hora
    de Brasilia) e uma amostra aleatoria das demais."""
    rnd = random.Random(semente)
    escolhidas = []
    for s in secoes:
        enc = s.encerramento + timedelta(hours=fusos.get((s.uf, s.municipio), 0))
        if (enc.hour, enc.minute) >= limiar:
            escolhidas.append((s, "tardia"))
        elif amostra and rnd.random() < amostra:
            escolhidas.append((s, "amostra"))
    return escolhidas


# --- coleta ---------------------------------------------------------------------


class _Gravador:
    def __init__(self, caminho: Path):
        caminho.parent.mkdir(parents=True, exist_ok=True)
        self.caminho = caminho
        self.lock = threading.Lock()
        self.feitos = set()
        if caminho.exists():
            for linha in caminho.read_text().splitlines():
                if linha.strip():
                    d = json.loads(linha)
                    self.feitos.add((d["municipio"], d["zona"], d["secao"]))

    def gravar(self, d: dict) -> None:
        with self.lock, self.caminho.open("a") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


class Limitador:
    """No maximo `por_segundo` pedidos por segundo, somando todas as threads.

    O `resultados.tse.jus.br` bloqueia o IP (429 em tudo, por varios minutos)
    quando passa de cerca de 150 pedidos por segundo."""

    def __init__(self, por_segundo: float, relogio=None, dormir=None):
        import time

        self.intervalo = 1.0 / por_segundo if por_segundo else 0.0
        self.relogio = relogio or time.monotonic
        self.dormir = dormir or time.sleep
        self.lock = threading.Lock()
        self.proximo = None

    def aguardar(self) -> None:
        if not self.intervalo:
            return
        with self.lock:
            agora = self.relogio()
            vez = agora if self.proximo is None or self.proximo < agora else self.proximo
            self.proximo = vez + self.intervalo
        if vez > agora:
            self.dormir(vez - agora)


_LIMITADOR = Limitador(0)


def _get(sessao: requests.Session, url: str, tentativas: int = 6) -> requests.Response:
    """GET com nova tentativa. O CDN do TSE responde 429 quando ha pedidos
    demais; nesse caso a espera e maior (e respeita o Retry-After)."""
    ultimo: Optional[Exception] = None
    for t in range(tentativas):
        espera = 2 * (t + 1)
        _LIMITADOR.aguardar()
        try:
            r = sessao.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 404:
                return r
            if r.status_code == 429:
                espera = max(int(r.headers.get("Retry-After", "0") or 0), 15 * (t + 1))
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            ultimo = e
        threading.Event().wait(espera)
    raise RuntimeError(f"falhou {url}: {ultimo}")


def _url_secao(e: Eleicao, uf: str, mu: int, zo: int, se: int) -> str:
    return f"{BASE_RESULTADOS}/{e.ciclo}/arquivo-urna/{e.pleito}/dados/{uf}/{mu:05d}/{zo:04d}/{se:04d}"


def _bu_2026(e: Eleicao, sessao, uf: str, mu: int, zo: int, se: int) -> Optional[SecaoBu]:
    base = _url_secao(e, uf, mu, zo, se)
    r = _get(sessao, f"{base}/p{e.pleito:06d}-{uf}-m{mu:05d}-z{zo:04d}-s{se:04d}-aux.json")
    if r.status_code == 404:
        return None
    bu_path, logs = arquivos_da_secao(r.json())
    if not bu_path:
        return None
    r = _get(sessao, f"{base}/{bu_path}")
    if r.status_code == 404:
        return None
    bu = parse_bu(r.content, cd_eleicao=e.cd_eleicao)
    return SecaoBu(uf, mu, zo, bu.local, se, bu.abertura, bu.encerramento, bu.comparecimento, bu.votos, logs)


def coletar_bu(e: Eleicao, saida: Path, ufs: Sequence[str], workers: int) -> None:
    sessao = requests.Session()
    sessao.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=workers * 2))
    for uf in ufs:
        grav = _Gravador(saida / e.chave / f"{uf}.bu.jsonl")
        if e.pacote:
            _coletar_bu_pacote(e, sessao, uf, grav, workers)
            continue
        cs = _get(sessao, f"{BASE_RESULTADOS}/{e.ciclo}/arquivo-urna/{e.pleito}/config/{uf}/{uf}-p{e.pleito:06d}-cs.json").json()
        pendentes = [s for s in secoes_do_config(cs) if s[1:] not in grav.feitos]
        for passada in range(1, 4):
            logger.info("%s %s: %d secoes pendentes (passada %d)", e.chave, uf, len(pendentes), passada)
            falhas = []
            with ThreadPoolExecutor(workers) as ex:
                futs = {ex.submit(_bu_2026, e, sessao, *s): s for s in pendentes}
                for i, f in enumerate(as_completed(futs), 1):
                    try:
                        sec = f.result()
                        if sec:
                            grav.gravar(sec.para_json())
                    except Exception as exc:  # noqa: BLE001 - registra e tenta na proxima passada
                        falhas.append(futs[f])
                        logger.warning("%s %s: %s", e.chave, futs[f], exc)
                    if i % 5000 == 0:
                        logger.info("%s %s: %d/%d", e.chave, uf, i, len(pendentes))
            if not falhas:
                break
            pendentes = falhas
        logger.info("%s %s: fim (%d sem BU apos as passadas)", e.chave, uf, len(falhas))


def _coletar_bu_pacote(e: Eleicao, sessao, uf: str, grav: _Gravador, workers: int) -> None:
    remoto = ArquivoRemoto(e.pacote.format(UF=uf.upper()), sessao)
    membros = membros_do_pacote(remoto)
    por_secao: Dict[Tuple[int, int, int], Dict[str, str]] = {}
    for nome in membros:
        lido = secao_do_nome_pacote(nome)
        if lido:
            *chave, tipo = lido
            por_secao.setdefault(tuple(chave), {})[tipo] = nome
    pendentes = [(k, v) for k, v in por_secao.items() if k not in grav.feitos and "bu" in v]
    logger.info("%s %s: %d secoes pendentes", e.chave, uf, len(pendentes))

    def um(chave, arquivos) -> SecaoBu:
        bu = parse_bu(ler_membro_zip(membros[arquivos["bu"]], remoto.faixa), cd_eleicao=e.cd_eleicao)
        logs = [arquivos["log"]] if "log" in arquivos else []
        mu, zo, se = chave
        return SecaoBu(uf, mu, zo, bu.local, se, bu.abertura, bu.encerramento, bu.comparecimento, bu.votos, logs)

    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(um, k, v): k for k, v in pendentes}
        for f in as_completed(futs):
            try:
                grav.gravar(f.result().para_json())
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s %s %s: %s", e.chave, uf, futs[f], exc)


def ler_bus(saida: Path, e: Eleicao, uf: str) -> List[SecaoBu]:
    caminho = saida / e.chave / f"{uf}.bu.jsonl"
    if not caminho.exists():
        return []
    return [SecaoBu.de_json(json.loads(l)) for l in caminho.read_text().splitlines() if l.strip()]


def coletar_log(
    e: Eleicao, saida: Path, ufs: Sequence[str], workers: int, limiar: Tuple[int, int], amostra: float
) -> None:
    sessao = requests.Session()
    sessao.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=workers * 2))
    for uf in ufs:
        secoes = ler_bus(saida, e, uf)
        fusos = fusos_por_municipio(secoes)
        grav = _Gravador(saida / e.chave / f"{uf}.log.jsonl")
        escolhidas = [(s, m) for s, m in selecionar_para_log(secoes, fusos, limiar=limiar, amostra=amostra)
                      if (s.municipio, s.zona, s.secao) not in grav.feitos]  # fmt: skip
        logger.info("%s %s: %d logs pendentes", e.chave, uf, len(escolhidas))
        remoto = membros = None
        if e.pacote:
            remoto = ArquivoRemoto(e.pacote.format(UF=uf.upper()), sessao)
            membros = membros_do_pacote(remoto)

        def um(s: SecaoBu, motivo: str) -> dict:
            votos: List[datetime] = []
            for caminho in s.logs:
                if e.pacote:
                    bruto = ler_membro_zip(membros[caminho], remoto.faixa)
                else:
                    r = _get(sessao, f"{_url_secao(e, s.uf, s.municipio, s.zona, s.secao)}/{caminho}")
                    if r.status_code == 404:
                        continue
                    bruto = r.content
                votos.extend(votos_do_log(extrair_log(bruto), dia=e.dia))
            fuso = fusos.get((s.uf, s.municipio), 0)
            fx = classificar_votos(votos, fuso=fuso)
            return {
                "uf": s.uf, "municipio": s.municipio, "zona": s.zona, "secao": s.secao,
                "motivo": motivo, "fuso": fuso, "votos_log": fx.total, "apos_17": fx.apos_17,
                "de_17_a_19": fx.de_17_a_19, "apos_19": fx.apos_19,
                "ultimo_voto": fx.ultimo.isoformat() if fx.ultimo else None,
                "por_minuto": _por_faixa_de_10min(votos, fuso),
            }  # fmt: skip

        with ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(um, s, m): s for s, m in escolhidas}
            for f in as_completed(futs):
                try:
                    grav.gravar(f.result())
                except Exception as exc:  # noqa: BLE001
                    s = futs[f]
                    logger.warning("%s %s %s/%s/%s: %s", e.chave, uf, s.municipio, s.zona, s.secao, exc)


def _por_faixa_de_10min(votos: Iterable[datetime], fuso: int) -> Dict[str, int]:
    """Votos de 16h50 em diante, em blocos de 10 minutos (hora de Brasilia)."""
    blocos: Dict[str, int] = {}
    for v in votos:
        b = v + timedelta(hours=fuso)
        if (b.hour, b.minute) < (16, 50):
            continue
        chave = f"{b.hour:02d}:{b.minute // 10 * 10:02d}"
        blocos[chave] = blocos.get(chave, 0) + 1
    return blocos


def ler_logs(saida: Path, e: Eleicao, ufs: Sequence[str]) -> Dict[Tuple[str, int, int, int], dict]:
    logs: Dict[Tuple[str, int, int, int], dict] = {}
    for uf in ufs:
        caminho = saida / e.chave / f"{uf}.log.jsonl"
        if not caminho.exists():
            continue
        for linha in caminho.read_text().splitlines():
            if linha.strip():
                d = json.loads(linha)
                logs[(d["uf"], d["municipio"], d["zona"], d["secao"])] = d
    return logs


def carregar(e: Eleicao, saida: Path, ufs: Sequence[str], cadastro_csv: Path) -> None:
    from mamute_scrappers.db.session import get_session
    from mamute_scrappers.tse_crawler.candidacy import _load_env_file
    from mamute_scrappers.tse_crawler.votacao_horario_agregacao import agregar, gravar, ler_cadastro

    turno = 1 if e.chave == "2026-1t" else 2
    cadastro = ler_cadastro(cadastro_csv, turno=turno)
    secoes = [s for uf in ufs for s in ler_bus(saida, e, uf)]
    agregados, locais = agregar(e.chave, secoes, ler_logs(saida, e, ufs), cadastro)
    if not e.chave.startswith("2026"):
        locais = []
    _load_env_file()
    db = get_session()
    try:
        gravar(db, e.chave, agregados, locais)
    finally:
        db.close()
    br = agregados[0]
    logger.info(
        "%s carregada: %d secoes, %d lidas, %d votos depois das 17h, %d locais",
        e.chave, br["secoes"], br["secoes_lidas"], br["votos_apos_17"], len(locais),
    )  # fmt: skip


def main(argv: Optional[Sequence[str]] = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("etapa", choices=["bu", "log", "carregar"])
    p.add_argument("--eleicao", choices=sorted(ELEICOES), required=True)
    p.add_argument("--saida", type=Path, required=True)
    p.add_argument("--uf", action="append", help="so estas UFs (padrao: todas, sem o exterior)")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--limiar", default="17:01", help="encerramento a partir do qual o log e lido (hh:mm, Brasilia)")
    p.add_argument("--amostra", type=float, default=0.01)
    p.add_argument("--por-segundo", type=float, default=40, help="limite de pedidos por segundo (resultados.tse.jus.br)")
    p.add_argument("--cadastro", type=Path, help="CSV de locais de votacao (so para carregar)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    global _LIMITADOR
    _LIMITADOR = Limitador(a.por_segundo)
    e = ELEICOES[a.eleicao]
    ufs = [u.lower() for u in a.uf] if a.uf else list(UFS)
    if a.etapa == "bu":
        coletar_bu(e, a.saida, ufs, a.workers)
    elif a.etapa == "carregar":
        if not a.cadastro:
            p.error("carregar precisa de --cadastro")
        carregar(e, a.saida, ufs, a.cadastro)
    else:
        h, m = (int(x) for x in a.limiar.split(":"))
        coletar_log(e, a.saida, ufs, a.workers, (h, m), a.amostra)


if __name__ == "__main__":
    main()
