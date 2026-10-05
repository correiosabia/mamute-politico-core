"""CS-128: resultado majoritario definido matematicamente antes do TSE encerrar

O TSE pode levar horas para encerrar a totalizacao das ultimas secoes. Para
presidente, governador e senador, o coletor marca o arquivo como
`definido_matematicamente` quando nem os eleitores das secoes que faltam
mudam o resultado (regra em `tse_crawler.resultados_parsing.definicao_matematica`),
e grava em `candidacy_result.situacao_matematica` a situacao de cada um
("Eleito", "2º turno", "Não eleito"). O aviso de majoritarios usa isso para
sair antes; a situacao oficial (`situacao`) continua vindo so do TSE.

Revision ID: cs128a1b2c3d4
Revises: cs127a1b2c3d4
"""

from alembic import op

revision = "cs128a1b2c3d4"
down_revision = "cs127a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tse_result_file "
        "ADD COLUMN IF NOT EXISTS definido_matematicamente BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE candidacy_result ADD COLUMN IF NOT EXISTS situacao_matematica TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE candidacy_result DROP COLUMN IF EXISTS situacao_matematica")
    op.execute("ALTER TABLE tse_result_file DROP COLUMN IF EXISTS definido_matematicamente")
