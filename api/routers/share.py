"""Link curto de um destaque do relatório por e-mail (público, sem login).

`/s/{code}` existe para a prévia: o robô do WhatsApp/X lê as tags e a pessoa
é mandada para o site. `/s/{code}.png` é a imagem dessa prévia.
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

try:
    from ..dependencies import get_db
    from ..services import share_cards
except ImportError:  # execução dentro de api/
    from dependencies import get_db
    from services import share_cards

router = APIRouter(prefix="/s", tags=["share"])


@router.get("/{code}.png")
def share_image(code: str, db: Session = Depends(get_db)) -> Response:
    dados = share_cards.load_share_link(db, code)
    if dados is None:
        raise HTTPException(status_code=404, detail="Prévia não encontrada.")
    png = share_cards.png_for(db, code, dados)
    if png is not None:
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    padrao = share_cards.fallback_image()
    if padrao:
        return RedirectResponse(padrao, status_code=302)
    raise HTTPException(status_code=404, detail="Prévia indisponível.")


@router.get("/{code}", response_model=None)
def share_page(code: str, db: Session = Depends(get_db)) -> Response:
    site = share_cards.site_url()
    dados = share_cards.load_share_link(db, code)
    if dados is None:
        return RedirectResponse(site, status_code=302)

    def e(valor: object) -> str:
        return html.escape(str(valor or ""), quote=True)

    quem = " · ".join(p for p in (dados.get("parliamentarian_name"), dados.get("chamber")) if p)
    titulo = dados.get("title") or "Destaque do Congresso"
    descricao = dados.get("summary") or quem
    url = f"{site}/api/s/{code}"
    corpo = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>{e(titulo)}</title>
<meta property="og:type" content="article">
<meta property="og:title" content="{e(titulo)}">
<meta property="og:description" content="{e(descricao)}">
<meta property="og:image" content="{e(url)}.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:url" content="{e(url)}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(titulo)}">
<meta name="twitter:description" content="{e(descricao)}">
<meta name="twitter:image" content="{e(url)}.png">
<meta http-equiv="refresh" content="0; url={e(site)}">
</head>
<body>
<p>{e(quem)}</p>
<p><a href="{e(site)}">Continuar</a></p>
<script>location.replace({site!r});</script>
</body>
</html>"""
    return HTMLResponse(corpo, headers={"Cache-Control": "public, max-age=300"})
