"""CS-160: estimativa de transferencia de votos entre turnos (regressao ecologica)."""

from __future__ import annotations

import numpy as np
from sqlalchemy import text

from eleicao_schema import make_session
from mamute_scrappers.tse_crawler.transferencia_votos import (
    REGULARIZACAO,
    PriorLinha,
    calcular_estimativas,
    carregar_prior,
    estimar_com_incerteza,
    estimar_matriz,
)

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
    gravadas = calcular_estimativas(session, reamostras=5, prior={})
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
    # eliminados juntos: separados, o dado nao distingue um do outro (CS-161)
    assert origens == {str(FIN_A), str(FIN_B), "outros_candidatos", "brancos_nulos", "abstencao"}
    linha = session.execute(
        text("SELECT sum(proporcao) FROM comparacao_turnos_estimativa "
             "WHERE recorte = 'brasil' AND origem = :o"), {"o": str(FIN_A)}
    ).scalar()
    assert abs(float(linha) - 1.0) < 1e-3


def test_nao_calcula_com_municipio_aberto() -> None:
    session = make_session()
    _base(session, aberto=True)
    assert calcular_estimativas(session, reamostras=5, prior={}) == 0
    assert session.execute(text("SELECT count(*) FROM comparacao_turnos_estimativa")).scalar() == 0


def test_recalculo_substitui_linhas() -> None:
    session = make_session()
    _base(session)
    primeira = calcular_estimativas(session, reamostras=5, prior={})
    assert calcular_estimativas(session, reamostras=5, prior={}) == primeira
    assert session.execute(text("SELECT count(*) FROM comparacao_turnos_estimativa")).scalar() == primeira


def test_nao_calcula_quando_falta_municipio_no_segundo_turno() -> None:
    session = make_session()
    _base(session)
    session.execute(text("DELETE FROM tse_resultado_municipio WHERE turno = 2 AND cd_tse = '5'"))
    session.commit()
    assert calcular_estimativas(session, reamostras=5, prior={}) == 0


def test_regularizado_ainda_recupera_matriz_conhecida() -> None:
    x, y, matriz, pesos = _sintetico()
    estimada = estimar_matriz(x, y, pesos, regularizacao=REGULARIZACAO)
    assert np.abs(estimada - matriz).max() < 0.03


def test_regularizacao_leva_origem_sem_informacao_para_a_media() -> None:
    # Terceira origem constante entre municipios: o dado nao diz nada sobre ela.
    rng = np.random.default_rng(11)
    a = rng.uniform(0.2, 0.7, size=300)
    x = np.column_stack([a, 0.95 - a, np.full(300, 0.05)])
    matriz = np.array([[0.9, 0.1], [0.1, 0.9], [0.5, 0.5]])
    y = np.clip(x @ matriz + rng.normal(0, 0.01, size=(300, 2)), 0, None)
    pesos = np.ones(300)
    media = (y * pesos[:, None]).sum(0) / pesos.sum()
    com = estimar_matriz(x, y, pesos, regularizacao=REGULARIZACAO)
    assert np.abs(com[2] - media).max() < 0.05
    # linhas bem informadas quase nao se mexem
    assert com[0, 0] > 0.85 and com[1, 1] > 0.85


def test_faixa_estreita_sem_ruido_e_cobre_sensibilidade() -> None:
    x, y, matriz, pesos = _sintetico()
    valor, minimo, maximo = estimar_com_incerteza(x, y, pesos, reamostras=20, semente=1)
    assert (minimo <= valor + 1e-9).all() and (valor <= maximo + 1e-9).all()
    assert (maximo - minimo).max() < 0.05
    dobro = estimar_matriz(x, y, pesos, regularizacao=REGULARIZACAO * 2)
    assert (minimo <= dobro + 1e-9).all() and (dobro <= maximo + 1e-9).all()


