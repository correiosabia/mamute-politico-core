"""Cards de números e destaques da home (CS-170). Sem login.

`GET /public/home` devolve os números (do snapshot diário de cobertura) e o
estado de publicação:

- `publicada`: o admin liberou os cards novos para todo mundo (página
  `home_numeros` em pagina_publicacao, controlada em Admin > Cobertura).
  Enquanto for `false`, a home mostra os cards antigos a quem não é admin.
- `eleicoes_2026_publica`: o card que leva a /eleicoes-2026 só aparece com a
  página pública, para não mandar o visitante a uma página fechada.

Os números não são segredo (vêm da mesma base que o site já mostra), então a
rota responde sempre; o portão é de apresentação, como nas feature flags.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

try:
    from ..dependencies import get_db
    from ..services.home_numeros import home_numeros
    from ..services.pagina_publicacao import estado_da_pagina
except ImportError:  # execução dentro de api/
    from dependencies import get_db
    from services.home_numeros import home_numeros
    from services.pagina_publicacao import estado_da_pagina

PAGINA = "home_numeros"
PAGINA_ELEICOES = "eleicoes_2026"

public_router = APIRouter(prefix="/public/home", tags=["public"])

# A home é a página mais acessada e o snapshot só muda uma vez por dia: cache
# por processo. O estado de publicação fica fora do cache — publicar ou
# despublicar vale na hora.
CACHE_SEGUNDOS = 600
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def limpar_cache() -> None:
    _cache.clear()


def _numeros_em_cache(db: Session) -> dict[str, Any]:
    agora = time.monotonic()
    guardado = _cache.get("numeros")
    if guardado and guardado[0] > agora:
        return guardado[1]
    valor = home_numeros(db)
    _cache["numeros"] = (agora + CACHE_SEGUNDOS, valor)
    return valor


@public_router.get("")
def numeros_da_home(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {
        **_numeros_em_cache(db),
        "publicada": estado_da_pagina(db, PAGINA) == "publica",
        "eleicoes_2026_publica": estado_da_pagina(db, PAGINA_ELEICOES) == "publica",
    }
