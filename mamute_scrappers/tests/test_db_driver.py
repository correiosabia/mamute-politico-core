"""Guarda contra a regressao da CS-103 do lado dos scrappers/alembic.

Mesmo motivo de api/tests/test_db_driver.py: o SQLAlchemy 2.1 trocou o driver
padrao de `postgresql://` para psycopg 3, e a imagem so tem psycopg2. Os
scrappers estao pinados em 2.0.44, mas o engine (e o alembic, que passa por
`get_engine`) tambem forca o driver para nao depender do pin.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# mamute_scrappers.db.engine exige DATABASE_URL no import. Formato de producao.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test_db")

from sqlalchemy import create_engine  # noqa: E402

from mamute_scrappers.db.engine import engine, get_engine, normalize_database_url  # noqa: E402

SCRAPPERS_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("url", "esperado"),
    [
        ("postgresql://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
        ("postgres://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
        ("postgresql+psycopg2://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
        ("sqlite://", "sqlite://"),
    ],
)
def test_normalize_database_url(url: str, esperado: str) -> None:
    assert normalize_database_url(url) == esperado


def test_url_de_producao_usa_psycopg2() -> None:
    eng = create_engine(normalize_database_url("postgresql://u:p@localhost/db"))
    assert eng.dialect.driver == "psycopg2"


def test_engine_e_get_engine_usam_psycopg2() -> None:
    assert engine.dialect.driver == "psycopg2"
    assert get_engine("postgresql://outro:x@localhost/outro").dialect.driver == "psycopg2"


@pytest.mark.parametrize(
    "arquivo",
    ["requirements.txt", "scripts/notificacao/requirements.txt"],
)
def test_sqlalchemy_tem_teto_de_versao(arquivo: str) -> None:
    linhas = [
        linha.strip()
        for linha in (SCRAPPERS_DIR / arquivo).read_text().splitlines()
        if linha.strip().lower().startswith("sqlalchemy")
    ]
    assert linhas, f"SQLAlchemy nao declarado em {arquivo}"
    for linha in linhas:
        assert "==" in linha or "<" in linha, f"{arquivo}: {linha} sem teto de versao"
