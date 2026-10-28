"""Associação entre parlamentares e proposições."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from ..base import Base


class AuthorsProposition(Base):
    __tablename__ = "authors_proposition"

    id = Column(BigInteger, primary_key=True)
    parliamentarian_id = Column(
        BigInteger,
        ForeignKey("parliamentarian.id", ondelete="CASCADE"),
        nullable=False,
    )
    proposition_id = Column(
        BigInteger,
        ForeignKey("proposition.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    parliamentarian = relationship("Parliamentarian", back_populates="authorships")
    proposition = relationship("Proposition", back_populates="authors")


__all__ = ["AuthorsProposition"]

