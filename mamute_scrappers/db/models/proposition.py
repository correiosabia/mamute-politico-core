"""Modelo principal de proposições legislativas."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, Date, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import deferred, relationship
from sqlalchemy.sql import func

from ..base import Base


class Proposition(Base):
    __tablename__ = "proposition"

    id = Column(BigInteger, primary_key=True, index=True)
    proposition_code = Column(BigInteger)
    title = Column(Text)
    link = Column(Text)
    proposition_acronym = Column(Text)
    proposition_number = Column(Integer)
    presentation_year = Column(Integer)
    agency_id = Column(BigInteger, ForeignKey("agency.id", ondelete="SET NULL"))
    proposition_type_id = Column(
        BigInteger,
        ForeignKey("proposition_type.id", ondelete="SET NULL"),
    )
    proposition_status_id = Column(
        BigInteger,
        ForeignKey("proposition_status.id", ondelete="SET NULL"),
    )
    current_status = Column(Text)
    proposition_description = Column(Text)
    presentation_date = Column(Date)
    presentation_month = Column(Integer)
    summary = Column(Text)
    details = Column(JSONB)
    # Áreas temáticas oficiais da Casa (lista de nomes, na ordem da fonte).
    # NULL = ainda não coletado; [] = coletado, a Casa não classificou.
    # `deferred`: fica fora dos SELECTs por padrão, então quem não lê temas
    # não quebra na janela do deploy em que a migration cs92 ainda não rodou.
    themes = deferred(Column(JSONB))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    agency = relationship("Agency", back_populates="propositions")
    proposition_type = relationship("PropositionType", back_populates="propositions")
    proposition_status = relationship("PropositionStatus", back_populates="propositions")
    authors = relationship(
        "AuthorsProposition",
        back_populates="proposition",
        cascade="all, delete-orphan",
    )
    roll_call_votes = relationship(
        "RollCallVote",
        back_populates="proposition",
        cascade="all, delete-orphan",
    )
    speeches_links = relationship(
        "SpeechesTranscriptsProposition",
        back_populates="proposition",
        cascade="all, delete-orphan",
    )


__all__ = ["Proposition"]

