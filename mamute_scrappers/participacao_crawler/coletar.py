"""Coleta de participação de todas as fontes (CS-179).

    python -m mamute_scrappers.participacao_crawler.coletar [--fonte NOME] [--dry-run]

Fontes: brasil_participativo, senado, camara. Cada fonte grava e faz commit
sozinha: uma fonte fora do ar não impede as outras. `--dry-run` só baixa e
mostra o que seria gravado.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("participacao")

FONTES = ("brasil_participativo", "senado", "camara")


def _dry_run(fonte: str) -> None:
    from mamute_scrappers.participacao_crawler import brasil_participativo, camara, senado
    from mamute_scrappers.participacao_crawler.comum import baixar

    if fonte == "brasil_participativo":
        itens, todos = brasil_participativo.build_items(brasil_participativo.fetch(), date.today())
        print(f"brasil_participativo: {len(itens)} no recorte de {len(todos)} processos")
    elif fonte == "senado":
        itens = senado.build_consultas(baixar(senado.CONSULTAS_URL).text)
        itens += senado.build_ideias(baixar(senado.IDEIAS_URL).text)
    else:
        itens = camara.build_lista(baixar(camara.LISTA_URL).text)
    for i in itens:
        print(f"  [{i['source']}] {i['reference'] or ''} {i['title'][:90]} | até {i['closes_on'] or 'sem prazo'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fonte", choices=FONTES, action="append")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    falhas = 0
    for fonte in args.fonte or FONTES:
        try:
            if args.dry_run:
                _dry_run(fonte)
                continue
            from mamute_scrappers.db import session_scope
            from mamute_scrappers.participacao_crawler import brasil_participativo, camara, senado

            with session_scope() as session:
                if fonte == "brasil_participativo":
                    r = brasil_participativo.collect(session, date.today())
                elif fonte == "senado":
                    r = senado.collect(session)
                else:
                    r = camara.collect(session)
            logger.info("participacao %s: %s", fonte, r)
        except Exception:  # noqa: BLE001 - uma fonte fora do ar não derruba as outras
            falhas += 1
            logger.exception("participacao %s: falhou", fonte)
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())
