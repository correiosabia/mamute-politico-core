"""Eventos anônimos da página Eleições 2026 e o painel no admin (CS-166)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.security import require_ghost_admin
from api.services import collection_events as ev
from api.services import page_events as pe

VISITANTE = "v-1234567890abcdef"
SESSAO = "s-1234567890abcdef"
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"


def _make_session(estado: str = "publica") -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table pagina_publicacao (chave text primary key, estado text, atualizado_em datetime, "
            "atualizado_por text)"
        )
        conn.exec_driver_sql("insert into pagina_publicacao (chave, estado) values ('eleicoes_2026', ?)", (estado,))
        conn.exec_driver_sql(
            """create table page_event (id integer primary key autoincrement, page text not null,
               created_at datetime default current_timestamp, visitor_id text, session_id text,
               event_type text not null, target text, detail text, duration_ms integer, device text,
               country text, region text, city text, referrer_host text, utm_source text,
               utm_medium text, utm_campaign text)"""
        )
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _client(session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    ev._limite.clear()
    monkeypatch.setattr(ev, "regiao_do_ip", lambda ip: ("BR", "Santa Catarina", "Florianópolis") if ip else None)
    main.app.dependency_overrides[get_db] = lambda: (yield session)
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@x.com"
    return TestClient(main.app)


@pytest.fixture()
def session() -> Session:
    return _make_session()


@pytest.fixture()
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    c = _client(session, monkeypatch)
    yield c
    main.app.dependency_overrides.clear()
    session.close()


def _lote(*eventos: dict, **extra) -> dict:
    return {"visitor": VISITANTE, "session": SESSAO, "events": list(eventos), **extra}


def _post(client: TestClient, lote: dict, ua: str = UA_IPHONE):
    return client.post(
        "/api/public/eleicoes-2026/events",
        json=lote,
        headers={"User-Agent": ua, "X-Forwarded-For": "200.1.2.3, 172.18.0.2"},
    )


def test_grava_visita_com_regiao_e_aparelho_sem_guardar_ip(client: TestClient, session: Session) -> None:
    resp = _post(client, _lote({"type": "visit"}, referrer="https://t.co/abc", utm={"source": "whatsapp"}))
    assert resp.status_code == 204
    linha = session.execute(
        text("select page, event_type, device, region, city, referrer_host, utm_source from page_event")
    ).one()
    assert tuple(linha) == ("eleicoes_2026", "visit", "mobile", "Santa Catarina", "Florianópolis", "t.co", "whatsapp")
    colunas = [c[1] for c in session.execute(text("pragma table_info(page_event)")).all()]
    assert not any("ip" == c or c.startswith("ip_") for c in colunas)


def test_descarta_tipo_desconhecido_e_robo(client: TestClient, session: Session) -> None:
    _post(client, _lote({"type": "visit"}, {"type": "hack"}))
    _post(client, _lote({"type": "visit"}), ua="Googlebot/2.1")
    tipos = session.execute(text("select event_type from page_event")).scalars().all()
    assert tipos == ["visit"]


def test_pagina_so_admin_nao_grava_nada(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _make_session("admin")
    client = _client(session, monkeypatch)
    try:
        assert _post(client, _lote({"type": "visit"})).status_code == 204
        assert session.execute(text("select count(*) from page_event")).scalar() == 0
    finally:
        main.app.dependency_overrides.clear()


def test_painel_soma_visitas_abas_compartilhamentos_e_cliques_no_mapa(client: TestClient) -> None:
    _post(
        client,
        _lote(
            {"type": "visit"},
            {"type": "tab_change", "target": "depois-das-17h"},
            {"type": "view_change", "target": "mapa"},
            {"type": "share", "target": "votos-17h", "detail": "whatsapp"},
            {"type": "share", "target": "votos-17h", "detail": "baixar"},
            {"type": "map_detail", "target": "local", "detail": "EEB X (SC)"},
        ),
    )
    _post(client, {**_lote({"type": "visit"}), "visitor": "v-outro-visitante-00", "session": "s-outra-sessao-0000"})
    resp = client.get("/api/admin/pages/eleicoes_2026/metricas?dias=7")
    assert resp.status_code == 200
    d = resp.json()
    assert d["kpis"] == {"visitas": 2, "visitantes": 2, "compartilhamentos": 2, "detalhes_no_mapa": 1}
    assert d["abas"] == [{"aba": "depois-das-17h", "trocas": 1}]
    assert d["visoes"] == [{"visao": "mapa", "trocas": 1}]
    assert {(c["peca"], c["meio"], c["total"]) for c in d["compartilhamentos"]} == {
        ("votos-17h", "whatsapp", 1),
        ("votos-17h", "baixar", 1),
    }
    assert d["detalhes_no_mapa"] == [{"tipo": "local", "lugar": "EEB X (SC)", "cliques": 1}]
    assert d["regioes"][0] == {"estado": "Santa Catarina", "pais": "BR", "visitas": 2}
    assert d["dispositivos"] == [{"aparelho": "mobile", "visitas": 2}]
    assert len(d["por_dia"]) == 8


def test_painel_de_pagina_desconhecida_e_404(client: TestClient) -> None:
    assert client.get("/api/admin/pages/outra/metricas").status_code == 404


def test_painel_exige_admin(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _client(session, monkeypatch)
    main.app.dependency_overrides.pop(require_ghost_admin)
    try:
        resp = TestClient(main.app).get("/api/admin/pages/eleicoes_2026/metricas")
        assert resp.status_code in (401, 403, 404)
    finally:
        main.app.dependency_overrides.clear()
    assert pe.PAGINAS_COM_METRICAS == {"eleicoes_2026"}
