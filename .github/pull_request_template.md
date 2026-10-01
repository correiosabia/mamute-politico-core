<!--
═══════════════════════════════════════════════════════════════
 PADRÃO DE PR DA LV DEV

 TÍTULO: começa com a key do Jira, exatamente como está lá.
         Ex: DEV-791: Corrige cálculo de saldo na tela de fechamento

 TASK RELACIONADA: cite SEM o hífen (DEV791, não DEV-791),
         senão o GitHub vincula ela junto e o quadro se move errado.

 Apague as seções que não se aplicam e apague estes comentários
 de instrução conforme for preenchendo.
═══════════════════════════════════════════════════════════════
-->

<!--
 SEÇÃO DE DEPLOY — se esta PR não exige nada rodado na mão
 antes ou depois de subir, APAGUE o bloco inteiro até a linha
 de separação. Se exige, troque ANTES por APÓS quando for o caso.
-->

## ❗ FAZER ANTES DO DEPLOY

> [!CAUTION]
> **O que precisa ser rodado**
> Por que, e o que quebra se não for feito.

---

## O que muda

<!-- 2 a 5 linhas, do ponto de vista de quem usa o sistema, não do diff. -->

## Causa raiz

<!--
 OBRIGATÓRIO em PR de bug. Apague a seção se não for correção.

 ESCRITO POR HUMANO. Esta seção não deve ser preenchida por IA,
 em nenhuma hipótese. Uma linha basta.

 Ex: "O erro 500 acontecia ao tentar excluir um usuário porque
      tentava direcionar pra uma URL de retorno que não existe mais."
-->

> [!NOTE]
> A preencher pelo autor.

## Como testar

<!-- Passo a passo pra quem revisa reproduzir. Dados de teste, usuário, rota. -->

1.
2.
3.

## Prints

<!--
 OBRIGATÓRIO quando a mudança aparece na tela.
 Bom senso em correção só de backend: pode apagar a seção.

 ANEXADO POR HUMANO. Se você automatizar a captura, ótimo,
 só não deixe passar sem print quando a tela mudou.
-->

> [!NOTE]
> A anexar pelo autor.

## Observações

<!-- Decisão técnica que valha explicar, dívida assumida, o que ficou de fora. Apague se não houver. -->

---

### Checklist

- [ ] CI verde, ou falha já corrigida e subida
- [ ] Título começa com a key do Jira exata
- [ ] Tasks relacionadas citadas sem hífen
- [ ] Print anexado, se a mudança aparece na tela
- [ ] Causa raiz escrita por mim, se for bug
- [ ] Bloco de antes/após deploy preenchido, se for o caso
- [ ] Consigo explicar o que fiz sem consultar a IA
