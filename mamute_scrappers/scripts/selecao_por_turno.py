"""Foto de quem cada pessoa selecionou em cada turno (CS-129).

`projetos_candidacy` e a selecao viva: desmarcar apaga a linha. Este script
copia para `projetos_candidacy_turno` o que estava selecionado quando a
votacao do turno fechou (17h de Brasilia), e essa foto nao muda mais. E ela
que a busca usa para mostrar "1º", "2º" ou "1º e 2º" ao lado do "+".

- Turno 1: todas as selecoes de candidaturas de 2026 feitas ate o corte.
- Turno 2: so as de candidaturas que foram ao 2o turno (situacao oficial
  "2º turno" no 1o turno), feitas ate o corte do 2o turno.

Liberacao para o 2o turno (CS-130, `--liberar`): quando o TSE encerra a
totalizacao do 1o turno em TODO o pais (mesmo gatilho do e-mail completo), a
selecao viva perde os nao eleitos ("Não eleito", "Suplente", quem nao consta
na totalizacao). Ficam os eleitos e quem foi ao 2o turno. So sai quem esta na
foto do 1o turno (o selo "1º" continua) e so acontece uma vez
(`selecao_liberacao`): se a pessoa marcar de novo, a escolha dela fica.
Roda no fim do cron `resultado-eleicao`, depois do e-mail completo.

Idempotente (ON CONFLICT DO NOTHING): rodar de novo nao duplica nem altera.
O corte e fixo por turno, entao rodar depois (atraso do cron, rodada manual)
nao pega selecao feita depois da votacao.

Cron: 25/10/2026 as 20:05 UTC (17:05 de Brasilia), ver docker/scrappers.cron.
A foto do 1o turno foi tirada em 05/10/2026 (e repetida pela migration cs129).

Uso:
  python -m mamute_scrappers.scripts.selecao_por_turno --turno 2 [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mamute_scrappers.tse_crawler.resultados_parsing import TZ_BRASILIA  # noqa: E402

logger = logging.getLogger(__name__)

CICLO = "ele2026"
ANO = 2026
SITUACAO_SEGUNDO_TURNO = "2º turno"
# Fechamento da votacao de cada turno, em Brasilia.
CORTES = {
    1: datetime(2026, 10, 4, 17, 0, tzinfo=TZ_BRASILIA),
    2: datetime(2026, 10, 25, 17, 0, tzinfo=TZ_BRASILIA),
}


def registrar_turno(
    session: Session, *, turno: int, corte: Optional[datetime] = None, dry_run: bool = False
) -> int:
    """Grava a foto do turno e devolve quantas selecoes entraram agora."""
    corte = corte or CORTES[turno]
    so_segundo_turno = ""
    if turno == 2:
        so_segundo_turno = (
            "AND EXISTS (SELECT 1 FROM candidacy_result r1 WHERE r1.candidacy_id = c.id "
            "AND r1.turno = 1 AND r1.situacao = :seg AND r1.totalizacao_final)"
        )
    selecao = f"""
        SELECT pc.projeto_id, pc.candidacy_id, :ciclo, :turno, pc.created_at
          FROM projetos_candidacy pc
          JOIN candidacy c ON c.id = pc.candidacy_id
         WHERE c.election_year = :ano
           AND pc.created_at <= :corte
           {so_segundo_turno}
    """
    params = {"ciclo": CICLO, "turno": turno, "ano": ANO, "corte": corte, "seg": SITUACAO_SEGUNDO_TURNO}
    if dry_run:
        n = session.execute(text(f"SELECT count(*) FROM ({selecao}) s"), params).scalar() or 0
        logger.info("dry-run: turno %s teria %s selecao(oes) ate %s", turno, n, corte.isoformat())
        return int(n)
    resultado = session.execute(
        text(
            "INSERT INTO projetos_candidacy_turno "
            "(projeto_id, candidacy_id, ciclo, turno, selecionado_em) "
            f"{selecao} ON CONFLICT DO NOTHING"
        ),
        params,
    )
    session.commit()
    n = int(resultado.rowcount or 0)
    logger.info("Turno %s: %s selecao(oes) novas na foto (corte %s)", turno, n, corte.isoformat())
    return n


def _tabela_existe(session: Session, tabela: str) -> bool:
    try:
        return tabela in inspect(session.get_bind()).get_table_names()
    except SQLAlchemyError:
        return False


# Continua selecionado: eleito ("Eleito", "Eleito por QP"...) ou 2o turno,
# pela situacao oficial do TSE. Mesma regra de `foi_eleito` no coletor.
_CONTINUA = """
    EXISTS (
        SELECT 1 FROM candidacy_result r
         WHERE r.candidacy_id = pc.candidacy_id AND r.turno = 1 AND r.totalizacao_final
           AND (LOWER(r.situacao) LIKE 'eleito%' OR r.situacao = :seg)
    )
