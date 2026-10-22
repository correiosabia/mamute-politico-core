"""Peças editáveis do relatório por e-mail (admin) e imagens públicas (sem login)."""

from __future__ import annotations

import base64
import binascii
import re

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

try:
    from ..dependencies import get_db
    from ..security import require_ghost_admin
    from ..services.email_settings import (
        EmailSettingsError,
        get_email_settings,
        load_public_image,
        save_email_settings,
        store_public_image,
    )
    from .admin import _log_admin_action
except ImportError:  # execução dentro de api/
    from dependencies import get_db
    from security import require_ghost_admin
    from services.email_settings import (
        EmailSettingsError,
        get_email_settings,
        load_public_image,
        save_email_settings,
        store_public_image,
    )
    from routers.admin import _log_admin_action

admin_router = APIRouter(prefix="/admin/settings/email", tags=["admin"])
public_router = APIRouter(prefix="/public-images", tags=["public"])

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EmailSettingsOut(BaseModel):
    banner_image_url: str
    banner_link_url: str
    footer_image_url: str
    footer_link_url: str
    instagram_url: str
    subscribe_url: str
    share_text: str


class EmailSettingsUpdate(BaseModel):
    """Só os campos enviados mudam; campo desconhecido é recusado (422)."""

    model_config = ConfigDict(extra="forbid")

    banner_image_url: str | None = None
    banner_link_url: str | None = None
    footer_image_url: str | None = None
    footer_link_url: str | None = None
    instagram_url: str | None = None
    subscribe_url: str | None = None
    share_text: str | None = None


class ImageUpload(BaseModel):
    content_type: str
    data_base64: str


class ImageUploadOut(BaseModel):
    url: str


@admin_router.get("", response_model=EmailSettingsOut)
def read_email_settings(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, str]:
    return get_email_settings(db)


@admin_router.put("", response_model=EmailSettingsOut)
def update_email_settings(
    payload: EmailSettingsUpdate,
    db: Session = Depends(get_db),
    admin_email: str = Depends(require_ghost_admin),
) -> dict[str, str]:
    before = get_email_settings(db)
    try:
        after = save_email_settings(db, payload.model_dump(exclude_none=True), admin_email)
    except EmailSettingsError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _log_admin_action(
        db,
        admin_email=admin_email,
        action="update_email_settings",
        entity="email_settings",
        entity_id="global",
        before=before,
        after=after,
    )
    db.commit()
    return after


@admin_router.post("/images", response_model=ImageUploadOut)
def upload_email_image(
    payload: ImageUpload,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_ghost_admin),
) -> dict[str, str]:
    try:
        data = base64.b64decode(payload.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="Não foi possível ler a imagem. Tente enviar de novo."
        ) from exc
    try:
        url = store_public_image(db, payload.content_type, data)
    except EmailSettingsError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {"url": url}


@public_router.get("/{sha}")
def read_public_image(sha: str, db: Session = Depends(get_db)) -> Response:
    imagem = load_public_image(db, sha) if _SHA256.match(sha) else None
    if imagem is None:
        raise HTTPException(status_code=404, detail="Imagem não encontrada.")
    content_type, data = imagem
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
