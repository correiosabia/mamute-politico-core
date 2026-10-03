"""Agregações para o painel de métricas admin.

Custo/margem são computados em Python (lendo tier.detalhes) para evitar
diferenças de acesso a JSON entre PostgreSQL e SQLite (usado nos testes).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, time as dt_time, timedelta
from datetime import timezone as dt_timezone
from typing import Any, Optional

from sqlalchemy import func, select, text
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Session

logger = logging.getLogger("admin_metrics")

_PAGE_LABELS = {
    "inicio": "Início",
    "dashboard": "Dashboard Geral",
    "selecao": "Selecionar Parlamentares",
    "pesquisa": "Pesquisa IA (visitas)",
    "parlamentar": "Página de Parlamentar",
}


def _page_label(page: Optional[str]) -> str:
    return _PAGE_LABELS.get(page or "", page or "—")


def _house_of(ptype: Optional[str]) -> str:
    return "senado" if ptype and "senad" in ptype.lower() else "camara"

# BCB SGS série 1 (dólar comercial venda) — primário; awesomeapi de fallback
# (o plano gratuito dela bloqueia o IP de prod por quota → 429 permanente).
_USD_BRL_BCB_URL = (
    "https://api.bcb.gov.br/dados/serie/bcdata.sgs.1/dados/ultimos/1?formato=json"
)
_USD_BRL_URL = "https://economia.awesomeapi.com.br/last/USD-BRL"
_RATE_TTL_SECONDS = 1800
_rate_cache: dict[str, Any] = {"value": None, "at": 0.0}
# Emergência de cold-start APENAS: sem env, sem linha em usd_brl_rate e API fora.
# Em regime a taxa vem da tabela usd_brl_rate (real, atualizada 1x/dia às 04h
# por mamute_scrappers.scripts.refresh_admin_caches). Sem valor fixo "de fachada".
_COLD_START_USD_BRL = 6.0


def _fetch_live_usd_brl() -> Optional[float]:
    try:
        import requests
    except Exception:  # noqa: BLE001
        return None
    try:
        resp = requests.get(_USD_BRL_BCB_URL, timeout=4)
        resp.raise_for_status()
        return float(resp.json()[0]["valor"])
    except Exception:  # noqa: BLE001 — câmbio nunca pode quebrar as métricas
        pass
    try:
        resp = requests.get(_USD_BRL_URL, timeout=4)
        resp.raise_for_status()
        return float(resp.json()["USDBRL"]["bid"])
    except Exception:  # noqa: BLE001
        return None


def _latest_rate_from_db(db: Optional[Session]) -> Optional[float]:
    if db is None:
        return None
    try:
        row = db.execute(
            text("SELECT bid FROM usd_brl_rate ORDER BY rate_date DESC LIMIT 1")
        ).first()
    except Exception:  # noqa: BLE001 — tabela ausente (migration/testes) → ignora
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    return float(row[0]) if row and row[0] is not None else None


def get_usd_brl_rate(db: Optional[Session] = None) -> float:
    """Taxa USD→BRL real do dia.

    Ordem: env (pin p/ testes) → cache em memória (30min) → tabela usd_brl_rate
    (fonte da verdade, preenchida diariamente) → busca ao vivo (bootstrap) →
    último valor conhecido. Sem fallback fixo desatualizado no caminho normal.
    """
    override = os.getenv("MAMUTE_USD_BRL_RATE")
    if override:
        try:
            return float(override)
        except ValueError:
            pass

    now = time.time()
    cached = _rate_cache["value"]
    if cached is not None and now - _rate_cache["at"] < _RATE_TTL_SECONDS:
        return float(cached)

    rate = _latest_rate_from_db(db)
    if rate is None:
        rate = _fetch_live_usd_brl()
    if rate is None:
        # Sem banco nem rede: usa o último valor conhecido em memória; se não há
        # nenhum (cold start absoluto), emergência logada — some no 1º ciclo.
        if cached is not None:
            return float(cached)
        logger.warning(
            "Câmbio USD→BRL indisponível (sem env/tabela/rede); usando cold-start %.2f",
            _COLD_START_USD_BRL,
        )
        return _COLD_START_USD_BRL

    _rate_cache["value"] = rate
    _rate_cache["at"] = now
    return rate

try:
    from ..db.models.project import Projetos, ProjetosParliamentarian, Tiers
    from ..db.models.chatbot_usage import ChatbotUsage
    from ..db.models.usage_event import UsageEvent
except ImportError:  # execução dentro de api/
    from db.models.project import Projetos, ProjetosParliamentarian, Tiers
    from db.models.chatbot_usage import ChatbotUsage
    from db.models.usage_event import UsageEvent


def current_period_start(today: Optional[date] = None) -> date:
    """Primeiro dia do mês atual (mesma convenção de chatbot_usage.period_start)."""
    ref = today or date.today()
    return ref.replace(day=1)


def _usage_by_project(db: Session, period_start: Optional[date]) -> dict[int, Any]:
    tokens = func.coalesce(ChatbotUsage.prompt_tokens, 0) + func.coalesce(
        ChatbotUsage.completion_tokens, 0
    )
    stmt = (
        select(
            ChatbotUsage.projeto_id.label("projeto_id"),
            func.count().label("calls"),
            func.coalesce(func.sum(tokens), 0).label("tokens"),
            func.coalesce(func.sum(ChatbotUsage.cost_usd), 0).label("cost"),
        )
        .where(ChatbotUsage.status == "completed")
        .group_by(ChatbotUsage.projeto_id)
    )
    if period_start is not None:
        stmt = stmt.where(ChatbotUsage.period_start == period_start)
    return {row.projeto_id: row for row in db.execute(stmt)}


def _favorites_by_project(db: Session) -> dict[int, int]:
    stmt = (
        select(
            ProjetosParliamentarian.projeto_id.label("projeto_id"),
            func.count().label("n"),
        )
        .where(ProjetosParliamentarian.deleted_at.is_(None))
        .group_by(ProjetosParliamentarian.projeto_id)
    )
    return {row.projeto_id: int(row.n) for row in db.execute(stmt)}


def _precos_reais(db: Session) -> Optional[dict[str, float]]:
    """Preço real por e-mail vindo do espelho do Ghost (CS-121); None sem espelho.

    Checa a tabela antes (em vez de try/rollback) para não desfazer a
    transação de quem chamou na janela do deploy antes da migration cs121.
    """
    try:
        from .admin_finance import preco_real_por_email
    except ImportError:  # execução dentro de api/
        from services.admin_finance import preco_real_por_email
    if not sqlalchemy_inspect(db.connection()).has_table("ghost_membro"):
        return None
    if not db.execute(text("SELECT 1 FROM ghost_membro LIMIT 1")).first():
        return None
    return preco_real_por_email(db)


def metrics_users(
    db: Session,
    period_start: date,
    usd_brl_rate: float,
    *,
    limit: Optional[int] = None,
    search: Optional[str] = None,
) -> list[dict[str, Any]]:
    query = select(Projetos).where(Projetos.deleted_at.is_(None))
    if search:
        like = f"%{search.strip()}%"
        query = query.where(Projetos.nome.ilike(like) | Projetos.email.ilike(like))
    projetos = db.execute(query).scalars().all()
    month = _usage_by_project(db, period_start)
    total = _usage_by_project(db, None)
    favorites = _favorites_by_project(db)
    precos_reais = _precos_reais(db)

    users: list[dict[str, Any]] = []
    for projeto in projetos:
        detalhes = (getattr(projeto.tier, "detalhes", None) or {}) if projeto.tier else {}
        preco = float(detalhes.get("preco_mensal") or 0)
        month_row = month.get(projeto.id)
        total_row = total.get(projeto.id)
        custo_mes = float(month_row.cost) if month_row else 0.0
        custo_mes_brl = custo_mes * usd_brl_rate
        consultas_mes = int(month_row.calls) if month_row else 0
        limite_consultas = detalhes.get("qtd_consultas_ia_mes")
        limite_parlamentares = detalhes.get("qtd_termos")
        monitorados = favorites.get(projeto.id, 0)
        # Preço real (com oferta) vem do Ghost; sem espelho, cai no de tabela.
        preco_real = (
            precos_reais.get((projeto.email or "").strip().lower(), 0.0)
            if precos_reais is not None
            else None
        )
        preco_margem = preco_real if preco_real is not None else preco

        users.append(
            {
                "projeto_id": projeto.id,
                "email": projeto.email,
                "nome": projeto.nome,
                "plano": projeto.tier.product_id if projeto.tier else None,
                "preco_mensal": round(preco, 2),
                "preco_real": round(preco_real, 2) if preco_real is not None else None,
                "consultas_mes": consultas_mes,
                "consultas_total": int(total_row.calls) if total_row else 0,
                "tokens_mes": int(month_row.tokens) if month_row else 0,
                "custo_mes": round(custo_mes, 6),
                "custo_mes_brl": round(custo_mes_brl, 2),
                "margem_mes": round(preco_margem - custo_mes_brl, 2),
                "parlamentares_monitorados": monitorados,
                "limite_parlamentares": limite_parlamentares,
                "limite_consultas": limite_consultas,
                # Acima do plano = estourou IA/mês OU parlamentares monitorados.
                "acima_do_plano": bool(
                    (limite_consultas is not None and consultas_mes > int(limite_consultas))
                    or (
                        limite_parlamentares is not None
                        and monitorados > int(limite_parlamentares)
                    )
                ),
            }
        )

    users.sort(key=lambda u: u["custo_mes"], reverse=True)
    if limit is not None:
        return users[:limit]
    return users


def _ia_by_day(
    db: Session, projeto_id: Optional[int], usd_brl_rate: float
) -> list[dict[str, Any]]:
    dia = func.date(ChatbotUsage.created_at)
    stmt = (
        select(
            dia.label("dia"),
            func.count().label("consultas"),
            func.coalesce(func.sum(ChatbotUsage.cost_usd), 0).label("custo"),
        )
        .where(ChatbotUsage.status == "completed")
        .group_by(dia)
        .order_by(dia)
    )
    if projeto_id is not None:
        stmt = stmt.where(ChatbotUsage.projeto_id == projeto_id)
    return [
        {
            "dia": str(row.dia),
            "consultas": int(row.consultas),
            "custo_brl": round(float(row.custo) * usd_brl_rate, 2),
        }
        for row in db.execute(stmt)
    ]


def _pages_by_project(db: Session, projeto_id: int) -> list[dict[str, Any]]:
    stmt = (
        select(UsageEvent.page.label("page"), func.count().label("views"))
        .where(
            UsageEvent.projeto_id == projeto_id,
            UsageEvent.event_type == "page_view",
        )
        .group_by(UsageEvent.page)
        .order_by(func.count().desc())
    )
    return [
        {"page": row.page or "—", "views": int(row.views)} for row in db.execute(stmt)
    ]


def _swaps_by_project(db: Session, projeto_id: int) -> dict[str, int]:
    stmt = (
        select(UsageEvent.event_type.label("etype"), func.count().label("cnt"))
        .where(
            UsageEvent.projeto_id == projeto_id,
            UsageEvent.event_type.in_(["favorite_added", "favorite_removed"]),
        )
        .group_by(UsageEvent.event_type)
    )
    counts = {row.etype: int(row.cnt) for row in db.execute(stmt)}
    adicionados = counts.get("favorite_added", 0)
    removidos = counts.get("favorite_removed", 0)
    return {"adicionados": adicionados, "removidos": removidos, "total": adicionados + removidos}


def metrics_user_detail(
    db: Session, projeto_id: int, period_start: date, usd_brl_rate: float
) -> Optional[dict[str, Any]]:
    base = next(
        (
            u
            for u in metrics_users(db, period_start, usd_brl_rate)
            if u["projeto_id"] == projeto_id
        ),
        None,
    )
    if base is None:
        return None
    return {
        **base,
        "ia_por_dia": _ia_by_day(db, projeto_id, usd_brl_rate),
        "paginas": _pages_by_project(db, projeto_id),
        "trocas": _swaps_by_project(db, projeto_id),
    }


def metrics_tools(db: Session) -> list[dict[str, Any]]:
    """Ferramentas mais usadas: page views + consultas IA + trocas de monitoramento."""
    tools: list[dict[str, Any]] = []

    page_rows = (
        db.execute(
            select(UsageEvent.page.label("page"), func.count().label("n"))
            .where(UsageEvent.event_type == "page_view")
            .group_by(UsageEvent.page)
        )
        .all()
    )
    for row in page_rows:
        if row.page:
            tools.append({"tool": _page_label(row.page), "uses": int(row.n)})

    ia_calls = db.execute(
        select(func.count()).where(ChatbotUsage.status == "completed")
    ).scalar_one()
    tools.append({"tool": "Pesquisa IA (consultas)", "uses": int(ia_calls or 0)})

    trocas = db.execute(
        select(func.count()).where(
            UsageEvent.event_type.in_(["favorite_added", "favorite_removed"])
        )
    ).scalar_one()
    tools.append({"tool": "Monitoramento (trocas)", "uses": int(trocas or 0)})

    tools.sort(key=lambda t: t["uses"], reverse=True)
    return tools


def metrics_sections(db: Session) -> list[dict[str, Any]]:
    """Seções mais vistas dentro das telas (event_type='section_view')."""
    stmt = (
        select(
            UsageEvent.page.label("page"),
            UsageEvent.section.label("section"),
            func.count().label("views"),
        )
        .where(UsageEvent.event_type == "section_view")
        .group_by(UsageEvent.page, UsageEvent.section)
        .order_by(func.count().desc())
    )
    return [
        {
            "page": _page_label(row.page),
            "section": row.section or "—",
            "views": int(row.views),
        }
        for row in db.execute(stmt)
    ]


def metrics_parliamentarians(db: Session, limit: int = 20) -> dict[str, Any]:
    """Parlamentares mais monitorados, separando Câmara/Senado e por estado."""
    rows = (
        db.execute(
            text(
                """
                SELECT p.id AS pid, p.name AS name, p.type AS ptype,
                       COALESCE(p.state_elected, '') AS state, COUNT(*) AS monitors
                FROM projetos_parliamentarian pp
                JOIN parliamentarian p ON p.id = pp.parliamentarian_id
                WHERE pp.deleted_at IS NULL
                GROUP BY p.id, p.name, p.type, p.state_elected
                ORDER BY monitors DESC
                """
            )
        )
        .mappings()
        .all()
    )

    top: list[dict[str, Any]] = []
    by_house = {"camara": 0, "senado": 0}
    by_state: dict[str, int] = {}
    for row in rows:
        house = _house_of(row["ptype"])
        monitors = int(row["monitors"])
        by_house[house] += monitors
        state = row["state"] or "?"
        by_state[state] = by_state.get(state, 0) + monitors
        top.append(
            {
                "parliamentarian_id": row["pid"],
                "name": row["name"],
                "house": house,
                "state": state,
                "monitors": monitors,
            }
        )

    by_state_list = sorted(
        ({"state": s, "monitors": n} for s, n in by_state.items()),
        key=lambda x: x["monitors"],
        reverse=True,
    )
    return {"top": top[:limit], "by_house": by_house, "by_state": by_state_list}


def metrics_mamutometro(db: Session, limit: int = 200) -> dict[str, Any]:
    """Parlamentares por marcações do mamutômetro, nas três leituras.

    `project_mamutometro` tem no máximo uma linha por (assinante, parlamentar),
    então COUNT(*) é "quantas pessoas marcaram", SUM(level) é o total de
    mamutinhos e AVG(level) a média por pessoa. Devolve a lista inteira (o teto
    de linhas é o nº de parlamentares) e o front escolhe a ordenação — top-N
    por visão mudaria a composição da lista a cada aba.

    Só agregados saem daqui: nível é privado por spec, e nada aqui responde
    "quem marcou o político X".
    """
    rows = (
        db.execute(
            text(
                """
                SELECT p.id AS pid, p.name AS name, p.type AS ptype,
                       COALESCE(p.state_elected, '') AS state,
                       COUNT(*) AS people,
                       SUM(pm.level) AS total,
                       AVG(pm.level) AS media
                FROM project_mamutometro pm
                JOIN parliamentarian p ON p.id = pm.parliamentarian_id
                GROUP BY p.id, p.name, p.type, p.state_elected
                ORDER BY people DESC, total DESC
                """
            )
        )
        .mappings()
        .all()
    )

    top = [
        {
            "parliamentarian_id": row["pid"],
            "name": row["name"],
            "house": _house_of(row["ptype"]),
            "state": row["state"] or "?",
            "people": int(row["people"]),
            "total": int(row["total"]),
            "average": round(float(row["media"]), 2),
        }
        for row in rows[:limit]
    ]
    # Totais sobre TODAS as linhas, não só as devolvidas — senão o corte do
    # limit faria os cards de total mentirem.
    totals = {
        "parliamentarians": len(rows),
        "marks": sum(int(row["people"]) for row in rows),
        "mamutinhos": sum(int(row["total"]) for row in rows),
    }
    return {"top": top, "totals": totals}


def metrics_candidacy_favorites(db: Session, limit: int = 20) -> dict[str, Any]:
    """Candidaturas mais acompanhadas (projetos_candidacy), com recortes.

    Recortes por cargo e por UF saem do MESMO conjunto de vínculos, para os
    números baterem entre si na tela.
    """
    rows = (
        db.execute(
            text(
                """
                SELECT c.id AS cid,
                       COALESCE(c.ballot_name, c.full_name) AS name,
                       c.office AS office, c.office_code AS office_code,
                       COALESCE(c.state, '?') AS state, c.party AS party,
                       COUNT(*) AS monitors
                FROM projetos_candidacy pc
                JOIN candidacy c ON c.id = pc.candidacy_id
                GROUP BY c.id, c.ballot_name, c.full_name, c.office,
                         c.office_code, c.state, c.party
                ORDER BY monitors DESC, name ASC
                """
            )
        )
        .mappings()
        .all()
    )

    by_office: dict[str, int] = {}
    by_state: dict[str, int] = {}
    total_links = 0
    for row in rows:
        monitors = int(row["monitors"])
        total_links += monitors
        office = row["office"] or "?"
        by_office[office] = by_office.get(office, 0) + monitors
        by_state[row["state"]] = by_state.get(row["state"], 0) + monitors

    top = [
        {
            "candidacy_id": row["cid"],
            "name": row["name"],
            "office": row["office"],
            "office_code": row["office_code"],
            "state": row["state"],
            "party": row["party"],
            "monitors": int(row["monitors"]),
        }
        for row in rows[:limit]
    ]
    users = db.execute(
        text("SELECT COUNT(DISTINCT projeto_id) FROM projetos_candidacy")
    ).scalar()
    return {
        "top": top,
        "by_office": sorted(
            ({"office": o, "monitors": n} for o, n in by_office.items()),
            key=lambda x: x["monitors"],
            reverse=True,
        ),
        "by_state": sorted(
            ({"state": s, "monitors": n} for s, n in by_state.items()),
            key=lambda x: x["monitors"],
            reverse=True,
        ),
        "totals": {
            "links": total_links,
            "candidacies": len(rows),
            "users": int(users or 0),
        },
    }


def _ia_saude(db: Session, period_start: Optional[date]) -> dict[str, Any]:
    """Saúde do chatbot no período: respostas vazias, falhas e tempo de resposta.

    Uma consulta 'completed' com answer_chars=0 significa que o LLM respondeu
    mas nenhum token chegou ao cliente (o usuário viu só o indicador de
    digitação) — foi assim que o streaming ficou quebrado semanas sem ninguém
    perceber. Este bloco existe para esse tipo de falha aparecer no painel.

    A duração usa updated_at - created_at (início do stream → registro final).
    Agregado em Python porque o volume mensal é pequeno e a subtração de
    timestamps não é portável entre Postgres e SQLite (testes).
    """
    stmt = select(
        ChatbotUsage.status,
        ChatbotUsage.answer_chars,
        ChatbotUsage.created_at,
        ChatbotUsage.updated_at,
    )
    if period_start is not None:
        stmt = stmt.where(ChatbotUsage.period_start == period_start)

    ok = vazias = falhas = canceladas = 0
    durations: list[float] = []
    for status, answer_chars, created_at, updated_at in db.execute(stmt):
        if status == "completed":
            if (answer_chars or 0) > 0:
                ok += 1
            else:
                vazias += 1
            if created_at and updated_at and updated_at >= created_at:
                durations.append((updated_at - created_at).total_seconds())
        elif status == "failed":
            falhas += 1
        elif status == "cancelled":
            canceladas += 1

    durations.sort()
    tempo_medio = round(sum(durations) / len(durations), 1) if durations else None
    tempo_p95 = (
        round(durations[min(int(len(durations) * 0.95), len(durations) - 1)], 1)
        if durations
        else None
    )
    return {
        "respostas_ok": ok,
        "respostas_vazias": vazias,
        "falhas": falhas,
        "canceladas": canceladas,
        "tempo_medio_s": tempo_medio,
        "tempo_p95_s": tempo_p95,
    }


def metrics_ia(db: Session, period_start: date, usd_brl_rate: float) -> dict[str, Any]:
    """Métricas focadas em IA: volume, tokens, custo, top usuários, série diária."""
    usage = _usage_by_project(db, period_start)
    consultas = sum(int(r.calls) for r in usage.values())
    tokens = sum(int(r.tokens) for r in usage.values())
    custo_usd = sum(float(r.cost) for r in usage.values())

    users = metrics_users(db, period_start, usd_brl_rate)
    top = sorted(users, key=lambda u: u["custo_mes"], reverse=True)[:10]
    return {
        "consultas_mes": consultas,
        "tokens_mes": tokens,
        "custo_mes_brl": round(custo_usd * usd_brl_rate, 2),
        "usd_brl_rate": round(usd_brl_rate, 4),
        "saude": _ia_saude(db, period_start),
        "por_dia": _ia_by_day(db, None, usd_brl_rate),
        "top_usuarios": [
            {
                "projeto_id": u["projeto_id"],
                "email": u["email"],
                "nome": u["nome"],
                "consultas_mes": u["consultas_mes"],
                "custo_mes_brl": u["custo_mes_brl"],
            }
            for u in top
        ],
    }


def metrics_overview(
    db: Session, period_start: date, usd_brl_rate: float
) -> dict[str, Any]:
    users = metrics_users(db, period_start, usd_brl_rate)
    receita_tabela = sum(u["preco_mensal"] for u in users)
    # CS-121: receita real (com ofertas) quando o espelho do Ghost existe.
    tem_espelho = any(u["preco_real"] is not None for u in users)
    receita = sum(u["preco_real"] or 0 for u in users) if tem_espelho else receita_tabela
    custo_usd = sum(u["custo_mes"] for u in users)
    custo_brl = sum(u["custo_mes_brl"] for u in users)
    return {
        "usuarios": len(users),
        "consultas_mes": sum(u["consultas_mes"] for u in users),
        "tokens_mes": sum(u["tokens_mes"] for u in users),
        "custo_mes": round(custo_usd, 6),
        "custo_mes_brl": round(custo_brl, 2),
        "receita_mes": round(receita, 2),
        "receita_tabela_mes": round(receita_tabela, 2),
        "margem_mes": round(receita - custo_brl, 2),
        "usd_brl_rate": round(usd_brl_rate, 4),
        "parlamentares_monitorados": sum(u["parlamentares_monitorados"] for u in users),
        "usuarios_acima_do_plano": sum(1 for u in users if u["acima_do_plano"]),
    }


# --------------------------------------------------------------------------- #
# E-mails: histórico de envios (email_send_log, escrito pelos scrappers) +
# próximos disparos previstos (espelha o cron: 11:00 UTC; day diário, week
# segunda, fortnight dias 1/16, month dia 1 — ver docker/scrappers.cron).
# --------------------------------------------------------------------------- #
_EMAIL_PERIODICIDADES = ["day", "week", "fortnight", "month"]
_EMAIL_PERIODICIDADE_LABELS = {
    "day": "Diário",
    "week": "Semanal",
    "fortnight": "Quinzenal",
    "month": "Mensal",
}
_EMAIL_CRON_HOUR_UTC = 11


def _next_email_run(
    periodicidade: str, now: Optional[datetime] = None
) -> Optional[datetime]:
    ref = now or datetime.now(dt_timezone.utc)
    for offset in range(0, 63):
        d = (ref + timedelta(days=offset)).date()
        run = datetime.combine(
            d, dt_time(hour=_EMAIL_CRON_HOUR_UTC), tzinfo=dt_timezone.utc
        )
        if run <= ref:
            continue
        if (
            periodicidade == "day"
            or (periodicidade == "week" and d.weekday() == 0)
            or (periodicidade == "fortnight" and d.day in (1, 16))
            or (periodicidade == "month" and d.day == 1)
        ):
            return run
    return None


def _email_history(db: Session, limit: int) -> Optional[list[dict[str, Any]]]:
    """Linhas de email_send_log; None se a tabela ainda não existe."""
    try:
        rows = db.execute(
            text(
                "SELECT id, projeto_id, email, periodicidade, status, detail, "
                "subject, stats, period_start, period_end, created_at "
                "FROM email_send_log ORDER BY created_at DESC, id DESC LIMIT :n"
            ),
            {"n": limit},
        ).mappings().all()
    except Exception:  # noqa: BLE001 — migration ainda não aplicada
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None

    def _iso(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):  # SQLite (testes) devolve texto no SQL cru
            return value
        return value.isoformat()

    out: list[dict[str, Any]] = []
    for r in rows:
        stats = r["stats"]
        if isinstance(stats, str):
            try:
                stats = json.loads(stats)
            except ValueError:
                stats = None
        out.append(
            {
                "id": r["id"],
                "projeto_id": r["projeto_id"],
                "email": r["email"],
                "periodicidade": r["periodicidade"],
                "periodicidade_label": _EMAIL_PERIODICIDADE_LABELS.get(
                    r["periodicidade"], r["periodicidade"]
                ),
                "status": r["status"],
                "detail": r["detail"],
                "subject": r["subject"],
                "stats": stats,
                "period_start": _iso(r["period_start"]),
                "period_end": _iso(r["period_end"]),
                "created_at": _iso(r["created_at"]),
            }
        )
    return out


def metrics_emails(
    db: Session, now: Optional[datetime] = None, history_limit: int = 200
) -> dict[str, Any]:
    """Aba E-mails do painel: histórico de envios + próximos disparos previstos."""
    historico = _email_history(db, history_limit)

    tiers = list(
        db.execute(select(Tiers).where(Tiers.deleted_at.is_(None))).scalars()
    )
    proj_counts = {
        row.tier_id: int(row.n)
        for row in db.execute(
            select(Projetos.tier_id, func.count().label("n"))
            .where(Projetos.deleted_at.is_(None), Projetos.tier_id.is_not(None))
            .group_by(Projetos.tier_id)
        )
    }
    fav_counts = {
        row.tier_id: int(row.n)
        for row in db.execute(
            select(
                Projetos.tier_id,
                func.count(func.distinct(Projetos.id)).label("n"),
            )
            .join(
                ProjetosParliamentarian,
                ProjetosParliamentarian.projeto_id == Projetos.id,
            )
            .where(
                Projetos.deleted_at.is_(None),
                Projetos.tier_id.is_not(None),
                ProjetosParliamentarian.deleted_at.is_(None),
            )
            .group_by(Projetos.tier_id)
        )
    }

    proximos: list[dict[str, Any]] = []
    for p in _EMAIL_PERIODICIDADES:
        matching = [t for t in tiers if p in (t.periodicidade_email or [])]
        tier_ids = [t.id for t in matching]
        run = _next_email_run(p, now)
        proximos.append(
            {
                "periodicidade": p,
                "periodicidade_label": _EMAIL_PERIODICIDADE_LABELS[p],
                "proximo_envio": run.isoformat() if run else None,
                "tiers": [t.tier_name_debug for t in matching],
                # destinatários = projetos ativos do tier; o envio ainda pula
                # quem não tem favoritos ou não teve atividade no período.
                "destinatarios": sum(proj_counts.get(i, 0) for i in tier_ids),
                "com_favoritos": sum(fav_counts.get(i, 0) for i in tier_ids),
            }
        )

    hist = historico or []
    enviados = [h for h in hist if h["status"] == "sent"]
    return {
        "log_disponivel": historico is not None,
        "kpis": {
            "enviados": len(enviados),
            "erros": sum(1 for h in hist if h["status"] == "error"),
            "pulados": sum(1 for h in hist if h["status"].startswith("skipped")),
            "ultimo_envio": enviados[0]["created_at"] if enviados else None,
        },
        "historico": hist,
        "proximos": proximos,
    }
