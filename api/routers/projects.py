"""Rotas relacionadas a projetos e seus favoritos."""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta
import json
import os
from typing import Any, List, Literal, Mapping, Optional
from zoneinfo import ZoneInfo
import unicodedata

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import asc, desc, func, null, nullslast, select
from sqlalchemy.orm import Session, selectinload

try:
    # Execução como pacote (api.routers.projects).
    from ..db.models.parliamentarian import Parliamentarian
    from ..db.models.authors_proposition import AuthorsProposition
    from ..db.models.committee_attendance import CommitteeAttendance
    from ..db.models.plenary_attendance import PlenaryAttendance
    from ..db.models.proposition import Proposition
    from ..db.models.candidacy import Candidacy
    from ..db.models.project import Projetos, ProjetosCandidacy, ProjetosParliamentarian
    from ..db.models.personal_marks import (
        ParliamentarianTag,
        ProjectMamutometro,
        ProjectTag,
    )
    from ..services.marcacoes import (
        esta_no_escopo,
        get_config as get_marcacoes_config,
        mamutometro_habilitado,
        mamutometro_limite,
    )
    from ..db.models.usage_event import UsageEvent
    from ..db.models.roll_call_votes import RollCallVote
    from ..db.models.speeches_transcripts import SpeechesTranscript
    from ..dependencies import get_db
    from ..security import get_admin_settings, resolve_ghost_admin
    from .parliamentarians import is_parliamentarian_visible
    from .propositions import PropositionOut, _serialize_proposition
    from .roll_call_votes import (
        RollCallVoteOut,
        _list_roll_call_votes_without_vote_date,
        _serialize_roll_call_vote,
        _table_has_column,
    )
except (ImportError, ValueError):  # pragma: no cover - caminho alternativo
    # Execução local dentro de api/ sem reconhecimento de pacote.
    from db.models.parliamentarian import Parliamentarian
    from db.models.authors_proposition import AuthorsProposition
    from db.models.committee_attendance import CommitteeAttendance
    from db.models.plenary_attendance import PlenaryAttendance
    from db.models.proposition import Proposition
    from db.models.candidacy import Candidacy
    from db.models.project import Projetos, ProjetosCandidacy, ProjetosParliamentarian
    from db.models.personal_marks import (
        ParliamentarianTag,
        ProjectMamutometro,
        ProjectTag,
    )
    from services.marcacoes import (
        esta_no_escopo,
        get_config as get_marcacoes_config,
        mamutometro_habilitado,
        mamutometro_limite,
    )
    from db.models.usage_event import UsageEvent
    from db.models.roll_call_votes import RollCallVote
    from db.models.speeches_transcripts import SpeechesTranscript
    from dependencies import get_db
    from security import get_admin_settings, resolve_ghost_admin
    from routers.parliamentarians import is_parliamentarian_visible
    from routers.propositions import PropositionOut, _serialize_proposition
    from routers.roll_call_votes import (
        RollCallVoteOut,
        _list_roll_call_votes_without_vote_date,
        _serialize_roll_call_vote,
        _table_has_column,
    )


router = APIRouter(prefix="/projects", tags=["projects"])

TIER_LIMITS_ENV = "MAMUTE_TIER_LIMITS_JSON"


class ProjectFavoriteOut(BaseModel):
    """Representação serializada do vínculo de favorito entre projeto e parlamentar."""

    id: int
    projeto_id: int
    parliamentarian_id: int
    position: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProjectFavoriteCreate(BaseModel):
    """Dados necessários para criar um novo favorito de projeto."""

    parliamentarian_id: int


class ProjectFavoriteOrderUpdate(BaseModel):
    """Nova ordem pessoal: a lista completa de monitorados, já ordenada."""

    ordered_parliamentarian_ids: List[int]


class CandidacyFavoriteOut(BaseModel):
    """Vínculo de acompanhamento entre o projeto e uma candidatura (2026)."""

    id: int
    projeto_id: int
    candidacy_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CandidacyFavoriteCreate(BaseModel):
    """Dados para registrar o acompanhamento de uma candidatura."""

    candidacy_id: int


class HouseFavoriteQuotaOut(BaseModel):
    """Limite de parlamentares monitorados de uma casa legislativa."""

    limit: int
    used: int
    remaining: int
    limit_reached: bool
    # Admins (MAMUTE_ADMIN_EMAILS) monitoram sem limite. Quando True, os campos
    # numéricos refletem só o uso atual — o front deve ignorar o limite.
    unlimited: bool = False


class ProjectFavoriteQuotaOut(BaseModel):
    """Limite de parlamentares monitorados para o projeto autenticado.

    Os campos de topo (``limit``/``used``/``remaining``/``limit_reached``) são os
    TOTAIS derivados (soma das casas), mantidos por compatibilidade. Os limites
    reais são aplicados por casa em ``camara`` e ``senado``.
    """

    limit: int
    used: int
    remaining: int
    limit_reached: bool
    unlimited: bool = False
    camara: HouseFavoriteQuotaOut
    senado: HouseFavoriteQuotaOut


class ProjectTagOut(BaseModel):
    """Tag livre do assinante, com quantos parlamentares ela marca."""

    id: int
    name: str
    slug: str
    parliamentarian_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class ProjectTagCreate(BaseModel):
    """Nome da tag, como a pessoa digitou."""

    name: str


class ParliamentarianTagsUpdate(BaseModel):
    """Conjunto completo de tags de um parlamentar."""

    tag_ids: List[int]


class ParliamentarianTagsOut(BaseModel):
    """Tags aplicadas a um parlamentar no projeto autenticado."""

    parliamentarian_id: int
    tag_ids: List[int]


class MamutometroOut(BaseModel):
    """Marcação do mamutômetro. `level` e nada mais — o significado é do dono."""

    parliamentarian_id: int
    level: int


class MamutometroUpdate(BaseModel):
    level: int


class ProjectDashboardStatsOut(BaseModel):
    """Estatísticas do dashboard, em duas janelas: 3 meses e legislatura.

    Os 3 meses respondem "o que o parlamentar andou fazendo agora"; a
    legislatura responde "que parlamentar ele é". A janela curta sozinha nao
    diferencia ninguem: com ~17 sessoes deliberativas em 3 meses, dois tercos
    da Camara marca 100% de presenca (CS-76).

    `actions_*` conta proposicao de autoria de QUALQUER tipo — requerimento,
    PEC, projeto de lei. Chamar isso de "projeto" superestimava quem so assina
    requerimento, e era o bug da CS-75.
    """

    actions_last_3_months: int
    actions_legislature: int
    attendance_last_3_months_percent: Optional[int] = None
    attendance_legislature_percent: Optional[int] = None
    recent_votes_count: int
    speeches_count: int
    legislature_start_year: int
    legislature_end_year: int


class ProjectDashboardActivityAuthorOut(BaseModel):
    """Parlamentar monitorado associado a uma atividade do dashboard."""

    id: int
    name: Optional[str] = None
    full_name: Optional[str] = None
    party: Optional[str] = None
    state_elected: Optional[str] = None
    type: Optional[str] = None


class ProjectDashboardActivityPropositionOut(PropositionOut):
    """Proposição com autores monitorados pelo projeto autenticado."""

    monitored_authors: List[ProjectDashboardActivityAuthorOut]


class ProjectDashboardActivityOut(BaseModel):
    """Atividades recentes dos parlamentares monitorados pelo projeto autenticado."""

    propositions: List[ProjectDashboardActivityPropositionOut]
    votes: List[RollCallVoteOut]


def _normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return normalized.lower().strip()


def _is_present_status(value: Optional[str]) -> bool:
    normalized = _normalize_text(value)
    if "presen" not in normalized:
        return False
    if "nao" in normalized or "não" in normalized:
        return False
    return True