def test_prior_permite_troca_de_lado() -> None:
    # Sem prior a regressao crava 100% de fidelidade; com prior de pesquisa
    # (95% fiel, 3% troca) a troca de lado continua possivel.
    x, y, matriz, pesos = _sintetico()
    prior = np.array([[0.95, 0.03, 0.02], [0.03, 0.95, 0.02], [0.4, 0.4, 0.2]])
    sem = estimar_matriz(x, y, pesos)
    com = estimar_matriz(x, y, pesos, regularizacao=0.5, prior=prior)
    assert np.abs(com - prior).sum() < np.abs(sem - prior).sum()
    assert com[0, 1] > 0.0 and com[1, 0] > 0.0


def test_carregar_prior_normaliza_e_guarda_fonte(tmp_path) -> None:
    arquivo = tmp_path / "prior.json"
    arquivo.write_text(
        '{"linhas": [{"origem": "201", "destinos": {"101": 30, "102": 50, "brancos_nulos": 20}, '
        '"forca": 0.2, "fonte": "Instituto X, 20/10"}]}',
        encoding="utf-8",
    )
    prior = carregar_prior("ele2026", arquivo)
    assert prior["201"].destinos == {"101": 0.3, "102": 0.5, "brancos_nulos": 0.2}
    assert prior["201"].forca == 0.2 and prior["201"].fonte == "Instituto X, 20/10"
    assert carregar_prior("ele2026", tmp_path / "nao-existe.json") == {}


def test_eliminado_com_prior_fica_separado_e_grava_fonte() -> None:
    session = make_session()
    _base(session)
    prior = {str(ELIM_1): PriorLinha({str(FIN_A): 0.6, str(FIN_B): 0.3, "brancos_nulos": 0.1}, 0.1, "Pesquisa Y")}
    calcular_estimativas(session, reamostras=3, prior=prior)
    origens = {
        r.origem: r.fonte_prior
        for r in session.execute(
            text("SELECT DISTINCT origem, fonte_prior FROM comparacao_turnos_estimativa WHERE recorte = 'brasil'")
        ).all()
    }
    assert origens[str(ELIM_1)] == "Pesquisa Y"
    assert "outros_candidatos" in origens and origens["outros_candidatos"] is None
    linha = session.execute(
        text("SELECT proporcao_prior FROM comparacao_turnos_estimativa "
             "WHERE recorte = 'brasil' AND origem = :o AND destino = :d"),
        {"o": str(ELIM_1), "d": str(FIN_A)},
    ).scalar()
    assert abs(float(linha) - 0.6) < 1e-4


def test_grava_faixa_de_incerteza() -> None:
    session = make_session()
    _base(session)
    calcular_estimativas(session, reamostras=5, prior={})
    linhas = session.execute(
        text("SELECT proporcao, proporcao_min, proporcao_max FROM comparacao_turnos_estimativa")
    ).all()
    assert linhas and all(l.proporcao_min <= l.proporcao + 1e-4 <= l.proporcao_max + 2e-4 for l in linhas)


def test_brasil_e_a_soma_das_regioes_pesada_pelos_votos() -> None:
    # Estratificacao: o Brasil nao e uma regressao nacional unica (vies de agregacao).
    session = make_session()
    _base(session)
    calcular_estimativas(session, reamostras=3, prior={})
    def prop(recorte, chave, origem, destino):
        return float(session.execute(
            text("SELECT proporcao FROM comparacao_turnos_estimativa WHERE recorte = :r AND chave = :c "
                 "AND origem = :o AND destino = :d"), {"r": recorte, "c": chave, "o": origem, "d": destino}
        ).scalar())
    votos = {
        reg: session.execute(
            text("SELECT sum(c.votos) FROM tse_resultado_municipio_candidato c "
                 "JOIN tse_resultado_municipio r ON r.id = c.resultado_id "
                 "JOIN ibge_municipio i ON i.cd_ibge = r.cd_ibge "
                 "WHERE r.turno = 1 AND c.sqcand = :s AND i.macrorregiao = :m"), {"s": FIN_A, "m": reg}
        ).scalar()
        for reg in ("Sul", "Nordeste")
    }
    o, d = str(FIN_A), str(FIN_A)
    esperado = sum(prop("macrorregiao", reg, o, d) * v for reg, v in votos.items()) / sum(votos.values())
    assert abs(prop("brasil", "BR", o, d) - esperado) < 2e-4
