"""CS-106: coleta do resultado (config -> arquivos -> tse_result_file/candidacy_result)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import text

from eleicao_schema import add_candidacy, make_session
from mamute_scrappers.tse_crawler.resultados import coletar

FIXTURES = Path(__file__).parent / "fixtures" / "tse_resultados"
BASE = "https://tse.test/oficial"
GOV_SP = f"{BASE}/ele2026/6259/dados/sp/sp-c0003-e006259-u.json"
DEP_SP = f"{BASE}/ele2026/6259/dados/sp/sp-c0006-e006259-u.json"
DOMINGO = date(2026, 10, 4)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTse:
    def __init__(self, arquivos: Dict[str, dict]) -> None:
        self.arquivos = {f"{BASE}/comum/config/ele-c.json": _load("oficial-ele-c.json"), **arquivos}
        self.chamadas: List[str] = []

    def __call__(self, url: str) -> Optional[dict]:
        self.chamadas.append(url)
        return self.arquivos.get(url)


def _sessao_com_candidatos():
    session = make_session()
    add_candidacy(session, 1, 41627170, office_code=3)  # 2o turno
    add_candidacy(session, 2, 41627384, office_code=3)  # nao eleito
    add_candidacy(session, 3, 41627158, office_code=6)  # eleito por media
    add_candidacy(session, 4, 41627126, office_code=6)  # suplente
    add_candidacy(session, 5, 41627170, office_code=3, year=2022)  # outro ano, nao casa
    return session


def test_antes_do_pleito_nao_baixa_arquivo() -> None:
    session = _sessao_com_candidatos()
    tse = FakeTse({GOV_SP: _load("sim-sp-governador.json")})
    stats = coletar(session, base=BASE, http_get=tse, hoje=date(2026, 10, 1))
    assert stats.eleicoes == 2 and stats.eleicoes_futuras == 2
    assert tse.chamadas == [f"{BASE}/comum/config/ele-c.json"]
    assert session.execute(text("SELECT count(*) FROM tse_result_file")).scalar() == 0


def test_grava_arquivo_e_situacao_dos_casados() -> None:
    session = _sessao_com_candidatos()
    tse = FakeTse({GOV_SP: _load("sim-sp-governador.json"), DEP_SP: _load("sim-sp-depfed.json")})
    stats = coletar(session, base=BASE, http_get=tse, hoje=DOMINGO)

    assert stats.arquivos_baixados == 2
    assert stats.arquivos_finais_agora == 2
    assert stats.candidatos_casados == 4
    # 108 arquivos estaduais + 1 presidente; so 2 existem no fake
    assert stats.arquivos_ausentes == 107

    arquivos = session.execute(
        text("SELECT uf, cargo_codigo, turno, totalizacao_final, candidatos_no_arquivo, candidatos_casados "
             "FROM tse_result_file ORDER BY cargo_codigo")
    ).all()
    assert [(a.uf, a.cargo_codigo, a.turno, bool(a.totalizacao_final)) for a in arquivos] == [
        ("sp", 3, 1, True),
        ("sp", 6, 1, True),
    ]
    assert arquivos[0].candidatos_no_arquivo == 12 and arquivos[0].candidatos_casados == 2

    res = {
        r.candidacy_id: r
        for r in session.execute(
            text("SELECT candidacy_id, turno, situacao, eleito, votos, totalizacao_final FROM candidacy_result")
        ).all()
    }
    assert set(res) == {1, 2, 3, 4}
    assert res[1].situacao == "2º turno" and bool(res[1].eleito) is True
    assert res[2].situacao == "Não eleito" and bool(res[2].eleito) is False
    assert res[3].situacao == "Eleito por média"
    assert res[4].situacao == "Suplente"
    assert all(r.turno == 1 and bool(r.totalizacao_final) for r in res.values())
    assert res[1].votos > 0


def test_segunda_rodada_nao_baixa_arquivo_encerrado() -> None:
    session = _sessao_com_candidatos()
    tse = FakeTse({GOV_SP: _load("sim-sp-governador.json")})
    coletar(session, base=BASE, http_get=tse, hoje=DOMINGO)
    tse.chamadas.clear()
    stats = coletar(session, base=BASE, http_get=tse, hoje=DOMINGO)
    assert GOV_SP not in tse.chamadas
    assert stats.arquivos_ja_finais == 1
    assert session.execute(text("SELECT count(*) FROM candidacy_result")).scalar() == 2


def test_arquivo_aberto_e_baixado_de_novo_e_atualizado() -> None:
    session = _sessao_com_candidatos()
    parcial = _load("sim-sp-governador.json")
    parcial["tf"] = "n"
    tse = FakeTse({GOV_SP: parcial})
    coletar(session, base=BASE, http_get=tse, hoje=DOMINGO)
    assert session.execute(text("SELECT totalizacao_final FROM tse_result_file")).scalar() in (0, False)

    tse.arquivos[GOV_SP] = _load("sim-sp-governador.json")
    tse.chamadas.clear()
    coletar(session, base=BASE, http_get=tse, hoje=DOMINGO)
    assert GOV_SP in tse.chamadas
    assert session.execute(text("SELECT totalizacao_final FROM tse_result_file")).scalar() in (1, True)
    finais = session.execute(
        text("SELECT count(*) FROM candidacy_result WHERE totalizacao_final")
    ).scalar()
    assert finais == 2


def test_grava_percentual_apurado_durante_a_apuracao() -> None:
    # CS-127: o card mostra votos parciais com o % de urnas apuradas.
    session = _sessao_com_candidatos()
    parcial = _load("sim-sp-governador.json")
    parcial["tf"] = "n"
    parcial["s"] = {**(parcial.get("s") or {}), "pst": "51,51"}
    coletar(session, base=BASE, http_get=FakeTse({GOV_SP: parcial}), hoje=DOMINGO)
    assert float(session.execute(text("SELECT percentual_apurado FROM tse_result_file")).scalar()) == 51.51
    apurados = session.execute(
        text("SELECT DISTINCT percentual_apurado FROM candidacy_result")
    ).scalars().all()
    assert [float(v) for v in apurados] == [51.51]


def test_dry_run_nao_grava() -> None:
    session = _sessao_com_candidatos()
    tse = FakeTse({GOV_SP: _load("sim-sp-governador.json")})
    stats = coletar(session, base=BASE, http_get=tse, hoje=DOMINGO, dry_run=True)
    assert stats.candidatos_casados == 2
    assert session.execute(text("SELECT count(*) FROM tse_result_file")).scalar() == 0
    assert session.execute(text("SELECT count(*) FROM candidacy_result")).scalar() == 0


def test_filtro_de_uf_e_cargo() -> None:
    session = _sessao_com_candidatos()
    tse = FakeTse({GOV_SP: _load("sim-sp-governador.json")})
    coletar(session, base=BASE, http_get=tse, hoje=DOMINGO, ufs=["SP"], cargos=[3])
    assert tse.chamadas == [f"{BASE}/comum/config/ele-c.json", GOV_SP]


def test_conta_eleitos_do_arquivo_inteiro_casados_ou_nao() -> None:
    # CS-107: o perfil dos eleitos confere o total do TSE com o que achou na base.
    session = make_session()
    add_candidacy(session, 4, 41627126, office_code=6)  # suplente; o eleito nao esta na base
    tse = FakeTse({GOV_SP: _load("sim-sp-governador.json"), DEP_SP: _load("sim-sp-depfed.json")})
    coletar(session, base=BASE, http_get=tse, hoje=DOMINGO)
    eleitos = dict(
        session.execute(
            text("SELECT cargo_codigo, eleitos_no_arquivo FROM tse_result_file")
        ).all()
    )
    # 2o turno nao e eleito; o "Eleito por media" conta mesmo sem casar.
    assert eleitos == {3: 0, 6: 1}


def test_arquivo_encerrado_sem_contagem_de_eleitos_e_baixado_de_novo_uma_vez() -> None:
    session = _sessao_com_candidatos()
    tse = FakeTse({DEP_SP: _load("sim-sp-depfed.json")})
    coletar(session, base=BASE, http_get=tse, hoje=DOMINGO, cargos=[6])
    # Simula arquivo encerrado coletado antes da CS-107.
    session.execute(text("UPDATE tse_result_file SET eleitos_no_arquivo = NULL"))
    session.commit()

    tse.chamadas.clear()
    coletar(session, base=BASE, http_get=tse, hoje=DOMINGO, cargos=[6])
    assert DEP_SP in tse.chamadas
    assert session.execute(text("SELECT eleitos_no_arquivo FROM tse_result_file")).scalar() == 1

    tse.chamadas.clear()
    coletar(session, base=BASE, http_get=tse, hoje=DOMINGO, cargos=[6])
    assert DEP_SP not in tse.chamadas


def test_coleta_segue_sem_a_coluna_de_eleitos_antes_da_migration() -> None:
    # Janela do deploy: containers novos rodando contra o schema da cs106.
    session = make_session()
    session.execute(text("ALTER TABLE tse_result_file DROP COLUMN eleitos_no_arquivo"))
    session.commit()
    add_candidacy(session, 3, 41627158, office_code=6)
    tse = FakeTse({DEP_SP: _load("sim-sp-depfed.json")})
    stats = coletar(session, base=BASE, http_get=tse, hoje=DOMINGO, cargos=[6])
    assert stats.candidatos_casados == 1
    assert session.execute(text("SELECT totalizacao_final FROM tse_result_file")).scalar() in (1, True)

    tse.chamadas.clear()
    coletar(session, base=BASE, http_get=tse, hoje=DOMINGO, cargos=[6])
    assert DEP_SP not in tse.chamadas
