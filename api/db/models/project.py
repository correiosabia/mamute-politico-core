"""Modelos relacionados aos projetos, tiers e favoritos de parlamentares."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..base import Base


class Tiers(Base):
    __tablename__ = "tiers"

    id = Column(BigInteger, primary_key=True, index=True)
    tier_name_debug = Column(Text, nullable=False)
    product_id = Column(Text, nullable=False, unique=True)
    detalhes = Column(
        MutableDict.as_mutable(JSONB),
        default=dict,
        nullable=False,
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    projetos = relationship(
        "Projetos",
        back_populates="tier",
        cascade="all, delete-orphan",
    )

    def _ensure_detalhes(self) -> None:
        if self.detalhes is None:
            self.detalhes = {}
        elif not isinstance(self.detalhes, dict):
            self.detalhes = dict(self.detalhes)

    def _get_detail(self, key: str, default: Any = None) -> Any:
        if not self.detalhes:
            return default
        return self.detalhes.get(key, default)

    def _set_detail(self, key: str, value: Any) -> None:
        self._ensure_detalhes()
        if value is None:
            self.detalhes.pop(key, None)
        else:
            self.detalhes[key] = value

    @property
    def qtd_termos(self) -> Optional[int]:
        return self._get_detail("qtd_termos")

    @qtd_termos.setter
    def qtd_termos(self, value: Optional[int]) -> None:
        self._set_detail("qtd_termos", value)

    @property
    def qtd_termos_camara(self) -> Optional[int]:
        return self._get_detail("qtd_termos_camara")

    @qtd_termos_camara.setter
    def qtd_termos_camara(self, value: Optional[int]) -> None:
        self._set_detail("qtd_termos_camara", value)

    @property
    def qtd_termos_senado(self) -> Optional[int]:
        return self._get_detail("qtd_termos_senado")

    @qtd_termos_senado.setter
    def qtd_termos_senado(self, value: Optional[int]) -> None:
        self._set_detail("qtd_termos_senado", value)

    @property
    def orgao(self) -> Optional[list[str]]:
        return self._get_detail("orgao", [])

    @orgao.setter
    def orgao(self, value: Optional[list[str]]) -> None:
        self._set_detail("orgao", value)

    @property
    def qtd_email(self) -> Optional[int]:
        return self._get_detail("qtd_email")

    @qtd_email.setter
    def qtd_email(self, value: Optional[int]) -> None:
        self._set_detail("qtd_email", value)

    @property
    def periodicidade_email(self) -> Optional[list[str]]:
        return self._get_detail("periodicidade_email", [])

    @periodicidade_email.setter
    def periodicidade_email(self, value: Optional[list[str]]) -> None:
        self._set_detail("periodicidade_email", value)


class Projetos(Base):
    __tablename__ = "projetos"
    __table_args__ = (
        UniqueConstraint("email", name="uq_projetos_email"),
    )

    id = Column(BigInteger, primary_key=True, index=True)
    nome = Column(Text, nullable=False)
    cliente = Column(Text, nullable=True)
    email = Column(Text, nullable=False)
    tier_id = Column(
        BigInteger,
        ForeignKey("tiers.id", ondelete="SET NULL"),
        nullable=True,
    )
    tag_ghost = Column(Text, nullable=True)
    qtd_termos = Column(
        Integer,
        nullable=False,
        server_default="0",
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    tier = relationship(
        "Tiers",
        back_populates="projetos",
    )
    favoritos = relationship(
        "ProjetosParliamentarian",
        back_populates="projeto",
        cascade="all, delete-orphan",
    )


class ProjetosParliamentarian(Base):
    __tablename__ = "projetos_parliamentarian"
    __table_args__ = (
        UniqueConstraint(
            "projeto_id",
            "parliamentarian_id",
            name="uq_projeto_parliamentarian_unique",
        ),
    )

    id = Column(BigInteger, primary_key=True, index=True)
    projeto_id = Column(
        BigInteger,
        ForeignKey("projetos.id", ondelete="CASCADE"),
        nullable=False,
    )
    parliamentarian_id = Column(
        BigInteger,
        ForeignKey("parliamentarian.id", ondelete="CASCADE"),
        nullable=False,
    )
    
    position = Column(Integer, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    projeto = relationship(
        "Projetos",
        back_populates="favoritos",
    )
    parliamentarian = relationship(
        "Parliamentarian",
        back_populates="favoritos",
    )


class ProjetosCandidacy(Base):
    """Candidatura que o assinante escolheu acompanhar (eleicao 2026).

    Apenas o REGISTRO da escolha: nenhuma feature consome este vinculo ainda
    (decisao de produto em 2026-08-23) — o dado existe para, na apuracao,
    podermos contar "voce acompanhou N candidatos, M foram eleitos". Sem cota
    e sem soft-delete: desmarcar apaga a linha. O que estava selecionado no
    fechamento de cada turno fica em `projetos_candidacy_turno` (CS-129), que
    nao muda quando a pessoa desmarca.
    """

    __tablename__ = "projetos_candidacy"
    __table_args__ = (
        UniqueConstraint(
            "projeto_id",
            "candidacy_id",
            name="uq_projetos_candidacy_unique",
        ),
    )

    id = Column(BigInteger, primary_key=True, index=True)
    projeto_id = Column(
        BigInteger,
        ForeignKey("projetos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidacy_id = Column(
        BigInteger,
        ForeignKey("candidacy.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


__all__ = ["Projetos", "ProjetosCandidacy", "ProjetosParliamentarian", "Tiers"]

