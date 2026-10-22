<!--
 TÍTULO: começa com a key do Jira, exatamente como está lá.
         Ex: CS-123: Corrige cálculo de saldo na tela de fechamento

 TASK RELACIONADA no corpo: cite SEM o hífen (CS123, não CS-123),
         senão o GitHub vincula ela junto e o quadro se move errado.

 Apague as seções que não se aplicam e apague estes comentários
 de instrução conforme for preenchendo.
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

<!-- OBRIGATÓRIO em PR de bug. Apague a seção se não for correção. Uma linha basta. -->

> [!NOTE]
> A preencher pelo autor.

## Como testar

<!-- Passo a passo pra quem revisa reproduzir. Dados de teste, usuário, rota. -->

1.
2.
3.

## Prints

<!-- OBRIGATÓRIO quando a mudança aparece na tela. Em correção só de backend, apague a seção. -->

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
- [ ] Bloco de antes/após deploy preenchido, se for o caso
