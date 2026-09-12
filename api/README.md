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
SELECT count(*) FILTER (WHERE r.vote_date IS NULL) AS sem_data,
       count(*)                                    AS votos,
       count(DISTINCT r.vote_date)                 AS dias_de_sessao
FROM roll_call_votes r
JOIN parliamentarian p ON p.id = r.parliamentarian_id
WHERE p.type ILIKE '%Senad%' AND r.vote_date >= '2023-02-01';
```

`dias_de_sessao` deve chegar perto de 121 para a legislatura inteira (valor
apurado em 10/09/2026). Muito abaixo disso significa que
`backfill-votes-speeches` ou `backfill-vote-dates` ainda não drenaram a fila.
