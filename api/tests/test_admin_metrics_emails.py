"""Aba E-mails: histórico do email_send_log + próximos disparos previstos."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api.services.admin_metrics import _next_email_run, metrics_emails

# terça 2026-07-14 09:00 UTC (antes das 11:00 do cron)
NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)


def _make_session(with_log_table: bool = True) -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table tiers (id integer primary key, tier_name_debug text, product_id text, "
            "detalhes text not null, created_at datetime, updated_at datetime, deleted_at datetime)"
        )
        conn.exec_driver_sql(
            "create table projetos (id integer primary key, nome text, cliente text, email text, "
            "tier_id integer, tag_ghost text, qtd_termos integer default 0, created_at datetime, "
            "updated_at datetime, deleted_at datetime)"
        )
        conn.exec_driver_sql(
            "create table projetos_parliamentarian (id integer primary key, projeto_id integer, "
            "parliamentarian_id integer, position integer, created_at datetime, "
            "updated_at datetime, deleted_at datetime)"
        )
        if with_log_table:
            conn.exec_driver_sql(
                "create table email_send_log (id integer primary key autoincrement, "
                "projeto_id bigint not null, email text not null, periodicidade text not null, "
                "status text not null, detail text, subject text, stats json, "
                "period_start date, period_end date, created_at timestamp)"
            )
        # tier 1: quinzenal; tier 2: diário+semanal; tier 3: sem periodicidade
        conn.exec_driver_sql(
            "insert into tiers (id, tier_name_debug, product_id, detalhes) values "
            "(1,'Cidadão Comum','free',:d1),(2,'Eleitor Elefante','p2',:d2),(3,'Mudo','p3',:d3)",
            {
                "d1": json.dumps({"periodicidade_email": ["fortnight"]}),
                "d2": json.dumps({"periodicidade_email": ["day", "week"]}),
                "d3": json.dumps({}),
            },
        )
        conn.exec_driver_sql(
            "insert into projetos (id, nome, email, tier_id, deleted_at) values "
            "(1,'Ana','ana@x.com',1,NULL),(2,'Bruno','bruno@x.com',1,NULL),"
            "(3,'Caio','caio@x.com',2,NULL),(4,'Deletado','del@x.com',1,'2026-01-01')"
        )
        # favoritos: só Ana (proj 1); um favorito soft-deletado no proj 3 não conta
        conn.exec_driver_sql(
            "insert into projetos_parliamentarian (projeto_id, parliamentarian_id, deleted_at) "
            "values (1,1,NULL),(3,2,'2026-01-01')"
        )
        if with_log_table:
            conn.exec_driver_sql(
                "insert into email_send_log (projeto_id, email, periodicidade, status, detail, "
                "subject, stats, period_start, period_end, created_at) values "
                "(1,'ana@x.com','fortnight','sent',NULL,'Relatório quinzenal',"
                "'{\"proposicoes\": 3}','2026-07-01','2026-07-16','2026-07-16 11:00:05'),"
                "(2,'bruno@x.com','fortnight','skipped_no_favorites',NULL,NULL,NULL,NULL,NULL,"
                "'2026-07-16 11:00:06'),"
                "(3,'caio@x.com','day','error','SMTP timeout',NULL,NULL,NULL,NULL,"
                "'2026-07-15 11:00:03')"
            )
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture()
def session() -> Session:
    return _make_session()


def test_next_email_run_all_periodicities() -> None:
    # NOW = terça 14/07 09:00 UTC
    assert _next_email_run("day", NOW) == datetime(2026, 7, 14, 11, 0, tzinfo=timezone.utc)
    assert _next_email_run("week", NOW) == datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc)
    assert _next_email_run("fortnight", NOW) == datetime(2026, 7, 16, 11, 0, tzinfo=timezone.utc)
    assert _next_email_run("month", NOW) == datetime(2026, 8, 1, 11, 0, tzinfo=timezone.utc)


def test_next_email_run_skips_today_after_cron_hour() -> None:
    after = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    assert _next_email_run("day", after) == datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc)


def test_metrics_emails_history_and_kpis(session: Session) -> None:
    data = metrics_emails(session, now=NOW)
    assert data["log_disponivel"] is True
    assert data["kpis"]["enviados"] == 1
    assert data["kpis"]["erros"] == 1
    assert data["kpis"]["pulados"] == 1
    # histórico ordenado do mais recente para o mais antigo
    assert [h["status"] for h in data["historico"]] == [
        "skipped_no_favorites",
        "sent",
        "error",
    ]
    sent = next(h for h in data["historico"] if h["status"] == "sent")
    assert sent["stats"] == {"proposicoes": 3}
    assert sent["period_start"] == "2026-07-01"
    assert sent["periodicidade_label"] == "Quinzenal"


def test_metrics_emails_upcoming(session: Session) -> None:
    proximos = {p["periodicidade"]: p for p in metrics_emails(session, now=NOW)["proximos"]}
    fortnight = proximos["fortnight"]
    assert fortnight["tiers"] == ["Cidadão Comum"]
    assert fortnight["destinatarios"] == 2  # Ana + Bruno (Deletado não conta)
    assert fortnight["com_favoritos"] == 1  # só Ana
    assert fortnight["proximo_envio"].startswith("2026-07-16T11:00")
    day = proximos["day"]
    assert day["tiers"] == ["Eleitor Elefante"]
    assert day["destinatarios"] == 1  # Caio
    assert day["com_favoritos"] == 0  # favorito do Caio está soft-deletado
    month = proximos["month"]
    assert month["tiers"] == []
    assert month["destinatarios"] == 0


def test_metrics_emails_without_log_table() -> None:
    session = _make_session(with_log_table=False)
    data = metrics_emails(session, now=NOW)
    assert data["log_disponivel"] is False
    assert data["historico"] == []
    assert data["kpis"]["enviados"] == 0
    # próximos envios continuam disponíveis mesmo sem a migration do log
    assert len(data["proximos"]) == 4


def test_destaques_gerais_contam_como_enviados(session: Session) -> None:
    """Com a flag email_destaques_gerais, quem era pulado passa a receber (CS-133)."""
    from sqlalchemy import text

    session.execute(
        text(
            "insert into email_send_log (projeto_id, email, periodicidade, status, created_at) values "
            "(2,'bruno@x.com','fortnight','sent_general','2026-07-16 11:00:07'),"
            "(1,'ana@x.com','fortnight','sent_no_activity','2026-07-16 11:00:08')"
        )
    )
    session.commit()

    kpis = metrics_emails(session, now=NOW)["kpis"]

    assert kpis["enviados"] == 3
    assert kpis["pulados"] == 1
