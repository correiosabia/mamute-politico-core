"""Guarda contra a regressao da CS-103 (API em loop de restart, 28/09/2026).

O SQLAlchemy 2.1 trocou o DBAPI padrao de `postgresql://` de psycopg2 para
psycopg 3. Producao monta a URL sem driver e a imagem so tem psycopg2, entao um
rebuild que puxou o 2.1 derrubou a API no import. Estes testes garantem que:

1. a engine usa psycopg2 mesmo com a URL no formato de producao;
2. as dependencias da API tem teto de versao (rebuild nao sobe minor/major
   sozinho).

Nao tocam banco: `create_engine` importa o DBAPI na criacao, mas so conecta
no primeiro uso.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from api.db.engine import POSTGRES_DRIVER, engine, get_engine, normalize_database_url

API_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("url", "esperado"),
    [
        ("postgresql://u:p@h:5432/db", "postgresql+psycopg2://u:p@h:5432/db"),
        ("postgres://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
        # Driver declarado e respeitado, inclusive se alguem optar por psycopg 3.
        ("postgresql+psycopg2://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:///:memory:", "sqlite:///:memory:"),
    ],
)
def test_normalize_database_url(url: str, esperado: str) -> None:
    assert normalize_database_url(url) == esperado


def test_url_de_producao_usa_psycopg2() -> None:
    """URL como o compose monta (sem driver) precisa cair no psycopg2.

    Sem a normalizacao, com SQLAlchemy 2.1 isto levanta
    `ModuleNotFoundError: No module named 'psycopg'`, o erro de producao.
    """
    eng = create_engine(normalize_database_url("postgresql://u:p@localhost:5432/db"))
    assert eng.dialect.driver == POSTGRES_DRIVER == "psycopg2"


def test_engine_do_modulo_usa_psycopg2() -> None:
    assert engine.dialect.driver == "psycopg2"


def test_get_engine_com_url_explicita_normaliza() -> None:
    """Caminho usado pelo alembic (`get_engine(DATABASE_URL)`)."""
    eng = get_engine("postgresql://outro:x@localhost:5432/outro_db")
    assert eng.dialect.driver == "psycopg2"


_REQ_LINE = re.compile(r"^(?P<nome>[A-Za-z0-9_.\-]+)(\[[^\]]+\])?(?P<spec>.*)$")


def _requisitos(path: Path) -> list[tuple[str, str]]:
    itens = []
    for linha in path.read_text().splitlines():
        linha = linha.split("#", 1)[0].split(";", 1)[0].strip()
        if not linha or linha.startswith("-"):
            continue
        m = _REQ_LINE.match(linha)
        assert m, f"linha de requirements nao reconhecida: {linha!r}"
        itens.append((m["nome"], m["spec"].strip()))
    return itens


def test_dependencias_da_api_tem_teto_de_versao() -> None:
    """Todo pacote precisa de `<` ou `==`.

    Range aberto + rebuild em todo deploy = upgrade que ninguem pediu. Foi
    `SQLAlchemy>=2.0,<3.0` que deixou o 2.1 entrar. Para subir uma dependencia,
    suba o teto aqui de proposito, num PR que passe pelo CI.
    """
    sem_teto = [
        f"{nome}{spec}"
        for nome, spec in _requisitos(API_DIR / "requirements.txt")
        if "<" not in spec and "==" not in spec
    ]
    assert not sem_teto, f"dependencias sem teto de versao: {sem_teto}"


def test_sqlalchemy_fica_na_serie_2_0() -> None:
    """Subir para 2.1 e decisao consciente: revisar driver e changelog antes."""
    specs = dict(_requisitos(API_DIR / "requirements.txt"))
    assert "<2.1" in specs["SQLAlchemy"].replace(" ", "")
