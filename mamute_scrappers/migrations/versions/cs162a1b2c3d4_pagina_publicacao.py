"""CS-162: estado de publicacao de paginas do admin (admin x publica)

Mesmo esquema da Na Lupa: o admin decide se a pagina abre ao publico, sem
recorte por plano. Sem linha = so admin. Primeira chave: `eleicoes_2026`
(Perfil dos eleitos + Diferenca 2o turno em /eleicoes-2026).

Revision ID: cs162a1b2c3d4
Revises: cs161a1b2c3d4
"""

from alembic import op

revision = "cs162a1b2c3d4"
down_revision = "cs161a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pagina_publicacao (
            chave TEXT PRIMARY KEY,
            estado TEXT NOT NULL DEFAULT 'admin' CHECK (estado IN ('admin', 'publica')),
            atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
            atualizado_por TEXT
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pagina_publicacao")
