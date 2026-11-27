"""Brasil Participativo: consultas e participações do governo federal (CS-179).

Fonte: a API GraphQL pública da plataforma (Decidim), sem chave:
POST https://brasilparticipativo.presidencia.gov.br/api
Uma consulta traz TODOS os processos (~600), com data de início e de fim. Por
ser a listagem completa, processo que some da resposta é dado como encerrado.

Pré-filtro antes da curadoria (medido em nov/2026, ~88 abertos):
- fim nos próximos 120 dias (processo "guarda-chuva" de agência termina em 2035
  ou 2060 e não é missão do dia);
- fora eleições de conselho, seleções e editais, que não são participação do
  cidadão comum.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Optional

from mamute_scrappers.participacao_crawler.comum import baixar, item, limpa, resumo

SOURCE = "brasil_participativo"
API_URL = "https://brasilparticipativo.presidencia.gov.br/api"
SITE = "https://brasilparticipativo.presidencia.gov.br/processes/"
ORGANIZACAO = "Governo federal"
JANELA_DIAS = 120
_FORA = re.compile(r"\b(elei[çc][ãaõo]|eleitora|sele[çc][ãaõo]|edita(l|is)\b|credenciamento)", re.IGNORECASE)

CONSULTA = (
    '{ participatoryProcesses { id slug title { translation(locale:"pt-BR") } '
    'shortDescription { translation(locale:"pt-BR") } startDate endDate } }'
)


def _data(texto: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(str(texto)[:10]) if texto else None
    except ValueError:
        return None


def _traduz(campo: Any) -> str:
    return limpa((campo or {}).get("translation")) if isinstance(campo, dict) else ""


def build_items(dados: dict[str, Any], hoje: date) -> tuple[list[dict[str, Any]], list[str]]:
    """(itens que passam no pré-filtro, ids de todos os processos da resposta)."""
    processos = ((dados or {}).get("data") or {}).get("participatoryProcesses") or []
    todos = [str(p.get("id")) for p in processos if p.get("id") is not None]
    itens = []
    for p in processos:
        fim, titulo = _data(p.get("endDate")), _traduz(p.get("title"))
        if not titulo or not p.get("slug") or fim is None:
            continue
        if fim < hoje or fim > hoje + timedelta(days=JANELA_DIAS):
            continue
        if _FORA.search(titulo) or _FORA.search(str(p.get("slug"))):
            continue
        itens.append(
            item(
                source=SOURCE,
                external_id=str(p["id"]),
                kind="consulta",
                title=titulo,
                summary=resumo(_traduz(p.get("shortDescription"))),
                organization=ORGANIZACAO,
                url=SITE + str(p["slug"]),
                opens_on=_data(p.get("startDate")),
                closes_on=fim,
            )
        )
    return itens, todos


def fetch() -> dict[str, Any]:
    return baixar(API_URL, metodo="POST", json={"query": CONSULTA}).json()


def collect(session: Any, hoje: date) -> dict[str, int]:
    from mamute_scrappers.participacao_crawler.comum import salvar

    itens, todos = build_items(fetch(), hoje)
    return salvar(session, SOURCE, itens, vistos_na_fonte=todos)
