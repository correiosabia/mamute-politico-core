"""Rotas de busca de candidaturas (CS-62).

SQLite in-memory com DDL cru e get_db sobrescrito — mesmo padrão de
test_electoral_history.py.

`unaccent_imutavel` é função do Postgres (migration d0e1f2a3b4c5). Aqui ela é
registrada como UDF equivalente na conexão SQLite, para o SQL emitido ser o
MESMO nos dois bancos — sem ramificar por dialeto no código de produção.
A UDF dobra por NFKD, que cobre acento latino igual ao `unaccent`; os casos em
que os dois divergem (ß→ss, Æ→AE) não são representáveis em SQLite de todo
jeito e valem só em Postgres.
"""
from __future__ import annotations

import unicodedata

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.security import verify_token


def _dobra_acento(valor: str | None) -> str | None:
    """Equivalente SQLite de `public.unaccent_imutavel` (NFKD sem diacrítico)."""
    if valor is None:
        return None
    decomposto = unicodedata.normalize("NFKD", valor)
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def _make_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _registra_udf(dbapi_conn, _record):  # noqa: ANN001
        dbapi_conn.create_function("unaccent_imutavel", 1, _dobra_acento)

    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            create table candidacy (
                id integer primary key,
                election_year integer not null,
                tse_candidate_id integer not null,
                office_code integer, office text, state text,
                ballot_number integer, ballot_name text, full_name text,
                party text, coalition text, status text,
                totalization_status text, cpf text, voter_id text,
                photo_url text, tse_last_update datetime,
                listing_fingerprint text, parliamentarian_id integer,
                match_status text not null, details text,
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp
            )
            """
        )
        conn.exec_driver_sql(
            """
            insert into candidacy
                (id, election_year, tse_candidate_id, office_code, office,
                 state, ballot_number, ballot_name, full_name, party,
                 parliamentarian_id, match_status)
            values
                (1, 2026, 1001, 5, 'Senador', 'CE', 123, 'LUCIANA FERREIRA',
                 'LUCIANA FERREIRA DA SILVA', 'PDT', 77, 'matched_cpf'),
                (2, 2026, 1002, 6, 'Deputado Federal', 'CE', 2222,
                 'JOÃO DO CEARÁ', 'JOÃO PEREIRA GONÇALVES', 'PT', null, 'unmatched'),
                (3, 2026, 1003, 5, 'Senador', 'SP', 456, 'ANA PAULA',
                 'ANA PAULA SOUZA', 'PSDB', null, 'unmatched'),
                (4, 2022, 1004, 5, 'Senador', 'CE', 789, 'LUCIANA ANTIGA',
                 'LUCIANA ANTIGA', 'PDT', null, 'unmatched'),
                (5, 2026, 1005, 1, null, 'BR', 10, 'CANDIDATA BR',
                 'CANDIDATA BR', 'NOVO', null, 'unmatched')
            """
        )
        conn.exec_driver_sql(
            """
            create table candidacy_result (
                id integer primary key, candidacy_id integer not null,
                turno integer not null, codigo_eleicao integer not null,
                situacao text, eleito boolean, votos integer, percentual numeric,
                destinacao_voto text,
                totalizacao_final boolean not null default 0,
                tse_atualizado_em datetime, coletado_em datetime,
                unique (candidacy_id, turno)
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
    main.app.dependency_overrides[verify_token] = lambda: None
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_lista_somente_a_eleicao_pedida_ordenada_por_nome(client):
    resp = client.get("/api/candidacies/")
    assert resp.status_code == 200
    body = resp.json()
    # 2022 fica fora: o default é a eleição de 2026.
    assert [c["ballot_name"] for c in body] == [
        "ANA PAULA",
        "CANDIDATA BR",
        "JOÃO DO CEARÁ",
        "LUCIANA FERREIRA",
    ]


def test_busca_por_nome_casa_urna_e_nome_completo(client):
    por_urna = client.get("/api/candidacies/", params={"name": "luciana"}).json()
    assert [c["id"] for c in por_urna] == [1]

    # "PEREIRA" só existe em full_name — a busca tem de alcançar as duas colunas.
    por_completo = client.get("/api/candidacies/", params={"name": "pereira"}).json()
    assert [c["id"] for c in por_completo] == [2]


def test_busca_por_nome_ignora_espaco_nas_pontas(client):
    resp = client.get("/api/candidacies/", params={"name": "  luciana  "})
    assert [c["id"] for c in resp.json()] == [1]


def test_filtro_de_estado_e_case_insensitive(client):
    resp = client.get("/api/candidacies/", params={"state": "ce"})
    assert sorted(c["id"] for c in resp.json()) == [1, 2]


def test_filtro_de_cargo_por_codigo(client):
    resp = client.get("/api/candidacies/", params={"office_code": 5})
    assert sorted(c["id"] for c in resp.json()) == [1, 3]


def test_filtros_combinados(client):
    resp = client.get(
        "/api/candidacies/", params={"state": "CE", "office_code": 5, "name": "luciana"}
    )
    assert [c["id"] for c in resp.json()] == [1]


def test_election_year_explicito_alcanca_eleicao_antiga(client):
    resp = client.get("/api/candidacies/", params={"election_year": 2022})
    assert [c["id"] for c in resp.json()] == [4]


def test_paginacao_nao_repete_nem_perde_linha(client):
    primeira = client.get("/api/candidacies/", params={"limit": 2, "offset": 0}).json()
    segunda = client.get("/api/candidacies/", params={"limit": 2, "offset": 2}).json()
    ids = [c["id"] for c in primeira] + [c["id"] for c in segunda]
    assert len(set(ids)) == 4


def test_payload_expoe_o_que_a_tela_precisa(client):
    candidatura = client.get("/api/candidacies/", params={"name": "luciana"}).json()[0]
    assert candidatura["office"] == "Senador"
    assert candidatura["state"] == "CE"
    assert candidatura["party"] == "PDT"
    assert candidatura["ballot_name"] == "LUCIANA FERREIRA"
    # A tela usa isto para decidir se o "+" pode monitorar a candidatura.
    assert candidatura["parliamentarian_id"] == 77
    assert candidatura["match_status"] == "matched_cpf"


def test_office_ausente_cai_no_rotulo_do_codigo(client):
    # Linha 5 tem office_code=1 e office nulo no banco.
    br = client.get("/api/candidacies/", params={"state": "BR"}).json()[0]
    assert br["office"] == "Presidente"


def test_nome_com_um_caractere_e_rejeitado(client):
    # min_length=2 evita varredura da base inteira por um "a".
    assert client.get("/api/candidacies/", params={"name": "a"}).status_code == 422


def test_filters_devolve_so_o_que_existe_na_base(client):
    body = client.get("/api/candidacies/filters").json()
    assert body["election_years"] == [2026, 2022]
    assert body["states"] == ["BR", "CE", "SP"]
    assert body["offices"] == [
        {"code": 1, "name": "Presidente"},
        {"code": 5, "name": "Senador"},
        {"code": 6, "name": "Deputado Federal"},
    ]


def test_busca_sem_acento_encontra_nome_com_acento(client):
    # O caso que motivou a migration d0e1f2a3b4c5: brasileiro digita sem acento.
    resp = client.get("/api/candidacies/", params={"name": "joao"})
    assert [c["id"] for c in resp.json()] == [2]

    resp = client.get("/api/candidacies/", params={"name": "ceara"})
    assert [c["id"] for c in resp.json()] == [2]


def test_busca_com_acento_continua_encontrando(client):
    # Dobrar os dois lados não pode quebrar quem digita corretamente.
    resp = client.get("/api/candidacies/", params={"name": "joão"})
    assert [c["id"] for c in resp.json()] == [2]

    resp = client.get("/api/candidacies/", params={"name": "cearÁ"})
    assert [c["id"] for c in resp.json()] == [2]


def test_busca_sem_acento_alcanca_o_nome_completo(client):
    # "GONÇALVES" só existe em full_name — as duas colunas são dobradas.
    resp = client.get("/api/candidacies/", params={"name": "goncalves"})
    assert [c["id"] for c in resp.json()] == [2]


def _resultado(session, candidacy_id, turno, situacao, eleito, final=True):
    from sqlalchemy import text

    session.execute(
        text(
            "insert into candidacy_result (candidacy_id, turno, codigo_eleicao, situacao, "
            "eleito, totalizacao_final) values (:c, :t, 6259, :s, :e, :f)"
        ),
        {"c": candidacy_id, "t": turno, "s": situacao, "e": eleito, "f": final},
    )
    session.commit()


def test_cs108_sem_resultado_vem_nulo(client):
    body = client.get("/api/candidacies/").json()
    assert all(c["resultado"] is None for c in body)


def test_cs108_resultado_do_ultimo_turno_encerrado(client, session):
    _resultado(session, 5, 1, "2º turno", True)
    _resultado(session, 5, 2, "Não eleito", False)
    _resultado(session, 1, 1, "Eleito", True)
    _resultado(session, 3, 1, "Não eleito", False, final=False)  # parcial: nao mostra
    body = {c["id"]: c["resultado"] for c in client.get("/api/candidacies/").json()}
    assert body[5] == {"turno": 2, "situacao": "Não eleito", "eleito": False, "fora_da_disputa": True}
    assert body[1]["situacao"] == "Eleito" and body[1]["fora_da_disputa"] is False
    assert body[3] is None


def test_cs108_filtro_segundo_turno(client, session):
    _resultado(session, 5, 1, "2º turno", True)
    _resultado(session, 1, 1, "Eleito", True)
    resp = client.get("/api/candidacies/", params={"resultado": "segundo_turno"})
    assert [c["id"] for c in resp.json()] == [5]
