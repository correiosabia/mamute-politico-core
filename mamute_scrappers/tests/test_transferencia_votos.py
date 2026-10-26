"""CS-160: estimativa de transferencia de votos entre turnos (regressao ecologica)."""

from __future__ import annotations

import numpy as np
from sqlalchemy import text

from eleicao_schema import make_session
from mamute_scrappers.tse_crawler.transferencia_votos import calcular_estimativas, estimar_matriz

FIN_A, FIN_B, ELIM_1, ELIM_2, ELIM_3, ELIM_4 = 101, 102, 201, 202, 203, 204


def _sintetico(n: int = 400, seed: int = 7):
    rng = np.random.default_rng(seed)
    x = rng.dirichlet([2.0, 2.0, 1.0], size=n)
    matriz = np.array([[0.9, 0.05, 0.05], [0.1, 0.8, 0.1], [0.3, 0.3, 0.4]])
    return x, x @ matriz, matriz, rng.integers(1_000, 100_000, size=n).astype(float)


def test_estimar_recupera_matriz_conhecida() -> None:
    x, y, matriz, pesos = _sintetico()
    estimada = estimar_matriz(x, y, pesos)
    assert np.abs(estimada - matriz).max() < 0.02


def test_linhas_somam_um_e_nao_negativas() -> None:
    x, y, _, pesos = _sintetico()
    ruido = np.random.default_rng(1).normal(0, 0.02, size=y.shape)
    estimada = estimar_matriz(x, np.clip(y + ruido, 0, None), pesos)
    assert np.allclose(estimada.sum(axis=1), 1.0, atol=1e-6)
    assert (estimada >= -1e-9).all()


def _add_municipio(session, mid: int, turno: int, uf: str, cd_ibge, votos: dict, *,
                   brancos=10, nulos=10, abstencao=100, final=True) -> None:
    validos = sum(votos.values())
    comparecimento = validos + brancos + nulos
    session.execute(
        text(
            "INSERT INTO tse_resultado_municipio (ciclo, codigo_eleicao, turno, cargo_codigo, uf, cd_tse, "
            "cd_ibge, nome, eleitorado, comparecimento, abstencao, brancos, nulos, validos, totalizacao_final) "
            "VALUES ('ele2026', :e, :t, 1, :uf, :cd, :ibge, :nome, :el, :co, :ab, :b, :n, :v, :f)"
        ),
        {"e": 6256 + turno, "t": turno, "uf": uf, "cd": str(mid), "ibge": cd_ibge, "nome": f"M{mid}",
         "el": comparecimento + abstencao, "co": comparecimento, "ab": abstencao, "b": brancos,
         "n": nulos, "v": validos, "f": final},
    )
    rid = session.execute(
        text("SELECT id FROM tse_resultado_municipio WHERE cd_tse = :cd AND turno = :t"),
        {"cd": str(mid), "t": turno},
    ).scalar_one()
    for sq, v in votos.items():
        session.execute(
            text("INSERT INTO tse_resultado_municipio_candidato (resultado_id, sqcand, nome_urna, votos) "
                 "VALUES (:r, :s, :nm, :v)"),
            {"r": rid, "s": sq, "nm": f"C{sq}", "v": v},
        )


def _base(session, *, aberto: bool = False) -> None:
    session.execute(text(
        "INSERT INTO ibge_municipio (cd_ibge, nome, uf, macrorregiao) VALUES "
        "(1, 'A', 'SC', 'Sul'), (2, 'B', 'SC', 'Sul'), (3, 'C', 'BA', 'Nordeste'), (4, 'D', 'BA', 'Nordeste')"
    ))
    rng = np.random.default_rng(3)
    for mid in range(1, 41):
        ibge = (mid % 4) + 1
        uf = "sc" if ibge <= 2 else "ba"
        a, b, e1, e2, e3, e4 = (int(v) for v in rng.integers(50, 1000, size=6))
        _add_municipio(session, mid, 1, uf, ibge, {FIN_A: a, FIN_B: b, ELIM_1: e1, ELIM_2: e2, ELIM_3: e3, ELIM_4: e4 // 10})
        _add_municipio(session, mid, 2, uf, ibge, {FIN_A: a + e1, FIN_B: b + e2 + e3},
                       final=not (aberto and mid == 1))
    _add_municipio(session, 99, 1, "zz", None, {FIN_A: 10, FIN_B: 20, ELIM_1: 1, ELIM_2: 1, ELIM_3: 1, ELIM_4: 1})
    _add_municipio(session, 99, 2, "zz", None, {FIN_A: 11, FIN_B: 23})
    session.commit()


def test_calcula_brasil_e_macrorregioes() -> None:
    session = make_session()
    _base(session)
    gravadas = calcular_estimativas(session)
    assert gravadas > 0
    recortes = {
        (r.recorte, r.chave)
        for r in session.execute(text("SELECT DISTINCT recorte, chave FROM comparacao_turnos_estimativa")).all()
    }
    assert recortes == {("brasil", "BR"), ("macrorregiao", "Sul"), ("macrorregiao", "Nordeste")}
    origens = {
        r.origem
        for r in session.execute(text("SELECT DISTINCT origem FROM comparacao_turnos_estimativa")).all()
    }
    # finalistas + 3 maiores eliminados + demais + brancos/nulos + abstencao
    assert origens == {str(FIN_A), str(FIN_B), str(ELIM_1), str(ELIM_2), str(ELIM_3), "demais", "brancos_nulos", "abstencao"}
    linha = session.execute(
        text("SELECT sum(proporcao) FROM comparacao_turnos_estimativa "
             "WHERE recorte = 'brasil' AND origem = :o"), {"o": str(FIN_A)}
    ).scalar()
    assert abs(float(linha) - 1.0) < 1e-3


def test_nao_calcula_com_municipio_aberto() -> None:
    session = make_session()
    _base(session, aberto=True)
    assert calcular_estimativas(session) == 0
    assert session.execute(text("SELECT count(*) FROM comparacao_turnos_estimativa")).scalar() == 0


def test_recalculo_substitui_linhas() -> None:
    session = make_session()
    _base(session)
    primeira = calcular_estimativas(session)
    assert calcular_estimativas(session) == primeira
    assert session.execute(text("SELECT count(*) FROM comparacao_turnos_estimativa")).scalar() == primeira
