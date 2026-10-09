"""Orquestração do envio em lote."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

from mamute_scrappers.db import session_scope
from mamute_scrappers.db.session import get_session

from .config import (
    PERIOD_DAYS,
    PERIODICIDADE_TESTE,
    default_highlight_limit,
    subject_for_periodicidade,
)
from .mailer import send_html_email
from .models import ProjectRecipient
from .flags import FLAG_DESTAQUES_GERAIS, carregar_flags, emails_admin
from .geral import build_general_highlights
from .settings import load_email_settings
from .share import share_code_for
from .report_builder import EnvioContexto, build_project_report, render_report_html
from .repository import (
    date_range_for_period,
    get_recipient_by_id,
    list_recipients_for_periodicity,
)
from .send_log import (
    STATUS_ERROR,
    STATUS_SENT,
    STATUS_SENT_GENERAL,
    STATUS_SENT_NO_ACTIVITY,
    STATUS_SKIPPED_NO_ACTIVITY,
    STATUS_SKIPPED_NO_FAVORITES,
    log_send_attempt,
)

logger = logging.getLogger(__name__)

_OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def _save_html(projeto_id: int, periodicidade: str, html_body: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"projeto_{projeto_id}_{periodicidade}.html"
    path.write_text(html_body, encoding="utf-8")
    return path


def _process_recipient(
    recipient: ProjectRecipient,
    periodicidade: str,
    *,
    dry_run: bool,
    highlight_limit: int,
    skip_empty: bool,
    save_html: bool,
    output_dir: Path,
    contexto: Optional[EnvioContexto] = None,
) -> str:
    session = get_session()

    # dry-run é simulação: não entra no histórico de envios.
    def _log(status: str, **kwargs: object) -> None:
        if dry_run:
            return
        log_send_attempt(
            session,
            projeto_id=recipient.id,
            email=recipient.email,
            periodicidade=periodicidade,
            status=status,
            **kwargs,  # type: ignore[arg-type]
        )

    try:
        report = build_project_report(
            session,
            recipient,
            periodicidade,
            highlight_limit=highlight_limit,
            contexto=contexto,
        )
        if report is None:
            _log(STATUS_SKIPPED_NO_FAVORITES)
            return f"projeto {recipient.id}: sem parlamentares favoritos"

        report_stats = {
            "proposicoes": report.stats.propositions_count,
            "votacoes": report.stats.votes_count,
            "discursos": report.stats.speeches_count,
            "emendas": report.stats.amendments_count,
            "destaques": len(report.highlights),
            "parlamentares": len(report.parliamentarians),
        }
        if skip_empty and not report.tem_atividade and report.motivo_geral is None:
            _log(
                STATUS_SKIPPED_NO_ACTIVITY,
                stats=report_stats,
                period_start=report.range_start,
                period_end=report.range_end,
            )
            return f"projeto {recipient.id}: sem atividade no período"

        if report.design_novo:
            _atribuir_links_curtos(session, report)
        html_body = render_report_html(
            report,
            periodicidade,
            settings=contexto.settings if contexto else None,
        )

        if save_html or dry_run:
            path = _save_html(recipient.id, periodicidade, html_body, output_dir)
            if dry_run:
                return (
                    f"projeto {recipient.id}: dry-run ({recipient.email}) "
                    f"→ {path}"
                )

        if dry_run:
            return f"projeto {recipient.id}: dry-run ({recipient.email})"

        subject = subject_for_periodicidade(periodicidade)
        send_html_email(html_body, recipient.email, subject)
        status = {
            "sem_selecao": STATUS_SENT_GENERAL,
            "sem_atividade": STATUS_SENT_NO_ACTIVITY,
        }.get(report.motivo_geral or "", STATUS_SENT)
        _log(
            status,
            subject=subject,
            stats=report_stats,
            period_start=report.range_start,
            period_end=report.range_end,
        )
        return f"projeto {recipient.id}: enviado para {recipient.email}"
    except Exception as exc:
        logger.exception("Erro no projeto %s", recipient.id)
        try:
            session.rollback()  # transação pode estar suja se o erro veio do DB
        except Exception:  # noqa: BLE001
            pass
        _log(STATUS_ERROR, detail=str(exc)[:2000])
        return f"projeto {recipient.id}: erro — {exc}"
    finally:
        session.close()


def _atribuir_links_curtos(session, report) -> None:
    """Código de compartilhamento de cada destaque (só o design novo mostra)."""
    casas = {fav.display_name: fav.chamber for fav in report.favorite_parliamentarians}
    itens = list(report.highlights)
    if report.geral:
        itens.extend(report.geral.votacoes)
    for item in itens:
        item.share_code = share_code_for(
            session, item, chamber=casas.get(item.parliamentarian_name, "")
        )


def resolve_recipients(
    periodicidade: str,
    *,
    projeto_id: Optional[int] = None,
    include_without_tier: bool = False,
) -> List[ProjectRecipient]:
    with session_scope() as session:
        if projeto_id is not None:
            recipient = get_recipient_by_id(session, projeto_id)
            if recipient is None:
                raise RuntimeError(f"Projeto {projeto_id} não encontrado ou inativo.")
            return [recipient]
        return list_recipients_for_periodicity(
            session,
            periodicidade,
            include_without_tier=include_without_tier,
        )


def carregar_contexto(periodicidade: str) -> EnvioContexto:
    """Flags e destaques gerais, uma vez por envio (e não por conta)."""
    with session_scope() as session:
        snapshot = carregar_flags(session)
        geral = None
        dias = PERIOD_DAYS.get(periodicidade)
        if dias and snapshot.estados.get(FLAG_DESTAQUES_GERAIS, "off") != "off":
            inicio, fim, _, _ = date_range_for_period(dias)
            try:
                geral = build_general_highlights(session, inicio, fim)
            except Exception:  # noqa: BLE001 (sem destaques gerais o envio segue como antes)
                logger.exception("Destaques gerais indisponíveis; quem não tem atividade será pulado.")
                session.rollback()
        settings = load_email_settings(session)
    return EnvioContexto(flags=snapshot, admins=emails_admin(), geral=geral, settings=settings)


def run(
    periodicidade: str,
    *,
    dry_run: bool = False,
    projeto_id: Optional[int] = None,
    highlight_limit: Optional[int] = None,
    skip_empty: bool = True,
    include_without_tier: bool = False,
    max_workers: Optional[int] = None,
    list_only: bool = False,
    save_html: bool = False,
    output_dir: Optional[Path] = None,
) -> List[str]:
    """Executa o envio (ou simulação) para todos os destinatários elegíveis."""
    results: List[str] = []
    limit = highlight_limit if highlight_limit is not None else default_highlight_limit(
        periodicidade
    )
    out_dir = output_dir or _OUTPUT_DIR

    recipients = resolve_recipients(
        periodicidade,
        projeto_id=projeto_id,
        include_without_tier=include_without_tier,
    )

    if list_only:
        for recipient in recipients:
            line = f"{recipient.id}\t{recipient.email}\t{recipient.nome}"
            print(line)
            results.append(line)
        return results

    if not recipients:
        logger.warning("Nenhum destinatário elegível.")
        return results

    if periodicidade == PERIODICIDADE_TESTE and not dry_run and projeto_id is None:
        logger.warning(
            "Modo %s envia para TODOS os projetos ativos. "
            "Use --dry-run ou --projeto-id em testes.",
            periodicidade,
        )

    contexto = carregar_contexto(periodicidade)
    logger.info("Flags do e-mail: %s", contexto.flags.estados or "todas desligadas")

    workers = max_workers or os.cpu_count() or 4
    logger.info(
        "Processando %s projeto(s) com %s worker(s). dry_run=%s limite=%s",
        len(recipients),
        workers,
        dry_run,
        limit,
    )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _process_recipient,
                recipient,
                periodicidade,
                dry_run=dry_run,
                highlight_limit=limit,
                skip_empty=skip_empty,
                save_html=save_html,
                output_dir=out_dir,
                contexto=contexto,
            )
            for recipient in recipients
        ]
        for future in as_completed(futures):
            message = future.result()
            logger.info(message)
            results.append(message)

    return results
