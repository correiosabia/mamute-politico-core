"""Modelos relacionados aos parlamentares."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..base import Base


class Parliamentarian(Base):
    __tablename__ = "parliamentarian"

    id = Column(BigInteger, primary_key=True)
    type = Column(Text)
    parliamentarian_code = Column(BigInteger)
    name = Column(Text)
    full_name = Column(Text)
    email = Column(Text)
    telephone = Column(Text)
    cpf = Column(Text)
    status = Column(Text)
    party = Column(Text)
    state_of_birth = Column(Text)
    city_of_birth = Column(Text)
    state_elected = Column(Text)
    site = Column(Text)
    education = Column(Text)
    office_name = Column(Text)
    office_building = Column(Text)
    office_number = Column(Text)
    office_floor = Column(Text)
    office_email = Column(Text)
    biography_link = Column(Text)
    biography_text = Column(Text)
    details = Column(JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    social_networks = relationship(
        "ParliamentarianSocialNetwork",
        back_populates="parliamentarian",
        cascade="all, delete-orphan",
    )
    plenary_attendances = relationship(
        "PlenaryAttendance",
        back_populates="parliamentarian",
        cascade="all, delete-orphan",
    )
    committee_attendances = relationship(
        "CommitteeAttendance",
        back_populates="parliamentarian",
        cascade="all, delete-orphan",
    )
    authorships = relationship(
        "AuthorsProposition",
        back_populates="parliamentarian",
        cascade="all, delete-orphan",
    )
    roll_call_votes = relationship(
        "RollCallVote",
        back_populates="parliamentarian",
        cascade="all, delete-orphan",
    )
    speeches = relationship(
        "SpeechesTranscript",
        back_populates="parliamentarian",
        cascade="all, delete-orphan",
    )
    videos_audios = relationship(
        "VideoAudio",
        back_populates="parliamentarian",
        cascade="all, delete-orphan",
    )
    favoritos = relationship(
        "ProjetosParliamentarian",
        back_populates="parliamentarian",
        cascade="all, delete-orphan",
    )

    amendments = relationship(
        "ParliamentaryAmendment",
        back_populates="parliamentarian",
    )

    expenses = relationship(
        "ParliamentaryExpense",
        back_populates="parliamentarian",
    )

    candidacies = relationship(
        "Candidacy",
        back_populates="parliamentarian",
    )

    electoral_history = relationship(
        "ElectoralHistory",
        back_populates="parliamentarian",
    )


__all__ = ["Parliamentarian"]

