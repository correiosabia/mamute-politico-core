"""Gate de plano nas rotas de dado (CS-58).

O desfoque no front e vitrine, nao seguranca: a fronteira e esta dependency.
Uma rota gatada declara `Depends(emendas_access)` e recebe um FeatureAccess:

* `full=True`  — devolve tudo (plano libera, ou admin);
* `full=False` — devolve a PREVIA: no maximo PREVIEW_ROWS linhas, em ordem
  fixa, IGNORANDO filtros e paginacao do cliente — honrar filtro em previa
  vira oraculo de extracao (enumerar o dataset variando o filtro);
* sem acesso   — 403 antes de tocar a rota.

O header `X-Feature-Preview` (lista de chaves separadas por virgula) forca a
previa e so e honrado para admin: e a lente de inspecao do painel de flags,
nao um modo do produto. Para usuario comum ele e ignorado por completo.

A chave passada a `feature_access` tem de existir no registro do front
(`ui/src/lib/featureFlags.ts`) — mesmo contrato do resto do sistema de
flags: o registro mora la, o banco so guarda estado.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

try:
    from .dependencies import get_db
    from .security import resolve_ghost_admin
    from .services.feature_flags import (
        ACCESS_BLOQUEADA,
        ACCESS_LIBERADA,
        enabled_flags_for_tier,
        resolve_for,
        tier_id_for_email,
    )
except ImportError:  # execução dentro de api/
    from dependencies import get_db
    from security import resolve_ghost_admin
    from services.feature_flags import (
        ACCESS_BLOQUEADA,
        ACCESS_LIBERADA,
        enabled_flags_for_tier,
        resolve_for,
        tier_id_for_email,
    )

# Tamanho da previa. Pequeno de proposito: e vitrine, nao amostra util.
PREVIEW_ROWS = 3


@dataclass(frozen=True)
class FeatureAccess:
    """Resultado do gate, ja resolvido para quem chamou."""

    full: bool


def _chaves_preview(header: str | None) -> set[str]:
    return {k.strip() for k in (header or "").split(",") if k.strip()}


def feature_access(key: str):
    """Fabrica a dependency de uma chave.

    As rotas usam as instancias module-level (`emendas_access`,
    `trajetoria_access`): e por identidade delas que os testes sobrescrevem o
    gate em `dependency_overrides`.
    """

    def dependency(
        request: Request,
        authorization: str | None = Header(default=None),
        x_feature_preview: str | None = Header(default=None),
        db: Session = Depends(get_db),
    ) -> FeatureAccess:
        if resolve_ghost_admin(request, authorization) is not None:
            # Admin ve tudo; o header de preview e a excecao deliberada, para
            # conferir a vitrine antes de vende-la.
            return FeatureAccess(
                full=key not in _chaves_preview(x_feature_preview)
            )

        # As rotas gatadas ja passam pelo verify_token do router, que popula
        # request.state.token_email para qualquer membro autenticado.
        email = getattr(request.state, "token_email", None)
        tier_id = tier_id_for_email(db, email)
        resolvido = resolve_for(
            db, is_admin=False, modos=enabled_flags_for_tier(db, tier_id)
        ).get(key)
        if resolvido == ACCESS_LIBERADA:
            return FeatureAccess(full=True)
        if resolvido == ACCESS_BLOQUEADA:
            return FeatureAccess(full=False)
        raise HTTPException(
            status_code=403, detail="Recurso não disponível no seu plano."
        )

    return dependency


def flag_liberada(key: str, request: Request, authorization: str | None, db: Session) -> bool:
    """Diz se a chave está liberada para quem chamou, sem levantar 403.

    Para detalhe opcional dentro de uma rota já gatada (o placar das missões,
    CS-179): sem acesso, a rota responde igual, só sem o detalhe. Cadeado conta
    como não liberado, porque não há prévia de um número só.
    """
    if resolve_ghost_admin(request, authorization) is not None:
        return resolve_for(db, is_admin=True).get(key) == ACCESS_LIBERADA
    email = getattr(request.state, "token_email", None)
    modos = enabled_flags_for_tier(db, tier_id_for_email(db, email))
    return resolve_for(db, is_admin=False, modos=modos).get(key) == ACCESS_LIBERADA


emendas_access = feature_access("emendas")
trajetoria_access = feature_access("trajetoria")
cota_access = feature_access("cota_parlamentar")
missoes_access = feature_access("missoes_do_dia")

__all__ = [
    "PREVIEW_ROWS",
    "FeatureAccess",
    "feature_access",
    "emendas_access",
    "trajetoria_access",
    "cota_access",
    "missoes_access",
    "flag_liberada",
]
