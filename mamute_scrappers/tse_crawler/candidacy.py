"""Coleta de candidaturas da Eleicao Geral (TSE/DivulgaCandContas) — CS-16.

Fluxo incremental: as listagens UF x cargo (~136 requests) rodam sempre; o
detalhe (que traz CPF e foto) so e buscado para candidatura nova ou cujo
fingerprint de listagem mudou. O fingerprint so e persistido quando o detalhe
foi lido com sucesso, entao falha de detalhe se auto-corrige na proxima
execucao.

Candidatura nunca e deletada: se sumir da listagem, a situacao muda pelo
proprio TSE (indeferido, renuncia, cassacao) e o historico fica.
"""

from __future__ import annotations

import logging
import sys
from collections import Counter
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mamute_scrappers.tse_crawler.client import DivulgaCandClient  # noqa: E402
from mamute_scrappers.tse_crawler.matching import (  # noqa: E402
    MATCH_STATUS_MANUAL,
    ParliamentarianRecord,
    build_index,
    match_candidacy,
)
from mamute_scrappers.tse_crawler.parsing import (  # noqa: E402
    build_listing_payload,
    compute_listing_fingerprint,
    merge_detail_payload,
)

logger = logging.getLogger(__name__)


UFS = (
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
    "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC",
    "SE", "SP", "TO",
)

# (codigo do cargo na DivulgaCandContas, nome, UFs onde o cargo existe).
# So titulares: vices e suplentes nao aparecem nas listagens destes cargos.
OFFICES = (
    (1, "Presidente", ("BR",)),
    (3, "Governador", UFS),
    (5, "Senador", UFS),
    (6, "Deputado Federal", UFS),
    (7, "Deputado Estadual", tuple(uf for uf in UFS if uf != "DF")),
    (8, "Deputado Distrital", ("DF",)),
)

Candidacy: Any = None

_LISTING_FIELDS = (
    "office_code",
    "office",
    "state",
    "ballot_number",
    "ballot_name",
    "full_name",
    "party",
    "coalition",
    "status",
    "totalization_status",
)

# So atualizados quando presentes no payload (ou seja, quando o detalhe foi
# lido nesta execucao). Um payload sem detalhe nao pode apagar o que ja havia.
_DETAIL_FIELDS = (
    "cpf",
    "voter_id",
    "photo_url",
    "tse_last_update",
    "details",
    "listing_fingerprint",
    # Perfil demografico (CS-63), extraido do mesmo payload de detalhe.
    "birth_date",
    "gender",
    "race",
    "education",
    "occupation",
    "marital_status",
    "nationality",
    "federation",
    "profile_source",
)


