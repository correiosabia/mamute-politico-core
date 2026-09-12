from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api.routers import projects


def test_subtract_months_clamps_to_last_valid_day() -> None:
    assert projects._subtract_months(date(2026, 5, 31), 3) == date(2026, 2, 28)


def test_last_three_months_range_uses_sao_paulo_calendar(monkeypatch) -> None:
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 5, 25, 17, 10, tzinfo=tz)

        combine = staticmethod(datetime.combine)

    monkeypatch.setattr(projects, "datetime", FixedDateTime)

    range_start, range_end, range_start_dt, range_end_dt_exclusive = (
        projects._last_three_months_range_sao_paulo()
    )

    assert range_start == date(2026, 2, 25)
    assert range_end == date(2026, 5, 25)
    assert range_start_dt.isoformat() == "2026-02-25T00:00:00-03:00"
    assert range_end_dt_exclusive.isoformat() == "2026-05-26T00:00:00-03:00"


def _make_stats_session() -> Session:
    """Banco minimo com as tres fontes de presenca: as duas coletadas na
    fonte (Camara) e as votacoes nominais das quais o Senado e inferido."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            create table parliamentarian (
                id integer primary key,
                type text,
                parliamentarian_code integer,
                name text,
                full_name text,
                email text,
                telephone text,
                cpf text,
                status text,
                party text,
                state_of_birth text,
                city_of_birth text,
                state_elected text,
                site text,
                education text,
                office_name text,
                office_building text,
                office_number text,
                office_floor text,
                office_email text,
                biography_link text,
                biography_text text,
                details text,
                created_at datetime not null,
                updated_at datetime not null
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table roll_call_votes (
                id integer primary key,
                parliamentarian_id integer not null,
                proposition_id integer not null,
                vote text,
                description text,
                link text,
                vote_date date,
                created_at datetime not null,
                updated_at datetime not null
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table plenary_attendance (
                id integer primary key,
                parliamentarian_id integer not null,
                date date,
                description text,
                session_attendance text,
                daily_attendance_justification text,
                created_at datetime not null,
                updated_at datetime not null
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table committee_attendance (
                id integer primary key,
                parliamentarian_id integer not null,
                date date,
                description text,
                frequency text,
                created_at datetime not null,
                updated_at datetime not null
            )
            """
        )
        conn.exec_driver_sql(
            """
            insert into parliamentarian (id, type, name, created_at, updated_at)
            values
                (1, 'Senador', 'Senadora Monitorada', '2026-01-01 00:00:00', '2026-01-01 00:00:00'),
                (2, 'Deputado', 'Deputado Monitorado', '2026-01-01 00:00:00', '2026-01-01 00:00:00')
            """
        )
    return Session(engine)


