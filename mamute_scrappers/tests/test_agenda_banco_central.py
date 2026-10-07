"""Agenda da diretoria do Banco Central: parser do HTML de um dia (CS-135)."""

from __future__ import annotations

from datetime import date

from mamute_scrappers.agenda_crawler.banco_central import build_items, cargo, lugar, parse_dia, por_dia

# Formato de 2025: texto aninhado em div/p/span, horário antes do travessão.
HTML_2025 = (
    '<div class="ExternalClassA"><div><strong>Manhã</strong></div>'
    '<div>11&#58;00 às 12&#58;00 – <div class="d-inline"><p class="d-inline">​A<span>udiência com '
    "Fulano de Tal, Presidente do Banco X, e Beltrano,&#160;no Edifício-Sede do Banco Central, em Brasília, "
    "para tratar de assuntos institucionais.&#160;<strong>(fechado à imprensa)</strong></span><br></p></div></div>"
    '<div><strong>Tarde</strong></div>'
    '<div><div class="d-inline"><p class="d-inline">​Despachos internos em Brasília.<br></p></div></div>'
    '<div>18&#58;00 às 19&#58;00 – <div><p>Audiência, por videoconferência, com Sicrano, no Rio de Janeiro, '
    "para tratar de supervisão.</p></div></div></div>"
)

# Formato de 2023: texto direto no <div>, com link no meio.
HTML_2023 = (
    '<div class="ExternalClassB"><div><strong>Manhã</strong></div>'
    '<div>11&#58;00 às 12&#58;00 – Reunião com <a href="/x.pdf">representantes</a> de Bancos, no Banco Central, '
    "em São Paulo, para tratar de assuntos do Sistema Financeiro. <strong>(fechado à imprensa)</strong></div>"
    "<div><strong>Tarde</strong><br></div>"
    "<div>17&#58;00 às 18&#58;00 – Reunião com Fulano, em Nova York, EUA. <strong>(fechado à imprensa)</strong></div></div>"
)


def test_parse_dia_2025():
    itens = parse_dia(HTML_2025)
    assert [(i["starts_at"], i["ends_at"]) for i in itens] == [("11:00", "12:00"), (None, None), ("18:00", "19:00")]
    assert itens[0]["description"].startswith("Audiência com Fulano de Tal")
    assert "​" not in itens[0]["description"] and "  " not in itens[0]["description"]
    assert itens[0]["place"] == "Brasília" and itens[0]["remote"] is False
    assert itens[1]["description"] == "Despachos internos em Brasília."
    assert itens[2]["place"] == "Rio de Janeiro" and itens[2]["remote"] is True


def test_parse_dia_2023():
    itens = parse_dia(HTML_2023)
    assert len(itens) == 2
    assert itens[0]["description"].startswith("Reunião com representantes de Bancos")
    assert itens[0]["place"] == "São Paulo"
    assert itens[1]["place"] == "Nova Iorque"


def test_lugar_e_cargo():
    assert lugar("Reunião no Banco Central, em Brasilia, para tratar") == "Brasília"
    assert lugar("Reunião por videoconferência, para tratar de supervisão.") is None
    erro_da_fonte = parse_dia("<div><div>18:30 às 19:30 – Audiência, por videoconfêrencia, com Fulano.</div></div>")
    assert erro_da_fonte[0]["remote"] is True
    assert cargo("Presi - 01/04/2025") == ("Presi", "Presidente do Banco Central")
    assert cargo("Dinor|Diorf - 15/01/2026")[1] == (
        "Diretor de Regulação e Diretor de Organização do Sistema Financeiro e de Resolução"
    )


def test_build_items():
    linhas = build_items(
        {"evento": "Difis - 17/03/2025", "dataEvento": "2025-03-17T03:00:00Z", "autoridade": "Fulano",
         "idAutoridade": 54, "descricao": HTML_2025}
    )
    assert [l["seq"] for l in linhas] == [0, 1, 2]
    assert {l["event_date"] for l in linhas} == {date(2025, 3, 17)}
    assert linhas[0]["authority_id"] == "54" and linhas[0]["office_label"] == "Diretor de Fiscalização"
    assert build_items({"evento": "Presi - x", "descricao": HTML_2025}) == []


def test_por_dia_junta_itens_repetidos_da_mesma_autoridade():
    base = {"evento": "Diorf - 27/01/25", "dataEvento": "2025-01-27T03:00:00Z", "idAutoridade": 50}
    dias = por_dia(
        [
            {**base, "descricao": HTML_2023},
            # A fonte repete a autoridade no mesmo dia (agenda do dia seguinte com a data errada).
            {**base, "evento": "Diorf - 28/01/25", "descricao": HTML_2025},
            {"evento": "Presi - x", "descricao": HTML_2025},
        ]
    )
    assert list(dias) == [("50", date(2025, 1, 27))]
    assert [l["seq"] for l in dias[("50", date(2025, 1, 27))]] == [0, 1, 2, 3, 4]
