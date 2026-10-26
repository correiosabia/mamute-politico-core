"""Agregacao dos votos depois das 17h (CS-163)."""

from __future__ import annotations

from datetime import datetime

from mamute_scrappers.tse_crawler.votacao_horario import SecaoBu
from mamute_scrappers.tse_crawler.votacao_horario_agregacao import LocalCadastro, agregar


def _sec(uf, mu, zo, local, se, enc_hora, enc_min, votos, comp=100, abre=8):
    return SecaoBu(
        uf=uf, municipio=mu, zona=zo, local=local, secao=se,
        abertura=datetime(2026, 10, 25, abre, 0), encerramento=datetime(2026, 10, 25, enc_hora, enc_min),
        comparecimento=comp, votos=votos, logs=["x"],
    )  # fmt: skip


def _log(uf, mu, zo, se, motivo, apos_17, de_17_a_19=None, apos_19=0, curva=None, ultimo="2026-10-25T17:40:00"):
    return {
        "uf": uf, "municipio": mu, "zona": zo, "secao": se, "motivo": motivo, "fuso": 0,
        "votos_log": 100, "apos_17": apos_17,
        "de_17_a_19": apos_17 - apos_19 if de_17_a_19 is None else de_17_a_19, "apos_19": apos_19,
        "ultimo_voto": ultimo if apos_17 else None, "por_minuto": curva or {},
    }  # fmt: skip


SECOES = [
    # SP: uma secao tardia (20 votos depois das 17h), duas no horario (uma na amostra, sem voto tardio)
    _sec("sp", 71072, 1, 10, 1, 17, 40, {"13": 40, "22": 60}),
    _sec("sp", 71072, 1, 10, 2, 17, 2, {"13": 50, "22": 50}),
    _sec("sp", 71072, 1, 20, 3, 17, 1, {"13": 70, "22": 30}),
    # BA: uma tardia com voto depois das 19h
    _sec("ba", 38490, 5, 30, 4, 19, 20, {"13": 80, "22": 20}),
]
LOGS = {
    ("sp", 71072, 1, 1): _log("sp", 71072, 1, 1, "tardia", 20, curva={"17:00": 12, "17:30": 8}),
    ("sp", 71072, 1, 2): _log("sp", 71072, 1, 2, "amostra", 0),
    ("ba", 38490, 5, 4): _log("ba", 38490, 5, 4, "tardia", 10, apos_19=4, curva={"17:10": 6, "19:10": 4}, ultimo="2026-10-25T19:12:00"),
}
CADASTRO = {
    ("sp", 71072, 1, 10): LocalCadastro("EE A", "Centro", "Rua 1", -23.5, -46.6, "SAO PAULO"),
    ("sp", 71072, 1, 20): LocalCadastro("EE B", "Mooca", "Rua 2", -23.55, -46.6, "SAO PAULO"),
    ("ba", 38490, 5, 30): LocalCadastro("EM C", "Liberdade", "Rua 3", -12.9, -38.5, "SALVADOR"),
}


def _por(agregados, nivel, chave=None):
    for a in agregados:
        if a["nivel"] == nivel and (chave is None or (a["uf"], a["municipio"]) == chave):
            return a
    raise AssertionError((nivel, chave))


def test_brasil_soma_secoes_votos_e_comparecimento():
    agregados, _ = agregar("2026-2t", SECOES, LOGS, CADASTRO)
    br = _por(agregados, "brasil")
    assert br["secoes"] == 4
    assert br["comparecimento"] == 400
    assert br["votos"] == {"13": 240, "22": 160}
    assert br["secoes_lidas"] == 3
    assert br["secoes_tardias"] == 2  # encerraram 17h05 ou depois
    assert br["secoes_voto_apos_17"] == 2
    assert br["votos_apos_17"] == 30
    assert br["de_17_a_19"] == 26
    assert br["apos_19"] == 4
    assert br["ultimo_voto"] == "2026-10-25T19:12:00"
    assert br["curva"] == {"17:00": 12, "17:10": 6, "17:30": 8, "19:10": 4}


def test_votos_das_secoes_com_voto_tardio_e_divisao_proporcional():
    agregados, _ = agregar("2026-2t", SECOES, LOGS, CADASTRO)
    br = _por(agregados, "brasil")
    assert br["votos_secoes_com_voto_apos_17"] == {"13": 120, "22": 80}
    # 20 votos tardios numa secao 40/60 e 10 numa secao 80/20
    assert br["apos_17_proporcional"] == {"13": 16.0, "22": 14.0}


def test_estimativa_da_amostra_para_as_secoes_nao_lidas():
    agregados, _ = agregar("2026-2t", SECOES, LOGS, CADASTRO)
    br = _por(agregados, "brasil")
    # amostra: 1 secao, 0 voto tardio -> nada a estimar nas nao lidas
    assert br["amostra_secoes"] == 1
    assert br["apos_17_estimado_nao_lidas"] == 0.0


def test_estimativa_usa_a_taxa_da_amostra():
    logs = dict(LOGS)
    logs[("sp", 71072, 1, 2)] = _log("sp", 71072, 1, 2, "amostra", 2)
    agregados, _ = agregar("2026-2t", SECOES, logs, CADASTRO)
    br = _por(agregados, "brasil")
    # 1 secao nao lida (a 3), taxa da amostra = 2 por secao
    assert br["apos_17_estimado_nao_lidas"] == 2.0


