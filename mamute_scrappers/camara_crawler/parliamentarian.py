"""Raspador de parlamentares da Câmara dos Deputados.

Este módulo fornece ganchos para buscar dados dos parlamentares na API da Câmara
e persistir as informações nas tabelas relacionadas (`Parliamentarian`,
`SocialNetwork`, `ParliamentarianSocialNetwork`). A função pública
`parliamentarian()` apenas orquestra o fluxo.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import unicodedata
from datetime import date
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    Iterable,
    List,
    Optional,
    TYPE_CHECKING,
    TypedDict,
)

import requests
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

CAMARA_API_BASE_URL = "https://dadosabertos.camara.leg.br/api/v2"
CAMARA_WEB_BASE_URL = "https://www.camara.leg.br"


class CamaraIngestionScope(str, Enum):
    """Conjuntos de deputados que podem ser sincronizados por deployment."""

    CURRENT_ONLY = "current_only"
    CURRENT_AND_LICENSED = "current_and_licensed"
    CURRENT_LEGISLATURE = "current_legislature"


MAMUTE_CAMARA_INGEST_SCOPE = "MAMUTE_CAMARA_INGEST_SCOPE"
DEFAULT_CAMARA_INGESTION_SCOPE = CamaraIngestionScope.CURRENT_ONLY


def get_camara_ingestion_scope(
    value: Optional[str] = None,
) -> CamaraIngestionScope:
    """Lê o escopo de ingestão com fallback seguro para deputados em exercício.

    ``value`` existe para permitir testes e chamadas programáticas; no scheduler,
    a configuração é lida de ``MAMUTE_CAMARA_INGEST_SCOPE``.
    """
    configured = value if value is not None else os.getenv(MAMUTE_CAMARA_INGEST_SCOPE)
    normalized = (configured or DEFAULT_CAMARA_INGESTION_SCOPE.value).strip().lower()
    try:
        return CamaraIngestionScope(normalized)
    except ValueError:
        logger.warning(
            "%s=%r é inválida; usando o padrão seguro %s.",
            MAMUTE_CAMARA_INGEST_SCOPE,
            configured,
            DEFAULT_CAMARA_INGESTION_SCOPE.value,
        )
        return DEFAULT_CAMARA_INGESTION_SCOPE


if TYPE_CHECKING:  # pragma: no cover - apenas para tipagem
    from mamute_scrappers.db.models import (
        Parliamentarian as ParliamentarianModel,
        ParliamentarianSocialNetwork as ParliamentarianSocialNetworkModel,
        SocialNetwork as SocialNetworkModel,
    )
    from mamute_scrappers.db.session import session_scope as session_scope_type

    SessionScopeCallable = Callable[[], ContextManager["Session"]]
else:
    ParliamentarianModel = Any
    ParliamentarianSocialNetworkModel = Any
    SocialNetworkModel = Any
    SessionScopeCallable = Callable[[], ContextManager[Session]]

Parliamentarian: Any = None
ParliamentarianSocialNetwork: Any = None
SocialNetwork: Any = None
_SESSION_SCOPE: Optional[SessionScopeCallable] = None


def _ensure_db_dependencies() -> None:
    global Parliamentarian, ParliamentarianSocialNetwork, SocialNetwork, _SESSION_SCOPE
    if _SESSION_SCOPE is not None:
        return

    try:
        from mamute_scrappers.db.models import (
            Parliamentarian as ParliamentarianModelRuntime,
            ParliamentarianSocialNetwork as ParliamentarianSocialNetworkModelRuntime,
            SocialNetwork as SocialNetworkModelRuntime,
        )
        from mamute_scrappers.db.session import session_scope as session_scope_runtime
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise RuntimeError("Não foi possível carregar dependências de banco.") from exc

    Parliamentarian = ParliamentarianModelRuntime
    ParliamentarianSocialNetwork = ParliamentarianSocialNetworkModelRuntime
    SocialNetwork = SocialNetworkModelRuntime
    _SESSION_SCOPE = session_scope_runtime


class SocialNetworkPayload(TypedDict, total=False):
    name: str
    url: Optional[str]
    profile_url: Optional[str]


class ParliamentarianPayload(TypedDict, total=False):
    id: int
    type: Optional[str]
    parliamentarian_code: Optional[int]
    name: Optional[str]
    full_name: Optional[str]
    email: Optional[str]
    telephone: Optional[str]
    cpf: Optional[str]
    status: Optional[str]
    party: Optional[str]
    state_of_birth: Optional[str]
    city_of_birth: Optional[str]
    state_elected: Optional[str]
    site: Optional[str]
    education: Optional[str]
    office_name: Optional[str]
    office_building: Optional[str]
    office_number: Optional[str]
    office_floor: Optional[str]
    office_email: Optional[str]
    biography_link: Optional[str]
    biography_text: Optional[str]
    details: Optional[dict]
    social_networks: List[SocialNetworkPayload]


PARLIAMENTARIAN_MUTABLE_FIELDS = [
    "type",
    "parliamentarian_code",
    "name",
    "full_name",
    "email",
    "telephone",
    "cpf",
    "status",
    "party",
    "state_of_birth",
    "city_of_birth",
    "state_elected",
    "site",
    "education",
    "office_name",
    "office_building",
    "office_number",
    "office_floor",
    "office_email",
    "biography_link",
    "biography_text",
    "details",
]


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


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _json_get(container: Optional[Dict[str, Any]], *keys: str) -> Optional[str]:
    """Busca valor aninhado em dict seguindo caminho de chaves."""
    if not isinstance(container, dict):
        return None
    
    current = container
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    
    return _coerce_text(current)


def _format_telephones(numbers: Iterable[str]) -> Optional[str]:
    formatted = " / ".join(num for num in numbers if num)
    return formatted or None


def _debug_print_payload(payload: ParliamentarianPayload) -> None:
    debug_repr = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    print(debug_repr)


def _wait_for_user() -> bool:
    try:
        input("Pressione ENTER (ou digite algo) para o próximo parlamentar; Ctrl+C para sair.")
        return True
    except (KeyboardInterrupt, EOFError):
        return False


def _request_json(url: str, *, params: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    headers = {"Accept": "application/json"}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
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


def _parse_iso_date(value: Any) -> Optional[date]:
    text = _coerce_text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _fetch_current_legislature_id(
    *,
    reference_date: Optional[date] = None,
) -> Optional[int]:
    """Retorna o id da legislatura que contém a data de referência."""
    today = reference_date or date.today()
    # A API da Câmara já devolve as legislaturas em ordem decrescente. Ela não
    # aceita ``ordenarPor=dataInicio`` (retorna HTTP 400), portanto não
    # enviamos parâmetros de ordenação para esta consulta.
    data = _request_json(
        f"{CAMARA_API_BASE_URL}/legislaturas",
    )
    if data is None:
        return None

    legislaturas = data.get("dados")
    if not isinstance(legislaturas, list):
        logger.error("Resposta da Câmara sem lista de legislaturas válida")
        return None

    for legislatura in legislaturas:
        if not isinstance(legislatura, dict):
            continue
        legislatura_id = legislatura.get("id")
        start = _parse_iso_date(legislatura.get("dataInicio"))
        end = _parse_iso_date(legislatura.get("dataFim"))
        if isinstance(legislatura_id, int) and start and end and start <= today <= end:
            return legislatura_id

    logger.error("Não foi encontrada legislatura vigente para %s", today.isoformat())
    return None


def _fetch_parliamentarian_list(
    scope: CamaraIngestionScope = DEFAULT_CAMARA_INGESTION_SCOPE,
) -> Optional[List[Dict[str, Any]]]:
    """Busca deputados conforme o escopo de ingestão configurado.

    O endpoint sem ``idLegislatura`` é a lista oficial de deputados em exercício.
    Para os escopos ampliados, a legislatura vigente inclui também parlamentares
    licenciados e outros registros históricos do mandato em curso.
    """
    url = f"{CAMARA_API_BASE_URL}/deputados"
    params = {"ordem": "ASC", "ordenarPor": "nome"}

    if scope is not CamaraIngestionScope.CURRENT_ONLY:
        legislature_id = _fetch_current_legislature_id()
        if legislature_id is None:
            return None
        params["idLegislatura"] = str(legislature_id)

    logger.debug("Consultando lista de deputados em %s com parâmetros %s", url, params)
    data = _request_json(url, params=params)
    
    if data is None:
        return None
    
    dados = data.get("dados")
    if not isinstance(dados, list):
        logger.error("Resposta da Câmara sem campo 'dados' ou tipo inválido")
        return None
    
    return dados


def _normalize_status(value: Optional[str]) -> str:
    if value is None:
        return ""
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


def _is_current_or_licensed(status: Optional[str]) -> bool:
    """Diz se a situação retornada pela Câmara deve entrar no escopo intermediário."""
    normalized = _normalize_status(status)
    if "licenc" in normalized:
        return True
    if "exerc" not in normalized:
        return False
    return not any(
        marker in normalized
        for marker in ("fora de exerc", "nao esta em exerc", "nao estava em exerc")
    )


def _fetch_parliamentarian_detail(deputado_id: int) -> Optional[Dict[str, Any]]:
    """Busca detalhes completos de um deputado específico."""
    url = f"{CAMARA_API_BASE_URL}/deputados/{deputado_id}"
    
    logger.debug("Consultando detalhes do deputado %s", deputado_id)
    data = _request_json(url)
    
    if data is None:
        return None
    
    dados = data.get("dados")
    if not isinstance(dados, dict):
        logger.error("Resposta da Câmara sem campo 'dados' ou tipo inválido para deputado %s", deputado_id)
        return None
    
    return dados


def _parse_social_networks_json(deputado: Dict[str, Any]) -> List[SocialNetworkPayload]:
    """Extrai redes sociais do JSON do deputado."""
    redes_sociais = deputado.get("redeSocial", [])
    if not isinstance(redes_sociais, list):
        redes_sociais = [redes_sociais] if redes_sociais else []
    
    social_networks: List[SocialNetworkPayload] = []
    
    for rede in redes_sociais:
        if not isinstance(rede, dict):
            continue
        
        name = _coerce_text(rede.get("nome"))
        profile_url = _coerce_text(rede.get("url"))
        
        if not name and not profile_url:
            continue
        
        social_payload: SocialNetworkPayload = {
            "name": name or "Rede social",
        }
        if profile_url:
            social_payload["profile_url"] = profile_url
        
        social_networks.append(social_payload)
    
    return social_networks


def _build_payload_from_json(deputado: Dict[str, Any]) -> Optional[ParliamentarianPayload]:
    """Constrói payload interno a partir do JSON da API da Câmara."""
    if not isinstance(deputado, dict):
        logger.debug("Deputado JSON inválido; pulando registro")
        return None
    
    deputado_id = deputado.get("id")
    if not isinstance(deputado_id, int):
        logger.debug("ID do deputado ausente ou inválido; pulando registro")
        return None
    
    # Buscar detalhes completos
    detail = _fetch_parliamentarian_detail(deputado_id)
    if detail is None:
        logger.warning("Não foi possível obter detalhes do deputado %s", deputado_id)
        return None
    
    # Extrair dados básicos e do último status
    ultimo_status = detail.get("ultimoStatus", {})
    if not isinstance(ultimo_status, dict):
        ultimo_status = {}
    
    gabinete = ultimo_status.get("gabinete", {})
    if not isinstance(gabinete, dict):
        gabinete = {}
    
    # Telefone
    telefone = _coerce_text(gabinete.get("telefone"))
    
    # Redes sociais
    social_networks = _parse_social_networks_json(detail)
    
    # Construir payload
    payload: ParliamentarianPayload = {
        "type": "Deputado",
        "parliamentarian_code": deputado_id,
        "name": _json_get(ultimo_status, "nome"),
        "full_name": _json_get(detail, "nomeCivil"),
        "email": _json_get(ultimo_status, "email"),
        "telephone": telefone,
        "cpf": _json_get(detail, "cpf"),
        "status": _json_get(ultimo_status, "situacao"),
        "party": _json_get(ultimo_status, "siglaPartido"),
        "state_of_birth": _json_get(detail, "ufNascimento"),
        "city_of_birth": _json_get(detail, "municipioNascimento"),
        "state_elected": _json_get(ultimo_status, "siglaUf"),
        "site": _json_get(detail, "urlWebsite"),
        "education": _json_get(detail, "escolaridade"),
        "office_name": _json_get(gabinete, "nome"),
        "office_building": _json_get(gabinete, "predio"),
        "office_number": _json_get(gabinete, "sala"),
        "office_floor": _json_get(gabinete, "andar"),
        "office_email": _json_get(gabinete, "email"),
        "biography_link": f"{CAMARA_WEB_BASE_URL}/deputados/{deputado_id}/biografia",
        "biography_text": None,  # Requer scraping HTML
        "details": detail,
        "social_networks": social_networks,
    }
    
    return payload


def parliamentarian(
    *,
    uf: Optional[str] = None,
    party: Optional[str] = None,
    deputado_id: Optional[int] = None,
    persist: bool = True,
    interactive: bool = False,
) -> None:
    """Busca parlamentares da Câmara e salva/atualiza o banco.

    Quando `persist` for `False`, apenas exibe os payloads. Com `interactive=True`,
    o fluxo aguarda uma confirmação (ENTER) entre cada parlamentar para facilitar
    a inspeção manual.
    """
    ingestion_scope = get_camara_ingestion_scope()
    logger.info(
        "Iniciando sincronização de parlamentares da Câmara (persist=%s, interactive=%s, scope=%s)",
        persist,
        interactive,
        ingestion_scope.value,
    )

    iterator = _fetch_parliamentarians(
        uf=uf,
        party=party,
        deputado_id=deputado_id,
        scope=ingestion_scope,
    )
    processed = 0

    try:
        if persist:
            _ensure_db_dependencies()
            if _SESSION_SCOPE is None:
                raise RuntimeError("Função de sessão do banco não carregada.")

            listed: set = set()
            with _SESSION_SCOPE() as session:
                for payload in iterator:
                    processed += 1
                    if payload.get("parliamentarian_code") is not None:
                        listed.add(int(payload["parliamentarian_code"]))
                    if interactive or not persist:
                        _debug_print_payload(payload)
                        if interactive and not _wait_for_user():
                            logger.info(
                                "Execução interrompida pelo usuário após %s registros.",
                                processed,
                            )
                            return
                    _upsert_parliamentarian(session, payload)
                # A lista da Câmara só traz quem está em exercício hoje: quem
                # saiu some dela e ficaria "Exercício" na base para sempre
                # (CS-141). Só numa execução completa (filtro = lista parcial)
                # e com lista não vazia (API fora do ar não é "todos saíram").
                if processed and uf is None and party is None and deputado_id is None:
                    refreshed = 0
                    for code in sorted(_codes_marked_in_exercise(session) - listed):
                        departed = _build_payload_from_json({"id": code})
                        if departed is not None:
                            _upsert_parliamentarian(session, departed)
                            refreshed += 1
                    if refreshed:
                        logger.info("Status atualizado de %s deputado(s) fora da lista de exercício", refreshed)
        else:
            for payload in iterator:
                processed += 1
                _debug_print_payload(payload)
                if interactive and not _wait_for_user():
                    logger.info(
                        "Execução interrompida pelo usuário após %s registros.", processed
                    )
                    return
    except KeyboardInterrupt:
        logger.info("Execução interrompida pelo usuário após %s registros.", processed)
        return

    if processed == 0:
        logger.warning("Nenhum parlamentar retornado pela API da Câmara")
    elif persist:
        logger.info("Sincronização de parlamentares concluída com sucesso (%s registros)", processed)
    else:
        logger.info("Processamento concluído em modo dry-run (%s registros)", processed)


def _fetch_parliamentarians(
    uf: Optional[str] = None,
    party: Optional[str] = None,
    deputado_id: Optional[int] = None,
    scope: CamaraIngestionScope = DEFAULT_CAMARA_INGESTION_SCOPE,
) -> Iterable[ParliamentarianPayload]:
    """Consulta o endpoint oficial (JSON) e transforma em payloads internos.

    O filtro ``--id`` é uma operação explícita de manutenção e não é limitado
    pelo escopo configurado. Nas execuções completas, ``current_and_licensed``
    descarta registros da legislatura atual que não estejam em exercício ou
    licenciados; ``current_legislature`` mantém todos eles.
    """
    
    # Se filtro por ID específico
    if deputado_id is not None:
        detail = _fetch_parliamentarian_detail(deputado_id)
        if detail is None:
            return
        payload = _build_payload_from_json(detail)
        if payload is not None:
            yield payload
        return
    
    # Buscar lista completa
    deputados_list = _fetch_parliamentarian_list(scope)
    if deputados_list is None:
        return
    
    for deputado_basic in deputados_list:
        if not isinstance(deputado_basic, dict):
            continue
        
        deputado_id_item = deputado_basic.get("id")
        if not isinstance(deputado_id_item, int):
            continue
        
        # Aplicar filtros (se houver)
        if uf is not None:
            sigla_uf = _coerce_text(deputado_basic.get("siglaUf"))
            if sigla_uf != uf.upper():
                continue
        
        if party is not None:
            sigla_partido = _coerce_text(deputado_basic.get("siglaPartido"))
            if sigla_partido != party.upper():
                continue
        
        # Buscar detalhes completos
        payload = _build_payload_from_json(deputado_basic)
        if payload is None:
            continue

        if (
            scope is CamaraIngestionScope.CURRENT_AND_LICENSED
            and not _is_current_or_licensed(payload.get("status"))
        ):
            logger.debug(
                "Deputado %s ignorado pelo escopo %s (situação=%r)",
                deputado_id_item,
                scope.value,
                payload.get("status"),
            )
            continue
        
        yield payload


def _codes_marked_in_exercise(session: Session) -> set:
    """Códigos dos deputados que a base ainda marca como em exercício."""
    rows = (
        session.query(Parliamentarian.parliamentarian_code)
        .filter(Parliamentarian.type == "Deputado", Parliamentarian.status == "Exercício")
        .all()
    )
    return {int(row[0]) for row in rows if row[0] is not None}


def _upsert_parliamentarian(session: Session, payload: ParliamentarianPayload) -> Any:
    """Atualiza ou cria um parlamentar conforme o payload informado."""
    code = payload.get("parliamentarian_code")
    if code is None:
        raise ValueError("Payload de parlamentar sem 'parliamentarian_code'.")

    if any(dep is None for dep in (Parliamentarian, ParliamentarianSocialNetwork, SocialNetwork)):
        raise RuntimeError("Dependências de banco não carregadas.")

    record: Any = (
        session.query(Parliamentarian).filter_by(parliamentarian_code=code).one_or_none()
    )
    if record is None:
        record = Parliamentarian(parliamentarian_code=code)
        session.add(record)

    for field in PARLIAMENTARIAN_MUTABLE_FIELDS:
        if field in payload:
            setattr(record, field, payload[field])

    _sync_social_networks(session, record, payload.get("social_networks", []))
    logger.debug("Parlamentar %s sincronizado", record.id)
    return record


def _sync_social_networks(
    session: Session,
    parliamentarian: Any,
    socials: Iterable[SocialNetworkPayload],
) -> None:
    """Sincroniza redes sociais associadas ao parlamentar."""
    existing_links = {link.social_network.name: link for link in parliamentarian.social_networks}
    handled = set()

    for social in socials:
        name = social.get("name")
        if not name:
            continue
        handled.add(name)

        network = session.query(SocialNetwork).filter_by(name=name).one_or_none()
        if network is None:
            network = SocialNetwork(name=name, url=social.get("url"))
            session.add(network)

        link = existing_links.get(name)
        if link is None:
            link = ParliamentarianSocialNetwork(
                parliamentarian=parliamentarian,
                social_network=network,
            )
            session.add(link)

        link.profile_url = social.get("profile_url")

    # Remove vínculos de redes que não estão mais presentes
    for name, link in existing_links.items():
        if name not in handled:
            session.delete(link)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Sincroniza parlamentares da Câmara dos Deputados.")
    parser.add_argument("--uf", help="Filtra por UF específica (ex.: SP).")
    parser.add_argument("--party", help="Filtra por partido específico (ex.: PT).")
    parser.add_argument(
        "--id",
        type=int,
        dest="deputado_id",
        help="Filtra por ID específico de deputado (ex.: 204560).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Não persiste no banco; apenas exibe os payloads.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Pausa após cada parlamentar para inspeção manual.",
    )

    args = parser.parse_args()

    parliamentarian(
        uf=args.uf,
        party=args.party,
        deputado_id=args.deputado_id,
        persist=not args.dry_run,
        interactive=args.interactive,
    )
