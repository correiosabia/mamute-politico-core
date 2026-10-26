"""CS-167: endereco e numero de secoes do local de votacao, para o detalhe no mapa

Preenchidos pela carga de `tse_crawler/votacao_horario.py carregar` (o endereco
vem do cadastro de locais do TSE). Linhas antigas ficam com NULL ate a recarga.

Revision ID: cs167a1b2c3d4
Revises: cs164a1b2c3d4
"""

from alembic import op

revision = "cs167a1b2c3d4"
down_revision = "cs164a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE resultado_local_votacao ADD COLUMN IF NOT EXISTS endereco TEXT")
    op.execute("ALTER TABLE resultado_local_votacao ADD COLUMN IF NOT EXISTS secoes INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE resultado_local_votacao DROP COLUMN IF EXISTS secoes")
    op.execute("ALTER TABLE resultado_local_votacao DROP COLUMN IF EXISTS endereco")
