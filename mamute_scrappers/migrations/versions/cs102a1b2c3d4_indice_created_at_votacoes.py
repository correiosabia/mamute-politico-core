"""CS-102: indice por created_at em roll_call_votes

O crawler de votacoes da Camara (`_get_last_vote_date`, a cada 3 h) pergunta
"qual o voto de deputado gravado mais recentemente?" para saber de onde
retomar (`JOIN parliamentarian WHERE type = ... ORDER BY created_at DESC
LIMIT 1`). Sem indice em created_at o Postgres lia os ~230k votos do tipo e
ordenava tudo para devolver 1 linha: 1,1 s de media e 3,1 s com cache frio,
o primeiro alerta de query lenta do monitor da LV DEV.

Com o indice o planner anda por created_at de tras para frente e para no
primeiro voto do tipo pedido: 210 ms -> 1 ms (Deputado), 360 ms -> 5 ms
(Senador). 2 MB de indice.

Ja criado a mao em producao em 24/09/2026 (00:23 CEST). CONCURRENTLY e
IF NOT EXISTS pelos mesmos motivos da cs74a1b2c3d4: nao trava escrita e o
deploy nao quebra ao encontrar o indice.

Revision ID: cs102a1b2c3d4
Revises: cs72a1b2c3d4
"""

from alembic import op

revision = "cs102a1b2c3d4"
down_revision = "cs72a1b2c3d4"
branch_labels = None
depends_on = None

INDEX = "ix_roll_call_votes_created_at"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON roll_call_votes (created_at)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
