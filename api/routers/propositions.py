"""Rotas relacionadas a proposições legislativas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import asc, desc, inspect as sqlalchemy_inspect, select
from sqlalchemy.exc import NoInspectionAvailable, SQLAlchemyError
from sqlalchemy.orm import Session, undefer

try:
    # Execução como pacote (api.routers.propositions).
    from ..db.models.proposition import Proposition
    from ..dependencies import get_db
except (ImportError, ValueError):
    # Execução local dentro de api/ sem reconhecimento de pacote.
    from db.models.proposition import Proposition
    from dependencies import get_db

router = APIRouter(prefix="/propositions", tags=["propositions"])
SENADO_PROPOSITION_BASE_URL = "https://www25.senado.leg.br/web/atividade/materias/-/materia"


class PropositionOut(BaseModel):
    """Representação serializada de uma proposição."""

    id: int
    proposition_code: Optional[int] = None
    title: Optional[str] = None
    link: Optional[str] = None
    link_xml: Optional[str] = None
    proposition_acronym: Optional[str] = None
    proposition_number: Optional[int] = None
    presentation_year: Optional[int] = None
    agency_id: Optional[int] = None
    proposition_type_id: Optional[int] = None
    proposition_status_id: Optional[int] = None
    current_status: Optional[str] = None
    proposition_description: Optional[str] = None
    presentation_date: Optional[date] = None
    presentation_month: Optional[int] = None
    summary: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    themes: Optional[List[str]] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


_THEMES_COLUMN_READY = False


def _themes_column_ready(db: Session) -> bool:
    """A coluna `themes` já existe? O deploy aplica a migration cs92 depois de
    subir a API; até lá, ler a coluna derrubaria toda consulta de proposição.

    Só o "sim" fica em cache (a coluna não some depois de criada), então o
    custo da checagem some após a primeira resposta positiva — importa porque
    a aba de proposições do perfil faz uma requisição por proposição.
    """
    global _THEMES_COLUMN_READY
    if _THEMES_COLUMN_READY:
        return True
    try:
        columns = sqlalchemy_inspect(db.get_bind()).get_columns("proposition")
    except SQLAlchemyError:
        return False
    _THEMES_COLUMN_READY = any(column.get("name") == "themes" for column in columns)
    return _THEMES_COLUMN_READY


def proposition_load_options(db: Session) -> list:
    """Opções de carga para quem serializa proposições com temas."""
    return [undefer(Proposition.themes)] if _themes_column_ready(db) else []


def _loaded_themes(proposition: Any) -> Optional[List[str]]:
    """Temas já carregados, sem nunca disparar carga preguiçosa da coluna."""
    try:
        state = sqlalchemy_inspect(proposition)
    except NoInspectionAvailable:
        themes = getattr(proposition, "themes", None)
    else:
        if "themes" in state.unloaded:
            return None
        themes = proposition.themes
    if not isinstance(themes, list):
        return None
    return [theme for theme in themes if isinstance(theme, str) and theme.strip()]


def _build_proposition_link(proposition: Proposition) -> Optional[str]:
    # TODO: Implementar a logica do link na fonte ao inves do codigo abaixo
    details = proposition.details if isinstance(proposition.details, dict) else {}
    full_text_url = details.get("urlInteiroTeor")
    if (
        proposition.proposition_acronym == "DOC"
        and isinstance(full_text_url, str)
        and full_text_url
    ):
        return full_text_url
    if isinstance(proposition.link, str) and "camara.leg.br" in proposition.link:
        # Proposições da Câmara (deputados) devem apontar para a ficha de tramitação.
        return proposition.link
    if proposition.proposition_code is not None:
        return f"{SENADO_PROPOSITION_BASE_URL}/{proposition.proposition_code}"
    if isinstance(proposition.link, str) and proposition.link:
        return proposition.link
    return None


def _serialize_proposition(proposition: Proposition) -> PropositionOut:
    computed_link = _build_proposition_link(proposition)
    return PropositionOut(
        id=proposition.id,
        proposition_code=proposition.proposition_code,
        title=proposition.title,
        link=computed_link,
        link_xml=proposition.link,
        proposition_acronym=proposition.proposition_acronym,
        proposition_number=proposition.proposition_number,
        presentation_year=proposition.presentation_year,
        agency_id=proposition.agency_id,
        proposition_type_id=proposition.proposition_type_id,
        proposition_status_id=proposition.proposition_status_id,
        current_status=proposition.current_status,
        proposition_description=proposition.proposition_description,
        presentation_date=proposition.presentation_date,
        presentation_month=proposition.presentation_month,
        summary=proposition.summary,
        details=proposition.details,
        themes=_loaded_themes(proposition),
        created_at=proposition.created_at,
        updated_at=proposition.updated_at,
    )


@router.get("/", response_model=List[PropositionOut])
def list_propositions(
    *,
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    year: Optional[int] = Query(None, description="Filtra pelo ano de apresentação."),
    acronym: Optional[str] = Query(
        None, description="Filtra pela sigla da proposição (ex: PEC, PL, etc)."
    ),
    presentation_date_from: Optional[date] = Query(
        None,
        description="Filtra por data de apresentação a partir desta data (inclusive).",
    ),
    presentation_date_to: Optional[date] = Query(
        None,
        description="Filtra por data de apresentação até esta data (inclusive).",
    ),
    created_from: Optional[datetime] = Query(
        None,
        description="Filtra por registros criados a partir deste instante (inclusive).",
    ),
    created_to: Optional[datetime] = Query(
        None,
        description="Filtra por registros criados até este instante (inclusive).",
    ),
    updated_from: Optional[datetime] = Query(
        None,
        description="Filtra por registros atualizados a partir deste instante (inclusive).",
    ),
    updated_to: Optional[datetime] = Query(
        None,
        description="Filtra por registros atualizados até este instante (inclusive).",
    ),
    sort_by: Literal[
        "created_at",
        "updated_at",
        "title",
        "presentation_date",
        "presentation_year",
        "proposition_number",
    ] = Query(default="created_at", description="Campo usado para ordenação."),
    sort_order: Literal["asc", "desc"] = Query(
        default="desc",
        description="Direção da ordenação.",
    ),
) -> List[PropositionOut]:
    """Retorna uma lista paginada de proposições."""
    stmt = (
        select(Proposition)
        .options(*proposition_load_options(db))
        .offset(offset)
        .limit(limit)
    )

    if year is not None:
        stmt = stmt.where(Proposition.presentation_year == year)
    if acronym:
        stmt = stmt.where(Proposition.proposition_acronym.ilike(f"%{acronym}%"))
    if presentation_date_from is not None:
        stmt = stmt.where(Proposition.presentation_date >= presentation_date_from)
    if presentation_date_to is not None:
        stmt = stmt.where(Proposition.presentation_date <= presentation_date_to)
    if created_from is not None:
        stmt = stmt.where(Proposition.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(Proposition.created_at <= created_to)
    if updated_from is not None:
        stmt = stmt.where(Proposition.updated_at >= updated_from)
    if updated_to is not None:
        stmt = stmt.where(Proposition.updated_at <= updated_to)

    sortable_columns = {
        "created_at": Proposition.created_at,
        "updated_at": Proposition.updated_at,
        "title": Proposition.title,
        "presentation_date": Proposition.presentation_date,
        "presentation_year": Proposition.presentation_year,
        "proposition_number": Proposition.proposition_number,
    }
    sort_column = sortable_columns[sort_by]
    stmt = stmt.order_by(asc(sort_column) if sort_order == "asc" else desc(sort_column))

    result = db.execute(stmt)
    propositions = result.scalars().all()
    return [_serialize_proposition(proposition) for proposition in propositions]


@router.get("/{proposition_id}", response_model=PropositionOut)
def get_proposition(
    proposition_id: int,
    db: Session = Depends(get_db),
) -> PropositionOut:
    """Recupera detalhes de uma proposição específica."""
    stmt = (
        select(Proposition)
        .options(*proposition_load_options(db))
        .where(Proposition.id == proposition_id)
    )
    result = db.execute(stmt).scalar_one_or_none()

    if result is None:
        raise HTTPException(status_code=404, detail="Proposição não encontrada.")

    return _serialize_proposition(result)


__all__ = ["router"]
