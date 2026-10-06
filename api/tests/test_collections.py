"""Coleções curadas: leitura pública, escrita admin e resolução de vínculos (CS-132).

SQLite in-memory com o schema gerado dos próprios models (JSONB vira JSON só
no SQLite) e get_db/gate sobrescritos, como em test_editorial_agendas_admin.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.db.base import Base
from api.db.models import (
    AdminAuditLog,
    Candidacy,
    CandidacyResult,
    Collection,
    CollectionBlock,
    CollectionMember,
    ElectoralHistory,
    Parliamentarian,
    ParliamentaryExpense,
    Proposition,
    RollCallVote,
    SpeechesTranscript,
)
from api.dependencies import get_db
from api.security import require_ghost_admin


@compiles(JSONB, "sqlite")
def _jsonb_no_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "JSON"


# No SQLite só INTEGER PRIMARY KEY gera id sozinho; BIGSERIAL é coisa do Postgres.
@compiles(BigInteger, "sqlite")
def _bigint_no_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "INTEGER"


_TABELAS = [
    m.__table__
    for m in (
        AdminAuditLog,
        Candidacy,
        CandidacyResult,
        Collection,
        CollectionMember,
        CollectionBlock,
        ElectoralHistory,
        Parliamentarian,
        ParliamentaryExpense,
        Proposition,
        RollCallVote,
        SpeechesTranscript,
    )
]


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_TABELAS)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield s
    s.close()


@pytest.fixture()
def client(session: Session) -> TestClient:
    main.app.dependency_overrides[get_db] = lambda: session
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@mamute.com"
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


@pytest.fixture()
def public_client(session: Session) -> TestClient:
    """Sem sobrescrever o gate: prova que a leitura pública não exige login."""
    main.app.dependency_overrides[get_db] = lambda: session
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _base(session: Session) -> None:
    """Uma deputada em exercício, um ex-candidato eleito agora e um senador sem CPF."""
    session.add_all(
        [
            Parliamentarian(
                id=10, type="Deputado", name="Ana Exemplo", party="AAA",
                state_elected="SP", status="Exercício", cpf="11111111111",
                details={"urlFoto": "https://exemplo/ana.jpg"},
            ),
            Parliamentarian(
                id=20, type="Senador", name="Beto Exemplo", party="BBB",
                state_elected="RJ", status="Exercício", cpf=None,
            ),
            Candidacy(
                id=100, election_year=2022, tse_candidate_id=1, office="Deputado Federal",
                state="SP", cpf="11111111111", parliamentarian_id=10, match_status="matched",
            ),
            Candidacy(
                id=101, election_year=2026, tse_candidate_id=2, office="Senador",
                state="SP", cpf="11111111111", parliamentarian_id=10, match_status="matched",
                ballot_name="ANA", party="AAA",
            ),
            Candidacy(
                id=200, election_year=2026, tse_candidate_id=3, office="Deputado Estadual",
                state="MG", cpf="22222222222", parliamentarian_id=None,
                match_status="unmatched", ballot_name="CARLA",
            ),
            CandidacyResult(
                id=1, candidacy_id=200, turno=1, codigo_eleicao=1, situacao="ELEITO",
                eleito=True, votos=50000, totalizacao_final=True,
            ),
            ParliamentaryExpense(
                id=1, house="camara", source_key="a", parliamentarian_id=10,
                year=date.today().year, month=1,
                expense_type="LOCAÇÃO OU FRETAMENTO DE AERONAVES", net_value=1000,
            ),
            ParliamentaryExpense(
                id=2, house="camara", source_key="b", parliamentarian_id=10,
                year=date.today().year, month=2, expense_type="COMBUSTÍVEIS", net_value=250,
            ),
            ElectoralHistory(
                id=1, election_year=2018, tse_candidate_id=9, parliamentarian_id=10,
                office="Deputado Federal", declared_assets=100000,
            ),
            ElectoralHistory(
                id=2, election_year=2022, tse_candidate_id=1, parliamentarian_id=10,
                candidacy_id=100, office="Deputado Federal", declared_assets=230000,
            ),
            Proposition(id=7, proposition_acronym="PL", proposition_number=1, presentation_year=2025, title="Projeto"),
            RollCallVote(id=70, parliamentarian_id=10, proposition_id=7, vote="Sim", vote_date=date(2025, 5, 1)),
            SpeechesTranscript(id=80, parliamentarian_id=20, date=date(2025, 6, 1), summary="Discurso"),
        ]
    )
    session.commit()


def _cria(client: TestClient, **extra) -> dict:
    corpo = {"slug": "caso-exemplo", "title": "Caso exemplo", **extra}
    resp = client.post("/api/admin/collections", json=corpo)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_rascunho_nao_aparece_na_leitura_publica(client, public_client):
    _cria(client)

    assert public_client.get("/api/collections").json() == []
    assert public_client.get("/api/collections/caso-exemplo").status_code == 404
    assert len(client.get("/api/admin/collections").json()) == 1


def test_publicar_libera_sem_login_e_grava_data(client, public_client):
    criada = _cria(client)
    resp = client.put(f"/api/admin/collections/{criada['id']}", json={"status": "published"})
    assert resp.status_code == 200
    assert resp.json()["published_at"] is not None

    publica = public_client.get("/api/collections/caso-exemplo")
    assert publica.status_code == 200
    assert publica.json()["title"] == "Caso exemplo"


def test_slug_invalido_e_repetido_viram_erro_legivel(client):
    resp = client.post("/api/admin/collections", json={"slug": "Com Espaço", "title": "X"})
    assert resp.status_code == 422
    assert "letras minúsculas" in resp.json()["detail"]

    _cria(client)
    resp = client.post("/api/admin/collections", json={"slug": "caso-exemplo", "title": "Y"})
    assert resp.status_code == 422
    assert "Já existe" in resp.json()["detail"]


def test_vinculos_resolvidos_por_cpf_id_e_candidatura(client, session):
    _base(session)
    criada = _cria(client, tier_labels={"1": "Grupo A", "2": "Grupo B", "9": "fora"})
    resp = client.put(
        f"/api/admin/collections/{criada['id']}/members",
        json={
            "members": [
                # Só CPF: acha a candidatura mais recente (2026) e a deputada.
                {"display_name": "Ana", "cpf": "111.111.111-11", "tier": 1,
                 "sources": [{"label": "Jornal", "url": "https://exemplo/1"}]},
                # Senador sem CPF na base: vale o id informado.
                {"display_name": "Beto", "parliamentarian_id": 20, "tier": 2},
                # Candidata estadual eleita, ainda sem perfil de parlamentar.
                {"display_name": "Carla", "cpf": "22222222222", "tier": 2},
                # Fora da base.
                {"display_name": "Fulano", "role_label": "Ex-ministro"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    ana, beto, carla, fulano = resp.json()

    assert ana["parliamentarian"]["id"] == 10
    assert ana["candidacy"]["id"] == 101
    assert ana["sources"] == [{"label": "Jornal", "url": "https://exemplo/1"}]
    cota = ana["expenses"]["by_year"][0]
    assert cota["total"] == 1250.0 and cota["aircraft"] == 1000.0
    assert [a["year"] for a in ana["assets"]] == [2018, 2022]

    assert beto["parliamentarian"]["id"] == 20 and beto["candidacy"] is None

    assert carla["parliamentarian"] is None
    assert carla["candidacy"]["office"] == "Deputado Estadual"
    assert carla["candidacy"]["result"]["elected"] is True

    assert fulano["parliamentarian"] is None and fulano["candidacy"] is None
    assert fulano["role_label"] == "Ex-ministro"

    detalhe = client.get(f"/api/admin/collections/{criada['id']}").json()
    assert detalhe["tier_labels"] == {"1": "Grupo A", "2": "Grupo B"}


def test_candidata_que_vira_parlamentar_passa_a_apontar_para_o_perfil(client, session):
    _base(session)
    criada = _cria(client)
    client.put(
        f"/api/admin/collections/{criada['id']}/members",
        json={"members": [{"display_name": "Carla", "cpf": "22222222222"}]},
    )

    # Nova legislatura: a carga cria o parlamentar e liga a candidatura.
    session.add(Parliamentarian(id=30, type="Deputado", name="Carla", status="Exercício"))
    session.get(Candidacy, 200).parliamentarian_id = 30
    session.commit()

    membro = client.get(f"/api/admin/collections/{criada['id']}").json()["members"][0]
    assert membro["parliamentarian"]["id"] == 30


def test_membro_com_id_inexistente_ou_cpf_invalido_e_recusado(client, session):
    _base(session)
    criada = _cria(client)
    url = f"/api/admin/collections/{criada['id']}/members"

    resp = client.put(url, json={"members": [{"display_name": "X", "parliamentarian_id": 999}]})
    assert resp.status_code == 422 and "não existe" in resp.json()["detail"]

    resp = client.put(url, json={"members": [{"display_name": "X", "cpf": "123"}]})
    assert resp.status_code == 422 and "CPF inválido" in resp.json()["detail"]


def test_salvar_membros_substitui_a_lista_e_preserva_ids(client, session):
    _base(session)
    criada = _cria(client)
    url = f"/api/admin/collections/{criada['id']}/members"
    primeiro = client.put(
        url, json={"members": [{"display_name": "A"}, {"display_name": "B"}]}
    ).json()

    segundo = client.put(
        url, json={"members": [{"id": primeiro[1]["id"], "display_name": "B editado"}]}
    ).json()

    assert [m["display_name"] for m in segundo] == ["B editado"]
    assert segundo[0]["id"] == primeiro[1]["id"]
    assert segundo[0]["position"] == 0


def test_blocos_trazem_o_registro_de_origem(client, session):
    _base(session)
    criada = _cria(client)
    membros = client.put(
        f"/api/admin/collections/{criada['id']}/members",
        json={"members": [{"display_name": "Ana", "parliamentarian_id": 10}]},
    ).json()

    resp = client.put(
        f"/api/admin/collections/{criada['id']}/blocks",
        json={
            "blocks": [
                {"kind": "text", "title": "Contexto", "body": "Parágrafo."},
                {"kind": "vote", "ref_id": 70, "member_id": membros[0]["id"]},
                {"kind": "speech", "ref_id": 80},
                {"kind": "expense", "ref_id": 1},
                {"kind": "proposition", "ref_id": 404},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    texto, voto, discurso, gasto, sumida = resp.json()

    assert texto["ref"] is None and texto["body"] == "Parágrafo."
    assert voto["ref"]["vote"] == "Sim" and voto["ref"]["proposition"]["acronym"] == "PL"
    assert discurso["ref"]["summary"] == "Discurso"
    assert gasto["ref"]["value"] == 1000.0
    # Registro que não existe mais: o bloco fica, sem o cartão de origem.
    assert sumida["ref"] is None


def test_bloco_invalido_e_recusado(client, session):
    _base(session)
    criada = _cria(client)
    url = f"/api/admin/collections/{criada['id']}/blocks"

    resp = client.put(url, json={"blocks": [{"kind": "qualquer"}]})
    assert resp.status_code == 422

    resp = client.put(url, json={"blocks": [{"kind": "vote"}]})
    assert resp.status_code == 422 and "registro da base" in resp.json()["detail"]

    resp = client.put(url, json={"blocks": [{"kind": "text", "member_id": 999}]})
    assert resp.status_code == 422 and "não está na coleção" in resp.json()["detail"]


def test_remover_membro_solta_os_blocos_dele(client, session):
    _base(session)
    criada = _cria(client)
    base = f"/api/admin/collections/{criada['id']}"
    membro = client.put(f"{base}/members", json={"members": [{"display_name": "A"}]}).json()[0]
    client.put(f"{base}/blocks", json={"blocks": [{"kind": "text", "member_id": membro["id"]}]})

    client.put(f"{base}/members", json={"members": []})

    bloco = client.get(base).json()["blocks"][0]
    assert bloco["member_id"] is None


def test_apagar_colecao_registra_auditoria(client, session):
    criada = _cria(client)
    assert client.delete(f"/api/admin/collections/{criada['id']}").status_code == 204
    assert client.get(f"/api/admin/collections/{criada['id']}").status_code == 404
    acoes = [a.action for a in session.query(AdminAuditLog).all()]
    assert acoes == ["create_collection", "delete_collection"]


def test_admin_exige_gate(public_client):
    """Sem token de admin, o painel responde como se a rota não existisse."""
    resp = public_client.get("/api/admin/collections")
    assert resp.status_code in (401, 404, 422)


def test_salvar_sem_vinculo_no_corpo_preserva_o_vinculo(client, session):
    """A leitura não devolve o CPF: o editor salva só o texto e o vínculo fica."""
    _base(session)
    criada = _cria(client)
    url = f"/api/admin/collections/{criada['id']}/members"
    ana = client.put(url, json={"members": [{"display_name": "Ana", "cpf": "11111111111", "tier": 1}]}).json()[0]
    assert ana["parliamentarian"]["id"] == 10

    editada = client.put(
        url, json={"members": [{"id": ana["id"], "display_name": "Ana editada", "tier": 2}]}
    ).json()[0]
    assert editada["display_name"] == "Ana editada" and editada["tier"] == 2
    assert editada["parliamentarian"]["id"] == 10
    assert editada["candidacy"]["id"] == 101

    # Mandar o campo explicitamente vazio desfaz o vínculo.
    solta = client.put(
        url, json={"members": [{"id": ana["id"], "display_name": "Ana", "cpf": None}]}
    ).json()[0]
    assert solta["parliamentarian"] is None
