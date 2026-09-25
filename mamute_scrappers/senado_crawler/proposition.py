"""Raspador de proposições de parlamentares do Senado Federal."""

from __future__ import annotations

import json
import logging
import sys
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
    TYPE_CHECKING,
    TypedDict,
)

import requests
from sqlalchemy.orm import Session
from xml.etree import ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

SENADO_API_BASE_URL = "https://legis.senado.leg.br/dadosabertos"
SENADO_AUTHORSHIP_VERSION = "7"
SENADO_PROCESS_VERSION = "1"

if TYPE_CHECKING:  # pragma: no cover - apenas para tipagem
    from mamute_scrappers.db.models import (
        AuthorsProposition as AuthorsPropositionModel,
        Parliamentarian as ParliamentarianModel,
        Proposition as PropositionModel,
        PropositionStatus as PropositionStatusModel,
        PropositionType as PropositionTypeModel,
    )
    from mamute_scrappers.db.session import session_scope as session_scope_type

    SessionScopeCallable = Callable[[], ContextManager["Session"]]
else:
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
_SESSION_SCOPE: Optional[SessionScopeCallable] = None


class AuthorPayload(TypedDict, total=False):
    parliamentarian_code: Optional[int]
    name: Optional[str]
    party: Optional[str]
    house: Optional[str]
    role: Optional[str]
    gender: Optional[str]
    is_main_author: Optional[bool]
    is_other_author: Optional[bool]


class PropositionPayload(TypedDict, total=False):
    parliamentarian_code: int
    proposition_code: int
    process_id: int
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
    is_main_author: Optional[bool]
    is_other_author: Optional[bool]
    authors: List[AuthorPayload]
    proposition_type_acronym: Optional[str]
    proposition_type_description: Optional[str]
    proposition_status_code: Optional[str]
    proposition_status_acronym: Optional[str]
    themes: List[str]


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
    # Só entra no payload quando o processo foi baixado — falha de rede não
    # apaga tema já coletado.
    "themes",
]


