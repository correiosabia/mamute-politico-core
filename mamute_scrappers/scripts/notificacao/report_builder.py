"""Montagem do HTML do relatório por projeto."""

from __future__ import annotations

import html
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from .config import (
    PERIODICIDADE_TESTE,
    PERIOD_DAYS,
    EmailBranding,
    get_branding,
    subject_for_periodicidade,
)
from .flags import (
    FLAG_BALANCO,
    FLAG_CONVITE,
    FLAG_DESIGN_NOVO,
    FLAG_DESTAQUES_GERAIS,
    FlagSnapshot,
)
from .models import (
    ActivityItem,
    DashboardStats,
    FavoriteParliamentarian,
    GeneralHighlights,
    ParliamentarianBalance,
    ProjectRecipient,
    ProjectReport,
)
from .repository import (
    date_range_for_period,
    fetch_mixed_highlights_all_time,
    fetch_project_highlights,
    list_favorite_parliamentarians,
)
from .stats import compute_dashboard_stats, compute_dashboard_stats_all_time

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "report.html"

_PERIOD_LABELS = {
    "day": "último dia",
    "week": "últimos 7 dias",
    "fortnight": "últimos 15 dias",
    "month": "últimos 30 dias",
    "total": "amostra recente (teste, até 10 itens por parlamentar)",
}

# Paleta para distinguir parlamentares (estilos inline — compatível com clientes de e-mail).
_MP_PALETTE = (
    {"border": "#2563eb", "bg": "#eff6ff", "title": "#1e40af", "link": "#2563eb"},
    {"border": "#059669", "bg": "#ecfdf5", "title": "#047857", "link": "#059669"},
    {"border": "#d97706", "bg": "#fffbeb", "title": "#b45309", "link": "#d97706"},
    {"border": "#7c3aed", "bg": "#f5f3ff", "title": "#5b21b6", "link": "#7c3aed"},
    {"border": "#db2777", "bg": "#fdf2f8", "title": "#9d174d", "link": "#db2777"},
    {"border": "#0891b2", "bg": "#ecfeff", "title": "#0e7490", "link": "#0891b2"},
    {"border": "#dc2626", "bg": "#fef2f2", "title": "#b91c1c", "link": "#dc2626"},
    {"border": "#4f46e5", "bg": "#eef2ff", "title": "#3730a3", "link": "#4f46e5"},
    {"border": "#65a30d", "bg": "#f7fee7", "title": "#4d7c0f", "link": "#65a30d"},
    {"border": "#0d9488", "bg": "#f0fdfa", "title": "#0f766e", "link": "#0d9488"},
)


def _favorites_with_highlights(
    favorites: list[FavoriteParliamentarian],
    highlights: list[ActivityItem],
) -> list[FavoriteParliamentarian]:
    """Parlamentares que entraram nos destaques do período (ordem dos favoritos)."""
    active_names = {item.parliamentarian_name for item in highlights}
    return [fav for fav in favorites if fav.display_name in active_names]


@dataclass
class EnvioContexto:
    """O que vale para o envio inteiro: flags, admins e os destaques gerais.

    Lido uma vez por envio. O padrão (tudo desligado) reproduz o relatório de
    sempre, que é o que acontece se ninguém ligar as flags.
    """

    flags: FlagSnapshot = field(default_factory=FlagSnapshot)
    admins: frozenset[str] = frozenset()
    geral: Optional[GeneralHighlights] = None
    settings: dict[str, str] = field(default_factory=dict)

    def ativa(self, key: str, recipient: ProjectRecipient) -> bool:
        return self.flags.ativa(key, recipient, self.admins)


# As flags do e-mail são do relatório quinzenal: o texto fala em "quinzena" e
# o diário com destaques gerais viraria um e-mail igual por dia.
PERIODICIDADE_DAS_FLAGS = "fortnight"


def _aplicar_flags(
    report: ProjectReport, contexto: EnvioContexto, recipient: ProjectRecipient, quinzenal: bool
) -> ProjectReport:
    report.mostrar_convite = quinzenal and contexto.ativa(FLAG_CONVITE, recipient)
    report.design_novo = quinzenal and contexto.ativa(FLAG_DESIGN_NOVO, recipient)
    return report


