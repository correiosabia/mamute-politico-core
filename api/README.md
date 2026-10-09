# API Mamute

Aplicação FastAPI para expor os dados coletados no projeto.

Projeto pai: [README raiz](../README.md)

## Pré-requisitos

- Python 3.11+
- Banco PostgreSQL já populado pelos scrappers

## Inicialização

1. Entre na pasta da API:

   ```bash
   cd api
   ```

2. Crie e ative o ambiente virtual:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. Instale as dependências:

   ```bash
   pip install -r requirements.txt
   ```

4. Configure variáveis de ambiente:

   ```bash
   cp .env.example .env
   ```

   Ajuste principalmente `DATABASE_URL`, as variáveis do Ghost Members e
   `GHOST_WEBHOOK_SECRET` se for receber webhooks do Ghost.

5. Inicie a API:

   ```bash
   uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
   ```

## Endereços locais

- API (rotas): prefixo `http://127.0.0.1:8000/api` (ex.: `/api/parliamentarians`, `/api/analysis/...`)
- Docs Swagger: `http://127.0.0.1:8000/api/docs`

## Observações

- Rotas protegidas exigem `Authorization: Bearer <token>` com JWT emitido pelo Ghost Members.
- O endpoint `POST /api/webhooks/ghost/members` recebe eventos `member.added`,
  `member.edited` e `member.deleted` do Ghost. Configure o mesmo segredo no
  Ghost Admin e em `GHOST_WEBHOOK_SECRET`. Passo a passo:
  [`../environments/ghost.md`](../environments/ghost.md).
- Quando `GHOST_API_KEY`/`GHOST_ADMIN_URL` estão disponíveis, o webhook consulta
  o member completo no Ghost Admin API antes de sincronizar o projeto local. Isso
  evita cair em `free` quando o payload do evento não traz `tiers/subscriptions`.
- A API também roda reconciliação Ghost -> tiers/projetos no startup por padrão.
  Desative com `MAMUTE_GHOST_RECONCILE_ON_STARTUP=false` se necessário.
- Em caso de rotação de chaves JWKS, reinicie a aplicação para recarregar a chave pública.
- O deployment define `MAMUTE_PARLIAMENTARIAN_CATALOG_SCOPE` para controlar a
  visibilidade do catálogo: `current_only` (padrão seguro),
  `current_and_licensed` ou `all_ingested`. A API aplica essa política a toda
  consulta e a expõe, para clientes autenticados, em
  `GET /api/parliamentarians/catalog-config`.

## Marcações pessoais do assinante (CS-18)

Três camadas sobre o vínculo de monitoramento, todas escopadas pelo e-mail do
JWT e nenhuma delas consumindo `qtd_termos`:

- **Ordem pessoal** — `PATCH /api/projects/me/favorites/order` reescreve as
  posições numa transação. Exige a lista completa de monitorados; lista
  desatualizada devolve 422 para o cliente recarregar em vez de aplicar pela
  metade.
- **Tags livres** — CRUD em `/api/projects/me/tags` e
  `PUT /api/projects/me/parliamentarians/{id}/tags`.
  `GET /api/projects/me/parliamentarian-tags` devolve todas as aplicações do
  projeto numa chamada só.
- **Mamutômetro** — escala de 1 a N cujo significado é definido por cada
  assinante e **nunca informado ao sistema**. `GET /api/projects/me/mamutometro`,
  `PUT`/`DELETE` em `/api/projects/me/parliamentarians/{id}/mamutometro`, e
  `DELETE /api/projects/me/mamutometro` para apagar tudo.

`GET /api/settings/marcacoes` devolve a configuração **já resolvida** para quem
chamou: se o plano tem mamutômetro, o tamanho da régua, o teto e o uso atual.
A interface não repete essas regras.

Onde cada configuração vive — cada uma no mecanismo que já existia para ela:

| Decisão | Onde | Padrão |
|---|---|---|
| Quais planos têm mamutômetro | `feature_flag_tier` da flag `mamutometro` | só planos pagos |
| Quantos parlamentares marcar | `qtd_mamutometro` em `tiers.detalhes` (ver README da raiz) | sem teto |
| Tamanho da régua, escopos e aviso | `marcacoes_config`, via `PUT /api/admin/settings/marcacoes` | 3 mamutes; mamutômetro só em monitorados; tags em todos |

**Configuração nunca destrói dado.** Reduzir a régua, apertar o escopo ou
remover a feature de um plano deixa as marcações onde estão: elas somem da tela
e voltam se a configuração voltar.

O nível **não tem significado no sistema**, e isso é o desenho — ver
[`docs/adr/0002-privacidade-do-mamutometro.md`](../docs/adr/0002-privacidade-do-mamutometro.md).
Marcação de mamutômetro não aparece em painel admin, relatório por e-mail,
resposta do chatbot nem em qualquer agregado por político.

