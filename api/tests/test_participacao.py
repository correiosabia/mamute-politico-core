"""Missões do dia: abas, placar, gate e curadoria (CS-179)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.db.base import Base
from api.db.models import AdminAuditLog, ParticipationItem
from api.dependencies import get_db
from api.feature_gate import FeatureAccess, missoes_access
from api.routers import participacao as rota
from api.security import require_ghost_admin, verify_token
from api.services import participacao as svc

HOJE = date(2026, 11, 26)


@compiles(JSONB, "sqlite")
def _jsonb_no_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_no_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "INTEGER"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[ParticipationItem.__table__, AdminAuditLog.__table__])
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield s
    s.close()


def _item(i: int, **kw) -> ParticipationItem:
    base = dict(
        id=i, source="brasil_participativo", external_id=str(i), kind="consulta", title=f"Item {i}",
        url=f"https://exemplo.gov.br/{i}", status="aprovado", source_open=True,
        first_seen_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        last_seen_at=datetime(2026, 11, 26, tzinfo=timezone.utc),
    )
    base.update(kw)
    return ParticipationItem(**base)


def _base(session: Session) -> None:
    placar = {"options": [{"label": "Sim", "count": 10}, {"label": "Não", "count": 990}]}
    session.add_all(
        [
            _item(1, closes_on=date(2026, 12, 20), theme="Saúde"),
            _item(2, closes_on=date(2026, 11, 30), theme="Educação"),
            # Sem prazo (consulta do Senado): fica em Hoje, depois dos que têm prazo.
            _item(3, source="senado_consulta", reference="PL 1/2026", closes_on=None, score=placar),
            # Prazo vencido: Já passou.
            _item(4, closes_on=date(2026, 11, 25), theme="Saúde"),
            # A fonte deixou de mostrar aberto: Já passou, mesmo sem prazo.
            _item(5, source="camara_enquete", closes_on=None, source_open=False,
                  closed_at=datetime(2026, 11, 20, tzinfo=timezone.utc)),
            # Fora da página: pendente e descartado.
            _item(6, status="pendente", closes_on=date(2026, 12, 1)),
            _item(7, status="descartado", closes_on=date(2026, 12, 1)),
            # Pendente encerrado: nem a curadoria precisa ver.
            _item(8, status="pendente", closes_on=date(2026, 11, 1)),
        ]
    )
    session.commit()


def test_abas_hoje_e_ja_passou(session):
    _base(session)
    hoje = svc.listar_publico(session, aba="hoje", hoje=HOJE)
    assert [i["id"] for i in hoje["items"]] == [2, 1, 3]
    assert hoje["items"][2]["sem_prazo"] is True and hoje["items"][2]["aberto"] is True
    assert hoje["temas"] == ["Educação", "Saúde"]
    passou = svc.listar_publico(session, aba="passou", hoje=HOJE)
    assert [i["id"] for i in passou["items"]] == [4, 5]
    assert all(i["aberto"] is False for i in passou["items"])
    assert passou["temas"] == ["Saúde"]


def test_filtro_por_tema(session):
    _base(session)
    r = svc.listar_publico(session, aba="hoje", tema="Saúde", hoje=HOJE)
    assert [i["id"] for i in r["items"]] == [1]


def test_placar_so_com_a_flag(session):
    _base(session)
    sem = svc.listar_publico(session, hoje=HOJE)
    assert all("score" not in i for i in sem["items"])
    com = svc.listar_publico(session, hoje=HOJE, com_placar=True)
    assert next(i for i in com["items"] if i["id"] == 3)["score"]["options"][1]["count"] == 990


@pytest.fixture()
def client(session: Session):
    main.app.dependency_overrides[get_db] = lambda: session
    main.app.dependency_overrides[verify_token] = lambda: {"sub": "leitor@x.com"}
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@mamute.com"
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_rota_completa_e_previa(client, session, monkeypatch):
    _base(session)
    monkeypatch.setattr(rota, "flag_liberada", lambda *a, **k: False)
    main.app.dependency_overrides[missoes_access] = lambda: FeatureAccess(full=True)
    r = client.get("/api/participacao", params={"aba": "passou"}, headers={"Authorization": "Ghost x"})
    assert r.status_code == 200 and r.json()["previa"] is False
    assert [i["id"] for i in r.json()["items"]] == [4, 5]

    # Prévia ignora aba e tema: no máximo 3 itens de Hoje, sem placar.
    main.app.dependency_overrides[missoes_access] = lambda: FeatureAccess(full=False)
    r = client.get("/api/participacao", params={"aba": "passou", "tema": "Saúde"}, headers={"Authorization": "Ghost x"})
    corpo = r.json()
    assert corpo["previa"] is True and len(corpo["items"]) == 3 and corpo["aba"] == "hoje"
    assert all("score" not in i for i in corpo["items"])


def test_rota_mostra_placar_com_a_flag(client, session, monkeypatch):
    _base(session)
    vistos = []
    monkeypatch.setattr(rota, "flag_liberada", lambda key, *a, **k: vistos.append(key) or True)
    main.app.dependency_overrides[missoes_access] = lambda: FeatureAccess(full=True)
    r = client.get("/api/participacao", headers={"Authorization": "Ghost x"})
    assert vistos == ["missoes_placar"]
    assert r.json()["com_placar"] is True
    assert next(i for i in r.json()["items"] if i["id"] == 3)["score"] is not None


def test_fila_da_curadoria(client, session):
    _base(session)
    r = client.get("/api/admin/participacao", params={"status": "pendente"})
    assert [i["id"] for i in r.json()["items"]] == [6]
    assert r.json()["contagem"] == {"pendente": 1, "aprovado": 5, "descartado": 1}


def test_aprovar_com_tema_e_auditoria(client, session):
    _base(session)
    r = client.patch("/api/admin/participacao/6", json={"status": "aprovado", "theme": " Meio  ambiente "})
    assert r.status_code == 200
    assert r.json()["status"] == "aprovado" and r.json()["theme"] == "Meio ambiente"
    assert r.json()["reviewed_by"] == "admin@mamute.com"
    log = session.execute(select(AdminAuditLog)).scalars().one()
    assert log.action == "participacao_revisar" and log.entity_id == "6"


def test_item_coletado_nao_edita_campo_da_fonte(client, session):
    _base(session)
    r = client.patch("/api/admin/participacao/6", json={"title": "Outro título"})
    assert r.status_code == 422
    assert "cadastrado à mão" in r.json()["detail"]
    assert session.get(ParticipationItem, 6).title == "Item 6"


def test_cadastro_manual_nasce_aprovado_e_encerra_pela_curadoria(client, session):
    r = client.post(
        "/api/admin/participacao",
        json={"title": "Abaixo-assinado X", "url": "https://www.change.org/p/x", "organization": "Change.org",
              "closes_on": None, "theme": "Cultura"},
    )
    assert r.status_code == 201
    novo = r.json()
    assert novo["status"] == "aprovado" and novo["manual"] is True and novo["kind"] == "peticao"
    assert novo["sem_prazo"] is True

    r = client.patch(f"/api/admin/participacao/{novo['id']}", json={"source_open": False})
    assert r.status_code == 200 and r.json()["aberto"] is False

    assert client.post("/api/admin/participacao", json={"title": "Y", "url": "change.org/p/y"}).status_code == 422


def test_lote(client, session):
    _base(session)
    r = client.post("/api/admin/participacao/lote", json={"ids": [6, 7, 1], "status": "descartado"})
    assert r.json() == {"alterados": 2}
    assert session.get(ParticipationItem, 6).status == "descartado"
