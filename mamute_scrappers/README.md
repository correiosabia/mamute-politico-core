# Mamute Scrappers

Módulo responsável pela coleta e sincronização de dados legislativos (Câmara e Senado), além de rotinas auxiliares de atualização.

Projeto pai: [README raiz](../README.md)

## Pré-requisitos

- Python 3.11+
- PostgreSQL acessível via `DATABASE_URL`

## Inicialização

1. Entre na pasta dos scrappers:

   ```bash
   cd mamute_scrappers
   ```

2. Crie e ative o ambiente virtual:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. Instale dependências:

   ```bash
   pip install -r requirements.txt
   ```

4. Configure variáveis de ambiente:

   ```bash
   cp .env.example .env
   ```

   Ajuste no mínimo `DATABASE_URL`. Para análise com OpenAI, configure também
   `OPENAI_API_KEY`. Para backfill manual de usuários do Ghost, configure
   `GHOST_API_KEY` e `GHOST_ADMIN_URL`.

5. Rode as migrações:

   ```bash
   alembic upgrade head
   ```

## Execução dos programas principais

### Coleta de pronunciamentos do Senado

```bash
python -m mamute_scrappers.senado_crawler.speechs_transcipts --help
```

### Coleta de discursos/transcrições da Câmara

Execute a partir da raiz do projeto (`mamute-politico`):

```bash
python -m mamute_scrappers.camara_crawler.speeches_transcripts --help
```

Exemplos:

```bash
# todos os deputados (persistindo no banco)
python -m mamute_scrappers.camara_crawler.speeches_transcripts

# deputado específico a partir de uma data
python -m mamute_scrappers.camara_crawler.speeches_transcripts --deputado-id 1234 --data-inicio 2026-01-01

# teste sem persistir no banco
python -m mamute_scrappers.camara_crawler.speeches_transcripts --dry-run
```

### Coleta de emendas parlamentares (Portal da Transparência)

Emenda **orçamentária** (destinação de verba), não emenda a proposição.

Exige `PORTAL_TRANSPARENCIA_API_KEY` no `.env` — cadastro gratuito em
portaldatransparencia.gov.br/api-de-dados/cadastrar-email. A fonte limita 30
requisições por minuto por chave e serve 15 registros por página; um ano tem
~400 páginas, ou seja ~15 min de coleta.

A fonte não devolve identificador de parlamentar, só o nome do autor em texto
livre, então cada emenda passa por um casamento por nome. O que não casa é
gravado mesmo assim, com `parliamentarian_id` nulo, e aparece em
`/admin/emendas-nao-casadas`.

```bash
# ano corrente, persistindo no banco
python -m mamute_scrappers.portal_crawler.emendas

# ano específico
python -m mamute_scrappers.portal_crawler.emendas --ano 2025

# diagnóstico: não persiste, reporta a taxa de casamento por nome
python -m mamute_scrappers.portal_crawler.emendas --ano 2026 --dry-run --limit 500

# backfill 2022 -> ano corrente (auto-encerra quando a fila zera)
python -m mamute_scrappers.scripts.backfill_emendas --chunks-per-run 2
python -m mamute_scrappers.scripts.backfill_emendas --status
```

### Coleta da cota parlamentar (CEAP Câmara + CEAPS Senado) — CS-57

Gastos de gabinete do parlamentar (combustível, aluguel de escritório,
divulgação, passagens), por mês, tipo de despesa e fornecedor, com link para o
documento fiscal.

Sem chave de API e sem rate limit relevante: a Câmara publica um **arquivo
anual em massa** (`camara.leg.br/cotas/Ano-{ano}.csv.zip`, atualizado
diariamente — a API REST de despesas está degradada e não é usada) e o Senado
uma **API JSON** (`adm.senado.gov.br/adm-dadosabertos`). As duas fontes
publicam o id do parlamentar, então o vínculo é join direto por
`parliamentarian_code`, sem casamento por nome.

```bash
# ano corrente, persistindo no banco
python -m mamute_scrappers.camara_crawler.expenses
python -m mamute_scrappers.senado_crawler.expenses

# ano específico
python -m mamute_scrappers.camara_crawler.expenses --ano 2024

# diagnóstico sem banco
python -m mamute_scrappers.camara_crawler.expenses --ano 2025 --dry-run --limit 50
python -m mamute_scrappers.senado_crawler.expenses --ano 2025 --dry-run --limit 50

# backfill 2022 -> ano corrente (10 chunks ano x casa; auto-encerra)
python -m mamute_scrappers.scripts.backfill_cota --chunks-per-run 2
python -m mamute_scrappers.scripts.backfill_cota --status
```

