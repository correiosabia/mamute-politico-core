"""Viagens coincidentes entre os membros de uma coleção, pela cota parlamentar (CS-132).

Cruza os gastos de passagem e hospedagem dos parlamentares de uma coleção e
devolve os momentos em que dois ou mais estiveram no mesmo lugar no mesmo dia.
É um dado descritivo: coincidência de agenda não indica nada sozinha, e a tela
precisa dizer isso.

Regras (todas aqui, nenhuma na tela):

1. SÓ VALE O PRÓPRIO PARLAMENTAR. Bilhete de assessor fica de fora: na Câmara o
   nome do passageiro precisa bater com o do parlamentar; no Senado o
   passageiro precisa vir marcado como PARLAMENTAR.
2. BRASÍLIA E A CASA NÃO CONTAM. Destino Brasília (rotina do mandato) e
   destino no próprio estado (volta para casa) são descartados. O que sobra é
   viagem para fora.
3. A DATA NÃO É A MESMA NAS DUAS CASAS. A Câmara publica a data de EMISSÃO do
   bilhete; o Senado publica a data do VOO. Cada pessoa leva a sua base de data
   (`date_basis`) e a tela mostra a diferença.
4. MESMO VOO só existe no Senado (é a única fonte com número do voo).
5. MESMO HOTEL é o mesmo CNPJ (com a filial) com notas a até 2 dias de distância.
6. GRUPO DE CONTROLE. Cada encontro "mesma cidade" vem com o contexto da base
   inteira (as mesmas regras aplicadas a todos os parlamentares): quantos foram
   para aquela cidade naquele dia, quantos vão num dia típico e quantos da
   lista seriam esperados se a escolha fosse ao acaso (hipergeométrica). Sem
   isso, cinco pessoas em São Paulo no mesmo dia pode ser só rotina.
7. CONTROLE POR PARTIDO. A mesma conta repetida só entre parlamentares dos
   partidos de quem está no encontro: separa "evento do partido" de "grupo da
   lista". Usa o partido ATUAL do cadastro (troca de legenda não é refeita).
"""

from __future__ import annotations

import math
import re
import statistics
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

try:
    from ..db.models.collection import CollectionMember
    from ..db.models.parliamentarian import Parliamentarian
    from ..db.models.parliamentary_expense import ParliamentaryExpense
    from .collection import TABELAS, _buscar_colecao, _num, resolver_vinculos
    from .editorial_agendas import tabelas_disponiveis
except ImportError:  # execução dentro de api/
    from db.models.collection import CollectionMember
    from db.models.parliamentarian import Parliamentarian
    from db.models.parliamentary_expense import ParliamentaryExpense
    from services.collection import TABELAS, _buscar_colecao, _num, resolver_vinculos
    from services.editorial_agendas import tabelas_disponiveis

# Quantos anos de cota entram no cruzamento (o mesmo recorte do resumo da pessoa).
ANOS = 4
# Distância máxima entre as notas de um mesmo hotel para contar como "juntos".
DIAS_HOTEL = 2
# Leitura pública: o cruzamento é caro e o dado muda uma vez por dia.
_CACHE_SEGUNDOS = 3600
_cache: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
# Linha de base (todos os parlamentares): varre ~140 mil notas, uns 10 s em prod.
_CACHE_BASE_SEGUNDOS = 6 * 3600
_cache_base: dict[int, tuple[float, "_LinhaDeBase"]] = {}
# Abaixo disso o encontro é marcado como incomum para o tamanho do grupo.
P_INCOMUM = 0.01
# Base pequena demais não serve de comparação (o Senado quase não marca o passageiro).
BASE_MINIMA = 30

