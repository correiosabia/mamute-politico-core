"""Flags do relatório por e-mail com a mesma regra do app (resolve_for)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from mamute_scrappers.scripts.notificacao import flags
from mamute_scrappers.scripts.notificacao.models import ProjectRecipient

ADMINS = frozenset({"admin@mamute.com"})
ASSINANTE_PAGO = ProjectRecipient(id=1, email="pago@x.com", nome="p", tier_id=4)
ASSINANTE_GRATIS = ProjectRecipient(id=2, email="gratis@x.com", nome="g", tier_id=1)
SEM_PLANO = ProjectRecipient(id=3, email="sem@x.com", nome="s", tier_id=None)
ADMIN = ProjectRecipient(id=9, email="Admin@Mamute.com", nome="a", tier_id=1)


def _session(*, com_tabelas: bool = True) -> Session:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    if com_tabelas:
        with engine.begin() as conn:
            conn.exec_driver_sql("create table feature_flag (key text primary key, state text)")
            conn.exec_driver_sql(
                "create table feature_flag_tier (flag_key text, tier_id integer, mode text)"
            )
    return Session(engine)


def _estado(s: Session, key: str, state: str, *liberados: int) -> None:
    s.execute(text("insert into feature_flag values (:k, :s)"), {"k": key, "s": state})
    for tier in liberados:
        s.execute(
            text("insert into feature_flag_tier values (:k, :t, 'liberado')"),
            {"k": key, "t": tier},
        )
    s.commit()


def test_sem_linha_e_desligada() -> None:
    snap = flags.carregar_flags(_session())

    assert not snap.ativa(flags.FLAG_BALANCO, ADMIN, ADMINS)


def test_admins_liga_so_para_admin_sem_diferenciar_caixa() -> None:
    s = _session()
    _estado(s, flags.FLAG_DESIGN_NOVO, "admins", 4)
    snap = flags.carregar_flags(s)

    assert snap.ativa(flags.FLAG_DESIGN_NOVO, ADMIN, ADMINS)
    assert not snap.ativa(flags.FLAG_DESIGN_NOVO, ASSINANTE_PAGO, ADMINS)


def test_all_liga_para_o_plano_liberado_e_admin() -> None:
    s = _session()
    _estado(s, flags.FLAG_BALANCO, "all", 4)
    snap = flags.carregar_flags(s)

    assert snap.ativa(flags.FLAG_BALANCO, ASSINANTE_PAGO, ADMINS)
    assert snap.ativa(flags.FLAG_BALANCO, ADMIN, ADMINS)
    assert not snap.ativa(flags.FLAG_BALANCO, ASSINANTE_GRATIS, ADMINS)


def test_all_sem_modo_no_plano_fica_so_com_admin() -> None:
    s = _session()
    _estado(s, flags.FLAG_CONVITE, "all")
    snap = flags.carregar_flags(s)

    assert snap.ativa(flags.FLAG_CONVITE, ADMIN, ADMINS)
    assert not snap.ativa(flags.FLAG_CONVITE, ASSINANTE_GRATIS, ADMINS)


def test_conta_sem_plano_nao_recebe_flag_por_plano() -> None:
    s = _session()
    _estado(s, flags.FLAG_DESTAQUES_GERAIS, "all", 1, 4)
    snap = flags.carregar_flags(s)

    assert not snap.ativa(flags.FLAG_DESTAQUES_GERAIS, SEM_PLANO, ADMINS)


def test_tabela_ausente_e_tudo_desligado() -> None:
    snap = flags.carregar_flags(_session(com_tabelas=False))

    for key in flags.TODAS:
        assert not snap.ativa(key, ADMIN, ADMINS)


@pytest.mark.parametrize("key", ["email_design_novo", "email_balanco", "email_convite_assinatura", "email_destaques_gerais"])
def test_chaves_batem_com_o_registro_do_app(key: str) -> None:
    """O registro mora em ui/src/lib/featureFlags.ts no app; a chave tem de ser igual."""
    assert key in flags.TODAS
