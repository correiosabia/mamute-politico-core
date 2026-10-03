"""Coleta e sincroniza pronunciamentos do Senado Federal."""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    TYPE_CHECKING,
    TypedDict,
)
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

try:
    from .speech_text_analysis import (
        PortugueseSpeechAnalyzer,
        analyze_with_chatgpt,
        load_portuguese_analyzer,
    )
except ImportError:  # pragma: no cover - depende de dependências opcionais
    PortugueseSpeechAnalyzer = Any  # type: ignore[assignment]
    analyze_with_chatgpt = None  # type: ignore[assignment]
    load_portuguese_analyzer = None  # type: ignore[assignment]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

LOG_LEVEL_NAMES = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}
LOG_LEVEL_CHOICES = tuple(LOG_LEVEL_NAMES.keys())

_SQLALCHEMY_LOGGER_NAMES = (
    "sqlalchemy.engine",
    "sqlalchemy.engine.Engine",
    "sqlalchemy.pool",
)
_SQL_LOG_LEVEL = logging.WARNING


def _configure_sqlalchemy_logging(level: int) -> None:
    """Define o nível de log para as saídas do SQLAlchemy."""
    global _SQL_LOG_LEVEL
    _SQL_LOG_LEVEL = level
    propagate = level <= logging.INFO

    for name in _SQLALCHEMY_LOGGER_NAMES:
        sqlalchemy_logger = logging.getLogger(name)
        sqlalchemy_logger.setLevel(level)
        sqlalchemy_logger.propagate = propagate


_configure_sqlalchemy_logging(_SQL_LOG_LEVEL)

BASE_URL = "https://www25.senado.leg.br/web/atividade/pronunciamentos/-/p/parlamentar"
BASE_SITE = "https://www25.senado.leg.br"
BASE_SITE_HOST = "www25.senado.leg.br"
SENADO_MATERIA_ENDPOINT = "https://legis.senado.leg.br/dadosabertos/materia"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MamutePronunciamentosBot/1.0)",
}

REQUEST_TIMEOUT = 30
DEFAULT_PAGE_DELAY = 0.1
DEFAULT_DETAIL_DELAY = 0.1

_http_session = requests.Session()
_http_session.headers.update(DEFAULT_HEADERS)

if TYPE_CHECKING:  # pragma: no cover - apenas para tipagem
    from mamute_scrappers.db.models import (
        Parliamentarian as ParliamentarianModel,
        Proposition as PropositionModel,
        SpeechesTranscript as SpeechesTranscriptModel,
        SpeechesTranscriptsEntity as SpeechesTranscriptsEntityModel,
        SpeechesTranscriptsKeyword as SpeechesTranscriptsKeywordModel,
        SpeechesTranscriptsProposition as SpeechesTranscriptsPropositionModel,
    )
    from mamute_scrappers.db.session import session_scope as session_scope_type

    SessionScopeCallable = Callable[[], ContextManager["Session"]]
else:
    ParliamentarianModel = Any
    PropositionModel = Any
    SpeechesTranscriptModel = Any
    SpeechesTranscriptsEntityModel = Any
    SpeechesTranscriptsKeywordModel = Any
    SpeechesTranscriptsPropositionModel = Any
    SessionScopeCallable = Callable[[], ContextManager[Session]]

Parliamentarian: Any = None
Proposition: Any = None
SpeechesTranscript: Any = None
SpeechesTranscriptsKeyword: Any = None
SpeechesTranscriptsEntity: Any = None
SpeechesTranscriptsProposition: Any = None
_SESSION_SCOPE: Optional[SessionScopeCallable] = None

_TEXT_ANALYZER: Optional[PortugueseSpeechAnalyzer] = None
_TEXT_ANALYZER_INITIALIZED = False

class SpeechPayload(TypedDict, total=False):
    parliamentarian_code: int
    date: Optional[date]
    date_raw: Optional[str]
    type: Optional[str]
    summary: Optional[str]
    speech_link: Optional[str]
    speech_text: Optional[str]
    publication_text: Optional[str]
    publication_link: Optional[str]
    session_number: Optional[str]
    hour_minute: Optional[str]
    referenced_propositions: List[int]
    metadata: Dict[str, Any]


SPEECH_MUTABLE_FIELDS = [
    "date",
    "session_number",
    "type",
    "speech_text",
    "summary",
    "hour_minute",
    "publication_link",
    "publication_text",
]


