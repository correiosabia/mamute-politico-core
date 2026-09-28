"""CS-92: `themes` (áreas temáticas oficiais) no PropositionOut.

NULL e [] chegam diferentes na interface: None = ainda não coletado,
[] = coletado e a Casa não classificou. E a rota não pode quebrar na janela
do deploy em que a migration cs92 ainda não criou a coluna.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.routers import propositions


@pytest.fixture(autouse=True)
def _reset_themes_column_cache(monkeypatch) -> None:
    # O "coluna existe" fica em cache no módulo; cada teste começa sem ele.
    monkeypatch.setattr(propositions, "_THEMES_COLUMN_READY", False)


def _make_session(*, with_themes_column: bool) -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    themes_column = "themes text," if with_themes_column else ""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"""
            create table proposition (
                id integer primary key,
                proposition_code integer,
                title text,
                link text,
                proposition_acronym text,
                proposition_number integer,
                presentation_year integer,
                agency_id integer,
                proposition_type_id integer,
                proposition_status_id integer,
                current_status text,
                proposition_description text,
                presentation_date date,
                presentation_month integer,
                summary text,
                details text,
                {themes_column}
                created_at datetime not null,
                updated_at datetime not null
            )
            """
        )
        columns = "id, proposition_code, link, proposition_acronym, created_at, updated_at"
        camara = "'https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao=1'"
        if with_themes_column:
            conn.exec_driver_sql(
                f"""
                insert into proposition ({columns}, themes) values
                    (1, 2491207, {camara}, 'PL', '2026-01-01', '2026-01-01', '["Saúde", "Educação"]'),
                    (2, 2482073, {camara}, 'REQ', '2026-01-01', '2026-01-01', '[]'),
                    (3, 2600000, {camara}, 'PL', '2026-01-01', '2026-01-01', null)
                """
            )
        else:
            conn.exec_driver_sql(
                f"""
                insert into proposition ({columns}) values
                    (1, 2491207, {camara}, 'PL', '2026-01-01', '2026-01-01')
                """
            )
    return Session(engine)


def _client(db_session: Session) -> TestClient:
    app = main.create_app()

    def fake_get_db():
        yield db_session

    app.dependency_overrides[main.verify_token] = lambda: {"sub": "assinante@example.com"}
    app.dependency_overrides[get_db] = fake_get_db
    return TestClient(app)


def test_get_proposition_distinguishes_themed_unclassified_and_uncollected() -> None:
    db_session = _make_session(with_themes_column=True)
    try:
        client = _client(db_session)

        themed = client.get("/api/propositions/1")
        unclassified = client.get("/api/propositions/2")
        uncollected = client.get("/api/propositions/3")

        assert themed.status_code == 200
        assert themed.json()["themes"] == ["Saúde", "Educação"]
        assert unclassified.json()["themes"] == []
        assert uncollected.json()["themes"] is None
    finally:
        db_session.close()


def test_list_propositions_includes_themes() -> None:
    db_session = _make_session(with_themes_column=True)
    try:
        response = _client(db_session).get("/api/propositions/?sort_by=presentation_year")

        assert response.status_code == 200
        by_id = {item["id"]: item["themes"] for item in response.json()}
        assert by_id == {1: ["Saúde", "Educação"], 2: [], 3: None}
    finally:
        db_session.close()


def test_get_proposition_survives_missing_themes_migration() -> None:
    db_session = _make_session(with_themes_column=False)
    try:
        response = _client(db_session).get("/api/propositions/1")

        assert response.status_code == 200
        assert response.json()["themes"] is None
    finally:
        db_session.close()


def test_serializer_never_lazy_loads_or_trusts_malformed_themes() -> None:
    malformed = SimpleNamespace(
        id=1,
        proposition_code=1,
        title=None,
        link=None,
        proposition_acronym="PL",
        proposition_number=None,
        presentation_year=None,
        agency_id=None,
        proposition_type_id=None,
        proposition_status_id=None,
        current_status=None,
        proposition_description=None,
        presentation_date=None,
        presentation_month=None,
        summary=None,
        details=None,
        themes=["Saúde", 42, "  "],
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )

    assert propositions._serialize_proposition(malformed).themes == ["Saúde"]
