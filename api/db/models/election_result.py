"""Resultado oficial da eleicao (CS-106) — espelho da API.

Tabelas escritas pelo coletor `tse_crawler.resultados` e pelo envio
`scripts/notificacao/resultado_eleicao.py`; a API le o resultado e so escreve
o `seen_at` do aviso (modal fechado). Migration: cs106a1b2c3d4.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

from ..base import Base

# JSONB no Postgres, JSON nos testes em SQLite.
_JSON = JSON().with_variant(JSONB(), "postgresql")


class CandidacyResult(Base):
    __tablename__ = "candidacy_result"
    __table_args__ = (
        UniqueConstraint("candidacy_id", "turno", name="uq_candidacy_result_turno"),
    )

    id = Column(BigInteger, primary_key=True)
    candidacy_id = Column(BigInteger, nullable=False)
    turno = Column(SmallInteger, nullable=False)
    codigo_eleicao = Column(Integer, nullable=False)
    situacao = Column(Text)
    eleito = Column(Boolean)
    votos = Column(BigInteger)
    percentual = Column(Numeric(7, 4))
    destinacao_voto = Column(Text)
    totalizacao_final = Column(Boolean, nullable=False, default=False)
    tse_atualizado_em = Column(DateTime(timezone=True))
    coletado_em = Column(DateTime(timezone=True))


class ElectionResultNotice(Base):
    __tablename__ = "election_result_notice"
    __table_args__ = (
        UniqueConstraint(
            "projeto_id", "ciclo", "turno", "disparo", name="uq_election_result_notice_disparo"
        ),
    )

    id = Column(BigInteger, primary_key=True)
    projeto_id = Column(BigInteger, nullable=False)
    ciclo = Column(Text, nullable=False)
    turno = Column(SmallInteger, nullable=False)
    # CS-119: "majoritarios" ou "completo" (o 2o turno so tem "completo").
    disparo = Column(Text, nullable=False, default="completo")
    payload = Column(_JSON, nullable=False)
    email_status = Column(Text, nullable=False)
    tentativas = Column(SmallInteger, nullable=False, default=0)
    ultimo_erro = Column(Text)
    created_at = Column(DateTime(timezone=True))
    sent_at = Column(DateTime(timezone=True))
    seen_at = Column(DateTime(timezone=True))
