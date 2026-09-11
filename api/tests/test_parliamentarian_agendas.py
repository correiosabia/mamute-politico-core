"""Pautas editoriais expostas na listagem de parlamentares (CS-72).

SQLite in-memory e get_db sobrescrito — mesmo padrão de test_word_cloud_terms.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.security import verify_token

DDL_PARLIAMENTARIAN = """
create table parliamentarian (
    id integer primary key,
    type text,
    parliamentarian_code integer,
    name text,
    full_name text,
    email text,
    telephone text,
    cpf text,
    status text,
    party text,
    state_of_birth text,
    city_of_birth text,
    state_elected text,
    site text,
    education text,
    office_name text,
    office_building text,
    office_number text,
    office_floor text,
    office_email text,
    biography_link text,
    biography_text text,
    details text,
    created_at datetime not null default current_timestamp,
    updated_at datetime not null default current_timestamp
)
"""

# O endpoint de detalhe faz selectinload das redes sociais.
DDL_SOCIAL = (
    """
    create table social_network (
        id integer primary key,
        name text,
        created_at datetime not null default current_timestamp,
        updated_at datetime not null default current_timestamp
    )
    """,
    """
    create table parliamentarian_social_network (
        id integer primary key,
        parliamentarian_id integer not null,
        social_network_id integer,
        profile_url text,
        created_at datetime not null default current_timestamp,
        updated_at datetime not null default current_timestamp
    )
    """,
)

DDL_AGENDA = """
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

DDL_PARLIAMENTARIAN_AGENDA = """
create table parliamentarian_agenda (
    id integer primary key,
    parliamentarian_id integer not null references parliamentarian(id) on delete cascade,
    agenda_id integer not null references editorial_agenda(id) on delete cascade,
    rank smallint not null,
    confidence numeric(3, 2),
    evidence text,
    model text,
    vocabulary_version integer not null default 1,
    computed_at datetime not null default current_timestamp,
    check (rank between 1 and 3),
    unique (parliamentarian_id, agenda_id),
    unique (parliamentarian_id, rank)
)
"""


def _make_session(*, com_tabelas_de_pauta: bool = True) -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(DDL_PARLIAMENTARIAN)
        for ddl in DDL_SOCIAL:
            conn.exec_driver_sql(ddl)
        if com_tabelas_de_pauta:
            conn.exec_driver_sql(DDL_AGENDA)
            conn.exec_driver_sql(DDL_PARLIAMENTARIAN_AGENDA)

        # Dois deputados em exercício: o catálogo padrão (current_only) só
        # mostra quem está em exercício.
        for pid, nome in ((1, "Fulano"), (2, "Beltrano")):
            conn.exec_driver_sql(
                "insert into parliamentarian (id, type, name, status) "
                f"values ({pid}, 'Deputado', '{nome}', 'Exercício')"
            )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _semear_pautas(session: Session) -> None:
    session.execute(
        text(
            "insert into editorial_agenda (id, name, slug, position, active) values "
            "(10, 'Meio Ambiente', 'meio-ambiente', 1, 1), "
            "(20, 'Saúde', 'saude', 2, 1), "
            "(30, 'Política Externa', 'politica-externa', 3, 0)"
        )
    )
    session.commit()


def _classificar(session: Session, parliamentarian_id: int, *pares) -> None:
    for agenda_id, rank in pares:
        session.execute(
            text(
                "insert into parliamentarian_agenda "
                "(parliamentarian_id, agenda_id, rank) values (:p, :a, :r)"
            ),
            {"p": parliamentarian_id, "a": agenda_id, "r": rank},
        )
    session.commit()


@pytest.fixture()
def session() -> Session:
    s = _make_session()
    yield s
    s.close()


@pytest.fixture()
def client(session: Session) -> TestClient:
    main.app.dependency_overrides[get_db] = lambda: session
    main.app.dependency_overrides[verify_token] = lambda: None
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _listar(client: TestClient) -> dict[str, list[dict]]:
    resposta = client.get("/api/parliamentarians/")
    assert resposta.status_code == 200
    return {p["name"]: p["agendas"] for p in resposta.json()}


