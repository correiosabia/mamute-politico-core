"""Design novo do relatório (flag `email_design_novo`): visual do site.

Fundo amarelo, cards brancos arredondados, títulos no azul-marinho do site.
É HTML de e-mail: layout em tabelas, estilos inline, largura máxima de 600 px
e nenhuma fonte web obrigatória (Gmail e Outlook ignoram CSS moderno e
`<style>` em vários cenários).

Cada destaque com código de link curto ganha a linha "Compartilhar"
(WhatsApp, X e o link visível, que é o "copiar" possível num e-mail).
"""

from __future__ import annotations

import html
from collections import OrderedDict

from .config import EmailBranding, subject_for_periodicidade
from .models import ActivityItem, DashboardStats, ProjectReport
from .report_builder import (
    _PERIOD_LABELS,
    AVISO_SEM_ATIVIDADE,
    AVISO_SEM_VOTACAO,
    SEM_ATIVIDADE_PARLAMENTAR,
    _contagens_texto,
    _format_date_range_label,
    _greeting_name,
    balanco_tem_atividade,
    titulo_votacoes,
    url_absoluta,
)
from .share import share_links

AMARELO = "#e6c54a"
MARINHO = "#1f2b44"
AZUL = "#1b76ff"
TEXTO = "#383838"
CINZA = "#6b7280"
FONTE = "Barlow, 'Segoe UI', Helvetica, Arial, sans-serif"

_CARD = "background:#ffffff;border-radius:16px;padding:20px 22px;"
_ROTULOS = {
    "proposição": "Projeto",
    "votação": "Votação",
    "discurso": "Discurso",
    "emenda": "Emenda",
}


def _e(valor: object) -> str:
    return html.escape(str(valor or ""))


def _linha(conteudo: str, *, espaco: int = 12) -> str:
    return f'<tr><td style="padding:0 0 {espaco}px 0;">{conteudo}</td></tr>'


def _card(conteudo: str, estilo: str = "") -> str:
    return f'<div style="{_CARD}{estilo}">{conteudo}</div>'


def _titulo_secao(texto: str) -> str:
    return _linha(
        f'<h2 style="margin:12px 0 0;font-size:20px;line-height:1.2;color:{MARINHO};'
        f'font-family:{FONTE};">{_e(texto)}</h2>',
        espaco=10,
    )


def _imagem(brand: EmailBranding, imagem: str, link: str) -> str:
    if not imagem:
        return ""
    img = (
        f'<img src="{_e(url_absoluta(brand.app_url, imagem))}" width="600" alt="Patrocínio" '
        'style="display:block;width:100%;max-width:600px;height:auto;border:0;border-radius:16px;">'
    )
    if link:
        img = f'<a href="{_e(link)}">{img}</a>'
    return _linha(img)


def _botao(texto: str, href: str, *, fundo: str = MARINHO, cor: str = "#ffffff") -> str:
    return (
        f'<a href="{_e(href)}" style="display:inline-block;padding:12px 22px;background:{fundo};'
        f"color:{cor};border-radius:999px;font-weight:700;font-size:15px;text-decoration:none;"
        f'font-family:{FONTE};">{_e(texto)}</a>'
    )


def _numeros(stats: DashboardStats) -> str:
    celulas = "".join(
        f'<td width="25%" style="padding:4px;"><div style="background:#ffffff;border-radius:16px;'
        f'padding:14px 6px;text-align:center;"><div style="font-size:24px;font-weight:800;color:{MARINHO};">'
        f'{valor}</div><div style="font-size:12px;color:{CINZA};font-weight:600;margin-top:4px;">{rotulo}</div>'
        "</div></td>"
        for valor, rotulo in (
            (stats.propositions_count, "Projetos"),
            (stats.votes_count, "Votações"),
            (stats.speeches_count, "Discursos"),
            (stats.amendments_count, "Emendas"),
        )
    )
    return _linha(f'<table width="100%" cellpadding="0" cellspacing="0"><tr>{celulas}</tr></table>')


def _compartilhar(item: ActivityItem, brand: EmailBranding, share_text: str) -> str:
    if not item.share_code:
        return ""
    links = share_links(brand.app_url, item.share_code, share_text)
    estilo = f"color:{AZUL};font-weight:700;text-decoration:none;"
    return (
        f'<div style="margin-top:12px;padding-top:10px;border-top:1px solid #eef1f6;font-size:13px;color:{CINZA};">'
        f'Compartilhar: <a href="{_e(links["whatsapp"])}" style="{estilo}">WhatsApp</a> · '
        f'<a href="{_e(links["x"])}" style="{estilo}">X</a> · '
        f'<a href="{_e(links["url"])}" style="color:{CINZA};">{_e(links["url"])}</a></div>'
    )


