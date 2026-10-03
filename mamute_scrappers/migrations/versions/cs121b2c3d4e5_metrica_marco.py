"""CS-121: marcos manuais nos gráficos da aba Plataforma

Datas que o admin marca à mão para explicar mudanças nas curvas (início de
uma promoção, da Fellowship, da cortesia para quem vinha do Correio Sabiá).
Aparecem como linha tracejada nos gráficos, com a descrição no hover.

Revision ID: cs121b2c3d4e5
Revises: cs121a1b2c3d4
"""

from alembic import op

revision = "cs121b2c3d4e5"
down_revision = "cs121a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS metrica_marco (
            id BIGSERIAL PRIMARY KEY,
            data DATE NOT NULL,
            titulo TEXT NOT NULL,
            descricao TEXT,
            criado_por TEXT,
            criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS metrica_marco")
