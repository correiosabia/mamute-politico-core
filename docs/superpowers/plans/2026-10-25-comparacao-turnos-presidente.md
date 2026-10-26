# Comparação 1º x 2º turno de presidente: plano de implementação (CS-160)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** coletar o resultado de presidente por município nos dois turnos, agregar por Brasil, macrorregião, UF, região intermediária do IBGE e capital x interior, estimar a transferência de votos e mostrar tudo, com frases prontas, numa aba nova do admin.

**Architecture:** a coleta (scrappers) grava município x turno e uma matriz de transferência calculada com numpy. A API agrega por SQL, gera as frases por regra fixa e responde numa rota admin. O app ganha uma aba que só lê essa rota.

**Tech Stack:** Python 3.11, SQLAlchemy `text()`, alembic, numpy (já instalado nos scrappers), FastAPI < 0.115, pytest com SQLite em memória; React + TypeScript + TanStack Query + vitest.

**Spec:** `docs/superpowers/specs/2026-10-25-comparacao-turnos-presidente-design.md`

## Global Constraints

- Core público e neutro: nomes genéricos (`resultado_municipio`, `runoff_comparison`); sem adjetivo, sem opinião, sem receita em código, commit ou PR.
- Key `CS-160` no título das duas PRs; core primeiro; merge do core com `--admin` (autorizado quando verde).
- Migration idempotente (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`), revisão `cs160a1b2c3d4`, `down_revision = "cs148a1b2c3d4"`.
- A API roda antes da migration: tabela ausente = resposta vazia com `disponivel: false`, nunca 500.
- Admin gate devolve 404 (`require_ghost_admin`).
- Agregação = soma de votos, nunca média de percentuais. Diferenças em pontos percentuais (pp).
- Frases de abstenção x margem e a estimativa só com os dois turnos 100% totalizados. Antes disso as demais frases levam o prefixo "Parcial: ".
- Estimativa sempre rotulada "Estimativa estatística, não é dado oficial do TSE".
- Textos de tela e frases em pt-BR, sem travessão.

## Review Focus

1. Município sem `cdi` (exterior, `zz`) ou sem linha no IBGE → entra em UF "Exterior"/sem região, nunca some do total do Brasil. Teste na Task 2 e na Task 5.
2. Arquivo municipal 404 durante a coleta → conta como ausente e segue; não grava linha zerada. Teste na Task 2.
3. Finalistas descobertos pelo arquivo do 2º turno (não fixos): o 1º turno de um município pode não ter um dos finalistas com votos (0) → % = 0, não erro. Teste na Task 5.
4. Recorte sem válidos (divisão por zero) → percentual `null`, nunca 0. Teste na Task 5.
5. TSE republica arquivo totalizado → `--refazer` rebaixa; sem ele, totalizado é pulado. Teste na Task 2.

---

### Task 1: Migration e schema de teste

**Files:**
- Create: `mamute_scrappers/migrations/versions/cs160a1b2c3d4_resultado_municipio.py`
- Modify: `mamute_scrappers/tests/eleicao_schema.py` (DDL SQLite equivalente)

Tabelas (Postgres):
- `ibge_municipio` (cd_ibge INTEGER PK, nome TEXT, uf TEXT, regiao_intermediaria_id INTEGER, regiao_intermediaria_nome TEXT, regiao_imediata_id INTEGER, regiao_imediata_nome TEXT, macrorregiao TEXT, atualizado_em TIMESTAMPTZ DEFAULT now()).
- `tse_resultado_municipio` (id BIGSERIAL PK, ciclo TEXT, codigo_eleicao INTEGER, turno SMALLINT, cargo_codigo INTEGER, uf TEXT, cd_tse TEXT, cd_ibge INTEGER NULL, nome TEXT, capital BOOLEAN, eleitorado BIGINT, comparecimento BIGINT, abstencao BIGINT, brancos BIGINT, nulos BIGINT, validos BIGINT, percentual_apurado NUMERIC(5,2), totalizacao_final BOOLEAN, tse_atualizado_em TIMESTAMPTZ, coletado_em TIMESTAMPTZ DEFAULT now(); UNIQUE (codigo_eleicao, cargo_codigo, cd_tse); index (ciclo, turno, cargo_codigo)).
- `tse_resultado_municipio_candidato` (resultado_id BIGINT FK ON DELETE CASCADE, sqcand BIGINT, numero INTEGER, nome_urna TEXT, votos BIGINT; PK (resultado_id, sqcand)).
- `comparacao_turnos_estimativa` (ciclo TEXT, cargo_codigo INTEGER, recorte TEXT, chave TEXT, origem TEXT, destino TEXT, proporcao NUMERIC(6,4), votos BIGINT, municipios INTEGER, calculado_em TIMESTAMPTZ DEFAULT now(); PK (ciclo, cargo_codigo, recorte, chave, origem, destino)).

- [ ] Escrever a migration com docstring no padrão das cs1xx (por quê, o que guarda) e `downgrade` com `DROP TABLE IF EXISTS`.
- [ ] Espelhar as 4 tabelas em `eleicao_schema.DDL`.
- [ ] `cd mamute_scrappers && python -m pytest tests -q` → tudo passa.
- [ ] Commit `feat(CS-160): tabelas do resultado por municipio`.

### Task 2: Parsing e coleta por município

**Files:**
- Modify: `mamute_scrappers/tse_crawler/resultados_parsing.py`
- Create: `mamute_scrappers/tse_crawler/resultados_municipio.py`
- Create fixtures: `mamute_scrappers/tests/fixtures/tse_resultados/mun-sc81051-t1.json`, `mun-sc81051-t2.json` (arquivos reais de Florianópolis), `mun-cm-mini.json` (lista com 2 municípios de SC + 1 do exterior), `ibge-mini.json` (2 municípios de SC)
- Test: `mamute_scrappers/tests/test_tse_resultados_municipio.py`

**Interfaces (produz):**
- `municipio_file_url(base, ciclo, eleicao, uf, cd_tse, cargo) -> str` = `{base}/{ciclo}/{eleicao}/dados/{uf}/{uf}{cd_tse}-c{cargo:04d}-e{eleicao:06d}-u.json`
- `municipios_url(base, ciclo, eleicao) -> str` = `{base}/{ciclo}/{eleicao}/config/mun-e{eleicao:06d}-cm.json`
- `@dataclass MunicipioTse(uf: str, cd_tse: str, cd_ibge: Optional[int], nome: str, capital: bool)`; `parse_municipios(payload) -> list[MunicipioTse]` (`abr[].cd` minúsculo; `cdi` vazio → None; `c == "s"` → capital).
- `@dataclass ResumoMunicipio(final, atualizado_em, percentual_apurado, eleitorado, comparecimento, abstencao, brancos, nulos, validos, candidatos: list[tuple[int sqcand, Optional[int] numero, str nome_urna, int votos]])`; `parse_resumo_municipio(payload) -> ResumoMunicipio`: `e.te`, `e.c`, `e.a`, `v.vb`, `v.tvn`, `v.vv`, candidatos de `_iter_candidatos` com `n`, `nmu`, `vap`.
- `parse_ibge(lista) -> list[dict]` com as colunas de `ibge_municipio` (`regiao-imediata.regiao-intermediaria`, `...UF.regiao.nome`).
- `coletar_municipios(session, *, base, ciclo="ele2026", cargo=1, http_get, refazer=False, workers=8, ibge_get=None) -> ColetaMunicipioStats` (`baixados`, `ja_finais`, `ausentes`, `finais`, `erros`). Descobre as eleições do cargo pelo `ele-c.json` (`parse_config`), baixa a lista de municípios de cada uma, pula `(codigo_eleicao, cd_tse)` já totalizado salvo `refazer`, baixa em `ThreadPoolExecutor(workers)` e grava na thread principal (upsert do resumo + delete/insert dos candidatos). Carrega `ibge_municipio` quando vazia via `ibge_get()`.
- CLI `python -m mamute_scrappers.tse_crawler.resultados_municipio [--refazer] [--workers N]` usa `http_get` de `resultados.py` e `requests.get(IBGE_URL, timeout=60)`; no fim chama `calcular_estimativas` (Task 3).

- [ ] Testes (escrever antes): `test_parse_municipios_capital_e_exterior` (Florianópolis capital, cdi 4205407; exterior com `cd_ibge is None`); `test_parse_resumo_florianopolis_t2` (eleitorado 423975, comparecimento 346830, Lula votos 162957, 2 candidatos); `test_coleta_grava_e_pula_totalizado` (2ª rodada não rebaixa; `refazer=True` rebaixa); `test_coleta_404_conta_ausente_sem_linha`; `test_coleta_carrega_ibge_uma_vez`.
- [ ] Rodar e ver falhar; implementar; rodar e ver passar.
- [ ] Commit `feat(CS-160): coleta de presidente por municipio`.

### Task 3: Estimativa de transferência

**Files:**
- Create: `mamute_scrappers/tse_crawler/transferencia_votos.py`
- Test: `mamute_scrappers/tests/test_transferencia_votos.py`

**Interfaces:**
- `ORIGENS`: finalistas (2, por sqcand do arquivo do 2º turno), os 3 eliminados de mais votos no Brasil (por sqcand), `"demais"`, `"brancos_nulos"`, `"abstencao"`. `DESTINOS`: os 2 finalistas, `"brancos_nulos"`, `"abstencao"`.
- `estimar_matriz(x: np.ndarray (n,k), y: np.ndarray (n,m), pesos: np.ndarray (n,)) -> np.ndarray (k,m)`: x e y em proporção do eleitorado de cada município; mínimos quadrados ponderados com cada linha da matriz no simplex (soma 1, >= 0). Algoritmo: gradiente projetado (projeção de cada linha no simplex, passo 1/L com L = maior autovalor de XᵀWX), até 5000 iterações ou variação < 1e-9.
- `calcular_estimativas(session, ciclo="ele2026", cargo=1) -> int` (linhas gravadas): só roda se TODOS os municípios dos dois turnos estiverem totalizados; senão devolve 0 sem apagar nada. Recortes `brasil` (chave `BR`) e `macrorregiao` (chave = nome IBGE; exterior fica só no Brasil). Grava `proporcao` e `votos = round(proporcao * votos_origem_no_recorte, -3)`.

- [ ] Testes: `test_estimar_recupera_matriz_conhecida` (400 municípios sintéticos, matriz 3x3 conhecida, ruído 0, erro max < 0.02); `test_linhas_somam_um`; `test_nao_calcula_com_municipio_aberto`.
- [ ] Implementar; testes passam.
- [ ] Commit `feat(CS-160): estimativa de transferencia de votos`.

### Task 4: Cron

**Files:**
- Modify: `mamute_scrappers/docker/scrappers.cron`

- [ ] Nova linha `*/30 * * * *` com `run-cron-job.sh resultado-municipio -- /usr/local/bin/python -m mamute_scrappers.tse_crawler.resultados_municipio`, separada do `resultado-eleicao` (falha aqui não segura o e-mail). Comentário: barato depois de totalizado (pula), pode sair depois de nov/2026.
- [ ] Commit `chore(CS-160): cron da coleta por municipio`.

### Task 5: API

**Files:**
- Create: `api/services/runoff_comparison.py`
- Modify: `api/routers/admin.py` (rota `GET /runoff-comparison`, mesmos dois imports condicionais usados por `elected_profile`)
- Modify: `api/README.md` (seção "Diferença 2º turno (CS-160)")
- Test: `api/tests/test_runoff_comparison.py`

**Interfaces:**
- `runoff_comparison(db: Session) -> dict` com:
  - `disponivel: bool` (tabelas existem e há dado do 2º turno);
  - `situacao: {turno1: {municipios, totalizados}, turno2: {...}, parcial: bool, tse_atualizado_em}`;
  - `finalistas: [{sqcand, numero, nome}]` (2, do 2º turno, ordem por votos no Brasil);
  - `recortes: [Recorte]`, `Recorte = {tipo: "brasil"|"macrorregiao"|"uf"|"regiao_intermediaria"|"capital"|"interior", chave, nome, uf|null, turno1: Totais, turno2: Totais, diferenca: {finalistas: {sqcand: {votos, pp}}, abstencao, abstencao_pp, brancos_nulos, comparecimento}, lider_turno1, vencedor_turno2, mudou_lider}`;
  - `Totais = {eleitorado, comparecimento, abstencao, abstencao_pct, brancos, nulos, validos, finalistas: {sqcand: {votos, pct}}, demais_votos (só t1), margem (só t2: votos e pp)}`;
  - `abstencao_x_margem: [{tipo, chave, nome, uf, variacao_abstencao, margem_votos, maior_que_margem}]` (UF e região intermediária; vazio se parcial);
  - `estimativa: {calculado_em, recortes: [{recorte, chave, linhas: [{origem, destino, proporcao, votos}]}]}` (vazio se parcial ou sem linhas);
  - `destaques: [{tipo, texto, recorte, chave}]`.
- UF "Exterior": chave `ZZ`, nome "Exterior". Capital/interior só para as 27 UFs.
- Frases (determinísticas, `_fmt_pct` com vírgula e 1 casa, votos com ponto de milhar):
  - `brasil`: "No Brasil, {nome} foi de {p1}% para {p2}% dos votos válidos ({±d} pp)." uma por finalista; "A abstenção foi de {a1}% para {a2}% do eleitorado."
  - `maior_ganho_uf`: top 3 UFs por pp de cada finalista: "Em {UF nome}, {nome} foi de {p1}% para {p2}% ({+d} pp)."
  - `maior_ganho_regiao`: top 3 regiões intermediárias por pp de cada finalista: "Na região de {regiao} ({UF}), {nome} foi de ... ."
  - `mudou_lider`: UFs e regiões onde o líder do 1º turno não venceu o 2º: "Em {nome}, {lider1} liderou o 1º turno e {vencedor2} venceu o 2º."
  - `capital_interior`: top 3 UFs pela maior |Δpp capital − Δpp interior| de um finalista: "Em {UF}, {nome} variou {dc} pp na capital e {di} pp no interior."
  - `abstencao_margem` (só não parcial): "Em {nome}, a abstenção variou {±n} eleitores entre os turnos; a margem final foi de {m} votos."
  - Parcial: prefixo "Parcial: " em todas.
- Nomes de UF: mapa sigla → nome no service.

- [ ] Testes (fixture SQLite com 3 municípios em 2 UFs + 1 exterior, 2 turnos): `test_sem_tabelas_responde_indisponivel`; `test_soma_brasil_inclui_exterior`; `test_pp_e_lider`; `test_percentual_null_sem_validos`; `test_finalista_sem_voto_no_t1_e_zero`; `test_parcial_sem_abstencao_e_estimativa_e_prefixo`; `test_frases_sem_travessao`; `test_rota_404_para_nao_admin` (padrão de `test_elected_profile.py`).
- [ ] Implementar; `cd api && python -m pytest tests/test_runoff_comparison.py -q` passa; suíte toda passa.
- [ ] README atualizado.
- [ ] Commit `feat(CS-160): rota admin da comparacao entre turnos`.

### Task 6: PR do core, merge, deploy, carga

- [ ] Branch `feat/cs160-comparacao-turnos`, PR "CS-160: comparação 1º x 2º turno de presidente por município" contra `main`, CI verde, merge `--admin`.
- [ ] Deploy (core não deploya sozinho): `gh workflow run deploy-prd.yml --ref main` no app, ou junto com a PR do app.
- [ ] Em prod: conferir `alembic_version = cs160a1b2c3d4`; rodar `docker exec prod-mamute-politico-scrappers python -m mamute_scrappers.tse_crawler.resultados_municipio`; conferir soma do Brasil e de 3 UFs contra os arquivos de UF do TSE (`e.te`, `e.a`, votos dos finalistas) nos dois turnos.

### Task 7: Tela no app

**Files:**
- Modify: `ui/src/api/admin.ts` (tipos `RunoffComparison*` espelhando a Task 5, `fetchRunoffComparison()` → `/admin/runoff-comparison`)
- Modify: `ui/src/hooks/useMetrics.ts` (`useRunoffComparison()`, `refetchInterval: 5 * 60_000`)
- Create: `ui/src/lib/segundoTurno.ts` + `ui/src/lib/segundoTurno.test.ts` (`formatarPp(pp: number|null) -> "+4,8 pp" | "−1,2 pp" | "—"`, `formatarVotos`, `ordenarUfs(recortes, criterio)`, `textoTodosDestaques(destaques) -> string` separado por linha em branco)
- Create: `ui/src/components/admin/DiferencaSegundoTurno.tsx`
- Modify: `ui/src/pages/AdminEleitosPage.tsx` (abas "Perfil dos eleitos" e "Diferença 2º turno" via `useSearchParams`, `?aba=segundo-turno`; conteúdo atual intocado)
- Modify: `ui/src/pages/AdminPage.tsx` (descrição do card cita a aba nova)

Conteúdo da aba, nesta ordem: situação (aviso amarelo se `parcial`), destaques (copiar cada um + "Copiar todos"), Brasil + macrorregiões (dois turnos e diferença), tabela por UF ordenável (expande regiões e capital x interior), abstenção x margem (ou "aguardando a totalização"), estimativa (aviso fixo + InfoPopover com o método). `disponivel: false` → card "Os dados do 2º turno ainda não foram coletados."

- [ ] Testes da lib (escrever antes) e implementação; `npm test`, `npm run lint`, `npx tsc -p tsconfig.app.json --noEmit`, `npm run build` passam.
- [ ] Conferir no navegador local contra a API de prod (túnel ou build com dados mock) que a aba abre e o copiar funciona.
- [ ] PR no app "CS-160: aba Diferença 2º turno no Perfil dos eleitos", CI verde, merge (deploya), conferir em prod.
- [ ] Jira CS-160 → Concluído com "Resolvido por".
