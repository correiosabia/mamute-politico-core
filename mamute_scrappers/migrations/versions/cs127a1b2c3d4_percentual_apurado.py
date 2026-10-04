"""CS-127: % de urnas apuradas do arquivo de resultado do TSE

Durante a apuracao o TSE nao marca eleitos, mas publica votos parciais e o %
de secoes totalizadas (`s.pst`). Guardamos esse % no arquivo
(`tse_result_file`) e em cada resultado de candidatura (`candidacy_result`),
para o card da busca mostrar "X votos · Y% das urnas apuradas" sem join.

NULL = linha coletada antes desta coluna existir; a rodada seguinte preenche.

Revision ID: cs127a1b2c3d4
Revises: cs121b2c3d4e5
"""

from alembic import op

revision = "cs127a1b2c3d4"
down_revision = "cs121b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tse_result_file "
        "ADD COLUMN IF NOT EXISTS percentual_apurado NUMERIC(5, 2)"
    )
    op.execute(
        "ALTER TABLE candidacy_result "
        "ADD COLUMN IF NOT EXISTS percentual_apurado NUMERIC(5, 2)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE candidacy_result DROP COLUMN IF EXISTS percentual_apurado")
    op.execute("ALTER TABLE tse_result_file DROP COLUMN IF EXISTS percentual_apurado")
