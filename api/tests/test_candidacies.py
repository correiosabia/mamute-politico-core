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

import json
import unicodedata

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
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
                 'CANDIDATA BR', 'NOVO', null, 'unmatched'),
                (6, 2022, 1006, 2, 'VICE-PRESIDENTE', 'BR', 22, 'VICE ANTIGO',
                 'VICE ANTIGO', 'PSB', null, 'unmatched')
            """
        )
        # Chapa no formato do detalhe da DivulgaCandContas: o vice só existe
        # dentro do payload do titular. O item sem nome tem de ser ignorado.
        conn.execute(
            text("update candidacy set details = :d where id = 5"),
            {
                "d": json.dumps(
                    {
                        "vices": [
                            {
                                "nm_URNA": "VICE DA CHAPA",
                                "nm_CANDIDATO": "VICE DA CHAPA COMPLETO",
                                "ds_CARGO": "Vice-presidente",
                                "sg_PARTIDO": "NOVO",
                            },
                            {"ds_CARGO": "Vice-presidente"},
                        ]
                    }
                )
            },
        )
        conn.exec_driver_sql(
            """
            create table projetos (
                id integer primary key, nome text not null, cliente text,
                email text not null, tier_id integer, tag_ghost text,
                qtd_termos integer not null default 0,
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp,
                deleted_at datetime
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table projetos_candidacy (
                id integer primary key, projeto_id integer not null,
                candidacy_id integer not null,
                created_at datetime not null default current_timestamp,
                unique (projeto_id, candidacy_id)
            )
            """
        )
        conn.exec_driver_sql(
            """
            insert into projetos (id, nome, email) values
                (10, 'Projeto 10', 'assinante@example.com'),
                (20, 'Projeto 20', 'outro@example.com')
            """
        )
        # Assinante acompanha 1 (CE/Senado/PDT) e 3 (SP/Senado/PSDB); o outro
        # projeto acompanha 2, que não pode vazar para o assinante.
        conn.exec_driver_sql(
            """
            insert into projetos_candidacy (projeto_id, candidacy_id) values
                (10, 1), (10, 3), (20, 2)
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
                percentual_apurado numeric,
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


@pytest.fixture()
def client_logado(session: Session) -> TestClient:
    """Cliente com e-mail no token, como o verify_token real deixa no request."""

    def fake_verify_token(request: Request) -> dict[str, str]:
        request.state.token_email = "assinante@example.com"
        return {"sub": "assinante@example.com"}

    main.app.dependency_overrides[get_db] = lambda: session
    main.app.dependency_overrides[verify_token] = fake_verify_token
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
    assert sorted(c["id"] for c in resp.json()) == [4, 6]


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
    assert body["parties"] == ["NOVO", "PDT", "PSDB", "PT"]


def test_filters_recorta_pela_eleicao_pedida(client):
    # CS-113: o VICE-PRESIDENTE de 2022 (linha própria do CSV) não pode
    # aparecer no dropdown de 2026, onde devolveria lista vazia.
    padrao = client.get("/api/candidacies/filters").json()
    assert 2 not in [o["code"] for o in padrao["offices"]]
    assert "PSB" not in padrao["parties"]

    antiga = client.get("/api/candidacies/filters", params={"election_year": 2022}).json()
    # A lista de anos não é recortada: é ela que permite trocar de eleição.
    assert antiga["election_years"] == [2026, 2022]
    assert antiga["states"] == ["BR", "CE"]
    assert antiga["offices"] == [
        {"code": 2, "name": "VICE-PRESIDENTE"},
        {"code": 5, "name": "Senador"},
    ]
    assert antiga["parties"] == ["PDT", "PSB"]


def test_filtro_de_partido(client):
    resp = client.get("/api/candidacies/", params={"party": "PDT"})
    # O PDT de 2022 (id 4) fica fora: o default é a eleição de 2026.
    assert [c["id"] for c in resp.json()] == [1]


def test_partido_combina_com_uf_e_cargo(client):
    resp = client.get(
        "/api/candidacies/", params={"party": "PDT", "state": "CE", "office_code": 5}
    )
    assert [c["id"] for c in resp.json()] == [1]

    resp = client.get(
        "/api/candidacies/", params={"party": "PDT", "state": "SP", "office_code": 5}
    )
    assert resp.json() == []


def test_chapa_expoe_vices_do_detalhe(client):
    br = client.get("/api/candidacies/", params={"state": "BR"}).json()[0]
    assert br["vices"] == [
        {"name": "VICE DA CHAPA", "role": "Vice-presidente", "party": "NOVO"}
    ]


def test_candidatura_sem_detalhe_tem_lista_de_vices_vazia(client):
    senado = client.get("/api/candidacies/", params={"name": "luciana"}).json()[0]
    assert senado["vices"] == []


def test_only_followed_devolve_so_as_acompanhadas_do_token(client_logado):
    resp = client_logado.get("/api/candidacies/", params={"only_followed": True})
    assert resp.status_code == 200
    # 2 é acompanhada pelo OUTRO projeto e não pode aparecer.
    assert sorted(c["id"] for c in resp.json()) == [1, 3]


def test_only_followed_combina_com_os_filtros(client_logado):
    resp = client_logado.get(
        "/api/candidacies/",
        params={"only_followed": True, "state": "SP", "office_code": 5, "party": "PSDB"},
    )
    assert [c["id"] for c in resp.json()] == [3]

    resp = client_logado.get(
        "/api/candidacies/", params={"only_followed": True, "party": "PT"}
    )
    assert resp.json() == []


def test_only_followed_sem_email_no_token_e_401(client):
    # O fixture `client` não grava token_email: sem dono não há "acompanhadas".
    resp = client.get("/api/candidacies/", params={"only_followed": True})
    assert resp.status_code == 401


def test_only_followed_falso_nao_exige_projeto(client):
    resp = client.get("/api/candidacies/", params={"only_followed": False})
    assert resp.status_code == 200
    assert len(resp.json()) == 4


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


def test_cs127_apuracao_parcial_aparece_antes_de_encerrar(client, session):
    from sqlalchemy import text

    session.execute(
        text(
            "insert into candidacy_result (candidacy_id, turno, codigo_eleicao, votos, "
            "percentual, totalizacao_final, percentual_apurado) "
            "values (3, 1, 6259, 123456, 12.34, 0, 47.26)"
        )
    )
    session.commit()
    body = {c["id"]: c for c in client.get("/api/candidacies/").json()}
    assert body[3]["resultado"] is None  # situacao oficial so com a totalizacao encerrada
    apuracao = body[3]["apuracao"]
    assert apuracao["votos"] == 123456
    assert apuracao["percentual"] == 12.34
    assert apuracao["percentual_apurado"] == 47.26
    assert apuracao["totalizacao_final"] is False
    assert body[1]["apuracao"] is None


def test_cs127_apuracao_mostra_o_turno_mais_recente(client, session):
    _resultado(session, 5, 1, "2º turno", True)
    _resultado(session, 5, 2, None, None, final=False)
    body = {c["id"]: c for c in client.get("/api/candidacies/").json()}
    assert body[5]["apuracao"]["turno"] == 2
    assert body[5]["resultado"]["turno"] == 1
