"""Flags do relatório por e-mail, com a mesma regra do app.

A regra é a de `api/services/feature_flags.resolve_for`, para o e-mail e a
tela nunca divergirem (o registro das chaves mora em `ui/src/lib/featureFlags.ts`
no app):

  off (ou sem linha) -> ninguém;
  admins            -> só e-mails de MAMUTE_ADMIN_EMAILS;
  all               -> admins + contas cujo plano tem a flag em modo
                       `liberado` (tela de Planos).

Os estados são lidos uma vez por envio (`carregar_flags`), não por conta.
Tabela ausente = tudo desligado: o envio continua como antes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import ProjectRecipient

FLAG_DESIGN_NOVO = "email_design_novo"
FLAG_BALANCO = "email_balanco"
FLAG_CONVITE = "email_convite_assinatura"
FLAG_DESTAQUES_GERAIS = "email_destaques_gerais"
TODAS = (FLAG_DESIGN_NOVO, FLAG_BALANCO, FLAG_CONVITE, FLAG_DESTAQUES_GERAIS)


@dataclass(frozen=True)
class FlagSnapshot:
    estados: dict[str, str] = field(default_factory=dict)
    planos_liberados: dict[str, frozenset[int]] = field(default_factory=dict)

    def ativa(self, key: str, recipient: ProjectRecipient, admins: frozenset[str]) -> bool:
        estado = self.estados.get(key, "off")
        if estado == "off":
            return False
        if recipient.email.strip().lower() in admins:
            return True
        if estado != "all" or recipient.tier_id is None:
            return False
        return recipient.tier_id in self.planos_liberados.get(key, frozenset())


def carregar_flags(session: Session) -> FlagSnapshot:
    try:
        estados = {
            r[0]: (r[1] or "off")
            for r in session.execute(
                text("SELECT key, state FROM feature_flag WHERE key IN :keys").bindparams(bindparam("keys", expanding=True)),
                {"keys": list(TODAS)},
            ).all()
        }
        liberados: dict[str, set[int]] = {}
        for key, tier_id in session.execute(
            text(
                "SELECT flag_key, tier_id FROM feature_flag_tier "
                "WHERE mode = 'liberado' AND flag_key IN :keys"
            ).bindparams(bindparam("keys", expanding=True)),
            {"keys": list(TODAS)},
        ).all():
            liberados.setdefault(key, set()).add(int(tier_id))
    except SQLAlchemyError:
        session.rollback()
        return FlagSnapshot()
    return FlagSnapshot(
        estados=estados,
        planos_liberados={k: frozenset(v) for k, v in liberados.items()},
    )

