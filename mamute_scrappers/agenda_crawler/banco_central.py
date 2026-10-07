"""Agenda da diretoria do Banco Central (CS-135).

Fonte: API do site do BC, aberta e sem chave,
https://www.bcb.gov.br/api/servico/sitebcb/agendadiretoria?lista=Agenda%20da%20Diretoria&inicioAgenda='AAAA-MM-DD'&fimAgenda='AAAA-MM-DD'
Cada item da resposta e o dia de uma autoridade (presidente, diretores,
procurador-geral, secretario-executivo, chefe de gabinete), com o dia inteiro
num HTML: um <div> por compromisso, com cabecalhos "Manha/Tarde/Noite".

O HTML muda de forma ao longo dos anos (2023 tem o texto direto no <div>; 2025
aninha div/p/span), mas o compromisso e sempre um filho direto do <div> de
fora. Por isso o parser conta profundidade em vez de procurar classes.

Coleta mes a mes. Cada (autoridade, dia) e regravado inteiro, porque o BC edita
compromissos ja publicados e a ordem dentro do dia pode mudar.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402

logger = logging.getLogger(__name__)

SOURCE = "bcb"
API_URL = "https://www.bcb.gov.br/api/servico/sitebcb/agendadiretoria"
PAGE_URL = "https://www.bcb.gov.br/acessoinformacao/agendadiretoria"
REQUEST_TIMEOUT = 120

# Sigla da agenda -> cargo. Desde 2026 Dinor e Diorf saem juntas ("Dinor|Diorf").
CARGOS = {
    "PRESI": "Presidente do Banco Central",
    "DIFIS": "Diretor de Fiscalização",
    "DINOR": "Diretor de Regulação",
    "DIORF": "Diretor de Organização do Sistema Financeiro e de Resolução",
    "DIPOM": "Diretor de Política Monetária",
    "DIPEC": "Diretor de Política Econômica",
    "DIREC": "Diretor de Relacionamento, Cidadania e Supervisão de Conduta",
    "DIRAD": "Diretor de Administração",
    "DIREX": "Diretor de Assuntos Internacionais e de Gestão de Riscos Corporativos",
    "PGBC": "Procurador-Geral do Banco Central",
    "GAPRE": "Chefe de Gabinete do Presidente",
    "SECRE": "Secretário-Executivo do Banco Central",
}

_PERIODOS = {"manhã", "manha", "tarde", "noite"}
_HORARIO = re.compile(
    r"^\(?\s*(?:horário local\)?\s*)?(\d{1,2}[:h]\d{2})(?:\s*(?:às|as|a|-)\s*(\d{1,2}[:h]\d{2}))?\s*(?:\(horário local\))?\s*[–—-]?\s*",
    re.IGNORECASE,
)
# ", em Brasília, para" / "em São Paulo." / "em Nova Iorque, EUA". Vale a última.
_LUGAR = re.compile(r"\b(?:em|no(?= Rio de Janeiro))\s+([A-ZÀ-Ú][\wÀ-ú'.-]*(?:\s+(?:de|do|da|dos|das|d')?\s*[A-ZÀ-Ú][\wÀ-ú'.-]*){0,3})")
# Grafias diferentes da mesma cidade na fonte.
_MESMA_CIDADE = {
    "Brasilia": "Brasília",
    "Washington D.C": "Washington",
    "Washington DC": "Washington",
    "Washington D.C.": "Washington",
    "Basiléia": "Basileia",
    "Nova York": "Nova Iorque",
}
_ESPACOS = re.compile(r"\s+")
_INVISIVEIS = dict.fromkeys(map(ord, "​‌‍﻿"), None)


class _Compromissos(HTMLParser):
    """Texto de cada filho direto do <div> de fora."""

    _BLOCOS = {"div", "p"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.profundidade = 0
        self.itens: list[str] = []
        self._atual: Optional[list[str]] = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "br":
            if self._atual is not None:
                self._atual.append(" ")
            return
        if tag not in self._BLOCOS:
            return
        self.profundidade += 1
        if self.profundidade == 2 and tag == "div":
            self._atual = []

    def handle_endtag(self, tag: str) -> None:
        if tag not in self._BLOCOS:
            return
        if self.profundidade == 2 and tag == "div" and self._atual is not None:
            self.itens.append("".join(self._atual))
            self._atual = None
        self.profundidade = max(0, self.profundidade - 1)

    def handle_data(self, data: str) -> None:
        if self._atual is not None:
            self._atual.append(data)
        elif self.profundidade == 1 and data.strip():
            # HTML sem os <div> internos (texto solto no de fora).
            self.itens.append(data)


def _sem_acento(texto: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def _limpa(texto: str) -> str:
    return _ESPACOS.sub(" ", texto.translate(_INVISIVEIS).replace("\xa0", " ")).strip()


def lugar(texto: str) -> Optional[str]:
    achados = [m.group(1).strip(" .") for m in _LUGAR.finditer(texto)]
    achados = [a for a in achados if len(a) <= 40]
    if not achados:
        return None
    return _MESMA_CIDADE.get(achados[-1], achados[-1])


def parse_dia(html: str) -> list[dict[str, Any]]:
    """Compromissos de um dia, na ordem da página."""

    parser = _Compromissos()
    parser.feed(html or "")
    parser.close()
    saida = []
    for bruto in parser.itens:
        texto = _limpa(bruto)
        # Cabeçalho do período, às vezes colado no começo do item.
        primeira = texto.split(" ", 1)
        if primeira[0].lower() in _PERIODOS:
            texto = primeira[1] if len(primeira) > 1 else ""
        if not texto or texto.lower() in _PERIODOS:
            continue
        inicio = fim = None
        m = _HORARIO.match(texto)
        if m:
            inicio = m.group(1).replace("h", ":")
            fim = m.group(2).replace("h", ":") if m.group(2) else None
            texto = texto[m.end():].strip()
        if not texto:
            continue
        saida.append(
            {
                "starts_at": inicio,
                "ends_at": fim,
                "description": texto,
                "place": lugar(texto),
                # A fonte escreve até "videoconfêrencia": compara sem acento.
                "remote": "videoconfer" in _sem_acento(texto.lower()),
            }
        )
    return saida


def cargo(evento: str) -> tuple[Optional[str], Optional[str]]:
    """'Presi - 01/04/2025' -> ('Presi', 'Presidente do Banco Central')."""
    sigla = (evento or "").split(" -")[0].strip()
    if not sigla:
        return None, None
    rotulos = [CARGOS[s.strip().upper()] for s in sigla.split("|") if s.strip().upper() in CARGOS]
    return sigla, (" e ".join(rotulos) or None)


def build_items(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Linhas de `official_agenda_item` para um dia de uma autoridade."""

    dia = date.fromisoformat(str(item.get("dataEvento", ""))[:10]) if item.get("dataEvento") else None
    autoridade = item.get("idAutoridade")
    if dia is None or autoridade is None:
        return []
    # dataEvento vem em UTC à meia-noite de Brasília (03:00Z): a data já é a local.
    sigla, rotulo = cargo(item.get("evento") or "")
    return [
        {
            "source": SOURCE,
            "authority_id": str(autoridade),
            "authority_name": item.get("autoridade"),
            "office": sigla,
            "office_label": rotulo,
            "event_date": dia,
            "seq": i,
            "url": PAGE_URL,
            "organization": "Banco Central do Brasil",
            "participants": None,
            **c,
        }
        for i, c in enumerate(parse_dia(item.get("descricao") or ""))
    ]


