"""Resultado de presidente por município e por local de votação, para o zoom
dos mapas da página Eleições 2026 (CS-164).

- Municípios: todos de uma vez, em lista enxuta (arrays na ordem de `campos`),
  com os dois turnos de 2026. Vem de `votacao_horario_agregado` (nível
  município), que a carga dos boletins de urna grava.
- Locais de votação (escolas): só os da área visível do mapa (`w, s, e, n`),
  com o 1º turno do mesmo local ao lado. Área grande demais (zoom de estado
  ou maior) não lista: o mapa só pede no zoom de cidade.

Finalistas 13 e 22, como em `votacao_horario.py`. Tabela ausente =
`disponivel: False` (a migration roda depois que os containers sobem).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

try:
    from .votacao_horario import _nome_proprio
except ImportError:  # execução dentro de api/
    from services.votacao_horario import _nome_proprio

PRINCIPAL = "2026-2t"
REFERENCIA = "2026-1t"
# Área máxima pedida de uma vez, em graus (lado). Uma capital cabe com folga.
LADO_MAXIMO = 3.0
LIMITE_LOCAIS = 5000

CAMPOS_MUNICIPIO = ["uf", "municipio", "nome", "lat", "lon", "v13", "v22", "validos", "v13_1t", "v22_1t", "validos_1t"]
CAMPOS_LOCAL = [
    "uf", "municipio", "zona", "local", "nome", "bairro", "lat", "lon",
    "v13", "v22", "validos", "v13_1t", "v22_1t", "validos_1t",
]  # fmt: skip


def _tem_tabela(db: Session, nome: str) -> bool:
    try:
        return sqlalchemy_inspect(db.get_bind()).has_table(nome)
    except SQLAlchemyError:
        return False


def _totais(dados: Any) -> List[Optional[int]]:
    votos = (json.loads(dados) if isinstance(dados, str) else dict(dados or {})).get("votos") or {}
    validos = sum(v for k, v in votos.items() if k not in ("branco", "nulo"))
    return [votos.get("13", 0), votos.get("22", 0), validos]


def municipios_resultado(db: Session) -> Dict[str, Any]:
    if not _tem_tabela(db, "votacao_horario_agregado"):
        return {"disponivel": False}
    linhas = db.execute(
        text(
            "SELECT eleicao, uf, municipio, nome, latitude, longitude, dados FROM votacao_horario_agregado "
            "WHERE nivel = 'municipio' AND eleicao IN (:p, :r)"
        ),
        {"p": PRINCIPAL, "r": REFERENCIA},
    ).all()
    por: Dict[tuple, Dict[str, Any]] = {}
    for l in linhas:
        m = por.setdefault((l.uf, l.municipio), {})
        m[l.eleicao] = l
    saida = []
    for (uf, mu), m in sorted(por.items()):
        base = m.get(PRINCIPAL) or m.get(REFERENCIA)
        if base.latitude is None or base.longitude is None:
            continue
        p = _totais(m[PRINCIPAL].dados) if PRINCIPAL in m else [None, None, None]
        r = _totais(m[REFERENCIA].dados) if REFERENCIA in m else [None, None, None]
        saida.append([uf.upper(), mu, _nome_proprio(base.nome), base.latitude, base.longitude, *p, *r])
    return {"disponivel": bool(saida), "campos": CAMPOS_MUNICIPIO, "municipios": saida}


def locais_resultado(db: Session, w: float, s: float, e: float, n: float) -> Dict[str, Any]:
    if not _tem_tabela(db, "resultado_local_votacao"):
        return {"locais": [], "truncado": False}
    if (e - w) > LADO_MAXIMO or (n - s) > LADO_MAXIMO:
        return {"locais": [], "truncado": True}
    linhas = db.execute(
        text(
            "SELECT p.uf, p.municipio, p.zona, p.local, p.nome, p.bairro, p.latitude, p.longitude, "
            "p.votos_13, p.votos_22, p.validos, r.votos_13 AS r13, r.votos_22 AS r22, r.validos AS rval "
            "FROM resultado_local_votacao p "
            "LEFT JOIN resultado_local_votacao r ON r.eleicao = :r AND r.uf = p.uf AND r.municipio = p.municipio "
            "AND r.zona = p.zona AND r.local = p.local "
            "WHERE p.eleicao = :p AND p.latitude BETWEEN :s AND :n AND p.longitude BETWEEN :w AND :e "
            "LIMIT :lim"
        ),
        {"p": PRINCIPAL, "r": REFERENCIA, "s": s, "n": n, "w": w, "e": e, "lim": LIMITE_LOCAIS + 1},
    ).all()
    truncado = len(linhas) > LIMITE_LOCAIS
    return {
        "campos": CAMPOS_LOCAL,
        "locais": [
            [l.uf.upper(), l.municipio, l.zona, l.local, l.nome, l.bairro, l.latitude, l.longitude,
             l.votos_13, l.votos_22, l.validos, l.r13, l.r22, l.rval]
            for l in linhas[:LIMITE_LOCAIS]
        ],
        "truncado": truncado,
    }  # fmt: skip
