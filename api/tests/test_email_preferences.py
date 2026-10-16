"""Descadastro dos e-mails do Mamute (CS-150): Minha conta e link sem login."""
from __future__ import annotations

from typing import Iterator

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.services.email_preferences import mascarar_email


def _make_session(*, com_colunas: bool = True) -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    extra = ", emails_desativados_em datetime, email_token text" if com_colunas else ""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table projetos (id integer primary key, nome text, cliente text, "
            "email text, tier_id integer, tag_ghost text, qtd_termos integer default 0, "
            f"created_at datetime, updated_at datetime, deleted_at datetime{extra})"
        )
        if com_colunas:
            conn.exec_driver_sql(
                "insert into projetos (id, nome, email, email_token, deleted_at) values "
                "(1, 'Ana', 'ana@x.com', 'tok-ana', NULL),"
                "(2, 'Bia', 'bia@x.com', 'tok-bia', NULL),"
                "(3, 'Del', 'del@x.com', 'tok-del', '2026-01-01')"
            )
        else:
            conn.exec_driver_sql(
                "insert into projetos (id, nome, email) values (1, 'Ana', 'ana@x.com')"
            )
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _client(db: Session, token_email: str = "ana@x.com") -> TestClient:
    def fake_verify_token(request: Request) -> dict[str, str]:
        request.state.token_email = token_email
        return {"sub": token_email}

    def fake_get_db() -> Iterator[Session]:
        yield db

    main.app.dependency_overrides[main.verify_token] = fake_verify_token
    main.app.dependency_overrides[get_db] = fake_get_db
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _limpa_overrides() -> Iterator[None]:
    yield
    main.app.dependency_overrides.clear()


def _desativado(db: Session, projeto_id: int):
    return db.execute(
        text("select emails_desativados_em from projetos where id = :id"), {"id": projeto_id}
    ).scalar()


def test_minha_conta_le_e_grava_a_preferencia() -> None:
    db = _make_session()
    client = _client(db)

    resp = client.get("/api/projects/me/email-preferences")
    assert resp.status_code == 200
    assert resp.json() == {"recebe": True, "email_mascarado": "an***@x.com"}

    resp = client.put("/api/projects/me/email-preferences", json={"recebe": False})
    assert resp.status_code == 200
    assert resp.json()["recebe"] is False
    assert _desativado(db, 1) is not None
    assert _desativado(db, 2) is None  # só o projeto do token

    resp = client.put("/api/projects/me/email-preferences", json={"recebe": True})
    assert resp.json()["recebe"] is True
    assert _desativado(db, 1) is None


def test_link_do_email_descadastra_sem_login() -> None:
    db = _make_session()
    client = TestClient(main.app)
    main.app.dependency_overrides[get_db] = lambda: (yield db)

    # Corpo do botão do Gmail (RFC 8058) é aceito e ignorado.
    resp = client.post(
        "/api/email-preferences/unsubscribe?t=tok-bia",
        data={"List-Unsubscribe": "One-Click"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"recebe": False, "email_mascarado": "bi***@x.com"}
    primeira = _desativado(db, 2)
    assert primeira is not None

    # Clicar de novo não muda a data do primeiro pedido.
    assert client.post("/api/email-preferences/unsubscribe?t=tok-bia").status_code == 200
    assert _desativado(db, 2) == primeira

    resp = client.post("/api/email-preferences/resubscribe?t=tok-bia")
    assert resp.json()["recebe"] is True
    assert _desativado(db, 2) is None


def test_link_nao_aceita_get() -> None:
    db = _make_session()
    main.app.dependency_overrides[get_db] = lambda: (yield db)
    resp = TestClient(main.app).get("/api/email-preferences/unsubscribe?t=tok-ana")
    assert resp.status_code == 405
    assert _desativado(db, 1) is None


@pytest.mark.parametrize("token", ["", "nao-existe", "tok-del"])
def test_token_invalido_ou_de_conta_apagada(token: str) -> None:
    db = _make_session()
    main.app.dependency_overrides[get_db] = lambda: (yield db)
    resp = TestClient(main.app).post(f"/api/email-preferences/unsubscribe?t={token}")
    assert resp.status_code == 404
    assert "Minha conta" in resp.json()["detail"]


def test_antes_da_migration_responde_503_com_texto_de_tela() -> None:
    db = _make_session(com_colunas=False)
    client = _client(db)
    resp = client.get("/api/projects/me/email-preferences")
    assert resp.status_code == 503
    assert "Tente de novo" in resp.json()["detail"]
    resp = client.post("/api/email-preferences/unsubscribe?t=qualquer")
    assert resp.status_code == 503


def test_mascarar_email() -> None:
    assert mascarar_email("luciana@gmail.com") == "lu***@gmail.com"
    assert mascarar_email("a@b.com") == "a***@b.com"
    assert mascarar_email("sem-arroba") == "***"
