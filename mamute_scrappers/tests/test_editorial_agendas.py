"""Job de classificação de pautas editoriais (CS-72).

SQLite in-memory e cliente de IA mockado — nenhum teste aqui toca rede.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

# mamute_scrappers.db.engine exige DATABASE_URL no import — mesmo cuidado de
# test_ghost_tiers_sync.py. Nenhum teste aqui conecta nesta URL: o job recebe
# a sessão SQLite injetada.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg2://test:test@localhost:5432/test_db"
)
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from mamute_scrappers.scripts import classify_editorial_agendas as job

DDL = (
    """
    create table parliamentarian (
        id integer primary key,
        name text,
        created_at datetime not null default current_timestamp,
        updated_at datetime not null default current_timestamp
    )
    """,
    """
    create table speeches_transcripts (
        id integer primary key,
        parliamentarian_id integer not null,
        date date,
        session_number text,
        type text,
        speech_link text,
        speech_text text,
        summary text,
        hour_minute text,
        publication_link text,
        publication_text text,
        created_at datetime not null default current_timestamp,
        updated_at datetime not null default current_timestamp
    )
    """,
    """
    create table speeches_transcripts_keywords (
        id integer primary key,
        speeches_transcripts_id integer not null,
        keyword text not null,
        term text not null,
        frequency integer not null default 0,
        rank integer not null default 1,
        is_primary boolean not null default 1
    )
    """,
    """
    create table proposition (
        id integer primary key,
        proposition_description text,
        presentation_date date
    )
    """,
    """
    create table authors_proposition (
        id integer primary key,
        parliamentarian_id integer not null,
        proposition_id integer not null,
        created_at datetime not null default current_timestamp,
        updated_at datetime not null default current_timestamp
    )
    """,
    """
    create table word_cloud_terms (
        id integer primary key,
        term text not null,
        kind text not null
    )
    """,
    """
    create table parliamentarian_agenda_run (
        parliamentarian_id integer primary key,
        outcome text not null,
        source text,
        model text,
        vocabulary_version integer not null default 1,
        computed_at datetime not null default current_timestamp,
        check (outcome in ('classificado', 'sem_pauta', 'sem_material'))
    )
    """,
    """
    create table editorial_agenda (
        id integer primary key,
        name text not null,
        slug text not null unique,
        description text,
        position smallint not null default 0,
        active boolean not null default 1,
        vocabulary_version integer not null default 1,
        created_at datetime not null default current_timestamp,
        updated_at datetime not null default current_timestamp
    )
    """,
    """
    create table parliamentarian_agenda (
        id integer primary key,
        parliamentarian_id integer not null,
        agenda_id integer not null,
        rank smallint not null,
        confidence numeric(3, 2),
        evidence text,
        model text,
        vocabulary_version integer not null default 1,
        computed_at datetime not null default current_timestamp,
        check (rank between 1 and 3),
        unique (parliamentarian_id, agenda_id),
        unique (parliamentarian_id, rank)
    )
    """,
)


class ClienteFake:
    """Devolve respostas prontas na ordem, e conta as chamadas."""

    def __init__(self, *respostas: str) -> None:
        self.respostas = list(respostas)
        self.chamadas: list[str] = []
        self.chat = self

    @property
    def completions(self) -> "ClienteFake":
        return self

    def create(self, *, model: str, messages: list[dict], **_: Any) -> Any:
        self.chamadas.append(messages[-1]["content"])
        conteudo = self.respostas.pop(0) if self.respostas else "{}"

        class _Msg:
            content = conteudo

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        return _Completion()


def _resposta(*pares: tuple[str, float]) -> str:
    return json.dumps(
        {
            "agendas": [
                {"slug": slug, "confidence": conf, "evidence": f"trecho de {slug}"}
                for slug, conf in pares
            ]
        }
    )


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        for ddl in DDL:
            conn.exec_driver_sql(ddl)
        conn.exec_driver_sql(
            "insert into editorial_agenda (id, name, slug, description, position) "
            "values (10, 'Meio Ambiente', 'meio-ambiente', 'Clima.', 1), "
            "(20, 'Saúde', 'saude', 'SUS.', 2), "
            "(30, 'Educação', 'educacao', 'Escolas.', 3), "
            "(40, 'Agropecuária', 'agropecuaria', 'Safra.', 4)"
        )
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield s
    s.close()


def _parlamentar(session: Session, pid: int, nome: str = "Fulano") -> None:
    session.execute(
        text("insert into parliamentarian (id, name) values (:i, :n)"),
        {"i": pid, "n": nome},
    )
    session.commit()


def _discurso(
    session: Session,
    pid: int,
    *,
    summary: Optional[str] = None,
    publication_text: Optional[str] = None,
    speech_text: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> int:
    resultado = session.execute(
        text(
            "insert into speeches_transcripts "
            "(parliamentarian_id, summary, publication_text, speech_text, created_at) "
            "values (:p, :s, :k, :t, coalesce(:c, current_timestamp))"
        ),
        {
            "p": pid,
            "s": summary,
            "k": publication_text,
            "t": speech_text,
            "c": created_at,
        },
    )
    session.commit()
    return int(resultado.lastrowid)


def _classificacoes(session: Session, pid: int) -> list[tuple]:
    return session.execute(
        text(
            "select ea.slug, pa.rank, pa.confidence, pa.model, pa.vocabulary_version "
            "from parliamentarian_agenda pa "
            "join editorial_agenda ea on ea.id = pa.agenda_id "
            "where pa.parliamentarian_id = :p order by pa.rank"
        ),
        {"p": pid},
    ).all()


def _rodadas(session: Session) -> dict:
    return {
        int(pid): outcome
        for pid, outcome in session.execute(
            text(
                "select parliamentarian_id, outcome from parliamentarian_agenda_run"
            )
        ).all()
    }


def _rodar(session: Session, cliente: ClienteFake, **kw) -> dict:
    return job.classificar(session=session, client=cliente, model="modelo-fake", **kw)


# --------------------------------------------------------------------------
# Vocabulário fechado
# --------------------------------------------------------------------------


class TestVocabularioFechado:
    def test_slug_inexistente_e_descartado(self, session: Session) -> None:
        """Sem esta regra a feature vira tag livre gerada pela IA."""

        _parlamentar(session, 1)
        _discurso(session, 1, summary="Discurso sobre o clima.")
        cliente = ClienteFake(
            _resposta(("energia-nuclear", 0.9), ("meio-ambiente", 0.8))
        )

        _rodar(session, cliente)

        assert [c[0] for c in _classificacoes(session, 1)] == ["meio-ambiente"]

    def test_resposta_so_com_slug_invalido_deixa_sem_pauta(
        self, session: Session
    ) -> None:
        _parlamentar(session, 1)
        _discurso(session, 1, summary="Discurso qualquer.")
        cliente = ClienteFake(_resposta(("pauta-inventada", 0.99)))

        contadores = _rodar(session, cliente)

        assert _classificacoes(session, 1) == []
        assert contadores["sem_pauta"] == 1

    def test_cinco_pautas_sao_cortadas_em_tres(self, session: Session) -> None:
        _parlamentar(session, 1)
        _discurso(session, 1, summary="Discurso amplo.")
        cliente = ClienteFake(
            _resposta(
                ("meio-ambiente", 0.9),
                ("saude", 0.8),
                ("educacao", 0.7),
                ("agropecuaria", 0.6),
                ("meio-ambiente", 0.5),
            )
        )

        _rodar(session, cliente)

        assert [c[0] for c in _classificacoes(session, 1)] == [
            "meio-ambiente",
            "saude",
            "educacao",
        ]
        assert [c[1] for c in _classificacoes(session, 1)] == [1, 2, 3]

    def test_resposta_vazia_deixa_sem_pauta_e_nao_inventa_outros(
        self, session: Session
    ) -> None:
        _parlamentar(session, 1)
        _discurso(session, 1, summary="Discurso genérico.")

        _rodar(session, ClienteFake('{"agendas": []}'))

        assert _classificacoes(session, 1) == []

    def test_json_invalido_nao_derruba_o_job(self, session: Session) -> None:
        _parlamentar(session, 1)
        _discurso(session, 1, summary="Discurso.")

        _rodar(session, ClienteFake("desculpe, não consegui"))

        assert _classificacoes(session, 1) == []

    def test_pauta_inativa_nao_entra_no_vocabulario(self, session: Session) -> None:
        session.execute(text("update editorial_agenda set active = 0 where id = 10"))
        session.commit()
        _parlamentar(session, 1)
        _discurso(session, 1, summary="Clima.")
        cliente = ClienteFake(_resposta(("meio-ambiente", 0.9), ("saude", 0.5)))

        _rodar(session, cliente)

        assert [c[0] for c in _classificacoes(session, 1)] == ["saude"]
        assert "meio-ambiente" not in cliente.chamadas[0]


# --------------------------------------------------------------------------
# A escada de entrada — a decisão do brief sobre deputados
# --------------------------------------------------------------------------


class TestEscadaDeEntrada:
    def test_deputado_sem_keywords_do_senado_e_classificado(
        self, session: Session
    ) -> None:
        """Este é o teste que trava a decisão do brief.

        `speeches_transcripts_keywords` só tem Senado. Se ela fosse a fonte
        primária, os 513 deputados ficariam de fora da feature inteira.
        """

        _parlamentar(session, 1, "Deputada")
        _discurso(session, 1, summary="Defesa do licenciamento ambiental.")
        cliente = ClienteFake(_resposta(("meio-ambiente", 0.9)))

        _rodar(session, cliente)

        assert [c[0] for c in _classificacoes(session, 1)] == ["meio-ambiente"]
        assert "licenciamento ambiental" in cliente.chamadas[0]

    def test_publication_text_da_camara_vira_palavra_chave(
        self, session: Session
    ) -> None:
        """No crawler da Câmara este campo guarda as keywords da API."""

        _parlamentar(session, 1)
        _discurso(session, 1, publication_text="Saúde pública, Vacinação, SUS")

        _rodar(session, ClienteFake(_resposta(("saude", 0.9))))

        assert [c[0] for c in _classificacoes(session, 1)] == ["saude"]

    def test_stopwords_da_nuvem_saem_das_palavras_chave(
        self, session: Session
    ) -> None:
        """Senão "senhor presidente" vira pauta."""

        session.execute(
            text(
                "insert into word_cloud_terms (term, kind) values "
                "('presidente', 'stopword'), ('obrigado', 'stopword')"
            )
        )
        session.commit()
        _parlamentar(session, 1)
        _discurso(session, 1, publication_text="presidente, obrigado, Vacinação")
        cliente = ClienteFake(_resposta(("saude", 0.9)))

        _rodar(session, cliente)

        linha_das_keywords = next(
            l for l in cliente.chamadas[0].splitlines() if l.startswith("Palavras-chave:")
        )
        assert "Vacinação" in linha_das_keywords
        assert "presidente" not in linha_das_keywords
        assert "obrigado" not in linha_das_keywords

    def test_keywords_do_senado_entram_quando_existem(
        self, session: Session
    ) -> None:
        _parlamentar(session, 1)
        speech_id = _discurso(session, 1, publication_text="Orçamento")
        session.execute(
            text(
                "insert into speeches_transcripts_keywords "
                "(speeches_transcripts_id, keyword, term, rank, is_primary) "
                "values (:s, 'reforma agrária', 'reforma agrária', 1, 1)"
            ),
            {"s": speech_id},
        )
        session.commit()
        cliente = ClienteFake(_resposta(("agropecuaria", 0.8)))

        _rodar(session, cliente)

        assert "reforma agrária" in cliente.chamadas[0]

    def test_sem_discurso_usa_ementas_de_autoria(self, session: Session) -> None:
        """Quem legisla mas não discursa não pode ficar de fora."""

        _parlamentar(session, 1)
        session.execute(
            text(
                "insert into proposition (id, proposition_description) values "
                "(100, 'Institui o Plano Nacional de Educação Infantil.')"
            )
        )
        session.execute(
            text(
                "insert into authors_proposition (parliamentarian_id, proposition_id) "
                "values (1, 100)"
            )
        )
        session.commit()
        cliente = ClienteFake(_resposta(("educacao", 0.9)))

        _rodar(session, cliente)

        assert [c[0] for c in _classificacoes(session, 1)] == ["educacao"]
        assert "Educação Infantil" in cliente.chamadas[0]

    def test_sem_material_nenhum_nao_chama_o_modelo(self, session: Session) -> None:
        """Não se paga token por quem não tem o que classificar."""

        _parlamentar(session, 1)
        cliente = ClienteFake(_resposta(("saude", 0.9)))

        contadores = _rodar(session, cliente)

        assert cliente.chamadas == []
        assert _classificacoes(session, 1) == []
        assert contadores["sem_material"] == 1


# --------------------------------------------------------------------------
# Idempotência e reprocessamento
# --------------------------------------------------------------------------


class TestIdempotencia:
    def test_rodar_duas_vezes_seguidas_nao_muda_nada(self, session: Session) -> None:
        _parlamentar(session, 1)
        _discurso(session, 1, summary="Clima.")

        _rodar(session, ClienteFake(_resposta(("meio-ambiente", 0.9))))
        antes = _classificacoes(session, 1)

        # Segunda rodada: nada mudou, então nem chega a chamar o modelo.
        cliente = ClienteFake(_resposta(("saude", 0.9)))
        contadores = _rodar(session, cliente)

        assert _classificacoes(session, 1) == antes
        assert cliente.chamadas == []
        assert contadores["pulados"] == 1

    def test_force_reclassifica_quem_esta_em_dia(self, session: Session) -> None:
        _parlamentar(session, 1)
        _discurso(session, 1, summary="Clima.")
        _rodar(session, ClienteFake(_resposta(("meio-ambiente", 0.9))))

        _rodar(session, ClienteFake(_resposta(("saude", 0.7))), force=True)

        assert [c[0] for c in _classificacoes(session, 1)] == ["saude"]

    def test_reclassificar_nao_duplica_rank(self, session: Session) -> None:
        """Delete-then-insert: nunca há classificação velha e nova ao mesmo tempo."""

        _parlamentar(session, 1)
        _discurso(session, 1, summary="Amplo.")
        _rodar(
            session,
            ClienteFake(_resposta(("meio-ambiente", 0.9), ("saude", 0.8))),
        )

        _rodar(
            session,
            ClienteFake(_resposta(("educacao", 0.9))),
            force=True,
        )

        linhas = _classificacoes(session, 1)
        assert [c[0] for c in linhas] == ["educacao"]
        assert [c[1] for c in linhas] == [1]


class TestReprocessamento:
    def test_vocabulario_novo_dispara_reclassificacao(self, session: Session) -> None:
        """O gatilho de quando o admin edita as descriptions."""

        _parlamentar(session, 1)
        _discurso(session, 1, summary="Clima.")
        _rodar(session, ClienteFake(_resposta(("meio-ambiente", 0.9))))

        session.execute(text("update editorial_agenda set vocabulary_version = 2"))
        session.commit()

        cliente = ClienteFake(_resposta(("saude", 0.9)))
        _rodar(session, cliente)

        assert len(cliente.chamadas) == 1
        assert [c[0] for c in _classificacoes(session, 1)] == ["saude"]
        assert _classificacoes(session, 1)[0][4] == 2

    def test_discurso_novo_dispara_reclassificacao(self, session: Session) -> None:
        _parlamentar(session, 1)
        antigo = datetime.now(timezone.utc) - timedelta(days=30)
        _discurso(session, 1, summary="Clima.", created_at=antigo)
        _rodar(session, ClienteFake(_resposta(("meio-ambiente", 0.9))))

        # Instante explícito: o current_timestamp do SQLite tem resolução de
        # 1 segundo, e o discurso novo cairia no mesmo segundo do computed_at.
        _discurso(
            session,
            1,
            summary="Agora falo de vacinação.",
            created_at=datetime.now(timezone.utc) + timedelta(days=1),
        )

        cliente = ClienteFake(_resposta(("saude", 0.9)))
        _rodar(session, cliente)

        assert len(cliente.chamadas) == 1
        assert [c[0] for c in _classificacoes(session, 1)] == ["saude"]

    def test_quem_nunca_foi_classificado_entra_sempre(
        self, session: Session
    ) -> None:
        _parlamentar(session, 1)
        _parlamentar(session, 2, "Beltrano")
        _discurso(session, 1, summary="Clima.")
        _discurso(session, 2, summary="Vacinas.")

        cliente = ClienteFake(
            _resposta(("meio-ambiente", 0.9)), _resposta(("saude", 0.9))
        )
        contadores = _rodar(session, cliente)

        assert contadores["classificados"] == 2

    def test_limit_e_parliamentarian_recortam_a_rodada(
        self, session: Session
    ) -> None:
        _parlamentar(session, 1)
        _parlamentar(session, 2, "Beltrano")
        _discurso(session, 1, summary="Clima.")
        _discurso(session, 2, summary="Vacinas.")

        _rodar(
            session,
            ClienteFake(_resposta(("saude", 0.9))),
            parliamentarian_id=2,
        )

        assert _classificacoes(session, 1) == []
        assert [c[0] for c in _classificacoes(session, 2)] == ["saude"]


# --------------------------------------------------------------------------
# Metadados e funções puras
# --------------------------------------------------------------------------


class TestRegistroDaTentativa:
    """O gasto invisivel da CS-72: quem nao encaixa em nada volta todo dia.

    Sem `parliamentarian_agenda_run`, zero linhas de pauta significa ao mesmo
    tempo "nunca analisei" e "analisei e nada encaixou" — e o job, sem
    conseguir distinguir, reenviava a mesma pessoa ao modelo em toda rodada,
    para sempre, sem nunca gravar nada.
    """

    def test_quem_ficou_sem_pauta_nao_volta_ao_modelo_na_rodada_seguinte(
        self, session: Session
    ) -> None:
        _parlamentar(session, 1)
        _discurso(session, 1, summary="Discurso puramente protocolar.")

        _rodar(session, ClienteFake(_resposta()))
        assert _classificacoes(session, 1) == []

        cliente = ClienteFake(_resposta())
        contadores = _rodar(session, cliente)

        assert cliente.chamadas == []
        assert contadores["pulados"] == 1

    def test_sem_material_tambem_nao_e_remontado_toda_rodada(
        self, session: Session
    ) -> None:
        """Nao gasta token, mas gastava trabalho: remontava o texto a cada vez."""

        _parlamentar(session, 1)

        primeira = _rodar(session, ClienteFake())
        assert primeira["sem_material"] == 1

        segunda = _rodar(session, ClienteFake())
        assert segunda["pulados"] == 1
        assert segunda["sem_material"] == 0

    def test_grava_o_desfecho_de_cada_rodada(self, session: Session) -> None:
        _parlamentar(session, 1)
        _parlamentar(session, 2, "Protocolar")
        _parlamentar(session, 3, "Silencioso")
        _discurso(session, 1, summary="Desmatamento na Amazônia.")
        _discurso(session, 2, summary="Questão de ordem.")

        _rodar(session, ClienteFake(_resposta(("meio-ambiente", 0.9)), _resposta()))

        assert _rodadas(session) == {
            1: "classificado",
            2: "sem_pauta",
            3: "sem_material",
        }

    def test_falha_do_provedor_nao_conta_como_analisado(
        self, session: Session
    ) -> None:
        """Erro de rede nao e "olhei e nao achei": tem que tentar de novo."""

        _parlamentar(session, 1)
        _discurso(session, 1, summary="Desmatamento na Amazônia.")

        class ClienteQuebrado(ClienteFake):
            def create(self, **_: Any) -> Any:
                raise RuntimeError("provedor fora do ar")

        contadores = _rodar(session, ClienteQuebrado())
        assert contadores["erros"] == 1
        assert _rodadas(session) == {}

        # Proxima rodada tenta de novo, em vez de tratar como resolvido.
        cliente = ClienteFake(_resposta(("meio-ambiente", 0.9)))
        _rodar(session, cliente)
        assert len(cliente.chamadas) == 1
        assert [c[0] for c in _classificacoes(session, 1)] == ["meio-ambiente"]


class TestGatilhosDeMaterial:
    """Discurso completado depois e autoria nova tambem pedem reclassificacao."""

    def test_discurso_atualizado_dispara_reclassificacao(
        self, session: Session
    ) -> None:
        """O crawler da Camara preenche a transcricao DEPOIS de criar a linha.

        Olhando so `created_at`, a pessoa seria classificada em cima de um
        discurso vazio e nunca mais revisitada.
        """

        _parlamentar(session, 1)
        speech_id = _discurso(session, 1, summary="Nota curta.")
        _rodar(session, ClienteFake(_resposta(("saude", 0.5))))

        # Mesma linha, texto completado depois — `created_at` nao muda.
        session.execute(
            text(
                "update speeches_transcripts "
                "set summary = :s, updated_at = :quando where id = :i"
            ),
            {
                "s": "Desmatamento e licenciamento ambiental na Amazônia.",
                "quando": datetime.now(timezone.utc) + timedelta(hours=1),
                "i": speech_id,
            },
        )
        session.commit()

        cliente = ClienteFake(_resposta(("meio-ambiente", 0.9)))
        _rodar(session, cliente)

        assert len(cliente.chamadas) == 1
        assert [c[0] for c in _classificacoes(session, 1)] == ["meio-ambiente"]

    def test_autoria_nova_dispara_reclassificacao(self, session: Session) -> None:
        """Quem nao discursa e classificado pelas ementas — e legisla mais."""

        _parlamentar(session, 1)
        session.execute(
            text(
                "insert into proposition (id, proposition_description) values "
                "(100, 'Institui o Plano Nacional de Educação Infantil.')"
            )
        )
        session.execute(
            text(
                "insert into authors_proposition "
                "(parliamentarian_id, proposition_id) values (1, 100)"
            )
        )
        session.commit()
        _rodar(session, ClienteFake(_resposta(("educacao", 0.9))))

        session.execute(
            text(
                "insert into proposition (id, proposition_description) values "
                "(200, 'Cria unidade de conservação ambiental.')"
            )
        )
        session.execute(
            text(
                "insert into authors_proposition "
                "(parliamentarian_id, proposition_id, created_at, updated_at) "
                "values (1, 200, :quando, :quando)"
            ),
            {"quando": datetime.now(timezone.utc) + timedelta(hours=1)},
        )
        session.commit()

        cliente = ClienteFake(_resposta(("meio-ambiente", 0.8)))
        _rodar(session, cliente)

        assert len(cliente.chamadas) == 1
        assert [c[0] for c in _classificacoes(session, 1)] == ["meio-ambiente"]


class TestMetadados:
    def test_grava_modelo_confianca_e_evidencia(self, session: Session) -> None:
        _parlamentar(session, 1)
        _discurso(session, 1, summary="Clima.")

        _rodar(session, ClienteFake(_resposta(("meio-ambiente", 0.87))))

        linha = _classificacoes(session, 1)[0]
        assert float(linha[2]) == pytest.approx(0.87)
        assert linha[3] == "modelo-fake"
        assert linha[4] == 1

        evidencia = session.execute(
            text("select evidence from parliamentarian_agenda where rank = 1")
        ).scalar_one()
        assert evidencia == "trecho de meio-ambiente"

    def test_confianca_fora_da_faixa_e_limitada(self, session: Session) -> None:
        _parlamentar(session, 1)
        _discurso(session, 1, summary="Clima.")

        _rodar(session, ClienteFake(_resposta(("meio-ambiente", 4.2))))

        assert float(_classificacoes(session, 1)[0][2]) == pytest.approx(1.0)


class TestInterpretarResposta:
    VOCAB = (
        job.Pauta(id=10, name="Meio Ambiente", slug="meio-ambiente", description=None),
    )

    def test_aceita_json_embrulhado_em_bloco_de_codigo(self) -> None:
        bruto = '```json\n{"agendas": [{"slug": "meio-ambiente"}]}\n```'

        assert [c.slug for c in job.interpretar_resposta(bruto, self.VOCAB)] == [
            "meio-ambiente"
        ]

    def test_aceita_lista_no_topo(self) -> None:
        bruto = '[{"slug": "meio-ambiente"}]'

        assert len(job.interpretar_resposta(bruto, self.VOCAB)) == 1

    def test_slug_com_caixa_e_espacos_e_normalizado(self) -> None:
        bruto = '{"agendas": [{"slug": " Meio-Ambiente "}]}'

        assert len(job.interpretar_resposta(bruto, self.VOCAB)) == 1

    def test_resposta_vazia_devolve_lista_vazia(self) -> None:
        assert job.interpretar_resposta("", self.VOCAB) == []


class TestPrompt:
    def test_prompt_leva_as_descriptions(self, session: Session) -> None:
        """São elas que decidem a fronteira entre pautas vizinhas."""

        vocabulario = job.carregar_vocabulario(session)
        _, user_prompt = job.montar_prompt(vocabulario, "texto")

        assert "Clima." in user_prompt
        assert "SUS." in user_prompt
        assert "meio-ambiente" in user_prompt