# Aeroportos do Brasil: código IATA -> (cidade, UF). Fora da lista, o código
# vira a própria cidade e a UF fica desconhecida (não é descartado como "casa").
AEROPORTOS: dict[str, tuple[str, str]] = {
    "BSB": ("Brasília", "DF"),
    "CGH": ("São Paulo", "SP"), "GRU": ("São Paulo", "SP"), "VCP": ("Campinas", "SP"),
    "RAO": ("Ribeirão Preto", "SP"), "SJP": ("São José do Rio Preto", "SP"),
    "PPB": ("Presidente Prudente", "SP"), "BAU": ("Bauru", "SP"), "JTC": ("Bauru", "SP"),
    "AQA": ("Araraquara", "SP"), "MII": ("Marília", "SP"), "ARU": ("Araçatuba", "SP"),
    "SJK": ("São José dos Campos", "SP"),
    "SDU": ("Rio de Janeiro", "RJ"), "GIG": ("Rio de Janeiro", "RJ"),
    "CAW": ("Campos dos Goytacazes", "RJ"), "MEA": ("Macaé", "RJ"),
    "CNF": ("Belo Horizonte", "MG"), "PLU": ("Belo Horizonte", "MG"),
    "UDI": ("Uberlândia", "MG"), "UBA": ("Uberaba", "MG"), "MOC": ("Montes Claros", "MG"),
    "IPN": ("Ipatinga", "MG"), "GVR": ("Governador Valadares", "MG"), "JDF": ("Juiz de Fora", "MG"),
    "IZA": ("Juiz de Fora", "MG"), "VAG": ("Varginha", "MG"),
    "VIX": ("Vitória", "ES"),
    "CWB": ("Curitiba", "PR"), "LDB": ("Londrina", "PR"), "MGF": ("Maringá", "PR"),
    "IGU": ("Foz do Iguaçu", "PR"), "CAC": ("Cascavel", "PR"),
    "FLN": ("Florianópolis", "SC"), "NVT": ("Navegantes", "SC"), "JOI": ("Joinville", "SC"),
    "XAP": ("Chapecó", "SC"), "CCM": ("Criciúma", "SC"),
    "POA": ("Porto Alegre", "RS"), "CXJ": ("Caxias do Sul", "RS"), "PFB": ("Passo Fundo", "RS"),
    "RIA": ("Santa Maria", "RS"), "PET": ("Pelotas", "RS"),
    "SSA": ("Salvador", "BA"), "IOS": ("Ilhéus", "BA"), "BPS": ("Porto Seguro", "BA"),
    "VDC": ("Vitória da Conquista", "BA"), "LEC": ("Lençóis", "BA"), "BRA": ("Barreiras", "BA"),
    "PAV": ("Paulo Afonso", "BA"), "FEC": ("Feira de Santana", "BA"),
    "REC": ("Recife", "PE"), "PNZ": ("Petrolina", "PE"), "FEN": ("Fernando de Noronha", "PE"),
    "CPV": ("Campina Grande", "PB"), "JPA": ("João Pessoa", "PB"),
    "NAT": ("Natal", "RN"), "MVF": ("Mossoró", "RN"),
    "FOR": ("Fortaleza", "CE"), "JDO": ("Juazeiro do Norte", "CE"), "JJD": ("Jijoca de Jericoacoara", "CE"),
    "THE": ("Teresina", "PI"), "PHB": ("Parnaíba", "PI"),
    "SLZ": ("São Luís", "MA"), "IMP": ("Imperatriz", "MA"),
    "MCZ": ("Maceió", "AL"),
    "AJU": ("Aracaju", "SE"),
    "BEL": ("Belém", "PA"), "STM": ("Santarém", "PA"), "MAB": ("Marabá", "PA"), "ATM": ("Altamira", "PA"),
    "MAO": ("Manaus", "AM"), "TBT": ("Tabatinga", "AM"),
    "MCP": ("Macapá", "AP"),
    "BVB": ("Boa Vista", "RR"),
    "PVH": ("Porto Velho", "RO"), "JPR": ("Ji-Paraná", "RO"),
    "RBR": ("Rio Branco", "AC"), "CZS": ("Cruzeiro do Sul", "AC"),
    "PMW": ("Palmas", "TO"), "AUX": ("Araguaína", "TO"),
    "GYN": ("Goiânia", "GO"), "CLV": ("Caldas Novas", "GO"), "RVD": ("Rio Verde", "GO"),
    "CGB": ("Cuiabá", "MT"), "ROO": ("Rondonópolis", "MT"), "SNZ": ("Sinop", "MT"), "OPS": ("Sinop", "MT"),
    "CGR": ("Campo Grande", "MS"), "DOU": ("Dourados", "MS"), "CMG": ("Corumbá", "MS"),
}

