"""Linha do tempo eleitoral de um politico (TSE/DivulgaCandContas) — CS-54.

Uma linha por pessoa x eleicao x cargo, semeada do `eleicoesAnteriores` que a
DivulgaCandContas devolve no detalhe de cada candidatura. `tse_candidate_id`
e o id da pessoa NAQUELA eleicao (muda a cada ano); o vinculo estavel com a
pessoa e `parliamentarian_id` e/ou `candidacy_id` (candidatura 2026),
denormalizados em todas as linhas.

`assets_fetched_at` NULL significa patrimonio pendente de busca — mesmo papel
do fingerprint da tabela candidacy: falha de detalhe se auto-corrige na
proxima execucao. Depois da busca, `assets_status` diz o que o TSE tinha
(CS-95): `declarado`, `sem_bens` ou `nao_divulgado` (eleicao sem bens no TSE).
Sem bens, `declared_assets` fica NULL, nunca zero.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..base import Base


class ElectoralHistory(Base):
    __tablename__ = "electoral_history"
    # A chave natural do TSE inclui a unidade eleitoral. Ate 2008 os ids de
    # candidato eram sequenciais curtos, unicos so dentro de (ano, UE): em
    # 2006 o id 10354 e do Flavio Bolsonaro no RJ E de Manoel do Carmo no AC.
    # Sem `state` na chave as duas disputas colidiam numa linha so (CS-69).
    # `candidacy` ja usava a chave certa em uq_candidacy_election_state_tse_id.
    # NULLS NOT DISTINCT porque `state` e nullable: sem isso, duas linhas com
    # state nulo escapariam da unicidade.
    __table_args__ = (
        UniqueConstraint(
            "election_year",
            "state",
            "tse_candidate_id",
            name="uq_electoral_history_year_state_tse_id",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id = Column(BigInteger, primary_key=True, index=True)
    election_year = Column(Integer, nullable=False, index=True)
    tse_candidate_id = Column(BigInteger, nullable=False)
    tse_election_id = Column(BigInteger)

    parliamentarian_id = Column(
        BigInteger,
        ForeignKey("parliamentarian.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    candidacy_id = Column(
        BigInteger,
        ForeignKey("candidacy.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    office = Column(Text)
    state = Column(Text)
    locality = Column(Text)
    party = Column(Text)
    ballot_name = Column(Text)
    full_name = Column(Text)
    ballot_number = Column(Integer)
    result = Column(Text)

    declared_assets = Column(Numeric(18, 2))
    assets_count = Column(Integer)
    assets = Column(JSONB)
    assets_fetched_at = Column(DateTime)
    # NULL enquanto assets_fetched_at for NULL; valores em history_parsing.
    assets_status = Column(Text)

    source_link = Column(Text)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    parliamentarian = relationship(
        "Parliamentarian", back_populates="electoral_history"
    )
    candidacy = relationship("Candidacy", back_populates="electoral_history")


__all__ = ["ElectoralHistory"]
