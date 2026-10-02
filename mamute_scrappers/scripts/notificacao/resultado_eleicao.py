"""Aviso do resultado da eleicao a quem acompanha candidatos (CS-106).

Roda depois da coleta (`tse_crawler.resultados`), no mesmo job de cron.

Quem recebe, por turno:
- turno 1: todo projeto ativo que acompanha candidatura do ano do ciclo;
- turno 2: so quem acompanha candidato que foi ao 2o turno (`situacao`
  "2º turno" no turno 1), e o aviso lista so esses candidatos.

Quando: assim que TODOS os candidatos da pessoa estao em arquivo do TSE com a
totalizacao encerrada (`tse_result_file.totalizacao_final`). Candidato que
nao aparece num arquivo encerrado (indeferido, renuncia) entra como "Nao
consta na totalizacao do TSE" e nao trava o envio.

Liga/desliga: flag `resultado_eleicao`, com a MESMA regra de
`api/services/feature_flags.resolve_for` que decide o modal no app, para
e-mail e modal nunca divergirem:
  off (ou sem linha) -> ninguem (a coleta continua gravando);
  admins            -> so admins (MAMUTE_ADMIN_EMAILS);
  all               -> admins + projetos cujo plano tem a flag `liberado` em
                       `feature_flag_tier` (a migration cs106 semeia isso nos
                       planos que ja tem a busca de candidaturas liberada).

Sem duplicata: `election_result_notice` tem unique (projeto, ciclo, turno). A
linha nasce `pending` antes do envio; vira `sent`, `error` (tenta de novo nas
proximas rodadas, ate MAX_TENTATIVAS) ou `skipped_no_email`. O `payload`
gravado e a foto do que foi enviado e e o que o modal mostra.

Uso:
  python -m mamute_scrappers.scripts.notificacao.resultado_eleicao [--turno 1|2]
         [--dry-run] [--projeto-id N] [--ignorar-flag]
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mamute_scrappers.scripts.notificacao.config import (  # noqa: E402
    EmailBranding,
    get_branding,
)
from mamute_scrappers.scripts.notificacao.send_log import (  # noqa: E402
    STATUS_ERROR,
    STATUS_SENT,
    log_send_attempt,
)

logger = logging.getLogger(__name__)

FLAG_KEY = "resultado_eleicao"
DEFAULT_CICLO = "ele2026"
SITUACAO_SEGUNDO_TURNO = "2º turno"
NAO_CONSTA = "Não consta na totalização do TSE"
SEM_SITUACAO = "Sem situação informada pelo TSE"
MAX_TENTATIVAS = 3

STATUS_PENDING = "pending"
STATUS_SKIPPED_NO_EMAIL = "skipped_no_email"

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "resultado_eleicao.html"
_OUTPUT_DIR = Path(__file__).resolve().parent / "output"

SendFn = Callable[[str, str, str], None]  # (html, to, subject)


@dataclass
class AvisoPronto:
    projeto_id: int
    email: Optional[str]
    nome: str
    turno: int
    tier_id: Optional[int] = None
    itens: List[dict] = field(default_factory=list)

    def payload(self) -> dict:
        return {"turno": self.turno, "itens": self.itens}


@dataclass
class EnvioStats:
    flag: str = "off"
    prontos: int = 0
    aguardando: int = 0
    enviados: int = 0
    ja_enviados: int = 0
    erros: int = 0
    sem_email: int = 0
    fora_do_recorte: int = 0

    def resumo(self) -> str:
        return (
            f"flag={self.flag} prontos={self.prontos} aguardando={self.aguardando} "
            f"enviados={self.enviados} ja_enviados={self.ja_enviados} erros={self.erros} "
            f"sem_email={self.sem_email} fora_do_recorte={self.fora_do_recorte}"
        )


def ano_do_ciclo(ciclo: str) -> int:
    return int("".join(ch for ch in ciclo if ch.isdigit()))


def assunto(turno: int) -> str:
    if turno == 2:
        return "Saiu o resultado do 2º turno dos candidatos que você acompanha"
    return "Saiu o resultado dos candidatos que você acompanha"


def estado_da_flag(session: Session) -> str:
    try:
        row = session.execute(
            text("SELECT state FROM feature_flag WHERE key = :k"), {"k": FLAG_KEY}
        ).first()
    except Exception:  # noqa: BLE001 — tabela ausente = desligado
        session.rollback()
        return "off"
    return (row.state if row else "off") or "off"


def planos_liberados(session: Session) -> set[int]:
    try:
        rows = session.execute(
            text(
                "SELECT tier_id FROM feature_flag_tier "
                "WHERE flag_key = :k AND mode = 'liberado'"
            ),
            {"k": FLAG_KEY},
        ).all()
    except Exception:  # noqa: BLE001 — tabela ausente = nenhum plano
        session.rollback()
        return set()
    return {int(r.tier_id) for r in rows}


def emails_admin() -> frozenset[str]:
    raw = os.getenv("MAMUTE_ADMIN_EMAILS", "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def _arquivos_finais(session: Session, ciclo: str, turno: int) -> set[tuple[int, str]]:
    rows = session.execute(
        text(
            "SELECT cargo_codigo, uf FROM tse_result_file "
            "WHERE ciclo = :ciclo AND turno = :turno AND totalizacao_final"
        ),
        {"ciclo": ciclo, "turno": turno},
    ).all()
    return {(int(r.cargo_codigo), r.uf.lower()) for r in rows}


def montar_avisos(
    session: Session,
    *,
    ciclo: str,
    turno: int,
    projeto_id: Optional[int] = None,
) -> tuple[List[AvisoPronto], int]:
    """Avisos prontos para envio e quantos projetos ainda aguardam o TSE."""
    filtro_turno2 = ""
    if turno == 2:
        filtro_turno2 = (
            "AND EXISTS (SELECT 1 FROM candidacy_result r1 "
            "WHERE r1.candidacy_id = c.id AND r1.turno = 1 AND r1.situacao = :seg)"
        )
    filtro_projeto = "AND p.id = :projeto_id" if projeto_id is not None else ""
    rows = session.execute(
        text(
            f"""
            SELECT p.id AS projeto_id, p.email, p.nome, p.tier_id,
                   c.id AS candidacy_id, c.ballot_name, c.full_name, c.office,
                   c.office_code, c.state, c.party, c.ballot_number,
                   r.situacao, r.votos, r.percentual
              FROM projetos_candidacy pc
              JOIN projetos p ON p.id = pc.projeto_id AND p.deleted_at IS NULL
              JOIN candidacy c ON c.id = pc.candidacy_id
              LEFT JOIN candidacy_result r
                     ON r.candidacy_id = c.id AND r.turno = :turno
             WHERE c.election_year = :ano
               {filtro_turno2}
               {filtro_projeto}
             ORDER BY p.id, c.office_code, c.ballot_name
            """
        ),
        {
            "turno": turno,
            "ano": ano_do_ciclo(ciclo),
            "seg": SITUACAO_SEGUNDO_TURNO,
            "projeto_id": projeto_id,
        },
    ).all()

    finais = _arquivos_finais(session, ciclo, turno)
    por_projeto: Dict[int, AvisoPronto] = {}
    aguardando: set[int] = set()
    for row in rows:
        aviso = por_projeto.setdefault(
            row.projeto_id,
            AvisoPronto(
                projeto_id=row.projeto_id,
                email=row.email,
                nome=row.nome or "",
                turno=turno,
                tier_id=row.tier_id,
            ),
        )
        uf = (row.state or "").lower()
        if row.office_code is None or (int(row.office_code), uf) not in finais:
            aguardando.add(row.projeto_id)
            continue
        if row.situacao:
            situacao = row.situacao
        elif row.votos is not None:
            situacao = SEM_SITUACAO
        else:
            situacao = NAO_CONSTA
        aviso.itens.append(
            {
                "candidacy_id": int(row.candidacy_id),
                "nome": row.ballot_name or row.full_name or "",
                "numero": row.ballot_number,
                "cargo": row.office or "",
                "uf": row.state or "",
                "partido": row.party or "",
                "situacao": situacao,
                "votos": int(row.votos) if row.votos is not None else None,
                "percentual": float(row.percentual) if row.percentual is not None else None,
            }
        )

    prontos = [a for pid, a in por_projeto.items() if pid not in aguardando and a.itens]
    return prontos, len(aguardando)


def _formata_votos(votos: Optional[int], percentual: Optional[float]) -> str:
    if votos is None:
        return ""
    texto = f"{votos:,}".replace(",", ".") + " votos"
    if percentual is not None:
        texto += f" ({percentual:.2f}%)".replace(".", ",")
    return texto


def _greeting_name(nome: str) -> str:
    first = nome.split("_")[0].strip()
    return (first or nome or "").upper()


def render_html(aviso: AvisoPronto, *, branding: EmailBranding | None = None) -> str:
    brand = branding or get_branding()
    linhas = []
    for item in aviso.itens:
        detalhe = " · ".join(
            p for p in (f"{item['cargo']} ({item['uf']})", item["partido"], _formata_votos(item["votos"], item["percentual"])) if p
        )
        linhas.append(
            '<tr><td style="padding:10px 0;border-bottom:1px solid #eee;">'
            f'<strong style="color:#111;">{html.escape(item["nome"])}</strong><br>'
            f'<span class="muted">{html.escape(detalhe)}</span></td>'
            '<td style="padding:10px 0 10px 12px;border-bottom:1px solid #eee;text-align:right;'
            f'white-space:nowrap;"><strong>{html.escape(item["situacao"])}</strong></td></tr>'
        )
    tabela = (
        '<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
        + "".join(linhas)
        + "</table>"
    )
    if aviso.turno == 2:
        intro = "O TSE encerrou a totalização do 2º turno. Veja como ficaram os candidatos que você acompanha:"
    else:
        intro = "O TSE encerrou a totalização dos cargos dos candidatos que você acompanha. Veja como cada um ficou:"
    # O app mora em /app (manage_url); app_url e a home do site.
    link = f"{brand.manage_url.rstrip('/')}/candidaturas"
    replacements = {
        "{{SUBJECT}}": html.escape(assunto(aviso.turno)),
        "{{LOGO_URL}}": html.escape(brand.logo_url),
        "{{PRIVACY_URL}}": html.escape(brand.privacy_url),
        "{{MANAGE_URL}}": html.escape(brand.manage_url),
        "{{GREETING_NAME}}": html.escape(_greeting_name(aviso.nome)),
        "{{INTRO}}": html.escape(intro),
        "{{RESULTADOS}}": tabela,
        "{{FONTE}}": "Situação conforme a divulgação oficial do Tribunal Superior Eleitoral.",
        "{{FOOTER}}": (
            "Veja seus candidatos em "
            f'<a href="{html.escape(link)}">{html.escape(link)}</a>.'
        ),
    }
    out = _TEMPLATE_PATH.read_text(encoding="utf-8")
    for key, value in replacements.items():
        out = out.replace(key, value)
    return out


def _reservar(session: Session, aviso: AvisoPronto, ciclo: str) -> Optional[dict]:
    """Garante a linha do aviso e devolve o estado atual dela."""
    session.execute(
        text(
            """
            INSERT INTO election_result_notice (projeto_id, ciclo, turno, payload, email_status)
            VALUES (:projeto_id, :ciclo, :turno, :payload, :status)
            ON CONFLICT (projeto_id, ciclo, turno) DO NOTHING
            """
        ),
        {
            "projeto_id": aviso.projeto_id,
            "ciclo": ciclo,
            "turno": aviso.turno,
            "payload": json.dumps(aviso.payload(), ensure_ascii=False),
            "status": STATUS_PENDING,
        },
    )
    session.commit()
    row = session.execute(
        text(
            "SELECT id, email_status, tentativas, payload FROM election_result_notice "
            "WHERE projeto_id = :p AND ciclo = :c AND turno = :t"
        ),
        {"p": aviso.projeto_id, "c": ciclo, "t": aviso.turno},
    ).first()
    return dict(row._mapping) if row else None


def _atualizar(session: Session, notice_id: int, **campos: object) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in campos)
    session.execute(
        text(f"UPDATE election_result_notice SET {sets} WHERE id = :id"),
        {"id": notice_id, **campos},
    )
    session.commit()


def enviar(
    session: Session,
    *,
    ciclo: str = DEFAULT_CICLO,
    turnos: Sequence[int] = (1, 2),
    send: Optional[SendFn] = None,
    dry_run: bool = False,
    projeto_id: Optional[int] = None,
    ignorar_flag: bool = False,
    admins: Optional[frozenset[str]] = None,
    save_html_dir: Optional[Path] = None,
) -> EnvioStats:
    stats = EnvioStats(flag=estado_da_flag(session))
    if stats.flag == "off" and not ignorar_flag:
        logger.info("Flag %s desligada: nenhum aviso enviado.", FLAG_KEY)
        return stats
    if send is None:
        from mamute_scrappers.scripts.notificacao.mailer import send_html_email

        def send(body: str, to: str, subject: str) -> None:
            send_html_email(body, to, subject)

    admins = admins if admins is not None else emails_admin()
    liberados = planos_liberados(session)

    def recebe(aviso: AvisoPronto) -> bool:
        if ignorar_flag:
            return True
        if (aviso.email or "").strip().lower() in admins:
            return True
        return stats.flag == "all" and aviso.tier_id is not None and int(aviso.tier_id) in liberados

    for turno in turnos:
        prontos, aguardando = montar_avisos(session, ciclo=ciclo, turno=turno, projeto_id=projeto_id)
        stats.aguardando += aguardando
        for aviso in prontos:
            if not recebe(aviso):
                stats.fora_do_recorte += 1
                continue
            stats.prontos += 1
            subject = assunto(turno)

            if dry_run:
                corpo = render_html(aviso)
                if save_html_dir is not None:
                    save_html_dir.mkdir(parents=True, exist_ok=True)
                    (save_html_dir / f"resultado_{ciclo}_t{turno}_projeto_{aviso.projeto_id}.html").write_text(
                        corpo, encoding="utf-8"
                    )
                logger.info("dry-run: projeto %s turno %s, %s candidato(s)", aviso.projeto_id, turno, len(aviso.itens))
                continue

            notice = _reservar(session, aviso, ciclo)
            if notice is None:
                continue
            status = notice["email_status"]
            if status in (STATUS_SENT, STATUS_SKIPPED_NO_EMAIL):
                stats.ja_enviados += 1
                continue
            if status == STATUS_ERROR and int(notice["tentativas"]) >= MAX_TENTATIVAS:
                stats.erros += 1
                continue

            # Reenvio usa a foto gravada: o e-mail e o modal mostram a mesma coisa.
            payload = notice["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            aviso.itens = payload.get("itens", aviso.itens)

            email = (aviso.email or "").strip()
            if not email:
                _atualizar(session, notice["id"], email_status=STATUS_SKIPPED_NO_EMAIL)
                stats.sem_email += 1
                continue

            periodicidade = f"eleicao_{ciclo}_t{turno}"
            try:
                send(render_html(aviso), email, subject)
            except Exception as exc:  # noqa: BLE001 — um e-mail nao derruba os outros
                logger.warning("Falha ao enviar aviso ao projeto %s: %s", aviso.projeto_id, exc)
                _atualizar(
                    session,
                    notice["id"],
                    email_status=STATUS_ERROR,
                    tentativas=int(notice["tentativas"]) + 1,
                    ultimo_erro=str(exc)[:500],
                )
                log_send_attempt(
                    session, projeto_id=aviso.projeto_id, email=email,
                    periodicidade=periodicidade, status=STATUS_ERROR,
                    detail=str(exc)[:500], subject=subject,
                )
                stats.erros += 1
                continue

            _atualizar(
                session,
                notice["id"],
                email_status=STATUS_SENT,
                tentativas=int(notice["tentativas"]) + 1,
                sent_at=datetime.now(timezone.utc),
                ultimo_erro=None,
            )
            log_send_attempt(
                session, projeto_id=aviso.projeto_id, email=email,
                periodicidade=periodicidade, status=STATUS_SENT, subject=subject,
                stats={"candidatos": len(aviso.itens)},
            )
            stats.enviados += 1

    return stats


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Envia o aviso do resultado da eleição.")
    parser.add_argument("--turno", type=int, choices=(1, 2), action="append", help="Padrão: 1 e 2.")
    parser.add_argument("--dry-run", action="store_true", help="Monta e grava HTML em output/, sem enviar.")
    parser.add_argument("--projeto-id", type=int, default=None)
    parser.add_argument(
        "--ignorar-flag",
        action="store_true",
        help="Envia mesmo com a flag desligada e para todos (uso manual, com --projeto-id).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if args.ignorar_flag and args.projeto_id is None and not args.dry_run:
        logging.error("--ignorar-flag exige --projeto-id (ou --dry-run): evita disparo em massa sem querer.")
        return 2

    from mamute_scrappers.db.session import get_session

    ciclo = os.getenv("MAMUTE_TSE_RESULTADOS_CICLO", DEFAULT_CICLO).strip() or DEFAULT_CICLO
    session = get_session()
    try:
        stats = enviar(
            session,
            ciclo=ciclo,
            turnos=tuple(args.turno or (1, 2)),
            dry_run=args.dry_run,
            projeto_id=args.projeto_id,
            ignorar_flag=args.ignorar_flag,
            save_html_dir=_OUTPUT_DIR if args.dry_run else None,
        )
    finally:
        session.close()
    logger.info("Aviso do resultado %s: %s", ciclo, stats.resumo())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
