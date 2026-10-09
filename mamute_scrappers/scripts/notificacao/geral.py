"""Destaques gerais do Congresso na quinzena (CS-133).

Vão para quem hoje ficava sem e-mail: conta sem parlamentar selecionado e
conta cujos parlamentares não tiveram atividade. Critério objetivo, sem
curadoria:

* votações: as últimas proposições com votação nominal no período (uma por
  proposição), com o placar de Sim e Não;
* temas: as palavras-chave principais mais frequentes nos discursos do
  período, com os mesmos filtros da nuvem de palavras (`word_cloud_terms`).

Calculado uma vez por envio e reaproveitado para todas as contas.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from sqlalchemy import case, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from mamute_scrappers.db.models import Proposition, RollCallVote

from .labels import (
    extract_ementa,
    format_proposition_display_title,
    is_camara_proposition,
)
from .links import resolve_proposition_link
from .models import ActivityItem, GeneralHighlights

MAX_VOTACOES = 5
MAX_TEMAS = 8
# Abaixo disso a quinzena não tem discurso analisado o bastante para falar em tema.
MIN_TEMAS = 3
# Recesso ou período eleitoral: até onde buscar as últimas votações.
JANELA_ULTIMAS_VOTACOES_DIAS = 120
TAMANHO_MINIMO_TEMA = 3


def _subtitulo(link: str | None, dia: date, sim: int, nao: int) -> str:
    """A mesma proposição pode ter votação na Câmara e no Senado: a casa diferencia."""
    casa = "Câmara" if is_camara_proposition(link) else "Senado"
    texto = f"{casa} · votação em {dia.strftime('%d/%m/%Y')}"
    if sim or nao:
        texto += f" · Sim {sim} × Não {nao}"
    return texto


def _votacoes(session: Session, inicio: date | None, fim: date) -> list[ActivityItem]:
    ultima = func.max(RollCallVote.vote_date)
    sim = func.sum(case((RollCallVote.vote == "Sim", 1), else_=0))
    nao = func.sum(case((RollCallVote.vote == "Não", 1), else_=0))
    stmt = (
        select(
            Proposition.id,
            Proposition.title,
            Proposition.link,
            Proposition.proposition_code,
            Proposition.proposition_acronym,
            Proposition.proposition_number,
            Proposition.presentation_year,
            Proposition.proposition_description,
            Proposition.summary,
            ultima,
            sim,
            nao,
        )
        .join(Proposition, Proposition.id == RollCallVote.proposition_id)
        .where(RollCallVote.vote_date <= fim)
        .where(RollCallVote.vote_date >= inicio if inicio else RollCallVote.vote_date.is_not(None))
        .group_by(
            Proposition.id,
            Proposition.title,
            Proposition.link,
            Proposition.proposition_code,
            Proposition.proposition_acronym,
            Proposition.proposition_number,
            Proposition.presentation_year,
            Proposition.proposition_description,
            Proposition.summary,
        )
        .order_by(ultima.desc(), Proposition.id.desc())
        .limit(MAX_VOTACOES)
    )
    itens: list[ActivityItem] = []
    for (pid, title, link, code, sigla, numero, ano, descricao, resumo, dia, n_sim, n_nao) in session.execute(stmt).all():
        dia = dia if isinstance(dia, date) else date.fromisoformat(str(dia))
        itens.append(
            ActivityItem(
                kind="votação",
                title=format_proposition_display_title(
                    title=title, link=link, acronym=sigla, number=numero, year=ano
                ),
                subtitle=_subtitulo(link, dia, int(n_sim or 0), int(n_nao or 0)),
                parliamentarian_name="",
                ementa=extract_ementa(descricao, resumo),
                link=resolve_proposition_link(link, code, camara=is_camara_proposition(link)),
                occurred_at=dia,
                kind_key="proposicao",
                item_id=int(pid),
            )
        )
    return itens


def _filtros_da_nuvem(session: Session) -> tuple[set[str], set[str]]:
    try:
        linhas = session.execute(text("SELECT term, kind FROM word_cloud_terms")).all()
    except SQLAlchemyError:
        session.rollback()
        return set(), set()
    stopwords = {t.lower() for t, k in linhas if k == "stopword"}
    excluidos = {t.lower() for t, k in linhas if k == "excluded"}
    return stopwords, excluidos


def _temas(session: Session, inicio: date, fim: date) -> list[str]:
    linhas = session.execute(
        text(
            "SELECT k.term, k.keyword, k.frequency FROM speeches_transcripts_keywords k "
            "JOIN speeches_transcripts s ON s.id = k.speeches_transcripts_id "
            "WHERE k.is_primary AND s.date >= :inicio AND s.date <= :fim"
        ),
        {"inicio": inicio, "fim": fim},
    ).all()
    stopwords, excluidos = _filtros_da_nuvem(session)
    soma: Counter[str] = Counter()
    for term, keyword, frequency in linhas:
        bruto = " ".join(str(term or keyword or "").split()).lower()
        if not bruto or bruto in excluidos:
            continue
        limpo = " ".join(p for p in bruto.split() if p not in stopwords)
        if len(limpo) < TAMANHO_MINIMO_TEMA or limpo in excluidos:
            continue
        soma[limpo] += int(frequency or 0)
    temas = [t for t, _ in sorted(soma.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_TEMAS]]
    return temas if len(temas) >= MIN_TEMAS else []


def build_general_highlights(session: Session, inicio: date, fim: date) -> GeneralHighlights:
    votacoes = _votacoes(session, inicio, fim)
    anteriores = False
    if not votacoes:
        votacoes = _votacoes(session, inicio - timedelta(days=JANELA_ULTIMAS_VOTACOES_DIAS), inicio)
        anteriores = bool(votacoes)
    return GeneralHighlights(
        votacoes=votacoes, temas=_temas(session, inicio, fim), votacoes_anteriores=anteriores
    )
