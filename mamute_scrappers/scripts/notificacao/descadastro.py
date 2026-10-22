"""Descadastro dos e-mails do Mamute (CS-150).

Quem pediu para parar de receber (`projetos.emails_desativados_em`) sai da lista
de envio, e todo e-mail leva o link de descadastro com o `email_token` do
leitor: no rodapé (página do app) e no cabeçalho List-Unsubscribe (botão
"Cancelar inscrição" do Gmail, que faz POST direto na API).

As colunas vêm de uma migration nova e o deploy sobe o código antes do alembic:
sem elas, ninguém é filtrado e o e-mail sai sem link, como antes.
"""

from __future__ import annotations

import dataclasses
import html
import logging
from typing import Iterable, List, Optional, Sequence
from urllib.parse import quote

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import EmailBranding
from .models import ProjectRecipient

logger = logging.getLogger(__name__)

COLUNA_DESATIVADO = "emails_desativados_em"
COLUNA_TOKEN = "email_token"


def tem_colunas_descadastro(session: Session) -> bool:
    try:
        colunas = {c["name"] for c in inspect(session.connection()).get_columns("projetos")}
    except SQLAlchemyError:
        return False
    return {COLUNA_DESATIVADO, COLUNA_TOKEN} <= colunas


def aplicar_preferencias(
    session: Session, recipients: Sequence[ProjectRecipient]
) -> List[ProjectRecipient]:
    """Tira quem desativou os e-mails e preenche o token do link de descadastro."""
    if not recipients or not tem_colunas_descadastro(session):
        return list(recipients)

    rows = session.execute(
        text(
            f"SELECT id, {COLUNA_TOKEN} AS token, {COLUNA_DESATIVADO} AS desativado "
            "FROM projetos WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": [r.id for r in recipients]},
    ).all()
    por_id = {int(row.id): row for row in rows}

    resultado: List[ProjectRecipient] = []
    desativados = 0
    for recipient in recipients:
        row = por_id.get(recipient.id)
        if row is not None and row.desativado is not None:
            desativados += 1
            continue
        token = row.token if row is not None else None
        resultado.append(dataclasses.replace(recipient, email_token=token or None))

    if desativados:
        logger.info("%s destinatário(s) pulado(s): pediram para não receber e-mails.", desativados)
    return resultado


def ids_desativados(session: Session, ids: Iterable[int]) -> set[int]:
    """Projetos (entre `ids`) que pediram para não receber e-mails."""
    lista = list(ids)
    if not lista or not tem_colunas_descadastro(session):
        return set()
    rows = session.execute(
        text(
            f"SELECT id FROM projetos WHERE id IN :ids AND {COLUNA_DESATIVADO} IS NOT NULL"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": lista},
    ).all()
    return {int(row.id) for row in rows}


def tokens_por_projeto(session: Session, ids: Iterable[int]) -> dict[int, str]:
    lista = list(ids)
    if not lista or not tem_colunas_descadastro(session):
        return {}
    rows = session.execute(
        text(f"SELECT id, {COLUNA_TOKEN} AS token FROM projetos WHERE id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        ),
        {"ids": lista},
    ).all()
    return {int(row.id): row.token for row in rows if row.token}


@dataclasses.dataclass(frozen=True)
class LinksDescadastro:
    pagina: str  # rodapé: página do app que confirma e permite voltar atrás
    one_click: str  # List-Unsubscribe: POST direto na API, sem página


def links_descadastro(brand: EmailBranding, token: Optional[str]) -> Optional[LinksDescadastro]:
    if not token:
        return None
    t = quote(token, safe="")
    return LinksDescadastro(
        pagina=f"{brand.manage_url.rstrip('/')}/descadastro?t={t}",
        one_click=f"{brand.app_url.rstrip('/')}/api/email-preferences/unsubscribe?t={t}",
    )


TEXTO_LINK = "Parar de receber estes e-mails"


def link_rodape_html(
    brand: EmailBranding, token: Optional[str], *, separador: str = " · ", estilo: str = ""
) -> str:
    """`· <a>Parar de receber estes e-mails</a>` para o rodapé; vazio sem token."""
    links = links_descadastro(brand, token)
    if links is None:
        return ""
    style = f' style="{estilo}"' if estilo else ""
    return f'{separador}<a href="{html.escape(links.pagina)}"{style}>{TEXTO_LINK}</a>'
