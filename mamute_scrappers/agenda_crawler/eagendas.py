"""Agendas do Executivo federal pelo e-Agendas da CGU (CS-135).

Fonte: dados abertos da CGU, um ZIP com um CSV por mês desde out/2022 (todo o
Executivo federal: ministérios, agências, autarquias, BC, CVM, PF, universidades),
https://dadosabertos-download.cgu.gov.br/dados_e-agendas/dados_e-agendas.zip
Sem chave. A API v2 do e-Agendas exige token gov.br e a busca do site está
quebrada; o ZIP é o caminho estável. Atualização mensal.

São ~1,3 milhão de compromissos (1,5 GB descompactado). Não guardamos tudo: só
o que cita algum TERMO MONITORADO, que vem de `collection.settings.agenda_terms`
de todas as coleções (mais os passados por --termo). Como o ZIP traz o histórico
inteiro, cada coleta substitui todas as linhas desta fonte: termo novo passa a
valer na coleta seguinte, termo retirado some.

O mesmo compromisso sai uma vez para cada agente público que participou (ID do
registro repetido). Guardamos uma linha por agente; a API junta na leitura.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import sys
import tempfile
import unicodedata
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402

logger = logging.getLogger(__name__)

SOURCE = "eagendas"
ZIP_URL = "https://dadosabertos-download.cgu.gov.br/dados_e-agendas/dados_e-agendas.zip"
PAGE_URL = "https://eagendas.cgu.gov.br/"
REQUEST_TIMEOUT = 600
MIN_TERMO = 4

_VAZIO = {"", "ND", "N/D", "NA"}
_ESPACOS = re.compile(r"\s+")


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def normaliza(texto: Optional[str]) -> str:
    return _ESPACOS.sub(" ", _sem_acento(texto or "").lower()).strip()


def _campo(valor: Optional[str]) -> Optional[str]:
    v = _ESPACOS.sub(" ", (valor or "").replace("&amp;", "&")).strip()
    return None if v.upper() in _VAZIO else v


def _titulo(texto: Optional[str]) -> Optional[str]:
    """'MINISTRO DE ESTADO DO TRABALHO' -> 'Ministro de Estado do Trabalho'."""
    if not texto:
        return None
    if texto != texto.upper():
        return texto
    minusculas = {"de", "da", "do", "das", "dos", "e", "a", "o", "em"}
    palavras = texto.lower().split(" ")
    return " ".join(p if (i and p in minusculas) else p[:1].upper() + p[1:] for i, p in enumerate(palavras))


def _data(texto: Optional[str]) -> Optional[date]:
    try:
        return datetime.strptime((texto or "").strip(), "%d-%m-%Y").date()
    except ValueError:
        return None


def _hora(texto: Optional[str]) -> Optional[str]:
    t = (texto or "").strip()
    return t if re.fullmatch(r"\d{2}:\d{2}", t) else None


def casa_termo(linha: dict[str, str], termos_norm: list[str]) -> bool:
    texto = normaliza(
        " ".join(
            linha.get(c, "")
            for c in ("Assunto do Compromisso", "Participantes", "Detalhes", "Informações complementares")
        )
    )
    return any(t in texto for t in termos_norm)


def build_row(linha: dict[str, str]) -> Optional[dict[str, Any]]:
    """Linha de `official_agenda_item` para um compromisso de um agente."""

    dia = _data(linha.get("Data de início"))
    registro = (linha.get("ID do registro") or "").strip()
    nome = _campo(linha.get("Nome"))
    assunto = _campo(linha.get("Assunto do Compromisso"))
    if dia is None or not registro or not nome or not assunto:
        return None
    cargo = _titulo(_campo(linha.get("Cargo/Função")))
    return {
        "source": SOURCE,
        # O ID se repete por agente: a chave é o par.
        "authority_id": f"{registro}:{normaliza(nome)}",
        "authority_name": _titulo(nome),
        "office": cargo,
        "office_label": cargo,
        "event_date": dia,
        "seq": 0,
        "starts_at": _hora(linha.get("Hora de início")),
        "ends_at": _hora(linha.get("Hora de término")),
        "description": assunto,
        "place": _campo(linha.get("Local")),
        "remote": (linha.get("Forma de realização") or "").strip().lower() == "virtual",
        "url": PAGE_URL,
        "organization": _campo(linha.get("Órgão/Entidade")),
        "participants": _campo(linha.get("Participantes")),
    }


def linhas_do_zip(caminho: Path) -> Iterator[dict[str, str]]:
    csv.field_size_limit(10**8)
    with zipfile.ZipFile(caminho) as z:
        for nome in sorted(n for n in z.namelist() if n.endswith(".csv")):
            with z.open(nome) as f:
                yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def filtra(linhas: Iterable[dict[str, str]], termos: list[str]) -> list[dict[str, Any]]:
    termos_norm = [normaliza(t) for t in termos if len(normaliza(t)) >= MIN_TERMO]
    if not termos_norm:
        return []
    vistos: set[tuple[str, date]] = set()
    saida = []
    for linha in linhas:
        if not casa_termo(linha, termos_norm):
            continue
        row = build_row(linha)
        if row is None or (row["authority_id"], row["event_date"]) in vistos:
            continue
        vistos.add((row["authority_id"], row["event_date"]))
        saida.append(row)
    return saida


def termos_monitorados(session: Any) -> list[str]:
    """Termos de agenda de todas as coleções (rascunho ou publicada)."""
    from sqlalchemy import text

    termos: list[str] = []
    for (lista,) in session.execute(text("SELECT settings -> 'agenda_terms' FROM collection")):
        if isinstance(lista, list):
            termos.extend(str(t) for t in lista)
    return sorted({t.strip() for t in termos if t and t.strip()})


def baixa_zip(destino: Path) -> Path:
    with requests.get(ZIP_URL, stream=True, timeout=REQUEST_TIMEOUT) as resp:
        resp.raise_for_status()
        with destino.open("wb") as f:
            for parte in resp.iter_content(chunk_size=1 << 20):
                f.write(parte)
    return destino


def substitui(session: Any, rows: list[dict[str, Any]]) -> None:
    from sqlalchemy import text

    session.execute(text("DELETE FROM official_agenda_item WHERE source = :s"), {"s": SOURCE})
    if rows:
        session.execute(
            text(
                "INSERT INTO official_agenda_item (source, authority_id, authority_name, office, "
                "office_label, event_date, seq, starts_at, ends_at, description, place, remote, url, "
                "organization, participants) "
                "VALUES (:source, :authority_id, :authority_name, :office, :office_label, :event_date, "
                ":seq, :starts_at, :ends_at, :description, :place, :remote, :url, :organization, :participants)"
            ),
            rows,
        )


def collect(*, extras: list[str], zip_local: Optional[Path] = None, persist: bool = True) -> dict[str, int]:
    if persist:
        from mamute_scrappers.db import session_scope

        contexto = session_scope()
    else:
        from contextlib import nullcontext

        contexto = nullcontext(None)

    with contexto as session, tempfile.TemporaryDirectory() as tmp:
        termos = sorted(set(extras) | set(termos_monitorados(session) if session is not None else []))
        logger.info("Termos monitorados: %s", termos)
        if not termos:
            logger.info("Nenhum termo monitorado: nada a coletar.")
            return {"termos": 0, "compromissos": 0}
        caminho = zip_local or baixa_zip(Path(tmp) / "e-agendas.zip")
        rows = filtra(linhas_do_zip(caminho), termos)
        logger.info("=== e-Agendas: %s compromissos citam os termos ===", len(rows))
        if session is not None:
            from mamute_scrappers.agenda_crawler.alerta import avisar_novos, foto

            antes = foto(session, SOURCE)
            # O ZIP é o histórico inteiro: substituir mantém a fonte igual ao conjunto de termos atual.
            substitui(session, rows)
            session.commit()
            avisar_novos(session, SOURCE, antes)
        else:
            logger.info("Modo dry-run: nada foi gravado.")
    return {"termos": len(termos), "compromissos": len(rows)}


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Coleta do e-Agendas (CGU) filtrada pelos termos monitorados.")
    parser.add_argument("--termo", action="append", default=[], help="Termo extra (pode repetir).")
    parser.add_argument("--zip", type=Path, help="Usa um ZIP já baixado em vez de baixar.")
    parser.add_argument("--dry-run", action="store_true", help="Não grava.")
    args = parser.parse_args()
    collect(extras=args.termo, zip_local=args.zip, persist=not args.dry_run)
