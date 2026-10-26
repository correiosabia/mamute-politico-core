"""Leitura dos arquivos de urna do TSE: boletim de urna (BU) e log (CS-163).

Funcoes puras, sem rede nem banco.

BU (`*-bu.dat`, `*.bu`): ASN.1 em DER, mesmo formato em 2022 e 2026. Em vez
de compilar a especificacao inteira, o leitor percorre a arvore e acha os
campos pela forma, que e estavel entre as versoes:

- o conteudo do boletim e o OCTET STRING do 5o campo do envelope;
- identificacao da secao (filho direto do boletim): SEQUENCE
  [[municipio, zona], local, secao];
- abertura e encerramento: o filho direto do boletim com duas datas
  "aaaammddThhmmss";
- resultado de uma eleicao: SEQUENCE que comeca pelo codigo da eleicao. Dentro
  dela, o comparecimento vem logo depois de um ENUMERATED, e cada votavel e
  uma SEQUENCE [tipo (0x81), quantidade (0x82), identificacao (0xa3)?]
  (tipo 1 nominal, 2 branco, 3 nulo; identificacao = [partido, numero]).

As horas do BU e do log estao no relogio local da urna. A conversao para o
horario de Brasilia usa `fuso_do_municipio` (a votacao abre as 8h de Brasilia).

Log (`*-log.jez` em 2026, `.logjez` em 2022): zip (2026) ou 7z (2022) com o
`logd.dat` em latin-1, uma linha por evento, separada por TAB. Cada voto e a
linha "O voto do eleitor foi computado".
"""

from __future__ import annotations

import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

FRASE_VOTO = "O voto do eleitor foi computado"
HORA_ABERTURA_BRASILIA = 8
_DATA = re.compile(rb"\d{8}T\d{6}")

TIPO_NOMINAL, TIPO_BRANCO, TIPO_NULO = 1, 2, 3
_UNIVERSAL_INT, _UNIVERSAL_ENUM, _UNIVERSAL_OCTET = 0x02, 0x0A, 0x04


@dataclass
class _No:
    tag: int
    valor: bytes
    filhos: List["_No"] = field(default_factory=list)

    @property
    def construido(self) -> bool:
        return bool(self.tag & 0x20)

    def inteiro(self) -> int:
        return int.from_bytes(self.valor, "big", signed=True)

    def eh_data(self) -> bool:
        return not self.construido and bool(_DATA.fullmatch(self.valor))


def _tlv(buf: bytes, i: int) -> Tuple[int, int, int]:
    tag = buf[i]
    i += 1
    if tag & 0x1F == 0x1F:
        while buf[i] & 0x80:
            i += 1
        i += 1
    tam = buf[i]
    i += 1
    if tam & 0x80:
        n = tam & 0x7F
        tam = int.from_bytes(buf[i : i + n], "big")
        i += n
    return tag, i, tam


def _decodificar(buf: bytes) -> List[_No]:
    nos: List[_No] = []
    i = 0
    while i < len(buf):
        tag, ini, tam = _tlv(buf, i)
        valor = buf[ini : ini + tam]
        no = _No(tag, valor)
        if no.construido:
            no.filhos = _decodificar(valor)
        nos.append(no)
        i = ini + tam
    return nos


def _percorrer(no: _No):
    yield no
    for f in no.filhos:
        yield from _percorrer(f)


def _data(no: _No) -> datetime:
    return datetime.strptime(no.valor.decode("ascii"), "%Y%m%dT%H%M%S")


@dataclass(frozen=True)
class BoletimUrna:
    municipio: int
    zona: int
    local: int
    secao: int
    abertura: datetime
    encerramento: datetime
    comparecimento: int
    # Votos por numero do candidato ("13"), mais "branco" e "nulo".
    votos: Dict[str, int]


def _identificacao(boletim: _No) -> Tuple[int, int, int, int]:
    # So nos filhos diretos: a correspondencia da urna tem uma identificacao
    # parecida, com outro "local".
    for no in boletim.filhos:
        f = no.filhos
        if (
            len(f) == 3
            and len(f[0].filhos) == 2
            and all(x.tag == _UNIVERSAL_INT for x in f[0].filhos)
            and f[1].tag == _UNIVERSAL_INT
            and f[2].tag == _UNIVERSAL_INT
        ):
            return f[0].filhos[0].inteiro(), f[0].filhos[1].inteiro(), f[1].inteiro(), f[2].inteiro()
    raise ValueError("BU sem identificacao da secao")


def _horarios(boletim: _No) -> Tuple[datetime, datetime]:
    for no in boletim.filhos:
        if len(no.filhos) >= 2 and no.filhos[0].eh_data() and no.filhos[1].eh_data():
            return _data(no.filhos[0]), _data(no.filhos[1])
    raise ValueError("BU sem abertura e encerramento")


