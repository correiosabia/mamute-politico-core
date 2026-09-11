"""Pautas editoriais dos parlamentares — CS-72.

NAO CONFUNDIR COM TAG PESSOAL. `project_tag` / `parliamentarian_tag`
(SPEC-001) sao do assinante: ele escreve o texto que quiser, so ele enxerga, e
o sistema nunca interpreta o significado. Aqui e o oposto em todos os eixos —
o vocabulario e da redacao, e igual para todo mundo, e quem preenche e um job
de classificacao. Uma tabela responde "como ESTE assinante organiza os
politicos dele"; a outra responde "sobre o que ESTE politico fala".

Por isso nao ha `projeto_id` em lugar nenhum deste arquivo, e nao deve haver:
no dia em que aparecer, as duas features viraram uma so e a promessa de
privacidade das marcacoes some junto.

VOCABULARIO FECHADO. `EditorialAgenda` so ganha linha pelo painel admin. O
classificador recebe a lista e descarta slug que nao esteja nela; parlamentar
sem evidencia fica sem pauta, e nunca existe uma pauta generica de escape.

`slug` E CHAVE ESTAVEL. As classificacoes ja gravadas se referem a pauta por
ele, e o front filtra por ele. Editar slug de pauta existente quebra essa
correspondencia em silencio — a camada de servico proibe, e essa proibicao e
regra de dominio, nao validacao de tela.

`active` EM VEZ DE DELETE, pela mesma razao das marcacoes: mudar configuracao
nunca destroi trabalho. Pauta desativada some da API publica e fica dormente.

TENTATIVA E RESULTADO SAO COISAS DIFERENTES. `ParliamentarianAgendaRun` guarda
"olhei esta pessoa em tal dia", inclusive quando o resultado foi nenhuma pauta.
Sem ela, ausencia de linha em `parliamentarian_agenda` significa ao mesmo tempo
"nunca analisei" e "analisei e nada encaixou" — e o job, incapaz de distinguir,
reenvia a mesma pessoa ao modelo em toda rodada, para sempre, sem nunca gravar
nada. E gasto invisivel: nao deixa rastro em lugar nenhum para alguem notar.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..base import Base

# Teto de pautas por parlamentar. Tres e o limite do desenho: a tela mostra as
# dominantes, nao um indice de assuntos. O banco garante com ck_..._rank.
MAX_PAUTAS_POR_PARLAMENTAR = 3

# Versao inicial do vocabulario, semeada pela migration cs72a1b2c3d4.
VOCABULARY_VERSION_INICIAL = 1


class EditorialAgenda(Base):
    """Uma pauta do vocabulario fechado (ex.: "Meio Ambiente")."""

    __tablename__ = "editorial_agenda"

    id = Column(BigInteger, primary_key=True, index=True)
    name = Column(Text, nullable=False)
    slug = Column(Text, nullable=False, unique=True)
    # A `description` NAO e documentacao
    description = Column(Text)
    position = Column(SmallInteger, nullable=False, server_default="0")
    active = Column(Boolean, nullable=False, server_default="true")
    vocabulary_version = Column(
        Integer, nullable=False, server_default=str(VOCABULARY_VERSION_INICIAL)
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    parliamentarians = relationship(
        "ParliamentarianAgenda",
        back_populates="agenda",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ParliamentarianAgenda(Base):

    __tablename__ = "parliamentarian_agenda"
    __table_args__ = (
        CheckConstraint(
            f"rank BETWEEN 1 AND {MAX_PAUTAS_POR_PARLAMENTAR}",
            name="ck_parliamentarian_agenda_rank",
        ),
        UniqueConstraint(
            "parliamentarian_id", "agenda_id", name="uq_parliamentarian_agenda"
        ),
        UniqueConstraint(
            "parliamentarian_id", "rank", name="uq_parliamentarian_agenda_rank"
        ),
    )

    id = Column(BigInteger, primary_key=True, index=True)
    parliamentarian_id = Column(
        BigInteger,
        ForeignKey("parliamentarian.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agenda_id = Column(
        BigInteger,
        ForeignKey("editorial_agenda.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    rank = Column(SmallInteger, nullable=False)
    confidence = Column(Numeric(3, 2))
    evidence = Column(Text)
    model = Column(Text)
    vocabulary_version = Column(
        Integer, nullable=False, server_default=str(VOCABULARY_VERSION_INICIAL)
    )
    computed_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    agenda = relationship("EditorialAgenda", back_populates="parliamentarians")


RUN_CLASSIFICADO = "classificado"
RUN_SEM_PAUTA = "sem_pauta"
RUN_SEM_MATERIAL = "sem_material"


class ParliamentarianAgendaRun(Base):

    __tablename__ = "parliamentarian_agenda_run"

    parliamentarian_id = Column(
        BigInteger,
        ForeignKey("parliamentarian.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # `classificado` | `sem_pauta` | `sem_material`.
    outcome = Column(Text, nullable=False)
    source = Column(Text)
    model = Column(Text)
    vocabulary_version = Column(
        Integer, nullable=False, server_default=str(VOCABULARY_VERSION_INICIAL)
    )
    computed_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "EditorialAgenda",
    "ParliamentarianAgenda",
    "ParliamentarianAgendaRun",
    "MAX_PAUTAS_POR_PARLAMENTAR",
    "VOCABULARY_VERSION_INICIAL",
    "RUN_CLASSIFICADO",
    "RUN_SEM_PAUTA",
    "RUN_SEM_MATERIAL",
]
