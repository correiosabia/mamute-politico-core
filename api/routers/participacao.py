"""Missões do dia (CS-179): leitura do assinante e curadoria no admin.

* `GET /participacao?aba=hoje|passou&tema=`: membro logado, gate de plano
  `missoes_do_dia` (prévia = PREVIEW_ROWS itens, sem filtro). O placar só vai
  com a flag `missoes_placar` liberada para quem chama.
* `/admin/participacao`: fila da curadoria (aprovar, descartar, tema, nota),
  aprovação em lote e cadastro manual. Gate igual ao resto do painel (404).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

try:
    from ..dependencies import get_db
    from ..feature_gate import PREVIEW_ROWS, FeatureAccess, flag_liberada, missoes_access
    from ..security import require_ghost_admin
    from ..services import participacao as svc
    from .admin import _log_admin_action
except ImportError:  # execução dentro de api/
    from dependencies import get_db
    from feature_gate import PREVIEW_ROWS, FeatureAccess, flag_liberada, missoes_access
    from security import require_ghost_admin
    from services import participacao as svc
    from routers.admin import _log_admin_action

router = APIRouter(prefix="/participacao", tags=["participacao"])
admin_router = APIRouter(prefix="/admin/participacao", tags=["admin"])

FLAG_PLACAR = "missoes_placar"


@router.get("")
def read_missoes(
    request: Request,
    aba: Literal["hoje", "passou"] = "hoje",
    tema: Optional[str] = Query(default=None, max_length=svc.MAX_TEMA),
    authorization: Optional[str] = Header(default=None),
    access: FeatureAccess = Depends(missoes_access),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    com_placar = flag_liberada(FLAG_PLACAR, request, authorization, db)
    if not access.full:
        # Prévia: ordem fixa, sem filtro (ver feature_gate).
        saida = svc.listar_publico(db, aba="hoje", com_placar=com_placar, limite=PREVIEW_ROWS)
        saida["previa"] = True
        return saida
    saida = svc.listar_publico(db, aba=aba, tema=tema, com_placar=com_placar)
    saida["previa"] = False
    return saida


class RevisaoIn(BaseModel):
    status: Optional[Literal["pendente", "aprovado", "descartado"]] = None
    theme: Optional[str] = None
    note: Optional[str] = None
    # Só para item cadastrado à mão:
    kind: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    reference: Optional[str] = None
    organization: Optional[str] = None
    url: Optional[str] = None
    opens_on: Optional[date] = None
    closes_on: Optional[date] = None
    source_open: Optional[bool] = None


class LoteIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    status: Literal["pendente", "aprovado", "descartado"]


class ManualIn(BaseModel):
    kind: str = "peticao"
    title: str
    url: str
    summary: Optional[str] = None
    reference: Optional[str] = None
    organization: Optional[str] = None
    opens_on: Optional[date] = None
    closes_on: Optional[date] = None
    theme: Optional[str] = None
    note: Optional[str] = None


def _erro(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@admin_router.get("")
def admin_list(
    status: Literal["pendente", "aprovado", "descartado"] = "pendente",
    fonte: Optional[str] = Query(default=None, max_length=40),
    busca: Optional[str] = Query(default=None, max_length=120),
    _admin: str = Depends(require_ghost_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return svc.listar_admin(db, status=status, fonte=fonte, busca=busca)


@admin_router.patch("/{item_id}")
def admin_review(
    item_id: int,
    payload: RevisaoIn,
    admin_email: str = Depends(require_ghost_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    dados = payload.model_dump(exclude_unset=True)
    try:
        antes = svc.foto(svc._buscar(db, item_id))
        item = svc.revisar(db, item_id, dados, admin_email=admin_email)
    except (svc.ParticipacaoError, LookupError) as exc:
        db.rollback()
        raise _erro(exc) from exc
    _log_admin_action(
        db, admin_email=admin_email, action="participacao_revisar", entity="participation_item",
        entity_id=str(item_id), before=antes, after=svc.foto(item),
    )
    db.commit()
    db.refresh(item)
    return svc.para_admin(item)


@admin_router.post("/lote")
def admin_batch(
    payload: LoteIn,
    admin_email: str = Depends(require_ghost_admin),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    n = svc.revisar_lote(db, payload.ids, payload.status, admin_email=admin_email)
    _log_admin_action(
        db, admin_email=admin_email, action="participacao_lote", entity="participation_item",
        entity_id=",".join(str(i) for i in payload.ids[:50]), before=None, after={"status": payload.status, "n": n},
    )
    db.commit()
    return {"alterados": n}


@admin_router.post("", status_code=201)
def admin_create(
    payload: ManualIn,
    admin_email: str = Depends(require_ghost_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        item = svc.criar_manual(db, payload.model_dump(), admin_email=admin_email)
    except svc.ParticipacaoError as exc:
        db.rollback()
        raise _erro(exc) from exc
    _log_admin_action(
        db, admin_email=admin_email, action="participacao_criar", entity="participation_item",
        entity_id=str(item.id), before=None, after=svc.foto(item),
    )
    db.commit()
    db.refresh(item)
    return svc.para_admin(item)