def _resultado(raiz: _No, cd_eleicao: int) -> Tuple[int, Dict[str, int]]:
    for no in _percorrer(raiz):
        f = no.filhos
        if not (f and f[0].tag == _UNIVERSAL_INT and f[0].inteiro() == cd_eleicao and len(f) > 2):
            continue
        comparecimento: Optional[int] = None
        votos: Dict[str, int] = {}
        for sub in _percorrer(no):
            s = sub.filhos
            if comparecimento is None and len(s) >= 2 and s[0].tag == _UNIVERSAL_ENUM and s[1].tag == _UNIVERSAL_INT:
                comparecimento = s[1].inteiro()
            elif len(s) >= 2 and s[0].tag == 0x81 and s[1].tag == 0x82:
                tipo, qtd = s[0].inteiro(), s[1].inteiro()
                if tipo == TIPO_NOMINAL:
                    ident = next((x for x in s if x.tag == 0xA3), None)
                    if ident is None or len(ident.filhos) < 2:
                        continue
                    chave = str(ident.filhos[1].inteiro())
                elif tipo == TIPO_BRANCO:
                    chave = "branco"
                elif tipo == TIPO_NULO:
                    chave = "nulo"
                else:
                    continue
                votos[chave] = votos.get(chave, 0) + qtd
        if comparecimento is None:
            raise ValueError(f"BU sem comparecimento na eleicao {cd_eleicao}")
        return comparecimento, votos
    raise ValueError(f"BU sem a eleicao {cd_eleicao}")


def parse_bu(dados: bytes, *, cd_eleicao: int) -> BoletimUrna:
    envelope = _decodificar(dados)[0]
    conteudo = next(f for f in envelope.filhos if f.tag == _UNIVERSAL_OCTET)
    boletim = _decodificar(conteudo.valor)[0]
    municipio, zona, local, secao = _identificacao(boletim)
    abertura, encerramento = _horarios(boletim)
    comparecimento, votos = _resultado(boletim, cd_eleicao)
    return BoletimUrna(municipio, zona, local, secao, abertura, encerramento, comparecimento, votos)


def extrair_log(dados: bytes) -> str:
    """Texto do `logd.dat` de um `log.jez` (zip em 2026, 7z em 2022)."""
    if dados[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(dados)) as z:
            nome = next(n for n in z.namelist() if n.endswith("logd.dat"))
            bruto = z.read(nome)
    else:
        import py7zr

        with py7zr.SevenZipFile(io.BytesIO(dados)) as z:
            arquivos = z.readall()
            nome = next(n for n in arquivos if n.endswith("logd.dat"))
            bruto = arquivos[nome].read()
    return bruto.decode("latin-1")


def votos_do_log(texto: str, *, dia: date) -> List[datetime]:
    """Hora (relogio da urna) de cada voto computado no dia da eleicao."""
    prefixo = dia.strftime("%d/%m/%Y")
    votos = []
    for linha in texto.splitlines():
        if not linha.startswith(prefixo):
            continue
        partes = linha.split("\t")
        if len(partes) >= 5 and partes[4] == FRASE_VOTO:
            votos.append(datetime.strptime(partes[0], "%d/%m/%Y %H:%M:%S"))
    return votos


def fuso_do_municipio(aberturas: Iterable[datetime]) -> int:
    """Horas a somar no relogio local para chegar ao horario de Brasilia.

    A votacao abre as 8h de Brasilia; a abertura mais comum do municipio diz
    o fuso (urna de contingencia aberta tarde nao muda a moda). Abertura um
    pouco antes da hora cheia (7h59) conta como a hora seguinte.
    """
    candidatos = Counter()
    for a in aberturas:
        hora = (a + timedelta(minutes=15)).hour
        candidatos[HORA_ABERTURA_BRASILIA - hora] += 1
    if not candidatos:
        return 0
    return candidatos.most_common(1)[0][0]


@dataclass(frozen=True)
class FaixasVotos:
    total: int
    apos_17: int
    de_17_a_19: int
    apos_19: int
    ultimo: Optional[datetime]


def classificar_votos(votos_locais: Iterable[datetime], *, fuso: int) -> FaixasVotos:
    """Conta os votos por faixa de horario, ja em horario de Brasilia."""
    total = apos_17 = de_17_a_19 = apos_19 = 0
    ultimo: Optional[datetime] = None
    for v in votos_locais:
        b = v + timedelta(hours=fuso)
        total += 1
        if ultimo is None or b > ultimo:
            ultimo = b
        if b.hour >= 17:
            apos_17 += 1
            if b.hour >= 19:
                apos_19 += 1
            else:
                de_17_a_19 += 1
    return FaixasVotos(total, apos_17, de_17_a_19, apos_19, ultimo)
