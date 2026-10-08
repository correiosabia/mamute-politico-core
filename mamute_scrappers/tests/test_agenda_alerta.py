"""Aviso de compromisso novo nas agendas oficiais (CS-135)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from mamute_scrappers.agenda_crawler import alerta


@pytest.fixture()
def session(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(
            text(
                "CREATE TABLE official_agenda_item (id INTEGER PRIMARY KEY, source TEXT, authority_id TEXT, "
                "authority_name TEXT, office_label TEXT, organization TEXT, event_date DATE, starts_at TEXT, "
                "description TEXT, participants TEXT)"
            )
        )
    s = sessionmaker(bind=engine)()
    monkeypatch.setattr(alerta, "_termos", lambda _s: ["Fulano Exemplar"])
    yield s
    s.close()


def _add(s, i, desc, autoridade="1", dia=date(2025, 4, 1), participantes=None):
    s.execute(
        text(
            "INSERT INTO official_agenda_item (id, source, authority_id, authority_name, office_label, organization, "
            "event_date, starts_at, description, participants) VALUES (:i, 'bcb', :a, 'Pessoa', 'Diretor', 'BC', "
            ":d, '11:00', :t, :p)"
        ),
        {"i": i, "a": autoridade, "d": dia, "t": desc, "p": participantes},
    )
    s.commit()


def test_so_avisa_o_que_e_novo_e_cita_termo(session, monkeypatch):
    _add(session, 1, "Audiência com Fulano Exemplar.")
    _add(session, 2, "Reunião sem relação.")
    antes = alerta.foto(session, "bcb")
    _add(session, 3, "Nova audiência com FULANO EXEMPLAR.", dia=date(2025, 5, 8))
    # A mesma reunião na agenda de outra autoridade: uma linha só no e-mail.
    _add(session, 4, "Nova audiência com FULANO EXEMPLAR.", autoridade="2", dia=date(2025, 5, 8))
    _add(session, 5, "Programa habitacional", dia=date(2025, 6, 1), participantes="Sicrano representando Fulano Exemplar")
    _add(session, 6, "Outra reunião sem relação.", dia=date(2025, 6, 2))
    novos = alerta.novos(session, "bcb", antes)
    assert [str(n["event_date"]) for n in novos] == ["2025-06-01", "2025-05-08"]

    enviados = []
    monkeypatch.setenv("AGENDA_ALERT_EMAILS", "a@x.com, b@x.com")
    import mamute_scrappers.scripts.notificacao.mailer as mailer

    monkeypatch.setattr(mailer, "send_html_email", lambda corpo, para, assunto: enviados.append((para, assunto, corpo)))
    assert alerta.avisar_novos(session, "bcb", antes) == 2
    assert [e[0] for e in enviados] == ["a@x.com", "b@x.com"]
    assert "2 reunião(ões) nova(s)" in enviados[0][1] and "Programa habitacional" in enviados[0][2]


def test_primeira_carga_e_erro_nao_avisam(session, monkeypatch):
    _add(session, 1, "Audiência com Fulano Exemplar.")
    assert alerta.novos(session, "bcb", set()) == []

    antes = alerta.foto(session, "bcb")
    _add(session, 2, "Outra com Fulano Exemplar.", dia=date(2025, 7, 1))
    monkeypatch.setenv("AGENDA_ALERT_EMAILS", "a@x.com")
    import mamute_scrappers.scripts.notificacao.mailer as mailer

    def quebra(*_a, **_k):
        raise RuntimeError("SMTP fora")

    monkeypatch.setattr(mailer, "send_html_email", quebra)
    # Falha no envio não sobe: a coleta segue.
    assert alerta.avisar_novos(session, "bcb", antes) == 0


def test_destinatarios(monkeypatch):
    monkeypatch.delenv("AGENDA_ALERT_EMAILS", raising=False)
    monkeypatch.setenv("MAMUTE_ADMIN_EMAILS", "x@y.com,invalido")
    assert alerta.destinatarios() == ["x@y.com"]
