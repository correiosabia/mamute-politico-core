"""Cliente HTTP da DivulgaCandContas (TSE).

API publica sem chave e sem SLA. Endpoints validados ao vivo em 2026-08-07:
`/eleicao/ordinarias` (id da eleicao geral 2026 = 20322002026),
`/candidatura/listar/...` (CPF vem nulo aqui) e `/candidatura/buscar/...`
(detalhe, com CPF, foto e dataUltimaAtualizacao).

O CDN (Akamai) do TSE passou a recusar (403) o TLS do Python: em 10/10/2026
`requests`, `httpx` e `urllib` levavam 403 com qualquer conjunto de headers,
enquanto o curl passava. Por isso o cliente usa `curl_cffi` imitando o Chrome
(TLS e HTTP/2 iguais aos do navegador). Sem isso, todo detalhe falhava em
silencio e o job da timeline rodava o dia inteiro (CS-126).

Falha persistente de LISTAGEM levanta IncompleteListingError: truncar em
silencio deixaria a eleicao incompleta com cara de sucesso — o mesmo bug do
504 das emendas 2022. Falha de DETALHE devolve None: o registro fica sem
fingerprint e a proxima execucao tenta de novo.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests
from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

BASE_URL = "https://divulgacandcontas.tse.jus.br/divulga/rest/v1"
DEFAULT_REQUEST_DELAY = 0.5
REQUEST_TIMEOUT = 60
MAX_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 5
# User-Agent e os sec-ch-* vem do perfil do navegador imitado.
IMPERSONATE = "chrome"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": "https://divulgacandcontas.tse.jus.br/divulga/",
}
# Erros de rede dos dois clientes (o requests fica para os testes e o tipo base).
_ERROS_DE_REDE = (requests.RequestException, cffi_requests.RequestsError)


class IncompleteListingError(RuntimeError):
    """Uma listagem UF x cargo nao pode ser lida; a eleicao ficaria incompleta."""


class _Bloqueado(Exception):
    """403 do CDN: repetir em segundos nao muda a resposta."""


class DivulgaCandClient:
    def __init__(self, *, request_delay: float = DEFAULT_REQUEST_DELAY) -> None:
        self._request_delay = request_delay

    def _get_json(self, url: str) -> Optional[Any]:
        try:
            response = cffi_requests.get(
                url,
                headers=HEADERS,
                impersonate=IMPERSONATE,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 403:
                raise _Bloqueado(url)
            response.raise_for_status()
            return response.json()
        except (*_ERROS_DE_REDE, ValueError) as exc:
            logger.error("Falha na DivulgaCandContas (%s): %s", url, exc)
            return None

    def _get_json_with_retry(self, url: str) -> Optional[Any]:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                data = self._get_json(url)
            except _Bloqueado:
                logger.error("DivulgaCandContas recusou (403) %s; sem nova tentativa.", url)
                return None
            if data is not None:
                if self._request_delay:
                    time.sleep(self._request_delay)
                return data
            if attempt < MAX_ATTEMPTS:
                wait = RETRY_BACKOFF_SECONDS * attempt
                logger.warning(
                    "Repetindo %s em %ss (tentativa %s/%s)",
                    url,
                    wait,
                    attempt,
                    MAX_ATTEMPTS,
                )
                time.sleep(wait)
        return None

    def find_general_election_id(self, year: int) -> Optional[int]:
        data = self._get_json_with_retry(f"{BASE_URL}/eleicao/ordinarias")
        if not isinstance(data, list):
            return None
        for election in data:
            if not isinstance(election, dict):
                continue
            if election.get("ano") == year and election.get("tipoAbrangencia") == "F":
                return election.get("id")
        return None

    def list_candidates(
        self, year: int, state: str, election_id: int, office_code: int
    ) -> List[Dict[str, Any]]:
        url = (
            f"{BASE_URL}/candidatura/listar/{year}/{state}/"
            f"{election_id}/{office_code}/candidatos"
        )
        data = self._get_json_with_retry(url)
        if data is None:
            raise IncompleteListingError(
                f"Listagem {state} cargo {office_code} ilegivel apos "
                f"{MAX_ATTEMPTS} tentativas; eleicao {year} ficaria incompleta."
            )
        candidates = data.get("candidatos") if isinstance(data, dict) else None
        if not isinstance(candidates, list):
            return []
        return [item for item in candidates if isinstance(item, dict)]

    def get_candidate_detail(
        self, year: int, state: str, election_id: int, candidate_id: int
    ) -> Optional[Dict[str, Any]]:
        url = (
            f"{BASE_URL}/candidatura/buscar/{year}/{state}/"
            f"{election_id}/candidato/{candidate_id}"
        )
        data = self._get_json_with_retry(url)
        return data if isinstance(data, dict) else None


__all__ = [
    "BASE_URL",
    "DEFAULT_REQUEST_DELAY",
    "DivulgaCandClient",
    "IncompleteListingError",
    "MAX_ATTEMPTS",
]
