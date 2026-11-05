"""CS-95: a timeline eleitoral guarda "ainda nao buscamos" x "buscamos e nao havia"

Duas situacoes caiam no mesmo vazio da aba Trajetoria:

- Patrimonio de uma eleicao: `electoral_history.assets_status` (NULL enquanto
  a busca nao aconteceu; depois `declarado`, `sem_bens` ou `nao_divulgado`).
  As linhas ja buscadas sao classificadas aqui. As sem nenhum bem tinham
  `declared_assets = 0` porque o TSE devolve `totalDeBens: 0` mesmo sem
  declaracao; o total passa a NULL, que a tela ja trata como "sem
  declaracao" e nao entra na variacao. Em 04/11/2026 eram 19.575 linhas com
  esse zero, 172 parlamentares com algum "R$ 0,00" e 165 com variacao
  calculada a partir dele.
- Parlamentar sem nenhuma linha: tabela `electoral_history_search`, com o
  resultado da ultima varredura completa da fase 2 do crawler.

Revision ID: cs95a1b2c3d4
Revises: cs101a1b2c3d4
"""

from alembic import op

revision = "cs95a1b2c3d4"
down_revision = "cs101a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE electoral_history ADD COLUMN IF NOT EXISTS assets_status TEXT")
    # Mesma regra de history_parsing.build_assets_payload; so linhas ja
    # buscadas e ainda sem status, entao rodar de novo nao muda nada.
    op.execute(
        """
        UPDATE electoral_history
        SET assets_status = CASE
                WHEN COALESCE(assets_count, 0) > 0 THEN 'declarado'
                WHEN election_year < 2006 THEN 'nao_divulgado'
                ELSE 'sem_bens'
            END,
            declared_assets = CASE
                WHEN COALESCE(assets_count, 0) > 0 THEN declared_assets
            END
        WHERE assets_fetched_at IS NOT NULL AND assets_status IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS electoral_history_search (
            parliamentarian_id BIGINT PRIMARY KEY
                REFERENCES parliamentarian (id) ON DELETE CASCADE,
            outcome TEXT NOT NULL,
            searched_at TIMESTAMP NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS electoral_history_search")
    # O zero das linhas sem bens nao volta: era a informacao errada.
    op.execute("ALTER TABLE electoral_history DROP COLUMN IF EXISTS assets_status")