def _ensure_db_dependencies() -> None:
    global Parliamentarian
    global Proposition
    global SpeechesTranscript
    global SpeechesTranscriptsKeyword
    global SpeechesTranscriptsEntity
    global SpeechesTranscriptsProposition
    global _SESSION_SCOPE
    if _SESSION_SCOPE is not None:
        return

    try:
        from mamute_scrappers.db.models import (
            Parliamentarian as ParliamentarianModelRuntime,
            Proposition as PropositionModelRuntime,
            SpeechesTranscript as SpeechesTranscriptModelRuntime,
            SpeechesTranscriptsEntity as SpeechesTranscriptsEntityModelRuntime,
            SpeechesTranscriptsKeyword as SpeechesTranscriptsKeywordModelRuntime,
            SpeechesTranscriptsProposition as SpeechesTranscriptsPropositionModelRuntime,
        )
        from mamute_scrappers.db.session import session_scope as session_scope_runtime
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise RuntimeError("Não foi possível carregar dependências de banco.") from exc

    Parliamentarian = ParliamentarianModelRuntime
    Proposition = PropositionModelRuntime
    SpeechesTranscript = SpeechesTranscriptModelRuntime
    SpeechesTranscriptsKeyword = SpeechesTranscriptsKeywordModelRuntime
    SpeechesTranscriptsEntity = SpeechesTranscriptsEntityModelRuntime
    SpeechesTranscriptsProposition = SpeechesTranscriptsPropositionModelRuntime
    _SESSION_SCOPE = session_scope_runtime

    _configure_sqlalchemy_logging(_SQL_LOG_LEVEL)


def _get_text_analyzer(model: Optional[str] = None) -> Optional[PortugueseSpeechAnalyzer]:
    global _TEXT_ANALYZER, _TEXT_ANALYZER_INITIALIZED

    if model is None and _TEXT_ANALYZER is not None:
        return _TEXT_ANALYZER

    if load_portuguese_analyzer is None:
        if not _TEXT_ANALYZER_INITIALIZED:
            logger.warning(
                "Dependências de NLP (spaCy) não instaladas; análise de palavras-chave será ignorada."
            )
            _TEXT_ANALYZER_INITIALIZED = True
        return None

    try:
        analyzer = load_portuguese_analyzer(model=model)
    except RuntimeError as exc:
        if not _TEXT_ANALYZER_INITIALIZED:
            logger.warning("Análise de discurso indisponível: %s", exc)
            _TEXT_ANALYZER_INITIALIZED = True
        return None

    if model is None:
        _TEXT_ANALYZER = analyzer
        _TEXT_ANALYZER_INITIALIZED = True
    return analyzer


MAX_ANALYSIS_CHARS = 200_000
_BINARY_SAMPLE_CHARS = 5_000
_MIN_READABLE_RATIO = 0.85
_BINARY_MARKERS = ("[Content_Types].xml", "word/document.xml", "%PDF-")
# "?" fica de fora de propósito: é o caractere que substitui os bytes que não
# viraram texto quando um arquivo binário chega como string.
_READABLE_PUNCTUATION = ".,;:!()-\"'%/ºª§$"


def _looks_like_binary(text: str) -> bool:
    """Detecta arquivo binário colado como texto (ex.: .docx inteiro vindo da API da Câmara)."""
    sample = text[:_BINARY_SAMPLE_CHARS]
    if not sample:
        return False
    if any(marker in sample for marker in _BINARY_MARKERS):
        return True
    readable = sum(
        1 for ch in sample if ch.isalnum() or ch.isspace() or ch in _READABLE_PUNCTUATION
    )
    return readable / len(sample) < _MIN_READABLE_RATIO


def _delete_speech_keywords(
    session: Session,
    speech_id: int,
    *,
    analysis_type: str,
) -> None:
    if SpeechesTranscriptsKeyword is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    session.query(SpeechesTranscriptsKeyword).filter(
        SpeechesTranscriptsKeyword.speeches_transcripts_id == speech_id,
        SpeechesTranscriptsKeyword.analysis_type == analysis_type,
    ).delete(synchronize_session=False)


def _delete_speech_entities(
    session: Session,
    speech_id: int,
    *,
    analysis_type: str,
) -> None:
    if SpeechesTranscriptsEntity is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    session.query(SpeechesTranscriptsEntity).filter(
        SpeechesTranscriptsEntity.speeches_transcripts_id == speech_id,
        SpeechesTranscriptsEntity.analysis_type == analysis_type,
    ).delete(synchronize_session=False)


def _persist_speech_keywords(
    session: Session,
    speech_id: int,
    keywords: Sequence[Dict[str, Any]],
    *,
    analysis_type: str,
) -> None:
    if SpeechesTranscriptsKeyword is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    _delete_speech_keywords(session, speech_id, analysis_type=analysis_type)
    for index, data in enumerate(keywords, start=1):
        keyword_value = str(data.get("keyword") or data.get("term") or "").strip().lower()
        term_value = str(data.get("term") or data.get("keyword") or "").strip()
        if not keyword_value or not term_value:
            continue

        frequency_value = _parse_int(data.get("frequency"))
        if frequency_value is None:
            frequency_value = 0

        rank_value = _parse_int(data.get("rank"))
        if rank_value is None or rank_value <= 0:
            rank_value = index

        session.add(
            SpeechesTranscriptsKeyword(
                speeches_transcripts_id=speech_id,
                keyword=keyword_value,
                term=term_value,
                frequency=frequency_value,
                rank=rank_value,
                is_primary=rank_value == 1,
                analysis_type=analysis_type,
            )
        )


