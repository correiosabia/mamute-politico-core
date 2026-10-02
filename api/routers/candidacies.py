from __future__ import annotations

from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.orm import Session

try:
    # Execução como pacote (api.routers.candidacies).
    from ..db.models.candidacy import Candidacy
    from ..db.models.project import ProjetosCandidacy
    from ..dependencies import get_db
    from .projects import _get_project_from_token_email
except (ImportError, ValueError):
    # Execução local dentro de api/ sem reconhecimento de pacote.
    from db.models.candidacy import Candidacy
    from db.models.project import ProjetosCandidacy
    from dependencies import get_db
    from routers.projects import _get_project_from_token_email

router = APIRouter(prefix="/candidacies", tags=["candidacies"])

DEFAULT_ELECTION_YEAR = 2026

# Nomes dos cargos por codigo da DivulgaCandContas
OFFICE_NAMES = {
    1: "Presidente",
    3: "Governador",
    5: "Senador",
    6: "Deputado Federal",
    7: "Deputado Estadual",
    8: "Deputado Distrital",
}


class ViceOut(BaseModel):
    """Vice ou suplente que compõe a chapa do titular (CS-113)."""

    name: str
    role: Optional[str] = None
    party: Optional[str] = None


class CandidacyOut(BaseModel):
    """Uma candidatura na lista de resultados da busca."""

    id: int
    election_year: int
    tse_candidate_id: int
    office_code: Optional[int] = None
    office: Optional[str] = None
    state: Optional[str] = None
    ballot_number: Optional[int] = None
    ballot_name: Optional[str] = None
    full_name: Optional[str] = None
    party: Optional[str] = None
    coalition: Optional[str] = None
    status: Optional[str] = None
    photo_url: Optional[str] = None
    # `parliamentarian_id` nao tem parlamentar correspondente na base.
    parliamentarian_id: Optional[int] = None
    match_status: str
    # Vice (Presidente, Governador) ou suplentes (Senador) da chapa. Desde 2026
    # eles não têm linha própria: só existem dentro do detalhe do titular.
    vices: List[ViceOut] = []

    model_config = ConfigDict(from_attributes=True)


class OfficeOut(BaseModel):

    code: int
    name: str


class CandidacyFiltersOut(BaseModel):

    election_years: List[int]
    states: List[str]
    offices: List[OfficeOut]
    parties: List[str]


def _texto(valor: Any) -> Optional[str]:
    if valor is None:
        return None
    limpo = " ".join(str(valor).split())
    return limpo or None


def _extrair_vices(details: Any) -> List[ViceOut]:
    """Lê `details["vices"]` (payload de detalhe da DivulgaCandContas).

    O payload é guardado cru pelo tse_crawler, então a leitura é defensiva:
    aceita as chaves no formato da API (`nm_URNA`, `ds_CARGO`, `sg_PARTIDO`) e
    no formato camelCase do titular, e pula item sem nome em vez de quebrar a
    listagem inteira.
    """
    if not isinstance(details, dict):
        return []
    itens = details.get("vices")
    if not isinstance(itens, list):
        return []

    vices: List[ViceOut] = []
    for item in itens:
        if not isinstance(item, dict):
            continue
        nome = _texto(
            item.get("nm_URNA")
            or item.get("nomeUrna")
            or item.get("nm_CANDIDATO")
            or item.get("nomeCompleto")
        )
        if not nome:
            continue
        partido = item.get("partido")
        sigla = partido.get("sigla") if isinstance(partido, dict) else None
        cargo = item.get("cargo")
        cargo_nome = cargo.get("nome") if isinstance(cargo, dict) else None
        vices.append(
            ViceOut(
                name=nome,
                role=_texto(item.get("ds_CARGO") or item.get("descricaoCargo") or cargo_nome),
                party=_texto(item.get("sg_PARTIDO") or sigla),
            )
        )
    return vices


def _serialize(candidacy: Candidacy) -> CandidacyOut:
    out = CandidacyOut.model_validate(candidacy)
    if not out.office and out.office_code is not None:
        out.office = OFFICE_NAMES.get(out.office_code)
    out.vices = _extrair_vices(candidacy.details)
    return out


