from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.orm import Session

try:
    # Execução como pacote (api.routers.candidacies).
    from ..db.models.candidacy import Candidacy
    from ..db.models.election_result import CandidacyResult
    from ..dependencies import get_db
except (ImportError, ValueError):
    # Execução local dentro de api/ sem reconhecimento de pacote.
    from db.models.candidacy import Candidacy
    from db.models.election_result import CandidacyResult
    from dependencies import get_db

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
    # Resultado oficial encerrado (CS-106/CS-108); None antes da totalizacao.
    resultado: Optional["CandidacyResultOut"] = None

    model_config = ConfigDict(from_attributes=True)


class CandidacyResultOut(BaseModel):
    """Situacao oficial do TSE no turno mais recente ja encerrado."""

    turno: int
    situacao: Optional[str] = None
    eleito: Optional[bool] = None
    # True quando saiu da disputa (nao eleito, suplente, derrotado no 2o turno):
    # o app mostra "Não eleito" e, para presidente/governador, oferece a
    # escolha de quem foi ao 2o turno.
    fora_da_disputa: bool = False


SITUACAO_SEGUNDO_TURNO = "2º turno"


def _resultados_por_candidatura(db: Session, ids: List[int]) -> dict[int, CandidacyResultOut]:
    if not ids:
        return {}
    rows = db.execute(
        select(CandidacyResult)
        .where(
            CandidacyResult.candidacy_id.in_(ids),
            CandidacyResult.totalizacao_final.is_(True),
        )
        .order_by(asc(CandidacyResult.turno))
    ).scalars()
    out: dict[int, CandidacyResultOut] = {}
    for r in rows:  # turno crescente: o ultimo encerrado prevalece
        out[int(r.candidacy_id)] = CandidacyResultOut(
            turno=r.turno,
            situacao=r.situacao,
            eleito=r.eleito,
            fora_da_disputa=r.eleito is False,
        )
    return out


CandidacyOut.model_rebuild()


class OfficeOut(BaseModel):

    code: int
    name: str


class CandidacyFiltersOut(BaseModel):

    election_years: List[int]
    states: List[str]
    offices: List[OfficeOut]


def _serialize(candidacy: Candidacy) -> CandidacyOut:
    out = CandidacyOut.model_validate(candidacy)
    if not out.office and out.office_code is not None:
        out.office = OFFICE_NAMES.get(out.office_code)
    return out


@router.get("/filters", response_model=CandidacyFiltersOut)
def get_candidacy_filters(
    *,
    db: Session = Depends(get_db),
) -> CandidacyFiltersOut:
    """Anos, UFs e cargos presentes na base, para montar os filtros da tela.

    Sai do banco em vez de constante no front para o dropdown nunca oferecer
    um filtro que devolveria lista vazia.
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
    states = [
        row
        for row in db.execute(
            select(Candidacy.state).distinct().order_by(asc(Candidacy.state))
        )
        .scalars()
        .all()
        if row
    ]
    office_rows = db.execute(
        select(Candidacy.office_code, func.min(Candidacy.office))
        .where(Candidacy.office_code.is_not(None))
        .group_by(Candidacy.office_code)
        .order_by(asc(Candidacy.office_code))
    ).all()
    offices = [
        OfficeOut(code=code, name=name or OFFICE_NAMES.get(code) or str(code))
        for code, name in office_rows
    ]
    return CandidacyFiltersOut(
        election_years=years, states=states, offices=offices
    )


@router.get("/", response_model=List[CandidacyOut])
def list_candidacies(
    *,
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
    sort_by: Literal["ballot_name", "full_name", "state", "party"] = Query(
        default="ballot_name", description="Campo usado para ordenação."
    ),
    sort_order: Literal["asc", "desc"] = Query(
        default="asc", description="Direção da ordenação."
    ),
    resultado: Optional[Literal["segundo_turno"]] = Query(
        default=None,
        description="segundo_turno: só quem foi ao 2º turno (nova escolha, CS-108).",
    ),
) -> List[CandidacyOut]:
    """Lista paginada de candidaturas, com busca por nome e filtros de UF e cargo."""
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

    if resultado == "segundo_turno":
        stmt = stmt.where(
            select(CandidacyResult.id)
            .where(
                CandidacyResult.candidacy_id == Candidacy.id,
                CandidacyResult.turno == 1,
                CandidacyResult.situacao == SITUACAO_SEGUNDO_TURNO,
                CandidacyResult.totalizacao_final.is_(True),
            )
            .exists()
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

    candidacies = db.execute(stmt).scalars().all()
    resultados = _resultados_por_candidatura(db, [int(c.id) for c in candidacies])
    out = []
    for c in candidacies:
        item = _serialize(c)
        item.resultado = resultados.get(int(c.id))
        out.append(item)
    return out
