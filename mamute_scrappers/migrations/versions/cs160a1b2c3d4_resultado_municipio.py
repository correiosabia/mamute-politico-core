"""CS-160: resultado de presidente por municipio, nos dois turnos

O arquivo de UF do TSE (`tse_result_file`) so diz quem ganhou em cada estado.
Para comparar o 1o com o 2o turno por regiao (regiao geografica
intermediaria do IBGE, capital x interior) precisamos do arquivo de cada
municipio, com eleitorado, comparecimento, abstencao, brancos, nulos e os
votos de cada candidato.

- `ibge_municipio`: municipio -> regiao imediata, regiao intermediaria e
  macrorregiao, da API de localidades do IBGE (carregada pela coleta).
- `tse_resultado_municipio`: um municipio x eleicao (turno) x cargo. O
  exterior vem com `uf = 'zz'` e sem codigo IBGE.
- `tse_resultado_municipio_candidato`: votos de cada candidato no municipio.
- `comparacao_turnos_estimativa`: matriz estimada de transferencia de votos
  entre os turnos (inferencia, nao dado oficial), por recorte.

Revision ID: cs160a1b2c3d4
Revises: cs148a1b2c3d4
"""

from alembic import op

revision = "cs160a1b2c3d4"
down_revision = "cs148a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ibge_municipio (
            cd_ibge INTEGER PRIMARY KEY,
            nome TEXT NOT NULL,
            uf TEXT NOT NULL,
            regiao_intermediaria_id INTEGER,
            regiao_intermediaria_nome TEXT,
            regiao_imediata_id INTEGER,
            regiao_imediata_nome TEXT,
            macrorregiao TEXT,
            atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tse_resultado_municipio (
            id BIGSERIAL PRIMARY KEY,
            ciclo TEXT NOT NULL,
            codigo_eleicao INTEGER NOT NULL,
            turno SMALLINT NOT NULL,
            cargo_codigo INTEGER NOT NULL,
            uf TEXT NOT NULL,
            cd_tse TEXT NOT NULL,
            cd_ibge INTEGER,
            nome TEXT NOT NULL,
            capital BOOLEAN NOT NULL DEFAULT false,
            eleitorado BIGINT,
            -- `e.est`: eleitorado das secoes ja totalizadas; base da abstencao enquanto a apuracao corre
            eleitorado_apurado BIGINT,
            comparecimento BIGINT,
            abstencao BIGINT,
            brancos BIGINT,
            nulos BIGINT,
            validos BIGINT,
            percentual_apurado NUMERIC(5,2),
            totalizacao_final BOOLEAN NOT NULL DEFAULT false,
            tse_atualizado_em TIMESTAMPTZ,
            coletado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_tse_resultado_municipio UNIQUE (codigo_eleicao, cargo_codigo, cd_tse)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tse_resultado_municipio_turno "
        "ON tse_resultado_municipio (ciclo, turno, cargo_codigo)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tse_resultado_municipio_candidato (
            resultado_id BIGINT NOT NULL REFERENCES tse_resultado_municipio(id) ON DELETE CASCADE,
            sqcand BIGINT NOT NULL,
            numero INTEGER,
            nome_urna TEXT,
            votos BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (resultado_id, sqcand)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS comparacao_turnos_estimativa (
            ciclo TEXT NOT NULL,
            cargo_codigo INTEGER NOT NULL,
            recorte TEXT NOT NULL,
            chave TEXT NOT NULL,
            origem TEXT NOT NULL,
            destino TEXT NOT NULL,
            proporcao NUMERIC(6,4) NOT NULL,
            votos BIGINT NOT NULL,
            municipios INTEGER NOT NULL,
            calculado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (ciclo, cargo_codigo, recorte, chave, origem, destino)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS comparacao_turnos_estimativa")
    op.execute("DROP TABLE IF EXISTS tse_resultado_municipio_candidato")
    op.execute("DROP TABLE IF EXISTS tse_resultado_municipio")
    op.execute("DROP TABLE IF EXISTS ibge_municipio")
