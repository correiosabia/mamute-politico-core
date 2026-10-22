"""Link curto de um destaque do relatório por e-mail (público, sem login).

`/s/{code}` existe para a prévia: o robô do WhatsApp/X lê as tags e a pessoa
é mandada para o site. `/s/{code}.png` é a imagem dessa prévia.
`/s/{code}/story` mostra o card vertical para salvar e postar no Instagram ou
no TikTok, e `/s/icones/{rede}.png` são os ícones das redes usados no e-mail.
"""

from __future__ import annotations

import html
from pathlib import Path

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

_ICONES = Path(__file__).resolve().parent.parent / "services" / "icones_redes"
_REDES_ICONE = {"whatsapp", "x", "facebook", "instagram", "tiktok"}
_REDES_STORY = {"instagram": "Instagram", "tiktok": "TikTok"}


@router.get("/icones/{rede}.png")
def share_icon(rede: str) -> Response:
    if rede not in _REDES_ICONE:
        raise HTTPException(status_code=404, detail="Ícone não encontrado.")
    return Response(
        content=(_ICONES / f"{rede}.png").read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


@router.get("/{code}/story.png")
def share_story_image(code: str, baixar: bool = False, db: Session = Depends(get_db)) -> Response:
    dados = share_cards.load_share_link(db, code)
    if dados is None:
        raise HTTPException(status_code=404, detail="Card não encontrado.")
    png = share_cards.png_for(db, code, dados, formato="story")
    if png is None:
        # Sem reserva: a imagem padrão é horizontal e ficaria errada num story.
        raise HTTPException(status_code=404, detail="Card indisponível.")
    headers = {"Cache-Control": "public, max-age=86400"}
    if baixar:
        headers["Content-Disposition"] = 'attachment; filename="mamute-story.png"'
    return Response(content=png, media_type="image/png", headers=headers)


@router.get("/{code}/story", response_model=None)
def share_story_page(code: str, rede: str = "", db: Session = Depends(get_db)) -> Response:
    site = share_cards.site_url()
    if share_cards.load_share_link(db, code) is None:
        return RedirectResponse(site, status_code=302)
    nome = _REDES_STORY.get(rede)
    destino = f"no story do {nome}" if nome else "no seu story"
    imagem = f"{site}/api/s/{code}/story.png"
    corpo = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Card para o story · Mamute Político</title>
<style>
body {{ margin: 0; background: #e6c54a; font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
  color: #1f2b44; display: flex; flex-direction: column; align-items: center; padding: 24px 16px 40px; }}
h1 {{ font-size: 20px; margin: 0 0 6px; text-align: center; }}
p {{ margin: 0 0 16px; text-align: center; font-size: 15px; max-width: 420px; }}
img {{ width: 100%; max-width: 360px; border-radius: 20px; box-shadow: 0 18px 40px -24px rgba(31,43,68,.55); }}
a.baixar {{ margin-top: 18px; background: #1f2b44; color: #fff; text-decoration: none; font-weight: 700;
  padding: 12px 26px; border-radius: 999px; }}
</style>
</head>
<body>
<h1>Poste {html.escape(destino)}</h1>
<p>Toque e segure a imagem para salvar (ou use o botão) e publique {html.escape(destino)}.</p>
<img src="{html.escape(imagem, quote=True)}" alt="Card do destaque para o story">
<a class="baixar" href="{html.escape(imagem, quote=True)}?baixar=1">Baixar imagem</a>
</body>
</html>"""
    return HTMLResponse(corpo, headers={"Cache-Control": "public, max-age=300"})


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
