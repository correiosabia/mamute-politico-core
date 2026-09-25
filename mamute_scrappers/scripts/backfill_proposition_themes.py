"""Backfill da coluna `themes` (áreas temáticas oficiais) em proposition — CS-92.

Os coletores passaram a gravar `themes` em proposição nova; este script
preenche as que já estavam no banco. A fila é o próprio banco: pendente é
`themes IS NULL`. Por isso não há estado de progresso, só a lista de
proposições cuja consulta falhou (para não travar a fila nelas).

  - Senado: sem rede. As classificações já estão no `details.processo` que o
    coletor guardou; o script só as extrai. Roda inteiro a cada execução
    (barato). Proposição sem `processo` guardado fica NULL — não há de onde
    tirar o tema sem recoletar a matéria.
  - Câmara: uma chamada a /proposicoes/{id}/temas por proposição, das mais
    recentes para as mais antigas (a legislatura atual fica pronta primeiro).
    `--chunks-per-run` limita quantas por execução.

Garantias:
  - UPDATE com filtro `themes IS NULL`: não sobrescreve o que o coletor
    incremental já gravou.
  - Falha de consulta deixa a proposição NULL e a registra em `failed` no
    arquivo de estado; `--retry-failed` devolve essas à fila.
  - Lock via flock — não roda duas vezes em paralelo.
  - Sem a coluna ainda (deploy aplica a migration depois de subir os
    containers), sai sem fazer nada e sem se declarar completo.

Uso:
    python -m mamute_scrappers.scripts.backfill_proposition_themes --status
    python -m mamute_scrappers.scripts.backfill_proposition_themes --chunks-per-run 3000
    python -m mamute_scrappers.scripts.backfill_proposition_themes --retry-failed
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

from sqlalchemy import func, inspect as sqlalchemy_inspect, select, update as sa_update

logger = logging.getLogger("backfill_proposition_themes")

REQUEST_DELAY = 0.1
DEFAULT_CHUNKS_PER_RUN = 3000
SENADO_BATCH_SIZE = 500
CAMARA_COMMIT_EVERY = 50
CAMARA_LINK_PATTERN = "%camara.leg.br%"

STATE_FILE = Path(os.getenv(
    "PROPOSITION_THEMES_STATE_FILE", "/app/state/backfill_proposition_themes.json"
))
LOCK_FILE = STATE_FILE.with_name("backfill_proposition_themes.lock")


def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Estado ilegível (%s); recomeçando do zero.", exc)
    return {"failed": [], "updated_at": None}


def _save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return None
    return fd


def _has_themes_column(session) -> bool:
    columns = sqlalchemy_inspect(session.get_bind()).get_columns("proposition")
    return any(column.get("name") == "themes" for column in columns)


def senado_themes_from_details(details: Any) -> Optional[List[str]]:
    """Temas do Senado a partir do `details` guardado (None se não houver processo)."""
    from mamute_scrappers.senado_crawler.proposition import extract_senado_themes

    if not isinstance(details, dict):
        return None
    return extract_senado_themes(details.get("processo"))


def _camara_pending_filter(Proposition, failed: Iterable[int]):
    conditions = [
        Proposition.themes.is_(None),
        Proposition.link.ilike(CAMARA_LINK_PATTERN),
        Proposition.proposition_code.is_not(None),
    ]
    failed_codes = list(failed)
    if failed_codes:
        conditions.append(Proposition.proposition_code.not_in(failed_codes))
    return conditions


def _senado_pending_filter(Proposition):
    return [
        Proposition.themes.is_(None),
        func.coalesce(Proposition.link, "").not_ilike(CAMARA_LINK_PATTERN),
        # Só o que tem processo guardado entra na fila; o resto nunca sairia
        # dela e o backfill nunca se declararia completo.
        func.jsonb_typeof(Proposition.details["processo"]) == "object",
    ]


def _count(session, Proposition, conditions) -> int:
    return session.execute(
        select(func.count()).select_from(Proposition).where(*conditions)
    ).scalar_one()


def _apply_themes(session, Proposition, proposition_id: int, themes: List[str]) -> int:
    result = session.execute(
        sa_update(Proposition)
        .where(Proposition.id == proposition_id)
        .where(Proposition.themes.is_(None))
        .values(themes=themes)
    )
    return result.rowcount or 0


def _backfill_senado(session_scope, Proposition) -> Tuple[int, int]:
    """Extrai os temas do Senado do JSON guardado. Devolve (atualizadas, sem tema)."""
    updated = 0
    without_theme = 0
    last_id = 0
    while True:
        with session_scope() as session:
            rows = session.execute(
                select(Proposition.id, Proposition.details)
                .where(*_senado_pending_filter(Proposition))
                .where(Proposition.id > last_id)
                .order_by(Proposition.id.asc())
                .limit(SENADO_BATCH_SIZE)
            ).all()
            if not rows:
                break
            for proposition_id, details in rows:
                last_id = proposition_id
                themes = senado_themes_from_details(details)
                if themes is None:
                    continue
                updated += _apply_themes(session, Proposition, proposition_id, themes)
                if not themes:
                    without_theme += 1
    return updated, without_theme


def _list_camara_pending(session, Proposition, failed: Iterable[int], limit: int) -> List[Tuple[int, int]]:
    rows = session.execute(
        select(Proposition.id, Proposition.proposition_code)
        .where(*_camara_pending_filter(Proposition, failed))
        .order_by(
            Proposition.presentation_date.desc().nulls_last(),
            Proposition.id.desc(),
        )
        .limit(limit)
    ).all()
    return [(int(row[0]), int(row[1])) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill das áreas temáticas oficiais (proposition.themes)."
    )
    parser.add_argument(
        "--chunks-per-run",
        type=int,
        default=DEFAULT_CHUNKS_PER_RUN,
        help=(
            "Proposições da Câmara consultadas por execução "
            f"(padrão: {DEFAULT_CHUNKS_PER_RUN})."
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Apenas mostra o progresso e sai.",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Devolve à fila as proposições cuja consulta falhou antes.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    # Import tardio: o módulo de DB faz import-time work que não queremos no --help.
    from mamute_scrappers.camara_crawler.proposition import _fetch_proposition_themes
    from mamute_scrappers.db import session_scope
    from mamute_scrappers.db.models import Proposition

    state = _load_state()
    if args.retry_failed:
        state["failed"] = []
    failed = set(state.get("failed", []))

    with session_scope() as session:
        if not _has_themes_column(session):
            logger.warning(
                "Coluna proposition.themes ainda não existe (migration cs92 pendente); "
                "tentando de novo na próxima execução."
            )
            return
        camara_pending = _count(session, Proposition, _camara_pending_filter(Proposition, failed))
        senado_pending = _count(session, Proposition, _senado_pending_filter(Proposition))

    logger.info(
        "Backfill de temas: Câmara %s pendentes (%s falhadas fora da fila); "
        "Senado %s pendentes.",
        camara_pending,
        len(failed),
        senado_pending,
    )

    if args.status:
        return

    if camara_pending == 0 and senado_pending == 0:
        logger.info("Backfill de temas completo — nada a fazer.")
        return

    lock_fd = _acquire_lock()
    if lock_fd is None:
        logger.info("Outro backfill_proposition_themes já está em execução; saindo.")
        return

    try:
        if senado_pending:
            updated, without_theme = _backfill_senado(session_scope, Proposition)
            logger.info(
                "Senado: %s proposições com temas gravados (%s sem classificação na Casa).",
                updated,
                without_theme,
            )

        with session_scope() as session:
            pending = _list_camara_pending(session, Proposition, failed, args.chunks_per_run)

        processed_ok = 0
        processed_fail = 0
        without_theme = 0
        for start in range(0, len(pending), CAMARA_COMMIT_EVERY):
            batch = pending[start : start + CAMARA_COMMIT_EVERY]
            with session_scope() as session:
                for proposition_id, code in batch:
                    themes = _fetch_proposition_themes(code)
                    if themes is None:
                        logger.warning("Não obtive os temas da proposição %s.", code)
                        failed.add(code)
                        processed_fail += 1
                    else:
                        _apply_themes(session, Proposition, proposition_id, themes)
                        processed_ok += 1
                        if not themes:
                            without_theme += 1
                    if REQUEST_DELAY > 0:
                        time.sleep(REQUEST_DELAY)
            state["failed"] = sorted(failed)
            _save_state(state)

        logger.info(
            "Câmara: %s consultadas (%s sem classificação na Casa), %s falhadas. "
            "Restam %s pendentes.",
            processed_ok,
            without_theme,
            processed_fail,
            max(0, camara_pending - processed_ok - processed_fail),
        )
    finally:
        _save_state(state)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
