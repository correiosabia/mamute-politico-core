"""Abertura dos e-mails do Mamute (rastreio por mensagem do Mailgun)

- `email_send_log.message_id`: Message-ID que o mailer pôs no e-mail. É por ele
  que o evento de abertura do Mailgun volta para a linha do envio.
- `email_aberturas`: uma linha por mensagem aberta (primeira e última abertura),
  gravada pelo job `scripts.notificacao.aberturas`, que lê a API de eventos do
  Mailgun de hora em hora. O Mailgun guarda poucos dias de eventos; o nosso banco
  é o histórico.

Idempotente: o deploy sobe o código antes do alembic.

Revision ID: cs148a1b2c3d4
Revises: cs150a1b2c3d4
"""

from alembic import op

revision = "cs148a1b2c3d4"
down_revision = "cs150a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE email_send_log ADD COLUMN IF NOT EXISTS message_id TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_email_send_log_message_id "
        "ON email_send_log (message_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS email_aberturas (
            message_id TEXT PRIMARY KEY,
            primeira_abertura_em TIMESTAMPTZ NOT NULL,
            ultima_abertura_em TIMESTAMPTZ NOT NULL,
            tags TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS email_aberturas")
    op.execute("DROP INDEX IF EXISTS ix_email_send_log_message_id")
    op.execute("ALTER TABLE email_send_log DROP COLUMN IF EXISTS message_id")
