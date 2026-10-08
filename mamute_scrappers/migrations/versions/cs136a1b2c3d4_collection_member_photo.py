"""Foto e crédito da foto de quem está numa coleção curada

Quem não está na base (ex-ministro, ministro do STF, ex-deputado que não está
mais na Câmara) não tem foto vinda do cadastro. O admin informa o endereço de
uma foto pública e o crédito exigido pela licença (ex.: "Marcelo Camargo/Agência
Brasil, CC BY 2.0"). A foto do cadastro continua tendo precedência.

Revision ID: cs136a1b2c3d4
Revises: cs135b2c3d4e5
"""

from alembic import op

revision = "cs136a1b2c3d4"
down_revision = "cs135b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE collection_member ADD COLUMN IF NOT EXISTS photo_url TEXT")
    op.execute("ALTER TABLE collection_member ADD COLUMN IF NOT EXISTS photo_credit TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE collection_member DROP COLUMN IF EXISTS photo_credit")
    op.execute("ALTER TABLE collection_member DROP COLUMN IF EXISTS photo_url")
