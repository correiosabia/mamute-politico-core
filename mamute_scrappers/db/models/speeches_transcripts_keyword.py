"""Palavras-chave extraídas dos discursos do Senado."""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import expression, func

from ..base import Base


class SpeechesTranscriptsKeyword(Base):
    __tablename__ = "speeches_transcripts_keywords"

    id = Column(BigInteger, primary_key=True)
    speeches_transcripts_id = Column(
        BigInteger,
        ForeignKey("speeches_transcripts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    keyword = Column(Text, nullable=False)
    term = Column(Text, nullable=False)
    frequency = Column(Integer, nullable=False)
    rank = Column(Integer, nullable=False)
    is_primary = Column(
        Boolean,
        nullable=False,
        server_default=expression.false(),
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    analysis_type = Column(Text, nullable=False, server_default="spacy")
    speech = relationship("SpeechesTranscript", back_populates="keywords")


__all__ = ["SpeechesTranscriptsKeyword"]
