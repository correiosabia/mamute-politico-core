"""Consultas ao banco para destinatários e atividade legislativa."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, List, Optional

import weakref

from sqlalchemy import Date, cast, column, func, inspect, select, table
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from mamute_scrappers.db.models import (
    AuthorsProposition,
    Parliamentarian,
    ParliamentaryAmendment,
    Projetos,
    ProjetosParliamentarian,
    Proposition,
    RollCallVote,
    SpeechesTranscript,
    SpeechesTranscriptsProposition,
    Tiers,
)

from .config import PERIODICIDADE_TESTE, PERIODICIDADES_VALIDAS
from .activity_filters import (
    proposition_in_period,
    vote_in_period,
    vote_occurred_at,
    vote_on_sql,
)
from .dates import format_activity_date
from .labels import (
    chamber_label_from_parliamentarian_type,
    extract_ementa,
    cortar_na_frase,
    format_brl,
    format_localidade,
    format_parliamentarian_display_name,
    format_proposition_display_title,
    is_camara_proposition,
)
from .models import FavoriteParliamentarian
from .links import (
    resolve_proposition_link,
    resolve_speech_activity_link,
    resolve_vote_display_link,
)
from .models import ActivityItem, ProjectRecipient
from .tzcompat import SAO_PAULO, combine_local, min_local, now_local

_VOTE_CANDIDATE_MULTIPLIER = 20

# Tabela da migration cs116. O deploy sobe o código antes do alembic: sem a
# tabela, o discurso sai com o sumário cortado, como antes.
_SPEECH_SHORT_SUMMARY = table("speech_short_summary", column("speech_id"), column("text"))
_TABELAS_POR_BANCO: "weakref.WeakKeyDictionary[Any, dict[str, bool]]" = weakref.WeakKeyDictionary()


def tem_tabela(session: Session, nome: str) -> bool:
    """Existência da tabela, consultada uma vez por engine."""
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    cache = _TABELAS_POR_BANCO.setdefault(engine, {})
    if nome not in cache:
        try:
            cache[nome] = inspect(engine).has_table(nome)
        except SQLAlchemyError:
            return False
    return cache[nome]

logger = logging.getLogger(__name__)


def list_recipients_for_periodicity(
    session: Session,
    periodicidade: str,
    *,
    include_without_tier: bool = False,
) -> List[ProjectRecipient]:
    """Retorna projetos ativos elegíveis para a periodicidade solicitada."""
    if periodicidade not in PERIODICIDADES_VALIDAS:
        raise ValueError(
            f"Periodicidade inválida: {periodicidade!r}. "
            f"Use: {', '.join(sorted(PERIODICIDADES_VALIDAS))}"
        )

    stmt = (
        select(Projetos)
        .options(joinedload(Projetos.tier))
        .where(Projetos.deleted_at.is_(None))
        .order_by(Projetos.id)
    )
    projects = list(session.execute(stmt).scalars().unique().all())

    if periodicidade == PERIODICIDADE_TESTE:
        recipients = [_to_recipient(project) for project in projects]
        logger.info(
            "%s destinatário(s) no modo teste (%s).",
            len(recipients),
            periodicidade,
        )
        return recipients

    recipients: List[ProjectRecipient] = []
    for project in projects:
        tier: Optional[Tiers] = project.tier
        if tier is None:
            if include_without_tier:
                recipients.append(_to_recipient(project))
            else:
                logger.debug(
                    "Projeto %s sem tier; ignorado (use --include-without-tier).",
                    project.id,
                )
            continue

        if tier.deleted_at is not None:
            logger.debug("Tier do projeto %s está inativo.", project.id)
            continue

        frequencies = tier.periodicidade_email or []
        if periodicidade not in frequencies:
            continue

        recipients.append(_to_recipient(project))

    logger.info(
        "%s destinatário(s) para periodicidade %s.",
        len(recipients),
        periodicidade,
    )
    return recipients


def get_recipient_by_id(session: Session, projeto_id: int) -> Optional[ProjectRecipient]:
    project = session.get(Projetos, projeto_id)
    if project is None or project.deleted_at is not None:
        return None
    return _to_recipient(project)


def _to_recipient(project: Projetos) -> ProjectRecipient:
    return ProjectRecipient(
        id=int(project.id),
        email=str(project.email).strip(),
        nome=str(project.nome),
        cliente=project.cliente,
        tier_id=int(project.tier_id) if project.tier_id is not None else None,
    )


def _display_name(full_name: Optional[str], name: Optional[str]) -> str:
    raw = (name or full_name or "Parlamentar").strip()
    return format_parliamentarian_display_name(raw)


def list_favorite_parliamentarians(
    session: Session, projeto_id: int
) -> List[FavoriteParliamentarian]:
    """Retorna favoritos do projeto, ordenados por nome."""
    stmt = (
        select(
            Parliamentarian.id,
            Parliamentarian.full_name,
            Parliamentarian.name,
            Parliamentarian.type,
        )
        .join(
            ProjetosParliamentarian,
            ProjetosParliamentarian.parliamentarian_id == Parliamentarian.id,
        )
        .where(ProjetosParliamentarian.projeto_id == projeto_id)
        .where(ProjetosParliamentarian.deleted_at.is_(None))
        .order_by(Parliamentarian.name)
    )
    favorites: List[FavoriteParliamentarian] = []
    for parliamentarian_id, full_name, name, mp_type in session.execute(stmt).all():
        favorites.append(
            FavoriteParliamentarian(
                id=int(parliamentarian_id),
                display_name=_display_name(full_name, name),
                chamber=chamber_label_from_parliamentarian_type(mp_type),
            )
        )
    return favorites


def list_favorite_parliamentarian_names(
    session: Session, projeto_id: int
) -> List[str]:
    return [fav.display_name for fav in list_favorite_parliamentarians(session, projeto_id)]


def list_favorite_parliamentarian_ids(session: Session, projeto_id: int) -> List[int]:
    stmt = select(ProjetosParliamentarian.parliamentarian_id).where(
        ProjetosParliamentarian.projeto_id == projeto_id,
        ProjetosParliamentarian.deleted_at.is_(None),
    )
    return [int(row) for row in session.execute(stmt).scalars().all()]


def date_range_for_period(past_days: int) -> tuple[date, date, datetime, datetime]:
    end_date = now_local().date()
    start_date = end_date - timedelta(days=max(past_days - 1, 0))
    start_dt = combine_local(start_date, datetime.min.time())
    end_dt_exclusive = combine_local(
        end_date + timedelta(days=1),
        datetime.min.time(),
    )
    return start_date, end_date, start_dt, end_dt_exclusive


def _limit_per_parliamentarian(total_limit: int, parliamentarian_count: int) -> int:
    if parliamentarian_count <= 0:
        return 0
    return max(1, total_limit // parliamentarian_count)


def fetch_recent_propositions(
    session: Session,
    parliamentarian_id: int,
    parliamentarian_name: str,
    range_start: date,
    range_end: date,
    range_start_dt: datetime,
    range_end_dt_exclusive: datetime,
    limit: int,
    *,
    include_ingested: bool = False,
) -> List[ActivityItem]:
    activity_date = func.coalesce(
        Proposition.presentation_date,
        cast(Proposition.created_at, Date),
    )
    stmt = (
        select(
            Proposition.title,
            Proposition.link,
            Proposition.proposition_code,
            Proposition.proposition_acronym,
            Proposition.proposition_number,
            Proposition.presentation_year,
            Proposition.proposition_description,
            Proposition.summary,
            Proposition.presentation_date,
            Proposition.created_at,
            Proposition.id,
        )
        .select_from(AuthorsProposition)
        .join(Proposition, Proposition.id == AuthorsProposition.proposition_id)
        .where(AuthorsProposition.parliamentarian_id == parliamentarian_id)
        .where(
            proposition_in_period(
                range_start,
                range_end,
                range_start_dt,
                range_end_dt_exclusive,
                include_ingested=include_ingested,
            )
        )
        .order_by(activity_date.desc().nulls_last(), Proposition.id.desc())
        .limit(limit)
    )
    items: List[ActivityItem] = []
    for (
        title,
        link,
        proposition_code,
        acronym,
        number,
        year,
        prop_description,
        prop_summary,
        presentation_date,
        created_at,
        proposition_id,
    ) in session.execute(stmt).all():
        occurred = presentation_date
        if occurred is None and created_at is not None:
            occurred = created_at.date()
        if presentation_date:
            subtitle = f"Apresentada em {presentation_date.strftime('%d/%m/%Y')}"
        elif created_at is not None:
            subtitle = (
                f"Registrada no Mamute em {created_at.strftime('%d/%m/%Y')}"
            )
        else:
            subtitle = ""
        items.append(
            ActivityItem(
                kind="proposição",
                title=format_proposition_display_title(
                    title=title,
                    link=link,
                    acronym=acronym,
                    number=number,
                    year=year,
                ),
                subtitle=subtitle,
                parliamentarian_name=parliamentarian_name,
                ementa=extract_ementa(prop_description, prop_summary),
                link=resolve_proposition_link(
                    link,
                    proposition_code,
                    camara=is_camara_proposition(link),
                ),
                occurred_at=occurred,
                kind_key="proposicao",
                item_id=int(proposition_id),
            )
        )
    return items


def _build_vote_activity_item(
    *,
    vote: Optional[str],
    description: Optional[str],
    vote_link: Optional[str],
    prop_title: Optional[str],
    prop_link: Optional[str],
    proposition_code: Optional[int],
    prop_acronym: Optional[str],
    prop_number: Optional[int],
    prop_year: Optional[int],
    prop_description: Optional[str],
    prop_summary: Optional[str],
    prop_details: Any,
    presentation_date: Optional[date],
    parliamentarian_name: str,
    vote_id: Optional[int] = None,
) -> ActivityItem:
    vote_date = vote_occurred_at(
        prop_details=prop_details,
        presentation_date=presentation_date,
    )
    vote_label = (vote or description or "Voto").strip()
    title = format_proposition_display_title(
        title=prop_title,
        link=prop_link,
        acronym=prop_acronym,
        number=prop_number,
        year=prop_year,
    )
    date_label = format_activity_date(vote_date)
    subtitle = f"{vote_label} — {date_label}" if date_label else vote_label
    return ActivityItem(
        kind="votação",
        title=title,
        subtitle=subtitle,
        parliamentarian_name=parliamentarian_name,
        ementa=extract_ementa(prop_description, prop_summary),
        link=resolve_vote_display_link(
            vote_link,
            prop_link,
            proposition_code,
            camara=is_camara_proposition(prop_link),
        ),
        occurred_at=vote_date,
        kind_key="votacao",
        item_id=vote_id,
    )


def _iter_vote_candidates(
    session: Session,
    parliamentarian_id: int,
    *,
    candidate_limit: int,
):
    stmt = (
        select(
            RollCallVote.vote,
            RollCallVote.description,
            RollCallVote.link,
            Proposition.title,
            Proposition.link,
            Proposition.proposition_code,
            Proposition.proposition_acronym,
            Proposition.proposition_number,
            Proposition.presentation_year,
            Proposition.proposition_description,
            Proposition.summary,
            Proposition.details,
            Proposition.presentation_date,
            RollCallVote.id,
        )
        .join(Proposition, Proposition.id == RollCallVote.proposition_id)
        .where(RollCallVote.parliamentarian_id == parliamentarian_id)
        .order_by(
            Proposition.presentation_date.desc().nulls_last(),
            RollCallVote.id.desc(),
        )
        .limit(candidate_limit)
    )
    yield from session.execute(stmt).all()


def _unpack_vote_row(row: tuple) -> dict[str, Any]:
    (
        vote,
        description,
        vote_link,
        prop_title,
        prop_link,
        proposition_code,
        prop_acronym,
        prop_number,
        prop_year,
        prop_description,
        prop_summary,
        prop_details,
        presentation_date,
        vote_id,
    ) = row
    return {
        "vote": vote,
        "description": description,
        "vote_link": vote_link,
        "prop_title": prop_title,
        "prop_link": prop_link,
        "proposition_code": proposition_code,
        "prop_acronym": prop_acronym,
        "prop_number": prop_number,
        "prop_year": prop_year,
        "prop_description": prop_description,
        "prop_summary": prop_summary,
        "prop_details": prop_details,
        "presentation_date": presentation_date,
        "vote_id": int(vote_id) if vote_id is not None else None,
    }


def _dedupe_speech_rows(rows: list[tuple]) -> list[tuple]:
    """Remove discursos duplicados (mesma data + mesmo resumo)."""
    seen: set[tuple] = set()
    unique: list[tuple] = []
    for row in rows:
        summary = (row[1] or "").strip()
        speech_date = row[4]
        speech_type = (row[5] or "").strip()
        key = (speech_date, summary or speech_type)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _proposition_link_by_speech_id(
    session: Session,
    speech_ids: List[int],
) -> dict[int, tuple[Optional[str], Optional[int]]]:
    """Primeira proposição vinculada por discurso (mais recente por apresentação)."""
    if not speech_ids:
        return {}
    stmt = (
        select(
            SpeechesTranscriptsProposition.speeches_transcripts_id,
            Proposition.link,
            Proposition.proposition_code,
        )
        .join(Proposition, Proposition.id == SpeechesTranscriptsProposition.proposition_id)
        .where(SpeechesTranscriptsProposition.speeches_transcripts_id.in_(speech_ids))
        .order_by(
            SpeechesTranscriptsProposition.speeches_transcripts_id,
            Proposition.presentation_date.desc().nulls_last(),
            Proposition.id.desc(),
        )
    )
    links: dict[int, tuple[Optional[str], Optional[int]]] = {}
    for speech_id, prop_link, proposition_code in session.execute(stmt).all():
        sid = int(speech_id)
        if sid not in links:
            links[sid] = (prop_link, proposition_code)
    return links


def _build_speech_activity_item(
    *,
    speech_id: int,
    summary: Optional[str],
    speech_link: Optional[str],
    publication_link: Optional[str],
    speech_date: Optional[date],
    speech_type: Optional[str],
    parliamentarian_name: str,
    proposition_by_speech: dict[int, tuple[Optional[str], Optional[int]]],
    short_summary: Optional[str] = None,
) -> ActivityItem:
    # O resumo curto já sai do job no tamanho certo; só o sumário bruto é cortado.
    text = (short_summary or "").strip() or cortar_na_frase(summary, 200)
    if not text:
        text = speech_type or "Discurso"

    prop_link, proposition_code = proposition_by_speech.get(speech_id, (None, None))
    display_link = resolve_speech_activity_link(
        speech_link,
        publication_link,
        proposition_link=prop_link,
        proposition_code=proposition_code,
        camara=is_camara_proposition(prop_link),
    )

    return ActivityItem(
        kind="discurso",
        title=text,
        subtitle=(
            f"{speech_date.strftime('%d/%m/%Y')}" if speech_date else ""
        ),
        parliamentarian_name=parliamentarian_name,
        link=display_link,
        occurred_at=speech_date,
        kind_key="discurso",
        item_id=speech_id,
    )


def fetch_recent_votes(
    session: Session,
    parliamentarian_id: int,
    parliamentarian_name: str,
    range_start_dt: datetime,
    range_end_dt_exclusive: datetime,
    limit: int,
) -> List[ActivityItem]:
    range_start = range_start_dt.date()
    range_end = range_end_dt_exclusive.date() - timedelta(days=1)

    stmt = (
        select(
            RollCallVote.vote,
            RollCallVote.description,
            RollCallVote.link,
            Proposition.title,
            Proposition.link,
            Proposition.proposition_code,
            Proposition.proposition_acronym,
            Proposition.proposition_number,
            Proposition.presentation_year,
            Proposition.proposition_description,
            Proposition.summary,
            Proposition.details,
            Proposition.presentation_date,
            RollCallVote.id,
        )
        .join(Proposition, Proposition.id == RollCallVote.proposition_id)
        .where(RollCallVote.parliamentarian_id == parliamentarian_id)
        .where(vote_in_period(range_start, range_end))
        .order_by(vote_on_sql().desc().nulls_last(), RollCallVote.id.desc())
        .limit(limit)
    )
    items: List[ActivityItem] = []
    for row in session.execute(stmt).all():
        items.append(
            _build_vote_activity_item(
                **_unpack_vote_row(row),
                parliamentarian_name=parliamentarian_name,
            )
        )
    return items


def fetch_recent_speeches(
    session: Session,
    parliamentarian_id: int,
    parliamentarian_name: str,
    range_start: date,
    range_end: date,
    limit: int,
) -> List[ActivityItem]:
    stmt = (
        select(
            SpeechesTranscript.id,
            SpeechesTranscript.summary,
            SpeechesTranscript.speech_link,
            SpeechesTranscript.publication_link,
            SpeechesTranscript.date,
            SpeechesTranscript.type,
        )
        .where(SpeechesTranscript.parliamentarian_id == parliamentarian_id)
        .where(SpeechesTranscript.date.is_not(None))
        .where(SpeechesTranscript.date >= range_start)
        .where(SpeechesTranscript.date <= range_end)
        .order_by(SpeechesTranscript.date.desc())
        .limit(limit)
    )
    rows = _dedupe_speech_rows(session.execute(stmt).all())
    speech_ids = [int(row[0]) for row in rows]
    prop_links = _proposition_link_by_speech_id(session, speech_ids)
    resumos = _short_summary_by_speech_id(session, speech_ids)
    return [
        _build_speech_activity_item(
            speech_id=int(row[0]),
            summary=row[1],
            speech_link=row[2],
            publication_link=row[3],
            speech_date=row[4],
            speech_type=row[5],
            parliamentarian_name=parliamentarian_name,
            proposition_by_speech=prop_links,
            short_summary=resumos.get(int(row[0])),
        )
        for row in rows
    ]


def _short_summary_by_speech_id(session: Session, speech_ids: List[int]) -> dict[int, str]:
    if not speech_ids or not tem_tabela(session, "speech_short_summary"):
        return {}
    stmt = select(_SPEECH_SHORT_SUMMARY.c.speech_id, _SPEECH_SHORT_SUMMARY.c.text).where(
        _SPEECH_SHORT_SUMMARY.c.speech_id.in_(speech_ids)
    )
    return {int(sid): texto for sid, texto in session.execute(stmt).all() if texto}


def _tipo_da_emenda(amendment_type: Optional[str]) -> str:
    """'Emenda Individual - Transferências...' -> 'Emenda individual'."""
    base = (amendment_type or "Emenda").split(" - ")[0].strip() or "Emenda"
    return base[:1].upper() + base[1:].lower()


def fetch_new_amendments(
    session: Session,
    parliamentarian_id: int,
    parliamentarian_name: str,
    range_start_dt: datetime,
    range_end_dt_exclusive: datetime,
    limit: int,
) -> List[ActivityItem]:
    """Emendas que apareceram na base no período (a tabela não tem data de indicação)."""
    stmt = (
        select(
            ParliamentaryAmendment.id,
            ParliamentaryAmendment.amendment_type,
            ParliamentaryAmendment.spending_locality,
            ParliamentaryAmendment.function,
            ParliamentaryAmendment.committed_value,
            ParliamentaryAmendment.created_at,
        )
        .where(ParliamentaryAmendment.parliamentarian_id == parliamentarian_id)
        .where(ParliamentaryAmendment.created_at >= range_start_dt)
        .where(ParliamentaryAmendment.created_at < range_end_dt_exclusive)
        .order_by(
            ParliamentaryAmendment.committed_value.desc().nulls_last(),
            ParliamentaryAmendment.id.desc(),
        )
        .limit(limit)
    )
    items: List[ActivityItem] = []
    for amendment_id, tipo, local, funcao, valor, created_at in session.execute(stmt).all():
        titulo = _tipo_da_emenda(tipo)
        if valor:
            titulo = f"{titulo} de {format_brl(valor)}"
        destino = " · ".join(
            parte
            for parte in (
                format_localidade(local),
                (funcao or "").strip(),
            )
            if parte
        )
        criada = created_at.date() if isinstance(created_at, datetime) else created_at
        items.append(
            ActivityItem(
                kind="emenda",
                title=titulo,
                subtitle=(
                    f"Registrada no Mamute em {criada.strftime('%d/%m/%Y')}" if criada else ""
                ),
                parliamentarian_name=parliamentarian_name,
                ementa=f"Para {destino}" if destino else None,
                occurred_at=criada,
                kind_key="emenda",
                item_id=int(amendment_id),
            )
        )
    return items


def _fetch_propositions_all_time(
    session: Session,
    parliamentarian_id: int,
    parliamentarian_name: str,
    limit: int,
) -> List[ActivityItem]:
    stmt = (
        select(
            Proposition.title,
            Proposition.link,
            Proposition.proposition_code,
            Proposition.proposition_acronym,
            Proposition.proposition_number,
            Proposition.presentation_year,
            Proposition.proposition_description,
            Proposition.summary,
            Proposition.presentation_date,
        )
        .select_from(AuthorsProposition)
        .join(Proposition, Proposition.id == AuthorsProposition.proposition_id)
        .where(AuthorsProposition.parliamentarian_id == parliamentarian_id)
        .where(Proposition.presentation_date.is_not(None))
        .order_by(Proposition.presentation_date.desc())
        .limit(limit)
    )
    items: List[ActivityItem] = []
    for (
        title,
        link,
        proposition_code,
        acronym,
        number,
        year,
        prop_description,
        prop_summary,
        presentation_date,
    ) in session.execute(stmt).all():
        items.append(
            ActivityItem(
                kind="proposição",
                title=format_proposition_display_title(
                    title=title,
                    link=link,
                    acronym=acronym,
                    number=number,
                    year=year,
                ),
                subtitle=(
                    f"Apresentada em {presentation_date.strftime('%d/%m/%Y')}"
                    if presentation_date
                    else ""
                ),
                parliamentarian_name=parliamentarian_name,
                ementa=extract_ementa(prop_description, prop_summary),
                link=resolve_proposition_link(
                    link,
                    proposition_code,
                    camara=is_camara_proposition(link),
                ),
                occurred_at=presentation_date,
            )
        )
    return items


def _fetch_votes_all_time(
    session: Session,
    parliamentarian_id: int,
    parliamentarian_name: str,
    limit: int,
) -> List[ActivityItem]:
    items: List[ActivityItem] = []
    for row in _iter_vote_candidates(
        session,
        parliamentarian_id,
        candidate_limit=max(limit * _VOTE_CANDIDATE_MULTIPLIER, limit),
    ):
        item = _build_vote_activity_item(
            **_unpack_vote_row(row),
            parliamentarian_name=parliamentarian_name,
        )
        items.append(item)
        if len(items) >= limit:
            break

    items.sort(key=_sort_key, reverse=True)
    return items[:limit]


def _fetch_speeches_all_time(
    session: Session,
    parliamentarian_id: int,
    parliamentarian_name: str,
    limit: int,
) -> List[ActivityItem]:
    stmt = (
        select(
            SpeechesTranscript.id,
            SpeechesTranscript.summary,
            SpeechesTranscript.speech_link,
            SpeechesTranscript.publication_link,
            SpeechesTranscript.date,
            SpeechesTranscript.type,
        )
        .where(SpeechesTranscript.parliamentarian_id == parliamentarian_id)
        .where(SpeechesTranscript.date.is_not(None))
        .order_by(SpeechesTranscript.date.desc())
        .limit(limit)
    )
    rows = _dedupe_speech_rows(session.execute(stmt).all())
    speech_ids = [int(row[0]) for row in rows]
    prop_links = _proposition_link_by_speech_id(session, speech_ids)
    resumos = _short_summary_by_speech_id(session, speech_ids)
    return [
        _build_speech_activity_item(
            speech_id=int(row[0]),
            summary=row[1],
            speech_link=row[2],
            publication_link=row[3],
            speech_date=row[4],
            speech_type=row[5],
            parliamentarian_name=parliamentarian_name,
            proposition_by_speech=prop_links,
            short_summary=resumos.get(int(row[0])),
        )
        for row in rows
    ]


def _collect_for_parliamentarian(
    session: Session,
    parliamentarian_id: int,
    parliamentarian_name: str,
    *,
    per_mp_limit: int,
    all_time: bool,
    include_ingested_propositions: bool = False,
    range_start: Optional[date] = None,
    range_end: Optional[date] = None,
    range_start_dt: Optional[datetime] = None,
    range_end_dt_exclusive: Optional[datetime] = None,
) -> List[ActivityItem]:
    per_kind = max(1, per_mp_limit // 3)
    chunk: List[ActivityItem] = []

    if all_time:
        chunk.extend(
            _fetch_votes_all_time(
                session, parliamentarian_id, parliamentarian_name, per_mp_limit
            )
        )
        chunk.extend(
            _fetch_propositions_all_time(
                session, parliamentarian_id, parliamentarian_name, per_kind
            )
        )
        chunk.extend(
            _fetch_speeches_all_time(
                session, parliamentarian_id, parliamentarian_name, per_kind
            )
        )
    else:
        assert range_start is not None and range_end is not None
        assert range_start_dt is not None and range_end_dt_exclusive is not None
        chunk.extend(
            fetch_recent_votes(
                session,
                parliamentarian_id,
                parliamentarian_name,
                range_start_dt,
                range_end_dt_exclusive,
                per_mp_limit,
            )
        )
        chunk.extend(
            fetch_recent_propositions(
                session,
                parliamentarian_id,
                parliamentarian_name,
                range_start,
                range_end,
                range_start_dt,
                range_end_dt_exclusive,
                per_kind,
                include_ingested=include_ingested_propositions,
            )
        )
        chunk.extend(
            fetch_recent_speeches(
                session,
                parliamentarian_id,
                parliamentarian_name,
                range_start,
                range_end,
                per_kind,
            )
        )
        chunk.extend(
            fetch_new_amendments(
                session,
                parliamentarian_id,
                parliamentarian_name,
                range_start_dt,
                range_end_dt_exclusive,
                per_kind,
            )
        )

    chunk.sort(key=_sort_key, reverse=True)
    return chunk[:per_mp_limit]


def fetch_project_highlights(
    session: Session,
    favorites: List[FavoriteParliamentarian],
    *,
    limit_total: int,
    all_time: bool = False,
    per_mp_limit: Optional[int] = None,
    include_ingested_propositions: bool = False,
    range_start: Optional[date] = None,
    range_end: Optional[date] = None,
    range_start_dt: Optional[datetime] = None,
    range_end_dt_exclusive: Optional[datetime] = None,
) -> List[ActivityItem]:
    """Busca destaques por parlamentar favorito (distribui o limite entre eles)."""
    if not favorites:
        return []

    per_mp = (
        per_mp_limit
        if per_mp_limit is not None
        else _limit_per_parliamentarian(limit_total, len(favorites))
    )
    highlights: List[ActivityItem] = []

    for favorite in favorites:
        highlights.extend(
            _collect_for_parliamentarian(
                session,
                favorite.id,
                favorite.display_name,
                per_mp_limit=per_mp,
                all_time=all_time,
                include_ingested_propositions=include_ingested_propositions,
                range_start=range_start,
                range_end=range_end,
                range_start_dt=range_start_dt,
                range_end_dt_exclusive=range_end_dt_exclusive,
            )
        )

    return highlights


def _sort_key(item: ActivityItem) -> datetime:
    value = item.occurred_at
    if value is None:
        return min_local()
    if isinstance(value, date) and not isinstance(value, datetime):
        return combine_local(value, datetime.min.time())
    if value.tzinfo is None:
        return value.replace(tzinfo=SAO_PAULO)
    return value


def fetch_mixed_highlights_all_time(
    session: Session,
    favorites: List[FavoriteParliamentarian],
    limit: int,
) -> List[ActivityItem]:
    """Últimas atividades sem filtro de período (modo total / teste)."""
    return fetch_project_highlights(
        session,
        favorites,
        limit_total=limit * max(len(favorites), 1),
        per_mp_limit=limit,
        all_time=True,
    )
