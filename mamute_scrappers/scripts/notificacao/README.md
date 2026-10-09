# Notificação por e-mail

Módulo standalone que envia relatórios HTML aos projetos cadastrados em `projetos`, com base nos parlamentares favoritos em `projetos_parliamentarian`.

**Caminho:** `mamute_scrappers/scripts/notificacao/`  
**Comando:** `python -m mamute_scrappers.scripts.notificacao` (execute na **raiz** do repositório `mamute-politico`)

---

## O que cada e-mail contém

| Bloco | Conteúdo |
|-------|----------|
| Cabeçalho | Logo Mamute, saudação (primeiro nome do projeto, antes do `_`) |
| Parlamentares monitorados | Chips só dos parlamentares que aparecem nos **destaques** do período (não lista todos os favoritos) |
| Resumo do período | Cards: proposições, votações, discursos + intervalo de datas |
| Destaques recentes | Atividades agrupadas por parlamentar; **ementa** da proposição (`summary`, cortada na frase); discursos com o **resumo curto** (`speech_short_summary`) e link da **tramitação** vinculada ou do discurso; **emendas** que entraram na base no período |

### Blocos que dependem de flag ou configuração (CS-116/133/134)

O registro das flags mora no app (`ui/src/lib/featureFlags.ts`); o envio lê o
estado com a mesma regra da tela (`flags.py`): `admins` = só e-mails de
`MAMUTE_ADMIN_EMAILS`; `all` = admins + contas cujo plano tem a flag em modo
`liberado`. Estados lidos uma vez por envio. Tudo desligado = e-mail de sempre.

| Flag | O que muda |
|------|------------|
| `email_destaques_gerais` | Conta sem parlamentar selecionado ou sem atividade deixa de ser pulada: recebe os **destaques gerais** (`geral.py`: últimas votações nominais, uma por proposição, com a casa e o placar; até 8 temas dos discursos, com os filtros da nuvem). Sem votação na quinzena, mostra as últimas registradas e diz isso. Status `sent_general` / `sent_no_activity` |
| `email_balanco` | Balanço de **todos** os parlamentares selecionados, inclusive quem não teve atividade ("Sem atividade registrada nesta quinzena") |
| `email_convite_assinatura` | Botão "Assine o Mamute Completo" (link vem de `subscribe_url`) |
| `email_design_novo` | Template novo (`render_v2.py`): fundo amarelo, cards brancos, linha **Compartilhar** em cada destaque |

Peças editáveis no Admin (tabela `email_settings`, chave vazia = bloco some):
banner do topo e rodapé de patrocínio (imagem + link), Instagram, link de
assinatura e texto padrão do compartilhamento.

**Compartilhar:** o envio grava em `share_link` um código por destaque
(reaproveitado entre contas e envios) e o e-mail aponta para
`{MAMUTE_APP_URL}/api/s/{código}`. A API devolve as tags de prévia e manda a
pessoa para o site; a imagem vem do serviço configurado em `OG_RENDER_URL`.

**Resumo curto dos discursos:** job `speech_short_summary` (cron 09:00 UTC).
Sumário oficial de até 280 caracteres vai como está; maior, o modelo condensa o
próprio sumário; sem sumário, resume o começo da transcrição. Falha do modelo
deixa o discurso sem resumo, com o sumário cortado de antes.

---

## Variáveis de ambiente

### Onde colocar

O script carrega o primeiro arquivo existente, nesta ordem:

1. `mamute_scrappers/scripts/notificacao/.env` (opcional, só deste módulo)
2. **`mamute_scrappers/.env`** ← **recomendado** (mesmo do crawler)
3. `.env` na raiz do repositório
4. `scripts/.env` (legado)

**Recomendação:** use **`mamute_scrappers/.env`**. Copie o exemplo:

```bash
cp mamute_scrappers/.env.example mamute_scrappers/.env
```

> Nunca commite `.env` com senhas. Os arquivos já estão no `.gitignore`.

### Variáveis obrigatórias

| Variável | Descrição | Exemplo |
|----------|-----------|---------|
| `DATABASE_URL` | PostgreSQL (mesma base da API/crawlers) | `postgresql://user:pass@localhost:5432/mamute` |
| `SMTP_USER` | Usuário SMTP (ex.: SES) | `AKIA...` |
| `SMTP_PASSWD` | Senha SMTP | `***` |
| `SMTP_SENDER` | E-mail remetente autorizado | `relatorios@mail.seudominio.com` |
| `SMTP_SERVER` | Host SMTP | `email-smtp.us-east-1.amazonaws.com` |

