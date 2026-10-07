"""Busca na agenda oficial de autoridades por termo (CS-135).

Quem é recebido por uma autoridade (empresário, advogado, dirigente de banco)
não tem cadastro na base: a agenda é o único registro público de onde e quando
essa pessoa esteve. Por isso a busca é por texto e quem escolhe os termos é o
admin da coleção (`settings.agenda_terms`).

A mesma reunião costuma sair na agenda de várias autoridades (presidente e três
diretores no mesmo horário). A busca junta essas linhas num compromisso só, com
a lista de quem participou do lado do órgão.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

try:
    from ..db.models.official_agenda import OfficialAgendaItem
    from .editorial_agendas import tabelas_disponiveis
except ImportError:  # execução dentro de api/
    from db.models.official_agenda import OfficialAgendaItem
    from services.editorial_agendas import tabelas_disponiveis

TABELA = "official_agenda_item"
LIMITE = 500
MAX_TERMOS = 10

FONTES = {
    "bcb": {
        "label": "Agenda da diretoria do Banco Central",
        "url": "https://www.bcb.gov.br/acessoinformacao/agendadiretoria",
    },
}


class AgendaError(ValueError):
    """Erro de entrada (termo curto, termos demais)."""


def termos_validos(termos: list[str]) -> list[str]:
    limpos: list[str] = []
    for t in termos or []:
        t = " ".join(str(t or "").split())
        if t and t.lower() not in {x.lower() for x in limpos}:
            limpos.append(t)
    if any(len(t) < 4 for t in limpos):
        raise AgendaError("Cada termo precisa de pelo menos 4 letras.")
    if len(limpos) > MAX_TERMOS:
        raise AgendaError(f"Use no máximo {MAX_TERMOS} termos.")
    return limpos


def search_agenda(
    db: Session, termos: list[str], *, desde: Optional[date] = None, limite: int = LIMITE
) -> dict[str, Any]:
    """Compromissos que citam algum dos termos, do mais recente para o mais antigo."""

    termos = termos_validos(termos)
    vazio: dict[str, Any] = {"terms": termos, "items": [], "sources": []}
    if not termos or not tabelas_disponiveis(db, TABELA):
        return vazio

    stmt = select(OfficialAgendaItem).where(
        or_(*[OfficialAgendaItem.description.ilike(f"%{t}%") for t in termos])
    )
    if desde is not None:
        stmt = stmt.where(OfficialAgendaItem.event_date >= desde)
    linhas = db.execute(
        stmt.order_by(
            OfficialAgendaItem.event_date.desc(),
            OfficialAgendaItem.starts_at,
            OfficialAgendaItem.office,
        ).limit(max(1, min(int(limite), 2000)))
    ).scalars()

    # Mesma fonte, mesmo dia, mesmo horário e mesma modalidade = mesma reunião.
    grupos: "OrderedDict[tuple[Any, ...], dict[str, Any]]" = OrderedDict()
    fontes: set[str] = set()
    for l in linhas:
        fontes.add(l.source)
        chave = (l.source, l.event_date, l.starts_at or f"seq{l.seq}:{l.authority_id}", bool(l.remote))
        item = grupos.get(chave)
        if item is None:
            item = grupos[chave] = {
                "source": l.source,
                "date": l.event_date.isoformat(),
                "starts_at": l.starts_at,
                "ends_at": l.ends_at,
                "description": l.description,
                "place": l.place,
                "remote": bool(l.remote),
                "url": l.url,
                "matched": [t for t in termos if t.lower() in l.description.lower()],
                "authorities": [],
            }
        item["authorities"].append(
            {"name": l.authority_name, "office": l.office, "office_label": l.office_label}
        )
        if not item["place"] and l.place:
            item["place"] = l.place

    vazio["items"] = list(grupos.values())
    vazio["sources"] = [{"source": f, **FONTES.get(f, {"label": f, "url": None})} for f in sorted(fontes)]
    return vazio
