"""Aviso do resultado da eleicao no app (CS-106).

O envio (`scripts/notificacao/resultado_eleicao.py`) grava um aviso por
projeto e turno em `election_result_notice`; aqui o app busca o aviso ainda
nao visto para abrir o modal e marca como visto quando a pessoa fecha. O que
o modal mostra e o mesmo `payload` que foi para o e-mail.

Quem decide se o modal aparece e a flag `resultado_eleicao` no front; sem
aviso gravado este endpoint so devolve 204.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

try:
    from ..db.models.election_result import ElectionResultNotice
    from ..dependencies import get_db
    from .projects import _get_project_from_token_email
except (ImportError, ValueError):
    from db.models.election_result import ElectionResultNotice
    from dependencies import get_db
    from routers.projects import _get_project_from_token_email

router = APIRouter(prefix="/projects", tags=["projects"])

# Linha `pending` e envio em andamento ou travado antes do SMTP: ainda nao
# mostra, para o modal nao aparecer antes do e-mail.
_STATUS_VISIVEIS = ("sent", "error", "skipped_no_email")


class ElectionResultNoticeOut(BaseModel):
    id: int
    turno: int
    payload: dict[str, Any]


@router.get(
    "/me/election-result-notice",
    response_model=Optional[ElectionResultNoticeOut],
    summary="Aviso de resultado da eleição ainda não visto pelo usuário",
    responses={204: {"description": "Nenhum aviso pendente"}},
)
def get_my_election_result_notice(
    request: Request,
    db: Session = Depends(get_db),
) -> Any:
    project = _get_project_from_token_email(request, db)
    notice = db.execute(
        select(ElectionResultNotice)
        .where(
            ElectionResultNotice.projeto_id == project.id,
            ElectionResultNotice.seen_at.is_(None),
            ElectionResultNotice.email_status.in_(_STATUS_VISIVEIS),
        )
        .order_by(desc(ElectionResultNotice.turno), desc(ElectionResultNotice.id))
        .limit(1)
    ).scalar_one_or_none()
    if notice is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return ElectionResultNoticeOut(id=notice.id, turno=notice.turno, payload=notice.payload)


@router.post(
    "/me/election-result-notice/{notice_id}/seen",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Marca o aviso de resultado como visto (modal fechado)",
)
def mark_my_election_result_notice_seen(
    notice_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    project = _get_project_from_token_email(request, db)
    notice = db.get(ElectionResultNotice, notice_id)
    if notice is None or int(notice.projeto_id) != int(project.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Aviso não encontrado.")
    # O aviso mostrado e o mais novo; os anteriores do mesmo ciclo (ex.: o de
    # majoritarios, quando o completo ja saiu) ficam vistos junto, para o
    # modal nao reaparecer com um resultado parcial e desatualizado (CS-119).
    agora = datetime.now(timezone.utc)
    for anterior in db.execute(
        select(ElectionResultNotice).where(
            ElectionResultNotice.projeto_id == project.id,
            ElectionResultNotice.ciclo == notice.ciclo,
            ElectionResultNotice.id <= notice.id,
            ElectionResultNotice.seen_at.is_(None),
        )
    ).scalars():
        anterior.seen_at = agora
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
