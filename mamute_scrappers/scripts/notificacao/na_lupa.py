"""Aviso por e-mail a quem segue um parlamentar que entrou num caso do Na Lupa (CS-137).

Pedido do Luiz (06/10): quem monitora um parlamentar fica sabendo quando ele
aparece num caso publicado do Na Lupa.

Quando dispara: o job roda no cron (a cada 15 minutos) e olha as pessoas de
casos **publicados** ligadas a um parlamentar da base, seja pelo
`parliamentarian_id` do membro, pelo CPF ou pela candidatura (mesma resolução
da página). O "momento" de cada pessoa no caso é o mais recente entre a
publicação do caso e a inclusão da pessoa; entra quem tem esse momento dentro
da janela (`JANELA_DIAS`, padrão 7 dias). Assim:

- publicar um caso avisa quem segue as pessoas dele;
- incluir alguém num caso já publicado avisa quem segue essa pessoa;
- caso antigo não dispara sozinho no dia em que a flag liga: para avisar
  sobre ele, rodar à mão com `--desde AAAA-MM-DD`.

Uma vez só: `na_lupa_notice` tem unique (projeto, caso, parlamentar).
Republicar, mudar o texto ou o nível da pessoa não reenvia. O e-mail sai
consolidado por projeto x caso: quem segue três pessoas do mesmo caso recebe
um e-mail com as três.

Nível: cada pessoa vai com o nível de citação do caso (`tier_labels`) e a
explicação dele (`settings.tier_descriptions`, ou o texto padrão da página),
e o e-mail termina dizendo que estar no caso, por si só, não quer dizer ser
investigado. Citado não é investigado.

Quem recebe: flag `email_na_lupa`, com a regra de
`api/services/feature_flags.resolve_for`:
  off (ou sem linha) -> ninguém;
  admins            -> só admins (MAMUTE_ADMIN_EMAILS);
  all               -> admins + projetos cujo plano tem a flag `liberado`
                       (tela de Planos). É lá que se decide quais planos recebem.
Quem pediu para parar de receber e-mails (CS-150) fica `skipped_opt_out`.

Uso:
  python -m mamute_scrappers.scripts.notificacao.na_lupa [--dry-run]
         [--projeto-id N] [--caso SLUG] [--desde AAAA-MM-DD] [--ignorar-flag]
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mamute_scrappers.scripts.notificacao.config import (  # noqa: E402
    EmailBranding,
    get_branding,
)
from mamute_scrappers.scripts.notificacao.descadastro import (  # noqa: E402
    ids_desativados,
    link_rodape_html,
    links_descadastro,
    tokens_por_projeto,
)
from mamute_scrappers.scripts.notificacao.send_log import (  # noqa: E402
    STATUS_ERROR,
    STATUS_SENT,
    log_send_attempt,
)

logger = logging.getLogger(__name__)

FLAG_KEY = "email_na_lupa"
JANELA_DIAS = 7
MAX_TENTATIVAS = 3
MAX_CONTEXTO = 280

STATUS_PENDING = "pending"
STATUS_SKIPPED_NO_EMAIL = "skipped_no_email"
STATUS_SKIPPED_OPT_OUT = "skipped_opt_out"

# Mesmo texto de apoio que a página usa quando o caso não traz o seu
# (DESCRICAO_PADRAO em ui/src/components/na-lupa/helpers.ts, no app).
DESCRICAO_PADRAO: Dict[str, str] = {
    "1": "Foi alvo de busca, tem investigação aberta ou a Polícia Federal aponta participação direta.",
    "2": "Aparece em mensagens, pagamentos, viagens ou delação. Não quer dizer que seja investigado.",
    "3": "Só tinha o número salvo no celular de Vorcaro. Isso, sozinho, não indica nada.",
}
ROTULO_SEM_NIVEL = "Autoridade citada"  # rotuloNivel() da página

AVISO_NIVEL = (
    "Estar num caso do Na Lupa, por si só, não quer dizer ser investigado nem ter "
    "cometido irregularidade. O nível mostra por que a pessoa aparece, e a página do "
    "caso traz a fonte de cada citação."
)

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "na_lupa.html"
_OUTPUT_DIR = Path(__file__).resolve().parent / "output"

SendFn = Callable[[str, str, str], Optional[str]]  # (html, to, subject) -> Message-ID


@dataclass
class AvisoNaLupa:
    projeto_id: int
    email: Optional[str]
    nome: str
    tier_id: Optional[int]
    caso: dict  # id, slug, title, subtitle
    itens: List[dict] = field(default_factory=list)
    email_token: Optional[str] = None


@dataclass
class EnvioStats:
    flag: str = "off"
    prontos: int = 0
    enviados: int = 0
    ja_enviados: int = 0
    erros: int = 0
    sem_email: int = 0
    fora_do_recorte: int = 0
    descadastrados: int = 0

    def resumo(self) -> str:
        return (
            f"flag={self.flag} prontos={self.prontos} enviados={self.enviados} "
            f"ja_enviados={self.ja_enviados} erros={self.erros} sem_email={self.sem_email} "
            f"fora_do_recorte={self.fora_do_recorte} descadastrados={self.descadastrados}"
        )


# --------------------------------------------------------------------------- #
# Flag e planos (mesma regra do app)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Texto do nível
# --------------------------------------------------------------------------- #
def _json(valor: Any) -> dict:
    if isinstance(valor, str):
        try:
            valor = json.loads(valor)
        except ValueError:
            return {}
    return valor if isinstance(valor, dict) else {}


def nivel_da_pessoa(tier: Optional[int], tier_labels: Any, settings: Any) -> tuple[str, Optional[str]]:
    """(rótulo, explicação) do nível, com as mesmas regras da página do caso."""
    if tier is None:
        return ROTULO_SEM_NIVEL, None
    chave = str(int(tier))
    rotulo = _json(tier_labels).get(chave) or f"Nível {chave}"
    descricoes = _json(settings).get("tier_descriptions")
    if not isinstance(descricoes, dict):
        descricoes = DESCRICAO_PADRAO
    explicacao = (descricoes.get(chave) or "").strip() or None
    return f"Nível {chave}: {rotulo}", explicacao


def _corta(texto: Optional[str], limite: int = MAX_CONTEXTO) -> Optional[str]:
    texto = " ".join((texto or "").split())
    if not texto:
        return None
    if len(texto) <= limite:
        return texto
    corte = texto[:limite].rsplit(" ", 1)[0].rstrip(",;:")
    return corte + "…"


# --------------------------------------------------------------------------- #
# Quem recebe o quê
# --------------------------------------------------------------------------- #
def _dt(valor: Any) -> Optional[datetime]:
    if valor is None:
        return None
    if isinstance(valor, str):
        try:
            valor = datetime.fromisoformat(valor.replace("Z", "+00:00"))
        except ValueError:
            return None
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor


def montar_avisos(
    session: Session,
    *,
    desde: datetime,
    projeto_id: Optional[int] = None,
    caso: Optional[str] = None,
) -> List[AvisoNaLupa]:
    """Um aviso por projeto x caso, com as pessoas seguidas que entraram desde `desde`."""
    filtros = []
    params: Dict[str, Any] = {"publicado": "published"}
    if projeto_id is not None:
        filtros.append("AND p.id = :projeto_id")
        params["projeto_id"] = projeto_id
    if caso is not None:
        filtros.append("AND c.slug = :caso")
        params["caso"] = caso
    rows = session.execute(
        text(
            f"""
            SELECT p.id AS projeto_id, p.email, p.nome, p.tier_id,
                   c.id AS collection_id, c.slug, c.title, c.subtitle,
                   c.tier_labels, c.settings, c.published_at, c.updated_at AS collection_updated_at,
                   mb.member_id, mb.display_name, mb.role_label, mb.tier, mb.context,
                   mb.member_created_at, mb.parliamentarian_id, mb.position
              FROM (
                    SELECT m.id AS member_id, m.collection_id, m.display_name, m.role_label,
                           m.tier, m.context, m.position, m.created_at AS member_created_at,
                           COALESCE(m.parliamentarian_id, pcpf.id, cand.parliamentarian_id)
                               AS parliamentarian_id
                      FROM collection_member m
                      LEFT JOIN parliamentarian pcpf
                        ON m.parliamentarian_id IS NULL AND m.cpf IS NOT NULL AND pcpf.cpf = m.cpf
                      LEFT JOIN candidacy cand ON cand.id = m.candidacy_id
                   ) mb
              JOIN collection c ON c.id = mb.collection_id AND c.status = :publicado
              JOIN projetos_parliamentarian pp ON pp.parliamentarian_id = mb.parliamentarian_id
              JOIN projetos p ON p.id = pp.projeto_id AND p.deleted_at IS NULL
             WHERE mb.parliamentarian_id IS NOT NULL
             {' '.join(filtros)}
             ORDER BY p.id, c.id, mb.position, mb.member_id
            """
        ),
        params,
    ).mappings().all()

    avisos: Dict[tuple[int, int], AvisoNaLupa] = {}
    vistos: set[tuple[int, int, int]] = set()
    for r in rows:
        chave = (int(r["projeto_id"]), int(r["collection_id"]), int(r["parliamentarian_id"]))
        if chave in vistos:  # CPF repetido ou duas linhas da mesma pessoa
            continue
        publicado_em = _dt(r["published_at"]) or _dt(r["collection_updated_at"])
        incluido_em = _dt(r["member_created_at"])
        momento = max(d for d in (publicado_em, incluido_em) if d is not None) if (publicado_em or incluido_em) else None
        if momento is None or momento < desde:
            continue
        vistos.add(chave)
        rotulo, explicacao = nivel_da_pessoa(r["tier"], r["tier_labels"], r["settings"])
        aviso = avisos.setdefault(
            chave[:2],
            AvisoNaLupa(
                projeto_id=chave[0],
                email=r["email"],
                nome=r["nome"] or "",
                tier_id=int(r["tier_id"]) if r["tier_id"] is not None else None,
                caso={
                    "id": chave[1],
                    "slug": r["slug"],
                    "title": r["title"],
                    "subtitle": r["subtitle"],
                },
            ),
        )
        aviso.itens.append(
            {
                "parliamentarian_id": chave[2],
                "member_id": int(r["member_id"]),
                "nome": r["display_name"],
                "papel": r["role_label"],
                "tier": r["tier"],
                "nivel": rotulo,
                "explicacao": explicacao,
                "contexto": _corta(r["context"]),
                # Entrou depois da publicação: o e-mail fala em "atualizamos o caso".
                "incluido_depois": bool(
                    publicado_em and incluido_em and incluido_em > publicado_em + timedelta(minutes=5)
                ),
            }
        )
    return list(avisos.values())


# --------------------------------------------------------------------------- #
# E-mail
# --------------------------------------------------------------------------- #
def assunto(aviso: AvisoNaLupa) -> str:
    titulo = aviso.caso["title"]
    if len(aviso.itens) == 1:
        return f"{aviso.itens[0]['nome']} aparece no caso “{titulo}”, do Na Lupa"
    return f"{len(aviso.itens)} parlamentares que você acompanha aparecem no caso “{titulo}”, do Na Lupa"


def introducao(aviso: AvisoNaLupa) -> str:
    titulo = aviso.caso["title"]
    abre = (
        f"Atualizamos o caso “{titulo}”, no Na Lupa."
        if all(i["incluido_depois"] for i in aviso.itens)
        else f"Publicamos no Na Lupa o caso “{titulo}”."
    )
    quem = (
        "Um parlamentar que você acompanha aparece nele:"
        if len(aviso.itens) == 1
        else "Parlamentares que você acompanha aparecem nele:"
    )
    return f"{abre} {quem}"


def _greeting_name(nome: str) -> str:
    first = nome.split("_")[0].strip()
    return (first or nome or "").upper()


def link_do_caso(brand: EmailBranding, slug: str) -> str:
    # O app mora em /app (manage_url); app_url é a home do site.
    return f"{brand.manage_url.rstrip('/')}/na-lupa/{slug}"


def render_html(aviso: AvisoNaLupa, *, branding: EmailBranding | None = None) -> str:
    brand = branding or get_branding()
    linhas = []
    for item in aviso.itens:
        papel = (
            f'<br><span class="muted">{html.escape(item["papel"])}</span>' if item.get("papel") else ""
        )
        explicacao = (
            f'<br><span class="muted">{html.escape(item["explicacao"])}</span>'
            if item.get("explicacao")
            else ""
        )
        contexto = (
            f'<p style="margin:8px 0 0;color:#444;font-size:15px;">{html.escape(item["contexto"])}</p>'
            if item.get("contexto")
            else ""
        )
        linhas.append(
            '<tr><td style="padding:14px 0;border-bottom:1px solid #efeeee;vertical-align:top;">'
            f'<strong style="color:#090909;font-size:16px;">{html.escape(item["nome"])}</strong>{papel}<br>'
            '<span style="display:inline-block;margin-top:6px;padding:2px 10px;border-radius:12px;'
            'background:#efeeee;color:#393939;font-size:12px;font-weight:bold;">'
            f'{html.escape(item["nivel"])}</span>{explicacao}{contexto}</td></tr>'
        )
    link = link_do_caso(brand, aviso.caso["slug"])
    subtitulo = (
        f'<p class="muted" style="margin:0 0 12px;">{html.escape(aviso.caso["subtitle"])}</p>'
        if aviso.caso.get("subtitle")
        else ""
    )
    replacements = {
        "{{SUBJECT}}": html.escape(assunto(aviso)),
        "{{LOGO_URL}}": html.escape(brand.logo_url),
        "{{PRIVACY_URL}}": html.escape(brand.privacy_url),
        "{{MANAGE_URL}}": html.escape(brand.manage_url),
        "{{DESCADASTRO}}": link_rodape_html(brand, aviso.email_token),
        "{{GREETING_NAME}}": html.escape(_greeting_name(aviso.nome)),
        "{{INTRO}}": html.escape(introducao(aviso)),
        "{{SUBTITULO}}": subtitulo,
        "{{PESSOAS}}": (
            '<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
            + "".join(linhas)
            + "</table>"
        ),
        "{{LINK_CASO}}": html.escape(link),
        "{{AVISO_NIVEL}}": html.escape(AVISO_NIVEL),
        "{{FOOTER}}": html.escape(
            "Você recebe este aviso porque acompanha "
            + ("este parlamentar" if len(aviso.itens) == 1 else "estes parlamentares")
            + " no Mamute."
        ),
    }
    out = _TEMPLATE_PATH.read_text(encoding="utf-8")
    for key, value in replacements.items():
        out = out.replace(key, value)
    return out


# --------------------------------------------------------------------------- #
# Envio
# --------------------------------------------------------------------------- #
def _reservar(session: Session, aviso: AvisoNaLupa) -> Dict[int, dict]:
    """Garante uma linha por pessoa do aviso e devolve o estado de cada uma."""
    for item in aviso.itens:
        session.execute(
            text(
                """
                INSERT INTO na_lupa_notice
                    (projeto_id, collection_id, parliamentarian_id, member_id, tier, tier_label, email_status)
                VALUES (:projeto_id, :collection_id, :parliamentarian_id, :member_id, :tier, :tier_label, :status)
                ON CONFLICT (projeto_id, collection_id, parliamentarian_id) DO NOTHING
                """
            ),
            {
                "projeto_id": aviso.projeto_id,
                "collection_id": aviso.caso["id"],
                "parliamentarian_id": item["parliamentarian_id"],
                "member_id": item["member_id"],
                "tier": item["tier"],
                "tier_label": item["nivel"],
                "status": STATUS_PENDING,
            },
        )
    session.commit()
    rows = session.execute(
        text(
            "SELECT id, parliamentarian_id, email_status, tentativas FROM na_lupa_notice "
            "WHERE projeto_id = :p AND collection_id = :c AND parliamentarian_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {
            "p": aviso.projeto_id,
            "c": aviso.caso["id"],
            "ids": [i["parliamentarian_id"] for i in aviso.itens],
        },
    ).mappings().all()
    return {int(r["parliamentarian_id"]): dict(r) for r in rows}


def _atualizar(session: Session, ids: Sequence[int], **campos: object) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in campos)
    session.execute(
        text(f"UPDATE na_lupa_notice SET {sets} WHERE id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        ),
        {"ids": list(ids), **campos},
    )
    session.commit()


def _ainda_a_enviar(linha: dict) -> bool:
    status = linha["email_status"]
    if status == STATUS_PENDING:
        return True
    return status == STATUS_ERROR and int(linha["tentativas"]) < MAX_TENTATIVAS


def enviar(
    session: Session,
    *,
    send: Optional[SendFn] = None,
    dry_run: bool = False,
    projeto_id: Optional[int] = None,
    caso: Optional[str] = None,
    desde: Optional[datetime] = None,
    agora: Optional[datetime] = None,
    ignorar_flag: bool = False,
    admins: Optional[frozenset[str]] = None,
    save_html_dir: Optional[Path] = None,
) -> EnvioStats:
    stats = EnvioStats(flag=estado_da_flag(session))
    if stats.flag == "off" and not ignorar_flag:
        logger.info("Flag %s desligada: nenhum aviso do Na Lupa enviado.", FLAG_KEY)
        return stats

    if not dry_run and not inspect(session.get_bind()).has_table("na_lupa_notice"):
        # O deploy sobe o código antes do alembic: sem a trava, não envia nada.
        logger.warning("Tabela na_lupa_notice ausente (migration cs137 pendente): nada enviado.")
        return stats

    agora = agora or datetime.now(timezone.utc)
    desde = desde or agora - timedelta(days=JANELA_DIAS)

    # List-Unsubscribe de cada destinatário; o `send` injetado nos testes não vê.
    one_click_por_email: Dict[str, str] = {}
    if send is None:
        from mamute_scrappers.scripts.notificacao.mailer import (
            TAG_MAMUTE,
            TAG_NA_LUPA,
            send_html_email,
        )

        def send(body: str, to: str, subject: str) -> Optional[str]:
            return send_html_email(
                body,
                to,
                subject,
                unsubscribe_url=one_click_por_email.get(to),
                tags=(TAG_MAMUTE, TAG_NA_LUPA),
            )

    admins = admins if admins is not None else emails_admin()
    liberados = planos_liberados(session)

    def recebe(aviso: AvisoNaLupa) -> bool:
        if ignorar_flag:
            return True
        if (aviso.email or "").strip().lower() in admins:
            return True
        return stats.flag == "all" and aviso.tier_id is not None and aviso.tier_id in liberados

    avisos = montar_avisos(session, desde=desde, projeto_id=projeto_id, caso=caso)
    ids = [a.projeto_id for a in avisos]
    desativados = ids_desativados(session, ids)
    tokens = tokens_por_projeto(session, ids)
    brand = get_branding()

    for aviso in avisos:
        aviso.email_token = tokens.get(aviso.projeto_id)
        if not recebe(aviso):
            stats.fora_do_recorte += 1
            continue

        if dry_run:
            stats.prontos += 1
            if save_html_dir is not None:
                save_html_dir.mkdir(parents=True, exist_ok=True)
                (save_html_dir / f"na_lupa_{aviso.caso['slug']}_projeto_{aviso.projeto_id}.html").write_text(
                    render_html(aviso), encoding="utf-8"
                )
            logger.info(
                "dry-run: projeto %s, caso %s, %s pessoa(s)",
                aviso.projeto_id, aviso.caso["slug"], len(aviso.itens),
            )
            continue

        linhas = _reservar(session, aviso)
        aviso.itens = [
            i for i in aviso.itens
            if i["parliamentarian_id"] in linhas and _ainda_a_enviar(linhas[i["parliamentarian_id"]])
        ]
        if not aviso.itens:
            stats.ja_enviados += 1
            continue
        stats.prontos += 1
        ids_linhas = [linhas[i["parliamentarian_id"]]["id"] for i in aviso.itens]
        tentativas = max(int(linhas[i["parliamentarian_id"]]["tentativas"]) for i in aviso.itens)

        email = (aviso.email or "").strip()
        if not email:
            _atualizar(session, ids_linhas, email_status=STATUS_SKIPPED_NO_EMAIL)
            stats.sem_email += 1
            continue
        if aviso.projeto_id in desativados:
            _atualizar(session, ids_linhas, email_status=STATUS_SKIPPED_OPT_OUT)
            stats.descadastrados += 1
            continue
        links = links_descadastro(brand, aviso.email_token)
        if links is not None:
            one_click_por_email[email] = links.one_click

        subject = assunto(aviso)
        periodicidade = f"na_lupa_{aviso.caso['slug']}"
        try:
            message_id = send(render_html(aviso), email, subject)
        except Exception as exc:  # noqa: BLE001 — um e-mail não derruba os outros
            logger.warning("Falha ao enviar aviso do Na Lupa ao projeto %s: %s", aviso.projeto_id, exc)
            _atualizar(
                session,
                ids_linhas,
                email_status=STATUS_ERROR,
                tentativas=tentativas + 1,
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
            ids_linhas,
            email_status=STATUS_SENT,
            tentativas=tentativas + 1,
            sent_at=datetime.now(timezone.utc),
            ultimo_erro=None,
        )
        log_send_attempt(
            session, projeto_id=aviso.projeto_id, email=email,
            periodicidade=periodicidade, status=STATUS_SENT, subject=subject,
            stats={"pessoas": len(aviso.itens)},
            message_id=message_id,
        )
        stats.enviados += 1

    return stats


def _data(valor: str) -> datetime:
    return datetime.fromisoformat(valor).replace(tzinfo=timezone.utc)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Avisa quem segue parlamentares citados em casos do Na Lupa.")
    parser.add_argument("--dry-run", action="store_true", help="Monta e grava HTML em output/, sem enviar.")
    parser.add_argument("--projeto-id", type=int, default=None)
    parser.add_argument("--caso", default=None, help="Só este caso (slug).")
    parser.add_argument(
        "--desde",
        type=_data,
        default=None,
        help=f"Considera quem entrou num caso desde esta data (AAAA-MM-DD). Padrão: últimos {JANELA_DIAS} dias.",
    )
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

    session = get_session()
    try:
        stats = enviar(
            session,
            dry_run=args.dry_run,
            projeto_id=args.projeto_id,
            caso=args.caso,
            desde=args.desde,
            ignorar_flag=args.ignorar_flag,
            save_html_dir=_OUTPUT_DIR if args.dry_run else None,
        )
    finally:
        session.close()
    logger.info("Aviso do Na Lupa: %s", stats.resumo())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
