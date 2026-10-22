"""Coleções curadas de políticos (CS-132).

Uma coleção reúne pessoas em torno de um tema e intercala blocos de conteúdo
escolhidos pelo admin. A pessoa (`CollectionMember`) pode ser parlamentar,
candidato ou alguém fora da base; o vínculo com o resto do schema é resolvido
na leitura (ver `services/collections.py`).
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from ..base import Base

_JSON = JSON().with_variant(JSONB(), "postgresql")

STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"

# Blocos que apontam para um registro da base guardam o id em `ref_id`.
BLOCK_KIND_TEXT = "text"
BLOCK_KIND_SPEECH = "speech"
BLOCK_KIND_VOTE = "vote"
BLOCK_KIND_PROPOSITION = "proposition"
BLOCK_KIND_EXPENSE = "expense"
BLOCK_KIND_LINK = "link"
BLOCK_KIND_DOCUMENT = "document"
BLOCK_KIND_CHART = "chart"

BLOCK_KINDS = (
    BLOCK_KIND_TEXT,
    BLOCK_KIND_SPEECH,
    BLOCK_KIND_VOTE,
    BLOCK_KIND_PROPOSITION,
    BLOCK_KIND_EXPENSE,
    BLOCK_KIND_LINK,
    BLOCK_KIND_DOCUMENT,
    BLOCK_KIND_CHART,
)
REF_BLOCK_KINDS = (
    BLOCK_KIND_SPEECH,
    BLOCK_KIND_VOTE,
    BLOCK_KIND_PROPOSITION,
    BLOCK_KIND_EXPENSE,
)


class Collection(Base):
    __tablename__ = "collection"

    id = Column(BigInteger, primary_key=True, index=True)
    slug = Column(Text, nullable=False, unique=True)
    title = Column(Text, nullable=False)
    subtitle = Column(Text)
    summary = Column(Text)
    status = Column(Text, nullable=False, default=STATUS_DRAFT)
    # {"1": "rótulo", "2": "..."}: o significado de cada tier é dado da coleção.
    tier_labels = Column(_JSON, nullable=False, default=dict)
    settings = Column(_JSON, nullable=False, default=dict)
    published_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CollectionMember(Base):
    __tablename__ = "collection_member"

    id = Column(BigInteger, primary_key=True, index=True)
    collection_id = Column(
        BigInteger, ForeignKey("collection.id", ondelete="CASCADE"), nullable=False
    )
    display_name = Column(Text, nullable=False)
    # Para quem não está na base: "Ministro do STF", "Ex-governador".
    role_label = Column(Text)
    cpf = Column(Text)
    parliamentarian_id = Column(
        BigInteger, ForeignKey("parliamentarian.id", ondelete="SET NULL")
    )
    candidacy_id = Column(BigInteger, ForeignKey("candidacy.id", ondelete="SET NULL"))
    tier = Column(SmallInteger)
    context = Column(Text)
    # [{"label": "...", "url": "..."}]
    sources = Column(_JSON, nullable=False, default=list)
    # Foto pública para quem não tem foto no cadastro, com o crédito que a licença pede.
    photo_url = Column(Text)
    photo_credit = Column(Text)
    position = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CollectionBlock(Base):
    __tablename__ = "collection_block"

    id = Column(BigInteger, primary_key=True, index=True)
    collection_id = Column(
        BigInteger, ForeignKey("collection.id", ondelete="CASCADE"), nullable=False
    )
    member_id = Column(
        BigInteger, ForeignKey("collection_member.id", ondelete="SET NULL")
    )
    kind = Column(Text, nullable=False)
    ref_id = Column(BigInteger)
    title = Column(Text)
    body = Column(Text)
    url = Column(Text)
    payload = Column(_JSON, nullable=False, default=dict)
    position = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
