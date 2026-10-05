"""Aviso do resultado da eleicao a quem acompanha candidatos (CS-106, CS-119).

Roda depois da coleta (`tse_crawler.resultados`), no mesmo job de cron (a cada
15 minutos).

Disparos (CS-119): o aviso e um e-mail consolidado por pessoa, e so sai quando
a totalizacao do TSE fecha em TODAS as UFs, para todo mundo junto.
- turno 1, `majoritarios`: quando presidente, governador e senador fecham em
  todas as UFs; lista so esses cargos, para quem acompanha algum deles;
- turno 1, `completo`: quando os deputados (federal, estadual, distrital)
  tambem fecham; resumo de todos os candidatos acompanhados, para todo mundo
  que acompanha alguem. Se os dois ficam prontos na mesma rodada, so sai o
  completo (ninguem recebe dois e-mails de uma vez);
- turno 2, `completo`: quando fecham os arquivos de 2o turno de todos os
  cargos x UFs que tiveram 2o turno; so para quem acompanha candidato que foi
  ao 2o turno (`situacao` "2º turno" no turno 1), listando so esses.
"Fechou" = o arquivo do TSE daquele cargo x UF esta com
`tse_result_file.totalizacao_final`. Excecao (CS-128): no disparo de
majoritarios do 1o turno tambem vale o arquivo `definido_matematicamente`
(os votos que faltam nao mudam o resultado; regra no coletor). Esse disparo
mostra so "Eleito", "2º turno" ou "Não eleito": oficial do TSE onde a
totalizacao encerrou, e com "matematicamente*" e uma nota onde ainda nao
encerrou (so no e-mail de quem acompanha um desses). Candidato que nao aparece num arquivo
encerrado (indeferido, renuncia) entra como "Nao consta na totalizacao do TSE".
A situacao vai com o texto do TSE ("Eleito por QP", "Suplente"...).

Liga/desliga: flag `resultado_eleicao`, com a MESMA regra de
`api/services/feature_flags.resolve_for` que decide o modal no app, para
e-mail e modal nunca divergirem:
  off (ou sem linha) -> ninguem (a coleta continua gravando);
  admins            -> so admins (MAMUTE_ADMIN_EMAILS);
  all               -> admins + projetos cujo plano tem a flag `liberado` em
                       `feature_flag_tier` (a migration cs106 semeia isso nos
                       planos que ja tem a busca de candidaturas liberada).

Sem duplicata: `election_result_notice` tem unique (projeto, ciclo, turno,
disparo). A linha nasce `pending` antes do envio; vira `sent`, `error` (tenta
de novo nas proximas rodadas, ate MAX_TENTATIVAS) ou `skipped_no_email`. O
`payload` gravado e a foto do que foi enviado e e o que o modal mostra.

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

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mamute_scrappers.scripts.notificacao.config import (  # noqa: E402
    EmailBranding,
    get_branding,
)
from mamute_scrappers.tse_crawler.resultados_parsing import (  # noqa: E402
    CARGO_DEP_DISTRITAL,
    CARGO_DEP_ESTADUAL,
    CARGO_DEP_FEDERAL,
    CARGO_GOVERNADOR,
    CARGO_PRESIDENTE,
    CARGO_SENADOR,
    abrangencias,
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
SITUACAO_ELEITO = "Eleito"
SITUACAO_NAO_ELEITO = "Não eleito"
NAO_CONSTA = "Não consta na totalização do TSE"
SEM_SITUACAO = "Sem situação informada pelo TSE"
MAX_TENTATIVAS = 3

DISPARO_MAJORITARIOS = "majoritarios"
DISPARO_COMPLETO = "completo"
CARGOS_MAJORITARIOS = frozenset({CARGO_PRESIDENTE, CARGO_GOVERNADOR, CARGO_SENADOR})
CARGOS_TODOS = CARGOS_MAJORITARIOS | {CARGO_DEP_FEDERAL, CARGO_DEP_ESTADUAL, CARGO_DEP_DISTRITAL}
CARGOS_SEGUNDO_TURNO = frozenset({CARGO_PRESIDENTE, CARGO_GOVERNADOR})

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
    disparo: str = DISPARO_COMPLETO
    tier_id: Optional[int] = None
    itens: List[dict] = field(default_factory=list)

    def payload(self) -> dict:
        return {"turno": self.turno, "disparo": self.disparo, "itens": self.itens}


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


def disparos_do_turno(turno: int) -> List[tuple[str, frozenset[int]]]:
    """Disparos do turno, do mais completo para o parcial, com os cargos de cada um."""
    if turno == 2:
        return [(DISPARO_COMPLETO, CARGOS_SEGUNDO_TURNO)]
    return [(DISPARO_COMPLETO, CARGOS_TODOS), (DISPARO_MAJORITARIOS, CARGOS_MAJORITARIOS)]


def assunto(turno: int, disparo: str = DISPARO_COMPLETO) -> str:
    if turno == 2:
        return "Saiu o resultado do 2º turno dos candidatos que você acompanha"
    if disparo == DISPARO_MAJORITARIOS:
        return "Saiu o resultado de presidente, governador e senador que você acompanha"
    return "Resultado completo: veja como ficaram todos os candidatos que você acompanha"


def introducao(turno: int, disparo: str = DISPARO_COMPLETO) -> str:
    if turno == 2:
        return "O TSE encerrou a totalização do 2º turno em todo o país. Veja como ficaram os candidatos que você acompanha:"
    if disparo == DISPARO_MAJORITARIOS:
        # CS-128: pode sair antes do TSE encerrar todas as UFs; quem tem item
        # definido matematicamente recebe a nota no fim da lista.
        return (
            "Saiu o resultado de presidente, governador e senador. "
            "Veja como ficaram os candidatos que você acompanha para esses cargos. "
            "O resultado dos deputados chega em outro e-mail, com a totalização oficial do TSE."
        )
    return (
        "O TSE encerrou a totalização de todos os cargos em todo o país. "
        "Veja o resumo de todos os candidatos que você acompanha:"
    )


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


def _tem_coluna(session: Session, tabela: str, coluna: str) -> bool:
    """Colunas de migrations novas (o deploy sobe o codigo antes do alembic)."""
    try:
        colunas = inspect(session.get_bind()).get_columns(tabela)
    except SQLAlchemyError:
        return False
    return any(c.get("name") == coluna for c in colunas)


def _arquivos_finais(
    session: Session, ciclo: str, turno: int, *, aceitar_matematico: bool = False
) -> set[tuple[int, str]]:
    condicao = "totalizacao_final"
    if aceitar_matematico and _tem_coluna(session, "tse_result_file", "definido_matematicamente"):
        condicao = "(totalizacao_final OR definido_matematicamente)"
    rows = session.execute(
        text(
            "SELECT cargo_codigo, uf FROM tse_result_file "
            f"WHERE ciclo = :ciclo AND turno = :turno AND {condicao}"
        ),
        {"ciclo": ciclo, "turno": turno},
    ).all()
    return {(int(r.cargo_codigo), r.uf.lower()) for r in rows}


def _uf_do_arquivo(cargo: int, uf: Optional[str]) -> str:
    return "br" if cargo == CARGO_PRESIDENTE else (uf or "").lower()


def arquivos_esperados(
    session: Session, *, ciclo: str, turno: int, cargos: frozenset[int]
) -> set[tuple[int, str]]:
    """Cargo x UF que precisam estar encerrados para o disparo sair.

    Turno 1: todos os arquivos que o coletor baixa para esses cargos. Turno 2:
    so os cargo x UF que tiveram alguem no 2o turno (vazio antes do turno 1
    fechar, e ai nada sai).
    """
    if turno == 1:
        return {(cargo, uf) for cargo in cargos for uf in abrangencias(cargo)}
    rows = session.execute(
        text(
            """
            SELECT DISTINCT c.office_code, c.state
              FROM candidacy_result r1
              JOIN candidacy c ON c.id = r1.candidacy_id
             WHERE r1.turno = 1 AND r1.situacao = :seg AND c.election_year = :ano
            """
        ),
        {"seg": SITUACAO_SEGUNDO_TURNO, "ano": ano_do_ciclo(ciclo)},
    ).all()
    return {
        (int(r.office_code), _uf_do_arquivo(int(r.office_code), r.state))
        for r in rows
        if r.office_code is not None and int(r.office_code) in cargos
    }


def arquivos_pendentes(
    session: Session,
    *,
    ciclo: str,
    turno: int,
    cargos: frozenset[int],
    aceitar_matematico: bool = False,
) -> Optional[set[tuple[int, str]]]:
    """Arquivos que ainda faltam fechar; None quando nao ha nada esperado."""
    esperados = arquivos_esperados(session, ciclo=ciclo, turno=turno, cargos=cargos)
    if not esperados:
        return None
    return esperados - _arquivos_finais(
        session, ciclo, turno, aceitar_matematico=aceitar_matematico
    )


def aceita_matematico(turno: int, disparo: str) -> bool:
    """So o disparo de majoritarios do 1o turno sai antes do TSE encerrar."""
    return turno == 1 and disparo == DISPARO_MAJORITARIOS


def situacao_resumida(situacao: Optional[str]) -> str:
    """Texto do TSE reduzido a Eleito / 2º turno / Não eleito (majoritarios)."""
    texto = (situacao or "").strip().lower()
    if texto.startswith("eleito"):
        return SITUACAO_ELEITO
    if texto.startswith("2"):
        return SITUACAO_SEGUNDO_TURNO
    return SITUACAO_NAO_ELEITO


def montar_avisos(
    session: Session,
    *,
    ciclo: str,
    turno: int,
    disparo: str = DISPARO_COMPLETO,
    cargos: Optional[frozenset[int]] = None,
    projeto_id: Optional[int] = None,
) -> tuple[List[AvisoPronto], int]:
    """Avisos do disparo e quantos arquivos do TSE ainda faltam fechar.

    Enquanto faltar qualquer arquivo esperado, ninguem fica pronto: o disparo
    e para todo mundo junto, depois que fecha em todas as UFs.
    """
    if cargos is None:
        cargos = dict(disparos_do_turno(turno))[disparo]
    matematico = aceita_matematico(turno, disparo)
    pendentes = arquivos_pendentes(
        session, ciclo=ciclo, turno=turno, cargos=cargos, aceitar_matematico=matematico
    )
    if pendentes is None:
        return [], 0
    if pendentes:
        return [], len(pendentes)

    filtro_turno2 = ""
    if turno == 2:
        filtro_turno2 = (
            "AND EXISTS (SELECT 1 FROM candidacy_result r1 "
            "WHERE r1.candidacy_id = c.id AND r1.turno = 1 AND r1.situacao = :seg)"
        )
    filtro_projeto = "AND p.id = :projeto_id" if projeto_id is not None else ""
    situacao_matematica = (
        "r.situacao_matematica"
        if matematico and _tem_coluna(session, "candidacy_result", "situacao_matematica")
        else "NULL"
    )
    rows = session.execute(
        text(
            f"""
            SELECT p.id AS projeto_id, p.email, p.nome, p.tier_id,
                   c.id AS candidacy_id, c.ballot_name, c.full_name, c.office,
                   c.office_code, c.state, c.party, c.ballot_number,
                   r.situacao, r.votos, r.percentual,
                   r.totalizacao_final, {situacao_matematica} AS situacao_matematica
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

    por_projeto: Dict[int, AvisoPronto] = {}
    for row in rows:
        if row.office_code is None or int(row.office_code) not in cargos:
            continue
        aviso = por_projeto.setdefault(
            row.projeto_id,
            AvisoPronto(
                projeto_id=row.projeto_id,
                email=row.email,
                nome=row.nome or "",
                turno=turno,
                disparo=disparo,
                tier_id=row.tier_id,
            ),
        )
        definido_matematicamente = False
        if matematico:
            # Oficial onde o TSE encerrou; conta nossa (com asterisco) onde nao.
            # Sem linha no arquivo (indeferido, renuncia) = Não eleito.
            if row.totalizacao_final:
                situacao = situacao_resumida(row.situacao)
            elif row.situacao_matematica:
                situacao = situacao_resumida(row.situacao_matematica)
                definido_matematicamente = True
            else:
                situacao = SITUACAO_NAO_ELEITO
        elif row.situacao:
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
                "matematicamente": definido_matematicamente,
            }
        )

    return [a for a in por_projeto.values() if a.itens], 0


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


