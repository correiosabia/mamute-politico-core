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

Origens: os dois finalistas, os eliminados que tem ponto de partida no
arquivo de pesquisas (abaixo), os outros candidatos somados, brancos + nulos e
abstencao. Destinos: os dois finalistas, brancos + nulos e abstencao.
Recortes: as macrorregioes do IBGE e o Brasil. O Brasil e a soma das
macrorregioes e do exterior, cada uma estimada separada e pesada pelos votos
de cada origem (estratificacao): numa regressao nacional unica, o comparecimento
novo do Nordeste, onde a abstencao e maior, ia quase todo para um finalista
(vies de agregacao). UF e regiao intermediaria ficam de fora: varias tem poucos
municipios e o resultado seria instavel.

Estabilidade e incerteza (CS-161, conferido com o 2o turno de 2026):

- **Sem ponto de partida, a regressao cai nos extremos.** Com so o dado
  municipal, origem pequena (2% a 3% dos votos) ia para 0% ou 100% (ex.:
  "eleitor do Cury: 52% Lula, 0% Flavio") e finalista ficava com 100% de
  fidelidade, sem nenhuma troca de lado. Sao artefatos do metodo.
- **Ponto de partida (prior) por linha.** O arquivo
  `dados/prior_transferencia_<ciclo>.json` traz, para cada origem, a
  distribuicao que pesquisas e a eleicao anterior indicam, um peso (`forca`) e
  a fonte. A estimativa e o MAP de um modelo com esse prior: minimiza o erro
  nos municipios + `forca` x escala x distancia ao prior. O dado municipal
  move o numero para onde ele tem informacao; onde nao tem, fica perto da
  pesquisa. Origem sem linha no arquivo usa a distribuicao media do 2o turno
  no recorte, com `REGULARIZACAO`; eliminado sem linha entra em "outros".
- **Faixa de incerteza.** `proporcao_min`/`proporcao_max` juntam o bootstrap
  de municipios (p5 a p95) e o resultado com 1/4 e com 4 vezes a `forca`.
  So o bootstrap engana (brancos e nulos davam faixa de 4 pontos e o valor ia
  de 58% a 81% conforme o ajuste). A tela marca faixa larga como incerta.

So calcula com os dois turnos 100% totalizados; antes disso nao grava nem
apaga nada.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

OUTROS_CANDIDATOS = "outros_candidatos"
EXTERIOR = "Exterior"
BRANCOS_NULOS = "brancos_nulos"
ABSTENCAO = "abstencao"
MAX_ITERACOES = 5000
TOLERANCIA = 1e-10
# Fracao da escala do problema (traco de XtWX / origens). Calibrado com o 2o
# turno de 2026: 0,01 mantem as linhas grandes (finalista -> ele mesmo em 98% a
# 100%) e tira as pequenas dos extremos.
REGULARIZACAO = 0.01
REAMOSTRAS = 100
# Fatores do peso do prior testados na faixa de incerteza: de 1/4 a 4 vezes.
SENSIBILIDADE = (0.25, 4.0)
DADOS = Path(__file__).resolve().parent / "dados"


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


def estimar_matriz(
    x: np.ndarray,
    y: np.ndarray,
    pesos: np.ndarray,
    regularizacao: "float | np.ndarray" = 0.0,
    prior: Optional[np.ndarray] = None,
) -> np.ndarray:
    """Matriz origens x destinos que minimiza
    sum_i w_i ||x_i M - y_i||^2 + sum_r lam_r ||M_r - P_r||^2, com cada linha
    de M no simplex. `P` = `prior` (origens x destinos) ou, sem ele, a
    distribuicao media de `y` em cada linha. `lam_r` = `regularizacao` (numero
    ou um por origem) x escala do problema. Gradiente projetado acelerado (FISTA)."""
    w = pesos / pesos.sum()
    xtw = x.T * w
    gram = xtw @ x
    xty = xtw @ y
    k, m = x.shape[1], y.shape[1]
    escala = float(np.trace(gram)) / k
    lam = (np.broadcast_to(np.asarray(regularizacao, dtype=float), (k,)) * escala)[:, None]
    if prior is None:
        prior = np.tile(w @ y, (k, 1))
    lipschitz = 2.0 * (float(np.linalg.eigvalsh(gram).max()) + float(lam.max()))
    if lipschitz <= 0:
        raise ValueError("sem variacao entre as observacoes")
    passo = 1.0 / lipschitz
    matriz = np.full((k, m), 1.0 / m)
    auxiliar, t = matriz.copy(), 1.0
    for _ in range(MAX_ITERACOES):
        gradiente = 2.0 * (gram @ auxiliar - xty) + 2.0 * lam * (auxiliar - prior)
        nova = _projetar_simplex(auxiliar - passo * gradiente)
        t_novo = (1.0 + (1.0 + 4.0 * t * t) ** 0.5) / 2.0
        auxiliar = nova + ((t - 1.0) / t_novo) * (nova - matriz)
        variacao = float(np.abs(nova - matriz).max())
        matriz, t = nova, t_novo
        if variacao < TOLERANCIA:
            break
    return matriz


