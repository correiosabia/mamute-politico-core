"""CS-128: resultado majoritario definido matematicamente antes do TSE encerrar."""

from __future__ import annotations

from mamute_scrappers.tse_crawler.resultados_parsing import (
    CARGO_DEP_FEDERAL,
    CARGO_GOVERNADOR,
    CARGO_PRESIDENTE,
    CARGO_SENADOR,
    definicao_matematica,
    parse_result_file,
)


def _payload(votos, *, restante, vagas=1, md=None, tf="n", vvc=None, sub_judice=()):
    cands = [
        {
            "sqcand": str(i + 1),
            "e": "n",
            "st": "",
            "vap": str(v),
            "pvap": "0,00",
            "dvt": "Anulado sub judice" if i + 1 in sub_judice else "Válido",
        }
        for i, v in enumerate(votos)
    ]
    validos = sum(votos)  # vvc: validos + anulados sub judice
    payload = {
        "t": "1",
        "tf": tf,
        "carg": [{"nv": str(vagas), "agr": [{"par": [{"cand": cands}]}]}],
        "s": {"pst": "99,99"},
        "e": {"esnt": str(restante)},
        "v": {"vvc": str(vvc if vvc is not None else validos)},
    }
    if md:
        payload["md"] = md
    return parse_result_file(payload)


def test_governador_eleito_quando_tse_e_a_conta_concordam() -> None:
    arquivo = _payload([5580, 4373, 47], restante=4, md="e")
    assert definicao_matematica(arquivo, CARGO_GOVERNADOR) == {1: "Eleito", 2: "Não eleito", 3: "Não eleito"}


def test_governador_sem_a_marca_do_tse_nao_vale() -> None:
    arquivo = _payload([5580, 4373, 47], restante=4)
    assert definicao_matematica(arquivo, CARGO_GOVERNADOR) is None


def test_marca_do_tse_que_a_conta_nao_confirma_nao_vale() -> None:
    # 5100 de 10000 + 300 restantes: com tudo contra, cai para 49,6%.
    arquivo = _payload([5100, 4900], restante=300, md="e")
    assert definicao_matematica(arquivo, CARGO_GOVERNADOR) is None


def test_presidente_segundo_turno() -> None:
    arquivo = _payload([4703, 4516, 289, 224], restante=1, md="s")
    assert definicao_matematica(arquivo, CARGO_PRESIDENTE) == {
        1: "2º turno",
        2: "2º turno",
        3: "Não eleito",
        4: "Não eleito",
    }


def test_segundo_turno_indefinido_se_o_terceiro_alcanca_o_segundo() -> None:
    arquivo = _payload([4000, 3000, 2950], restante=60, md="s")
    assert definicao_matematica(arquivo, CARGO_GOVERNADOR) is None


def test_ninguem_definido_enquanto_alguem_pode_passar_de_50() -> None:
    arquivo = _payload([4990, 4000, 1010], restante=100, md="s")
    assert definicao_matematica(arquivo, CARGO_GOVERNADOR) is None


def test_senado_duas_vagas() -> None:
    arquivo = _payload([4335, 3986, 2899, 2879], restante=10, vagas=2)
    assert definicao_matematica(arquivo, CARGO_SENADOR) == {
        1: "Eleito",
        2: "Eleito",
        3: "Não eleito",
        4: "Não eleito",
    }


def test_senado_segunda_vaga_apertada_fica_indefinida() -> None:
    arquivo = _payload([4335, 2014, 1998], restante=16, vagas=2)
    assert definicao_matematica(arquivo, CARGO_SENADOR) is None
    arquivo = _payload([4335, 2014, 1998], restante=15, vagas=2)
    assert definicao_matematica(arquivo, CARGO_SENADOR) is not None


def test_sub_judice_em_posicao_decisiva_deixa_indefinido() -> None:
    # Se a candidatura for liberada, os votos dela contam: nao da para cravar.
    arquivo = _payload([4335, 5000, 3986, 100], restante=10, vagas=2, sub_judice=(2,))
    assert definicao_matematica(arquivo, CARGO_SENADOR) is None


def test_sub_judice_fora_das_vagas_nao_atrapalha() -> None:
    arquivo = _payload([4335, 3986, 100, 50], restante=10, vagas=2, sub_judice=(3,))
    assert definicao_matematica(arquivo, CARGO_SENADOR) == {
        1: "Eleito",
        2: "Eleito",
        3: "Não eleito",
        4: "Não eleito",
    }


def test_maioria_absoluta_conta_os_votos_sub_judice() -> None:
    # Caso real do RJ 2026: 50,88% dos validos puros, 49,27% com os sub judice.
    arquivo = _payload(
        [4271199, 3706984, 274411, 235347], restante=0, md="s", vvc=8669038, sub_judice=(3,)
    )
    assert definicao_matematica(arquivo, CARGO_GOVERNADOR) == {
        1: "2º turno",
        2: "2º turno",
        3: "Não eleito",
        4: "Não eleito",
    }


def test_arquivo_encerrado_ou_proporcional_nao_usa_a_regra() -> None:
    assert definicao_matematica(_payload([10, 1], restante=0, md="e", tf="s"), CARGO_GOVERNADOR) is None
    assert definicao_matematica(_payload([10, 1], restante=0, vagas=8), CARGO_DEP_FEDERAL) is None


def test_sem_eleitores_restantes_no_arquivo_nao_decide() -> None:
    arquivo = _payload([10, 1], restante=0, md="e")
    arquivo = arquivo.__class__(**{**arquivo.__dict__, "eleitores_restantes": None})
    assert definicao_matematica(arquivo, CARGO_GOVERNADOR) is None
