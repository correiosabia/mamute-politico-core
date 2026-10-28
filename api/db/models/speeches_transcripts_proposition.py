"""Associação entre discursos e proposições relacionadas."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..base import Base


class SpeechesTranscriptsProposition(Base):
    __tablename__ = "speeches_transcripts_proposition"

    id = Column(BigInteger, primary_key=True)
    speeches_transcripts_id = Column(
        BigInteger,
        ForeignKey("speeches_transcripts.id", ondelete="CASCADE"),
        nullable=False,
    )
    proposition_id = Column(
        BigInteger,
        ForeignKey("proposition.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    speech = relationship("SpeechesTranscript", back_populates="propositions")
    proposition = relationship("Proposition", back_populates="speeches_links")


__all__ = ["SpeechesTranscriptsProposition"]

