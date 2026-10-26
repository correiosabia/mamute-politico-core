# Comparação 1º x 2º turno de presidente (aba "Diferença 2º turno")

Data: 25/10/2026. Repos: core (coleta, cálculo e API) e app (tela). Duas PRs, core primeiro.

## Por quê

Na noite do 2º turno de 2026 o Luiz pediu um levantamento de como a eleição presidencial mudou
do 1º para o 2º turno: quanto cada finalista ganhou ou perdeu por estado e por região, como
variou a abstenção e se essa variação foi grande o bastante para pesar no resultado. O material
é para a equipe editorial montar um e-mail aos assinantes. O que interessa são frases de região
("na região de Chapecó o candidato X subiu 9 pontos"), não uma lista de cidades.

Sucesso = a equipe editorial abre a aba, entende em poucos minutos onde a eleição mudou e copia
frases prontas e corretas para o e-mail.

## Escopo

**Dentro**
- Só presidente. 1º turno = eleição TSE 6257, 2º turno = 6258 (`ele-c.json` do ciclo `ele2026`).
- Recortes: Brasil, 5 macrorregiões, 27 UFs mais o Exterior, regiões geográficas intermediárias
  do IBGE (133) e capital x interior em cada UF.
- Para cada recorte, nos dois turnos e na diferença entre eles: eleitorado, comparecimento,
  abstenção, brancos, nulos, válidos, votos e % dos válidos de cada finalista, margem do 2º turno.
- Estimativa de transferência de votos (decisão do Luiz: "sim, como estimativa").
- Destaques prontos: frases geradas a partir dos números (decisão do Luiz).
- Aba nova "Diferença 2º turno" na tela Perfil dos eleitos, só para admin.

**Fora**
- Governador e outros cargos.
- Lista navegável de municípios na tela (o dado municipal fica no banco, mas a tela não lista cidade).
- E-mail ou envio: a equipe editorial escreve o e-mail; aqui só sai o material.
- Mapa (pode vir depois; não é necessário para as frases).

**Anti-objetivos** (o que faz a entrega falhar mesmo funcionando)
- Apresentar a estimativa de transferência como fato. Ela é inferência ecológica e sempre
  aparece marcada como estimativa, com o aviso, separada dos números medidos.
- Concluir quem se absteve. Dá para comparar totais (a abstenção subiu mais que a margem), não
  para dizer que eleitores de X deixaram de votar. As frases não podem afirmar causa.
- Viés: nada de adjetivo, nada de "virada histórica" ou "derrota". Frases descritivas, mesma
  forma para os dois candidatos. O core é público e neutro (nomes de código genéricos).
- Número errado por dado parcial sem aviso.

## Fonte de dados (conferido em 25/10/2026)

- Arquivo por município: `{base}/ele2026/{eleicao}/dados/{uf}/{uf}{cd_mun_tse}-c0001-e{eleicao:06d}-u.json`
  (ex.: `.../6258/dados/sc/sc81051-c0001-e006258-u.json`, Florianópolis). Mesmo formato do
  arquivo de UF já parseado em `tse_crawler/resultados_parsing.py`:
  - `e.te` eleitorado, `e.c` comparecimento, `e.a` abstenção;
  - `v.vb` brancos, `v.tvn` nulos (`vn` + `vnt`), `v.vv` válidos;
  - `carg[0].agr[].par[].cand[]`: `sqcand`, `n` (número), `nmu`, `vap` (votos);
  - `s.pst` % de seções apuradas, `tf` = "s" quando a totalização encerrou, `dt`/`ht` hora.
- Lista de municípios: `{base}/ele2026/{eleicao}/config/mun-e{eleicao:06d}-cm.json`, com
  `abr[].cd` (UF, `zz` = exterior), `mu[].cd` (código TSE), `cdi` (código IBGE), `nm`, `c` ("s" = capital).
  5.757 entradas no 1º turno.
- IBGE: `https://servicodados.ibge.gov.br/api/v1/localidades/municipios` (uma chamada, todos os
  municípios) dá `regiao-imediata.regiao-intermediaria.{id,nome}` e a macrorregião. Exterior não
  tem código IBGE: entra como UF "Exterior", sem região.
- Gotchas já conhecidos: o bucket responde 403/404 para arquivo inexistente; a hora `dt/ht` de AC
  e AM vem no fuso local; usar o mesmo `User-Agent` e cliente HTTP da coleta de resultados.

## Como funciona

### Coleta (core, `mamute_scrappers`)

Módulo novo ao lado de `tse_crawler/resultados.py`, nome genérico (ex.: `tse_crawler/resultados_municipio.py`).

