"""Agenda da Presidência e dos ministros do STF (CS-135).

Fonte: o GraphQL do portal de notícias do STF (WordPress + WPGraphQL), o mesmo
que as páginas de agenda usam, sem login:
https://noticias.stf.jus.br/graphql?query={agendaMinistrosPorDiaCategoria(...)}
Uma consulta por mês traz todas as agendas pedidas (`slugs`), com os eventos de
cada dia: hora, título e texto (HTML curto, em geral "Assunto: ...").

Cobertura medida em out/2026: a Presidência publica desde 30/09/2025; entre os
ministros, só alguns publicam (Zanin, Cármen Lúcia; Fachin até assumir a
Presidência). Pedimos todas as agendas conhecidas; quem não publica volta vazio.

Gotchas:
1. A HORA VEM 3 HORAS ATRASADA. As páginas do STF somam 3 (função corrigeHora
   no JS do site); aqui também.
2. HÁ UM WAF NA FRENTE. Rajadas recebem 202 com corpo vazio
   (`x-amzn-waf-action: challenge`). O coletor espera entre consultas e tenta de
   novo com espera maior.
3. Cada (agenda, dia) é regravado inteiro, como na agenda do BC.
"""

from __future__ import annotations

import html
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402

from mamute_scrappers.agenda_crawler.banco_central import _meses, save_day  # noqa: E402

logger = logging.getLogger(__name__)

SOURCE = "stf"
GRAPHQL_URL = "https://noticias.stf.jus.br/graphql"
PAGE_URL = "https://portal.stf.jus.br/ministro/agendaMinistro.asp"
ORGANIZACAO = "Supremo Tribunal Federal"
REQUEST_TIMEOUT = 60
# Espera entre consultas e esperas das novas tentativas quando o WAF barra.
PAUSA = 25
ESPERAS_WAF = (60, 150, 300)
HORAS_ATRASO = 3

# Agendas pedidas: as do JS do site e as que ele não lista mas seguem o padrão.
SLUGS = (
    "presidente",
    "min-alexandre-de-moraes",
    "min-andre-mendonca",
    "min-carmen-lucia",
    "min-cristiano-zanin",
    "min-dias-toffoli",
    "min-edson-fachin",
    "min-flavio-dino",
    "min-gilmar-mendes",
    "min-luis-roberto-barroso",
    "min-luiz-fux",
    "min-nunes-marques",
)

_HORA = re.compile(r"^\s*(\d{1,2})\s*[hH:.,]?\s*(\d{2})?")
_TAGS = re.compile(r"<[^>]+>")
_ESPACOS = re.compile(r"\s+")


class WafBloqueou(RuntimeError):
    """O WAF do STF devolveu o desafio em vez dos dados."""


def hora_corrigida(texto: Optional[str]) -> Optional[str]:
    """'12h00' na fonte = 15:00 de Brasília (o site soma 3 horas)."""
    m = _HORA.match(texto or "")
    if not m:
        return None
    h = (int(m.group(1)) + HORAS_ATRASO) % 24
    return f"{h:02d}:{int(m.group(2) or 0):02d}"


def limpa(texto: Optional[str]) -> str:
    return _ESPACOS.sub(" ", html.unescape(_TAGS.sub(" ", texto or "")).replace("\xa0", " ")).strip()


def quem(nome_fonte: str) -> tuple[str, str, str]:
    """'MIN. CRISTIANO ZANIN' -> (id, 'Cristiano Zanin', 'Ministro do STF'); 'PRESIDENTE' -> presidência."""
    nome = limpa(nome_fonte)
    if nome.upper() == "PRESIDENTE":
        return "presidente", "Presidência do STF", "Presidente do Supremo Tribunal Federal"
    sem_prefixo = re.sub(r"^(MIN(ISTRO|ISTRA)?\.?)\s+", "", nome, flags=re.IGNORECASE)
    bonito = " ".join(p if p.lower() in {"de", "da", "do", "dos", "das"} else p.capitalize() for p in sem_prefixo.lower().split())
    return re.sub(r"[^a-z]+", "-", bonito.lower()).strip("-"), bonito, "Ministro do Supremo Tribunal Federal"


