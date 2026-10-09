"""Tipos de dados usados na montagem e envio dos relatórios."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass(frozen=True)
class ProjectRecipient:
    """Destinatário resolvido a partir da tabela `projetos`."""

    id: int
    email: str
    nome: str
    cliente: Optional[str] = None
    tier_id: Optional[int] = None


@dataclass(frozen=True)
class FavoriteParliamentarian:
    """Parlamentar favorito do projeto com metadados para o e-mail."""

    id: int
    display_name: str
    chamber: str = ""


@dataclass
class DashboardStats:
    propositions_count: int = 0
    votes_count: int = 0
    speeches_count: int = 0
    amendments_count: int = 0
    attendance_avg_percent: Optional[int] = None


@dataclass
class ActivityItem:
    kind: str
    title: str
    subtitle: str
    parliamentarian_name: str
    ementa: Optional[str] = None
    link: Optional[str] = None
    occurred_at: Optional[date | datetime] = None
    # Para o link curto do compartilhamento: proposicao|votacao|discurso|emenda + id.
    kind_key: str = ""
    item_id: Optional[int] = None


@dataclass
class ProjectReport:
    recipient: ProjectRecipient
    parliamentarians: list[str] = field(default_factory=list)
    favorite_parliamentarians: list[FavoriteParliamentarian] = field(
        default_factory=list
    )
    stats: DashboardStats = field(default_factory=DashboardStats)
    highlights: list[ActivityItem] = field(default_factory=list)
    range_start: Optional[date] = None
    range_end: Optional[date] = None
