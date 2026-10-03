"""Espelho financeiro do Ghost no Postgres (CS-121).

Lê, do MySQL do Ghost, membros, assinaturas (com o `mrr` real, já com a oferta
aplicada) e os eventos de assinatura e de status, e grava uma cópia completa
nas tabelas `ghost_*` (migration cs121a1b2c3d4). As métricas financeiras do
painel admin leem só essa cópia.

Acesso: `GHOST_DB_RO_URL` (usuário `mamute_ro`, só SELECT nas tabelas usadas
aqui). Sem a variável, o sync não roda e o painel avisa.

Quando roda: sob demanda, pela API. `garantir_sync_recente` renova a cópia
quando a última boa tem mais de `IDADE_MAXIMA` (a aba Financeiro chama ao
abrir) e o botão "Sincronizar agora" força. Não há cron: o volume é pequeno
(centenas a poucos milhares de linhas, ~1 s).

Datas do Ghost são UTC sem fuso; aqui viram UTC com fuso.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

IDADE_MAXIMA = timedelta(hours=1)

_SQL_MEMBROS = """
    SELECT m.id, m.email, m.status, m.created_at, p.name AS plano,
           p.monthly_price AS plano_valor_mensal
      FROM members m
      LEFT JOIN members_products mp ON mp.member_id = m.id
      LEFT JOIN products p ON p.id = mp.product_id
     ORDER BY m.id, mp.sort_order
"""

_SQL_ASSINATURAS = """
    SELECT s.id, c.member_id, m.email, s.plan_nickname AS plano, s.status,
           s.plan_interval AS intervalo, s.plan_amount AS valor_tabela, s.mrr,
           o.name AS oferta, o.discount_type AS oferta_desconto_tipo,
           o.discount_amount AS oferta_desconto_valor, o.duration AS oferta_duracao,
           o.duration_in_months AS oferta_meses, s.discount_end AS desconto_fim,
           s.start_date AS inicio
      FROM members_stripe_customers_subscriptions s
      LEFT JOIN members_stripe_customers c ON c.customer_id = s.customer_id
      LEFT JOIN members m ON m.id = c.member_id
      LEFT JOIN offers o ON o.id = s.offer_id
"""

_SQL_EVENTOS = """
    SELECT id, member_id, subscription_id, type, mrr_delta, created_at
      FROM members_paid_subscription_events
"""

_SQL_STATUS = """
    SELECT id, member_id, from_status, to_status, created_at
      FROM members_status_events