def _add_votes(session: Session, rows: list[tuple[int, str, str]]) -> None:
    for index, (parliamentarian_id, vote_date, vote) in enumerate(rows, start=1):
        session.execute(
            text(
                "insert into roll_call_votes"
                " (id, parliamentarian_id, proposition_id, vote, vote_date,"
                "  created_at, updated_at)"
                " values (:id, :pid, 1, :vote, :vote_date,"
                "  '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            ),
            {"id": index, "pid": parliamentarian_id, "vote": vote, "vote_date": vote_date},
        )
    session.commit()


_JANELA = (date(2026, 1, 1), date(2026, 12, 31))


def test_senate_presence_counts_session_days_not_votes() -> None:
    """Um dia com 3 votacoes pesa igual a um dia com 1 — senao o dia cheio
    dominaria o indicador (CS-79)."""
    session = _make_stats_session()
    _add_votes(
        session,
        [
            (1, "2026-03-10", "Sim"),
            (1, "2026-03-10", "Nao"),
            (1, "2026-03-10", "Votou"),
            (1, "2026-03-11", "NCom"),
        ],
    )

    assert projects._calculate_attendance_avg_percent(session, [1], *_JANELA) == 50


def test_senate_presence_treats_day_as_present_when_any_vote_shows_up() -> None:
    """Sair mais cedo nao vira falta: presente em qualquer votacao do dia
    significa presente no dia."""
    session = _make_stats_session()
    _add_votes(session, [(1, "2026-03-10", "Sim"), (1, "2026-03-10", "AP")])

    assert projects._calculate_attendance_avg_percent(session, [1], *_JANELA) == 100


def test_senate_presence_counts_justified_absence_as_absence() -> None:
    """Licenca e missao sao ausencia, como na Camara — sem isso quase todo
    senador marcaria 100% e o numero nao diferenciaria ninguem."""
    session = _make_stats_session()
    _add_votes(
        session,
        [
            (1, "2026-03-10", "Sim"),
            (1, "2026-03-11", "LS"),
            (1, "2026-03-12", "MIS"),
            (1, "2026-03-13", "AP"),
        ],
    )

    assert projects._calculate_attendance_avg_percent(session, [1], *_JANELA) == 25


def test_senate_presence_ignores_codes_that_are_not_about_attendance() -> None:
    """`FAL` (falecimento) e `NH` (nao houve votacao) nao sao falta de
    ninguem: o dia sai do calculo em vez de contar como ausencia."""
    session = _make_stats_session()
    _add_votes(
        session,
        [
            (1, "2026-03-10", "Sim"),
            (1, "2026-03-11", "NH"),
            (1, "2026-03-12", "FAL"),
            (1, "2026-03-13", "codigo-que-a-api-inventou"),
        ],
    )

    assert projects._calculate_attendance_avg_percent(session, [1], *_JANELA) == 100


def test_senate_presence_ignores_days_without_any_record() -> None:
    """Dia sem registro nenhum e dia fora do exercicio (suplente que ainda
    nao assumiu, ministro licenciado), nao falta: so entram no denominador
    os dias em que o proprio senador tem registro."""
    session = _make_stats_session()
    _add_votes(
        session,
        [
            # A senadora so assumiu em marco; fevereiro tem sessao do colega.
            (2, "2026-02-05", "Sim"),
            (1, "2026-03-10", "Sim"),
            (1, "2026-03-11", "Sim"),
        ],
    )

    assert projects._calculate_attendance_avg_percent(session, [1], *_JANELA) == 100


def test_senate_presence_is_none_without_votes() -> None:
    """Sem votacao coletada o card mostra "sem dado", nunca 0%."""
    session = _make_stats_session()

    assert projects._calculate_attendance_avg_percent(session, [1], *_JANELA) is None


def test_senate_presence_survives_missing_vote_date_migration() -> None:
    """O deploy aplica migration depois de subir os containers: sem a coluna
    o indicador volta a "sem dado" em vez de estourar."""
    session = _make_stats_session()
    _add_votes(session, [(1, "2026-03-10", "Sim")])
    session.execute(text("alter table roll_call_votes drop column vote_date"))
    session.commit()

    assert projects._calculate_attendance_avg_percent(session, [1], *_JANELA) is None


def test_chamber_presence_still_reads_the_collected_tables() -> None:
    """A Camara nao muda: continua vindo de `plenary_attendance` (ADR 0001).
    A votacao nominal do deputado nao pode vazar para o indicador dele."""
    session = _make_stats_session()
    _add_votes(session, [(2, "2026-03-10", "Sim"), (2, "2026-03-11", "Sim")])
    session.execute(
        text(
            "insert into plenary_attendance"
            " (id, parliamentarian_id, date, session_attendance, created_at, updated_at)"
            " values (1, 2, '2026-03-10', 'Presente', '2026-01-01', '2026-01-01'),"
            "        (2, 2, '2026-03-11', 'Ausente', '2026-01-01', '2026-01-01')"
        )
    )
    session.commit()

    assert projects._calculate_attendance_avg_percent(session, [2], *_JANELA) == 50


def test_mixed_project_combines_both_sources() -> None:
    """Painel do projeto mistura as duas casas: senadora 100% (1 dia) e
    deputado 0% (1 sessao) viram 50%."""
    session = _make_stats_session()
    _add_votes(session, [(1, "2026-03-10", "Sim")])
    session.execute(
        text(
            "insert into plenary_attendance"
            " (id, parliamentarian_id, date, session_attendance, created_at, updated_at)"
            " values (1, 2, '2026-03-10', 'Ausente', '2026-01-01', '2026-01-01')"
        )
    )
    session.commit()

    assert projects._calculate_attendance_avg_percent(session, [1, 2], *_JANELA) == 50