def _item(item: ActivityItem, brand: EmailBranding, share_text: str) -> str:
    rotulo = _ROTULOS.get(item.kind, item.kind.capitalize())
    partes = [
        f'<div style="font-size:12px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:{AZUL};">{_e(rotulo)}</div>',
        f'<div style="font-size:17px;font-weight:700;line-height:1.35;color:{MARINHO};margin-top:6px;">{_e(item.title)}</div>',
    ]
    if item.ementa:
        partes.append(f'<div style="font-size:15px;line-height:1.5;color:{TEXTO};margin-top:6px;">{_e(item.ementa)}</div>')
    rodape = _e(item.subtitle)
    if item.link:
        rodape += (
            f'{" · " if rodape else ""}<a href="{_e(item.link)}" style="color:{AZUL};'
            'font-weight:700;text-decoration:none;">Ver na fonte</a>'
        )
    if rodape:
        partes.append(f'<div style="font-size:13px;color:{CINZA};margin-top:8px;">{rodape}</div>')
    partes.append(_compartilhar(item, brand, share_text))
    return _linha(_card("".join(partes)))


def _cabecalho_parlamentar(nome: str, casa: str) -> str:
    casa_html = f'<span style="color:{CINZA};font-weight:600;"> · {_e(casa)}</span>' if casa else ""
    return _linha(
        f'<div style="display:inline-block;background:{MARINHO};color:#ffffff;border-radius:999px;'
        f'padding:8px 16px;font-size:14px;font-weight:700;">{_e(nome)}</div>'
        f'<span style="font-size:13px;">{casa_html}</span>',
        espaco=8,
    )


def _destaques(report: ProjectReport, brand: EmailBranding, share_text: str) -> str:
    grupos: OrderedDict[str, list[ActivityItem]] = OrderedDict(
        (fav.display_name, []) for fav in report.favorite_parliamentarians
    )
    for item in report.highlights:
        grupos.setdefault(item.parliamentarian_name, []).append(item)
    casas = {fav.display_name: fav.chamber for fav in report.favorite_parliamentarians}
    blocos: list[str] = []
    for nome, itens in grupos.items():
        if not itens:
            continue
        blocos.append(_cabecalho_parlamentar(nome, casas.get(nome, "")))
        blocos.extend(_item(item, brand, share_text) for item in itens)
    if not blocos:
        return ""
    return _titulo_secao("Destaques da quinzena") + "".join(blocos)


def _balanco(report: ProjectReport) -> str:
    if not report.balanco:
        return ""
    linhas: list[str] = []
    for linha in report.balanco:
        fav = linha.favorite
        if balanco_tem_atividade(linha.stats):
            detalhe = _e(_contagens_texto(linha.stats))
            if linha.destaque:
                detalhe += f'<div style="color:{TEXTO};margin-top:4px;">Destaque: {_e(linha.destaque.title)}</div>'
        else:
            detalhe = _e(SEM_ATIVIDADE_PARLAMENTAR)
        casa = f" · {_e(fav.chamber)}" if fav.chamber else ""
        linhas.append(
            f'<div style="padding:12px 0;border-bottom:1px solid #eef1f6;">'
            f'<div style="font-weight:700;color:{MARINHO};">{_e(fav.display_name)}'
            f'<span style="color:{CINZA};font-weight:600;">{casa}</span></div>'
            f'<div style="font-size:14px;color:{CINZA};margin-top:4px;">{detalhe}</div></div>'
        )
    return _titulo_secao("Balanço dos seus parlamentares") + _linha(_card("".join(linhas)))


def _geral(report: ProjectReport, brand: EmailBranding, share_text: str) -> str:
    if not report.motivo_geral:
        return ""
    partes: list[str] = []
    if report.motivo_geral == "sem_atividade":
        partes.append(_linha(_card(f'<div style="font-size:15px;color:{TEXTO};">{_e(AVISO_SEM_ATIVIDADE)}</div>')))
    geral = report.geral
    if geral and geral.votacoes:
        partes.append(_titulo_secao(titulo_votacoes(geral)))
        if geral.votacoes_anteriores:
            partes.append(_linha(f'<div style="font-size:14px;color:{MARINHO};">{_e(AVISO_SEM_VOTACAO)}</div>', espaco=10))
        partes.extend(_item(item, brand, share_text) for item in geral.votacoes)
    if geral and geral.temas:
        chips = "".join(
            f'<span style="display:inline-block;margin:0 6px 8px 0;padding:7px 14px;background:#ffffff;'
            f'border-radius:999px;font-size:14px;font-weight:600;color:{MARINHO};">{_e(t)}</span>'
            for t in geral.temas
        )
        partes.append(_titulo_secao("Temas mais falados nos discursos") + _linha(chips))
    return "".join(partes)