def _load_env_file() -> None:
    """Carrega o .env antes de ler o banco (mesma politica de emendas.py)."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover — dotenv e dependencia declarada
        return

    for env_file in (
        PROJECT_ROOT / "mamute_scrappers" / ".env",
        PROJECT_ROOT / ".env",
        Path.cwd() / ".env",
    ):
        if env_file.exists():
            load_dotenv(env_file, override=False)


def _ensure_model() -> None:
    global Candidacy
    if Candidacy is not None:
        return
    from mamute_scrappers.db.models import Candidacy as CandidacyRuntime

    Candidacy = CandidacyRuntime


def upsert_candidacy(session: Any, payload: Dict[str, Any]) -> Tuple[Any, bool]:
    """Grava ou atualiza uma candidatura pela chave natural do TSE.

    Campos de listagem sempre sao atualizados. Campos de detalhe so quando
    presentes no payload. `manual` no match_status prevalece sobre o robo.
    """
    if Candidacy is None:
        _ensure_model()

    record = (
        session.query(Candidacy)
        .filter(
            Candidacy.election_year == payload["election_year"],
            Candidacy.tse_candidate_id == payload["tse_candidate_id"],
        )
        .one_or_none()
    )

    created = False
    if record is None:
        record = Candidacy(
            election_year=payload["election_year"],
            tse_candidate_id=payload["tse_candidate_id"],
        )
        session.add(record)
        created = True

    for field in _LISTING_FIELDS:
        setattr(record, field, payload.get(field))

    for field in _DETAIL_FIELDS:
        if field in payload:
            setattr(record, field, payload[field])

    if record.match_status != MATCH_STATUS_MANUAL:
        record.parliamentarian_id = payload.get("parliamentarian_id")
        record.match_status = payload.get("match_status")

    if created:
        # flush antes do proximo lookup no mesmo lote (autoflush=False na
        # sessao de producao); sem isso, repeticao no lote viraria duplicata.
        session.flush()

    return record, created


def _load_parliamentarian_index():
    from mamute_scrappers.db import session_scope
    from mamute_scrappers.db.models import Parliamentarian

    with session_scope() as session:
        rows = session.query(
            Parliamentarian.id,
            Parliamentarian.name,
            Parliamentarian.full_name,
            Parliamentarian.cpf,
            Parliamentarian.state_elected,
        ).all()

    return build_index(
        [
            ParliamentarianRecord(
                id=r[0], name=r[1], full_name=r[2], cpf=r[3], state_elected=r[4]
            )
            for r in rows
        ]
    )


def _load_known_fingerprints(year: int) -> Dict[int, Optional[str]]:
    _ensure_model()
    from mamute_scrappers.db import session_scope

    with session_scope() as session:
        rows = (
            session.query(Candidacy.tse_candidate_id, Candidacy.listing_fingerprint)
            .filter(Candidacy.election_year == year)
            .all()
        )
    return {r[0]: r[1] for r in rows}


def run(
    *,
    ano: Optional[int] = None,
    persist: bool = True,
    dry_run_limit: Optional[int] = None,
    max_details: Optional[int] = None,
) -> None:
    ano = ano or date.today().year
    _load_env_file()
    client = DivulgaCandClient()

    election_id = client.find_general_election_id(ano)
    if election_id is None:
        logger.error("Nenhuma eleicao geral encontrada para %s.", ano)
        raise SystemExit(1)
    logger.info("Eleicao geral %s: id %s", ano, election_id)

    index = _load_parliamentarian_index()
    known = _load_known_fingerprints(ano)
    logger.info("Candidaturas ja conhecidas: %s", len(known))

    if persist:
        _ensure_model()
        from mamute_scrappers.db import session_scope

        session_context = session_scope()
    else:
        session_context = nullcontext(None)

    status_counter: Counter = Counter()
    total = 0
    unchanged = 0
    processed = 0
    inserted = 0
    updated = 0
    details_fetched = 0
    details_failed = 0

    with session_context as session:
        for office_code, office_name, states in OFFICES:
            for uf in states:
                candidates = client.list_candidates(ano, uf, election_id, office_code)
                for item in candidates:
                    total += 1
                    payload = build_listing_payload(
                        item,
                        election_year=ano,
                        office_code=office_code,
                        office_name=office_name,
                        state=uf,
                    )
                    if payload is None:
                        continue

                    fingerprint = compute_listing_fingerprint(item)
                    tse_id = payload["tse_candidate_id"]
                    if known.get(tse_id) == fingerprint:
                        unchanged += 1
                        continue

                    detail = None
                    if max_details is None or details_fetched < max_details:
                        details_fetched += 1
                        detail = client.get_candidate_detail(
                            ano, uf, election_id, tse_id
                        )
                        if detail is None:
                            details_failed += 1

                    if detail is not None:
                        payload = merge_detail_payload(payload, detail)
                        payload["listing_fingerprint"] = fingerprint

                    result = match_candidacy(
                        cpf=payload.get("cpf"),
                        full_name=payload.get("full_name"),
                        ballot_name=payload.get("ballot_name"),
                        state=uf,
                        index=index,
                    )
                    payload["parliamentarian_id"] = result.parliamentarian_id
                    payload["match_status"] = result.status
                    status_counter[result.status] += 1
                    processed += 1

                    if session is not None:
                        _, created = upsert_candidacy(session, payload)
                        if created:
                            inserted += 1
                        else:
                            updated += 1
                        known[tse_id] = payload.get("listing_fingerprint")
                        # Commit a cada gravacao: a proxima linha pode chamar o
                        # TSE, e esperar a resposta com transacao aberta trava o
                        # autovacuum e segura lock (CS-126). Tambem garante que a
                        # carga inicial (~29k) nao some por falha de rede no fim.
                        session.commit()

                    if dry_run_limit is not None and processed >= dry_run_limit:
                        _log_summary(
                            ano, total, unchanged, processed, inserted, updated,
                            details_fetched, details_failed, status_counter,
                            persist,
                        )
                        return

    _log_summary(
        ano, total, unchanged, processed, inserted, updated,
        details_fetched, details_failed, status_counter, persist,
    )


def _log_summary(
    ano, total, unchanged, processed, inserted, updated,
    details_fetched, details_failed, status_counter, persist,
) -> None:
    logger.info("=== Candidaturas %s ===", ano)
    logger.info(
        "Listados: %s | sem mudanca: %s | processados: %s",
        total, unchanged, processed,
    )
    logger.info(
        "Detalhes buscados: %s (falhas: %s)", details_fetched, details_failed
    )
    logger.info("Casamento: %s", dict(status_counter))
    if persist:
        logger.info("Persistencia: %s inseridos, %s atualizados.", inserted, updated)
    else:
        logger.info("Modo dry-run: nada foi gravado.")


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Coleta candidaturas da Eleicao Geral na DivulgaCandContas."
    )
    parser.add_argument(
        "--ano", type=int, help="Ano da eleicao (default: ano corrente)."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Nao persiste; apenas reporta."
    )
    parser.add_argument(
        "--limit", type=int, help="Interrompe apos N candidaturas processadas."
    )
    parser.add_argument(
        "--max-details",
        type=int,
        help=(
            "Teto de buscas de detalhe nesta execucao; o excedente fica sem "
            "fingerprint e e retomado na proxima."
        ),
    )

    args = parser.parse_args()
    run(
        ano=args.ano,
        persist=not args.dry_run,
        dry_run_limit=args.limit,
        max_details=args.max_details,
    )
