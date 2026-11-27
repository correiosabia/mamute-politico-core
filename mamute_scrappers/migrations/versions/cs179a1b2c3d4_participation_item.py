"""CS-179: itens de participação (consultas, ideias, enquetes, abaixo-assinados)

Cada linha é uma oportunidade de participação aberta numa fonte pública
(Brasil Participativo, e-Cidadania do Senado, enquetes da Câmara) ou cadastrada
à mão pela curadoria (Change.org, Avaaz e afins). A coleta só enche a fila:
`status` nasce `pendente` e só o que um admin aprova (`aprovado`) aparece para o
leitor. Regravar a fonte nunca mexe em status, tema e nota.

`closes_on` NULL = sem prazo (consulta do Senado e enquete da Câmara ficam
abertas enquanto a proposição tramita). `source_open` = a fonte ainda mostra o
item aberto. `score` é o placar como a fonte publica (opções e contagens).

Revision ID: cs179a1b2c3d4
Revises: cs137a1b2c3d4
"""

from alembic import op

revision = "cs179a1b2c3d4"
down_revision = "cs137a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS participation_item (
            id BIGSERIAL PRIMARY KEY,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            reference TEXT,
            organization TEXT,
            url TEXT NOT NULL,
            opens_on DATE,
            closes_on DATE,
            score JSONB,
            score_updated_at TIMESTAMPTZ,
            source_open BOOLEAN NOT NULL DEFAULT TRUE,
            closed_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'pendente',
            theme TEXT,
            note TEXT,
            reviewed_by TEXT,
            reviewed_at TIMESTAMPTZ,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_participation_item UNIQUE (source, external_id),
            CONSTRAINT ck_participation_item_status CHECK (status IN ('pendente', 'aprovado', 'descartado'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_participation_item_status "
        "ON participation_item (status, source_open)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS participation_item")
