"""CS-119: aviso do resultado em disparos (majoritarios e completo)

No 1o turno o aviso sai em dois disparos consolidados: um quando presidente,
governador e senador fecham em todas as UFs (`majoritarios`) e outro, com o
resumo completo, quando os deputados fecham (`completo`). A chave de unicidade
passa a incluir o disparo para a mesma pessoa poder receber os dois no turno.

Linhas antigas viram `completo` (em prod a tabela estava vazia em 03/10/2026).

Revision ID: cs119a1b2c3d4
Revises: cs107a1b2c3d4
"""

from alembic import op

revision = "cs119a1b2c3d4"
down_revision = "cs107a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE election_result_notice "
        "ADD COLUMN IF NOT EXISTS disparo TEXT NOT NULL DEFAULT 'completo'"
    )
    op.execute(
        "ALTER TABLE election_result_notice "
        "DROP CONSTRAINT IF EXISTS uq_election_result_notice"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_election_result_notice_disparo "
        "ON election_result_notice (projeto_id, ciclo, turno, disparo)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_election_result_notice_disparo")
    op.execute("DELETE FROM election_result_notice WHERE disparo <> 'completo'")
    op.execute(
        "ALTER TABLE election_result_notice "
        "ADD CONSTRAINT uq_election_result_notice UNIQUE (projeto_id, ciclo, turno)"
    )
    op.execute("ALTER TABLE election_result_notice DROP COLUMN IF EXISTS disparo")
