"""Configuração da engine e da fábrica de sessões."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# Garante leitura do arquivo .env mesmo quando o comando é executado de subpastas.
BASE_DIR = Path(__file__).resolve().parents[2]
CURRENT_DIR = Path(__file__).resolve().parent

ENV_CANDIDATES = [
    BASE_DIR / ".env",
    BASE_DIR / "@.env",
    CURRENT_DIR.parent / ".env",
    CURRENT_DIR.parent / "@.env",
    CURRENT_DIR / ".env",
    CURRENT_DIR / "@.env",
    Path.cwd() / ".env",
    Path.cwd() / "@.env",
]

for env_file in ENV_CANDIDATES:
    if env_file.exists():
        load_dotenv(env_file, override=False)

# Driver do Postgres sempre explícito. Uma URL `postgresql://` deixa a escolha
# do DBAPI para o SQLAlchemy, e o default mudou no 2.1 (psycopg2 → psycopg 3).
# Foi isso que derrubou a API em 28/09/2026 (CS-103): o rebuild puxou o 2.1 e a
# imagem só tem psycopg2. Com o driver fixado aqui, o default da lib não importa.
POSTGRES_DRIVER = "psycopg2"
_IMPLICIT_POSTGRES_SCHEMES = ("postgresql://", "postgres://")


def normalize_database_url(url: str) -> str:
    """Troca `postgresql://`/`postgres://` por `postgresql+psycopg2://`.

    URLs que já declaram o driver (`postgresql+algo://`) e de outros bancos
    (ex.: sqlite nos testes) passam intactas.
    """
    for scheme in _IMPLICIT_POSTGRES_SCHEMES:
        if url.startswith(scheme):
            return f"postgresql+{POSTGRES_DRIVER}://" + url[len(scheme):]
    return url


APPLICATION_NAME = os.getenv("APPLICATION_NAME", "MAMUTE_POLITICO_CRAWLER")
_RAW_DATABASE_URL = os.getenv("DATABASE_URL")

if not _RAW_DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL não definido. Ajuste o arquivo .env ou exporte a variável antes de executar."
    )

DATABASE_URL = normalize_database_url(_RAW_DATABASE_URL)


def _str_to_bool(value: Optional[str], default: bool = False) -> bool:
    """Converte strings típicas de variáveis de ambiente em booleanos."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


SQLALCHEMY_ECHO = _str_to_bool(os.getenv("SQLALCHEMY_ECHO"))

CONNECT_ARGS: dict[str, str] = {}
if DATABASE_URL.startswith("postgresql"):
    CONNECT_ARGS["application_name"] = APPLICATION_NAME

create_engine_kwargs = {
    "echo": SQLALCHEMY_ECHO,
    "future": True,
}
if CONNECT_ARGS:
    create_engine_kwargs["connect_args"] = CONNECT_ARGS

engine: Engine = create_engine(
    DATABASE_URL,
    **create_engine_kwargs,
)

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    future=True,
)


@lru_cache
def get_engine(url: Optional[str] = None) -> Engine:
    """Permite obter uma engine reaproveitando a configuração padrão."""
    if url:
        url = normalize_database_url(url)
    if url and url != str(engine.url):
        return create_engine(url, echo=engine.echo, future=True)
    return engine


__all__ = [
    "APPLICATION_NAME",
    "DATABASE_URL",
    "POSTGRES_DRIVER",
    "SQLALCHEMY_ECHO",
    "engine",
    "SessionLocal",
    "get_engine",
    "normalize_database_url",
]

