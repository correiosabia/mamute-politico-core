"""Configurações globais lidas pelo app (não-admin).

A escrita fica em `/admin/settings/*`, atrás do gate de administrador.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

try:
    from ..dependencies import get_db
    from ..security import resolve_ghost_admin
    from ..services.feature_flags import (
        enabled_flags_for_tier,
        resolve_for,
        tier_id_for_email,
    )
    from ..services.marcacoes import (
        get_config as get_marcacoes_config,
        mamutometro_habilitado,
        mamutometro_limite,
        mamutometro_usados,
    )
    from ..db.models.project import Projetos
    from ..services.editorial_agendas import get_agendas
    from ..services.word_cloud_terms import get_terms
except ImportError:  # execução dentro de api/
    from dependencies import get_db
    from security import resolve_ghost_admin
    from services.feature_flags import (
        enabled_flags_for_tier,
        resolve_for,
        tier_id_for_email,
    )
    from services.marcacoes import (
        get_config as get_marcacoes_config,
        mamutometro_habilitado,
        mamutometro_limite,
        mamutometro_usados,
    )
    from db.models.project import Projetos
    from services.editorial_agendas import get_agendas
    from services.word_cloud_terms import get_terms

router = APIRouter(prefix="/settings", tags=["settings"])


class WordCloudTermsOut(BaseModel):
    stopwords: list[str]
    excluded_terms: list[str]


@router.get("/word-cloud-terms", response_model=WordCloudTermsOut)
def read_word_cloud_terms(db: Session = Depends(get_db)) -> dict:
    return get_terms(db)


class EditorialAgendaOut(BaseModel):
    """Pauta do vocabulário fechado, para o filtro de Temas."""

    id: int
    name: str
    slug: str
    description: Optional[str] = None
    position: int


@router.get("/editorial-agendas", response_model=list[EditorialAgendaOut])
def read_editorial_agendas(db: Session = Depends(get_db)) -> list[dict]:
    """Só as pautas ativas, na ordem em que a tela lista os filtros.

    Devolve `[]` — e não 500 — enquanto a tabela não existe: o deploy aplica
    as migrations depois de subir os containers.
    """

    return get_agendas(db)


@router.get("/feature-flags")
def read_feature_flags(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Acesso às flags já resolvido para quem chamou.

    Devolve `'liberada' | 'bloqueada' | 'oculta'`, e não o estado cru: quem
    não é admin não precisa saber que a flag existe em modo `admins`, e o
    front não repete a regra. `'bloqueada'` é o cadeado da CS-58 — entrada
    visível, conteúdo em prévia desfocada.

    Isto controla a exibição na interface; a fronteira de segurança
    correspondente é a dependency de `api/feature_gate.py` nas rotas de dado.
    """
    is_admin = resolve_ghost_admin(request, authorization) is not None
    email = getattr(request.state, "token_email", None)
    tier_id = tier_id_for_email(db, email)
    return resolve_for(
        db,
        is_admin=is_admin,
        modos=enabled_flags_for_tier(db, tier_id),
    )


class MamutometroSettingsOut(BaseModel):
    """Mamutômetro já resolvido para quem chamou."""

    # O plano tem a feature? Vem da flag por plano — a UI não repete a regra.
    enabled: bool
    # Tamanho da régua: quantos mamutes a escala tem. Global, não por plano.
    max_level: int
    # Aviso neutro de primeira utilização, editável pelo painel.
    notice_text: str
    # 'monitorados' | 'todos'
    escopo: str
    # Teto de marcações do plano; null = sem teto.
    limit: Optional[int] = None
    used: int = 0


class TagsSettingsOut(BaseModel):
    escopo: str


class MarcacoesSettingsOut(BaseModel):
    mamutometro: MamutometroSettingsOut
    tags: TagsSettingsOut


@router.get("/marcacoes", response_model=MarcacoesSettingsOut)
def read_marcacoes_settings(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> MarcacoesSettingsOut:
    """Configuração das marcações pessoais, resolvida para quem chamou.

    Devolve o resultado, não a regra: `enabled` já considera o plano, e
    `limit`/`used` já são os do assinante. Mesmo princípio do `useFeatureFlag` —
    quanto menor o formato no call site, mais barata é a remoção depois.
    """
    config = get_marcacoes_config(db)
    is_admin = resolve_ghost_admin(request, authorization) is not None
    email = getattr(request.state, "token_email", None)

    projeto = None
    if email:
        projeto = db.execute(
            select(Projetos).where(
                Projetos.email == email,
                Projetos.deleted_at.is_(None),
            )
        ).scalar_one_or_none()

    habilitado = (
        mamutometro_habilitado(db, projeto, is_admin=is_admin)
        if projeto is not None
        else is_admin
    )

    return MarcacoesSettingsOut(
        mamutometro=MamutometroSettingsOut(
            enabled=habilitado,
            max_level=int(config.mamutometro_max_level),
            notice_text=config.mamutometro_notice_text,
            escopo=config.mamutometro_escopo,
            limit=mamutometro_limite(projeto) if projeto is not None else None,
            used=mamutometro_usados(db, int(projeto.id)) if projeto is not None else 0,
        ),
        tags=TagsSettingsOut(escopo=config.tags_escopo),
    )
