"""CRUD admin de tiers + auditoria. SQLite in-memory, gate e get_db sobrescritos."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.security import require_ghost_admin


def _make_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            create table tiers (
                id integer primary key,
                tier_name_debug text not null,
                product_id text not null,
                detalhes text not null,
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp,
                deleted_at datetime
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table admin_audit_log (
                id integer primary key,
                admin_email text not null,
                action text not null,
                entity text not null,
                entity_id text,
                before text,
                after text,
                created_at datetime not null default current_timestamp
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table projetos (
                id integer primary key,
                nome text not null,
                cliente text,
                email text not null,
                tier_id integer,
                tag_ghost text,
                qtd_termos integer not null default 0,
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp,
                deleted_at datetime
            )
            """
        )
        conn.exec_driver_sql(
            "insert into tiers (id, tier_name_debug, product_id, detalhes) "
            "values (1, 'Cidadão', 'cidadao-mamute', :d)",
            {"d": json.dumps({"qtd_termos": 10, "qtd_consultas_ia_mes": 200})},
        )
        conn.exec_driver_sql(
            "insert into projetos (id, nome, email, tier_id) "
            "values (1, 'bot-um', 'um@x.com', 1), (2, 'bot-dois', 'dois@x.com', 1)"
        )
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


@pytest.fixture()
def client() -> TestClient:
    session = _make_session()

    def _override_get_db():
        try:
            yield session
        finally:
            pass

    main.app.dependency_overrides[get_db] = _override_get_db
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@x.com"
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    session.close()


def test_list_tiers(client: TestClient) -> None:
    resp = client.get("/api/admin/tiers")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["product_id"] == "cidadao-mamute"
    assert data[0]["detalhes"]["qtd_termos"] == 10


def test_update_tier_merges_and_audits(client: TestClient) -> None:
    resp = client.put(
        "/api/admin/tiers/1",
        json={"qtd_consultas_ia_mes": 500, "periodicidade_email": ["week", "month"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    # merge: mantém qtd_termos, atualiza a consulta e adiciona a periodicidade
    assert body["detalhes"]["qtd_termos"] == 10
    assert body["detalhes"]["qtd_consultas_ia_mes"] == 500
    assert body["detalhes"]["periodicidade_email"] == ["week", "month"]

    # segunda chamada: confere persistência + auditoria
    again = client.get("/api/admin/tiers").json()
    assert again[0]["detalhes"]["qtd_consultas_ia_mes"] == 500


def test_update_ignores_preco_mensal_read_only(client: TestClient) -> None:
    """preco_mensal vem do Ghost — o PUT admin não deve gravá-lo."""
    resp = client.put("/api/admin/tiers/1", json={"preco_mensal": 49.9})
    assert resp.status_code == 200
    assert "preco_mensal" not in resp.json()["detalhes"]


def test_update_rejects_negative(client: TestClient) -> None:
    resp = client.put("/api/admin/tiers/1", json={"qtd_termos": -3})
    assert resp.status_code == 422


def test_update_unknown_tier_404(client: TestClient) -> None:
    resp = client.put("/api/admin/tiers/999", json={"qtd_termos": 5})
    assert resp.status_code == 404


def test_update_tier_persists_per_house_limits(client: TestClient) -> None:
    resp = client.put(
        "/api/admin/tiers/1",
        json={"qtd_termos_camara": 5, "qtd_termos_senado": 2},
    )
    assert resp.status_code == 200
    detalhes = resp.json()["detalhes"]
    assert detalhes["qtd_termos_camara"] == 5
    assert detalhes["qtd_termos_senado"] == 2
    # persistência
    again = client.get("/api/admin/tiers").json()
    assert again[0]["detalhes"]["qtd_termos_camara"] == 5
    assert again[0]["detalhes"]["qtd_termos_senado"] == 2


def test_update_rejects_negative_per_house(client: TestClient) -> None:
    resp = client.put("/api/admin/tiers/1", json={"qtd_termos_camara": -1})
    assert resp.status_code == 422


def test_update_tier_persists_history_days(client: TestClient) -> None:
    """`qtd_dias_historico_ia` é o gate do histórico da Pesquisa IA (CS-55).

    O painel sempre enviou o campo, mas o schema do PUT não o declarava e o
    Pydantic descartava a chave em silêncio: 200 OK, auditoria com before ==
    after, e nenhum plano conseguia ligar o histórico.
    """
    resp = client.put("/api/admin/tiers/1", json={"qtd_dias_historico_ia": 90})
    assert resp.status_code == 200
    assert resp.json()["detalhes"]["qtd_dias_historico_ia"] == 90
    again = client.get("/api/admin/tiers").json()
    assert again[0]["detalhes"]["qtd_dias_historico_ia"] == 90


def test_update_rejects_negative_history_days(client: TestClient) -> None:
    resp = client.put("/api/admin/tiers/1", json={"qtd_dias_historico_ia": -1})
    assert resp.status_code == 422
