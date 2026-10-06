"""Coleções: viagens coincidentes pela cota e busca de registros dos membros (CS-132).

Mesmo arranjo de test_collections: SQLite in-memory com o schema dos models e
get_db/gate sobrescritos.
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
from api.db.models.authors_proposition import AuthorsProposition
from api.dependencies import get_db
from api.security import require_ghost_admin
from api.services import collection_travel
from api.services.collection_travel import destinos, mesmo_passageiro


@compiles(JSONB, "sqlite")
def _jsonb_no_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_no_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "INTEGER"


_TABELAS = [
    m.__table__
    for m in (
        AdminAuditLog, AuthorsProposition, Candidacy, CandidacyResult, Collection,
        CollectionMember, CollectionBlock, ElectoralHistory, Parliamentarian,
        ParliamentaryExpense, Proposition, RollCallVote, SpeechesTranscript,
    )
]

ANO = date.today().year


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=_TABELAS)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    collection_travel._cache.clear()
    yield s
    s.close()


@pytest.fixture()
def client(session: Session) -> TestClient:
    main.app.dependency_overrides[get_db] = lambda: session
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@mamute.com"
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _gasto(id_: int, parl: int, tipo: str, dia: date, detalhe: str = "", casa: str = "camara", **extra):
    return ParliamentaryExpense(
        id=id_, house=casa, source_key=str(id_), parliamentarian_id=parl, year=dia.year,
        month=dia.month, expense_type=tipo, document_date=dia, details=detalhe,
        net_value=extra.pop("valor", 900), **extra,
    )


def _colecao(client: TestClient, membros: list[dict], *, publicar: bool = False) -> int:
    criada = client.post("/api/admin/collections", json={"slug": "caso", "title": "Caso"}).json()
    client.put(f"/api/admin/collections/{criada['id']}/members", json={"members": membros})
    if publicar:
        client.put(f"/api/admin/collections/{criada['id']}", json={"status": "published"})
    return criada["id"]


def _base_viagens(session: Session) -> None:
    dia = date(ANO, 3, 10)
    passagem = "PASSAGEM AÉREA - SIGEPA"
    session.add_all(
        [
            Parliamentarian(id=1, type="Deputado", name="Ana Lima", full_name="ANA MARIA DE LIMA", state_elected="BA"),
            Parliamentarian(id=2, type="Deputado", name="Beto Reis", full_name="ROBERTO CARLOS REIS", state_elected="PB"),
            Parliamentarian(id=3, type="Deputado", name="Caio Paulista", full_name="CAIO DA SILVA", state_elected="SP"),
            Parliamentarian(id=4, type="Senador", name="Duda", full_name="Eduarda Souza", state_elected="RJ"),
            Parliamentarian(id=5, type="Senador", name="Edu", full_name="Eduardo Nunes", state_elected="PI"),
            # Ana e Beto vão a São Paulo no mesmo dia (bilhetes emitidos juntos).
            _gasto(1, 1, passagem, dia, "Passageiro: ANA M DE LIMA; Trecho: SSA/CGH"),
            _gasto(2, 2, passagem, dia, "Passageiro: ROBERTO C REIS; Trecho: BSB/GRU/BSB"),
            # Caio também vai a SP, mas é o estado dele: volta para casa, não conta.
            _gasto(3, 3, passagem, dia, "Passageiro: CAIO DA SILVA; Trecho: BSB/CGH"),
            # Bilhete de assessor da Ana: não conta.
            _gasto(4, 1, passagem, date(ANO, 4, 2), "Passageiro: JOSE ASSESSOR; Trecho: SSA/REC"),
            _gasto(5, 2, passagem, date(ANO, 4, 2), "Passageiro: ROBERTO REIS; Trecho: JPA/REC"),
            # Brasília nunca conta.
            _gasto(6, 1, passagem, date(ANO, 5, 5), "Passageiro: ANA LIMA; Trecho: SSA/BSB"),
            _gasto(7, 2, passagem, date(ANO, 5, 5), "Passageiro: ROBERTO REIS; Trecho: JPA/BSB"),
            # Senado: os dois senadores no mesmo voo, com a data do voo.
            _gasto(
                8, 4, "Passagens aéreas, aquáticas e terrestres nacionais", date(ANO, 6, 1),
                "Companhia Aérea: GOL. Passageiros: EDUARDA SOUZA (Matrícula 1, PARLAMENTAR), "
                f"Voo: 1717 - BSB CGH - 20/06/{str(ANO)[2:]};", casa="senado",
            ),
            _gasto(
                9, 5, "Passagens aéreas, aquáticas e terrestres nacionais", date(ANO, 6, 3),
                "Passageiros: EDUARDO NUNES (Matrícula 2, PARLAMENTAR), "
                f"Voo: 1717 - BSB CGH - 20/06/{str(ANO)[2:]}; FULANO (Matrícula 3, COMISSIONADO), "
                f"Voo: 1717 - BSB CGH - 20/06/{str(ANO)[2:]};", casa="senado",
            ),
            # Mesmo hotel (mesmo CNPJ), notas a 1 dia de distância.
            _gasto(10, 1, "HOSPEDAGEM ,EXCETO DO PARLAMENTAR NO DISTRITO FEDERAL.", date(ANO, 7, 1),
                   supplier_id="11.111.111/0001-11", supplier_name="HOTEL X"),
            _gasto(11, 3, "HOSPEDAGEM ,EXCETO DO PARLAMENTAR NO DISTRITO FEDERAL.", date(ANO, 7, 2),
                   supplier_id="11.111.111/0001-11", supplier_name="HOTEL X"),
            # Mesmo hotel, um mês depois: não é "junto".
            _gasto(12, 2, "HOSPEDAGEM ,EXCETO DO PARLAMENTAR NO DISTRITO FEDERAL.", date(ANO, 8, 20),
                   supplier_id="11.111.111/0001-11", supplier_name="HOTEL X"),
        ]
    )
    session.commit()


def test_regras_de_nome_e_destino():
    assert mesmo_passageiro("DIEGO H SILVA MARTINS", "DIEGO HENRIQUE SILVA MARTINS")
    assert mesmo_passageiro("Mario Frias", "MARIO LUIS FRIAS")
    assert not mesmo_passageiro("ALISSON PINHEIRO", "ELMAR JOSE NASCIMENTO")
    assert destinos(["BSB", "GRU", "DOU"]) == ["DOU"]
    assert destinos(["VCP", "BSB", "VCP"]) == ["BSB"]
    assert destinos(["CGH", ""]) == []


def test_viagens_coincidentes(client, session):
    _base_viagens(session)
    cid = _colecao(
        client,
        [
            {"display_name": "Ana", "parliamentarian_id": 1},
            {"display_name": "Beto", "parliamentarian_id": 2},
            {"display_name": "Caio", "parliamentarian_id": 3},
            {"display_name": "Duda", "parliamentarian_id": 4},
            {"display_name": "Edu", "parliamentarian_id": 5},
        ],
    )
    eventos = client.get(f"/api/admin/collections/{cid}/travel").json()["events"]
    chaves = {(e["kind"], e["date"], e["place"]) for e in eventos}

    sp = next(e for e in eventos if e["kind"] == "same_city" and e["date"] == f"{ANO}-03-10")
    assert sp["place"] == "São Paulo"
    assert sorted(p["display_name"] for p in sp["people"]) == ["Ana", "Beto"]
    assert {p["date_basis"] for p in sp["people"]} == {"issue"}

    voo = next(e for e in eventos if e["kind"] == "same_flight")
    assert voo["date"] == f"{ANO}-06-20" and "1717" in voo["detail"]
    assert sorted(p["display_name"] for p in voo["people"]) == ["Duda", "Edu"]
    assert {p["date_basis"] for p in voo["people"]} == {"flight"}

    hotel = [e for e in eventos if e["kind"] == "same_hotel"]
    assert len(hotel) == 1
    assert sorted(p["display_name"] for p in hotel[0]["people"]) == ["Ana", "Caio"]

    # Recife só teve a Ana pelo assessor; Brasília nunca entra.
    assert not any(place in ("Recife", "Brasília") for _, _, place in chaves)


def test_viagens_publicas_so_de_colecao_publicada(client, session):
    _base_viagens(session)
    membros = [{"display_name": "Ana", "parliamentarian_id": 1}, {"display_name": "Beto", "parliamentarian_id": 2}]
    cid = _colecao(client, membros)
    main.app.dependency_overrides.pop(require_ghost_admin)
    assert client.get("/api/collections/caso/travel").status_code == 404

    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@mamute.com"
    client.put(f"/api/admin/collections/{cid}", json={"status": "published"})
    main.app.dependency_overrides.pop(require_ghost_admin)
    resp = client.get("/api/collections/caso/travel")
    assert resp.status_code == 200
    assert resp.json()["events"][0]["place"] == "São Paulo"


def _base_busca(session: Session) -> None:
    session.add_all(
        [
            Parliamentarian(id=1, type="Deputado", name="Ana Lima"),
            Parliamentarian(id=2, type="Deputado", name="Beto Reis"),
            Parliamentarian(id=9, type="Deputado", name="Fora da coleção"),
            Proposition(id=50, proposition_acronym="PL", proposition_number=10, presentation_year=ANO,
                        title="PL 10", summary="Muda o limite do fundo garantidor de crédito.",
                        presentation_date=date(ANO, 2, 1)),
            Proposition(id=51, proposition_acronym="PL", proposition_number=11, presentation_year=ANO,
                        title="PL 11", summary="Outro assunto."),
            AuthorsProposition(id=1, parliamentarian_id=1, proposition_id=50),
            AuthorsProposition(id=2, parliamentarian_id=2, proposition_id=50),
            RollCallVote(id=60, parliamentarian_id=1, proposition_id=50, vote="Sim", vote_date=date(ANO, 3, 1)),
            RollCallVote(id=61, parliamentarian_id=2, proposition_id=50, vote="Não", vote_date=date(ANO, 3, 1)),
            RollCallVote(id=62, parliamentarian_id=9, proposition_id=50, vote="Sim", vote_date=date(ANO, 3, 1)),
            RollCallVote(id=63, parliamentarian_id=1, proposition_id=51, vote="Sim", vote_date=date(ANO, 3, 2)),
            SpeechesTranscript(id=70, parliamentarian_id=2, date=date(ANO, 4, 1), summary="Fala",
                               speech_text="Senhor presidente, o Fundo Garantidor precisa de regra."),
            SpeechesTranscript(id=71, parliamentarian_id=9, date=date(ANO, 4, 1),
                               speech_text="Fundo Garantidor também, mas não estou na coleção."),
        ]
    )
    session.commit()


def test_busca_registros_dos_membros(client, session):
    _base_busca(session)
    cid = _colecao(
        client,
        [{"display_name": "Ana", "parliamentarian_id": 1}, {"display_name": "Beto", "parliamentarian_id": 2}],
    )
    r = client.get(f"/api/admin/collections/{cid}/search", params={"q": "fundo garantidor"}).json()

    assert [d["ref_id"] for d in r["speeches"]] == [70]
    assert "Fundo Garantidor" in r["speeches"][0]["excerpt"]

    assert len(r["votes"]) == 1
    votacao = r["votes"][0]
    assert votacao["proposition"]["number"] == 10
    assert sorted(v["display_name"] for v in votacao["votes"]) == ["Ana", "Beto"]
    assert votacao["tally"] == {"Sim": 1, "Não": 1}

    assert [p["ref_id"] for p in r["propositions"]] == [50]
    assert sorted(a["display_name"] for a in r["propositions"][0]["authors"]) == ["Ana", "Beto"]


def test_busca_por_membro_e_termo_curto(client, session):
    _base_busca(session)
    cid = _colecao(
        client,
        [{"display_name": "Ana", "parliamentarian_id": 1}, {"display_name": "Beto", "parliamentarian_id": 2}],
    )
    ana = client.get(f"/api/admin/collections/{cid}").json()["members"][0]["id"]
    r = client.get(
        f"/api/admin/collections/{cid}/search",
        params={"q": "garantidor", "member_id": ana, "kinds": "speech,vote"},
    ).json()
    assert r["speeches"] == [] and r["propositions"] == []
    assert [v["display_name"] for v in r["votes"][0]["votes"]] == ["Ana"]

    resp = client.get(f"/api/admin/collections/{cid}/search", params={"q": "fu"})
    assert resp.status_code == 422 and "3 letras" in resp.json()["detail"]


def test_votacoes_do_mesmo_dia_ficam_separadas(client, session):
    _base_busca(session)
    session.add_all(
        [
            RollCallVote(id=64, parliamentarian_id=1, proposition_id=50, vote="Não",
                         vote_date=date(ANO, 3, 1), description="Votação da Emenda nº 2"),
            RollCallVote(id=65, parliamentarian_id=2, proposition_id=50, vote="Sim",
                         vote_date=date(ANO, 3, 1), description="Votação da Emenda nº 2"),
        ]
    )
    session.commit()
    cid = _colecao(
        client,
        [{"display_name": "Ana", "parliamentarian_id": 1}, {"display_name": "Beto", "parliamentarian_id": 2}],
    )
    r = client.get(f"/api/admin/collections/{cid}/search", params={"q": "garantidor", "kinds": "vote"}).json()
    assert len(r["votes"]) == 2
    emenda = next(g for g in r["votes"] if g["description"] == "Votação da Emenda nº 2")
    assert emenda["tally"] == {"Não": 1, "Sim": 1}