## Temas oficiais das proposições (CS-92)

`PropositionOut.themes` (em `/api/propositions/`, `/api/propositions/{id}` e
nas proposições do `dashboard-activity`) é a lista de áreas temáticas oficiais
da Casa. Três estados, e a interface mostra cada um de um jeito:

| `themes` | Significa |
|---|---|
| `["Saúde", "Educação"]` | temas oficiais, na ordem da fonte (não há tema principal) |
| `[]` | coletado, a Casa não classificou (REQ, EMC, PRL...) |
| `null` | ainda não coletado — backfill em andamento ou migration pendente |

A coluna é `deferred` no model: nenhuma consulta a lê sem pedir
(`proposition_load_options`), e quem pede só pede depois de ver que a coluna
existe. Assim, na janela do deploy antes da migration `cs92a1b2c3d4`, as rotas
de proposição seguem respondendo, com `themes: null`.

## Perfil dos eleitos (CS-107)

`GET /api/admin/elected-profile` (admin; 404 para os outros) devolve, para o
Senado e a Câmara, o gênero e a cor/raça dos eleitos de 2026, no Brasil e por
UF. Regras em `services/elected_profile.py`:

| Regra | Por quê |
|---|---|
| Eleito = `eleito` **e** situação começando por "Eleito", no 1º turno | o TSE também marca `e = "s"` quem foi ao 2º turno |
| UF só entra com `tse_result_file.totalizacao_final` | resultado parcial nunca aparece |
| `brasil` é `null` até as 27 UFs encerrarem | um total do Brasil com UF faltando seria parcial |
| `valor: null` = candidatura sem o dado na base | não confundir com "NÃO INFORMADO", que é o candidato que não declarou |
| `percentual: null` = UF sem eleito encontrado na base | nunca mostrar 0% sem base para dividir |

`eleitos_no_tse` vem de `tse_result_file.eleitos_no_arquivo`, que a coleta
conta no arquivo inteiro (casado ou não com a base). Se for maior que
`eleitos`, a tela avisa que o percentual não cobre todos os eleitos. `null` =
arquivo coletado antes da migration `cs107a1b2c3d4`; a coleta baixa esses de
novo uma vez, e na janela do deploy a rota responde sem a coluna.

Conferência manual de uma UF em produção (ex.: Câmara/BA):

```sql
SELECT c.gender, c.race, count(*)
FROM candidacy_result cr
JOIN candidacy c ON c.id = cr.candidacy_id
WHERE c.election_year = 2026 AND c.office_code = 6 AND c.state = 'BA'
  AND cr.turno = 1 AND cr.totalizacao_final AND cr.eleito
  AND lower(cr.situacao) LIKE 'eleito%'
GROUP BY 1, 2;
```

e comparar com os eleitos (`e = "s"` e `st` "Eleito...") do arquivo
`<base>/ele2026/6259/dados/ba/ba-c0006-e006259-u.json`.

## Coleções curadas (CS-132)

Página editada pelo admin que reúne pessoas em torno de um tema, com blocos de
conteúdo intercalados. Regras em `services/collection.py`.

| Rota | Quem | O quê |
|---|---|---|
| `GET /api/collections` | qualquer um, sem login | coleções publicadas |
| `GET /api/collections/{slug}` | qualquer um, sem login | coleção publicada completa (404 se rascunho) |
| `GET /api/admin/collections[/{id}]` | admin | todas, rascunhos incluídos |
| `POST /api/admin/collections`, `PUT /api/admin/collections/{id}` | admin | cria e edita título, slug, situação, rótulos dos níveis |
| `PUT /api/admin/collections/{id}/members` | admin | grava a lista completa de pessoas (o que não veio, sai) |
| `PUT /api/admin/collections/{id}/blocks` | admin | grava a lista completa de blocos, na ordem recebida |
| `DELETE /api/admin/collections/{id}` | admin | apaga a coleção com pessoas e blocos |

Cada pessoa volta com `parliamentarian`, `candidacy` (com o resultado do último
turno), `expenses` (cota por ano dos últimos 4 anos, com `aircraft` = fretamento
de aeronaves) e `assets` (bens declarados ao TSE por eleição). Esses vínculos
são resolvidos a cada leitura:

| Regra | Por quê |
|---|---|
| `parliamentarian_id` informado tem precedência | senador não tem CPF na base |
| Sem ele, parlamentar pelo CPF ou pela candidatura mais recente do CPF | candidato eleito passa a apontar para o perfil quando a legislatura nova entra na base, sem regravar nada |
| `parliamentarian: null` e `candidacy: null` = pessoa fora da base | a tela mostra só nome, `role_label` e contexto |
| Bloco com `ref: null` = registro de origem sumiu da base | o bloco continua com o texto do admin |

