"""Sincroniza discursos/pronunciamentos dos parlamentares da Câmara dos Deputados."""

from __future__ import annotations

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
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

CAMARA_API_BASE_URL = "https://dadosabertos.camara.leg.br/api/v2"

REQUEST_DELAY = 0.1  # Delay entre requests (segundos)
PAGE_SIZE = 50

if TYPE_CHECKING:  # pragma: no cover - apenas para tipagem
    from mamute_scrappers.db.models import (
        Parliamentarian as ParliamentarianModel,
        SpeechesTranscript as SpeechesTranscriptModel,
    )
    from mamute_scrappers.db.session import session_scope as session_scope_type

    SessionScopeCallable = Callable[[], ContextManager["Session"]]
else:
    ParliamentarianModel = Any
    SpeechesTranscriptModel = Any
    SessionScopeCallable = Callable[[], ContextManager[Session]]

SpeechesTranscript: Any = None
Parliamentarian: Any = None
_SESSION_SCOPE: Optional[SessionScopeCallable] = None


class SpeechPayload(TypedDict, total=False):
    parliamentarian_code: int
    date: Optional[date]
    session_number: Optional[str]
    type: Optional[str]
    speech_link: Optional[str]
    speech_text: Optional[str]
    summary: Optional[str]
    hour_minute: Optional[str]
    publication_link: Optional[str]
    publication_text: Optional[str]


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
    global SpeechesTranscript, Parliamentarian, _SESSION_SCOPE
    if _SESSION_SCOPE is not None:
        return

    try:
        from mamute_scrappers.db.models import (
            Parliamentarian as ParliamentarianModelRuntime,
            SpeechesTranscript as SpeechesTranscriptModelRuntime,
        )
        from mamute_scrappers.db.session import session_scope as session_scope_runtime
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise RuntimeError("Não foi possível carregar dependências de banco.") from exc

    SpeechesTranscript = SpeechesTranscriptModelRuntime
    Parliamentarian = ParliamentarianModelRuntime
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


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = _coerce_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_date(value: Any) -> Optional[date]:
    dt = _parse_datetime(value)
    return dt.date() if dt else None


def _extract_hour_minute(value: Any) -> Optional[str]:
    """Extrai hora:minuto de datetime."""
    dt = _parse_datetime(value)
    if dt is None:
        return None
    return dt.strftime("%H:%M")


def _request_json(url: str, *, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    headers = {"Accept": "application/json"}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=90)
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


def _get_last_speech_date(session: Session, deputado_id: int) -> Optional[date]:
    """Busca a data do último discurso de um deputado específico."""
    if SpeechesTranscript is None or Parliamentarian is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    parliamentarian = (
        session.query(Parliamentarian)
        .filter_by(parliamentarian_code=deputado_id)
        .one_or_none()
    )

    if parliamentarian is None:
        return None

    last_speech = (
        session.query(SpeechesTranscript)
        .filter(SpeechesTranscript.parliamentarian_id == parliamentarian.id)
        .filter(SpeechesTranscript.date.isnot(None))
        .order_by(SpeechesTranscript.date.desc())
        .first()
    )

    if last_speech and last_speech.date:
        return last_speech.date

    return None


