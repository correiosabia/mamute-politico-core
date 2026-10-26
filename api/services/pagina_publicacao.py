"""Estado de publicação de páginas do admin que podem abrir ao público (CS-162).

Mesmo esquema da Na Lupa: o admin decide, sem recorte por plano. Estados:

- `admin`: só o painel administrativo vê (padrão; sem linha ou sem tabela).
- `publica`: qualquer pessoa, sem login, vê a versão pública.

Não usa feature flag: `useFeatureFlag` no app só consulta com token, então
para visitante sem login seria sempre falso. O portão é este estado, lido pela
rota pública.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

Estado = Literal["admin", "publica"]
ESTADOS: tuple[str, ...] = ("admin", "publica")
PAGINAS: tuple[str, ...] = ("eleicoes_2026",)


def _tabela_existe(db: Session) -> bool:
    try:
        return sqlalchemy_inspect(db.get_bind()).has_table("pagina_publicacao")
    except SQLAlchemyError:
        return False


def estado_da_pagina(db: Session, chave: str) -> str:
    """`publica` só com linha gravada; qualquer outra coisa é `admin`."""
    if not _tabela_existe(db):
        return "admin"
    estado = db.execute(
        text("SELECT estado FROM pagina_publicacao WHERE chave = :chave"), {"chave": chave}
    ).scalar()
    return "publica" if estado == "publica" else "admin"


def definir_estado(db: Session, chave: str, estado: str, email: str, *, commit: bool = True) -> str:
    if estado not in ESTADOS:
        raise ValueError(f"estado inválido: {estado}")
    db.execute(
        text(
            """
            INSERT INTO pagina_publicacao (chave, estado, atualizado_em, atualizado_por)
            VALUES (:chave, :estado, :agora, :email)
            ON CONFLICT (chave) DO UPDATE SET
                estado = excluded.estado,
                atualizado_em = excluded.atualizado_em,
                atualizado_por = excluded.atualizado_por
            """
        ),
        {"chave": chave, "estado": estado, "agora": datetime.now(timezone.utc), "email": email},
    )
    if commit:
        db.commit()
    return estado