### Perfil demográfico dos candidatos (TSE) — CS-63

Cor/raça, gênero, escolaridade, ocupação, estado civil, nascimento e
federação de **todos os candidatos** das eleições gerais, como colunas na
tabela `candidacy`. Duas fontes que convergem na mesma chave natural
`(election_year, state, tse_candidate_id)`:

- **2026 (vivo):** o crawler da DivulgaCandContas (`tse_crawler.candidacy`)
  já preenche as colunas a partir do detalhe. Para promover o que foi
  coletado ANTES das colunas existirem (JSONB `details`), rodar uma vez:

  ```bash
  python -m mamute_scrappers.tse_crawler.promote_profile            # ~20k linhas, local
  python -m mamute_scrappers.tse_crawler.promote_profile --dry-run  # só conta
  ```

- **Histórico 1994→2022 (carga única):** CSVs de dados abertos do TSE
  (`consulta_cand_{ano}.zip`, ~3-5 MB/ano), ~190 mil candidaturas no total:

  ```bash
  python -m mamute_scrappers.tse_crawler.consulta_cand                    # todas as gerais
  python -m mamute_scrappers.tse_crawler.consulta_cand --anos 1998,2002   # anos específicos
  python -m mamute_scrappers.tse_crawler.consulta_cand --anos 2022 --dry-run --limit 50
  ```

Gotchas da fonte (medidos em 2026-08-20): o CDN do TSE (Akamai) exige o
conjunto **completo** de headers de navegador (User-Agent sozinho = 403);
em 2002/2006 o `SQ_CANDIDATO` é sequencial **por UF** e as linhas de 1º
turno vêm duplicadas em dobro; cor/raça só existe desde **2014** e federação
desde **2022** (NULL antes disso é lacuna da fonte). O CSV nunca sobrescreve
campo preenchido pela API — só completa NULL.

### Temas oficiais das proposições — CS-92

`proposition.themes` guarda as áreas temáticas oficiais da Casa (lista, na
ordem da fonte). `NULL` = ainda não coletado; `[]` = coletado, a Casa não
classificou.

- **Câmara:** `camara_crawler.proposition` consulta
  `/proposicoes/{id}/temas` junto com o detalhe de toda proposição nova.
  O tema sai junto com a proposição (60 de 60 PLs de jul-set/2026 já tinham);
  REQ, EMC, PRL, PAR e SBT nunca recebem tema — `[]` é o esperado para eles.
- **Senado:** `senado_crawler.proposition` extrai
  `processo.classificacoes[].descricao` do processo que já baixa (sem
  requisição extra).
- **Histórico:** o backfill preenche o que está `NULL`. Senado sai do JSON
  guardado, sem rede; Câmara é 1 chamada por proposição (~0,3 s), das mais
  recentes para as mais antigas. Roda no cron de hora em hora e num burst no
  boot do container; auto-encerra quando a fila zera.

```bash
python -m mamute_scrappers.scripts.backfill_proposition_themes --status
python -m mamute_scrappers.scripts.backfill_proposition_themes --chunks-per-run 3000
python -m mamute_scrappers.scripts.backfill_proposition_themes --retry-failed  # devolve falhas à fila
```

Cobertura em produção (tipos que a Câmara classifica):

```sql
SELECT proposition_acronym,
       count(*)                                           AS total,
       count(*) FILTER (WHERE jsonb_array_length(themes) > 0) AS com_tema,
       count(*) FILTER (WHERE themes = '[]'::jsonb)       AS sem_classificacao,
       count(*) FILTER (WHERE themes IS NULL)             AS nao_coletado
FROM proposition
WHERE link ILIKE '%camara.leg.br%' AND presentation_date >= '2023-02-01'
GROUP BY 1 ORDER BY 2 DESC;
```

### Reprocessar análise de texto de pronunciamentos

```bash
python -m mamute_scrappers.scripts.rebuild_speech_text_analysis --help
```

### Reconciliar usuários/projetos via Ghost

```bash
python -m mamute_scrappers.scripts.create_users
```

Esse comando é um backfill manual. A sincronização contínua Ghost -> projetos é
recebida pela API em `POST /api/webhooks/ghost/members`; a própria API também
faz uma reconciliação Ghost -> tiers/projetos no startup quando
`GHOST_API_KEY`/`GHOST_ADMIN_URL` estão configurados.

