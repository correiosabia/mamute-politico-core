from __future__ import annotations

from decimal import Decimal

from mamute_scrappers.tse_crawler import history_parsing

ENTRY_2022 = {
    "id": "160001621846",
    "sgUe": "PR",
    "cargo": "Senador",
    "local": "PARANÁ",
    "nrAno": 2022,
    "txLink": "https://divulgacandcontas.tse.jus.br/divulga/#/candidato/2022/2040602022/PR/160001621846",
    "partido": "UNIÃO",
    "nomeUrna": "SERGIO MORO",
    "idEleicao": "2040602022",
    "nrCandidato": 444,
    "nomeCandidato": "SERGIO FERNANDO MORO",
    "situacaoTotalizacao": "Eleito",
}


def test_build_history_payload():
    payload = history_parsing.build_history_payload(
        ENTRY_2022, candidacy_id=7, parliamentarian_id=3
    )
    assert payload == {
        "election_year": 2022,
        "tse_candidate_id": 160001621846,
        "tse_election_id": 2040602022,
        "office": "Senador",
        "state": "PR",
        "locality": "PARANÁ",
        "party": "UNIÃO",
        "ballot_name": "SERGIO MORO",
        "full_name": "SERGIO FERNANDO MORO",
        "ballot_number": 444,
        "result": "Eleito",
        "source_link": ENTRY_2022["txLink"],
        "candidacy_id": 7,
        "parliamentarian_id": 3,
    }


def test_entrada_sem_id_ou_ano_e_descartada():
    assert history_parsing.build_history_payload({"nrAno": 2022}) is None
    assert history_parsing.build_history_payload({"id": "123"}) is None
    assert history_parsing.build_history_payload({"id": "abc", "nrAno": 2022}) is None


def test_build_assets_payload_usa_total_da_fonte():
    detail = {"totalDeBens": 1036642.25, "bens": [{"valor": 1000.0}, {"valor": 500.5}]}
    payload = history_parsing.build_assets_payload(detail, 2022)
    assert payload["declared_assets"] == Decimal("1036642.25")
    assert payload["assets_count"] == 2
    assert payload["assets"] == detail["bens"]


def test_build_assets_payload_soma_quando_nao_ha_total():
    detail = {"totalDeBens": None, "bens": [{"valor": 1000.0}, {"valor": 500.5}]}
    assert history_parsing.build_assets_payload(detail, 2022)["declared_assets"] == Decimal(
        "1500.50"
    )


def test_build_assets_payload_com_bens_e_declarado():
    payload = history_parsing.build_assets_payload({"totalDeBens": 0, "bens": [{"valor": 0}]}, 2022)
    assert payload["assets_status"] == "declarado"
    assert payload["declared_assets"] == Decimal("0.00")


def test_build_assets_payload_sem_bens():
    payload = history_parsing.build_assets_payload({"totalDeBens": None, "bens": None}, 2022)
    assert payload == {
        "declared_assets": None,
        "assets_count": 0,
        "assets": [],
        "assets_status": "sem_bens",
    }


def test_build_assets_payload_sem_bens_nao_vira_zero():
    """CS-95: o TSE manda totalDeBens 0 sem nenhum bem. Gravar o zero punha
    "R$ 0,00" na Trajetoria e a variacao seguinte partia dele."""
    payload = history_parsing.build_assets_payload({"totalDeBens": 0, "bens": []}, 2018)
    assert payload["declared_assets"] is None
    assert payload["assets_status"] == "sem_bens"


def test_build_assets_payload_eleicao_sem_bens_no_tse():
    payload = history_parsing.build_assets_payload({"totalDeBens": 0, "bens": []}, 2004)
    assert payload["declared_assets"] is None
    assert payload["assets_status"] == "nao_divulgado"
