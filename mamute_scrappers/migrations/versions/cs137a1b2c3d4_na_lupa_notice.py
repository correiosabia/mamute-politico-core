"""CS-137: aviso por e-mail a quem segue um parlamentar que entrou num caso do Na Lupa

`na_lupa_notice`: uma linha por projeto (quem recebe) x caso x parlamentar
seguido. A unique e a trava contra e-mail repetido: publicar o caso de novo,
mudar o texto ou o nivel da pessoa nao reenvia. O e-mail sai consolidado por
projeto x caso (uma pessoa que segue tres citados recebe um e-mail so), e as
linhas daquele e-mail andam juntas de `pending` para `sent`, `error`
(tenta de novo ate MAX_TENTATIVAS), `skipped_no_email` ou `skipped_opt_out`.
`tier` e `tier_label` guardam o nivel que foi para o e-mail.

A flag `email_na_lupa` nao precisa de linha: sem linha vale `off`.

Revision ID: cs137a1b2c3d4
Revises: cs87a1b2c3d4
"""

from alembic import op

revision = "cs137a1b2c3d4"
down_revision = "cs87a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS na_lupa_notice (
            id BIGSERIAL PRIMARY KEY,
            projeto_id BIGINT NOT NULL
                REFERENCES projetos (id) ON DELETE CASCADE,
            collection_id BIGINT NOT NULL
                REFERENCES collection (id) ON DELETE CASCADE,
            parliamentarian_id BIGINT NOT NULL
                REFERENCES parliamentarian (id) ON DELETE CASCADE,
            member_id BIGINT,
            tier SMALLINT,
            tier_label TEXT,
            email_status TEXT NOT NULL DEFAULT 'pending',
            tentativas SMALLINT NOT NULL DEFAULT 0,
            ultimo_erro TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            sent_at TIMESTAMPTZ,
            CONSTRAINT uq_na_lupa_notice UNIQUE (projeto_id, collection_id, parliamentarian_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_na_lupa_notice_collection "
        "ON na_lupa_notice (collection_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS na_lupa_notice")