def _persist_speech_entities(
    session: Session,
    speech_id: int,
    entities: Sequence[Dict[str, Any]],
    *,
    analysis_type: str,
) -> None:
    if SpeechesTranscriptsEntity is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    _delete_speech_entities(session, speech_id, analysis_type=analysis_type)
    for data in entities:
        text_value = str(data.get("text") or "").strip()
        label_value = str(data.get("label") or "OUTRO").strip().upper()
        if not text_value:
            continue

        start_value = _parse_int(data.get("start_char"))
        end_value = _parse_int(data.get("end_char"))

        session.add(
            SpeechesTranscriptsEntity(
                speeches_transcripts_id=speech_id,
                text=text_value,
                label=label_value or "OUTRO",
                start_char=start_value,
                end_char=end_value,
                analysis_type=analysis_type,
            )
        )


def _update_speech_text_analysis(
    session: Session,
    record: Any,
    *,
    analyzer: Optional[PortugueseSpeechAnalyzer] = None,
    keyword_limit: int = 15,
    analyzer_model: Optional[str] = None,
    analysis_type: str = "spacy",
) -> None:
    normalized_type = (analysis_type or "spacy").strip().lower()

    speech_id = getattr(record, "id", None)
    if speech_id is None:
        session.flush()
        speech_id = getattr(record, "id", None)
    if speech_id is None:
        logger.debug("Registro de pronunciamento ainda sem ID; análise ignorada.")
        return

    speech_text = getattr(record, "speech_text", None)
    if speech_text and _looks_like_binary(speech_text):
        logger.warning("Discurso %s parece conteúdo binário; análise ignorada.", speech_id)
        speech_text = None
    if not speech_text:
        _delete_speech_keywords(session, speech_id, analysis_type=normalized_type)
        _delete_speech_entities(session, speech_id, analysis_type=normalized_type)
        return

    keywords: Sequence[Dict[str, Any]] = []
    entities: Sequence[Dict[str, Any]] = []

    if normalized_type == "spacy":
        if analyzer is None:
            analyzer = _get_text_analyzer(model=analyzer_model)
        if analyzer is None:
            logger.debug(
                "Não foi possível carregar o analisador spaCy; análise ignorada para o discurso %s.",
                speech_id,
            )
            return
        # Há discurso com mais de 1 milhão de caracteres (anexo inteiro como
        # "DISCURSO ENCAMINHADO"), acima do limite do spaCy. Os temas saem do
        # começo do texto; o resto não muda a nuvem.
        analysis_text = speech_text[:MAX_ANALYSIS_CHARS]
        try:
            keywords = analyzer.extract_keywords(analysis_text, limit=keyword_limit)
            entities = analyzer.extract_entities(analysis_text)
        except Exception as exc:  # pragma: no cover - depende do modelo spaCy
            logger.warning(
                "Falha na análise spaCy do discurso %s: %s", speech_id, exc
            )
            return
    elif normalized_type == "chatgpt":
        if analyze_with_chatgpt is None:
            logger.warning(
                "Dependências para ChatGPT não instaladas; análise ignorada para o discurso %s.",
                speech_id,
            )
            return

        try:
            keywords, entities = analyze_with_chatgpt(
                speech_text,
                keyword_limit=keyword_limit,
                model=analyzer_model,
            )
        except Exception as exc:  # pragma: no cover - integrações externas
            logger.warning(
                "Falha ao executar análise via ChatGPT para o discurso %s: %s",
                speech_id,
                exc,
            )
            return
    else:
        logger.warning(
            "Tipo de análise '%s' não suportado; discurso %s não será processado.",
            normalized_type,
            speech_id,
        )
        return

    _persist_speech_keywords(
        session,
        speech_id,
        keywords,
        analysis_type=normalized_type,
    )
    _persist_speech_entities(
        session,
        speech_id,
        entities,
        analysis_type=normalized_type,
    )

    logger.debug(
        "Análise de texto (%s) atualizada para discurso %s (%s palavras-chave, %s entidades).",
        normalized_type,
        speech_id,
        len(keywords),
        len(entities),
    )


