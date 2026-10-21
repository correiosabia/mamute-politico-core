"""Aberturas dos e-mails do Mamute, lidas da API de eventos do Mailgun (CS-148).

O mailer liga o rastreio de abertura só nas mensagens com a tag `mamute`
(relatório e aviso de resultado da eleição) e grava o Message-ID de cada envio
em `email_send_log.message_id`. Este job busca os eventos `opened` dessa tag e
grava uma linha por mensagem aberta em `email_aberturas`; a API junta as duas
tabelas pelo Message-ID para calcular a % de abertura.

Roda de hora em hora pelo cron e relê as últimas `--horas` (padrão 26) a cada
vez: o Mailgun guarda poucos dias de eventos, e reler a mesma janela não
duplica nada (upsert por mensagem, guardando a primeira e a última abertura).

Sem `MAILGUN_API_KEY` ou sem a tabela (migration cs148 pendente), sai sem erro.

    python -m mamute_scrappers.scripts.notificacao.aberturas [--horas 26]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from mamute_scrappers.scripts.notificacao.mailer import TAG_MAMUTE  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.mailgun.net/v3"
DEFAULT_DOMAIN = "mamutepolitico.com.br"
DEFAULT_HORAS = 26
PAGE_LIMIT = 300
MAX_PAGINAS = 200

# (url, params) -> JSON da resposta. Injetável nos testes.
HttpGet = Callable[[str, Optional[Dict[str, Any]]], Dict[str, Any]]


@dataclass
class Abertura:
    message_id: str
    primeira: datetime
    ultima: datetime
    tags: str


@dataclass(frozen=True)
class MailgunConfig:
    api_key: str
    api_base: str
    domain: str


def get_mailgun_config() -> Optional[MailgunConfig]:
    # config.py carrega o .env (mesmo caminho do envio).
    from mamute_scrappers.scripts.notificacao import config as _config  # noqa: F401

    key = os.getenv("MAILGUN_API_KEY", "").strip()
    if not key:
        return None
    sender = os.getenv("MAILGUN_SMTP_SENDER", "") or os.getenv("SMTP_SENDER", "")
    domain = os.getenv("MAILGUN_DOMAIN", "").strip() or sender.rpartition("@")[2] or DEFAULT_DOMAIN
    base = os.getenv("MAILGUN_API_BASE", "").strip() or DEFAULT_API_BASE
    return MailgunConfig(api_key=key, api_base=base.rstrip("/"), domain=domain)


def _http_get(cfg: MailgunConfig) -> HttpGet:
    import requests

    def get(url: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        resp = requests.get(url, params=params, auth=("api", cfg.api_key), timeout=60)
        resp.raise_for_status()
        return resp.json()

    return get


def eventos_de_abertura(
    get: HttpGet, cfg: MailgunConfig, *, desde: datetime
) -> Iterator[Dict[str, Any]]:
    """Eventos `opened` da tag `mamute` a partir de `desde`, página a página."""
    url = f"{cfg.api_base}/{cfg.domain}/events"
    params: Optional[Dict[str, Any]] = {
        "event": "opened",
        "tags": TAG_MAMUTE,
        "begin": int(desde.timestamp()),
        "ascending": "yes",
        "limit": PAGE_LIMIT,
    }
    for _ in range(MAX_PAGINAS):
        data = get(url, params)
        items = data.get("items") or []
        if not items:
            return
        yield from items
        url = (data.get("paging") or {}).get("next") or ""
        if not url:
            return
        params = None  # a URL de `next` já traz os filtros


def agrupar(eventos: Iterable[Dict[str, Any]]) -> Dict[str, Abertura]:
    """Uma abertura por mensagem: primeira e última vez que foi aberta."""
    por_mensagem: Dict[str, Abertura] = {}
    for ev in eventos:
        headers = (ev.get("message") or {}).get("headers") or {}
        message_id = str(headers.get("message-id") or "").strip().strip("<>")
        ts = ev.get("timestamp")
        if not message_id or ts is None:
            continue
        quando = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        tags = ",".join(sorted(str(t) for t in (ev.get("tags") or [])))
        atual = por_mensagem.get(message_id)
        if atual is None:
            por_mensagem[message_id] = Abertura(message_id, quando, quando, tags)
        else:
            atual.primeira = min(atual.primeira, quando)
            atual.ultima = max(atual.ultima, quando)
    return por_mensagem


_UPSERT = """
    INSERT INTO email_aberturas (message_id, primeira_abertura_em, ultima_abertura_em, tags)
    VALUES (:message_id, :primeira, :ultima, :tags)
    ON CONFLICT (message_id) DO UPDATE SET
        primeira_abertura_em = CASE
            WHEN excluded.primeira_abertura_em < email_aberturas.primeira_abertura_em
            THEN excluded.primeira_abertura_em ELSE email_aberturas.primeira_abertura_em END,
        ultima_abertura_em = CASE
            WHEN excluded.ultima_abertura_em > email_aberturas.ultima_abertura_em
            THEN excluded.ultima_abertura_em ELSE email_aberturas.ultima_abertura_em END,
        tags = excluded.tags,
        updated_at = CURRENT_TIMESTAMP
"""


def gravar(session: Session, aberturas: Iterable[Abertura]) -> int:
    n = 0
    for a in aberturas:
        session.execute(
            text(_UPSERT),
            {"message_id": a.message_id, "primeira": a.primeira, "ultima": a.ultima, "tags": a.tags},
        )
        n += 1
    session.commit()
    return n


def tabela_existe(session: Session) -> bool:
    from sqlalchemy import inspect

    return inspect(session.connection()).has_table("email_aberturas")


def sincronizar(
    session: Session,
    *,
    horas: int = DEFAULT_HORAS,
    cfg: Optional[MailgunConfig] = None,
    get: Optional[HttpGet] = None,
    agora: Optional[datetime] = None,
) -> Optional[int]:
    """Grava as aberturas das últimas `horas`. None quando não dá para rodar."""
    cfg = cfg or get_mailgun_config()
    if cfg is None:
        logger.info("MAILGUN_API_KEY ausente: aberturas não são coletadas.")
        return None
    if not tabela_existe(session):
        logger.info("Tabela email_aberturas ausente (migration cs148 pendente).")
        return None
    agora = agora or datetime.now(timezone.utc)
    desde = datetime.fromtimestamp(agora.timestamp() - horas * 3600, tz=timezone.utc)
    aberturas = agrupar(eventos_de_abertura(get or _http_get(cfg), cfg, desde=desde))
    return gravar(session, aberturas.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grava as aberturas dos e-mails do Mamute.")
    parser.add_argument("--horas", type=int, default=DEFAULT_HORAS, help="Janela relida no Mailgun.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from mamute_scrappers.db import session_scope

    inicio = time.monotonic()
    with session_scope() as session:
        n = sincronizar(session, horas=args.horas)
    if n is not None:
        logger.info("Aberturas gravadas: %s mensagem(ns) em %.1fs.", n, time.monotonic() - inicio)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
