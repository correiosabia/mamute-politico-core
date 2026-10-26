"""Leitura dos arquivos de urna do TSE: BU e log (CS-163)."""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from pathlib import Path

import py7zr
import pytest

from mamute_scrappers.tse_crawler.urna_parsing import (
    classificar_votos,
    extrair_log,
    fuso_do_municipio,
    parse_bu,
    votos_do_log,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tse_urna"


def _bu(nome: str) -> bytes:
    return (FIXTURES / nome).read_bytes()


def test_bu_2026_traz_horarios_local_e_votos_de_presidente():
    bu = parse_bu(_bu("bu_2026_2t_ac_01074_0004_0001.dat"), cd_eleicao=6258)
    assert (bu.municipio, bu.zona, bu.local, bu.secao) == (1074, 4, 1015, 1)
    assert bu.abertura == datetime(2026, 10, 25, 6, 0, 1)
    assert bu.encerramento == datetime(2026, 10, 25, 15, 1, 52)
    assert bu.comparecimento == 202
    assert bu.votos == {"13": 68, "22": 127, "branco": 3, "nulo": 4}


def test_bu_2022_mesmo_leitor():
    bu = parse_bu(_bu("bu_2022_2t_ac_01015_0002_0088.dat"), cd_eleicao=545)
    assert (bu.municipio, bu.zona, bu.local, bu.secao) == (1015, 2, 1112, 88)
    assert bu.abertura == datetime(2022, 10, 30, 6, 8, 36)
    assert bu.encerramento == datetime(2022, 10, 30, 15, 7, 26)
    assert bu.comparecimento == 139
    assert bu.votos["13"] == 53 and bu.votos["22"] == 84
    assert bu.votos["branco"] == 1 and bu.votos["nulo"] == 1


def test_bu_sem_a_eleicao_pedida_da_erro():
    with pytest.raises(ValueError):
        parse_bu(_bu("bu_2026_2t_ac_01074_0004_0001.dat"), cd_eleicao=9999)


LOG = "\n".join(
    [
        "04/10/2026 16:59:00\tINFO\t1\tVOTA\tO voto do eleitor foi computado\tA",
        "25/10/2026 06:00:01\tINFO\t1\tGAP\tMR gerada pelo computador: X\tB",
        "25/10/2026 14:59:59\tINFO\t1\tVOTA\tO voto do eleitor foi computado\tC",
        "25/10/2026 15:00:00\tINFO\t1\tVOTA\tO voto do eleitor foi computado\tD",
        "25/10/2026 16:59:59\tINFO\t1\tVOTA\tO voto do eleitor foi computado\tE",
        "25/10/2026 17:10:00\tINFO\t1\tVOTA\tO voto do eleitor foi computado\tF",
    ]
)


def test_votos_do_log_pega_so_o_dia_da_eleicao_e_a_frase_exata():
    votos = votos_do_log(LOG, dia=date(2026, 10, 25))
    assert votos == [
        datetime(2026, 10, 25, 14, 59, 59),
        datetime(2026, 10, 25, 15, 0, 0),
        datetime(2026, 10, 25, 16, 59, 59),
        datetime(2026, 10, 25, 17, 10, 0),
    ]


def test_extrair_log_le_zip_de_2026():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("logd.dat", LOG.encode("latin-1"))
    assert extrair_log(buf.getvalue()) == LOG


def test_extrair_log_le_7z_de_2022(tmp_path):
    caminho = tmp_path / "x.logjez"
    with py7zr.SevenZipFile(caminho, "w") as z:
        z.writestr(LOG.encode("latin-1"), "logd.dat")
    assert extrair_log(caminho.read_bytes()) == LOG


def test_fuso_sai_da_abertura_mais_comum_do_municipio():
    # Acre: abre as 6h locais = 8h de Brasilia. Uma urna de contingencia
    # aberta as 10h nao muda o fuso.
    aberturas = [datetime(2026, 10, 25, 6, 0, 1)] * 5 + [datetime(2026, 10, 25, 10, 12)]
    assert fuso_do_municipio(aberturas) == 2
    assert fuso_do_municipio([datetime(2026, 10, 25, 7, 59, 58), datetime(2026, 10, 25, 8, 0, 3)]) == 0
    assert fuso_do_municipio([datetime(2026, 10, 25, 7, 0, 2)]) == 1
    assert fuso_do_municipio([datetime(2026, 10, 25, 9, 0, 0)]) == -1  # Fernando de Noronha
    assert fuso_do_municipio([]) == 0


def test_classificar_votos_em_horario_de_brasilia():
    votos = [
        datetime(2026, 10, 25, 14, 59, 59),
        datetime(2026, 10, 25, 15, 0, 0),
        datetime(2026, 10, 25, 16, 59, 59),
        datetime(2026, 10, 25, 17, 10, 0),
    ]
    faixas = classificar_votos(votos, fuso=2)
    assert faixas.total == 4
    assert faixas.apos_17 == 3
    assert faixas.de_17_a_19 == 2
    assert faixas.apos_19 == 1
    assert faixas.ultimo == datetime(2026, 10, 25, 19, 10, 0)
