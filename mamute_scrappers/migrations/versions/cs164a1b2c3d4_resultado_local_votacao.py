"""CS-164: resultado de presidente por local de votacao (escola), para o zoom dos mapas

Uma linha por eleicao e local (soma das secoes do BU), com as coordenadas do
cadastro de locais do TSE. Gravada pela carga de
`tse_crawler/votacao_horario.py carregar` (eleicoes de 2026).

Revision ID: cs164a1b2c3d4
Revises: cs163a1b2c3d4
"""

from alembic import op

revision = "cs164a1b2c3d4"
down_revision = "cs163a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS resultado_local_votacao (
            id BIGSERIAL PRIMARY KEY,
            eleicao TEXT NOT NULL,
            uf TEXT NOT NULL,
            municipio INTEGER NOT NULL,
            zona INTEGER NOT NULL,
            local INTEGER NOT NULL,
            nome TEXT,
            bairro TEXT,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            comparecimento INTEGER NOT NULL,
            validos INTEGER NOT NULL,
            votos_13 INTEGER NOT NULL,
            votos_22 INTEGER NOT NULL,
            UNIQUE (eleicao, uf, municipio, zona, local)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_resultado_local_votacao_coordenada "
        "ON resultado_local_votacao (eleicao, latitude, longitude)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS resultado_local_votacao")
