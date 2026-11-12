"""Eventos de uso do sistema (navegação, trocas de parlamentares)."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Float, Index, Text
from sqlalchemy.sql import func

from ..base import Base


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_projeto_type", "projeto_id", "event_type"),
        Index("ix_usage_events_created_at", "created_at"),
    )

    id = Column(BigInteger, primary_key=True, index=True)
    projeto_id = Column(BigInteger, nullable=True)
    email = Column(Text, nullable=True)
    event_type = Column(Text, nullable=False)  # page_view | favorite_added | favorite_removed | section_view
    page = Column(Text, nullable=True)
    section = Column(Text, nullable=True)
    parliamentarian_id = Column(BigInteger, nullable=True)
    # Localidade estimada pelo IP na gravação (CS-87). O IP não é guardado. Depois de
    # 90 dias estes campos são apagados (`services.acessos.limpar_localidades_antigas`).
    country = Column(Text, nullable=True)
    region = Column(Text, nullable=True)
    city = Column(Text, nullable=True)
    lat = Column(Float, nullable=True)
    lon = Column(Float, nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["UsageEvent"]
