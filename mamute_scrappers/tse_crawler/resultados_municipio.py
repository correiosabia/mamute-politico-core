"""Coleta do resultado por municipio nos dois turnos (CS-160).

Base da comparacao entre o 1o e o 2o turno de presidente por regiao. A cada
rodada:

1. le `ele-c.json` e acha as eleicoes do ciclo que tem o cargo (presidente:
   6257 no 1o turno, 6258 no 2o em 2026);
2. baixa a lista de municipios de cada eleicao (inclui o exterior, `zz`);
3. baixa o arquivo de cada municipio que ainda nao esta totalizado na base
   (com `--refazer`, todos: o TSE republica arquivo ja totalizado, como no
   RJ em 10/10/2026) e grava resumo + votos por candidato;
4. carrega `ibge_municipio` (regiao intermediaria) se estiver vazia.

Os downloads correm em paralelo (`--workers`); a gravacao e na thread
principal, com a mesma sessao.

Uso:
python -m mamute_scrappers.tse_crawler.resultados_municipio [--refazer] [--workers 8] [--cargo 1]
"""

from __future__ import annotations

import argparse
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, List, Optional

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

from mamute_scrappers.tse_crawler.resultados import (
    DEFAULT_BASE,
    DEFAULT_CICLO,
    HttpGet,
    TseIndisponivel,
    http_get_json,
)
from mamute_scrappers.tse_crawler.resultados_parsing import (
    CARGO_PRESIDENTE,
    MunicipioTse,
    ResumoMunicipio,
    config_url,
    municipio_file_url,
    municipios_url,
    parse_config,
    parse_ibge,
    parse_municipios,
    parse_resumo_municipio,
)

logger = logging.getLogger(__name__)

IBGE_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"
DEFAULT_WORKERS = 8


@dataclass
class ColetaMunicipioStats:
    eleicoes: int = 0
    municipios: int = 0
    baixados: int = 0
    ja_finais: int = 0
    ausentes: int = 0
    finais: int = 0
    erros: int = 0
    ibge_carregados: int = 0

    def resumo(self) -> str:
        return (
            f"eleicoes={self.eleicoes} municipios={self.municipios} baixados={self.baixados} "
            f"ja_finais={self.ja_finais} ausentes={self.ausentes} finais={self.finais} "
            f"erros={self.erros} ibge={self.ibge_carregados}"
        )


def ibge_get_padrao() -> list:
    response = requests.get(IBGE_URL, timeout=120)
    response.raise_for_status()
    return response.json()


def carregar_ibge(session: Session, ibge_get: Callable[[], list]) -> int:
    """Carrega `ibge_municipio` uma vez (tabela vazia). Devolve quantos gravou."""
    if session.execute(text("SELECT count(*) FROM ibge_municipio")).scalar():
        return 0
    linhas = parse_ibge(ibge_get())
    if not linhas:
        return 0
    session.execute(
        text(
            "INSERT INTO ibge_municipio (cd_ibge, nome, uf, regiao_intermediaria_id, "
            "regiao_intermediaria_nome, regiao_imediata_id, regiao_imediata_nome, macrorregiao) "
            "VALUES (:cd_ibge, :nome, :uf, :regiao_intermediaria_id, :regiao_intermediaria_nome, "
            ":regiao_imediata_id, :regiao_imediata_nome, :macrorregiao) "
            "ON CONFLICT (cd_ibge) DO NOTHING"
        ),
        linhas,
    )
    session.commit()
    return len(linhas)


def _ja_finais(session: Session, codigo_eleicao: int, cargo: int) -> set[str]:
    rows = session.execute(
        text(
            "SELECT cd_tse FROM tse_resultado_municipio "
            "WHERE codigo_eleicao = :e AND cargo_codigo = :c AND totalizacao_final"
        ),
        {"e": codigo_eleicao, "c": cargo},
    ).all()
    return {r.cd_tse for r in rows}


_UPSERT = text(
    """
    INSERT INTO tse_resultado_municipio
        (ciclo, codigo_eleicao, turno, cargo_codigo, uf, cd_tse, cd_ibge, nome, capital,
         eleitorado, comparecimento, abstencao, brancos, nulos, validos,
         percentual_apurado, totalizacao_final, tse_atualizado_em, coletado_em)
    VALUES
        (:ciclo, :codigo_eleicao, :turno, :cargo, :uf, :cd_tse, :cd_ibge, :nome, :capital,
         :eleitorado, :comparecimento, :abstencao, :brancos, :nulos, :validos,
         :apurado, :final, :tse_atualizado_em, :agora)
    ON CONFLICT (codigo_eleicao, cargo_codigo, cd_tse) DO UPDATE SET
        turno = excluded.turno, uf = excluded.uf, cd_ibge = excluded.cd_ibge,
        nome = excluded.nome, capital = excluded.capital,
        eleitorado = excluded.eleitorado, comparecimento = excluded.comparecimento,
        abstencao = excluded.abstencao, brancos = excluded.brancos, nulos = excluded.nulos,
        validos = excluded.validos, percentual_apurado = excluded.percentual_apurado,
        totalizacao_final = excluded.totalizacao_final,
        tse_atualizado_em = excluded.tse_atualizado_em, coletado_em = excluded.coletado_em
    """
)


