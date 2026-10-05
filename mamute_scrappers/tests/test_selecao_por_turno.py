"""CS-129: foto de quem cada pessoa selecionou em cada turno."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from eleicao_schema import add_candidacy, add_projeto, make_session
from mamute_scrappers.scripts.selecao_por_turno import registrar_turno

# SQLite guarda texto: corte e created_at na mesma forma (UTC, sem fuso).
CORTE_T1 = datetime(2026, 10, 4, 20, 0)
CORTE_T2 = datetime(2026, 10, 25, 20, 0)


def _selecionar(s, projeto_id: int, candidacy_id: int, quando: str) -> None:
    s.execute(
        text(
            "INSERT INTO projetos_candidacy (projeto_id, candidacy_id, created_at) "
            "VALUES (:p, :c, :q)"
        ),
        {"p": projeto_id, "c": candidacy_id, "q": quando},
    )
    s.commit()


def _foto(s) -> set[tuple[int, int, int]]:
    rows = s.execute(text("SELECT projeto_id, candidacy_id, turno FROM projetos_candidacy_turno")).all()
    return {(r.projeto_id, r.candidacy_id, r.turno) for r in rows}


def _cenario():
    s = make_session()
    add_projeto(s, 10, "ana@x.com")
    add_candidacy(s, 1, 111, office_code=1, state="BR", name="PRES 2T")
    add_candidacy(s, 2, 222, office_code=6, state="SP", name="DEP")
    add_candidacy(s, 3, 333, office_code=1, state="BR", name="PRES FORA")
    s.execute(
        text(
            "INSERT INTO candidacy_result (candidacy_id, turno, codigo_eleicao, situacao, totalizacao_final) "
            "VALUES (1, 1, 6257, '2º turno', 1), (3, 1, 6257, 'Não eleito', 1)"
        )
    )
    s.commit()
    return s


def test_turno_1_pega_so_o_que_foi_selecionado_ate_a_votacao_fechar() -> None:
    s = _cenario()
    _selecionar(s, 10, 2, "2026-09-01 12:00:00")
    _selecionar(s, 10, 3, "2026-10-04 19:59:00")
    _selecionar(s, 10, 1, "2026-10-05 01:00:00")  # depois do corte
    assert registrar_turno(s, turno=1, corte=CORTE_T1) == 2
    assert _foto(s) == {(10, 2, 1), (10, 3, 1)}


def test_foto_nao_muda_quando_a_pessoa_desmarca() -> None:
    s = _cenario()
    _selecionar(s, 10, 3, "2026-09-01 12:00:00")
    registrar_turno(s, turno=1, corte=CORTE_T1)
    s.execute(text("DELETE FROM projetos_candidacy"))
    s.commit()
    assert registrar_turno(s, turno=1, corte=CORTE_T1) == 0  # idempotente
    assert _foto(s) == {(10, 3, 1)}


def test_turno_2_so_quem_foi_ao_segundo_turno() -> None:
    s = _cenario()
    _selecionar(s, 10, 1, "2026-10-10 12:00:00")
    _selecionar(s, 10, 2, "2026-10-10 12:00:00")  # deputado: nao tem 2o turno
    _selecionar(s, 10, 3, "2026-10-10 12:00:00")  # nao foi ao 2o turno
    assert registrar_turno(s, turno=2, corte=CORTE_T2) == 1
    assert _foto(s) == {(10, 1, 2)}


def test_dry_run_nao_grava() -> None:
    s = _cenario()
    _selecionar(s, 10, 2, "2026-09-01 12:00:00")
    assert registrar_turno(s, turno=1, corte=CORTE_T1, dry_run=True) == 1
    assert _foto(s) == set()


def test_cortes_oficiais_sao_17h_de_brasilia() -> None:
    from mamute_scrappers.scripts.selecao_por_turno import CORTES

    assert CORTES[1].astimezone(timezone.utc) == datetime(2026, 10, 4, 20, 0, tzinfo=timezone.utc)
    assert CORTES[2].astimezone(timezone.utc) == datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc)


# CS-130: liberacao da selecao para o 2o turno.

from mamute_scrappers.scripts.selecao_por_turno import liberar_para_segundo_turno  # noqa: E402
from mamute_scrappers.tse_crawler.resultados_parsing import abrangencias  # noqa: E402

TODOS = (1, 3, 5, 6, 7, 8)


def _fechar_tudo(s, *, exceto: tuple[int, str] | None = None) -> None:
    for cargo in TODOS:
        for uf in abrangencias(cargo):
            s.execute(
                text(
                    "INSERT INTO tse_result_file (ciclo, codigo_eleicao, turno, uf, cargo_codigo, totalizacao_final) "
                    "VALUES ('ele2026', 6259, 1, :uf, :cargo, :f)"
                ),
                {"uf": uf, "cargo": cargo, "f": (cargo, uf) != exceto},
            )
    s.commit()


def _liberacao_cenario():
    """Ana selecionou: presidente no 2o turno (1), deputado eleito (2),
    presidente nao eleito (3), deputado suplente (4), indeferido sem resultado (5)."""
    s = _cenario()
    add_candidacy(s, 4, 444, office_code=6, state="SP", name="SUPLENTE")
    add_candidacy(s, 5, 555, office_code=6, state="SP", name="INDEFERIDO")
    s.execute(
        text(
            "INSERT INTO candidacy_result (candidacy_id, turno, codigo_eleicao, situacao, totalizacao_final) "
            "VALUES (2, 1, 6259, 'Eleito por QP', 1), (4, 1, 6259, 'Suplente', 1)"
        )
    )
    for cid in (1, 2, 3, 4, 5):
        _selecionar(s, 10, cid, "2026-09-01 12:00:00")
    registrar_turno(s, turno=1, corte=CORTE_T1)
    return s


def _selecionados(s) -> set[int]:
    return {r[0] for r in s.execute(text("SELECT candidacy_id FROM projetos_candidacy")).all()}


def test_liberacao_tira_so_nao_eleitos_e_mantem_o_selo() -> None:
    s = _liberacao_cenario()
    _fechar_tudo(s)
    assert liberar_para_segundo_turno(s) == 3
    assert _selecionados(s) == {1, 2}  # 2o turno e eleito ficam
    assert {cid for _, cid, t in _foto(s) if t == 1} == {1, 2, 3, 4, 5}  # selo "1º" segue


def test_liberacao_espera_o_tse_fechar_tudo() -> None:
    s = _liberacao_cenario()
    _fechar_tudo(s, exceto=(6, "am"))
    assert liberar_para_segundo_turno(s) is None
    assert _selecionados(s) == {1, 2, 3, 4, 5}


def test_liberacao_acontece_uma_vez_so() -> None:
    s = _liberacao_cenario()
    _fechar_tudo(s)
    liberar_para_segundo_turno(s)
    _selecionar(s, 10, 3, "2026-10-06 12:00:00")  # a pessoa marca de novo
    assert liberar_para_segundo_turno(s) is None
    assert 3 in _selecionados(s)


def test_liberacao_nao_tira_quem_nao_esta_na_foto() -> None:
    s = _liberacao_cenario()
    s.execute(text("DELETE FROM projetos_candidacy_turno WHERE candidacy_id = 3"))
    s.commit()
    _fechar_tudo(s)
    liberar_para_segundo_turno(s)
    assert 3 in _selecionados(s)  # sem selo "1º", nao sai


def test_liberacao_dry_run_nao_apaga() -> None:
    s = _liberacao_cenario()
    _fechar_tudo(s)
    assert liberar_para_segundo_turno(s, dry_run=True) == 3
    assert _selecionados(s) == {1, 2, 3, 4, 5}
    assert s.execute(text("SELECT count(*) FROM selecao_liberacao")).scalar() == 0
