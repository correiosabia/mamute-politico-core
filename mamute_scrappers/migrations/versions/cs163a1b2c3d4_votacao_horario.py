"""CS-163: votos depois das 17h, a partir dos arquivos de urna do TSE

Duas tabelas, preenchidas pela carga de `tse_crawler/votacao_horario.py`
(`carregar`), uma linha por eleicao e recorte:

- `votacao_horario_agregado`: Brasil, UF e municipio. As colunas soltas sao
  as usadas para ordenar e filtrar; o resto vem em `dados`.
- `votacao_horario_local`: local de votacao (escola) com pelo menos um voto
  depois das 17h, com bairro e coordenadas do cadastro do TSE.

Revision ID: cs163a1b2c3d4
Revises: cs162a1b2c3d4
"""

from alembic import op

revision = "cs163a1b2c3d4"
down_revision = "cs162a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS votacao_horario_agregado (
            id BIGSERIAL PRIMARY KEY,
            eleicao TEXT NOT NULL,
            nivel TEXT NOT NULL CHECK (nivel IN ('brasil', 'uf', 'municipio')),
            uf TEXT,
            municipio INTEGER,
            nome TEXT NOT NULL,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            secoes INTEGER NOT NULL,
            comparecimento BIGINT NOT NULL,
            votos_apos_17 INTEGER NOT NULL,
            dados JSONB NOT NULL,
            atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_votacao_horario_agregado
        ON votacao_horario_agregado (eleicao, nivel, COALESCE(uf, ''), COALESCE(municipio, 0))
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS votacao_horario_local (
            id BIGSERIAL PRIMARY KEY,
            eleicao TEXT NOT NULL,
            uf TEXT NOT NULL,
            municipio INTEGER NOT NULL,
            zona INTEGER NOT NULL,
            local INTEGER NOT NULL,
            nome TEXT,
            bairro TEXT,
            endereco TEXT,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            votos_apos_17 INTEGER NOT NULL,
            dados JSONB NOT NULL,
            UNIQUE (eleicao, uf, municipio, zona, local)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_votacao_horario_local_municipio ON votacao_horario_local (eleicao, uf, municipio)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS votacao_horario_local")
    op.execute("DROP TABLE IF EXISTS votacao_horario_agregado")
