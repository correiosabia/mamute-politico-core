"""Entidades nomeadas extraídas dos discursos do Senado."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..base import Base


class SpeechesTranscriptsEntity(Base):
    __tablename__ = "speeches_transcripts_entities"

    id = Column(BigInteger, primary_key=True)
    speeches_transcripts_id = Column(
        BigInteger,
        ForeignKey("speeches_transcripts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label = Column(Text, nullable=False)
    text = Column(Text, nullable=False)
    start_char = Column(Integer, nullable=True)
    end_char = Column(Integer, nullable=True)
    analysis_type = Column(Text, nullable=False, server_default="spacy")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    speech = relationship("SpeechesTranscript", back_populates="entities")


__all__ = ["SpeechesTranscriptsEntity"]

