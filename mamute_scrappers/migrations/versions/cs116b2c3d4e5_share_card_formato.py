"""Cache da imagem de compartilhamento por formato

`share_card_cache` guardava uma imagem por código (a prévia 1200x630). O card
vertical de story (Instagram e TikTok) é outra imagem do mesmo destaque, então
o cache passa a ser por (código, formato). As linhas existentes viram "og".

Idempotente: o deploy sobe o código antes do alembic e a migration pode rodar
de novo sem quebrar.

Revision ID: cs116b2c3d4e5
Revises: cs116a1b2c3d4
"""

from alembic import op

revision = "cs116b2c3d4e5"
down_revision = "cs116a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE share_card_cache ADD COLUMN IF NOT EXISTS formato TEXT NOT NULL DEFAULT 'og'")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM pg_index i
                  JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
                 WHERE i.indrelid = 'share_card_cache'::regclass
                   AND i.indisprimary
                   AND a.attname = 'formato'
            ) THEN
                ALTER TABLE share_card_cache DROP CONSTRAINT IF EXISTS share_card_cache_pkey;
                ALTER TABLE share_card_cache ADD CONSTRAINT share_card_cache_pkey PRIMARY KEY (code, formato);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM share_card_cache WHERE formato <> 'og'")
    op.execute("ALTER TABLE share_card_cache DROP CONSTRAINT IF EXISTS share_card_cache_pkey")
    op.execute("ALTER TABLE share_card_cache ADD CONSTRAINT share_card_cache_pkey PRIMARY KEY (code)")
    op.execute("ALTER TABLE share_card_cache DROP COLUMN IF EXISTS formato")
