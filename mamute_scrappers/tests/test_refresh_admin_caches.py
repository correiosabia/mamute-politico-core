"""Rotina de caches admin: total oficial da Câmara, cobertura rica e câmbio."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rac = _load(
    "test_refresh_admin_caches_module",
    "mamute_scrappers/scripts/refresh_admin_caches.py",
)


class _Resp:
    def __init__(self, data: Any) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._data


# --------------------------------------------------------------------------- #
# Funções puras
# --------------------------------------------------------------------------- #
def test_parse_last_page() -> None:
    payload = {"links": [{"rel": "last", "href": "https://x/api?ano=2025&pagina=48389&itens=1"}]}
    assert rac.parse_last_page(payload) == 48389
    assert rac.parse_last_page({"dados": [1, 2, 3]}) == 3


def test_fetch_camara_year_total_no_sigla() -> None:
    captured: dict[str, Any] = {}

    def http_get(url: str, **kwargs: Any) -> _Resp:
        captured["params"] = kwargs.get("params")
        return _Resp({"links": [{"rel": "last", "href": "https://x?pagina=25750&itens=1"}]})

    assert rac.fetch_camara_year_total(2019, http_get) == 25750
    # total do ano = SEM siglaTipo (todos os tipos)
    assert "siglaTipo" not in captured["params"]
    assert captured["params"]["ano"] == 2019


def test_prop_status_bands() -> None:
    assert rac._prop_status(100, None) == "sem_referencia"
    assert rac._prop_status(208, 100) == "superset"   # >=120
    assert rac._prop_status(98, 100) == "completo"    # >=95
    assert rac._prop_status(86, 100) == "quase"       # 80-95
    assert rac._prop_status(50, 100) == "parcial"     # <80


def test_fetch_usd_brl_bid_bcb() -> None:
    def http_get(url: str, **k: Any) -> _Resp:
        assert "bcb.gov.br" in url
        return _Resp([{"data": "13/07/2026", "valor": "5.1183"}])

    assert rac.fetch_usd_brl_bid(http_get) == 5.1183


def test_fetch_usd_brl_bid_fallback_awesomeapi() -> None:
    def http_get(url: str, **k: Any) -> _Resp:
        if "bcb.gov.br" in url:
            raise RuntimeError("BCB fora")
        return _Resp({"USDBRL": {"bid": "6.12"}})

    assert rac.fetch_usd_brl_bid(http_get) == 6.12


# --------------------------------------------------------------------------- #
# Cobertura rica (SQLite espelha o Postgres de prod)
# --------------------------------------------------------------------------- #
def _coverage_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as c:
        c.exec_driver_sql("create table parliamentarian (id integer primary key, type text)")
        c.exec_driver_sql(
            "create table proposition (id integer primary key, presentation_year integer, "
            "proposition_type_id integer)"
        )
        c.exec_driver_sql(
            "create table authors_proposition (id integer primary key, proposition_id integer, "
            "parliamentarian_id integer)"
        )
        c.exec_driver_sql(
            "create table speeches_transcripts (id integer primary key, parliamentarian_id integer, date text)"
        )
        c.exec_driver_sql(
            "create table roll_call_votes (id integer primary key, parliamentarian_id integer, "
            "link text, vote_date text)"
        )
        c.exec_driver_sql(
            "create table api_coverage (id integer primary key, source text, year integer, "
            "sigla_type text, api_count integer, synced_at text)"
        )
        c.exec_driver_sql("insert into parliamentarian (id, type) values (1,'Deputado'),(2,'Senador')")
        # p1 2025 camara(tipo ok); p2 2025 senado(sem tipo); p3 2025 desconhecido→camara(sem tipo); p4 2024 camara
        c.exec_driver_sql(
            "insert into proposition (id, presentation_year, proposition_type_id) values "
            "(1,2025,10),(2,2025,null),(3,2025,null),(4,2024,10)"
        )
        c.exec_driver_sql(
            "insert into authors_proposition (proposition_id, parliamentarian_id) values (1,1),(2,2),(4,1)"
        )
        # oficial Câmara 2025 = 1 (nosso câmara 2025 = p1+p3 = 2 → superset)
        c.exec_driver_sql(
            "insert into api_coverage (source, year, sigla_type, api_count) values ('camara',2025,'TODOS',1)"
        )
        c.exec_driver_sql(
            "insert into speeches_transcripts (parliamentarian_id, date) values (1,'2025-03-01'),(2,'2024-05-01')"
        )
        c.exec_driver_sql(
            "insert into roll_call_votes (parliamentarian_id, link, vote_date) values "
            "(1,'L1','2025-06-01'),(1,'L1','2025-06-01'),(2,'L2','2025-06-02')"
        )
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_build_coverage_payload_new_shape() -> None:
    p = rac.build_coverage_payload(_coverage_session())

    assert p["kpis"] == {
        "proposicoes": 4,
        "discursos": 2,
        "votacoes_nominais": 2,  # distinct link L1, L2
        "parlamentares": 2,
    }

    prop = {r["year"]: r for r in p["proposicoes"]["camara"]}
    assert prop[2025]["nosso"] == 2          # camara + desconhecido
    assert prop[2025]["oficial"] == 1
    assert prop[2025]["status"] == "superset"
    assert prop[2024]["oficial"] is None
    assert prop[2024]["status"] == "sem_referencia"
    sen = {r["year"]: r["nosso"] for r in p["proposicoes"]["senado"]}
    assert sen[2025] == 1
    assert p["proposicoes"]["sem_tipo_total"] == 2

    disc = {r["year"]: r["nosso"] for r in p["discursos"]["camara"]}
    assert disc[2025] == 1
    disc_sen = {r["year"]: r["nosso"] for r in p["discursos"]["senado"]}
    assert disc_sen[2024] == 1

    vote = {r["year"]: r["nosso"] for r in p["votacoes"]["camara"]}
    assert vote[2025] == 1
    vote_sen = {r["year"]: r["nosso"] for r in p["votacoes"]["senado"]}
    assert vote_sen[2025] == 1

    assert p["parlamentares"]["deputados"]["nossa_base"] == 1
    assert p["parlamentares"]["deputados"]["cadeiras"] == 513
    assert p["parlamentares"]["senadores"]["nossa_base"] == 1
    assert len(p["consolidado"]) == 6

    # Registros oficiais (CS-170): 2 parlamentares + 4 proposições + 2 discursos
    # + 3 votos (linhas, não sessões). authors_proposition e api_coverage não contam.
    reg = p["registros_oficiais"]
    assert reg["total"] == 11
    assert {item["tabela"]: item["n"] for item in reg["por_tabela"]} == {
        "parliamentarian": 2,
        "proposition": 4,
        "roll_call_votes": 3,
        "speeches_transcripts": 2,
    }


def test_count_official_records_ignora_tabela_inexistente() -> None:
    """Tabela que a migration ainda não criou some do detalhe — não vira zero."""
    session = _coverage_session()
    with session.get_bind().begin() as c:
        c.exec_driver_sql("create table parliamentary_expense (id integer primary key)")
    reg = rac.count_official_records(session)
    por_tabela = {item["tabela"]: item["n"] for item in reg["por_tabela"]}
    assert por_tabela["parliamentary_expense"] == 0  # existe, ainda que vazia
    assert "parliamentary_amendment" not in por_tabela  # não existe
    assert "authors_proposition" not in por_tabela  # ligação, fora da conta
    assert reg["total"] == 11


# --------------------------------------------------------------------------- #
# Câmbio: upsert idempotente
# --------------------------------------------------------------------------- #
def test_store_usd_brl_rate_upserts() -> None:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as c:
        c.exec_driver_sql(
            "create table usd_brl_rate (rate_date date primary key, bid numeric, "
            "fetched_at datetime default current_timestamp)"
        )
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    today = date(2026, 7, 6)
    rac.store_usd_brl_rate(session, 6.10, today)
    rac.store_usd_brl_rate(session, 6.25, today)  # mesmo dia → atualiza
    session.commit()
    rows = session.execute(text("select rate_date, bid from usd_brl_rate")).all()
    assert len(rows) == 1
    assert float(rows[0][1]) == 6.25
