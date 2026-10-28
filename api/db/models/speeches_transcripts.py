"""Discursos e notas taquigráficas dos parlamentares."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..base import Base


class SpeechesTranscript(Base):
    __tablename__ = "speeches_transcripts"

    id = Column(BigInteger, primary_key=True)
    parliamentarian_id = Column(
        BigInteger,
        ForeignKey("parliamentarian.id", ondelete="CASCADE"),
        nullable=False,
        # Toda consulta de "o que este parlamentar fez" filtra por aqui
        # (migration cs74a1b2c3d4).
        index=True,
    )
    date = Column(Date)
    session_number = Column(Text)
    type = Column(Text)
    speech_link = Column(Text)
    speech_text = Column(Text)
    summary = Column(Text)
    hour_minute = Column(Text)
    publication_link = Column(Text)
    publication_text = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    parliamentarian = relationship("Parliamentarian", back_populates="speeches")
    propositions = relationship(
        "SpeechesTranscriptsProposition",
        back_populates="speech",
        cascade="all, delete-orphan",
    )
    keywords = relationship(
        "SpeechesTranscriptsKeyword",
        back_populates="speech",
        cascade="all, delete-orphan",
    )
    entities = relationship(
        "SpeechesTranscriptsEntity",
        back_populates="speech",
        cascade="all, delete-orphan",
    )


__all__ = ["SpeechesTranscript"]

