"""Raspador de proposições de parlamentares da Câmara dos Deputados (API REST)."""

from __future__ import annotations

import re
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    TYPE_CHECKING,
    TypedDict,
)

import requests
from sqlalchemy import func
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

CAMARA_API_BASE_URL = "https://dadosabertos.camara.leg.br/api/v2"
CAMARA_PROPOSICOES_ENDPOINT = f"{CAMARA_API_BASE_URL}/proposicoes"
CAMARA_PROPOSICAO_WEB_URL = "https://www.camara.leg.br/proposicoesWeb/fichadetramitacao"

REQUEST_DELAY = 0.1  # Delay entre requests de detalhes (segundos)
PAGE_SIZE = 100
HTTP_TIMEOUT_SECONDS = 30
REQUEST_MAX_RETRIES = 4
REQUEST_RETRY_BASE_DELAY = 2.0

if TYPE_CHECKING:  # pragma: no cover - apenas para tipagem
    from mamute_scrappers.db.models import (
        Agency as AgencyModel,
        AuthorsProposition as AuthorsPropositionModel,
        Parliamentarian as ParliamentarianModel,
        Proposition as PropositionModel,
        PropositionStatus as PropositionStatusModel,
        PropositionType as PropositionTypeModel,
    )
    from mamute_scrappers.db.session import session_scope as session_scope_type

    SessionScopeCallable = Callable[[], ContextManager["Session"]]
else:
    AgencyModel = Any
    AuthorsPropositionModel = Any
    ParliamentarianModel = Any
    PropositionModel = Any
    PropositionStatusModel = Any
    PropositionTypeModel = Any
    SessionScopeCallable = Callable[[], ContextManager[Session]]

Proposition: Any = None
AuthorsProposition: Any = None
Parliamentarian: Any = None
PropositionType: Any = None
PropositionStatus: Any = None
Agency: Any = None
_SESSION_SCOPE: Optional[SessionScopeCallable] = None


class PropositionPayload(TypedDict, total=False):
    proposition_code: int
    title: Optional[str]
    link: Optional[str]
    proposition_acronym: Optional[str]
    proposition_number: Optional[int]
    presentation_year: Optional[int]
    presentation_date: Optional[date]
    presentation_month: Optional[int]
    current_status: Optional[str]
    summary: Optional[str]
    proposition_description: Optional[str]
    details: Dict[str, Any]
    agency_code: Optional[int]
    proposition_type_code: Optional[str]
    author_codes: List[int]
    themes: List[str]


AUTO_SYNC_MISSING_AUTHORS_DEFAULT = False


PROPOSITION_MUTABLE_FIELDS = [
    "title",
    "link",
    "proposition_acronym",
    "proposition_number",
    "presentation_year",
    "presentation_date",
    "presentation_month",
    "current_status",
    "summary",
    "proposition_description",
    "details",
    # Só entra no payload quando a consulta de temas deu certo — falha de rede
    # não apaga tema já coletado.
    "themes",
]


def _ensure_db_dependencies() -> None:
    global Proposition, AuthorsProposition, Parliamentarian, PropositionType, PropositionStatus, Agency, _SESSION_SCOPE
    if _SESSION_SCOPE is not None:
        return

    try:
        from mamute_scrappers.db.models import (
            Agency as AgencyModelRuntime,
            AuthorsProposition as AuthorsPropositionModelRuntime,
            Parliamentarian as ParliamentarianModelRuntime,
            Proposition as PropositionModelRuntime,
            PropositionStatus as PropositionStatusModelRuntime,
            PropositionType as PropositionTypeModelRuntime,
        )
        from mamute_scrappers.db.session import session_scope as session_scope_runtime
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise RuntimeError("Não foi possível carregar dependências de banco.") from exc

    Proposition = PropositionModelRuntime
    AuthorsProposition = AuthorsPropositionModelRuntime
    Parliamentarian = ParliamentarianModelRuntime
    PropositionType = PropositionTypeModelRuntime
    PropositionStatus = PropositionStatusModelRuntime
    Agency = AgencyModelRuntime
    _SESSION_SCOPE = session_scope_runtime


def _normalize_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _coerce_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return _normalize_text(value)
    return _normalize_text(str(value))


