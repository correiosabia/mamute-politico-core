"""CS-129: foto de quem cada pessoa selecionou em cada turno

`projetos_candidacy` e a selecao viva (desmarcar apaga a linha). Esta tabela
guarda o que estava selecionado quando a votacao de cada turno fechou e nao
muda depois: e o que permite mostrar "1º", "2º" ou "1º e 2º" na busca e,
depois da posse, lembrar quem a pessoa escolheu, inclusive quem nao se elegeu.
So a selecao da pessoa (nunca "em quem votou").

A foto do 1o turno foi tirada em producao em 05/10/2026 com este mesmo SQL
(148 linhas); aqui ela se repete de forma idempotente para outros ambientes.
A do 2o turno sai do cron em 25/10 (scripts/selecao_por_turno.py).

Revision ID: cs129a1b2c3d4
Revises: cs128a1b2c3d4
"""

from alembic import op

revision = "cs129a1b2c3d4"
down_revision = "cs128a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS projetos_candidacy_turno (
            id BIGSERIAL PRIMARY KEY,
            projeto_id BIGINT NOT NULL REFERENCES projetos(id) ON DELETE CASCADE,
            candidacy_id BIGINT NOT NULL REFERENCES candidacy(id) ON DELETE CASCADE,
            ciclo TEXT NOT NULL,
            turno SMALLINT NOT NULL,
            selecionado_em TIMESTAMPTZ,
            registrado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_projetos_candidacy_turno UNIQUE (projeto_id, candidacy_id, ciclo, turno)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_projetos_candidacy_turno_projeto "
        "ON projetos_candidacy_turno (projeto_id, ciclo)"
    )
    op.execute(
        """
        INSERT INTO projetos_candidacy_turno (projeto_id, candidacy_id, ciclo, turno, selecionado_em)
        SELECT pc.projeto_id, pc.candidacy_id, 'ele2026', 1, pc.created_at
          FROM projetos_candidacy pc
          JOIN candidacy c ON c.id = pc.candidacy_id
         WHERE c.election_year = 2026
           AND pc.created_at <= timestamptz '2026-10-04 17:00:00-03'
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS projetos_candidacy_turno")
