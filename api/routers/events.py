"""Ingesta de eventos de uso do app (page views) e sinal de presença. Auth de membro.

CS-87: os eventos e a presença guardam a localidade estimada pelo IP (país,
estado, cidade e o centro da cidade). O IP é usado só nessa conta e não é gravado.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

try:
    from ..security import verify_token
    from ..dependencies import get_db
    from ..db.models.usage_event import UsageEvent
    from ..services import acessos
    from ..services import collection_events as ev
except ImportError:  # execução dentro de api/
    from security import verify_token
    from dependencies import get_db
    from db.models.usage_event import UsageEvent
    from services import acessos
    from services import collection_events as ev

router = APIRouter(prefix="/events", tags=["events"])

# page_view e section_view vêm do cliente; favoritos são logados server-side.
_ALLOWED_TYPES = {"page_view", "section_view"}


class EventIn(BaseModel):
    type: str
    page: Optional[str] = None
    section: Optional[str] = None


class EventsBatch(BaseModel):
    events: list[EventIn]


def _resolve_projeto_id(db: Session, email: Optional[str]) -> Optional[int]:
    if not email:
        return None
    row = db.execute(
        text(
            "SELECT id FROM projetos WHERE lower(email) = :e "
            "AND deleted_at IS NULL LIMIT 1"
        ),
        {"e": email},
    ).first()
    return int(row[0]) if row else None


def _regiao(request: Request) -> Optional[tuple]:
    ip = ev.ip_do_cliente(request.headers.get("x-forwarded-for"), request.client.host if request.client else None)
    return ev.regiao_do_ip(ip)


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
def ingest_events(
    batch: EventsBatch,
    request: Request,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
) -> Response:
    email = (payload.get("sub") or "").strip().lower() or None
    projeto_id = _resolve_projeto_id(db, email)
    regiao = _regiao(request) or (None, None, None)
    lat, lon = ev.coordenadas(regiao)
    for event in batch.events:
        if event.type not in _ALLOWED_TYPES:
            continue
        db.add(
            UsageEvent(
                projeto_id=projeto_id,
                email=email,
                event_type=event.type,
                page=event.page,
                section=event.section,
                country=regiao[0],
                region=regiao[1],
                city=regiao[2],
                lat=lat,
                lon=lon,
            )
        )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/presence", status_code=status.HTTP_204_NO_CONTENT)
def presence(
    request: Request,
    payload: dict = Depends(verify_token),
    db: Session = Depends(get_db),
) -> Response:
    """Sinal de presença (CS-87): a UI manda a cada 2 min com a aba visível.

    Alimenta "ativos agora" e o quadro de horários do admin. Também puxa a
    limpeza diária das localidades com mais de 90 dias.
    """
    email = (payload.get("sub") or "").strip().lower() or None
    if email:
        acessos.registrar_presenca(
            db, email=email, projeto_id=_resolve_projeto_id(db, email), regiao=_regiao(request)
        )
        acessos.limpar_se_preciso(db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
