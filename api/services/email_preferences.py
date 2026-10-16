"""Preferência de receber os e-mails do Mamute (CS-150).

Um interruptor só por leitor: `projetos.emails_desativados_em` (NULL = recebe).
Vale para o resumo periódico e para o aviso de resultado da eleição; não mexe
nas newsletters do Ghost, que têm descadastro próprio.

Dois caminhos gravam aqui:
- logado, pela Minha conta (projeto vem do JWT);
- sem login, pelo link do rodapé ou pelo botão "Cancelar inscrição" do Gmail,
  com o `projetos.email_token` do leitor.

As colunas vêm de migration nova e o deploy sobe o código antes do alembic:
sem elas, `EmailPreferencesUnavailable` (o router responde 503 com texto de tela).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

_COLUNAS = {"emails_desativados_em", "email_token"}
_TOKEN_MAX = 200


class EmailPreferencesUnavailable(RuntimeError):
    """Migration ainda não rodou."""


@dataclass(frozen=True)
class EmailPreference:
    recebe: bool
    email_mascarado: str


def _disponivel(db: Session) -> bool:
    try:
        colunas = {c["name"] for c in inspect(db.connection()).get_columns("projetos")}
    except SQLAlchemyError:
        return False
    return _COLUNAS <= colunas


def _exigir(db: Session) -> None:
    if not _disponivel(db):
        raise EmailPreferencesUnavailable()


def mascarar_email(email: str) -> str:
    """`lu***@gmail.com`: confirma de quem é o link sem expor o endereço inteiro."""
    usuario, _, dominio = (email or "").strip().partition("@")
    if not dominio:
        return "***"
    return f"{usuario[:2]}***@{dominio}"


def get_preference(db: Session, projeto_id: int) -> EmailPreference:
    _exigir(db)
    row = db.execute(
        text("SELECT email, emails_desativados_em FROM projetos WHERE id = :id"),
        {"id": projeto_id},
    ).one()
    return EmailPreference(
        recebe=row.emails_desativados_em is None,
        email_mascarado=mascarar_email(row.email),
    )


def set_preference(db: Session, projeto_id: int, recebe: bool) -> EmailPreference:
    """Grava a escolha. Desativar de novo não muda a data do primeiro pedido."""
    _exigir(db)
    if recebe:
        db.execute(
            text("UPDATE projetos SET emails_desativados_em = NULL WHERE id = :id"),
            {"id": projeto_id},
        )
    else:
        db.execute(
            text(
                "UPDATE projetos SET emails_desativados_em = :agora "
                "WHERE id = :id AND emails_desativados_em IS NULL"
            ),
            {"id": projeto_id, "agora": datetime.now(timezone.utc)},
        )
    return get_preference(db, projeto_id)


def projeto_do_token(db: Session, token: Optional[str]) -> Optional[int]:
    """Projeto ativo dono do token; None se o token não existe ou é inválido."""
    _exigir(db)
    token = (token or "").strip()
    if not token or len(token) > _TOKEN_MAX:
        return None
    row = db.execute(
        text(
            "SELECT id FROM projetos WHERE email_token = :token AND deleted_at IS NULL"
        ),
        {"token": token},
    ).first()
    return int(row.id) if row is not None else None
