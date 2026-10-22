"""Constroi a timeline eleitoral (electoral_history) — CS-54.

Tres fases, todas idempotentes:

1. Semear do JSONB local: `eleicoesAnteriores` de cada candidacy 2026. A fase
   re-roda sempre (barata, zero API), entao o `result` acompanha o TSE e
   candidaturas novas ganham timeline sozinhas. A linha do proprio 2026 ja
   nasce com patrimonio, copiado de candidacy.details (bens ja armazenados).
2. Semear parlamentares sem candidatura 2026: varre listagens das gerais
   2022/2018 (cargos 1/3/5/6), casa por nome+UF com confirmacao de CPF via
   detalhe quando o parlamentar tem CPF, e semeia a timeline completa a
   partir do `eleicoesAnteriores` do detalhe (municipais incluidas).
3. Drenar patrimonio: linhas com assets_fetched_at NULL -> detalhe daquele
   ano/eleicao -> bens. Parlamentares primeiro, depois anos mais recentes.

Nenhuma transacao fica aberta enquanto o job espera o TSE (CS-126): em
04/10/2026 a fase 3 ficou 8 h "idle in transaction", travando o autovacuum e
segurando lock em electoral_history. A fila e lida para a memoria e a
transacao fecha antes da primeira chamada; cada gravacao e comitada na hora.
O job desiste com o TSE fora do ar (falhas seguidas) e tem teto de tempo.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mamute_scrappers.tse_crawler.client import DivulgaCandClient  # noqa: E402
from mamute_scrappers.tse_crawler.history_parsing import (  # noqa: E402
    build_assets_payload,
    build_history_payload,
)
from mamute_scrappers.tse_crawler.parsing import (  # noqa: E402
    normalize_cpf,
    normalize_text,
)

logger = logging.getLogger(__name__)

COMMIT_EVERY = 200
# Cada detalhe que falha custa ate ~4 min (tentativas + espera): com o TSE fora,
# 10 falhas seguidas ja sao sinal suficiente para encerrar e tentar amanha.
MAX_FALHAS_SEGUIDAS = 10
# Paginacao por keyset na fase 1. NAO trocar por yield_per: o commit
# intermediario do upsert fecha a transacao e invalida o named cursor do
# Postgres ("named cursor isn't valid anymore") — derrubou a carga inicial de
# 2026-08-09 aos ~1.6k de ~25k registros. Cada pagina e materializada com
# .all() antes de qualquer commit.
SEED_BATCH_SIZE = 500
SEED_ELECTION_YEARS = (2022, 2018)
# presidente, governador, senador, dep. federal — todo parlamentar em
# exercicio disputou um destes cargos numa geral; estaduais nao sao
# necessarios para encontra-los.
SEED_OFFICE_CODES = (1, 3, 5, 6)

UFS = (
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
    "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC",
    "SE", "SP", "TO",
)

ElectoralHistory: Any = None
Candidacy: Any = None
Parliamentarian: Any = None

_DISPUTE_FIELDS = (
    "tse_election_id",
    "office",
    "state",
    "locality",
    "party",
    "ballot_name",
    "full_name",
    "ballot_number",
    "result",
    "source_link",
)
# Vinculo so e escrito quando o payload traz valor: reseed vindo de outra
# origem (ex.: so candidacy_id) nao pode apagar um parliamentarian_id ja
# resolvido — e vice-versa.
_LINK_FIELDS = ("parliamentarian_id", "candidacy_id")
# Assets so sao escritos quando o payload traz assets_fetched_at: um reseed
# de esqueleto nao pode zerar patrimonio ja coletado.
_ASSET_FIELDS = ("declared_assets", "assets_count", "assets", "assets_fetched_at")


def _load_env_file() -> None:
    """Carrega o .env antes de tocar no banco (mesma politica de emendas.py)."""
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


def _ensure_models() -> None:
    global ElectoralHistory, Candidacy, Parliamentarian
    if ElectoralHistory is not None:
        return
    from mamute_scrappers.db.models import (
        Candidacy as CandidacyRuntime,
        ElectoralHistory as ElectoralHistoryRuntime,
        Parliamentarian as ParliamentarianRuntime,
    )

    ElectoralHistory = ElectoralHistoryRuntime
    Candidacy = CandidacyRuntime
    Parliamentarian = ParliamentarianRuntime


def upsert_history(session: Any, payload: Dict[str, Any]) -> Tuple[Any, bool]:
    """Grava ou atualiza uma linha da timeline pela chave natural do TSE."""
    if ElectoralHistory is None:
        _ensure_models()

    # `state` (o sgUe do TSE) faz parte da chave natural: ate 2008 o
    # tse_candidate_id era sequencial curto e so unico dentro da unidade
    # eleitoral. Sem ele, duas disputas de pessoas diferentes no mesmo ano
    # colidiam nesta linha — e como `_LINK_FIELDS` nunca limpa vinculo, o
    # parliamentarian_id da primeira ficava grudado nos dados da segunda (CS-69).
    record = (
        session.query(ElectoralHistory)
        .filter(
            ElectoralHistory.election_year == payload["election_year"],
            ElectoralHistory.state.is_not_distinct_from(payload.get("state")),
            ElectoralHistory.tse_candidate_id == payload["tse_candidate_id"],
        )
        .one_or_none()
    )

    created = False
    if record is None:
        record = ElectoralHistory(
            election_year=payload["election_year"],
            state=payload.get("state"),
            tse_candidate_id=payload["tse_candidate_id"],
        )
        session.add(record)
        created = True

    for field in _DISPUTE_FIELDS:
        if field in payload:
            setattr(record, field, payload.get(field))

    for field in _LINK_FIELDS:
        if payload.get(field) is not None:
            setattr(record, field, payload[field])

    if "assets_fetched_at" in payload:
        for field in _ASSET_FIELDS:
            setattr(record, field, payload.get(field))

    if created:
        # flush antes do proximo lookup no mesmo lote (autoflush=False na
        # sessao de producao); sem isso, repeticao no lote viraria duplicata.
        session.flush()

    return record, created


def _seed_entries(
    session: Any,
    entries: Any,
    *,
    candidacy_id: Optional[int],
    parliamentarian_id: Optional[int],
    assets_source: Optional[Dict[str, Any]],
    assets_year: Optional[int],
    counters: Dict[str, int],
) -> None:
    """Upsert de uma lista de eleicoesAnteriores; assets so no ano de origem."""
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            counters["malformed"] += 1
            continue
        payload = build_history_payload(
            entry, candidacy_id=candidacy_id, parliamentarian_id=parliamentarian_id
        )
        if payload is None:
            counters["malformed"] += 1
            logger.warning("Entrada de historico malformada ignorada: %s", entry)
            continue
        if assets_source is not None and payload["election_year"] == assets_year:
            payload.update(build_assets_payload(assets_source))
            payload["assets_fetched_at"] = datetime.utcnow()
        _, created = upsert_history(session, payload)
        counters["created" if created else "updated"] += 1
        if (counters["created"] + counters["updated"]) % COMMIT_EVERY == 0:
            session.commit()


def seed_from_candidacies(session: Any) -> Dict[str, int]:
    """Fase 1: timeline de todo candidato 2026 a partir do JSONB ja local."""
    _ensure_models()
    counters = {"created": 0, "updated": 0, "malformed": 0, "candidacies": 0}

    last_id = 0
    while True:
        batch = (
            session.query(
                Candidacy.id,
                Candidacy.parliamentarian_id,
                Candidacy.election_year,
                Candidacy.details,
            )
            .filter(Candidacy.details.isnot(None), Candidacy.id > last_id)
            .order_by(Candidacy.id)
            .limit(SEED_BATCH_SIZE)
            .all()
        )
        if not batch:
            break
        for cand_id, parliamentarian_id, election_year, details in batch:
            last_id = cand_id
            if not isinstance(details, dict):
                continue
            counters["candidacies"] += 1
            _seed_entries(
                session,
                details.get("eleicoesAnteriores"),
                candidacy_id=cand_id,
                parliamentarian_id=parliamentarian_id,
                assets_source=details,
                assets_year=election_year,
                counters=counters,
            )
    session.commit()
    return counters


def _pending_parliamentarians(session: Any) -> List[Any]:
    """Parlamentares sem nenhuma linha de timeline."""
    subquery = session.query(ElectoralHistory.parliamentarian_id).filter(
        ElectoralHistory.parliamentarian_id.isnot(None)
    )
    return (
        session.query(
            Parliamentarian.id,
            Parliamentarian.name,
            Parliamentarian.full_name,
            Parliamentarian.cpf,
            Parliamentarian.state_elected,
        )
        .filter(~Parliamentarian.id.in_(subquery))
        .all()
    )


def seed_missing_parliamentarians(
    session: Any, client: DivulgaCandClient
) -> Dict[str, int]:
    """Fase 2: parlamentares sem candidatura 2026, via gerais 2022/2018."""
    _ensure_models()
    counters = {
        "created": 0,
        "updated": 0,
        "malformed": 0,
        "pending_initial": 0,
        "seeded": 0,
        "cpf_rejected": 0,
        "still_pending": 0,
    }

    pending: Dict[int, Any] = {row[0]: row for row in _pending_parliamentarians(session)}
    counters["pending_initial"] = len(pending)
    # Fecha a transacao da leitura antes das listagens do TSE (CS-126).
    session.commit()
    if not pending:
        return counters

    def index_pending() -> Dict[str, List[int]]:
        index: Dict[str, List[int]] = {}
        for pid, row in pending.items():
            for attribute in (row[1], row[2]):  # name, full_name
                key = normalize_text(attribute)
                if key:
                    bucket = index.setdefault(key, [])
                    if pid not in bucket:
                        bucket.append(pid)
        return index

    for year in SEED_ELECTION_YEARS:
        if not pending:
            break
        election_id = client.find_general_election_id(year)
        if election_id is None:
            logger.warning("Eleicao geral %s nao encontrada; pulando.", year)
            continue
        index = index_pending()
        for office_code in SEED_OFFICE_CODES:
            states = ("BR",) if office_code == 1 else UFS
            for uf in states:
                if not pending:
                    break
                candidates = client.list_candidates(year, uf, election_id, office_code)
                for item in candidates:
                    hits: List[int] = []
                    for name_key in (item.get("nomeCompleto"), item.get("nomeUrna")):
                        key = normalize_text(name_key)
                        for pid in index.get(key, []):
                            if pid in pending and pid not in hits:
                                hits.append(pid)
                    # Exatamente um alvo pendente, com UF de eleicao batendo
                    # (BR/presidente nao filtra) — mesma regra da candidacy.
                    if len(hits) != 1:
                        continue
                    pid = hits[0]
                    row = pending[pid]
                    if uf != "BR" and normalize_text(row[4]) != normalize_text(uf):
                        continue
                    detail = client.get_candidate_detail(
                        year, uf, election_id, item.get("id")
                    )
                    if detail is None:
                        continue
                    parl_cpf = normalize_cpf(row[3])
                    if parl_cpf and normalize_cpf(detail.get("cpf")) != parl_cpf:
                        counters["cpf_rejected"] += 1
                        continue
                    _seed_entries(
                        session,
                        detail.get("eleicoesAnteriores"),
                        candidacy_id=None,
                        parliamentarian_id=pid,
                        assets_source=detail,
                        assets_year=year,
                        counters=counters,
                    )
                    session.commit()
                    counters["seeded"] += 1
                    del pending[pid]
                    index = index_pending()

    session.commit()
    counters["still_pending"] = len(pending)
    if pending:
        names = [row[1] for row in list(pending.values())[:20]]
        logger.info("Parlamentares ainda sem timeline (%s): %s", len(pending), names)
    return counters


def drain_assets(
    session: Any,
    client: DivulgaCandClient,
    max_details: Optional[int],
    *,
    prazo_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Fase 3: preenche bens das linhas com assets_fetched_at NULL."""
    _ensure_models()
    counters: Dict[str, Any] = {"fetched": 0, "failed": 0, "pending_before": 0}

    query = (
        session.query(
            ElectoralHistory.election_year,
            ElectoralHistory.state,
            ElectoralHistory.tse_election_id,
            ElectoralHistory.tse_candidate_id,
        )
        .filter(ElectoralHistory.assets_fetched_at.is_(None))
        .order_by(
            ElectoralHistory.parliamentarian_id.is_(None),
            ElectoralHistory.election_year.desc(),
            ElectoralHistory.id,
        )
    )
    counters["pending_before"] = query.count()
    fila = (query.limit(max_details) if max_details else query).all()
    # Fecha a transacao da leitura antes de qualquer chamada ao TSE.
    session.commit()

    inicio = time.monotonic()
    seguidas = 0
    for year, state, election_id, candidate_id in fila:
        if prazo_s is not None and time.monotonic() - inicio > prazo_s:
            counters["interrompido"] = "prazo"
            logger.warning("Fase 3: teto de %ss atingido; o resto fica para a proxima.", prazo_s)
            break
        if election_id is None or not state:
            counters["failed"] += 1
            continue
        detail = client.get_candidate_detail(year, state, election_id, candidate_id)
        if detail is None:
            counters["failed"] += 1
            seguidas += 1
            if seguidas >= MAX_FALHAS_SEGUIDAS:
                counters["interrompido"] = "falhas_seguidas"
                logger.error(
                    "Fase 3: %s falhas seguidas na DivulgaCandContas; encerrando.", seguidas
                )
                break
            continue
        seguidas = 0
        upsert_history(
            session,
            {
                "election_year": year,
                # O state faz parte da chave natural (CS-69); sem ele o upsert
                # criaria outra linha e a original ficaria pendente para sempre.
                "state": state,
                "tse_candidate_id": candidate_id,
                **build_assets_payload(detail),
                "assets_fetched_at": datetime.utcnow(),
            },
        )
        session.commit()
        counters["fetched"] += 1
    return counters


