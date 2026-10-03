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
    assinatura = lambda i, m, email, intervalo, valor, mrr, oferta=None, pct=None, dur=None, inicio="2026-07-02 10:00:00", fim=None: {  # noqa: E731
        "id": i, "member_id": m, "email": email, "plano": "Mamute Completo", "status": "active",
        "intervalo": intervalo, "valor_tabela": valor, "mrr": mrr, "oferta": oferta,
        "oferta_desconto_tipo": "percent" if pct else None, "oferta_desconto_valor": pct,
        "oferta_duracao": dur, "oferta_meses": 12 if dur == "repeating" else None,
        "desconto_fim": _dt(fim) if fim else None, "inicio": _dt(inicio),
    }
    assinaturas = [
        assinatura("s2", "m2", "paga@x.com", "month", 5000, 1000, "Mamute 80", 80, "repeating"),
        assinatura("s3", "m3", "fellow@x.com", "month", 5000, 0, "Fellowship", 100, "forever", inicio="2026-07-10 10:00:00"),
        assinatura("s4", "m4", "anual@x.com", "year", 50000, 4167, inicio="2026-07-20 10:00:00"),
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


def test_oferta_temporaria_que_o_ghost_lanca_cheia_vale_com_desconto() -> None:
    """O MRR do Ghost ignora oferta "por 12 meses": vem 5000 com 90% de desconto."""
    dados = _ghost()
    dados["assinaturas"][0].update(
        mrr=5000, oferta="Mamute 90", oferta_desconto_valor=90, desconto_fim=_dt("2099-01-01 00:00:00")
    )
    db = _sessao()
    sincronizar(db, ghost_url="mysql://x", leitor=lambda _u: dados)
    assert preco_real_por_email(db)["paga@x.com"] == 5.0
    # depois que o desconto acaba, volta a valer o MRR do Ghost
    dados["assinaturas"][0]["desconto_fim"] = _dt("2020-01-01 00:00:00")
    sincronizar(db, ghost_url="mysql://x", leitor=lambda _u: dados)
    assert preco_real_por_email(db)["paga@x.com"] == 50.0


def test_preco_real_por_email_ignora_assinatura_cancelada() -> None:
    precos = preco_real_por_email(_sincronizado())
    assert precos == {"paga@x.com": 10.0, "fellow@x.com": 0.0, "anual@x.com": 41.67, "cortesia@x.com": 0.0}


def test_crescimento_reconstroi_semanas_pelo_historico() -> None:
    semanas = crescimento(_sincronizado(), hoje=date(2026, 7, 26))["semanas"]
    por_semana = {s["semana"]: s for s in semanas}
    # semana que fecha em 05/07: free + pagante de 80%
    primeira = por_semana["2026-07-05"]
    assert {k: primeira[k] for k in ("gerais", "com_plano", "pagantes", "isentos", "receita_real", "receita_tabela")} == {
        "gerais": 2, "com_plano": 1, "pagantes": 1, "isentos": 0, "receita_real": 10.0, "receita_tabela": 50.0,
    }
    # entradas da semana: 2 membros, 1 assinatura (a cancelada sem evento de fim fica fora)
    assert (primeira["membros_entradas"], primeira["plano_entradas"], primeira["plano_saidas"]) == (2, 1, 0)
    assert primeira["variacao_gerais"] is None  # sem semana anterior
    segunda = por_semana["2026-07-12"]
    assert segunda["variacao_gerais"] == 50.0  # 2 -> 3 membros
    # cancelamento da s9 (assinatura do free) nunca conta: sem evento, fica fora
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


def test_taxas_da_stripe_somam_o_mes_e_avisam_sem_chave() -> None:
    from api.services.stripe_fees import taxas_do_mes

    assert taxas_do_mes(chave="")["disponivel"] is False

    class Resp:
        def __init__(self, corpo: dict) -> None:
            self.corpo = corpo

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.corpo

    paginas = [
        {"data": [{"id": "t1", "type": "charge", "amount": 1000, "fee": 79}], "has_more": True},
        {"data": [{"id": "t2", "type": "charge", "amount": 5000, "fee": 239},
                  {"id": "t3", "type": "payout", "amount": -4000, "fee": 0}], "has_more": False},
    ]
    chamadas = []

    def http_get(url, params, auth, timeout):
        chamadas.append(dict(params))
        return Resp(paginas[len(chamadas) - 1])

    r = taxas_do_mes(chave="rk_test", http_get=http_get, agora=datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert r == {"disponivel": True, "recebido_bruto": 60.0, "taxas": 3.18, "taxas_percentual": 5.3, "cobrancas": 2}
    assert chamadas[1]["starting_after"] == "t1"


def test_marcos_criar_listar_apagar_e_validar() -> None:
    import pytest

    from api.services import metric_milestones

    db = _sessao()
    db.execute(text(
        "create table metrica_marco (id integer primary key autoincrement, data date not null, "
        "titulo text not null, descricao text, criado_por text, criado_em timestamp default current_timestamp)"
    ))
    db.commit()
    marco = metric_milestones.criar(
        db, data=date(2026, 9, 28), titulo="  Início da Fellowship ", descricao="15 jornalistas", criado_por="a@x.com"
    )
    assert marco["titulo"] == "Início da Fellowship" and marco["data"] == "2026-09-28"
    metric_milestones.criar(db, data=date(2026, 8, 12), titulo="Mamute 80/90", descricao=None, criado_por=None)
    assert [m["titulo"] for m in metric_milestones.listar(db)] == ["Mamute 80/90", "Início da Fellowship"]
    with pytest.raises(ValueError):
        metric_milestones.criar(db, data=date(2026, 9, 1), titulo="  ", descricao=None, criado_por=None)
    assert metric_milestones.apagar(db, marco["id"]) is True
    assert metric_milestones.apagar(db, marco["id"]) is False


def test_crescimento_mensal_e_saida_de_membro_apagado() -> None:
    db = _sincronizado()
    db.execute(text("create table projetos (id integer primary key, deleted_at timestamp)"))
    db.execute(text("insert into projetos (id, deleted_at) values (1, '2026-07-15 12:00:00'), (2, null)"))
    db.commit()
    meses = crescimento(db, hoje=date(2026, 8, 10), granularidade="mes")["periodos"]
    assert [m["periodo"] for m in meses] == ["2026-07-31", "2026-08-31"]
    julho, agosto = meses
    assert (julho["membros_entradas"], julho["membros_saidas"], julho["plano_entradas"]) == (5, 1, 4)
    assert julho["com_plano"] == 4 and julho["parcial"] is False
    assert agosto["parcial"] is True and agosto["membros_entradas"] == 0
    assert agosto["variacao_com_plano"] == 0.0