def update_speech_text_analysis(
    session: Session,
    record: Any,
    *,
    analyzer: Optional[PortugueseSpeechAnalyzer] = None,
    keyword_limit: int = 15,
    analyzer_model: Optional[str] = None,
    analysis_type: str = "spacy",
) -> None:
    """Executa (ou atualiza) a análise de NLP para um pronunciamento específico."""
    _update_speech_text_analysis(
        session,
        record,
        analyzer=analyzer,
        keyword_limit=keyword_limit,
        analyzer_model=analyzer_model,
        analysis_type=analysis_type,
    )


def rebuild_speech_text_analysis(
    *,
    parliamentarian_code: Optional[int] = None,
    analyzer_model: Optional[str] = None,
    keyword_limit: int = 15,
    batch_size: int = 100,
    limit: Optional[int] = None,
    analysis_type: str = "spacy",
    parliamentarian_type: Optional[str] = None,
    only_missing: bool = False,
) -> None:
    """Reexecuta a análise de NLP em lote para registros já existentes.

    `parliamentarian_type` restringe a uma casa ("Deputado" ou "Senador").
    `only_missing` processa só discursos com texto e sem palavras-chave do
    `analysis_type` informado. Processa do id mais novo para o mais antigo, com
    paginação por id (o conjunto filtrado encolhe durante a execução).
    """
    _ensure_db_dependencies()

    if _SESSION_SCOPE is None or SpeechesTranscript is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    normalized_type = (analysis_type or "spacy").strip().lower()

    analyzer: Optional[PortugueseSpeechAnalyzer] = None
    if normalized_type == "spacy":
        analyzer = _get_text_analyzer(model=analyzer_model)
        if analyzer is None:
            logger.warning(
                "Analisador spaCy indisponível; nenhuma atualização foi realizada."
            )
            return
    elif normalized_type == "chatgpt":
        if analyze_with_chatgpt is None:
            logger.warning(
                "Dependências para ChatGPT não instaladas; nenhuma atualização foi realizada."
            )
            return
    else:
        logger.warning("Tipo de análise '%s' não suportado.", normalized_type)
        return

    processed = 0
    with _SESSION_SCOPE() as session:
        base_query = session.query(SpeechesTranscript.id)
        if parliamentarian_code is not None or parliamentarian_type is not None:
            if Parliamentarian is None:
                raise RuntimeError("Modelo Parliamentarian não carregado.")
            base_query = base_query.join(Parliamentarian)
            if parliamentarian_code is not None:
                base_query = base_query.filter(
                    Parliamentarian.parliamentarian_code == parliamentarian_code
                )
            if parliamentarian_type is not None:
                base_query = base_query.filter(Parliamentarian.type == parliamentarian_type)
        if only_missing:
            has_keywords = (
                session.query(SpeechesTranscriptsKeyword.id)
                .filter(
                    SpeechesTranscriptsKeyword.speeches_transcripts_id
                    == SpeechesTranscript.id,
                    SpeechesTranscriptsKeyword.analysis_type == normalized_type,
                )
                .exists()
            )
            base_query = base_query.filter(
                SpeechesTranscript.speech_text.isnot(None),
                SpeechesTranscript.speech_text != "",
                ~has_keywords,
            )

        last_id: Optional[int] = None
        while True:
            effective_limit = batch_size
            if limit is not None:
                remaining = limit - processed
                if remaining <= 0:
                    break
                effective_limit = min(effective_limit, remaining)

            page_query = base_query
            if last_id is not None:
                page_query = page_query.filter(SpeechesTranscript.id < last_id)
            id_batch = [
                row[0]
                for row in page_query.order_by(SpeechesTranscript.id.desc()).limit(
                    effective_limit
                )
            ]
            if not id_batch:
                break
            last_id = id_batch[-1]

            speeches = (
                session.query(SpeechesTranscript)
                .filter(SpeechesTranscript.id.in_(id_batch))
                .order_by(SpeechesTranscript.id.desc())
                .all()
            )

            for speech in speeches:
                update_speech_text_analysis(
                    session,
                    speech,
                    analyzer=analyzer,
                    keyword_limit=keyword_limit,
                    analyzer_model=analyzer_model,
                    analysis_type=normalized_type,
                )
                processed += 1

            session.commit()
            logger.info("Análise textual: %s pronunciamentos processados.", processed)

    logger.info(
        "Análise textual reconstruída para %s pronunciamentos (analysis_type=%s).",
        processed,
        normalized_type,
    )
def _normalize_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except (TypeError, ValueError):
        logger.debug("Data inválida recebida: %s", value)
        return None


def _normalize_years(years: Optional[Sequence[int]]) -> List[int]:
    if years is None:
        return [date.today().year]

    normalized: List[int] = []
    for year in years:
        if isinstance(year, int):
            normalized.append(year)
            continue
        try:
            normalized.append(int(year))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.warning("Ano inválido ignorado: %s", year)
    if not normalized:
        normalized.append(date.today().year)
    return list(dict.fromkeys(normalized))


