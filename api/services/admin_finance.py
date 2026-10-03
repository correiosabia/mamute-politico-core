"""Métricas financeiras do painel admin (CS-121).

Lê só o espelho do Ghost (`ghost_*`, ver `ghost_finance_sync`).

Definições (decididas pelo Luiz em 03/10/2026):
- **Gerais**: todos os membros do Ghost.
- **Com plano**: assinatura ativa (inclui trial e pagamento atrasado) ou
  cortesia (`comped`). Inclui os isentos.
- **Pagantes**: com plano e pagando algo (MRR > 0).
- **Isentos**: com plano e MRR = 0 (oferta de 100%, como a Fellowship, ou
  cortesia).
- **Receita real**: soma do MRR das assinaturas ativas, já com a oferta
  aplicada (o Ghost calcula; a anual entra dividida por 12).
- **Receita de tabela**: o que entraria sem desconto (preço do plano; a anual
  dividida por 12; cortesia pelo preço mensal do plano dela).
- **Receita isenta**: tabela − real. O que deixa de entrar com os descontos.

Valores em reais (o Ghost guarda centavos).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

STATUS_ATIVOS = ("active", "trialing", "past_due")
STATUS_COM_PLANO = ("paid", "comped")
SEM_OFERTA = "Sem oferta"
CORTESIA = "Cortesia (comped)"


def _reais(centavos: float) -> float:
    return round(centavos / 100, 2)


def _tabela_mensal(valor_centavos: int, intervalo: Optional[str]) -> float:
    return valor_centavos / 12 if (intervalo or "").lower() == "year" else float(valor_centavos)


def _utc(valor: Any) -> Optional[datetime]:
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(valor)).replace(tzinfo=timezone.utc)


def _assinaturas_ativas(db: Session) -> list[Any]:
    return db.execute(
        text(
            """
            SELECT id, membro_id, email, status, intervalo, valor_tabela_centavos,
                   mrr_centavos, oferta, oferta_desconto_tipo, oferta_desconto_valor,
                   oferta_duracao, oferta_meses
              FROM ghost_assinatura
             WHERE status IN ('active', 'trialing', 'past_due')
            """
        )
    ).all()


def _cortesias(db: Session) -> list[Any]:
    return db.execute(
        text(
            "SELECT id, email, plano_valor_mensal_centavos FROM ghost_membro "
            "WHERE status = 'comped'"
        )
    ).all()


def _descricao_oferta(a: Any) -> Optional[str]:
    if not a.oferta:
        return None
    if a.oferta_desconto_tipo == "percent":
        desconto = f"{a.oferta_desconto_valor}%"
    elif a.oferta_desconto_tipo == "fixed":
        desconto = f"R$ {_reais(a.oferta_desconto_valor or 0):.2f}".replace(".", ",")
    else:
        desconto = a.oferta_desconto_tipo or "desconto"
    if a.oferta_duracao == "forever":
        duracao = "para sempre"
    elif a.oferta_duracao == "repeating" and a.oferta_meses:
        duracao = f"por {a.oferta_meses} meses"
    elif a.oferta_duracao == "once":
        duracao = "só no 1º pagamento"
    else:
        duracao = a.oferta_duracao or ""
    return f"{desconto} {duracao}".strip()


def resumo_financeiro(db: Session) -> dict[str, Any]:
    ativas = _assinaturas_ativas(db)
    cortesias = _cortesias(db)
    gerais = int(db.execute(text("SELECT count(*) FROM ghost_membro")).scalar() or 0)

    real = sum(a.mrr_centavos for a in ativas)
    tabela = sum(_tabela_mensal(a.valor_tabela_centavos, a.intervalo) for a in ativas)
    tabela += sum(c.plano_valor_mensal_centavos or 0 for c in cortesias)

    pagantes = {a.membro_id or a.id for a in ativas if a.mrr_centavos > 0}
    isentos_assinatura = {a.membro_id or a.id for a in ativas if a.mrr_centavos <= 0} - pagantes
    isentos = len(isentos_assinatura) + len(cortesias)

    ofertas: dict[str, dict[str, Any]] = {}
    for a in ativas:
        chave = a.oferta or SEM_OFERTA
        linha = ofertas.setdefault(
            chave,
            {
                "oferta": chave,
                "desconto": _descricao_oferta(a),
                "assinaturas": 0,
                "receita_real": 0.0,
                "receita_tabela": 0.0,
            },
        )
        linha["assinaturas"] += 1
        linha["receita_real"] += a.mrr_centavos
        linha["receita_tabela"] += _tabela_mensal(a.valor_tabela_centavos, a.intervalo)
    if cortesias:
        valor = sum(c.plano_valor_mensal_centavos or 0 for c in cortesias)
        ofertas[CORTESIA] = {
            "oferta": CORTESIA,
            "desconto": "100% (plano dado pelo admin no Ghost)",
            "assinaturas": len(cortesias),
            "receita_real": 0.0,
            "receita_tabela": float(valor),
        }
    linhas_ofertas = []
    for linha in ofertas.values():
        linhas_ofertas.append(
            {
                **linha,
                "receita_real": _reais(linha["receita_real"]),
                "receita_tabela": _reais(linha["receita_tabela"]),
                "receita_isenta": _reais(linha["receita_tabela"] - linha["receita_real"]),
            }
        )
    linhas_ofertas.sort(key=lambda o: (-o["receita_isenta"], o["oferta"]))

    return {
        "usuarios": {
            "gerais": gerais,
            "com_plano": len(pagantes) + isentos,
            "pagantes": len(pagantes),
            "isentos": isentos,
        },
        "receita_real": _reais(real),
        "receita_tabela": _reais(tabela),
        "receita_isenta": _reais(tabela - real),
        "receita_isenta_percentual": round(100 * (tabela - real) / tabela, 1) if tabela else None,
        "ofertas": linhas_ofertas,
    }


def preco_real_por_email(db: Session) -> dict[str, float]:
    """Quanto cada membro paga por mês hoje (R$). Cortesia = 0; free fica de fora."""
    precos: dict[str, float] = {}
    for c in _cortesias(db):
        if c.email:
            precos[c.email] = 0.0
    for a in _assinaturas_ativas(db):
        if a.email:
            precos[a.email] = precos.get(a.email, 0.0) + _reais(a.mrr_centavos)
    return precos


def _fim_da_semana(d: date) -> date:
    return d + timedelta(days=6 - d.weekday())  # domingo


def crescimento(db: Session, *, hoje: Optional[date] = None) -> dict[str, Any]:
    """Séries semanais (fechamento de domingo) de usuários e receita.

    Reconstrução pelo histórico do Ghost:
    - status de cada membro na data = último evento de status até ela;
    - MRR de cada assinatura na data = soma dos `mrr_delta` até ela (é como o
      próprio Ghost monta o gráfico de MRR);
    - receita de tabela na data usa o preço ATUAL da assinatura/plano de quem
      estava com plano (o Ghost não guarda o histórico de preço de tabela).
    """
    hoje = hoje or datetime.now(timezone.utc).date()
    membros = db.execute(text("SELECT id, status, plano_valor_mensal_centavos, criado_em FROM ghost_membro")).all()
    if not membros:
        return {"semanas": []}
    status_ev = db.execute(
        text("SELECT membro_id, para_status, criado_em FROM ghost_membro_status_evento ORDER BY criado_em")
    ).all()
    eventos = db.execute(
        text("SELECT assinatura_id, membro_id, mrr_delta_centavos, criado_em FROM ghost_assinatura_evento ORDER BY criado_em")
    ).all()
    assinaturas = db.execute(
        text("SELECT id, membro_id, intervalo, valor_tabela_centavos FROM ghost_assinatura")
    ).all()

    tabela_por_membro: dict[str, float] = {}
    for a in assinaturas:
        if a.membro_id:
            tabela_por_membro[a.membro_id] = _tabela_mensal(a.valor_tabela_centavos, a.intervalo)
    plano_por_membro = {m.id: float(m.plano_valor_mensal_centavos or 0) for m in membros}
    criado = {m.id: _utc(m.criado_em) for m in membros}
    historico: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for e in status_ev:
        historico[e.membro_id].append((_utc(e.criado_em), e.para_status))
    eventos_utc = [(_utc(e.criado_em), e.assinatura_id, e.membro_id, e.mrr_delta_centavos) for e in eventos]

    inicio = min(c for c in criado.values() if c is not None).date()
    semana = _fim_da_semana(inicio)
    fim = _fim_da_semana(hoje)
    pontos = []
    while semana <= fim:
        corte = datetime.combine(min(semana, hoje), datetime.max.time(), tzinfo=timezone.utc)
        mrr_assinatura: dict[str, int] = defaultdict(int)
        membro_da_assinatura: dict[str, Optional[str]] = {}
        for quando, assinatura, membro, delta in eventos_utc:
            if quando <= corte:
                mrr_assinatura[assinatura] += delta
                membro_da_assinatura[assinatura] = membro
        pagantes = {membro_da_assinatura[s] for s, v in mrr_assinatura.items() if v > 0}

        gerais = com_plano = 0
        tabela = 0.0
        for m in membros:
            if criado[m.id] is None or criado[m.id] > corte:
                continue
            gerais += 1
            status = "free"
            for quando, para in historico.get(m.id, ()):
                if quando <= corte:
                    status = para
                else:
                    break
            if status in STATUS_COM_PLANO:
                com_plano += 1
                tabela += tabela_por_membro.get(m.id, plano_por_membro.get(m.id, 0.0))
        real = sum(v for v in mrr_assinatura.values() if v > 0)
        pontos.append(
            {
                "semana": semana.isoformat(),
                "gerais": gerais,
                "com_plano": com_plano,
                "pagantes": len(pagantes),
                "isentos": max(com_plano - len(pagantes), 0),
                "receita_real": _reais(real),
                "receita_tabela": _reais(tabela),
            }
        )
        semana += timedelta(days=7)
    return {"semanas": pontos}


__all__ = ["crescimento", "preco_real_por_email", "resumo_financeiro"]