# Mesmas cores do selo de situacao no card da busca (ResultadoCandidatura.tsx).
_SELO = {
    SITUACAO_ELEITO: ("#090909", "#ffffff"),
    SITUACAO_SEGUNDO_TURNO: ("#1b76ff", "#ffffff"),
}
_SELO_PADRAO = ("#efeeee", "#7f7b7b")
_AZUL = "#1b76ff"

FONTE_OFICIAL = "Situação conforme a divulgação oficial do Tribunal Superior Eleitoral."


def _selo(situacao: str, matematicamente: bool) -> str:
    fundo, cor = _SELO.get(situacao, _SELO_PADRAO)
    selo = (
        f'<span style="display:inline-block;background:{fundo};color:{cor};'
        "border-radius:76px;padding:3px 10px;font-size:11px;font-weight:bold;"
        f'letter-spacing:0.04em;text-transform:uppercase;">{html.escape(situacao)}</span>'
    )
    if matematicamente:
        selo += (
            '<br><span style="display:inline-block;margin-top:4px;font-size:12px;'
            f'color:{_AZUL};">matematicamente*</span>'
        )
    return selo


def _nota_matematica(itens: Sequence[dict]) -> str:
    """Nota so para quem acompanha candidato de arquivo ainda nao encerrado."""
    onde = list(
        dict.fromkeys(
            item["cargo"] if not item["uf"] or item["uf"].upper() == "BR" else f'{item["cargo"]} ({item["uf"]})'
            for item in itens
            if item.get("matematicamente")
        )
    )
    if not onde:
        return ""
    lista = ", ".join(onde[:-1]) + (" e " if len(onde) > 1 else "") + onde[-1]
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:18px;">'
        f'<tr><td style="background:#f2f7ff;border-left:3px solid {_AZUL};padding:12px 14px;'
        'font-size:14px;line-height:1.5;color:#383838;">'
        "<strong>* Definido matematicamente.</strong> "
        f"Para {html.escape(lista)}, o TSE ainda não encerrou a totalização, mas os votos "
        "que faltam apurar não mudam mais o resultado. A confirmação oficial ainda pode "
        "levar algumas horas."
        + (
            " Os demais resultados já são oficiais."
            if any(not item.get("matematicamente") for item in itens)
            else ""
        )
        + "</td></tr></table>"
    )


