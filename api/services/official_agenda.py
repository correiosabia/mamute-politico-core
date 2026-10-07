"""Busca na agenda oficial de autoridades por termo (CS-135).

Quem é recebido por uma autoridade (empresário, advogado, dirigente de banco)
não tem cadastro na base: a agenda é o único registro público de onde e quando
essa pessoa esteve. Por isso a busca é por texto e quem escolhe os termos é o
admin da coleção (`settings.agenda_terms`).

A mesma reunião costuma sair na agenda de várias autoridades (presidente e três
diretores no mesmo horário) e, no BC, nas duas fontes (agenda da diretoria e
e-Agendas). A busca junta essas linhas num compromisso só (mesmo dia, horário,
modalidade e órgão), com a lista de quem participou do lado do órgão, sem nome
repetido.

O termo pode estar só na lista de participantes (o assunto diz "Programa habitacional" e
o banco procurado está entre os convidados). Nesse caso a resposta traz o trecho dos
participantes onde o termo aparece.
"""

from __future__ import annotations

import re
import unicodedata
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
    "eagendas": {
        "label": "e-Agendas da CGU (Executivo federal)",
        "url": "https://eagendas.cgu.gov.br/",
    },
}
# Caracteres de cada lado do termo no trecho dos participantes.
TRECHO = 90


def _norm(texto: Optional[str]) -> str:
    sem = "".join(c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", sem.lower()).strip()


def _trecho(texto: str, termos: list[str]) -> Optional[str]:
    """Pedaço dos participantes em volta do primeiro termo achado (sem CPF mascarado)."""
    limpo = re.sub(r"\s*\(CPF:[^)]*\)", "", texto)
    alvo = _norm(limpo)
    for t in termos:
        i = alvo.find(_norm(t))
        if i >= 0:
            ini, fim = max(0, i - TRECHO), min(len(limpo), i + len(t) + TRECHO)
            return ("…" if ini else "") + limpo[ini:fim].strip() + ("…" if fim < len(limpo) else "")
    return None


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
        or_(
            *[OfficialAgendaItem.description.ilike(f"%{t}%") for t in termos],
            *[OfficialAgendaItem.participants.ilike(f"%{t}%") for t in termos],
        )
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

    # Mesmo dia, horário, modalidade e órgão = mesma reunião, venha de que fonte vier.
    grupos: "OrderedDict[tuple[Any, ...], dict[str, Any]]" = OrderedDict()
    nomes: dict[tuple[Any, ...], set[str]] = {}
    fontes: set[str] = set()
    for l in linhas:
        fontes.add(l.source)
        orgao = l.organization or FONTES.get(l.source, {}).get("label")
        chave = (l.event_date, l.starts_at or f"seq{l.seq}:{l.authority_id}", bool(l.remote), _norm(orgao))
        desc = _norm(l.description)
        item = grupos.get(chave)
        if item is None:
            item = grupos[chave] = {
                "source": l.source,
                "sources": [],
                "date": l.event_date.isoformat(),
                "starts_at": l.starts_at,
                "ends_at": l.ends_at,
                "description": l.description,
                "organization": orgao,
                "place": l.place,
                "remote": bool(l.remote),
                "url": l.url,
                "matched": [],
                "participants_excerpt": None,
                "authorities": [],
            }
            nomes[chave] = set()
        if l.source not in item["sources"]:
            item["sources"].append(l.source)
        for t in termos:
            if t not in item["matched"] and (_norm(t) in desc or _norm(t) in _norm(l.participants)):
                item["matched"].append(t)
        # Termo só nos participantes: o trecho explica por que o compromisso entrou.
        if not any(_norm(t) in desc for t in termos) and l.participants and not item["participants_excerpt"]:
            item["participants_excerpt"] = _trecho(l.participants, termos)
        nome = _norm(l.authority_name)
        if nome and nome in nomes[chave]:
            continue
        nomes[chave].add(nome)
        item["authorities"].append(
            {"name": l.authority_name, "office": l.office, "office_label": l.office_label}
        )
        if not item["place"] and l.place:
            item["place"] = l.place

    vazio["items"] = list(grupos.values())
    vazio["sources"] = [{"source": f, **FONTES.get(f, {"label": f, "url": None})} for f in sorted(fontes)]
    return vazio