def _balanco(
    session: Session,
    favorites: list[FavoriteParliamentarian],
    highlights: list[ActivityItem],
    **periodo,
) -> list[ParliamentarianBalance]:
    """Todos os selecionados, inclusive quem não teve atividade (dizendo isso)."""
    linhas: list[ParliamentarianBalance] = []
    for favorite in favorites:
        destaque = next(
            (item for item in highlights if item.parliamentarian_name == favorite.display_name),
            None,
        )
        linhas.append(
            ParliamentarianBalance(
                favorite=favorite,
                stats=compute_dashboard_stats(session, [favorite.id], **periodo),
                destaque=destaque,
            )
        )
    return linhas


def build_project_report(
    session: Session,
    recipient: ProjectRecipient,
    periodicidade: str,
    *,
    highlight_limit: int = 9,
    branding: EmailBranding | None = None,
    contexto: EnvioContexto | None = None,
) -> ProjectReport | None:
    """Monta dados do relatório.

    Sem favoritos devolve None (conta pulada), a não ser que a flag dos
    destaques gerais esteja ligada para a conta.
    """
    contexto = contexto or EnvioContexto()
    quinzenal = periodicidade == PERIODICIDADE_DAS_FLAGS
    # Sem conteúdo geral (falhou ou a quinzena veio vazia) volta a pular.
    gerais = (
        quinzenal
        and contexto.geral is not None
        and not contexto.geral.vazio
        and contexto.ativa(FLAG_DESTAQUES_GERAIS, recipient)
    )
    favorites = list_favorite_parliamentarians(session, recipient.id)
    if not favorites:
        if not gerais:
            return None
        range_start, range_end, _, _ = date_range_for_period(PERIOD_DAYS[periodicidade])
        report = ProjectReport(
            recipient=recipient,
            range_start=range_start,
            range_end=range_end,
            motivo_geral="sem_selecao",
            geral=contexto.geral,
        )
        return _aplicar_flags(report, contexto, recipient, quinzenal)

    parliamentarian_ids = [fav.id for fav in favorites]

    range_start: Optional[date] = None
    range_end: Optional[date] = None

    include_ingested = periodicidade == "day"

    if periodicidade == PERIODICIDADE_TESTE:
        highlights = fetch_mixed_highlights_all_time(
            session, favorites, highlight_limit
        )
        stats = compute_dashboard_stats_all_time(session, parliamentarian_ids)
        range_start, range_end = None, None
    else:
        past_days = PERIOD_DAYS[periodicidade]
        if past_days is None:
            raise ValueError(f"Periodicidade sem janela de dias: {periodicidade!r}")

        range_start, range_end, range_start_dt, range_end_dt = date_range_for_period(
            past_days
        )
        stats = compute_dashboard_stats(
            session,
            parliamentarian_ids,
            range_start,
            range_end,
            range_start_dt=range_start_dt,
            range_end_dt_exclusive=range_end_dt,
            include_ingested_propositions=include_ingested,
        )
        highlights = fetch_project_highlights(
            session,
            favorites,
            limit_total=highlight_limit,
            all_time=False,
            include_ingested_propositions=include_ingested,
            range_start=range_start,
            range_end=range_end,
            range_start_dt=range_start_dt,
            range_end_dt_exclusive=range_end_dt,
        )

    active_favorites = _favorites_with_highlights(favorites, highlights)

    report = ProjectReport(
        recipient=recipient,
        parliamentarians=[fav.display_name for fav in active_favorites],
        favorite_parliamentarians=active_favorites,
        stats=stats,
        highlights=highlights,
        range_start=range_start,
        range_end=range_end,
    )
    if gerais and not report.tem_atividade:
        report.motivo_geral = "sem_atividade"
        report.geral = contexto.geral
    if quinzenal and contexto.ativa(FLAG_BALANCO, recipient):
        report.balanco = _balanco(
            session,
            favorites,
            highlights,
            range_start=range_start,
            range_end=range_end,
            range_start_dt=range_start_dt,
            range_end_dt_exclusive=range_end_dt,
            include_ingested_propositions=include_ingested,
        )
    return _aplicar_flags(report, contexto, recipient, quinzenal)


