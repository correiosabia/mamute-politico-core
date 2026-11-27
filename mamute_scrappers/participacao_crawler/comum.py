"""Peças comuns da coleta de participação (CS-179).

Cada fonte vira linhas de `participation_item` com a mesma forma. A coleta só
alimenta a FILA DE CURADORIA: item novo nasce `pendente` e nada aparece para o
leitor sem um admin aprovar. Regravar uma fonte nunca mexe no que a curadoria
decidiu (status, tema, nota).

Regras de segurança:
1. Fonte que volta vazia não fecha nada. HTML que mudou de formato costuma dar
   zero item; fechar tudo nessa hora esvaziaria a página. Só registra o aviso.
2. Fechar por ausência só vale para fonte cuja listagem é completa (o Brasil
   Participativo devolve todos os processos). Senado e Câmara listam um recorte
   (os mais votados), então sair da lista não quer dizer que encerrou.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import date
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

TIMEOUT = 60
# Uma requisição por segundo por fonte, no máximo.
PAUSA = 1.0

MESES = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}

_TAGS = re.compile(r"<[^>]+>")
_ESPACOS = re.compile(r"\s+")


def limpa(texto: Optional[str]) -> str:
    return _ESPACOS.sub(" ", html.unescape(_TAGS.sub(" ", texto or "")).replace("\xa0", " ")).strip()


def resumo(texto: Optional[str], limite: int = 400) -> Optional[str]:
    t = limpa(texto)
    if not t:
        return None
    if len(t) <= limite:
        return t
    corte = t[:limite].rsplit(" ", 1)[0]
    return f"{corte}…"


def numero(texto: Optional[str]) -> Optional[int]:
    """'1.280.708' -> 1280708; '59.807 apoios' -> 59807."""
    m = re.search(r"\d[\d.]*", texto or "")
    return int(m.group(0).replace(".", "")) if m else None


def data_por_extenso(texto: Optional[str]) -> Optional[date]:
    """'Apoios abertos até 25 de fevereiro de 2027' -> date(2027, 2, 25)."""
    m = re.search(r"(\d{1,2})\s+de\s+([a-zç]+)\s+de\s+(\d{4})", (texto or "").lower())
    if not m or m.group(2) not in MESES:
        return None
    return date(int(m.group(3)), MESES[m.group(2)], int(m.group(1)))


def baixar(url: str, *, metodo: str = "GET", **kw) -> Any:
    """GET/POST com a impressão TLS de um navegador.

    O Brasil Participativo derruba o TLS do Python puro (EOF no handshake), o
    mesmo bloqueio do TSE (CS-126); o curl_cffi passa. Usamos para todas as
    fontes, para o coletor não depender de qual servidor resolveu filtrar.
    """
    from curl_cffi import requests as cffi

    resp = cffi.request(metodo, url, impersonate="chrome", timeout=TIMEOUT, **kw)
    resp.raise_for_status()
    return resp


def item(
    *,
    source: str,
    external_id: str,
    kind: str,
    title: str,
    url: str,
    summary: Optional[str] = None,
    reference: Optional[str] = None,
    organization: Optional[str] = None,
    opens_on: Optional[date] = None,
    closes_on: Optional[date] = None,
    score: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "external_id": str(external_id),
        "kind": kind,
        "title": title,
        "url": url,
        "summary": summary,
        "reference": reference,
        "organization": organization,
        "opens_on": opens_on,
        "closes_on": closes_on,
        "score": score,
    }


_UPSERT = """
INSERT INTO participation_item (source, external_id, kind, title, summary, reference, organization,
    url, opens_on, closes_on, score, score_updated_at, source_open, first_seen_at, last_seen_at)
VALUES (:source, :external_id, :kind, :title, :summary, :reference, :organization,
    :url, :opens_on, :closes_on, CAST(:score AS JSONB), CASE WHEN :score IS NULL THEN NULL ELSE now() END,
    TRUE, now(), now())
ON CONFLICT (source, external_id) DO UPDATE SET
    kind = EXCLUDED.kind,
    title = EXCLUDED.title,
    summary = EXCLUDED.summary,
    reference = EXCLUDED.reference,
    organization = EXCLUDED.organization,
    url = EXCLUDED.url,
    opens_on = EXCLUDED.opens_on,
    closes_on = EXCLUDED.closes_on,
    score = COALESCE(EXCLUDED.score, participation_item.score),
    score_updated_at = COALESCE(EXCLUDED.score_updated_at, participation_item.score_updated_at),
    source_open = TRUE,
    closed_at = NULL,
    last_seen_at = now()
"""


def salvar(
    session: Any,
    source: str,
    itens: list[dict[str, Any]],
    *,
    vistos_na_fonte: Optional[Iterable[str]] = None,
) -> dict[str, int]:
    """Grava os itens da fonte. `vistos_na_fonte` (listagem completa) fecha os ausentes."""
    from sqlalchemy import text

    if not itens and not vistos_na_fonte:
        logger.warning("participacao: fonte %s voltou vazia; nada foi fechado", source)
        return {"gravados": 0, "fechados": 0}

    for linha in itens:
        params = dict(linha)
        params["score"] = json.dumps(linha["score"], ensure_ascii=False) if linha.get("score") else None
        session.execute(text(_UPSERT), params)

    fechados = 0
    vistos = sorted({str(v) for v in (vistos_na_fonte or [])})
    if vistos:
        abertos = session.execute(
            text("SELECT id, external_id FROM participation_item WHERE source = :s AND source_open"),
            {"s": source},
        ).all()
        ausentes = [i for i, ext in abertos if ext not in set(vistos)]
        if ausentes:
            session.execute(
                text(
                    "UPDATE participation_item SET source_open = FALSE, closed_at = now() "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": ausentes},
            )
            fechados = len(ausentes)
    return {"gravados": len(itens), "fechados": fechados}


def aprovados_abertos(session: Any, source: str) -> list[str]:
    """external_id dos itens aprovados e ainda abertos de uma fonte (para atualizar placar)."""
    from sqlalchemy import text

    return [
        r[0]
        for r in session.execute(
            text(
                "SELECT external_id FROM participation_item "
                "WHERE source = :s AND status = 'aprovado' AND source_open"
            ),
            {"s": source},
        )
    ]


def atualizar_placar(session: Any, source: str, external_id: str, score: dict[str, Any]) -> None:
    from sqlalchemy import text

    session.execute(
        text(
            "UPDATE participation_item SET score = CAST(:score AS JSONB), score_updated_at = now(), "
            "last_seen_at = now() WHERE source = :s AND external_id = :e"
        ),
        {"score": json.dumps(score, ensure_ascii=False), "s": source, "e": str(external_id)},
    )


def pausa(esperar=time.sleep) -> None:
    esperar(PAUSA)