def estimar_com_incerteza(
    x: np.ndarray,
    y: np.ndarray,
    pesos: np.ndarray,
    *,
    prior: Optional[np.ndarray] = None,
    forca: "float | np.ndarray" = REGULARIZACAO,
    reamostras: int = REAMOSTRAS,
    semente: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(valor, minimo, maximo). Valor = estimativa com o prior; faixa = p5 a p95
    do bootstrap de municipios, alargada para incluir o resultado com 1/4 e
    com 4 vezes a `forca` (quanto o numero depende do peso dado a pesquisa)."""
    forca_arr = np.asarray(forca, dtype=float)
    valor = estimar_matriz(x, y, pesos, regularizacao=forca_arr, prior=prior)
    sensibilidade = [
        estimar_matriz(x, y, pesos, regularizacao=forca_arr * fator, prior=prior) for fator in SENSIBILIDADE
    ]
    rng = np.random.default_rng(semente)
    amostras = []
    for _ in range(reamostras):
        idx = rng.integers(0, len(x), size=len(x))
        amostras.append(estimar_matriz(x[idx], y[idx], pesos[idx], regularizacao=forca_arr, prior=prior))
    pontos = [valor, *sensibilidade]
    if amostras:
        pontos += [np.percentile(amostras, 5, axis=0), np.percentile(amostras, 95, axis=0)]
    return valor, np.minimum.reduce(pontos), np.maximum.reduce(pontos)


@dataclass(frozen=True)
class PriorLinha:
    destinos: Dict[str, float]
    forca: float
    fonte: str


def carregar_prior(ciclo: str, caminho: Optional[Path] = None) -> Dict[str, PriorLinha]:
    """Pontos de partida por origem (sqcand, `outros_candidatos`,
    `brancos_nulos`, `abstencao`). Arquivo ausente = sem prior."""
    caminho = caminho or DADOS / f"prior_transferencia_{ciclo}.json"
    if not caminho.exists():
        return {}
    bruto = json.loads(caminho.read_text(encoding="utf-8"))
    linhas: Dict[str, PriorLinha] = {}
    for item in bruto.get("linhas") or []:
        destinos = {str(k): float(v) for k, v in (item.get("destinos") or {}).items()}
        total = sum(destinos.values())
        if total <= 0:
            continue
        linhas[str(item["origem"])] = PriorLinha(
            destinos={k: v / total for k, v in destinos.items()},
            forca=float(item.get("forca", REGULARIZACAO)),
            fonte=str(item.get("fonte") or ""),
        )
    return linhas


def _tem_coluna(session: Session, tabela: str, coluna: str) -> bool:
    try:
        colunas = inspect(session.get_bind()).get_columns(tabela)
    except SQLAlchemyError:
        return False
    return any(c.get("name") == coluna for c in colunas)


def _tudo_totalizado(session: Session, ciclo: str, cargo: int) -> bool:
    row = session.execute(
        text(
            """
            SELECT count(DISTINCT turno) AS turnos,
                   sum(CASE WHEN totalizacao_final THEN 0 ELSE 1 END) AS abertos,
                   sum(CASE WHEN turno = 1 THEN 1 ELSE 0 END) AS municipios_t1,
                   sum(CASE WHEN turno = 2 THEN 1 ELSE 0 END) AS municipios_t2
              FROM tse_resultado_municipio
             WHERE ciclo = :ciclo AND cargo_codigo = :cargo
            """
        ),
        {"ciclo": ciclo, "cargo": cargo},
    ).one()
    # Municipio sem arquivo numa rodada (404, TSE fora) some da base: menos
    # municipios no 2o turno que no 1o = ainda nao esta tudo la.
    return (
        int(row.turnos or 0) == 2
        and int(row.abertos or 0) == 0
        and int(row.municipios_t2 or 0) >= int(row.municipios_t1 or 0)
    )


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


def _linhas(
    ciclo: str,
    cargo: int,
    recorte: str,
    chave: str,
    origens: List[str],
    destinos: List[str],
    matriz: np.ndarray,
    minimo: np.ndarray,
    maximo: np.ndarray,
    matriz_prior: np.ndarray,
    prior: Dict[str, PriorLinha],
    votos_origem: np.ndarray,
    municipios: int,
) -> List[dict]:
    return [
        {
            "ciclo": ciclo,
            "cargo": cargo,
            "recorte": recorte,
            "chave": chave,
            "origem": origem,
            "destino": destino,
            "proporcao": round(float(matriz[i, j]), 4),
            "proporcao_min": round(float(minimo[i, j]), 4),
            "proporcao_max": round(float(maximo[i, j]), 4),
            "proporcao_prior": round(float(matriz_prior[i, j]), 4),
            "fonte_prior": prior[origem].fonte if origem in prior else None,
            "votos": int(round(float(matriz[i, j]) * votos_origem[i], -3)),
            "municipios": municipios,
        }
        for i, origem in enumerate(origens)
        for j, destino in enumerate(destinos)
    ]


def calcular_estimativas(
    session: Session,
    ciclo: str = "ele2026",
    cargo: int = 1,
    *,
    reamostras: int = REAMOSTRAS,
    prior: Optional[Dict[str, PriorLinha]] = None,
) -> int:
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
    if len(finalistas) < 2:
        return 0

    prior = carregar_prior(ciclo) if prior is None else prior
    # Eliminado so fica separado se tiver ponto de partida; o resto vai para "outros".
    separados = [
        sq for sq, _ in sorted(total1.items(), key=lambda kv: -kv[1])
        if sq not in finalistas and str(sq) in prior
    ]
    origens: List[str] = [str(s) for s in finalistas + separados] + [OUTROS_CANDIDATOS, BRANCOS_NULOS, ABSTENCAO]
    destinos: List[str] = [str(s) for s in finalistas] + [BRANCOS_NULOS, ABSTENCAO]

    municipios = sorted(set(resumo1) & set(resumo2))
    xs, ys, pesos, absolutos, macro = [], [], [], [], []
    for cd in municipios:
        r1, r2 = resumo1[cd], resumo2[cd]
        el1, el2 = int(r1["eleitorado"] or 0), int(r2["eleitorado"] or 0)
        if el1 <= 0 or el2 <= 0:
            continue
        v1 = votos1.get(cd, {})
        outros = sum(v for sq, v in v1.items() if sq not in finalistas and sq not in separados)
        linha1 = [v1.get(sq, 0) for sq in finalistas + separados] + [
            outros,
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
    # Exterior (sem macrorregiao) e um estrato proprio: entra no Brasil, nao vira linha.
    estrato = np.array([m or EXTERIOR for m in macro], dtype=object)

    linhas = []
    soma_brasil = np.zeros((len(origens), len(destinos)))
    soma_min = np.zeros_like(soma_brasil)
    soma_max = np.zeros_like(soma_brasil)
    votos_brasil = np.zeros(len(origens))
    for chave in sorted(set(estrato)):
        mascara = estrato == chave
        if mascara.sum() < len(origens) * 2:
            logger.info("Estimativa: %s com poucos municipios; pulando.", chave)
            continue
        media = (y[mascara] * w[mascara][:, None]).sum(axis=0) / w[mascara].sum()
        matriz_prior = np.array(
            [
                [prior[o].destinos.get(d, 0.0) for d in destinos] if o in prior else list(media)
                for o in origens
            ]
        )
        forca = np.array([prior[o].forca if o in prior else REGULARIZACAO for o in origens])
        matriz, minimo, maximo = estimar_com_incerteza(
            x[mascara], y[mascara], w[mascara], prior=matriz_prior, forca=forca, reamostras=reamostras
        )
        votos_origem = a[mascara].sum(axis=0)
        soma_brasil += matriz * votos_origem[:, None]
        soma_min += minimo * votos_origem[:, None]
        soma_max += maximo * votos_origem[:, None]
        votos_brasil += votos_origem
        if chave == EXTERIOR:
            continue
        linhas += _linhas(
            ciclo, cargo, "macrorregiao", chave, origens, destinos, matriz, minimo, maximo,
            matriz_prior, prior, votos_origem, int(mascara.sum()),
        )

    if votos_brasil.any():
        com_voto = np.where(votos_brasil > 0, votos_brasil, 1.0)[:, None]
        media = (y * w[:, None]).sum(axis=0) / w.sum()
        matriz_prior_br = np.array(
            [[prior[o].destinos.get(d, 0.0) for d in destinos] if o in prior else list(media) for o in origens]
        )
        linhas = _linhas(
            ciclo, cargo, "brasil", "BR", origens, destinos,
            soma_brasil / com_voto, soma_min / com_voto, soma_max / com_voto,
            matriz_prior_br, prior, votos_brasil, len(x),
        ) + linhas

    # Antes do DELETE: o inspector devolve a conexao ao pool e isso desfaria a transacao.
    faixa = _tem_coluna(session, "comparacao_turnos_estimativa", "proporcao_min")
    session.execute(
        text("DELETE FROM comparacao_turnos_estimativa WHERE ciclo = :ciclo AND cargo_codigo = :cargo"),
        {"ciclo": ciclo, "cargo": cargo},
    )
    if linhas:
        # A faixa (CS-161) so existe depois da migration cs161a1b2c3d4; o deploy
        # sobe o codigo antes dela.
        colunas = ", proporcao_min, proporcao_max, proporcao_prior, fonte_prior" if faixa else ""
        valores = ", :proporcao_min, :proporcao_max, :proporcao_prior, :fonte_prior" if faixa else ""
        session.execute(
            text(
                "INSERT INTO comparacao_turnos_estimativa "
                f"(ciclo, cargo_codigo, recorte, chave, origem, destino, proporcao{colunas}, votos, municipios) "
                f"VALUES (:ciclo, :cargo, :recorte, :chave, :origem, :destino, :proporcao{valores}, :votos, :municipios)"
            ),
            linhas,
        )
    session.commit()
    return len(linhas)
