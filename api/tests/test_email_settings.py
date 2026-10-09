"""Configurações do relatório por e-mail e imagens públicas (CS-134).

SQLite in-memory, gate e get_db sobrescritos (mesmo padrão de test_word_cloud_terms).
"""
from __future__ import annotations

import base64
import hashlib

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.security import require_ghost_admin

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _make_session(*, com_tabelas: bool = True) -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
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
        if com_tabelas:
            conn.exec_driver_sql(
                """
                create table email_settings (
                    key text primary key,
                    value text not null default '',
                    updated_at datetime not null default current_timestamp,
                    updated_by text
                )
                """
            )
            conn.exec_driver_sql(
                """
                create table public_image (
                    sha256 text primary key,
                    content_type text not null,
                    data blob not null,
                    created_at datetime not null default current_timestamp
                )
                """
            )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture()
def session() -> Session:
    s = _make_session()
    yield s
    s.close()


@pytest.fixture()
def client(session: Session) -> TestClient:
    main.app.dependency_overrides[get_db] = lambda: session
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@mamute.com"
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


URL = "/api/admin/settings/email"
CHAVES = {
    "banner_image_url",
    "banner_link_url",
    "footer_image_url",
    "footer_link_url",
    "instagram_url",
    "subscribe_url",
    "share_text",
}


class TestLeitura:
    def test_todas_as_chaves_vazias_quando_nada_configurado(self, client: TestClient) -> None:
        r = client.get(URL)

        assert r.status_code == 200
        assert r.json() == {k: "" for k in CHAVES}

    def test_tabela_ainda_nao_migrada_le_como_vazia(self) -> None:
        """O deploy sobe o código antes do alembic."""
        s = _make_session(com_tabelas=False)
        main.app.dependency_overrides[get_db] = lambda: s
        main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@mamute.com"
        try:
            r = TestClient(main.app).get(URL)
        finally:
            main.app.dependency_overrides.clear()

        assert r.status_code == 200
        assert r.json() == {k: "" for k in CHAVES}


class TestEscrita:
    def test_grava_e_devolve_o_estado_final(self, client: TestClient) -> None:
        r = client.put(
            URL,
            json={
                "instagram_url": "https://instagram.com/mamute",
                "share_text": "  Recebi isso no relatório.  ",
            },
        )

        assert r.status_code == 200
        body = r.json()
        assert body["instagram_url"] == "https://instagram.com/mamute"
        assert body["share_text"] == "Recebi isso no relatório."
        assert client.get(URL).json() == body

    def test_chave_ausente_no_put_nao_apaga_a_gravada(self, client: TestClient) -> None:
        client.put(URL, json={"instagram_url": "https://instagram.com/a"})
        client.put(URL, json={"subscribe_url": "https://site.com/assinar"})

        body = client.get(URL).json()
        assert body["instagram_url"] == "https://instagram.com/a"
        assert body["subscribe_url"] == "https://site.com/assinar"

    def test_string_vazia_limpa_o_campo(self, client: TestClient) -> None:
        client.put(URL, json={"instagram_url": "https://instagram.com/a"})
        client.put(URL, json={"instagram_url": ""})

        assert client.get(URL).json()["instagram_url"] == ""

    def test_aceita_caminho_relativo_de_imagem_enviada(self, client: TestClient) -> None:
        r = client.put(URL, json={"banner_image_url": "/api/public-images/abc"})

        assert r.status_code == 200

    def test_recusa_chave_desconhecida(self, client: TestClient) -> None:
        r = client.put(URL, json={"cor_do_fundo": "amarelo"})

        assert r.status_code == 422

    @pytest.mark.parametrize("url", ["javascript:alert(1)", "http://inseguro.com", "ftp://x"])
    def test_recusa_link_que_nao_e_https(self, client: TestClient, url: str) -> None:
        r = client.put(URL, json={"banner_link_url": url})

        assert r.status_code == 422
        assert "https://" in r.json()["detail"]

    def test_registra_auditoria(self, client: TestClient, session: Session) -> None:
        client.put(URL, json={"instagram_url": "https://instagram.com/a"})

        linha = session.execute(
            text("select admin_email, action, after from admin_audit_log")
        ).one()
        assert linha[0] == "admin@mamute.com"
        assert linha[1] == "update_email_settings"
        assert "instagram.com/a" in linha[2]


class TestImagens:
    def _upload(self, client: TestClient, data: bytes, content_type: str = "image/png"):
        return client.post(
            f"{URL}/images",
            json={"content_type": content_type, "data_base64": base64.b64encode(data).decode()},
        )

    def test_upload_devolve_url_publica_pelo_hash(self, client: TestClient) -> None:
        r = self._upload(client, PNG_1PX)

        assert r.status_code == 200
        sha = hashlib.sha256(PNG_1PX).hexdigest()
        assert r.json() == {"url": f"/api/public-images/{sha}"}

    def test_mesma_imagem_duas_vezes_nao_duplica(self, client: TestClient, session: Session) -> None:
        self._upload(client, PNG_1PX)
        self._upload(client, PNG_1PX)

        assert session.execute(text("select count(*) from public_image")).scalar_one() == 1

    def test_imagem_publica_e_servida_sem_login_com_cache(self, client: TestClient) -> None:
        url = self._upload(client, PNG_1PX).json()["url"]
        main.app.dependency_overrides.pop(require_ghost_admin)

        r = client.get(url)

        assert r.status_code == 200
        assert r.content == PNG_1PX
        assert r.headers["content-type"] == "image/png"
        assert "immutable" in r.headers["cache-control"]

    def test_hash_inexistente_404(self, client: TestClient) -> None:
        assert client.get("/api/public-images/" + "0" * 64).status_code == 404

    def test_recusa_tipo_que_nao_e_imagem(self, client: TestClient) -> None:
        r = self._upload(client, b"<script>", content_type="text/html")

        assert r.status_code == 422

    def test_recusa_acima_de_1_mb(self, client: TestClient) -> None:
        r = self._upload(client, b"\0" * (1024 * 1024 + 1))

        assert r.status_code == 422
        assert "1 MB" in r.json()["detail"]

    def test_recusa_base64_invalido(self, client: TestClient) -> None:
        r = client.post(f"{URL}/images", json={"content_type": "image/png", "data_base64": "@@@"})

        assert r.status_code == 422


def test_rotas_admin_nao_existem_para_nao_admin(session: Session) -> None:
    def _nega():
        raise HTTPException(status_code=404, detail="Not Found")

    main.app.dependency_overrides[get_db] = lambda: session
    main.app.dependency_overrides[require_ghost_admin] = _nega
    try:
        c = TestClient(main.app)
        assert c.get(URL).status_code == 404
        assert c.put(URL, json={}).status_code == 404
    finally:
        main.app.dependency_overrides.clear()
