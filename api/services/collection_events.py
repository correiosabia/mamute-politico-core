"""Eventos anônimos das páginas públicas de coleção (CS-145).

Quem lê uma coleção não tem login, então o evento não tem dono: o navegador
manda um id aleatório de visitante (guardado no próprio site) e um de sessão.
O IP só serve para estimar a região (país, estado, cidade) numa base local e
para o limite de envio; ele não é gravado.

O painel lê a tabela crua e agrega em Python: o volume é de milhares de eventos
por dia, e a janela padrão é de 30 dias.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

try:
    from ..db.models.collection import STATUS_PUBLISHED, Collection
    from ..db.models.collection_event import CollectionEvent
except ImportError:  # execução dentro de api/
    from db.models.collection import STATUS_PUBLISHED, Collection
    from db.models.collection_event import CollectionEvent

logger = logging.getLogger(__name__)

TIPOS = {
    "visit",  # abriu a página
    "section_time",  # tempo numa seção (target = seção, duration_ms)
    "view_change",  # trocou de visão (target = tudo, citados...)
    "card_click",  # clicou num card (target = id da pessoa)
    "details_open",  # abriu "Ver dados" (target = id da pessoa)
    "profile_click",  # foi ao perfil (target = id da pessoa)
    "follow_click",  # clicou em "Acompanhar" (target = id da pessoa)
    "share",  # compartilhou (target = "caso" ou a peça; detail = meio)
    "reduce_motion",  # ligou ou desligou "Reduzir animações" (detail = on/off)
    "back_to_top",  # usou "Voltar ao topo"
    "email_signup",  # se cadastrou no e-mail pela página
}
TAM_TEXTO = 80
TETO_DURACAO_MS = 30 * 60 * 1000
LIMITE_POR_MINUTO = 240
MAX_EVENTOS_POR_LOTE = 50
FUSO = ZoneInfo("America/Sao_Paulo")

_ROBO = re.compile(
    r"bot|crawl|spider|slurp|facebookexternalhit|whatsapp|telegram|preview|headless|"
    r"lighthouse|python-requests|curl|wget|httpclient|okhttp|scrapy|phantom|puppeteer|playwright",
    re.I,
)
_MOVEL = re.compile(r"Mobile|Android|iPhone|iPad|iPod", re.I)


# --------------------------------------------------------------------------
# entrada
# --------------------------------------------------------------------------


def ip_do_cliente(xff: Optional[str], peer: Optional[str]) -> Optional[str]:
    """IP de quem acessou: o último IP público da cadeia de proxies.

    O começo do X-Forwarded-For vem do próprio cliente e pode ser forjado; os
    proxies da casa (nginx do host, Caddy) só acrescentam no fim e têm IP
    privado, então o último público é o que o nosso primeiro proxy viu.
    """
    cadeia = [p.strip() for p in (xff or "").split(",") if p.strip()]
    if peer:
        cadeia.append(peer)
    for candidato in reversed(cadeia):
        try:
            ip = ipaddress.ip_address(candidato)
        except ValueError:
            continue
        if ip.is_global:
            return str(ip)
    return None


def eh_robo(user_agent: Optional[str]) -> bool:
    return not user_agent or bool(_ROBO.search(user_agent))


def aparelho(user_agent: Optional[str]) -> str:
    return "mobile" if user_agent and _MOVEL.search(user_agent) else "desktop"


def host_de_origem(referrer: Optional[str]) -> Optional[str]:
    if not referrer:
        return None
    host = (urlparse(referrer).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("l.") or host.startswith("lm."):  # l.instagram.com, lm.facebook.com
        host = host.split(".", 1)[1]
    return host[:TAM_TEXTO] or None


def _corta(valor: Optional[str]) -> Optional[str]:
    if valor is None:
        return None
    valor = str(valor).strip()
    return valor[:TAM_TEXTO] or None


def _duracao(ms: Optional[int]) -> Optional[int]:
    if ms is None or ms < 0:
        return None
    return min(int(ms), TETO_DURACAO_MS)


# --------------------------------------------------------------------------
# limite por IP (memória do processo; a API roda com um worker só)
# --------------------------------------------------------------------------

_limite: dict[str, tuple[int, int]] = {}
_trava = threading.Lock()


def consome_cota(chave: str, quantidade: int) -> int:
    """Quantos eventos ainda cabem no minuto atual para esta chave (já descontados)."""
    minuto = int(time.time() // 60)
    with _trava:
        if len(_limite) > 50_000:
            _limite.clear()
        inicio, usados = _limite.get(chave, (minuto, 0))
        if inicio != minuto:
            usados = 0
        cabem = max(0, min(quantidade, LIMITE_POR_MINUTO - usados))
        _limite[chave] = (minuto, usados + cabem)
        return cabem


# --------------------------------------------------------------------------
# região pelo IP (base DB-IP Lite, local)
# --------------------------------------------------------------------------

_leitor: Any = None
_leitor_tentado = False


def _abre_base() -> Any:
    global _leitor, _leitor_tentado
    if _leitor_tentado:
        return _leitor
    _leitor_tentado = True
    caminho = os.getenv("GEOIP_DB_PATH", "/geoip/dbip-city-lite.mmdb")
    if not os.path.exists(caminho):
        logger.warning("Base de região por IP ausente em %s: eventos ficam sem região.", caminho)
        return None
    try:
        import maxminddb

        _leitor = maxminddb.open_database(caminho)
    except Exception:  # pragma: no cover - base corrompida
        logger.exception("Não consegui abrir a base de região por IP.")
    return _leitor


def regiao_do_ip(ip: Optional[str]) -> Optional[tuple[Optional[str], Optional[str], Optional[str]]]:
    """(país ISO, estado, cidade) aproximados, ou None."""
    leitor = _abre_base() if ip else None
    if leitor is None:
        return None
    try:
        dado = leitor.get(ip) or {}
    except Exception:
        return None

    def nome(no: Any) -> Optional[str]:
        nomes = (no or {}).get("names") or {}
        return nomes.get("pt-BR") or nomes.get("en")

    pais = (dado.get("country") or {}).get("iso_code")
    subdivisoes = dado.get("subdivisions") or []
    estado = nome(subdivisoes[0]) if subdivisoes else None
    return pais, _corta(estado), _corta(nome(dado.get("city")))


# --------------------------------------------------------------------------
# gravação
# --------------------------------------------------------------------------


def colecao_publicada(db: Session, slug: str) -> Optional[int]:
    return db.execute(
        select(Collection.id).where(Collection.slug == slug, Collection.status == STATUS_PUBLISHED)
    ).scalar_one_or_none()


def grava_lote(
    db: Session,
    *,
    collection_id: int,
    visitor: str,
    session_id: str,
    eventos: list[dict[str, Any]],
    user_agent: Optional[str],
    ip: Optional[str],
    referrer: Optional[str],
    utm: dict[str, Optional[str]],
) -> int:
    validos = [e for e in eventos[:MAX_EVENTOS_POR_LOTE] if e.get("type") in TIPOS]
    if not validos:
        return 0
    cabem = consome_cota(ip or f"sem-ip:{visitor}", len(validos))
    if not cabem:
        return 0
    regiao = regiao_do_ip(ip) or (None, None, None)
    comum = dict(
        collection_id=collection_id,
        visitor_id=visitor,
        session_id=session_id,
        device=aparelho(user_agent),
        country=regiao[0],
        region=regiao[1],
        city=regiao[2],
        referrer_host=host_de_origem(referrer),
        utm_source=_corta(utm.get("source")),
        utm_medium=_corta(utm.get("medium")),
        utm_campaign=_corta(utm.get("campaign")),
    )
    for e in validos[:cabem]:
        db.add(
            CollectionEvent(
                **comum,
                event_type=e["type"],
                target=_corta(e.get("target")),
                detail=_corta(e.get("detail")),
                duration_ms=_duracao(e.get("duration_ms")),
            )
        )
    db.commit()
    return cabem


# --------------------------------------------------------------------------
# painel
# --------------------------------------------------------------------------


def lista_casos(db: Session) -> list[dict[str, Any]]:
    linhas = db.execute(
        select(Collection.slug, Collection.title)
        .where(Collection.status == STATUS_PUBLISHED)
        .order_by(Collection.published_at.desc(), Collection.id.desc())
    ).all()
    return [{"slug": s, "titulo": t} for s, t in linhas]


def _dia_local(quando: Any) -> date:
    if isinstance(quando, str):  # SQLite devolve texto
        quando = datetime.fromisoformat(quando)
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    return quando.astimezone(FUSO).date()


def _top(contador: Counter, chave: str, valor: str, n: int = 15) -> list[dict[str, Any]]:
    return [{chave: k, valor: v} for k, v in sorted(contador.items(), key=lambda kv: (-kv[1], str(kv[0])))[:n]]


def metricas_do_caso(db: Session, slug: str, dias: int = 30) -> Optional[dict[str, Any]]:
    caso = db.execute(select(Collection.id, Collection.title).where(Collection.slug == slug)).first()
    if caso is None:
        return None
    colecao_id, titulo = caso
    dias = max(1, min(dias, 365))
    fim = datetime.now(timezone.utc)
    inicio = fim - timedelta(days=dias)

    linhas = db.execute(
        text(
            "SELECT created_at, visitor_id, session_id, event_type, target, detail, duration_ms, "
            "device, country, region, city, referrer_host, utm_source "
            "FROM collection_event WHERE collection_id = :c AND created_at >= :i"
        ),
        {"c": colecao_id, "i": inicio},
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
    tempo_total_ms = 0
    secao_ms: Counter = Counter()
    secao_sessoes: dict[str, set[str]] = defaultdict(set)
    visoes: Counter = Counter()
    pessoas: dict[str, Counter] = defaultdict(Counter)
    meios: dict[str, Counter] = defaultdict(Counter)
    outros: Counter = Counter()
    cadastros = 0

    for (quando, visitante, sessao, tipo, alvo, detalhe, ms, device, pais, estado, cidade, ref, utm) in linhas:
        if tipo == "visit":
            sessoes.add(sessao)
            visitantes.add(visitante)
            dia = _dia_local(quando)
            por_dia_visitas[dia] += 1
            por_dia_visitantes[dia].add(visitante)
            if estado:
                estados[(estado, pais)] += 1
            if cidade:
                cidades[(cidade, estado)] += 1
            paises[pais or "?"] += 1
            origens[utm or ref or "direto"] += 1
            aparelhos[device or "desktop"] += 1
        elif tipo == "section_time" and alvo and ms:
            tempo_total_ms += ms
            secao_ms[alvo] += ms
            secao_sessoes[alvo].add(sessao)
        elif tipo == "view_change" and alvo:
            visoes[alvo] += 1
        elif tipo in ("card_click", "details_open", "profile_click", "follow_click") and alvo:
            pessoas[alvo][tipo] += 1
        elif tipo == "share":
            meio = detalhe or "?"
            meios[meio]["do_caso" if alvo == "caso" else "de_cards"] += 1
            if alvo and alvo.isdigit():
                pessoas[alvo]["share"] += 1
        elif tipo == "reduce_motion" and detalhe == "on":
            outros["reduzir_animacoes"] += 1
        elif tipo == "back_to_top":
            outros["voltar_ao_topo"] += 1
        elif tipo == "email_signup":
            cadastros += 1

    nomes = dict(
        db.execute(
            text("SELECT id, display_name FROM collection_member WHERE collection_id = :c"), {"c": colecao_id}
        ).all()
    )
    politicos = []
    for alvo, c in pessoas.items():
        politicos.append(
            {
                "id": int(alvo) if alvo.isdigit() else None,
                "nome": nomes.get(int(alvo)) if alvo.isdigit() else alvo,
                "cliques": c["card_click"],
                "ver_dados": c["details_open"],
                "perfil": c["profile_click"],
                "acompanhar": c["follow_click"],
                "compartilhamentos": c["share"],
            }
        )
    politicos.sort(key=lambda p: -(p["cliques"] + p["ver_dados"] + p["perfil"] + p["acompanhar"] + p["compartilhamentos"]))

    visitas = len(sessoes)
    total_share = sum(sum(c.values()) for c in meios.values())
    dia = inicio.astimezone(FUSO).date()
    por_dia = []
    while dia <= fim.astimezone(FUSO).date():
        por_dia.append({"dia": dia.isoformat(), "visitas": por_dia_visitas[dia], "visitantes": len(por_dia_visitantes[dia])})
        dia += timedelta(days=1)

    return {
        "caso": {"slug": slug, "titulo": titulo},
        "periodo": {"dias": dias, "inicio": inicio.isoformat(), "fim": fim.isoformat()},
        "kpis": {
            "visitas": visitas,
            "visitantes": len(visitantes),
            "tempo_medio_s": round(tempo_total_ms / 1000 / visitas) if visitas else 0,
            "compartilhamentos": total_share,
            "cadastros_email": cadastros,
        },
        "por_dia": por_dia,
        "regioes": [
            {"estado": e, "pais": p, "visitas": v}
            for (e, p), v in sorted(estados.items(), key=lambda kv: (-kv[1], kv[0][0]))[:15]
        ],
        "cidades": [
            {"cidade": c, "estado": e, "visitas": v}
            for (c, e), v in sorted(cidades.items(), key=lambda kv: (-kv[1], kv[0][0]))[:15]
        ],
        "paises": _top(paises, "pais", "visitas"),
        "origens": _top(origens, "origem", "visitas"),
        "dispositivos": _top(aparelhos, "aparelho", "visitas"),
        "secoes": [
            {"secao": s, "visitas": len(secao_sessoes[s]), "tempo_medio_s": round(ms / 1000 / len(secao_sessoes[s]))}
            for s, ms in sorted(secao_ms.items(), key=lambda kv: -kv[1])
        ],
        "visoes": _top(visoes, "visao", "trocas"),
        "politicos": politicos[:30],
        "compartilhamentos": sorted(
            (
                {"meio": m, "total": c["do_caso"] + c["de_cards"], "do_caso": c["do_caso"], "de_cards": c["de_cards"]}
                for m, c in meios.items()
            ),
            key=lambda x: -x["total"],
        ),
        "outros": {"reduzir_animacoes": outros["reduzir_animacoes"], "voltar_ao_topo": outros["voltar_ao_topo"]},
    }
