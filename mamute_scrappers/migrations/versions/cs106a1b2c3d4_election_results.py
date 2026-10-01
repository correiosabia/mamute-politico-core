"""CS-106: resultado oficial da eleicao e aviso a quem acompanha candidatos

Tres tabelas:

- `tse_result_file`: um registro por arquivo de resultado do TSE (eleicao x
  UF x cargo). Guarda se a totalizacao daquele arquivo ja encerrou (`tf` do
  JSON). Existe a parte porque um arquivo pode fechar sem nenhum candidato
  casado na nossa base, e o envio precisa saber que ele fechou mesmo assim.
- `candidacy_result`: situacao oficial de cada candidatura por turno, com o
  texto cru do TSE ("Eleito", "Eleito por media", "2o turno", "Suplente",
  "Nao eleito"). Base das tasks CS-107 (perfil dos eleitos) e CS-108 (2o turno).
- `election_result_notice`: um aviso por projeto x ciclo x turno. A unique e
  a trava contra e-mail duplicado; `seen_at` e o "ja vi" do modal no app.

Revision ID: cs106a1b2c3d4
Revises: cs92a1b2c3d4
"""

from alembic import op

revision = "cs106a1b2c3d4"
down_revision = "cs92a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tse_result_file (
            id BIGSERIAL PRIMARY KEY,
            ciclo TEXT NOT NULL,
            codigo_eleicao INTEGER NOT NULL,
            turno SMALLINT NOT NULL,
            uf TEXT NOT NULL,
            cargo_codigo INTEGER NOT NULL,
            totalizacao_final BOOLEAN NOT NULL DEFAULT FALSE,
            tse_atualizado_em TIMESTAMPTZ,
            candidatos_no_arquivo INTEGER NOT NULL DEFAULT 0,
            candidatos_casados INTEGER NOT NULL DEFAULT 0,
            coletado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_tse_result_file UNIQUE (codigo_eleicao, uf, cargo_codigo)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tse_result_file_turno_cargo_uf "
        "ON tse_result_file (turno, cargo_codigo, uf)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS candidacy_result (
            id BIGSERIAL PRIMARY KEY,
            candidacy_id BIGINT NOT NULL
                REFERENCES candidacy (id) ON DELETE CASCADE,
            turno SMALLINT NOT NULL,
            codigo_eleicao INTEGER NOT NULL,
            situacao TEXT,
            eleito BOOLEAN,
            votos BIGINT,
            percentual NUMERIC(7, 4),
            destinacao_voto TEXT,
            totalizacao_final BOOLEAN NOT NULL DEFAULT FALSE,
            tse_atualizado_em TIMESTAMPTZ,
            coletado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_candidacy_result_turno UNIQUE (candidacy_id, turno)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_candidacy_result_turno_situacao "
        "ON candidacy_result (turno, situacao)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS election_result_notice (
            id BIGSERIAL PRIMARY KEY,
            projeto_id BIGINT NOT NULL
                REFERENCES projetos (id) ON DELETE CASCADE,
            ciclo TEXT NOT NULL,
            turno SMALLINT NOT NULL,
            payload JSONB NOT NULL,
            email_status TEXT NOT NULL DEFAULT 'pending',
            tentativas SMALLINT NOT NULL DEFAULT 0,
            ultimo_erro TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            sent_at TIMESTAMPTZ,
            seen_at TIMESTAMPTZ,
            CONSTRAINT uq_election_result_notice UNIQUE (projeto_id, ciclo, turno)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS election_result_notice")
    op.execute("DROP TABLE IF EXISTS candidacy_result")
    op.execute("DROP TABLE IF EXISTS tse_result_file")
