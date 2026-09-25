"""CS-92: temas oficiais da proposicao

A coluna "Tema" da aba de proposicoes era um palpite da interface: ela
vasculhava o `details` bruto atras de "keywords"/"indexacao", que sao a
indexacao por palavra-chave (feita depois, pelo setor de documentacao) e nao a
area tematica. Proposicao recente saia vazia.

`themes` guarda as areas tematicas oficiais da Casa, na ordem em que ela
devolve:
  - Camara: GET /proposicoes/{id}/temas (1 a 4 por proposicao, sem
    hierarquia — o campo `relevancia` vem 0 em todas, entao nao ha um
    "tema principal" para escolher);
  - Senado: `details.processo.classificacoes[].descricao`, que o coletor ja
    guarda.

NULL e [] significam coisas diferentes e a interface mostra as duas de jeito
diferente: NULL = ainda nao coletado; [] = coletado, a Casa nao classificou
(a Camara nao classifica REQ, EMC, PRL, PAR, SBT). O historico e preenchido
por `scripts/backfill_proposition_themes.py`.

Revision ID: cs92a1b2c3d4
Revises: cs102a1b2c3d4
"""

from alembic import op

revision = "cs92a1b2c3d4"
down_revision = "cs102a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE proposition ADD COLUMN IF NOT EXISTS themes JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE proposition DROP COLUMN IF EXISTS themes")
