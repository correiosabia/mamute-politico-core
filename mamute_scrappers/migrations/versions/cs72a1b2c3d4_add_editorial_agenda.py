"""CS-72: pautas editoriais dos parlamentares

Quem acompanha politica procura parlamentar por assunto ("quem trabalha com
meio ambiente?"), nao por nome — e hoje a unica entrada da tela de selecao e o
nome. Estas duas tabelas dao o eixo que faltava.

Decisoes que nao se leem no DDL:

VOCABULARIO FECHADO. `editorial_agenda` e semeada aqui com as 14 pautas do
design e so cresce pelo painel admin. O classificador (CS-72, fatia 3) recebe
essa lista e DESCARTA slug que nao esteja nela — nunca cria. Sem isso a feature
vira tag livre gerada por IA, que e exatamente o que o brief proibe.

AS `description` SAO O CLASSIFICADOR, nao documentacao. Elas entram no prompt e
decidem a fronteira entre pautas vizinhas. As quatro que mais brigam estao
escritas com o "nao e" explicito: corrupcao x emenda parlamentar, agropecuaria
x meio ambiente, costumes x direitos humanos, e violencia dentro de seguranca
publica. Vagas, o rank vira sorteio.

DE 1 A 3 PAUTAS por parlamentar, garantido pelo banco (`ck_..._rank` mais as
duas UNIQUE). Quem nao tem evidencia fica SEM pauta: a tela diz que a analise
esta pendente, e nunca existe uma pauta generica de escape.

`active` EM VEZ DE DELETE. Apagar uma pauta que ja classificou alguem
destruiria o trabalho do job; o admin desativa e a linha fica dormente, igual
as marcacoes (SPEC-001).

TENTATIVA E RESULTADO SAO TABELAS DIFERENTES. `parliamentarian_agenda` guarda o
resultado (as pautas); `parliamentarian_agenda_run` guarda que a pessoa FOI
olhada, mesmo quando nada encaixou. Sem a segunda, zero linhas na primeira quer
dizer as duas coisas ao mesmo tempo — "nunca analisei" e "analisei e deu nada" —
e o job reenvia essa pessoa ao modelo em toda rodada sem nunca gravar nada.

`vocabulary_version` MORA NA PROPRIA `editorial_agenda`, e a versao corrente e
o MAX da coluna. Nao ha tabela de linha unica para um inteiro (a `marcacoes_config`
ja registra a aversao a isso), e usar MAX(id) nao serviria: editar a
`description` de uma pauta existente precisa disparar reclassificacao, e nao
cria id novo.

Revision ID: cs72a1b2c3d4
Revises: cs74a1b2c3d4
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "cs72a1b2c3d4"
down_revision = "cs74a1b2c3d4"
branch_labels = None
depends_on = None


# As 14 pautas do design. A ordem e a `position` — e a ordem em que a tela
# lista os filtros, escolhida por relevancia editorial e nao alfabetica.
#
# Cada `description` e escrita para ser lida por um modelo, nao por uma pessoa:
# diz o que ENTRA, e nas fronteiras disputadas diz o que NAO entra e para onde
# vai. Ao editar qualquer uma, releia as vizinhas — o custo de uma fronteira
# vaga e o rank virar sorteio.
SEED_PAUTAS = [
    (
        "Meio Ambiente",
        "meio-ambiente",
        "Clima, desmatamento, licenciamento ambiental, unidades de conservação, "
        "poluição, recursos hídricos, saneamento, resíduos sólidos, energia limpa "
        "e transição energética. Inclui o licenciamento e o desmatamento ligados "
        "à atividade rural: quando o debate é sobre o impacto ambiental da "
        "produção, a pauta é esta, e não Agropecuária.",
    ),
    (
        "Saúde",
        "saude",
        "SUS, financiamento e gestão da saúde, vigilância sanitária, epidemias e "
        "vacinação, medicamentos, planos de saúde, saúde mental, carreiras da "
        "saúde e infraestrutura hospitalar.",
    ),
    (
        "Educação",
        "educacao",
        "Educação básica e superior, financiamento (FUNDEB, piso do magistério), "
        "currículo, avaliação, creches, educação profissional, carreiras docentes "
        "e acesso ao ensino. Pesquisa científica e pós-graduação stricto sensu "
        "pendem para Ciência e Tecnologia quando o foco é a produção de "
        "conhecimento, e não o ensino.",
    ),
    (
        "Segurança Pública",
        "seguranca-publica",
        "Violência, criminalidade e homicídios; polícia e carreiras policiais; "
        "armas e munições; sistema prisional; facções e crime organizado; "
        "tráfico de drogas do ponto de vista do enfrentamento criminal; "
        "legislação penal e processual penal. É AQUI que mora qualquer discurso "
        "sobre violência, inclusive violência doméstica e violência contra "
        "grupos específicos, quando o eixo é o crime, a punição ou o "
        "policiamento; se o eixo for proteção e direitos da vítima, é Direitos "
        "Humanos.",
    ),
    (
        "Corrupção e Transparência",
        "corrupcao-e-transparencia",
        "Improbidade administrativa, desvio de recursos públicos, CPIs, controle "
        "externo (TCU, CGU), prestação de contas, lei de acesso à informação, "
        "dados abertos, conflito de interesses e lobby. NÃO é emenda parlamentar "
        "em si: destinação, execução ou defesa de emendas e orçamento público "
        "pertence a Economia e Tributação, ou à pauta setorial do objeto "
        "financiado. Só classifique aqui quando a acusação de irregularidade, o "
        "controle ou a transparência forem o assunto, e não o gasto.",
    ),
    (
        "Economia e Tributação",
        "economia-e-tributacao",
        "Política fiscal e orçamento público (incluindo emendas parlamentares e "
        "sua destinação), reforma tributária, impostos e taxas, juros e política "
        "monetária, dívida pública, crédito, comércio interno, micro e pequenas "
        "empresas, desestatização e regulação econômica.",
    ),
    (
        "Trabalho e Previdência",
        "trabalho-e-previdencia",
        "Legislação trabalhista, sindicatos e negociação coletiva, salário "
        "mínimo, desemprego e políticas de emprego, trabalho por aplicativo e "
        "informalidade, saúde e segurança do trabalho, INSS, aposentadorias, "
        "benefícios previdenciários e assistenciais (BPC), reforma da "
        "previdência.",
    ),
    (
        "Direitos Humanos",
        "direitos-humanos",
        "Direitos de grupos historicamente vulneráveis: mulheres, população "
        "negra, indígenas e quilombolas, pessoas com deficiência, crianças e "
        "adolescentes, idosos, população LGBT, pessoas em situação de rua, "
        "migrantes e refugiados. Igualdade, combate à discriminação, proteção "
        "social e reparação. A fronteira com Costumes e Religião é proposital: "
        "aqui está a proteção de pessoas e o combate à discriminação; lá está a "
        "disputa moral sobre condutas e valores.",
    ),
    (
        "Agropecuária",
        "agropecuaria",
        "Produção agrícola e pecuária, crédito rural e Plano Safra, seguro "
        "agrícola, defesa agropecuária e sanidade animal e vegetal, agricultura "
        "familiar, abastecimento, exportação de commodities, maquinário e "
        "insumos, regularização fundiária produtiva. Quando o assunto for "
        "licenciamento, desmatamento, agrotóxicos sob a ótica do dano ambiental "
        "ou área de preservação, classifique em Meio Ambiente — mesmo que o "
        "parlamentar seja da bancada ruralista.",
    ),
    (
        "Infraestrutura e Transportes",
        "infraestrutura-e-transportes",
        "Rodovias, ferrovias, portos, aeroportos e hidrovias; mobilidade urbana "
        "e transporte público; obras públicas e concessões; habitação e "
        "urbanismo; energia elétrica e combustíveis do ponto de vista de "
        "geração, distribuição e preço; telecomunicações como infraestrutura "
        "física.",
    ),
    (
        "Ciência e Tecnologia",
        "ciencia-e-tecnologia",
        "Pesquisa científica e fomento (CNPq, CAPES, FINEP), inovação, "
        "propriedade intelectual, inteligência artificial, regulação de "
        "plataformas digitais e redes sociais, proteção de dados e privacidade, "
        "segurança cibernética, inclusão digital, biotecnologia e espaço.",
    ),
    (
        "Cultura e Esporte",
        "cultura-e-esporte",
        "Política cultural e financiamento à cultura (Lei Rouanet, Aldir Blanc), "
        "patrimônio histórico, audiovisual, economia criativa; esporte "
        "profissional e amador, clubes, apostas esportivas, grandes eventos e "
        "incentivo ao esporte.",
    ),
    (
        "Política Externa",
        "politica-externa",
        "Relações diplomáticas, tratados e acordos internacionais, blocos "
        "regionais (Mercosul, BRICS), comércio exterior e tarifas, defesa "
        "nacional e Forças Armadas, fronteiras, cooperação internacional e "
        "posicionamento sobre conflitos no exterior.",
    ),
    (
        "Costumes e Religião",
        "costumes-e-religiao",
        "Disputa moral sobre condutas e valores: aborto, eutanásia, "
        "descriminalização de drogas, concepção de família, educação sexual, "
        "ideologia de gênero como pauta de debate público, liberdade religiosa, "
        "laicidade do Estado e relação entre igrejas e poder público. Separada "
        "de Direitos Humanos DE PROPÓSITO: o mesmo tema pode aparecer nas duas, "
        "e o que decide é o ângulo — proteção e direito da pessoa vai para "
        "Direitos Humanos; permissão, proibição ou valor moral da conduta vem "
        "para cá.",
    ),
]


def upgrade() -> None:
    op.create_table(
        "editorial_agenda",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("description", sa.Text()),
        sa.Column("position", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        # Versao do vocabulario a que esta linha pertence. O PUT do admin sobe
        # o valor de TODAS as linhas para um numero novo; a versao corrente e o
        # MAX. E o gatilho de reclassificacao do job.
        sa.Column(
            "vocabulary_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "parliamentarian_agenda",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "parliamentarian_id",
            sa.BigInteger(),
            sa.ForeignKey("parliamentarian.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agenda_id",
            sa.BigInteger(),
            sa.ForeignKey("editorial_agenda.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2)),
        sa.Column("evidence", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column(
            "vocabulary_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("rank BETWEEN 1 AND 3", name="ck_parliamentarian_agenda_rank"),
        sa.UniqueConstraint(
            "parliamentarian_id", "agenda_id", name="uq_parliamentarian_agenda"
        ),
        sa.UniqueConstraint(
            "parliamentarian_id", "rank", name="uq_parliamentarian_agenda_rank"
        ),
    )
    # Sem este indice a listagem de 513 parlamentares custaria um seq scan por
    # requisicao — mesmo motivo da cs74a1b2c3d4.
    op.create_index(
        "ix_parliamentarian_agenda_parliamentarian_id",
        "parliamentarian_agenda",
        ["parliamentarian_id"],
    )
    op.create_index(
        "ix_parliamentarian_agenda_agenda_id",
        "parliamentarian_agenda",
        ["agenda_id"],
    )

    # Uma linha por parlamentar, sobrescrita a cada rodada: "olhei fulano em tal
    # dia, e deu isto". Separa "nunca analisei" de "analisei e nada encaixou" —
    # em `parliamentarian_agenda` os dois casos sao zero linhas, e o job sem
    # essa distincao reenvia a mesma pessoa ao modelo todo dia, para sempre.
    op.create_table(
        "parliamentarian_agenda_run",
        sa.Column(
            "parliamentarian_id",
            sa.BigInteger(),
            sa.ForeignKey("parliamentarian.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("source", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column(
            "vocabulary_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome in ('classificado', 'sem_pauta', 'sem_material')",
            name="ck_parliamentarian_agenda_run_outcome",
        ),
    )

    tabela = sa.table(
        "editorial_agenda",
        sa.column("name", sa.Text),
        sa.column("slug", sa.Text),
        sa.column("description", sa.Text),
        sa.column("position", sa.SmallInteger),
    )
    op.bulk_insert(
        tabela,
        [
            {
                "name": name,
                "slug": slug,
                "description": description,
                "position": posicao,
            }
            for posicao, (name, slug, description) in enumerate(SEED_PAUTAS, start=1)
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_parliamentarian_agenda_agenda_id", table_name="parliamentarian_agenda"
    )
    op.drop_index(
        "ix_parliamentarian_agenda_parliamentarian_id",
        table_name="parliamentarian_agenda",
    )
    op.drop_table("parliamentarian_agenda_run")
    op.drop_table("parliamentarian_agenda")
    op.drop_table("editorial_agenda")
