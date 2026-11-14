"""Números dos cards da home (CS-170), lidos do snapshot de cobertura.

O cálculo pesado já roda na rotina diária (refresh_admin_caches, 04h) e fica em
coverage_snapshot — o mesmo levantamento da tela Admin > Cobertura. Aqui só
extraímos os dois números da home:

- `parlamentares`: deputados/as + senadores/as da base, contando suplentes;
- `registros_oficiais`: total de registros tirados de fonte oficial.

Número que o snapshot ainda não tem (rotina não rodou depois do deploy) volta
`None`, nunca zero: a home esconde o número em vez de mostrar "0".
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

try:
    from .admin_coverage import db_coverage
except ImportError:  # execução dentro de api/
    from services.admin_coverage import db_coverage


def _inteiro_positivo(valor: Any) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        return None
    return valor


def home_numeros(db: Session) -> dict[str, Any]:
    snapshot = db_coverage(db)

    parlamentares = snapshot.get("parlamentares") or {}
    deputados = _inteiro_positivo((parlamentares.get("deputados") or {}).get("nossa_base"))
    senadores = _inteiro_positivo((parlamentares.get("senadores") or {}).get("nossa_base"))
    total_parlamentares = deputados + senadores if deputados and senadores else None

    registros = _inteiro_positivo((snapshot.get("registros_oficiais") or {}).get("total"))

    return {
        "parlamentares": total_parlamentares,
        "registros_oficiais": registros,
        "atualizado_em": snapshot.get("computed_at"),
    }
