"""Modelo de presença em plenário."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..base import Base


class PlenaryAttendance(Base):
    __tablename__ = "plenary_attendance"

    id = Column(BigInteger, primary_key=True)
    parliamentarian_id = Column(
        BigInteger,
        ForeignKey("parliamentarian.id", ondelete="CASCADE"),
        nullable=False,
    )
    date = Column(Date)
    description = Column(Text)
    session_attendance = Column(Text)
    daily_attendance_justification = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    parliamentarian = relationship("Parliamentarian", back_populates="plenary_attendances")


__all__ = ["PlenaryAttendance"]