def _parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        logger.debug("Data ISO inválida recebida: %s", value)
        return None


def _extract_materia_code(url: Optional[str]) -> Optional[int]:
    if not url:
        return None
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path:
        return None
    last_segment = path.split("/")[-1]
    try:
        return int(last_segment)
    except ValueError:
        return None


def _resolve_senado_site_url(href: Optional[str]) -> Optional[str]:
    if not href:
        return None

    absolute = urljoin(BASE_SITE, href.strip())
    parsed = urlparse(absolute)
    host = parsed.hostname.lower() if parsed.hostname else None
    if parsed.scheme != "https" or host != BASE_SITE_HOST:
        logger.warning("Ignorando link fora do site do Senado: %s", absolute)
        return None
    return absolute


def _build_soup(response: requests.Response) -> BeautifulSoup:
    return BeautifulSoup(response.text, "html.parser")


def _format_parliamentarian_display(name: Optional[str], code: Optional[int]) -> str:
    if name and code is not None:
        return f"{name} ({code})"
    if name:
        return name
    if code is not None:
        return str(code)
    return "Parlamentar"


def _log_page_progress(
    name: Optional[str],
    code: Optional[int],
    year: int,
    page: int,
    items: int,
) -> None:
    logger.info(
        "Senador %s | Ano %s | Página %s | Itens %s",
        _format_parliamentarian_display(name, code),
        year,
        page,
        items,
    )


def _request_get(url: str) -> Optional[requests.Response]:
    try:
        response = _http_session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Falha ao consultar %s: %s", url, exc)
        return None
    return response


def _parse_list_row(tr: Any) -> Optional[SpeechPayload]:
    cells = tr.find_all("td")
    if len(cells) < 5:
        return None

    link_tag = cells[0].find("a")
    link = _resolve_senado_site_url(link_tag.get("href") if link_tag else None)
    if link is None:
        return None

    date_raw = _normalize_text(cells[0].get_text(" ", strip=True))
    payload: SpeechPayload = {
        "date_raw": date_raw,
        "date": _parse_date(date_raw),
        "speech_link": link,
        "type": _normalize_text(cells[1].get_text(" ", strip=True)),
        "summary": _normalize_text(cells[4].get_text(" ", strip=True)),
        "metadata": {
            "house": _normalize_text(cells[2].get_text(" ", strip=True)),
            "party_state": _normalize_text(cells[3].get_text(" ", strip=True)),
        },
    }
    return payload


def _extract_session_description(soup: BeautifulSoup) -> Optional[str]:
    for emphasis in soup.find_all("em"):
        text = emphasis.get_text(" ", strip=True)
        if "Sessão" not in text:
            continue
        session_type_span = emphasis.find("span", string=lambda x: isinstance(x, str) and "Sessão" in x)
        if session_type_span is None:
            continue
        session_type = _normalize_text(session_type_span.get_text(" ", strip=True))
        session_number = None
        for candidate in session_type_span.find_all_previous("span"):
            candidate_text = _normalize_text(candidate.get_text(" ", strip=True))
            if not candidate_text or candidate_text == session_type:
                continue
            if any(char.isdigit() for char in candidate_text):
                session_number = candidate_text
                break
        if session_type:
            return f"{session_number} {session_type}".strip() if session_number else session_type
    return None


