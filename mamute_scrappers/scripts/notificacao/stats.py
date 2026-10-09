"""Métricas do relatório alinhadas ao dashboard da API."""

from __future__ import annotations

import unicodedata
from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mamute_scrappers.db.models import (
    AuthorsProposition,
    CommitteeAttendance,
    ParliamentaryAmendment,
    PlenaryAttendance,
    Proposition,
    RollCallVote,
    SpeechesTranscript,
)

from .activity_filters import proposition_in_period, vote_in_period
from .models import DashboardStats


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


def compute_dashboard_stats(
    session: Session,
    parliamentarian_ids: List[int],
    range_start: date,
    range_end: date,
    *,
    range_start_dt: datetime,
    range_end_dt_exclusive: datetime,
    include_ingested_propositions: bool = False,
) -> DashboardStats:
    if not parliamentarian_ids:
        return DashboardStats()

    propositions_stmt = (
        select(func.count(func.distinct(Proposition.id)))
        .select_from(AuthorsProposition)
        .join(Proposition, Proposition.id == AuthorsProposition.proposition_id)
        .where(AuthorsProposition.parliamentarian_id.in_(parliamentarian_ids))
        .where(
            proposition_in_period(
                range_start,
                range_end,
                range_start_dt,
                range_end_dt_exclusive,
                include_ingested=include_ingested_propositions,
            )
        )
    )
    votes_stmt = (
        select(func.count(RollCallVote.id))
        .select_from(RollCallVote)
        .join(Proposition, Proposition.id == RollCallVote.proposition_id)
        .where(RollCallVote.parliamentarian_id.in_(parliamentarian_ids))
        .where(vote_in_period(range_start, range_end))
    )
    speeches_stmt = select(func.count(SpeechesTranscript.id)).where(
        SpeechesTranscript.parliamentarian_id.in_(parliamentarian_ids),
        SpeechesTranscript.date.is_not(None),
        SpeechesTranscript.date >= range_start,
        SpeechesTranscript.date <= range_end,
    )

    return DashboardStats(
        propositions_count=int(session.execute(propositions_stmt).scalar_one() or 0),
        votes_count=int(session.execute(votes_stmt).scalar_one() or 0),
        speeches_count=int(session.execute(speeches_stmt).scalar_one() or 0),
        amendments_count=count_new_amendments(
            session, parliamentarian_ids, range_start_dt, range_end_dt_exclusive
        ),
        attendance_avg_percent=_attendance_avg_percent(
            session, parliamentarian_ids, range_start, range_end
        ),
    )


def count_new_amendments(
    session: Session,
    parliamentarian_ids: List[int],
    range_start_dt: datetime,
    range_end_dt_exclusive: datetime,
) -> int:
    """Emendas que apareceram na base no período (sem data de indicação na fonte)."""
    if not parliamentarian_ids:
        return 0
    stmt = select(func.count(ParliamentaryAmendment.id)).where(
        ParliamentaryAmendment.parliamentarian_id.in_(parliamentarian_ids),
        ParliamentaryAmendment.created_at >= range_start_dt,
        ParliamentaryAmendment.created_at < range_end_dt_exclusive,
    )
    return int(session.execute(stmt).scalar_one() or 0)


def compute_dashboard_stats_all_time(
    session: Session,
    parliamentarian_ids: List[int],
) -> DashboardStats:
    """Totais no banco para os favoritos (modo teste / total)."""
    if not parliamentarian_ids:
        return DashboardStats()

    propositions_stmt = (
        select(func.count(func.distinct(Proposition.id)))
        .select_from(AuthorsProposition)
        .join(Proposition, Proposition.id == AuthorsProposition.proposition_id)
        .where(AuthorsProposition.parliamentarian_id.in_(parliamentarian_ids))
    )
    votes_stmt = select(func.count(RollCallVote.id)).where(
        RollCallVote.parliamentarian_id.in_(parliamentarian_ids)
    )
    speeches_stmt = select(func.count(SpeechesTranscript.id)).where(
        SpeechesTranscript.parliamentarian_id.in_(parliamentarian_ids),
        SpeechesTranscript.date.is_not(None),
    )

    return DashboardStats(
        propositions_count=int(session.execute(propositions_stmt).scalar_one() or 0),
        votes_count=int(session.execute(votes_stmt).scalar_one() or 0),
        speeches_count=int(session.execute(speeches_stmt).scalar_one() or 0),
        attendance_avg_percent=None,
    )


def _attendance_avg_percent(
    session: Session,
    parliamentarian_ids: List[int],
    range_start: date,
    range_end: date,
) -> Optional[int]:
    plenary_stmt = select(
        PlenaryAttendance.session_attendance,
        PlenaryAttendance.daily_attendance_justification,
    ).where(
        PlenaryAttendance.parliamentarian_id.in_(parliamentarian_ids),
        PlenaryAttendance.date.is_not(None),
        PlenaryAttendance.date >= range_start,
        PlenaryAttendance.date <= range_end,
    )
    committee_stmt = select(CommitteeAttendance.frequency).where(
        CommitteeAttendance.parliamentarian_id.in_(parliamentarian_ids),
        CommitteeAttendance.date.is_not(None),
        CommitteeAttendance.date >= range_start,
        CommitteeAttendance.date <= range_end,
    )

    scores: List[int] = []
    for session_attendance, daily_justification in session.execute(plenary_stmt).all():
        status_value = session_attendance or daily_justification
        scores.append(1 if _is_present_status(status_value) else 0)

    for (frequency,) in session.execute(committee_stmt).all():
        scores.append(1 if _is_present_status(frequency) else 0)

    if not scores:
        return None
    return int(round((sum(scores) / len(scores)) * 100))
