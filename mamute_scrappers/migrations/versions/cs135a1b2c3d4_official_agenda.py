"""CS-135: agenda oficial de autoridades

Compromissos publicados por orgaos publicos na agenda de suas autoridades
(primeira fonte: diretoria do Banco Central). Cada linha e um compromisso de um
dia, com horario, texto, cidade e se foi por videoconferencia. Quem participa
fica no proprio texto: a busca e por termo, nao por pessoa cadastrada, porque
quem e recebido (empresarios, advogados) nao existe na base.

A coleta regrava o dia inteiro de cada autoridade (`source`, `authority_id`,
`event_date`): a fonte edita compromissos depois de publicados.

Revision ID: cs135a1b2c3d4
Revises: cs132a1b2c3d4
"""

from alembic import op

revision = "cs135a1b2c3d4"
down_revision = "cs132a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS official_agenda_item (
            id BIGSERIAL PRIMARY KEY,
            source TEXT NOT NULL,
            authority_id TEXT NOT NULL,
            authority_name TEXT,
            office TEXT,
            office_label TEXT,
            event_date DATE NOT NULL,
            seq INTEGER NOT NULL,
            starts_at TEXT,
            ends_at TEXT,
            description TEXT NOT NULL,
            place TEXT,
            remote BOOLEAN NOT NULL DEFAULT FALSE,
            url TEXT,
            collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_official_agenda_item UNIQUE (source, authority_id, event_date, seq)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_official_agenda_item_date "
        "ON official_agenda_item (event_date)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS official_agenda_item")
