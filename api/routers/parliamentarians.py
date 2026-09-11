"""Rotas relacionadas a parlamentares."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
import logging
import os
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, asc, desc, or_, select
from sqlalchemy.orm import Session, selectinload

try:
    # Execução como pacote (api.routers.parliamentarians).
    from ..db.models.editorial_agenda import EditorialAgenda, ParliamentarianAgenda
    from ..services.editorial_agendas import tabelas_disponiveis
    from ..db.models.parliamentarian import Parliamentarian
    from ..db.models.social_network import ParliamentarianSocialNetwork
    from ..dependencies import get_db
except (ImportError, ValueError):
    # Execução local dentro de api/ sem reconhecimento de pacote.
    from db.models.editorial_agenda import EditorialAgenda, ParliamentarianAgenda
    from services.editorial_agendas import tabelas_disponiveis
    from db.models.parliamentarian import Parliamentarian
    from db.models.social_network import ParliamentarianSocialNetwork
    from dependencies import get_db

router = APIRouter(prefix="/parliamentarians", tags=["parliamentarians"])
logger = logging.getLogger(__name__)


class ParliamentarianSituation(str, Enum):
    """Situações de mandato que podem ser expostas pelo catálogo."""

    EXERCICIO = "exercicio"
    AFASTADO = "afastado"
    LICENCIADO = "licenciado"
    FIM_DE_MANDATO = "fim_de_mandato"


class ParliamentarianCatalogScope(str, Enum):
    """Escopos de visibilidade do catálogo, configurados no deployment da API."""

    CURRENT_ONLY = "current_only"
    CURRENT_AND_LICENSED = "current_and_licensed"
    ALL_INGESTED = "all_ingested"


MAMUTE_PARLIAMENTARIAN_CATALOG_SCOPE = "MAMUTE_PARLIAMENTARIAN_CATALOG_SCOPE"
DEFAULT_PARLIAMENTARIAN_CATALOG_SCOPE = ParliamentarianCatalogScope.CURRENT_ONLY

_CATALOG_SCOPE_SITUATIONS: Dict[
    ParliamentarianCatalogScope, Tuple[ParliamentarianSituation, ...]
] = {
    ParliamentarianCatalogScope.CURRENT_ONLY: (ParliamentarianSituation.EXERCICIO,),
    ParliamentarianCatalogScope.CURRENT_AND_LICENSED: (
        ParliamentarianSituation.EXERCICIO,
        ParliamentarianSituation.LICENCIADO,
    ),
    ParliamentarianCatalogScope.ALL_INGESTED: tuple(ParliamentarianSituation),
}


class ParliamentarianCatalogConfigOut(BaseModel):
    """Configuração pública do catálogo para clientes autenticados."""

    allowed_situations: List[ParliamentarianSituation]
    default_situacao: ParliamentarianSituation


def get_parliamentarian_catalog_scope(
    value: Optional[str] = None,
) -> ParliamentarianCatalogScope:
    """Lê o escopo da API com fallback seguro para parlamentares em exercício."""
    configured = value if value is not None else os.getenv(MAMUTE_PARLIAMENTARIAN_CATALOG_SCOPE)
    normalized = (configured or DEFAULT_PARLIAMENTARIAN_CATALOG_SCOPE.value).strip().lower()
    try:
        return ParliamentarianCatalogScope(normalized)
    except ValueError:
        logger.warning(
            "%s=%r é inválida; usando o padrão seguro %s.",
            MAMUTE_PARLIAMENTARIAN_CATALOG_SCOPE,
            configured,
            DEFAULT_PARLIAMENTARIAN_CATALOG_SCOPE.value,
        )
        return DEFAULT_PARLIAMENTARIAN_CATALOG_SCOPE


def get_parliamentarian_catalog_config(
    value: Optional[str] = None,
) -> ParliamentarianCatalogConfigOut:
    """Resolve a política exposta à UI no runtime do deployment."""
    scope = get_parliamentarian_catalog_scope(value)
    allowed_situations = list(_CATALOG_SCOPE_SITUATIONS[scope])
    return ParliamentarianCatalogConfigOut(
        allowed_situations=allowed_situations,
        default_situacao=ParliamentarianSituation.EXERCICIO,
    )


def _resolve_catalog_situacao(
    situacao: Optional[ParliamentarianSituation],
    *,
    config: Optional[ParliamentarianCatalogConfigOut] = None,
) -> ParliamentarianSituation:
    """Aplica a política antes de consultar o banco, sem vazar situações ocultas."""
    resolved_config = config or get_parliamentarian_catalog_config()
    requested = situacao or resolved_config.default_situacao
    if requested not in resolved_config.allowed_situations:
        raise HTTPException(
            status_code=403,
            detail="A situação solicitada não está disponível neste catálogo.",
        )
    return requested

_SENADO_STATUS_KEYS = (
    "status",
    "situacao",
    "Situacao",
    "SituacaoParlamentar",
    "DescricaoSituacao",
    "DescricaoStatus",
)


def _coerce_non_empty_str(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _is_senador_type(parliamentarian_type: Optional[str]) -> bool:
    if not parliamentarian_type:
        return False
    return "senad" in parliamentarian_type.lower()


def _parse_iso_date(value: Any) -> Optional[date]:
    text = _coerce_non_empty_str(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _find_senado_status_in_object(obj: Dict[str, Any]) -> Optional[str]:
    for key in _SENADO_STATUS_KEYS:
        parsed = _coerce_non_empty_str(obj.get(key))
        if parsed:
            return parsed

    for nested_key in ("IdentificacaoParlamentar", "Mandato", "Mandatos", "DadosBasicosParlamentar"):
        nested = obj.get(nested_key)
        if isinstance(nested, dict):
            parsed = _find_senado_status_in_object(nested)
            if parsed:
                return parsed

    return None


def _collect_mandato_legislatura_periods(mandato: Dict[str, Any]) -> List[Tuple[date, date]]:
    periods: List[Tuple[date, date]] = []
    for key in ("PrimeiraLegislaturaDoMandato", "SegundaLegislaturaDoMandato"):
        legislatura = mandato.get(key)
        if not isinstance(legislatura, dict):
            continue
        start = _parse_iso_date(legislatura.get("DataInicio"))
        end = _parse_iso_date(legislatura.get("DataFim"))
        if start is not None and end is not None:
            periods.append((start, end))
    return periods


def _derive_senado_status_from_mandato(
    details: Dict[str, Any],
    *,
    reference_date: Optional[date] = None,
) -> Optional[str]:
    """Infere situação do senador a partir das datas de legislatura no mandato."""
    today = reference_date or date.today()

    for root_key in ("lista", "detalhe"):
        root = details.get(root_key)
        if not isinstance(root, dict):
            continue
        mandato = root.get("Mandato")
        if not isinstance(mandato, dict):
            continue

        periods = _collect_mandato_legislatura_periods(mandato)
        if not periods:
            continue

        if any(start <= today <= end for start, end in periods):
            return "Exercício"

        latest_end = max(end for _, end in periods)
        if today > latest_end:
            return "Fim de mandato"

        earliest_start = min(start for start, _ in periods)
        if today < earliest_start:
            return "Fora de exercício"

    return None


def _resolve_parliamentarian_status(
    *,
    parliamentarian_type: Optional[str],
    stored_status: Optional[str],
    details: Optional[Dict[str, Any]],
    reference_date: Optional[date] = None,
) -> Optional[str]:
    """Preenche status a partir da coluna, details ou mandato (Senado)."""
    resolved = _coerce_non_empty_str(stored_status)
    if resolved:
        return resolved

    if not details or not isinstance(details, dict):
        return None

    if not _is_senador_type(parliamentarian_type):
        ultimo_status = details.get("ultimoStatus")
        if isinstance(ultimo_status, dict):
            return _coerce_non_empty_str(ultimo_status.get("situacao"))
        return None

    for root_key in ("lista", "detalhe"):
        root = details.get(root_key)
        if isinstance(root, dict):
            parsed = _find_senado_status_in_object(root)
            if parsed:
                return parsed

    derived = _derive_senado_status_from_mandato(details, reference_date=reference_date)
    if derived:
        return derived

    # Lista oficial do Senado é "ParlamentarEmExercicio".
    return "Exercício"


def _extract_photo_url_from_details(details: Optional[Dict[str, Any]]) -> Optional[str]:
    """Extrai URL da foto a partir de details (Senado ou Câmara)."""
    if not details or not isinstance(details, dict):
        return None

    direct = details.get("UrlFotoParlamentar")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    lista = details.get("lista")
    if isinstance(lista, dict):
        ident = lista.get("IdentificacaoParlamentar")
        if isinstance(ident, dict):
            url = ident.get("UrlFotoParlamentar")
            if isinstance(url, str) and url.strip():
                return url.strip()

    detalhe = details.get("detalhe")
    if isinstance(detalhe, dict):
        ident = detalhe.get("IdentificacaoParlamentar")
        if isinstance(ident, dict):
            url = ident.get("UrlFotoParlamentar")
            if isinstance(url, str) and url.strip():
                return url.strip()

    ultimo_status = details.get("ultimoStatus")
    if isinstance(ultimo_status, dict):
        url = ultimo_status.get("urlFoto")
        if isinstance(url, str) and url.strip():
            return url.strip()

    return None


def _enrich_details_with_photo_url(details: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Garante UrlFotoParlamentar em details para consumo uniforme (ex.: deputados da Câmara)."""
    if not details or not isinstance(details, dict):
        return details

    photo_url = _extract_photo_url_from_details(details)
    if not photo_url:
        return details

    if details.get("UrlFotoParlamentar"):
        return details

    enriched = dict(details)
    enriched["UrlFotoParlamentar"] = photo_url
    return enriched


