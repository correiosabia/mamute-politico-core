"""Rotas para análises de discursos do Senado."""

from __future__ import annotations

from datetime import date as date_type
from typing import Dict, List, Literal, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

try:
    # Execução como pacote (api.routers.analysis).
    from ..dependencies import get_db
    from .projects import (
        _current_legislature_range,
        _last_three_months_range_sao_paulo,
    )
    from ..db.models import (
        Parliamentarian,
        SpeechesTranscript,
        SpeechesTranscriptsEntity,
        SpeechesTranscriptsKeyword,
    )
except (ImportError, ValueError):
    # Execução local dentro de api/ sem reconhecimento de pacote.
    from dependencies import get_db
    from routers.projects import (
        _current_legislature_range,
        _last_three_months_range_sao_paulo,
    )
    from db.models import (
        Parliamentarian,
        SpeechesTranscript,
        SpeechesTranscriptsEntity,
        SpeechesTranscriptsKeyword,
    )

AnalysisType = Literal["spacy", "chatgpt"]
TermsWindow = Literal["last_3_months", "legislature"]

router = APIRouter(prefix="/analysis", tags=["analysis"])


class KeywordOut(BaseModel):
    """Representação de uma palavra-chave extraída."""

    id: int
    keyword: str
    term: str
    frequency: int
    rank: int
    is_primary: bool
    analysis_type: str

    model_config = ConfigDict(from_attributes=True)


class EntityOut(BaseModel):
    """Representação de uma entidade nomeada extraída."""

    id: int
    label: str
    text: str
    start_char: Optional[int] = None
    end_char: Optional[int] = None
    analysis_type: str

    model_config = ConfigDict(from_attributes=True)


class SpeechAnalysisOut(BaseModel):
    """Resposta agregada de palavras-chave e entidades de um discurso."""

    speech_id: int
    keywords: List[KeywordOut]
    entities: List[EntityOut]

    model_config = ConfigDict(from_attributes=True)


class SpeechAnalysisSummaryOut(BaseModel):
    """Resumo da análise disponível para um discurso."""

    id: int
    date: Optional[date_type] = None
    analysis_types: List[str]
    primary_keyword: Optional[KeywordOut] = None
    keywords_count: int
    entities_count: int

    model_config = ConfigDict(from_attributes=True)


class SpeechTermOut(BaseModel):
    """Palavra-chave principal somada sobre os discursos da janela."""

    term: str
    frequency: int
    rank: int
    speeches: int


class ParliamentarianSpeechTermsOut(BaseModel):
    """Temas dos discursos de um parlamentar numa janela de tempo.

    `speeches_count` conta todos os discursos da janela e `speeches_analyzed`
    so os que tem palavra-chave principal: a tela precisa distinguir "nao
    discursou" de "discursou e ainda nao foi analisado".
    """

    window: TermsWindow
    date_from: date_type
    date_to: date_type
    speeches_count: int
    speeches_analyzed: int
    terms: List[SpeechTermOut]


def _terms_window_range(window: TermsWindow) -> tuple[date_type, date_type]:
    """Mesmas janelas do card de estatisticas, para os numeros baterem."""
    if window == "legislature":
        return _current_legislature_range()
    range_start, range_end, _, _ = _last_three_months_range_sao_paulo()
    return range_start, range_end


@router.get("/{speech_id}", response_model=SpeechAnalysisOut)
def get_speech_analysis(
    speech_id: int,
    *,
    db: Session = Depends(get_db),
    analysis_type: Optional[AnalysisType] = Query(
        None,
        description="Filtra os resultados por tipo de análise (spacy ou chatgpt).",
    ),
    limit_keywords: Optional[int] = Query(
        None,
        ge=1,
        le=100,
        description="Limita a quantidade de palavras-chave retornadas.",
    ),
    limit_entities: Optional[int] = Query(
        None,
        ge=1,
        le=100,
        description="Limita a quantidade de entidades retornadas.",
    ),
) -> SpeechAnalysisOut:
    """Retorna as palavras-chave e entidades associadas a um discurso."""
    speech = db.get(SpeechesTranscript, speech_id)
    if speech is None:
        raise HTTPException(status_code=404, detail="Discurso não encontrado.")

    keyword_stmt = (
        select(SpeechesTranscriptsKeyword)
        .where(SpeechesTranscriptsKeyword.speeches_transcripts_id == speech_id)
        .order_by(SpeechesTranscriptsKeyword.rank.asc())
    )
    if analysis_type is not None:
        keyword_stmt = keyword_stmt.where(
            SpeechesTranscriptsKeyword.analysis_type == analysis_type
        )
    if limit_keywords is not None:
        keyword_stmt = keyword_stmt.limit(limit_keywords)
    keywords = db.execute(keyword_stmt).scalars().all()

    entity_stmt = (
        select(SpeechesTranscriptsEntity)
        .where(SpeechesTranscriptsEntity.speeches_transcripts_id == speech_id)
        .order_by(SpeechesTranscriptsEntity.text.asc())
    )
    if analysis_type is not None:
        entity_stmt = entity_stmt.where(
            SpeechesTranscriptsEntity.analysis_type == analysis_type
        )
    if limit_entities is not None:
        entity_stmt = entity_stmt.limit(limit_entities)
    entities = db.execute(entity_stmt).scalars().all()

    return SpeechAnalysisOut(
        speech_id=speech_id,
        keywords=[KeywordOut.model_validate(keyword) for keyword in keywords],
        entities=[EntityOut.model_validate(entity) for entity in entities],
    )


