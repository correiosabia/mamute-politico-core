"""Configuracao compartilhada de pytest para api/tests/.

Garante que:
1. `api` seja importavel como pacote (sys.path).
2. `DATABASE_URL` exista em ambiente de teste — `api/db/engine.py` exige no
   import. Smoke tests nao tocam DB; SQLAlchemy `create_engine(url)` so valida
   a URL, conexao real e lazy. URL placeholder e suficiente.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Raiz do repositorio = parent de api/
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Placeholder valido pra parser do SQLAlchemy. setdefault preserva valor real
# se o dev rodar pytest com DATABASE_URL ja exportada.
# Mesmo formato de producao (docker-compose monta `postgresql://`, sem driver):
# com `postgresql+psycopg2://` aqui o CI nunca passava pelo caminho que
# escolhe o driver sozinho, e foi esse caminho que quebrou em 28/09 (CS-103).
os.environ.setdefault(
    "DATABASE_URL", "postgresql://test:test@localhost:5432/test_db"
)

# Câmbio fixo nos testes: evita que rotas que chamam get_usd_brl_rate tentem
# rede (o valor real vem da tabela usd_brl_rate em prod). Testes que exercitam
# a resolução via banco/rede removem esta env explicitamente (monkeypatch.delenv).
os.environ.setdefault("MAMUTE_USD_BRL_RATE", "5.0")
