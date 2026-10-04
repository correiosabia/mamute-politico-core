"""Leitura dos JSONs de divulgacao de resultados do TSE (CS-106).

Funcoes puras, sem rede nem banco, para os testes cobrirem o formato.

Layout validado em 01/10/2026 contra o oficial e o simulado de 2026:

- Configuracao: `<base>/comum/config/ele-c.json`. Lista os pleitos (`pl`) por
  ciclo (`c`, ex.: "ele2026"), cada um com data (`dt`, dd/mm/aaaa) e eleicoes
  (`e`): codigo (`cd`), turno (`t`), codigo do 2o turno (`cdt2`) e cargos
  (`abr[].cp[].cd`). Os codigos so existem nesse arquivo; por isso nada de
  codigo de eleicao fixo aqui.
- Resultado: `<base>/<ciclo>/<eleicao>/dados/<uf>/<uf>-c<cargo:04>-e<eleicao:06>-u.json`.
  Topo: `tf` ("s" = totalizacao encerrada), `t` (turno), `dt`/`ht` (data e
  hora da totalizacao). Candidatos em `carg[].agr[].par[].cand[]`: `sqcand`
  (= SQ_CANDIDATO = candidacy.tse_candidate_id), `st` (situacao em texto),
  `e` ("s"/"n"), `vap` (votos), `pvap` ("8,39"), `dvt` (destinacao do voto).
  `e = "s"` tambem vale para quem foi ao 2o turno; ver `foi_eleito`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Iterator, List, Optional
from zoneinfo import ZoneInfo

TZ_BRASILIA = ZoneInfo("America/Sao_Paulo")

# Cargos que temos em `candidacy` (codigos da DivulgaCandContas, iguais aos do
# arquivo de resultado). Conselheiro Distrital (25) fica de fora.
CARGO_PRESIDENTE = 1
CARGO_GOVERNADOR = 3
CARGO_SENADOR = 5
CARGO_DEP_FEDERAL = 6
CARGO_DEP_ESTADUAL = 7
CARGO_DEP_DISTRITAL = 8
CARGOS_COLETADOS = frozenset(
    {
        CARGO_PRESIDENTE,
        CARGO_GOVERNADOR,
        CARGO_SENADOR,
        CARGO_DEP_FEDERAL,
        CARGO_DEP_ESTADUAL,
        CARGO_DEP_DISTRITAL,
    }
)

UFS = (
    "ac", "al", "am", "ap", "ba", "ce", "df", "es", "go", "ma", "mg", "ms",
    "mt", "pa", "pb", "pe", "pi", "pr", "rj", "rn", "ro", "rr", "rs", "sc",
    "se", "sp", "to",
)  # fmt: skip


@dataclass(frozen=True)
class EleicaoConfig:
    codigo: int
    turno: int
    data: date
    cargos: tuple[int, ...]


@dataclass(frozen=True)
class CandidatoResultado:
    sqcand: int
    situacao: Optional[str]
    eleito: Optional[bool]
    votos: Optional[int]
    percentual: Optional[Decimal]
    destinacao_voto: Optional[str]


@dataclass(frozen=True)
class ArquivoResultado:
    turno: int
    final: bool
    atualizado_em: Optional[datetime]
    candidatos: List[CandidatoResultado] = field(default_factory=list)
    # % de secoes totalizadas (`s.pst`), o "% de urnas apuradas" (CS-127).
    percentual_apurado: Optional[Decimal] = None


def _parse_date_br(raw: Any) -> Optional[date]:
    try:
        return datetime.strptime(str(raw).strip(), "%d/%m/%Y").date()
    except (TypeError, ValueError):
        return None


def parse_config(ele_c: dict, ciclo: str) -> List[EleicaoConfig]:
    """Eleicoes do ciclo com ao menos um cargo que coletamos."""
    eleicoes: List[EleicaoConfig] = []
    for pleito in ele_c.get("pl") or []:
        if pleito.get("c") != ciclo:
            continue
        data = _parse_date_br(pleito.get("dt"))
        if data is None:
            continue
        for eleicao in pleito.get("e") or []:
            cargos = sorted(
                {
                    int(cp["cd"])
                    for abr in eleicao.get("abr") or []
                    for cp in abr.get("cp") or []
                    if str(cp.get("cd", "")).isdigit()
                    and int(cp["cd"]) in CARGOS_COLETADOS
                }
            )
            if not cargos:
                continue
            eleicoes.append(
                EleicaoConfig(
                    codigo=int(eleicao["cd"]),
                    turno=int(eleicao.get("t") or 1),
                    data=data,
                    cargos=tuple(cargos),
                )
            )
    return eleicoes


def abrangencias(cargo: int) -> tuple[str, ...]:
    """UFs (em minusculas) que tem arquivo para o cargo."""
    if cargo == CARGO_PRESIDENTE:
        return ("br",)
    if cargo == CARGO_DEP_DISTRITAL:
        return ("df",)
    if cargo == CARGO_DEP_ESTADUAL:
        return tuple(uf for uf in UFS if uf != "df")
    return UFS


def config_url(base: str) -> str:
    return f"{base.rstrip('/')}/comum/config/ele-c.json"


def result_file_url(base: str, ciclo: str, eleicao: int, uf: str, cargo: int) -> str:
    uf = uf.lower()
    return (
        f"{base.rstrip('/')}/{ciclo}/{eleicao}/dados/{uf}/"
        f"{uf}-c{cargo:04d}-e{eleicao:06d}-u.json"
    )


def _parse_int(raw: Any) -> Optional[int]:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _parse_percent(raw: Any) -> Optional[Decimal]:
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw).replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def _parse_timestamp(dt: Any, ht: Any) -> Optional[datetime]:
    if not dt or not ht:
        return None
    try:
        naive = datetime.strptime(f"{dt} {ht}", "%d/%m/%Y %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=TZ_BRASILIA)


def _iter_candidatos(payload: dict) -> Iterator[dict]:
    for cargo in payload.get("carg") or []:
        for agr in cargo.get("agr") or []:
            for par in agr.get("par") or []:
                for cand in par.get("cand") or []:
                    yield cand


def parse_result_file(payload: dict) -> ArquivoResultado:
    candidatos: List[CandidatoResultado] = []
    for cand in _iter_candidatos(payload):
        sqcand = _parse_int(cand.get("sqcand"))
        if sqcand is None:
            continue
        e = (cand.get("e") or "").strip().lower()
        candidatos.append(
            CandidatoResultado(
                sqcand=sqcand,
                situacao=(cand.get("st") or "").strip() or None,
                eleito=True if e == "s" else False if e == "n" else None,
                votos=_parse_int(cand.get("vap")),
                percentual=_parse_percent(cand.get("pvap")),
                destinacao_voto=(cand.get("dvt") or "").strip() or None,
            )
        )
    return ArquivoResultado(
        turno=int(payload.get("t") or 1),
        final=(payload.get("tf") or "").strip().lower() == "s",
        atualizado_em=_parse_timestamp(payload.get("dt"), payload.get("ht")),
        candidatos=candidatos,
        percentual_apurado=_parse_percent((payload.get("s") or {}).get("pst")),
    )


def foi_eleito(candidato: CandidatoResultado) -> bool:
    """Eleito de fato no arquivo, para contar eleitos (CS-107).

    `e = "s"` sozinho nao basta: o TSE tambem marca assim quem foi ao 2o turno
    (conferido no simulado de governador). Eleito e `e = "s"` com a situacao
    comecando por "Eleito" ("Eleito", "Eleito por QP", "Eleito por media").
    A API (`api/services/elected_profile.py`) usa a mesma regra.
    """
    return bool(candidato.eleito) and (candidato.situacao or "").lower().startswith("eleito")


def arquivos_da_eleicao(eleicao: EleicaoConfig) -> Iterable[tuple[str, int]]:
    """Pares (uf, cargo) que a eleicao publica."""
    for cargo in eleicao.cargos:
        for uf in abrangencias(cargo):
            yield uf, cargo