def _fetch_speeches_list(
    deputado_id: int,
    data_inicio: str,
    page: int = 1,
    data_fim: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Busca lista paginada de discursos de um deputado.

    A API da Câmara rejeita (HTTP 400) intervalos maiores que 4 anos entre
    dataInicio e dataFim. Por isso o backfill fatia por janelas e envia dataFim.
    """
    url = f"{CAMARA_API_BASE_URL}/deputados/{deputado_id}/discursos"
    params = {
        "dataInicio": data_inicio,
        "ordem": "ASC",
        "ordenarPor": "dataHoraInicio",
        "pagina": page,
        "itens": PAGE_SIZE,
    }
    if data_fim:
        params["dataFim"] = data_fim

    logger.debug(
        "Buscando discursos (deputado=%s, dataInicio=%s, página=%s)",
        deputado_id,
        data_inicio,
        page,
    )
    return _request_json(url, params=params)


def _iter_speeches_paginated(
    deputado_id: int,
    data_inicio: str,
    data_fim: Optional[str] = None,
) -> Iterable[Dict[str, Any]]:
    """Itera sobre discursos de um deputado com paginação."""
    page = 1
    total_fetched = 0

    while True:
        data = _fetch_speeches_list(deputado_id, data_inicio, page, data_fim=data_fim)

        if data is None:
            logger.warning("Falha ao buscar página %s (deputado %s)", page, deputado_id)
            break

        dados = data.get("dados")
        if not isinstance(dados, list) or not dados:
            logger.debug("Página %s sem dados (deputado %s)", page, deputado_id)
            break

        for discurso in dados:
            if isinstance(discurso, dict):
                total_fetched += 1
                yield discurso

        logger.debug(
            "Deputado %s: página %s processada (%s discursos)",
            deputado_id,
            page,
            len(dados),
        )

        # Verificar se tem próxima página
        links = data.get("links", [])
        has_next = any(
            isinstance(link, dict) and link.get("rel") == "next"
            for link in links
        )

        if not has_next:
            break

        page += 1

        if REQUEST_DELAY > 0:
            time.sleep(REQUEST_DELAY)

    if total_fetched > 0:
        logger.info("Deputado %s: %s discursos obtidos", deputado_id, total_fetched)


def _build_speech_payload(
    deputado_id: int,
    discurso: Dict[str, Any],
) -> Optional[SpeechPayload]:
    """Constrói payload a partir dos dados da API."""
    data_hora_inicio = discurso.get("dataHoraInicio")
    speech_date = _parse_date(data_hora_inicio)

    fase_evento = discurso.get("faseEvento")
    fase_titulo = None
    if isinstance(fase_evento, dict):
        fase_titulo = _coerce_text(fase_evento.get("titulo"))

    url_texto = _coerce_text(discurso.get("urlTexto"))
    keywords = _coerce_text(discurso.get("keywords"))

    payload: SpeechPayload = {
        "parliamentarian_code": deputado_id,
        "date": speech_date,
                "session_number": None,  # API não fornece
        "type": _coerce_text(discurso.get("tipoDiscurso")),
        "speech_link": url_texto,
        "speech_text": _coerce_text(discurso.get("transcricao")),
        "summary": _coerce_text(discurso.get("sumario")),
        "hour_minute": _extract_hour_minute(data_hora_inicio),
        "publication_link": url_texto,  # Mesmo link
        "publication_text": keywords,
    }

    return payload


def _debug_print_payload(payload: SpeechPayload) -> None:
    debug_repr = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    print(debug_repr)


def _upsert_speech(
    session: Session,
    payload: SpeechPayload,
) -> Tuple[Any, bool]:
    """Insere ou atualiza discurso."""
    if SpeechesTranscript is None or Parliamentarian is None:
        raise RuntimeError("Dependências de banco não carregadas.")

    parlamentarian_code = payload.get("parliamentarian_code")
    if parlamentarian_code is None:
        raise ValueError("Payload sem 'parlamentarian_code'.")

    # Buscar deputado
    parliamentarian = (
        session.query(Parliamentarian)
        .filter_by(parliamentarian_code=parlamentarian_code)
        .one_or_none()
    )

    if parliamentarian is None:
        logger.warning(
            "Deputado %s não encontrado no banco",
            parlamentarian_code,
        )
        return None, False

    # Buscar discurso existente
    speech_link = payload.get("speech_link") or None
    record = None
    created = False

    if speech_link:
        # speech_link da Câmara histórica é a URL do Diário, compartilhada por
        # vários discursos do mesmo dia -> (parliamentarian_id, speech_link) NÃO
        # é único. Usar .first() em vez de .one_or_none() para não estourar
        # MultipleResultsFound (que travava toda a passada do crawler).
        record = (
            session.query(SpeechesTranscript)
            .filter(SpeechesTranscript.parliamentarian_id == parliamentarian.id)
            .filter(SpeechesTranscript.speech_link == speech_link)
            .order_by(SpeechesTranscript.id)
            .first()
        )
    else:
        # Muitos discursos da Câmara (ex.: 'PELA ORDEM', 'BREVES COMUNICAÇÕES')
        # não têm urlTexto -> speech_link nulo. Sem chave de dedup, cada passada
        # do crawler reinsere o mesmo discurso (duplicação massiva). Deduplicar
        # pela chave natural: parlamentar + data + hora + tipo + texto.
        record = (
            session.query(SpeechesTranscript)
            .filter(SpeechesTranscript.parliamentarian_id == parliamentarian.id)
            .filter(SpeechesTranscript.speech_link.is_(None))
            .filter(SpeechesTranscript.date == payload.get("date"))
            .filter(SpeechesTranscript.hour_minute == payload.get("hour_minute"))
            .filter(SpeechesTranscript.type == payload.get("type"))
            .filter(
                func.coalesce(SpeechesTranscript.speech_text, "")
                == (payload.get("speech_text") or "")
            )
            .order_by(SpeechesTranscript.id)
            .first()
        )

    if record is None:
        record = SpeechesTranscript(
            parliamentarian=parliamentarian,
            speech_link=speech_link,
        )
        session.add(record)
        created = True

    # Atualizar campos mutáveis
    for field in SPEECH_MUTABLE_FIELDS:
        value = payload.get(field)
        if hasattr(record, field):
            setattr(record, field, value)

    return record, created


def _analyze_speech_text(session: Session, record: Any) -> None:
    """Extrai palavras-chave e entidades do discurso (alimenta a nuvem de temas).

    Reaproveita o pipeline de NLP do crawler do Senado. Falha na análise não
    derruba a sincronização: o discurso fica salvo e pode ser reprocessado com
    `scripts.rebuild_speech_text_analysis --only-missing`.
    """
    try:
        from mamute_scrappers.senado_crawler import speechs_transcipts as senado_speeches

        senado_speeches._ensure_db_dependencies()
        senado_speeches.update_speech_text_analysis(session, record)
    except Exception as exc:  # pragma: no cover - depende de spaCy/modelo no ambiente
        logger.warning(
            "Falha na análise textual do discurso %s: %s",
            getattr(record, "id", None),
            exc,
        )


def _speech_text_changed(record: Any) -> bool:
    history = sa_inspect(record).attrs.speech_text.history
    return history.has_changes()


def speeches_transcripts(
    *,
    deputado_id: Optional[int] = None,
    data_inicio: Optional[str] = None,
    data_fim: Optional[str] = None,
    persist: bool = True,
    interactive: bool = False,
) -> None:
    """Busca discursos da Câmara e opcionalmente salva/atualiza o banco.

    Args:
        deputado_id: ID específico de deputado (default: todos)
        data_inicio: Data inicial no formato YYYY-MM-DD (default: últimos 90 dias)
        persist: Se False, apenas exibe payloads (dry-run)
        interactive: Pausa após cada discurso
    """
    logger.info(
        "Iniciando sincronização de discursos da Câmara (persist=%s)",
        persist,
    )

    _ensure_db_dependencies()
    if _SESSION_SCOPE is None:
        raise RuntimeError("Função de sessão do banco não carregada.")

    processed = 0
    inserted = 0
    updated = 0

    try:
        with _SESSION_SCOPE() as session:
            # Buscar lista de deputados
            if deputado_id:
                deputados = (
                    session.query(Parliamentarian)
                    .filter_by(parliamentarian_code=deputado_id, type="Deputado")
                    .all()
                )
                if not deputados:
                    logger.warning("Deputado %s não encontrado no banco", deputado_id)
                    return
            else:
                deputados = (
                    session.query(Parliamentarian)
                    .filter_by(type="Deputado")
                    .order_by(Parliamentarian.parliamentarian_code)
                    .all()
                )

            logger.info("Processando %s deputados", len(deputados))

            for idx, deputado in enumerate(deputados, 1):
                dep_id = deputado.parliamentarian_code
                dep_nome = deputado.name or "Sem nome"

                logger.info(
                    "[%s/%s] Deputado %s: %s",
                    idx,
                    len(deputados),
                    dep_id,
                    dep_nome,
                )

                # Determinar data inicial
                start_date = data_inicio
                if start_date is None and persist:
                    last_date = _get_last_speech_date(session, dep_id)
                    if last_date:
                        # Buscar desde 1 dia antes (margem de segurança)
                        start_date = (last_date - timedelta(days=1)).strftime("%Y-%m-%d")
                        logger.debug("Busca incremental desde: %s", start_date)

                if start_date is None:
                    # Padrão: últimos 90 dias
                    start_date = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")

                dep_processed = 0

                for discurso in _iter_speeches_paginated(dep_id, start_date, data_fim=data_fim):
                    payload = _build_speech_payload(dep_id, discurso)

                    if payload is None:
                        continue

                    processed += 1
                    dep_processed += 1

                    if interactive or not persist:
                        _debug_print_payload(payload)
                        if interactive:
                            try:
                                input("Pressione ENTER para continuar; Ctrl+C para sair.")
                            except (KeyboardInterrupt, EOFError):
                                logger.info("Execução interrompida pelo usuário.")
                                return

                    if persist:
                        result = _upsert_speech(session, payload)
                        if result[0] is not None:
                            record, created = result
                            if created:
                                inserted += 1
                            else:
                                updated += 1
                            if created or _speech_text_changed(record):
                                _analyze_speech_text(session, record)

                if dep_processed > 0:
                    logger.info(
                        "Deputado %s: %s discursos processados",
                        dep_id,
                        dep_processed,
                    )

                # Delay entre deputados
                if REQUEST_DELAY > 0 and idx < len(deputados):
                    time.sleep(REQUEST_DELAY)

    except KeyboardInterrupt:
        logger.info("Execução interrompida pelo usuário após %s discursos.", processed)
        return

    if processed == 0:
        logger.warning("Nenhum discurso retornado pela Câmara")
    elif persist:
        logger.info(
            "Sincronização concluída: %s inseridos, %s atualizados, %s total",
            inserted,
            updated,
            processed,
        )
    else:
        logger.info("Processamento concluído em modo dry-run (%s discursos)", processed)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description="Sincroniza discursos dos deputados da Câmara."
    )
    parser.add_argument(
        "--deputado-id",
        type=int,
        help="ID específico de deputado (default: todos)",
    )
    parser.add_argument(
        "--data-inicio",
        type=str,
        help="Data inicial no formato YYYY-MM-DD (default: últimos 90 dias ou última data)",
    )
    parser.add_argument(
        "--data-fim",
        type=str,
        help="Data final YYYY-MM-DD (a API rejeita intervalos > 4 anos).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Não persiste no banco; apenas exibe os payloads.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Pausa após cada discurso para inspeção manual.",
    )

    args = parser.parse_args()

    speeches_transcripts(
        deputado_id=args.deputado_id,
        data_inicio=args.data_inicio,
        data_fim=args.data_fim,
        persist=not args.dry_run,
        interactive=args.interactive,
    )

