"""CS-121: espelho financeiro do Ghost (membros, assinaturas e eventos)

A receita do painel era a soma do preço de tabela do plano de cada usuário e
ignorava ofertas e cortesias. O Ghost guarda o
valor real de cada assinatura (`mrr`, já com desconto) e o histórico de
mudanças (`members_paid_subscription_events.mrr_delta`,
`members_status_events`). Estas tabelas são uma cópia dessas informações,
renovada pela API (`api/services/ghost_finance_sync.py`): as métricas leem só
o Postgres, e uma mudança no schema do Ghost quebra o sync com erro no log,
não a tela.

Valores em centavos, como no Ghost. Cópia completa a cada sync (o volume é de
centenas a poucos milhares de linhas).

Revision ID: cs121a1b2c3d4
Revises: cs119a1b2c3d4
"""

from alembic import op

revision = "cs121a1b2c3d4"
down_revision = "cs119a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_membro (
            id TEXT PRIMARY KEY,
            email TEXT,
            status TEXT NOT NULL,
            plano TEXT,
            plano_valor_mensal_centavos INTEGER,
            criado_em TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_membro_status_evento (
            id TEXT PRIMARY KEY,
            membro_id TEXT NOT NULL,
            de_status TEXT,
            para_status TEXT NOT NULL,
            criado_em TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_assinatura (
            id TEXT PRIMARY KEY,
            membro_id TEXT,
            email TEXT,
            plano TEXT,
            status TEXT NOT NULL,
            intervalo TEXT,
            valor_tabela_centavos INTEGER NOT NULL DEFAULT 0,
            mrr_centavos INTEGER NOT NULL DEFAULT 0,
            oferta TEXT,
            oferta_desconto_tipo TEXT,
            oferta_desconto_valor INTEGER,
            oferta_duracao TEXT,
            oferta_meses INTEGER,
            desconto_fim TIMESTAMPTZ,
            inicio TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_assinatura_evento (
            id TEXT PRIMARY KEY,
            membro_id TEXT,
            assinatura_id TEXT,
            tipo TEXT NOT NULL,
            mrr_delta_centavos INTEGER NOT NULL DEFAULT 0,
            criado_em TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ghost_sync_execucao (
            id BIGSERIAL PRIMARY KEY,
            iniciado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
            concluido_em TIMESTAMPTZ,
            ok BOOLEAN NOT NULL DEFAULT FALSE,
            membros INTEGER,
            assinaturas INTEGER,
            eventos INTEGER,
            erro TEXT
        )
        """
    )


def downgrade() -> None:
    for tabela in (
        "ghost_sync_execucao",
        "ghost_assinatura_evento",
        "ghost_assinatura",
        "ghost_membro_status_evento",
        "ghost_membro",
    ):
        op.execute(f"DROP TABLE IF EXISTS {tabela}")
