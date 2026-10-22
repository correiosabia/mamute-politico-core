"""Coleções curadas de políticos (CS-132).

Duas portas para o mesmo serviço:

* `/collections` — leitura PÚBLICA, sem login: só coleções publicadas. O link
  da coleção pode ser compartilhado com quem não assina.
* `/admin/collections` — leitura e escrita do admin, rascunhos incluídos.
  Gate igual ao resto do painel (404 para não-admin).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

try:
    from ..dependencies import get_db
    from ..security import require_ghost_admin
    from ..services import collection as svc
    from ..services import collection_events as eventos
    from ..services import collection_suggestions as busca
    from ..services import collection_travel as viagens
    from ..services import official_agenda as agenda
    from .admin import _log_admin_action
except ImportError:  # execução dentro de api/
    from dependencies import get_db
    from security import require_ghost_admin
    from services import collection as svc
    from services import collection_events as eventos
    from services import collection_suggestions as busca
    from services import collection_travel as viagens
    from services import official_agenda as agenda
    from routers.admin import _log_admin_action

router = APIRouter(prefix="/collections", tags=["collections"])
admin_router = APIRouter(prefix="/admin/collections", tags=["admin"])
metrics_router = APIRouter(prefix="/admin/metrics/collections", tags=["admin"])

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
    photo_url: Optional[str] = None
    photo_credit: Optional[str] = None


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


_ID_ANONIMO = r"^[A-Za-z0-9_-]{8,64}$"


class EventoIn(BaseModel):
    type: str = Field(max_length=40)
    target: Optional[str] = Field(default=None, max_length=500)
    detail: Optional[str] = Field(default=None, max_length=500)
    duration_ms: Optional[int] = None


class UtmIn(BaseModel):
    source: Optional[str] = Field(default=None, max_length=500)
    medium: Optional[str] = Field(default=None, max_length=500)
    campaign: Optional[str] = Field(default=None, max_length=500)


class LoteEventosIn(BaseModel):
    visitor: str = Field(pattern=_ID_ANONIMO)
    session: str = Field(pattern=_ID_ANONIMO)
    referrer: Optional[str] = Field(default=None, max_length=2000)
    utm: UtmIn = Field(default_factory=UtmIn)
    events: list[EventoIn] = Field(default_factory=list, max_length=200)


@router.post("/{slug}/events", status_code=status.HTTP_204_NO_CONTENT)
def ingest_events(
    slug: str, lote: LoteEventosIn, request: Request, db: Session = Depends(get_db)
) -> Response:
    """Eventos anônimos de leitura (CS-145). Sempre 204: o navegador não espera
    resposta, e quem envia lixo não descobre o que foi aceito."""
    user_agent = request.headers.get("user-agent")
    colecao_id = None if eventos.eh_robo(user_agent) else eventos.colecao_publicada(db, slug)
    if colecao_id is not None:
        eventos.grava_lote(
            db,
            collection_id=colecao_id,
            visitor=lote.visitor,
            session_id=lote.session,
            eventos=[e.model_dump() for e in lote.events],
            user_agent=user_agent,
            ip=eventos.ip_do_cliente(
                request.headers.get("x-forwarded-for"), request.client.host if request.client else None
            ),
            referrer=lote.referrer,
            utm=lote.utm.model_dump(),
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{slug}/travel")
def read_published_travel(slug: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Viagens coincidentes dos membros (cota parlamentar).

    Só de coleção publicada e com `settings.travel_public` ligado pelo admin: o
    cruzamento precisa de apuração antes de ir ao público, então nasce fechado.
    """
    colecao = svc.get_collection_meta(db, slug=slug)
    if colecao is None or colecao["settings"].get("travel_public") is not True:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    return viagens.travel_overlaps(db, colecao["id"])


def _termos_da_colecao(settings: dict[str, Any]) -> list[str]:
    termos = settings.get("agenda_terms")
    return [str(t) for t in termos] if isinstance(termos, list) else []


@router.get("/{slug}/agenda")
def read_published_agenda(slug: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Compromissos de agendas oficiais que citam os termos escolhidos pelo admin."""
    colecao = svc.get_collection_meta(db, slug=slug)
    if colecao is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    try:
        return agenda.search_agenda(db, _termos_da_colecao(colecao["settings"]))
    except agenda.AgendaError:
        # Termo inválido salvo antes da validação: o público só vê vazio.
        return {"terms": [], "items": [], "sources": []}


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
            # exclude_unset: vínculo ausente no corpo é "não mexer", não "apagar".
            db, collection_id, [m.model_dump(exclude_unset=True) for m in payload.members]
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


@admin_router.get("/{collection_id}/agenda")
def read_admin_agenda(
    collection_id: int,
    q: Optional[str] = Query(None, description="Termo avulso; sem ele, usa os termos salvos na coleção."),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    colecao = svc._buscar_colecao(db, collection_id=collection_id)
    if colecao is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    settings = colecao.settings if isinstance(colecao.settings, dict) else {}
    termos = [q] if q is not None else _termos_da_colecao(settings)
    try:
        return agenda.search_agenda(db, termos)
    except agenda.AgendaError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


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


# --------------------------------------------------------------------------
# painel: aba "Na Lupa" de Métricas e Insights (CS-145)
# --------------------------------------------------------------------------


@metrics_router.get("")
def metrics_list(
    db: Session = Depends(get_db), _admin: str = Depends(require_ghost_admin)
) -> list[dict[str, Any]]:
    return eventos.lista_casos(db)


@metrics_router.get("/{slug}")
def metrics_case(
    slug: str,
    dias: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    dados = eventos.metricas_do_caso(db, slug, dias)
    if dados is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NAO_ENCONTRADA)
    return dados
