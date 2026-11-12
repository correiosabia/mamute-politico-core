"""CS-87: usuários ativos agora e mapa de origem dos acessos no admin

- `member_presence`: último sinal de presença de cada membro logado, com a
  localidade estimada pelo IP naquele sinal.
- `member_presence_hour`: membro x hora cheia com sinal (quadro de horários).
- `usage_events`: país, estado, cidade e o centro da cidade (lat/lon) do
  page_view/section_view.
- `collection_event` e `page_event`: lat/lon do centro da cidade, para os
  visitantes anônimos aparecerem como ponto no mapa.

O IP não é gravado em lugar nenhum. A localidade ligada ao e-mail é apagada
depois de 90 dias pela própria API (services/acessos.py).

Idempotente: o deploy sobe o código antes do alembic e a migration pode rodar
de novo sem quebrar.

Revision ID: cs87a1b2c3d4
Revises: cs95a1b2c3d4
"""

from alembic import op

revision = "cs87a1b2c3d4"
down_revision = "cs95a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS member_presence (
            email TEXT PRIMARY KEY,
            projeto_id BIGINT,
            last_seen_at TIMESTAMPTZ NOT NULL,
            country TEXT,
            region TEXT,
            city TEXT,
            lat DOUBLE PRECISION,
            lon DOUBLE PRECISION
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_member_presence_visto ON member_presence (last_seen_at)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS member_presence_hour (
            hora TIMESTAMPTZ NOT NULL,
            email TEXT NOT NULL,
            PRIMARY KEY (hora, email)
        )
        """
    )
    for coluna, tipo in (
        ("country", "TEXT"),
        ("region", "TEXT"),
        ("city", "TEXT"),
        ("lat", "DOUBLE PRECISION"),
        ("lon", "DOUBLE PRECISION"),
    ):
        op.execute(f"ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS {coluna} {tipo}")
    for tabela in ("collection_event", "page_event"):
        op.execute(f"ALTER TABLE {tabela} ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION")
        op.execute(f"ALTER TABLE {tabela} ADD COLUMN IF NOT EXISTS lon DOUBLE PRECISION")


def downgrade() -> None:
    for tabela in ("collection_event", "page_event"):
        op.execute(f"ALTER TABLE {tabela} DROP COLUMN IF EXISTS lon")
        op.execute(f"ALTER TABLE {tabela} DROP COLUMN IF EXISTS lat")
    for coluna in ("lon", "lat", "city", "region", "country"):
        op.execute(f"ALTER TABLE usage_events DROP COLUMN IF EXISTS {coluna}")
    op.execute("DROP TABLE IF EXISTS member_presence_hour")
    op.execute("DROP TABLE IF EXISTS member_presence")
