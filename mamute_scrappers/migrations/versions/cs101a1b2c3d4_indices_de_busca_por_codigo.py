"""CS-101: indices nas colunas de busca por codigo + remove duplicatas da PK

O pg_stat_statements acumulado de 23/09 a 27/10/2026 mostrou que o tempo do
banco vai quase todo para buscas de uma linha por codigo externo, feitas pelos
crawlers a cada registro (upsert), em colunas sem indice. Cada chamada era um
seq scan da tabela inteira:

    proposition.proposition_code        1,78 mi chamadas  285 ms   ~141 h
    speeches_transcripts.speech_link    1,07 mi chamadas   88 ms    ~26 h
    authors_proposition.proposition_id   279 mil chamadas  91 ms     ~7 h
    proposition_status.proposition_status_code  283 mil    27 ms     ~2 h
    roll_call_votes (proposition_id, parliamentarian_id)  1,5 mi  4,5 ms  ~2 h

Somadas, ~180 h de execucao em 34 dias, lendo do disco tabelas de 0,8 a
1,3 GB. E a mesma leitura que disputa disco com as telas do app.

Entram tambem os dois indices pedidos na CS-101 (`parliamentarian_code` e
`plenary_attendance (parliamentarian_id, date)`) e o de
`speeches_transcripts_proposition.speeches_transcripts_id`. Nenhum e UNIQUE:
`proposition_code` e `speech_link` tem duplicatas em producao.

Os `ix_<tabela>_id` removidos repetem a chave primaria (btree sobre `id`), so
custam escrita e espaco (~240 MB; 186 MB em entities/keywords, que recebem
~1,2 mi DELETEs e inserts em lote). A PK atende as mesmas consultas.

CONCURRENTLY para nao travar escrita durante o deploy (fora de transacao, dai
o autocommit_block). IF [NOT] EXISTS para ser idempotente: os indices foram
criados a mao em producao antes do merge.

Revision ID: cs101a1b2c3d4
Revises: cs166b1c2d3e4
"""

from alembic import op

revision = "cs101a1b2c3d4"
down_revision = "cs166b1c2d3e4"
branch_labels = None
depends_on = None

NOVOS = (
    ("ix_proposition_proposition_code", "proposition", "proposition_code"),
    ("ix_speeches_transcripts_speech_link", "speeches_transcripts", "speech_link"),
    ("ix_authors_proposition_proposition_id", "authors_proposition", "proposition_id"),
    ("ix_proposition_status_code", "proposition_status", "proposition_status_code"),
    ("ix_roll_call_votes_proposition_parl", "roll_call_votes", "proposition_id, parliamentarian_id"),
    (
        "ix_speeches_transcripts_proposition_speech_id",
        "speeches_transcripts_proposition",
        "speeches_transcripts_id",
    ),
    ("ix_parliamentarian_code", "parliamentarian", "parliamentarian_code"),
    ("ix_plenary_attendance_parl_date", "plenary_attendance", "parliamentarian_id, date"),
)

DUPLICATAS_DA_PK = (
    "speeches_transcripts_entities",
    "speeches_transcripts_keywords",
    "proposition",
    "roll_call_votes",
    "authors_proposition",
    "speeches_transcripts",
    "plenary_attendance",
    "proposition_status",
    "speeches_transcripts_proposition",
    "parliamentarian",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for index_name, table, columns in NOVOS:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} ON {table} ({columns})")
        for table in DUPLICATAS_DA_PK:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS ix_{table}_id")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for table in DUPLICATAS_DA_PK:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_{table}_id ON {table} (id)")
        for index_name, _table, _columns in NOVOS:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")
