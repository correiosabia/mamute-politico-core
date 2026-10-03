"""CS-121: espelho financeiro do Ghost e métricas da aba Financeiro."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api.services.admin_finance import crescimento, preco_real_por_email, resumo_financeiro
from api.services.ghost_finance_sync import garantir_sync_recente, sincronizar, ultimo_sync

DDL = [
    "create table ghost_membro (id text primary key, email text, status text not null, plano text, "
    "plano_valor_mensal_centavos integer, criado_em timestamp not null)",
    "create table ghost_membro_status_evento (id text primary key, membro_id text not null, "
    "de_status text, para_status text not null, criado_em timestamp not null)",
    "create table ghost_assinatura (id text primary key, membro_id text, email text, plano text, "
    "status text not null, intervalo text, valor_tabela_centavos integer not null default 0, "
    "mrr_centavos integer not null default 0, oferta text, oferta_desconto_tipo text, "
    "oferta_desconto_valor integer, oferta_duracao text, oferta_meses integer, "
    "desconto_fim timestamp, inicio timestamp)",
    "create table ghost_assinatura_evento (id text primary key, membro_id text, assinatura_id text, "
    "tipo text not null, mrr_delta_centavos integer not null default 0, criado_em timestamp not null)",
    "create table ghost_sync_execucao (id integer primary key autoincrement, "
    "iniciado_em timestamp not null default current_timestamp, concluido_em timestamp, "
    "ok boolean not null default 0, membros integer, assinaturas integer, eventos integer, erro text)",
]


def _sessao() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as conn:
        for ddl in DDL:
            conn.exec_driver_sql(ddl)
    return sessionmaker(bind=engine)()


def _dt(texto: str) -> datetime:
    return datetime.fromisoformat(texto)  # como o MySQL do Ghost: UTC sem fuso


def _ghost() -> dict:
    """Cinco membros: free, pagante com 80% (R$ 10), Fellowship 100%, anual sem
    oferta (R$ 500/ano) e cortesia (comped) no plano de R$ 50."""
    membros = [
        {"id": "m1", "email": "Free@x.com", "status": "free", "created_at": _dt("2026-07-01 10:00:00"), "plano": "Elefante Livre", "plano_valor_mensal": None},
        {"id": "m2", "email": "paga@x.com", "status": "paid", "created_at": _dt("2026-07-02 10:00:00"), "plano": "Mamute Completo", "plano_valor_mensal": 5000},
        {"id": "m3", "email": "fellow@x.com", "status": "paid", "created_at": _dt("2026-07-10 10:00:00"), "plano": "Mamute Completo", "plano_valor_mensal": 5000},
        {"id": "m4", "email": "anual@x.com", "status": "paid", "created_at": _dt("2026-07-20 10:00:00"), "plano": "Mamute Completo", "plano_valor_mensal": 5000},
        {"id": "m5", "email": "cortesia@x.com", "status": "comped", "created_at": _dt("2026-07-21 10:00:00"), "plano": "Mamute Completo", "plano_valor_mensal": 5000},
    ]
    assinatura = lambda i, m, email, intervalo, valor, mrr, oferta=None, pct=None, dur=None: {  # noqa: E731
        "id": i, "member_id": m, "email": email, "plano": "Mamute Completo", "status": "active",
        "intervalo": intervalo, "valor_tabela": valor, "mrr": mrr, "oferta": oferta,
        "oferta_desconto_tipo": "percent" if pct else None, "oferta_desconto_valor": pct,
        "oferta_duracao": dur, "oferta_meses": 12 if dur == "repeating" else None,
        "desconto_fim": None, "inicio": _dt("2026-07-02 10:00:00"),
    }
    assinaturas = [
        assinatura("s2", "m2", "paga@x.com", "month", 5000, 1000, "Mamute 80", 80, "repeating"),
        assinatura("s3", "m3", "fellow@x.com", "month", 5000, 0, "Fellowship", 100, "forever"),
        assinatura("s4", "m4", "anual@x.com", "year", 50000, 4167),
        {**assinatura("s9", "m1", "free@x.com", "month", 5000, 0), "status": "canceled"},
    ]
    eventos = [
        {"id": "e2", "member_id": "m2", "subscription_id": "s2", "type": "created", "mrr_delta": 1000, "created_at": _dt("2026-07-02 10:00:00")},
        {"id": "e3", "member_id": "m3", "subscription_id": "s3", "type": "created", "mrr_delta": 0, "created_at": _dt("2026-07-10 10:00:00")},
        {"id": "e4", "member_id": "m4", "subscription_id": "s4", "type": "created", "mrr_delta": 4167, "created_at": _dt("2026-07-20 10:00:00")},
    ]
    status = [
        {"id": "t2", "member_id": "m2", "from_status": "free", "to_status": "paid", "created_at": _dt("2026-07-02 10:00:00")},
        {"id": "t3", "member_id": "m3", "from_status": "free", "to_status": "paid", "created_at": _dt("2026-07-10 10:00:00")},
        {"id": "t4", "member_id": "m4", "from_status": "free", "to_status": "paid", "created_at": _dt("2026-07-20 10:00:00")},
        {"id": "t5", "member_id": "m5", "from_status": "free", "to_status": "comped", "created_at": _dt("2026-07-21 10:00:00")},
    ]
    return {"membros": membros, "assinaturas": assinaturas, "eventos": eventos, "status": status}


def _sincronizado() -> Session:
    db = _sessao()
    assert sincronizar(db, ghost_url="mysql://teste", leitor=lambda _url: _ghost())["ok"]
    return db


def test_resumo_separa_pagantes_isentos_e_receita_isenta() -> None:
    resumo = resumo_financeiro(_sincronizado())
    assert resumo["usuarios"] == {"gerais": 5, "com_plano": 4, "pagantes": 2, "isentos": 2}
    # real: 10 + 41,67; tabela: 50 + 50 + 41,67 (anual/12) + 50 (cortesia)
    assert resumo["receita_real"] == 51.67
    assert resumo["receita_tabela"] == 191.67
    assert resumo["receita_isenta"] == 140.0
    ofertas = {o["oferta"]: o for o in resumo["ofertas"]}
    assert ofertas["Fellowship"]["receita_isenta"] == 50.0
    assert ofertas["Fellowship"]["desconto"] == "100% para sempre"
    assert ofertas["Mamute 80"]["desconto"] == "80% por 12 meses"
    assert ofertas["Cortesia (comped)"]["assinaturas"] == 1
    assert ofertas["Sem oferta"]["receita_isenta"] == 0.0


def test_preco_real_por_email_ignora_assinatura_cancelada() -> None:
    precos = preco_real_por_email(_sincronizado())
    assert precos == {"paga@x.com": 10.0, "fellow@x.com": 0.0, "anual@x.com": 41.67, "cortesia@x.com": 0.0}


def test_crescimento_reconstroi_semanas_pelo_historico() -> None:
    semanas = crescimento(_sincronizado(), hoje=date(2026, 7, 26))["semanas"]
    por_semana = {s["semana"]: s for s in semanas}
    # semana que fecha em 05/07: free + pagante de 80%
    assert por_semana["2026-07-05"] == {
        "semana": "2026-07-05", "gerais": 2, "com_plano": 1, "pagantes": 1, "isentos": 0,
        "receita_real": 10.0, "receita_tabela": 50.0,
    }
    # semana que fecha em 26/07: todos; Fellowship e cortesia isentos
    ultima = por_semana["2026-07-26"]
    assert (ultima["gerais"], ultima["com_plano"], ultima["pagantes"], ultima["isentos"]) == (5, 4, 2, 2)
    assert ultima["receita_real"] == 51.67


def test_sync_sem_url_avisa_e_falha_fica_registrada() -> None:
    db = _sessao()
    assert sincronizar(db, ghost_url=None)["ok"] is False

    def quebra(_url: str) -> dict:
        raise RuntimeError("Unknown column 'mrr'")

    assert sincronizar(db, ghost_url="mysql://x", leitor=quebra)["ok"] is False
    assert "Unknown column" in ultimo_sync(db)["ultimo_erro"]
    assert ultimo_sync(db)["sincronizado_em"] is None


def test_sync_troca_o_conteudo_e_so_renova_quando_velho() -> None:
    db = _sincronizado()
    chamadas = []

    def leitor(_url: str) -> dict:
        chamadas.append(1)
        dados = _ghost()
        dados["membros"] = dados["membros"][:1]
        return dados

    garantir_sync_recente(db, ghost_url="mysql://x", leitor=leitor)
    assert chamadas == []  # cópia de agora há pouco: não renova
    garantir_sync_recente(
        db, agora=datetime(2099, 1, 1, tzinfo=timezone.utc), ghost_url="mysql://x", leitor=leitor
    )
    assert chamadas == [1]
    assert db.execute(text("select count(*) from ghost_membro")).scalar() == 1