def _ensure_db_dependencies() -> None:
    global Proposition, AuthorsProposition, Parliamentarian, PropositionType, PropositionStatus, _SESSION_SCOPE
    if _SESSION_SCOPE is not None:
        return

    try:
        from mamute_scrappers.db.models import (
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


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _element_to_dict(element: Optional[ET.Element]) -> Optional[Any]:
    if element is None:
        return None
    children = list(element)
    if not children:
        return _normalize_text(element.text)

    result: Dict[str, Any] = {}
    for child in children:
        value = _element_to_dict(child)
        if value is None:
            continue
        existing = result.get(child.tag)
        if existing is None:
            result[child.tag] = value
        else:
            if not isinstance(existing, list):
                result[child.tag] = [existing]
            result[child.tag].append(value)
    return result or None


def _author_from_dict(data: Any) -> Optional[AuthorPayload]:
    if not isinstance(data, dict):
        return None

    author: AuthorPayload = {}

    code = _parse_int(_coerce_text(data.get("codigoParlamentar") or data.get("CodigoParlamentar")))
    if code is not None:
        author["parliamentarian_code"] = code

    name = _coerce_text(
        data.get("autor")
        or data.get("Autor")
        or data.get("nome")
        or data.get("Nome")
        or data.get("parlamentar")
    )
    if name:
        author["name"] = name

    party = _coerce_text(data.get("siglaPartido") or data.get("partido") or data.get("SiglaPartido"))
    if party:
        author["party"] = party

    house = _coerce_text(
        data.get("casaEnte")
        or data.get("siglaEnte")
        or data.get("casaIdentificadora")
        or data.get("CasaEnte")
    )
    if house:
        author["house"] = house

    role = _coerce_text(
        data.get("descricaoTipo")
        or data.get("siglaTipo")
        or data.get("cargo")
        or data.get("siglaCargo")
        or data.get("DescricaoTipo")
    )
    if role:
        author["role"] = role

    gender = _coerce_text(data.get("sexo") or data.get("Sexo"))
    if gender:
        author["gender"] = gender

    main_flag = _parse_bool_text(_coerce_text(data.get("IndicadorAutorPrincipal")))
    if main_flag is not None:
        author["is_main_author"] = main_flag

    others_flag = _parse_bool_text(_coerce_text(data.get("IndicadorOutrosAutores")))
    if others_flag is not None:
        author["is_other_author"] = others_flag

    if not author:
        return None

    return author


def _collect_authors(
    parliamentarian_code: int,
    entry: Dict[str, Any],
    process_detail: Optional[Dict[str, Any]],
) -> List[AuthorPayload]:
    authors: List[AuthorPayload] = []
    seen: set[tuple[Optional[int], Optional[str]]] = set()

    def register(author_data: AuthorPayload) -> None:
        key = (author_data.get("parliamentarian_code"), author_data.get("name"))
        if key in seen:
            return
        seen.add(key)
        authors.append(author_data)

    base_author: AuthorPayload = {}
    if parliamentarian_code is not None:
        base_author["parliamentarian_code"] = parliamentarian_code
    is_main = entry.get("is_main_author")
    if is_main is not None:
        base_author["is_main_author"] = is_main
    is_other = entry.get("is_other_author")
    if is_other is not None:
        base_author["is_other_author"] = is_other
    if base_author:
        register(base_author)

    if not isinstance(process_detail, dict):
        return authors

    documento = process_detail.get("documento")
    if isinstance(documento, dict):
        for author in _ensure_list(documento.get("autoria")):
            converted = _author_from_dict(author)
            if converted:
                register(converted)

    for key in ("autoriaIniciativa", "autoria"):
        for author in _ensure_list(process_detail.get(key)):
            converted = _author_from_dict(author)
            if converted:
                register(converted)

    return authors


def _parse_bool_text(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"sim", "s", "true"}:
        return True
    if lowered in {"não", "nao", "n", "false"}:
        return False
    return None


def _parse_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _debug_print_payload(payload: PropositionPayload) -> None:
    debug_repr = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    print(debug_repr)


def _wait_for_user() -> bool:
    try:
        input("Pressione ENTER (ou digite algo) para a próxima proposição; Ctrl+C para sair.")
        return True
    except (KeyboardInterrupt, EOFError):
        return False


def _request_xml(url: str, *, params: Optional[Dict[str, str]] = None) -> Optional[ET.Element]:
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Falha ao consultar %s: %s", url, exc)
        return None

    try:
        return ET.fromstring(response.content)
    except ET.ParseError as exc:
        logger.error("Erro ao parsear XML do Senado (%s): %s", url, exc)
        return None


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
        logger.error("Erro ao parsear JSON do Senado (%s): %s", url, exc)
        return None

    if not isinstance(data, dict):
        logger.error("Resposta JSON inesperada do Senado (%s): tipo %s", url, type(data))
        return None

    return data


def _request_authorings_xml(code: int) -> Optional[ET.Element]:
    url = f"{SENADO_API_BASE_URL}/senador/{code}/autorias"
    params = {"v": SENADO_AUTHORSHIP_VERSION}
    root = _request_xml(url, params=params)
    if root is None:
        return None
    return root.find("Parlamentar")


def _request_process_json(process_id: int) -> Optional[Dict[str, Any]]:
    url = f"{SENADO_API_BASE_URL}/processo/{process_id}"
    params = {"v": SENADO_PROCESS_VERSION}
    data = _request_json(url, params=params)
    if data is None:
        return None

    # Algumas respostas vêm embrulhadas em chaves adicionais.
    for key in ("processo", "Processo"):
        wrapped = data.get(key)
        if isinstance(wrapped, dict):
            if key in wrapped:
                inner = wrapped.get(key)
                if isinstance(inner, dict):
                    return inner
            return wrapped

    return data


def _extract_authoring_entries(parlamentar_element: Optional[ET.Element]) -> List[Dict[str, Any]]:
    if parlamentar_element is None:
        logger.warning("Resposta do Senado sem nó 'Parlamentar'.")
        return []

    autorias_container = parlamentar_element.find("Autorias")
    if autorias_container is None:
        logger.warning("Resposta do Senado sem nó 'Autorias'.")
        return []

    entries: List[Dict[str, Any]] = []
    for autoria in autorias_container.findall("Autoria"):
        materia = autoria.find("Materia")
        if materia is None:
            continue

        process_id = _parse_int(_coerce_text(materia.findtext("IdentificacaoProcesso")))
        materia_code = _parse_int(_coerce_text(materia.findtext("Codigo")))
        date_text = _coerce_text(materia.findtext("Data"))
        presentation_date = _parse_date(date_text)

        entry = {
            "process_id": process_id,
            "materia_code": materia_code,
            "descricao_identificacao": _coerce_text(materia.findtext("DescricaoIdentificacao")),
            "sigla": _coerce_text(materia.findtext("Sigla")),
            "numero": _parse_int(_coerce_text(materia.findtext("Numero"))),
            "ano": _parse_int(_coerce_text(materia.findtext("Ano"))),
            "ementa": _coerce_text(materia.findtext("Ementa")),
            "presentation_date": presentation_date,
            "presentation_date_raw": date_text,
            "is_main_author": _parse_bool_text(_coerce_text(autoria.findtext("IndicadorAutorPrincipal"))),
            "is_other_author": _parse_bool_text(_coerce_text(autoria.findtext("IndicadorOutrosAutores"))),
            "autoria_dict": _element_to_dict(autoria),
        }
        entries.append(entry)

    return entries


def _extract_status_details(process_detail: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {"description": None, "code": None, "acronym": None}
    if not isinstance(process_detail, dict):
        return result

    best_situation: Optional[Dict[str, Any]] = None
    best_date: Optional[datetime] = None

    autuacoes = process_detail.get("autuacoes")
    if isinstance(autuacoes, list):
        for autuacao in autuacoes:
            if not isinstance(autuacao, dict):
                continue
            situacoes = autuacao.get("situacoes")
            if not isinstance(situacoes, list):
                continue
            for situacao in situacoes:
                if not isinstance(situacao, dict):
                    continue
                start_str = _coerce_text(situacao.get("inicio") or situacao.get("Inicio"))
                start_dt = _parse_datetime(start_str)
                if best_situation is None:
                    best_situation = situacao
                    best_date = start_dt
                    continue
                if start_dt is None:
                    continue
                if best_date is None or start_dt > best_date:
                    best_situation = situacao
                    best_date = start_dt

    if best_situation is not None:
        description = _coerce_text(best_situation.get("descricao") or best_situation.get("Descricao"))
        if description:
            result["description"] = description

        code = _parse_int(
            _coerce_text(
                best_situation.get("idTipo")
                or best_situation.get("IdTipo")
                or best_situation.get("idSituacaoIniciada")
                or best_situation.get("IdSituacaoIniciada")
            )
        )
        if code is not None:
            result["code"] = str(code)

        acronym = _coerce_text(
            best_situation.get("sigla")
            or best_situation.get("Sigla")
            or best_situation.get("siglaSituacaoIniciada")
            or best_situation.get("SiglaSituacaoIniciada")
        )
        if acronym:
            result["acronym"] = acronym

    if result["description"] is None:
        conteudo = process_detail.get("conteudo")
        if isinstance(conteudo, dict):
            conteudo_tipo = _coerce_text(conteudo.get("tipo") or conteudo.get("Tipo"))
            if conteudo_tipo:
                result["description"] = conteudo_tipo

    return result


def extract_senado_themes(process_detail: Any) -> Optional[List[str]]:
    """Extrai as classificações temáticas oficiais do JSON de /processo/{id}.

    Usa o nível mais específico (`descricao`, ex.: "Responsabilidade Civil"),
    não a hierarquia inteira. Devolve None sem processo (não coletado) e []
    quando o Senado não classificou a matéria.
    """
    if not isinstance(process_detail, dict):
        return None

    themes: List[str] = []
    for item in _ensure_list(process_detail.get("classificacoes")):
        if not isinstance(item, dict):
            continue
        name = _coerce_text(item.get("descricao"))
        if name and name not in themes:
            themes.append(name)
    return themes


def _build_details(entry: Dict[str, Any], process_detail: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    details: Dict[str, Any] = {}
    if entry.get("autoria_dict") is not None:
        details["autoria"] = entry["autoria_dict"]
    if process_detail is not None:
        details["processo"] = process_detail
    if entry.get("presentation_date_raw") is not None:
        details.setdefault("metadata", {})["presentation_date_raw"] = entry["presentation_date_raw"]
    if not details:
        return None
    return details


def _build_payload(
    parliamentarian_code: int,
    entry: Dict[str, Any],
    process_detail: Optional[Dict[str, Any]],
) -> Optional[PropositionPayload]:
    process_id = entry.get("process_id")
    if process_id is None:
        logger.debug(
            "Ignorando proposição sem 'IdentificacaoProcesso' para o parlamentar %s.",
            parliamentarian_code,
        )
        return None

    proposition_code = None
    if isinstance(process_detail, dict):
        proposition_code = _parse_int(_coerce_text(process_detail.get("codigoMateria")))
    if proposition_code is None:
        proposition_code = entry.get("materia_code")
    if proposition_code is None:
        proposition_code = process_id

    presentation_date: Optional[date] = entry.get("presentation_date")
    if presentation_date is None and isinstance(process_detail, dict):
        documento = process_detail.get("documento")
        if isinstance(documento, dict):
            presentation_date = _parse_date(_coerce_text(documento.get("dataApresentacao")))

    summary = entry.get("ementa")
    conteudo = process_detail.get("conteudo") if isinstance(process_detail, dict) else None
    if summary is None and isinstance(conteudo, dict):
        summary = _coerce_text(conteudo.get("ementa"))

    status_details = _extract_status_details(process_detail)
    current_status = _normalize_text(status_details.get("description"))
    status_code = status_details.get("code")
    status_acronym = status_details.get("acronym")

    presentation_year = entry.get("ano")
    if presentation_year is None and presentation_date is not None:
        presentation_year = presentation_date.year

    proposition_type_acronym = entry.get("sigla") or _coerce_text(
        process_detail.get("sigla") if isinstance(process_detail, dict) else None
    )
    proposition_type_description = _coerce_text(
        process_detail.get("descricaoSigla") if isinstance(process_detail, dict) else None
    )

    authors = _collect_authors(parliamentarian_code, entry, process_detail)

    payload: PropositionPayload = {
        "parliamentarian_code": parliamentarian_code,
        "proposition_code": proposition_code,
        "process_id": process_id,
        "title": entry.get("descricao_identificacao")
        or _coerce_text(process_detail.get("identificacao") if isinstance(process_detail, dict) else None),
        "link": f"{SENADO_API_BASE_URL}/processo/{process_id}?v={SENADO_PROCESS_VERSION}",
        "proposition_acronym": entry.get("sigla")
        or _coerce_text(process_detail.get("sigla") if isinstance(process_detail, dict) else None),
        "proposition_number": entry.get("numero")
        or _parse_int(_coerce_text(process_detail.get("numero") if isinstance(process_detail, dict) else None)),
        "presentation_year": presentation_year,
        "presentation_date": presentation_date,
        "presentation_month": presentation_date.month if presentation_date else None,
        "current_status": current_status,
        "summary": summary,
        "proposition_description": summary,
        "details": _build_details(entry, process_detail) or {},
        "is_main_author": entry.get("is_main_author"),
        "is_other_author": entry.get("is_other_author"),
        "authors": authors,
        "proposition_type_acronym": proposition_type_acronym,
        "proposition_type_description": proposition_type_description,
        "proposition_status_code": status_code,
        "proposition_status_acronym": status_acronym,
    }

    themes = extract_senado_themes(process_detail)
    if themes is not None:
        payload["themes"] = themes

    return payload


def _fetch_propositions_for_parliamentarian(
    parliamentarian_code: int,
    *,
    limit: Optional[int] = None,
    since_year: Optional[int] = None,
) -> Iterable[PropositionPayload]:
    parlamentar_element = _request_authorings_xml(parliamentarian_code)
    entries = _extract_authoring_entries(parlamentar_element)
    if not entries:
        return []

    entries.sort(
        key=lambda item: (
            item.get("presentation_date") or date.min,
            item.get("numero") or 0,
            item.get("materia_code") or 0,
        ),
        reverse=True,
    )

    if since_year is not None:
        # Backfill: mantém só proposições apresentadas a partir de since_year.
        entries = [
            entry
            for entry in entries
            if (entry.get("presentation_date") or date.min).year >= since_year
        ]

    if limit is not None:
        entries = entries[:limit]

    seen_process_ids: set[int] = set()
    payloads: List[PropositionPayload] = []

    for entry in entries:
        process_id = entry.get("process_id")
        if process_id is None or process_id in seen_process_ids:
            continue
        seen_process_ids.add(process_id)

        process_detail = _request_process_json(process_id)
        payload = _build_payload(parliamentarian_code, entry, process_detail)
        if payload is None:
            continue
        payloads.append(payload)

    return payloads


def _upsert_proposition(
    session: Session,
    parliamentarian_record: Any,
    payload: PropositionPayload,
) -> Any:
    code = payload.get("proposition_code")
    if code is None:
        raise ValueError("Payload de proposição sem identificador único.")

    if Proposition is None or AuthorsProposition is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    record: Any = session.query(Proposition).filter_by(proposition_code=code).one_or_none()
    if record is None:
        record = Proposition(proposition_code=code)
        session.add(record)

    for field in PROPOSITION_MUTABLE_FIELDS:
        if field in payload:
            setattr(record, field, payload[field])

    session.flush()  # garante 'id' para associação de autoria

    _sync_authorships(session, record, payload, parliamentarian_record)
    _assign_type_and_status(session, record, payload)

    logger.debug(
        "Proposição %s sincronizada para o parlamentar %s.",
        record.proposition_code,
        parliamentarian_record.parliamentarian_code,
    )
    return record


def _sync_authorships(
    session: Session,
    record: Any,
    payload: PropositionPayload,
    parliamentarian_record: Any,
) -> None:
    if Parliamentarian is None or AuthorsProposition is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    target_codes: set[int] = set()

    current_code = getattr(parliamentarian_record, "parliamentarian_code", None)
    if isinstance(current_code, int):
        target_codes.add(current_code)

    for author in payload.get("authors", []):
        code = author.get("parliamentarian_code")
        if isinstance(code, int):
            target_codes.add(code)

    if not target_codes:
        return

    existing_links = (
        session.query(AuthorsProposition)
        .join(Parliamentarian)
        .filter(AuthorsProposition.proposition_id == record.id)
        .all()
    )

    existing_codes = {
        link.parliamentarian.parliamentarian_code
        for link in existing_links
        if getattr(link.parliamentarian, "parliamentarian_code", None) is not None
    }

    missing_codes = target_codes - existing_codes
    if not missing_codes:
        return

    linked_parliamentarians = (
        session.query(Parliamentarian)
        .filter(Parliamentarian.parliamentarian_code.in_(list(missing_codes)))
        .all()
    )

    for author_parliamentarian in linked_parliamentarians:
        session.add(
            AuthorsProposition(
                parliamentarian=author_parliamentarian,
                proposition=record,
            )
        )


def _assign_type_and_status(
    session: Session,
    record: Any,
    payload: PropositionPayload,
) -> None:
    if PropositionType is None or PropositionStatus is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    type_acronym = payload.get("proposition_type_acronym")
    if type_acronym:
        type_record = (
            session.query(PropositionType)
            .filter_by(acronym=type_acronym, type="Senado")
            .order_by(PropositionType.id.asc())
            .first()
        )
        if type_record is None:
            description = payload.get("proposition_type_description") or type_acronym
            type_record = PropositionType(
                acronym=type_acronym,
                type="Senado",
                name=type_acronym,
                description=description,
            )
            session.add(type_record)
            session.flush()
        record.proposition_type = type_record

    status_code = payload.get("proposition_status_code")
    status_acronym = payload.get("proposition_status_acronym")
    status_description = payload.get("current_status")

    status_record = None
    if status_code:
        status_record = (
            session.query(PropositionStatus)
            .filter(PropositionStatus.proposition_status_code == status_code)
            .one_or_none()
        )
    if status_record is None and status_acronym:
        status_record = (
            session.query(PropositionStatus)
            .filter(PropositionStatus.acronym == status_acronym)
            .one_or_none()
        )

    if status_record is None and (status_code or status_acronym or status_description):
        status_record = PropositionStatus(
            proposition_status_code=status_code or status_acronym,
            acronym=status_acronym,
            name=status_description or status_acronym,
            description=status_description or status_acronym,
        )
        session.add(status_record)
        session.flush()

    if status_record is not None:
        # Atualiza campos caso tenham novas informações.
        updates: Dict[str, Optional[str]] = {
            "acronym": status_acronym or status_record.acronym,
            "description": status_description or status_record.description,
            "name": status_description or status_record.name,
        }
        for field, value in updates.items():
            if value and getattr(status_record, field) != value:
                setattr(status_record, field, value)
        record.proposition_status = status_record


def _iter_target_parliamentarians(
    session: Session,
    parliamentarian_code: Optional[int] = None,
) -> List[Any]:
    if Parliamentarian is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    query = session.query(Parliamentarian)
    if parliamentarian_code is not None:
        query = query.filter(Parliamentarian.parliamentarian_code == parliamentarian_code)
    else:
        # Este crawler consulta a API do Senado (/senador/{code}/autorias).
        # Deputados da Câmara não têm autorias lá e só geram requests inúteis,
        # então restringe a senadores quando não há um código específico.
        query = query.filter(Parliamentarian.type == "Senador")

    parliamentarians = [
        parliamentarian
        for parliamentarian in query.all()
        if parliamentarian.parliamentarian_code is not None
    ]

    if not parliamentarians and parliamentarian_code is not None:
        logger.warning(
            "Nenhum parlamentar encontrado com o código %s.",
            parliamentarian_code,
        )

    return parliamentarians


def proposition(
    *,
    parliamentarian_code: Optional[int] = None,
    limit: Optional[int] = 25,
    since_year: Optional[int] = None,
    persist: bool = True,
    interactive: bool = False,
) -> None:
    """Busca proposições do Senado e opcionalmente salva/atualiza o banco.

    Quando ``since_year`` é informado (backfill), o ``limit`` é ignorado e são
    sincronizadas todas as proposições apresentadas a partir daquele ano.
    """
    logger.info(
        "Iniciando sincronização de proposições do Senado (persist=%s, interactive=%s, since_year=%s)",
        persist,
        interactive,
        since_year,
    )

    _ensure_db_dependencies()
    if _SESSION_SCOPE is None:
        raise RuntimeError("Função de sessão do banco não carregada.")

    processed = 0

    try:
        with _SESSION_SCOPE() as session:
            parliamentarians = _iter_target_parliamentarians(session, parliamentarian_code)
            if not parliamentarians:
                logger.warning("Nenhum parlamentar com código disponível para sincronizar.")
                return

            for parliamentarian_record in parliamentarians:
                code = parliamentarian_record.parliamentarian_code
                logger.debug(
                    "Processando proposições do parlamentar %s (%s).",
                    parliamentarian_record.name or "",
                    code,
                )

                payloads = _fetch_propositions_for_parliamentarian(
                    code,
                    limit=None if since_year else limit,
                    since_year=since_year,
                )
                if not payloads:
                    logger.debug(
                        "Nenhuma proposição retornada para o parlamentar %s.",
                        code,
                    )
                    continue

                for payload in payloads:
                    processed += 1
                    if not persist or interactive:
                        _debug_print_payload(payload)
                        if interactive and not _wait_for_user():
                            logger.info(
                                "Execução interrompida pelo usuário após %s proposições.",
                                processed,
                            )
                            return
                    if persist:
                        _upsert_proposition(session, parliamentarian_record, payload)
    except KeyboardInterrupt:
        logger.info("Execução interrompida manualmente após %s proposições.", processed)
        return

    if processed == 0:
        logger.warning("Nenhuma proposição retornada pela API do Senado.")
    elif persist:
        logger.info("Sincronização de proposições concluída com sucesso (%s registros).", processed)
    else:
        logger.info("Processamento concluído em modo dry-run (%s registros).", processed)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Sincroniza proposições do Senado.")
    parser.add_argument(
        "--parliamentarian-code",
        type=int,
        help="Filtra por código específico de parlamentar (ex.: 5982).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Quantidade máxima de proposições recentes por parlamentar (padrão: 25).",
    )
    parser.add_argument(
        "--since-year",
        type=int,
        help="Backfill: sincroniza todas as proposições apresentadas a partir deste ano "
        "(ex.: 2022). Quando definido, --limit é ignorado.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Não persiste no banco; apenas exibe os payloads obtidos.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Pausa após cada proposição para inspeção manual.",
    )

    args = parser.parse_args()

    proposition(
        parliamentarian_code=args.parliamentarian_code,
        limit=args.limit,
        since_year=args.since_year,
        persist=not args.dry_run,
        interactive=args.interactive,
    )

