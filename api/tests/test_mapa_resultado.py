"""Resultado por município e por local de votação para o zoom dos mapas (CS-164)."""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api.services.mapa_resultado import locais_resultado, municipios_resultado


def _session(com_tabelas: bool = True) -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if com_tabelas:
        with engine.begin() as c:
            c.exec_driver_sql(
                "create table votacao_horario_agregado (id integer primary key, eleicao text, nivel text, uf text, "
                "municipio integer, nome text, latitude real, longitude real, secoes integer, comparecimento integer, "
                "votos_apos_17 integer, dados text, atualizado_em timestamp)"
            )
            c.exec_driver_sql(
                "create table resultado_local_votacao (id integer primary key, eleicao text, uf text, municipio integer, "
                "zona integer, local integer, nome text, bairro text, endereco text, secoes integer, latitude real, "
                "longitude real, comparecimento integer, validos integer, votos_13 integer, votos_22 integer)"
            )
            for eleicao, votos in (("2026-2t", {"13": 60, "22": 40, "branco": 5}), ("2026-1t", {"13": 50, "22": 30, "30": 20})):
                c.exec_driver_sql(
                    "insert into votacao_horario_agregado (eleicao, nivel, uf, municipio, nome, latitude, longitude, "
                    "secoes, comparecimento, votos_apos_17, dados) values (?, 'municipio', 'sc', 81051, 'FLORIANÓPOLIS', "
                    "-27.6, -48.5, 1, 105, 0, ?)",
                    (eleicao, json.dumps({"votos": votos, "secoes": 1, "comparecimento": 105})),
                )
            c.exec_driver_sql(
                "insert into votacao_horario_agregado (eleicao, nivel, uf, municipio, nome, latitude, longitude, secoes, "
                "comparecimento, votos_apos_17, dados) values ('2022-2t', 'municipio', 'sc', 81051, 'X', 0, 0, 1, 1, 0, '{}')"
            )
            locais = [
                ("2026-2t", 1, -27.59, -48.55, 70, 30, 100),
                ("2026-1t", 1, -27.59, -48.55, 40, 35, 90),
                ("2026-2t", 2, -27.60, -48.50, 10, 20, 30),
                ("2026-2t", 3, -23.5, -46.6, 1, 1, 2),  # fora da área
            ]
            for eleicao, local, lat, lon, v13, v22, val in locais:
                c.exec_driver_sql(
                    "insert into resultado_local_votacao (eleicao, uf, municipio, zona, local, nome, bairro, endereco, "
                    "secoes, latitude, longitude, comparecimento, validos, votos_13, votos_22) values (?, 'sc', 81051, "
                    "12, ?, 'EEB X', 'Centro', 'RUA A, 10', 3, ?, ?, ?, ?, ?, ?)",
                    (eleicao, local, lat, lon, val, val, v13, v22),
                )
    return sessionmaker(bind=engine)()


def test_sem_tabela():
    assert municipios_resultado(_session(False)) == {"disponivel": False}
    assert locais_resultado(_session(False), -49, -28, -48, -27) == {"locais": [], "truncado": False}


def test_municipios_com_os_dois_turnos_em_lista_enxuta():
    r = municipios_resultado(_session())
    assert r["disponivel"] is True
    assert r["campos"] == [
        "uf", "municipio", "nome", "lat", "lon", "v13", "v22", "validos", "v13_1t", "v22_1t", "validos_1t",
        "secoes", "comparecimento",
    ]  # fmt: skip
    assert r["municipios"] == [["SC", 81051, "Florianópolis", -27.6, -48.5, 60, 40, 100, 50, 30, 100, 1, 105]]


def test_locais_so_na_area_pedida_com_o_1o_turno():
    r = locais_resultado(_session(), -48.6, -27.7, -48.4, -27.5)
    assert r["truncado"] is False
    assert r["campos"][:4] == ["uf", "municipio", "zona", "local"]
    linhas = {l[3]: dict(zip(r["campos"], l)) for l in r["locais"]}
    assert set(linhas) == {1, 2}
    um = linhas[1]
    assert [um[c] for c in ("v13", "v22", "validos", "v13_1t", "v22_1t", "validos_1t")] == [70, 30, 100, 40, 35, 90]
    assert [linhas[2][c] for c in ("v13_1t", "v22_1t", "validos_1t")] == [None, None, None]


def test_local_traz_endereco_secoes_e_comparecimento_para_o_detalhe():
    r = locais_resultado(_session(), -48.6, -27.7, -48.4, -27.5)
    um = next(dict(zip(r["campos"], l)) for l in r["locais"] if l[3] == 1)
    assert (um["endereco"], um["secoes"], um["comparecimento"]) == ("RUA A, 10", 3, 100)


def test_area_grande_demais_nao_lista():
    r = locais_resultado(_session(), -60, -35, -30, 5)
    assert r == {"locais": [], "truncado": True}
