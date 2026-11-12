"""Acessos à plataforma no admin (CS-87): quem está usando agora, de onde e em que horário.

Fontes:

- Membros logados
  - `member_presence`: sinal de presença que a UI manda a cada 2 min com a aba
    visível. Dá o "ativos agora" sem perder quem lê uma página parada.
  - `member_presence_hour`: membro x hora cheia com sinal. Dá o quadro de horários.
  - `usage_events`: page_view e section_view, agora com a localidade estimada.
    Dá o mapa do período e completa o quadro de horários para trás (antes da
    CS-87 só havia page_view).
- Visitantes anônimos das páginas públicas: `collection_event` (Na Lupa, CS-145)
  e `page_event` (Eleições 2026, CS-166). Cada página tem o seu id de visitante,
  então quem lê as duas conta duas vezes; é o mesmo limite dos painéis delas.

Localidade = país, estado, cidade e o centro da cidade, estimados pelo IP na
gravação (base DB-IP Lite). O IP não é gravado. Depois de 90 dias a localidade
ligada ao e-mail é apagada (`limpar_localidades_antigas`), sem cron: roda no
máximo uma vez por dia, puxada pelo próprio sinal de presença.

As consultas trazem as linhas cruas e agregam em Python, como os painéis da
Na Lupa e de Eleições 2026: o volume é pequeno (cerca de 12 mil page_views em
30 dias, em nov/2026).
"""

from __future__ import annotations

import threading
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

try:
    from . import collection_events as ev
except ImportError:  # execução dentro de api/
    from services import collection_events as ev

JANELA_ATIVOS_MIN = 15
RETENCAO_DIAS = 90
PERIODOS_DIAS = (1, 7, 30, 90)
DIAS_SEMANA = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")

# Nome do estado como vem da DB-IP (em pt-BR quase sempre; o DF vem em inglês)
# -> sigla. Chave sem acento e minúscula.
_SIGLAS = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA",
    "ceara": "CE", "distrito federal": "DF", "federal district": "DF",
    "espirito santo": "ES", "goias": "GO", "maranhao": "MA", "mato grosso": "MT",
    "mato grosso do sul": "MS", "minas gerais": "MG", "para": "PA", "paraiba": "PB",
    "parana": "PR", "pernambuco": "PE", "piaui": "PI", "rio de janeiro": "RJ",
    "rio grande do norte": "RN", "rio grande do sul": "RS", "rondonia": "RO",
    "roraima": "RR", "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE",
    "tocantins": "TO",
}


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def uf_do_estado(estado: Optional[str], pais: Optional[str] = "BR") -> Optional[str]:
    """Sigla da UF a partir do nome do estado da base de IP; None fora do Brasil ou se não reconhecer."""
    if not estado or (pais and pais != "BR"):
        return None
    chave = " ".join(_sem_acento(estado).lower().replace("state of ", "").split())
    return _SIGLAS.get(chave)


def _quando(valor: Any) -> datetime:
    if isinstance(valor, str):  # SQLite devolve texto
        valor = datetime.fromisoformat(valor)
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor


def _hora_cheia(agora: datetime) -> datetime:
    return agora.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------
# gravação
# --------------------------------------------------------------------------


def registrar_presenca(
    db: Session,
    *,
    email: str,
    projeto_id: Optional[int],
    regiao: Optional[tuple],
    agora: Optional[datetime] = None,
) -> None:
    """Atualiza o último sinal do membro e marca a hora cheia em que ele estava."""
    agora = agora or datetime.now(timezone.utc)
    regiao = regiao or (None, None, None)
    lat, lon = ev.coordenadas(regiao)
    db.execute(
        text(
            "INSERT INTO member_presence (email, projeto_id, last_seen_at, country, region, city, lat, lon) "
            "VALUES (:e, :p, :t, :pais, :estado, :cidade, :lat, :lon) "
            "ON CONFLICT (email) DO UPDATE SET projeto_id = excluded.projeto_id, "
            "last_seen_at = excluded.last_seen_at, country = excluded.country, region = excluded.region, "
            "city = excluded.city, lat = excluded.lat, lon = excluded.lon"
        ),
        {
            "e": email, "p": projeto_id, "t": agora, "pais": regiao[0], "estado": regiao[1],
            "cidade": regiao[2], "lat": lat, "lon": lon,
        },
    )
    db.execute(
        text("INSERT INTO member_presence_hour (hora, email) VALUES (:h, :e) ON CONFLICT DO NOTHING"),
        {"h": _hora_cheia(agora), "e": email},
    )
    db.commit()


# --------------------------------------------------------------------------
# retenção
# --------------------------------------------------------------------------

_ultima_limpeza: Optional[datetime] = None
_trava_limpeza = threading.Lock()


