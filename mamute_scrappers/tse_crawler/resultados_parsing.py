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
from typing import Any, Dict, Iterable, Iterator, List, Optional
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

    @property
    def valido(self) -> bool:
        """Voto contado para o candidato (sub judice e anulado ficam de fora)."""
        return (self.destinacao_voto or "").lower().startswith("v")


@dataclass(frozen=True)
class ArquivoResultado:
    turno: int
    final: bool
    atualizado_em: Optional[datetime]
    candidatos: List[CandidatoResultado] = field(default_factory=list)
    # % de secoes totalizadas (`s.pst`), o "% de urnas apuradas" (CS-127).
    percentual_apurado: Optional[Decimal] = None
    # CS-128: insumos da definicao matematica antes do TSE encerrar.
    vagas: Optional[int] = None  # `carg[].nv`
    # `v.vvc`: validos + anulados sub judice. E sobre ele que o TSE calcula a
    # maioria absoluta (RJ 2026: 50,88% dos validos puros, 49,27% do vvc, 2o turno).
    votos_validos: Optional[int] = None
    eleitores_restantes: Optional[int] = None  # `e.esnt`: eleitores das secoes nao totalizadas
    # `md` do TSE: "e" = eleito definido, "s" = 2o turno definido (so presidente/governador).
    matematicamente_tse: Optional[str] = None


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
        vagas=_parse_int(((payload.get("carg") or [{}])[0] or {}).get("nv")),
        votos_validos=_parse_int(
            (payload.get("v") or {}).get("vvc") or (payload.get("v") or {}).get("vv")
        ),
        eleitores_restantes=_parse_int((payload.get("e") or {}).get("esnt")),
        matematicamente_tse=(payload.get("md") or "").strip().lower() or None,
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


ELEITO = "Eleito"
SEGUNDO_TURNO = "2º turno"
NAO_ELEITO = "Não eleito"
CARGOS_MAIORIA_ABSOLUTA = frozenset({CARGO_PRESIDENTE, CARGO_GOVERNADOR})
CARGOS_MAJORITARIOS_DEFINIVEIS = CARGOS_MAIORIA_ABSOLUTA | {CARGO_SENADOR}


def definicao_matematica(arquivo: ArquivoResultado, cargo: int) -> Optional[Dict[int, str]]:
    """Situacao de cada candidato quando o resultado ja nao pode mudar (CS-128).

    So para cargos majoritarios e arquivo ainda aberto. Devolve None quando
    ainda pode mudar ou quando falta dado. A conta e a do pior caso: os
    eleitores das secoes que faltam (`e.esnt`) votam todos contra quem esta
    na frente. Cada eleitor da no maximo 1 voto a cada candidato (no Senado
    com 2 vagas sao 2 votos, mas em candidatos diferentes), entao ninguem
    ganha mais que `eleitores_restantes`. Empate e tratado como indefinido.

    Presidente e governador: o TSE publica a propria marca (`md`); so vale
    quando a nossa conta chega ao MESMO resultado. Senador: o TSE nao marca,
    vale a nossa conta.
    """
    if arquivo.final or cargo not in CARGOS_MAJORITARIOS_DEFINIVEIS:
        return None
    restante = arquivo.eleitores_restantes
    if restante is None or restante < 0:
        return None
    # Sub judice entra na disputa: se a candidatura for liberada, os votos
    # contam. Por isso o ranking usa todos, e o resultado so vale quando
    # ninguem sub judice cai numa posicao decisiva.
    disputa = sorted(
        (c for c in arquivo.candidatos if c.votos is not None),
        key=lambda c: c.votos or 0,
        reverse=True,
    )
    if not disputa:
        return None
    votos = [int(c.votos or 0) for c in disputa]

    def _monta(primeiros: int, situacao: str) -> Optional[Dict[int, str]]:
        if any(not c.valido for c in disputa[:primeiros]):
            return None
        out = {c.sqcand: NAO_ELEITO for c in arquivo.candidatos}
        for c in disputa[:primeiros]:
            out[c.sqcand] = situacao
        return out

    if cargo in CARGOS_MAIORIA_ABSOLUTA:
        total = arquivo.votos_validos if arquivo.votos_validos is not None else sum(votos)
        # Eleito: maioria absoluta mesmo com todos os restantes contra.
        if 2 * votos[0] > total + restante:
            nossa, resultado = "e", _monta(1, ELEITO)
        elif any(2 * (v + restante) > total + restante for v in votos):
            return None  # alguem ainda pode passar de 50%
        elif len(votos) >= 2 and (len(votos) == 2 or votos[2] + restante < votos[1]):
            nossa, resultado = "s", _monta(2, SEGUNDO_TURNO)
        else:
            return None
        return resultado if arquivo.matematicamente_tse == nossa else None

    vagas = arquivo.vagas or 0
    if vagas < 1 or len(votos) < vagas:
        return None
    if len(votos) > vagas and votos[vagas] + restante >= votos[vagas - 1]:
        return None
    return _monta(vagas, ELEITO)