def _serialize_parliamentarian(parliamentarian: Parliamentarian) -> "ParliamentarianOut":
    details = _enrich_details_with_photo_url(parliamentarian.details)
    photo_url = _extract_photo_url_from_details(details)
    return ParliamentarianOut(
        id=parliamentarian.id,
        type=parliamentarian.type,
        parliamentarian_code=parliamentarian.parliamentarian_code,
        name=parliamentarian.name,
        full_name=parliamentarian.full_name,
        email=parliamentarian.email,
        telephone=parliamentarian.telephone,
        cpf=parliamentarian.cpf,
        status=_resolve_parliamentarian_status(
            parliamentarian_type=parliamentarian.type,
            stored_status=parliamentarian.status,
            details=details,
        ),
        party=parliamentarian.party,
        state_of_birth=parliamentarian.state_of_birth,
        city_of_birth=parliamentarian.city_of_birth,
        state_elected=parliamentarian.state_elected,
        site=parliamentarian.site,
        education=parliamentarian.education,
        office_name=parliamentarian.office_name,
        office_building=parliamentarian.office_building,
        office_number=parliamentarian.office_number,
        office_floor=parliamentarian.office_floor,
        office_email=parliamentarian.office_email,
        biography_link=parliamentarian.biography_link,
        biography_text=parliamentarian.biography_text,
        details=details,
        photo_url=photo_url,
        created_at=parliamentarian.created_at,
        updated_at=parliamentarian.updated_at,
    )