No container dos scrappers, a reconciliação Ghost -> tiers/projetos roda também
no startup por padrão, antes do cron ficar em foreground:

```bash
python -m mamute_scrappers.scripts.ghost_tiers_sync
python -m mamute_scrappers.scripts.create_users
```

Ela é idempotente e não bloqueia o container se o Ghost ou o banco estiverem
temporariamente indisponíveis. Para desligar esse comportamento em um ambiente,
configure `MAMUTE_GHOST_RECONCILE_ON_STARTUP=false`.

### Relatórios por e-mail (notificação)

Execute na **raiz** do repositório (`mamute-politico`). Configure `DATABASE_URL` e
as variáveis `SMTP_*` em `mamute_scrappers/.env` (veja `.env.example`). Em produção,
cada projeto só recebe o relatório se o tier tiver a periodicidade em
`tiers.detalhes.periodicidade_email` (ex.: `["week"]` ou `["month"]`).

Documentação completa: [`scripts/notificacao/README.md`](scripts/notificacao/README.md).

```bash
# Listar destinatários elegíveis (sem enviar)
python -m mamute_scrappers.scripts.notificacao --periodicidade week --list-only
python -m mamute_scrappers.scripts.notificacao --periodicidade month --list-only

# Relatório semanal — todos os projetos com "week" no tier
python -m mamute_scrappers.scripts.notificacao --periodicidade week

# Relatório mensal — todos os projetos com "month" no tier
python -m mamute_scrappers.scripts.notificacao --periodicidade month
```

Teste de um projeto (HTML em `mamute_scrappers/scripts/notificacao/output/`):

```bash
python -m mamute_scrappers.scripts.notificacao --periodicidade week --projeto-id 1 --dry-run
```

## Cronjobs recomendados

Exemplo de configuração para atualização contínua de projetos, trâmites e dados auxiliares:

```cron
##########################
# MAMUTE POLITICO
##########################
PROJECT_ROOT=mamute-politico
PYTHON_BIN=mamute-politico/.venv/bin/python
LOG_DIR=mamute-politico/mamute_scrappers/.logs

# Sync Ghost -> projetos é feito via webhook da API.
# Rode mamute_scrappers.scripts.create_users manualmente apenas para reconciliação.

# Novas proposições/projetos (a cada 6h)
0 */6 * * *   cd $PROJECT_ROOT && $PYTHON_BIN -m mamute_scrappers.senado_crawler.proposition >> $LOG_DIR/crawlers/propositions.log 2>&1

# Atualização de trâmites/status (diário às 03h)
0 3 * * *     cd $PROJECT_ROOT && $PYTHON_BIN -m mamute_scrappers.senado_crawler.proposition_status >> $LOG_DIR/crawlers/proposition_status.log 2>&1

# Tipos de proposição (diário às 04h)
0 4 * * *     cd $PROJECT_ROOT && $PYTHON_BIN -m mamute_scrappers.senado_crawler.proposition_type >> $LOG_DIR/crawlers/proposition_type.log 2>&1

# Votações nominais (a cada 3h)
0 */3 * * *   cd $PROJECT_ROOT && $PYTHON_BIN -m mamute_scrappers.senado_crawler.roll_call_votes >> $LOG_DIR/crawlers/roll_call_votes.log 2>&1

# Discursos/taquigrafias (a cada 2h)
0 */2 * * *   cd $PROJECT_ROOT && $PYTHON_BIN -m mamute_scrappers.senado_crawler.speechs_transcipts >> $LOG_DIR/crawlers/speechs_transcripts.log 2>&1

# Parlamentares da Câmara (diário às 05h30)
30 5 * * *    cd $PROJECT_ROOT && $PYTHON_BIN -m mamute_scrappers.camara_crawler.parliamentarian >> $LOG_DIR/crawlers/camara_parliamentarians.log 2>&1

# Relatórios por e-mail — semanal (segundas 08:00)
0 8 * * 1     cd $PROJECT_ROOT && $PYTHON_BIN -m mamute_scrappers.scripts.notificacao --periodicidade week >> $LOG_DIR/notificacao/week.log 2>&1

# Relatórios por e-mail — mensal (dia 1, 08:00)
0 8 1 * *     cd $PROJECT_ROOT && $PYTHON_BIN -m mamute_scrappers.scripts.notificacao --periodicidade month >> $LOG_DIR/notificacao/month.log 2>&1
```

## Observações

- Use `--help` nos comandos para ver todos os parâmetros disponíveis.
- É recomendado executar os scrappers antes de iniciar `api` e `chatbot_backend`.
