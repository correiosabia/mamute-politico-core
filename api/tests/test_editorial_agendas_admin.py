"""Vocabulário de pautas editoriais: leitura pública e escrita admin (CS-72).

SQLite in-memory, gate e get_db sobrescritos — mesmo padrão de
test_word_cloud_terms.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.security import require_ghost_admin, verify_token


def _make_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            create table editorial_agenda (
                id integer primary key,
                name text not null,
                slug text not null unique,
                description text,
                position smallint not null default 0,
                active boolean not null default 1,
                vocabulary_version integer not null default 1,
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table parliamentarian_agenda (
                id integer primary key,
                parliamentarian_id integer not null,
                agenda_id integer not null,
                rank smallint not null,
                confidence numeric(3, 2),
                evidence text,
                model text,
                vocabulary_version integer not null default 1,
                computed_at datetime not null default current_timestamp
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
    main.app.dependency_overrides[verify_token] = lambda: None
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _put(client: TestClient, agendas: list[dict]):
    return client.put(
        "/api/admin/settings/editorial-agendas", json={"agendas": agendas}
    )


def _admin(client: TestClient) -> list[dict]:
    r = client.get("/api/admin/settings/editorial-agendas")
    assert r.status_code == 200
    return r.json()


def _publico(client: TestClient) -> list[dict]:
    r = client.get("/api/settings/editorial-agendas")
    assert r.status_code == 200
    return r.json()


MEIO_AMBIENTE = {
    "name": "Meio Ambiente",
    "slug": "meio-ambiente",
    "description": "Clima, desmatamento, licenciamento.",
}
SAUDE = {"name": "Saúde", "slug": "saude", "description": "SUS, vacinação."}


class TestLeitura:
    def test_vazio_quando_nada_configurado(self, client: TestClient) -> None:
        assert _admin(client) == []
        assert _publico(client) == []

    def test_position_vem_da_ordem_do_array(self, client: TestClient) -> None:
        _put(client, [SAUDE, MEIO_AMBIENTE])

        assert [a["slug"] for a in _publico(client)] == ["saude", "meio-ambiente"]
        assert [a["position"] for a in _publico(client)] == [1, 2]

    def test_publico_esconde_inativas_e_admin_mostra(self, client: TestClient) -> None:
        _put(client, [MEIO_AMBIENTE, {**SAUDE, "active": False}])

        assert [a["slug"] for a in _publico(client)] == ["meio-ambiente"]
        assert [a["slug"] for a in _admin(client)] == ["meio-ambiente", "saude"]


class TestSlugImutavel:
    """Editar slug quebraria a correspondência com as classificações."""

    def test_recusa_renomear_slug_de_pauta_existente(
        self, client: TestClient
    ) -> None:
        criado = _put(client, [MEIO_AMBIENTE]).json()[0]

        resposta = _put(
            client, [{**MEIO_AMBIENTE, "id": criado["id"], "slug": "ambiente"}]
        )

        assert resposta.status_code == 422
        assert "não pode ser alterado" in resposta.json()["detail"]

    def test_recusa_nao_grava_nada(self, client: TestClient) -> None:
        criado = _put(client, [MEIO_AMBIENTE]).json()[0]

        _put(client, [{**MEIO_AMBIENTE, "id": criado["id"], "slug": "ambiente"}])

        assert [a["slug"] for a in _admin(client)] == ["meio-ambiente"]

    def test_nome_e_descricao_podem_mudar(self, client: TestClient) -> None:
        criado = _put(client, [MEIO_AMBIENTE]).json()[0]

        resposta = _put(
            client,
            [
                {
                    "id": criado["id"],
                    "slug": "meio-ambiente",
                    "name": "Clima e Meio Ambiente",
                    "description": "Nova fronteira.",
                }
            ],
        )

        assert resposta.status_code == 200
        assert resposta.json()[0]["name"] == "Clima e Meio Ambiente"
        assert resposta.json()[0]["description"] == "Nova fronteira."


class TestNuncaApaga:
    def test_pauta_omitida_e_desativada_e_nao_removida(
        self, client: TestClient
    ) -> None:
        _put(client, [MEIO_AMBIENTE, SAUDE])

        _put(client, [MEIO_AMBIENTE])

        assert [a["slug"] for a in _publico(client)] == ["meio-ambiente"]
        inativas = [a for a in _admin(client) if not a["active"]]
        assert [a["slug"] for a in inativas] == ["saude"]

    def test_classificacao_da_pauta_desativada_sobrevive(
        self, client: TestClient, session: Session
    ) -> None:
        """Desativar não pode destruir o trabalho do job."""

        saude = _put(client, [MEIO_AMBIENTE, SAUDE]).json()[1]
        session.execute(
            text(
                "insert into parliamentarian_agenda "
                "(parliamentarian_id, agenda_id, rank) values (1, :a, 1)"
            ),
            {"a": saude["id"]},
        )
        session.commit()

        _put(client, [MEIO_AMBIENTE])

        sobreviveu = session.execute(
            text("select count(*) from parliamentarian_agenda where agenda_id = :a"),
            {"a": saude["id"]},
        ).scalar_one()
        assert sobreviveu == 1

    def test_reativar_traz_a_pauta_de_volta(self, client: TestClient) -> None:
        _put(client, [MEIO_AMBIENTE, SAUDE])
        _put(client, [MEIO_AMBIENTE])

        _put(client, [MEIO_AMBIENTE, SAUDE])

        assert [a["slug"] for a in _publico(client)] == ["meio-ambiente", "saude"]


class TestVocabularyVersion:
    """É o gatilho da reclassificação — sem ele o admin edita e nada acontece."""

    def test_pauta_nova_sobe_a_versao(self, client: TestClient) -> None:
        v1 = _put(client, [MEIO_AMBIENTE]).json()[0]["vocabulary_version"]

        v2 = _put(client, [MEIO_AMBIENTE, SAUDE]).json()[0]["vocabulary_version"]

        assert v2 > v1

    def test_editar_description_sobe_a_versao(self, client: TestClient) -> None:
        criado = _put(client, [MEIO_AMBIENTE]).json()[0]

        depois = _put(
            client,
            [{**MEIO_AMBIENTE, "id": criado["id"], "description": "Outra fronteira."}],
        ).json()[0]

        assert depois["vocabulary_version"] > criado["vocabulary_version"]

    def test_desativar_sobe_a_versao(self, client: TestClient) -> None:
        antes = _put(client, [MEIO_AMBIENTE, SAUDE]).json()

        depois = _put(client, [MEIO_AMBIENTE]).json()

        assert depois[0]["vocabulary_version"] > antes[0]["vocabulary_version"]

    def test_salvar_sem_editar_nao_sobe_a_versao(self, client: TestClient) -> None:
        """Abrir a tela e salvar não pode custar reclassificar 500 pessoas."""

        antes = _put(client, [MEIO_AMBIENTE, SAUDE]).json()

        depois = _put(
            client,
            [
                {k: a[k] for k in ("id", "name", "slug", "description", "active")}
                for a in antes
            ],
        ).json()

        assert [a["vocabulary_version"] for a in depois] == [
            a["vocabulary_version"] for a in antes
        ]

    def test_versao_e_a_mesma_em_todas_as_linhas(self, client: TestClient) -> None:
        """A versão é do vocabulário, não da pauta."""

        _put(client, [MEIO_AMBIENTE])
        depois = _put(client, [MEIO_AMBIENTE, SAUDE]).json()

        assert len({a["vocabulary_version"] for a in depois}) == 1


class TestNormalizacao:
    def test_slug_em_caixa_baixa_e_sem_espacos_sobrando(
        self, client: TestClient
    ) -> None:
        r = _put(client, [{**MEIO_AMBIENTE, "slug": "  Meio-Ambiente  "}])

        assert r.json()[0]["slug"] == "meio-ambiente"

    def test_item_sem_nome_ou_slug_e_ignorado(self, client: TestClient) -> None:
        r = _put(client, [MEIO_AMBIENTE, {"name": "", "slug": ""}])

        assert [a["slug"] for a in r.json()] == ["meio-ambiente"]

    def test_item_sem_id_mas_com_slug_conhecido_nao_duplica(
        self, client: TestClient
    ) -> None:
        """A tela pode perder o id num reload; o slug é a chave estável."""

        _put(client, [MEIO_AMBIENTE])

        r = _put(client, [{**MEIO_AMBIENTE, "name": "Meio Ambiente e Clima"}])

        assert len(r.json()) == 1
        assert r.json()[0]["name"] == "Meio Ambiente e Clima"


class TestAuditoria:
    def test_registra_quem_alterou_e_o_antes_e_depois(
        self, client: TestClient, session: Session
    ) -> None:
        _put(client, [MEIO_AMBIENTE])
        _put(client, [MEIO_AMBIENTE, SAUDE])

        linhas = session.execute(
            text(
                "select admin_email, action, entity, before, after "
                "from admin_audit_log order by id"
            )
        ).all()

        assert len(linhas) == 2
        assert linhas[1][0] == "admin@mamute.com"
        assert linhas[1][1] == "update_editorial_agendas"
        assert linhas[1][2] == "editorial_agenda"
        assert "saude" not in linhas[1][3]
        assert "saude" in linhas[1][4]


class TestGateAdmin:
    def test_escrita_nao_existe_para_nao_admin(self, session: Session) -> None:
        from fastapi import HTTPException

        def _nega():
            raise HTTPException(status_code=404, detail="Not Found")

        main.app.dependency_overrides[get_db] = lambda: session
        main.app.dependency_overrides[require_ghost_admin] = _nega
        try:
            r = TestClient(main.app).put(
                "/api/admin/settings/editorial-agendas", json={"agendas": []}
            )
            assert r.status_code == 404
        finally:
            main.app.dependency_overrides.clear()


class TestJanelaDeDeploy:
    def test_leitura_publica_responde_200_sem_a_tabela(self) -> None:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        sem_tabela = sessionmaker(bind=engine)()
        main.app.dependency_overrides[get_db] = lambda: sem_tabela
        main.app.dependency_overrides[verify_token] = lambda: None
        try:
            r = TestClient(main.app).get("/api/settings/editorial-agendas")

            assert r.status_code == 200
            assert r.json() == []
        finally:
            main.app.dependency_overrides.clear()
            sem_tabela.close()
