"""Relatório de exemplo para os testes de render do e-mail."""
from __future__ import annotations

from datetime import date

from mamute_scrappers.scripts.notificacao.config import EmailBranding
from mamute_scrappers.scripts.notificacao.models import (
    ActivityItem,
    DashboardStats,
    FavoriteParliamentarian,
    ProjectRecipient,
    ProjectReport,
)

BRANDING = EmailBranding(
    app_url="https://mamutepolitico.com.br",
    logo_url="https://mamutepolitico.com.br/logo.png",
    banner_url="https://mamutepolitico.com.br/logo.png",
    privacy_url="https://mamutepolitico.com.br/privacidade",
    manage_url="https://mamutepolitico.com.br/app",
)

ANA = FavoriteParliamentarian(id=10, display_name="Ana Souza", chamber="Câmara")
BIA = FavoriteParliamentarian(id=20, display_name="Bia <Lima>", chamber="Senado")


def relatorio() -> ProjectReport:
    destaques = [
        ActivityItem(
            kind="proposição",
            title="PL 10/2026",
            subtitle="Apresentada em 01/10/2026",
            parliamentarian_name="Ana Souza",
            ementa="Institui o dia do mamute.",
            link="https://www.camara.leg.br/p/1",
            occurred_at=date(2026, 10, 1),
            kind_key="proposicao",
            item_id=1,
        ),
        ActivityItem(
            kind="discurso",
            title='Defende a "ponte" & a estrada.',
            subtitle="02/10/2026",
            parliamentarian_name="Bia <Lima>",
            link="https://www25.senado.leg.br/d/2",
            occurred_at=date(2026, 10, 2),
            kind_key="discurso",
            item_id=2,
        ),
    ]
    return ProjectReport(
        recipient=ProjectRecipient(id=1, email="ana@x.com", nome="Maria_Projeto", tier_id=1),
        parliamentarians=["Ana Souza", "Bia <Lima>"],
        favorite_parliamentarians=[ANA, BIA],
        stats=DashboardStats(propositions_count=1, votes_count=0, speeches_count=1, amendments_count=0),
        highlights=destaques,
        range_start=date(2026, 9, 25),
        range_end=date(2026, 10, 9),
    )