"""


def liberar_para_segundo_turno(session: Session, *, dry_run: bool = False) -> Optional[int]:
    """Tira da selecao viva os nao eleitos do 1o turno; None = nao era a hora."""
    from mamute_scrappers.scripts.notificacao.resultado_eleicao import (
        CARGOS_TODOS,
        arquivos_pendentes,
    )

    if not _tabela_existe(session, "selecao_liberacao"):
        logger.info("Liberacao: tabela selecao_liberacao ainda nao existe (deploy antes do alembic).")
        return None
    ja = session.execute(
        text("SELECT executado_em FROM selecao_liberacao WHERE ciclo = :c AND turno = 1"),
        {"c": CICLO},
    ).first()
    if ja is not None:
        logger.info("Liberacao do 1o turno ja feita em %s.", ja.executado_em)
        return None
    pendentes = arquivos_pendentes(session, ciclo=CICLO, turno=1, cargos=CARGOS_TODOS)
    if pendentes is None or pendentes:
        logger.info(
            "Liberacao: aguardando %s arquivo(s) do TSE fechar.",
            len(pendentes) if pendentes else "a apuracao",
        )
        return None

    alvo = f"""
        SELECT pc.id FROM projetos_candidacy pc
          JOIN candidacy c ON c.id = pc.candidacy_id
         WHERE c.election_year = :ano
           AND EXISTS (
               SELECT 1 FROM projetos_candidacy_turno t
                WHERE t.projeto_id = pc.projeto_id AND t.candidacy_id = pc.candidacy_id
                  AND t.ciclo = :ciclo AND t.turno = 1
           )
           AND NOT {_CONTINUA}
    """
    params = {"ano": ANO, "ciclo": CICLO, "seg": SITUACAO_SEGUNDO_TURNO}
    if dry_run:
        n = session.execute(text(f"SELECT count(*) FROM ({alvo}) a"), params).scalar() or 0
        logger.info("dry-run: liberacao tiraria %s selecao(oes) de nao eleitos.", n)
        return int(n)
    removidas = session.execute(
        text(f"DELETE FROM projetos_candidacy WHERE id IN ({alvo})"), params
    ).rowcount or 0
    session.execute(
        text("INSERT INTO selecao_liberacao (ciclo, turno, removidas) VALUES (:c, 1, :n)"),
        {"c": CICLO, "n": int(removidas)},
    )
    session.commit()
    logger.info("Liberacao do 1o turno: %s selecao(oes) de nao eleitos sairam da lista.", removidas)
    return int(removidas)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    acao = parser.add_mutually_exclusive_group(required=True)
    acao.add_argument("--turno", type=int, choices=(1, 2), help="Tira a foto do turno.")
    acao.add_argument(
        "--liberar", action="store_true", help="Libera a selecao para o 2o turno (CS-130)."
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    from mamute_scrappers.db.session import get_session

    session = get_session()
    try:
        if args.liberar:
            liberar_para_segundo_turno(session, dry_run=args.dry_run)
        else:
            registrar_turno(session, turno=args.turno, dry_run=args.dry_run)
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