def build_items(dia_fonte: dict[str, Any]) -> dict[tuple[str, date], list[dict[str, Any]]]:
    """Linhas de um dia da resposta, agrupadas por (agenda, dia)."""

    try:
        d, m, a = (int(x) for x in str(dia_fonte.get("data", "")).split("/"))
        dia = date(a, m, d)
    except ValueError:
        return {}
    saida: dict[tuple[str, date], list[dict[str, Any]]] = {}
    for ministro in dia_fonte.get("ministro") or []:
        ident, nome, cargo = quem(ministro.get("nomeMinistro") or "")
        linhas = saida.setdefault((ident, dia), [])
        for ev in ministro.get("eventos") or []:
            titulo, texto = limpa(ev.get("titulo")), limpa(ev.get("texto"))
            descricao = " ".join(p for p in (titulo, texto) if p)
            if not descricao:
                continue
            linhas.append(
                {
                    "source": SOURCE,
                    "authority_id": ident,
                    "authority_name": nome,
                    "office": ident,
                    "office_label": cargo,
                    "event_date": dia,
                    "seq": len(linhas),
                    "starts_at": hora_corrigida(ev.get("hora")),
                    "ends_at": None,
                    "description": descricao,
                    "place": None,
                    "remote": "videoconfer" in descricao.lower(),
                    "url": PAGE_URL,
                    "organization": ORGANIZACAO,
                    "participants": None,
                }
            )
    return saida


def _consulta(inicio: date, fim: date) -> str:
    slugs = ", ".join(f'"{s}"' for s in SLUGS)
    return (
        "{agendaMinistrosPorDiaCategoria(where:{categories:{operator:IN,slugs:[" + slugs + "]},"
        f"dateQuery:{{after:{{day:{inicio.day},month:{inicio.month},year:{inicio.year}}},"
        f"before:{{day:{fim.day},month:{fim.month},year:{fim.year}}}}},"
        "orderby:{field:DATE,order:ASC}},first:500){data ministro{nomeMinistro eventos{hora titulo texto}}}}"
    )


def fetch_range(inicio: date, fim: date, *, esperar=time.sleep) -> list[dict[str, Any]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "application/json",
    }
    for tentativa, espera in enumerate((0, *ESPERAS_WAF)):
        if espera:
            logger.info("WAF do STF barrou; nova tentativa em %s s.", espera)
            esperar(espera)
        resp = requests.get(GRAPHQL_URL, params={"query": _consulta(inicio, fim)}, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 202 or not resp.content:
            continue
        resp.raise_for_status()
        return list((resp.json().get("data") or {}).get("agendaMinistrosPorDiaCategoria") or [])
    raise WafBloqueou(f"WAF do STF barrou {inicio:%m/%Y} depois de {tentativa + 1} tentativas.")


def collect(inicio: date, fim: date, *, persist: bool = True, esperar=time.sleep) -> dict[str, int]:
    if persist:
        from mamute_scrappers.db import session_scope

        contexto = session_scope()
    else:
        from contextlib import nullcontext

        contexto = nullcontext(None)

    dias = compromissos = 0
    with contexto as session:
        from mamute_scrappers.agenda_crawler.alerta import avisar_novos, foto

        antes = foto(session, SOURCE) if session is not None else set()
        for i, (de, ate) in enumerate(_meses(inicio, fim)):
            if i:
                esperar(PAUSA)
            for dia_fonte in fetch_range(de, ate, esperar=esperar):
                for (autoridade, dia), linhas in build_items(dia_fonte).items():
                    dias += 1
                    compromissos += len(linhas)
                    if session is not None:
                        save_day(session, linhas, authority_id=autoridade, event_date=dia, source=SOURCE)
            if session is not None:
                session.commit()
            logger.info("Agenda STF %s a %s ok.", de, ate)
        if session is not None:
            avisar_novos(session, SOURCE, antes)
    logger.info("=== Agenda STF %s a %s: %s dias, %s compromissos ===", inicio, fim, dias, compromissos)
    if not persist:
        logger.info("Modo dry-run: nada foi gravado.")
    return {"dias": dias, "compromissos": compromissos}


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Coleta a agenda da Presidência e dos ministros do STF.")
    parser.add_argument("--desde", type=date.fromisoformat, help="AAAA-MM-DD (default: 1º dia do mês passado).")
    parser.add_argument("--ate", type=date.fromisoformat, help="AAAA-MM-DD (default: hoje + 30 dias).")
    parser.add_argument("--dry-run", action="store_true", help="Não grava.")
    args = parser.parse_args()
    hoje = date.today()
    desde = args.desde or (hoje.replace(day=1) - timedelta(days=1)).replace(day=1)
    ate = args.ate or hoje + timedelta(days=30)
    collect(desde, ate, persist=not args.dry_run)
