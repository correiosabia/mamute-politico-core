"""Senado e-Cidadania: Consulta Pública e Ideia Legislativa (CS-179).

O e-Cidadania não tem API de participação (nem nos dados abertos do Senado). A
coleta lê o HTML das listagens, liberado pelo robots.txt. Os termos de uso
proíbem votar por robô, não ler.

* Consulta Pública: toda matéria em tramitação tem uma, aberta enquanto
  tramita, SEM PRAZO. A listagem `situacao=aberta` vem ordenada pelo total de
  votos; pegamos as primeiras (`LIMITE_CONSULTAS`). O placar é Sim/Não.
* Ideia Legislativa: tem prazo ("Apoios abertos até ...", 4 meses) e meta de
  20 mil apoios. Pegamos as ideias abertas com pelo menos `MIN_APOIOS`.

Sair da listagem não quer dizer que encerrou (a listagem é um recorte): as
consultas aprovadas pela curadoria têm o placar atualizado pela página de cada
matéria.
"""

from __future__ import annotations

from typing import Any, Optional

from bs4 import BeautifulSoup

from mamute_scrappers.participacao_crawler.comum import (
    baixar,
    data_por_extenso,
    item,
    limpa,
    numero,
    resumo,
)

BASE = "https://www12.senado.leg.br/ecidadania/"
CONSULTAS_URL = BASE + "pesquisamateria?situacao=aberta"
IDEIAS_URL = BASE + "pesquisaideia?situacao=5"
ORGANIZACAO = "Senado Federal"
SOURCE_CONSULTA = "senado_consulta"
SOURCE_IDEIA = "senado_ideia"
LIMITE_CONSULTAS = 30
MIN_APOIOS = 1000


def placar_sim_nao(sim: Optional[int], nao: Optional[int]) -> Optional[dict[str, Any]]:
    if sim is None or nao is None:
        return None
    return {"options": [{"label": "Sim", "count": sim}, {"label": "Não", "count": nao}]}


def _id(href: str, chave: str) -> Optional[str]:
    if chave not in (href or ""):
        return None
    valor = href.split("id=", 1)[-1].split("&", 1)[0]
    return valor if valor.isdigit() else None


def build_consultas(pagina: str, limite: int = LIMITE_CONSULTAS) -> list[dict[str, Any]]:
    soup = BeautifulSoup(pagina, "html.parser")
    itens = []
    for bloco in soup.select("div.resumo-materia"):
        cab = bloco.select_one("header a")
        if cab is None:
            continue
        ident = _id(cab.get("href", ""), "visualizacaomateria")
        ementa = limpa(bloco.select_one("section").get_text(" ") if bloco.select_one("section") else "")
        if not ident or not ementa:
            continue
        numeros = [numero(s.get_text()) for s in bloco.select("figure.grafico-consulta-publica header span")]
        itens.append(
            item(
                source=SOURCE_CONSULTA,
                external_id=ident,
                kind="consulta",
                title=resumo(ementa, 220) or ementa,
                summary=ementa if len(ementa) > 220 else None,
                reference=limpa(cab.get_text()),
                organization=ORGANIZACAO,
                url=BASE + f"visualizacaomateria?id={ident}",
                closes_on=None,
                score=placar_sim_nao(*(numeros + [None, None])[:2]),
            )
        )
        if len(itens) >= limite:
            break
    return itens


def placar_da_materia(pagina: str) -> Optional[dict[str, Any]]:
    """Placar na página da matéria (spans contabilizacao-favor / -contra)."""
    soup = BeautifulSoup(pagina, "html.parser")
    sim, nao = soup.select_one(".contabilizacao-favor"), soup.select_one(".contabilizacao-contra")
    if sim is None or nao is None:
        return None
    return placar_sim_nao(numero(sim.get_text()), numero(nao.get_text()))


def build_ideias(pagina: str, min_apoios: int = MIN_APOIOS) -> list[dict[str, Any]]:
    soup = BeautifulSoup(pagina, "html.parser")
    itens = []
    for li in soup.select("li.resumo-ideia"):
        a = li.select_one("a[href]")
        titulo = limpa(li.select_one("h4").get_text(" ")) if li.select_one("h4") else ""
        ident = _id(a.get("href", "") if a else "", "visualizacaoideia")
        if not ident or not titulo:
            continue
        apoios = numero(li.select_one(".contabilizacao").get_text()) if li.select_one(".contabilizacao") else None
        legenda = limpa(li.select_one(".grafico-legenda").get_text(" ")) if li.select_one(".grafico-legenda") else ""
        meta = numero(legenda.split("Meta de", 1)[1]) if "Meta de" in legenda else None
        prazo = data_por_extenso(" ".join(p.get_text(" ") for p in li.select("p")))
        if apoios is None or apoios < min_apoios:
            continue
        itens.append(
            item(
                source=SOURCE_IDEIA,
                external_id=ident,
                kind="ideia",
                title=titulo,
                organization=ORGANIZACAO,
                url=BASE + f"visualizacaoideia?id={ident}",
                closes_on=prazo,
                score={"options": [{"label": "Apoios", "count": apoios}], "goal": meta},
            )
        )
    return itens


def collect(session: Any, *, esperar=None) -> dict[str, int]:
    from mamute_scrappers.participacao_crawler.comum import (
        aprovados_abertos,
        atualizar_placar,
        pausa,
        salvar,
    )

    def _pausa():
        pausa(esperar) if esperar else pausa()

    consultas = build_consultas(baixar(CONSULTAS_URL).text)
    r1 = salvar(session, SOURCE_CONSULTA, consultas)
    _pausa()
    r2 = salvar(session, SOURCE_IDEIA, build_ideias(baixar(IDEIAS_URL).text))

    # Consultas aprovadas que saíram do recorte: placar pela página da matéria.
    na_lista = {c["external_id"] for c in consultas}
    atualizados = 0
    for ident in aprovados_abertos(session, SOURCE_CONSULTA):
        if ident in na_lista:
            continue
        _pausa()
        placar = placar_da_materia(baixar(BASE + f"visualizacaomateria?id={ident}").text)
        if placar:
            atualizar_placar(session, SOURCE_CONSULTA, ident, placar)
            atualizados += 1
    return {
        "consultas": r1["gravados"],
        "ideias": r2["gravados"],
        "placares_atualizados": atualizados,
    }