def por_dia(itens: list[dict[str, Any]]) -> dict[tuple[str, date], list[dict[str, Any]]]:
    """Linhas agrupadas por (autoridade, dia), com `seq` contínuo.

    A fonte às vezes manda dois itens para a mesma autoridade no mesmo dia (em
    jan/2025 a agenda de 28/01 veio com a data de 27/01). Gravar item a item faria
    o segundo apagar o primeiro; agrupado, o dia fica com os dois.
    """
    dias: dict[tuple[str, date], list[dict[str, Any]]] = {}
    for item in itens:
        if not item.get("dataEvento") or item.get("idAutoridade") is None:
            continue
        chave = (str(item["idAutoridade"]), date.fromisoformat(str(item["dataEvento"])[:10]))
        linhas = dias.setdefault(chave, [])
        for linha in build_items(item):
            linhas.append({**linha, "seq": len(linhas)})
    return dias


def fetch_range(inicio: date, fim: date) -> list[dict[str, Any]]:
    params = {
        "lista": "Agenda da Diretoria",
        "inicioAgenda": f"'{inicio.isoformat()}'",
        "fimAgenda": f"'{fim.isoformat()}'",
    }
    resp = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return list(resp.json().get("conteudo") or [])


def _meses(inicio: date, fim: date):
    atual = inicio.replace(day=1)
    while atual <= fim:
        proximo = (atual.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield max(atual, inicio), min(proximo - timedelta(days=1), fim)
        atual = proximo


def save_day(
    session: Any, linhas: list[dict[str, Any]], *, authority_id: str, event_date: date, source: str = SOURCE
) -> None:
    """Regrava o dia de uma autoridade (usado também pelo coletor do STF)."""
    from sqlalchemy import text

    session.execute(
        text(
            "DELETE FROM official_agenda_item "
            "WHERE source = :s AND authority_id = :a AND event_date = :d"
        ),
        {"s": source, "a": authority_id, "d": event_date},
    )
    if linhas:
        session.execute(
            text(
                "INSERT INTO official_agenda_item (source, authority_id, authority_name, office, "
                "office_label, event_date, seq, starts_at, ends_at, description, place, remote, url, "
                "organization, participants) "
                "VALUES (:source, :authority_id, :authority_name, :office, :office_label, :event_date, "
                ":seq, :starts_at, :ends_at, :description, :place, :remote, :url, :organization, :participants)"
            ),
            linhas,
        )


def collect(inicio: date, fim: date, *, persist: bool = True) -> dict[str, int]:
    if persist:
        from mamute_scrappers.db import session_scope

        contexto = session_scope()
    else:
        from contextlib import nullcontext

        contexto = nullcontext(None)

    dias = compromissos = 0
    with contexto as session:
        for de, ate in _meses(inicio, fim):
            itens = fetch_range(de, ate)
            for (autoridade, dia), linhas in por_dia(itens).items():
                dias += 1
                compromissos += len(linhas)
                if session is not None:
                    save_day(session, linhas, authority_id=autoridade, event_date=dia)
            if session is not None:
                session.commit()
            logger.info("Agenda BC %s a %s: %s dias de autoridade.", de, ate, len(itens))
    logger.info("=== Agenda BC %s a %s: %s dias, %s compromissos ===", inicio, fim, dias, compromissos)
    if not persist:
        logger.info("Modo dry-run: nada foi gravado.")
    return {"dias": dias, "compromissos": compromissos}


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Coleta a agenda da diretoria do Banco Central.")
    parser.add_argument("--desde", type=date.fromisoformat, help="AAAA-MM-DD (default: 1º dia do mês passado).")
    parser.add_argument("--ate", type=date.fromisoformat, help="AAAA-MM-DD (default: hoje + 30 dias).")
    parser.add_argument("--dry-run", action="store_true", help="Não grava.")
    args = parser.parse_args()
    hoje = date.today()
    desde = args.desde or (hoje.replace(day=1) - timedelta(days=1)).replace(day=1)
    # A agenda é publicada com antecedência: o mês que vem já pode ter compromissos.
    ate = args.ate or hoje + timedelta(days=30)
    collect(desde, ate, persist=not args.dry_run)