@router.get(
    "/parliamentarian/{code}",
    response_model=List[SpeechAnalysisSummaryOut],
)
def list_parliamentarian_speech_analysis(
    code: int,
    *,
    db: Session = Depends(get_db),
    analysis_type: Optional[AnalysisType] = Query(
        None,
        description="Filtra os resultados por tipo de análise (spacy ou chatgpt).",
    ),
    page: int = Query(1, ge=1, description="Página a ser retornada."),
    page_size: int = Query(
        20,
        ge=1,
        le=100,
        description="Quantidade de discursos por página.",
    ),
    date_from: Optional[date_type] = Query(
        None,
        description="Filtra discursos a partir desta data (inclusive).",
    ),
    date_to: Optional[date_type] = Query(
        None,
        description="Filtra discursos até esta data (inclusive).",
    ),
    sort_order: Literal["asc", "desc"] = Query(
        "desc",
        description="Direção da ordenação temporal dos discursos.",
    ),
) -> List[SpeechAnalysisSummaryOut]:
    """Lista os discursos de um parlamentar com metadados das análises."""
    offset = (page - 1) * page_size

    speech_stmt = (
        select(SpeechesTranscript)
        .join(
            Parliamentarian,
            SpeechesTranscript.parliamentarian_id == Parliamentarian.id,
        )
        .where(Parliamentarian.parliamentarian_code == code)
    )
    if date_from is not None:
        speech_stmt = speech_stmt.where(SpeechesTranscript.date >= date_from)
    if date_to is not None:
        speech_stmt = speech_stmt.where(SpeechesTranscript.date <= date_to)

    if sort_order == "asc":
        speech_stmt = speech_stmt.order_by(SpeechesTranscript.date.asc(), SpeechesTranscript.id.asc())
    else:
        speech_stmt = speech_stmt.order_by(
            SpeechesTranscript.date.desc(),
            SpeechesTranscript.id.desc(),
        )
    speech_stmt = speech_stmt.offset(offset).limit(page_size)

    if analysis_type is not None:
        keyword_exists = (
            select(SpeechesTranscriptsKeyword.id)
            .where(
                SpeechesTranscriptsKeyword.speeches_transcripts_id
                == SpeechesTranscript.id
            )
            .where(SpeechesTranscriptsKeyword.analysis_type == analysis_type)
            .limit(1)
            .correlate(SpeechesTranscript)
            .exists()
        )
        entity_exists = (
            select(SpeechesTranscriptsEntity.id)
            .where(
                SpeechesTranscriptsEntity.speeches_transcripts_id
                == SpeechesTranscript.id
            )
            .where(SpeechesTranscriptsEntity.analysis_type == analysis_type)
            .limit(1)
            .correlate(SpeechesTranscript)
            .exists()
        )
        speech_stmt = speech_stmt.where(or_(keyword_exists, entity_exists))

    speeches = db.execute(speech_stmt).scalars().all()
    if not speeches:
        return []

    speech_ids = [speech.id for speech in speeches]
    summaries: Dict[int, Dict[str, object]] = {
        speech.id: {
            "date": speech.date,
            "analysis_types": set(),  # type: Set[str]
            "primary_candidates": [],
            "keywords_count": 0,
            "entities_count": 0,
        }
        for speech in speeches
    }

    if speech_ids:
        keyword_stmt = select(SpeechesTranscriptsKeyword).where(
            SpeechesTranscriptsKeyword.speeches_transcripts_id.in_(speech_ids)
        )
        if analysis_type is not None:
            keyword_stmt = keyword_stmt.where(
                SpeechesTranscriptsKeyword.analysis_type == analysis_type
            )
        keywords = db.execute(keyword_stmt).scalars().all()

        for keyword in keywords:
            summary = summaries.get(keyword.speeches_transcripts_id)
            if summary is None:
                continue
            summary["analysis_types"].add(keyword.analysis_type)  # type: ignore[attr-defined]
            summary["keywords_count"] = int(summary["keywords_count"]) + 1
            if keyword.is_primary:
                summary["primary_candidates"].append(keyword)

        entity_stmt = select(SpeechesTranscriptsEntity).where(
            SpeechesTranscriptsEntity.speeches_transcripts_id.in_(speech_ids)
        )
        if analysis_type is not None:
            entity_stmt = entity_stmt.where(
                SpeechesTranscriptsEntity.analysis_type == analysis_type
            )
        entities = db.execute(entity_stmt).scalars().all()

        for entity in entities:
            summary = summaries.get(entity.speeches_transcripts_id)
            if summary is None:
                continue
            summary["analysis_types"].add(entity.analysis_type)  # type: ignore[attr-defined]
            summary["entities_count"] = int(summary["entities_count"]) + 1

    response: List[SpeechAnalysisSummaryOut] = []
    for speech in speeches:
        data = summaries[speech.id]
        analysis_types_set: Set[str] = data["analysis_types"]  # type: ignore[assignment]
        primary_candidates: List[SpeechesTranscriptsKeyword] = data[
            "primary_candidates"
        ]
        primary_keyword = None
        if primary_candidates:
            primary_keyword = sorted(
                primary_candidates,
                key=lambda item: item.rank,
            )[0]

        if analysis_type is not None and not analysis_types_set:
            # Pula discursos sem registros do tipo solicitado.
            continue

        response.append(
            SpeechAnalysisSummaryOut(
                id=speech.id,
                date=data["date"],
                analysis_types=sorted(analysis_types_set),
                primary_keyword=(
                    KeywordOut.model_validate(primary_keyword)
                    if primary_keyword is not None
                    else None
                ),
                keywords_count=int(data["keywords_count"]),
                entities_count=int(data["entities_count"]),
            )
        )

    return response