def _subtract_months(value: date, months: int) -> date:
    target_year = value.year
    target_month = value.month - months
    while target_month <= 0:
        target_month += 12
        target_year -= 1
    target_day = min(value.day, calendar.monthrange(target_year, target_month)[1])
    return date(target_year, target_month, target_day)


# A 57a legislatura comecou em 01/02/2023. Legislatura dura 4 anos e sempre
# comeca em 1o de fevereiro do ano seguinte a eleicao geral: 2019, 2023, 2027...
_LEGISLATURA_ANO_REFERENCIA = 2023
_LEGISLATURA_DURACAO_ANOS = 4


def _current_legislature_range(today: Optional[date] = None) -> tuple[date, date]:
    """Janela da legislatura vigente, derivada da data — nao hardcoded.

    Quando virar a 58a em fev/2027 o recorte se ajusta sozinho, sem deploy.
    """
    if today is None:
        today = datetime.now(ZoneInfo("America/Sao_Paulo")).date()

    delta = today.year - _LEGISLATURA_ANO_REFERENCIA
    # `//` arredonda para baixo tambem em negativo, entao anos anteriores a 2023
    # caem na legislatura correta sem caso especial.
    ano_inicio = _LEGISLATURA_ANO_REFERENCIA + (delta // _LEGISLATURA_DURACAO_ANOS) * _LEGISLATURA_DURACAO_ANOS
    if today < date(ano_inicio, 2, 1):
        # Janeiro ainda pertence a legislatura anterior.
        ano_inicio -= _LEGISLATURA_DURACAO_ANOS

    inicio = date(ano_inicio, 2, 1)
    fim = date(ano_inicio + _LEGISLATURA_DURACAO_ANOS, 1, 31)
    # Nao adianta consultar futuro: corta em hoje para a janela ser comparavel.
    return inicio, min(fim, today)


def _last_three_months_range_sao_paulo() -> tuple[date, date, datetime, datetime]:
    tz = ZoneInfo("America/Sao_Paulo")
    now_local = datetime.now(tz)
    range_start_date = _subtract_months(now_local.date(), 3)
    range_end_date = now_local.date()
    range_start_dt = datetime.combine(range_start_date, time.min, tzinfo=tz)
    range_end_dt_exclusive = datetime.combine(
        range_end_date + timedelta(days=1),
        time.min,
        tzinfo=tz,
    )
    return range_start_date, range_end_date, range_start_dt, range_end_dt_exclusive


def _colunas_de_favorito(db: Session):
    tem_position = _table_has_column(db, "projetos_parliamentarian", "position")
    return (
        ProjetosParliamentarian.id,
        ProjetosParliamentarian.projeto_id,
        ProjetosParliamentarian.parliamentarian_id,
        (ProjetosParliamentarian.position if tem_position else null()).label("position"),
        ProjetosParliamentarian.created_at,
        ProjetosParliamentarian.updated_at,
    )


def _apply_favorite_ordering(db: Session, stmt, sort_by: str, sort_order: str):
    if sort_by == "position" and not _table_has_column(
        db, "projetos_parliamentarian", "position"
    ):
        sort_by, sort_order = "created_at", "desc"

    if sort_by == "position":
        return stmt.order_by(
            nullslast(asc(ProjetosParliamentarian.position)),
            desc(ProjetosParliamentarian.created_at),
        )
    sortable_columns = {
        "created_at": ProjetosParliamentarian.created_at,
        "updated_at": ProjetosParliamentarian.updated_at,
        "id": ProjetosParliamentarian.id,
        "parliamentarian_id": ProjetosParliamentarian.parliamentarian_id,
    }
    sort_column = sortable_columns[sort_by]
    return stmt.order_by(asc(sort_column) if sort_order == "asc" else desc(sort_column))


def _get_project_favorite_ids(db: Session, project_id: int) -> List[int]:
    stmt = select(ProjetosParliamentarian.parliamentarian_id).where(
        ProjetosParliamentarian.projeto_id == project_id
    )
    return [int(item) for item in db.execute(stmt).scalars().all()]


def _count_propositions_in_range(
    db: Session, parliamentarian_ids: List[int], range_start: date, range_end: date
) -> int:
    stmt = (
        select(func.count(func.distinct(Proposition.id)))
        .select_from(AuthorsProposition)
        .join(Proposition, Proposition.id == AuthorsProposition.proposition_id)
        .where(AuthorsProposition.parliamentarian_id.in_(parliamentarian_ids))
        .where(Proposition.presentation_date.is_not(None))
        .where(Proposition.presentation_date >= range_start)
        .where(Proposition.presentation_date <= range_end)
    )
    return int(db.execute(stmt).scalar_one() or 0)


def _count_recent_votes(
    db: Session,
    parliamentarian_ids: List[int],
    range_start_dt: datetime,
    range_end_dt_exclusive: datetime,
) -> int:
    stmt = select(func.count(RollCallVote.id)).where(
        RollCallVote.parliamentarian_id.in_(parliamentarian_ids),
        RollCallVote.created_at >= range_start_dt,
        RollCallVote.created_at < range_end_dt_exclusive,
    )
    return int(db.execute(stmt).scalar_one() or 0)


def _count_speeches_in_range(
    db: Session, parliamentarian_ids: List[int], range_start: date, range_end: date
) -> int:
    stmt = select(func.count(SpeechesTranscript.id)).where(
        SpeechesTranscript.parliamentarian_id.in_(parliamentarian_ids),
        SpeechesTranscript.date.is_not(None),
        SpeechesTranscript.date >= range_start,
        SpeechesTranscript.date <= range_end,
    )
    return int(db.execute(stmt).scalar_one() or 0)


_SENADO_PRESENTE = frozenset(
    {
        # Votou de fato — a sigla vem ora como voto, ora como comparecimento.
        "sim",
        "nao",
        "abstencao",
        "votou",
        "vo",
        "vs",  # Votacao secreta: votou, o voto e que nao e publicado.
        # Esteve na sessao sem registrar voto.
        "p-nrv",  # Presente - nao registrou voto.
        "p-od",  # Presente - obstrucao declarada.
        "ob",  # Em obstrucao declarada.
        "psf",  # Presente no Senado Federal.
        "sf",
        # Presidiu.
        "pr",
        "ps",
    }
)

_SENADO_AUSENTE = frozenset(
    {
        "ncom",  # Nao compareceu.
        "aus",  # Comunicacao de ausencia.
        "ap",  # Atividade parlamentar.
        "mis",  # Missao da Casa no Pais/exterior.
        "mer",  # No Parlamento do Mercosul — fora do plenario.
        "rep",  # Representacao em solenidade.
        "ep",
        "epr",  # No exercicio da Presidencia da Republica.
        "afo",  # Afastamento do exercicio.
        "dj",  # Afastamento por decisao judicial.
        "ll",  # Privacao de liberdade.
        "gr",  # Grupo de risco.
        "lcs",
        "leg",
        "l1",
        "l2",
        "l3",
        "l4",
        "l5",
        "l6",
        "l7",
        "la",
        "laf",
        "lap",
        "lc",
        "lg",
        "lga",
        "ln",
        "lp",
        "lpa",
        "ls",
        "lsp",
        "na",  # Dispositivo nao citado: justificativa sem base legal citada.
    }
)


def _senado_comparecimento(value: Optional[str]) -> Optional[bool]:
    """True presente, False ausente, None quando o codigo nao fala de presenca.

    O None e deliberado: `FAL` (falecimento) ou `NH` (nao houve votacao) nao
    sao falta de ninguem, e um codigo novo que a API passe a emitir tambem
    nao deve virar falta sem alguem conferir.
    """
    normalized = _normalize_text(value)
    if not normalized:
        return None
    if normalized.startswith("presidente"):
        return True
    if normalized in _SENADO_PRESENTE:
        return True
    if normalized in _SENADO_AUSENTE:
        return False
    return None


def _senate_parliamentarian_ids(db: Session, parliamentarian_ids: List[int]) -> set[int]:
    if not parliamentarian_ids:
        return set()
    stmt = select(Parliamentarian.id).where(
        Parliamentarian.id.in_(parliamentarian_ids),
        Parliamentarian.type.ilike("%Senad%"),
    )
    return {int(row) for row in db.execute(stmt).scalars().all()}


def _senate_attendance_day_scores(
    db: Session, senate_ids: set[int], range_start: date, range_end: date
) -> List[int]:

    if not senate_ids:
        return []
    # A coluna chega depois dos containers no deploy; sem ela nao ha dia de
    # sessao para agregar, e um numero errado e pior que "sem dado".
    if not _table_has_column(db, "roll_call_votes", "vote_date"):
        return []

    stmt = select(
        RollCallVote.parliamentarian_id,
        RollCallVote.vote_date,
        RollCallVote.vote,
    ).where(
        RollCallVote.parliamentarian_id.in_(senate_ids),
        RollCallVote.vote_date.is_not(None),
        RollCallVote.vote_date >= range_start,
        RollCallVote.vote_date <= range_end,
    )

    # (senador, dia) -> esteve presente em alguma votacao daquele dia.
    days: dict[tuple[int, date], bool] = {}
    for parliamentarian_id, vote_date, vote in db.execute(stmt).all():
        status_value = _senado_comparecimento(vote)
        if status_value is None:
            continue
        key = (int(parliamentarian_id), vote_date)
        days[key] = days.get(key, False) or status_value

    return [1 if present else 0 for present in days.values()]


def _calculate_attendance_avg_percent(
    db: Session, parliamentarian_ids: List[int], range_start: date, range_end: date
) -> Optional[int]:

    senate_ids = _senate_parliamentarian_ids(db, parliamentarian_ids)
    other_ids = [
        parliamentarian_id
        for parliamentarian_id in parliamentarian_ids
        if parliamentarian_id not in senate_ids
    ]

    presence_scores: List[int] = []

    if other_ids:
        plenary_stmt = select(
            PlenaryAttendance.session_attendance,
            PlenaryAttendance.daily_attendance_justification,
        ).where(
            PlenaryAttendance.parliamentarian_id.in_(other_ids),
            PlenaryAttendance.date.is_not(None),
            PlenaryAttendance.date >= range_start,
            PlenaryAttendance.date <= range_end,
        )
        committee_stmt = select(CommitteeAttendance.frequency).where(
            CommitteeAttendance.parliamentarian_id.in_(other_ids),
            CommitteeAttendance.date.is_not(None),
            CommitteeAttendance.date >= range_start,
            CommitteeAttendance.date <= range_end,
        )

        for session_attendance, daily_justification in db.execute(plenary_stmt).all():
            status_value = session_attendance or daily_justification
            presence_scores.append(1 if _is_present_status(status_value) else 0)

        for (frequency,) in db.execute(committee_stmt).all():
            presence_scores.append(1 if _is_present_status(frequency) else 0)

    presence_scores.extend(
        _senate_attendance_day_scores(db, senate_ids, range_start, range_end)
    )

    if not presence_scores:
        return None
    avg_ratio = sum(presence_scores) / len(presence_scores)
    return int(round(avg_ratio * 100))


def _list_project_dashboard_propositions(
    db: Session,
    parliamentarian_ids: List[int],
    limit: int,
) -> List[ProjectDashboardActivityPropositionOut]:
    authorship_proposition_ids = select(AuthorsProposition.proposition_id).where(
        AuthorsProposition.parliamentarian_id.in_(parliamentarian_ids)
    )
    stmt = (
        select(Proposition)
        .where(Proposition.id.in_(authorship_proposition_ids))
        .order_by(
            desc(Proposition.presentation_date).nulls_last(),
            desc(Proposition.created_at),
            desc(Proposition.id),
        )
        .limit(limit)
    )
    propositions = db.execute(stmt).scalars().all()
    if not propositions:
        return []

    proposition_ids = [int(proposition.id) for proposition in propositions]
    favorite_author_links = (
        select(
            AuthorsProposition.proposition_id,
            AuthorsProposition.parliamentarian_id,
        )
        .where(AuthorsProposition.proposition_id.in_(proposition_ids))
        .where(AuthorsProposition.parliamentarian_id.in_(parliamentarian_ids))
        .distinct()
        .subquery()
    )
    authors_stmt = (
        select(favorite_author_links.c.proposition_id, Parliamentarian)
        .join(
            Parliamentarian,
            Parliamentarian.id == favorite_author_links.c.parliamentarian_id,
        )
        .order_by(
            favorite_author_links.c.proposition_id,
            asc(Parliamentarian.name),
            asc(Parliamentarian.id),
        )
    )
    monitored_authors_by_proposition: dict[int, List[ProjectDashboardActivityAuthorOut]] = {}
    for proposition_id, parliamentarian in db.execute(authors_stmt).all():
        monitored_authors_by_proposition.setdefault(int(proposition_id), []).append(
            ProjectDashboardActivityAuthorOut(
                id=int(parliamentarian.id),
                name=parliamentarian.name,
                full_name=parliamentarian.full_name,
                party=parliamentarian.party,
                state_elected=parliamentarian.state_elected,
                type=parliamentarian.type,
            )
        )

    return [
        ProjectDashboardActivityPropositionOut(
            **_serialize_proposition(proposition).model_dump(),
            monitored_authors=monitored_authors_by_proposition.get(
                int(proposition.id),
                [],
            ),
        )
        for proposition in propositions
    ]


def _list_project_dashboard_votes(
    db: Session,
    parliamentarian_ids: List[int],
    limit: int,
) -> List[RollCallVoteOut]:
    if not _table_has_column(db, "roll_call_votes", "vote_date"):
        return _list_roll_call_votes_without_vote_date(
            db,
            parliamentarian_ids=parliamentarian_ids,
            limit=limit,
        )

    stmt = (
        select(RollCallVote)
        .options(
            selectinload(RollCallVote.proposition),
            selectinload(RollCallVote.parliamentarian),
        )
        .where(RollCallVote.parliamentarian_id.in_(parliamentarian_ids))
        .order_by(
            desc(RollCallVote.created_at),
            desc(RollCallVote.id),
        )
        .limit(limit)
    )
    votes = db.execute(stmt).scalars().all()
    return [_serialize_roll_call_vote(vote) for vote in votes]


def _ensure_active_project(
    db: Session,
    project_id: int,
    *,
    lock_for_update: bool = False,
) -> Projetos:
    stmt = select(Projetos).where(Projetos.id == project_id)
    if lock_for_update:
        stmt = stmt.with_for_update()
    project = db.execute(stmt).scalar_one_or_none()
    if project is None or getattr(project, "deleted_at", None) is not None:
        raise HTTPException(status_code=404, detail="Projeto não encontrado.")
    return project


def _get_project_from_token_email(request: Request, db: Session) -> Projetos:
    token_email = getattr(request.state, "token_email", None)
    if not token_email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token sem e-mail (sub) para identificar o projeto.",
        )

    stmt = select(Projetos).where(
        Projetos.email == token_email,
        Projetos.deleted_at.is_(None),
    )
    project = db.execute(stmt).scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Projeto não encontrado para o e-mail autenticado.",
        )

    return project


