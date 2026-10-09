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
) -> None:
    try:
        session.execute(
            text(
                """
                INSERT INTO email_send_log
                    (projeto_id, email, periodicidade, status, detail,
                     subject, stats, period_start, period_end)
                VALUES
                    (:projeto_id, :email, :periodicidade, :status, :detail,
                     :subject, :stats, :period_start, :period_end)
                """
            ),
            {
                "projeto_id": projeto_id,
                "email": email,
                "periodicidade": periodicidade,
                "status": status,
                "detail": detail,
                "subject": subject,
                "stats": json.dumps(stats) if stats is not None else None,
                "period_start": period_start,
                "period_end": period_end,
            },
        )
        session.commit()
    except Exception:  # noqa: BLE001 — log não pode quebrar o envio
        logger.warning(
            "Não foi possível registrar em email_send_log (projeto %s, %s).",
            projeto_id,
            status,
            exc_info=True,
        )
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
