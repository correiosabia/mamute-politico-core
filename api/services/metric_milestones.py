"""Marcos manuais dos gráficos da aba Plataforma (CS-121).

O admin registra uma data com título e descrição (ex.: "Início da
Fellowship"); a tela desenha uma linha tracejada na semana dessa data e mostra
a descrição ao passar o mouse.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

TITULO_MAX = 80
DESCRICAO_MAX = 500


def listar(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text("SELECT id, data, titulo, descricao, criado_por FROM metrica_marco ORDER BY data, id")
    ).all()
    return [
        {
            "id": int(r.id),
            "data": r.data.isoformat() if hasattr(r.data, "isoformat") else str(r.data),
            "titulo": r.titulo,
            "descricao": r.descricao,
            "criado_por": r.criado_por,
        }
        for r in rows
    ]


def criar(
    db: Session, *, data: date, titulo: str, descricao: Optional[str], criado_por: Optional[str]
) -> dict[str, Any]:
    titulo = (titulo or "").strip()
    if not titulo:
        raise ValueError("Dê um título ao marco.")
    if len(titulo) > TITULO_MAX:
        raise ValueError(f"Título com no máximo {TITULO_MAX} caracteres.")
    descricao = (descricao or "").strip() or None
    if descricao and len(descricao) > DESCRICAO_MAX:
        raise ValueError(f"Descrição com no máximo {DESCRICAO_MAX} caracteres.")
    db.execute(
        text(
            "INSERT INTO metrica_marco (data, titulo, descricao, criado_por) "
            "VALUES (:data, :titulo, :descricao, :por)"
        ),
        {"data": data, "titulo": titulo, "descricao": descricao, "por": criado_por},
    )
    db.commit()
    novo = db.execute(text("SELECT max(id) FROM metrica_marco")).scalar()
    return next(m for m in listar(db) if m["id"] == int(novo))


def apagar(db: Session, marco_id: int) -> bool:
    apagados = db.execute(text("DELETE FROM metrica_marco WHERE id = :id"), {"id": marco_id}).rowcount
    db.commit()
    return bool(apagados)


__all__ = ["apagar", "criar", "listar"]
