"""CS-148: rastreio de abertura dos e-mails do Mamute (mailer, send_log e job)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from mamute_scrappers.scripts.notificacao import aberturas, mailer
from mamute_scrappers.scripts.notificacao.config import SmtpConfig
from mamute_scrappers.scripts.notificacao.send_log import STATUS_SENT, log_send_attempt

AGORA = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
CFG = aberturas.MailgunConfig(api_key="k", api_base="https://mg.test/v3", domain="mamute.test")
SMTP = SmtpConfig(
    user="u", password="p", sender="relatorios@mamute.test",
    server="smtp.test", port=587, from_name="Mamute",
)


def _session(*, com_message_id: bool = True, com_aberturas: bool = True) -> Session:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE email_send_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "projeto_id BIGINT NOT NULL, email TEXT NOT NULL, periodicidade TEXT NOT NULL, "
            "status TEXT NOT NULL, detail TEXT, subject TEXT, stats JSON, "
            "period_start DATE, period_end DATE, "
            + ("message_id TEXT, " if com_message_id else "")
            + "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        if com_aberturas:
            c.exec_driver_sql(
                "CREATE TABLE email_aberturas (message_id TEXT PRIMARY KEY, "
                "primeira_abertura_em TIMESTAMP NOT NULL, ultima_abertura_em TIMESTAMP NOT NULL, "
                "tags TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
    return Session(engine)


class _FakeSMTP:
    enviadas: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg, **k):
        _FakeSMTP.enviadas.append(msg)


def _enviar(monkeypatch, **kwargs):
    _FakeSMTP.enviadas = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", _FakeSMTP)
    mid = mailer.send_html_email("<p>oi</p>", "ana@x.com", "Relatório", smtp=SMTP, **kwargs)
    return mid, _FakeSMTP.enviadas[0]


def test_com_tags_liga_so_abertura_e_devolve_message_id(monkeypatch):
    mid, msg = _enviar(monkeypatch, tags=(mailer.TAG_MAMUTE, mailer.TAG_RELATORIO))
    assert msg["Message-ID"] == f"<{mid}>"
    assert mid.endswith("@mamute.test")
    assert msg["X-Mailgun-Track-Opens"] == "yes"
    assert msg["X-Mailgun-Track-Clicks"] == "no"
    assert msg.get_all("X-Mailgun-Tag") == ["mamute", "mamute-relatorio"]


def test_sem_tags_nao_rastreia(monkeypatch):
    mid, msg = _enviar(monkeypatch)
    assert mid
    assert msg["X-Mailgun-Track-Opens"] is None
    assert msg.get_all("X-Mailgun-Tag") is None


def test_send_log_grava_message_id():
    s = _session()
    log_send_attempt(
        s, projeto_id=1, email="a@x.com", periodicidade="fortnight",
        status=STATUS_SENT, message_id="abc@mamute.test",
    )
    assert s.execute(text("SELECT message_id FROM email_send_log")).scalar() == "abc@mamute.test"


def test_send_log_sem_coluna_ainda_grava_a_linha():
    # Deploy sobe o código antes da migration: a linha não pode se perder.
    s = _session(com_message_id=False)
    log_send_attempt(
        s, projeto_id=1, email="a@x.com", periodicidade="fortnight",
        status=STATUS_SENT, message_id="abc@mamute.test",
    )
    assert s.execute(text("SELECT count(*) FROM email_send_log")).scalar() == 1


def _evento(mid, ts, tags=("mamute", "mamute-relatorio")):
    return {"event": "opened", "timestamp": ts, "tags": list(tags),
            "message": {"headers": {"message-id": mid}}}


def _fake_get(paginas):
    chamadas = []

    def get(url, params):
        chamadas.append((url, params))
        i = len(chamadas) - 1
        if i >= len(paginas):
            return {"items": [], "paging": {}}
        return {"items": paginas[i], "paging": {"next": f"https://mg.test/next/{i + 1}"}}

    return get, chamadas


def test_sincronizar_pagina_agrupa_e_filtra_pela_tag():
    t0 = AGORA.timestamp() - 3600
    get, chamadas = _fake_get([
        [_evento("m1@x", t0), _evento("m2@x", t0 + 5)],
        [_evento("m1@x", t0 + 60), {"event": "opened", "timestamp": t0, "message": {"headers": {}}}],
    ])
    s = _session()
    assert aberturas.sincronizar(s, cfg=CFG, get=get, agora=AGORA) == 2

    url, params = chamadas[0]
    assert url == "https://mg.test/v3/mamute.test/events"
    assert params["event"] == "opened" and params["tags"] == "mamute"
    assert params["begin"] == int(AGORA.timestamp()) - 26 * 3600
    assert chamadas[1] == ("https://mg.test/next/1", None)

    rows = dict(s.execute(text(
        "SELECT message_id, ultima_abertura_em FROM email_aberturas"
    )).all())
    assert set(rows) == {"m1@x", "m2@x"}


def test_reler_a_janela_nao_duplica_e_mantem_a_primeira_abertura():
    t0 = AGORA.timestamp() - 7200
    s = _session()
    aberturas.sincronizar(s, cfg=CFG, get=_fake_get([[_evento("m1@x", t0 + 100)]])[0], agora=AGORA)
    aberturas.sincronizar(
        s, cfg=CFG, get=_fake_get([[_evento("m1@x", t0), _evento("m1@x", t0 + 900)]])[0], agora=AGORA
    )
    rows = s.execute(text(
        "SELECT primeira_abertura_em, ultima_abertura_em FROM email_aberturas"
    )).all()
    assert len(rows) == 1
    primeira, ultima = rows[0]
    assert str(primeira) < str(ultima)
    assert str(primeira).startswith(datetime.fromtimestamp(t0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"))


def test_sem_chave_ou_sem_tabela_nao_roda(monkeypatch):
    monkeypatch.delenv("MAILGUN_API_KEY", raising=False)
    assert aberturas.sincronizar(_session()) is None
    get, chamadas = _fake_get([[_evento("m1@x", AGORA.timestamp())]])
    assert aberturas.sincronizar(_session(com_aberturas=False), cfg=CFG, get=get) is None
    assert chamadas == []