_IATA = re.compile(r"^[A-Z]{3}$")
_CAMARA_PAX = re.compile(r"Passageiro:\s*([^;]+)")
_CAMARA_TRECHO = re.compile(r"Trecho:\s*([A-Z/ ]+)")
# "NOME (Matrícula 123, PARLAMENTAR), Voo: 1717 - THE BSB - 03/04/24"
_SENADO_VOO = re.compile(
    r"([^;:]+?)\s*\(Matr[íi]cula[^,]*,\s*([A-ZÇÃÕÉÍ]+)\),\s*Voo:\s*(\S+)\s*-\s*([A-Z]{3})\s+([A-Z]{3})\s*-\s*(\d{2}/\d{2}/\d{2,4})"
)
_PALAVRAS_VAZIAS = {"DE", "DA", "DO", "DAS", "DOS", "E", "JR", "JUNIOR", "FILHO", "NETO", "SOBRINHO"}


def _tokens(nome: Optional[str]) -> list[str]:
    sem_acento = unicodedata.normalize("NFD", nome or "").encode("ascii", "ignore").decode()
    return [t for t in re.split(r"[^A-Z]+", sem_acento.upper()) if t]


def mesmo_passageiro(passageiro: Optional[str], *nomes: Optional[str]) -> bool:
    """Primeiro nome igual e sobrenome em comum (a Câmara abrevia o meio do nome)."""

    pax = [t for t in _tokens(passageiro) if t not in _PALAVRAS_VAZIAS]
    if not pax:
        return False
    for nome in nomes:
        alvo = [t for t in _tokens(nome) if t not in _PALAVRAS_VAZIAS]
        if len(alvo) < 2 or len(pax) < 2:
            continue
        if pax[0] == alvo[0] and set(pax[1:]) & set(alvo[1:]):
            return True
    return False


def destinos(aeroportos: list[str]) -> list[str]:
    """Para onde a pessoa foi: o último trecho, ou o meio de uma ida e volta."""

    aeroportos = [a for a in aeroportos if _IATA.match(a)]
    if len(aeroportos) < 2:
        return []
    if len(aeroportos) >= 3 and aeroportos[0] == aeroportos[-1]:
        return list(dict.fromkeys(aeroportos[1:-1]))
    return [aeroportos[-1]]


def _cidade(iata: str) -> tuple[str, Optional[str]]:
    cidade, uf = AEROPORTOS.get(iata, (iata, None))
    return cidade, uf


def _data_senado(texto: str) -> Optional[date]:
    for formato in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    return None


def _e_passagem(tipo: str) -> bool:
    t = (tipo or "").lower()
    return "passage" in t and ("aérea" in t or "aerea" in t or "aéreas" in t)


def _e_hospedagem(e: ParliamentaryExpense) -> bool:
    tipo = (e.expense_type or "").lower()
    if e.house == "camara":
        return tipo.startswith("hospedagem")
    # No Senado a categoria mistura hospedagem, combustível e alimentação.
    texto = f"{e.details or ''} {e.supplier_name or ''}".lower()
    return "hosped" in tipo and bool(re.search(r"hotel|hosped|pousada|resort|flat", texto))


def _trechos(e: ParliamentaryExpense, parl: Parliamentarian) -> list[dict[str, Any]]:
    """Viagens do próprio parlamentar numa nota de passagem, já com destino e data."""

    detalhes = e.details or ""
    if e.house == "senado":
        saida = []
        for nome, categoria, voo, origem, destino, quando in _SENADO_VOO.findall(detalhes):
            if categoria.upper() != "PARLAMENTAR":
                continue
            dia = _data_senado(quando)
            if dia is None:
                continue
            saida.append(
                {"date": dia, "basis": "flight", "dest": destino, "route": f"{origem}/{destino}",
                 "flight": voo, "passenger": " ".join(nome.split())}
            )
        return saida

    pax = _CAMARA_PAX.search(detalhes)
    trecho = _CAMARA_TRECHO.search(detalhes)
    if not pax or not trecho or e.document_date is None:
        return []
    passageiro = " ".join(pax.group(1).split())
    if not mesmo_passageiro(passageiro, parl.full_name, parl.name):
        return []
    aeroportos = [a.strip() for a in trecho.group(1).split("/")]
    return [
        {"date": e.document_date, "basis": "issue", "dest": d, "route": "/".join(a for a in aeroportos if a),
         "flight": None, "passenger": passageiro}
        for d in destinos(aeroportos)
    ]