def _get_project_from_token_email_for_path(
    request: Request,
    db: Session,
    project_id: int,
) -> Projetos:
    project = _get_project_from_token_email(request, db)
    if int(project.id) != int(project_id):
        raise HTTPException(status_code=404, detail="Projeto não encontrado.")
    return project


def _ensure_parliamentarian_exists(db: Session, parliamentarian_id: int) -> Parliamentarian:
    parliamentarian = db.get(Parliamentarian, parliamentarian_id)
    if parliamentarian is None:
        raise HTTPException(status_code=404, detail="Parlamentar não encontrado.")
    return parliamentarian


_HOUSES = ("camara", "senado")
_HOUSE_LIMIT_FIELD = {"camara": "qtd_termos_camara", "senado": "qtd_termos_senado"}
_HOUSE_LABEL = {"camara": "deputados", "senado": "senadores"}


def _house_of(ptype: Optional[str]) -> str:
    """Deriva a casa (camara/senado) do campo livre ``Parliamentarian.type``.

    Espelha ``api.services.admin_metrics._house_of``; mantido local para não
    acoplar o router à camada de serviço por um helper de uma linha.
    """
    return "senado" if ptype and "senad" in ptype.lower() else "camara"


def _get_project_favorite_counts(db: Session, project_id: int) -> dict[str, int]:
    stmt = (
        select(Parliamentarian.type)
        .select_from(ProjetosParliamentarian)
        .join(
            Parliamentarian,
            Parliamentarian.id == ProjetosParliamentarian.parliamentarian_id,
        )
        .where(
            ProjetosParliamentarian.projeto_id == project_id,
            ProjetosParliamentarian.deleted_at.is_(None),
        )
    )
    counts = {"camara": 0, "senado": 0}
    for (ptype,) in db.execute(stmt).all():
        counts[_house_of(ptype)] += 1
    return counts