### Variáveis opcionais

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `SMTP_PORT` | `587` | Porta SMTP (TLS) |
| `SMTP_FROM_NAME` | `Mamute Político` | Nome exibido no remetente |
| `MAMUTE_APP_URL` | `https://mamutepolitico.com.br` | Link do painel no texto |
| `MAMUTE_EMAIL_LOGO_URL` | logo Mamute no app | URL da imagem do cabeçalho |
| `MAMUTE_EMAIL_BANNER_URL` | igual ao logo | Banner (não usado no template atual) |
| `MAMUTE_PRIVACY_URL` | `/privacidade` | Link política de privacidade |
| `MAMUTE_EMAIL_MANAGE_URL` | `/app` | Link “gerenciar monitoramento” |
| `SQLALCHEMY_ECHO` | `0` | `1` para log SQL (debug) |

### Exemplo completo de `mamute_scrappers/.env`

```env
# Banco
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mamute
APPLICATION_NAME=MAMUTE_POLITICO_NOTIFICACAO

# SMTP (Amazon SES ou compatível)
SMTP_USER=seu_usuario_smtp
SMTP_PASSWD=sua_senha_smtp
SMTP_SENDER=relatorios@mail.seudominio.com
SMTP_SERVER=email-smtp.us-east-1.amazonaws.com
SMTP_PORT=587
SMTP_FROM_NAME=Mamute Político

# Branding (opcional)
MAMUTE_EMAIL_LOGO_URL=https://mamutepolitico.com.br/app/assets/logo-mamute-Cn9vnXen.png
MAMUTE_APP_URL=https://mamutepolitico.com.br
MAMUTE_EMAIL_MANAGE_URL=https://mamutepolitico.com.br/app
MAMUTE_PRIVACY_URL=https://mamutepolitico.com.br/privacidade
```

---

## Instalação

Na raiz do repositório, com venv ativo:

```bash
pip install -r mamute_scrappers/requirements.txt
pip install -r mamute_scrappers/scripts/notificacao/requirements.txt
```

- **Python 3.11+** é o recomendado no projeto.
- Em **Python 3.8–3.10**, o pacote `backports.zoneinfo` (já listado em `requirements.txt`) é necessário.

---

## Periodicidades

| `--periodicidade` | Uso | Período do resumo | Destinatários |
|-------------------|-----|-------------------|---------------|
| `day` | Relatório **diário** | calendário de hoje (SP); proposições também entram se foram **indexadas hoje** no Mamute | Projetos cujo tier inclui `"day"` em `periodicidade_email` |
| `week` | Relatório **semanal** | 7 dias | Tier com `"week"` |
| `month` | Relatório **mensal** | 30 dias | Tier com `"month"` |
| `total` | **Teste / preview** | cards com **totais históricos** dos favoritos; destaques **sem filtro de data** (até 10 por parlamentar) | **Todos** os projetos ativos |

### Configurar quem recebe (produção)

No JSON `tiers.detalhes`, campo `periodicidade_email`:

```json
{
  "periodicidade_email": ["day", "week", "month"],
  "qtd_email": 1
}
```

Exemplo: tier só semanal → `["week"]`. Projeto sem tier ou sem o valor na lista **não** entra no envio (use `--include-without-tier` ou `--projeto-id` para exceções).

---

## Fluxo recomendado

### 1. Validar ambiente e destinatários

```bash
cd /caminho/para/mamute-politico

# Quem receberia o semanal?
python -m mamute_scrappers.scripts.notificacao --periodicidade week --list-only

# Incluir projetos sem tier (debug)
python -m mamute_scrappers.scripts.notificacao --periodicidade week --list-only --include-without-tier
```

Saída TSV: `id    email    nome`

### 2. Gerar HTML sem enviar (`--dry-run`)

```bash
python -m mamute_scrappers.scripts.notificacao --periodicidade week --projeto-id 1 --dry-run
```

Arquivo gerado:

`mamute_scrappers/scripts/notificacao/output/projeto_1_week.html`

Abra no navegador e revise layout, links e dados.

### 3. Envio real

Somente quando SMTP e conteúdo estiverem corretos.

---

## Exemplos de comando

Todos os exemplos assumem `cd` na raiz do repositório e venv ativo.

### Diário (`day`)

```bash
# Listar destinatários do diário
python -m mamute_scrappers.scripts.notificacao --periodicidade day --list-only

# Simular um projeto
python -m mamute_scrappers.scripts.notificacao --periodicidade day --projeto-id 1 --dry-run

# Enviar para todos elegíveis (tier com "day")
python -m mamute_scrappers.scripts.notificacao --periodicidade day

# Enviar mesmo sem atividade no período
python -m mamute_scrappers.scripts.notificacao --periodicidade day --send-if-empty
```

### Semanal (`week`)

