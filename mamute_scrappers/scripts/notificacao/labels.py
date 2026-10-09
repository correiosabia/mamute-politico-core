"""Rótulos de exibição de proposições no e-mail."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional


_FIM_DE_FRASE = re.compile(r"[.!?…](?=\s|$)")


def is_camara_proposition(link: Optional[str]) -> bool:
    return isinstance(link, str) and "camara.leg.br" in link.lower()


def format_tipo_numero_ano(
    acronym: Optional[str],
    number: Optional[int],
    year: Optional[int],
) -> Optional[str]:
    """Ex.: PL 1234/2025 (mesmo padrão da UI)."""
    sigla = (acronym or "").strip()
    if not sigla:
        return None
    if number is None:
        return sigla
    if year:
        return f"{sigla} {number}/{year}"
    return f"{sigla} {number}"


def format_proposition_display_title(
    *,
    title: Optional[str],
    link: Optional[str],
    acronym: Optional[str] = None,
    number: Optional[int] = None,
    year: Optional[int] = None,
) -> str:
    """
    Título curto para o e-mail.

    Câmara: prioriza sigla + número/ano.
    Demais casas: usa `title` da proposição.
    """
    if is_camara_proposition(link):
        tipo_numero = format_tipo_numero_ano(acronym, number, year)
        if tipo_numero:
            return tipo_numero[:200]

    cleaned = (title or "").strip()
    if cleaned:
        return cleaned[:200]
    return "Proposição"


def chamber_label_from_parliamentarian_type(
    parliamentarian_type: Optional[str],
) -> str:
    """Retorna 'Câmara', 'Senado' ou vazio conforme `parliamentarian.type`."""
    if not parliamentarian_type:
        return ""
    lowered = parliamentarian_type.lower()
    if "senad" in lowered:
        return "Senado"
    if "deput" in lowered:
        return "Câmara"
    return ""


def format_parliamentarian_display_name(name: Optional[str]) -> str:
    """Ex.: MICHEL TEMER → Michel Temer; ARAÚJO BASTO → Araújo Basto."""
    cleaned = (name or "").strip()
    if not cleaned:
        return "Parlamentar"
    return " ".join(
        part[0].upper() + part[1:].lower() if part else "" for part in cleaned.split()
    )


def cortar_na_frase(texto: Optional[str], limite: int) -> str:
    """Corta no fim da última frase que cabe; sem frase inteira, na palavra com reticências."""
    limpo = " ".join((texto or "").split())
    if len(limpo) <= limite:
        return limpo
    trecho = limpo[:limite]
    fins = [m.end() for m in _FIM_DE_FRASE.finditer(trecho)]
    if fins:
        return trecho[: fins[-1]].strip()
    palavras = trecho[: limite - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{palavras}…"


_CONECTIVOS = frozenset({"de", "do", "da", "dos", "das", "e"})


def format_localidade(valor: Optional[str]) -> str:
    """'MATÃO - SP' -> 'Matão - SP'; 'MATO GROSSO (UF)' -> 'Mato Grosso (UF)'."""
    partes = []
    for i, parte in enumerate((valor or "").split()):
        if (len(parte) == 2 and parte.isalpha() and parte.isupper()) or parte.startswith("("):
            partes.append(parte)
        elif i and parte.lower() in _CONECTIVOS:
            partes.append(parte.lower())
        else:
            partes.append(parte[:1].upper() + parte[1:].lower())
    return " ".join(partes)


def format_brl(valor: float | Decimal | int) -> str:
    """196000 -> "R$ 196.000,00"."""
    inteiro, centavos = f"{Decimal(str(valor)):,.2f}".split(".")
    return f"R$ {inteiro.replace(',', '.')},{centavos}"


def extract_ementa(
    proposition_description: Optional[str],
    summary: Optional[str],
    *,
    max_length: int = 280,
) -> Optional[str]:
    """Ementa da proposição, cortada na frase.

    `summary` é a ementa nas duas Casas; na Câmara `proposition_description`
    traz só o nome do tipo ("Projeto de Lei"), então fica de reserva (CS-123).
    """
    text = (summary or proposition_description or "").strip()
    if not text:
        return None
    return cortar_na_frase(text, max_length)