def _extract_publication(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
    dt = soup.find("dt", string=lambda x: isinstance(x, str) and "Publicação" in x)
    if dt is None:
        return None, None
    dd = dt.find_next_sibling("dd")
    if dd is None:
        return None, None
    text = _normalize_text(dd.get_text(" ", strip=True))
    link_tag = dd.find("a")
    link = _resolve_senado_site_url(link_tag.get("href") if link_tag else None)
    return text, link


def _extract_hour(soup: BeautifulSoup) -> Optional[str]:
    labels = ("Hora", "Horário", "Hora e Minuto")
    for label in labels:
        dt = soup.find("dt", string=lambda x, lbl=label: isinstance(x, str) and lbl in x)
        if dt is None:
            continue
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue
        return _normalize_text(dd.get_text(" ", strip=True))
    return None


def _extract_referenced_propositions(soup: BeautifulSoup) -> List[int]:
    dt = soup.find("dt", string=lambda x: isinstance(x, str) and "Matérias referenciadas" in x)
    if dt is None:
        return []
    dd = dt.find_next_sibling("dd")
    if dd is None:
        return []

    codes: List[int] = []
    for anchor in dd.find_all("a"):
        href = anchor.get("href")
        absolute = _resolve_senado_site_url(href)
        code = _extract_materia_code(absolute)
        if code is not None and code not in codes:
            codes.append(code)
    return codes


def _fetch_speech_detail(url: str) -> Dict[str, Any]:
    response = _request_get(url)
    if response is None:
        return {}

    soup = _build_soup(response)

    speech_text = None
    texto_div = soup.select_one("div.texto-integral")
    if texto_div is not None:
        speech_text = _normalize_text(texto_div.get_text(" ", strip=True))

    publication_text, publication_link = _extract_publication(soup)
    session_number = _extract_session_description(soup)
    hour_minute = _extract_hour(soup)
    referenced = _extract_referenced_propositions(soup)

    return {
        "speech_text": speech_text,
        "publication_text": publication_text,
        "publication_link": publication_link,
        "session_number": session_number,
        "hour_minute": hour_minute,
        "referenced_propositions": referenced,
    }


def _collect_page_items(
    parliamentarian_code: int,
    year: int,
    page: int,
) -> Optional[List[SpeechPayload]]:
    url = f"{BASE_URL}/{parliamentarian_code}/{year}/{page}"
    logger.debug("Buscando pronunciamentos: parlamentar=%s ano=%s página=%s", parliamentarian_code, year, page)

    response = _request_get(url)
    if response is None:
        return None

    soup = _build_soup(response)
    table = soup.select_one("table.table.table-striped")
    if table is None:
        return []

    rows = table.select("tbody tr")
    if not rows:
        return []

    items: List[SpeechPayload] = []
    for row in rows:
        payload = _parse_list_row(row)
        if payload is None:
            continue
        payload["parliamentarian_code"] = parliamentarian_code
        items.append(payload)
    return items


def _iter_speeches_for_parliamentarian(
    parliamentarian_code: int,
    year: int,
    *,
    page_delay: float,
    detail_delay: float,
    parliamentarian_name: Optional[str],
) -> Iterable[SpeechPayload]:
    page = 1
    while True:
        page_items = _collect_page_items(parliamentarian_code, year, page)
        if page_items is None:
            break
        if not page_items:
            break

        _log_page_progress(parliamentarian_name, parliamentarian_code, year, page, len(page_items))

        for payload in page_items:
            payload["_meta_page"] = page
            payload["_meta_year"] = year
            payload["_meta_parliamentarian_name"] = parliamentarian_name
            payload["_meta_parliamentarian_code"] = parliamentarian_code
            link = payload.get("speech_link")
            if link:
                detail = _fetch_speech_detail(link)
                payload.update(detail)
                if detail_delay > 0:
                    time.sleep(detail_delay)
            yield payload

        page += 1
        if page_delay > 0:
            time.sleep(page_delay)


def _request_materia_json(code: int) -> Optional[Dict[str, Any]]:
    url = f"{SENADO_MATERIA_ENDPOINT}/{code}"
    try:
        response = _http_session.get(
            url,
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Falha ao consultar matéria %s: %s", code, exc)
        return None

    try:
        data = response.json()
    except ValueError as exc:
        logger.error("Erro ao parsear JSON da matéria %s: %s", code, exc)
        return None

    materia = data.get("DetalheMateria", {}).get("Materia")
    if not isinstance(materia, dict):
        logger.warning("Resposta inesperada ao consultar matéria %s.", code)
        return None
    return materia


def _create_proposition_from_api(session: Session, code: int) -> Optional[Any]:
    if Proposition is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    materia = _request_materia_json(code)
    if materia is None:
        return None

    identificacao = materia.get("IdentificacaoMateria") or {}
    dados_basicos = materia.get("DadosBasicosMateria") or {}
    decisao_destino = materia.get("DecisaoEDestino") or {}
    decisao = decisao_destino.get("Decisao") if isinstance(decisao_destino, dict) else {}

    presentation_date = _parse_iso_date(dados_basicos.get("DataApresentacao"))
    presentation_year = _parse_int(identificacao.get("AnoMateria"))
    if presentation_year is None and presentation_date is not None:
        presentation_year = presentation_date.year

    proposition_number = _parse_int(identificacao.get("NumeroMateria"))
    summary = _normalize_text(dados_basicos.get("EmentaMateria"))
    title = _normalize_text(identificacao.get("DescricaoIdentificacaoMateria"))
    acronym = _normalize_text(identificacao.get("SiglaSubtipoMateria"))
    current_status = _normalize_text(decisao.get("Descricao") if isinstance(decisao, dict) else None)

    details: Dict[str, Any] = {}
    if identificacao:
        details["identificacao"] = identificacao
    if dados_basicos:
        details["dados_basicos"] = dados_basicos
    if decisao_destino:
        details["decisao_destino"] = decisao_destino

    record = Proposition(
        proposition_code=code,
        title=title,
        link=f"{BASE_SITE}/web/atividade/materias/-/materia/{code}",
        proposition_acronym=acronym,
        proposition_number=proposition_number,
        presentation_year=presentation_year,
        presentation_date=presentation_date,
        presentation_month=presentation_date.month if presentation_date else None,
        current_status=current_status,
        summary=summary,
        proposition_description=summary,
        details=details or None,
    )
    session.add(record)
    session.flush()

    logger.debug("Proposição %s criada a partir da API do Senado.", code)
    return record


def _iter_target_parliamentarians(
    session: Session,
    parliamentarian_code: Optional[int],
) -> List[Any]:
    if Parliamentarian is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    query = session.query(Parliamentarian)
    if parliamentarian_code is not None:
        query = query.filter(Parliamentarian.parliamentarian_code == parliamentarian_code)

    parliamentarians = [
        row for row in query.all() if getattr(row, "parliamentarian_code", None) is not None
    ]

    if not parliamentarians and parliamentarian_code is not None:
        logger.warning(
            "Nenhum parlamentar encontrado com o código %s.",
            parliamentarian_code,
        )

    return parliamentarians


def _debug_print_payload(payload: SpeechPayload) -> None:
    serializable = dict(payload)
    if isinstance(serializable.get("date"), date):
        serializable["date"] = serializable["date"].isoformat()
    print(json.dumps(serializable, ensure_ascii=False, indent=2, default=str))


def _upsert_speech(
    session: Session,
    parliamentarian_record: Any,
    payload: SpeechPayload,
) -> Any:
    if SpeechesTranscript is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    speech_link = payload.get("speech_link")
    record = None
    if speech_link:
        record = (
            session.query(SpeechesTranscript)
            .filter(SpeechesTranscript.speech_link == speech_link)
            .one_or_none()
        )

    created = record is None

    if record is None:
        record = SpeechesTranscript(
            parliamentarian_id=parliamentarian_record.id,
            speech_link=speech_link,
        )
        session.add(record)
        session.flush()
    else:
        if getattr(record, "parliamentarian_id", None) != parliamentarian_record.id:
            record.parliamentarian = parliamentarian_record

    for field in SPEECH_MUTABLE_FIELDS:
        value = payload.get(field)
        setattr(record, field, value)

    if created:
        meta_page = payload.get("_meta_page")
        meta_year = payload.get("_meta_year")
        logger.info(
            "Inserido pronunciamento | Senador %s | Ano %s | Página %s | Referência %s",
            _format_parliamentarian_display(
                getattr(parliamentarian_record, "name", None),
                getattr(parliamentarian_record, "parliamentarian_code", None),
            ),
            meta_year,
            meta_page,
            speech_link or record.id,
        )
    return record


def _sync_referenced_propositions(
    session: Session,
    record: Any,
    proposition_codes: Iterable[int],
) -> None:
    if SpeechesTranscriptsProposition is None or Proposition is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    if getattr(record, "id", None) is None:
        session.flush()

    target_codes = {code for code in proposition_codes if isinstance(code, int)}
    if not target_codes:
        existing_links = (
            session.query(SpeechesTranscriptsProposition)
            .filter(SpeechesTranscriptsProposition.speeches_transcripts_id == record.id)
            .all()
        )
        for link in existing_links:
            session.delete(link)
        return

    existing_links = (
        session.query(SpeechesTranscriptsProposition)
        .join(Proposition)
        .filter(SpeechesTranscriptsProposition.speeches_transcripts_id == record.id)
        .all()
    )

    existing_codes = {
        getattr(link.proposition, "proposition_code", None): link
        for link in existing_links
        if link.proposition is not None
    }

    for code, link in list(existing_codes.items()):
        if code not in target_codes:
            session.delete(link)

    for link in existing_links:
        if getattr(link.proposition, "proposition_code", None) is None:
            session.delete(link)

    for code in sorted(target_codes):
        if code in existing_codes:
            continue

        proposition = (
            session.query(Proposition)
            .filter(Proposition.proposition_code == code)
            .one_or_none()
        )
        if proposition is None:
            proposition = _create_proposition_from_api(session, code)

        if proposition is None:
            logger.debug(
                "Não foi possível localizar ou criar a proposição %s referenciada no pronunciamento %s.",
                code,
                record.id,
            )
            continue

        session.add(
            SpeechesTranscriptsProposition(
                speech=record,
                proposition=proposition,
            )
        )


def speechs_transcipts(
    *,
    years: Optional[Sequence[int]] = None,
    parliamentarian_code: Optional[int] = None,
    persist: bool = True,
    interactive: bool = False,
    page_delay: float = DEFAULT_PAGE_DELAY,
    detail_delay: float = DEFAULT_DETAIL_DELAY,
) -> None:
    """Coleta pronunciamentos do Senado e opcionalmente persiste no banco."""
    target_years = _normalize_years(years)
    logger.debug(
        "Iniciando sincronização de pronunciamentos (persist=%s, interactive=%s, anos=%s).",
        persist,
        interactive,
        ", ".join(str(year) for year in target_years),
    )

    _ensure_db_dependencies()
    if _SESSION_SCOPE is None:
        raise RuntimeError("Função de sessão do banco não carregada.")

    total_processed = 0
    total_persisted = 0

    try:
        with _SESSION_SCOPE() as session:
            parliamentarians = _iter_target_parliamentarians(session, parliamentarian_code)
            if not parliamentarians:
                logger.warning("Nenhum parlamentar disponível para sincronizar pronunciamentos.")
                return

            for parliamentarian_record in parliamentarians:
                code = getattr(parliamentarian_record, "parliamentarian_code", None)
                if not isinstance(code, int):
                    logger.debug(
                        "Parlamentar %s sem código numérico válido; registro ignorado.",
                        getattr(parliamentarian_record, "name", None),
                    )
                    continue

                for year in target_years:
                    logger.info(
                        "Senador %s | Iniciando ano %s",
                        _format_parliamentarian_display(
                            getattr(parliamentarian_record, "name", None),
                            code,
                        ),
                        year,
                    )
                    year_processed = 0
                    for payload in _iter_speeches_for_parliamentarian(
                        code,
                        year,
                        page_delay=page_delay,
                        detail_delay=detail_delay,
                        parliamentarian_name=getattr(parliamentarian_record, "name", None),
                    ):
                        year_processed += 1
                        total_processed += 1

                        if not persist or interactive:
                            _debug_print_payload(payload)
                            if interactive:
                                try:
                                    input("Pressione ENTER para continuar; Ctrl+C para sair.")
                                except (KeyboardInterrupt, EOFError):
                                    logger.info("Execução interrompida pelo usuário.")
                                    return

                        if persist:
                            record = _upsert_speech(session, parliamentarian_record, payload)
                            _sync_referenced_propositions(
                                session,
                                record,
                                payload.get("referenced_propositions", []),
                            )
                            update_speech_text_analysis(session, record)
                            total_persisted += 1

                    if year_processed == 0:
                        logger.debug(
                            "Nenhum pronunciamento encontrado para o parlamentar %s no ano %s.",
                            code,
                            year,
                        )
                    else:
                        logger.info(
                            "Ano %s processado (%s pronunciamentos) para o parlamentar %s.",
                            year,
                            year_processed,
                            code,
                        )
    except KeyboardInterrupt:
        logger.info("Execução interrompida pelo usuário.")
        return

    if persist:
        logger.debug(
            "Sincronização concluída (%s pronunciamentos processados, %s registros persistidos).",
            total_processed,
            total_persisted,
        )
    else:
        logger.debug(
            "Execução concluída em modo dry-run (%s pronunciamentos processados).",
            total_processed,
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sincroniza pronunciamentos de parlamentares do Senado.")
    parser.add_argument(
        "--year",
        action="append",
        type=int,
        dest="years",
        help="Ano a ser sincronizado (pode ser informado várias vezes).",
    )
    parser.add_argument(
        "--parliamentarian",
        type=int,
        dest="parliamentarian_code",
        help="Código do parlamentar no Senado.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Não persiste no banco; apenas exibe os registros.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Pausa entre cada registro para inspeção manual.",
    )
    parser.add_argument(
        "--page-delay",
        type=float,
        default=DEFAULT_PAGE_DELAY,
        help="Intervalo em segundos entre requisições de páginas.",
    )
    parser.add_argument(
        "--detail-delay",
        type=float,
        default=DEFAULT_DETAIL_DELAY,
        help="Intervalo em segundos entre requisições de detalhes.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=LOG_LEVEL_CHOICES,
        help="Define o nível de log geral (padrão: INFO).",
    )
    parser.add_argument(
        "--sql-log-level",
        default="WARNING",
        choices=LOG_LEVEL_CHOICES,
        help="Define o nível de log específico do SQLAlchemy (padrão: WARNING).",
    )

    args = parser.parse_args()
    root_log_level = LOG_LEVEL_NAMES[args.log_level]
    sqlalchemy_log_level = LOG_LEVEL_NAMES[args.sql_log_level]

    logging.basicConfig(level=root_log_level)
    _configure_sqlalchemy_logging(sqlalchemy_log_level)
    logger.debug(
        "Configuração de log aplicada (geral=%s, sqlalchemy=%s).",
        args.log_level,
        args.sql_log_level,
    )

    speechs_transcipts(
        years=args.years,
        parliamentarian_code=args.parliamentarian_code,
        persist=not args.dry_run,
        interactive=args.interactive,
        page_delay=args.page_delay,
        detail_delay=args.detail_delay,
    )
