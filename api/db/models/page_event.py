"""Eventos anônimos de quem lê uma página pública do admin (CS-166).

Irmã de `collection_event` (CS-145), com a chave da página (`page`) no lugar da
coleção. Sem IP e sem login: `visitor_id` e `session_id` são aleatórios,
gerados no navegador. A região é estimada pelo IP na hora da gravação.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, Text
from sqlalchemy.sql import func

from ..base import Base


class PageEvent(Base):
    __tablename__ = "page_event"
    __table_args__ = (Index("ix_page_event_pagina_data", "page", "created_at"),)

    id = Column(BigInteger, primary_key=True)
    page = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    visitor_id = Column(Text, nullable=False)
    session_id = Column(Text, nullable=False)
    event_type = Column(Text, nullable=False)
    target = Column(Text)
    detail = Column(Text)
    duration_ms = Column(Integer)
    device = Column(Text)
    country = Column(Text)
    region = Column(Text)
    city = Column(Text)
    referrer_host = Column(Text)
    utm_source = Column(Text)
    utm_medium = Column(Text)
    utm_campaign = Column(Text)


__all__ = ["PageEvent"]
