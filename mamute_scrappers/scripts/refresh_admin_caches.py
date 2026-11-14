"""Rotina diária (04h) que preenche os caches dos painéis admin.

Roda no container dos scrappers (único com cron). NÃO pode importar `api.*` —
o container só tem `mamute_scrappers/`. Por isso a lógica de cobertura e as
funções de contagem das APIs abertas ficam aqui em SQL cru / funções puras.

Faz três coisas, cada uma isolada (uma falha não aborta as outras):

  1. api_coverage      — total ANUAL oficial de proposições da Câmara (todos os
     tipos, via API aberta). Guardado com sigla_type='TODOS'.
  2. coverage_snapshot — relatório de cobertura já computado (proposições,
     discursos, votações, parlamentares e o total de registros oficiais dos
     cards da home — CS-170) como um único JSON. A API só lê.
  3. usd_brl_rate      — cotação USD→BRL do dia via API pública.

Uso:
    python -m mamute_scrappers.scripts.refresh_admin_caches
    python -m mamute_scrappers.scripts.refresh_admin_caches --coverage-start 2018
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

logger = logging.getLogger("refresh_admin_caches")

CAMARA_URL = "https://dadosabertos.camara.leg.br/api/v2/proposicoes"
# BCB SGS série 1 (dólar comercial venda, dias úteis) — primário. A awesomeapi
# fica de fallback: o plano gratuito bloqueia o IP de prod por quota (429).
USD_BRL_BCB_URL = (
    "https://api.bcb.gov.br/dados/serie/bcdata.sgs.1/dados/ultimos/1?formato=json"
)
USD_BRL_URL = "https://economia.awesomeapi.com.br/last/USD-BRL"

MIN_YEAR = 2018
# Cadeiras oficiais (constitucionais). A base tem mais (suplentes + histórico).
CADEIRAS_CAMARA = 513
CADEIRAS_SENADO = 81
# Marca a linha de api_coverage que guarda o TOTAL anual (todos os tipos).
TOTAL_SIGLA = "TODOS"

# Tabelas que somam o "total de registros oficiais" do card da home (CS-170):
# cada linha é um dado tirado de fonte oficial (Câmara, Senado, TSE, Portal da
# Transparência, Transferegov). Ficam de fora tabelas de ligação (autorias,
# proposição x discurso), cadastros auxiliares (tipos, situações, comissões),
# dados derivados por nós (resumos, palavras-chave) e a apuração por município
# ou local de votação, que repete o mesmo resultado em milhares de linhas.
REGISTROS_OFICIAIS: tuple[tuple[str, str], ...] = (
    ("parliamentarian", "Parlamentares"),
    ("proposition", "Proposições"),
    ("roll_call_votes", "Votos nominais"),
    ("speeches_transcripts", "Discursos"),
    ("parliamentary_expense", "Gastos da cota parlamentar"),
    ("parliamentary_amendment", "Emendas parlamentares"),
    ("amendment_action_plan", "Planos de ação das emendas Pix"),
    ("plenary_attendance", "Presenças em plenário"),
    ("committee_attendance", "Presenças em comissões"),
    ("official_agenda_item", "Compromissos da agenda oficial"),
    ("candidacy", "Candidaturas (TSE)"),
    ("candidacy_result", "Resultados de candidaturas (TSE)"),
    ("electoral_history", "Histórico eleitoral (TSE)"),
)


# --------------------------------------------------------------------------- #
# Total anual oficial de proposições da Câmara (todos os tipos).
# A API sem siglaTipo já dá o total do ano — o "last page" com itens=1 = total.
# --------------------------------------------------------------------------- #
def parse_last_page(payload: dict[str, Any]) -> int:
    for link in payload.get("links", []) or []:
        if link.get("rel") == "last":
            href = link.get("href") or ""
            pagina = parse_qs(urlparse(href).query).get("pagina", [None])[0]
            if pagina and pagina.isdigit():
                return int(pagina)
    return len(payload.get("dados", []) or [])


def fetch_camara_year_total(year: int, http_get: Callable[..., Any]) -> int:
    resp = http_get(
        CAMARA_URL,
        params={"ano": year, "itens": 1, "pagina": 1},
        headers={"accept": "application/json"},
        timeout=25,
    )
    resp.raise_for_status()
    return parse_last_page(resp.json())


def upsert_api_total(session: Session, source: str, year: int, count: int) -> None:
    session.execute(
        text(
            """
            INSERT INTO api_coverage (source, year, sigla_type, api_count, synced_at)
            VALUES (:source, :year, :sigla, :count, CURRENT_TIMESTAMP)
            ON CONFLICT (source, year, sigla_type) DO UPDATE SET
                api_count = EXCLUDED.api_count,
                synced_at = CURRENT_TIMESTAMP
            """
        ),
        {"source": source, "year": year, "sigla": TOTAL_SIGLA, "count": count},
    )


def sync_camara_year_totals(
    session: Session, http_get: Callable[..., Any], start: int, end: int
) -> int:
    n = 0
    for year in range(start, end + 1):
        try:
            total = fetch_camara_year_total(year, http_get)
            upsert_api_total(session, "camara", year, total)
            n += 1
        except Exception:  # noqa: BLE001 — um ano não pode abortar o resto
            logger.exception("Falha no total oficial Câmara %s", year)
    return n


# --------------------------------------------------------------------------- #
# Cobertura da base (relatório multi-seção). Casa é derivada do tipo do autor
# (proposições) ou do parlamentar (discursos/votações). JOIN+GROUP BY em vez de
# EXISTS correlacionado (o antigo travava em ~104k proposições).
# --------------------------------------------------------------------------- #
def _year_expr(session: Session, col: str) -> str:
    """Extração de ano portável (Postgres em prod, SQLite nos testes)."""
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        return f"cast(strftime('%Y', {col}) as integer)"
    return f"extract(year from {col})::int"


def _scalar(session: Session, sql: str) -> int:
    return int(session.execute(text(sql)).scalar() or 0)


def _prop_status(nosso: int, oficial: int | None) -> str:
    if not oficial:
        return "sem_referencia"
    pct = nosso / oficial * 100
    if pct >= 120:
        return "superset"
    if pct >= 95:
        return "completo"
    if pct >= 80:
        return "quase"
    return "parcial"


def _year_house_counts(session: Session, sql: str) -> dict[int, dict[str, int]]:
    """sql deve retornar (yr, casa, n). Casa 'senado' vs qualquer outra = câmara."""
    by_year: dict[int, dict[str, int]] = {}
    for r in session.execute(text(sql), {"miny": MIN_YEAR}).mappings():
        if r["yr"] is None:
            continue
        bucket = by_year.setdefault(int(r["yr"]), {"camara": 0, "senado": 0})
        key = "senado" if r["casa"] == "senado" else "camara"
        bucket[key] += int(r["n"])
    return by_year


def count_official_records(session: Session) -> dict[str, Any]:
    """Total de registros oficiais da base, com o detalhe por tabela.

    Tabela que ainda não existe (deploy novo, migration pendente) fica fora do
    detalhe em vez de entrar como zero — zero passaria por "coletado e vazio".
    """
    inspector = inspect(session.get_bind())
    por_tabela = []
    for tabela, rotulo in REGISTROS_OFICIAIS:
        if not inspector.has_table(tabela):
            continue
        por_tabela.append(
            {"tabela": tabela, "rotulo": rotulo, "n": _scalar(session, f"select count(*) from {tabela}")}
        )
    return {
        "total": sum(item["n"] for item in por_tabela),
        "por_tabela": por_tabela,
        "nota": "Soma do que veio de fonte oficial (Câmara, Senado, TSE, Portal da "
        "Transparência, Transferegov). Fora da conta: tabelas de ligação, cadastros "
        "auxiliares, dados gerados pelo Mamute e a apuração por município ou local "
        "de votação. É o número do card de dados oficiais da home.",
    }


def _to_lists(by_year: dict[int, dict[str, int]]) -> tuple[list[dict], list[dict]]:
    """Duas listas (câmara, senado) ordenadas por ano desc, só com anos != 0."""
    camara, senado = [], []
    for y in sorted(by_year, reverse=True):
        camara.append({"year": y, "nosso": by_year[y]["camara"]})
        senado.append({"year": y, "nosso": by_year[y]["senado"]})
    return camara, senado


def build_coverage_payload(session: Session) -> dict[str, Any]:
    yr_disc = _year_expr(session, "sp.date")
    yr_vote = _year_expr(session, "rcv.vote_date")

    kpis = {
        "proposicoes": _scalar(session, "select count(*) from proposition"),
        "discursos": _scalar(session, "select count(*) from speeches_transcripts"),
        "votacoes_nominais": _scalar(
            session, "select count(distinct link) from roll_call_votes"
        ),
        "parlamentares": _scalar(session, "select count(*) from parliamentarian"),
    }

    # --- Proposições: casa pelo autor; Câmara = camara + desconhecido ---
    prop_rows = session.execute(
        text(
            """
            select yr, house, count(*) n,
                   sum(case when sem_tipo then 1 else 0 end) sem_tipo
            from (
                select p.id, p.presentation_year yr,
                    case
                        when max(case when lower(coalesce(par.type,'')) like '%senad%'
                                      then 1 else 0 end) = 1 then 'senado'
                        when count(ap.id) > 0 then 'camara'
                        else 'desconhecido'
                    end house,
                    (p.proposition_type_id is null) sem_tipo
                from proposition p
                left join authors_proposition ap on ap.proposition_id = p.id
                left join parliamentarian par on par.id = ap.parliamentarian_id
                group by p.id, p.presentation_year, p.proposition_type_id
            ) x
            where yr >= :miny
            group by yr, house
            """
        ),
        {"miny": MIN_YEAR},
    ).mappings().all()

    prop_by_year: dict[int, dict[str, int]] = {}
    sem_tipo_total = 0
    for r in prop_rows:
        y = prop_by_year.setdefault(int(r["yr"]), {"camara": 0, "senado": 0})
        if r["house"] == "senado":
            y["senado"] += int(r["n"])
        else:  # camara + desconhecido
            y["camara"] += int(r["n"])
        sem_tipo_total += int(r["sem_tipo"] or 0)

    oficial = {
        int(r["year"]): int(r["n"])
        for r in session.execute(
            text(
                "select year, sum(api_count) n from api_coverage "
                "where source='camara' and sigla_type=:s group by year"
            ),
            {"s": TOTAL_SIGLA},
        ).mappings()
    }

    prop_camara, prop_senado, superset_years = [], [], []
    for y in sorted(prop_by_year, reverse=True):
        v = prop_by_year[y]
        of = oficial.get(y)
        status = _prop_status(v["camara"], of)
        if status == "superset":
            superset_years.append(y)
        prop_camara.append(
            {
                "year": y,
                "nosso": v["camara"],
                "oficial": of,
                "pct": round(v["camara"] / of * 100, 1) if of else None,
                "status": status,
            }
        )
        prop_senado.append({"year": y, "nosso": v["senado"]})

    superset_years.sort()
    nota_superset = (
        "A partir de "
        + (str(superset_years[0]) if superset_years else "2024")
        + " a base tem mais registros que o oficial: além das proposições "
        "principais, capturamos sub-documentos (pareceres, substitutivos, emendas). "
        f"Há {sem_tipo_total:,} registros sem 'tipo' preenchido (2018+)."
    ).replace(",", ".")

    # --- Discursos ---
    disc_by_year = _year_house_counts(
        session,
        f"""
        select {yr_disc} yr,
               case when lower(coalesce(par.type,'')) like '%senad%'
                    then 'senado' else 'camara' end casa,
               count(*) n
        from speeches_transcripts sp
        join parliamentarian par on par.id = sp.parliamentarian_id
        where {yr_disc} >= :miny
        group by yr, casa
        """,
    )
    disc_camara, disc_senado = _to_lists(disc_by_year)

    # --- Votações nominais (sessões = distinct link) ---
    vote_by_year = _year_house_counts(
        session,
        f"""
        select {yr_vote} yr,
               case when lower(coalesce(par.type,'')) like '%senad%'
                    then 'senado' else 'camara' end casa,
               count(distinct rcv.link) n
        from roll_call_votes rcv
        join parliamentarian par on par.id = rcv.parliamentarian_id
        where {yr_vote} >= :miny
        group by yr, casa
        """,
    )
    vote_camara, vote_senado = _to_lists(vote_by_year)

    # --- Parlamentares ---
    dep = _scalar(
        session,
        "select count(*) from parliamentarian where lower(coalesce(type,'')) like '%deputad%'",
    )
    sen = _scalar(
        session,
        "select count(*) from parliamentarian where lower(coalesce(type,'')) like '%senad%'",
    )

    # --- Consolidado (status por categoria) ---
    prop_status_overall = (
        "parcial"
        if any(r["status"] == "parcial" for r in prop_camara if r["oficial"])
        else "completo"
    )
    disc_camara_min = min((r["year"] for r in disc_camara if r["nosso"] > 0), default=None)
    disc_camara_status = "completo" if disc_camara_min and disc_camara_min <= MIN_YEAR else "quase"

    consolidado = [
        {
            "categoria": "Proposições (Câmara, principais)",
            "status": prop_status_overall,
            "observacao": "Câmara = proposições cujo autor não é senador. 2024+ acima "
            "de 100% por conta de sub-documentos.",
        },
        {
            "categoria": "Proposições (Senado)",
            "status": "sem_referencia",
            "observacao": "Cobertura presente; a API do Senado não expõe total anual "
            "confiável para calcular %.",
        },
        {
            "categoria": "Discursos (Câmara)",
            "status": disc_camara_status,
            "observacao": (
                "Presente desde " + str(disc_camara_min) + "."
                if disc_camara_status == "completo"
                else "Início da cobertura em " + str(disc_camara_min) + "."
            )
            if disc_camara_min
            else "Sem discursos da Câmara na base.",
        },
        {
            "categoria": "Discursos (Senado)",
            "status": "completo",
            "observacao": "Sem total agregado na API oficial — avaliação por presença.",
        },
        {
            "categoria": "Votações nominais",
            "status": "completo",
            "observacao": "Sessões nominais nas duas casas. Universo ≠ total da API "
            "(que inclui simbólicas), então não há % direto.",
        },
        {
            "categoria": "Parlamentares",
            "status": "completo",
            "observacao": f"Base tem {dep + sen} (cadeiras atuais {CADEIRAS_CAMARA + CADEIRAS_SENADO}) "
            "— inclui suplentes e legislaturas anteriores.",
        },
    ]

    return {
        "kpis": kpis,
        "proposicoes": {
            "camara": prop_camara,
            "senado": prop_senado,
            "sem_tipo_total": sem_tipo_total,
            "nota_superset": nota_superset,
        },
        "discursos": {
            "camara": disc_camara,
            "senado": disc_senado,
            "nota": "A API oficial não expõe total agregado de discursos (só por "
            "parlamentar), então não há % — avaliação por presença/ausência. "
            "Duplicatas da Câmara já foram limpas em produção.",
        },
        "votacoes": {
            "camara": vote_camara,
            "senado": vote_senado,
            "nota": "A base guarda só votações nominais (voto individual registrado). "
            "A API conta todas (inclui simbólicas), então um % direto não é comparável. "
            "Os números são as sessões nominais capturadas.",
        },
        "parlamentares": {
            "deputados": {
                "nossa_base": dep,
                "cadeiras": CADEIRAS_CAMARA,
                "status": "completo",
            },
            "senadores": {
                "nossa_base": sen,
                "cadeiras": CADEIRAS_SENADO,
                "status": "completo",
            },
            "nota": "A base tem mais parlamentares que as cadeiras atuais porque "
            "inclui suplentes e parlamentares de legislaturas anteriores.",
        },
        "registros_oficiais": count_official_records(session),
        "consolidado": consolidado,
    }


def store_coverage_snapshot(session: Session, payload: dict[str, Any]) -> None:
    """Substitui o snapshot (mantemos só a linha mais recente)."""
    session.execute(text("DELETE FROM coverage_snapshot"))
    session.execute(
        text(
            "INSERT INTO coverage_snapshot (payload, computed_at) "
            "VALUES (CAST(:payload AS JSONB), CURRENT_TIMESTAMP)"
        ),
        {"payload": json.dumps(payload)},
    )


# --------------------------------------------------------------------------- #
# Câmbio USD→BRL do dia.
# --------------------------------------------------------------------------- #
def fetch_usd_brl_bid(http_get: Callable[..., Any]) -> float:
    try:
        resp = http_get(USD_BRL_BCB_URL, timeout=10)
        resp.raise_for_status()
        return float(resp.json()[0]["valor"])
    except Exception:  # noqa: BLE001
        logger.warning("BCB indisponível para USD→BRL; tentando awesomeapi.")
        resp = http_get(USD_BRL_URL, timeout=10)
        resp.raise_for_status()
        return float(resp.json()["USDBRL"]["bid"])


def store_usd_brl_rate(session: Session, bid: float, today: date) -> None:
    session.execute(
        text(
            """
            INSERT INTO usd_brl_rate (rate_date, bid, fetched_at)
            VALUES (:d, :bid, CURRENT_TIMESTAMP)
            ON CONFLICT (rate_date) DO UPDATE SET
                bid = EXCLUDED.bid,
                fetched_at = CURRENT_TIMESTAMP
            """
        ),
        {"d": today, "bid": bid},
    )


# --------------------------------------------------------------------------- #
def main() -> None:
    import requests

    from mamute_scrappers.db.session import get_session

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    current_year = datetime.now().year
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-start", type=int, default=MIN_YEAR)
    parser.add_argument("--coverage-end", type=int, default=current_year)
    args = parser.parse_args()

    session = get_session()
    try:
        # 1) Total anual oficial da Câmara → api_coverage
        try:
            n = sync_camara_year_totals(
                session, requests.get, args.coverage_start, args.coverage_end
            )
            session.commit()
            logger.info("api_coverage (totais Câmara): %s anos sincronizados.", n)
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("Totais oficiais da Câmara falharam.")

        # 2) Snapshot da cobertura
        try:
            payload = build_coverage_payload(session)
            store_coverage_snapshot(session, payload)
            session.commit()
            logger.info(
                "coverage_snapshot atualizado (props %s anos, discursos %s, votações %s).",
                len(payload["proposicoes"]["camara"]),
                len(payload["discursos"]["camara"]),
                len(payload["votacoes"]["camara"]),
            )
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("coverage_snapshot falhou.")

        # 3) Câmbio do dia
        try:
            bid = fetch_usd_brl_bid(requests.get)
            store_usd_brl_rate(session, bid, datetime.now().date())
            session.commit()
            logger.info("usd_brl_rate do dia = %s", bid)
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("usd_brl_rate falhou.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
