"""Orquestrador de backfill da coluna `vote_date` em roll_call_votes.

Os votos históricos foram persistidos antes do `vote_date` existir, então
não têm a data real de votação populada (a UI cai pro `created_at`, que é
o dia da ingestão — daí cards exibindo "23/05/2026" pra votações antigas).

Estratégia:
  - Cada votação na API gera N linhas em `roll_call_votes` com o MESMO `link`.
    Logo, agrupando por link, ~221k votos da Câmara correspondem a ~5-10k
    votações distintas; idem para o Senado. Uma chamada HTTP por link.
  - Câmara: link = `.../api/v2/votacoes/{votacao_id}`. Pegamos
    `dataHoraRegistro` (ou `data`) no `GET .../votacoes/{id}`.
  - Senado: o link gravado pelo crawler NÃO resolve — `/votacao/{codigo}`
    responde 404 e `/votacao?codigoSessaoVotacao=` ignora o filtro e devolve
    a lista padrão, cuja primeira data não é a desta votação (CS-94). O único
    filtro que o endpoint respeita é `codigoMateria`: listamos as votações da
    matéria (código em `proposition.proposition_code`), achamos a nossa pelo
    código que está no link e usamos o `dataSessao` dela.

Resíduo — votação que não ganha data:
  - Falha que pode não se repetir (rede, 5xx, 429, resposta ilegível) volta
    para a fila; só vira resíduo depois de MAX_TRANSIENT_ATTEMPTS rodadas.
    Várias falhas seguidas na mesma rodada = fonte fora do ar: a rodada
    encerra sem contar tentativa para ninguém.
  - Resposta definitiva sem data (404, votação ausente da matéria, campo de
    data vazio) vira resíduo na hora, com o motivo guardado no state file.
  - Votos sem `link` nunca entram na fila; o `--status` conta à parte.

Garantias:
  - State file persistente (`backfill_vote_dates.json`) — re-execução idempotente.
    As falhas gravadas no formato antigo (lista sem motivo, incluindo os 404
    do Senado) voltam para a fila uma vez.
  - Lock via flock — não roda duas vezes em paralelo.
  - `--chunks-per-run`: idem ao backfill_propositions; cron horário processa
    em fatias. Auto-encerra quando não há mais pendentes.
  - UPDATE com filtro `vote_date IS NULL`: votos já populados pelo crawler
    incremental (pós-migration) não são tocados.

Uso:
    python -m mamute_scrappers.scripts.backfill_vote_dates --status
    python -m mamute_scrappers.scripts.backfill_vote_dates --chunks-per-run 200
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import requests
from sqlalchemy import func, select, update as sa_update

logger = logging.getLogger("backfill_vote_dates")

REQUEST_DELAY = 0.1
DEFAULT_CHUNKS_PER_RUN = 100
MAX_TRANSIENT_ATTEMPTS = 5
MAX_CONSECUTIVE_TRANSIENT = 5
STATE_VERSION = 2

SENADO_VOTACAO_URL = "https://legis.senado.leg.br/dadosabertos/votacao"
SENADO_VOTE_VERSION = "1"

STATE_FILE = Path(os.getenv(
    "VOTE_DATE_STATE_FILE", "/app/state/backfill_vote_dates.json"
))
LOCK_FILE = STATE_FILE.with_name("backfill_vote_dates.lock")

DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
)

# Motivos de resíduo (chaves do state file e do --status).
REASON_NOT_FOUND = "fonte_404"
REASON_NO_DATE = "fonte_sem_data"
REASON_NOT_IN_MATERIA = "votacao_ausente_da_materia"
REASON_NO_MATERIA = "sem_codigo_materia"
REASON_UNKNOWN_SOURCE = "origem_desconhecida"
REASON_TRANSIENT_EXHAUSTED = "falha_repetida"

SOURCE_DOWN_MESSAGE = "fonte fora do ar"

_SENADO_VOTE_CODE_RE = re.compile(r"/votacao/(\d+)")
_SENADO_SESSION_VOTE_RE = re.compile(r"[?&]codigoSessaoVotacao=(\d+)")


class TransientFetchError(Exception):
    """Falha que pode não se repetir (rede, 5xx, 429, resposta ilegível)."""


# (data, None) quando achou; (None, motivo) quando a fonte respondeu sem data.
FetchResult = tuple[Optional[date], Optional[str]]


def _empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "done": [],
        "failed": {},
        "attempts": {},
        "updated_at": None,
    }


def _normalize_state(raw: Any) -> dict[str, Any]:
    state = _empty_state()
    if not isinstance(raw, dict):
        return state
    state["done"] = list(raw.get("done") or [])
    state["updated_at"] = raw.get("updated_at")
    failed = raw.get("failed")
    if isinstance(failed, dict):
        state["failed"] = dict(failed)
    elif failed:
        # Formato anterior: lista sem motivo, que misturava 404 do Senado
        # (link errado, contornado agora) com quedas de rede. Tudo volta pra fila.
        logger.info(
            "%s falhas gravadas no formato antigo voltam para a fila.", len(failed)
        )
    attempts = raw.get("attempts")
    if isinstance(attempts, dict):
        state["attempts"] = {k: int(v) for k, v in attempts.items()}
    return state


def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return _normalize_state(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Estado ilegível (%s); recomeçando do zero.", exc)
    return _empty_state()


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


def _parse_date(value: Any) -> Optional[date]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _request_json(url: str, params: Optional[dict[str, str]] = None) -> Optional[Any]:
    """JSON da fonte; None quando ela diz que o recurso não existe.

    Levanta TransientFetchError para o que vale tentar de novo depois.
    """
    headers = {"Accept": "application/json"}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise TransientFetchError(f"rede: {exc}") from exc
    if response.status_code in (400, 404, 410):
        return None
    if response.status_code >= 400:
        raise TransientFetchError(f"HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise TransientFetchError(f"JSON inválido: {exc}") from exc


def _fetch_camara_date(link: str, materia_code: Optional[int] = None) -> FetchResult:
    """Câmara: link = .../votacoes/{id}. GET retorna dados.dataHoraRegistro."""
    data = _request_json(link)
    if data is None:
        return None, REASON_NOT_FOUND
    dados = data.get("dados") if isinstance(data, dict) else None
    if not isinstance(dados, dict):
        return None, REASON_NO_DATE
    found = _parse_date(dados.get("dataHoraRegistro")) or _parse_date(dados.get("data"))
    if found is None:
        return None, REASON_NO_DATE
    return found, None


def _senado_vote_keys(link: str) -> tuple[Optional[int], Optional[int]]:
    """(codigoVotacaoSve, codigoSessaoVotacao) extraídos do link gravado."""
    vote_code = _SENADO_VOTE_CODE_RE.search(link)
    session_vote = _SENADO_SESSION_VOTE_RE.search(link)
    return (
        int(vote_code.group(1)) if vote_code else None,
        int(session_vote.group(1)) if session_vote else None,
    )


def _iter_vote_entries(obj: Any) -> Iterator[dict[str, Any]]:
    """Votações em qualquer profundidade (a v1 devolve lista; formatos antigos aninham)."""
    if isinstance(obj, dict):
        keys = {k.lower() for k in obj if isinstance(k, str)}
        if "codigovotacaosve" in keys or "codigosessaovotacao" in keys:
            yield obj
            return
        for value in obj.values():
            yield from _iter_vote_entries(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_vote_entries(item)


def _field(entry: dict[str, Any], name: str) -> Any:
    return entry.get(name) or entry.get(name[:1].upper() + name[1:])


def _match_senado_vote(
    payload: Any, *, vote_code: Optional[int], session_vote: Optional[int]
) -> Optional[dict[str, Any]]:
    for entry in _iter_vote_entries(payload):
        if vote_code is not None and _to_int(_field(entry, "codigoVotacaoSve")) == vote_code:
            return entry
        if session_vote is not None and _to_int(_field(entry, "codigoSessaoVotacao")) == session_vote:
            return entry
    return None


@lru_cache(maxsize=512)
def _senado_votes_for_materia(materia_code: int) -> Optional[Any]:
    # Destaques e emendas da mesma matéria caem na mesma chamada. Exceção
    # (falha passageira) não entra no cache.
    return _request_json(
        SENADO_VOTACAO_URL,
        params={"codigoMateria": str(materia_code), "v": SENADO_VOTE_VERSION},
    )


def _fetch_senado_date(link: str, materia_code: Optional[int] = None) -> FetchResult:
    """Senado: acha a votação entre as da matéria e devolve o dataSessao dela."""
    vote_code, session_vote = _senado_vote_keys(link)
    if vote_code is None and session_vote is None:
        return None, REASON_UNKNOWN_SOURCE
    if materia_code is None:
        return None, REASON_NO_MATERIA
    payload = _senado_votes_for_materia(materia_code)
    if payload is None:
        return None, REASON_NOT_FOUND
    entry = _match_senado_vote(payload, vote_code=vote_code, session_vote=session_vote)
    if entry is None:
        return None, REASON_NOT_IN_MATERIA
    found = _parse_date(_field(entry, "dataSessao"))
    if found is None:
        return None, REASON_NO_DATE
    return found, None


def _resolve_fetcher(link: str) -> Optional[Callable[[str, Optional[int]], FetchResult]]:
    if "camara.leg.br" in link:
        return _fetch_camara_date
    if "senado.leg.br" in link:
        return _fetch_senado_date
    return None


def _record_transient(state: dict[str, Any], link: str) -> None:
    attempts = state["attempts"]
    count = attempts.get(link, 0) + 1
    if count >= MAX_TRANSIENT_ATTEMPTS:
        attempts.pop(link, None)
        state["failed"][link] = REASON_TRANSIENT_EXHAUSTED
    else:
        attempts[link] = count


def _undo_transient(state: dict[str, Any], link: str, previous: Optional[int]) -> None:
    state["failed"].pop(link, None)
    if previous:
        state["attempts"][link] = previous
    else:
        state["attempts"].pop(link, None)


def _pending(
    pending_links: list[tuple[str, Optional[int]]], state: dict[str, Any]
) -> list[tuple[str, Optional[int]]]:
    done = set(state["done"])
    failed = state["failed"]
    attempts = state["attempts"]
    queue = [item for item in pending_links if item[0] not in done and item[0] not in failed]
    # Quem já falhou por instabilidade vai pro fim: não trava a fila nova.
    queue.sort(key=lambda item: attempts.get(item[0], 0))
    return queue


def _residue_summary(
    state: dict[str, Any], pending_links: list[tuple[str, Optional[int]]]
) -> str:
    still_null = {link for link, _ in pending_links}
    reasons = Counter(
        reason for link, reason in state["failed"].items() if link in still_null
    )
    if not reasons:
        return "nenhum"
    return ", ".join(f"{reason}={count}" for reason, count in sorted(reasons.items()))


def _list_pending_links(session, RollCallVote, Proposition) -> list[tuple[str, Optional[int]]]:
    """DISTINCT links com vote_date NULL, com o código da matéria de cada um."""
    rows = session.execute(
        select(RollCallVote.link, func.min(Proposition.proposition_code))
        .join(Proposition, Proposition.id == RollCallVote.proposition_id, isouter=True)
        .where(RollCallVote.vote_date.is_(None))
        .where(RollCallVote.link.is_not(None))
        .group_by(RollCallVote.link)
    ).all()
    return [(row[0], _to_int(row[1])) for row in rows if row[0]]


def _count_null_votes(session, RollCallVote) -> tuple[int, int]:
    """(votos sem data, votos sem data e sem link)."""
    total = session.execute(
        select(func.count()).select_from(RollCallVote).where(RollCallVote.vote_date.is_(None))
    ).scalar_one()
    without_link = session.execute(
        select(func.count())
        .select_from(RollCallVote)
        .where(RollCallVote.vote_date.is_(None))
        .where(RollCallVote.link.is_(None))
    ).scalar_one()
    return int(total), int(without_link)


def _apply_vote_date(session, RollCallVote, link: str, vote_date: date) -> int:
    """UPDATE limitado a votos com vote_date NULL — não sobrescreve dados frescos."""
    result = session.execute(
        sa_update(RollCallVote)
        .where(RollCallVote.link == link)
        .where(RollCallVote.vote_date.is_(None))
        .values(vote_date=vote_date)
    )
    return result.rowcount or 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill da coluna vote_date em roll_call_votes."
    )
    parser.add_argument(
        "--chunks-per-run",
        type=int,
        default=DEFAULT_CHUNKS_PER_RUN,
        help=f"Links processados por execução (padrão: {DEFAULT_CHUNKS_PER_RUN}).",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Apenas mostra o progresso e sai.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    # Import tardio: o módulo de DB faz import-time work que não queremos no --help.
    from mamute_scrappers.db import session_scope
    from mamute_scrappers.db.models import Proposition, RollCallVote

    state = _load_state()

    with session_scope() as session:
        pending_links = _list_pending_links(session, RollCallVote, Proposition)
        null_votes, null_without_link = _count_null_votes(session, RollCallVote)

    pending = _pending(pending_links, state)

    logger.info(
        "Backfill vote_dates: %s links done, %s pendentes (%s aguardando nova tentativa), "
        "%s links sem data no DB. Resíduo por motivo: %s. Votos sem data: %s, dos quais "
        "%s sem link (fora da fila).",
        len(state["done"]),
        len(pending),
        len(state["attempts"]),
        len(pending_links),
        _residue_summary(state, pending_links),
        null_votes,
        null_without_link,
    )

    if args.status:
        return

    if not pending:
        logger.info("Backfill vote_dates completo — nada a fazer.")
        return

    lock_fd = _acquire_lock()
    if lock_fd is None:
        logger.info("Outro backfill_vote_dates já está em execução; saindo.")
        return

    done = set(state["done"])
    processed_ok = 0
    processed_residue = 0
    processed_retry = 0
    rows_updated_total = 0
    # (link, tentativas antes desta rodada) das falhas passageiras seguidas.
    streak: list[tuple[str, Optional[int]]] = []
    source_down = False
    try:
        for link, materia_code in pending[: args.chunks_per_run]:
            fetcher = _resolve_fetcher(link)
            if fetcher is None:
                logger.warning("Link %s sem origem reconhecida; vira resíduo.", link)
                state["failed"][link] = REASON_UNKNOWN_SOURCE
                processed_residue += 1
            else:
                try:
                    vote_date, reason = fetcher(link, materia_code)
                except TransientFetchError as exc:
                    logger.warning("Falha passageira em %s: %s", link, exc)
                    streak.append((link, state["attempts"].get(link)))
                    _record_transient(state, link)
                    processed_retry += 1
                    if len(streak) >= MAX_CONSECUTIVE_TRANSIENT:
                        # A fonte caiu: não é culpa destes links.
                        for streak_link, previous in streak:
                            _undo_transient(state, streak_link, previous)
                        source_down = True
                else:
                    streak.clear()
                    state["attempts"].pop(link, None)
                    if vote_date is None:
                        logger.warning("Sem data para %s (%s).", link, reason)
                        state["failed"][link] = reason
                        processed_residue += 1
                    else:
                        with session_scope() as session:
                            rows = _apply_vote_date(session, RollCallVote, link, vote_date)
                        rows_updated_total += rows
                        logger.info("%s → %s (%s linhas)", link, vote_date.isoformat(), rows)
                        done.add(link)
                        processed_ok += 1

            state["done"] = sorted(done)
            _save_state(state)

            if source_down:
                logger.warning(
                    "%s falhas seguidas: %s; encerrando a rodada sem contar "
                    "tentativa para estes links.",
                    len(streak),
                    SOURCE_DOWN_MESSAGE,
                )
                break

            if REQUEST_DELAY > 0:
                time.sleep(REQUEST_DELAY)

        logger.info(
            "Execução concluída: %s links OK, %s sem data na fonte, %s para tentar de novo, "
            "%s votos atualizados.",
            processed_ok,
            processed_residue,
            processed_retry,
            rows_updated_total,
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
