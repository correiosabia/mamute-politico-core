"""Coleções curadas de políticos (CS-132).

Duas portas para o mesmo serviço:

* `/collections` — leitura PÚBLICA, sem login: só coleções publicadas. O link
  da coleção pode ser compartilhado com quem não assina.
* `/admin/collections` — leitura e escrita do admin, rascunhos incluídos.
  Gate igual ao resto do painel (404 para não-admin).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

try:
    from ..dependencies import get_db
    from ..security import require_ghost_admin
    from ..services import collection as svc
    from ..services import collection_suggestions as busca
    from ..services import collection_travel as viagens
    from .admin import _log_admin_action
except ImportError:  # execução dentro de api/
    from dependencies import get_db
    from security import require_ghost_admin
    from services import collection as svc
    from services import collection_suggestions as busca
    from services import collection_travel as viagens
    from routers.admin import _log_admin_action

router = APIRouter(prefix="/collections", tags=["collections"])
admin_router = APIRouter(prefix="/admin/collections", tags=["admin"])

_NAO_ENCONTRADA = "Coleção não encontrada."


class CollectionIn(BaseModel):
    slug: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    summary: Optional[str] = None
    status: Optional[Literal["draft", "published"]] = None
    tier_labels: Optional[dict[str, str]] = None
    settings: Optional[dict[str, Any]] = None


class SourceIn(BaseModel):
    label: Optional[str] = None
    url: Optional[str] = None


class MemberIn(BaseModel):
    id: Optional[int] = None
    display_name: str
    role_label: Optional[str] = None
    cpf: Optional[str] = None
    parliamentarian_id: Optional[int] = None
    candidacy_id: Optional[int] = None
    tier: Optional[int] = Field(default=None, ge=1, le=5)
    context: Optional[str] = None
    sources: list[SourceIn] = Field(default_factory=list)


class MembersUpdate(BaseModel):
    members: list[MemberIn]


class BlockIn(BaseModel):
    id: Optional[int] = None
    kind: str
    ref_id: Optional[int] = None
    member_id: Optional[int] = None
    title: Optional[str] = None
    body: Optional[str] = None
    url: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class BlocksUpdate(BaseModel):
    blocks: list[BlockIn]


def _erro(exc: svc.CollectionError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


# --------------------------------------------------------------------------
# público
# --------------------------------------------------------------------------


@router.get("")
def list_published(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return svc.list_collections(db)


@router.get("/{slug}")
def read_published(slug: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    colecao = svc.get_collection(db, slug=slug)
    if colecao is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    return colecao


@router.get("/{slug}/travel")
def read_published_travel(slug: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Viagens coincidentes dos membros (cota parlamentar). Só de coleção publicada."""
    colecao = svc.get_collection_meta(db, slug=slug)
    if colecao is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    return viagens.travel_overlaps(db, colecao["id"])


# --------------------------------------------------------------------------
# admin
# --------------------------------------------------------------------------


@admin_router.get("")
def list_admin(
    db: Session = Depends(get_db), _admin: str = Depends(require_ghost_admin)
) -> list[dict[str, Any]]:
    return svc.list_collections(db, incluir_rascunhos=True)


@admin_router.get("/{collection_id}")
def read_admin(
    collection_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    colecao = svc.get_collection(db, collection_id=collection_id, incluir_rascunhos=True)
    if colecao is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    return colecao


@admin_router.post("", status_code=status.HTTP_201_CREATED)
def create_admin(
    payload: CollectionIn,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    try:
        criada = svc.create_collection(db, payload.model_dump(exclude_unset=True))
    except svc.CollectionError as exc:
        db.rollback()
        raise _erro(exc) from exc
    _log_admin_action(
        db,
        admin_email=admin_email,
        action="create_collection",
        entity="collection",
        entity_id=str(criada["id"]),
        before=None,
        after={k: criada[k] for k in ("slug", "title", "status")},
    )
    db.commit()
    return criada


@admin_router.put("/{collection_id}")
def update_admin(
    collection_id: int,
    payload: CollectionIn,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    antes = svc.get_collection(db, collection_id=collection_id, incluir_rascunhos=True)
    if antes is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    try:
        depois = svc.update_collection(db, collection_id, payload.model_dump(exclude_unset=True))
    except svc.CollectionError as exc:
        db.rollback()
        raise _erro(exc) from exc
    campos = ("slug", "title", "subtitle", "summary", "status", "tier_labels")
    _log_admin_action(
        db,
        admin_email=admin_email,
        action="update_collection",
        entity="collection",
        entity_id=str(collection_id),
        before={k: antes.get(k) for k in campos},
        after={k: depois.get(k) for k in campos},
    )
    db.commit()
    return depois


@admin_router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_admin(
    collection_id: int,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> Response:
    antes = svc.get_collection(db, collection_id=collection_id, incluir_rascunhos=True)
    if antes is None or not svc.delete_collection(db, collection_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    _log_admin_action(
        db,
        admin_email=admin_email,
        action="delete_collection",
        entity="collection",
        entity_id=str(collection_id),
        before=antes,
        after=None,
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@admin_router.put("/{collection_id}/members")
def replace_members_admin(
    collection_id: int,
    payload: MembersUpdate,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> list[dict[str, Any]]:
    try:
        membros = svc.replace_members(
            db, collection_id, [m.model_dump() for m in payload.members]
        )
    except svc.CollectionError as exc:
        db.rollback()
        raise _erro(exc) from exc
    if membros is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    _log_admin_action(
        db,
        admin_email=admin_email,
        action="replace_collection_members",
        entity="collection",
        entity_id=str(collection_id),
        before=None,
        after={"members": len(membros)},
    )
    db.commit()
    return membros


@admin_router.put("/{collection_id}/blocks")
def replace_blocks_admin(
    collection_id: int,
    payload: BlocksUpdate,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> list[dict[str, Any]]:
    try:
        blocos = svc.replace_blocks(db, collection_id, [b.model_dump() for b in payload.blocks])
    except svc.CollectionError as exc:
        db.rollback()
        raise _erro(exc) from exc
    if blocos is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    _log_admin_action(
        db,
        admin_email=admin_email,
        action="replace_collection_blocks",
        entity="collection",
        entity_id=str(collection_id),
        before=None,
        after={"blocks": len(blocos)},
    )
    db.commit()
    return blocos


@admin_router.get("/{collection_id}/travel")
def read_admin_travel(
    collection_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    encontros = viagens.travel_overlaps(db, collection_id)
    if encontros is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    return encontros


@admin_router.get("/{collection_id}/search")
def search_admin(
    collection_id: int,
    q: str = Query(..., description="Termo buscado nos registros dos membros."),
    kinds: Optional[str] = Query(None, description="speech,vote,proposition (padrão: todos)."),
    member_id: Optional[int] = None,
    limit: int = Query(busca.LIMITE_PADRAO, ge=1, le=100),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    tipos = tuple(k for k in (kinds or "").split(",") if k in busca.TIPOS) or busca.TIPOS
    try:
        achados = busca.search_records(
            db, collection_id, termo=q, tipos=tipos, member_id=member_id, limite=limit
        )
    except svc.CollectionError as exc:
        raise _erro(exc) from exc
    if achados is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    return achados
