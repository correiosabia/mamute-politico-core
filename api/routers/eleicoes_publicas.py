"""Página Eleições 2026: versão pública e controle de publicação (CS-162).

* `/public/eleicoes-2026/*`: sem login. Responde 404 enquanto a página está
  "só admin". A comparação entre turnos vai sem os destaques prontos (são
  ferramenta interna para o e-mail).
* `/admin/pages/{chave}`: o admin lê e muda o estado (`admin` | `publica`).
  Gate igual ao resto do painel (404 para não admin).
* Resultado por município e por local de votação (CS-164):
  `/admin/mapa-resultado/{municipios,locais}` e as versões públicas.
* Métricas anônimas (CS-166): `POST /public/eleicoes-2026/events` (sempre 204;
  só grava com a página pública e de quem não é robô) e o painel em
  `/admin/pages/{chave}/metricas`.
* Votos depois das 17h (CS-163): `/admin/votacao-horario` e a versão
  pública, mais o detalhe por município (`/{uf}/{municipio}`, locais de
  votação). Os dados são públicos do TSE; a versão pública é igual à do admin.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

try:
    from ..dependencies import get_db
    from ..security import require_ghost_admin
    from ..services.elected_profile import elected_profile
    from ..services.pagina_publicacao import PAGINAS, definir_estado, estado_da_pagina
    from ..services.runoff_comparison import runoff_comparison
    from ..services.mapa_resultado import locais_resultado, municipios_resultado
    from ..services.votacao_horario import locais_do_municipio, votacao_horario
    from ..services import collection_events as eventos
    from ..services import page_events
    from .admin import _log_admin_action
    from .collection import LoteEventosIn
except ImportError:  # execução dentro de api/
    from dependencies import get_db
    from security import require_ghost_admin
    from services.elected_profile import elected_profile
    from services.pagina_publicacao import PAGINAS, definir_estado, estado_da_pagina
    from services.runoff_comparison import runoff_comparison
    from services.mapa_resultado import locais_resultado, municipios_resultado
    from services.votacao_horario import locais_do_municipio, votacao_horario
    from services import collection_events as eventos
    from services import page_events
    from routers.admin import _log_admin_action
    from routers.collection import LoteEventosIn

PAGINA = "eleicoes_2026"

public_router = APIRouter(prefix="/public/eleicoes-2026", tags=["public"])
admin_router = APIRouter(prefix="/admin/pages", tags=["admin"])
admin_dados_router = APIRouter(prefix="/admin/votacao-horario", tags=["admin"])
admin_mapa_router = APIRouter(prefix="/admin/mapa-resultado", tags=["admin"])


# A página pública pode ser compartilhada em massa e cada resposta agrega ~11 mil
# municípios: cache curto por processo. O estado de publicação NÃO entra no
# cache, então despublicar vale na hora.
CACHE_SEGUNDOS = 120
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def limpar_cache() -> None:
    _cache.clear()


def _em_cache(nome: str, gerar: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    agora = time.monotonic()
    guardado = _cache.get(nome)
    if guardado and guardado[0] > agora:
        return guardado[1]
    valor = gerar()
    _cache[nome] = (agora + CACHE_SEGUNDOS, valor)
    return valor


def _exige_publica(db: Session) -> None:
    if estado_da_pagina(db, PAGINA) != "publica":
        raise HTTPException(status_code=404, detail="Página não encontrada.")


@public_router.get("/elected-profile")
def perfil_publico(db: Session = Depends(get_db)) -> dict[str, Any]:
    _exige_publica(db)
    return _em_cache("perfil", lambda: elected_profile(db))


@public_router.get("/runoff-comparison")
def comparacao_publica(db: Session = Depends(get_db)) -> dict[str, Any]:
    _exige_publica(db)
    return _em_cache("comparacao", lambda: {**runoff_comparison(db), "destaques": []})


@public_router.get("/votacao-horario")
def votacao_horario_publica(db: Session = Depends(get_db)) -> dict[str, Any]:
    _exige_publica(db)
    return _em_cache("votacao_horario", lambda: votacao_horario(db))


def _locais_ou_404(db: Session, uf: str, municipio: int) -> dict[str, Any]:
    resultado = locais_do_municipio(db, uf, municipio)
    if resultado is None:
        raise HTTPException(status_code=404, detail="Município sem votos depois das 17h.")
    return resultado


@public_router.get("/votacao-horario/{uf}/{municipio}")
def locais_publicos(uf: str, municipio: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    _exige_publica(db)
    return _em_cache(f"locais:{uf.upper()}:{municipio}", lambda: _locais_ou_404(db, uf, municipio))


@admin_dados_router.get("")
def votacao_horario_admin(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return votacao_horario(db)


@admin_dados_router.get("/{uf}/{municipio}")
def locais_admin(
    uf: str,
    municipio: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return _locais_ou_404(db, uf, municipio)


# CS-164: resultado por município e por local de votação, para o zoom dos mapas.


def _area(w: float, s: float, e: float, n: float) -> tuple[float, float, float, float]:
    # Arredonda para o cache servir a quem olha a mesma região.
    return round(w, 2), round(s, 2), round(e, 2), round(n, 2)


@public_router.get("/mapa-resultado/municipios")
def mapa_municipios_publico(db: Session = Depends(get_db)) -> dict[str, Any]:
    _exige_publica(db)
    return _em_cache("mapa_municipios", lambda: municipios_resultado(db))


@public_router.get("/mapa-resultado/locais")
def mapa_locais_publico(w: float, s: float, e: float, n: float, db: Session = Depends(get_db)) -> dict[str, Any]:
    _exige_publica(db)
    area = _area(w, s, e, n)
    return _em_cache(f"mapa_locais:{area}", lambda: locais_resultado(db, *area))


@admin_mapa_router.get("/municipios")
def mapa_municipios_admin(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return municipios_resultado(db)


@admin_mapa_router.get("/locais")
def mapa_locais_admin(
    w: float,
    s: float,
    e: float,
    n: float,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return locais_resultado(db, *_area(w, s, e, n))


class EstadoIn(BaseModel):
    estado: Literal["admin", "publica"]


@public_router.post("/events", status_code=204)
def gravar_eventos(lote: LoteEventosIn, request: Request, db: Session = Depends(get_db)) -> Response:
    """Métricas anônimas da página (CS-166). Sempre 204: o navegador não espera
    resposta, e quem envia lixo não descobre o que foi aceito."""
    user_agent = request.headers.get("user-agent")
    if not eventos.eh_robo(user_agent) and estado_da_pagina(db, PAGINA) == "publica":
        page_events.grava_lote(
            db,
            pagina=PAGINA,
            visitor=lote.visitor,
            session_id=lote.session,
            eventos=[e.model_dump() for e in lote.events],
            user_agent=user_agent,
            ip=eventos.ip_do_cliente(
                request.headers.get("x-forwarded-for"), request.client.host if request.client else None
            ),
            referrer=lote.referrer,
            utm=lote.utm.model_dump(),
        )
    return Response(status_code=204)


def _pagina_conhecida(chave: str) -> None:
    if chave not in PAGINAS:
        raise HTTPException(status_code=404, detail="Página não encontrada.")


@admin_router.get("/{chave}")
def ler_estado(
    chave: str,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, str]:
    _pagina_conhecida(chave)
    return {"chave": chave, "estado": estado_da_pagina(db, chave)}


@admin_router.get("/{chave}/metricas")
def ler_metricas(
    chave: str,
    dias: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    if chave not in page_events.PAGINAS_COM_METRICAS:
        raise HTTPException(status_code=404, detail="Página não encontrada.")
    return page_events.metricas_da_pagina(db, chave, dias)


@admin_router.put("/{chave}")
def mudar_estado(
    chave: str,
    corpo: EstadoIn,
    db: Session = Depends(get_db),
    admin: str = Depends(require_ghost_admin),
) -> dict[str, str]:
    _pagina_conhecida(chave)
    antes = estado_da_pagina(db, chave)
    estado = definir_estado(db, chave, corpo.estado, admin, commit=False)
    _log_admin_action(
        db,
        admin_email=admin,
        action="set_page_visibility",
        entity="pagina_publicacao",
        entity_id=chave,
        before={"estado": antes},
        after={"estado": estado},
    )
    db.commit()
    return {"chave": chave, "estado": estado}
