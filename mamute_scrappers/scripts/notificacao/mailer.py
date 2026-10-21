"""Envio de e-mail via SMTP (SES ou outro provedor compatível)."""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Sequence
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid

from .config import SmtpConfig, get_smtp_config
from .tzcompat import now_local

logger = logging.getLogger(__name__)

# Tags do Mailgun (CS-148). "mamute" junta tudo o que o job de aberturas busca;
# a segunda separa o tipo de e-mail. A newsletter do Ghost usa outras tags
# (bulk-email/ghost-email) e o magic link sai sem tag, então nada se mistura.
TAG_MAMUTE = "mamute"
TAG_RELATORIO = "mamute-relatorio"
TAG_RESULTADO_ELEICAO = "mamute-resultado-eleicao"


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
    tags: Sequence[str] = (),
) -> str:
    """Envia e devolve o Message-ID (sem `<>`), que liga a abertura ao envio.

    Com `tags`, liga o rastreio de abertura do Mailgun só nesta mensagem: o
    rastreio do domínio fica desligado porque valeria também para o magic link
    do Ghost. Clique não é rastreado (reescreveria os links do e-mail).
    """
    config = smtp or get_smtp_config()
    sent_at = now_local()
    dated_subject = (
        f"{subject} | {sent_at.strftime('%d/%m/%Y')} — {sent_at.strftime('%H:%M')}"
    )

    msg = MIMEMultipart()
    msg["From"] = _format_from_header(config.from_name, config.sender)
    msg["To"] = to_address
    msg["Subject"] = str(Header(dated_subject, "utf-8"))
    domain = config.sender.rpartition("@")[2] or None
    message_id = make_msgid(domain=domain)
    msg["Message-ID"] = message_id
    if tags:
        msg["X-Mailgun-Track-Opens"] = "yes"
        msg["X-Mailgun-Track-Clicks"] = "no"
        for tag in tags:
            msg["X-Mailgun-Tag"] = tag
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
    return message_id.strip("<>")
