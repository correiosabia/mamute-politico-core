"""CS-83: indice por data em speeches_transcripts

O contexto SQL do chatbot pede "os N discursos mais recentes que casam com
os termos" (`ORDER BY date DESC NULLS LAST, id DESC LIMIT N`). Quando o termo
e generico ("projeto" aparece em 52% dos 123k discursos), o indice trigram
nao ajuda e o Postgres varria a tabela inteira (6 GB de buffers) para
devolver 5 linhas — 6 s por consulta, 5 consultas por pergunta.

Com um indice que casa com o ORDER BY, o planner anda pelos discursos mais
recentes e para nos N primeiros que casam: com termo generico isso sao
poucas dezenas de linhas. Termo raro continua no trigram (custo decide).

Medido em producao em 10/09/2026 (relatorio da CS-83). CONCURRENTLY e
IF NOT EXISTS pelos mesmos motivos da cs74a1b2c3d4.

Revision ID: cs83a1b2c3d4
Revises: cs74a1b2c3d4
"""

from alembic import op

revision = "cs83a1b2c3d4"
down_revision = "cs74a1b2c3d4"
branch_labels = None
depends_on = None

INDEX = "ix_speeches_transcripts_date_id"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
            "ON speeches_transcripts (date DESC NULLS LAST, id DESC)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
