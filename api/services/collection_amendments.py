"""Emendas parlamentares dos membros de uma coleção, com linha de base (CS-135).

Para cada parlamentar: empenhado e pago por ano, quanto foi por transferência
especial (a "emenda Pix", que vai direto para o caixa do ente sem convênio nem
objeto definido) e os principais destinos e áreas.

Número solto não diz nada, então a coleção também recebe a LINHA DE BASE: a
mediana de todos os parlamentares com emenda no mesmo período. A tela compara a
lista com ela; nunca apresenta a emenda como irregular.

Recorte: os mesmos 4 anos da cota. A fonte (Portal da Transparência) tem um
defeito de escala que o coletor já trata; 2022 ainda tem resíduo, por isso o
recorte começa depois.
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

try:
    from ..db.models.parliamentarian import Parliamentarian
    from ..db.models.parliamentary_amendment import ParliamentaryAmendment
    from .editorial_agendas import tabelas_disponiveis
except ImportError:  # execução dentro de api/
    from db.models.parliamentarian import Parliamentarian
    from db.models.parliamentary_amendment import ParliamentaryAmendment
    from services.editorial_agendas import tabelas_disponiveis

ANOS = 4
TOP = 3
# Destinos que não dizem para onde o dinheiro foi.
DESTINOS_VAGOS = {"MÚLTIPLO", "MULTIPLO", "Nacional", "NACIONAL"}
_CACHE_BASE_SEGUNDOS = 6 * 3600
_cache_base: dict[int, tuple[float, Optional[dict[str, Any]]]] = {}


def _ano_corte() -> int:
    return datetime.now(timezone.utc).year - ANOS + 1


def _e_pix(tipo: Optional[str]) -> bool:
    return "especia" in (tipo or "").lower()


def _f(valor: Any) -> float:
    return float(valor or 0)


def resumo_emendas(db: Session, parl_ids: set[int]) -> dict[int, dict[str, Any]]:
    """Por parlamentar: série anual, fatia Pix, destinos e áreas principais."""

    if not parl_ids or not tabelas_disponiveis(db, "parliamentary_amendment"):
        return {}
    pa = ParliamentaryAmendment
    filtro = (pa.parliamentarian_id.in_(parl_ids), pa.year >= _ano_corte())
    pix = func.lower(pa.amendment_type).like("%especia%")

    por_ano = db.execute(
        select(
            pa.parliamentarian_id, pa.year, func.count(),
            func.sum(pa.committed_value), func.sum(pa.paid_value),
            func.sum(pa.committed_value).filter(pix),
        ).where(*filtro).group_by(pa.parliamentarian_id, pa.year)
    ).all()
    saida: dict[int, dict[str, Any]] = defaultdict(lambda: {"by_year": [], "destinations": [], "areas": []})
    for parl_id, ano, n, empenhado, pago, empenhado_pix in por_ano:
        saida[int(parl_id)]["by_year"].append(
            {"year": int(ano), "count": int(n), "committed": _f(empenhado), "paid": _f(pago), "special": _f(empenhado_pix)}
        )

    for coluna, chave in ((pa.spending_locality, "destinations"), (pa.function, "areas")):
        linhas = db.execute(
            select(pa.parliamentarian_id, coluna, func.sum(pa.committed_value))
            .where(*filtro, coluna.isnot(None))
            .group_by(pa.parliamentarian_id, coluna)
        ).all()
        por_parl: dict[int, list[tuple[str, float]]] = defaultdict(list)
        for parl_id, nome, valor in linhas:
            if chave == "destinations" and nome in DESTINOS_VAGOS:
                continue
            por_parl[int(parl_id)].append((nome, _f(valor)))
        for parl_id, itens in por_parl.items():
            if parl_id in saida:
                itens.sort(key=lambda x: -x[1])
                saida[parl_id][chave] = [{"name": n, "committed": v} for n, v in itens[:TOP]]

    for resumo in saida.values():
        resumo["by_year"].sort(key=lambda a: a["year"])
        total = sum(a["committed"] for a in resumo["by_year"])
        resumo["committed"] = total
        resumo["paid"] = sum(a["paid"] for a in resumo["by_year"])
        resumo["special_share"] = (sum(a["special"] for a in resumo["by_year"]) / total) if total else None
    return dict(saida)


def _mediana(linhas: list[tuple[float, float]]) -> Optional[dict[str, Any]]:
    """linhas = (empenhado, empenhado Pix) por parlamentar."""
    validas = [(t, p) for t, p in linhas if t > 0]
    if not validas:
        return None
    return {
        "parliamentarians": len(validas),
        "median_committed": statistics.median(t for t, _ in validas),
        "median_special_share": statistics.median(p / t for t, p in validas),
        "special_share_all": sum(p for _, p in validas) / sum(t for t, _ in validas),
    }


def linha_de_base(db: Session) -> Optional[dict[str, Any]]:
    """Mediana entre todos os parlamentares com emenda no recorte, separada por casa.

    Deputado e senador têm limites de emenda individual diferentes: comparar a
    lista com a mediana geral faria senador parecer fora da curva só por ser senador.
    """

    corte = _ano_corte()
    guardado = _cache_base.get(corte)
    if guardado and time.monotonic() - guardado[0] < _CACHE_BASE_SEGUNDOS:
        return guardado[1]
    if not tabelas_disponiveis(db, "parliamentary_amendment"):
        return None
    pa = ParliamentaryAmendment
    pix = func.lower(pa.amendment_type).like("%especia%")
    linhas = db.execute(
        select(Parliamentarian.type, func.sum(pa.committed_value), func.sum(pa.committed_value).filter(pix))
        .join(Parliamentarian, Parliamentarian.id == pa.parliamentarian_id)
        .where(pa.year >= corte)
        .group_by(pa.parliamentarian_id, Parliamentarian.type)
    ).all()
    por_tipo: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for tipo, total, total_pix in linhas:
        por_tipo[(tipo or "").strip() or "Outro"].append((_f(total), _f(total_pix)))
    geral = _mediana([x for lista in por_tipo.values() for x in lista])
    base = (
        {"since_year": corte, **geral, "by_type": {t: _mediana(l) for t, l in por_tipo.items()}}
        if geral
        else None
    )
    _cache_base[corte] = (time.monotonic(), base)
    return base
