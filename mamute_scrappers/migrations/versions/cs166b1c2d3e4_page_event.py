"""Eventos anônimos de leitura das páginas públicas do admin (Eleições 2026)

Tabela `page_event`, irmã de `collection_event` (CS-145): um registro por
evento (visita, troca de aba, compartilhamento, clique no mapa). Sem IP e sem
login; a região é estimada pelo IP na gravação e só país, estado e cidade
ficam.

Idempotente: o deploy sobe o código antes do alembic e a migration pode rodar
de novo sem quebrar.

Revision ID: cs166b1c2d3e4
Revises: cs167a1b2c3d4
"""

from alembic import op

revision = "cs166b1c2d3e4"
down_revision = "cs167a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS page_event (
            id BIGSERIAL PRIMARY KEY,
            page TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            visitor_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            target TEXT,
            detail TEXT,
            duration_ms INTEGER,
            device TEXT,
            country TEXT,
            region TEXT,
            city TEXT,
            referrer_host TEXT,
            utm_source TEXT,
            utm_medium TEXT,
            utm_campaign TEXT
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_page_event_pagina_data ON page_event (page, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS page_event")
