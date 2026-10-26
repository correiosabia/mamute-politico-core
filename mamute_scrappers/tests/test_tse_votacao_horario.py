"""Coletor dos arquivos de urna para votos depois das 17h (CS-163)."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime

import pytest

from mamute_scrappers.tse_crawler.votacao_horario import (
    MembroZip,
    SecaoBu,
    arquivos_da_secao,
    fusos_por_municipio,
    ler_membro_zip,
    secao_do_nome_pacote,
    secoes_do_config,
    selecionar_para_log,
)


def test_secoes_do_config_lista_municipio_zona_secao():
    cs = {
        "abr": [
            {
                "cd": "ac",
                "mu": [
                    {"cd": "01074", "zon": [{"cd": "0004", "sec": [{"ns": "0001"}, {"ns": "0005"}]}]},
                ],
            }
        ]
    }
    assert secoes_do_config(cs) == [("ac", 1074, 4, 1), ("ac", 1074, 4, 5)]


def test_arquivos_da_secao_pega_bu_e_todos_os_logs_validos():
    aux = {
        "hashes": [
            {"hash": "aa", "st": "Totalizado", "arq": [{"nm": "a-bu.dat", "tp": "bu"}, {"nm": "a-log.jez", "tp": "log"}]},
            {"hash": "bb", "st": "Excluído", "arq": [{"nm": "b-bu.dat", "tp": "bu"}, {"nm": "b-log.jez", "tp": "log"}]},
            {"hash": "cc", "st": "Recebido", "arq": [{"nm": "c-log.jez", "tp": "log"}]},
        ]
    }
    bu, logs = arquivos_da_secao(aux)
    assert bu == "aa/a-bu.dat"
    assert logs == ["aa/a-log.jez", "cc/c-log.jez"]


def test_arquivos_da_secao_sem_bu():
    assert arquivos_da_secao({"hashes": []}) == (None, [])


def test_secao_do_nome_pacote_2022_e_2026():
    assert secao_do_nome_pacote("o00407-0101500020088.bu") == (1015, 2, 88, "bu")
    assert secao_do_nome_pacote("o00407-0101500020088.logjez") == (1015, 2, 88, "log")
    assert secao_do_nome_pacote("o03220ac0101500020088-bu.dat") == (1015, 2, 88, "bu")
    assert secao_do_nome_pacote("o03220ac0101500020088-log.jez") == (1015, 2, 88, "log")
    assert secao_do_nome_pacote("o03220ac0101500020088-rdv.dat") == (1015, 2, 88, "rdv")
    assert secao_do_nome_pacote("leiame.pdf") is None


def test_ler_membro_zip_por_faixa_de_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("um.txt", b"primeiro" * 50)
        z.writestr("dois.bu", b"segundo arquivo")
    dados = buf.getvalue()
    infos = {i.filename: i for i in zipfile.ZipFile(io.BytesIO(dados)).infolist()}
    pedidos = []

    def faixa(inicio: int, fim: int) -> bytes:
        pedidos.append((inicio, fim))
        return dados[inicio : fim + 1]

    i = infos["dois.bu"]
    membro = MembroZip(i.filename, i.header_offset, i.compress_size, i.compress_type)
    assert ler_membro_zip(membro, faixa) == b"segundo arquivo"
    assert len(pedidos) == 1


def _secao(municipio, abertura, encerramento, secao=1):
    return SecaoBu(
        uf="ac", municipio=municipio, zona=1, local=1, secao=secao,
        abertura=abertura, encerramento=encerramento, comparecimento=100,
        votos={"13": 50, "22": 50}, logs=["x"],
    )  # fmt: skip


def test_fusos_por_municipio_e_selecao_em_horario_de_brasilia():
    acre = [
        _secao(1, datetime(2026, 10, 25, 6, 0), datetime(2026, 10, 25, 15, 0, 30), secao=1),
        _secao(1, datetime(2026, 10, 25, 6, 0), datetime(2026, 10, 25, 15, 40), secao=2),
    ]
    sp = [_secao(2, datetime(2026, 10, 25, 8, 0), datetime(2026, 10, 25, 17, 0, 20), secao=3)]
    fusos = fusos_por_municipio(acre + sp)
    assert fusos == {("ac", 1): 2, ("ac", 2): 0}
    escolhidas = selecionar_para_log(acre + sp, fusos, limiar=(17, 1), amostra=0.0)
    assert [(s.secao, motivo) for s, motivo in escolhidas] == [(2, "tardia")]


def test_selecao_inclui_amostra_das_que_fecharam_no_horario():
    secoes = [_secao(2, datetime(2026, 10, 25, 8, 0), datetime(2026, 10, 25, 17, 0, 20), secao=n) for n in range(200)]
    escolhidas = selecionar_para_log(secoes, {("ac", 2): 0}, limiar=(17, 1), amostra=0.05, semente=1)
    assert 3 <= len(escolhidas) <= 20
    assert all(m == "amostra" for _, m in escolhidas)


def test_limitador_espaca_os_pedidos():
    from mamute_scrappers.tse_crawler.votacao_horario import Limitador

    relogio = [0.0]
    esperas = []

    def dormir(s):
        esperas.append(s)
        relogio[0] += s

    lim = Limitador(10, relogio=lambda: relogio[0], dormir=dormir)
    for _ in range(5):
        lim.aguardar()
    # 10 por segundo: um pedido a cada 0,1 s
    assert relogio[0] == pytest.approx(0.4)