@router.get(
    "/parliamentarian/{code}/terms",
    response_model=ParliamentarianSpeechTermsOut,
)
def list_parliamentarian_speech_terms(
    code: int,
    *,
    db: Session = Depends(get_db),
    window: TermsWindow = Query(
        "legislature",
        description="Janela dos discursos: ultimos 3 meses ou legislatura vigente.",
    ),
    limit: int = Query(
        300,
        ge=1,
        le=1000,
        description="Quantidade maxima de termos, dos mais frequentes para os menos.",
    ),
) -> ParliamentarianSpeechTermsOut:
    """Soma a palavra-chave principal de cada discurso da janela.

    A listagem paginada para em 100 discursos, entao a nuvem nunca via a
    legislatura de quem discursa muito. Aqui a soma e feita no banco e volta
    so o resultado. Os termos voltam crus: stopwords e termos excluidos sao
    aplicados por quem consome, com as listas da tela de configuracoes.
    """
    date_from, date_to = _terms_window_range(window)

    speeches_in_window = (
        select(SpeechesTranscript.id)
        .join(
            Parliamentarian,
            SpeechesTranscript.parliamentarian_id == Parliamentarian.id,
        )
        .where(
            Parliamentarian.parliamentarian_code == code,
            SpeechesTranscript.date >= date_from,
            SpeechesTranscript.date <= date_to,
        )
    )
    speeches_count = int(
        db.execute(
            select(func.count()).select_from(speeches_in_window.subquery())
        ).scalar_one()
    )

    # Mesmo criterio da listagem: entre as palavras marcadas como principais,
    # vale a de menor rank (empate decidido pelo id para ser deterministico).
    primary = (
        select(
            SpeechesTranscriptsKeyword.term,
            SpeechesTranscriptsKeyword.keyword,
            SpeechesTranscriptsKeyword.frequency,
            SpeechesTranscriptsKeyword.rank,
            func.row_number()
            .over(
                partition_by=SpeechesTranscriptsKeyword.speeches_transcripts_id,
                order_by=(
                    SpeechesTranscriptsKeyword.rank.asc(),
                    SpeechesTranscriptsKeyword.id.asc(),
                ),
            )
            .label("position"),
        )
        .where(
            SpeechesTranscriptsKeyword.is_primary.is_(True),
            SpeechesTranscriptsKeyword.speeches_transcripts_id.in_(speeches_in_window),
        )
        .subquery()
    )
    first_primary = select(primary).where(primary.c.position == 1).subquery()

    speeches_analyzed = int(
        db.execute(select(func.count()).select_from(first_primary)).scalar_one()
    )

    # A tela usa `term` e cai para `keyword` quando ele vem vazio.
    term_expr = func.coalesce(func.nullif(first_primary.c.term, ""), first_primary.c.keyword)
    frequency_sum = func.coalesce(func.sum(first_primary.c.frequency), 0)
    rows = db.execute(
        select(
            term_expr.label("term"),
            frequency_sum.label("frequency"),
            func.min(first_primary.c.rank).label("rank"),
            func.count().label("speeches"),
        )
        .group_by(term_expr)
        .order_by(frequency_sum.desc(), func.min(first_primary.c.rank).asc(), term_expr.asc())
        .limit(limit)
    ).all()

    return ParliamentarianSpeechTermsOut(
        window=window,
        date_from=date_from,
        date_to=date_to,
        speeches_count=speeches_count,
        speeches_analyzed=speeches_analyzed,
        terms=[
            SpeechTermOut(
                term=row.term,
                frequency=int(row.frequency),
                rank=int(row.rank),
                speeches=int(row.speeches),
            )
            for row in rows
            if row.term
        ],
    )


__all__ = ["router"]

