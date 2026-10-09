"""Conteúdo dos destaques do relatório: resumo curto de discurso e emendas novas (CS-116)."""
from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from mamute_scrappers.scripts.notificacao import labels, repository, stats
from mamute_scrappers.scripts.notificacao.tzcompat import combine_local

INICIO, FIM = date(2026, 9, 25), date(2026, 10, 9)
INICIO_DT = combine_local(INICIO, datetime.min.time())
FIM_DT = combine_local(date(2026, 10, 10), datetime.min.time())


def _session(*, com_resumo: bool = True) -> Session:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table speeches_transcripts (id integer primary key, parliamentarian_id integer,"
            " date date, summary text, speech_link text, publication_link text, type text)"
        )
        conn.exec_driver_sql(
            "create table speeches_transcripts_proposition (id integer primary key,"
            " speeches_transcripts_id integer, proposition_id integer)"
        )
        conn.exec_driver_sql(
            "create table proposition (id integer primary key, link text, proposition_code integer,"
            " presentation_date date)"
        )
        conn.exec_driver_sql(
            "create table parliamentary_amendment (id integer primary key, amendment_code text,"
            " year integer, amendment_type text, parliamentarian_id integer, match_status text,"
            " spending_locality text, function text, committed_value numeric, paid_value numeric,"
            " created_at datetime)"
        )
        if com_resumo:
            conn.exec_driver_sql(
                "create table speech_short_summary (speech_id integer primary key, text text,"
                " source text, model text, created_at datetime)"
            )
    return Session(engine)


def _discurso(s: Session, sid: int, summary: str, dia: str = "2026-10-01") -> None:
    s.execute(
        text(
            "insert into speeches_transcripts (id, parliamentarian_id, date, summary, type)"
            " values (:id, 1, :dia, :summary, 'Discurso')"
        ),
        {"id": sid, "dia": dia, "summary": summary},
    )


def _emenda(s: Session, eid: int, criada: str, valor: float = 196000, pid: int = 1) -> None:
    s.execute(
        text(
            "insert into parliamentary_amendment (id, amendment_code, year, amendment_type,"
            " parliamentarian_id, match_status, spending_locality, function, committed_value,"
            " paid_value, created_at) values (:id, :code, 2026,"
            " 'Emenda Individual - Transferências com Finalidade Definida', :pid, 'matched',"
            " 'CAMARAGIBE - PE', 'Desporto e lazer', :valor, 0, :criada)"
        ),
        {"id": eid, "code": f"E{eid}", "pid": pid, "valor": valor, "criada": criada},
    )


def _discursos(s: Session):
    return repository.fetch_recent_speeches(s, 1, "Fulano", INICIO, FIM, 10)


class TestDiscursos:
    def test_usa_o_resumo_curto_quando_existe(self) -> None:
        s = _session()
        _discurso(s, 1, "Sumário oficial muito longo " * 20)
        s.execute(text("insert into speech_short_summary (speech_id, text, source) values (1, 'Defende a ponte.', 'ia_sumario')"))
        s.commit()

        [item] = _discursos(s)

        assert item.title == "Defende a ponte."
        assert (item.kind_key, item.item_id) == ("discurso", 1)

    def test_sem_resumo_curto_mantem_o_sumario_cortado(self) -> None:
        s = _session()
        _discurso(s, 1, "x" * 300)
        s.commit()

        [item] = _discursos(s)

        assert item.title == "x" * 197 + "..."

    def test_tabela_ainda_nao_migrada_nao_quebra(self) -> None:
        s = _session(com_resumo=False)
        _discurso(s, 1, "Sumário curto.")
        s.commit()

        [item] = _discursos(s)

        assert item.title == "Sumário curto."


class TestEmendas:
    def test_so_entram_as_criadas_na_janela(self) -> None:
        s = _session()
        _emenda(s, 1, "2026-10-02 10:00:00")
        _emenda(s, 2, "2026-08-07 10:00:00")
        _emenda(s, 3, "2026-10-03 10:00:00", pid=2)
        s.commit()

        itens = repository.fetch_new_amendments(s, 1, "Fulano", INICIO_DT, FIM_DT, 10)

        assert [i.item_id for i in itens] == [1]
        item = itens[0]
        assert item.kind == "emenda"
        assert item.kind_key == "emenda"
        assert item.title == "Emenda individual de R$ 196.000,00"
        assert item.ementa == "Para Camaragibe - PE · Desporto e lazer"

    def test_maiores_valores_primeiro_e_limite(self) -> None:
        s = _session()
        _emenda(s, 1, "2026-10-02", valor=10)
        _emenda(s, 2, "2026-10-02", valor=900)
        _emenda(s, 3, "2026-10-02", valor=500)
        s.commit()

        itens = repository.fetch_new_amendments(s, 1, "Fulano", INICIO_DT, FIM_DT, 2)

        assert [i.item_id for i in itens] == [2, 3]

    def test_contagem_no_resumo_do_periodo(self) -> None:
        s = _session()
        _emenda(s, 1, "2026-10-02")
        _emenda(s, 2, "2026-10-03", pid=2)
        _emenda(s, 3, "2026-01-03")
        s.commit()

        assert stats.count_new_amendments(s, [1, 2], INICIO_DT, FIM_DT) == 2
        assert stats.count_new_amendments(s, [], INICIO_DT, FIM_DT) == 0


class TestTextos:
    @pytest.mark.parametrize(
        "valor,esperado",
        [(196000, "R$ 196.000,00"), (1234567.8, "R$ 1.234.567,80"), (0.5, "R$ 0,50")],
    )
    def test_formata_reais(self, valor: float, esperado: str) -> None:
        assert labels.format_brl(valor) == esperado

    def test_ementa_corta_na_frase(self) -> None:
        ementa = "Altera a lei de licitações. " + "Dispõe sobre muitas outras coisas " * 20

        assert labels.extract_ementa(ementa, None) == "Altera a lei de licitações."

    def test_ementa_curta_fica_inteira(self) -> None:
        assert labels.extract_ementa(None, "Institui o dia do mamute.") == "Institui o dia do mamute."


def test_ementa_vem_do_summary_e_nao_do_nome_do_tipo() -> None:
    """Na Câmara `proposition_description` é "Projeto de Lei"; a ementa está em `summary` (CS-123)."""
    assert labels.extract_ementa("Projeto de Lei", "Dispõe sobre cargos.") == "Dispõe sobre cargos."