def _coerce_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return parsed
    return {}


def _parse_tier_limits_env() -> Mapping[str, Any]:
    raw = os.getenv(TIER_LIMITS_ENV, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{TIER_LIMITS_ENV} não é um JSON válido.",
        ) from exc
    if not isinstance(payload, Mapping):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{TIER_LIMITS_ENV} deve ser um objeto por slug de tier.",
        )
    return payload


def _project_tier_details(project: Projetos) -> Mapping[str, Any]:
    tier = getattr(project, "tier", None)
    return _coerce_mapping(getattr(tier, "detalhes", None))


def _project_tier_product_id(project: Projetos) -> str | None:
    tier = getattr(project, "tier", None)
    for candidate in (getattr(tier, "product_id", None), getattr(project, "cliente", None)):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _project_tier_slug(project: Projetos) -> str:
    detalhes = _project_tier_details(project)
    ghost = _coerce_mapping(detalhes.get("ghost"))
    slug = ghost.get("slug")
    if isinstance(slug, str) and slug.strip():
        return slug.strip()
    product_id = _project_tier_product_id(project)
    if product_id == "free":
        return "free"
    return product_id or "free"


def _coerce_non_negative_int(value: Any, *, field_name: str, slug: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Limite {field_name!r} inválido para o tier {slug!r}.",
        ) from exc
    if parsed < 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Limite {field_name!r} inválido para o tier {slug!r}.",
        )
    return parsed


def _tier_limit_from_env(project: Projetos, field_name: str) -> int | None:
    limits = _parse_tier_limits_env()
    if not limits:
        return None

    slug = _project_tier_slug(project)
    product_id = _project_tier_product_id(project)
    for lookup_key in (slug, product_id):
        if not lookup_key or lookup_key not in limits:
            continue
        entry = limits[lookup_key]
        if isinstance(entry, Mapping):
            if field_name not in entry:
                continue
            return _coerce_non_negative_int(
                entry[field_name],
                field_name=field_name,
                slug=lookup_key,
            )
        if field_name == "qtd_termos":
            return _coerce_non_negative_int(
                entry,
                field_name=field_name,
                slug=lookup_key,
            )
    return None


def _tier_limit_from_db(project: Projetos, field_name: str) -> int | None:
    detalhes = _project_tier_details(project)
    raw = detalhes.get(field_name)
    if raw is None:
        return None
    return _coerce_non_negative_int(
        raw, field_name=field_name, slug=_project_tier_slug(project)
    )


def _legacy_global_favorite_limit(project: Projetos) -> int:
    env_limit = _tier_limit_from_env(project, "qtd_termos")
    if env_limit is not None:
        return env_limit
    db_limit = _tier_limit_from_db(project, "qtd_termos")
    if db_limit is not None:
        return db_limit
    return max(0, int(project.qtd_termos or 0))


def _project_favorite_limit_for_house(project: Projetos, house: str) -> int:
    field_name = _HOUSE_LIMIT_FIELD[house]
    env_limit = _tier_limit_from_env(project, field_name)
    if env_limit is not None:
        return env_limit
    db_limit = _tier_limit_from_db(project, field_name)
    if db_limit is not None:
        return db_limit
    # Fallback sem regressão: tier sem limite por casa herda o total global
    # como limite de CADA casa (regra de seed da migração).
    return _legacy_global_favorite_limit(project)


def _is_admin_project(project: Projetos) -> bool:
    """Projetos de admins (MAMUTE_ADMIN_EMAILS) monitoram sem limite."""

    email = (project.email or "").strip().lower()
    return bool(email) and email in get_admin_settings()["emails"]


def _build_project_favorite_quota(db: Session, project: Projetos) -> ProjectFavoriteQuotaOut:
    counts = _get_project_favorite_counts(db, int(project.id))
    unlimited = _is_admin_project(project)
    houses: dict[str, HouseFavoriteQuotaOut] = {}
    for house in _HOUSES:
        limit = _project_favorite_limit_for_house(project, house)
        used = counts[house]
        houses[house] = HouseFavoriteQuotaOut(
            limit=limit,
            used=used,
            remaining=max(0, limit - used),
            limit_reached=False if unlimited else used >= limit,
            unlimited=unlimited,
        )
    total_limit = houses["camara"].limit + houses["senado"].limit
    total_used = houses["camara"].used + houses["senado"].used
    return ProjectFavoriteQuotaOut(
        limit=total_limit,
        used=total_used,
        remaining=max(0, total_limit - total_used),
        limit_reached=False if unlimited else total_used >= total_limit,
        unlimited=unlimited,
        camara=houses["camara"],
        senado=houses["senado"],
    )


def _ensure_project_favorite_quota_available(
    db: Session, project: Projetos, house: str
) -> None:
    if _is_admin_project(project):
        return
    limit = _project_favorite_limit_for_house(project, house)
    used = _get_project_favorite_counts(db, int(project.id))[house]
    if used >= limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Limite de {_HOUSE_LABEL[house]} monitorados atingido para seu "
                f"plano ({used}/{limit})."
            ),
        )


CANDIDACY_FAVORITE_DEFAULT_LIMIT = 10  # mesmo default do seed de qtd_candidatos


def _ensure_candidacy_favorite_quota_available(
    db: Session, project: Projetos
) -> None:
    if _is_admin_project(project):
        return
    limit = _tier_limit_from_env(project, "qtd_candidatos")
    if limit is None:
        limit = _tier_limit_from_db(project, "qtd_candidatos")
    if limit is None:
        limit = CANDIDACY_FAVORITE_DEFAULT_LIMIT
    used = db.execute(
        select(func.count())
        .select_from(ProjetosCandidacy)
        .where(ProjetosCandidacy.projeto_id == project.id)
    ).scalar_one()
    if int(used) >= limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Limite de candidaturas acompanhadas atingido para seu "
                f"plano ({used}/{limit})."
            ),
        )


