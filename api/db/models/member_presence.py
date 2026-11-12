"""Presença de membros logados (CS-87): quem está usando a plataforma agora.

- `member_presence`: uma linha por membro, com o último sinal e a localidade
  estimada pelo IP naquele sinal. É o que alimenta "ativos agora".
- `member_presence_hour`: uma linha por membro e hora cheia (UTC) em que ele
  deu sinal. É o que alimenta o quadro de horários, para escolher janela de
  manutenção.

O IP nunca é gravado. As duas tabelas perdem as linhas com mais de 90 dias
(`services.acessos.limpar_localidades_antigas`).
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Float, Index, Text

from ..base import Base


class MemberPresence(Base):
    __tablename__ = "member_presence"
    __table_args__ = (Index("ix_member_presence_visto", "last_seen_at"),)

    email = Column(Text, primary_key=True)
    projeto_id = Column(BigInteger, nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    country = Column(Text)
    region = Column(Text)
    city = Column(Text)
    lat = Column(Float)
    lon = Column(Float)


class MemberPresenceHour(Base):
    __tablename__ = "member_presence_hour"

    hora = Column(DateTime(timezone=True), primary_key=True)
    email = Column(Text, primary_key=True)


__all__ = ["MemberPresence", "MemberPresenceHour"]
