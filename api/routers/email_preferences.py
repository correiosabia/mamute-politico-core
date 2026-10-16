"""Receber ou não os e-mails do Mamute (CS-150).

- `/projects/me/email-preferences` (logado): interruptor da Minha conta.
- `/email-preferences/unsubscribe|resubscribe?t=` (sem login): link do rodapé e
  botão "Cancelar inscrição" do Gmail (List-Unsubscribe-Post, RFC 8058). O token
  é o `projetos.email_token` do leitor; quem tem o e-mail tem o link.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

try:
    from ..dependencies import get_db
    from ..services.email_preferences import (
        EmailPreference,
        EmailPreferencesUnavailable,
        get_preference,
        projeto_do_token,
        set_preference,
    )
    from .projects import _get_project_from_token_email
except (ImportError, ValueError):  # execução dentro de api/
    from dependencies import get_db
    from services.email_preferences import (
        EmailPreference,
        EmailPreferencesUnavailable,
        get_preference,
        projeto_do_token,
        set_preference,
    )
    from routers.projects import _get_project_from_token_email

me_router = APIRouter(prefix="/projects/me/email-preferences", tags=["projects"])
public_router = APIRouter(prefix="/email-preferences", tags=["public"])

_INDISPONIVEL = "Não foi possível alterar seus e-mails agora. Tente de novo em alguns minutos."
_LINK_INVALIDO = (
    "Este link de descadastro não é válido. Entre no Mamute e desative os e-mails "
    "em Minha conta."
)


class EmailPreferenceOut(BaseModel):
    recebe: bool
    email_mascarado: str


class EmailPreferenceUpdate(BaseModel):
    recebe: bool


def _out(pref: EmailPreference) -> EmailPreferenceOut:
    return EmailPreferenceOut(recebe=pref.recebe, email_mascarado=pref.email_mascarado)


@me_router.get("", response_model=EmailPreferenceOut)
def read_my_email_preference(request: Request, db: Session = Depends(get_db)) -> EmailPreferenceOut:
    project = _get_project_from_token_email(request, db)
    try:
        return _out(get_preference(db, int(project.id)))
    except EmailPreferencesUnavailable as exc:
        raise HTTPException(status_code=503, detail=_INDISPONIVEL) from exc


@me_router.put("", response_model=EmailPreferenceOut)
def update_my_email_preference(
    payload: EmailPreferenceUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> EmailPreferenceOut:
    project = _get_project_from_token_email(request, db)
    try:
        pref = set_preference(db, int(project.id), payload.recebe)
    except EmailPreferencesUnavailable as exc:
        raise HTTPException(status_code=503, detail=_INDISPONIVEL) from exc
    db.commit()
    return _out(pref)


def _por_token(db: Session, token: Optional[str], recebe: bool) -> EmailPreferenceOut:
    try:
        projeto_id = projeto_do_token(db, token)
        if projeto_id is None:
            raise HTTPException(status_code=404, detail=_LINK_INVALIDO)
        pref = set_preference(db, projeto_id, recebe)
    except EmailPreferencesUnavailable as exc:
        raise HTTPException(status_code=503, detail=_INDISPONIVEL) from exc
    db.commit()
    return _out(pref)


# POST, nunca GET: antivírus e pré-visualização de link abrem GETs sozinhos e
# descadastrariam o leitor sem ele clicar. O corpo (List-Unsubscribe=One-Click
# do Gmail) é ignorado.
@public_router.post("/unsubscribe", response_model=EmailPreferenceOut)
def unsubscribe_by_token(
    t: Optional[str] = Query(None, max_length=200),
    db: Session = Depends(get_db),
) -> EmailPreferenceOut:
    return _por_token(db, t, recebe=False)


@public_router.post("/resubscribe", response_model=EmailPreferenceOut)
def resubscribe_by_token(
    t: Optional[str] = Query(None, max_length=200),
    db: Session = Depends(get_db),
) -> EmailPreferenceOut:
    return _por_token(db, t, recebe=True)
