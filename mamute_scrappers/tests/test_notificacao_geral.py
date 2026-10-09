"""Destaques gerais do Congresso para quem não tem parlamentar ou atividade (CS-133)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from mamute_scrappers.scripts.notificacao import geral

INICIO, FIM = date(2026, 9, 25), date(2026, 10, 9)


def _session(*, com_nuvem: bool = True) -> Session:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table proposition (id integer primary key, title text, link text,"
            " proposition_code integer, proposition_acronym text, proposition_number integer,"
            " presentation_year integer, proposition_description text, summary text,"
            " presentation_date date)"
        )
        conn.exec_driver_sql(
            "create table roll_call_votes (id integer primary key, parliamentarian_id integer,"
            " proposition_id integer, vote text, description text, link text, vote_date date)"
        )
        conn.exec_driver_sql("create table speeches_transcripts (id integer primary key, date date)")
        conn.exec_driver_sql(
            "create table speeches_transcripts_keywords (id integer primary key,"
            " speeches_transcripts_id integer, keyword text, term text, frequency integer,"
            " rank integer, is_primary boolean)"
        )
        if com_nuvem:
            conn.exec_driver_sql("create table word_cloud_terms (term text, kind text)")
    return Session(engine)


def _proposicao(s: Session, pid: int, sigla: str = "PL", numero: int = 1) -> None:
    s.execute(
        text(
            "insert into proposition (id, title, link, proposition_acronym, proposition_number,"
            " presentation_year, proposition_description) values (:id, :t, :link, :sigla, :n, 2026,"
            " :ementa)"
        ),
        {
            "id": pid,
            "t": f"{sigla} {numero}/2026",
            "link": f"https://www.camara.leg.br/p/{pid}",
            "sigla": sigla,
            "n": numero,
            "ementa": f"Ementa da {pid}.",
        },
    )


def _votos(s: Session, pid: int, dia: str, sim: int, nao: int) -> None:
    for i in range(sim + nao):
        s.execute(
            text(
                "insert into roll_call_votes (parliamentarian_id, proposition_id, vote, vote_date)"
                " values (:p, :pid, :v, :dia)"
            ),
            {"p": i, "pid": pid, "v": "Sim" if i < sim else "Não", "dia": dia},
        )


def _tema(s: Session, sid: int, dia: str, termo: str, freq: int = 1, primario: bool = True) -> None:
    s.execute(text("insert into speeches_transcripts values (:id, :dia)"), {"id": sid, "dia": dia})
    s.execute(
        text(
            "insert into speeches_transcripts_keywords (speeches_transcripts_id, keyword, term,"
            " frequency, rank, is_primary) values (:id, :t, :t, :f, 1, :p)"
        ),
        {"id": sid, "t": termo, "f": freq, "p": primario},
    )


class TestVotacoes:
    def test_uma_por_proposicao_mais_recentes_primeiro_com_placar(self) -> None:
        s = _session()
        _proposicao(s, 1, numero=10)
        _proposicao(s, 2, numero=20)
        _votos(s, 1, "2026-10-01", sim=3, nao=1)
        _votos(s, 2, "2026-10-05", sim=1, nao=2)
        s.commit()

        out = geral.build_general_highlights(s, INICIO, FIM)

        assert [v.item_id for v in out.votacoes] == [2, 1]
        assert out.votacoes[1].subtitle == "Votação em 01/10/2026 · Sim 3 × Não 1"
        assert out.votacoes[1].ementa == "Ementa da 1."
        assert out.votacoes[1].kind_key == "proposicao"

    def test_fora_da_janela_nao_entra_e_limite_de_cinco(self) -> None:
        s = _session()
        for pid in range(1, 8):
            _proposicao(s, pid, numero=pid)
            _votos(s, pid, f"2026-10-0{pid}", sim=1, nao=0)
        _proposicao(s, 99, numero=99)
        _votos(s, 99, "2026-08-01", sim=5, nao=0)
        s.commit()

        out = geral.build_general_highlights(s, INICIO, FIM)

        assert [v.item_id for v in out.votacoes] == [7, 6, 5, 4, 3]


class TestTemas:
    def test_soma_a_frequencia_das_palavras_principais_da_quinzena(self) -> None:
        s = _session()
        _tema(s, 1, "2026-10-01", "saúde", freq=3)
        _tema(s, 2, "2026-10-02", "Saúde", freq=2)
        _tema(s, 3, "2026-10-02", "educação", freq=4)
        _tema(s, 4, "2026-10-02", "segurança", freq=9, primario=False)
        _tema(s, 5, "2026-08-02", "antigo", freq=50)
        s.commit()

        assert geral.build_general_highlights(s, INICIO, FIM).temas == ["saúde", "educação"]

    def test_respeita_termos_irrelevantes_e_stopwords_da_nuvem(self) -> None:
        s = _session()
        _tema(s, 1, "2026-10-01", "presidente", freq=10)
        _tema(s, 2, "2026-10-01", "projeto de lei", freq=8)
        _tema(s, 3, "2026-10-01", "reforma tributária", freq=1)
        s.execute(text("insert into word_cloud_terms values ('presidente', 'excluded')"))
        s.execute(text("insert into word_cloud_terms values ('projeto', 'stopword')"))
        s.execute(text("insert into word_cloud_terms values ('de', 'stopword')"))
        s.commit()

        assert geral.build_general_highlights(s, INICIO, FIM).temas == ["lei", "reforma tributária"]

    def test_sem_tabela_da_nuvem_nao_filtra_e_nao_quebra(self) -> None:
        s = _session(com_nuvem=False)
        _tema(s, 1, "2026-10-01", "saúde")
        s.commit()

        assert geral.build_general_highlights(s, INICIO, FIM).temas == ["saúde"]

    def test_no_maximo_oito(self) -> None:
        s = _session()
        for i in range(12):
            _tema(s, i, "2026-10-01", f"tema{i:02d}", freq=100 - i)
        s.commit()

        assert len(geral.build_general_highlights(s, INICIO, FIM).temas) == 8


def test_quinzena_sem_nada_fica_vazia() -> None:
    out = geral.build_general_highlights(_session(), INICIO, FIM)

    assert out.vazio
