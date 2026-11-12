"""CS-87: ativos agora, mapa de origem dos acessos e quadro de horários no admin."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.security import require_ghost_admin, verify_token
from api.services import acessos
from api.services import collection_events as ev

AGORA = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)  # quinta, 12h em Brasília
IP = "200.150.100.50"
FLORIPA = ("BR", "Santa Catarina", "Florianópolis", -27.6, -48.55)


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table projetos (id integer primary key, nome text, email text, tier_id integer, deleted_at datetime)"
        )
        conn.exec_driver_sql("insert into projetos (id, nome, email) values (1, 'Ana', 'ana@x.com')")
        conn.exec_driver_sql(
            """create table usage_events (id integer primary key, projeto_id integer, email text,
               event_type text not null, page text, section text, parliamentarian_id integer,
               country text, region text, city text, lat real, lon real,
               created_at datetime default current_timestamp)"""
        )
        conn.exec_driver_sql(
            """create table member_presence (email text primary key, projeto_id integer,
               last_seen_at datetime not null, country text, region text, city text, lat real, lon real)"""
        )
        conn.exec_driver_sql(
            "create table member_presence_hour (hora datetime not null, email text not null, primary key (hora, email))"
        )
        for tabela in ("collection_event", "page_event"):
            conn.exec_driver_sql(
                f"""create table {tabela} (id integer primary key autoincrement, created_at datetime,
                   visitor_id text, country text, region text, city text, lat real, lon real)"""
            )
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture()
def session() -> Session:
    s = _make_session()
    yield s
    s.close()


@pytest.fixture()
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(ev, "regiao_do_ip", lambda ip: FLORIPA if ip == IP else None)
    monkeypatch.setattr(acessos, "_ultima_limpeza", None)
    main.app.dependency_overrides[get_db] = lambda: (yield session)
    main.app.dependency_overrides[verify_token] = lambda: {"sub": "Ana@X.com"}
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@x.com"
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _evento(session: Session, email: str, quando: datetime, estado: str | None = "São Paulo",
            cidade: str | None = "Campinas", pais: str | None = "BR", tipo: str = "page_view") -> None:
    session.execute(
        text(
            "insert into usage_events (email, event_type, page, country, region, city, lat, lon, created_at) "
            "values (:e, :t, 'dashboard', :p, :r, :c, -22.9, -47.06, :q)"
        ),
        {"e": email, "t": tipo, "p": pais, "r": estado, "c": cidade, "q": quando},
    )
    session.commit()


def _anonimo(session: Session, tabela: str, visitante: str, quando: datetime, estado: str = "Federal District",
             cidade: str = "Brasília") -> None:
    session.execute(
        text(
            f"insert into {tabela} (created_at, visitor_id, country, region, city, lat, lon) "
            "values (:q, :v, 'BR', :r, :c, -15.78, -47.93)"
        ),
        {"q": quando, "v": visitante, "r": estado, "c": cidade},
    )
    session.commit()


# --------------------------------------------------------------------------
# UF a partir do nome que a DB-IP grava
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "estado,pais,uf",
    [
        ("São Paulo", "BR", "SP"),
        ("Federal District", "BR", "DF"),
        ("Goiás", "BR", "GO"),
        ("Pará", "BR", "PA"),
        ("Paraíba", "BR", "PB"),
        ("Paraná", "BR", "PR"),
        ("Mato Grosso do Sul", "BR", "MS"),
        ("rio grande do norte", "BR", "RN"),
        ("California", "US", None),
        ("Buenos Aires", "AR", None),
        (None, "BR", None),
        ("Estado Inventado", "BR", None),
    ],
)
def test_uf_do_estado(estado, pais, uf) -> None:
    assert acessos.uf_do_estado(estado, pais) == uf


def test_todas_as_27_ufs_tem_nome() -> None:
    assert len(list(acessos.siglas())) == 27


# --------------------------------------------------------------------------
# gravação: presença e page_view com localidade, sem IP
# --------------------------------------------------------------------------


def test_presenca_grava_localidade_e_hora_sem_guardar_ip(client: TestClient, session: Session) -> None:
    resp = client.post("/api/events/presence", headers={"X-Forwarded-For": f"{IP}, 172.18.0.2"})
    assert resp.status_code == 204

    linha = session.execute(text("select * from member_presence")).mappings().one()
    assert linha["email"] == "ana@x.com"
    assert linha["projeto_id"] == 1
    assert (linha["country"], linha["region"], linha["city"]) == ("BR", "Santa Catarina", "Florianópolis")
    assert (linha["lat"], linha["lon"]) == (-27.6, -48.55)
    assert session.execute(text("select count(*) from member_presence_hour")).scalar() == 1

    # O IP não aparece em nenhuma coluna de nenhuma tabela.
    for tabela in ("member_presence", "member_presence_hour"):
        for registro in session.execute(text(f"select * from {tabela}")).all():
            assert IP not in [str(v) for v in registro]


def test_presenca_repetida_atualiza_a_mesma_linha(client: TestClient, session: Session) -> None:
    client.post("/api/events/presence", headers={"X-Forwarded-For": IP})
    client.post("/api/events/presence")  # sem IP público: localidade some, a linha é a mesma
    assert session.execute(text("select count(*) from member_presence")).scalar() == 1
    assert session.execute(text("select region from member_presence")).scalar() is None
    assert session.execute(text("select count(*) from member_presence_hour")).scalar() == 1


def test_page_view_grava_localidade_sem_ip(client: TestClient, session: Session) -> None:
    resp = client.post(
        "/api/events",
        json={"events": [{"type": "page_view", "page": "dashboard"}]},
        headers={"X-Forwarded-For": IP},
    )
    assert resp.status_code == 204
    linha = session.execute(text("select * from usage_events")).mappings().one()
    assert (linha["country"], linha["region"], linha["city"]) == ("BR", "Santa Catarina", "Florianópolis")
    assert (linha["lat"], linha["lon"]) == (-27.6, -48.55)
    assert IP not in [str(v) for v in linha.values()]


# --------------------------------------------------------------------------
# ativos agora
# --------------------------------------------------------------------------


def test_ativos_agora_conta_so_quem_deu_sinal_nos_ultimos_15_min(session: Session) -> None:
    for email, minutos, estado in (("a@x", 1, "São Paulo"), ("b@x", 14, "Federal District"), ("c@x", 16, "Bahia")):
        acessos.registrar_presenca(
            session, email=email, projeto_id=None, regiao=("BR", estado, None), agora=AGORA - timedelta(minutes=minutos)
        )
    _anonimo(session, "collection_event", "v-1", AGORA - timedelta(minutes=3))
    _anonimo(session, "collection_event", "v-1", AGORA - timedelta(minutes=2))  # mesmo visitante
    _anonimo(session, "page_event", "v-2", AGORA - timedelta(minutes=5), estado="São Paulo")
    _anonimo(session, "page_event", "v-3", AGORA - timedelta(minutes=40))  # fora da janela

    ativos = acessos.ativos_agora(session, 15, AGORA)

    assert ativos["membros"] == 2
    assert ativos["visitantes"] == 2
    por_uf = {x["uf"]: x for x in ativos["por_uf"]}
    assert por_uf["SP"] == {"uf": "SP", "membros": 1, "visitantes": 1}
    assert por_uf["DF"] == {"uf": "DF", "membros": 1, "visitantes": 1}
    assert "BA" not in por_uf


def test_painel_usa_janela_padrao_de_15_minutos(session: Session) -> None:
    acessos.registrar_presenca(session, email="a@x", projeto_id=None, regiao=None, agora=AGORA - timedelta(minutes=14))
    acessos.registrar_presenca(session, email="b@x", projeto_id=None, regiao=None, agora=AGORA - timedelta(minutes=17))
    assert acessos.painel(session, 7, agora=AGORA)["ativos"]["membros"] == 1


# --------------------------------------------------------------------------
# mapa do período
# --------------------------------------------------------------------------


def test_mapa_conta_pessoas_distintas_por_uf_cidade_e_pais(session: Session) -> None:
    _evento(session, "a@x", AGORA - timedelta(days=1))
    _evento(session, "a@x", AGORA - timedelta(days=2))  # mesma pessoa, mesma cidade
    _evento(session, "b@x", AGORA - timedelta(days=3), estado="São Paulo", cidade="São Paulo")
    _evento(session, "c@x", AGORA - timedelta(days=1), estado="California", cidade="San Jose", pais="US")
    _evento(session, "d@x", AGORA - timedelta(days=1), estado=None, cidade=None, pais=None)
    _evento(session, "e@x", AGORA - timedelta(days=20))  # fora dos 7 dias
    _evento(session, "f@x", AGORA - timedelta(days=1), tipo="favorite_added")  # não é navegação
    _anonimo(session, "collection_event", "v-1", AGORA - timedelta(days=1))

    mapa = acessos.mapa_do_periodo(session, 7, AGORA)

    membros = mapa["membros"]
    assert membros["pessoas"] == 4
    assert membros["sem_localidade"] == 1
    assert membros["ufs"] == {"SP": 2}
    assert membros["cidades"][0] == {"uf": "SP", "cidade": "Campinas", "pessoas": 1, "lat": -22.9, "lon": -47.06}
    assert {c["cidade"] for c in membros["cidades"]} == {"Campinas", "São Paulo"}
    assert membros["exterior"] == [{"pais": "US", "pessoas": 1}]

    assert mapa["visitantes"]["ufs"] == {"DF": 1}
    assert mapa["visitantes"]["cidades"][0]["cidade"] == "Brasília"


# --------------------------------------------------------------------------
# quadro de horários
# --------------------------------------------------------------------------


def test_quadro_de_horarios_em_horario_de_brasilia(session: Session) -> None:
    # Quinta 12/11 às 12h em Brasília = 15h UTC: dois membros (um por presença, outro por page_view).
    acessos.registrar_presenca(session, email="a@x", projeto_id=None, regiao=None, agora=AGORA)
    _evento(session, "b@x", AGORA + timedelta(minutes=10))
    _evento(session, "a@x", AGORA + timedelta(minutes=20))  # a@x de novo na mesma hora: conta 1

    quadro = acessos.quadro_de_horarios(session, 7, AGORA + timedelta(minutes=30))

    quinta = acessos.DIAS_SEMANA.index("qui")
    # Em 7 dias, a quinta 12h aparece 1 vez (só a de hoje cai dentro do período)
    assert quadro["media"][quinta][12] == 2.0
    assert quadro["media"][quinta][3] == 0.0
    assert quadro["pico"] == 2.0
    assert len(quadro["mais_calmos"]) == 5
    assert all(c["media"] == 0.0 for c in quadro["mais_calmos"])


# --------------------------------------------------------------------------
# retenção de 90 dias
# --------------------------------------------------------------------------


def test_limpeza_apaga_localidade_com_mais_de_90_dias_e_mantem_o_evento(session: Session) -> None:
    _evento(session, "velho@x", AGORA - timedelta(days=91))
    _evento(session, "novo@x", AGORA - timedelta(days=89))
    acessos.registrar_presenca(session, email="velho@x", projeto_id=None, regiao=FLORIPA, agora=AGORA - timedelta(days=91))
    acessos.registrar_presenca(session, email="novo@x", projeto_id=None, regiao=FLORIPA, agora=AGORA - timedelta(days=1))

    apagados = acessos.limpar_localidades_antigas(session, AGORA)

    assert apagados == {"eventos": 1, "presencas": 1, "horas": 1}
    eventos = dict(session.execute(text("select email, region from usage_events")).all())
    assert eventos == {"velho@x": None, "novo@x": "São Paulo"}  # o evento velho continua, sem localidade
    assert [r[0] for r in session.execute(text("select email from member_presence")).all()] == ["novo@x"]
    assert [r[0] for r in session.execute(text("select email from member_presence_hour")).all()] == ["novo@x"]


def test_limpeza_roda_no_maximo_uma_vez_por_dia(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acessos, "_ultima_limpeza", None)
    chamadas: list[datetime] = []
    monkeypatch.setattr(acessos, "limpar_localidades_antigas", lambda db, agora: chamadas.append(agora))

    assert acessos.limpar_se_preciso(session, AGORA) is True
    assert acessos.limpar_se_preciso(session, AGORA + timedelta(hours=23)) is False
    assert acessos.limpar_se_preciso(session, AGORA + timedelta(hours=25)) is True
    assert len(chamadas) == 2


# --------------------------------------------------------------------------
# rota do admin
# --------------------------------------------------------------------------


def test_rota_admin_devolve_as_tres_partes(client: TestClient, session: Session) -> None:
    client.post("/api/events/presence", headers={"X-Forwarded-For": IP})
    resp = client.get("/api/admin/metrics/acessos?dias=30")
    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo["dias"] == 30
    assert corpo["retencao_dias"] == 90
    assert corpo["ativos"]["membros"] == 1
    assert corpo["ativos"]["por_uf"][0]["uf"] == "SC"
    assert set(corpo["mapa"]) == {"membros", "visitantes"}
    assert len(corpo["horarios"]["media"]) == 7


def test_rota_admin_corrige_periodo_invalido(client: TestClient) -> None:
    assert client.get("/api/admin/metrics/acessos?dias=999").json()["dias"] == 7