def limpar_localidades_antigas(db: Session, agora: Optional[datetime] = None) -> dict[str, int]:
    """Apaga a localidade ligada ao e-mail com mais de RETENCAO_DIAS.

    - `usage_events`: o evento fica (é o histórico de uso), só sem localidade.
    - `member_presence` e `member_presence_hour`: a linha sai.

    Os eventos anônimos das páginas públicas não têm e-mail e seguem a regra
    deles (CS-145), por isso não entram aqui.
    """
    agora = agora or datetime.now(timezone.utc)
    corte = agora - timedelta(days=RETENCAO_DIAS)
    eventos = db.execute(
        text(
            "UPDATE usage_events SET country = NULL, region = NULL, city = NULL, lat = NULL, lon = NULL "
            "WHERE created_at < :c AND (country IS NOT NULL OR region IS NOT NULL OR city IS NOT NULL "
            "OR lat IS NOT NULL)"
        ),
        {"c": corte},
    ).rowcount
    presencas = db.execute(text("DELETE FROM member_presence WHERE last_seen_at < :c"), {"c": corte}).rowcount
    horas = db.execute(text("DELETE FROM member_presence_hour WHERE hora < :c"), {"c": corte}).rowcount
    db.commit()
    return {"eventos": eventos or 0, "presencas": presencas or 0, "horas": horas or 0}


def limpar_se_preciso(db: Session, agora: Optional[datetime] = None) -> bool:
    """Roda a limpeza no máximo uma vez a cada 24 h por processo. Nunca derruba quem chamou."""
    global _ultima_limpeza
    agora = agora or datetime.now(timezone.utc)
    with _trava_limpeza:
        if _ultima_limpeza and agora - _ultima_limpeza < timedelta(hours=24):
            return False
        _ultima_limpeza = agora
    try:
        limpar_localidades_antigas(db, agora)
    except Exception:  # pragma: no cover - a limpeza é de manutenção, o sinal não pode falhar por ela
        db.rollback()
        ev.logger.exception("Falha ao apagar localidades antigas (CS-87).")
    return True


# --------------------------------------------------------------------------
# painel
# --------------------------------------------------------------------------


def _anonimos(db: Session, desde: datetime) -> list[tuple]:
    """(quando, visitante, país, estado, cidade, lat, lon) das duas páginas públicas."""
    linhas: list[tuple] = []
    for tabela in ("collection_event", "page_event"):
        linhas += db.execute(
            text(
                f"SELECT created_at, visitor_id, country, region, city, lat, lon FROM {tabela} "
                "WHERE created_at >= :d"
            ),
            {"d": desde},
        ).all()
    return linhas


def ativos_agora(db: Session, minutos: int = JANELA_ATIVOS_MIN, agora: Optional[datetime] = None) -> dict[str, Any]:
    agora = agora or datetime.now(timezone.utc)
    desde = agora - timedelta(minutes=minutos)
    membros = db.execute(
        text("SELECT email, country, region FROM member_presence WHERE last_seen_at >= :d"),
        {"d": desde},
    ).all()
    visitantes: dict[str, tuple] = {}
    for _quando_ev, visitante, pais, estado, *_ in _anonimos(db, desde):
        visitantes[visitante] = (pais, estado)

    por_uf: dict[str, dict[str, int]] = defaultdict(lambda: {"membros": 0, "visitantes": 0})
    for _email, pais, estado in membros:
        por_uf[uf_do_estado(estado, pais) or ("exterior" if pais and pais != "BR" else "?")]["membros"] += 1
    for pais, estado in visitantes.values():
        por_uf[uf_do_estado(estado, pais) or ("exterior" if pais and pais != "BR" else "?")]["visitantes"] += 1

    return {
        "minutos": minutos,
        "atualizado_em": agora.isoformat(),
        "membros": len(membros),
        "visitantes": len(visitantes),
        "por_uf": sorted(
            ({"uf": uf, **v} for uf, v in por_uf.items()),
            key=lambda x: (-(x["membros"] + x["visitantes"]), x["uf"]),
        ),
    }


def _soma_local(
    acumulado: dict,
    chave: tuple,
    quem: str,
    lat: Optional[float],
    lon: Optional[float],
) -> None:
    item = acumulado[chave]
    item["quem"].add(quem)
    if lat is not None and lon is not None:
        item["lat"].append(float(lat))
        item["lon"].append(float(lon))


def _novo_local() -> dict[str, Any]:
    return {"quem": set(), "lat": [], "lon": []}


