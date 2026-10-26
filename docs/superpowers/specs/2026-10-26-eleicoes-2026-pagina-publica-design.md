# Eleições 2026: página pública com gráficos e método explicado (CS-162)

Data: 26/10/2026. Repos: core (estado de publicação + rotas públicas) e app (página). Segue a CS-160
(aba "Diferença 2º turno") e a CS-161 (estimativa com pesquisas como ponto de partida).

## Por quê

A aba de comparação entre turnos ficou com cara de planilha. O Luiz pediu: mais gráficos (com opção
de ver como planilha), destaques recolhíveis, um modal que explique o método em linguagem de notícia
(para a equipe editorial poder publicar e explicar), salvar e copiar os gráficos, e a possibilidade de
abrir a tela ao público, com o mesmo esquema da Na Lupa: um estado "só admin" ou "pública", sem
recorte por plano.

## Escopo

**Dentro**
- Página nova `/eleicoes-2026`, com as abas "Perfil dos eleitos" e "Diferença 2º turno" (`?aba=`).
  A tela de admin `/admin/perfil-dos-eleitos` passa a usar os mesmos componentes e ganha o controle de
  publicação.
- Estado de publicação no banco: tabela `pagina_publicacao` (chave PK, estado `admin` | `publica`,
  atualizado_em, atualizado_por). Chave `eleicoes_2026`. Sem linha = `admin`.
- Rotas públicas (sem login): `GET /api/public/eleicoes-2026/elected-profile` e
  `GET /api/public/eleicoes-2026/runoff-comparison`. Respondem 404 quando o estado é `admin`. A versão
  pública da comparação vem sem `destaques`.
- Rotas admin: `GET /api/admin/pages/eleicoes_2026` e `PUT /api/admin/pages/eleicoes_2026 {estado}`.
- Visual:
  - Diferença 2º turno: gráfico de barras divergentes da variação (pp) de cada finalista por UF;
    "haltere" 1º turno → 2º turno por macrorregião; barras da abstenção x margem; matriz de
    transferência como barras empilhadas por origem, com a faixa de incerteza e a pesquisa de
    referência marcada.
  - Perfil dos eleitos: barras empilhadas de gênero, cor/raça e escolaridade no Brasil.
  - Cada gráfico tem: "Ver como planilha" (alterna para a tabela atual), "Salvar imagem" (PNG) e
    "Copiar imagem" (área de transferência; com fallback para salvar quando o navegador não deixar).
- Destaques prontos (só admin): grupos recolhíveis, com "Expandir tudo" e "Recolher tudo".
- Modal "Como esta análise foi feita": fonte dos dados, o que é medido e o que é estimado, o método
  da estimativa em linguagem simples, o papel das pesquisas e da eleição anterior como ponto de
  partida, o que a faixa de incerteza significa, limitações, e o contexto histórico usado como base
  (ex.: Minas Gerais como termômetro desde 1989), sempre com a fonte. Botão "Copiar texto" para a
  explicação ir direto para a matéria.

**Fora**
- Tabelas de eleições antigas na tela (o histórico é base da análise e aparece no modal, não como
  consulta). Decisão do Luiz.
- Recorte por plano (só admin x pública).
- Mapa.

**Anti-objetivos**
- Estimativa apresentada como fato, ou número "incerto" em destaque de gráfico sem a marca.
- Frase que afirma causa ou com adjetivo.
- Dado parcial sem aviso.
- Página pública vazando destaques internos ou qualquer dado de assinante.

## Restrições

- Feature de página pública não pode ficar atrás de `useFeatureFlag` (ele só consulta com token;
  visitante sem login vê `false`). O portão é o estado no banco, verificado na rota pública.
- Core público e neutro: nomes genéricos, sem opinião.
- Migration idempotente; rota pública responde 404 antes dela (tabela ausente = `admin`).
- FastAPI < 0.115 em prod.
- Sem travessão nos textos; faixa numérica escrita "40 a 63%".

## Decisões do Luiz (26/10/2026)

- Público vê tudo menos os destaques prontos.
- Histórico: usar como base da análise e explicar no modal, sem exibir os dados de todas as eleições.
- Endereço `/eleicoes-2026`.
- Deploy em duas etapas: primeiro a estimativa corrigida (CS-161), depois esta página.

## Decisões minhas

- Uma tabela genérica `pagina_publicacao` por chave, em vez de coluna solta, para servir a outras
  páginas no futuro.
- A página nasce em `admin`; publicar é o botão no admin.
- Gráficos em recharts (já no projeto), exportação com html-to-image (já usado na Na Lupa).