def sai_na_liberacao(situacao: Optional[str]) -> bool:
    """Nao eleito no 1o turno: sai da selecao na liberacao para o 2o (CS-130).

    Mesma regra de scripts/selecao_por_turno.py: continua quem foi eleito
    ("Eleito", "Eleito por QP"...) ou foi ao 2o turno.
    """
    texto = (situacao or "").strip()
    return not (texto.lower().startswith("eleito") or texto == SITUACAO_SEGUNDO_TURNO)


def _nota_liberacao(aviso: AvisoPronto) -> str:
    """Linha do e-mail completo do 1o turno para quem tem nao eleito na lista."""
    if aviso.turno != 1 or aviso.disparo != DISPARO_COMPLETO:
        return ""
    if not any(sai_na_liberacao(item["situacao"]) for item in aviso.itens):
        return ""
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:18px;">'
        f'<tr><td style="background:#f2f7ff;border-left:3px solid {_AZUL};padding:12px 14px;'
        'font-size:14px;line-height:1.5;color:#383838;">'
        "<strong>Sua seleção foi liberada para o 2º turno.</strong> "
        "Quem não se elegeu saiu da sua lista de acompanhamento e continua marcado com o "
        "selo <strong>1º</strong> na busca de candidaturas, para você lembrar quem escolheu. "
        "Eleitos e quem foi ao 2º turno continuam selecionados."
        "</td></tr></table>"
    )