def url_absoluta(app_url: str, valor: str) -> str:
    """Imagem enviada pelo Admin vem como caminho; cliente de e-mail precisa da URL inteira."""
    from .share import origem

    return f"{origem(app_url)}{valor}" if valor.startswith("/") else valor


def render_report_html(
    report: ProjectReport,
    periodicidade: str,
    *,
    branding: EmailBranding | None = None,
    settings: dict[str, str] | None = None,
) -> str:
    brand = branding or get_branding()
    config = settings or {}
    if report.design_novo:
        from .render_v2 import render_v2

        return render_v2(report, periodicidade, brand, config)
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")

    subject = subject_for_periodicidade(periodicidade)
    period_label = _PERIOD_LABELS.get(periodicidade, periodicidade)

    parliamentarians_html = _render_parliamentarian_chips(report.parliamentarians)

    date_range_label = _format_date_range_label(
        report.range_start,
        report.range_end,
        periodicidade=periodicidade,
    )

    intro = (
        f"Este relatório reúne atividade dos parlamentares que você monitora no "
        f'<a href="{html.escape(brand.app_url)}">Mamute Político</a> '
        f"no período: {html.escape(period_label)}."
    )
    if report.motivo_geral == "sem_selecao":
        intro = (
            "Você ainda não escolheu parlamentares para acompanhar. Enquanto isso, "
            "veja o que movimentou o Congresso no período: "
            f"{html.escape(period_label)}."
        )
        parliamentarians_html = (
            f'<a href="{html.escape(brand.manage_url)}" style="display:inline-block;'
            'padding:10px 18px;background:#1f2b44;color:#fff;border-radius:999px;'
            'font-weight:700;text-decoration:none;">Escolha seus parlamentares</a>'
        )

    test_notice = _render_test_notice(periodicidade)

    footer = (
        f"Acesse o painel completo em "
        f'<a href="{html.escape(brand.app_url)}">{html.escape(brand.app_url)}</a>.'
    )
    if config.get("instagram_url"):
        footer += (
            f'<br>Siga o Mamute no <a href="{html.escape(config["instagram_url"])}">Instagram</a>.'
        )

    replacements = {
        "{{SUBJECT}}": html.escape(subject),
        "{{LOGO_URL}}": html.escape(brand.logo_url),
        "{{PRIVACY_URL}}": html.escape(brand.privacy_url),
        "{{MANAGE_URL}}": html.escape(brand.manage_url),
        "{{GREETING_NAME}}": html.escape(_greeting_name(report.recipient.nome)),
        "{{INTRO}}": intro,
        "{{TEST_NOTICE}}": test_notice,
        "{{PARLIAMENTARIANS}}": parliamentarians_html,
        "{{PERIOD_LABEL}}": html.escape(period_label),
        "{{STATS_SUMMARY}}": (
            '<p style="margin:8px 0 0;">Os números dos seus parlamentares aparecem aqui '
            "depois que você escolher quem acompanhar.</p>"
            if report.motivo_geral == "sem_selecao"
            else _render_stats_summary(report.stats, date_range_label)
        ),
        "{{HIGHLIGHTS}}": (
            _render_geral_v1(report)
            if report.motivo_geral
            else _render_highlights(report.highlights, report.favorite_parliamentarians)
        ),
        "{{FOOTER}}": footer,
        "{{BANNER}}": _render_imagem_linha(
            brand, config.get("banner_image_url", ""), config.get("banner_link_url", ""), "Patrocínio"
        ),
        "{{BALANCO}}": _render_balanco_v1(report.balanco),
        "{{CONVITE}}": _render_convite_v1(report, config),
        "{{RODAPE_PATROCINIO}}": _render_imagem_linha(
            brand, config.get("footer_image_url", ""), config.get("footer_link_url", ""), "Patrocínio"
        ),
    }

    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def _greeting_name(project_name: str) -> str:
    """Usa a primeira parte do nome do projeto (antes de `_`), em maiúsculas."""
    first = project_name.split("_")[0].strip()
    return (first or project_name).upper()


