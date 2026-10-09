"""Resumo curto (até duas frases) de cada discurso recente, para o relatório por e-mail.

A Câmara e o Senado publicam um sumário de cada discurso
(`speeches_transcripts.summary`), mas a mediana tem ~520 caracteres: longo
demais para um item de e-mail. A regra:

* sumário de até 280 caracteres: vai como está (fonte `oficial`);
* sumário maior: o modelo condensa o próprio sumário (`ia_sumario`), o que
  mantém o resumo preso ao texto oficial;
* sem sumário: o modelo resume o começo da transcrição (`ia_transcricao`).

Grava em `speech_short_summary`, um discurso por commit. Falha do modelo não
grava nada para aquele discurso e o lote segue: o e-mail mostra o discurso sem
resumo, só com o link. Discurso já resumido é pulado, então rodar de novo só
paga pelo que é novo.

Uso: python -m mamute_scrappers.scripts.speech_short_summary [--dias 20] [--limite 600]
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from mamute_scrappers.scripts.notificacao.labels import cortar_na_frase

logger = logging.getLogger(__name__)

MAX_OFICIAL = 280
MAX_RESUMO = 240
MAX_TRANSCRICAO = 6000

FONTE_OFICIAL = "oficial"
FONTE_IA_SUMARIO = "ia_sumario"
FONTE_IA_TRANSCRICAO = "ia_transcricao"

PROMPT_SISTEMA = (
    "Você resume discursos de parlamentares brasileiros para um boletim informativo. "
    "Escreva em português do Brasil, no máximo duas frases curtas e no máximo 240 caracteres. "
    "Diga do que o discurso trata, usando só o que está no texto recebido. "
    "Não opine, não adjetive, não acrescente contexto de fora e não cite o nome do parlamentar. "
    "Responda só com o resumo, sem aspas e sem prefixo."
)

def _limpo(valor: Optional[str]) -> str:
    return " ".join((valor or "").split())


def escolher_fonte(summary: Optional[str], speech_text: Optional[str]) -> Optional[tuple[str, str]]:
    """Devolve (fonte, texto de entrada) ou None quando não há o que resumir."""
    sumario = _limpo(summary)
    if sumario and len(sumario) <= MAX_OFICIAL:
        return FONTE_OFICIAL, sumario
    if sumario:
        return FONTE_IA_SUMARIO, sumario
    transcricao = _limpo(speech_text)
    if transcricao:
        return FONTE_IA_TRANSCRICAO, transcricao[:MAX_TRANSCRICAO]
    return None


def resumir(client: Any, fonte: str, texto: str, *, model: str) -> str:
    origem = "Sumário oficial do discurso" if fonte == FONTE_IA_SUMARIO else "Trecho da transcrição do discurso"
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": PROMPT_SISTEMA},
            {"role": "user", "content": f"{origem}:\n\n{texto}"},
        ],
        temperature=0.2,
    )
    conteudo = completion.choices[0].message.content if completion.choices else ""
    return cortar_na_frase((conteudo or "").strip().strip('"'), MAX_RESUMO)


def _pendentes(session: Session, desde: date, limite: int) -> list[tuple[int, Optional[str], Optional[str]]]:
    rows = session.execute(
        text(
            "SELECT s.id, s.summary, s.speech_text FROM speeches_transcripts s "
            "LEFT JOIN speech_short_summary r ON r.speech_id = s.id "
            "WHERE r.speech_id IS NULL AND s.date >= :desde "
            "ORDER BY s.date DESC, s.id DESC LIMIT :limite"
        ),
        {"desde": desde, "limite": limite},
    ).all()
    return [(int(r[0]), r[1], r[2]) for r in rows]


def _gravar(session: Session, speech_id: int, resumo: str, fonte: str, model: Optional[str]) -> None:
    session.execute(
        text(
            "INSERT INTO speech_short_summary (speech_id, text, source, model) "
            "VALUES (:id, :text, :source, :model) ON CONFLICT (speech_id) DO NOTHING"
        ),
        {"id": speech_id, "text": resumo, "source": fonte, "model": model},
    )
    session.commit()


def run(
    session: Session,
    client: Any,
    *,
    hoje: Optional[date] = None,
    dias: int = 20,
    limite: int = 600,
    model: Optional[str] = None,
) -> dict[str, int]:
    """`client=None` grava só os oficiais (sem chave de IA, o resto espera a próxima rodada)."""
    from mamute_scrappers.scripts.classify_editorial_agendas import DEFAULT_MODEL

    modelo = model or DEFAULT_MODEL
    desde = (hoje or date.today()) - timedelta(days=dias)
    contadores = {FONTE_OFICIAL: 0, FONTE_IA_SUMARIO: 0, FONTE_IA_TRANSCRICAO: 0, "falhas": 0, "sem_ia": 0, "vazios": 0}

    for speech_id, summary, speech_text in _pendentes(session, desde, limite):
        escolha = escolher_fonte(summary, speech_text)
        if escolha is None:
            contadores["vazios"] += 1
            continue
        fonte, entrada = escolha
        if fonte == FONTE_OFICIAL:
            _gravar(session, speech_id, entrada, fonte, None)
            contadores[fonte] += 1
            continue
        if client is None:
            contadores["sem_ia"] += 1
            continue
        try:
            resumo = resumir(client, fonte, entrada, model=modelo)
        except Exception:  # noqa: BLE001 (um discurso não derruba o lote)
            logger.warning("Falha ao resumir o discurso %s.", speech_id, exc_info=True)
            contadores["falhas"] += 1
            continue
        if not resumo:
            contadores["falhas"] += 1
            continue
        _gravar(session, speech_id, resumo, fonte, modelo)
        contadores[fonte] += 1

    logger.info("Resumo curto dos discursos: %s", contadores)
    return contadores


def main() -> None:
    parser = argparse.ArgumentParser(description="Resumo curto dos discursos recentes (e-mail).")
    parser.add_argument("--dias", type=int, default=20)
    parser.add_argument("--limite", type=int, default=600)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from mamute_scrappers.db.session import session_scope
    from mamute_scrappers.scripts.classify_editorial_agendas import construir_cliente

    try:
        client = construir_cliente()
    except RuntimeError as exc:
        logger.warning("Sem cliente de IA (%s); só os sumários oficiais curtos serão gravados.", exc)
        client = None

    with session_scope() as session:
        contadores = run(session, client, dias=args.dias, limite=args.limite, model=args.model)

    tentou_ia = contadores["falhas"] + contadores[FONTE_IA_SUMARIO] + contadores[FONTE_IA_TRANSCRICAO]
    if contadores["falhas"] and contadores["falhas"] == tentou_ia:
        logger.error("Todas as chamadas ao modelo falharam nesta rodada.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
