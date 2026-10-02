"""CS-107: quantos eleitos cada arquivo de resultado do TSE traz

`tse_result_file.eleitos_no_arquivo` conta os eleitos do arquivo inteiro,
casados ou nao com a nossa base. O perfil dos eleitos no admin compara esse
numero com os eleitos que encontrou em `candidacy`: se o TSE elegeu 70 em SP e
a base so tem 69, a tela avisa em vez de calcular percentual sobre um
conjunto incompleto sem dizer.

NULL = arquivo coletado antes desta coluna existir. O coletor baixa de novo,
uma vez, os arquivos encerrados que estao com NULL.

Revision ID: cs107a1b2c3d4
Revises: cs106a1b2c3d4
"""

from alembic import op

revision = "cs107a1b2c3d4"
down_revision = "cs106a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tse_result_file "
        "ADD COLUMN IF NOT EXISTS eleitos_no_arquivo INTEGER"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tse_result_file DROP COLUMN IF EXISTS eleitos_no_arquivo")
