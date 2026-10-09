"""Link curto de um destaque do e-mail, para compartilhar com prévia em imagem.

O envio grava em `share_link` o que a prévia precisa mostrar (título, resumo,
parlamentar) e devolve um código aleatório; a API serve `/api/s/{código}` com
as tags de prévia e redireciona a pessoa para o site. Um destaque tem um código
só (único por tipo + id), reaproveitado entre contas e envios. O código não diz
nada de quem compartilhou.
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime
from typing import Optional
from urllib.parse import quote, urlencode, urlsplit

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import ActivityItem

TAMANHO_CODIGO = 10
_ALFABETO = string.ascii_letters + string.digits
TEXTO_PADRAO = "Recebi isso no relatório do Mamute Político. Inscreva-se também em mamutepolitico.com.br"


def _novo_codigo() -> str:
    return "".join(secrets.choice(_ALFABETO) for _ in range(TAMANHO_CODIGO))


def _codigo_existente(session: Session, kind: str, item_id: int) -> Optional[str]:
    row = session.execute(
        text("SELECT code FROM share_link WHERE kind = :kind AND item_id = :id"),
        {"kind": kind, "id": item_id},
    ).first()
    return row[0] if row else None


def share_code_for(session: Session, item: ActivityItem, *, chamber: str) -> Optional[str]:
    """Código do destaque, criando na primeira vez. None se não dá para compartilhar."""
    if not item.kind_key or item.item_id is None:
        return None
    try:
        existente = _codigo_existente(session, item.kind_key, item.item_id)
        if existente:
            return existente
        ocorreu = item.occurred_at.date() if isinstance(item.occurred_at, datetime) else item.occurred_at
        session.execute(
            text(
                "INSERT INTO share_link (code, kind, item_id, title, summary,"
                " parliamentarian_name, chamber, occurred_at)"
                " VALUES (:code, :kind, :id, :title, :summary, :nome, :chamber, :ocorreu)"
                " ON CONFLICT DO NOTHING"
            ),
            {
                "code": _novo_codigo(),
                "kind": item.kind_key,
                "id": item.item_id,
                "title": item.title,
                "summary": item.ementa,
                "nome": item.parliamentarian_name or None,
                "chamber": chamber or None,
                "ocorreu": ocorreu,
            },
        )
        session.commit()
        return _codigo_existente(session, item.kind_key, item.item_id)
    except SQLAlchemyError:
        session.rollback()
        return None


def origem(app_url: str) -> str:
    """'https://site/app' -> 'https://site'. A API mora em /api na raiz; /app* é a UI."""
    partes = urlsplit(app_url)
    return f"{partes.scheme}://{partes.netloc}" if partes.netloc else app_url.rstrip("/")


def share_links(app_url: str, code: str, share_text: str) -> dict[str, str]:
    url = f"{origem(app_url)}/api/s/{code}"
    texto = (share_text or "").strip() or TEXTO_PADRAO
    return {
        "url": url,
        "whatsapp": "https://wa.me/?" + urlencode({"text": f"{texto} {url}"}, quote_via=quote),
        "x": "https://x.com/intent/post?" + urlencode({"text": texto, "url": url}, quote_via=quote),
    }
