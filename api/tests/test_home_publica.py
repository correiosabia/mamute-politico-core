"""Números e destaques da home (CS-170): rota pública lida do snapshot de cobertura."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.routers import home_publica as rota
from api.services.home_numeros import home_numeros
from api.services.pagina_publicacao import definir_estado


SNAPSHOT = {
    "kpis": {},
    "parlamentares": {
        "deputados": {"nossa_base": 560, "cadeiras": 513, "status": "completo"},
        "senadores": {"nossa_base": 88, "cadeiras": 81, "status": "completo"},
    },
    "registros_oficiais": {"total": 523_456, "por_tabela": [], "nota": ""},
}


def _session(*, snapshot: dict | None = SNAPSHOT, com_tabelas: bool = True) -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if com_tabelas:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "create table coverage_snapshot (id integer primary key, payload text, computed_at text)"
            )
            conn.exec_driver_sql(
                "create table pagina_publicacao (chave text primary key, estado text not null, "
                "atualizado_em timestamp, atualizado_por text)"
            )
            if snapshot is not None:
                conn.exec_driver_sql(
                    "insert into coverage_snapshot (payload, computed_at) values (?, ?)",
                    (json.dumps(snapshot), "2026-11-10T04:00:00"),
                )
    return sessionmaker(bind=engine)()


@pytest.fixture(autouse=True)
def _limpa():
    rota.limpar_cache()
    yield
    main.app.dependency_overrides.clear()
    rota.limpar_cache()


def _cliente(db: Session) -> TestClient:
    # Sem sobrescrever verify_token: pega se a rota passar a exigir login.
    main.app.dependency_overrides[get_db] = lambda: db
    return TestClient(main.app)


def test_numeros_saem_do_snapshot() -> None:
    assert home_numeros(_session()) == {
        "parlamentares": 648,  # deputados + senadores, com suplentes
        "registros_oficiais": 523_456,
        "atualizado_em": "2026-11-10T04:00:00",
    }


def test_snapshot_antigo_sem_registros_oficiais_volta_none_e_nao_zero() -> None:
    antigo = {k: v for k, v in SNAPSHOT.items() if k != "registros_oficiais"}
    numeros = home_numeros(_session(snapshot=antigo))
    assert numeros["parlamentares"] == 648
    assert numeros["registros_oficiais"] is None


def test_sem_snapshot_ou_sem_tabela_volta_none() -> None:
    for db in (_session(snapshot=None), _session(com_tabelas=False)):
        numeros = home_numeros(db)
        assert numeros["parlamentares"] is None
        assert numeros["registros_oficiais"] is None


def test_rota_publica_responde_sem_login_e_nasce_nao_publicada() -> None:
    resposta = _cliente(_session()).get("/api/public/home")
    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["parlamentares"] == 648
    assert corpo["registros_oficiais"] == 523_456
    assert corpo["publicada"] is False
    assert corpo["eleicoes_2026_publica"] is False


def test_publicar_vale_na_hora_mesmo_com_numeros_em_cache() -> None:
    db = _session()
    cliente = _cliente(db)
    assert cliente.get("/api/public/home").json()["publicada"] is False

    definir_estado(db, "home_numeros", "publica", "admin@mamute.com")
    definir_estado(db, "eleicoes_2026", "publica", "admin@mamute.com")
    corpo = cliente.get("/api/public/home").json()
    assert corpo["publicada"] is True
    assert corpo["eleicoes_2026_publica"] is True


def test_admin_controla_home_numeros_pela_rota_de_paginas() -> None:
    from api.security import require_ghost_admin

    db = _session()
    with db.get_bind().begin() as conn:
        conn.exec_driver_sql(
            "create table admin_audit_log (id integer primary key, admin_email text not null, "
            "action text not null, entity text not null, entity_id text, before text, after text, "
            "created_at datetime not null default current_timestamp)"
        )
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@mamute.com"
    cliente = _cliente(db)

    resposta = cliente.put("/api/admin/pages/home_numeros", json={"estado": "publica"})
    assert resposta.status_code == 200
    assert cliente.get("/api/public/home").json()["publicada"] is True
