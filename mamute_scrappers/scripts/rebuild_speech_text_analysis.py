"""Reprocessa palavras-chave e entidades nomeadas de pronunciamentos existentes."""

from __future__ import annotations

import argparse
import logging

from mamute_scrappers.senado_crawler.speechs_transcipts import (
    LOG_LEVEL_CHOICES,
    LOG_LEVEL_NAMES,
    rebuild_speech_text_analysis,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reexecuta a extração de palavras-chave e entidades (NER) para discursos já coletados."
        )
    )
    parser.add_argument(
        "--parliamentarian",
        type=int,
        dest="parliamentarian_code",
        help="Filtra por código do parlamentar (Câmara ou Senado).",
    )
    parser.add_argument(
        "--parliamentarian-type",
        choices=("Deputado", "Senador"),
        help="Restringe a uma casa: Deputado ou Senador.",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Processa só discursos com texto e ainda sem palavras-chave.",
    )
    parser.add_argument(
        "--model",
        dest="analyzer_model",
        help=(
            "Nome do modelo spaCy (ex.: pt_core_news_md) ou do ChatGPT "
            "(ex.: gpt-4o-mini)."
        ),
    )
    parser.add_argument(
        "--keyword-limit",
        type=int,
        default=15,
        help="Número máximo de palavras-chave salvas por discurso (padrão: 15).",
    )
    parser.add_argument(
        "--analysis-type",
        dest="analysis_type",
        default="spacy",
        help="Identificador da análise (ex.: spacy, chatgpt). Padrão: spacy.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Quantidade de discursos processados antes de cada commit (padrão: 100).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limita o total de discursos processados.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=LOG_LEVEL_CHOICES,
        help="Define o nível de log geral (padrão: INFO).",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(level=LOG_LEVEL_NAMES[args.log_level])
    rebuild_speech_text_analysis(
        parliamentarian_code=args.parliamentarian_code,
        analyzer_model=args.analyzer_model,
        keyword_limit=args.keyword_limit,
        batch_size=args.batch_size,
        limit=args.limit,
        analysis_type=args.analysis_type,
        parliamentarian_type=args.parliamentarian_type,
        only_missing=args.only_missing,
    )


if __name__ == "__main__":
    main()