def mapa_do_periodo(db: Session, dias: int, agora: Optional[datetime] = None) -> dict[str, Any]:
    """Pessoas distintas por UF, cidade e país no período (membros e visitantes separados)."""
    agora = agora or datetime.now(timezone.utc)
    desde = agora - timedelta(days=dias)
    membros = db.execute(
        text(
            "SELECT email, country, region, city, lat, lon FROM usage_events "
            "WHERE created_at >= :d AND email IS NOT NULL AND event_type IN ('page_view', 'section_view')"
        ),
        {"d": desde},
    ).all()
    anonimos = [(v, p, e, c, la, lo) for _q, v, p, e, c, la, lo in _anonimos(db, desde)]

    resultado: dict[str, Any] = {}
    for publico, linhas in (("membros", membros), ("visitantes", anonimos)):
        ufs: dict[tuple, dict] = defaultdict(_novo_local)
        cidades: dict[tuple, dict] = defaultdict(_novo_local)
        paises: dict[tuple, dict] = defaultdict(_novo_local)
        com_local: set[str] = set()
        todos: set[str] = set()
        for quem, pais, estado, cidade, lat, lon in linhas:
            todos.add(quem)
            if not pais and not estado:
                continue
            com_local.add(quem)
            uf = uf_do_estado(estado, pais)
            if uf:
                _soma_local(ufs, (uf,), quem, None, None)
                if cidade:
                    _soma_local(cidades, (uf, cidade), quem, lat, lon)
            elif pais and pais != "BR":
                _soma_local(paises, (pais,), quem, None, None)
        resultado[publico] = {
            "pessoas": len(todos),
            "sem_localidade": len(todos - com_local),
            "ufs": {k[0]: len(v["quem"]) for k, v in ufs.items()},
            "cidades": sorted(
                (
                    {
                        "uf": k[0],
                        "cidade": k[1],
                        "pessoas": len(v["quem"]),
                        "lat": round(sum(v["lat"]) / len(v["lat"]), 2) if v["lat"] else None,
                        "lon": round(sum(v["lon"]) / len(v["lon"]), 2) if v["lon"] else None,
                    }
                    for k, v in cidades.items()
                ),
                key=lambda x: (-x["pessoas"], x["uf"], x["cidade"]),
            ),
            "exterior": sorted(
                ({"pais": k[0], "pessoas": len(v["quem"])} for k, v in paises.items()),
                key=lambda x: (-x["pessoas"], x["pais"]),
            ),
        }
    return resultado


def quadro_de_horarios(db: Session, dias: int, agora: Optional[datetime] = None) -> dict[str, Any]:
    """Média de membros distintos por dia da semana x hora (horário de Brasília).

    Cada célula = membros distintos naquela hora, na média das semanas do
    período (as ocorrências sem ninguém contam como zero). Junta o sinal de
    presença com os page_views, que existem desde antes da CS-87.
    """
    agora = agora or datetime.now(timezone.utc)
    # Exatamente dias x 24 horas cheias, terminando na hora atual (cada célula
    # aparece `dias / 7` vezes, sem contar a mesma hora da semana duas vezes).
    desde = _hora_cheia(agora) - timedelta(days=dias) + timedelta(hours=1)
    pares: set[tuple[datetime, str]] = set()
    for hora, email in db.execute(
        text("SELECT hora, email FROM member_presence_hour WHERE hora >= :d"), {"d": desde}
    ).all():
        pares.add((_hora_cheia(_quando(hora)), email))
    for criado, email in db.execute(
        text(
            "SELECT created_at, email FROM usage_events WHERE created_at >= :d AND email IS NOT NULL "
            "AND event_type IN ('page_view', 'section_view')"
        ),
        {"d": desde},
    ).all():
        pares.add((_hora_cheia(_quando(criado)), email))

    pessoas_por_hora: dict[datetime, int] = defaultdict(int)
    for hora, _email in pares:
        pessoas_por_hora[hora] += 1

    soma = [[0] * 24 for _ in range(7)]
    ocorrencias = [[0] * 24 for _ in range(7)]
    hora = desde
    fim = _hora_cheia(agora)
    while hora <= fim:
        local = hora.astimezone(ev.FUSO)
        soma[local.weekday()][local.hour] += pessoas_por_hora.get(hora, 0)
        ocorrencias[local.weekday()][local.hour] += 1
        hora += timedelta(hours=1)

    media = [
        [round(soma[d][h] / ocorrencias[d][h], 2) if ocorrencias[d][h] else 0.0 for h in range(24)]
        for d in range(7)
    ]
    celulas = [(media[d][h], d, h) for d in range(7) for h in range(24) if ocorrencias[d][h]]
    calmos = sorted(celulas)[:5]
    return {
        "dias": DIAS_SEMANA,
        "media": media,
        "mais_calmos": [{"dia": DIAS_SEMANA[d], "hora": h, "media": m} for m, d, h in calmos],
        "pico": max((m for m, _d, _h in celulas), default=0.0),
    }


def painel(db: Session, dias: int, minutos: int = JANELA_ATIVOS_MIN, agora: Optional[datetime] = None) -> dict[str, Any]:
    if dias not in PERIODOS_DIAS:
        dias = 7
    minutos = max(5, min(60, minutos))
    agora = agora or datetime.now(timezone.utc)
    return {
        "dias": dias,
        "ativos": ativos_agora(db, minutos, agora),
        "mapa": mapa_do_periodo(db, dias, agora),
        "horarios": quadro_de_horarios(db, dias, agora),
        "retencao_dias": RETENCAO_DIAS,
    }


def siglas() -> Iterable[str]:
    return sorted(set(_SIGLAS.values()))