def _gravar(
    session: Session,
    *,
    ciclo: str,
    codigo_eleicao: int,
    turno: int,
    cargo: int,
    municipio: MunicipioTse,
    resumo: ResumoMunicipio,
) -> None:
    session.execute(
        _UPSERT,
        {
            "ciclo": ciclo,
            "codigo_eleicao": codigo_eleicao,
            "turno": turno,
            "cargo": cargo,
            "uf": municipio.uf,
            "cd_tse": municipio.cd_tse,
            "cd_ibge": municipio.cd_ibge,
            "nome": municipio.nome,
            "capital": municipio.capital,
            "eleitorado": resumo.eleitorado,
            "comparecimento": resumo.comparecimento,
            "abstencao": resumo.abstencao,
            "brancos": resumo.brancos,
            "nulos": resumo.nulos,
            "validos": resumo.validos,
            "apurado": float(resumo.percentual_apurado) if resumo.percentual_apurado is not None else None,
            "final": resumo.final,
            "tse_atualizado_em": resumo.atualizado_em,
            "agora": datetime.now(timezone.utc),
        },
    )
    resultado_id = session.execute(
        text(
            "SELECT id FROM tse_resultado_municipio "
            "WHERE codigo_eleicao = :e AND cargo_codigo = :c AND cd_tse = :m"
        ),
        {"e": codigo_eleicao, "c": cargo, "m": municipio.cd_tse},
    ).scalar_one()
    session.execute(
        text("DELETE FROM tse_resultado_municipio_candidato WHERE resultado_id = :id"),
        {"id": resultado_id},
    )
    if resumo.candidatos:
        session.execute(
            text(
                "INSERT INTO tse_resultado_municipio_candidato (resultado_id, sqcand, numero, nome_urna, votos) "
                "VALUES (:id, :sqcand, :numero, :nome, :votos)"
            ),
            [
                {"id": resultado_id, "sqcand": c.sqcand, "numero": c.numero, "nome": c.nome_urna, "votos": c.votos}
                for c in resumo.candidatos
            ],
        )


def _baixar(http_get: HttpGet, url: str) -> tuple[Optional[dict], bool]:
    """(payload, erro). Falha persistente do TSE vira erro do arquivo, nao da rodada."""
    try:
        return http_get(url), False
    except TseIndisponivel as exc:
        logger.warning("TSE indisponivel: %s", exc)
        return None, True


def coletar_municipios(
    session: Session,
    *,
    base: str = DEFAULT_BASE,
    ciclo: str = DEFAULT_CICLO,
    cargo: int = CARGO_PRESIDENTE,
    http_get: HttpGet = http_get_json,
    ibge_get: Optional[Callable[[], list]] = None,
    refazer: bool = False,
    workers: int = DEFAULT_WORKERS,
) -> ColetaMunicipioStats:
    stats = ColetaMunicipioStats()
    stats.ibge_carregados = carregar_ibge(session, ibge_get or ibge_get_padrao)

    config = http_get(config_url(base))
    if config is None:
        raise TseIndisponivel(f"{config_url(base)}: configuracao ausente")
    eleicoes = [e for e in parse_config(config, ciclo) if cargo in e.cargos]
    stats.eleicoes = len(eleicoes)

    for eleicao in eleicoes:
        lista = http_get(municipios_url(base, ciclo, eleicao.codigo))
        if lista is None:
            logger.info("Sem lista de municipios para a eleicao %s", eleicao.codigo)
            continue
        municipios = parse_municipios(lista)
        stats.municipios += len(municipios)
        finais = set() if refazer else _ja_finais(session, eleicao.codigo, cargo)
        pendentes: List[MunicipioTse] = []
        for municipio in municipios:
            if municipio.cd_tse in finais:
                stats.ja_finais += 1
            else:
                pendentes.append(municipio)

        urls = [
            municipio_file_url(base, ciclo, eleicao.codigo, m.uf, m.cd_tse, cargo) for m in pendentes
        ]
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for municipio, (payload, erro) in zip(pendentes, pool.map(lambda u: _baixar(http_get, u), urls)):
                if erro:
                    stats.erros += 1
                    continue
                if payload is None:
                    stats.ausentes += 1
                    continue
                resumo = parse_resumo_municipio(payload)
                _gravar(
                    session,
                    ciclo=ciclo,
                    codigo_eleicao=eleicao.codigo,
                    turno=eleicao.turno,
                    cargo=cargo,
                    municipio=municipio,
                    resumo=resumo,
                )
                stats.baixados += 1
                if resumo.final:
                    stats.finais += 1
                if stats.baixados % 500 == 0:
                    session.commit()
        session.commit()
    return stats


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coleta o resultado por municipio (TSE).")
    parser.add_argument("--refazer", action="store_true", help="Rebaixa tambem os ja totalizados.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--cargo", type=int, default=CARGO_PRESIDENTE)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    from mamute_scrappers.tse_crawler.candidacy import _load_env_file

    _load_env_file()
    from mamute_scrappers.db.session import get_session
    from mamute_scrappers.tse_crawler.transferencia_votos import calcular_estimativas

    base = os.getenv("MAMUTE_TSE_RESULTADOS_BASE", DEFAULT_BASE).strip() or DEFAULT_BASE
    ciclo = os.getenv("MAMUTE_TSE_RESULTADOS_CICLO", DEFAULT_CICLO).strip() or DEFAULT_CICLO

    session = get_session()
    try:
        stats = coletar_municipios(
            session, base=base, ciclo=ciclo, cargo=args.cargo, refazer=args.refazer, workers=args.workers
        )
        logger.info("Resultado por municipio %s: %s", ciclo, stats.resumo())
        # Barato (numpy sobre ~5,7 mil linhas) e so grava com tudo totalizado.
        gravadas = calcular_estimativas(session, ciclo=ciclo, cargo=args.cargo)
        logger.info("Estimativa de transferencia: %s linhas", gravadas)
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
