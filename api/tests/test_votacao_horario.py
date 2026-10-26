"""Votos depois das 17h (CS-163): serviço lido pela página Eleições 2026."""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api.services.votacao_horario import locais_do_municipio, votacao_horario


def _linha(votos, apos_17, comparecimento, estimado=0.0, **extra):
    return {
        "secoes": 10, "comparecimento": comparecimento, "votos": votos, "secoes_lidas": 3,
        "secoes_tardias": 2, "secoes_voto_apos_17": 2 if apos_17 else 0, "votos_apos_17": apos_17,
        "de_17_a_19": apos_17, "apos_19": 0, "ultimo_voto": None, "encerramento_max": None,
        "curva": {}, "votos_secoes_com_voto_apos_17": {}, "apos_17_proporcional": {},
        "amostra_secoes": 1, "amostra_apos_17": 0, "apos_17_estimado_nao_lidas": estimado, **extra,
    }  # fmt: skip


def _session(com_tabelas: bool = True) -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if com_tabelas:
        with engine.begin() as c:
            c.exec_driver_sql(
                "create table votacao_horario_agregado (id integer primary key, eleicao text, nivel text, uf text, "
                "municipio integer, nome text, latitude real, longitude real, secoes integer, comparecimento integer, "
                "votos_apos_17 integer, dados text, atualizado_em timestamp default '2026-10-26 12:00:00')"
            )
            c.exec_driver_sql(
                "create table votacao_horario_local (id integer primary key, eleicao text, uf text, municipio integer, "
                "zona integer, local integer, nome text, bairro text, endereco text, latitude real, longitude real, "
                "votos_apos_17 integer, dados text)"
            )
            c.exec_driver_sql(
                "create table candidacy (id integer primary key, election_year integer, office_code integer, "
                "ballot_number integer, ballot_name text)"
            )
            c.exec_driver_sql("insert into candidacy values (1, 2026, 1, 13, 'LULA'), (2, 2026, 1, 22, 'FLÁVIO BOLSONARO'),"
                              " (3, 2022, 1, 13, 'LULA'), (4, 2022, 1, 22, 'JAIR BOLSONARO')")

            def ag(eleicao, nivel, uf, mu, nome, linha, lat=None, lon=None):
                c.exec_driver_sql(
                    "insert into votacao_horario_agregado (eleicao, nivel, uf, municipio, nome, latitude, longitude, "
                    "secoes, comparecimento, votos_apos_17, dados) values (?,?,?,?,?,?,?,?,?,?,?)",
                    (eleicao, nivel, uf, mu, nome, lat, lon, linha["secoes"], linha["comparecimento"],
                     linha["votos_apos_17"], json.dumps(linha)),
                )  # fmt: skip

            ag("2026-2t", "brasil", None, None, "Brasil", _linha({"13": 600, "22": 400}, 30, 1000, estimado=5))
            ag("2026-2t", "uf", "sp", None, "SP", _linha({"13": 200, "22": 300}, 20, 500))
            ag("2026-2t", "uf", "ba", None, "BA", _linha({"13": 400, "22": 100}, 10, 500))
            ag("2026-2t", "municipio", "sp", 71072, "SÃO PAULO", _linha({"13": 200, "22": 300}, 20, 500), -23.5, -46.6)
            ag("2026-2t", "municipio", "sp", 70000, "SEM VOTO TARDIO", _linha({"13": 1, "22": 1}, 0, 2), -22.0, -47.0)
            ag("2026-1t", "brasil", None, None, "Brasil", _linha({"13": 500, "22": 450}, 12, 1000))
            ag("2026-1t", "uf", "sp", None, "SP", _linha({"13": 200, "22": 250}, 8, 500))
            ag("2026-1t", "municipio", "sp", 71072, "SÃO PAULO", _linha({"13": 200, "22": 250}, 8, 500), -23.5, -46.6)
            ag("2022-2t", "brasil", None, None, "Brasil", _linha({"13": 510, "22": 490}, 9, 1000))
            c.exec_driver_sql(
                "insert into votacao_horario_local (eleicao, uf, municipio, zona, local, nome, bairro, endereco, "
                "latitude, longitude, votos_apos_17, dados) values ('2026-2t','sp',71072,1,10,'EE A','Centro','Rua 1',"
                "-23.5,-46.6,20,?)",
                (json.dumps({"secoes": 2, "comparecimento": 200, "votos": {"13": 80, "22": 120}, "votos_apos_17": 20,
                             "de_17_a_19": 20, "apos_19": 0, "secoes_voto_apos_17": 1,
                             "ultimo_voto": "2026-10-25T17:40:00", "encerramento_max": "2026-10-25T17:41:00"}),),
            )  # fmt: skip
    return sessionmaker(bind=engine)()


def test_sem_tabela_responde_indisponivel():
    assert votacao_horario(_session(com_tabelas=False)) == {"disponivel": False}


def test_eleicoes_na_ordem_e_finalistas_pelo_numero():
    r = votacao_horario(_session())
    assert r["disponivel"] is True
    assert [e["chave"] for e in r["eleicoes"]] == ["2026-2t", "2026-1t", "2022-2t"]
    assert r["finalistas"]["2026-2t"] == [{"numero": "13", "nome": "Lula"}, {"numero": "22", "nome": "Flávio Bolsonaro"}]
    assert r["finalistas"]["2022-2t"][1] == {"numero": "22", "nome": "Jair Bolsonaro"}


def test_brasil_com_percentual_margem_e_teste_de_pior_caso():
    br = votacao_horario(_session())["brasil"]["2026-2t"]
    assert br["votos_apos_17"] == 30
    assert br["apos_17_total_estimado"] == 35.0
    assert br["apos_17_pct"] == 3.0  # 30 de 1000
    assert br["margem"] == {"vencedor": "13", "votos": 200}
    # mesmo que todos os votos depois das 17h fossem do vencedor, a diferença ficaria acima de zero
    assert br["margem"]["votos"] > br["apos_17_total_estimado"]
    assert br["poderia_inverter"] is False


def test_ufs_com_nome_e_as_tres_eleicoes():
    r = votacao_horario(_session())
    sp = next(u for u in r["ufs"] if u["uf"] == "SP")
    assert sp["nome"] == "São Paulo"
    assert set(sp["por_eleicao"]) == {"2026-2t", "2026-1t"}
    assert sp["por_eleicao"]["2026-2t"]["margem"] == {"vencedor": "22", "votos": 100}


def test_municipios_so_com_voto_depois_das_17h_e_base_do_1o_turno():
    r = votacao_horario(_session())
    assert [m["municipio"] for m in r["municipios"]] == [71072]
    m = r["municipios"][0]
    assert m["nome"] == "São Paulo" and m["uf"] == "SP"
    assert m["latitude"] == -23.5
    assert m["base"]["2026-1t"] == {"votos_apos_17": 8, "comparecimento": 500, "apos_17_pct": 1.6}


def test_locais_do_municipio():
    r = locais_do_municipio(_session(), "SP", 71072)
    assert r["municipio"]["nome"] == "São Paulo"
    assert r["locais"][0]["nome"] == "EE A"
    assert r["locais"][0]["votos_apos_17"] == 20
    assert r["locais"][0]["bairro"] == "Centro"
    assert locais_do_municipio(_session(), "SP", 1) is None