def _log_favorite_event(
    db: Session,
    project: Projetos,
    parliamentarian_id: int,
    event_type: str,
) -> None:
    """Registra add/remove de favorito para métricas. Fail-soft."""
    try:
        db.add(
            UsageEvent(
                projeto_id=int(project.id),
                email=getattr(project, "email", None),
                event_type=event_type,
                parliamentarian_id=parliamentarian_id,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 — métrica nunca pode quebrar a ação
        db.rollback()


def _create_project_favorite(
    db: Session,
    project_id: int,
    parliamentarian_id: int,
) -> ProjetosParliamentarian:
    project = _ensure_active_project(db, project_id, lock_for_update=True)
    parliamentarian = _ensure_parliamentarian_exists(db, parliamentarian_id)

    existing_stmt = select(ProjetosParliamentarian).where(
        ProjetosParliamentarian.projeto_id == project_id,
        ProjetosParliamentarian.parliamentarian_id == parliamentarian_id,
    )
    existing = db.execute(existing_stmt).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Parlamentar já está favoritado neste projeto.",
        )

    _ensure_project_favorite_quota_available(
        db, project, _house_of(parliamentarian.type)
    )

    favorite = ProjetosParliamentarian(
        projeto_id=project_id,
        parliamentarian_id=parliamentarian_id,
    )
    db.add(favorite)
    db.commit()
    db.refresh(favorite)
    _log_favorite_event(db, project, parliamentarian_id, "favorite_added")
    return favorite


def _delete_project_favorite(db: Session, project_id: int, parliamentarian_id: int) -> None:
    project = _ensure_active_project(db, project_id)
    stmt = select(ProjetosParliamentarian).where(
        ProjetosParliamentarian.projeto_id == project_id,
        ProjetosParliamentarian.parliamentarian_id == parliamentarian_id,
    )
    favorite = db.execute(stmt).scalar_one_or_none()

    if favorite is None:
        raise HTTPException(
            status_code=404,
            detail="Favorito não encontrado para o projeto informado.",
        )

    db.delete(favorite)
    db.commit()
    _log_favorite_event(db, project, parliamentarian_id, "favorite_removed")


@router.get(
    "/me/favorites",
    response_model=List[ProjectFavoriteOut],
    summary="Lista favoritos do projeto do usuário autenticado",
)
def list_my_project_favorites(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    created_from: Optional[datetime] = Query(
        None,
        description="Filtra por favoritos criados a partir deste instante (inclusive).",
    ),
    created_to: Optional[datetime] = Query(
        None,
        description="Filtra por favoritos criados até este instante (inclusive).",
    ),
    updated_from: Optional[datetime] = Query(
        None,
        description="Filtra por favoritos atualizados a partir deste instante (inclusive).",
    ),
    updated_to: Optional[datetime] = Query(
        None,
        description="Filtra por favoritos atualizados até este instante (inclusive).",
    ),
    sort_by: Literal[
        "position", "created_at", "updated_at", "id", "parliamentarian_id"
    ] = Query(
        default="position",
        description=(
            "Campo usado para ordenação. 'position' é a ordem pessoal do assinante: "
            "sempre crescente, com quem nunca foi ordenado no fim."
        ),
    ),
    sort_order: Literal["asc", "desc"] = Query(
        default="desc",
        description="Direção da ordenação. Ignorado quando sort_by='position'.",
    ),
) -> List[ProjectFavoriteOut]:
    """Retorna os favoritos do projeto identificado pelo e-mail do token JWT."""
    project = _get_project_from_token_email(request, db)
    stmt = select(*_colunas_de_favorito(db)).where(
        ProjetosParliamentarian.projeto_id == project.id
    )
    if created_from is not None:
        stmt = stmt.where(ProjetosParliamentarian.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(ProjetosParliamentarian.created_at <= created_to)
    if updated_from is not None:
        stmt = stmt.where(ProjetosParliamentarian.updated_at >= updated_from)
    if updated_to is not None:
        stmt = stmt.where(ProjetosParliamentarian.updated_at <= updated_to)
    stmt = _apply_favorite_ordering(db, stmt, sort_by, sort_order)
    stmt = stmt.offset(offset).limit(limit)
    return [ProjectFavoriteOut(**linha) for linha in db.execute(stmt).mappings()]


@router.get(
    "/me/favorites/quota",
    response_model=ProjectFavoriteQuotaOut,
    summary="Retorna limite de favoritos do projeto autenticado",
)
def get_my_project_favorites_quota(
    request: Request,
    db: Session = Depends(get_db),
) -> ProjectFavoriteQuotaOut:
    """Retorna limite, uso e saldo de parlamentares monitorados do projeto."""
    project = _get_project_from_token_email(request, db)
    return _build_project_favorite_quota(db, project)


@router.patch(
    "/me/favorites/order",
    response_model=List[ProjectFavoriteOut],
    summary="Define a ordem pessoal dos parlamentares monitorados",
)
def reorder_my_project_favorites(
    request: Request,
    payload: ProjectFavoriteOrderUpdate,
    db: Session = Depends(get_db),
) -> List[ProjectFavoriteOut]:
    """Reescreve as posições 0..n-1 do projeto do token, numa transação só.

    Exige a lista COMPLETA de monitorados. Se ela não bater exatamente com o que
    está no banco, a ordem do cliente está velha (favorito adicionado ou removido
    em outra aba/dispositivo) e aplicá-la parcialmente deixaria posições órfãs —
    por isso 422, e o cliente recarrega antes de tentar de novo.
    """
    project = _get_project_from_token_email(request, db)

    favorites = (
        db.execute(
            select(ProjetosParliamentarian).where(
                ProjetosParliamentarian.projeto_id == project.id
            )
        )
        .scalars()
        .all()
    )

    requested = payload.ordered_parliamentarian_ids
    current_ids = {int(item.parliamentarian_id) for item in favorites}
    if len(requested) != len(set(requested)) or set(requested) != current_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Sua lista de parlamentares monitorados mudou. "
                "Atualize a página e ordene novamente."
            ),
        )

    by_parliamentarian = {int(item.parliamentarian_id): item for item in favorites}
    for index, parliamentarian_id in enumerate(requested):
        by_parliamentarian[parliamentarian_id].position = index

    db.commit()

    stmt = _apply_favorite_ordering(
        db,
        select(*_colunas_de_favorito(db)).where(
            ProjetosParliamentarian.projeto_id == project.id
        ),
        "position",
        "asc",
    )
    return [ProjectFavoriteOut(**linha) for linha in db.execute(stmt).mappings()]


@router.post(
    "/me/favorites",
    response_model=ProjectFavoriteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Adiciona favorito ao projeto do usuário autenticado",
)
def add_my_project_favorite(
    request: Request,
    payload: ProjectFavoriteCreate,
    db: Session = Depends(get_db),
) -> ProjetosParliamentarian:
    """Cria favorito usando o projeto identificado pelo e-mail do token JWT."""
    project = _get_project_from_token_email(request, db)
    return _create_project_favorite(db, project.id, payload.parliamentarian_id)


@router.delete(
    "/me/favorites/{parliamentarian_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove favorito do projeto do usuário autenticado",
)
def remove_my_project_favorite(
    request: Request,
    parliamentarian_id: int,
    db: Session = Depends(get_db),
) -> Response:
    """Remove favorito usando o projeto identificado pelo e-mail do token JWT."""
    project = _get_project_from_token_email(request, db)
    _delete_project_favorite(db, project.id, parliamentarian_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me/candidacy-favorites",
    response_model=List[CandidacyFavoriteOut],
    summary="Lista as candidaturas acompanhadas pelo usuário autenticado",
)
def list_my_candidacy_favorites(
    request: Request,
    db: Session = Depends(get_db),
) -> List[ProjetosCandidacy]:
    """Só o registro da escolha — nenhuma feature consome o vínculo ainda."""
    project = _get_project_from_token_email(request, db)
    return (
        db.execute(
            select(ProjetosCandidacy)
            .where(ProjetosCandidacy.projeto_id == project.id)
            .order_by(asc(ProjetosCandidacy.id))
        )
        .scalars()
        .all()
    )


