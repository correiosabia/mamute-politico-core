"""Página pública Eleições 2026: estado admin x pública (CS-162)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.routers import eleicoes_publicas as rota
from api.security import require_ghost_admin, verify_token
from api.services.pagina_publicacao import definir_estado, estado_da_pagina


def _session(com_tabela: bool = True) -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if com_tabela:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "create table pagina_publicacao (chave text primary key, estado text not null, "
                "atualizado_em timestamp, atualizado_por text)"
            )
            conn.exec_driver_sql(
                "create table admin_audit_log (id integer primary key, admin_email text not null, "
                "action text not null, entity text not null, entity_id text, before text, after text, "
                "created_at datetime not null default current_timestamp)"
            )
    return sessionmaker(bind=engine)()


@pytest.fixture
def db() -> Session:
    return _session()


@pytest.fixture(autouse=True)
def _servicos(monkeypatch):
    monkeypatch.setattr(rota, "elected_profile", lambda db: {"ano": 2026, "casas": []})
    monkeypatch.setattr(
        rota, "runoff_comparison",
        lambda db: {"disponivel": True, "recortes": [], "destaques": [{"texto": "interno"}]},
    )


def _cliente(db: Session, *, admin: bool = False) -> TestClient:
    main.app.dependency_overrides[get_db] = lambda: db
    main.app.dependency_overrides[verify_token] = lambda: None
    if admin:
        main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@mamute.com"
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _limpa():
    yield
    main.app.dependency_overrides.clear()


def test_sem_linha_e_sem_tabela_e_so_admin() -> None:
    assert estado_da_pagina(_session(), "eleicoes_2026") == "admin"
    assert estado_da_pagina(_session(com_tabela=False), "eleicoes_2026") == "admin"


def test_definir_e_ler_estado(db: Session) -> None:
    definir_estado(db, "eleicoes_2026", "publica", "admin@mamute.com")
    assert estado_da_pagina(db, "eleicoes_2026") == "publica"
    definir_estado(db, "eleicoes_2026", "admin", "admin@mamute.com")
    assert estado_da_pagina(db, "eleicoes_2026") == "admin"


def test_rotas_publicas_404_enquanto_so_admin(db: Session) -> None:
    cliente = _cliente(db)
    assert cliente.get("/api/public/eleicoes-2026/elected-profile").status_code == 404
    assert cliente.get("/api/public/eleicoes-2026/runoff-comparison").status_code == 404


def test_rotas_publicas_abrem_quando_publica_e_sem_destaques(db: Session) -> None:
    definir_estado(db, "eleicoes_2026", "publica", "admin@mamute.com")
    cliente = _cliente(db)
    assert cliente.get("/api/public/eleicoes-2026/elected-profile").json() == {"ano": 2026, "casas": []}
    comparacao = cliente.get("/api/public/eleicoes-2026/runoff-comparison").json()
    assert comparacao["disponivel"] is True and comparacao["destaques"] == []


def test_admin_le_e_muda_o_estado(db: Session) -> None:
    cliente = _cliente(db, admin=True)
    assert cliente.get("/api/admin/pages/eleicoes_2026").json() == {"chave": "eleicoes_2026", "estado": "admin"}
    resp = cliente.put("/api/admin/pages/eleicoes_2026", json={"estado": "publica"})
    assert resp.status_code == 200 and resp.json()["estado"] == "publica"
    assert estado_da_pagina(db, "eleicoes_2026") == "publica"
    log = db.execute(text("select action, entity_id, before, after from admin_audit_log")).one()
    assert log.action == "set_page_visibility" and log.entity_id == "eleicoes_2026"
    assert '"admin"' in log.before and '"publica"' in log.after


def test_admin_recusa_estado_e_pagina_desconhecidos(db: Session) -> None:
    cliente = _cliente(db, admin=True)
    assert cliente.put("/api/admin/pages/eleicoes_2026", json={"estado": "assinantes"}).status_code == 422
    assert cliente.get("/api/admin/pages/outra").status_code == 404


def test_admin_404_para_quem_nao_e_admin(db: Session) -> None:
    cliente = _cliente(db)
    assert cliente.put("/api/admin/pages/eleicoes_2026", json={"estado": "publica"}).status_code == 404
