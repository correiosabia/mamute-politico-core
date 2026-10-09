"""Peças editáveis do e-mail (tabela `email_settings`, editada no Admin).

Só leitura aqui; quem grava é a API. Tabela ausente (deploy sobe o código antes
do alembic) = nada configurado, e cada bloco com chave vazia some do e-mail.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


def load_email_settings(session: Session) -> dict[str, str]:
    try:
        linhas = session.execute(text("SELECT key, value FROM email_settings")).all()
    except SQLAlchemyError:
        session.rollback()
        return {}
    return {key: (value or "").strip() for key, value in linhas if (value or "").strip()}