def _render_test_notice(periodicidade: str) -> str:
    """Aviso visível apenas no envio `--periodicidade total` (não vai em day/week/month)."""
    if periodicidade != PERIODICIDADE_TESTE:
        return ""
    return (
        '<p style="margin:14px 0 0;padding:12px 14px;background:#fffbeb;'
        'border-left:4px solid #f59e0b;border-radius:6px;font-size:13px;color:#92400e;">'
        "<strong>Envio de teste.</strong> Os destaques mostram as atividades mais "
        "recentes de cada parlamentar, sem filtro de data. Nos relatórios "
        "diário, semanal e mensal este aviso não aparece."
        "</p>"
    )


def _render_parliamentarian_chips(names: list[str]) -> str:
    if not names:
        return (
            '<span style="color:#878787;font-size:14px;">'
            "Nenhum parlamentar com atividade nos destaques deste período.</span>"
        )
    chips: list[str] = []
    for index, name in enumerate(names):
        colors = _palette_for_index(index)
        chips.append(
            f'<span style="display:inline-block;margin:5px 8px 5px 0;padding:7px 12px;'
            f'background:{colors["bg"]};color:{colors["title"]};'
            f'border:1px solid {colors["border"]};border-radius:999px;'
            f'font-size:13px;font-weight:600;line-height:1.2;">'
            f"{html.escape(name)}</span>"
        )
    return (
        '<div style="margin-top:8px;line-height:1.6;">' + "".join(chips) + "</div>"
    )


def _stat_card(
    label: str,
    value: int | str,
    *,
    accent: str,
    background: str,
    value_size: str = "26px",
) -> str:
    return (
        f'<td width="25%" style="padding:6px;vertical-align:top;">'
        f'<div style="background:{background};border:1px solid #e5e7eb;'
        f"border-top:3px solid {accent};border-radius:8px;padding:14px 10px;text-align:center;\">"
        f'<div style="font-size:{value_size};font-weight:700;color:#111;line-height:1.2;">'
        f"{html.escape(str(value))}</div>"
        f'<div style="font-size:12px;color:#4b5563;margin-top:6px;font-weight:600;">'
        f"{html.escape(label)}</div></div></td>"
    )


def _render_stats_summary(stats: DashboardStats, date_range_label: str) -> str:
    row_metrics = "".join(
        [
            _stat_card(
                "Proposições",
                stats.propositions_count,
                accent="#2563eb",
                background="#eff6ff",
            ),
            _stat_card(
                "Votações",
                stats.votes_count,
                accent="#059669",
                background="#ecfdf5",
            ),
            _stat_card(
                "Discursos",
                stats.speeches_count,
                accent="#7c3aed",
                background="#f5f3ff",
            ),
            _stat_card(
                "Emendas",
                stats.amendments_count,
                accent="#d97706",
                background="#fffbeb",
            ),
        ]
    )
    period_row = (
        '<tr><td colspan="4" style="padding:6px;">'
        f'<div style="background:#f9fafb;border:1px solid #e5e7eb;border-top:3px solid #6b7280;'
        f'border-radius:8px;padding:14px 16px;text-align:center;">'
        f'<div style="font-size:11px;color:#6b7280;font-weight:600;'
        f'text-transform:uppercase;letter-spacing:0.04em;">Período analisado</div>'
        f'<div style="font-size:15px;font-weight:700;color:#111;margin-top:4px;">'
        f"{html.escape(date_range_label)}</div></div></td></tr>"
    )
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px;">'
        f"<tr>{row_metrics}</tr>"
        f"{period_row}"
        "</table>"
    )