def _viagens_validas(e: Any, parl: Parliamentarian) -> list[tuple[str, dict[str, Any]]]:
    """(cidade, trecho) das viagens do próprio parlamentar, sem Brasília nem o estado dele."""

    casa_uf = (parl.state_elected or "").upper()
    saida = []
    for t in _trechos(e, parl):
        cidade, uf = _cidade(t["dest"])
        if t["dest"] == "BSB" or (uf and uf == casa_uf):
            continue
        saida.append((cidade, t))
    return saida


class _LinhaDeBase:
    """Quem, na base inteira, viajou para cada cidade em cada dia (regras 1 e 2)."""

    def __init__(self) -> None:
        self.dias: dict[tuple[str, date, str], set[int]] = defaultdict(set)
        self.dias_ativos: dict[str, set[date]] = defaultdict(set)
        self.viajantes: dict[str, set[int]] = defaultdict(set)
        self.partido: dict[int, str] = {}
        self._tipico: dict[tuple[str, str], float] = {}

    def tipico(self, casa: str, cidade: str) -> float:
        """Mediana de viajantes para a cidade nos dias em que a casa emitiu algum bilhete."""
        chave = (casa, cidade)
        if chave not in self._tipico:
            contagens = [len(self.dias.get((casa, d, cidade), ())) for d in self.dias_ativos.get(casa, ())]
            self._tipico[chave] = float(statistics.median(contagens)) if contagens else 0.0
        return self._tipico[chave]


def _linha_de_base(db: Session, ano_corte: int) -> _LinhaDeBase:
    guardado = _cache_base.get(ano_corte)
    if guardado and time.monotonic() - guardado[0] < _CACHE_BASE_SEGUNDOS:
        return guardado[1]
    parls = {int(p.id): p for p in db.execute(select(Parliamentarian)).scalars()}
    base = _LinhaDeBase()
    base.partido = {pid: (p.party or "").strip().upper() for pid, p in parls.items() if (p.party or "").strip()}
    linhas = db.execute(
        select(
            ParliamentaryExpense.parliamentarian_id,
            ParliamentaryExpense.house,
            ParliamentaryExpense.details,
            ParliamentaryExpense.document_date,
            ParliamentaryExpense.expense_type,
        ).where(
            ParliamentaryExpense.year >= ano_corte,
            ParliamentaryExpense.net_value > 0,
            ParliamentaryExpense.expense_type.ilike("%passage%"),
        )
    )
    for linha in linhas:
        parl = parls.get(int(linha.parliamentarian_id)) if linha.parliamentarian_id is not None else None
        if parl is None or not _e_passagem(linha.expense_type):
            continue
        for cidade, t in _viagens_validas(linha, parl):
            base.dias[(linha.house, t["date"], cidade)].add(int(parl.id))
            base.dias_ativos[linha.house].add(t["date"])
            base.viajantes[linha.house].add(int(parl.id))
    _cache_base[ano_corte] = (time.monotonic(), base)
    return base


def _cauda_hipergeometrica(k: int, populacao: int, marcados: int, sorteados: int) -> float:
    """P(X >= k): chance de k ou mais da lista entre `sorteados`, se fosse ao acaso."""
    if populacao <= 0 or sorteados <= 0:
        return 1.0
    total = math.comb(populacao, sorteados)
    topo = min(marcados, sorteados)
    return sum(
        math.comb(marcados, i) * math.comb(populacao - marcados, sorteados - i)
        for i in range(max(k, 0), topo + 1)
    ) / total