def render_v2(
    report: ProjectReport,
    periodicidade: str,
    brand: EmailBranding,
    config: dict[str, str],
) -> str:
    assunto = subject_for_periodicidade(periodicidade)
    periodo = _format_date_range_label(report.range_start, report.range_end, periodicidade=periodicidade)
    share_text = config.get("share_text", "")

    if report.motivo_geral == "sem_selecao":
        abertura = (
            "Você ainda não escolheu parlamentares para acompanhar. Enquanto isso, "
            "veja o que movimentou o Congresso na quinzena."
        )
        acao = f'<div style="margin-top:16px;">{_botao("Escolha seus parlamentares", brand.manage_url)}</div>'
    else:
        abertura = (
            "Este é o resumo do que fizeram os parlamentares que você acompanha no Mamute Político."
        )
        acao = ""

    corpo = [
        _linha(
            f'<div style="text-align:center;padding:8px 0 4px;"><img src="{_e(brand.logo_url)}" width="200" '
            'alt="Mamute Político" style="display:inline-block;border:0;"></div>'
        ),
        _imagem(brand, config.get("banner_image_url", ""), config.get("banner_link_url", "")),
        _linha(
            _card(
                f'<div style="font-size:13px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:{AZUL};">'
                f"{_e(assunto)} · {_e(periodo)}</div>"
                f'<h1 style="margin:8px 0 0;font-size:26px;line-height:1.2;color:{MARINHO};">Olá, {_e(_greeting_name(report.recipient.nome).title())}!</h1>'
                f'<p style="margin:10px 0 0;font-size:16px;line-height:1.5;color:{TEXTO};">{_e(abertura)}</p>{acao}'
            )
        ),
    ]
    if report.motivo_geral != "sem_selecao":
        corpo.append(_numeros(report.stats))
    corpo.append(_balanco(report))
    corpo.append(_destaques(report, brand, share_text))
    corpo.append(_geral(report, brand, share_text))

    if report.mostrar_convite and config.get("subscribe_url"):
        corpo.append(
            _linha(
                _card(
                    '<div style="font-size:20px;font-weight:800;color:#ffffff;line-height:1.25;">'
                    "Quer o balanço de todos os parlamentares que você acompanha?</div>"
                    '<p style="margin:8px 0 16px;font-size:15px;color:#dfe5f0;">No Mamute Completo o '
                    "relatório traz cada um deles, com números e destaques.</p>"
                    + _botao("Assine o Mamute Completo", config["subscribe_url"], fundo=AMARELO, cor=MARINHO),
                    f"background:{MARINHO};text-align:center;",
                )
            )
        )

    corpo.append(_imagem(brand, config.get("footer_image_url", ""), config.get("footer_link_url", "")))

    instagram = ""
    if config.get("instagram_url"):
        instagram = (
            f'<a href="{_e(config["instagram_url"])}" style="color:{MARINHO};font-weight:700;">'
            "Siga o Mamute no Instagram</a><br>"
        )
    corpo.append(
        _linha(
            f'<div style="text-align:center;font-size:13px;color:{MARINHO};padding:8px 0 16px;">{instagram}'
            f'<a href="{_e(brand.app_url)}" style="color:{MARINHO};">Abrir o painel</a> · '
            f'<a href="{_e(brand.manage_url)}" style="color:{MARINHO};">Gerenciar monitoramento</a> · '
            f'<a href="{_e(brand.privacy_url)}" style="color:{MARINHO};">Privacidade</a></div>'
        )
    )

    return (
        '<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(assunto)}</title></head>"
        f'<body style="margin:0;padding:0;background:{AMARELO};">'
        f'<table width="100%" cellpadding="0" cellspacing="0" style="background:{AMARELO};font-family:{FONTE};">'
        '<tr><td align="center" style="padding:20px 12px;">'
        '<table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;">'
        + "".join(corpo)
        + "</table></td></tr></table></body></html>"
    )
