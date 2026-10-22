"""Eventos anônimos das páginas de coleção e a aba do painel (CS-145)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.security import require_ghost_admin
from api.services import collection_events as ev

VISITANTE = "v-1234567890abcdef"
SESSAO = "s-1234567890abcdef"


def _make_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table collection (id integer primary key, slug text, title text, status text, "
            "published_at datetime)"
        )
        conn.exec_driver_sql(
            "create table collection_member (id integer primary key, collection_id integer, "
            "display_name text)"
        )
        conn.exec_driver_sql(
            """create table collection_event (id integer primary key autoincrement,
               collection_id integer not null, created_at datetime default current_timestamp,
               visitor_id text, session_id text, event_type text not null, target text,
               detail text, duration_ms integer, device text, country text, region text,
               city text, referrer_host text, utm_source text, utm_medium text, utm_campaign text)"""
        )
        conn.exec_driver_sql(
            "insert into collection values (1,'caso-a','Caso A','published','2026-10-01'),"
            "(2,'rascunho','Rascunho','draft',null)"
        )
        conn.exec_driver_sql("insert into collection_member values (7,1,'Fulano'),(8,1,'Beltrana')")
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture()
def session() -> Session:
    return _make_session()


@pytest.fixture()
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    ev._limite.clear()
    monkeypatch.setattr(ev, "regiao_do_ip", lambda ip: ("BR", "São Paulo", "Campinas") if ip else None)
    main.app.dependency_overrides[get_db] = lambda: (yield session)
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@x.com"
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    session.close()


def _lote(*eventos: dict, **extra) -> dict:
    return {"visitor": VISITANTE, "session": SESSAO, "events": list(eventos), **extra}


UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"


def test_grava_visita_com_regiao_aparelho_e_origem_sem_guardar_ip(client: TestClient, session: Session) -> None:
    resp = client.post(
        "/api/collections/caso-a/events",
        json=_lote({"type": "visit"}, referrer="https://www.instagram.com/stories/x", utm={"source": "email"}),
        headers={"User-Agent": UA_IPHONE, "X-Forwarded-For": "200.1.2.3, 172.18.0.2"},
    )
    assert resp.status_code == 204
    linha = session.execute(
        text("select event_type, device, country, region, city, referrer_host, utm_source from collection_event")
    ).one()
    assert tuple(linha) == ("visit", "mobile", "BR", "São Paulo", "Campinas", "instagram.com", "email")
    colunas = session.execute(text("select * from collection_event")).keys()
    assert not any("ip" == c or c.endswith("_ip") for c in colunas)


def test_tipo_desconhecido_e_campos_longos_sao_descartados_ou_cortados(client: TestClient, session: Session) -> None:
    client.post(
        "/api/collections/caso-a/events",
        json=_lote({"type": "hack"}, {"type": "share", "target": "x" * 500, "detail": "whatsapp"}),
    )
    rows = session.execute(text("select event_type, length(target) from collection_event")).all()
    assert rows == [("share", 80)]


def test_robo_nao_grava(client: TestClient, session: Session) -> None:
    client.post(
        "/api/collections/caso-a/events",
        json=_lote({"type": "visit"}),
        headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"},
    )
    assert session.execute(text("select count(*) from collection_event")).scalar_one() == 0


def test_rascunho_e_caso_inexistente_nao_gravam(client: TestClient, session: Session) -> None:
    assert client.post("/api/collections/rascunho/events", json=_lote({"type": "visit"})).status_code == 204
    assert client.post("/api/collections/nao-existe/events", json=_lote({"type": "visit"})).status_code == 204
    assert session.execute(text("select count(*) from collection_event")).scalar_one() == 0


def test_id_de_visitante_invalido_e_recusado(client: TestClient) -> None:
    resp = client.post("/api/collections/caso-a/events", json={"visitor": "<script>", "session": SESSAO, "events": []})
    assert resp.status_code == 422


def test_limite_por_ip(client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ev, "LIMITE_POR_MINUTO", 3)
    for _ in range(3):
        client.post("/api/collections/caso-a/events", json=_lote({"type": "visit"}, {"type": "back_to_top"}))
    assert session.execute(text("select count(*) from collection_event")).scalar_one() == 3


def test_tempo_da_secao_tem_teto() -> None:
    assert ev._duracao(10 * 60 * 60 * 1000) == ev.TETO_DURACAO_MS
    assert ev._duracao(-5) is None


@pytest.mark.parametrize(
    "xff,peer,esperado",
    [
        ("200.1.2.3, 172.18.0.2", "172.18.0.5", "200.1.2.3"),
        # O cliente pode forjar o começo da lista: vale o último IP público.
        ("1.1.1.1, 200.1.2.3, 10.0.0.1", "172.18.0.5", "200.1.2.3"),
        (None, "200.9.9.9", "200.9.9.9"),
        ("lixo", "172.18.0.5", None),
    ],
)
def test_ip_do_cliente(xff: str | None, peer: str, esperado: str | None) -> None:
    assert ev.ip_do_cliente(xff, peer) == esperado


def _evento(session: Session, tipo: str, *, visitante=VISITANTE, sessao=SESSAO, quando=None, **campos) -> None:
    quando = quando or datetime.now(timezone.utc)
    session.execute(
        text(
            "insert into collection_event (collection_id, created_at, visitor_id, session_id, event_type, "
            "target, detail, duration_ms, device, country, region, city, referrer_host, utm_source) "
            "values (1, :q, :v, :s, :t, :target, :detail, :ms, :device, 'BR', :region, :city, :ref, :utm)"
        ),
        {
            "q": quando, "v": visitante, "s": sessao, "t": tipo,
            "target": campos.get("target"), "detail": campos.get("detail"), "ms": campos.get("ms"),
            "device": campos.get("device", "desktop"), "region": campos.get("region", "São Paulo"),
            "city": campos.get("city", "São Paulo"), "ref": campos.get("ref"), "utm": campos.get("utm"),
        },
    )
    session.commit()


def test_painel_resume_o_caso(client: TestClient, session: Session) -> None:
    _evento(session, "visit", ref="instagram.com", device="mobile")
    _evento(session, "visit", sessao="s-outra-sessao-0001", utm="email")
    _evento(session, "visit", visitante="v-outro-visitante-01", sessao="s-terceira-sessao-01", region="Bahia", city="Salvador")
    _evento(session, "section_time", target="topo", ms=30_000)
    _evento(session, "section_time", target="topo", ms=10_000, sessao="s-outra-sessao-0001")
    _evento(session, "card_click", target="7")
    _evento(session, "details_open", target="7")
    _evento(session, "follow_click", target="7")
    _evento(session, "share", target="caso", detail="whatsapp")
    _evento(session, "share", target="7", detail="instagram")
    _evento(session, "view_change", target="citados")
    _evento(session, "reduce_motion", detail="on")
    _evento(session, "back_to_top")
    _evento(session, "email_signup")
    # fora do período
    _evento(session, "visit", quando=datetime.now(timezone.utc) - timedelta(days=40), sessao="s-antiga-sessao-001")

    r = client.get("/api/admin/metrics/collections/caso-a?dias=30")
    assert r.status_code == 200
    d = r.json()
    assert d["kpis"]["visitas"] == 3
    assert d["kpis"]["visitantes"] == 2
    assert d["kpis"]["cadastros_email"] == 1
    assert d["kpis"]["compartilhamentos"] == 2
    assert d["kpis"]["tempo_medio_s"] == 13  # 40 s em 3 visitas
    assert {"estado": "São Paulo", "visitas": 2} in [{k: x[k] for k in ("estado", "visitas")} for x in d["regioes"]]
    assert {o["origem"]: o["visitas"] for o in d["origens"]} == {"instagram.com": 1, "email": 1, "direto": 1}
    assert d["dispositivos"] == [{"aparelho": "desktop", "visitas": 2}, {"aparelho": "mobile", "visitas": 1}]
    assert d["secoes"] == [{"secao": "topo", "visitas": 2, "tempo_medio_s": 20}]
    fulano = d["politicos"][0]
    assert (fulano["nome"], fulano["cliques"], fulano["ver_dados"], fulano["acompanhar"], fulano["compartilhamentos"]) == (
        "Fulano", 1, 1, 1, 1,
    )
    assert {c["meio"]: (c["do_caso"], c["de_cards"]) for c in d["compartilhamentos"]} == {
        "whatsapp": (1, 0), "instagram": (0, 1),
    }
    assert d["visoes"] == [{"visao": "citados", "trocas": 1}]
    assert d["outros"] == {"reduzir_animacoes": 1, "voltar_ao_topo": 1}
    assert sum(p["visitas"] for p in d["por_dia"]) == 3


def test_lista_de_casos_para_as_subabas(client: TestClient) -> None:
    r = client.get("/api/admin/metrics/collections")
    assert r.status_code == 200
    assert [c["slug"] for c in r.json()] == ["caso-a"]
