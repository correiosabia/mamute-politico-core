# CS-106: avisar quem acompanha candidatos sobre o resultado da eleição

Data: 2026-10-01 · Task: [CS-106](https://lvdev.atlassian.net/browse/CS-106) · Status: design aprovado pelo Luiz em 01/10/2026

## Objetivo

Quem marcou candidaturas para acompanhar (58 marcações de 19 pessoas em prod em
01/10) recebe, depois que o TSE encerra a totalização, um e-mail único com o
desfecho de cada candidato e vê o mesmo resumo num modal, uma vez só, no próximo
acesso. No 2º turno, novo envio só para quem acompanha candidato que disputou o
2º turno. A situação gravada aqui é a base das tasks irmãs (perfil dos eleitos
no admin e reseleção do 2º turno).

## Decisões do Luiz (01/10)

1. **Momento do envio:** por pessoa, assim que **todos** os candidatos que ela
   acompanha estiverem em arquivo com totalização encerrada. Cron automático.
2. **Situação exibida:** o texto cru do TSE (campo `st`): "Eleito", "Eleito por
   média", "2º turno", "Suplente", "Não eleito".
3. **Modal visto:** guardado no banco (vale entre dispositivos), não em
   localStorage.

## Levantamento da fonte (fechado em 01/10/2026)

- Configuração de produção já publicada em 30/09:
  `https://resultados.tse.jus.br/oficial/comum/config/ele-c.json`. Pleito
  `ele2026` de 04/10/2026:
  - 6257 Federal 1º turno (cargo 1 Presidente), `cdt2` = 6258
  - 6259 Estadual 1º turno (3 Governador, 5 Senador, 6 Dep. Federal,
    7 Dep. Estadual, 8 Dep. Distrital), `cdt2` = 6260
  - 6261 Municipal (25 Conselheiro Distrital): fora do escopo, não temos essas
    candidaturas.
- Arquivo de resultado por cargo e abrangência:
  `<base>/<ciclo>/<eleicao>/dados/<uf>/<uf>-c<cargo:04d>-e<eleicao:06d>-u.json`
  (Presidente usa `br`). Exemplo: `.../oficial/ele2026/6259/dados/sp/sp-c0006-e006259-u.json`.
  Os arquivos oficiais **já respondem** (zerados, `tf = "n"`).
- Campos usados: topo `tf` ("s" = totalização encerrada), `t` (turno), `dt`/`ht`
  (data/hora da totalização). Candidatos em `carg[0].agr[].par[].cand[]`:
  `sqcand`, `st`, `e` ("s"/"n"), `vap` (votos), `pvap` ("8,39"), `dvt`
  (destinação do voto: "Válido", "Anulado sub judice"...).
- `sqcand` = `candidacy.tse_candidate_id` (conferido: 280002551544 é FLAVIO
  BOLSONARO nos dois lados).
- Simulado: `https://resultados-sim.tse.jus.br/simulado/simulado2026`, mesmo
  formato, mas com `sqcand` fictícios (não casam com a nossa base). Serve para
  o teste de ponta a ponta com massa de teste que mapeia sqcands do simulado.
- Acesso: GET simples com User-Agent responde 200 (sem o bloqueio Akamai do
  cdn.tse.jus.br). Usar o mesmo conjunto de headers de navegador do
  `consulta_cand.py` por segurança.

## Arquitetura

Tudo no core, exceto o modal. Duas PRs: core (coleta, tabelas, envio,
endpoints) e app (modal + registro da flag). Merge do core primeiro.

### 1. Coleta: `mamute_scrappers/tse_crawler/resultados.py`

- Config por env (prefixo `MAMUTE_` já passa pelo allowlist do
  `entrypoint.sh`):
  - `MAMUTE_TSE_RESULTADOS_BASE` (default
    `https://resultados.tse.jus.br/oficial`)
  - `MAMUTE_TSE_RESULTADOS_CICLO` (default `ele2026`)
- Cada rodada lê `<base>/comum/config/ele-c.json`, pega o pleito do ciclo e
  descobre eleições, turno (`t`) e cargos. Nada de código de eleição fixo no
  código; o 2º turno aparece no arquivo e entra sem deploy.
- Antes da data do pleito (`dt` do ele-c), não baixa nada daquela eleição.
- Cargos coletados: 1, 3, 5, 6, 7, 8. UFs: as 27 (cargo 1 só `br`; cargo 8 só
  `df`; cargo 7 nunca `df`). Arquivo 404 = ignorado e logado (ex.: UF sem 2º
  turno).
- Arquivo cujas linhas já estão gravadas com `totalizacao_final = true` não é
  baixado de novo.
- Upsert em `candidacy_result` por candidato que casar com `candidacy`
  (`election_year` do ciclo + `tse_candidate_id = sqcand`). Não casou = contador
  no log, sem gravar.
- CLI: `python -m mamute_scrappers.tse_crawler.resultados [--dry-run] [--uf SP] [--cargo 6]`.
  `--dry-run` imprime taxa de casamento e situação sem gravar.

### 2. Tabela `candidacy_result`

| coluna | tipo | nota |
|---|---|---|
| id | serial PK | |
| candidacy_id | FK candidacy ON DELETE CASCADE | |
| turno | smallint | 1 ou 2 |
| codigo_eleicao | int | ex.: 6259 |
| situacao | text NULL | `st` cru; vazio do TSE vira NULL |
| eleito | bool NULL | campo `e` |
| votos | bigint NULL | `vap` |
| percentual | numeric(7,4) NULL | `pvap` com vírgula convertida |
| destinacao_voto | text NULL | `dvt` |
| totalizacao_final | bool not null | `tf == "s"` do arquivo |
| tse_atualizado_em | timestamptz NULL | `dt`+`ht` do arquivo (America/Sao_Paulo) |
| coletado_em | timestamptz not null | |

Unique `(candidacy_id, turno)`. Índice em `(turno, situacao)` para as tasks
irmãs.

### 3. Tabela `election_result_notice`

| coluna | tipo | nota |
|---|---|---|
| id | serial PK | |
| projeto_id | FK projetos ON DELETE CASCADE | |
| ciclo | text | `ele2026` |
| turno | smallint | |
| payload | jsonb | foto do que foi enviado (lista de candidatos) |
| email_status | text | `pending`, `sent`, `error`, `skipped_no_email` |
| tentativas | smallint default 0 | |
| ultimo_erro | text NULL | |
| created_at | timestamptz | |
| sent_at | timestamptz NULL | |
| seen_at | timestamptz NULL | modal fechado |

Unique `(projeto_id, ciclo, turno)`: é a trava contra envio duplicado.

### 4. Envio: `mamute_scrappers/scripts/notificacao/resultado_eleicao.py`

- CLI: `python -m mamute_scrappers.scripts.notificacao.resultado_eleicao [--dry-run] [--turno 1|2] [--projeto-id N]`.
- Lê o estado da flag `resultado_eleicao` na tabela `feature_flag`:
  `off` (ou sem linha) = não envia; `admins` = só projetos cujo e-mail está em
  `MAMUTE_ADMIN_EMAILS`; `all` = todos.
- Candidatos de cada pessoa: `projetos_candidacy` + `candidacy` do ano do
  ciclo, projetos com `deleted_at IS NULL`.
- **Pessoa pronta no turno T** quando, para cada candidato acompanhado (no
  turno 2, só os que tiveram `situacao = '2º turno'` no turno 1), existe linha
  em `candidacy_result` do turno T com `totalizacao_final = true`, **ou** o
  arquivo do cargo/UF daquele candidato está encerrado e ele não consta (sai
  como "Não consta na totalização do TSE"). Para saber se o arquivo está
  encerrado sem o candidato: qualquer linha `candidacy_result` final do mesmo
  turno, cargo e UF.
- Turno 2: só entra quem tem pelo menos um candidato com `situacao = '2º
  turno'` no turno 1.
- Envio idempotente:
  1. `INSERT ... ON CONFLICT DO NOTHING RETURNING id` com `email_status =
     'pending'` e o payload.
  2. Só quem inseriu (ou quem tem `error` com `tentativas < 3`) envia.
  3. Sucesso: `sent`, `sent_at`. Falha SMTP: `error`, `tentativas + 1`,
     `ultimo_erro`. Próxima rodada tenta de novo.
  4. Também grava em `email_send_log` com `periodicidade =
     'eleicao_<ciclo>_t<turno>'` para a aba E-mails do admin.
- E-mail: template novo `templates/resultado_eleicao.html`, mesmo visual do
  `report.html`, mesmo mecanismo `{{KEY}}`. Assunto "Saiu o resultado dos
  candidatos que você acompanha" (2º turno: "Saiu o resultado do 2º turno dos
  candidatos que você acompanha"). Uma linha por candidato: nome de urna,
  cargo/UF, partido, situação crua, votos e %. Link para o app.
- Payload (usado também pelo modal): `{"turno": 1, "itens": [{"candidacy_id",
  "nome", "cargo", "uf", "partido", "situacao", "votos", "percentual"}]}`.

### 5. Cron

Uma linha em `docker/scrappers.cron`, a cada 30 min:
`run-cron-job.sh resultado-eleicao -- sh -c "python -m ...resultados && python -m ...resultado_eleicao"`.
Antes do pleito sai na hora; depois de tudo enviado, custa só os arquivos ainda
abertos. Remoção da linha depois do 2º turno fica registrada como pendência.

### 6. API (core)

- `GET /projects/me/election-result-notice` → aviso mais recente com
  `seen_at IS NULL` e `email_status != 'pending'`: `{id, turno, payload}`; ou
  `204` se não houver.
- `POST /projects/me/election-result-notice/{id}/seen` → grava `seen_at`;
  404 se o aviso não for do projeto do token.
- Modelo SQLAlchemy em `api/db/models/` (o `candidacy_result` também, para as
  tasks irmãs).

### 7. Modal (app)

- Flag `resultado_eleicao` registrada em `ui/src/lib/featureFlags.ts` (nasce
  `off`, sem migration).
- `ResultadoEleicaoModal` montado no `App.tsx` ao lado dos providers, só com
  usuário logado e `useFeatureFlag('resultado_eleicao')`.
- Busca o endpoint; se houver aviso, abre `Dialog` com a lista (mesmo conteúdo
  do e-mail) e link "Ver candidaturas" (`/candidaturas`). Fechar (X, botão ou
  link) chama o POST `seen`.

## Liga/desliga

- O `if` mora em dois lugares: no envio (estado da flag no banco) e no modal
  (`useFeatureFlag`). Quem muda é o admin na tela de Feature Flags, sem deploy.
- Se ninguém fizer nada: a flag nasce `off`, a coleta grava resultados e
  ninguém recebe e-mail nem modal.
- Roteiro de domingo: quando o presidente fechar, `admins`; conferir e-mail e
  modal; passar para `all`.

## Erros e casos de borda

- TSE fora do ar / timeout: retry com backoff; a rodada falha e a próxima (30
  min) tenta de novo. Nada é enviado com dado incompleto, porque pessoa pronta
  exige `totalizacao_final`.
- Projeto sem e-mail: `skipped_no_email`, modal ainda aparece.
- Pessoa que passa a acompanhar um candidato depois do envio: não gera novo
  e-mail (unique por turno). Aceito.
- Candidato com `situacao` vazia mesmo em arquivo final: exibe "Sem situação
  informada pelo TSE".

## Testes

- Unitários (scrappers): parser com fixtures recortadas do simulado (inclui
  "Eleito por média", "Suplente", "Anulado sub judice"); montagem das URLs a
  partir do ele-c; regra de pessoa pronta (turnos 1 e 2, candidato ausente);
  idempotência (roda 2x, envia 1); retry de erro; flag off/admins/all.
- Unitários (api): endpoints GET/POST no padrão SQLite de
  `test_candidacy_favorites.py`.
- Ponta a ponta local: Postgres local + Mailpit, base apontando para o
  simulado, massa de teste que troca `tse_candidate_id` de algumas
  candidaturas locais pelos sqcands do simulado; rodar 2x, conferir 1 e-mail
  no Mailpit e o modal no app.
- Prod antes de 04/10: `--dry-run` da coleta contra os arquivos oficiais para
  medir a taxa de casamento das 58 marcações.

## Fora do escopo

Perfil dos eleitos no admin e reseleção do 2º turno (tasks irmãs, só leem
`candidacy_result`). Conselheiro Distrital.