def render_html(aviso: AvisoPronto, *, branding: EmailBranding | None = None) -> str:
    brand = branding or get_branding()
    linhas = []
    for item in aviso.itens:
        detalhe = " · ".join(
            p for p in (f"{item['cargo']} ({item['uf']})", item["partido"], _formata_votos(item["votos"], item["percentual"])) if p
        )
        numero = (
            f' <span style="color:#7f7b7b;font-size:12px;font-weight:bold;">{html.escape(str(item["numero"]))}</span>'
            if item.get("numero") is not None
            else ""
        )
        linhas.append(
            '<tr><td style="padding:12px 0;border-bottom:1px solid #efeeee;vertical-align:top;">'
            f'<strong style="color:#090909;font-size:16px;">{html.escape(item["nome"])}</strong>{numero}<br>'
            f'<span class="muted">{html.escape(detalhe)}</span></td>'
            '<td style="padding:12px 0 12px 12px;border-bottom:1px solid #efeeee;text-align:right;'
            f'vertical-align:top;white-space:nowrap;">{_selo(item["situacao"], bool(item.get("matematicamente")))}</td></tr>'
        )
    tabela = (
        '<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
        + "".join(linhas)
        + "</table>"
        + _nota_matematica(aviso.itens)
        + _nota_liberacao(aviso)
    )
    intro = introducao(aviso.turno, aviso.disparo)
    # O app mora em /app (manage_url); app_url e a home do site.
    link = f"{brand.manage_url.rstrip('/')}/candidaturas"
    replacements = {
        "{{SUBJECT}}": html.escape(assunto(aviso.turno, aviso.disparo)),
        "{{LOGO_URL}}": html.escape(brand.logo_url),
        "{{PRIVACY_URL}}": html.escape(brand.privacy_url),
        "{{MANAGE_URL}}": html.escape(brand.manage_url),
        "{{GREETING_NAME}}": html.escape(_greeting_name(aviso.nome)),
        "{{INTRO}}": html.escape(intro),
        "{{RESULTADOS}}": tabela,
        "{{FONTE}}": (
            FONTE_OFICIAL[:-1] + ", exceto onde marcado com *."
            if any(item.get("matematicamente") for item in aviso.itens)
            else FONTE_OFICIAL
        ),
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
            INSERT INTO election_result_notice (projeto_id, ciclo, turno, disparo, payload, email_status)
            VALUES (:projeto_id, :ciclo, :turno, :disparo, :payload, :status)
            ON CONFLICT (projeto_id, ciclo, turno, disparo) DO NOTHING
            """
        ),
        {
            "projeto_id": aviso.projeto_id,
            "ciclo": ciclo,
            "turno": aviso.turno,
            "disparo": aviso.disparo,
            "payload": json.dumps(aviso.payload(), ensure_ascii=False),
            "status": STATUS_PENDING,
        },
    )
    session.commit()
    row = session.execute(
        text(
            "SELECT id, email_status, tentativas, payload FROM election_result_notice "
            "WHERE projeto_id = :p AND ciclo = :c AND turno = :t AND disparo = :d"
        ),
        {"p": aviso.projeto_id, "c": ciclo, "t": aviso.turno, "d": aviso.disparo},
    ).first()
    return dict(row._mapping) if row else None


def _atualizar(session: Session, notice_id: int, **campos: object) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in campos)
    session.execute(
        text(f"UPDATE election_result_notice SET {sets} WHERE id = :id"),
        {"id": notice_id, **campos},
    )
    session.commit()


def _avisos_da_rodada(
    session: Session,
    ciclo: str,
    turnos: Sequence[int],
    projeto_id: Optional[int],
    stats: "EnvioStats",
):
    """(turno, disparo, prontos) de cada disparo que ja pode sair nesta rodada."""
    for turno in turnos:
        completo_pronto = False
        for disparo, cargos in disparos_do_turno(turno):
            if disparo == DISPARO_MAJORITARIOS and completo_pronto:
                # O completo ja inclui os majoritarios: nao manda os dois juntos.
                continue
            prontos, pendentes = montar_avisos(
                session, ciclo=ciclo, turno=turno, disparo=disparo,
                cargos=cargos, projeto_id=projeto_id,
            )
            if pendentes:
                stats.aguardando += pendentes
                logger.info(
                    "Turno %s %s: aguardando %s arquivo(s) do TSE fechar.", turno, disparo, pendentes
                )
                continue
            if disparo == DISPARO_COMPLETO and arquivos_pendentes(
                session, ciclo=ciclo, turno=turno, cargos=cargos
            ) is not None:
                completo_pronto = True
            yield turno, disparo, prontos


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

    for turno, disparo, prontos in _avisos_da_rodada(session, ciclo, turnos, projeto_id, stats):
        for aviso in prontos:
            if not recebe(aviso):
                stats.fora_do_recorte += 1
                continue
            stats.prontos += 1
            subject = assunto(turno, disparo)

            if dry_run:
                corpo = render_html(aviso)
                if save_html_dir is not None:
                    save_html_dir.mkdir(parents=True, exist_ok=True)
                    (save_html_dir / f"resultado_{ciclo}_t{turno}_{disparo}_projeto_{aviso.projeto_id}.html").write_text(
                        corpo, encoding="utf-8"
                    )
                logger.info(
                    "dry-run: projeto %s turno %s %s, %s candidato(s)",
                    aviso.projeto_id, turno, disparo, len(aviso.itens),
                )
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

            periodicidade = f"eleicao_{ciclo}_t{turno}_{disparo}"
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