class TestListagem:
    def test_devolve_pautas_ordenadas_por_rank(
        self, client: TestClient, session: Session
    ) -> None:
        _semear_pautas(session)
        # Inserido fora de ordem de propósito: quem ordena é a query.
        _classificar(session, 1, (20, 2), (10, 1))

        agendas = _listar(client)["Fulano"]

        assert [a["slug"] for a in agendas] == ["meio-ambiente", "saude"]
        assert [a["rank"] for a in agendas] == [1, 2]
        assert agendas[0]["name"] == "Meio Ambiente"

    def test_parlamentar_sem_classificacao_devolve_lista_vazia(
        self, client: TestClient, session: Session
    ) -> None:
        """Sem evidência é lista vazia, nunca uma pauta genérica de escape."""

        _semear_pautas(session)
        _classificar(session, 1, (10, 1))

        assert _listar(client)["Beltrano"] == []

    def test_pauta_inativa_nao_aparece(
        self, client: TestClient, session: Session
    ) -> None:
        """Desativar no admin some da API sem apagar a classificação."""

        _semear_pautas(session)
        _classificar(session, 1, (10, 1), (30, 2))

        assert [a["slug"] for a in _listar(client)["Fulano"]] == ["meio-ambiente"]

        # A linha continua no banco, dormente.
        restantes = session.execute(
            text("select count(*) from parliamentarian_agenda where agenda_id = 30")
        ).scalar_one()
        assert restantes == 1

    def test_uma_query_para_a_lista_inteira(
        self, client: TestClient, session: Session
    ) -> None:
        """N+1 aqui seriam 500+ round-trips numa tela de seleção."""

        from sqlalchemy import event

        _semear_pautas(session)
        _classificar(session, 1, (10, 1))
        _classificar(session, 2, (20, 1))

        consultas: list[str] = []

        def _registrar(conn, cursor, statement, *args) -> None:
            if statement.lstrip().upper().startswith("SELECT") and (
                "parliamentarian_agenda" in statement
            ):
                consultas.append(statement)

        event.listen(session.get_bind(), "before_cursor_execute", _registrar)
        try:
            _listar(client)
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", _registrar)

        assert len(consultas) == 1


class TestDetalhe:
    def test_perfil_tambem_traz_as_pautas(
        self, client: TestClient, session: Session
    ) -> None:
        _semear_pautas(session)
        _classificar(session, 1, (10, 1))

        resposta = client.get("/api/parliamentarians/1")

        assert resposta.status_code == 200
        assert [a["slug"] for a in resposta.json()["agendas"]] == ["meio-ambiente"]


class TestJanelaDeDeploy:
    """O deploy aplica migrations DEPOIS de subir os containers."""

    def test_listagem_responde_200_sem_as_tabelas(self) -> None:
        sem_tabelas = _make_session(com_tabelas_de_pauta=False)
        main.app.dependency_overrides[get_db] = lambda: sem_tabelas
        main.app.dependency_overrides[verify_token] = lambda: None
        try:
            resposta = TestClient(main.app).get("/api/parliamentarians/")

            assert resposta.status_code == 200
            assert len(resposta.json()) == 2
            assert all(p["agendas"] == [] for p in resposta.json())
        finally:
            main.app.dependency_overrides.clear()
            sem_tabelas.close()

    def test_detalhe_responde_200_sem_as_tabelas(self) -> None:
        sem_tabelas = _make_session(com_tabelas_de_pauta=False)
        main.app.dependency_overrides[get_db] = lambda: sem_tabelas
        main.app.dependency_overrides[verify_token] = lambda: None
        try:
            resposta = TestClient(main.app).get("/api/parliamentarians/1")

            assert resposta.status_code == 200
            assert resposta.json()["agendas"] == []
        finally:
            main.app.dependency_overrides.clear()
            sem_tabelas.close()