def run(
    *,
    max_details: Optional[int] = None,
    skip_seed: bool = False,
    parliamentarians_only: bool = False,
    max_minutes: Optional[int] = None,
) -> None:
    _load_env_file()
    _ensure_models()
    from mamute_scrappers.db import session_scope

    client = DivulgaCandClient()

    with session_scope() as session:
        if not skip_seed:
            seeded = seed_from_candidacies(session)
            logger.info("Fase 1 (candidaturas locais): %s", seeded)
            missing = seed_missing_parliamentarians(session, client)
            logger.info("Fase 2 (parlamentares sem 2026): %s", missing)
        if not parliamentarians_only:
            drained = drain_assets(
                session,
                client,
                max_details,
                prazo_s=max_minutes * 60 if max_minutes else None,
            )
            logger.info("Fase 3 (patrimonio): %s", drained)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Constroi a timeline eleitoral a partir da DivulgaCandContas."
    )
    parser.add_argument(
        "--max-details",
        type=int,
        help="Teto de detalhes de patrimonio por execucao (fase 3).",
    )
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="Pula as fases 1-2; so drena patrimonio.",
    )
    parser.add_argument(
        "--parliamentarians-only",
        action="store_true",
        help="So as fases 1-2 (semear); nao drena patrimonio.",
    )

    parser.add_argument(
        "--max-minutes",
        type=int,
        help="Teto de tempo da fase 3; o que sobrar fica para a proxima execucao.",
    )

    args = parser.parse_args()
    run(
        max_details=args.max_details,
        skip_seed=args.skip_seed,
        parliamentarians_only=args.parliamentarians_only,
        max_minutes=args.max_minutes,
    )
