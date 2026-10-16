"""Envio de e-mail via SMTP (SES ou outro provedor compatível)."""

from __future__ import annotations

import logging
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from .config import SmtpConfig, get_smtp_config
from .tzcompat import now_local

logger = logging.getLogger(__name__)


def _format_from_header(from_name: str, sender: str) -> str:
    """From com nome acentuado sem quebrar o envelope SMTP (ex.: Amazon SES)."""
    name = from_name.strip() or sender
    try:
        name.encode("ascii")
        return formataddr((name, sender))
    except UnicodeEncodeError:
        return formataddr((str(Header(name, "utf-8")), sender))


def send_html_email(
    html_body: str,
    to_address: str,
    subject: str,
    *,
    smtp: SmtpConfig | None = None,
    unsubscribe_url: str | None = None,
) -> None:
    config = smtp or get_smtp_config()
    sent_at = now_local()
    dated_subject = (
        f"{subject} | {sent_at.strftime('%d/%m/%Y')} — {sent_at.strftime('%H:%M')}"
    )

    msg = MIMEMultipart()
    msg["From"] = _format_from_header(config.from_name, config.sender)
    msg["To"] = to_address
    msg["Subject"] = str(Header(dated_subject, "utf-8"))
    if unsubscribe_url:
        # Botão "Cancelar inscrição" do Gmail/Yahoo: POST de um clique na API (RFC 8058).
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    logger.info("Enviando e-mail para %s", to_address)
    with smtplib.SMTP(config.server, config.port, timeout=60) as server:
        server.starttls()
        server.login(config.user, config.password)
        server.send_message(msg, from_addr=config.sender, to_addrs=[to_address])
    logger.info("E-mail enviado para %s", to_address)