def _format_date_range_label(
    range_start: Optional[date],
    range_end: Optional[date],
    *,
    periodicidade: str,
) -> str:
    if range_start and range_end:
        return f"{range_start.strftime('%d/%m/%Y')} a {range_end.strftime('%d/%m/%Y')}"
    if periodicidade == PERIODICIDADE_TESTE:
        return "Totais históricos dos favoritos · destaques sem filtro de data"
    return "—"


def _palette_for_index(index: int) -> dict[str, str]:
    return _MP_PALETTE[index % len(_MP_PALETTE)]


def _render_one_highlight(item: ActivityItem, *, link_color: str) -> str:
    title = html.escape(item.title)
    subtitle = html.escape(item.subtitle)
    kind = html.escape(item.kind.capitalize())
    link_html = ""
    if item.link:
        safe_link = html.escape(item.link, quote=True)
        link_html = (
            f' <a href="{safe_link}" style="color:{link_color};font-weight:bold;'
            f'text-decoration:none;">Ver detalhes</a>'
        )
    ementa_html = ""
    if item.ementa:
        ementa_html = (
            f'<br><span style="color:#374151;font-size:13px;line-height:1.45;">'
            f"{html.escape(item.ementa)}</span>"
        )

    return (
        f'<div class="highlight" style="padding:10px 0;border-bottom:1px solid #e5e7eb;">'
        f'<span style="color:#6b7280;font-size:14px;">{kind}</span><br>'
        f"<strong style=\"color:#111;\">{title}</strong>"
        f"{ementa_html}<br>"
        f'<span style="color:#6b7280;font-size:14px;">{subtitle}</span>{link_html}'
        f"</div>"
    )


def _format_highlight_heading(favorite: FavoriteParliamentarian) -> str:
    name = html.escape(favorite.display_name)
    if not favorite.chamber:
        return name
    chamber = html.escape(favorite.chamber)
    return (
        f'<span style="font-weight:700;">{chamber}</span>'
        f'<span style="font-weight:400;color:#6b7280;"> · </span>'
        f"{name}"
    )


def _render_highlights(
    items: list[ActivityItem],
    parliamentarians: list[FavoriteParliamentarian],
) -> str:
    if not items and not parliamentarians:
        return '<p class="muted">Nenhuma atividade registrada no período.</p>'

    grouped: OrderedDict[str, list[ActivityItem]] = OrderedDict(
        (favorite.display_name, []) for favorite in parliamentarians
    )
    for item in items:
        key = item.parliamentarian_name
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(item)

    sections: list[str] = []
    for index, favorite in enumerate(parliamentarians):
        mp_items = grouped.get(favorite.display_name, [])
        if not mp_items:
            continue
        colors = _palette_for_index(index)
        blocks = "\n".join(
            _render_one_highlight(item, link_color=colors["link"]) for item in mp_items
        )
        sections.append(
            f'<div class="mp-group" style="margin:16px 0;padding:12px 14px;'
            f'background:{colors["bg"]};border-left:4px solid {colors["border"]};'
            f'border-radius:6px;">'
            f'<h3 style="margin:0 0 10px;font-size:16px;color:{colors["title"]};'
            f'padding-bottom:6px;border-bottom:1px solid {colors["border"]};">'
            f"{_format_highlight_heading(favorite)}</h3>"
            f"{blocks}"
            f"</div>"
        )

    if not sections:
        return '<p class="muted">Nenhuma atividade registrada no período.</p>'

    return "\n".join(sections)


# --- Blocos novos do design atual (CS-116/133/134). Vazios = string vazia, ---
# --- para o e-mail sair idêntico ao de antes quando nada está ligado.       ---

AVISO_SEM_ATIVIDADE = (
    "Seus parlamentares não tiveram atividade registrada nesta quinzena. "
    "Veja o que movimentou o Congresso no período."
)
SEM_ATIVIDADE_PARLAMENTAR = "Sem atividade registrada nesta quinzena."
AVISO_SEM_VOTACAO = (
    "Não houve votação nominal no plenário nesta quinzena. Estas foram as últimas registradas."
)


def titulo_votacoes(geral: GeneralHighlights) -> str:
    return "Últimas votações no plenário" if geral.votacoes_anteriores else "Votações no plenário"


