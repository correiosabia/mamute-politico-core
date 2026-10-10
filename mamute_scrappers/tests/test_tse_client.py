from __future__ import annotations

from unittest import mock

import pytest
import requests

from mamute_scrappers.tse_crawler import client as client_mod
from mamute_scrappers.tse_crawler.client import (
    DivulgaCandClient,
    IncompleteListingError,
)


def _response(json_data, status=200):
    resp = mock.Mock()
    resp.raise_for_status = mock.Mock()
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(str(status))
    resp.json = mock.Mock(return_value=json_data)
    return resp


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)


def test_find_general_election_id(no_sleep):
    payload = [
        {"id": 20322002026, "ano": 2026, "tipoAbrangencia": "F"},
        {"id": 2045202024, "ano": 2024, "tipoAbrangencia": "M"},
    ]
    with mock.patch.object(client_mod.cffi_requests, "get", return_value=_response(payload)):
        assert DivulgaCandClient().find_general_election_id(2026) == 20322002026
        assert DivulgaCandClient().find_general_election_id(2030) is None


def test_listagem_retenta_e_devolve_candidatos(no_sleep):
    ok = _response({"candidatos": [{"id": 1}, {"id": 2}]})
    with mock.patch.object(
        client_mod.cffi_requests, "get", side_effect=[_response(None, status=504), ok]
    ):
        candidates = DivulgaCandClient().list_candidates(2026, "AC", 20322002026, 5)
    assert [c["id"] for c in candidates] == [1, 2]


def test_listagem_com_falha_persistente_e_ruidosa(no_sleep):
    bad = requests.ConnectionError("down")
    with mock.patch.object(client_mod.cffi_requests, "get", side_effect=bad):
        with pytest.raises(IncompleteListingError):
            DivulgaCandClient().list_candidates(2026, "AC", 20322002026, 5)


def test_detalhe_com_falha_persistente_devolve_none(no_sleep):
    bad = requests.ConnectionError("down")
    with mock.patch.object(client_mod.cffi_requests, "get", side_effect=bad):
        detail = DivulgaCandClient().get_candidate_detail(2026, "AC", 20322002026, 99)
    assert detail is None


def test_listagem_vazia_e_fim_legitimo(no_sleep):
    with mock.patch.object(
        client_mod.cffi_requests, "get", return_value=_response({"candidatos": []})
    ):
        assert DivulgaCandClient().list_candidates(2026, "RR", 20322002026, 5) == []


def test_imita_o_chrome(no_sleep):
    """CS-126: o Akamai do TSE recusa o TLS do Python (conferido em 10/10/2026:
    requests/httpx/urllib = 403 com qualquer header; curl_cffi como Chrome = 200)."""
    with mock.patch.object(
        client_mod.cffi_requests, "get", return_value=_response({"id": 1})
    ) as get:
        DivulgaCandClient().get_candidate_detail(2026, "BR", 20322002026, 1)

    assert get.call_args.kwargs["impersonate"] == "chrome"
    assert "json" in get.call_args.kwargs["headers"]["Accept"]


def test_403_nao_e_repetido(no_sleep):
    """Bloqueio do CDN não passa com retry: repetir só multiplica a espera."""
    with mock.patch.object(
        client_mod.cffi_requests, "get", return_value=_response(None, status=403)
    ) as get:
        resp = get.return_value
        resp.status_code = 403
        assert DivulgaCandClient().get_candidate_detail(2026, "BR", 1, 1) is None

    assert get.call_count == 1
