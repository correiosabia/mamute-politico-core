"""Registro de cada tentativa de envio em `email_send_log`.

Uma linha por projeto processado pela rotina (enviado, pulado ou erro).
A API só lê a tabela (aba "E-mails" do painel de métricas). Falha ao gravar
o log NUNCA pode derrubar o envio — só loga warning.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

STATUS_SENT = "sent"
STATUS_ERROR = "error"
STATUS_SKIPPED_NO_FAVORITES = "skipped_no_favorites"
STATUS_SKIPPED_NO_ACTIVITY = "skipped_no_activity"
# Com a flag email_destaques_gerais: quem antes era pulado recebe os destaques gerais.
STATUS_SENT_GENERAL = "sent_general"
STATUS_SENT_NO_ACTIVITY = "sent_no_activity"


def log_send_attempt(
    session: Session,
    *,
    projeto_id: int,
    email: str,
    periodicidade: str,
    status: str,
    detail: Optional[str] = None,
    subject: Optional[str] = None,
    stats: Optional[dict[str, Any]] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    message_id: Optional[str] = None,
) -> None:
    params = {
        "projeto_id": projeto_id,
        "email": email,
        "periodicidade": periodicidade,
        "status": status,
        "detail": detail,
        "subject": subject,
        "stats": json.dumps(stats) if stats is not None else None,
        "period_start": period_start,
        "period_end": period_end,
    }
    # message_id (CS-148) liga a abertura ao envio. O deploy sobe o código antes
    # da migration: sem a coluna, grava a linha sem ele em vez de perder o log.
    tentativas = [True, False] if message_id else [False]
    for com_message_id in tentativas:
        try:
            _insert(session, params, message_id if com_message_id else None)
            return
        except Exception:  # noqa: BLE001 — log não pode quebrar o envio
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
    logger.warning(
        "Não foi possível registrar em email_send_log (projeto %s, %s).",
        projeto_id,
        status,
        exc_info=True,
    )


def _insert(session: Session, params: dict[str, Any], message_id: Optional[str]) -> None:
    colunas = (
        "projeto_id, email, periodicidade, status, detail, "
        "subject, stats, period_start, period_end"
    )
    valores = (
        ":projeto_id, :email, :periodicidade, :status, :detail, "
        ":subject, :stats, :period_start, :period_end"
    )
    if message_id:
        colunas += ", message_id"
        valores += ", :message_id"
        params = {**params, "message_id": message_id}
    session.execute(
        text(f"INSERT INTO email_send_log ({colunas}) VALUES ({valores})"), params
    )
    session.commit()