@router.post(
    "/me/candidacy-favorites",
    response_model=CandidacyFavoriteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Registra o acompanhamento de uma candidatura",
)
def add_my_candidacy_favorite(
    request: Request,
    payload: CandidacyFavoriteCreate,
    db: Session = Depends(get_db),
) -> ProjetosCandidacy:
    """Registra a escolha. A única regra é a cota `qtd_candidatos` do plano —
    semeada pela migration e1f2a3b4c5d6 justamente esperando este endpoint."""
    project = _get_project_from_token_email(request, db)
    _ensure_candidacy_favorite_quota_available(db, project)

    candidacy = db.get(Candidacy, payload.candidacy_id)
    if candidacy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidatura não encontrada.",
        )

    existing = db.execute(
        select(ProjetosCandidacy).where(
            ProjetosCandidacy.projeto_id == project.id,
            ProjetosCandidacy.candidacy_id == payload.candidacy_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Você já acompanha esta candidatura.",
        )

    favorite = ProjetosCandidacy(
        projeto_id=project.id, candidacy_id=payload.candidacy_id
    )
    db.add(favorite)
    db.commit()
    db.refresh(favorite)
    return favorite


@router.delete(
    "/me/candidacy-favorites/{candidacy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deixa de acompanhar uma candidatura",
)
def remove_my_candidacy_favorite(
    request: Request,
    candidacy_id: int,
    db: Session = Depends(get_db),
) -> Response:
    project = _get_project_from_token_email(request, db)
    favorite = db.execute(
        select(ProjetosCandidacy).where(
            ProjetosCandidacy.projeto_id == project.id,
            ProjetosCandidacy.candidacy_id == candidacy_id,
        )
    ).scalar_one_or_none()
    if favorite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Você não acompanha esta candidatura.",
        )
    db.delete(favorite)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me/dashboard-activity",
    response_model=ProjectDashboardActivityOut,
    summary="Atividades recentes dos parlamentares favoritados no projeto autenticado",
)
def get_my_dashboard_activity(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
) -> ProjectDashboardActivityOut:
    """Retorna atividades recentes escopadas aos parlamentares favoritados."""
    project = _get_project_from_token_email(request, db)
    parliamentarian_ids = _get_project_favorite_ids(db, project.id)
    if not parliamentarian_ids:
        return ProjectDashboardActivityOut(propositions=[], votes=[])

    return ProjectDashboardActivityOut(
        propositions=_list_project_dashboard_propositions(
            db,
            parliamentarian_ids,
            limit,
        ),
        votes=_list_project_dashboard_votes(
            db,
            parliamentarian_ids,
            limit,
        ),
    )


# Tags NAO consomem cota de plano: o que o plano vende e monitoramento —
# coleta, dashboard, e-mail e IA —, nao organizacao pessoal. Os tetos abaixo
# sao higiene (evitar lista impraticavel e texto abusivo), nao regra comercial.
MAX_TAGS_POR_PROJETO = 50
MAX_TAGS_POR_PARLAMENTAR = 10
MAX_CARACTERES_TAG = 30


def _tag_slug(nome: str) -> str:
    """Slug de comparacao: sem acento, minusculo, espacos colapsados.

    "Meio Ambiente", "meio  ambiente" e "MEIO AMBIENTE" viram a mesma tag —
    o que a pessoa digitou fica em `name`, para exibir de volta como ela quis.
    """
    return " ".join(_normalize_text(nome).split())


def _validar_nome_de_tag(nome: str) -> str:
    limpo = (nome or "").strip()
    if not limpo:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Dê um nome para a tag.",
        )
    if len(limpo) > MAX_CARACTERES_TAG:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"A tag pode ter no máximo {MAX_CARACTERES_TAG} caracteres.",
        )
    return limpo


def _contagem_por_tag(db: Session, project_id: int) -> dict[int, int]:
    linhas = db.execute(
        select(ParliamentarianTag.tag_id, func.count())
        .where(ParliamentarianTag.projeto_id == project_id)
        .group_by(ParliamentarianTag.tag_id)
    ).all()
    return {int(tag_id): int(total) for tag_id, total in linhas}


def _serializar_tag(tag: ProjectTag, contagem: dict[int, int]) -> ProjectTagOut:
    return ProjectTagOut(
        id=int(tag.id),
        name=tag.name,
        slug=tag.slug,
        parliamentarian_count=contagem.get(int(tag.id), 0),
    )


def _tag_do_projeto(db: Session, project_id: int, tag_id: int) -> ProjectTag:
    """Busca a tag JA escopada pelo projeto do token (cláusula 0e).

    404 e nao 403: dizer "existe, mas nao e sua" ja entrega a existencia da tag
    de outra conta.
    """
    tag = db.execute(
        select(ProjectTag).where(
            ProjectTag.id == tag_id,
            ProjectTag.projeto_id == project_id,
        )
    ).scalar_one_or_none()
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag não encontrada.")
    return tag


@router.get(
    "/me/tags",
    response_model=List[ProjectTagOut],
    summary="Lista as tags do projeto autenticado",
)
def list_my_project_tags(
    request: Request,
    db: Session = Depends(get_db),
) -> List[ProjectTagOut]:
    """Tags do projeto do token, com quantos parlamentares cada uma marca."""
    project = _get_project_from_token_email(request, db)
    tags = (
        db.execute(
            select(ProjectTag)
            .where(ProjectTag.projeto_id == project.id)
            .order_by(asc(ProjectTag.slug))
        )
        .scalars()
        .all()
    )
    contagem = _contagem_por_tag(db, int(project.id))
    return [_serializar_tag(tag, contagem) for tag in tags]


@router.post(
    "/me/tags",
    response_model=ProjectTagOut,
    status_code=status.HTTP_201_CREATED,
    summary="Cria uma tag no projeto autenticado",
)
def create_my_project_tag(
    request: Request,
    payload: ProjectTagCreate,
    db: Session = Depends(get_db),
) -> ProjectTagOut:
    project = _get_project_from_token_email(request, db)
    nome = _validar_nome_de_tag(payload.name)
    slug = _tag_slug(nome)

    existente = db.execute(
        select(ProjectTag).where(
            ProjectTag.projeto_id == project.id,
            ProjectTag.slug == slug,
        )
    ).scalar_one_or_none()
    if existente is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Você já tem a tag "{existente.name}".',
        )

    total = db.execute(
        select(func.count()).select_from(ProjectTag).where(
            ProjectTag.projeto_id == project.id
        )
    ).scalar_one()
    if int(total) >= MAX_TAGS_POR_PROJETO:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Você já tem {MAX_TAGS_POR_PROJETO} tags. "
                "Renomeie ou apague uma para criar outra."
            ),
        )

    tag = ProjectTag(projeto_id=project.id, name=nome, slug=slug)
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return _serializar_tag(tag, {})


@router.patch(
    "/me/tags/{tag_id}",
    response_model=ProjectTagOut,
    summary="Renomeia uma tag do projeto autenticado",
)
def rename_my_project_tag(
    request: Request,
    tag_id: int,
    payload: ProjectTagCreate,
    db: Session = Depends(get_db),
) -> ProjectTagOut:
    project = _get_project_from_token_email(request, db)
    tag = _tag_do_projeto(db, int(project.id), tag_id)

    nome = _validar_nome_de_tag(payload.name)
    slug = _tag_slug(nome)

    colisao = db.execute(
        select(ProjectTag).where(
            ProjectTag.projeto_id == project.id,
            ProjectTag.slug == slug,
            ProjectTag.id != tag.id,
        )
    ).scalar_one_or_none()
    if colisao is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Você já tem a tag "{colisao.name}".',
        )

    tag.name = nome
    tag.slug = slug
    db.commit()
    db.refresh(tag)
    return _serializar_tag(tag, _contagem_por_tag(db, int(project.id)))


