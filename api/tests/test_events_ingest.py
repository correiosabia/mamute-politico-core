"""Ingesta de page_view events, resolvendo o projeto pelo e-mail do token."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.security import verify_token


def _make_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table projetos (id integer primary key, nome text, email text, "
            "tier_id integer, deleted_at datetime)"
        )
        conn.exec_driver_sql(
            """create table usage_events (id integer primary key, projeto_id integer,
               email text, event_type text not null, page text, section text,
               parliamentarian_id integer, country text, region text, city text, lat real, lon real, created_at datetime default current_timestamp)"""
        )
        conn.exec_driver_sql(
            "insert into projetos (id, nome, email) values (1, 'Ana', 'ana@x.com')"
        )
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture()
def session() -> Session:
    return _make_session()


@pytest.fixture()
def client(session: Session) -> TestClient:
    main.app.dependency_overrides[get_db] = lambda: (yield session)
    main.app.dependency_overrides[verify_token] = lambda: {"sub": "ana@x.com"}
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    session.close()


def test_ingest_page_views(client: TestClient, session: Session) -> None:
    resp = client.post(
        "/api/events",
        json={"events": [{"type": "page_view", "page": "dashboard"}, {"type": "page_view", "page": "pesquisa"}]},
    )
    assert resp.status_code == 204
    rows = session.execute(
        text("select projeto_id, email, event_type, page from usage_events order by id")
    ).all()
    assert len(rows) == 2
    assert rows[0] == (1, "ana@x.com", "page_view", "dashboard")


def test_ingest_section_view(client: TestClient, session: Session) -> None:
    resp = client.post(
        "/api/events",
        json={"events": [{"type": "section_view", "page": "parlamentar", "section": "taquigraficas"}]},
    )
    assert resp.status_code == 204
    row = session.execute(
        text("select event_type, page, section from usage_events order by id desc limit 1")
    ).first()
    assert row == ("section_view", "parlamentar", "taquigraficas")


def test_ingest_ignores_non_page_view(client: TestClient, session: Session) -> None:
    # cliente não pode forjar eventos de favorito (server-side only)
    resp = client.post(
        "/api/events",
        json={"events": [{"type": "favorite_added", "page": None}]},
    )
    assert resp.status_code == 204
    count = session.execute(text("select count(*) from usage_events")).scalar_one()
    assert count == 0
