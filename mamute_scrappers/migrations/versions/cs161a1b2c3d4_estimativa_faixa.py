"""CS-161: faixa de incerteza e ponto de partida da estimativa de transferencia

`proporcao_min`/`proporcao_max` = p5 a p95 do bootstrap de municipios,
alargado com o resultado de metade e do dobro do peso do prior. A tela marca
como incerta a celula com faixa larga. `proporcao_prior`/`fonte_prior` = o
ponto de partida (pesquisa ou eleicao anterior) e de onde veio, para a tela
mostrar pesquisa x estimativa. Ver `tse_crawler/transferencia_votos.py`.

Revision ID: cs161a1b2c3d4
Revises: cs160a1b2c3d4
"""

from alembic import op

revision = "cs161a1b2c3d4"
down_revision = "cs160a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE comparacao_turnos_estimativa ADD COLUMN IF NOT EXISTS proporcao_min NUMERIC(6,4)")
    op.execute("ALTER TABLE comparacao_turnos_estimativa ADD COLUMN IF NOT EXISTS proporcao_max NUMERIC(6,4)")
    op.execute("ALTER TABLE comparacao_turnos_estimativa ADD COLUMN IF NOT EXISTS proporcao_prior NUMERIC(6,4)")
    op.execute("ALTER TABLE comparacao_turnos_estimativa ADD COLUMN IF NOT EXISTS fonte_prior TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE comparacao_turnos_estimativa DROP COLUMN IF EXISTS fonte_prior")
    op.execute("ALTER TABLE comparacao_turnos_estimativa DROP COLUMN IF EXISTS proporcao_prior")
    op.execute("ALTER TABLE comparacao_turnos_estimativa DROP COLUMN IF EXISTS proporcao_max")
    op.execute("ALTER TABLE comparacao_turnos_estimativa DROP COLUMN IF EXISTS proporcao_min")