@router.delete(
    "/me/tags/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Apaga uma tag do projeto autenticado",
)
def delete_my_project_tag(
    request: Request,
    tag_id: int,
    db: Session = Depends(get_db),
) -> Response:
    """Apagar a tag tira a etiqueta de todos os parlamentares (cascade), e
    nunca mexe no monitoramento."""
    project = _get_project_from_token_email(request, db)
    tag = _tag_do_projeto(db, int(project.id), tag_id)
    db.delete(tag)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me/parliamentarian-tags",
    response_model=List[ParliamentarianTagsOut],
    summary="Tags aplicadas a cada parlamentar, no projeto autenticado",
)
def list_my_parliamentarian_tags(
    request: Request,
    db: Session = Depends(get_db),
) -> List[ParliamentarianTagsOut]:
    """Todas as aplicacoes do projeto de uma vez.

    Uma chamada so em vez de uma por parlamentar: a tela lista dezenas de
    cards, e N requests para montar chips seria desperdicio obvio. O volume e
    pequeno por construcao (teto de 50 tags x 10 por parlamentar).
    """
    project = _get_project_from_token_email(request, db)
    linhas = db.execute(
        select(ParliamentarianTag.parliamentarian_id, ParliamentarianTag.tag_id)
        .where(ParliamentarianTag.projeto_id == project.id)
        .order_by(asc(ParliamentarianTag.parliamentarian_id))
    ).all()

    por_parlamentar: dict[int, List[int]] = {}
    for parliamentarian_id, tag_id in linhas:
        por_parlamentar.setdefault(int(parliamentarian_id), []).append(int(tag_id))
    return [
        ParliamentarianTagsOut(parliamentarian_id=pid, tag_ids=tag_ids)
        for pid, tag_ids in por_parlamentar.items()
    ]


@router.put(
    "/me/parliamentarians/{parliamentarian_id}/tags",
    response_model=ParliamentarianTagsOut,
    summary="Define as tags de um parlamentar, no projeto autenticado",
)
def set_my_parliamentarian_tags(
    request: Request,
    parliamentarian_id: int,
    payload: ParliamentarianTagsUpdate,
    db: Session = Depends(get_db),
) -> ParliamentarianTagsOut:
    """Substitui o conjunto inteiro de tags do parlamentar. Idempotente.

    Substituir (e nao adicionar/remover uma a uma) espelha a tela, que edita a
    lista toda e salva de uma vez — mesma politica de `word_cloud_terms`.
    """
    project = _get_project_from_token_email(request, db)

    _exigir_politico_marcavel(
        db, project, parliamentarian_id, get_marcacoes_config(db).tags_escopo
    )

    desejadas = list(dict.fromkeys(payload.tag_ids))
    if len(desejadas) > MAX_TAGS_POR_PARLAMENTAR:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Um parlamentar pode ter no máximo {MAX_TAGS_POR_PARLAMENTAR} tags."
            ),
        )

    if desejadas:
        validas = set(
            db.execute(
                select(ProjectTag.id).where(
                    ProjectTag.projeto_id == project.id,
                    ProjectTag.id.in_(desejadas),
                )
            ).scalars()
        )
        if len(validas) != len(desejadas):
            raise HTTPException(status_code=404, detail="Tag não encontrada.")

    atuais = {
        int(linha.tag_id): linha
        for linha in db.execute(
            select(ParliamentarianTag).where(
                ParliamentarianTag.projeto_id == project.id,
                ParliamentarianTag.parliamentarian_id == parliamentarian_id,
            )
        ).scalars()
    }

    for tag_id, linha in atuais.items():
        if tag_id not in desejadas:
            db.delete(linha)
    for tag_id in desejadas:
        if tag_id not in atuais:
            db.add(
                ParliamentarianTag(
                    projeto_id=project.id,
                    tag_id=tag_id,
                    parliamentarian_id=parliamentarian_id,
                )
            )
    db.commit()

    return ParliamentarianTagsOut(
        parliamentarian_id=parliamentarian_id,
        tag_ids=desejadas,
    )


# O SIGNIFICADO de cada nível não existe aqui, e é assim de propósito: cada
# assinante escolhe a própria regra e nunca a informa. Por isso não há nome de
# campo, mensagem ou log que sugira o que 1, 2 ou 3 querem dizer.
MENSAGEM_TETO_MAMUTOMETRO = "Limite atingido. Faça um upgrade do plano em 'Conta'."


def _exigir_mamutometro_no_plano(
    db: Session, project: Projetos, *, is_admin: bool = False
) -> None:
    """404 (não 403) quando o plano não tem a feature.

    403 confirmaria que o recurso existe para quem não o tem — e a resposta
    ficaria diferente da de um político fora do escopo, dando ao cliente um
    jeito de distinguir os dois casos.

    `is_admin` precisa chegar aqui resolvido: `services/feature_flags` define que
    admin vê tudo que não está `off`, porque o papel é prévia e conferência, não
    assinatura. Sem isso, a conta que confere a feature em produção — onde não há
    staging — veria a escala na tela e tomaria 404 ao marcar.
    """
    if not mamutometro_habilitado(db, project, is_admin=is_admin):
        raise HTTPException(status_code=404, detail="Recurso não encontrado.")


def _exigir_politico_marcavel(
    db: Session, project: Projetos, parliamentarian_id: int, escopo: str
) -> None:
    if not is_parliamentarian_visible(db, parliamentarian_id):
        raise HTTPException(status_code=404, detail="Parlamentar não encontrado.")
    if not esta_no_escopo(db, int(project.id), parliamentarian_id, escopo):
        raise HTTPException(status_code=404, detail="Parlamentar não encontrado.")


@router.get(
    "/me/mamutometro",
    response_model=List[MamutometroOut],
    summary="Marcações do mamutômetro do projeto autenticado",
)
def list_my_mamutometro(
    request: Request,
    db: Session = Depends(get_db),
) -> List[MamutometroOut]:
    """Devolve o nível gravado, sem recorte pela régua vigente.

    Se o admin reduzir a régua, a marcação continua aqui como está — quem
    apara para exibição é a tela (`min(level, max_level)`). Configuração nunca
    destrói dado do assinante.
    """
    project = _get_project_from_token_email(request, db)
    linhas = db.execute(
        select(ProjectMamutometro)
        .where(ProjectMamutometro.projeto_id == project.id)
        .order_by(asc(ProjectMamutometro.parliamentarian_id))
    ).scalars()
    return [
        MamutometroOut(
            parliamentarian_id=int(linha.parliamentarian_id), level=int(linha.level)
        )
        for linha in linhas
    ]


@router.put(
    "/me/parliamentarians/{parliamentarian_id}/mamutometro",
    response_model=MamutometroOut,
    summary="Marca o mamutômetro de um parlamentar",
)
def set_my_mamutometro(
    request: Request,
    parliamentarian_id: int,
    payload: MamutometroUpdate,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> MamutometroOut:
    project = _get_project_from_token_email(request, db)
    is_admin = resolve_ghost_admin(request, authorization) is not None
    _exigir_mamutometro_no_plano(db, project, is_admin=is_admin)

    config = get_marcacoes_config(db)
    _exigir_politico_marcavel(
        db, project, parliamentarian_id, config.mamutometro_escopo
    )

    max_level = int(config.mamutometro_max_level)
    if not 1 <= int(payload.level) <= max_level:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Escolha um valor entre 1 e {max_level}.",
        )

    existente = db.execute(
        select(ProjectMamutometro).where(
            ProjectMamutometro.projeto_id == project.id,
            ProjectMamutometro.parliamentarian_id == parliamentarian_id,
        )
    ).scalar_one_or_none()

    if existente is None:
        # O teto do plano trava CRIAR, nunca ALTERAR: quem já marcou não pode
        # ficar preso a um nível porque o admin reduziu o limite depois.
        limite = mamutometro_limite(project)
        if limite is not None:
            usados = db.execute(
                select(func.count())
                .select_from(ProjectMamutometro)
                .where(ProjectMamutometro.projeto_id == project.id)
            ).scalar_one()
            if int(usados) >= int(limite):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=MENSAGEM_TETO_MAMUTOMETRO,
                )
        existente = ProjectMamutometro(
            projeto_id=project.id,
            parliamentarian_id=parliamentarian_id,
            level=int(payload.level),
        )
        db.add(existente)
    else:
        existente.level = int(payload.level)

    db.commit()
    return MamutometroOut(parliamentarian_id=parliamentarian_id, level=int(payload.level))


