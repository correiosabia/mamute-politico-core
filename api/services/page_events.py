"""Eventos anônimos das páginas públicas do admin, como Eleições 2026 (CS-166).

Mesmo tratamento da Na Lupa (`collection_events`, CS-145), que é reaproveitado
aqui: id aleatório de visitante e de sessão gerados no navegador, IP só para
estimar a região (país, estado, cidade) e para o limite de envio, nunca
gravado. Robôs e páginas que não estão públicas não gravam nada.

O painel lê a tabela crua e agrega em Python, como o da Na Lupa.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

try:
    from ..db.models.page_event import PageEvent
    from . import collection_events as ev
except ImportError:  # execução dentro de api/
    from db.models.page_event import PageEvent
    from services import collection_events as ev

# Páginas que coletam métricas (chave de `pagina_publicacao`).
PAGINAS_COM_METRICAS = {"eleicoes_2026"}

TIPOS = {
    "visit",  # abriu a página (target = aba de entrada)
    "tab_change",  # trocou de aba (target = aba)
    "tab_time",  # tempo lendo uma aba (target = aba, duration_ms)
    "scroll_depth",  # rolou até um marco da aba (target = aba; detail = 25, 50, 75 ou 100)
    "control",  # escolha dentro da aba (target = "aba/controle"; detail = valor)
    "view_change",  # trocou gráfico, mapa ou planilha (target = visão; detail = título do gráfico)
    "share",  # compartilhou (target = peça, ex. "votos-17h"; detail = meio)
    "map_detail",  # abriu o detalhe de um ponto do mapa (target = "local" ou "municipio"; detail = lugar)
}
MARCOS_DE_ROLAGEM = ("25", "50", "75", "100")
# Escolhas que são exportação de um gráfico (detail = título).
EXPORTACOES = {"grafico_baixado", "grafico_copiado"}


def grava_lote(
    db: Session,
    *,
    pagina: str,
    visitor: str,
    session_id: str,
    eventos: list[dict[str, Any]],
    user_agent: Optional[str],
    ip: Optional[str],
    referrer: Optional[str],
    utm: dict[str, Optional[str]],
) -> int:
    validos = [e for e in eventos[: ev.MAX_EVENTOS_POR_LOTE] if e.get("type") in TIPOS]
    if not validos:
        return 0
    cabem = ev.consome_cota(ip or f"sem-ip:{visitor}", len(validos))
    if not cabem:
        return 0
    regiao = ev.regiao_do_ip(ip) or (None, None, None)
    comum = dict(
        page=pagina,
        visitor_id=visitor,
        session_id=session_id,
        device=ev.aparelho(user_agent),
        country=regiao[0],
        region=regiao[1],
        city=regiao[2],
        referrer_host=ev.host_de_origem(referrer),
        utm_source=ev._corta(utm.get("source")),
        utm_medium=ev._corta(utm.get("medium")),
        utm_campaign=ev._corta(utm.get("campaign")),
    )
    for e in validos[:cabem]:
        db.add(
            PageEvent(
                **comum,
                event_type=e["type"],
                target=ev._corta(e.get("target")),
                detail=ev._corta(e.get("detail")),
                duration_ms=ev._duracao(e.get("duration_ms")),
            )
        )
    db.commit()
    return cabem


def metricas_da_pagina(db: Session, pagina: str, dias: int = 30) -> dict[str, Any]:
    dias = max(1, min(dias, 365))
    fim = datetime.now(timezone.utc)
    inicio = fim - timedelta(days=dias)
    linhas = db.execute(
        text(
            "SELECT created_at, visitor_id, session_id, event_type, target, detail, duration_ms, "
            "device, country, region, city, referrer_host, utm_source "
            "FROM page_event WHERE page = :p AND created_at >= :i"
        ),
        {"p": pagina, "i": inicio},
    ).all()

    sessoes: set[str] = set()
    visitantes: set[str] = set()
    por_dia_visitas: Counter = Counter()
    por_dia_visitantes: dict[date, set[str]] = defaultdict(set)
    estados: Counter = Counter()
    cidades: Counter = Counter()
    paises: Counter = Counter()
    origens: Counter = Counter()
    aparelhos: Counter = Counter()
    entradas: Counter = Counter()
    trocas: Counter = Counter()
    viram: dict[str, set[str]] = defaultdict(set)
    aba_ms: Counter = Counter()
    aba_sessoes_com_tempo: dict[str, set[str]] = defaultdict(set)
    rolagem: dict[tuple[str, str], set[str]] = defaultdict(set)
    tempo_total_ms = 0
    escolhas: Counter = Counter()
    graficos: dict[str, Counter] = defaultdict(Counter)
    visoes: Counter = Counter()
    compartilhamentos: Counter = Counter()
    detalhes: Counter = Counter()

    for quando, visitante, sessao, tipo, alvo, detalhe, ms, device, pais, estado, cidade, ref, utm in linhas:
        if tipo == "visit":
            if alvo:
                entradas[alvo] += 1
                viram[alvo].add(sessao)
            sessoes.add(sessao)
            visitantes.add(visitante)
            dia = ev._dia_local(quando)
            por_dia_visitas[dia] += 1
            por_dia_visitantes[dia].add(visitante)
            if estado:
                estados[(estado, pais)] += 1
            if cidade:
                cidades[(cidade, estado)] += 1
            paises[pais or "?"] += 1
            origens[utm or ref or "direto"] += 1
            aparelhos[device or "desktop"] += 1
        elif tipo == "tab_change" and alvo:
            trocas[alvo] += 1
            viram[alvo].add(sessao)
        elif tipo == "tab_time" and alvo and ms:
            tempo_total_ms += ms
            aba_ms[alvo] += ms
            aba_sessoes_com_tempo[alvo].add(sessao)
            viram[alvo].add(sessao)
        elif tipo == "scroll_depth" and alvo and detalhe in MARCOS_DE_ROLAGEM:
            rolagem[(alvo, detalhe)].add(sessao)
            viram[alvo].add(sessao)
        elif tipo == "control" and alvo and "/" in alvo:
            aba, controle = alvo.split("/", 1)
            escolhas[(aba, controle, detalhe or "")] += 1
            if controle in EXPORTACOES and detalhe:
                graficos[detalhe]["exportacoes"] += 1
        elif tipo == "view_change" and alvo:
            visoes[alvo] += 1
            if detalhe:
                graficos[detalhe]["trocas"] += 1
        elif tipo == "share":
            compartilhamentos[(alvo or "?", detalhe or "?")] += 1
        elif tipo == "map_detail":
            detalhes[(alvo or "?", detalhe or "?")] += 1

    dia = inicio.astimezone(ev.FUSO).date()
    por_dia = []
    while dia <= fim.astimezone(ev.FUSO).date():
        por_dia.append({"dia": dia.isoformat(), "visitas": por_dia_visitas[dia], "visitantes": len(por_dia_visitantes[dia])})
        dia += timedelta(days=1)

    def ordem(contador: Counter):
        return sorted(contador.items(), key=lambda kv: (-kv[1], str(kv[0])))

    def pct(parte: int, todo: int) -> int:
        return round(100 * parte / todo) if todo else 0

    abas = [
        {
            "aba": aba,
            "entradas": entradas[aba],
            "trocas": trocas[aba],
            "sessoes": len(quem),
            "tempo_medio_s": (
                round(aba_ms[aba] / 1000 / len(aba_sessoes_com_tempo[aba])) if aba_sessoes_com_tempo[aba] else 0
            ),
            "rolagem": {m: pct(len(rolagem[(aba, m)]), len(quem)) for m in MARCOS_DE_ROLAGEM},
        }
        for aba, quem in sorted(viram.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]

    return {
        "pagina": pagina,
        "periodo": {"dias": dias, "inicio": inicio.isoformat(), "fim": fim.isoformat()},
        "kpis": {
            "visitas": len(sessoes),
            "visitantes": len(visitantes),
            "tempo_medio_s": round(tempo_total_ms / 1000 / len(sessoes)) if sessoes else 0,
            "compartilhamentos": sum(compartilhamentos.values()),
            "detalhes_no_mapa": sum(detalhes.values()),
        },
        "por_dia": por_dia,
        "regioes": [{"estado": e, "pais": p, "visitas": v} for (e, p), v in ordem(estados)[:15]],
        "cidades": [{"cidade": c, "estado": e, "visitas": v} for (c, e), v in ordem(cidades)[:15]],
        "paises": ev._top(paises, "pais", "visitas"),
        "origens": ev._top(origens, "origem", "visitas"),
        "dispositivos": ev._top(aparelhos, "aparelho", "visitas"),
        "abas": abas,
        "escolhas": [
            {"aba": a, "controle": c, "valor": v, "vezes": n}
            for (a, c, v), n in sorted(escolhas.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1], kv[0][2]))[:120]
        ],
        "graficos": [
            {"titulo": t, "trocas": c["trocas"], "exportacoes": c["exportacoes"]}
            for t, c in sorted(graficos.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))[:20]
        ],
        "visoes": ev._top(visoes, "visao", "trocas"),
        "compartilhamentos": [{"peca": p, "meio": m, "total": v} for (p, m), v in ordem(compartilhamentos)],
        "detalhes_no_mapa": [{"tipo": t, "lugar": l, "cliques": v} for (t, l), v in ordem(detalhes)[:30]],
    }
