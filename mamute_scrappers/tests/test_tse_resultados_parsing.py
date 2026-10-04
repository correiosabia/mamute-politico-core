"""CS-106: leitura dos JSONs de resultado do TSE (fixtures do simulado e do oficial de 2026)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from mamute_scrappers.tse_crawler.resultados_parsing import (
    abrangencias,
    arquivos_da_eleicao,
    config_url,
    foi_eleito,
    parse_config,
    parse_result_file,
    result_file_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tse_resultados"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_config_oficial_2026_descobre_eleicoes_sem_codigo_fixo() -> None:
    eleicoes = parse_config(_load("oficial-ele-c.json"), "ele2026")
    por_codigo = {e.codigo: e for e in eleicoes}
    # Municipal (6261, so Conselheiro Distrital) nao entra: nao temos o cargo.
    assert set(por_codigo) == {6257, 6259}
    assert por_codigo[6257].cargos == (1,)
    assert por_codigo[6259].cargos == (3, 5, 6, 7, 8)
    assert all(e.turno == 1 for e in eleicoes)
    assert por_codigo[6259].data == date(2026, 10, 4)


def test_config_ignora_outro_ciclo() -> None:
    assert parse_config(_load("oficial-ele-c.json"), "ele2024") == []


def test_urls_seguem_o_layout_do_tse() -> None:
    base = "https://resultados.tse.jus.br/oficial/"
    assert config_url(base) == "https://resultados.tse.jus.br/oficial/comum/config/ele-c.json"
    assert result_file_url(base, "ele2026", 6259, "SP", 6) == (
        "https://resultados.tse.jus.br/oficial/ele2026/6259/dados/sp/sp-c0006-e006259-u.json"
    )
    assert result_file_url(base, "ele2026", 6257, "br", 1).endswith(
        "/6257/dados/br/br-c0001-e006257-u.json"
    )


def test_abrangencias_por_cargo() -> None:
    assert abrangencias(1) == ("br",)
    assert abrangencias(8) == ("df",)
    assert "df" not in abrangencias(7)
    assert len(abrangencias(7)) == 26
    assert len(abrangencias(6)) == 27


def test_arquivos_da_eleicao_estadual() -> None:
    eleicao = next(e for e in parse_config(_load("oficial-ele-c.json"), "ele2026") if e.codigo == 6259)
    pares = list(arquivos_da_eleicao(eleicao))
    # 27 governador + 27 senador + 27 dep. federal + 26 dep. estadual + 1 distrital
    assert len(pares) == 108
    assert ("df", 8) in pares and ("df", 7) not in pares


def test_arquivo_encerrado_com_segundo_turno() -> None:
    arquivo = parse_result_file(_load("sim-sp-governador.json"))
    assert arquivo.final is True
    assert arquivo.turno == 1
    assert arquivo.atualizado_em is not None
    assert arquivo.atualizado_em.utcoffset().total_seconds() == -3 * 3600
    situacoes = {c.situacao for c in arquivo.candidatos}
    assert "2º turno" in situacoes
    segundo = [c for c in arquivo.candidatos if c.situacao == "2º turno"]
    assert len(segundo) == 2
    assert all(c.eleito is True for c in segundo)  # campo `e` vem "s" para quem segue
    sub_judice = [c for c in arquivo.candidatos if c.destinacao_voto == "Anulado sub judice"]
    assert sub_judice, "a fixture cobre candidatura sub judice"


def test_situacoes_de_deputado_e_percentual() -> None:
    arquivo = parse_result_file(_load("sim-sp-depfed.json"))
    por_situacao = {c.situacao: c for c in arquivo.candidatos}
    assert set(por_situacao) == {"Eleito por média", "Suplente", "Não eleito"}
    assert por_situacao["Eleito por média"].eleito is True
    assert por_situacao["Suplente"].eleito is False
    for c in arquivo.candidatos:
        assert isinstance(c.votos, int)
        assert isinstance(c.percentual, Decimal)


def test_arquivo_oficial_ainda_aberto() -> None:
    arquivo = parse_result_file(_load("oficial-br-presidente-aberto.json"))
    assert arquivo.final is False
    assert arquivo.atualizado_em is None  # dt/ht vazios antes da apuracao
    assert len(arquivo.candidatos) == 13
    # antes da apuracao `st` vem vazio: vira None, nunca string vazia
    assert all(c.situacao is None for c in arquivo.candidatos)
    assert 280002551544 in {c.sqcand for c in arquivo.candidatos}


def test_foi_eleito_nao_conta_quem_foi_ao_segundo_turno() -> None:
    # O TSE marca e="s" tambem para o 2o turno; eleito exige situacao "Eleito...".
    governador = parse_result_file(_load("sim-sp-governador.json"))
    assert sum(1 for c in governador.candidatos if c.eleito) == 2
    assert sum(1 for c in governador.candidatos if foi_eleito(c)) == 0

    deputados = parse_result_file(_load("sim-sp-depfed.json"))
    eleitos = [c for c in deputados.candidatos if foi_eleito(c)]
    assert [c.situacao for c in eleitos] == ["Eleito por média"]

    aberto = parse_result_file(_load("oficial-br-presidente-aberto.json"))
    assert not any(foi_eleito(c) for c in aberto.candidatos)


def test_percentual_apurado_vem_do_resumo_do_arquivo() -> None:
    # CS-127: "% de urnas apuradas" = secoes totalizadas (`s.pst`).
    parcial = _load("sim-sp-governador.json")
    parcial["tf"] = "n"
    parcial["s"] = {**(parcial.get("s") or {}), "pst": "47,26"}
    assert parse_result_file(parcial).percentual_apurado == Decimal("47.26")

    sem_resumo = _load("sim-sp-governador.json")
    sem_resumo.pop("s", None)
    assert parse_result_file(sem_resumo).percentual_apurado is None
