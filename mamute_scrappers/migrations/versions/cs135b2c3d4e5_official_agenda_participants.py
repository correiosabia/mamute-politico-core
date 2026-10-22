"""CS-135: participantes e órgão na agenda oficial

A segunda fonte (e-Agendas da CGU, Executivo federal) publica a lista de
participantes de cada compromisso, públicos e privados, à parte do assunto.
Quem foi recebido às vezes só aparece ali, então a busca precisa olhar os dois
campos. `organization` guarda o órgão da autoridade (na agenda do BC é sempre o
próprio BC).

Revision ID: cs135b2c3d4e5
Revises: cs135a1b2c3d4
"""

from alembic import op

revision = "cs135b2c3d4e5"
down_revision = "cs135a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE official_agenda_item ADD COLUMN IF NOT EXISTS participants TEXT")
    op.execute("ALTER TABLE official_agenda_item ADD COLUMN IF NOT EXISTS organization TEXT")
    op.execute(
        "UPDATE official_agenda_item SET organization = 'Banco Central do Brasil' "
        "WHERE source = 'bcb' AND organization IS NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE official_agenda_item DROP COLUMN IF EXISTS organization")
    op.execute("ALTER TABLE official_agenda_item DROP COLUMN IF EXISTS participants")
