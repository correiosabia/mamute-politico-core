"""Drill-down por usuário: IA por dia, páginas mais usadas, trocas."""
from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.security import require_ghost_admin
from api.services.admin_metrics import metrics_user_detail

PERIOD = date(2026, 7, 1)
RATE = 5.0


def _make_session() -> Session:
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
        conn.exec_driver_sql(
            "create table chatbot_usage (id integer primary key, projeto_id integer, email text, "
            "request_id text, period_start date, status text, question_chars integer, answer_chars integer, "
            "model text, prompt_tokens integer, completion_tokens integer, cost_usd numeric, "
            "created_at datetime, updated_at datetime)"
        )
        conn.exec_driver_sql(
            "create table usage_events (id integer primary key, projeto_id integer, email text, "
            "event_type text not null, page text, parliamentarian_id integer, country text, region text, city text, lat real, lon real, created_at datetime)"
        )
        conn.exec_driver_sql(
            "insert into tiers (id, tier_name_debug, product_id, detalhes) values (1,'Cid','cidadao-mamute',:d)",
            {"d": json.dumps({"preco_mensal": 50, "qtd_consultas_ia_mes": 100, "qtd_termos": 10})},
        )
        conn.exec_driver_sql(
            "insert into projetos (id, nome, email, tier_id) values (1, 'Ana', 'ana@x.com', 1)"
        )
        conn.exec_driver_sql(
            "insert into chatbot_usage (projeto_id, email, request_id, period_start, status, "
            "prompt_tokens, completion_tokens, cost_usd, created_at) values "
            "(1,'ana@x.com','r1','2026-07-01','completed',100,200,0.10,'2026-07-01 10:00'),"
            "(1,'ana@x.com','r2','2026-07-01','completed',100,200,0.05,'2026-07-02 10:00')"
        )
        conn.exec_driver_sql(
            "insert into usage_events (projeto_id, event_type, page) values "
            "(1,'page_view','dashboard'),(1,'page_view','dashboard'),(1,'page_view','pesquisa')"
        )
        conn.exec_driver_sql(
            "insert into usage_events (projeto_id, event_type, parliamentarian_id) values "
            "(1,'favorite_added',10),(1,'favorite_added',11),(1,'favorite_removed',10)"
        )
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture()
def session() -> Session:
    return _make_session()


@pytest.fixture()
def client(session: Session) -> TestClient:
    main.app.dependency_overrides[get_db] = lambda: (yield session)
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@x.com"
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    session.close()


def test_user_detail(session: Session) -> None:
    d = metrics_user_detail(session, 1, PERIOD, RATE)
    assert d is not None
    assert d["email"] == "ana@x.com"
    assert len(d["ia_por_dia"]) == 2  # dois dias distintos
    assert {p["page"]: p["views"] for p in d["paginas"]} == {"dashboard": 2, "pesquisa": 1}
    assert d["trocas"] == {"adicionados": 2, "removidos": 1, "total": 3}


def test_user_detail_route_404(client: TestClient) -> None:
    assert client.get("/api/admin/metrics/users/999").status_code == 404
    assert client.get("/api/admin/metrics/users/1").status_code == 200
