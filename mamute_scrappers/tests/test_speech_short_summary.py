"""Resumo curto dos discursos para o relatório por e-mail (CS-116)."""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from mamute_scrappers.scripts import speech_short_summary as job

HOJE = date(2026, 10, 9)


class ClienteFake:
    def __init__(self, *respostas: Any) -> None:
        self.respostas = list(respostas)
        self.chamadas: list[str] = []
        self.chat = self

    @property
    def completions(self) -> "ClienteFake":
        return self

    def create(self, *, model: str, messages: list[dict], **_: Any) -> Any:
        self.chamadas.append(messages[-1]["content"])
        resposta = self.respostas.pop(0) if self.respostas else "Resumo."
        if isinstance(resposta, Exception):
            raise resposta

        class _Msg:
            content = resposta

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        return _Completion()


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table speeches_transcripts (id integer primary key, date date,"
            " summary text, speech_text text)"
        )
        conn.exec_driver_sql(
            "create table speech_short_summary (speech_id integer primary key,"
            " text text not null, source text not null, model text,"
            " created_at datetime default current_timestamp)"
        )
    s = Session(engine)
    yield s
    s.close()


def _discurso(session: Session, sid: int, *, summary=None, texto=None, dia="2026-10-05"):
    session.execute(
        text("insert into speeches_transcripts values (:id, :dia, :summary, :texto)"),
        {"id": sid, "dia": dia, "summary": summary, "texto": texto},
    )
    session.commit()


def _resumos(session: Session) -> dict[int, tuple[str, str]]:
    return {
        r[0]: (r[1], r[2])
        for r in session.execute(text("select speech_id, text, source from speech_short_summary"))
    }


class TestEscolherFonte:
    def test_sumario_curto_e_oficial(self) -> None:
        assert job.escolher_fonte("  Defende a reforma.  ", None) == ("oficial", "Defende a reforma.")

    def test_sumario_longo_vai_para_a_ia(self) -> None:
        longo = "Frase. " * 60
        assert job.escolher_fonte(longo, "texto")[0] == "ia_sumario"

    def test_sem_sumario_usa_a_transcricao_cortada(self) -> None:
        fonte, texto = job.escolher_fonte(None, "x" * 9000)
        assert fonte == "ia_transcricao"
        assert len(texto) == job.MAX_TRANSCRICAO

    def test_sem_nada_nao_resume(self) -> None:
        assert job.escolher_fonte("  ", "  ") is None


class TestCortarNaFrase:
    def test_texto_curto_fica_igual(self) -> None:
        assert job.cortar_na_frase("Uma frase.", 240) == "Uma frase."

    def test_corta_no_fim_da_ultima_frase_que_cabe(self) -> None:
        texto = "Primeira frase. " + "Segunda frase bem longa " * 20
        assert job.cortar_na_frase(texto, 40) == "Primeira frase."

    def test_sem_ponto_corta_na_palavra_com_reticencias(self) -> None:
        resultado = job.cortar_na_frase("palavra " * 50, 40)
        assert resultado.endswith("…")
        assert len(resultado) <= 40


class TestRun:
    def test_sumario_curto_grava_sem_chamar_a_ia(self, session: Session) -> None:
        _discurso(session, 1, summary="Homenageia os professores.")
        cliente = ClienteFake()

        out = job.run(session, cliente, hoje=HOJE)

        assert _resumos(session) == {1: ("Homenageia os professores.", "oficial")}
        assert cliente.chamadas == []
        assert out["oficial"] == 1

    def test_sumario_longo_e_condensado_pela_ia_a_partir_do_sumario(self, session: Session) -> None:
        longo = "Sessão especial sobre saúde. " * 20
        _discurso(session, 1, summary=longo, texto="TRANSCRICAO")
        cliente = ClienteFake("Discursa sobre saúde pública.")

        job.run(session, cliente, hoje=HOJE)

        assert _resumos(session) == {1: ("Discursa sobre saúde pública.", "ia_sumario")}
        assert "Sessão especial sobre saúde." in cliente.chamadas[0]
        assert "TRANSCRICAO" not in cliente.chamadas[0]

    def test_sem_sumario_resume_a_transcricao(self, session: Session) -> None:
        _discurso(session, 1, texto="O deputado falou da ponte.")
        cliente = ClienteFake("Cobra a obra da ponte.")

        job.run(session, cliente, hoje=HOJE)

        assert _resumos(session) == {1: ("Cobra a obra da ponte.", "ia_transcricao")}

    def test_resposta_longa_da_ia_e_cortada(self, session: Session) -> None:
        _discurso(session, 1, texto="texto")
        cliente = ClienteFake("Primeira frase curta. " + "Muito texto a mais " * 30)

        job.run(session, cliente, hoje=HOJE)

        assert _resumos(session)[1][0] == "Primeira frase curta."

    def test_falha_da_ia_nao_grava_e_nao_para_o_lote(self, session: Session) -> None:
        _discurso(session, 1, texto="um", dia="2026-10-06")  # mais novo: vai primeiro
        _discurso(session, 2, texto="dois", dia="2026-10-05")
        cliente = ClienteFake(RuntimeError("timeout"), "Resumo do dois.")

        out = job.run(session, cliente, hoje=HOJE)

        assert _resumos(session) == {2: ("Resumo do dois.", "ia_transcricao")}
        assert out["falhas"] == 1

    def test_resposta_vazia_da_ia_conta_como_falha(self, session: Session) -> None:
        _discurso(session, 1, texto="um")

        out = job.run(session, ClienteFake("   "), hoje=HOJE)

        assert _resumos(session) == {}
        assert out["falhas"] == 1

    def test_discurso_ja_resumido_e_pulado(self, session: Session) -> None:
        _discurso(session, 1, summary="Curto.")
        job.run(session, ClienteFake(), hoje=HOJE)
        cliente = ClienteFake()

        out = job.run(session, cliente, hoje=HOJE)

        assert out["oficial"] == 0
        assert cliente.chamadas == []

    def test_discurso_fora_da_janela_e_ignorado(self, session: Session) -> None:
        _discurso(session, 1, summary="Antigo.", dia="2026-08-01")

        job.run(session, ClienteFake(), hoje=HOJE, dias=20)

        assert _resumos(session) == {}

    def test_sem_cliente_so_grava_os_oficiais(self, session: Session) -> None:
        """Sem chave de IA o job ainda adianta o que não precisa dela."""
        _discurso(session, 1, summary="Curto.")
        _discurso(session, 2, texto="sem sumário")

        out = job.run(session, None, hoje=HOJE)

        assert _resumos(session) == {1: ("Curto.", "oficial")}
        assert out["sem_ia"] == 1
