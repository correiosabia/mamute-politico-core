"""Estimativa de transferencia de votos entre o 1o e o 2o turno (CS-160).

Pergunta: dos eleitores de cada opcao no 1o turno, que fracao foi para cada
opcao no 2o? Ninguem sabe isso de verdade (o voto e secreto); o que da e uma
**estimativa estatistica**, nunca dado oficial:

- cada municipio e uma observacao; `x` = parcela do eleitorado em cada origem
  no 1o turno, `y` = parcela em cada destino no 2o;
- procura a matriz `M` (origens x destinos) que melhor explica `y ≈ x · M`,
  ponderando pelo eleitorado, com cada linha de `M` somando 1 e sem negativo
  (regressao ecologica com restricoes, resolvida por gradiente projetado);
- limitacao conhecida (falacia ecologica): supoe que eleitores da mesma
  origem se comportam igual em todo o recorte.

Origens: os dois finalistas, os 3 eliminados com mais votos no Brasil, os
demais somados, brancos + nulos e abstencao. Destinos: os dois finalistas,
brancos + nulos e abstencao. Recortes: Brasil (inclui o exterior) e as
macrorregioes do IBGE. UF e regiao intermediaria ficam de fora: varias tem
poucos municipios e o resultado seria instavel.

So calcula com os dois turnos 100% totalizados; antes disso nao grava nem
apaga nada.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ELIMINADOS_SEPARADOS = 3
DEMAIS = "demais"
BRANCOS_NULOS = "brancos_nulos"
ABSTENCAO = "abstencao"
MAX_ITERACOES = 5000
TOLERANCIA = 1e-10


def _projetar_simplex(linhas: np.ndarray) -> np.ndarray:
    """Projeta cada linha no simplex (soma 1, sem negativo). Duchi et al. 2008."""
    n, k = linhas.shape
    ordenado = -np.sort(-linhas, axis=1)
    acumulado = np.cumsum(ordenado, axis=1) - 1.0
    indices = np.arange(1, k + 1)
    condicao = ordenado - acumulado / indices > 0
    rho = k - 1 - np.argmax(condicao[:, ::-1], axis=1)
    theta = acumulado[np.arange(n), rho] / (rho + 1)
    return np.maximum(linhas - theta[:, None], 0.0)


def estimar_matriz(x: np.ndarray, y: np.ndarray, pesos: np.ndarray) -> np.ndarray:
    """Matriz origens x destinos que minimiza sum_i w_i ||x_i M - y_i||^2,
    com cada linha de M no simplex. Gradiente projetado acelerado (FISTA)."""
    w = pesos / pesos.sum()
    xtw = x.T * w
    gram = xtw @ x
    xty = xtw @ y
    lipschitz = 2.0 * float(np.linalg.eigvalsh(gram).max())
    if lipschitz <= 0:
        raise ValueError("sem variacao entre as observacoes")
    passo = 1.0 / lipschitz
    k, m = x.shape[1], y.shape[1]
    matriz = np.full((k, m), 1.0 / m)
    auxiliar, t = matriz.copy(), 1.0
    for _ in range(MAX_ITERACOES):
        gradiente = 2.0 * (gram @ auxiliar - xty)
        nova = _projetar_simplex(auxiliar - passo * gradiente)
        t_novo = (1.0 + (1.0 + 4.0 * t * t) ** 0.5) / 2.0
        auxiliar = nova + ((t - 1.0) / t_novo) * (nova - matriz)
        variacao = float(np.abs(nova - matriz).max())
        matriz, t = nova, t_novo
        if variacao < TOLERANCIA:
            break
    return matriz


def _tudo_totalizado(session: Session, ciclo: str, cargo: int) -> bool:
    row = session.execute(
        text(
            """
            SELECT count(DISTINCT turno) AS turnos,
                   sum(CASE WHEN totalizacao_final THEN 0 ELSE 1 END) AS abertos
              FROM tse_resultado_municipio
             WHERE ciclo = :ciclo AND cargo_codigo = :cargo
            """
        ),
        {"ciclo": ciclo, "cargo": cargo},
    ).one()
    return int(row.turnos or 0) == 2 and int(row.abertos or 0) == 0


def _votos_por_municipio(session: Session, ciclo: str, cargo: int, turno: int) -> Dict[str, Dict[int, int]]:
    rows = session.execute(
        text(
            """
            SELECT r.cd_tse, c.sqcand, c.votos
              FROM tse_resultado_municipio r
              JOIN tse_resultado_municipio_candidato c ON c.resultado_id = r.id
             WHERE r.ciclo = :ciclo AND r.cargo_codigo = :cargo AND r.turno = :turno
            """
        ),
        {"ciclo": ciclo, "cargo": cargo, "turno": turno},
    ).all()
    votos: Dict[str, Dict[int, int]] = defaultdict(dict)
    for r in rows:
        votos[r.cd_tse][int(r.sqcand)] = int(r.votos or 0)
    return votos


def _resumos(session: Session, ciclo: str, cargo: int, turno: int) -> Dict[str, dict]:
    rows = session.execute(
        text(
            """
            SELECT r.cd_tse, r.eleitorado, r.abstencao, r.brancos, r.nulos, i.macrorregiao
              FROM tse_resultado_municipio r
              LEFT JOIN ibge_municipio i ON i.cd_ibge = r.cd_ibge
             WHERE r.ciclo = :ciclo AND r.cargo_codigo = :cargo AND r.turno = :turno
            """
        ),
        {"ciclo": ciclo, "cargo": cargo, "turno": turno},
    ).all()
    return {r.cd_tse: dict(r._mapping) for r in rows}


def calcular_estimativas(session: Session, ciclo: str = "ele2026", cargo: int = 1) -> int:
    """Recalcula e grava `comparacao_turnos_estimativa`. Devolve linhas gravadas
    (0 quando algum municipio ainda nao foi totalizado)."""
    if not _tudo_totalizado(session, ciclo, cargo):
        logger.info("Estimativa: aguardando a totalizacao de todos os municipios.")
        return 0

    votos1 = _votos_por_municipio(session, ciclo, cargo, 1)
    votos2 = _votos_por_municipio(session, ciclo, cargo, 2)
    resumo1 = _resumos(session, ciclo, cargo, 1)
    resumo2 = _resumos(session, ciclo, cargo, 2)

    total2: Dict[int, int] = defaultdict(int)
    for votos in votos2.values():
        for sq, v in votos.items():
            total2[sq] += v
    finalistas = [sq for sq, _ in sorted(total2.items(), key=lambda kv: -kv[1])[:2]]
    total1: Dict[int, int] = defaultdict(int)
    for votos in votos1.values():
        for sq, v in votos.items():
            total1[sq] += v
    eliminados = [
        sq for sq, _ in sorted(total1.items(), key=lambda kv: -kv[1]) if sq not in finalistas
    ][:ELIMINADOS_SEPARADOS]
    if len(finalistas) < 2:
        return 0

    origens: List[str] = [str(s) for s in finalistas + eliminados] + [DEMAIS, BRANCOS_NULOS, ABSTENCAO]
    destinos: List[str] = [str(s) for s in finalistas] + [BRANCOS_NULOS, ABSTENCAO]

    municipios = sorted(set(resumo1) & set(resumo2))
    xs, ys, pesos, absolutos, macro = [], [], [], [], []
    for cd in municipios:
        r1, r2 = resumo1[cd], resumo2[cd]
        el1, el2 = int(r1["eleitorado"] or 0), int(r2["eleitorado"] or 0)
        if el1 <= 0 or el2 <= 0:
            continue
        v1 = votos1.get(cd, {})
        separados = [v1.get(sq, 0) for sq in finalistas + eliminados]
        demais = sum(v for sq, v in v1.items() if sq not in finalistas and sq not in eliminados)
        linha1 = separados + [
            demais,
            int(r1["brancos"] or 0) + int(r1["nulos"] or 0),
            int(r1["abstencao"] or 0),
        ]
        v2 = votos2.get(cd, {})
        linha2 = [v2.get(sq, 0) for sq in finalistas] + [
            int(r2["brancos"] or 0) + int(r2["nulos"] or 0),
            int(r2["abstencao"] or 0),
        ]
        xs.append([v / el1 for v in linha1])
        ys.append([v / el2 for v in linha2])
        pesos.append(el2)
        absolutos.append(linha1)
        macro.append(r1["macrorregiao"])

    x, y, w, a = np.array(xs), np.array(ys), np.array(pesos, dtype=float), np.array(absolutos, dtype=float)
    macro_arr = np.array(macro, dtype=object)
    recortes: List[tuple[str, str, np.ndarray]] = [("brasil", "BR", np.ones(len(x), dtype=bool))]
    for nome in sorted({m for m in macro if m}):
        recortes.append(("macrorregiao", nome, macro_arr == nome))

    linhas = []
    for recorte, chave, mascara in recortes:
        if mascara.sum() < len(origens) * 2:
            logger.info("Estimativa: %s/%s com poucos municipios; pulando.", recorte, chave)
            continue
        matriz = estimar_matriz(x[mascara], y[mascara], w[mascara])
        votos_origem = a[mascara].sum(axis=0)
        for i, origem in enumerate(origens):
            for j, destino in enumerate(destinos):
                proporcao = float(matriz[i, j])
                linhas.append(
                    {
                        "ciclo": ciclo,
                        "cargo": cargo,
                        "recorte": recorte,
                        "chave": chave,
                        "origem": origem,
                        "destino": destino,
                        "proporcao": round(proporcao, 4),
                        "votos": int(round(proporcao * votos_origem[i], -3)),
                        "municipios": int(mascara.sum()),
                    }
                )

    session.execute(
        text("DELETE FROM comparacao_turnos_estimativa WHERE ciclo = :ciclo AND cargo_codigo = :cargo"),
        {"ciclo": ciclo, "cargo": cargo},
    )
    if linhas:
        session.execute(
            text(
                "INSERT INTO comparacao_turnos_estimativa "
                "(ciclo, cargo_codigo, recorte, chave, origem, destino, proporcao, votos, municipios) "
                "VALUES (:ciclo, :cargo, :recorte, :chave, :origem, :destino, :proporcao, :votos, :municipios)"
            ),
            linhas,
        )
    session.commit()
    return len(linhas)
