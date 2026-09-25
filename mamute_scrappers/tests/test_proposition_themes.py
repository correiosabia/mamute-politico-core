"""CS-92: extração das áreas temáticas oficiais (Câmara e Senado).

Formatos reais das fontes:
  - Câmara, GET /proposicoes/2270800/temas;
  - Senado, `classificacoes` de GET /processo/8441243 (PL 2338/2023).
"""

from __future__ import annotations

from mamute_scrappers.camara_crawler import proposition as camara
from mamute_scrappers.scripts import backfill_proposition_themes as backfill
from mamute_scrappers.senado_crawler import proposition as senado


CAMARA_TEMAS = {
    "dados": [
        {"codTema": 68, "tema": "Direito Constitucional", "relevancia": 0},
        {"codTema": 76, "tema": "Direito e Justiça", "relevancia": 0},
        {"codTema": 53, "tema": "Processo Legislativo e Atuação Parlamentar", "relevancia": 0},
    ],
    "links": [{"rel": "self", "href": "https://dadosabertos.camara.leg.br/api/v2/proposicoes/2270800/temas"}],
}

SENADO_PROCESSO = {
    "id": 8441243,
    "identificacao": "PL 2338/2023",
    "classificacoes": [
        {
            "codigo": 33805092,
            "descricao": "Ciência, Tecnologia e Informática",
            "descricaoHierarquia": "Economia e Desenvolvimento / Ciência, Tecnologia e Informática",
        },
        {
            "codigo": 33805512,
            "descricao": "Responsabilidade Civil",
            "descricaoHierarquia": "Jurídico / Direito Civil / Responsabilidade Civil",
        },
    ],
}


# --- Câmara -----------------------------------------------------------------


def test_camara_themes_keep_source_order() -> None:
    assert camara.parse_camara_themes(CAMARA_TEMAS) == [
        "Direito Constitucional",
        "Direito e Justiça",
        "Processo Legislativo e Atuação Parlamentar",
    ]


def test_camara_without_classification_is_empty_not_missing() -> None:
    # REQ, EMC, PRL...: a Câmara responde 200 com lista vazia.
    assert camara.parse_camara_themes({"dados": [], "links": []}) == []


def test_camara_failed_or_unexpected_response_is_not_collected() -> None:
    assert camara.parse_camara_themes(None) is None
    assert camara.parse_camara_themes({"erro": "x"}) is None


def test_camara_themes_skip_blank_and_duplicates() -> None:
    data = {"dados": [{"tema": " Saúde "}, {"tema": "Saúde"}, {"tema": ""}, "lixo"]}
    assert camara.parse_camara_themes(data) == ["Saúde"]


def test_fetch_themes_hits_temas_endpoint(monkeypatch) -> None:
    calls = []

    def fake_request(url, *, params=None):
        calls.append(url)
        return CAMARA_TEMAS

    monkeypatch.setattr(camara, "_request_json", fake_request)

    assert camara._fetch_proposition_themes(2270800)[0] == "Direito Constitucional"
    assert calls == ["https://dadosabertos.camara.leg.br/api/v2/proposicoes/2270800/temas"]


def test_camara_payload_carries_themes_only_when_collected() -> None:
    basic = {"id": 2270800, "siglaTipo": "PEC", "numero": 3, "ano": 2021}

    collected = camara._build_payload_from_data(basic, None, [], ["Saúde"])
    failed = camara._build_payload_from_data(basic, None, [], None)

    assert collected["themes"] == ["Saúde"]
    # Falha de consulta não entra no payload: o upsert não apaga tema existente.
    assert "themes" not in failed


# --- Senado -----------------------------------------------------------------


def test_senado_themes_use_most_specific_level() -> None:
    assert senado.extract_senado_themes(SENADO_PROCESSO) == [
        "Ciência, Tecnologia e Informática",
        "Responsabilidade Civil",
    ]


def test_senado_process_without_classification_is_empty() -> None:
    assert senado.extract_senado_themes({"id": 1, "classificacoes": []}) == []
    assert senado.extract_senado_themes({"id": 1}) == []


def test_senado_without_process_is_not_collected() -> None:
    assert senado.extract_senado_themes(None) is None


def test_senado_payload_carries_themes_only_with_process() -> None:
    entry = {"process_id": 8441243, "materia_code": 157233, "sigla": "PL"}

    with_process = senado._build_payload(5012, entry, SENADO_PROCESSO)
    without_process = senado._build_payload(5012, entry, None)

    assert with_process["themes"] == [
        "Ciência, Tecnologia e Informática",
        "Responsabilidade Civil",
    ]
    assert "themes" not in without_process


def test_backfill_reads_senado_themes_from_stored_details() -> None:
    assert backfill.senado_themes_from_details({"processo": SENADO_PROCESSO}) == [
        "Ciência, Tecnologia e Informática",
        "Responsabilidade Civil",
    ]
    assert backfill.senado_themes_from_details({"autoria": {}}) is None
    assert backfill.senado_themes_from_details(None) is None
