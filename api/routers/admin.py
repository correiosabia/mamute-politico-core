"""Rotas administrativas — gated por require_ghost_admin (404 para não-admin)."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from requests import RequestException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

try:
    from ..security import require_ghost_admin
    from ..dependencies import get_db
    from ..db.models.project import Projetos, Tiers
    from ..db.models.admin_audit_log import AdminAuditLog
    from ..db.models.feature_flag import FeatureFlag
    from ..db.models.parliamentary_amendment import ParliamentaryAmendment
    from ..services.ghost_admin import (
        generate_admin_token,
        get_ghost_admin_settings,
        set_ghost_tier_active,
    )
    from ..services.ghost_tiers_sync import (
        GhostTiersSyncError,
        run_sync as run_ghost_tiers_sync,
    )
    from ..services.admin_coverage import db_coverage
    from ..services.openrouter_credits import credits_overview
    from ..services.feature_flags import (
        count_tiers_enabled as count_feature_flag_tiers,
        enabled_flags_for_tier,
        get_states as get_feature_flag_states,
        set_state as set_feature_flag_state,
        set_tier_flags,
    )
    from ..services.marcacoes import (
        get_config as get_marcacoes_config,
        set_config as set_marcacoes_config,
    )
    from ..services.editorial_agendas import (
        SlugImutavelError,
        get_agendas as get_editorial_agendas,
        replace_agendas as replace_editorial_agendas,
    )
    from ..services.word_cloud_terms import (
        get_terms as get_word_cloud_terms,
        replace_terms as replace_word_cloud_terms,
    )
    from ..services.admin_metrics import (
        current_period_start,
        get_usd_brl_rate,
        metrics_emails,
        metrics_ia,
        metrics_candidacy_favorites,
        metrics_mamutometro,
        metrics_overview,
        metrics_parliamentarians,
        metrics_sections,
        metrics_tools,
        metrics_user_detail,
        metrics_users,
    )
except ImportError:  # execução dentro de api/
    from security import require_ghost_admin
    from dependencies import get_db
    from db.models.project import Projetos, Tiers
    from db.models.admin_audit_log import AdminAuditLog
    from db.models.feature_flag import FeatureFlag
    from db.models.parliamentary_amendment import ParliamentaryAmendment
    from services.ghost_admin import (
        generate_admin_token,
        get_ghost_admin_settings,
        set_ghost_tier_active,
    )
    from services.ghost_tiers_sync import (
        GhostTiersSyncError,
        run_sync as run_ghost_tiers_sync,
    )
    from services.admin_coverage import db_coverage
    from services.openrouter_credits import credits_overview
    from services.feature_flags import (
        count_tiers_enabled as count_feature_flag_tiers,
        enabled_flags_for_tier,
        get_states as get_feature_flag_states,
        set_state as set_feature_flag_state,
        set_tier_flags,
    )
    from services.marcacoes import (
        get_config as get_marcacoes_config,
        set_config as set_marcacoes_config,
    )
    from services.editorial_agendas import (
        SlugImutavelError,
        get_agendas as get_editorial_agendas,
        replace_agendas as replace_editorial_agendas,
    )
    from services.word_cloud_terms import (
        get_terms as get_word_cloud_terms,
        replace_terms as replace_word_cloud_terms,
    )
    from services.admin_metrics import (
        current_period_start,
        get_usd_brl_rate,
        metrics_emails,
        metrics_ia,
        metrics_candidacy_favorites,
        metrics_mamutometro,
        metrics_overview,
        metrics_parliamentarians,
        metrics_sections,
        metrics_tools,
        metrics_user_detail,
        metrics_users,
    )

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/whoami")
def whoami(admin_email: str = Depends(require_ghost_admin)) -> dict:
    """Valida o gate ponta a ponta: só admin autenticado chega aqui."""
    return {"email": admin_email, "is_admin": True}


class TierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tier_name_debug: str
    product_id: str
    detalhes: dict[str, Any]
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Estado espelhado do Ghost (CS-28). `arquivado` é o status lá; `deleted_at`
    # é o efeito aqui, que só acontece quando não há assinante.
    deleted_at: Optional[datetime] = None
    arquivado: bool = False
    pending_review: bool = False
    orphan: bool = False
    assinantes: int = 0


class TierSyncOut(BaseModel):
    """Resumo do que o sync mudou, para o painel mostrar o resultado."""

    created: list[dict[str, Any]] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    archived: list[dict[str, Any]] = Field(default_factory=list)
    reactivated: list[str] = Field(default_factory=list)
    orphans: list[str] = Field(default_factory=list)


class TierDetailsUpdate(BaseModel):
    # preco_mensal NÃO é editável aqui: vem do Ghost (ghost_tiers_sync). Idem
    # tier_name_debug. O painel os exibe como só-leitura.
    qtd_termos: Optional[int] = Field(default=None, ge=0)
    qtd_termos_camara: Optional[int] = Field(default=None, ge=0)
    qtd_termos_senado: Optional[int] = Field(default=None, ge=0)
    # Candidaturas da eleição de 2026 (CS-62). Default de 10 semeado na
    # migration e1f2a3b4c5d6; a aplicação do limite depende do monitoramento de
    # candidatura, que ainda não existe no backend.
    qtd_candidatos: Optional[int] = Field(default=None, ge=0)
    qtd_consultas_ia_mes: Optional[int] = Field(default=None, ge=0)
    qtd_consultas_ia_semana: Optional[int] = Field(default=None, ge=0)
    # Dias que o plano mostra do histórico da Pesquisa IA (CS-55). O painel
    # sempre enviou o campo; sem ele aqui o Pydantic descartava a chave em
    # silêncio e nenhum plano conseguia ligar o histórico.
    qtd_dias_historico_ia: Optional[int] = Field(default=None, ge=0)
    periodicidade_email: Optional[list[str]] = None
    orgao: Optional[list[str]] = None
    # As feature flags do plano NAO entram aqui: vivem em `feature_flag_tier`,
    # tabela dedicada, e sao editadas por /admin/tiers/{id}/features.


def _log_admin_action(
    db: Session,
    *,
    admin_email: str,
    action: str,
    entity: str,
    entity_id: str,
    before: Any,
    after: Any,
) -> None:
    db.add(
        AdminAuditLog(
            admin_email=admin_email,
            action=action,
            entity=entity,
            entity_id=entity_id,
            before=json.dumps(before, ensure_ascii=False),
            after=json.dumps(after, ensure_ascii=False),
        )
    )


def _tier_out(tier: Tiers, assinantes: int) -> TierOut:
    ghost = tier.detalhes.get("ghost") if isinstance(tier.detalhes, dict) else None
    ghost = ghost if isinstance(ghost, dict) else {}
    return TierOut(
        id=tier.id,
        tier_name_debug=tier.tier_name_debug,
        product_id=tier.product_id,
        detalhes=dict(tier.detalhes or {}),
        created_at=tier.created_at,
        updated_at=tier.updated_at,
        deleted_at=tier.deleted_at,
        arquivado=ghost.get("active") is False,
        pending_review=bool(ghost.get("pending_review")),
        orphan=bool(ghost.get("orphan")),
        assinantes=assinantes,
    )


def _assinantes_por_tier(db: Session) -> dict[Any, int]:
    stmt = (
        select(Projetos.tier_id, func.count(Projetos.id))
        .where(Projetos.deleted_at.is_(None), Projetos.tier_id.is_not(None))
        .group_by(Projetos.tier_id)
    )
    return {tier_id: total for tier_id, total in db.execute(stmt).all()}


@router.get("/tiers", response_model=list[TierOut])
def list_tiers(
    include_archived: bool = False,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> list[TierOut]:
    """Lista os planos. `include_archived` traz também os arquivados no Ghost."""
    stmt = select(Tiers)
    if not include_archived:
        stmt = stmt.where(Tiers.deleted_at.is_(None))
    stmt = stmt.order_by(Tiers.id)

    assinantes = _assinantes_por_tier(db)
    tiers = list(db.execute(stmt).scalars().all())
    return [_tier_out(tier, assinantes.get(tier.id, 0)) for tier in tiers]


@router.post("/tiers/sync", response_model=TierSyncOut)
def sync_tiers_route(
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> TierSyncOut:
    """Puxa o catálogo do Ghost na hora, sem esperar o cron das 04h15."""
    try:
        summary = run_ghost_tiers_sync(db)
    except GhostTiersSyncError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Falha ao consultar os planos no Ghost.",
        ) from exc

    resultado = summary.as_dict()
    if summary.changed:
        _log_admin_action(
            db,
            admin_email=admin_email,
            action="sync_tiers",
            entity="tiers",
            entity_id="*",
            before=None,
            after=resultado,
        )
        db.commit()
    return TierSyncOut(**resultado)


@router.post("/tiers/{tier_id}/unarchive", response_model=TierOut)
def unarchive_tier(
    tier_id: int,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> TierOut:
    """Reativa o plano no Ghost e traz o catálogo de volta.

    O status é do Ghost, não do painel: por isso a reativação é escrita lá e o
    estado local só reflete o que voltou.
    """
    tier = db.get(Tiers, tier_id)
    if tier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tier não encontrado."
        )

    detalhes = dict(tier.detalhes or {})
    ghost = detalhes.get("ghost") if isinstance(detalhes.get("ghost"), dict) else {}
    ghost_tier_id = ghost.get("target_tier_id") or (
        tier.product_id if tier.product_id != "free" else None
    )
    if not ghost_tier_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Plano sem id do Ghost; não dá para reativar por aqui.",
        )

    settings = get_ghost_admin_settings()
    if settings is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GHOST_API/GHOST_ADMIN_URL ausentes.",
        )

    try:
        token = generate_admin_token(settings.api_key)
        set_ghost_tier_active(settings.admin_url, token, str(ghost_tier_id), True)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plano não existe mais no Ghost.",
        ) from exc
    except RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Falha ao reativar o plano no Ghost.",
        ) from exc

    _log_admin_action(
        db,
        admin_email=admin_email,
        action="unarchive_tier",
        entity="tiers",
        entity_id=str(tier_id),
        before={"deleted_at": str(tier.deleted_at), "ghost": ghost},
        after={"active": True},
    )
    db.commit()

    try:
        run_ghost_tiers_sync(db)
    except (GhostTiersSyncError, RequestException):
        # O Ghost já aceitou a reativação; o cron reconcilia o resto.
        logger.warning("Reativação gravada no Ghost, mas o re-sync falhou.", exc_info=True)

    db.refresh(tier)
    assinantes = _assinantes_por_tier(db).get(tier.id, 0)
    return _tier_out(tier, assinantes)


@router.put("/tiers/{tier_id}", response_model=TierOut)
def update_tier(
    tier_id: int,
    payload: TierDetailsUpdate,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> Tiers:
    tier = db.get(Tiers, tier_id)
    if tier is None or tier.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tier não encontrado.")

    before = dict(tier.detalhes or {})
    updates = payload.model_dump(exclude_unset=True)
    new_detalhes = dict(before)
    new_detalhes.update(updates)
    tier.detalhes = new_detalhes

    _log_admin_action(
        db,
        admin_email=admin_email,
        action="update_tier",
        entity="tiers",
        entity_id=str(tier_id),
        before=before,
        after=new_detalhes,
    )
    db.commit()
    db.refresh(tier)
    return tier


@router.get("/metrics/overview")
def metrics_overview_route(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return metrics_overview(db, current_period_start(), get_usd_brl_rate(db))


@router.get("/metrics/users")
def metrics_users_route(
    limit: Optional[int] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    period = current_period_start()
    rate = get_usd_brl_rate(db)
    return {
        "period_start": period.isoformat(),
        "usd_brl_rate": round(rate, 4),
        "users": metrics_users(db, period, rate, limit=limit, search=search),
    }


@router.get("/metrics/tools")
def metrics_tools_route(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return {"tools": metrics_tools(db)}


@router.get("/metrics/sections")
def metrics_sections_route(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return {"sections": metrics_sections(db)}


@router.get("/metrics/parliamentarians")
def metrics_parliamentarians_route(
    limit: int = 20,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return metrics_parliamentarians(db, limit=limit)


@router.get("/metrics/mamutometro")
def metrics_mamutometro_route(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return metrics_mamutometro(db)


@router.get("/metrics/candidacies")
def metrics_candidacies_route(
    limit: int = 20,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return metrics_candidacy_favorites(db, limit=limit)


@router.get("/metrics/ia")
def metrics_ia_route(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return metrics_ia(db, current_period_start(), get_usd_brl_rate(db))


@router.get("/metrics/emails")
def metrics_emails_route(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return metrics_emails(db)


@router.get("/coverage")
def coverage_route(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    return db_coverage(db)


@router.get("/metrics/users/{projeto_id}")
def metrics_user_detail_route(
    projeto_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    detail = metrics_user_detail(
        db, projeto_id, current_period_start(), get_usd_brl_rate(db)
    )
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuário não encontrado.")
    return detail


class WordCloudTermsUpdate(BaseModel):
    """Listas completas — a tela edita e salva de uma vez."""

    stopwords: list[str] = Field(default_factory=list)
    excluded_terms: list[str] = Field(default_factory=list)


class WordCloudTermsOut(BaseModel):
    stopwords: list[str]
    excluded_terms: list[str]


@router.get("/settings/word-cloud-terms", response_model=WordCloudTermsOut)
def read_word_cloud_terms_admin(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, list[str]]:
    return get_word_cloud_terms(db)


@router.put("/settings/word-cloud-terms", response_model=WordCloudTermsOut)
def update_word_cloud_terms_route(
    payload: WordCloudTermsUpdate,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> dict[str, list[str]]:
    before = get_word_cloud_terms(db)
    after = replace_word_cloud_terms(db, payload.stopwords, payload.excluded_terms)

    _log_admin_action(
        db,
        admin_email=admin_email,
        action="update_word_cloud_terms",
        entity="word_cloud_terms",
        entity_id="global",
        before=before,
        after=after,
    )
    db.commit()
    return after


class EditorialAgendaIn(BaseModel):
    """Uma pauta na lista que a tela salva de uma vez.

    `id` ausente = pauta nova. `id` presente com `slug` diferente do gravado é
    recusado: as classificações já feitas apontam para o slug antigo.
    """

    id: Optional[int] = None
    name: str
    slug: str
    description: Optional[str] = None
    active: bool = True


class EditorialAgendasUpdate(BaseModel):
    """Vocabulário completo — a ordem do array vira a `position`."""

    agendas: list[EditorialAgendaIn] = Field(default_factory=list)


class EditorialAgendaOut(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str] = None
    position: int
    active: bool
    vocabulary_version: int


@router.get("/settings/editorial-agendas", response_model=list[EditorialAgendaOut])
def read_editorial_agendas_admin(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> list[dict[str, Any]]:
    """Vocabulário completo, inclusive as pautas desativadas.

    A tela precisa das inativas para poder reativar — a leitura pública em
    `/settings/editorial-agendas` mostra só as ativas.
    """

    return get_editorial_agendas(db, incluir_inativas=True)


@router.put("/settings/editorial-agendas", response_model=list[EditorialAgendaOut])
def update_editorial_agendas_route(
    payload: EditorialAgendasUpdate,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> list[dict[str, Any]]:
    """Substitui o vocabulário inteiro.

    Pauta omitida é desativada, nunca apagada, e mudança real sobe a
    `vocabulary_version` — que é o gatilho da reclassificação do job.
    """

    before = get_editorial_agendas(db, incluir_inativas=True)
    try:
        after = replace_editorial_agendas(db, payload.agendas)
    except SlugImutavelError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    _log_admin_action(
        db,
        admin_email=admin_email,
        action="update_editorial_agendas",
        entity="editorial_agenda",
        entity_id="global",
        before=before,
        after=after,
    )
    db.commit()
    return after


class FeatureFlagUpdate(BaseModel):
    state: Literal["off", "admins", "all"]


class FeatureFlagOut(BaseModel):
    key: str
    state: str
    updated_at: Optional[datetime] = None
    # Quantos planos ativos liberam / mostram cadeado. Denuncia o caso
    # silencioso: flag em `all` sem nenhum plano nao aparece para ninguem.
    tiers_liberados: int = 0
    tiers_cadeado: int = 0
    tiers_total: int = 0


@router.get("/settings/feature-flags", response_model=list[FeatureFlagOut])
def read_feature_flags_admin(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> list[dict]:
    """Tri-estado cru de cada flag gravada.

    Devolve só o que está no banco. Quem decide o que aparece na tela é o
    registro do front: chave sem linha vale `off`, e linha sem chave no
    registro (flag já removida do código) simplesmente não é exibida.
    """
    quando = {
        linha.key: linha.updated_at
        for linha in db.execute(select(FeatureFlag)).scalars()
    }
    ligados = count_feature_flag_tiers(db)
    total = db.execute(
        select(func.count(Tiers.id)).where(Tiers.deleted_at.is_(None))
    ).scalar_one()
    return [
        {
            "key": key,
            "state": state,
            "updated_at": quando.get(key),
            "tiers_liberados": ligados.get(key, {}).get("liberado", 0),
            "tiers_cadeado": ligados.get(key, {}).get("cadeado", 0),
            "tiers_total": total,
        }
        for key, state in sorted(get_feature_flag_states(db).items())
    ]


@router.put("/settings/feature-flags/{key}", response_model=FeatureFlagOut)
def update_feature_flag_route(
    key: str,
    payload: FeatureFlagUpdate,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> dict:
    antes = get_feature_flag_states(db).get(key, "off")
    depois = set_feature_flag_state(db, key, payload.state)

    _log_admin_action(
        db,
        admin_email=admin_email,
        action="update_feature_flag",
        entity="feature_flag",
        entity_id=key,
        before={"state": antes},
        after={"state": payload.state},
    )
    db.commit()
    return depois


class TierFeaturesUpdate(BaseModel):
    """Mapa completo recurso -> modo do plano. Salvar substitui tudo.

    Chave ausente = oculto no plano; 'cadeado' = entrada visivel com previa
    desfocada (CS-58); 'liberado' = acesso pleno.
    """

    features: dict[str, Literal["liberado", "cadeado"]] = Field(
        default_factory=dict
    )


class TierFeaturesOut(BaseModel):
    tier_id: int
    features: dict[str, str]


@router.get("/tiers/{tier_id}/features", response_model=TierFeaturesOut)
def read_tier_features(
    tier_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict:
    """Features liberadas para um plano.

    O tri-estado global (`off`/`admins`/`all`) fica em /admin/configuracoes e
    e o ciclo de vida do lancamento. Aqui se decide quem recebe depois que a
    feature saiu da previa.
    """
    tier = db.get(Tiers, tier_id)
    if tier is None or tier.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tier não encontrado."
        )
    return {"tier_id": tier_id, "features": enabled_flags_for_tier(db, tier_id)}


@router.put("/tiers/{tier_id}/features", response_model=TierFeaturesOut)
def update_tier_features(
    tier_id: int,
    payload: TierFeaturesUpdate,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> dict:
    tier = db.get(Tiers, tier_id)
    if tier is None or tier.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tier não encontrado."
        )

    before = enabled_flags_for_tier(db, tier_id)
    after = set_tier_flags(db, tier_id, payload.features)

    _log_admin_action(
        db,
        admin_email=admin_email,
        action="update_tier_features",
        entity="feature_flag_tier",
        entity_id=str(tier_id),
        before={"features": before},
        after={"features": after},
    )
    db.commit()
    return {"tier_id": tier_id, "features": after}


@router.get("/metrics/credits")
def metrics_credits_route(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, Any]:
    """Saldo do OpenRouter e repartição do gasto entre chatbot e embeddings."""

    return credits_overview(db)


_MONEY_CENTS = Decimal("0.01")


def _to_money(value: Any) -> Decimal:
    """Normaliza soma monetaria para Decimal com 2 casas.

    O SQLite devolve float em SUM(); o Postgres devolve Decimal. Passar por str
    evita a expansao binaria de Decimal(float).
    """
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value.quantize(_MONEY_CENTS)
    return Decimal(str(value)).quantize(_MONEY_CENTS)


class UnmatchedAuthorOut(BaseModel):
    """Autor de emenda que o casamento automatico nao resolveu."""

    author_name_raw: Optional[str] = None
    amendment_count: int
    committed_total: str
    match_status: str


@router.get("/amendments/unmatched", response_model=list[UnmatchedAuthorOut])
def list_unmatched_amendment_authors(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> list[UnmatchedAuthorOut]:
    """Autores nao casados, agrupados e ordenados por valor.

    O Portal da Transparencia so devolve o nome do autor em texto livre, entao
    parte das emendas nunca casa automaticamente — em geral quem ja deixou o
    mandato. Esta rota existe para que esse residuo seja visivel e auditavel,
    em vez de sumir silenciosamente.
    """
    total = func.coalesce(func.sum(ParliamentaryAmendment.committed_value), 0)
    stmt = (
        select(
            ParliamentaryAmendment.author_name_raw,
            ParliamentaryAmendment.match_status,
            func.count(ParliamentaryAmendment.id).label("amendment_count"),
            total.label("committed_total"),
        )
        .where(ParliamentaryAmendment.match_status.in_(("unmatched", "ambiguous")))
        .group_by(
            ParliamentaryAmendment.author_name_raw,
            ParliamentaryAmendment.match_status,
        )
        .order_by(total.desc())
    )

    return [
        UnmatchedAuthorOut(
            author_name_raw=row.author_name_raw,
            match_status=row.match_status,
            amendment_count=row.amendment_count,
            committed_total=str(_to_money(row.committed_total)),
        )
        for row in db.execute(stmt)
    ]


class MarcacoesConfigOut(BaseModel):
    """Configuração das marcações pessoais, como o painel a edita."""

    mamutometro_max_level: int
    mamutometro_notice_text: str
    mamutometro_escopo: str
    tags_escopo: str
    updated_at: Optional[datetime] = None


class MarcacoesConfigUpdate(BaseModel):
    # Tamanho da régua (1..5). Quantos POLÍTICOS cada plano pode marcar não
    # está aqui: é teto comercial e vive em `qtd_mamutometro`, na tela de tiers,
    # junto de `qtd_termos`.
    mamutometro_max_level: int
    mamutometro_notice_text: str
    mamutometro_escopo: Literal["monitorados", "todos"]
    tags_escopo: Literal["monitorados", "todos"]


def _serializar_marcacoes_config(config) -> dict:
    return {
        "mamutometro_max_level": int(config.mamutometro_max_level),
        "mamutometro_notice_text": config.mamutometro_notice_text,
        "mamutometro_escopo": config.mamutometro_escopo,
        "tags_escopo": config.tags_escopo,
        "updated_at": getattr(config, "updated_at", None),
    }


@router.get("/settings/marcacoes", response_model=MarcacoesConfigOut)
def read_marcacoes_config_admin(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict:
    return _serializar_marcacoes_config(get_marcacoes_config(db))


@router.put("/settings/marcacoes", response_model=MarcacoesConfigOut)
def update_marcacoes_config_route(
    payload: MarcacoesConfigUpdate,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> dict:
    """Grava a configuração das marcações.

    Nada aqui apaga marcação de assinante: reduzir a régua ou apertar o escopo
    só muda o que a tela mostra. O dado fica dormente e volta se a configuração
    voltar — ver a seção de marcações pessoais em `api/README.md`.
    """
    before = _serializar_marcacoes_config(get_marcacoes_config(db))
    try:
        depois = set_marcacoes_config(
            db,
            mamutometro_max_level=payload.mamutometro_max_level,
            mamutometro_notice_text=payload.mamutometro_notice_text,
            mamutometro_escopo=payload.mamutometro_escopo,
            tags_escopo=payload.tags_escopo,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    after = _serializar_marcacoes_config(depois)
    _log_admin_action(
        db,
        admin_email=admin_email,
        action="update_marcacoes_config",
        entity="marcacoes_config",
        entity_id="global",
        before=before,
        after=after,
    )
    db.commit()
    return after
