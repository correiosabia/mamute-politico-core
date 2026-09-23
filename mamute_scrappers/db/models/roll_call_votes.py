"""Votações nominais dos parlamentares."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..base import Base


class RollCallVote(Base):
    __tablename__ = "roll_call_votes"

    id = Column(BigInteger, primary_key=True, index=True)
    parliamentarian_id = Column(
        BigInteger,
        ForeignKey("parliamentarian.id", ondelete="CASCADE"),
        nullable=False,
    )
    proposition_id = Column(
        BigInteger,
        ForeignKey("proposition.id", ondelete="CASCADE"),
        nullable=False,
    )
    vote = Column(Text)
    description = Column(Text)
    link = Column(Text)
    # Data em que a votação efetivamente ocorreu. Popular pelo crawler a partir
    # de dataHoraRegistro (Câmara) ou DataSessao (Senado). Nullable enquanto o
    # backfill_vote_dates ainda não populou os históricos.
    vote_date = Column(Date)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        # O crawler da Camara busca o voto gravado mais recente para saber de
        # onde retomar (migration cs102a1b2c3d4).
        index=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    parliamentarian = relationship("Parliamentarian", back_populates="roll_call_votes")
    proposition = relationship("Proposition", back_populates="roll_call_votes")


__all__ = ["RollCallVote"]