```bash
python -m mamute_scrappers.scripts.notificacao --periodicidade week --list-only

python -m mamute_scrappers.scripts.notificacao --periodicidade week --dry-run

python -m mamute_scrappers.scripts.notificacao --periodicidade week --projeto-id 42 --dry-run

python -m mamute_scrappers.scripts.notificacao --periodicidade week
```

### Mensal (`month`)

```bash
python -m mamute_scrappers.scripts.notificacao --periodicidade month --list-only

python -m mamute_scrappers.scripts.notificacao --periodicidade month --projeto-id 1 --dry-run

python -m mamute_scrappers.scripts.notificacao --periodicidade month
```

### Teste / preview (`total`)

Modo para validar template e links **sem** depender do tier. Envia amostra (até **10** destaques por parlamentar, distribuídos entre favoritos). Exibe aviso amarelo “Envio de teste” no e-mail.

```bash
# HTML de teste para um projeto
python -m mamute_scrappers.scripts.notificacao --periodicidade total --projeto-id 1 --dry-run

# Listar todos os projetos que receberiam no modo total
python -m mamute_scrappers.scripts.notificacao --periodicidade total --list-only

# CUIDADO: envia e-mail para TODOS os projetos ativos
python -m mamute_scrappers.scripts.notificacao --periodicidade total --projeto-id 1
```

> **Não agende `total` no cron.** Use apenas `day`, `week` e `month` em produção.

### Opções úteis

| Flag | Descrição |
|------|-----------|
| `--dry-run` | Monta HTML em `output/`, **não** envia e-mail |
| `--list-only` | Lista `id`, `email`, `nome` e encerra |
| `--projeto-id N` | Apenas um projeto (ignora filtro de tier) |
| `--include-without-tier` | Inclui projetos sem tier na listagem/envio |
| `--send-if-empty` | Envia mesmo sem atividade no período |
| `--highlight-limit N` | Máximo de destaques (padrão: 9 ou 10 no `total`) |
| `--save-html` | Grava HTML mesmo em envio real |
| `--max-workers N` | Paralelismo (padrão: CPUs) |
| `-v` / `--verbose` | Log DEBUG |

### Atalho legado

```bash
python -m scripts.notificacao --periodicidade week --dry-run
```

Redireciona para o mesmo módulo em `mamute_scrappers`.

---

## Cron em produção

Ajuste o caminho do projeto e do Python/venv:

```cron
# Diário — 08:00 todos os dias
0 8 * * *   cd /app/mamute-politico && .venv/bin/python -m mamute_scrappers.scripts.notificacao --periodicidade day

# Semanal — segundas 08:00
0 8 * * 1   cd /app/mamute-politico && .venv/bin/python -m mamute_scrappers.scripts.notificacao --periodicidade week

# Mensal — dia 1 de cada mês 08:00
0 8 1 * *   cd /app/mamute-politico && .venv/bin/python -m mamute_scrappers.scripts.notificacao --periodicidade month
```

---

## Estrutura do módulo

```
mamute_scrappers/scripts/notificacao/
├── __main__.py          # CLI
├── config.py            # env, branding, periodicidades
├── repository.py        # consultas ao banco
├── links.py             # URLs legíveis (alinhado à UI/API)
├── report_builder.py    # HTML
├── mailer.py            # SMTP
├── runner.py            # envio em lote
├── templates/report.html
├── requirements.txt
├── output/              # HTML gerados (--dry-run); gitignored
└── README.md
```

---

## Problemas comuns

| Sintoma | Causa provável | O que fazer |
|---------|----------------|-------------|
| `ModuleNotFoundError: zoneinfo` | Python &lt; 3.9 | `pip install backports.zoneinfo` ou use Python 3.11+ |
| `DATABASE_URL não definido` | `.env` ausente ou fora do caminho | Crie `mamute_scrappers/.env` |
| `Variáveis SMTP ausentes` | SMTP não preenchido | Complete `SMTP_*` no `.env` |
| `0 destinatário(s)` | Tier sem `periodicidade_email` | Ajuste o tier ou use `--projeto-id` / `--include-without-tier` |
| Link “Ver detalhes” abre XML | Versão antiga do script | Atualize: links usam `links.py` (páginas Câmara/Senado) |
| `sem parlamentares favoritos` | `projetos_parliamentarian` vazio | Cadastre favoritos no app |
| SES `554` / `User name is missing` com `Político` | Cabeçalho `From` mal codificado | Atualize o `mailer.py`; ou use `SMTP_FROM_NAME=Mamute Politico` (sem acento) |

---

## Dependências

- `mamute_scrappers/requirements.txt` — SQLAlchemy, psycopg2, python-dotenv, etc.
- `mamute_scrappers/scripts/notificacao/requirements.txt` — complemento mínimo do módulo

Referência de exemplo: `mamute_scrappers/.env.example`
