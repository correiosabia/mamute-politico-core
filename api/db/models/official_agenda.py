"""Compromisso publicado na agenda oficial de uma autoridade (CS-135)."""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, Integer, Text, UniqueConstraint
from sqlalchemy.sql import func

from ..base import Base


class OfficialAgendaItem(Base):
    __tablename__ = "official_agenda_item"
    __table_args__ = (
        UniqueConstraint("source", "authority_id", "event_date", "seq", name="uq_official_agenda_item"),
    )

    id = Column(BigInteger, primary_key=True, index=True)
    source = Column(Text, nullable=False)
    authority_id = Column(Text, nullable=False)
    authority_name = Column(Text, nullable=True)
    office = Column(Text, nullable=True)
    office_label = Column(Text, nullable=True)
    event_date = Column(Date, nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    starts_at = Column(Text, nullable=True)
    ends_at = Column(Text, nullable=True)
    description = Column(Text, nullable=False)
    place = Column(Text, nullable=True)
    remote = Column(Boolean, nullable=False, default=False)
    url = Column(Text, nullable=True)
    organization = Column(Text, nullable=True)
    participants = Column(Text, nullable=True)
    collected_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