def _contexto(
    base: _LinhaDeBase, casa: str, dia: date, cidade: str, membros_casa: set[int], presentes: set[int]
) -> Optional[dict[str, Any]]:
    na_lista = len(presentes)
    if len(base.viajantes.get(casa, ())) < BASE_MINIMA:
        return None
    viajantes = len(base.dias.get((casa, dia, cidade), ())) or na_lista
    populacao = len(base.viajantes.get(casa, ())) or viajantes
    marcados = len(membros_casa & base.viajantes.get(casa, set())) or na_lista
    p = _cauda_hipergeometrica(na_lista, populacao, marcados, viajantes)
    return {
        "house": casa,
        "travelers": viajantes,
        "listed": na_lista,
        "typical_day": base.tipico(casa, cidade),
        "expected": round(viajantes * marcados / populacao, 2) if populacao else None,
        "p_value": p,
        "unusual": p < P_INCOMUM,
        "party": _contexto_partido(base, casa, dia, cidade, membros_casa, presentes),
    }


def _contexto_partido(
    base: _LinhaDeBase, casa: str, dia: date, cidade: str, membros_casa: set[int], presentes: set[int]
) -> Optional[dict[str, Any]]:
    """A mesma conta, só entre parlamentares dos partidos de quem está no encontro."""

    partidos = sorted({base.partido[p] for p in presentes if p in base.partido})
    if not partidos:
        return None
    do_partido = {p for p, sigla in base.partido.items() if sigla in partidos}
    populacao = len(base.viajantes.get(casa, set()) & do_partido)
    marcados = len(membros_casa & base.viajantes.get(casa, set()) & do_partido)
    no_dia = base.dias.get((casa, dia, cidade), set()) & do_partido
    na_lista = len(presentes & do_partido)
    viajantes = len(no_dia) or na_lista
    if populacao < na_lista or populacao == 0:
        return None
    p = _cauda_hipergeometrica(na_lista, populacao, marcados, viajantes)
    return {
        "parties": partidos,
        "travelers": viajantes,
        "others": len(no_dia - membros_casa),
        "expected": round(viajantes * marcados / populacao, 2),
        "p_value": p,
        "unusual": p < P_INCOMUM,
    }


def _pessoa(membro: CollectionMember, e: ParliamentaryExpense, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "member_id": int(membro.id),
        "display_name": membro.display_name,
        "expense_id": int(e.id),
        "value": _num(e.net_value),
        "link": e.document_url,
        **extra,
    }


def _distintos(pessoas: list[dict[str, Any]]) -> int:
    return len({p["member_id"] for p in pessoas})