1. Lê `ele-c.json`, acha as duas eleições de presidente do ciclo (cargo 1, turnos 1 e 2).
2. Baixa a lista de municípios de cada eleição.
3. Para cada município x turno ainda não totalizado na base, baixa o arquivo e grava. Arquivo com
   `tf = "s"` não é baixado de novo, **mas** guarda `tse_atualizado_em` e a coleta tem um modo
   `--refazer` para rebaixar tudo (lição do RJ em 25/10: o TSE republica arquivo já totalizado).
4. Concorrência pequena e educada (ex.: 8 conexões, timeout e retry), para ~11,5 mil arquivos
   caberem em poucos minutos sem martelar o TSE.
5. Ao final, se algo mudou, recalcula a estimativa de transferência (abaixo) e grava.

Tabelas novas (migration alembic, idempotente: lição da #173):

- `ibge_municipio` (cd_ibge pk, nome, uf, regiao_intermediaria_id, regiao_intermediaria_nome,
  regiao_imediata_id, regiao_imediata_nome, macrorregiao). Carregada pela coleta se estiver vazia.
- `tse_resultado_municipio` (ciclo, codigo_eleicao, turno, uf, cd_tse, cd_ibge null, nome,
  capital bool, eleitorado, comparecimento, abstencao, brancos, nulos, validos,
  percentual_apurado, totalizacao_final, tse_atualizado_em, coletado_em; unique
  codigo_eleicao + cd_tse).
- `tse_resultado_municipio_candidato` (resultado_id fk, sqcand, numero, nome_urna, votos; unique
  resultado_id + sqcand).
- `comparacao_turnos_estimativa` (ciclo, recorte, chave, origem, destino, proporcao, votos,
  municipios, calculado_em): a matriz de transferência por recorte.

Agendamento: entra no cron `resultado-eleicao` existente enquanto houver arquivo do 2º turno não
totalizado (barato depois disso, porque pula os totalizados). A primeira carga do 1º turno pode
ser rodada à mão logo após o deploy.

### Estimativa de transferência

Pergunta: dos eleitores de cada opção no 1º turno, que fração foi para cada opção no 2º?

- Origens (1º turno, sobre o eleitorado do município): Lula, Flávio, Cury, Renan, Caiado,
  demais candidatos somados, brancos + nulos, abstenção.
- Destinos (2º turno): Lula, Flávio, brancos + nulos, abstenção.
- Método: regressão ecológica com restrições (cada linha da matriz soma 1, valores entre 0 e 1),
  municípios como observações, ponderados pelo eleitorado. Resolver com numpy (já instalado no
  container dos scrappers; não adicionar scipy sem necessidade).
- Recortes com estimativa: Brasil e as 5 macrorregiões. UF e região intermediária NÃO têm
  estimativa (poucas observações em várias UFs; o resultado seria instável).
- Eleitorado diferente entre turnos (é o mesmo cadastro, mas confirmar): normalizar pelo
  eleitorado do 2º turno e registrar a diferença.
- Sempre exibida como "Estimativa estatística, não é dado oficial do TSE", com uma frase sobre o
  método e sua limitação (falácia ecológica). O valor em votos é arredondado (milhares).

### API (core, `api`)

`GET /api/admin/runoff-comparison` (admin; 404 para os outros, igual a `elected-profile`).
Regras num service novo (`services/runoff_comparison.py`), testado com o banco de teste.

Resposta (forma indicativa; o builder ajusta nomes):

- `situacao`: % de municípios totalizados em cada turno, hora mais recente do TSE, `parcial` bool.
- `finalistas`: [{sqcand/número, nome}] dos dois do 2º turno (descobertos pelo arquivo do 2º turno,
  não fixados no código).
- `recortes`: lista de {tipo: brasil | macrorregiao | uf | regiao_intermediaria | capital | interior,
  chave, nome, uf, turno1: {...}, turno2: {...}, diferenca: {...}}, com votos e % dos válidos de
  cada finalista, demais candidatos (só turno 1), abstenção, brancos, nulos, comparecimento,
  margem do 2º turno, `lider_turno1`, `vencedor_turno2`, `mudou_lider` bool.
- `abstencao_x_margem`: por UF e região intermediária, variação absoluta da abstenção e a margem do
  2º turno, com `abstencao_maior_que_margem` bool.
- `estimativa`: matrizes por recorte (Brasil e macrorregiões), com `calculado_em`.
- `destaques`: lista de frases prontas (abaixo), cada uma com o recorte e os números de origem.

Agregação: soma dos municípios (nunca média de percentuais). Percentuais em pontos percentuais
(pp) para diferenças.

### Destaques prontos

Gerados no service a partir dos números, com regras simples e determinísticas (sem LLM):

- Maiores ganhos de cada finalista entre UFs e entre regiões intermediárias (em pp do % válido).
- Recortes onde o líder mudou do 1º para o 2º turno.
- Onde a variação da abstenção superou a margem do 2º turno.
- Capital x interior: onde a diferença entre os dois mais cresceu.
- Brasil: variação nacional de cada finalista, da abstenção e de brancos + nulos.

Forma das frases: neutra e simétrica, número sempre com a base ("de 47,0% para 51,8% dos votos
válidos, +4,8 pp"). Frase de abstenção não afirma causa ("a abstenção cresceu 412 mil eleitores,
mais que a margem final de 298 mil"). Frases da estimativa ficam num bloco separado e começam com
"Estimativa:". Cada frase tem botão copiar; um "copiar todas" junta em texto corrido.

### Tela (app, `ui`)

- `AdminEleitosPage` ganha abas no topo: "Perfil dos eleitos" (o conteúdo atual, intocado) e
  "Diferença 2º turno". A aba escolhida vai na URL (`?aba=segundo-turno`) para dar para mandar link.
  O card do painel admin passa a citar a aba nova na descrição.
- Conteúdo da aba, de cima para baixo:
  1. Situação: aviso amarelo se algum município ainda não estiver totalizado (com %), senão a
     fonte e a hora do TSE.
  2. Destaques prontos (o que a equipe editorial mais usa).
  3. Brasil: dois turnos lado a lado + diferença, e as 5 macrorregiões.
  4. Tabela por UF (ordenável por variação de cada finalista, abstenção e margem; mostra se o
     líder mudou). Clicar numa UF abre as regiões intermediárias dela e capital x interior.
  5. Abstenção x margem.
  6. Estimativa de transferência (Brasil e macrorregiões), com o aviso fixo e um InfoPopover
     explicando o método em linguagem simples.
- Segue o visual da tela atual (`mp-card`, cores e tipografia de `AdminEleitosPage`). Sem cor de
  partido; os dois finalistas usam duas cores neutras e consistentes em toda a aba.
- Formatação em `lib/` separada e testada, como `lib/perfilEleitos.ts`.

## Restrições

- Core público: nada de opinião, nomes de pessoas em código além do que vem do dado, receita ou
  nomes internos em commit/PR. Nomes de código genéricos (`runoff_comparison`, `resultado_municipio`).
- Prod: FastAPI < 0.115, Postgres nativo no host, deploy pelo push na main do app (core antes);
  migration roda depois do código subir, então o código tolera tabela ausente (padrão
  `_tem_coluna` já usado na coleta).
- Sem feature flag: é tela de admin, como Perfil dos eleitos (decisão minha, ver abaixo).
- Jira: task nova no projeto CS, key no título das duas PRs.

## Decisões do Luiz (25/10/2026)

- Recorte abaixo da UF: regiões intermediárias do IBGE + capital x interior.
- Mostrar estimativa de transferência, marcada como estimativa.
- Tela + destaques prontos para copiar.
- Começar já, com dado parcial; a tela fica certa quando o TSE fechar.
- Aba nova chamada "Diferença 2º turno" dentro de Perfil dos eleitos.

## Decisões minhas (para o Luiz revisar)

- Estimativa só para Brasil e macrorregiões, não por UF.
- Origens agrupadas: os três maiores eliminados separados, o resto somado.
- Sem feature flag (admin only, igual ao Perfil dos eleitos).
- Destaques por regra fixa, sem LLM, para serem reproduzíveis e auditáveis.
- Exterior aparece como UF própria e entra no total do Brasil.

## Respostas à revisão do desenho (25/10, Luiz ausente: decisões minhas pelo padrão mais seguro)

- **Prazo:** entregar tudo de uma vez nesta leva (Luiz pediu autonomia até o deploy). Ordem de
  implementação: coleta + números medidos primeiro, estimativa e frases depois, para que um corte
  de escopo, se necessário, deixe algo útil.
- **Dado parcial:** a tela mostra os números com o aviso de parcial (e o % de municípios
  totalizados). As frases de abstenção x margem e a estimativa de transferência só são calculadas
  quando os dois turnos estão 100% totalizados; antes disso o bloco diz "aguardando a totalização".
  As demais frases saem com o prefixo "Parcial:" enquanto houver município aberto.
- **Quem usa a tela:** só admins (hoje inclui o Luiz). O Maurício recebe o texto pelo
  "copiar todas", colado pelo Luiz. Não criar permissão nova.
- **Capital x interior:** métrica = variação (pp do % válido) de cada finalista na capital e no
  interior da mesma UF. A frase destaca as UFs onde a diferença entre a variação na capital e no
  interior é maior ("Em SP, Fulano subiu 6,1 pp na capital e 1,2 pp no interior").
- **Estimativa:** só Brasil e macrorregiões, como já estava.

## Deixado para o builder

- Nomes exatos de colunas, endpoints e componentes.
- Limiares das frases (quantos destaques por tipo, corte mínimo em pp para virar frase).
- Algoritmo exato do mínimo quadrado com restrições (projeção iterativa, multiplicadores etc.),
  desde que testado contra um caso sintético com matriz conhecida.
- Layout fino da tabela e da área de estimativa no celular.
