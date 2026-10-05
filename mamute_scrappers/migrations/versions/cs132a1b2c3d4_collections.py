"""CS-132: colecoes curadas de politicos

Uma colecao reune pessoas (parlamentares, candidatos ou qualquer outra figura
publica) em torno de um tema, com blocos de conteudo editados pelo admin:
texto, discurso, votacao, proposicao, gasto de cota, link, documento.

A pessoa da colecao (`collection_member`) se liga ao resto da base de tres
jeitos, todos opcionais: `parliamentarian_id`, `candidacy_id` e `cpf`. O CPF e
o que mantem o vinculo vivo quando a pessoa muda de situacao (candidato que
assume mandato, parlamentar de outra casa ou de outra esfera): a leitura
resolve o parlamentar e a candidatura mais recente por ele, sem precisar
regravar a colecao.

`tier` e um agrupamento de 1 a 5 dentro da colecao. O significado de cada
numero mora na propria colecao (`tier_labels`), nao no codigo.

Revision ID: cs132a1b2c3d4
Revises: cs130a1b2c3d4
"""

from alembic import op

revision = "cs132a1b2c3d4"
down_revision = "cs130a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS collection (
            id BIGSERIAL PRIMARY KEY,
            slug TEXT NOT NULL,
            title TEXT NOT NULL,
            subtitle TEXT,
            summary TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            tier_labels JSONB NOT NULL DEFAULT '{}'::jsonb,
            settings JSONB NOT NULL DEFAULT '{}'::jsonb,
            published_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_collection_slug UNIQUE (slug),
            CONSTRAINT ck_collection_status CHECK (status IN ('draft', 'published'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_member (
            id BIGSERIAL PRIMARY KEY,
            collection_id BIGINT NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
            display_name TEXT NOT NULL,
            role_label TEXT,
            cpf TEXT,
            parliamentarian_id BIGINT REFERENCES parliamentarian(id) ON DELETE SET NULL,
            candidacy_id BIGINT REFERENCES candidacy(id) ON DELETE SET NULL,
            tier SMALLINT,
            context TEXT,
            sources JSONB NOT NULL DEFAULT '[]'::jsonb,
            position INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_collection_member_tier CHECK (tier IS NULL OR tier BETWEEN 1 AND 5)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_collection_member_collection "
        "ON collection_member (collection_id, position)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_collection_member_parliamentarian "
        "ON collection_member (parliamentarian_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_collection_member_cpf "
        "ON collection_member (cpf)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_block (
            id BIGSERIAL PRIMARY KEY,
            collection_id BIGINT NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
            member_id BIGINT REFERENCES collection_member(id) ON DELETE SET NULL,
            kind TEXT NOT NULL,
            ref_id BIGINT,
            title TEXT,
            body TEXT,
            url TEXT,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            position INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_collection_block_collection "
        "ON collection_block (collection_id, position)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS collection_block")
    op.execute("DROP TABLE IF EXISTS collection_member")
    op.execute("DROP TABLE IF EXISTS collection")
