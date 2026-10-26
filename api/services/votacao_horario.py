"""Votos depois das 17h (CS-163), lidos das tabelas que a carga grava
(`mamute_scrappers/tse_crawler/votacao_horario.py carregar`).

- Eleições: 2º turno de 2026 (a que interessa) e duas referências sem
  prorrogação, o 1º turno de 2026 e o 2º turno de 2022. Só entram as que já
  foram carregadas.
- `votos_apos_17` é o medido nos logs lidos; `apos_17_total_estimado` soma a
  estimativa das seções não lidas (ver a carga).
- Margem e pior caso: `poderia_inverter` é verdadeiro quando os votos depois
  das 17h (com a estimativa) somam pelo menos a diferença entre os dois
  primeiros. É o teste do pior caso: supõe que todos esses votos foram para
  quem venceu. Não diz em quem os votos foram (o voto é secreto).
- Tabela ausente (a migration roda depois que os containers sobem) =
  `disponivel: False`.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import bindparam, text
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

try:
    from .runoff_comparison import UFS
except ImportError:  # execução dentro de api/
    from services.runoff_comparison import UFS

ELEICOES = [
    {"chave": "2026-2t", "rotulo": "2º turno 2026", "dia": "2026-10-25", "ano": 2026},
    {"chave": "2026-1t", "rotulo": "1º turno 2026", "dia": "2026-10-04", "ano": 2026},
    {"chave": "2022-2t", "rotulo": "2º turno 2022", "dia": "2022-10-30", "ano": 2022},
]
PRINCIPAL = "2026-2t"
# Campos de cada cidade na lista (a lista pode ter milhares: o resto fica no detalhe).
CAMPOS_CIDADE = (
    "secoes", "comparecimento", "votos", "secoes_tardias", "secoes_voto_apos_17", "votos_apos_17",
    "de_17_a_19", "apos_19", "ultimo_voto", "encerramento_max", "apos_17_pct", "margem",
)  # fmt: skip
FINALISTAS = ("13", "22")
_MINUSCULAS = {"de", "da", "do", "das", "dos", "e", "d'"}


def _nome_proprio(nome: str) -> str:
    palavras = (nome or "").lower().split()
    return " ".join(p if i and p in _MINUSCULAS else p[:1].upper() + p[1:] for i, p in enumerate(palavras))


def _tem_tabela(db: Session, nome: str) -> bool:
    try:
        return sqlalchemy_inspect(db.get_bind()).has_table(nome)
    except SQLAlchemyError:
        return False


def _dados(valor: Any) -> Dict[str, Any]:
    return json.loads(valor) if isinstance(valor, str) else dict(valor or {})


def _completar(linha: Dict[str, Any]) -> Dict[str, Any]:
    comp = linha.get("comparecimento") or 0
    medido = linha.get("votos_apos_17") or 0
    total = round(medido + (linha.get("apos_17_estimado_nao_lidas") or 0.0), 2)
    votos = linha.get("votos") or {}
    a, b = (votos.get(n, 0) for n in FINALISTAS)
    margem = {"vencedor": FINALISTAS[0] if a >= b else FINALISTAS[1], "votos": abs(a - b)}
    return {
        **linha,
        "apos_17_total_estimado": total,
        "apos_17_pct": round(100 * medido / comp, 3) if comp else None,
        "margem": margem,
        "poderia_inverter": total >= margem["votos"],
    }


def _finalistas(db: Session, anos: List[int]) -> Dict[int, Dict[str, str]]:
    nomes: Dict[int, Dict[str, str]] = {ano: {} for ano in anos}
    if not _tem_tabela(db, "candidacy"):
        return nomes
    linhas = db.execute(
        text(
            "SELECT election_year, ballot_number, MIN(ballot_name) FROM candidacy "
            "WHERE office_code = 1 AND ballot_number IN (13, 22) AND election_year IN :anos "
            "GROUP BY election_year, ballot_number"
        ).bindparams(bindparam("anos", expanding=True)),
        {"anos": anos},
    ).all()
    for ano, numero, nome in linhas:
        nomes[ano][str(numero)] = _nome_proprio(nome)
    return nomes


def votacao_horario(db: Session) -> Dict[str, Any]:
    if not _tem_tabela(db, "votacao_horario_agregado"):
        return {"disponivel": False}
    linhas = db.execute(
        text(
            "SELECT eleicao, nivel, uf, municipio, nome, latitude, longitude, dados, atualizado_em "
            "FROM votacao_horario_agregado"
        )
    ).all()
    if not linhas:
        return {"disponivel": False}

    carregadas = {l.eleicao for l in linhas}
    eleicoes = [e for e in ELEICOES if e["chave"] in carregadas]
    nomes = _finalistas(db, sorted({e["ano"] for e in eleicoes}))
    finalistas = {
        e["chave"]: [{"numero": n, "nome": nomes.get(e["ano"], {}).get(n, n)} for n in FINALISTAS] for e in eleicoes
    }

    brasil: Dict[str, Any] = {}
    ufs: Dict[str, Dict[str, Any]] = {}
    municipios: Dict[tuple, Dict[str, Dict[str, Any]]] = {}
    meta: Dict[tuple, Any] = {}
    atualizado = None
    for l in linhas:
        dados = _completar(_dados(l.dados))
        atualizado = max(atualizado, l.atualizado_em) if atualizado else l.atualizado_em
        if l.nivel == "brasil":
            brasil[l.eleicao] = dados
        elif l.nivel == "uf":
            sigla = l.uf.upper()
            u = ufs.setdefault(sigla, {"uf": sigla, "nome": UFS.get(sigla, (sigla,))[0], "por_eleicao": {}})
            u["por_eleicao"][l.eleicao] = dados
        else:
            chave = (l.uf.upper(), l.municipio)
            municipios.setdefault(chave, {})[l.eleicao] = dados
            if l.eleicao == PRINCIPAL or chave not in meta:
                meta[chave] = l

    lista_mun = []
    for chave, por in municipios.items():
        principal = por.get(PRINCIPAL)
        if not principal or not principal["votos_apos_17"]:
            continue
        m = meta[chave]
        lista_mun.append({
            "uf": chave[0], "municipio": chave[1], "nome": _nome_proprio(m.nome),
            "latitude": m.latitude, "longitude": m.longitude, **{k: principal[k] for k in CAMPOS_CIDADE},
            "base": {
                e: {"votos_apos_17": d["votos_apos_17"], "comparecimento": d["comparecimento"], "apos_17_pct": d["apos_17_pct"]}
                for e, d in por.items() if e != PRINCIPAL
            },
        })  # fmt: skip
    lista_mun.sort(key=lambda m: -m["votos_apos_17"])

    return {
        "disponivel": True,
        "eleicoes": [{k: e[k] for k in ("chave", "rotulo", "dia")} for e in eleicoes],
        "principal": PRINCIPAL,
        "finalistas": finalistas,
        "brasil": brasil,
        "ufs": sorted(ufs.values(), key=lambda u: u["nome"]),
        "municipios": lista_mun,
        "atualizado_em": str(atualizado) if atualizado else None,
    }


def locais_do_municipio(db: Session, uf: str, municipio: int) -> Optional[Dict[str, Any]]:
    """Locais de votação (escolas) do município com voto depois das 17h no
    2º turno de 2026, com bairro, endereço e coordenadas."""
    if not _tem_tabela(db, "votacao_horario_local"):
        return None
    m = db.execute(
        text(
            "SELECT nome, latitude, longitude, dados FROM votacao_horario_agregado "
            "WHERE eleicao = :e AND nivel = 'municipio' AND uf = :uf AND municipio = :mu"
        ),
        {"e": PRINCIPAL, "uf": uf.lower(), "mu": municipio},
    ).first()
    if m is None:
        return None
    locais = db.execute(
        text(
            "SELECT zona, local, nome, bairro, endereco, latitude, longitude, votos_apos_17, dados "
            "FROM votacao_horario_local WHERE eleicao = :e AND uf = :uf AND municipio = :mu "
            "ORDER BY votos_apos_17 DESC, zona, local"
        ),
        {"e": PRINCIPAL, "uf": uf.lower(), "mu": municipio},
    ).all()
    return {
        "municipio": {"uf": uf.upper(), "municipio": municipio, "nome": _nome_proprio(m.nome),
                      "latitude": m.latitude, "longitude": m.longitude, **_completar(_dados(m.dados))},
        "locais": [
            {**_dados(l.dados), "zona": l.zona, "local": l.local, "nome": l.nome, "bairro": l.bairro,
             "endereco": l.endereco, "latitude": l.latitude, "longitude": l.longitude,
             "votos_apos_17": l.votos_apos_17}
            for l in locais
        ],
    }  # fmt: skip
