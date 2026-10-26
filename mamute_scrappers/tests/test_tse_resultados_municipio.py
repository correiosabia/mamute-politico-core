"""CS-160: coleta do resultado de presidente por municipio (lista -> arquivos -> tabelas)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import text

from eleicao_schema import make_session
from mamute_scrappers.tse_crawler.resultados_municipio import coletar_municipios
from mamute_scrappers.tse_crawler.resultados_parsing import (
    municipio_file_url,
    municipios_url,
    parse_ibge,
    parse_municipios,
    parse_resumo_municipio,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tse_resultados"
BASE = "https://tse.test/oficial"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _ele_c() -> dict:
    """Config com as duas eleicoes de presidente de 2026."""
    return {
        "pl": [
            {"c": "ele2026", "dt": "04/10/2026", "e": [
                {"cd": "6257", "t": "1", "abr": [{"cp": [{"cd": "1"}]}]},
                {"cd": "6259", "t": "1", "abr": [{"cp": [{"cd": "3"}]}]},
            ]},
            {"c": "ele2026", "dt": "25/10/2026", "e": [
                {"cd": "6258", "t": "2", "abr": [{"cp": [{"cd": "1"}]}]},
            ]},
        ]
    }


def _final(payload: dict) -> dict:
    out = copy.deepcopy(payload)
    out["tf"] = "s"
    return out


class FakeTse:
    def __init__(self, arquivos: Dict[str, dict]) -> None:
        self.arquivos = {f"{BASE}/comum/config/ele-c.json": _ele_c(), **arquivos}
        self.chamadas: List[str] = []

    def __call__(self, url: str) -> Optional[dict]:
        self.chamadas.append(url)
        return self.arquivos.get(url)


def _fake_completo() -> FakeTse:
    cm = _load("mun-cm-mini.json")
    t1 = _final(_load("mun-sc81051-t1.json"))
    t2 = _final(_load("mun-sc81051-t2.json"))
    return FakeTse(
        {
            municipios_url(BASE, "ele2026", 6257): cm,
            municipios_url(BASE, "ele2026", 6258): cm,
            municipio_file_url(BASE, "ele2026", 6257, "sc", "81051", 1): t1,
            municipio_file_url(BASE, "ele2026", 6258, "sc", "81051", 1): t2,
        }
    )


class FakeIbge:
    def __init__(self) -> None:
        self.chamadas = 0

    def __call__(self) -> list:
        self.chamadas += 1
        return _load("ibge-mini.json")


def test_url_do_arquivo_do_municipio() -> None:
    assert municipio_file_url(BASE, "ele2026", 6258, "SC", "81051", 1) == (
        f"{BASE}/ele2026/6258/dados/sc/sc81051-c0001-e006258-u.json"
    )
    assert municipios_url(BASE, "ele2026", 6258) == f"{BASE}/ele2026/6258/config/mun-e006258-cm.json"


def test_parse_municipios_capital_e_exterior() -> None:
    municipios = {m.cd_tse: m for m in parse_municipios(_load("mun-cm-mini.json"))}
    floripa = municipios["81051"]
    assert (floripa.uf, floripa.cd_ibge, floripa.nome, floripa.capital) == ("sc", 4205407, "FLORIANÓPOLIS", True)
    assert municipios["81477"].capital is False
    exterior = [m for m in municipios.values() if m.uf == "zz"]
    assert len(exterior) == 1 and exterior[0].cd_ibge is None


def test_parse_resumo_florianopolis_t2() -> None:
    resumo = parse_resumo_municipio(_load("mun-sc81051-t2.json"))
    assert resumo.eleitorado == 423975
    assert resumo.comparecimento == 346830
    assert resumo.abstencao == 77145
    assert resumo.brancos == 3292
    assert resumo.nulos == 6428
    assert resumo.validos == 337110
    assert resumo.final is False
    assert len(resumo.candidatos) == 2
    lula = [c for c in resumo.candidatos if c.numero == 13][0]
    assert lula.votos == 162957 and lula.nome_urna == "LULA" and lula.sqcand == 280002542548


def test_parse_ibge_regiao_intermediaria() -> None:
    linhas = {l["cd_ibge"]: l for l in parse_ibge(_load("ibge-mini.json"))}
    assert linhas[4207502]["regiao_intermediaria_nome"] == "Blumenau"
    assert linhas[4207502]["uf"] == "SC"
    assert linhas[4205407]["macrorregiao"] == "Sul"


def test_parse_ibge_municipio_sem_microrregiao_usa_regiao_imediata() -> None:
    sem_micro = copy.deepcopy(_load("ibge-mini.json")[0])
    sem_micro["microrregiao"] = None
    linhas = parse_ibge([sem_micro])
    assert linhas[0]["uf"] == "SC" and linhas[0]["regiao_intermediaria_nome"] == "Florianópolis"


def test_coleta_grava_municipio_e_candidatos() -> None:
    session = make_session()
    stats = coletar_municipios(session, base=BASE, http_get=_fake_completo(), ibge_get=FakeIbge(), workers=2)

    assert stats.baixados == 2 and stats.finais == 2
    linhas = session.execute(
        text("SELECT turno, uf, cd_ibge, capital, eleitorado, abstencao, totalizacao_final "
             "FROM tse_resultado_municipio ORDER BY turno")
    ).all()
    assert [(l.turno, l.uf, l.cd_ibge, bool(l.capital), bool(l.totalizacao_final)) for l in linhas] == [
        (1, "sc", 4205407, True, True),
        (2, "sc", 4205407, True, True),
    ]
    assert linhas[1].eleitorado == 423975 and linhas[1].abstencao == 77145
    candidatos_t2 = session.execute(
        text("SELECT count(*) FROM tse_resultado_municipio_candidato c "
             "JOIN tse_resultado_municipio r ON r.id = c.resultado_id WHERE r.turno = 2")
    ).scalar()
    assert candidatos_t2 == 2


def test_coleta_404_conta_ausente_sem_linha() -> None:
    session = make_session()
    stats = coletar_municipios(session, base=BASE, http_get=_fake_completo(), ibge_get=FakeIbge(), workers=2)
    # cm-mini tem Indaial e 1 do exterior sem arquivo no fake, nos 2 turnos
    assert stats.ausentes == 4
    assert session.execute(
        text("SELECT count(*) FROM tse_resultado_municipio WHERE cd_tse <> '81051'")
    ).scalar() == 0


def test_coleta_pula_totalizado_e_refazer_rebaixa() -> None:
    session = make_session()
    coletar_municipios(session, base=BASE, http_get=_fake_completo(), ibge_get=FakeIbge(), workers=2)

    tse = _fake_completo()
    stats = coletar_municipios(session, base=BASE, http_get=tse, ibge_get=FakeIbge(), workers=2)
    assert stats.ja_finais == 2 and stats.baixados == 0
    assert not any("sc81051" in url for url in tse.chamadas)

    tse = _fake_completo()
    stats = coletar_municipios(session, base=BASE, http_get=tse, ibge_get=FakeIbge(), workers=2, refazer=True)
    assert stats.baixados == 2
    assert session.execute(text("SELECT count(*) FROM tse_resultado_municipio")).scalar() == 2
    assert session.execute(text("SELECT count(*) FROM tse_resultado_municipio_candidato")).scalar() == 14


def test_coleta_rebaixa_parcial() -> None:
    session = make_session()
    tse = _fake_completo()
    tse.arquivos[municipio_file_url(BASE, "ele2026", 6258, "sc", "81051", 1)] = _load("mun-sc81051-t2.json")
    coletar_municipios(session, base=BASE, http_get=tse, ibge_get=FakeIbge(), workers=2)

    tse = _fake_completo()
    stats = coletar_municipios(session, base=BASE, http_get=tse, ibge_get=FakeIbge(), workers=2)
    assert stats.baixados == 1 and stats.ja_finais == 1


def test_coleta_carrega_ibge_uma_vez() -> None:
    session = make_session()
    ibge = FakeIbge()
    coletar_municipios(session, base=BASE, http_get=_fake_completo(), ibge_get=ibge, workers=2)
    coletar_municipios(session, base=BASE, http_get=_fake_completo(), ibge_get=ibge, workers=2)
    assert ibge.chamadas == 1
    assert session.execute(text("SELECT count(*) FROM ibge_municipio")).scalar() == 2
