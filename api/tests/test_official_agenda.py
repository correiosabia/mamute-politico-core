"""Agenda oficial de autoridades: busca por termo e rotas da coleção (CS-135)."""
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
from api.db.models import AdminAuditLog, Collection, CollectionBlock, CollectionMember, OfficialAgendaItem
from api.dependencies import get_db
from api.security import require_ghost_admin
from api.services.official_agenda import AgendaError, search_agenda


@compiles(JSONB, "sqlite")
def _jsonb_no_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_no_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "INTEGER"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[m.__table__ for m in (AdminAuditLog, Collection, CollectionMember, CollectionBlock, OfficialAgendaItem)],
    )
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield s
    s.close()


@pytest.fixture()
def client(session: Session) -> TestClient:
    main.app.dependency_overrides[get_db] = lambda: session
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@mamute.com"
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _item(i: int, autoridade: str, office: str, dia: date, texto: str, hora: str = "11:00", **extra):
    return OfficialAgendaItem(
        id=i, source="bcb", authority_id=autoridade, authority_name=f"Pessoa {autoridade}", office=office,
        office_label=f"Cargo {office}", event_date=dia, seq=extra.pop("seq", 0), starts_at=hora,
        description=texto, place=extra.pop("place", "Brasília"), remote=extra.pop("remote", False), **extra,
    )


def _base(session: Session) -> None:
    reuniao = "Audiência com Fulano Exemplar, Presidente do Banco X, em Brasília."
    session.add_all(
        [
            # A mesma reunião na agenda de três autoridades.
            _item(1, "1", "Presi", date(2025, 4, 1), reuniao),
            _item(2, "2", "Difis", date(2025, 4, 1), reuniao),
            _item(3, "3", "Dinor", date(2025, 4, 1), "A udiência com Fulano Exemplar, do Banco X, em Brasília."),
            # Outro horário no mesmo dia: outra reunião.
            _item(4, "2", "Difis", date(2025, 4, 1), "Reunião por videoconferência com Fulano Exemplar.",
                  hora="18:00", seq=1, place=None, remote=True),
            _item(5, "1", "Presi", date(2024, 2, 7), "Reunião com o Banco X, em São Paulo.", place="São Paulo"),
            _item(6, "1", "Presi", date(2024, 2, 8), "Reunião com outra empresa, em São Paulo.", place="São Paulo"),
        ]
    )
    session.commit()


def test_busca_agrupa_mesma_reuniao(session):
    _base(session)
    r = search_agenda(session, ["Fulano Exemplar"])
    assert [i["date"] for i in r["items"]] == ["2025-04-01", "2025-04-01"]
    manha = next(i for i in r["items"] if i["starts_at"] == "11:00")
    assert sorted(a["office"] for a in manha["authorities"]) == ["Difis", "Dinor", "Presi"]
    noite = next(i for i in r["items"] if i["starts_at"] == "18:00")
    assert noite["remote"] is True and len(noite["authorities"]) == 1
    assert r["sources"][0]["label"] == "Agenda da diretoria do Banco Central"


def test_busca_varios_termos_e_validacao(session):
    _base(session)
    r = search_agenda(session, ["Fulano Exemplar", "Banco X", "banco x"])
    assert r["terms"] == ["Fulano Exemplar", "Banco X"]
    assert {i["date"] for i in r["items"]} == {"2025-04-01", "2024-02-07"}
    sp = next(i for i in r["items"] if i["date"] == "2024-02-07")
    assert sp["matched"] == ["Banco X"]
    assert search_agenda(session, [])["items"] == []
    with pytest.raises(AgendaError):
        search_agenda(session, ["abc"])


def test_rotas_da_colecao(client, session):
    _base(session)
    cid = client.post("/api/admin/collections", json={"slug": "caso", "title": "Caso"}).json()["id"]
    # Rascunho: a rota pública não abre.
    main.app.dependency_overrides.pop(require_ghost_admin)
    assert client.get("/api/collections/caso/agenda").status_code == 404
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@mamute.com"

    # Sem termos salvos: vazio. Termo avulso do admin funciona.
    assert client.get(f"/api/admin/collections/{cid}/agenda").json()["items"] == []
    assert len(client.get(f"/api/admin/collections/{cid}/agenda", params={"q": "Banco X"}).json()["items"]) == 2
    assert client.get(f"/api/admin/collections/{cid}/agenda", params={"q": "ab"}).status_code == 400

    client.put(f"/api/admin/collections/{cid}", json={"status": "published", "settings": {"agenda_terms": ["Fulano Exemplar"]}})
    main.app.dependency_overrides.pop(require_ghost_admin)
    publico = client.get("/api/collections/caso/agenda").json()
    assert publico["terms"] == ["Fulano Exemplar"] and len(publico["items"]) == 2
