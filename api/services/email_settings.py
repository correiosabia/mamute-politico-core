"""Peças editáveis do relatório por e-mail e imagens públicas enviadas pelo admin.

Chave/valor com chaves fixas: o código do envio conhece cada uma e um bloco
com chave vazia simplesmente não aparece no e-mail. As imagens ficam no banco
(e não em disco) para sobreviver a deploy e entrar no backup; a URL é o hash
do conteúdo, então o cliente de e-mail pode guardar em cache para sempre.

Tabelas da migration cs116; o deploy sobe o código antes do alembic, então
leitura sem tabela devolve tudo vazio em vez de quebrar.
"""

from __future__ import annotations

import hashlib
from typing import Mapping, Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

EMAIL_SETTING_KEYS = (
    "banner_image_url",
    "banner_link_url",
    "footer_image_url",
    "footer_link_url",
    "instagram_url",
    "subscribe_url",
    "share_text",
)
_URL_KEYS = frozenset(k for k in EMAIL_SETTING_KEYS if k.endswith("_url"))
_SHARE_TEXT_MAX = 500

IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
IMAGE_MAX_BYTES = 1024 * 1024
PUBLIC_IMAGE_PATH = "/api/public-images/"


class EmailSettingsError(ValueError):
    """Entrada recusada; a mensagem vai para a tela do admin."""


def get_email_settings(db: Session) -> dict[str, str]:
    valores = {k: "" for k in EMAIL_SETTING_KEYS}
    try:
        linhas = db.execute(text("SELECT key, value FROM email_settings")).all()
    except SQLAlchemyError:
        db.rollback()
        return valores
    for key, value in linhas:
        if key in valores:
            valores[key] = value or ""
    return valores


def _limpar(key: str, value: object) -> str:
    valor = str(value or "").strip()
    if key in _URL_KEYS and valor and not (
        valor.startswith("https://") or valor.startswith(PUBLIC_IMAGE_PATH)
    ):
        raise EmailSettingsError(
            "Os links precisam começar com https:// (ou ser uma imagem enviada aqui)."
        )
    if key == "share_text" and len(valor) > _SHARE_TEXT_MAX:
        raise EmailSettingsError(
            f"O texto de compartilhamento pode ter até {_SHARE_TEXT_MAX} caracteres."
        )
    return valor


def save_email_settings(
    db: Session, values: Mapping[str, object], admin_email: str
) -> dict[str, str]:
    """Grava só as chaves enviadas; as ausentes ficam como estão. Sem commit."""
    desconhecidas = sorted(set(values) - set(EMAIL_SETTING_KEYS))
    if desconhecidas:
        raise EmailSettingsError(f"Campo desconhecido: {', '.join(desconhecidas)}.")

    limpos = {k: _limpar(k, v) for k, v in values.items()}
    for key, value in limpos.items():
        db.execute(
            text(
                "INSERT INTO email_settings (key, value, updated_at, updated_by) "
                "VALUES (:key, :value, CURRENT_TIMESTAMP, :by) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at, updated_by = excluded.updated_by"
            ),
            {"key": key, "value": value, "by": admin_email},
        )
    db.flush()
    return get_email_settings(db)


def store_public_image(db: Session, content_type: str, data: bytes) -> str:
    """Grava a imagem (idempotente pelo hash) e devolve a URL pública. Sem commit."""
    if content_type not in IMAGE_TYPES:
        raise EmailSettingsError("Envie uma imagem PNG, JPG, GIF ou WebP.")
    if not data:
        raise EmailSettingsError("A imagem chegou vazia. Tente enviar de novo.")
    if len(data) > IMAGE_MAX_BYTES:
        raise EmailSettingsError("A imagem passa de 1 MB. Reduza o tamanho e envie de novo.")

    sha = hashlib.sha256(data).hexdigest()
    existe = db.execute(
        text("SELECT 1 FROM public_image WHERE sha256 = :sha"), {"sha": sha}
    ).first()
    if existe is None:
        db.execute(
            text(
                "INSERT INTO public_image (sha256, content_type, data) "
                "VALUES (:sha, :content_type, :data)"
            ),
            {"sha": sha, "content_type": content_type, "data": data},
        )
        db.flush()
    return f"{PUBLIC_IMAGE_PATH}{sha}"


def load_public_image(db: Session, sha: str) -> Optional[tuple[str, bytes]]:
    try:
        row = db.execute(
            text("SELECT content_type, data FROM public_image WHERE sha256 = :sha"),
            {"sha": sha},
        ).first()
    except SQLAlchemyError:
        db.rollback()
        return None
    if row is None:
        return None
    return row[0], bytes(row[1])
