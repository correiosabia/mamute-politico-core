"""Link curto de um destaque do relatório por e-mail e a imagem de prévia.

Quem grava o link é o envio (`share_link`). Aqui só se lê:

* a página do link devolve as tags de prévia (`og:*`) para o robô do
  WhatsApp/X e manda a pessoa para o site;
* a imagem vem do cache no banco ou de um serviço externo de render
  (`OG_RENDER_URL`, POST /render com os dados do destaque, devolve PNG).
  Sem serviço ou com falha, a prévia cai numa imagem padrão e nada é gravado:
  o link nunca quebra por causa da imagem.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Any, Optional

import requests
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

CODE_RE = re.compile(r"^[A-Za-z0-9]{10}$")
RENDER_TIMEOUT_S = 10


def site_url() -> str:
    return os.getenv("MAMUTE_SITE_URL", "https://mamutepolitico.com.br").rstrip("/")


def fallback_image() -> str:
    return os.getenv("MAMUTE_SHARE_FALLBACK_IMAGE", "").strip()


def load_share_link(db: Session, code: str) -> Optional[dict[str, Any]]:
    if not CODE_RE.match(code):
        return None
    try:
        row = db.execute(
            text(
                "SELECT kind, item_id, title, summary, parliamentarian_name, chamber, occurred_at"
                " FROM share_link WHERE code = :code"
            ),
            {"code": code},
        ).mappings().first()
    except SQLAlchemyError:
        db.rollback()
        return None
    if row is None:
        return None
    dados = dict(row)
    ocorreu = dados.get("occurred_at")
    dados["occurred_at"] = ocorreu.isoformat() if isinstance(ocorreu, date) else ocorreu
    return dados


def cached_png(db: Session, code: str) -> Optional[bytes]:
    try:
        row = db.execute(
            text("SELECT png FROM share_card_cache WHERE code = :code"), {"code": code}
        ).first()
    except SQLAlchemyError:
        db.rollback()
        return None
    return bytes(row[0]) if row else None


def store_png(db: Session, code: str, png: bytes) -> None:
    try:
        db.execute(
            text("INSERT INTO share_card_cache (code, png) VALUES (:code, :png) ON CONFLICT DO NOTHING"),
            {"code": code, "png": png},
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.warning("Não foi possível guardar a prévia %s em cache.", code, exc_info=True)


def _render(dados: dict[str, Any]) -> Optional[bytes]:
    base = os.getenv("OG_RENDER_URL", "").strip().rstrip("/")
    if not base:
        return None
    try:
        resp = requests.post(f"{base}/render", json=dados, timeout=RENDER_TIMEOUT_S)
    except requests.RequestException:
        logger.warning("Serviço de prévia indisponível.", exc_info=True)
        return None
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/png"):
        logger.warning("Serviço de prévia respondeu %s.", resp.status_code)
        return None
    return resp.content


def png_for(db: Session, code: str, dados: dict[str, Any]) -> Optional[bytes]:
    png = cached_png(db, code)
    if png is not None:
        return png
    png = _render(
        {
            "kind": dados.get("kind"),
            "title": dados.get("title"),
            "summary": dados.get("summary"),
            "parliamentarian_name": dados.get("parliamentarian_name"),
            "chamber": dados.get("chamber"),
            "occurred_at": dados.get("occurred_at"),
        }
    )
    if png is not None:
        store_png(db, code, png)
    return png
