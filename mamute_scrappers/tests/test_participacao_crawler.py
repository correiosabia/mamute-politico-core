"""Coleta das missões do dia: parsers com páginas reais de nov/2026 (CS-179)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from mamute_scrappers.participacao_crawler import brasil_participativo, camara, comum, senado

FX = Path(__file__).parent / "fixtures" / "participacao"


def _fx(nome: str) -> str:
    return (FX / nome).read_text(encoding="utf-8")


def test_utilitarios():
    assert comum.numero("1.280.708") == 1280708
    assert comum.numero("59.807 apoios confirmados") == 59807
    assert comum.numero("") is None
    assert comum.data_por_extenso("Apoios abertos até 25 de fevereiro de 2027") == date(2027, 2, 25)
    assert comum.data_por_extenso("1º de março") is None
    assert comum.resumo("a " * 300, 20).endswith("…")


def test_brasil_participativo_prefiltro():
    dados = json.loads(_fx("brasil_participativo.json"))
    itens, todos = brasil_participativo.build_items(dados, date(2026, 11, 26))
    slugs = {i["url"].rsplit("/", 1)[-1] for i in itens}
    # Guarda-chuva até 2035 (aneel), seleção de OSC e processo de 2023 ficam fora.
    assert slugs == {
        "politicadedados",
        "consultas-publicas-conitec",
        "IBGE-Prioridade-de-divulgacao-nos-temas-das-pesquisas-e-resultados-2026",
    }
    # A lista completa de ids serve para fechar quem sumiu da fonte.
    assert len(todos) == 6
    ibge = next(i for i in itens if "IBGE" in i["url"])
    assert ibge["closes_on"] == date(2026, 12, 4) and ibge["opens_on"] == date(2026, 11, 4)
    assert ibge["kind"] == "consulta" and ibge["source"] == "brasil_participativo"


def test_brasil_participativo_descarta_eleicoes_no_plural():
    dados = {"data": {"participatoryProcesses": [
        {"id": 1, "slug": "cfbio", "title": {"translation": "Eleições Diretas no CFBio"}, "endDate": "2026-11-27"},
        {"id": 2, "slug": "x", "title": {"translation": "Editais de cultura"}, "endDate": "2026-11-27"},
    ]}}
    assert brasil_participativo.build_items(dados, date(2026, 11, 26))[0] == []


def test_senado_consultas():
    itens = senado.build_consultas(_fx("senado_consulta.html"))
    assert [i["reference"] for i in itens] == ["PL 5064/2023", "MPV 1394/2026", "PL 5555/2020"]
    pl = itens[0]
    assert pl["external_id"] == "160575" and pl["closes_on"] is None
    assert pl["url"].endswith("visualizacaomateria?id=160575")
    assert pl["score"] == {"options": [{"label": "Sim", "count": 733590}, {"label": "Não", "count": 1280708}]}
    assert len(pl["title"]) <= 221 and pl["summary"]
    assert len(senado.build_consultas(_fx("senado_consulta.html"), limite=2)) == 2


def test_senado_placar_da_materia():
    assert senado.placar_da_materia(_fx("senado_materia.html"))["options"][1]["count"] == 1280708
    assert senado.placar_da_materia("<html></html>") is None


def test_senado_ideias():
    itens = senado.build_ideias(_fx("senado_ideia.html"))
    primeira = itens[0]
    assert primeira["external_id"] == "252656" and primeira["kind"] == "ideia"
    assert primeira["closes_on"] == date(2027, 2, 25)
    assert primeira["score"] == {"options": [{"label": "Apoios", "count": 59807}], "goal": 20000}
    # A última ideia da página tem menos de 1.000 apoios e fica fora.
    assert len(itens) == 2
    assert len(senado.build_ideias(_fx("senado_ideia.html"), min_apoios=0)) == 3


def test_camara_lista_da_ultima_semana():
    itens = camara.build_lista(_fx("camara_enquetes.html"))
    ids = [i["external_id"] for i in itens]
    assert len(ids) == len(set(ids)) > 0
    assert "2607415" in ids
    pl = next(i for i in itens if i["external_id"] == "2607415")
    assert pl["reference"] == "PL 994/2026" and pl["kind"] == "enquete" and pl["closes_on"] is None
    assert pl["url"] == "https://www.camara.leg.br/enquetes/2607415"


def test_camara_placar():
    p = camara.placar(_fx("camara_resultado.html"))
    assert [o["label"] for o in p["options"]] == [
        "Concordo totalmente", "Concordo na maior parte", "Estou indeciso",
        "Discordo na maior parte", "Discordo totalmente",
    ]
    assert p["options"][-1]["count"] == 130814
    assert p["note"] == "Resultado parcial desde 06/03/2026"
    assert camara.placar("<html></html>") is None


class _Sessao:
    def __init__(self):
        self.chamadas = []

    def execute(self, *a, **k):
        self.chamadas.append(a)


def test_fonte_vazia_nao_fecha_nada():
    s = _Sessao()
    assert comum.salvar(s, "senado_consulta", []) == {"gravados": 0, "fechados": 0}
    assert s.chamadas == []