def test_uf_e_municipio_com_nome_e_coordenada_do_cadastro():
    agregados, _ = agregar("2026-2t", SECOES, LOGS, CADASTRO)
    sp = _por(agregados, "uf", ("sp", None))
    assert sp["votos_apos_17"] == 20 and sp["secoes"] == 3
    mu = _por(agregados, "municipio", ("sp", 71072))
    assert mu["nome"] == "SAO PAULO"
    assert mu["latitude"] == -23.525 and mu["longitude"] == -46.6


def test_locais_so_os_com_voto_depois_das_17h():
    _, locais = agregar("2026-2t", SECOES, LOGS, CADASTRO)
    assert [(l["uf"], l["local"]) for l in locais] == [("ba", 30), ("sp", 10)]
    a = next(l for l in locais if l["local"] == 10)
    assert a["nome"] == "EE A" and a["bairro"] == "Centro"
    assert a["secoes"] == 2 and a["votos_apos_17"] == 20
    assert a["encerramento_max"] == "2026-10-25T17:40:00"


def test_encerramento_vai_para_brasilia_pelo_fuso():
    # Acre: abre 6h, fecha 15h40 locais -> 17h40 de Brasilia (tardia)
    sec = [_sec("ac", 1392, 1, 5, 1, 15, 40, {"13": 1}, abre=6)]
    agregados, _ = agregar("2026-2t", sec, {}, {})
    assert _por(agregados, "brasil")["secoes_tardias"] == 1


# --- carga ---------------------------------------------------------------------

import json  # noqa: E402

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from mamute_scrappers.tse_crawler.votacao_horario_agregacao import gravar, ler_cadastro  # noqa: E402

CABECALHO = (
    "DT_GERACAO;NR_TURNO;SG_UF;CD_MUNICIPIO;NM_MUNICIPIO;NR_ZONA;NR_SECAO;NR_LOCAL_VOTACAO;"
    "NM_LOCAL_VOTACAO;DS_ENDERECO;NM_BAIRRO;NR_TELEFONE_LOCAL;NR_LATITUDE;NR_LONGITUDE"
)


def test_ler_cadastro_filtra_o_turno_e_le_coordenada_com_virgula(tmp_path):
    csv = tmp_path / "lv.csv"
    linhas = [
        CABECALHO,
        '"25/10/2026";"2";"AC";"01015";"CAPIXABA";"2";"67";"1015";"ESCOLA A";"AV. X";"CENTRO";"+55";"-10,57";"-67,67"',
        '"25/10/2026";"2";"AC";"01015";"CAPIXABA";"2";"68";"1015";"ESCOLA A";"AV. X";"CENTRO";"+55";"-10,57";"-67,67"',
        '"25/10/2026";"1";"AC";"01015";"CAPIXABA";"2";"69";"2000";"ESCOLA B";"AV. Y";"CENTRO";"+55";"-10,5";"-67,6"',
        '"25/10/2026";"2";"AC";"01015";"CAPIXABA";"2";"70";"3000";"ESCOLA C";"AV. Z";"VILA";"";"-1";"-1"',
    ]
    csv.write_text("\n".join(linhas), encoding="latin-1")
    cad = ler_cadastro(csv, turno=2)
    assert set(cad) == {("ac", 1015, 2, 1015), ("ac", 1015, 2, 3000)}
    a = cad[("ac", 1015, 2, 1015)]
    assert (a.nome, a.bairro, a.latitude, a.longitude, a.municipio_nome) == ("ESCOLA A", "CENTRO", -10.57, -67.67, "CAPIXABA")
    # -1 e o "sem coordenada" do TSE
    assert cad[("ac", 1015, 2, 3000)].latitude is None


def _banco() -> Session:
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with eng.begin() as c:
        c.execute(text("""CREATE TABLE votacao_horario_agregado (
            id INTEGER PRIMARY KEY AUTOINCREMENT, eleicao TEXT, nivel TEXT, uf TEXT, municipio INTEGER,
            nome TEXT, latitude REAL, longitude REAL, secoes INTEGER, comparecimento INTEGER,
            votos_apos_17 INTEGER, dados TEXT, atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""))
        c.execute(text("""CREATE TABLE votacao_horario_local (
            id INTEGER PRIMARY KEY AUTOINCREMENT, eleicao TEXT, uf TEXT, municipio INTEGER, zona INTEGER,
            local INTEGER, nome TEXT, bairro TEXT, endereco TEXT, latitude REAL, longitude REAL,
            votos_apos_17 INTEGER, dados TEXT)"""))
    return Session(eng)


def test_gravar_substitui_a_eleicao_inteira():
    db = _banco()
    agregados, locais = agregar("2026-2t", SECOES, LOGS, CADASTRO)
    gravar(db, "2026-2t", agregados, locais)
    gravar(db, "2026-2t", agregados, locais)  # de novo: nao duplica
    n = db.execute(text("SELECT count(*) FROM votacao_horario_agregado WHERE eleicao = '2026-2t'")).scalar()
    assert n == len(agregados)
    dados = db.execute(text("SELECT dados FROM votacao_horario_agregado WHERE nivel = 'brasil'")).scalar()
    assert json.loads(dados)["votos_apos_17"] == 30
    assert db.execute(text("SELECT count(*) FROM votacao_horario_local")).scalar() == 2
