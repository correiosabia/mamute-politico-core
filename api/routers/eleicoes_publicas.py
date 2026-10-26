"""Página Eleições 2026: versão pública e controle de publicação (CS-162).

* `/public/eleicoes-2026/*`: sem login. Responde 404 enquanto a página está
  "só admin". A comparação entre turnos vai sem os destaques prontos (são
  ferramenta interna para o e-mail).
* `/admin/pages/{chave}`: o admin lê e muda o estado (`admin` | `publica`).
  Gate igual ao resto do painel (404 para não admin).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

try:
    from ..dependencies import get_db
    from ..security import require_ghost_admin
    from ..services.elected_profile import elected_profile
    from ..services.pagina_publicacao import PAGINAS, definir_estado, estado_da_pagina
    from ..services.runoff_comparison import runoff_comparison
    from .admin import _log_admin_action
except ImportError:  # execução dentro de api/
    from dependencies import get_db
    from security import require_ghost_admin
    from services.elected_profile import elected_profile
    from services.pagina_publicacao import PAGINAS, definir_estado, estado_da_pagina
    from services.runoff_comparison import runoff_comparison
    from routers.admin import _log_admin_action

PAGINA = "eleicoes_2026"

public_router = APIRouter(prefix="/public/eleicoes-2026", tags=["public"])
admin_router = APIRouter(prefix="/admin/pages", tags=["admin"])


def _exige_publica(db: Session) -> None:
    if estado_da_pagina(db, PAGINA) != "publica":
        raise HTTPException(status_code=404, detail="Página não encontrada.")


@public_router.get("/elected-profile")
def perfil_publico(db: Session = Depends(get_db)) -> dict[str, Any]:
    _exige_publica(db)
    return elected_profile(db)


@public_router.get("/runoff-comparison")
def comparacao_publica(db: Session = Depends(get_db)) -> dict[str, Any]:
    _exige_publica(db)
    return {**runoff_comparison(db), "destaques": []}


class EstadoIn(BaseModel):
    estado: Literal["admin", "publica"]


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