@router.get("/filters", response_model=CandidacyFiltersOut)
def get_candidacy_filters(
    *,
    db: Session = Depends(get_db),
    election_year: int = Query(
        DEFAULT_ELECTION_YEAR,
        description="Eleição cujos estados, cargos e partidos serão listados.",
    ),
) -> CandidacyFiltersOut:
    """Anos da base e, para a eleição pedida, UFs, cargos e partidos.

    Sai do banco em vez de constante no front para o dropdown nunca oferecer
    um filtro que devolveria lista vazia. Por isso UF, cargo e partido são
    recortados pela eleição (CS-113): vice e suplente só têm linha própria nas
    cargas de CSV de 2010 a 2022 e apareciam no dropdown de 2026 sem resultado.
    """
    years = [
        row
        for row in db.execute(
            select(Candidacy.election_year)
            .distinct()
            .order_by(desc(Candidacy.election_year))
        )
        .scalars()
        .all()
        if row is not None
    ]
    da_eleicao = Candidacy.election_year == election_year
    states = [
        row
        for row in db.execute(
            select(Candidacy.state)
            .where(da_eleicao)
            .distinct()
            .order_by(asc(Candidacy.state))
        )
        .scalars()
        .all()
        if row
    ]
    office_rows = db.execute(
        select(Candidacy.office_code, func.min(Candidacy.office))
        .where(da_eleicao, Candidacy.office_code.is_not(None))
        .group_by(Candidacy.office_code)
        .order_by(asc(Candidacy.office_code))
    ).all()
    offices = [
        OfficeOut(code=code, name=name or OFFICE_NAMES.get(code) or str(code))
        for code, name in office_rows
    ]
    parties = [
        row
        for row in db.execute(
            select(Candidacy.party)
            .where(da_eleicao)
            .distinct()
            .order_by(asc(Candidacy.party))
        )
        .scalars()
        .all()
        if row
    ]
    return CandidacyFiltersOut(
        election_years=years, states=states, offices=offices, parties=parties
    )


@router.get("/", response_model=List[CandidacyOut])
def list_candidacies(
    *,
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    election_year: int = Query(
        DEFAULT_ELECTION_YEAR, description="Ano da eleição das candidaturas."
    ),
    name: Optional[str] = Query(
        default=None,
        min_length=2,
        description="Busca por nome de urna ou nome completo (case-insensitive).",
    ),
    state: Optional[str] = Query(
        default=None, description="UF da candidatura (ex.: CE). 'BR' para presidente."
    ),
    office_code: Optional[int] = Query(
        default=None,
        description=(
            "Código do cargo na DivulgaCandContas: 1 presidente, 3 governador, "
            "5 senador, 6 dep. federal, 7 dep. estadual, 8 dep. distrital."
        ),
    ),
    party: Optional[str] = Query(
        default=None, description="Sigla do partido, como em /filters (ex.: PT)."
    ),
    only_followed: bool = Query(
        default=False,
        description="Só as candidaturas que o usuário autenticado acompanha.",
    ),
    sort_by: Literal["ballot_name", "full_name", "state", "party"] = Query(
        default="ballot_name", description="Campo usado para ordenação."
    ),
    sort_order: Literal["asc", "desc"] = Query(
        default="asc", description="Direção da ordenação."
    ),
) -> List[CandidacyOut]:
    """Lista paginada de candidaturas, com busca por nome e filtros de UF,
    cargo, partido e "só as que acompanho"."""
    stmt = select(Candidacy).where(Candidacy.election_year == election_year)

    if name:
        # `strip` evita que espaço colado no fim vire filtro que não casa nada.
        termo = f"%{name.strip()}%"
        # Os dois lados passam pela mesma funcao: dobrar so a coluna faria
        # "JOÃO" digitado pelo usuario deixar de casar com o indice dobrado.
        padrao = func.unaccent_imutavel(termo)
        stmt = stmt.where(
            or_(
                func.unaccent_imutavel(Candidacy.ballot_name).ilike(padrao),
                func.unaccent_imutavel(Candidacy.full_name).ilike(padrao),
            )
        )

    if state:
        stmt = stmt.where(Candidacy.state == state.strip().upper())

    if office_code is not None:
        stmt = stmt.where(Candidacy.office_code == office_code)

    if party:
        # Sem upper(): há sigla com caixa mista na fonte ("Solidariedade").
        stmt = stmt.where(Candidacy.party == party.strip())

    if only_followed:
        # Mesmo dono das rotas /projects/me/candidacy-favorites: o projeto do
        # e-mail do token. Sem projeto identificado, responde 401/404 em vez de
        # uma lista vazia que esconderia o problema.
        project = _get_project_from_token_email(request, db)
        stmt = stmt.where(
            Candidacy.id.in_(
                select(ProjetosCandidacy.candidacy_id).where(
                    ProjetosCandidacy.projeto_id == project.id
                )
            )
        )

    sortable_columns = {
        "ballot_name": Candidacy.ballot_name,
        "full_name": Candidacy.full_name,
        "state": Candidacy.state,
        "party": Candidacy.party,
    }
    sort_column = sortable_columns[sort_by]
    stmt = stmt.order_by(asc(sort_column) if sort_order == "asc" else desc(sort_column))
    # `id` como desempate: sem ele, paginação com nomes repetidos pode repetir
    # ou perder linha entre páginas.
    stmt = stmt.order_by(asc(Candidacy.id)).offset(offset).limit(limit)

    return [_serialize(c) for c in db.execute(stmt).scalars().all()]
