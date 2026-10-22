"""Descadastro dos e-mails do Mamute (CS-150): quem desativou não recebe, e todo
e-mail leva o link de descadastro (rodapé + List-Unsubscribe)."""
from __future__ import annotations

import dataclasses
from email import message_from_string

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from notificacao_fixture import BRANDING, relatorio
from test_resultado_eleicao_envio import ASSUNTO_MAJ, CICLO, FakeMailer, _cenario, _fechar

from eleicao_schema import set_flag
from mamute_scrappers.scripts.notificacao import mailer
from mamute_scrappers.scripts.notificacao.config import SmtpConfig
from mamute_scrappers.scripts.notificacao.descadastro import (
    aplicar_preferencias,
    links_descadastro,
)
from mamute_scrappers.scripts.notificacao.models import ProjectRecipient
from mamute_scrappers.scripts.notificacao.report_builder import render_report_html
from mamute_scrappers.scripts.notificacao.resultado_eleicao import (
    CARGOS_MAJORITARIOS,
    DISPARO_MAJORITARIOS,
    enviar,
    montar_avisos,
    render_html,
)

ANA = ProjectRecipient(id=1, email="ana@x.com", nome="Ana")
BIA = ProjectRecipient(id=2, email="bia@x.com", nome="Bia")


def _session(*, com_colunas: bool = True) -> Session:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    extra = ", emails_desativados_em TIMESTAMP, email_token TEXT" if com_colunas else ""
    with engine.begin() as conn:
        conn.exec_driver_sql(f"CREATE TABLE projetos (id INTEGER PRIMARY KEY, email TEXT{extra})")
        if com_colunas:
            conn.exec_driver_sql(
                "INSERT INTO projetos VALUES (1, 'ana@x.com', NULL, 'tok-ana'),"
                "(2, 'bia@x.com', '2026-10-16 09:00:00', 'tok-bia')"
            )
    return Session(engine)


class TestListaDeEnvio:
    def test_tira_quem_desativou_e_preenche_o_token(self) -> None:
        resultado = aplicar_preferencias(_session(), [ANA, BIA])
        assert [r.email for r in resultado] == ["ana@x.com"]
        assert resultado[0].email_token == "tok-ana"

    def test_antes_da_migration_envia_para_todos_sem_link(self) -> None:
        resultado = aplicar_preferencias(_session(com_colunas=False), [ANA, BIA])
        assert resultado == [ANA, BIA]


class TestLinks:
    def test_pagina_no_app_e_one_click_na_api(self) -> None:
        links = links_descadastro(BRANDING, "a b&c")
        assert links.pagina == "https://mamutepolitico.com.br/app/descadastro?t=a%20b%26c"
        assert links.one_click == (
            "https://mamutepolitico.com.br/api/email-preferences/unsubscribe?t=a%20b%26c"
        )

    def test_sem_token_nao_tem_link(self) -> None:
        assert links_descadastro(BRANDING, None) is None


def _com_token(report, token: str | None):
    report.recipient = dataclasses.replace(report.recipient, email_token=token)
    return report


@pytest.mark.parametrize("design_novo", [False, True])
def test_rodape_do_relatorio_tem_o_link(design_novo: bool) -> None:
    report = _com_token(relatorio(), "tok-ana")
    report.design_novo = design_novo
    html = render_report_html(report, "fortnight", branding=BRANDING, settings={})
    assert "Parar de receber estes e-mails" in html
    assert "/app/descadastro?t=tok-ana" in html


@pytest.mark.parametrize("design_novo", [False, True])
def test_sem_token_o_rodape_fica_como_antes(design_novo: bool) -> None:
    report = relatorio()
    report.design_novo = design_novo
    html = render_report_html(report, "fortnight", branding=BRANDING, settings={})
    assert "Parar de receber" not in html
    assert "{{DESCADASTRO}}" not in html


class _SmtpFalso:
    enviadas: list[str] = []

    def __init__(self, *a, **k) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        pass

    def starttls(self) -> None:
        pass

    def login(self, *a) -> None:
        pass

    def send_message(self, msg, **k) -> None:
        _SmtpFalso.enviadas.append(msg.as_string())


def test_mailer_poe_list_unsubscribe_one_click(monkeypatch) -> None:
    monkeypatch.setattr(mailer.smtplib, "SMTP", _SmtpFalso)
    _SmtpFalso.enviadas = []
    smtp = SmtpConfig(user="u", password="p", sender="r@x.com", server="s", port=587, from_name="M")

    mailer.send_html_email("<p>oi</p>", "ana@x.com", "Assunto", smtp=smtp, unsubscribe_url="https://x/u?t=1")
    mailer.send_html_email("<p>oi</p>", "ana@x.com", "Assunto", smtp=smtp)

    com, sem = (message_from_string(m) for m in _SmtpFalso.enviadas)
    assert com["List-Unsubscribe"] == "<https://x/u?t=1>"
    assert com["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert sem["List-Unsubscribe"] is None


class TestResultadoDaEleicao:
    def _cenario_com_colunas(self):
        s = _cenario()
        s.execute(text("ALTER TABLE projetos ADD COLUMN emails_desativados_em TIMESTAMP"))
        s.execute(text("ALTER TABLE projetos ADD COLUMN email_token TEXT"))
        s.execute(text("UPDATE projetos SET email_token = 'tok-' || id"))
        s.commit()
        return s

    def test_quem_desativou_nao_recebe_mas_o_aviso_fica_no_app(self) -> None:
        s = self._cenario_com_colunas()
        s.execute(text("UPDATE projetos SET emails_desativados_em = '2026-10-16' WHERE id = 30"))
        s.commit()
        set_flag(s, "all")
        _fechar(s, CARGOS_MAJORITARIOS)
        mailer_falso = FakeMailer()

        stats = enviar(s, ciclo=CICLO, send=mailer_falso, turnos=(1,))

        assert mailer_falso.enviados == [("ana@x.com", ASSUNTO_MAJ)]
        assert stats.descadastrados == 1
        status = s.execute(
            text("SELECT email_status FROM election_result_notice WHERE projeto_id = 30")
        ).scalar()
        assert status == "skipped_opt_out"

    def test_e_mail_do_resultado_tem_o_link(self) -> None:
        s = self._cenario_com_colunas()
        _fechar(s, CARGOS_MAJORITARIOS)
        prontos, _ = montar_avisos(s, ciclo=CICLO, turno=1, disparo=DISPARO_MAJORITARIOS)
        aviso = prontos[0]
        aviso.email_token = "tok-10"
        assert "/app/descadastro?t=tok-10" in render_html(aviso, branding=BRANDING)
