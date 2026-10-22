"""Temas agregados dos discursos por janela (CS-124).

A listagem paginada da nuvem para em 100 discursos; a rota agregada soma a
palavra-chave principal de todos os discursos da janela no banco.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api.routers import analysis
from api.routers import projects


_LEGISLATURA = (date(2023, 2, 1), date(2026, 10, 9))
_TRES_MESES = (date(2026, 7, 9), date(2026, 10, 9))


def _make_session(monkeypatch) -> Session:
    monkeypatch.setattr(analysis, "_current_legislature_range", lambda: _LEGISLATURA)
    monkeypatch.setattr(
        analysis,
        "_last_three_months_range_sao_paulo",
        lambda: (*_TRES_MESES, None, None),
    )
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table parliamentarian (id integer primary key, parliamentarian_code integer)"
        )
        conn.exec_driver_sql(
            "create table speeches_transcripts"
            " (id integer primary key, parliamentarian_id integer, date date)"
        )
        conn.exec_driver_sql(
            """
            create table speeches_transcripts_keywords (
                id integer primary key,
                speeches_transcripts_id integer not null,
                keyword text not null,
                term text not null,
                frequency integer not null,
                rank integer not null,
                is_primary boolean not null default 0,
                analysis_type text not null default 'spacy'
            )
            """
        )
        conn.exec_driver_sql(
            "insert into parliamentarian (id, parliamentarian_code) values (1, 5000), (2, 6000)"
        )
    return Session(engine)


def _add_speech(session: Session, speech_id: int, parliamentarian_id: int, day: str) -> None:
    session.execute(
        text("insert into speeches_transcripts values (:id, :pid, :day)"),
        {"id": speech_id, "pid": parliamentarian_id, "day": day},
    )


def _add_keyword(
    session: Session,
    speech_id: int,
    term: str,
    frequency: int,
    rank: int,
    is_primary: bool = True,
    keyword: str = "",
) -> None:
    session.execute(
        text(
            "insert into speeches_transcripts_keywords"
            " (speeches_transcripts_id, keyword, term, frequency, rank, is_primary)"
            " values (:sid, :keyword, :term, :frequency, :rank, :primary)"
        ),
        {
            "sid": speech_id,
            "keyword": keyword or term,
            "term": term,
            "frequency": frequency,
            "rank": rank,
            "primary": is_primary,
        },
    )


def _terms(session: Session, window: str, code: int = 5000, limit: int = 300):
    return analysis.list_parliamentarian_speech_terms(
        code, db=session, window=window, limit=limit
    )


def test_legislature_sums_every_speech_not_just_the_first_hundred(monkeypatch) -> None:
    """Quem discursa muito tinha a nuvem cortada nos 100 mais recentes."""
    session = _make_session(monkeypatch)
    for speech_id in range(1, 151):
        _add_speech(session, speech_id, 1, "2024-05-10")
        _add_keyword(session, speech_id, "saúde", 2, 1)
    session.commit()

    out = _terms(session, "legislature")

    assert out.speeches_count == 150
    assert out.speeches_analyzed == 150
    assert [(t.term, t.frequency, t.speeches) for t in out.terms] == [("saúde", 300, 150)]
    assert (out.date_from, out.date_to) == _LEGISLATURA


def test_windows_follow_the_stats_card(monkeypatch) -> None:
    """As duas janelas sao as mesmas do card, para os numeros baterem."""
    session = _make_session(monkeypatch)
    _add_speech(session, 1, 1, "2022-12-20")  # legislatura anterior
    _add_speech(session, 2, 1, "2024-03-01")
    _add_speech(session, 3, 1, "2026-09-01")
    _add_keyword(session, 1, "antigo", 9, 1)
    _add_keyword(session, 2, "educação", 3, 1)
    _add_keyword(session, 3, "segurança", 4, 1)
    session.commit()

    legislatura = _terms(session, "legislature")
    recentes = _terms(session, "last_3_months")

    assert {t.term for t in legislatura.terms} == {"educação", "segurança"}
    assert legislatura.speeches_count == 2
    assert [t.term for t in recentes.terms] == ["segurança"]
    assert (recentes.date_from, recentes.date_to) == _TRES_MESES


def test_only_the_best_ranked_primary_keyword_counts(monkeypatch) -> None:
    """Mesmo criterio da listagem: entre as principais vale a de menor rank,
    e palavra-chave que nao e principal nao entra."""
    session = _make_session(monkeypatch)
    _add_speech(session, 1, 1, "2025-01-10")
    _add_keyword(session, 1, "segunda", 50, 2)
    _add_keyword(session, 1, "primeira", 1, 1)
    _add_keyword(session, 1, "secundaria", 99, 3, is_primary=False)
    session.commit()

    out = _terms(session, "legislature")

    assert [t.term for t in out.terms] == ["primeira"]


def test_term_falls_back_to_keyword_when_empty(monkeypatch) -> None:
    session = _make_session(monkeypatch)
    _add_speech(session, 1, 1, "2025-01-10")
    _add_keyword(session, 1, "", 1, 1, keyword="reforma tributária")
    session.commit()

    assert [t.term for t in _terms(session, "legislature").terms] == ["reforma tributária"]


def test_distinguishes_no_speech_from_not_analyzed(monkeypatch) -> None:
    """Zero discurso e discurso sem analise sao coisas diferentes na tela."""
    session = _make_session(monkeypatch)
    _add_speech(session, 1, 1, "2025-01-10")
    session.commit()

    analisado = _terms(session, "legislature")
    outro = _terms(session, "legislature", code=6000)

    assert (analisado.speeches_count, analisado.speeches_analyzed) == (1, 0)
    assert analisado.terms == []
    assert (outro.speeches_count, outro.speeches_analyzed) == (0, 0)


def test_orders_by_frequency_and_respects_limit(monkeypatch) -> None:
    session = _make_session(monkeypatch)
    for speech_id, (term, frequency) in enumerate(
        [("a", 1), ("b", 5), ("c", 3), ("b", 1)], start=1
    ):
        _add_speech(session, speech_id, 1, "2025-01-10")
        _add_keyword(session, speech_id, term, frequency, 1)
    session.commit()

    out = _terms(session, "legislature", limit=2)

    assert [(t.term, t.frequency) for t in out.terms] == [("b", 6), ("c", 3)]


def test_dashboard_stats_count_speeches_in_both_windows(monkeypatch) -> None:
    """O card ganha os discursos da legislatura sem mexer na janela curta."""
    monkeypatch.setattr(projects, "_current_legislature_range", lambda: _LEGISLATURA)
    monkeypatch.setattr(
        projects,
        "_last_three_months_range_sao_paulo",
        lambda: (
            *_TRES_MESES,
            datetime(2026, 7, 9),
            datetime(2026, 10, 10),
        ),
    )
    calls: list[tuple[date, date]] = []

    def fake_count(db, ids, start, end):
        calls.append((start, end))
        return len(calls)

    monkeypatch.setattr(projects, "_count_speeches_in_range", fake_count)
    monkeypatch.setattr(projects, "_count_propositions_in_range", lambda *a: 0)
    monkeypatch.setattr(projects, "_calculate_attendance_avg_percent", lambda *a: None)
    monkeypatch.setattr(projects, "_count_recent_votes", lambda *a: 0)

    out = projects._build_dashboard_stats(None, [1])

    assert calls == [_TRES_MESES, _LEGISLATURA]
    assert (out.speeches_count, out.speeches_legislature) == (1, 2)
