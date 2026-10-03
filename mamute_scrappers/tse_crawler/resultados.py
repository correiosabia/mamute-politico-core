"""Coleta do resultado oficial da eleicao (CS-106).

Fonte: JSONs de divulgacao do TSE (`resultados.tse.jus.br`). A cada rodada:

1. le `ele-c.json` e descobre as eleicoes do ciclo (codigos, turno, data);
2. pula eleicao cuja data ainda nao chegou (antes do pleito os arquivos
   existem, mas zerados);
3. baixa cada arquivo UF x cargo que ainda nao estiver com a totalizacao
   encerrada no banco (`tse_result_file.totalizacao_final`);
4. grava o estado do arquivo e a situacao de cada candidato que casa com
   `candidacy` (`sqcand` = `tse_candidate_id`, no ano do ciclo).

Arquivo encerrado nao e baixado de novo, entao depois da apuracao a rodada
fica barata. Rodar varias vezes e seguro: tudo e upsert.

Config por env (prefixo MAMUTE_, que o entrypoint repassa ao cron):
  MAMUTE_TSE_RESULTADOS_BASE   default https://resultados.tse.jus.br/oficial
  MAMUTE_TSE_RESULTADOS_CICLO  default ele2026
Para testar contra o simulado:
  MAMUTE_TSE_RESULTADOS_BASE=https://resultados-sim.tse.jus.br/simulado/simulado2026

Uso:
  python -m mamute_scrappers.tse_crawler.resultados [--dry-run] [--ignorar-data]
         [--uf SP] [--cargo 6]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Set, Tuple

import requests
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mamute_scrappers.tse_crawler.resultados_parsing import (  # noqa: E402
    TZ_BRASILIA,
    arquivos_da_eleicao,
    config_url,
    foi_eleito,
    parse_config,
    parse_result_file,
    result_file_url,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://resultados.tse.jus.br/oficial"
DEFAULT_CICLO = "ele2026"
REQUEST_TIMEOUT = 60
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 5
REQUEST_DELAY = 0.2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

# Devolve o JSON, ou None quando o arquivo nao existe (404).
HttpGet = Callable[[str], Optional[dict]]


class TseIndisponivel(RuntimeError):
    """Falha persistente de rede/servidor: a rodada para e o cron tenta de novo."""


def http_get_json(url: str) -> Optional[dict]:
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            # O bucket do TSE responde 403/404 para arquivo inexistente.
            if response.status_code in (403, 404):
                return None
            response.raise_for_status()
            if REQUEST_DELAY:
                time.sleep(REQUEST_DELAY)
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise TseIndisponivel(f"{url}: {last_error}")


def ano_do_ciclo(ciclo: str) -> int:
    digits = "".join(ch for ch in ciclo if ch.isdigit())
    return int(digits)


@dataclass
class ColetaStats:
    eleicoes: int = 0
    eleicoes_futuras: int = 0
    arquivos_baixados: int = 0
    arquivos_ausentes: int = 0
    arquivos_ja_finais: int = 0
    arquivos_finais_agora: int = 0
    candidatos_no_tse: int = 0
    candidatos_casados: int = 0
    sqcands_vistos: Set[int] = field(default_factory=set)

    def resumo(self) -> str:
        return (
            f"eleicoes={self.eleicoes} (futuras={self.eleicoes_futuras}) "
            f"baixados={self.arquivos_baixados} ausentes={self.arquivos_ausentes} "
            f"ja_finais={self.arquivos_ja_finais} finais_agora={self.arquivos_finais_agora} "
            f"candidatos_tse={self.candidatos_no_tse} casados={self.candidatos_casados}"
        )


def _mapa_candidaturas(session: Session, ano: int) -> Dict[int, int]:
    rows = session.execute(
        text("SELECT id, tse_candidate_id FROM candidacy WHERE election_year = :ano"),
        {"ano": ano},
    ).all()
    return {int(r.tse_candidate_id): int(r.id) for r in rows}


def _tem_coluna_eleitos(session: Session) -> bool:
    """`eleitos_no_arquivo` (CS-107) ainda nao existe na janela do deploy
    antes da migration cs107a1b2c3d4; ate la a coleta segue sem ela."""
    try:
        colunas = inspect(session.get_bind()).get_columns("tse_result_file")
    except SQLAlchemyError:
        return True
    return any(c.get("name") == "eleitos_no_arquivo" for c in colunas)


def _arquivos_finais(
    session: Session, ciclo: str, *, com_eleitos: bool
) -> Set[Tuple[int, str, int]]:
    sem_contagem = " AND eleitos_no_arquivo IS NOT NULL" if com_eleitos else ""
    rows = session.execute(
        text(
            "SELECT codigo_eleicao, uf, cargo_codigo FROM tse_result_file "
            "WHERE ciclo = :ciclo AND totalizacao_final" + sem_contagem
        ),
        {"ciclo": ciclo},
    ).all()
    return {(int(r.codigo_eleicao), r.uf, int(r.cargo_codigo)) for r in rows}


def _upsert_arquivo(*, com_eleitos: bool):
    coluna = ", eleitos_no_arquivo" if com_eleitos else ""
    valor = ", :eleitos" if com_eleitos else ""
    atualiza = "eleitos_no_arquivo = excluded.eleitos_no_arquivo," if com_eleitos else ""
    return text(
        f"""
        INSERT INTO tse_result_file
            (ciclo, codigo_eleicao, turno, uf, cargo_codigo, totalizacao_final,
             tse_atualizado_em, candidatos_no_arquivo, candidatos_casados,
             coletado_em{coluna})
        VALUES
            (:ciclo, :codigo_eleicao, :turno, :uf, :cargo_codigo, :final,
             :atualizado_em, :no_arquivo, :casados, :agora{valor})
        ON CONFLICT (codigo_eleicao, uf, cargo_codigo) DO UPDATE SET
            turno = excluded.turno,
            totalizacao_final = excluded.totalizacao_final,
            tse_atualizado_em = excluded.tse_atualizado_em,
            candidatos_no_arquivo = excluded.candidatos_no_arquivo,
            candidatos_casados = excluded.candidatos_casados,
            {atualiza}
            coletado_em = excluded.coletado_em
        """
    )

_UPSERT_RESULTADO = text(
    """
    INSERT INTO candidacy_result
        (candidacy_id, turno, codigo_eleicao, situacao, eleito, votos,
         percentual, destinacao_voto, totalizacao_final, tse_atualizado_em,
         coletado_em)
    VALUES
        (:candidacy_id, :turno, :codigo_eleicao, :situacao, :eleito, :votos,
         :percentual, :destinacao_voto, :final, :atualizado_em, :agora)
    ON CONFLICT (candidacy_id, turno) DO UPDATE SET
        codigo_eleicao = excluded.codigo_eleicao,
        situacao = excluded.situacao,
        eleito = excluded.eleito,
        votos = excluded.votos,
        percentual = excluded.percentual,
        destinacao_voto = excluded.destinacao_voto,
        totalizacao_final = excluded.totalizacao_final,
        tse_atualizado_em = excluded.tse_atualizado_em,
        coletado_em = excluded.coletado_em
    """
)


def coletar(
    session: Session,
    *,
    base: str = DEFAULT_BASE,
    ciclo: str = DEFAULT_CICLO,
    http_get: HttpGet = http_get_json,
    hoje: Optional[date] = None,
    ignorar_data: bool = False,
    dry_run: bool = False,
    ufs: Optional[Iterable[str]] = None,
    cargos: Optional[Iterable[int]] = None,
) -> ColetaStats:
    stats = ColetaStats()
    hoje = hoje or datetime.now(TZ_BRASILIA).date()
    filtro_ufs = {u.lower() for u in ufs} if ufs else None
    filtro_cargos = set(cargos) if cargos else None

    config = http_get(config_url(base))
    if config is None:
        raise TseIndisponivel(f"configuracao ausente em {config_url(base)}")
    eleicoes = parse_config(config, ciclo)
    stats.eleicoes = len(eleicoes)
    if not eleicoes:
        logger.warning("Nenhuma eleicao do ciclo %s em %s", ciclo, config_url(base))
        return stats

    candidaturas = _mapa_candidaturas(session, ano_do_ciclo(ciclo))
    com_eleitos = _tem_coluna_eleitos(session)
    ja_finais = _arquivos_finais(session, ciclo, com_eleitos=com_eleitos)
    upsert_arquivo = _upsert_arquivo(com_eleitos=com_eleitos)

    for eleicao in eleicoes:
        if eleicao.data > hoje and not ignorar_data:
            stats.eleicoes_futuras += 1
            logger.info("Eleicao %s (turno %s) so em %s; pulando.", eleicao.codigo, eleicao.turno, eleicao.data)
            continue
        for uf, cargo in arquivos_da_eleicao(eleicao):
            if filtro_ufs is not None and uf not in filtro_ufs:
                continue
            if filtro_cargos is not None and cargo not in filtro_cargos:
                continue
            if (eleicao.codigo, uf, cargo) in ja_finais:
                stats.arquivos_ja_finais += 1
                continue

            url = result_file_url(base, ciclo, eleicao.codigo, uf, cargo)
            payload = http_get(url)
            if payload is None:
                stats.arquivos_ausentes += 1
                logger.info("Sem arquivo: %s", url)
                continue
            stats.arquivos_baixados += 1

            arquivo = parse_result_file(payload)
            casados = [
                (candidaturas[c.sqcand], c)
                for c in arquivo.candidatos
                if c.sqcand in candidaturas
            ]
            stats.candidatos_no_tse += len(arquivo.candidatos)
            stats.candidatos_casados += len(casados)
            stats.sqcands_vistos.update(c.sqcand for c in arquivo.candidatos)
            if arquivo.final:
                stats.arquivos_finais_agora += 1
            if len(casados) < len(arquivo.candidatos):
                logger.info(
                    "%s/%s c%s: %s de %s candidatos sem candidatura na base",
                    eleicao.codigo, uf, cargo,
                    len(arquivo.candidatos) - len(casados), len(arquivo.candidatos),
                )
            if dry_run:
                continue

            agora = datetime.now(timezone.utc)
            session.execute(
                upsert_arquivo,
                {
                    "ciclo": ciclo,
                    "codigo_eleicao": eleicao.codigo,
                    "turno": eleicao.turno,
                    "uf": uf,
                    "cargo_codigo": cargo,
                    "final": arquivo.final,
                    "atualizado_em": arquivo.atualizado_em,
                    "no_arquivo": len(arquivo.candidatos),
                    "casados": len(casados),
                    "eleitos": sum(1 for c in arquivo.candidatos if foi_eleito(c)),
                    "agora": agora,
                },
            )
            if casados:
                session.execute(
                    _UPSERT_RESULTADO,
                    [
                        {
                            "candidacy_id": candidacy_id,
                            "turno": eleicao.turno,
                            "codigo_eleicao": eleicao.codigo,
                            "situacao": c.situacao,
                            "eleito": c.eleito,
                            "votos": c.votos,
                            "percentual": float(c.percentual) if c.percentual is not None else None,
                            "destinacao_voto": c.destinacao_voto,
                            "final": arquivo.final,
                            "atualizado_em": arquivo.atualizado_em,
                            "agora": agora,
                        }
                        for candidacy_id, c in casados
                    ],
                )
            session.commit()

    return stats


def _cobertura_acompanhadas(session: Session, ano: int, sqcands_vistos: Set[int]) -> str:
    rows = session.execute(
        text(
            """
            SELECT c.tse_candidate_id
              FROM projetos_candidacy pc
              JOIN candidacy c ON c.id = pc.candidacy_id
             WHERE c.election_year = :ano
            """
        ),
        {"ano": ano},
    ).all()
    total = len(rows)
    vistos = sum(1 for r in rows if int(r.tse_candidate_id) in sqcands_vistos)
    return f"acompanhadas_{ano}={total} encontradas_nos_arquivos={vistos}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coleta o resultado oficial da eleicao (TSE).")
    parser.add_argument("--dry-run", action="store_true", help="Baixa e casa, sem gravar.")
    parser.add_argument(
        "--ignorar-data",
        action="store_true",
        help="Baixa mesmo antes da data do pleito (teste de casamento).",
    )
    parser.add_argument("--uf", action="append", help="Restringe a UF (repetivel).")
    parser.add_argument("--cargo", type=int, action="append", help="Restringe ao cargo (repetivel).")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    from mamute_scrappers.tse_crawler.candidacy import _load_env_file

    _load_env_file()
    from mamute_scrappers.db.session import get_session

    base = os.getenv("MAMUTE_TSE_RESULTADOS_BASE", DEFAULT_BASE).strip() or DEFAULT_BASE
    ciclo = os.getenv("MAMUTE_TSE_RESULTADOS_CICLO", DEFAULT_CICLO).strip() or DEFAULT_CICLO

    session = get_session()
    try:
        stats = coletar(
            session,
            base=base,
            ciclo=ciclo,
            ignorar_data=args.ignorar_data,
            dry_run=args.dry_run,
            ufs=args.uf,
            cargos=args.cargo,
        )
        logger.info("Resultado %s @ %s: %s", ciclo, base, stats.resumo())
        if args.dry_run:
            logger.info(_cobertura_acompanhadas(session, ano_do_ciclo(ciclo), stats.sqcands_vistos))
    except TseIndisponivel as exc:
        logger.error("TSE indisponivel, tenta na proxima rodada: %s", exc)
        return 1
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
