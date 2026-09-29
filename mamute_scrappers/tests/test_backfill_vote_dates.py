"""Testes das funções puras do orquestrador backfill_vote_dates."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bvd = _load(
    "test_backfill_vote_dates_module",
    "mamute_scrappers/scripts/backfill_vote_dates.py",
)

SENADO = "https://legis.senado.leg.br/dadosabertos"

# Formato de GET /dadosabertos/votacao?codigoMateria=166095&v=1 (campos
# irrelevantes cortados). A segunda votação teve a data trocada para o teste
# distinguir as duas.
SENADO_MATERIA_PAYLOAD = [
    {
        "codigoMateria": 166095,
        "codigoSessaoVotacao": 7013,
        "codigoVotacaoSve": 4329,
        "dataSessao": "2025-09-30",
        "identificacao": "PLP 108/2024",
    },
    {
        "codigoMateria": 166095,
        "codigoSessaoVotacao": 7014,
        "codigoVotacaoSve": 4330,
        "dataSessao": "2025-10-01",
        "identificacao": "PLP 108/2024",
    },
]


@pytest.fixture(autouse=True)
def _clear_materia_cache():
    bvd._senado_votes_for_materia.cache_clear()
    yield
    bvd._senado_votes_for_materia.cache_clear()


def _stub_request(monkeypatch, response):
    calls: list[tuple[str, dict | None]] = []

    def fake(url, params=None):
        calls.append((url, params))
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(bvd, "_request_json", fake)
    return calls


def test_parse_date_iso_with_time() -> None:
    assert bvd._parse_date("2024-08-14T15:30:00") == date(2024, 8, 14)


def test_parse_date_iso_date_only() -> None:
    assert bvd._parse_date("2024-08-14") == date(2024, 8, 14)


def test_parse_date_brazilian_format() -> None:
    assert bvd._parse_date("14/08/2024") == date(2024, 8, 14)


def test_parse_date_returns_none_for_garbage() -> None:
    assert bvd._parse_date("not-a-date") is None
    assert bvd._parse_date("") is None
    assert bvd._parse_date(None) is None


def test_resolve_fetcher_dispatches_by_url_host() -> None:
    assert (
        bvd._resolve_fetcher("https://dadosabertos.camara.leg.br/api/v2/votacoes/123")
        is bvd._fetch_camara_date
    )
    assert bvd._resolve_fetcher(f"{SENADO}/votacao/45?v=1") is bvd._fetch_senado_date
    assert bvd._resolve_fetcher("https://example.com/foo") is None


def test_senado_vote_keys_reads_both_link_formats() -> None:
    assert bvd._senado_vote_keys(f"{SENADO}/votacao/4330?v=1") == (4330, None)
    assert bvd._senado_vote_keys(
        f"{SENADO}/votacao?codigoSessaoVotacao=7014&v=1"
    ) == (None, 7014)


def test_senado_date_comes_from_the_matching_vote_of_the_materia(monkeypatch) -> None:
    calls = _stub_request(monkeypatch, SENADO_MATERIA_PAYLOAD)

    assert bvd._fetch_senado_date(f"{SENADO}/votacao/4330?v=1", 166095) == (
        date(2025, 10, 1),
        None,
    )
    # Consulta por matéria — o filtro que o endpoint respeita —, nunca o link.
    assert calls == [(bvd.SENADO_VOTACAO_URL, {"codigoMateria": "166095", "v": "1"})]


def test_senado_session_vote_link_matches_by_session_code(monkeypatch) -> None:
    _stub_request(monkeypatch, SENADO_MATERIA_PAYLOAD)

    assert bvd._fetch_senado_date(
        f"{SENADO}/votacao?codigoSessaoVotacao=7013&v=1", 166095
    ) == (date(2025, 9, 30), None)


def test_senado_never_takes_the_date_of_another_vote(monkeypatch) -> None:
    # O bug do CS-94: a lista padrão tinha data, mas não era desta votação.
    _stub_request(monkeypatch, SENADO_MATERIA_PAYLOAD)

    assert bvd._fetch_senado_date(f"{SENADO}/votacao/9999?v=1", 166095) == (
        None,
        bvd.REASON_NOT_IN_MATERIA,
    )


def test_senado_without_materia_code_is_residue(monkeypatch) -> None:
    calls = _stub_request(monkeypatch, SENADO_MATERIA_PAYLOAD)

    assert bvd._fetch_senado_date(f"{SENADO}/votacao/4330?v=1", None) == (
        None,
        bvd.REASON_NO_MATERIA,
    )
    assert calls == []


def test_senado_votes_of_same_materia_share_one_request(monkeypatch) -> None:
    calls = _stub_request(monkeypatch, SENADO_MATERIA_PAYLOAD)

    bvd._fetch_senado_date(f"{SENADO}/votacao/4329?v=1", 166095)
    bvd._fetch_senado_date(f"{SENADO}/votacao/4330?v=1", 166095)

    assert len(calls) == 1


def test_senado_transient_failure_propagates(monkeypatch) -> None:
    _stub_request(monkeypatch, bvd.TransientFetchError("HTTP 503"))

    with pytest.raises(bvd.TransientFetchError):
        bvd._fetch_senado_date(f"{SENADO}/votacao/4330?v=1", 166095)


def test_camara_falls_back_to_data_field(monkeypatch) -> None:
    _stub_request(monkeypatch, {"dados": {"id": "1-1", "data": "2019-05-14"}})

    assert bvd._fetch_camara_date(
        "https://dadosabertos.camara.leg.br/api/v2/votacoes/1-1"
    ) == (date(2019, 5, 14), None)


def test_camara_404_is_residue(monkeypatch) -> None:
    _stub_request(monkeypatch, None)

    assert bvd._fetch_camara_date(
        "https://dadosabertos.camara.leg.br/api/v2/votacoes/1-1"
    ) == (None, bvd.REASON_NOT_FOUND)


def test_legacy_failed_list_goes_back_to_the_queue() -> None:
    state = bvd._normalize_state(
        {"done": ["a"], "failed": ["b", "c"], "updated_at": "2026-06-01T00:00:00+00:00"}
    )

    assert state["done"] == ["a"]
    assert state["failed"] == {}
    assert bvd._pending([("a", None), ("b", None), ("c", 1)], state) == [
        ("b", None),
        ("c", 1),
    ]


def test_transient_failure_becomes_residue_only_after_max_attempts() -> None:
    state = bvd._empty_state()

    for _ in range(bvd.MAX_TRANSIENT_ATTEMPTS - 1):
        bvd._record_transient(state, "x")
    assert state["attempts"] == {"x": bvd.MAX_TRANSIENT_ATTEMPTS - 1}
    assert bvd._pending([("x", None)], state) == [("x", None)]

    bvd._record_transient(state, "x")
    assert state["attempts"] == {}
    assert state["failed"] == {"x": bvd.REASON_TRANSIENT_EXHAUSTED}
    assert bvd._pending([("x", None)], state) == []


def test_undo_transient_restores_previous_attempts() -> None:
    state = bvd._empty_state()
    state["attempts"] = {"x": bvd.MAX_TRANSIENT_ATTEMPTS - 1}

    bvd._record_transient(state, "x")
    bvd._record_transient(state, "y")
    bvd._undo_transient(state, "x", bvd.MAX_TRANSIENT_ATTEMPTS - 1)
    bvd._undo_transient(state, "y", None)

    assert state["failed"] == {}
    assert state["attempts"] == {"x": bvd.MAX_TRANSIENT_ATTEMPTS - 1}


def test_links_retrying_go_to_the_end_of_the_queue() -> None:
    state = bvd._empty_state()
    state["attempts"] = {"old": 2}

    assert bvd._pending([("old", None), ("new", None)], state) == [
        ("new", None),
        ("old", None),
    ]


def test_residue_summary_counts_only_links_still_without_date() -> None:
    state = bvd._empty_state()
    state["failed"] = {
        "a": bvd.REASON_NOT_FOUND,
        "b": bvd.REASON_NOT_FOUND,
        "c": bvd.REASON_NOT_IN_MATERIA,
        "preenchido-pelo-crawler": bvd.REASON_NOT_FOUND,
    }

    summary = bvd._residue_summary(state, [("a", None), ("b", None), ("c", None)])

    assert summary == "fonte_404=2, votacao_ausente_da_materia=1"
    assert bvd._residue_summary(bvd._empty_state(), []) == "nenhum"