@router.delete(
    "/me/parliamentarians/{parliamentarian_id}/mamutometro",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a marcação do mamutômetro de um parlamentar",
)
def delete_my_mamutometro(
    request: Request,
    parliamentarian_id: int,
    db: Session = Depends(get_db),
) -> Response:
    """Remover não depende de escopo nem de plano: quem marcou pode desmarcar,
    mesmo que a configuração tenha mudado desde então."""
    project = _get_project_from_token_email(request, db)
    linha = db.execute(
        select(ProjectMamutometro).where(
            ProjectMamutometro.projeto_id == project.id,
            ProjectMamutometro.parliamentarian_id == parliamentarian_id,
        )
    ).scalar_one_or_none()
    if linha is not None:
        db.delete(linha)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/me/mamutometro",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Apaga todas as marcações do mamutômetro",
)
def delete_all_my_mamutometro(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Apagar é apagar: `DELETE`, não `deleted_at`."""
    project = _get_project_from_token_email(request, db)
    for linha in db.execute(
        select(ProjectMamutometro).where(ProjectMamutometro.projeto_id == project.id)
    ).scalars():
        db.delete(linha)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{project_id}/favorites",
    response_model=List[ProjectFavoriteOut],
    summary="Lista favoritos de um projeto",
)
def list_project_favorites(
    request: Request,
    project_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    created_from: Optional[datetime] = Query(
        None,
        description="Filtra por favoritos criados a partir deste instante (inclusive).",
    ),
    created_to: Optional[datetime] = Query(
        None,
        description="Filtra por favoritos criados até este instante (inclusive).",
    ),
    updated_from: Optional[datetime] = Query(
        None,
        description="Filtra por favoritos atualizados a partir deste instante (inclusive).",
    ),
    updated_to: Optional[datetime] = Query(
        None,
        description="Filtra por favoritos atualizados até este instante (inclusive).",
    ),
    sort_by: Literal[
        "position", "created_at", "updated_at", "id", "parliamentarian_id"
    ] = Query(
        default="position",
        description=(
            "Campo usado para ordenação. 'position' é a ordem pessoal do assinante: "
            "sempre crescente, com quem nunca foi ordenado no fim."
        ),
    ),
    sort_order: Literal["asc", "desc"] = Query(
        default="desc",
        description="Direção da ordenação. Ignorado quando sort_by='position'.",
    ),
) -> List[ProjectFavoriteOut]:
    """Retorna os parlamentares marcados como favoritos por um projeto específico."""
    project = _get_project_from_token_email_for_path(request, db, project_id)

    stmt = select(*_colunas_de_favorito(db)).where(
        ProjetosParliamentarian.projeto_id == project.id
    )
    if created_from is not None:
        stmt = stmt.where(ProjetosParliamentarian.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(ProjetosParliamentarian.created_at <= created_to)
    if updated_from is not None:
        stmt = stmt.where(ProjetosParliamentarian.updated_at >= updated_from)
    if updated_to is not None:
        stmt = stmt.where(ProjetosParliamentarian.updated_at <= updated_to)
    stmt = _apply_favorite_ordering(db, stmt, sort_by, sort_order)
    stmt = stmt.offset(offset).limit(limit)
    return [ProjectFavoriteOut(**linha) for linha in db.execute(stmt).mappings()]


@router.post(
    "/{project_id}/favorites",
    response_model=ProjectFavoriteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Adiciona um parlamentar aos favoritos do projeto",
)
def add_project_favorite(
    request: Request,
    project_id: int,
    payload: ProjectFavoriteCreate,
    db: Session = Depends(get_db),
) -> ProjetosParliamentarian:
    """Cria o vínculo de favorito entre um projeto e um parlamentar."""
    project = _get_project_from_token_email_for_path(request, db, project_id)
    return _create_project_favorite(db, project.id, payload.parliamentarian_id)


@router.delete(
    "/{project_id}/favorites/{parliamentarian_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove um parlamentar dos favoritos do projeto",
)
def remove_project_favorite(
    request: Request,
    project_id: int,
    parliamentarian_id: int,
    db: Session = Depends(get_db),
) -> Response:
    """Remove o vínculo de favorito entre um projeto e um parlamentar."""
    project = _get_project_from_token_email_for_path(request, db, project_id)
    _delete_project_favorite(db, project.id, parliamentarian_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _build_dashboard_stats(
    db: Session, parliamentarian_ids: List[int]
) -> ProjectDashboardStatsOut:
    """Monta o card em duas janelas: ultimos 3 meses e legislatura vigente.

    Votacoes e discursos seguem so na janela curta — sao "o que aconteceu
    recentemente", e o card ja fica cheio demais com seis numeros.
    """
    leg_inicio, leg_fim = _current_legislature_range()

    if not parliamentarian_ids:
        return ProjectDashboardStatsOut(
            actions_last_3_months=0,
            actions_legislature=0,
            attendance_last_3_months_percent=None,
            attendance_legislature_percent=None,
            recent_votes_count=0,
            speeches_count=0,
            legislature_start_year=leg_inicio.year,
            legislature_end_year=leg_inicio.year + _LEGISLATURA_DURACAO_ANOS,
        )

    range_start, range_end, range_start_dt, range_end_dt_exclusive = (
        _last_three_months_range_sao_paulo()
    )

    return ProjectDashboardStatsOut(
        actions_last_3_months=_count_propositions_in_range(
            db, parliamentarian_ids, range_start, range_end
        ),
        actions_legislature=_count_propositions_in_range(
            db, parliamentarian_ids, leg_inicio, leg_fim
        ),
        attendance_last_3_months_percent=_calculate_attendance_avg_percent(
            db, parliamentarian_ids, range_start, range_end
        ),
        attendance_legislature_percent=_calculate_attendance_avg_percent(
            db, parliamentarian_ids, leg_inicio, leg_fim
        ),
        recent_votes_count=_count_recent_votes(
            db, parliamentarian_ids, range_start_dt, range_end_dt_exclusive
        ),
        speeches_count=_count_speeches_in_range(
            db, parliamentarian_ids, range_start, range_end
        ),
        legislature_start_year=leg_inicio.year,
        legislature_end_year=leg_inicio.year + _LEGISLATURA_DURACAO_ANOS,
    )


@router.get(
    "/me/dashboard-stats",
    response_model=ProjectDashboardStatsOut,
    summary="Estatísticas do dashboard em duas janelas (3 meses e legislatura)",
)
def get_my_dashboard_stats(
    request: Request,
    db: Session = Depends(get_db),
) -> ProjectDashboardStatsOut:
    """Retorna estatísticas dos últimos 3 meses para parlamentares favoritados no projeto."""
    project = _get_project_from_token_email(request, db)
    parliamentarian_ids = _get_project_favorite_ids(db, project.id)
    return _build_dashboard_stats(db, parliamentarian_ids)


@router.get(
    "/me/parliamentarians/{parliamentarian_id}/dashboard-stats",
    response_model=ProjectDashboardStatsOut,
    summary="Estatísticas dos últimos 3 meses para um parlamentar específico",
)
def get_my_parliamentarian_dashboard_stats(
    parliamentarian_id: int,
    db: Session = Depends(get_db),
) -> ProjectDashboardStatsOut:
    """Retorna estatísticas do parlamentar em duas janelas: 3 meses e legislatura."""
    _ensure_parliamentarian_exists(db, parliamentarian_id)
    return _build_dashboard_stats(db, [parliamentarian_id])


__all__ = ["router"]
