"""Link curto com prévia de um destaque do relatório por e-mail (CS-116)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.services import share_cards

CODE = "Abc123XyZ0"
PNG = b"\x89PNG\r\n\x1a\nfake"


def _make_session(*, com_tabelas: bool = True) -> Session:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    if com_tabelas:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "create table share_link (code text primary key, kind text, item_id integer,"
                " title text, summary text, parliamentarian_name text, chamber text,"
                " occurred_at date, created_at datetime default current_timestamp)"
            )
            conn.exec_driver_sql(
                "create table share_card_cache (code text primary key, png blob,"
                " created_at datetime default current_timestamp)"
            )
            conn.exec_driver_sql(
                "insert into share_link (code, kind, item_id, title, summary, parliamentarian_name,"
                " chamber, occurred_at) values (?, 'discurso', 7, ?, ?, 'Ana Souza', 'Câmara', '2026-10-02')",
                (CODE, 'Defende a "ponte" <já>', "Cobra a obra & a estrada."),
            )
    return sessionmaker(bind=engine)()


@pytest.fixture()
def session() -> Session:
    return _make_session()


@pytest.fixture()
def client(session: Session, monkeypatch) -> TestClient:
    monkeypatch.setenv("MAMUTE_SITE_URL", "https://site.example")
    monkeypatch.delenv("OG_RENDER_URL", raising=False)
    monkeypatch.delenv("MAMUTE_SHARE_FALLBACK_IMAGE", raising=False)
    main.app.dependency_overrides[get_db] = lambda: session
    yield TestClient(main.app, follow_redirects=False)
    main.app.dependency_overrides.clear()


class TestPagina:
    def test_tags_de_previa_e_redirecionamento(self, client: TestClient) -> None:
        r = client.get(f"/api/s/{CODE}")

        assert r.status_code == 200
        corpo = r.text
        assert f'<meta property="og:image" content="https://site.example/api/s/{CODE}.png">' in corpo
        assert '<meta name="twitter:card" content="summary_large_image">' in corpo
        assert 'http-equiv="refresh" content="0; url=https://site.example"' in corpo
        assert "Ana Souza" in corpo

    def test_titulo_e_resumo_escapados(self, client: TestClient) -> None:
        corpo = client.get(f"/api/s/{CODE}").text

        assert "Defende a &quot;ponte&quot; &lt;já&gt;" in corpo
        assert "Cobra a obra &amp; a estrada." in corpo
        assert "<já>" not in corpo

    @pytest.mark.parametrize("code", ["naoexiste0", "curto", "com-hifen-x", "A" * 40])
    def test_codigo_invalido_ou_inexistente_vai_para_o_site(self, client: TestClient, code: str) -> None:
        r = client.get(f"/api/s/{code}")

        assert r.status_code == 302
        assert r.headers["location"] == "https://site.example"

    def test_tabela_ainda_nao_migrada_vai_para_o_site(self, monkeypatch) -> None:
        s = _make_session(com_tabelas=False)
        main.app.dependency_overrides[get_db] = lambda: s
        try:
            r = TestClient(main.app, follow_redirects=False).get(f"/api/s/{CODE}")
        finally:
            main.app.dependency_overrides.clear()

        assert r.status_code == 302


class TestImagem:
    def test_servida_do_cache_sem_chamar_o_render(self, client, session, monkeypatch) -> None:
        session.execute(text("insert into share_card_cache (code, png) values (:c, :p)"), {"c": CODE, "p": PNG})
        session.commit()
        monkeypatch.setattr(share_cards, "_render", lambda dados: pytest.fail("não devia renderizar"))

        r = client.get(f"/api/s/{CODE}.png")

        assert r.status_code == 200
        assert r.content == PNG
        assert r.headers["content-type"] == "image/png"

    def test_render_ok_grava_no_cache(self, client, session, monkeypatch) -> None:
        recebidos = []
        monkeypatch.setattr(share_cards, "_render", lambda dados: recebidos.append(dados) or PNG)

        r = client.get(f"/api/s/{CODE}.png")

        assert r.content == PNG
        assert recebidos[0]["title"] == 'Defende a "ponte" <já>'
        assert recebidos[0]["parliamentarian_name"] == "Ana Souza"
        assert recebidos[0]["occurred_at"] == "2026-10-02"
        assert session.execute(text("select count(*) from share_card_cache")).scalar_one() == 1

    def test_render_fora_do_ar_usa_imagem_padrao_e_nao_grava(self, client, session, monkeypatch) -> None:
        monkeypatch.setenv("MAMUTE_SHARE_FALLBACK_IMAGE", "https://site.example/logo.png")
        monkeypatch.setattr(share_cards, "_render", lambda dados: None)

        r = client.get(f"/api/s/{CODE}.png")

        assert r.status_code == 302
        assert r.headers["location"] == "https://site.example/logo.png"
        assert session.execute(text("select count(*) from share_card_cache")).scalar_one() == 0

    def test_sem_render_e_sem_imagem_padrao_404(self, client, monkeypatch) -> None:
        monkeypatch.setattr(share_cards, "_render", lambda dados: None)

        assert client.get(f"/api/s/{CODE}.png").status_code == 404

    def test_codigo_inexistente_404(self, client) -> None:
        assert client.get("/api/s/naoexiste0.png").status_code == 404


class TestChamadaAoRender:
    def test_sem_url_configurada_nao_chama(self, monkeypatch) -> None:
        monkeypatch.delenv("OG_RENDER_URL", raising=False)

        assert share_cards._render({"title": "x"}) is None

    def test_erro_de_rede_vira_none(self, monkeypatch) -> None:
        import requests

        monkeypatch.setenv("OG_RENDER_URL", "http://og-render:8000")

        def _falha(*a, **k):
            raise requests.ConnectionError("fora")

        monkeypatch.setattr(share_cards.requests, "post", _falha)

        assert share_cards._render({"title": "x"}) is None

    def test_resposta_que_nao_e_png_vira_none(self, monkeypatch) -> None:
        monkeypatch.setenv("OG_RENDER_URL", "http://og-render:8000")

        class _Resp:
            status_code = 200
            headers = {"content-type": "text/html"}
            content = b"<html>"

        monkeypatch.setattr(share_cards.requests, "post", lambda *a, **k: _Resp())

        assert share_cards._render({"title": "x"}) is None


def test_nao_segura_transacao_enquanto_espera_o_render(client, session, monkeypatch) -> None:
    """Revisão I4: render travado não pode prender conexão do pool por 10 s."""
    estado = {}

    def _render(dados):
        estado["em_transacao"] = session.in_transaction()
        return None

    monkeypatch.setattr(share_cards, "_render", _render)

    client.get(f"/api/s/{CODE}.png")

    assert estado["em_transacao"] is False


def test_timeout_de_conexao_e_leitura_somam_dez_segundos(monkeypatch) -> None:
    monkeypatch.setenv("OG_RENDER_URL", "http://og-render:8000")
    chamadas = {}

    def _post(url, json, timeout):
        chamadas["timeout"] = timeout
        raise share_cards.requests.Timeout()

    monkeypatch.setattr(share_cards.requests, "post", _post)

    share_cards._render({"title": "x"})

    assert sum(chamadas["timeout"]) <= 10
