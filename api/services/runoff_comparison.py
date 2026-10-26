"""Comparação 1º x 2º turno de presidente no painel admin (CS-160).

Lê o resultado por município que a coleta grava
(`mamute_scrappers/tse_crawler/resultados_municipio.py`) e responde, para
cada recorte, os dois turnos lado a lado e a diferença:

- **Recortes:** Brasil (inclui o exterior), macrorregião e região geográfica
  intermediária do IBGE (`ibge_municipio`), UF (exterior = "ZZ") e capital x
  interior em cada uma das 27 UFs.
- **Finalistas** são os dois mais votados no 2º turno; nada de candidato fixo
  no código. No 1º turno, os outros candidatos viram `demais_votos`.
- **Soma de votos, nunca média de percentuais.** Percentual = votos / válidos
  do recorte; recorte sem válidos tem percentual `None` (nunca 0%).
  Diferenças em pontos percentuais (pp).
- **Parcial:** enquanto houver município sem totalização encerrada, a tela
  recebe `parcial = true`, as frases saem com "Parcial: " e ficam de fora a
  comparação abstenção x margem e a estimativa de transferência (com dado
  parcial elas inventariam variação).
- **Estimativa** vem pronta de `comparacao_turnos_estimativa` (calculada na
  coleta). É inferência estatística, não dado oficial; a tela diz isso.
- **Destaques:** frases montadas por regra fixa a partir dos números, neutras
  e iguais para os dois finalistas. Nenhuma afirma causa.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

CICLO = "ele2026"
CARGO_PRESIDENTE = 1
UF_EXTERIOR = "ZZ"
TOP_DESTAQUES = 3
TOP_REGIOES_ABSTENCAO = 5

# Sigla -> (nome, preposição que abre a frase)
UFS: Dict[str, tuple[str, str]] = {
    "AC": ("Acre", "No"), "AL": ("Alagoas", "Em"), "AM": ("Amazonas", "No"),
    "AP": ("Amapá", "No"), "BA": ("Bahia", "Na"), "CE": ("Ceará", "No"),
    "DF": ("Distrito Federal", "No"), "ES": ("Espírito Santo", "No"),
    "GO": ("Goiás", "Em"), "MA": ("Maranhão", "No"), "MG": ("Minas Gerais", "Em"),
    "MS": ("Mato Grosso do Sul", "Em"), "MT": ("Mato Grosso", "Em"), "PA": ("Pará", "No"),
    "PB": ("Paraíba", "Na"), "PE": ("Pernambuco", "Em"), "PI": ("Piauí", "No"),
    "PR": ("Paraná", "No"), "RJ": ("Rio de Janeiro", "No"), "RN": ("Rio Grande do Norte", "No"),
    "RO": ("Rondônia", "Em"), "RR": ("Roraima", "Em"), "RS": ("Rio Grande do Sul", "No"),
    "SC": ("Santa Catarina", "Em"), "SE": ("Sergipe", "Em"), "SP": ("São Paulo", "Em"),
    "TO": ("Tocantins", "No"), UF_EXTERIOR: ("Exterior", "No"),
}  # fmt: skip

MACRO_PREPOSICAO = {"Norte": "No", "Nordeste": "No", "Sudeste": "No", "Sul": "No", "Centro-Oeste": "No"}

ROTULOS = {"demais": "Demais candidatos", "brancos_nulos": "Brancos e nulos", "abstencao": "Abstenção"}


def _tabela_existe(db: Session, tabela: str) -> bool:
    try:
        return sqlalchemy_inspect(db.get_bind()).has_table(tabela)
    except SQLAlchemyError:
        return False


def _iso(valor: Any) -> Optional[str]:
    if valor is None:
        return None
    return valor.isoformat() if hasattr(valor, "isoformat") else str(valor)


@dataclass
class _Totais:
    eleitorado: int = 0
    # Base da abstenção: eleitorado das seções já totalizadas (`e.est` do TSE).
    # Com a apuração em curso, comparecimento e abstenção só contam essas seções.
    eleitorado_apurado: int = 0
    comparecimento: int = 0
    abstencao: int = 0
    brancos: int = 0
    nulos: int = 0
    validos: int = 0
    votos: Dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def somar(self, linha: dict, votos: Dict[int, int]) -> None:
        for campo in ("eleitorado", "comparecimento", "abstencao", "brancos", "nulos", "validos"):
            setattr(self, campo, getattr(self, campo) + int(linha[campo] or 0))
        apurado = linha.get("eleitorado_apurado")
        self.eleitorado_apurado += int(apurado if apurado is not None else linha["eleitorado"] or 0)
        for sq, v in votos.items():
            self.votos[sq] += v


@dataclass
class _Recorte:
    tipo: str
    chave: str
    nome: str
    uf: Optional[str]
    turnos: Dict[int, _Totais] = field(default_factory=lambda: {1: _Totais(), 2: _Totais()})


def _pct(parte: int, todo: int) -> Optional[float]:
    return round(100.0 * parte / todo, 4) if todo else None


def _menos(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else round(a - b, 4)


def _fmt_pct(valor: Optional[float]) -> str:
    return "—" if valor is None else f"{valor:.1f}".replace(".", ",")


def _fmt_pp(valor: float) -> str:
    return ("+" if valor >= 0 else "-") + f"{abs(valor):.1f}".replace(".", ",")


def _fmt_int(valor: int, sinal: bool = False) -> str:
    texto = f"{abs(valor):,}".replace(",", ".")
    if sinal:
        return ("+" if valor >= 0 else "-") + texto
    return texto


def _vazio(disponivel: bool = False) -> dict[str, Any]:
    return {
        "disponivel": disponivel,
        "situacao": None,
        "finalistas": [],
        "recortes": [],
        "abstencao_x_margem": [],
        "estimativa": {"calculado_em": None, "recortes": []},
        "destaques": [],
    }


def _carregar(db: Session) -> tuple[list[dict], Dict[int, Dict[int, int]], Dict[int, str]]:
    linhas = [
        dict(r._mapping)
        for r in db.execute(
            text(
                """
                SELECT r.id, r.turno, r.uf, r.cd_tse, r.cd_ibge, r.capital, r.eleitorado,
                       r.eleitorado_apurado,
                       r.comparecimento, r.abstencao, r.brancos, r.nulos, r.validos,
                       r.totalizacao_final, r.tse_atualizado_em,
                       i.regiao_intermediaria_id, i.regiao_intermediaria_nome, i.macrorregiao
                  FROM tse_resultado_municipio r
                  LEFT JOIN ibge_municipio i ON i.cd_ibge = r.cd_ibge
                 WHERE r.ciclo = :ciclo AND r.cargo_codigo = :cargo
                """
            ),
            {"ciclo": CICLO, "cargo": CARGO_PRESIDENTE},
        ).all()
    ]
    votos: Dict[int, Dict[int, int]] = defaultdict(dict)
    nomes: Dict[int, str] = {}
    for r in db.execute(
        text(
            """
            SELECT c.resultado_id, c.sqcand, c.nome_urna, c.votos
              FROM tse_resultado_municipio_candidato c
              JOIN tse_resultado_municipio r ON r.id = c.resultado_id
             WHERE r.ciclo = :ciclo AND r.cargo_codigo = :cargo
            """
        ),
        {"ciclo": CICLO, "cargo": CARGO_PRESIDENTE},
    ).all():
        votos[int(r.resultado_id)][int(r.sqcand)] = int(r.votos or 0)
        if r.nome_urna:
            nomes[int(r.sqcand)] = r.nome_urna
    return linhas, votos, nomes


def _recortes_da_linha(linha: dict) -> Iterable[tuple[str, str, str, Optional[str]]]:
    uf = (linha["uf"] or "").upper()
    yield "brasil", "BR", "Brasil", None
    if linha["macrorregiao"]:
        yield "macrorregiao", linha["macrorregiao"], linha["macrorregiao"], None
    yield "uf", uf, UFS.get(uf, (uf, "Em"))[0], uf
    if linha["regiao_intermediaria_id"] is not None:
        yield (
            "regiao_intermediaria",
            str(linha["regiao_intermediaria_id"]),
            linha["regiao_intermediaria_nome"] or "",
            uf,
        )
    if uf != UF_EXTERIOR:
        tipo = "capital" if linha["capital"] else "interior"
        yield tipo, uf, UFS.get(uf, (uf, "Em"))[0], uf


def _totais_json(t: _Totais, finalistas: List[int], turno: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "eleitorado": t.eleitorado,
        "comparecimento": t.comparecimento,
        "abstencao": t.abstencao,
        "abstencao_pct": _pct(t.abstencao, t.eleitorado_apurado),
        "brancos": t.brancos,
        "nulos": t.nulos,
        "validos": t.validos,
        "finalistas": {
            str(sq): {"votos": t.votos.get(sq, 0), "pct": _pct(t.votos.get(sq, 0), t.validos)}
            for sq in finalistas
        },
    }
    if turno == 1:
        out["demais_votos"] = sum(v for sq, v in t.votos.items() if sq not in finalistas)
    else:
        a, b = (t.votos.get(sq, 0) for sq in finalistas)
        out["margem"] = {"votos": abs(a - b), "pp": _pct(abs(a - b), t.validos)}
    return out


def _lider(t: _Totais, candidatos: Iterable[int]) -> Optional[str]:
    melhores = sorted(((t.votos.get(sq, 0), sq) for sq in candidatos), reverse=True)
    if not melhores or melhores[0][0] == 0 or (len(melhores) > 1 and melhores[0][0] == melhores[1][0]):
        return None
    return str(melhores[0][1])


def _recorte_json(r: _Recorte, finalistas: List[int]) -> dict[str, Any]:
    t1, t2 = r.turnos[1], r.turnos[2]
    j1, j2 = _totais_json(t1, finalistas, 1), _totais_json(t2, finalistas, 2)
    lider1 = _lider(t1, t1.votos.keys())
    vencedor2 = _lider(t2, finalistas)
    return {
        "tipo": r.tipo,
        "chave": r.chave,
        "nome": r.nome,
        "uf": r.uf,
        "turno1": j1,
        "turno2": j2,
        "diferenca": {
            "finalistas": {
                str(sq): {
                    "votos": j2["finalistas"][str(sq)]["votos"] - j1["finalistas"][str(sq)]["votos"],
                    "pp": _menos(j2["finalistas"][str(sq)]["pct"], j1["finalistas"][str(sq)]["pct"]),
                }
                for sq in finalistas
            },
            "abstencao": t2.abstencao - t1.abstencao,
            "abstencao_pp": _menos(j2["abstencao_pct"], j1["abstencao_pct"]),
            "brancos_nulos": (t2.brancos + t2.nulos) - (t1.brancos + t1.nulos),
            "comparecimento": t2.comparecimento - t1.comparecimento,
        },
        "lider_turno1": lider1,
        "vencedor_turno2": vencedor2,
        "mudou_lider": bool(lider1 and vencedor2 and lider1 != vencedor2),
    }


def _onde(recorte: dict) -> str:
    """"Em Santa Catarina", "Na região de Blumenau (SC)", "No Sul"."""
    tipo = recorte["tipo"]
    if tipo == "brasil":
        return "No Brasil"
    if tipo == "macrorregiao":
        return f"{MACRO_PREPOSICAO.get(recorte['nome'], 'Em')} {recorte['nome']}"
    if tipo == "regiao_intermediaria":
        return f"Na região de {recorte['nome']} ({recorte['uf']})"
    nome, prep = UFS.get(recorte["chave"], (recorte["nome"], "Em"))
    return f"{prep} {nome}"


def _destaques(
    recortes: List[dict],
    finalistas: List[int],
    nomes: Dict[int, str],
    abstencao_x_margem: List[dict],
    parcial: bool,
) -> List[dict[str, Any]]:
    frases: List[dict[str, Any]] = []

    def add(tipo: str, recorte: dict, texto: str) -> None:
        frases.append({"tipo": tipo, "texto": texto, "recorte": recorte["tipo"], "chave": recorte["chave"]})

    por_tipo: Dict[str, List[dict]] = defaultdict(list)
    for r in recortes:
        por_tipo[r["tipo"]].append(r)

    for brasil in por_tipo["brasil"]:
        for sq in finalistas:
            p1 = brasil["turno1"]["finalistas"][str(sq)]["pct"]
            p2 = brasil["turno2"]["finalistas"][str(sq)]["pct"]
            pp = brasil["diferenca"]["finalistas"][str(sq)]["pp"]
            if pp is None:
                continue
            add("brasil", brasil,
                f"No Brasil, {nomes.get(sq, sq)} foi de {_fmt_pct(p1)}% para {_fmt_pct(p2)}% "
                f"dos votos válidos ({_fmt_pp(pp)} pp).")
        a1, a2 = brasil["turno1"]["abstencao_pct"], brasil["turno2"]["abstencao_pct"]
        if not parcial and a1 is not None and a2 is not None:
            add("brasil", brasil, f"A abstenção foi de {_fmt_pct(a1)}% para {_fmt_pct(a2)}% do eleitorado.")

    for tipo, tipo_frase in (("uf", "maior_ganho_uf"), ("regiao_intermediaria", "maior_ganho_regiao")):
        candidatos = [r for r in por_tipo[tipo] if r["chave"] != UF_EXTERIOR]
        for sq in finalistas:
            com_ganho = [
                r for r in candidatos
                if (r["diferenca"]["finalistas"][str(sq)]["pp"] or 0) > 0
            ]
            com_ganho.sort(key=lambda r: -r["diferenca"]["finalistas"][str(sq)]["pp"])
            for r in com_ganho[:TOP_DESTAQUES]:
                f1 = r["turno1"]["finalistas"][str(sq)]["pct"]
                f2 = r["turno2"]["finalistas"][str(sq)]["pct"]
                pp = r["diferenca"]["finalistas"][str(sq)]["pp"]
                add(tipo_frase, r,
                    f"{_onde(r)}, {nomes.get(sq, sq)} foi de {_fmt_pct(f1)}% para {_fmt_pct(f2)}% "
                    f"dos votos válidos ({_fmt_pp(pp)} pp).")

    # Com apuração em curso não existe "venceu": as frases de líder esperam o fim.
    for tipo in ("uf", "regiao_intermediaria") if not parcial else ():
        for r in por_tipo[tipo]:
            if r["mudou_lider"]:
                lider = nomes.get(int(r["lider_turno1"]), r["lider_turno1"])
                vencedor = nomes.get(int(r["vencedor_turno2"]), r["vencedor_turno2"])
                add("mudou_lider", r, f"{_onde(r)}, {lider} liderou o 1º turno e {vencedor} venceu o 2º.")

    capitais = {r["chave"]: r for r in por_tipo["capital"]}
    interiores = {r["chave"]: r for r in por_tipo["interior"]}
    contrastes = []
    for uf, cap in capitais.items():
        interior = interiores.get(uf)
        if interior is None:
            continue
        for sq in finalistas:
            dc = cap["diferenca"]["finalistas"][str(sq)]["pp"]
            di = interior["diferenca"]["finalistas"][str(sq)]["pp"]
            if dc is None or di is None:
                continue
            contrastes.append((abs(dc - di), uf, sq, dc, di, cap))
    contrastes.sort(key=lambda c: -c[0])
    vistos: set[str] = set()
    for _, uf, sq, dc, di, cap in contrastes:
        if uf in vistos or len(vistos) >= TOP_DESTAQUES:
            continue
        vistos.add(uf)
        uf_recorte = {"tipo": "uf", "chave": uf, "nome": cap["nome"]}
        add("capital_interior", cap,
            f"{_onde(uf_recorte)}, {nomes.get(sq, sq)} variou {_fmt_pp(dc)} pp na capital "
            f"e {_fmt_pp(di)} pp no interior.")

    if not parcial:
        acima = [a for a in abstencao_x_margem if a["maior_que_margem"]]
        ufs = [a for a in acima if a["tipo"] == "uf"]
        regioes = sorted(
            (a for a in acima if a["tipo"] == "regiao_intermediaria"),
            key=lambda a: -abs(a["variacao_abstencao"]),
        )[:TOP_REGIOES_ABSTENCAO]
        for a in ufs + regioes:
            add("abstencao_margem", a,
                f"{_onde(a)}, a abstenção variou {_fmt_int(a['variacao_abstencao'], sinal=True)} eleitores "
                f"entre os turnos; a margem final foi de {_fmt_int(a['margem_votos'])} votos.")

    if parcial:
        for f in frases:
            f["texto"] = "Parcial: " + f["texto"]
    return frases


def _estimativa(db: Session, nomes: Dict[int, str]) -> dict[str, Any]:
    if not _tabela_existe(db, "comparacao_turnos_estimativa"):
        return {"calculado_em": None, "recortes": []}
    rows = db.execute(
        text(
            """
            SELECT recorte, chave, origem, destino, proporcao, votos, municipios, calculado_em
              FROM comparacao_turnos_estimativa
             WHERE ciclo = :ciclo AND cargo_codigo = :cargo
             ORDER BY CASE recorte WHEN 'brasil' THEN 0 ELSE 1 END, chave
            """
        ),
        {"ciclo": CICLO, "cargo": CARGO_PRESIDENTE},
    ).all()

    def rotulo(chave: str) -> str:
        if chave in ROTULOS:
            return ROTULOS[chave]
        return nomes.get(int(chave), chave) if chave.isdigit() else chave

    recortes: Dict[tuple[str, str], dict[str, Any]] = {}
    calculado_em = None
    for r in rows:
        bloco = recortes.setdefault(
            (r.recorte, r.chave),
            {"recorte": r.recorte, "chave": r.chave, "municipios": int(r.municipios), "linhas": []},
        )
        bloco["linhas"].append(
            {
                "origem": r.origem,
                "origem_nome": rotulo(r.origem),
                "destino": r.destino,
                "destino_nome": rotulo(r.destino),
                "proporcao": float(r.proporcao),
                "votos": int(r.votos),
            }
        )
        calculado_em = calculado_em or _iso(r.calculado_em)
    return {"calculado_em": calculado_em, "recortes": list(recortes.values())}


def runoff_comparison(db: Session) -> dict[str, Any]:
    if not (_tabela_existe(db, "tse_resultado_municipio") and _tabela_existe(db, "ibge_municipio")):
        return _vazio()
    linhas, votos, nomes = _carregar(db)
    if not any(l["turno"] == 2 for l in linhas) or not any(l["turno"] == 1 for l in linhas):
        return _vazio()

    total2: Dict[int, int] = defaultdict(int)
    for l in linhas:
        if l["turno"] == 2:
            for sq, v in votos.get(int(l["id"]), {}).items():
                total2[sq] += v
    finalistas = [sq for sq, _ in sorted(total2.items(), key=lambda kv: -kv[1])[:2]]
    if len(finalistas) < 2:
        return _vazio()

    situacao: Dict[int, Dict[str, int]] = {1: {"municipios": 0, "totalizados": 0}, 2: {"municipios": 0, "totalizados": 0}}
    atualizado = None
    recortes: Dict[tuple[str, str], _Recorte] = {}
    for l in linhas:
        turno = int(l["turno"])
        situacao[turno]["municipios"] += 1
        situacao[turno]["totalizados"] += 1 if l["totalizacao_final"] else 0
        # O exterior vem com a hora local de cada país (Tóquio "no futuro"): fica de fora.
        if l["tse_atualizado_em"] is not None and (l["uf"] or "").upper() != UF_EXTERIOR:
            iso = _iso(l["tse_atualizado_em"])
            atualizado = max(atualizado, iso) if atualizado else iso
        for tipo, chave, nome, uf in _recortes_da_linha(l):
            recorte = recortes.setdefault((tipo, chave), _Recorte(tipo, chave, nome, uf))
            recorte.turnos[turno].somar(l, votos.get(int(l["id"]), {}))

    # Município sem arquivo numa rodada (404, TSE fora) não tem linha: menos
    # municípios no 2º turno que no 1º também é parcial.
    parcial = any(s["totalizados"] < s["municipios"] for s in situacao.values()) or (
        situacao[2]["municipios"] < situacao[1]["municipios"]
    )
    ordem = {"brasil": 0, "macrorregiao": 1, "uf": 2, "regiao_intermediaria": 3, "capital": 4, "interior": 5}
    recortes_json = [
        _recorte_json(r, finalistas)
        for r in sorted(recortes.values(), key=lambda r: (ordem[r.tipo], r.uf or "", r.nome))
    ]

    abstencao_x_margem: List[dict[str, Any]] = []
    if not parcial:
        for r in recortes_json:
            if r["tipo"] not in ("uf", "regiao_intermediaria"):
                continue
            variacao = r["diferenca"]["abstencao"]
            margem = r["turno2"]["margem"]["votos"]
            abstencao_x_margem.append(
                {
                    "tipo": r["tipo"],
                    "chave": r["chave"],
                    "nome": r["nome"],
                    "uf": r["uf"],
                    "variacao_abstencao": variacao,
                    "margem_votos": margem,
                    "maior_que_margem": abs(variacao) > margem,
                }
            )

    estimativa = _estimativa(db, nomes) if not parcial else {"calculado_em": None, "recortes": []}

    return {
        "disponivel": True,
        "situacao": {
            "turno1": situacao[1],
            "turno2": situacao[2],
            "parcial": parcial,
            "tse_atualizado_em": atualizado,
        },
        "finalistas": [{"sqcand": str(sq), "nome": nomes.get(sq, str(sq))} for sq in finalistas],
        "recortes": recortes_json,
        "abstencao_x_margem": abstencao_x_margem,
        "estimativa": estimativa,
        "destaques": _destaques(recortes_json, finalistas, nomes, abstencao_x_margem, parcial),
    }
