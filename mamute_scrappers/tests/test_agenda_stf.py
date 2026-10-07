"""Agenda do STF: parser da resposta do GraphQL e tratamento do WAF (CS-135)."""

from __future__ import annotations

from datetime import date

import pytest

from mamute_scrappers.agenda_crawler import stf

DIA = {
    "data": "04/02/2025",
    "ministro": [
        {
            "nomeMinistro": "MIN. CRISTIANO ZANIN",
            "eventos": [
                {"hora": "12h00", "titulo": "Dr. Fulano de Tal &#8211; advogado", "texto": "<p>Assunto: ADPF 1068</p>\n"},
                {"hora": "21h30", "titulo": "Reunião por videoconferência com Beltrano", "texto": ""},
                {"hora": "", "titulo": "", "texto": ""},
            ],
        },
        {"nomeMinistro": "PRESIDENTE", "eventos": [{"hora": "08h00", "titulo": "Sessão Plenária", "texto": ""}]},
    ],
}


def test_hora_soma_tres_horas():
    assert stf.hora_corrigida("12h00") == "15:00"
    assert stf.hora_corrigida("21h30") == "00:30"
    assert stf.hora_corrigida("9h") == "12:00"
    assert stf.hora_corrigida("") is None


def test_build_items():
    dias = stf.build_items(DIA)
    assert set(dias) == {("cristiano-zanin", date(2025, 2, 4)), ("presidente", date(2025, 2, 4))}
    zanin = dias[("cristiano-zanin", date(2025, 2, 4))]
    assert [l["seq"] for l in zanin] == [0, 1]
    assert zanin[0]["authority_name"] == "Cristiano Zanin"
    assert zanin[0]["description"] == "Dr. Fulano de Tal – advogado Assunto: ADPF 1068"
    assert zanin[0]["starts_at"] == "15:00" and zanin[0]["organization"] == "Supremo Tribunal Federal"
    assert zanin[1]["remote"] is True
    pres = dias[("presidente", date(2025, 2, 4))][0]
    assert pres["authority_name"] == "Presidência do STF"
    assert pres["office_label"] == "Presidente do Supremo Tribunal Federal"
    assert stf.build_items({"data": "x"}) == {}


class _Resp:
    def __init__(self, status: int, corpo: bytes, dados=None):
        self.status_code, self.content, self._dados = status, corpo, dados

    def raise_for_status(self):
        pass

    def json(self):
        return self._dados


def test_waf_tenta_de_novo_e_desiste(monkeypatch):
    respostas = [_Resp(202, b""), _Resp(200, b"{}", {"data": {"agendaMinistrosPorDiaCategoria": [DIA]}})]
    esperas: list[int] = []
    monkeypatch.setattr(stf.requests, "get", lambda *a, **k: respostas.pop(0))
    assert stf.fetch_range(date(2025, 2, 1), date(2025, 2, 28), esperar=esperas.append) == [DIA]
    assert esperas == [stf.ESPERAS_WAF[0]]

    monkeypatch.setattr(stf.requests, "get", lambda *a, **k: _Resp(202, b""))
    with pytest.raises(stf.WafBloqueou):
        stf.fetch_range(date(2025, 2, 1), date(2025, 2, 28), esperar=lambda s: None)
