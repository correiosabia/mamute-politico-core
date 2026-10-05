"""CS-130: registro da liberacao da selecao para o 2o turno

Quando o TSE encerra a totalizacao do 1o turno em todo o pais, a selecao viva
(`projetos_candidacy`) e liberada: saem os nao eleitos (o selo "1º" continua,
pela foto em `projetos_candidacy_turno`). Isso acontece UMA vez; esta tabela
guarda que ja aconteceu, para a pessoa poder marcar de novo quem quiser sem o
cron tirar outra vez.

Revision ID: cs130a1b2c3d4
Revises: cs129a1b2c3d4
"""

from alembic import op

revision = "cs130a1b2c3d4"
down_revision = "cs129a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS selecao_liberacao (
            ciclo TEXT NOT NULL,
            turno SMALLINT NOT NULL,
            executado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
            removidas INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (ciclo, turno)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS selecao_liberacao")