"""


def _utc(valor: Any) -> Optional[datetime]:
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(valor)).replace(tzinfo=timezone.utc)


def ler_ghost(ghost_url: str) -> dict[str, list[dict[str, Any]]]:
    """Lê as quatro consultas do MySQL do Ghost."""
    engine = create_engine(ghost_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            def linhas(sql: str) -> list[dict[str, Any]]:
                return [dict(r._mapping) for r in conn.execute(text(sql))]

            return {
                "membros": linhas(_SQL_MEMBROS),
                "assinaturas": linhas(_SQL_ASSINATURAS),
                "eventos": linhas(_SQL_EVENTOS),
                "status": linhas(_SQL_STATUS),
            }
    finally:
        engine.dispose()


def gravar(db: Session, dados: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """Troca o conteúdo das tabelas `ghost_*` pelo que veio do Ghost (uma transação)."""
    membros: dict[str, dict[str, Any]] = {}
    for m in dados["membros"]:
        # Membro com mais de um plano: fica o primeiro (sort_order).
        membros.setdefault(m["id"], m)

    for tabela in (
        "ghost_membro",
        "ghost_membro_status_evento",
        "ghost_assinatura",
        "ghost_assinatura_evento",
    ):
        db.execute(text(f"DELETE FROM {tabela}"))

    if membros:
        db.execute(
            text(
                "INSERT INTO ghost_membro (id, email, status, plano, plano_valor_mensal_centavos, criado_em) "
                "VALUES (:id, :email, :status, :plano, :valor, :criado_em)"
            ),
            [
                {
                    "id": m["id"],
                    "email": (m.get("email") or "").strip().lower() or None,
                    "status": m.get("status") or "free",
                    "plano": m.get("plano"),
                    "valor": m.get("plano_valor_mensal"),
                    "criado_em": _utc(m.get("created_at")),
                }
                for m in membros.values()
            ],
        )
    if dados["status"]:
        db.execute(
            text(
                "INSERT INTO ghost_membro_status_evento (id, membro_id, de_status, para_status, criado_em) "
                "VALUES (:id, :membro_id, :de, :para, :criado_em)"
            ),
            [
                {
                    "id": e["id"],
                    "membro_id": e["member_id"],
                    "de": e.get("from_status"),
                    "para": e.get("to_status") or "free",
                    "criado_em": _utc(e["created_at"]),
                }
                for e in dados["status"]
            ],
        )
    if dados["assinaturas"]:
        db.execute(
            text(
                """
                INSERT INTO ghost_assinatura (
                    id, membro_id, email, plano, status, intervalo, valor_tabela_centavos,
                    mrr_centavos, oferta, oferta_desconto_tipo, oferta_desconto_valor,
                    oferta_duracao, oferta_meses, desconto_fim, inicio
                ) VALUES (
                    :id, :membro_id, :email, :plano, :status, :intervalo, :valor_tabela,
                    :mrr, :oferta, :oferta_desconto_tipo, :oferta_desconto_valor,
                    :oferta_duracao, :oferta_meses, :desconto_fim, :inicio
                )
                """
            ),
            [
                {
                    "id": a["id"],
                    "membro_id": a.get("member_id"),
                    "email": (a.get("email") or "").strip().lower() or None,
                    "plano": a.get("plano"),
                    "status": a.get("status") or "unknown",
                    "intervalo": a.get("intervalo"),
                    "valor_tabela": int(a.get("valor_tabela") or 0),
                    "mrr": int(a.get("mrr") or 0),
                    "oferta": a.get("oferta"),
                    "oferta_desconto_tipo": a.get("oferta_desconto_tipo"),
                    "oferta_desconto_valor": a.get("oferta_desconto_valor"),
                    "oferta_duracao": a.get("oferta_duracao"),
                    "oferta_meses": a.get("oferta_meses"),
                    "desconto_fim": _utc(a.get("desconto_fim")),
                    "inicio": _utc(a.get("inicio")),
                }
                for a in dados["assinaturas"]
            ],
        )
    if dados["eventos"]:
        db.execute(
            text(
                "INSERT INTO ghost_assinatura_evento (id, membro_id, assinatura_id, tipo, mrr_delta_centavos, criado_em) "
                "VALUES (:id, :membro_id, :assinatura_id, :tipo, :mrr_delta, :criado_em)"
            ),
            [
                {
                    "id": e["id"],
                    "membro_id": e.get("member_id"),
                    "assinatura_id": e.get("subscription_id"),
                    "tipo": e.get("type") or "unknown",
                    "mrr_delta": int(e.get("mrr_delta") or 0),
                    "criado_em": _utc(e["created_at"]),
                }
                for e in dados["eventos"]
            ],
        )
    return {
        "membros": len(membros),
        "assinaturas": len(dados["assinaturas"]),
        "eventos": len(dados["eventos"]) + len(dados["status"]),
    }


def sincronizar(
    db: Session,
    *,
    ghost_url: Optional[str] = None,
    leitor: Callable[[str], dict[str, list[dict[str, Any]]]] = ler_ghost,
) -> dict[str, Any]:
    """Renova a cópia e registra a execução em `ghost_sync_execucao`."""
    url = ghost_url or os.getenv("GHOST_DB_RO_URL")
    if not url:
        return {"ok": False, "erro": "GHOST_DB_RO_URL não configurada na API."}
    try:
        dados = leitor(url)
        contagem = gravar(db, dados)
        db.execute(
            text(
                "INSERT INTO ghost_sync_execucao (iniciado_em, concluido_em, ok, membros, assinaturas, eventos) "
                "VALUES (:agora, :agora, TRUE, :membros, :assinaturas, :eventos)"
            ),
            {"agora": datetime.now(timezone.utc), **contagem},
        )
        db.commit()
        return {"ok": True, **contagem}
    except Exception as exc:  # noqa: BLE001 — erro vira registro e aviso na tela
        db.rollback()
        logger.exception("Falha no sync financeiro do Ghost")
        db.execute(
            text(
                "INSERT INTO ghost_sync_execucao (iniciado_em, concluido_em, ok, erro) "
                "VALUES (:agora, :agora, FALSE, :erro)"
            ),
            {"agora": datetime.now(timezone.utc), "erro": str(exc)[:500]},
        )
        db.commit()
        return {"ok": False, "erro": str(exc)[:500]}


def ultimo_sync(db: Session) -> dict[str, Any]:
    ok = db.execute(
        text("SELECT max(concluido_em) FROM ghost_sync_execucao WHERE ok")
    ).scalar()
    falha = db.execute(
        text(
            "SELECT concluido_em, erro FROM ghost_sync_execucao "
            "WHERE NOT ok ORDER BY id DESC LIMIT 1"
        )
    ).first()
    ok_em = _utc(ok)
    falha_em = _utc(falha.concluido_em) if falha else None
    return {
        "sincronizado_em": ok_em.isoformat() if ok_em else None,
        # Só mostra a falha se ela for mais nova que o último sucesso.
        "ultimo_erro": (
            falha.erro if falha and (ok_em is None or (falha_em and falha_em > ok_em)) else None
        ),
    }


def garantir_sync_recente(db: Session, *, agora: Optional[datetime] = None, **kwargs: Any) -> None:
    """Sincroniza se a última cópia boa for mais velha que IDADE_MAXIMA."""
    ultimo = _utc(db.execute(text("SELECT max(concluido_em) FROM ghost_sync_execucao WHERE ok")).scalar())
    agora = agora or datetime.now(timezone.utc)
    if ultimo is None or agora - ultimo > IDADE_MAXIMA:
        sincronizar(db, **kwargs)


__all__ = ["garantir_sync_recente", "gravar", "ler_ghost", "sincronizar", "ultimo_sync"]
