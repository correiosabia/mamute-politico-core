"""Oportunidade de participação: consulta, ideia, enquete ou abaixo-assinado (CS-179)."""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from ..base import Base


class ParticipationItem(Base):
    __tablename__ = "participation_item"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_participation_item"),)

    id = Column(BigInteger, primary_key=True, index=True)
    source = Column(Text, nullable=False)
    external_id = Column(Text, nullable=False)
    kind = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    summary = Column(Text, nullable=True)
    reference = Column(Text, nullable=True)
    organization = Column(Text, nullable=True)
    url = Column(Text, nullable=False)
    opens_on = Column(Date, nullable=True)
    closes_on = Column(Date, nullable=True)
    score = Column(JSONB, nullable=True)
    score_updated_at = Column(DateTime(timezone=True), nullable=True)
    source_open = Column(Boolean, nullable=False, default=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(Text, nullable=False, default="pendente")
    theme = Column(Text, nullable=True)
    note = Column(Text, nullable=True)
    reviewed_by = Column(Text, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
