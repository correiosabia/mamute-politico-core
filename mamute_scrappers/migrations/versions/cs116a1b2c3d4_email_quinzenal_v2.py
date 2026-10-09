"""Relatório por e-mail: configurações, imagens públicas, resumo curto e link curto

* `email_settings`: chave/valor das peças editáveis do e-mail (imagens de
  topo e rodapé, links, texto de compartilhamento). Chave vazia = bloco some.
* `public_image`: imagens enviadas pelo painel admin, servidas pela API.
  Ficam no banco para sobreviver a deploy e entrar no backup.
* `speech_short_summary`: resumo de até duas frases de cada discurso, usado
  no e-mail. Tabela própria para não tocar na `speeches_transcripts`.
* `share_link` + `share_card_cache`: link curto de um destaque do e-mail e a
  imagem de prévia já desenhada.

Revision ID: cs116a1b2c3d4
Revises: cs136a1b2c3d4
"""

from alembic import op

revision = "cs116a1b2c3d4"
down_revision = "cs136a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS email_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by TEXT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public_image (
            sha256 TEXT PRIMARY KEY,
            content_type TEXT NOT NULL,
            data BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS speech_short_summary (
            speech_id BIGINT PRIMARY KEY
                REFERENCES speeches_transcripts (id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            source TEXT NOT NULL,
            model TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS share_link (
            code TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            item_id BIGINT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            parliamentarian_name TEXT,
            chamber TEXT,
            occurred_at DATE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_share_link_kind_item UNIQUE (kind, item_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS share_card_cache (
            code TEXT PRIMARY KEY REFERENCES share_link (code) ON DELETE CASCADE,
            png BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS share_card_cache")
    op.execute("DROP TABLE IF EXISTS share_link")
    op.execute("DROP TABLE IF EXISTS speech_short_summary")
    op.execute("DROP TABLE IF EXISTS public_image")
    op.execute("DROP TABLE IF EXISTS email_settings")
