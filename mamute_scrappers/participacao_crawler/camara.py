"""Câmara dos Deputados: enquetes das proposições (CS-179).

Toda proposição tem uma enquete (5 níveis, de "concordo totalmente" a "discordo
totalmente"), aberta enquanto tramita, SEM PRAZO. Não há API: a página
/enquetes só lista as mais comentadas e as mais votadas. Pegamos as listas da
última semana (as que estão em alta), e o placar vem de /enquetes/{id}/resultados.
O id da enquete é o id da proposição na API v2 da Câmara.

Enquetes aprovadas pela curadoria que saíram das listas continuam com o placar
atualizado.
"""

from __future__ import annotations

from typing import Any, Optional

from bs4 import BeautifulSoup

from mamute_scrappers.participacao_crawler.comum import baixar, item, limpa, numero, resumo

SOURCE = "camara_enquete"
LISTA_URL = "https://www.camara.leg.br/enquetes"
ENQUETE_URL = "https://www.camara.leg.br/enquetes/{id}"
ORGANIZACAO = "Câmara dos Deputados"
SECOES = ("Enquetes mais comentadas na última semana", "Enquetes mais votadas na última semana")


def build_lista(pagina: str) -> list[dict[str, Any]]:
    """Enquetes das listas da última semana, sem repetir, na ordem da página."""
    soup = BeautifulSoup(pagina, "html.parser")
    vistos: dict[str, dict[str, Any]] = {}
    for titulo in soup.find_all(["h2", "h3"]):
        if limpa(titulo.get_text()) not in SECOES:
            continue
        lista = titulo.find_next("ol")
        if lista is None:
            continue
        for a in lista.select("a.info-com-badge-numerico"):
            ident = a.get("href", "").rstrip("/").rsplit("/", 1)[-1]
            texto = limpa(a.select_one(".info-com-badge-numerico__texto").get_text(" ")) if a.select_one(
                ".info-com-badge-numerico__texto"
            ) else ""
            if not ident.isdigit() or not texto or ident in vistos:
                continue
            ref = a.select_one(".info-com-badge-numerico__retranca")
            vistos[ident] = item(
                source=SOURCE,
                external_id=ident,
                kind="enquete",
                title=resumo(texto, 220) or texto,
                summary=texto if len(texto) > 220 else None,
                reference=limpa(ref.get_text()) if ref else None,
                organization=ORGANIZACAO,
                url=ENQUETE_URL.format(id=ident),
                closes_on=None,
            )
    return list(vistos.values())


def placar(pagina: str) -> Optional[dict[str, Any]]:
    soup = BeautifulSoup(pagina, "html.parser")
    opcoes = []
    for linha in soup.select("tr.resumo-resposta"):
        rotulo, valor = linha.select_one(".resumo-resposta__opiniao"), linha.select_one(
            ".resumo-resposta__participacoes"
        )
        if rotulo is None or valor is None or numero(valor.get_text()) is None:
            continue
        opcoes.append({"label": limpa(rotulo.get_text()), "count": numero(valor.get_text())})
    if not opcoes:
        return None
    desde = soup.select_one(".resultado-descricao")
    return {"options": opcoes, "note": limpa(desde.get_text()) if desde else None}


def collect(session: Any, *, esperar=None) -> dict[str, int]:
    from mamute_scrappers.participacao_crawler.comum import aprovados_abertos, atualizar_placar, pausa, salvar

    def _pausa():
        pausa(esperar) if esperar else pausa()

    itens = build_lista(baixar(LISTA_URL).text)
    for linha in itens:
        _pausa()
        linha["score"] = placar(baixar(ENQUETE_URL.format(id=linha["external_id"]) + "/resultados").text)
    r = salvar(session, SOURCE, itens)

    na_lista = {i["external_id"] for i in itens}
    atualizados = 0
    for ident in aprovados_abertos(session, SOURCE):
        if ident in na_lista:
            continue
        _pausa()
        p = placar(baixar(ENQUETE_URL.format(id=ident) + "/resultados").text)
        if p:
            atualizar_placar(session, SOURCE, ident, p)
            atualizados += 1
    return {"enquetes": r["gravados"], "placares_atualizados": atualizados}
