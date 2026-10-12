"""Eventos anônimos de leitura das coleções públicas

Tabela `collection_event`: um registro por evento (visita, tempo na seção,
clique, compartilhamento...). Sem IP e sem login; a região é estimada pelo IP
na gravação e só país, estado e cidade ficam.

Idempotente: o deploy sobe o código antes do alembic e a migration pode rodar
de novo sem quebrar.

Revision ID: cs145a1b2c3d4
Revises: cs116b2c3d4e5
"""

from alembic import op

revision = "cs145a1b2c3d4"
down_revision = "cs116b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_event (
            id BIGSERIAL PRIMARY KEY,
            collection_id BIGINT NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
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
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_collection_event_colecao_data "
        "ON collection_event (collection_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS collection_event")