Toda escrita entra no `admin_audit_log`.

## Discursos e temas por janela (CS-124)

O card de Estatísticas e a nuvem de temas usam as mesmas duas janelas: últimos
3 meses ("o que ele andou fazendo agora") e legislatura vigente ("que
parlamentar ele é"). A Pesquisa IA consulta o histórico inteiro, por isso cita
discursos fora das duas.

- `GET /api/projects/me/parliamentarians/{id}/dashboard-stats` devolve
  `speeches_count` (3 meses) e `speeches_legislature`.
- `GET /api/analysis/parliamentarian/{code}/terms?window=legislature|last_3_months`
  soma no banco a palavra-chave principal de cada discurso da janela. A
  listagem paginada (`/analysis/parliamentarian/{code}`) para em 100 discursos
  e cortava a nuvem de quem discursa muito.

Os termos de `/terms` voltam crus, sem normalizar caixa e sem stopwords: quem
consome aplica as listas de `word_cloud_terms`, as mesmas da tela de
configurações. `speeches_count` e `speeches_analyzed` vêm separados para a tela
distinguir "não discursou" de "discursou e ainda não foi analisado".

## Relatório por e-mail: configurações e link curto (CS-116/CS-134)

- `GET`/`PUT /api/admin/settings/email` (admin): peças editáveis do e-mail
  (`banner_image_url`, `banner_link_url`, `footer_image_url`,
  `footer_link_url`, `instagram_url`, `subscribe_url`, `share_text`). O PUT
  muda só os campos enviados; links precisam ser `https://` ou uma imagem
  enviada aqui. Auditado em `admin_audit_log`.
- `POST /api/admin/settings/email/images` (admin): `{content_type, data_base64}`,
  PNG/JPG/GIF/WebP até 1 MB. Grava no banco (`public_image`) e devolve
  `/api/public-images/{sha256}`, servida sem login e com cache imutável.
- `GET /api/s/{código}` (público): tags de prévia (`og:*`, `twitter:*`) do
  destaque gravado em `share_link` e redirecionamento para `MAMUTE_SITE_URL`.
  Código inexistente vai direto para o site.
- `GET /api/s/{código}.png` (público): imagem da prévia. Vem do cache
  (`share_card_cache`) ou do serviço em `OG_RENDER_URL` (POST `/render`,
  timeout de 10 s). Falha = redireciona para `MAMUTE_SHARE_FALLBACK_IMAGE`
  (sem ela, 404) e não grava cache.

Todas as tabelas são da migration `cs116a1b2c3d4`; antes dela, leitura devolve
vazio em vez de erro.

## Presença no card de Estatísticas (CS-79)

`GET /api/projects/me/parliamentarians/{id}/dashboard-stats` devolve
`attendance_last_3_months_percent` e `attendance_legislature_percent`. A
origem do número muda com a casa, e a tela diz qual está mostrando:

| Casa | Origem | O que o número conta |
|---|---|---|
| Câmara | `plenary_attendance` + `committee_attendance` | registros de presença coletados na fonte |
| Senado | `roll_call_votes` | dias de sessão em que o senador aparece presente em alguma votação nominal |

O Senado não publica presença por sessão de plenário — a API só traz o
comparecimento de cada senador em cada votação nominal —, então o número dele
é inferido. As regras da inferência (agrega por dia, licença conta como
ausência, dia sem registro é dia fora de exercício) e a validação contra a
legislatura 2023-2027 estão em `docs/adr/0001-dashboard-presence-metric-source.md`.

Depende de `roll_call_votes.vote_date`. Sem a coluna — a janela do deploy
antes das migrations — o indicador do Senado volta a "sem dado" em vez de
devolver número errado.

Para conferir a cobertura em produção:

```sql
SELECT count(*) FILTER (WHERE r.vote_date IS NULL)  AS sem_data,
       count(*)                                     AS votos,
       count(DISTINCT r.vote_date)
         FILTER (WHERE r.vote_date >= '2023-02-01') AS dias_de_sessao
FROM roll_call_votes r
JOIN parliamentarian p ON p.id = r.parliamentarian_id
WHERE p.type ILIKE '%Senad%';
```

(`sem_data` conta todos os votos do Senado ainda sem data; com o filtro de data
no `WHERE`, como estava antes, ele dava sempre zero.)

`dias_de_sessao` deve chegar perto de 121 para a legislatura inteira (valor
apurado em 10/09/2026). Muito abaixo disso significa que
`backfill-votes-speeches` ou `backfill-vote-dates` ainda não drenaram a fila.
