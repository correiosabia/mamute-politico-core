"""Montagem do relatório e status do envio com as flags do e-mail (CS-116/CS-133)."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from mamute_scrappers.scripts.notificacao import flags, report_builder, runner
from mamute_scrappers.scripts.notificacao.models import (
    ActivityItem,
    DashboardStats,
    FavoriteParliamentarian,
    GeneralHighlights,
    ProjectRecipient,
)

PAGO = ProjectRecipient(id=1, email="pago@x.com", nome="Pago", tier_id=4)
ANA = FavoriteParliamentarian(id=10, display_name="Ana", chamber="Câmara")
BIA = FavoriteParliamentarian(id=20, display_name="Bia", chamber="Senado")
GERAL = GeneralHighlights(temas=["saúde"])


def _contexto(*ligadas: str) -> report_builder.EnvioContexto:
    snap = flags.FlagSnapshot(
        estados={k: "all" for k in ligadas},
        planos_liberados={k: frozenset({4}) for k in ligadas},
    )
    return report_builder.EnvioContexto(flags=snap, geral=GERAL)


def _destaque(nome: str) -> ActivityItem:
    return ActivityItem(kind="discurso", title="Fala", subtitle="", parliamentarian_name=nome, occurred_at=date(2026, 10, 1))


@pytest.fixture()
def banco(monkeypatch):
    """Favoritos, contagens e destaques controlados pelo teste."""
    estado = SimpleNamespace(favoritos=[ANA, BIA], contagens={10: 0, 20: 0}, destaques=[])

    monkeypatch.setattr(report_builder, "list_favorite_parliamentarians", lambda s, pid: estado.favoritos)

    def _stats(session, ids, *a, **k):
        return DashboardStats(speeches_count=sum(estado.contagens.get(i, 0) for i in ids))

    monkeypatch.setattr(report_builder, "compute_dashboard_stats", _stats)
    monkeypatch.setattr(report_builder, "fetch_project_highlights", lambda *a, **k: list(estado.destaques))
    return estado


def _montar(contexto=None):
    return report_builder.build_project_report(None, PAGO, "fortnight", contexto=contexto)


class TestMontagem:
    def test_sem_selecao_e_flag_desligada_continua_pulando(self, banco) -> None:
        banco.favoritos = []

        assert _montar(_contexto()) is None

    def test_sem_selecao_com_flag_recebe_os_destaques_gerais(self, banco) -> None:
        banco.favoritos = []

        report = _montar(_contexto(flags.FLAG_DESTAQUES_GERAIS))

        assert report.motivo_geral == "sem_selecao"
        assert report.geral is GERAL
        assert report.range_start is not None

    def test_sem_atividade_com_flag_recebe_os_destaques_gerais(self, banco) -> None:
        report = _montar(_contexto(flags.FLAG_DESTAQUES_GERAIS))

        assert report.motivo_geral == "sem_atividade"
        assert report.geral is GERAL

    def test_com_atividade_nao_recebe_destaques_gerais(self, banco) -> None:
        banco.contagens = {10: 2, 20: 0}
        banco.destaques = [_destaque("Ana")]

        report = _montar(_contexto(flags.FLAG_DESTAQUES_GERAIS))

        assert report.motivo_geral is None
        assert report.geral is None

    def test_emenda_conta_como_atividade(self, banco, monkeypatch) -> None:
        monkeypatch.setattr(
            report_builder, "compute_dashboard_stats", lambda *a, **k: DashboardStats(amendments_count=1)
        )

        report = _montar(_contexto(flags.FLAG_DESTAQUES_GERAIS))

        assert report.motivo_geral is None

    def test_balanco_cobre_todos_os_selecionados_inclusive_sem_atividade(self, banco) -> None:
        banco.contagens = {10: 2, 20: 0}
        banco.destaques = [_destaque("Ana")]

        report = _montar(_contexto(flags.FLAG_BALANCO))

        assert [b.favorite.display_name for b in report.balanco] == ["Ana", "Bia"]
        assert report.balanco[0].stats.speeches_count == 2
        assert report.balanco[0].destaque is banco.destaques[0]
        assert report.balanco[1].stats.speeches_count == 0
        assert report.balanco[1].destaque is None

    def test_sem_flag_de_balanco_nao_monta_balanco(self, banco) -> None:
        assert _montar(_contexto()).balanco == []

    def test_convite_e_design_seguem_as_flags(self, banco) -> None:
        report = _montar(_contexto(flags.FLAG_CONVITE, flags.FLAG_DESIGN_NOVO))

        assert report.mostrar_convite
        assert report.design_novo

    def test_sem_contexto_e_tudo_como_antes(self, banco) -> None:
        report = _montar()

        assert (report.motivo_geral, report.balanco, report.mostrar_convite, report.design_novo) == (
            None,
            [],
            False,
            False,
        )


class TestStatus:
    @pytest.fixture()
    def envio(self, monkeypatch):
        registro = SimpleNamespace(status=[], enviados=[])
        monkeypatch.setattr(runner, "get_session", lambda: SimpleNamespace(close=lambda: None, rollback=lambda: None))
        monkeypatch.setattr(
            runner, "log_send_attempt", lambda session, **k: registro.status.append(k["status"])
        )
        monkeypatch.setattr(runner, "render_report_html", lambda *a, **k: "<html></html>")
        monkeypatch.setattr(runner, "send_html_email", lambda html, to, subject: registro.enviados.append(to))
        return registro

    def _processar(self, contexto=None) -> None:
        runner._process_recipient(
            PAGO,
            "fortnight",
            dry_run=False,
            highlight_limit=9,
            skip_empty=True,
            save_html=False,
            output_dir=None,
            contexto=contexto,
        )

    def test_sem_selecao_sem_flag(self, banco, envio) -> None:
        banco.favoritos = []
        self._processar(_contexto())

        assert envio.status == ["skipped_no_favorites"]
        assert envio.enviados == []

    def test_sem_selecao_com_flag(self, banco, envio) -> None:
        banco.favoritos = []
        self._processar(_contexto(flags.FLAG_DESTAQUES_GERAIS))

        assert envio.status == ["sent_general"]
        assert envio.enviados == ["pago@x.com"]

    def test_sem_atividade_sem_flag(self, banco, envio) -> None:
        self._processar(_contexto())

        assert envio.status == ["skipped_no_activity"]

    def test_sem_atividade_com_flag(self, banco, envio) -> None:
        self._processar(_contexto(flags.FLAG_DESTAQUES_GERAIS))

        assert envio.status == ["sent_no_activity"]
        assert envio.enviados == ["pago@x.com"]

    def test_com_atividade(self, banco, envio) -> None:
        banco.contagens = {10: 1}
        self._processar(_contexto())

        assert envio.status == ["sent"]


class TestCompartilhar:
    def _rodar(self, monkeypatch, banco, contexto):
        renderizados = []
        monkeypatch.setattr(runner, "get_session", lambda: SimpleNamespace(close=lambda: None, rollback=lambda: None))
        monkeypatch.setattr(runner, "log_send_attempt", lambda session, **k: None)
        monkeypatch.setattr(runner, "send_html_email", lambda *a: None)
        monkeypatch.setattr(
            runner, "render_report_html", lambda report, p, **k: renderizados.append((report, k)) or "<html/>"
        )
        monkeypatch.setattr(
            runner, "share_code_for", lambda s, item, chamber: f"cod{item.item_id}-{chamber}"
        )
        banco.contagens = {10: 1}
        banco.destaques = [
            ActivityItem(kind="discurso", title="x", subtitle="", parliamentarian_name="Ana", kind_key="discurso", item_id=7)
        ]
        runner._process_recipient(
            PAGO, "fortnight", dry_run=False, highlight_limit=9, skip_empty=True,
            save_html=False, output_dir=None, contexto=contexto,
        )
        return renderizados[0]

    def test_design_novo_ganha_codigo_com_a_casa_do_parlamentar(self, monkeypatch, banco) -> None:
        contexto = _contexto(flags.FLAG_DESIGN_NOVO)
        contexto.settings = {"share_text": "oi"}

        report, kwargs = self._rodar(monkeypatch, banco, contexto)

        assert report.highlights[0].share_code == "cod7-Câmara"
        assert kwargs["settings"] == {"share_text": "oi"}

    def test_design_atual_nao_grava_link_curto(self, monkeypatch, banco) -> None:
        report, _ = self._rodar(monkeypatch, banco, _contexto())

        assert report.highlights[0].share_code is None


class TestRevisao:
    def test_flags_so_valem_para_o_quinzenal(self, banco) -> None:
        """Revisão I2: o mesmo runner faz o diário; texto e cadência são da quinzena."""
        banco.favoritos = []
        contexto = _contexto(flags.FLAG_DESTAQUES_GERAIS)

        assert report_builder.build_project_report(None, PAGO, "day", contexto=contexto) is None

        banco.favoritos = [ANA]
        todas = _contexto(*flags.TODAS)
        report = report_builder.build_project_report(None, PAGO, "week", contexto=todas)
        assert (report.motivo_geral, report.balanco, report.mostrar_convite, report.design_novo) == (
            None,
            [],
            False,
            False,
        )

    def test_destaques_gerais_vazios_voltam_a_pular(self, banco) -> None:
        """Revisão I1: sem conteúdo geral, mandar e-mail vazio é pior que pular."""
        banco.favoritos = []
        contexto = _contexto(flags.FLAG_DESTAQUES_GERAIS)
        contexto.geral = GeneralHighlights()

        assert _montar(contexto) is None

    def test_falha_ao_montar_destaques_gerais_nao_derruba_o_envio(self, monkeypatch) -> None:
        from contextlib import contextmanager

        @contextmanager
        def _sessao():
            yield SimpleNamespace(rollback=lambda: None)

        monkeypatch.setattr(runner, "session_scope", _sessao)
        monkeypatch.setattr(
            runner,
            "carregar_flags",
            lambda s: flags.FlagSnapshot(estados={flags.FLAG_DESTAQUES_GERAIS: "all"}),
        )

        def _explode(*a):
            raise RuntimeError("timeout")

        monkeypatch.setattr(runner, "build_general_highlights", _explode)
        monkeypatch.setattr(runner, "load_email_settings", lambda s: {})

        contexto = runner.carregar_contexto("fortnight")

        assert contexto.geral is None
