"""Descadastro dos e-mails do Mamute (resumo periódico e resultado da eleição)

Duas colunas em `projetos`:

- `emails_desativados_em`: preenchida quando o leitor pede para parar de
  receber; NULL = recebe. O envio pula quem tem data aqui.
- `email_token`: segredo aleatório por leitor, usado no link de descadastro do
  rodapé e no List-Unsubscribe (funcionam sem login). Nasce preenchido para
  todo mundo pelo DEFAULT, inclusive para os projetos criados depois.

Idempotente: o deploy sobe o código antes do alembic e a migration pode rodar
de novo sem quebrar. O código checa se as colunas existem antes de usá-las.

Revision ID: cs150a1b2c3d4
Revises: cs145a1b2c3d4
"""

from alembic import op

revision = "cs150a1b2c3d4"
down_revision = "cs145a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE projetos ADD COLUMN IF NOT EXISTS emails_desativados_em TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE projetos ADD COLUMN IF NOT EXISTS email_token TEXT "
        "NOT NULL DEFAULT gen_random_uuid()::text"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_projetos_email_token "
        "ON projetos (email_token)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_projetos_email_token")
    op.execute("ALTER TABLE projetos DROP COLUMN IF EXISTS email_token")
    op.execute("ALTER TABLE projetos DROP COLUMN IF EXISTS emails_desativados_em")