def _parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> Optional[date]:
    text = _coerce_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _request_json(url: str, *, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    headers = {"Accept": "application/json"}
    for attempt in range(1, REQUEST_MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            is_retryable = (
                isinstance(
                    exc,
                    (
                        requests.Timeout,
                        requests.ConnectionError,
                    ),
                )
                or status_code == 429
                or (status_code is not None and 500 <= status_code < 600)
            )
            if is_retryable and attempt < REQUEST_MAX_RETRIES:
                delay_seconds = REQUEST_RETRY_BASE_DELAY * attempt
                logger.warning(
                    "Falha temporária ao consultar %s (tentativa %s/%s, status=%s). "
                    "Aguardando %.1fs para tentar novamente.",
                    url,
                    attempt,
                    REQUEST_MAX_RETRIES,
                    status_code,
                    delay_seconds,
                )
                time.sleep(delay_seconds)
                continue

            logger.error("Falha ao consultar %s: %s", url, exc)
            return None

    try:
        data = response.json()
    except ValueError as exc:
        logger.error("Erro ao parsear JSON da Câmara (%s): %s", url, exc)
        return None

    if not isinstance(data, dict):
        logger.error("Resposta JSON inesperada da Câmara (%s): tipo %s", url, type(data))
        return None

    return data


def _get_last_proposition_date(session: Session, year_start: int) -> Optional[date]:
    """Busca a data da última proposição salva no banco."""
    if Proposition is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    last_proposition = (
        session.query(Proposition)
        .filter(Proposition.presentation_year >= year_start)
        .filter(Proposition.presentation_date.isnot(None))
        .order_by(Proposition.presentation_date.desc())
        .first()
    )

    if last_proposition and last_proposition.presentation_date:
        return last_proposition.presentation_date

    return None


def _fetch_proposition_detail(proposition_id: int) -> Optional[Dict[str, Any]]:
    """Busca detalhes completos de uma proposição específica."""
    url = f"{CAMARA_PROPOSICOES_ENDPOINT}/{proposition_id}"
    
    logger.debug("Buscando detalhes da proposição %s", proposition_id)
    data = _request_json(url)
    
    if data is None:
        return None
    
    dados = data.get("dados")
    if not isinstance(dados, dict):
        logger.error("Resposta sem campo 'dados' para proposição %s", proposition_id)
        return None
    
    return dados

def _fetch_proposition_authors(proposition_id: int) -> List[int]:
    """Busca autores de uma proposição específica."""
    url = f"{CAMARA_PROPOSICOES_ENDPOINT}/{proposition_id}/autores"
    
    logger.debug("Buscando autores da proposição %s", proposition_id)
    data = _request_json(url)
    
    if data is None:
        return []
    
    dados = data.get("dados")
    if not isinstance(dados, list):
        return []
    
    author_codes = []
    for autor in dados:
        if not isinstance(autor, dict):
            continue
        
        # Extrair ID da URI (ex: "https://.../deputados/220715" -> 220715)
        uri = autor.get("uri")
        if isinstance(uri, str):
            segments = uri.rstrip("/").split("/")
            if segments:
                try:
                    author_id = int(segments[-1])
                    if author_id not in author_codes:
                        author_codes.append(author_id)
                except (ValueError, TypeError):
                    logger.debug("Falha ao extrair ID do autor da URI: %s", uri)
    
    return author_codes


def parse_camara_themes(data: Any) -> Optional[List[str]]:
    """Extrai os nomes das áreas temáticas da resposta de /proposicoes/{id}/temas.

    Devolve None quando a resposta não tem o formato esperado (tratado como
    "não coletado") e [] quando a Câmara não classificou a proposição.
    """
    if not isinstance(data, dict):
        return None
    dados = data.get("dados")
    if not isinstance(dados, list):
        return None

    themes: List[str] = []
    for item in dados:
        if not isinstance(item, dict):
            continue
        name = _coerce_text(item.get("tema"))
        if name and name not in themes:
            themes.append(name)
    return themes


def _fetch_proposition_themes(proposition_id: int) -> Optional[List[str]]:
    """Busca as áreas temáticas oficiais de uma proposição (None se a consulta falhar)."""
    url = f"{CAMARA_PROPOSICOES_ENDPOINT}/{proposition_id}/temas"
    logger.debug("Buscando temas da proposição %s", proposition_id)
    return parse_camara_themes(_request_json(url))


def _fetch_deputado_detail(deputado_id: int) -> Optional[Dict[str, Any]]:
    """Busca detalhes de um deputado específico na API da Câmara."""
    url = f"{CAMARA_API_BASE_URL}/deputados/{deputado_id}"
    data = _request_json(url)
    if data is None:
        return None
    dados = data.get("dados")
    if not isinstance(dados, dict):
        return None
    return dados


def _ensure_author_parliamentarian(session: Session, author_code: int) -> Optional[Any]:
    """Garante que o autor exista em parliamentarian; tenta sincronizar da API quando faltar."""
    if Parliamentarian is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    parliamentarian = (
        session.query(Parliamentarian)
        .filter_by(parliamentarian_code=author_code)
        .one_or_none()
    )
    if parliamentarian is not None:
        return parliamentarian

    missing_cache = session.info.setdefault("missing_author_codes", set())
    synced_cache = session.info.setdefault("synced_author_codes", set())
    if author_code in missing_cache:
        return None
    if author_code in synced_cache:
        return (
            session.query(Parliamentarian)
            .filter_by(parliamentarian_code=author_code)
            .one_or_none()
        )

    detail = _fetch_deputado_detail(author_code)
    if detail is None:
        missing_cache.add(author_code)
        logger.info(
            "Autor %s não está disponível no endpoint de deputados da Câmara; "
            "novas tentativas automáticas serão ignoradas nesta execução.",
            author_code,
        )
        return None

    ultimo_status = detail.get("ultimoStatus")
    if not isinstance(ultimo_status, dict):
        ultimo_status = {}

    parliamentarian = Parliamentarian(
        type="Deputado",
        parliamentarian_code=author_code,
        name=_coerce_text(ultimo_status.get("nome")) or _coerce_text(detail.get("nomeCivil")),
        full_name=_coerce_text(detail.get("nomeCivil")),
        email=_coerce_text(ultimo_status.get("email")),
        status=_coerce_text(ultimo_status.get("situacao")),
        party=_coerce_text(ultimo_status.get("siglaPartido")),
        state_of_birth=_coerce_text(detail.get("ufNascimento")),
        city_of_birth=_coerce_text(detail.get("municipioNascimento")),
        state_elected=_coerce_text(ultimo_status.get("siglaUf")),
        site=_coerce_text(detail.get("urlWebsite")),
        details=detail,
    )
    session.add(parliamentarian)
    session.flush()
    synced_cache.add(author_code)
    logger.info("Autor %s sincronizado automaticamente via API da Câmara", author_code)
    return parliamentarian

def _iter_propositions_paginated(
    year_start: int,
    force_full: bool = False,
    data_inicio: Optional[str] = None,
    pagina_inicial: int = 1,
    data_fim: Optional[str] = None,
    session: Optional[Session] = None,
) -> Generator[Tuple[Dict[str, Any], bool], None, None]:
    """Itera sobre proposições da Câmara com paginação automática.

    Usa `dataApresentacaoInicio`/`dataApresentacaoFim` (data de APRESENTAÇÃO)
    em combinação com `ano=year_start` para que cada chunk capture exatamente
    as proposições apresentadas no intervalo — sem o vazamento histórico que
    `dataInicio/Fim` (data de TRAMITAÇÃO) gerava: chunks semanais antes pegavam
    ~3.800 itens (de muitos anos); agora pegam ~720 (estritamente apresentados
    naquela semana).

    Args:
        year_start: Ano de apresentação a buscar (passado também como `ano=` na API).
        force_full: Se True, ignora busca incremental.
        data_inicio: Data de apresentação inicial YYYY-MM-DD (sobrescreve incremental).
        pagina_inicial: Página inicial da paginação (padrão: 1).
        data_fim: Data de apresentação final YYYY-MM-DD (limita o período — útil
            para backfill em fatias semanais).
        session: Sessão do banco (para busca incremental).

    Yields:
        Tupla (dados_basicos, is_new)
    """
    # Determinar data inicial
    start_date = None

    if data_inicio:
        # Prioridade 1: Data fornecida como parâmetro
        start_date = data_inicio
        logger.info("Usando data de apresentação inicial fornecida: %s", start_date)
    elif not force_full and session:
        # Prioridade 2: Busca incremental (última data no banco)
        last_date = _get_last_proposition_date(session, year_start)
        if last_date:
            next_day = last_date + timedelta(days=1)
            start_date = next_day.strftime("%Y-%m-%d")
            logger.info("Busca incremental desde: %s", start_date)

    # Se não tem data inicial, usa início do ano
    if not start_date:
        start_date = f"{year_start}-01-01"
        logger.info("Buscando desde início do ano: %s", start_date)

    # Parâmetros base da API: `ano` + `dataApresentacaoInicio/Fim` filtram por
    # data de APRESENTAÇÃO (semântica que queremos). O `ano` é redundante quando
    # o intervalo está estritamente dentro de um ano, mas blindagem contra
    # respostas que ignoram o filtro de data em alguns tipos de proposição.
    base_params = {
        "ano": year_start,
        "dataApresentacaoInicio": start_date,
        "ordem": "DESC",
        "ordenarPor": "id",
        "itens": PAGE_SIZE,
    }
    if data_fim:
        base_params["dataApresentacaoFim"] = data_fim
        logger.info("Limitando período até: %s", data_fim)
    
    page = max(1, pagina_inicial)
    total_pages = None
    total_items = None
    
    while True:
        params = base_params.copy()
        params["pagina"] = page
        
        logger.info(
            "Buscando proposições: página %s%s (desde %s)",
            page,
            f"/{total_pages}" if total_pages else "",
            start_date,
        )
        
        # Fazer request para API
        data = _request_json(
            url=f"{CAMARA_PROPOSICOES_ENDPOINT}",
            params=params,
        )
        
        if data is None:
            logger.warning("Nenhum dado retornado na página %s", page)
            break
        
        # Extrair informações de paginação
        items = data.get("dados", [])
        links = data.get("links", [])
        
        # Na primeira resposta processada, extrair total de páginas
        if total_pages is None:
            for link in links:
                if link.get("rel") == "last":
                    href = link.get("href", "")
                    match = re.search(r"pagina=(\d+)", href)
                    if match:
                        total_pages = int(match.group(1))
                        total_items = total_pages * PAGE_SIZE
                        logger.info(
                            "Total estimado: ~%s proposições em %s páginas",
                            total_items,
                            total_pages,
                        )
        
        if not items:
            logger.info("Nenhuma proposição encontrada na página %s", page)
            break
        
        # Yield cada proposição
        for item in items:
            yield item, True  # True = pode ser nova (não verificado aqui)
        
        # Verificar se tem próxima página
        has_next = any(link.get("rel") == "next" for link in links)
        if not has_next:
            logger.info("Última página alcançada (%s)", page)
            break
        
        page += 1
        
        # Delay entre páginas
        if REQUEST_DELAY > 0:
            time.sleep(REQUEST_DELAY)


def _build_payload_from_data(
    basic_data: Dict[str, Any],
    detail_data: Optional[Dict[str, Any]],
    author_codes: Optional[List[int]] = None,
    themes: Optional[List[str]] = None,
) -> Optional[PropositionPayload]:
    """Constrói payload interno a partir dos dados básicos e detalhados."""

    proposition_id = _parse_int(basic_data.get("id"))
    if proposition_id is None:
        logger.debug("Proposição sem ID; pulando registro")
        return None

    # Dados básicos (sempre disponíveis)
    presentation_date = _parse_date(basic_data.get("dataApresentacao"))
    presentation_year = _parse_int(basic_data.get("ano"))

    # A API da Câmara devolve "ano": 0 (não null) em vários tipos de tramitação
    # (PRL, RPD, PAR, SBT, ...). Tratamos 0 como ausente e derivamos o ano da
    # data de apresentação, que nesses casos vem preenchida.
    if not presentation_year and presentation_date is not None:
        presentation_year = presentation_date.year

    # Código do tipo de proposição
    proposition_type_code_raw = basic_data.get("codTipo")
    proposition_type_code = None
    if isinstance(proposition_type_code_raw, int):
        proposition_type_code = str(proposition_type_code_raw)
    elif isinstance(proposition_type_code_raw, str) and proposition_type_code_raw.strip():
        proposition_type_code = proposition_type_code_raw.strip()

    payload: PropositionPayload = {
        "proposition_code": proposition_id,
        "title": _coerce_text(basic_data.get("ementa")),  # Na lista, ementa é resumida
        "link": f"{CAMARA_PROPOSICAO_WEB_URL}?idProposicao={proposition_id}",
        "proposition_acronym": _coerce_text(basic_data.get("siglaTipo")),
        "proposition_number": _parse_int(basic_data.get("numero")),
        "presentation_year": presentation_year,
        "presentation_date": presentation_date,
        "presentation_month": presentation_date.month if presentation_date else None,
        "summary": _coerce_text(basic_data.get("ementa")),
        "details": basic_data,  # Começa com dados básicos
        "proposition_type_code": proposition_type_code,
        "author_codes": [],
        "agency_code": None,
        "current_status": None,
        "proposition_description": None,
    }

    # Enriquecer com dados detalhados (se disponíveis)
    if detail_data is not None:
        payload["details"] = detail_data  # Substituir com dados completos

        # Ementa completa
        ementa_completa = _coerce_text(detail_data.get("ementa"))
        if ementa_completa:
            payload["summary"] = ementa_completa
            payload["title"] = ementa_completa

        # Descrição do tipo
        payload["proposition_description"] = _coerce_text(detail_data.get("descricaoTipo"))

        # Status atual
        status_proposicao = detail_data.get("statusProposicao")
        if isinstance(status_proposicao, dict):
            payload["current_status"] = _coerce_text(
                status_proposicao.get("descricaoSituacao")
                or status_proposicao.get("despacho")
            )

        # Órgão do trâmite. A API da Câmara não expõe `idOrgaoOrigem`; o órgão
        # disponível vem em statusProposicao.uriOrgao (.../orgaos/{id}), cujo id
        # casa com agency.agency_code.
        agency_code = None
        if isinstance(status_proposicao, dict):
            uri_orgao = status_proposicao.get("uriOrgao")
            if uri_orgao:
                match = re.search(r"/orgaos/(\d+)", uri_orgao)
                if match:
                    agency_code = int(match.group(1))
        payload["agency_code"] = agency_code

        payload["author_codes"] = author_codes if author_codes is not None else []

    if themes is not None:
        payload["themes"] = themes

    return payload


def _debug_print_payload(payload: PropositionPayload) -> None:
    debug_repr = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    print(debug_repr)


def _proposition_exists(session: Session, proposition_code: int) -> bool:
    """Verifica se proposição já existe no banco."""
    if Proposition is None:
        raise RuntimeError("Dependências de banco não carregadas.")
    
    return (
        session.query(Proposition)
        .filter_by(proposition_code=proposition_code)
        .count()
    ) > 0


def _upsert_proposition(
    session: Session,
    payload: PropositionPayload,
    *,
    auto_sync_missing_authors: bool = AUTO_SYNC_MISSING_AUTHORS_DEFAULT,
) -> Tuple[Any, bool]:
    """Atualiza ou cria uma proposição conforme o payload informado.
    
    Returns:
        Tuple[record, created]: registro e flag indicando se foi criado
    """
    code = payload.get("proposition_code")
    if code is None:
        raise ValueError("Payload de proposição sem 'proposition_code'.")

    if Proposition is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    record: Any = session.query(Proposition).filter_by(proposition_code=code).one_or_none()
    created = False
    if record is None:
        record = Proposition(proposition_code=code)
        session.add(record)
        created = True

    for field in PROPOSITION_MUTABLE_FIELDS:
        if field in payload:
            setattr(record, field, payload[field])

    session.flush()  # Garante ID para relacionamentos

    _assign_type(session, record, payload)
    _assign_agency(session, record, payload)
    _sync_authors(
        session,
        record,
        payload.get("author_codes", []),
        auto_sync_missing_authors=auto_sync_missing_authors,
    )
    _assign_status(session, record, payload)

    return record, created


def _assign_type(session: Session, record: Any, payload: PropositionPayload) -> None:
    """Atribui tipo de proposição (FK)."""
    if PropositionType is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    proposition_type_code = payload.get("proposition_type_code")
    if not proposition_type_code:
        return

    type_record = (
        session.query(PropositionType)
        .filter_by(proposition_type_code=proposition_type_code, type="Camara")
        .order_by(PropositionType.id.asc())
        .first()
    )

    if type_record is None:
        logger.warning(
            "Tipo de proposição %s não encontrado no banco (proposição %s)",
            proposition_type_code,
            payload.get("proposition_code")
        )
        return

    record.proposition_type = type_record


def _assign_agency(session: Session, record: Any, payload: PropositionPayload) -> None:
    """Atribui órgão de origem (FK, pode ser NULL)."""
    if Agency is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    agency_code = payload.get("agency_code")
    if agency_code is None:
        record.agency = None
        return

    agency_record = session.query(Agency).filter_by(agency_code=agency_code).one_or_none()

    if agency_record is None:
        logger.debug(
            "Órgão %s não encontrado no banco (proposição %s)",
            agency_code,
            payload.get("proposition_code")
        )
        record.agency = None
        return

    record.agency = agency_record

def _assign_status(session: Session, record: Any, payload: PropositionPayload) -> None:
    """Atribui status de proposição (FK), criando automaticamente se não existir."""
    if PropositionStatus is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    current_status_text = payload.get("current_status")
    if not current_status_text:
        record.proposition_status = None
        return

    # Buscar status existente
    status_record = (
        session.query(PropositionStatus)
        .filter_by(description=current_status_text)
        .one_or_none()
    )

    # Se não existe, criar automaticamente
    if status_record is None:
        # Gerar código único baseado no hash do texto
        import hashlib
        status_code = f"CAM-{hashlib.md5(current_status_text.encode()).hexdigest()[:8]}"
        
        status_record = PropositionStatus(
            proposition_status_code=status_code,
            description=current_status_text,
            name=current_status_text[:100] if len(current_status_text) > 100 else current_status_text,
            acronym=None,
        )
        session.add(status_record)
        session.flush()
        
        logger.info("Status criado automaticamente: %s (Câmara)", current_status_text)

    record.proposition_status = status_record


def _sync_authors(
    session: Session,
    record: Any,
    author_codes: List[int],
    *,
    auto_sync_missing_authors: bool = AUTO_SYNC_MISSING_AUTHORS_DEFAULT,
) -> None:
    """Sincroniza autores (M2M via authors_proposition)."""
    if Parliamentarian is None or AuthorsProposition is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    if not author_codes:
        return

    # Buscar autores existentes vinculados
    existing_links = (
        session.query(AuthorsProposition)
        .filter(AuthorsProposition.proposition_id == record.id)
        .all()
    )

    existing_codes = {
        link.parliamentarian.parliamentarian_code
        for link in existing_links
        if link.parliamentarian and link.parliamentarian.parliamentarian_code is not None
    }

    # Adicionar novos vínculos
    for code in author_codes:
        if code in existing_codes:
            continue

        parliamentarian = (
            _ensure_author_parliamentarian(session, code)
            if auto_sync_missing_authors
            else session.query(Parliamentarian).filter_by(parliamentarian_code=code).one_or_none()
        )

        if parliamentarian is None:
            logger.warning(
                "Autor %s não encontrado no banco (proposição %s)",
                code,
                record.proposition_code
            )
            continue

        session.add(
            AuthorsProposition(
                parliamentarian=parliamentarian,
                proposition=record,
            )
        )


def proposition(
    *,
    year_start: int = 2025,
    force_full: bool = False,
    data_inicio: Optional[str] = None,
    pagina_inicial: int = 1,
    data_fim: Optional[str] = None,
    auto_sync_missing_authors: bool = AUTO_SYNC_MISSING_AUTHORS_DEFAULT,
    persist: bool = True,
    interactive: bool = False,
) -> None:
    """Busca proposições da Câmara e opcionalmente salva/atualiza o banco.

    Args:
        year_start: Ano inicial para buscar proposições (padrão: 2025)
        force_full: Ignora busca incremental e reprocessa tudo
        data_inicio: Data inicial no formato YYYY-MM-DD (default: últimos 30 dias)
        pagina_inicial: Página inicial para retomada da paginação (padrão: 1)
        data_fim: Data final YYYY-MM-DD — limita o período (backfill em fatias)
        auto_sync_missing_authors: Busca/cadastra autores ausentes automaticamente
        persist: Se False, apenas exibe payloads (dry-run)
        interactive: Pausa após cada proposição
    """
    logger.info(
        "Iniciando sincronização de proposições da Câmara "
        "(persist=%s, year=%s, force_full=%s, data_inicio=%s, data_fim=%s)",
        persist,
        year_start,
        force_full,
        data_inicio,
        data_fim,
    )

    _ensure_db_dependencies()
    if _SESSION_SCOPE is None:
        raise RuntimeError("Função de sessão do banco não carregada.")

    processed = 0
    inserted = 0
    updated = 0
    skipped_existing = 0

    try:
        with _SESSION_SCOPE() as session:
            iterator = _iter_propositions_paginated(
                year_start,
                force_full=force_full,
                data_inicio=data_inicio,
                pagina_inicial=pagina_inicial,
                data_fim=data_fim,
                session=session if persist else None,
            )

            for basic_data, _ in iterator:
                proposition_id = _parse_int(basic_data.get("id"))
                if proposition_id is None:
                    continue

                processed += 1

                # Verificar se já existe (para otimizar)
                exists = False
                if persist:
                    exists = _proposition_exists(session, proposition_id)

                # Buscar detalhes completos (sempre para novas, opcional para existentes)
                detail_data = None
                author_codes_list = []
                themes = None
                if not exists or force_full:
                    detail_data = _fetch_proposition_detail(proposition_id)
                    if detail_data and REQUEST_DELAY > 0:
                        time.sleep(REQUEST_DELAY)
                    
                    # Buscar autores (endpoint separado)
                    author_codes_list = _fetch_proposition_authors(proposition_id)
                    if author_codes_list and REQUEST_DELAY > 0:
                        time.sleep(REQUEST_DELAY)

                    # Áreas temáticas oficiais (endpoint separado)
                    themes = _fetch_proposition_themes(proposition_id)
                    if REQUEST_DELAY > 0:
                        time.sleep(REQUEST_DELAY)

                # Construir payload
                payload = _build_payload_from_data(
                    basic_data, detail_data, author_codes_list, themes
                )
                if payload is None:
                    continue

                if interactive or not persist:
                    _debug_print_payload(payload)
                    if interactive:
                        try:
                            input("Pressione ENTER para continuar; Ctrl+C para sair.")
                        except (KeyboardInterrupt, EOFError):
                            logger.info("Execução interrompida pelo usuário.")
                            return

                if persist:
                    # Se existe e não forçou full, apenas atualizar campos básicos
                    if exists and not force_full and detail_data is None:
                        skipped_existing += 1
                        logger.debug("Proposição %s já existe, pulando detalhes", proposition_id)
                        continue

                    record, created = _upsert_proposition(
                        session,
                        payload,
                        auto_sync_missing_authors=auto_sync_missing_authors,
                    )
                    if created:
                        inserted += 1
                        logger.info("Proposição %s inserida", proposition_id)
                    else:
                        updated += 1
                        logger.debug("Proposição %s atualizada", proposition_id)

    except KeyboardInterrupt:
        logger.info("Execução interrompida pelo usuário após %s proposições.", processed)
        return

    if processed == 0:
        logger.warning("Nenhuma proposição retornada pela Câmara")
    elif persist:
        logger.info(
            "Sincronização concluída: %s inseridas, %s atualizadas, %s existentes puladas, %s total",
            inserted,
            updated,
            skipped_existing,
            processed,
        )
    else:
        logger.info("Processamento concluído em modo dry-run (%s proposições)", processed)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Sincroniza proposições da Câmara dos Deputados.")
    parser.add_argument(
        "--year",
        type=int,
        default=2025,
        help="Ano inicial para buscar proposições (padrão: 2025).",
    )
    parser.add_argument(
        "--data-inicio",
        type=str,
        help=(
            "Data de APRESENTAÇÃO inicial (YYYY-MM-DD). Sobrescreve a busca "
            "incremental. Filtra `dataApresentacaoInicio` na API da Câmara."
        ),
    )
    parser.add_argument(
        "--data-fim",
        type=str,
        help=(
            "Data de APRESENTAÇÃO final (YYYY-MM-DD). Combinada com --data-inicio "
            "fatia o backfill por janelas. Filtra `dataApresentacaoFim` na API."
        ),
    )
    parser.add_argument(
        "--force-full",
        action="store_true",
        help="Ignora busca incremental e reprocessa todas as proposições com detalhes.",
    )
    parser.add_argument(
        "--pagina-inicial",
        type=int,
        default=1,
        help="Página inicial para retomar a paginação (padrão: 1).",
    )
    parser.add_argument(
        "--auto-sync-missing-authors",
        action="store_true",
        help=(
            "Quando um autor não existir no banco, tenta buscá-lo na API da Câmara e "
            "cadastrá-lo automaticamente."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Não persiste no banco; apenas exibe os payloads.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Pausa após cada proposição para inspeção manual.",
    )

    args = parser.parse_args()

    proposition(
        year_start=args.year,
        data_inicio=args.data_inicio,
        data_fim=args.data_fim,
        force_full=args.force_full,
        pagina_inicial=args.pagina_inicial,
        auto_sync_missing_authors=args.auto_sync_missing_authors,
        persist=not args.dry_run,
        interactive=args.interactive,
    )
