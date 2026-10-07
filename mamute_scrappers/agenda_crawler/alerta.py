"""Aviso por e-mail quando uma coleta de agenda traz compromisso novo sobre um caso (CS-135).

Cada coletor tira uma foto das linhas da fonte antes de gravar (`foto`) e, depois
do commit, chama `avisar_novos`. Compromisso novo = linha que cita algum termo
monitorado (`collection.settings.agenda_terms`) e não existia na foto (mesma
autoridade, dia e texto). Vai um e-mail só, com a lista.

Para quem: `AGENDA_ALERT_EMAILS` (vírgula) ou, sem ela, `MAMUTE_ADMIN_EMAILS`.
Sem destinatário ou sem SMTP configurado, só registra no log.

Regras de segurança:
- Fonte vazia antes da coleta (primeira carga, backfill) não avisa: seria a lista
  inteira de uma vez.
- Erro no envio nunca derruba a coleta.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

ROTULO = {
    "bcb": "Agenda da diretoria do Banco Central",
    "eagendas": "e-Agendas da CGU (Executivo federal)",
    "stf": "Agenda do STF",
}
MAX_LINHAS = 50


def _chave(authority_id: str, event_date: Any, description: str) -> tuple[str, str, str]:
    return (str(authority_id), str(event_date), hashlib.md5((description or "").encode()).hexdigest())


def foto(session: Any, source: str) -> set[tuple[str, str, str]]:
    from sqlalchemy import text

    linhas = session.execute(
        text("SELECT authority_id, event_date, description FROM official_agenda_item WHERE source = :s"),
        {"s": source},
    )
    return {_chave(a, d, t) for a, d, t in linhas}


def _termos(session: Any) -> list[str]:
    from mamute_scrappers.agenda_crawler.eagendas import termos_monitorados

    return [t for t in termos_monitorados(session) if len(t.strip()) >= 4]


def novos(session: Any, source: str, antes: set[tuple[str, str, str]]) -> list[dict[str, Any]]:
    """Linhas da fonte que citam algum termo e não estavam na foto."""
    from sqlalchemy import text

    termos = _termos(session)
    if not antes or not termos:
        return []
    filtro = " OR ".join(
        f"lower(description) LIKE :t{i} OR lower(coalesce(participants, '')) LIKE :t{i}" for i in range(len(termos))
    )
    linhas = session.execute(
        text(
            "SELECT authority_id, authority_name, office_label, organization, event_date, starts_at, description "
            f"FROM official_agenda_item WHERE source = :s AND ({filtro}) ORDER BY event_date DESC"
        ),
        {"s": source, **{f"t{i}": f"%{t.lower()}%" for i, t in enumerate(termos)}},
    ).mappings()
    vistos: set[tuple[str, str]] = set()
    saida = []
    for l in linhas:
        if _chave(l["authority_id"], l["event_date"], l["description"]) in antes:
            continue
        # A mesma reunião para vários agentes vira uma linha só no e-mail.
        reuniao = (str(l["event_date"]), (l["description"] or "")[:120])
        if reuniao in vistos:
            continue
        vistos.add(reuniao)
        saida.append(dict(l))
    return saida


def destinatarios() -> list[str]:
    bruto = os.getenv("AGENDA_ALERT_EMAILS", "").strip() or os.getenv("MAMUTE_ADMIN_EMAILS", "")
    return [e.strip() for e in bruto.split(",") if "@" in e]


def corpo(source: str, linhas: list[dict[str, Any]]) -> str:
    itens = "".join(
        "<li style='margin-bottom:10px'>"
        f"<strong>{html.escape(str(l['event_date']))}"
        f"{(' ' + html.escape(l['starts_at'])) if l.get('starts_at') else ''}</strong> · "
        f"{html.escape(l.get('organization') or '')}<br>"
        f"{html.escape(l.get('authority_name') or '')}"
        f"{(' (' + html.escape(l['office_label']) + ')') if l.get('office_label') else ''}<br>"
        f"<span style='color:#4b5563'>{html.escape((l.get('description') or '')[:400])}</span>"
        "</li>"
        for l in linhas[:MAX_LINHAS]
    )
    resto = f"<p>E mais {len(linhas) - MAX_LINHAS}.</p>" if len(linhas) > MAX_LINHAS else ""
    return (
        "<div style='font-family:Arial,sans-serif;font-size:14px;color:#1f2b44'>"
        f"<p>A coleta da <strong>{html.escape(ROTULO.get(source, source))}</strong> trouxe "
        f"{len(linhas)} compromisso(s) novo(s) que citam os termos monitorados dos casos da Na Lupa.</p>"
        f"<ul>{itens}</ul>{resto}"
        "<p style='color:#6b7280'>Eles já aparecem na linha do tempo do caso. Aviso automático do Mamute.</p></div>"
    )


def avisar_novos(session: Any, source: str, antes: set[tuple[str, str, str]]) -> int:
    """Manda o e-mail (se houver novidade) e devolve quantos compromissos novos achou."""
    try:
        lista = novos(session, source, antes)
        if not lista:
            return 0
        para = destinatarios()
        logger.info("Agenda %s: %s compromisso(s) novo(s) sobre os casos.", source, len(lista))
        if not para:
            logger.info("Sem AGENDA_ALERT_EMAILS nem MAMUTE_ADMIN_EMAILS: aviso só no log.")
            return len(lista)
        from mamute_scrappers.scripts.notificacao.mailer import send_html_email

        assunto = f"Na Lupa: {len(lista)} reunião(ões) nova(s) na {ROTULO.get(source, source)}"
        for email in para:
            send_html_email(corpo(source, lista), email, assunto)
        return len(lista)
    except Exception:  # noqa: BLE001 — aviso nunca derruba a coleta
        logger.exception("Falha ao avisar compromissos novos da agenda %s.", source)
        return 0