def travel_overlaps(db: Session, collection_id: int) -> Optional[dict[str, Any]]:
    """Encontros de agenda entre os membros da coleção. None se a coleção não existe."""

    if not tabelas_disponiveis(db, *TABELAS):
        return None
    colecao = _buscar_colecao(db, collection_id=collection_id)
    if colecao is None:
        return None
    chave = (int(colecao.id), str(colecao.updated_at))
    guardado = _cache.get(chave)
    if guardado and time.monotonic() - guardado[0] < _CACHE_SEGUNDOS:
        return guardado[1]

    membros = (
        db.execute(select(CollectionMember).where(CollectionMember.collection_id == colecao.id))
        .scalars()
        .all()
    )
    # Uma pessoa por parlamentar: se o admin cadastrou duas vezes, vale a primeira.
    membro_por_parl: dict[int, tuple[CollectionMember, Parliamentarian]] = {}
    for m, (parl, _cand) in zip(membros, resolver_vinculos(db, membros)):
        if parl is not None:
            membro_por_parl.setdefault(int(parl.id), (m, parl))

    ano_corte = datetime.now(timezone.utc).year - ANOS + 1
    resultado: dict[str, Any] = {"since_year": ano_corte, "events": []}
    if len(membro_por_parl) < 2 or not tabelas_disponiveis(db, "parliamentary_expense"):
        _cache[chave] = (time.monotonic(), resultado)
        return resultado

    gastos = db.execute(
        select(ParliamentaryExpense).where(
            ParliamentaryExpense.parliamentarian_id.in_(membro_por_parl),
            ParliamentaryExpense.year >= ano_corte,
            ParliamentaryExpense.net_value > 0,
            or_(
                ParliamentaryExpense.expense_type.ilike("%passage%"),
                ParliamentaryExpense.expense_type.ilike("%hosped%"),
            ),
        )
    ).scalars()

    por_cidade: dict[tuple[date, str], list[dict[str, Any]]] = defaultdict(list)
    por_voo: dict[tuple[date, str, str], list[dict[str, Any]]] = defaultdict(list)
    hoteis: dict[str, list[tuple[date, dict[str, Any]]]] = defaultdict(list)
    nome_hotel: dict[str, str] = {}

    casa_do_parl: dict[int, str] = {}
    for e in gastos:
        membro, parl = membro_por_parl[int(e.parliamentarian_id)]
        if _e_passagem(e.expense_type):
            for cidade, t in _viagens_validas(e, parl):
                casa_do_parl[int(membro.id)] = e.house
                pessoa = _pessoa(
                    membro, e,
                    {"passenger": t["passenger"], "route": t["route"], "date": t["date"].isoformat(),
                     "date_basis": t["basis"], "flight": t["flight"]},
                )
                por_cidade[(t["date"], cidade)].append(pessoa)
                if t["flight"]:
                    por_voo[(t["date"], t["flight"], t["route"])].append(pessoa)
        elif _e_hospedagem(e) and e.document_date is not None:
            hotel = (e.supplier_id or "").strip() or " ".join((e.supplier_name or "").upper().split())
            if not hotel:
                continue
            nome_hotel.setdefault(hotel, e.supplier_name or hotel)
            hoteis[hotel].append(
                (e.document_date, _pessoa(membro, e, {"date": e.document_date.isoformat(), "date_basis": "invoice"}))
            )

    eventos: list[dict[str, Any]] = []
    voos_vistos: set[tuple[date, str]] = set()
    for (dia, voo, rota), pessoas in por_voo.items():
        if _distintos(pessoas) > 1:
            voos_vistos.add((dia, _cidade(rota.split("/")[-1])[0]))
            eventos.append(
                {"kind": "same_flight", "date": dia.isoformat(), "place": _cidade(rota.split("/")[-1])[0],
                 "detail": f"Voo {voo}, trecho {rota}", "people": pessoas}
            )
    base = _linha_de_base(db, ano_corte) if por_cidade else None
    membros_por_casa: dict[str, set[int]] = defaultdict(set)
    for parl_id, (m, _p) in membro_por_parl.items():
        if int(m.id) in casa_do_parl:
            membros_por_casa[casa_do_parl[int(m.id)]].add(parl_id)
    parl_do_membro = {int(m.id): parl_id for parl_id, (m, _p) in membro_por_parl.items()}
    for (dia, cidade), pessoas in por_cidade.items():
        if _distintos(pessoas) > 1:
            casas = {casa_do_parl.get(p["member_id"]) for p in pessoas}
            contexto = None
            # Câmara (data de emissão) e Senado (data do voo) não se comparam: só casa única.
            if base is not None and len(casas) == 1 and None not in casas:
                casa = casas.pop()
                presentes = {parl_do_membro[p["member_id"]] for p in pessoas}
                contexto = _contexto(base, casa, dia, cidade, membros_por_casa[casa], presentes)
            eventos.append(
                {"kind": "same_city", "date": dia.isoformat(), "place": cidade, "detail": None,
                 "people": pessoas, "also_same_flight": (dia, cidade) in voos_vistos, "context": contexto}
            )
    for hotel, notas in hoteis.items():
        notas.sort(key=lambda n: n[0])
        grupo: list[tuple[date, dict[str, Any]]] = []
        for nota in notas + [(date.max, {})]:
            if grupo and (nota[0] - grupo[-1][0]).days > DIAS_HOTEL:
                pessoas = [p for _, p in grupo]
                if _distintos(pessoas) > 1:
                    eventos.append(
                        {"kind": "same_hotel", "date": grupo[0][0].isoformat(), "place": nome_hotel[hotel],
                         "detail": None, "people": pessoas}
                    )
                grupo = []
            if nota[1]:
                grupo.append(nota)

    # Mais gente primeiro; empate, o mais recente.
    eventos.sort(key=lambda ev: ev["date"], reverse=True)
    eventos.sort(key=lambda ev: _distintos(ev["people"]), reverse=True)
    resultado["events"] = eventos
    if base is not None:
        resultado["control"] = {
            casa: {"travelers": len(base.viajantes.get(casa, ())), "listed": len(ids & base.viajantes.get(casa, set()))}
            for casa, ids in membros_por_casa.items()
        }
    _cache[chave] = (time.monotonic(), resultado)
    return resultado