class EditorialAgendaOut(BaseModel):
    """Pauta editorial atribuída a um parlamentar (CS-72)."""

    id: int
    name: str
    slug: str
    rank: int

    model_config = ConfigDict(from_attributes=True)


class ParliamentarianOut(BaseModel):
    """Representação serializada de um parlamentar."""

    id: int
    type: Optional[str] = None
    parliamentarian_code: Optional[int] = None
    name: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    telephone: Optional[str] = None
    cpf: Optional[str] = None
    status: Optional[str] = None
    party: Optional[str] = None
    state_of_birth: Optional[str] = None
    city_of_birth: Optional[str] = None
    state_elected: Optional[str] = None
    site: Optional[str] = None
    education: Optional[str] = None
    office_name: Optional[str] = None
    office_building: Optional[str] = None
    office_number: Optional[str] = None
    office_floor: Optional[str] = None
    office_email: Optional[str] = None
    biography_link: Optional[str] = None
    biography_text: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    photo_url: Optional[str] = None
    agendas: List[EditorialAgendaOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SocialNetworkLinkOut(BaseModel):
    """Rede social vinculada a um parlamentar."""

    name: Optional[str] = None
    profile_url: Optional[str] = None


class ParliamentarianDetailOut(ParliamentarianOut):
    """Representação detalhada de um parlamentar, incluindo redes sociais."""

    social_networks: List[SocialNetworkLinkOut] = Field(default_factory=list)


def _serialize_parliamentarian_detail(parliamentarian: Parliamentarian) -> ParliamentarianDetailOut:
    """Serializa um parlamentar com suas redes sociais."""
    base = ParliamentarianOut.model_validate(parliamentarian)
    social_networks = [
        SocialNetworkLinkOut(
            name=link.social_network.name if link.social_network else None,
            profile_url=link.profile_url,
        )
        for link in parliamentarian.social_networks
        if link.profile_url or (link.social_network and link.social_network.name)
    ]
    return ParliamentarianDetailOut(**base.model_dump(), social_networks=social_networks)


def _attach_agendas(db: Session, parlamentares: List[ParliamentarianOut]) -> None:

    if not parlamentares:
        return
    if not tabelas_disponiveis(db):
        return

    ids = [p.id for p in parlamentares]
    linhas = db.execute(
        select(
            ParliamentarianAgenda.parliamentarian_id,
            EditorialAgenda.id,
            EditorialAgenda.name,
            EditorialAgenda.slug,
            ParliamentarianAgenda.rank,
        )
        .join(EditorialAgenda, EditorialAgenda.id == ParliamentarianAgenda.agenda_id)
        # Pauta desativada no admin some da API mas não é apagada: a linha de
        # classificação continua lá, dormente, e volta se a pauta voltar.
        .where(
            EditorialAgenda.active.is_(True),
            ParliamentarianAgenda.parliamentarian_id.in_(ids),
        )
        .order_by(
            ParliamentarianAgenda.parliamentarian_id,
            ParliamentarianAgenda.rank,
        )
    ).all()

    por_parlamentar: Dict[int, List[EditorialAgendaOut]] = {}
    for parliamentarian_id, agenda_id, name, slug, rank in linhas:
        por_parlamentar.setdefault(parliamentarian_id, []).append(
            EditorialAgendaOut(id=agenda_id, name=name, slug=slug, rank=rank)
        )

    for parlamentar in parlamentares:
        parlamentar.agendas = por_parlamentar.get(parlamentar.id, [])


def _apply_situacao_filter(stmt, situacao: str):
    """Aplica filtro de situação parlamentar com base na coluna status."""
    is_deputado = Parliamentarian.type.ilike("%Deput%")
    is_senador = Parliamentarian.type.ilike("%Senad%")

    if situacao == "exercicio":
        return stmt.where(
            or_(
                and_(is_deputado, Parliamentarian.status.ilike("%exerc%")),
                is_senador,
            )
        )
    if situacao == "afastado":
        return stmt.where(
            or_(
                Parliamentarian.status.ilike("%afast%"),
                Parliamentarian.status.ilike("%fora de exerc%"),
            )
        )
    if situacao == "licenciado":
        return stmt.where(Parliamentarian.status.ilike("%licenc%"))
    if situacao == "fim_de_mandato":
        return stmt.where(
            or_(
                Parliamentarian.status.ilike("%fim de mandato%"),
                Parliamentarian.status.ilike("%vac%"),
                and_(
                    is_deputado,
                    or_(
                        Parliamentarian.status.is_(None),
                        Parliamentarian.name.is_(None),
                        Parliamentarian.party.is_(None),
                    ),
                ),
            )
        )
    return stmt


def is_parliamentarian_visible(db: Session, parliamentarian_id: int) -> bool:
    """Diz se o parlamentar aparece sob a política de catálogo vigente.

    Existe para manter a política num lugar só: quem grava marcação pessoal
    (tag, e adiante voto) não pode alcançar quem o catálogo esconde. Reescrever
    o predicado fora daqui é exatamente como as duas visões divergem — e a
    divergência viraria um oráculo de existência para ids ocultos.
    """
    config = get_parliamentarian_catalog_config()
    for situacao in config.allowed_situations:
        stmt = select(Parliamentarian.id).where(Parliamentarian.id == parliamentarian_id)
        if db.execute(_apply_situacao_filter(stmt, situacao.value)).first() is not None:
            return True
    return False


@router.get("/catalog-config", response_model=ParliamentarianCatalogConfigOut)
def get_catalog_config() -> ParliamentarianCatalogConfigOut:
    """Retorna a política de visibilidade em vigor para o cliente autenticado."""
    return get_parliamentarian_catalog_config()


@router.get("/", response_model=List[ParliamentarianOut])
def list_parliamentarians(
    *,
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    party: Optional[str] = Query(default=None, description="Filtrar por partido"),
    type: Optional[List[Literal["deputado", "senado"]]] = Query(
        default=None,
        description="Filtrar por tipo de parlamentar: deputado, senado (pode repetir para ambos).",
    ),
    situacao: Optional[ParliamentarianSituation] = Query(
        default=None,
        description="Filtrar por situação do mandato: exercicio, afastado, licenciado, fim_de_mandato.",
    ),
    created_from: Optional[datetime] = Query(
        default=None,
        description="Filtra por registros criados a partir deste instante (inclusive).",
    ),
    created_to: Optional[datetime] = Query(
        default=None,
        description="Filtra por registros criados até este instante (inclusive).",
    ),
    updated_from: Optional[datetime] = Query(
        default=None,
        description="Filtra por registros atualizados a partir deste instante (inclusive).",
    ),
    updated_to: Optional[datetime] = Query(
        default=None,
        description="Filtra por registros atualizados até este instante (inclusive).",
    ),
    sort_by: Literal["created_at", "updated_at", "name", "full_name", "party"] = Query(
        default="created_at",
        description="Campo usado para ordenação.",
    ),
    sort_order: Literal["asc", "desc"] = Query(
        default="desc",
        description="Direção da ordenação.",
    ),
) -> List[ParliamentarianOut]:
    """Retorna uma lista paginada de parlamentares."""
    stmt = select(Parliamentarian).offset(offset).limit(limit)

    if party:
        stmt = stmt.where(Parliamentarian.party.ilike(f"%{party}%"))

    if type:
        normalized_types = set(type)
        type_filters = []
        if "deputado" in normalized_types:
            type_filters.append(Parliamentarian.type.ilike("%Deput%"))
        if "senado" in normalized_types:
            # Banco pode armazenar "senador" ou "senado".
            type_filters.append(Parliamentarian.type.ilike("%Senad%"))
        if type_filters:
            stmt = stmt.where(or_(*type_filters))

    effective_situacao = _resolve_catalog_situacao(situacao)
    stmt = _apply_situacao_filter(stmt, effective_situacao)

    if created_from is not None:
        stmt = stmt.where(Parliamentarian.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(Parliamentarian.created_at <= created_to)
    if updated_from is not None:
        stmt = stmt.where(Parliamentarian.updated_at >= updated_from)
    if updated_to is not None:
        stmt = stmt.where(Parliamentarian.updated_at <= updated_to)

    sortable_columns = {
        "created_at": Parliamentarian.created_at,
        "updated_at": Parliamentarian.updated_at,
        "name": Parliamentarian.name,
        "full_name": Parliamentarian.full_name,
        "party": Parliamentarian.party,
    }
    sort_column = sortable_columns[sort_by]
    stmt = stmt.order_by(asc(sort_column) if sort_order == "asc" else desc(sort_column))

    result = db.execute(stmt)
    saida = [_serialize_parliamentarian(p) for p in result.scalars().all()]
    _attach_agendas(db, saida)
    return saida


@router.get("/{parliamentarian_id}", response_model=ParliamentarianDetailOut)
def get_parliamentarian(
    parliamentarian_id: int,
    db: Session = Depends(get_db),
) -> ParliamentarianDetailOut:
    """Busca um parlamentar específico pelo identificador."""
    stmt = (
        select(Parliamentarian)
        .where(Parliamentarian.id == parliamentarian_id)
        .options(
            selectinload(Parliamentarian.social_networks).selectinload(
                ParliamentarianSocialNetwork.social_network
            )
        )
    )
    result = db.execute(stmt).scalar_one_or_none()

    if result is None:
        raise HTTPException(status_code=404, detail="Parlamentar não encontrado.")

    saida = _serialize_parliamentarian_detail(result)
    _attach_agendas(db, [saida])
    return saida


__all__ = ["router"]
