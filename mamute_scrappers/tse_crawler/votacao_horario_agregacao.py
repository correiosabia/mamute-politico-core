"""Agregacao dos votos depois das 17h por Brasil, UF, municipio e local (CS-163).

Entrada: o BU de cada secao e o resultado do log das secoes lidas (ver
`votacao_horario.py`). Saida: linhas prontas para `votacao_horario_agregado`
e `votacao_horario_local`.

Regras:

- Horario de Brasilia em tudo: o fuso de cada municipio sai da abertura
  (`fusos_por_municipio`).
- "Tardia" = secao que encerrou as 17h05 ou depois. O log de todas elas e
  lido; das que encerraram antes, so uma amostra aleatoria.
- `votos_apos_17` e o que foi medido nos logs lidos. As secoes nao lidas
  (encerraram ate 17h04) ainda podem ter algum voto depois das 17h; a taxa da
  amostra estima esse resto (`apos_17_estimado_nao_lidas`), sempre separado
  do medido.
- O voto e secreto: nao se sabe em quem foi cada voto tardio.
  `apos_17_proporcional` divide os votos tardios de cada secao pela
  proporcao do resultado dessa secao. E uma aproximacao, nao uma medida.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from statistics import median
from typing import Dict, Iterable, List, Optional, Tuple

from mamute_scrappers.tse_crawler.votacao_horario import SecaoBu, fusos_por_municipio

LIMIAR_TARDIA = (17, 5)
AMOSTRA_MINIMA_UF = 30


@dataclass(frozen=True)
class LocalCadastro:
    nome: str
    bairro: str
    endereco: str
    latitude: Optional[float]
    longitude: Optional[float]
    municipio_nome: str


ChaveSecao = Tuple[str, int, int, int]


class _Acumulador:
    def __init__(self) -> None:
        self.secoes = 0
        self.comparecimento = 0
        self.votos: Dict[str, int] = defaultdict(int)
        self.secoes_lidas = 0
        self.secoes_tardias = 0
        self.secoes_voto_apos_17 = 0
        self.votos_apos_17 = 0
        self.de_17_a_19 = 0
        self.apos_19 = 0
        self.ultimo_voto: Optional[str] = None
        self.encerramento_max: Optional[str] = None
        self.curva: Dict[str, int] = defaultdict(int)
        self.votos_secoes_tardias: Dict[str, int] = defaultdict(int)
        self.proporcional: Dict[str, float] = defaultdict(float)
        self.amostra_secoes = 0
        self.amostra_apos_17 = 0
        self.nao_lidas = 0

    def somar(self, s: SecaoBu, encerramento: str, tardia: bool, log: Optional[dict]) -> None:
        self.secoes += 1
        self.comparecimento += s.comparecimento
        for k, v in s.votos.items():
            self.votos[k] += v
        if tardia:
            self.secoes_tardias += 1
        if self.encerramento_max is None or encerramento > self.encerramento_max:
            self.encerramento_max = encerramento
        if log is None:
            self.nao_lidas += 1
            return
        self.secoes_lidas += 1
        if log["motivo"] == "amostra":
            self.amostra_secoes += 1
            self.amostra_apos_17 += log["apos_17"]
        apos = log["apos_17"]
        if not apos:
            return
        self.secoes_voto_apos_17 += 1
        self.votos_apos_17 += apos
        self.de_17_a_19 += log["de_17_a_19"]
        self.apos_19 += log["apos_19"]
        if log.get("ultimo_voto") and (self.ultimo_voto is None or log["ultimo_voto"] > self.ultimo_voto):
            self.ultimo_voto = log["ultimo_voto"]
        for k, v in (log.get("por_minuto") or {}).items():
            self.curva[k] += v
        total = sum(s.votos.values())
        for k, v in s.votos.items():
            self.votos_secoes_tardias[k] += v
            if total:
                self.proporcional[k] += apos * v / total

    def taxa_amostra(self) -> Optional[float]:
        return self.amostra_apos_17 / self.amostra_secoes if self.amostra_secoes else None

    def linha(self, taxa: Optional[float]) -> dict:
        nao_lidas_fora_amostra = self.nao_lidas
        return {
            "secoes": self.secoes,
            "comparecimento": self.comparecimento,
            "votos": dict(sorted(self.votos.items())),
            "secoes_lidas": self.secoes_lidas,
            "secoes_tardias": self.secoes_tardias,
            "secoes_voto_apos_17": self.secoes_voto_apos_17,
            "votos_apos_17": self.votos_apos_17,
            "de_17_a_19": self.de_17_a_19,
            "apos_19": self.apos_19,
            "ultimo_voto": self.ultimo_voto,
            "encerramento_max": self.encerramento_max,
            "curva": dict(sorted(self.curva.items())),
            "votos_secoes_com_voto_apos_17": dict(sorted(self.votos_secoes_tardias.items())),
            "apos_17_proporcional": {k: round(v, 2) for k, v in sorted(self.proporcional.items())},
            "amostra_secoes": self.amostra_secoes,
            "amostra_apos_17": self.amostra_apos_17,
            "apos_17_estimado_nao_lidas": round((taxa or 0.0) * nao_lidas_fora_amostra, 2),
        }


def agregar(
    eleicao: str,
    secoes: Iterable[SecaoBu],
    logs: Dict[ChaveSecao, dict],
    cadastro: Dict[Tuple[str, int, int, int], LocalCadastro],
) -> Tuple[List[dict], List[dict]]:
    secoes = list(secoes)
    fusos = fusos_por_municipio(secoes)
    brasil = _Acumulador()
    por_uf: Dict[str, _Acumulador] = defaultdict(_Acumulador)
    por_mun: Dict[Tuple[str, int], _Acumulador] = defaultdict(_Acumulador)
    por_local: Dict[Tuple[str, int, int, int], _Acumulador] = defaultdict(_Acumulador)

    for s in secoes:
        enc = s.encerramento + timedelta(hours=fusos.get((s.uf, s.municipio), 0))
        tardia = (enc.hour, enc.minute) >= LIMIAR_TARDIA
        log = logs.get((s.uf, s.municipio, s.zona, s.secao))
        enc_txt = enc.isoformat()
        for acc in (brasil, por_uf[s.uf], por_mun[(s.uf, s.municipio)], por_local[(s.uf, s.municipio, s.zona, s.local)]):
            acc.somar(s, enc_txt, tardia, log)

    taxa_br = brasil.taxa_amostra()
    agregados = [{"eleicao": eleicao, "nivel": "brasil", "uf": None, "municipio": None, "nome": "Brasil",
                  "latitude": None, "longitude": None, **brasil.linha(taxa_br)}]  # fmt: skip
    for uf, acc in sorted(por_uf.items()):
        taxa = acc.taxa_amostra() if acc.amostra_secoes >= AMOSTRA_MINIMA_UF else taxa_br
        agregados.append({"eleicao": eleicao, "nivel": "uf", "uf": uf, "municipio": None, "nome": uf.upper(),
                          "latitude": None, "longitude": None, **acc.linha(taxa)})  # fmt: skip

    coords: Dict[Tuple[str, int], List[Tuple[float, float]]] = defaultdict(list)
    nomes: Dict[Tuple[str, int], str] = {}
    for (uf, mu, _zo, _lo), c in cadastro.items():
        nomes.setdefault((uf, mu), c.municipio_nome)
        if c.latitude is not None and c.longitude is not None:
            coords[(uf, mu)].append((c.latitude, c.longitude))
    for (uf, mu), acc in sorted(por_mun.items()):
        pts = coords.get((uf, mu))
        lat = round(median(p[0] for p in pts), 5) if pts else None
        lon = round(median(p[1] for p in pts), 5) if pts else None
        agregados.append({"eleicao": eleicao, "nivel": "municipio", "uf": uf, "municipio": mu,
                          "nome": nomes.get((uf, mu), str(mu)), "latitude": lat, "longitude": lon,
                          **acc.linha(None)})  # fmt: skip

    locais = []
    for (uf, mu, zo, lo), acc in sorted(por_local.items()):
        if not acc.votos_apos_17:
            continue
        c = cadastro.get((uf, mu, zo, lo))
        linha = acc.linha(None)
        locais.append({
            "eleicao": eleicao, "uf": uf, "municipio": mu, "zona": zo, "local": lo,
            "nome": c.nome if c else None, "bairro": c.bairro if c else None,
            "endereco": c.endereco if c else None,
            "latitude": c.latitude if c else None, "longitude": c.longitude if c else None,
            **{k: linha[k] for k in ("secoes", "comparecimento", "votos", "secoes_voto_apos_17", "votos_apos_17",
                                     "de_17_a_19", "apos_19", "ultimo_voto", "encerramento_max")},
        })  # fmt: skip
    return agregados, locais


# --- cadastro de locais e carga ---------------------------------------------------


def _coordenada(txt: str) -> Optional[float]:
    try:
        v = float((txt or "").replace(",", "."))
    except ValueError:
        return None
    return None if v in (-1.0, 0.0) else v


def ler_cadastro(caminho, *, turno: int) -> Dict[Tuple[str, int, int, int], LocalCadastro]:
    """Locais de votacao do `eleitorado_local_votacao_<ano>_*.csv` (Dados
    Abertos do TSE), so do turno pedido. Uma linha por secao no arquivo; aqui
    fica uma por local. O telefone do local nao e lido."""
    import csv

    saida: Dict[Tuple[str, int, int, int], LocalCadastro] = {}
    with open(caminho, encoding="latin-1", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            if int(row["NR_TURNO"]) != turno:
                continue
            chave = (row["SG_UF"].lower(), int(row["CD_MUNICIPIO"]), int(row["NR_ZONA"]), int(row["NR_LOCAL_VOTACAO"]))
            if chave in saida:
                continue
            saida[chave] = LocalCadastro(
                nome=row["NM_LOCAL_VOTACAO"].strip(),
                bairro=row["NM_BAIRRO"].strip(),
                endereco=row["DS_ENDERECO"].strip(),
                latitude=_coordenada(row["NR_LATITUDE"]),
                longitude=_coordenada(row["NR_LONGITUDE"]),
                municipio_nome=row["NM_MUNICIPIO"].strip(),
            )
    return saida


def gravar(db, eleicao: str, agregados: List[dict], locais: List[dict]) -> None:
    """Troca tudo da eleicao numa transacao so (a carga e refeita inteira)."""
    import json

    from sqlalchemy import text

    jsonb = "CAST(:dados AS JSONB)" if db.get_bind().dialect.name == "postgresql" else ":dados"
    db.execute(text("DELETE FROM votacao_horario_local WHERE eleicao = :e"), {"e": eleicao})
    db.execute(text("DELETE FROM votacao_horario_agregado WHERE eleicao = :e"), {"e": eleicao})
    colunas_ag = ("eleicao", "nivel", "uf", "municipio", "nome", "latitude", "longitude")
    db.execute(
        text(
            "INSERT INTO votacao_horario_agregado (eleicao, nivel, uf, municipio, nome, latitude, longitude, "
            f"secoes, comparecimento, votos_apos_17, dados) VALUES (:eleicao, :nivel, :uf, :municipio, :nome, "
            f":latitude, :longitude, :secoes, :comparecimento, :votos_apos_17, {jsonb})"
        ),
        [
            {**{k: a[k] for k in colunas_ag}, "secoes": a["secoes"], "comparecimento": a["comparecimento"],
             "votos_apos_17": a["votos_apos_17"],
             "dados": json.dumps({k: v for k, v in a.items() if k not in colunas_ag}, ensure_ascii=False)}
            for a in agregados
        ],
    )  # fmt: skip
    if locais:
        colunas_lo = ("eleicao", "uf", "municipio", "zona", "local", "nome", "bairro", "endereco", "latitude", "longitude")
        db.execute(
            text(
                "INSERT INTO votacao_horario_local (eleicao, uf, municipio, zona, local, nome, bairro, endereco, "
                "latitude, longitude, votos_apos_17, dados) VALUES (:eleicao, :uf, :municipio, :zona, :local, "
                f":nome, :bairro, :endereco, :latitude, :longitude, :votos_apos_17, {jsonb})"
            ),
            [
                {**{k: lo[k] for k in colunas_lo}, "votos_apos_17": lo["votos_apos_17"],
                 "dados": json.dumps({k: v for k, v in lo.items() if k not in colunas_lo}, ensure_ascii=False)}
                for lo in locais
            ],
        )  # fmt: skip
    db.commit()


# --- resultado por local de votacao (CS-164) -------------------------------------------
# Os mapas de resultado mostram, no zoom de cidade, um ponto por local de votacao
# (escola). Vem do mesmo BU: soma das secoes do local. Finalistas 13 e 22.

FINALISTAS = ("13", "22")


def resultado_por_local(
    secoes: Iterable[SecaoBu], cadastro: Dict[Tuple[str, int, int, int], LocalCadastro]
) -> List[dict]:
    locais: Dict[Tuple[str, int, int, int], dict] = {}
    for s in secoes:
        chave = (s.uf, s.municipio, s.zona, s.local)
        l = locais.get(chave)
        if l is None:
            c = cadastro.get(chave)
            l = {
                "uf": s.uf, "municipio": s.municipio, "zona": s.zona, "local": s.local,
                "nome": c.nome if c else None, "bairro": c.bairro if c else None,
                "endereco": c.endereco if c else None, "secoes": 0,
                "latitude": c.latitude if c else None, "longitude": c.longitude if c else None,
                "comparecimento": 0, "validos": 0, "votos_13": 0, "votos_22": 0,
            }  # fmt: skip
            locais[chave] = l
        l["secoes"] += 1
        l["comparecimento"] += s.comparecimento
        l["validos"] += sum(v for k, v in s.votos.items() if k not in ("branco", "nulo"))
        l["votos_13"] += s.votos.get(FINALISTAS[0], 0)
        l["votos_22"] += s.votos.get(FINALISTAS[1], 0)
    return [locais[k] for k in sorted(locais)]


def gravar_resultado_local(db, eleicao: str, linhas: List[dict]) -> None:
    """Troca os locais da eleicao em `resultado_local_votacao`."""
    from sqlalchemy import text

    db.execute(text("DELETE FROM resultado_local_votacao WHERE eleicao = :e"), {"e": eleicao})
    if linhas:
        db.execute(
            text(
                "INSERT INTO resultado_local_votacao (eleicao, uf, municipio, zona, local, nome, bairro, endereco, "
                "secoes, latitude, longitude, comparecimento, validos, votos_13, votos_22) VALUES (:eleicao, :uf, "
                ":municipio, :zona, :local, :nome, :bairro, :endereco, :secoes, :latitude, :longitude, "
                ":comparecimento, :validos, :votos_13, :votos_22)"
            ),
            [{"eleicao": eleicao, **l} for l in linhas],
        )
    db.commit()
