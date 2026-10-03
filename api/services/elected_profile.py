"""Perfil dos eleitos de 2026 no painel admin (CS-107).

Para a matéria do resultado: percentuais de gênero, cor/raça e escolaridade
de quem foi eleito para o Senado e a Câmara, e quantos se reelegeram, no
Brasil e por UF.

- **Quem foi eleito** vem do resultado oficial gravado pela coleta da CS-106
  (`candidacy_result`), só do 1º turno: Senado e Câmara não têm 2º.
- **Gênero, cor/raça e escolaridade** vêm da candidatura
  (`candidacy.gender`/`race`/`education`, CS-63), porque o arquivo de
  resultado do TSE não traz esses campos. O cruzamento é pelo sequencial do
  candidato (`sqcand` = `tse_candidate_id`), feito na coleta.
- **Reeleito (CS-120)** = eleito para a mesma casa em que exerce mandato hoje:
  a candidatura aponta (`candidacy.parliamentarian_id`) para parlamentar com
  `status = 'Exercício'` do mesmo tipo (Deputado na Câmara, Senador no
  Senado). Suplente em exercício conta; deputado eleito senador não. O campo
  `st_REELEICAO` do TSE não serve: vem `false` para todo mundo em 2026.
  Deputados casam por CPF; senadores, por nome (a base não tem o CPF deles).
  "Em exercício hoje" vale até a posse de 01/02/2027.
- **Nunca parcial.** Uma UF só entra quando o arquivo dela está com a
  totalização encerrada (`tse_result_file.totalizacao_final`), e o total do
  Brasil só aparece com as 27 encerradas.
- **Conferência.** `eleitos_no_tse` é quantos eleitos o arquivo do TSE traz,
  casados ou não com a base. Se for maior que os eleitos encontrados, a tela
  avisa que o percentual daquela UF não cobre todo mundo. `None` = arquivo
  coletado antes da contagem existir (migration cs107a1b2c3d4).
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

ANO = 2026
CICLO = "ele2026"
TURNO = 1

UFS = (
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
    "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC",
    "SE", "SP", "TO",
)  # fmt: skip

# (chave, nome na tela, código do cargo no TSE)
CASAS = (
    ("senado", "Senado", 5),
    ("camara", "Câmara dos Deputados", 6),
)

# Valores na forma dos CSVs do TSE, em maiúsculas (ver tse_crawler/profile.py).
# Fora da lista entra depois, em ordem alfabética; sem informação, por último.
_ORDEM_GENERO = ("FEMININO", "MASCULINO")
_ORDEM_COR_RACA = ("BRANCA", "PRETA", "PARDA", "AMARELA", "INDÍGENA")
_PRETOS_E_PARDOS = frozenset({"PRETA", "PARDA"})
_ORDEM_ESCOLARIDADE = (
    "SUPERIOR COMPLETO",
    "SUPERIOR INCOMPLETO",
    "ENSINO MÉDIO COMPLETO",
    "ENSINO MÉDIO INCOMPLETO",
    "ENSINO FUNDAMENTAL COMPLETO",
    "ENSINO FUNDAMENTAL INCOMPLETO",
    "LÊ E ESCREVE",
)
_SUPERIOR_COMPLETO = "SUPERIOR COMPLETO"

# Linha agregada: (gênero, cor/raça, escolaridade, reeleito, quantidade).
Linha = tuple[Optional[str], Optional[str], Optional[str], bool, int]


def _tem_coluna(db: Session, tabela: str, coluna: str) -> bool:
    try:
        colunas = sqlalchemy_inspect(db.get_bind()).get_columns(tabela)
    except SQLAlchemyError:
        return True
    return any(c.get("name") == coluna for c in colunas)


def _iso(valor: Any) -> Optional[str]:
    if valor is None:
        return None
    return valor.isoformat() if hasattr(valor, "isoformat") else str(valor)


def _texto(valor: Any) -> Optional[str]:
    texto = (valor or "").strip().upper()
    return texto or None


def _arquivos(db: Session, ciclo: str) -> dict[tuple[int, str], dict[str, Any]]:
    """Estado do arquivo do TSE de cada (cargo, UF) do 1º turno."""
    # Na janela do deploy antes da cs107 a contagem ainda não existe.
    eleitos = (
        "eleitos_no_arquivo"
        if _tem_coluna(db, "tse_result_file", "eleitos_no_arquivo")
        else "NULL"
    )
    rows = db.execute(
        text(
            f"""
            SELECT cargo_codigo, uf, totalizacao_final, tse_atualizado_em,
                   {eleitos} AS eleitos_no_tse
            FROM tse_result_file
            WHERE ciclo = :ciclo AND turno = :turno AND cargo_codigo IN (5, 6)
            """
        ),
        {"ciclo": ciclo, "turno": TURNO},
    ).mappings()
    return {
        (int(r["cargo_codigo"]), r["uf"].upper()): {
            "final": bool(r["totalizacao_final"]),
            "tse_atualizado_em": r["tse_atualizado_em"],
            "eleitos_no_tse": (
                int(r["eleitos_no_tse"]) if r["eleitos_no_tse"] is not None else None
            ),
        }
        for r in rows
    }


def _eleitos(db: Session, ano: int) -> dict[tuple[int, str], list[Linha]]:
    """Eleitos encontrados na base, agrupados por (cargo, UF) e perfil.

    Eleito = `eleito` com a situação começando por "Eleito": o TSE também marca
    `e = "s"` quem foi ao 2º turno. Mesma regra de `foi_eleito` na coleta.
    """
    rows = db.execute(
        text(
            """
            SELECT c.office_code AS cargo, UPPER(TRIM(c.state)) AS uf,
                   c.gender AS genero, c.race AS cor_raca, c.education AS escolaridade,
                   CASE WHEN p.status = 'Exercício'
                         AND ((c.office_code = 6 AND p.type = 'Deputado')
                              OR (c.office_code = 5 AND p.type = 'Senador'))
                        THEN 1 ELSE 0 END AS reeleito,
                   COUNT(*) AS n
            FROM candidacy_result cr
            JOIN candidacy c ON c.id = cr.candidacy_id
            LEFT JOIN parliamentarian p ON p.id = c.parliamentarian_id
            WHERE c.election_year = :ano
              AND cr.turno = :turno
              AND cr.totalizacao_final
              AND cr.eleito
              AND LOWER(cr.situacao) LIKE 'eleito%'
              AND c.office_code IN (5, 6)
            GROUP BY 1, 2, 3, 4, 5, 6
            """
        ),
        {"ano": ano, "turno": TURNO},
    ).mappings()
    grupos: dict[tuple[int, str], list[Linha]] = {}
    for r in rows:
        chave = (int(r["cargo"]), r["uf"])
        grupos.setdefault(chave, []).append(
            (
                _texto(r["genero"]),
                _texto(r["cor_raca"]),
                _texto(r["escolaridade"]),
                bool(r["reeleito"]),
                int(r["n"]),
            )
        )
    return grupos


def _pct(parte: int, total: int) -> Optional[float]:
    return round(100 * parte / total, 1) if total else None


def _chave_ordem(valor: Optional[str], ordem: tuple[str, ...]) -> tuple[int, int, str]:
    if valor is None:
        return (2, 0, "")
    if valor in ordem:
        return (0, ordem.index(valor), "")
    return (1, 0, valor)


def _distribuicao(
    contagem: Counter, total: int, ordem: tuple[str, ...]
) -> list[dict[str, Any]]:
    return [
        {"valor": valor, "eleitos": n, "percentual": _pct(n, total)}
        for valor, n in sorted(contagem.items(), key=lambda item: _chave_ordem(item[0], ordem))
    ]


def _perfil(linhas: Iterable[Linha], eleitos_no_tse: Optional[int]) -> dict[str, Any]:
    genero: Counter = Counter()
    cor_raca: Counter = Counter()
    escolaridade: Counter = Counter()
    reeleitos = 0
    for g, r, e, reeleito, n in linhas:
        genero[g] += n
        cor_raca[r] += n
        escolaridade[e] += n
        if reeleito:
            reeleitos += n
    total = sum(genero.values())
    pretos_e_pardos = sum(n for valor, n in cor_raca.items() if valor in _PRETOS_E_PARDOS)
    return {
        "eleitos": total,
        "eleitos_no_tse": eleitos_no_tse,
        "genero": _distribuicao(genero, total, _ORDEM_GENERO),
        "cor_raca": _distribuicao(cor_raca, total, _ORDEM_COR_RACA),
        "pretos_e_pardos": {
            "eleitos": pretos_e_pardos,
            "percentual": _pct(pretos_e_pardos, total),
        },
        "escolaridade": _distribuicao(escolaridade, total, _ORDEM_ESCOLARIDADE),
        "superior_completo": {
            "eleitos": escolaridade[_SUPERIOR_COMPLETO],
            "percentual": _pct(escolaridade[_SUPERIOR_COMPLETO], total),
        },
        "reeleitos": {"eleitos": reeleitos, "percentual": _pct(reeleitos, total)},
    }


def _categorias(perfis: list[dict[str, Any]], campo: str, ordem: tuple[str, ...]) -> list[Optional[str]]:
    valores = {item["valor"] for perfil in perfis for item in perfil[campo]}
    return sorted(valores, key=lambda valor: _chave_ordem(valor, ordem))


def _casa(
    chave: str,
    nome: str,
    cargo: int,
    arquivos: dict[tuple[int, str], dict[str, Any]],
    eleitos: dict[tuple[int, str], list[Linha]],
) -> dict[str, Any]:
    por_uf: list[dict[str, Any]] = []
    linhas_brasil: list[Linha] = []
    eleitos_no_tse: list[Optional[int]] = []
    atualizacoes: list[Any] = []
    for uf in UFS:
        arquivo = arquivos.get((cargo, uf))
        encerrada = bool(arquivo and arquivo["final"])
        if not encerrada:
            por_uf.append({"uf": uf, "encerrada": False, "tse_atualizado_em": None, "perfil": None})
            continue
        linhas = eleitos.get((cargo, uf), [])
        linhas_brasil.extend(linhas)
        eleitos_no_tse.append(arquivo["eleitos_no_tse"])
        if arquivo["tse_atualizado_em"] is not None:
            atualizacoes.append(arquivo["tse_atualizado_em"])
        por_uf.append(
            {
                "uf": uf,
                "encerrada": True,
                "tse_atualizado_em": _iso(arquivo["tse_atualizado_em"]),
                "perfil": _perfil(linhas, arquivo["eleitos_no_tse"]),
            }
        )

    aguardando = [item["uf"] for item in por_uf if not item["encerrada"]]
    brasil = None
    if not aguardando:
        total_tse = None if None in eleitos_no_tse else sum(eleitos_no_tse)  # type: ignore[arg-type]
        brasil = _perfil(linhas_brasil, total_tse)

    perfis = [item["perfil"] for item in por_uf if item["perfil"]]
    return {
        "casa": chave,
        "nome": nome,
        "cargo_codigo": cargo,
        "ufs_total": len(UFS),
        "ufs_encerradas": len(UFS) - len(aguardando),
        "ufs_aguardando": aguardando,
        "tse_atualizado_em": _iso(max(atualizacoes)) if atualizacoes else None,
        "categorias": {
            "genero": _categorias(perfis, "genero", _ORDEM_GENERO),
            "cor_raca": _categorias(perfis, "cor_raca", _ORDEM_COR_RACA),
            "escolaridade": _categorias(perfis, "escolaridade", _ORDEM_ESCOLARIDADE),
        },
        "brasil": brasil,
        "por_uf": por_uf,
    }


def elected_profile(db: Session, *, ano: int = ANO, ciclo: str = CICLO) -> dict[str, Any]:
    arquivos = _arquivos(db, ciclo)
    eleitos = _eleitos(db, ano)
    return {
        "ano": ano,
        "casas": [_casa(chave, nome, cargo, arquivos, eleitos) for chave, nome, cargo in CASAS],
    }


__all__ = ["elected_profile"]