def _render_imagem_linha(brand: EmailBranding, imagem: str, link: str, alt: str) -> str:
    if not imagem:
        return ""
    img = (
        f'<img src="{html.escape(url_absoluta(brand.app_url, imagem))}" width="600" '
        f'alt="{html.escape(alt)}" style="display:block;width:100%;max-width:600px;height:auto;border:0;">'
    )
    if link:
        img = f'<a href="{html.escape(link)}">{img}</a>'
    return f"\n          <tr>\n            <td>{img}</td>\n          </tr>"


def _render_convite_v1(report: ProjectReport, config: dict[str, str]) -> str:
    link = config.get("subscribe_url", "")
    if not report.mostrar_convite or not link:
        return ""
    return (
        '\n          <tr>\n            <td class="section" style="text-align:center;">'
        '<p style="margin:0 0 12px;">Quer o balanço completo de todos os parlamentares que você acompanha?</p>'
        f'<a href="{html.escape(link)}" style="display:inline-block;padding:12px 22px;'
        'background:#1b76ff;color:#fff;border-radius:999px;font-weight:700;text-decoration:none;">'
        "Assine o Mamute Completo</a></td>\n          </tr>"
    )


def _contagens_texto(stats: DashboardStats) -> str:
    partes = [
        (stats.propositions_count, "projeto", "projetos"),
        (stats.votes_count, "votação", "votações"),
        (stats.speeches_count, "discurso", "discursos"),
        (stats.amendments_count, "emenda", "emendas"),
    ]
    return " · ".join(f"{n} {um if n == 1 else varios}" for n, um, varios in partes)


def balanco_tem_atividade(stats: DashboardStats) -> bool:
    return (
        stats.propositions_count + stats.votes_count + stats.speeches_count + stats.amendments_count
    ) > 0


def _render_balanco_v1(balanco: list[ParliamentarianBalance]) -> str:
    if not balanco:
        return ""
    linhas = []
    for linha in balanco:
        if balanco_tem_atividade(linha.stats):
            detalhe = html.escape(_contagens_texto(linha.stats))
            if linha.destaque:
                detalhe += f"<br><span style=\"color:#374151;\">Destaque: {html.escape(linha.destaque.title)}</span>"
        else:
            detalhe = html.escape(SEM_ATIVIDADE_PARLAMENTAR)
        linhas.append(
            '<div style="padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;">'
            f"<strong>{_format_highlight_heading(linha.favorite)}</strong><br>"
            f'<span style="color:#6b7280;">{detalhe}</span></div>'
        )
    return (
        '<h2 style="color:#111;font-size:18px;margin:24px 0 8px;">Balanço dos seus parlamentares</h2>'
        + "".join(linhas)
    )


def _render_geral_v1(report: ProjectReport) -> str:
    partes: list[str] = []
    if report.motivo_geral == "sem_atividade":
        partes.append(f'<p style="margin:0 0 12px;">{html.escape(AVISO_SEM_ATIVIDADE)}</p>')
    geral = report.geral
    if geral and geral.votacoes:
        partes.append(
            f'<h3 style="margin:12px 0 6px;font-size:16px;color:#111;">{titulo_votacoes(geral)}</h3>'
        )
        if geral.votacoes_anteriores:
            partes.append(f'<p style="margin:0 0 8px;color:#6b7280;">{html.escape(AVISO_SEM_VOTACAO)}</p>')
        partes.extend(_render_one_highlight(item, link_color="#1b76ff") for item in geral.votacoes)
    if geral and geral.temas:
        temas = ", ".join(html.escape(t) for t in geral.temas)
        partes.append(
            '<h3 style="margin:16px 0 6px;font-size:16px;color:#111;">Temas mais falados nos discursos</h3>'
            f'<p style="margin:0;color:#374151;">{temas}</p>'
        )
    if len(partes) <= 1:
        partes.append('<p class="muted">Nenhuma atividade registrada no período.</p>')
    return "\n".join(partes)
