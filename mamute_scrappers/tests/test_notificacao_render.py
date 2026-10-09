"""Render do relatório: design atual com os blocos novos e design novo (CS-116/133/134)."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from notificacao_fixture import ANA, BIA, BRANDING, relatorio

from mamute_scrappers.scripts.notificacao import settings, share
from mamute_scrappers.scripts.notificacao.models import (
    ActivityItem,
    DashboardStats,
    GeneralHighlights,
    ParliamentarianBalance,
)
from mamute_scrappers.scripts.notificacao.report_builder import render_report_html

SNAPSHOT = Path(__file__).parent / "fixtures" / "notificacao" / "report_v1_snapshot.html"
CONFIG = {
    "banner_image_url": "https://img.example/banner.png",
    "banner_link_url": "https://patrocinio.example/topo",
    "footer_image_url": "/api/public-images/abc",
    "footer_link_url": "https://patrocinio.example/rodape",
    "instagram_url": "https://instagram.com/mamute",
    "subscribe_url": "https://mamutepolitico.com.br/assinar",
    "share_text": "Recebi isso no relatório do Mamute Político.",
}
GERAL = GeneralHighlights(
    votacoes=[
        ActivityItem(
            kind="votação",
            title="PEC 18/2025",
            subtitle="Votação em 01/10/2026 · Sim 487 × Não 15",
            parliamentarian_name="",
            ementa="Altera a Constituição.",
            kind_key="proposicao",
            item_id=99,
        )
    ],
    temas=["saúde", "reforma tributária"],
)


def _render(report=None, config=None, **kw) -> str:
    return render_report_html(
        report or relatorio(), "fortnight", branding=BRANDING, settings=config or {}, **kw
    )


class TestDesignAtual:
    def test_sem_configuracao_e_sem_flags_fica_igual_ao_de_hoje(self) -> None:
        assert _render() == SNAPSHOT.read_text(encoding="utf-8")

    def test_banner_e_rodape_com_link_quando_configurados(self) -> None:
        html = _render(config=CONFIG)

        assert 'href="https://patrocinio.example/topo"' in html
        assert 'src="https://img.example/banner.png"' in html
        # Imagem enviada pelo admin vira URL absoluta: cliente de e-mail não resolve caminho.
        assert 'src="https://mamutepolitico.com.br/api/public-images/abc"' in html
        assert 'href="https://instagram.com/mamute"' in html

    def test_convite_so_quando_a_flag_liga(self) -> None:
        report = relatorio()
        assert "Assine o Mamute Completo" not in _render(report, CONFIG)

        report.mostrar_convite = True
        assert "Assine o Mamute Completo" in _render(report, CONFIG)

    def test_convite_sem_link_configurado_nao_aparece(self) -> None:
        report = relatorio()
        report.mostrar_convite = True

        assert "Assine o Mamute Completo" not in _render(report, {})

    def test_sem_selecao_mostra_convite_para_escolher_e_destaques_gerais(self) -> None:
        report = relatorio()
        report.favorite_parliamentarians, report.parliamentarians, report.highlights = [], [], []
        report.motivo_geral, report.geral = "sem_selecao", GERAL

        html = _render(report)

        assert "Escolha seus parlamentares" in html
        assert "PEC 18/2025" in html
        assert "Sim 487 × Não 15" in html
        assert "reforma tributária" in html
        assert ">Proposições<" not in html  # zero pareceria dado não coletado

    def test_sem_atividade_diz_isso_com_clareza(self) -> None:
        report = relatorio()
        report.highlights = []
        report.motivo_geral, report.geral = "sem_atividade", GERAL

        html = _render(report)

        assert "não tiveram atividade registrada" in html
        assert "PEC 18/2025" in html

    def test_balanco_lista_todos_inclusive_sem_atividade(self) -> None:
        report = relatorio()
        report.balanco = [
            ParliamentarianBalance(ANA, DashboardStats(propositions_count=2, speeches_count=1), report.highlights[0]),
            ParliamentarianBalance(BIA, DashboardStats()),
        ]

        html = _render(report)

        assert "Balanço dos seus parlamentares" in html
        assert "Bia &lt;Lima&gt;" in html
        assert "Sem atividade registrada nesta quinzena" in html

    def test_card_de_emendas_no_resumo(self) -> None:
        assert ">Emendas<" in _render()


class TestDesignNovo:
    def _novo(self, **kw) -> str:
        report = relatorio()
        report.design_novo = True
        for k, v in kw.items():
            setattr(report, k, v)
        return report

    def test_usa_o_visual_do_site(self) -> None:
        html = _render(self._novo(), CONFIG)

        assert "#e6c54a" in html
        assert "border-radius:16px" in html
        assert "Ana Souza" in html

    def test_escapa_html_vindo_do_banco(self) -> None:
        html = _render(self._novo(), CONFIG)

        assert "Bia <Lima>" not in html
        assert "Bia &lt;Lima&gt;" in html

    def test_compartilhar_so_com_codigo_do_link_curto(self) -> None:
        report = self._novo()
        assert "Compartilhar" not in _render(report, CONFIG)

        report.highlights[1].share_code = "Abc123XyZ0"
        html = _render(report, CONFIG)

        assert "Compartilhar" in html
        assert "https://mamutepolitico.com.br/api/s/Abc123XyZ0" in html

    def test_link_do_whatsapp_leva_texto_e_link_codificados(self) -> None:
        links = share.share_links(BRANDING.app_url, "Abc123XyZ0", 'Recebi "isso" & mais.')
        texto = parse_qs(urlparse(links["whatsapp"]).query)["text"][0]

        assert links["whatsapp"].startswith("https://wa.me/?text=")
        assert texto == 'Recebi "isso" & mais. https://mamutepolitico.com.br/api/s/Abc123XyZ0'
        assert parse_qs(urlparse(links["x"]).query)["url"][0] == links["url"]

    def test_sem_texto_configurado_usa_o_padrao(self) -> None:
        links = share.share_links(BRANDING.app_url, "Abc123XyZ0", "")

        assert "Inscreva-se" in parse_qs(urlparse(links["whatsapp"]).query)["text"][0]

    def test_destaques_gerais_e_balanco_tambem_no_design_novo(self) -> None:
        report = self._novo(motivo_geral="sem_atividade", geral=GERAL, highlights=[])
        report.balanco = [ParliamentarianBalance(BIA, DashboardStats())]

        html = _render(report, CONFIG)

        assert "PEC 18/2025" in html
        assert "Sem atividade registrada nesta quinzena" in html


def _sessao(*, com_tabelas: bool = True) -> Session:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    if com_tabelas:
        with engine.begin() as conn:
            conn.exec_driver_sql("create table email_settings (key text primary key, value text)")
            conn.exec_driver_sql(
                "create table share_link (code text primary key, kind text, item_id integer,"
                " title text, summary text, parliamentarian_name text, chamber text,"
                " occurred_at date, created_at datetime, unique (kind, item_id))"
            )
    return Session(engine)


class TestConfiguracoes:
    def test_le_as_chaves_gravadas(self) -> None:
        s = _sessao()
        s.execute(text("insert into email_settings values ('instagram_url', 'https://instagram.com/m')"))
        s.commit()

        assert settings.load_email_settings(s)["instagram_url"] == "https://instagram.com/m"

    def test_tabela_ausente_e_tudo_vazio(self) -> None:
        assert settings.load_email_settings(_sessao(com_tabelas=False)) == {}


class TestCodigoDoLinkCurto:
    def test_grava_e_reaproveita_o_codigo_do_mesmo_destaque(self) -> None:
        s = _sessao()
        item = relatorio().highlights[0]

        primeiro = share.share_code_for(s, item, chamber="Câmara")
        segundo = share.share_code_for(s, item, chamber="Câmara")

        assert primeiro == segundo
        assert len(primeiro) == 10 and primeiro.isalnum()
        linha = s.execute(text("select kind, item_id, title, summary, chamber from share_link")).one()
        assert tuple(linha) == ("proposicao", 1, "PL 10/2026", "Institui o dia do mamute.", "Câmara")

    def test_item_sem_identificador_nao_ganha_codigo(self) -> None:
        item = ActivityItem(kind="discurso", title="x", subtitle="", parliamentarian_name="")

        assert share.share_code_for(_sessao(), item, chamber="") is None

    def test_tabela_ausente_nao_quebra(self) -> None:
        assert share.share_code_for(_sessao(com_tabelas=False), relatorio().highlights[0], chamber="") is None

    @pytest.mark.parametrize("n", [3])
    def test_codigos_diferentes_para_itens_diferentes(self, n: int) -> None:
        s = _sessao()
        codigos = set()
        for i in range(n):
            item = ActivityItem(kind="discurso", title="x", subtitle="", parliamentarian_name="", kind_key="discurso", item_id=i)
            codigos.add(share.share_code_for(s, item, chamber=""))

        assert len(codigos) == n


@pytest.mark.parametrize("design_novo", [False, True])
def test_quinzena_sem_votacao_avisa_que_sao_as_ultimas(design_novo: bool) -> None:
    report = relatorio()
    report.highlights = []
    report.design_novo = design_novo
    report.motivo_geral = "sem_atividade"
    report.geral = GeneralHighlights(votacoes=GERAL.votacoes, votacoes_anteriores=True)

    html = _render(report)

    assert "Não houve votação nominal no plenário nesta quinzena" in html
    assert "Últimas votações no plenário" in html


def test_rotas_da_api_usam_a_origem_mesmo_com_app_url_terminando_em_app() -> None:
    """Revisão C1: /app* vai para a SPA no Caddy; a API mora em /api na raiz."""
    from mamute_scrappers.scripts.notificacao.report_builder import url_absoluta

    links = share.share_links("https://mamutepolitico.com.br/app/", "Abc123XyZ0", "")

    assert links["url"] == "https://mamutepolitico.com.br/api/s/Abc123XyZ0"
    assert url_absoluta("https://mamutepolitico.com.br/app", "/api/public-images/a") == (
        "https://mamutepolitico.com.br/api/public-images/a"
    )
