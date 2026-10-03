"""Métricas financeiras do painel admin (CS-121).

Lê só o espelho do Ghost (`ghost_*`, ver `ghost_finance_sync`).

Definições (decididas pelo Luiz em 03/10/2026):
- **Gerais**: todos os membros do Ghost.
- **Com plano**: assinatura ativa (inclui trial e pagamento atrasado) ou
  cortesia (`comped`). Inclui os isentos.
- **Pagantes**: com plano e pagando algo (MRR > 0).
- **Isentos**: com plano e MRR = 0 (oferta de 100%, como a Fellowship, ou
  cortesia).
- **Receita real**: quanto as assinaturas ativas pagam por mês. O MRR do
  Ghost só desconta oferta "para sempre"; oferta temporária ("por 12 meses",
  como Mamute 70/80/90) entra nele pelo preço cheio. Por isso o valor real de
  cada assinatura é o MENOR entre o MRR do Ghost e o preço com a oferta,
  enquanto a oferta estiver valendo (`desconto_fim` no futuro, ou "para
  sempre"). O MRR do Ghost continua valendo quando é menor (desconto aplicado
  direto na Stripe, que o Ghost enxerga e nós não). Anual dividida por 12.
- **Receita de tabela**: o que entraria sem desconto (preço do plano; a anual
  dividida por 12; cortesia pelo preço mensal do plano dela).
- **Receita isenta**: tabela − real. O que deixa de entrar com os descontos.

Valores em reais (o Ghost guarda centavos).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import inspect as sqlalchemy_inspect
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
                   oferta_duracao, oferta_meses, desconto_fim, inicio
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


def _oferta_valendo(a: Any, quando: datetime) -> bool:
    if not a.oferta or not a.oferta_desconto_tipo:
        return False
    if a.oferta_duracao == "forever":
        return True
    fim = _utc(a.desconto_fim)
    return fim is not None and quando < fim


def _preco_com_oferta(a: Any) -> float:
    tabela = _tabela_mensal(a.valor_tabela_centavos, a.intervalo)
    valor = a.oferta_desconto_valor or 0
    if a.oferta_desconto_tipo == "percent":
        return tabela * (1 - valor / 100)
    if a.oferta_desconto_tipo == "fixed":
        fixo = valor / 12 if (a.intervalo or "").lower() == "year" else valor
        return max(tabela - fixo, 0.0)
    return tabela


def _preco_real(a: Any, quando: datetime, *, ativa_hoje: bool) -> float:
    """Valor mensal real da assinatura na data (centavos).

    Base = MRR do Ghost se a assinatura está ativa hoje; se já acabou, o Ghost
    zera o MRR, então a base vira o preço de tabela. Com oferta valendo na
    data, fica o menor entre a base e o preço com a oferta.
    """
    base = float(a.mrr_centavos) if ativa_hoje else _tabela_mensal(a.valor_tabela_centavos, a.intervalo)
    if _oferta_valendo(a, quando):
        return min(base, _preco_com_oferta(a))
    return base


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
    agora = datetime.now(timezone.utc)
    ativas = _assinaturas_ativas(db)
    precos = {a.id: _preco_real(a, agora, ativa_hoje=True) for a in ativas}
    cortesias = _cortesias(db)
    gerais = int(db.execute(text("SELECT count(*) FROM ghost_membro")).scalar() or 0)

    real = sum(precos.values())
    tabela = sum(_tabela_mensal(a.valor_tabela_centavos, a.intervalo) for a in ativas)
    tabela += sum(c.plano_valor_mensal_centavos or 0 for c in cortesias)

    pagantes = {a.membro_id or a.id for a in ativas if precos[a.id] > 0}
    isentos_assinatura = {a.membro_id or a.id for a in ativas if precos[a.id] <= 0} - pagantes
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
        linha["receita_real"] += precos[a.id]
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
    agora = datetime.now(timezone.utc)
    for a in _assinaturas_ativas(db):
        if a.email:
            precos[a.email] = round(
                precos.get(a.email, 0.0) + _preco_real(a, agora, ativa_hoje=True) / 100, 2
            )
    return precos


def _fim_da_semana(d: date) -> date:
    return d + timedelta(days=6 - d.weekday())  # domingo


def _fim_do_mes(d: date) -> date:
    proximo = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return proximo - timedelta(days=1)


def _fins_de_periodo(primeira: date, hoje: date, granularidade: str) -> list[date]:
    fins = []
    if granularidade == "mes":
        atual = _fim_do_mes(primeira)
        while atual <= _fim_do_mes(hoje):
            fins.append(atual)
            atual = _fim_do_mes(atual + timedelta(days=1))
    else:
        atual = _fim_da_semana(primeira)
        while atual <= _fim_da_semana(hoje):
            fins.append(atual)
            atual += timedelta(days=7)
    return fins


def _variacao(atual: float, anterior: Optional[float]) -> Optional[float]:
    if anterior is None or anterior == 0:
        return None
    return round(100 * (atual - anterior) / anterior, 1)


def _saidas_de_membros(db: Session) -> list[datetime]:
    """Membros apagados (o Ghost apaga de vez; o webhook marca `projetos.deleted_at`)."""
    if not sqlalchemy_inspect(db.connection()).has_table("projetos"):
        return []
    return [
        _utc(r.deleted_at)
        for r in db.execute(text("SELECT deleted_at FROM projetos WHERE deleted_at IS NOT NULL")).all()
    ]


def crescimento(
    db: Session, *, hoje: Optional[date] = None, granularidade: str = "semana"
) -> dict[str, Any]:
    """Séries por semana (fechamento de domingo) ou mês, com entradas e saídas.

    Reconstrução pelo histórico do Ghost:
    - assinatura ativa na data = começou até ela e não tinha sido cancelada
      ou expirada (eventos `canceled`/`expired`); as ativas hoje não acabaram;
    - cortesia na data = último evento de status até ela é `comped` (para
      assinatura paga o Ghost nem sempre grava evento de status, por isso o
      "com plano" vem das assinaturas, não dos eventos);
    - valor real na data = mesma regra do resumo (`_preco_real`), com a oferta
      valendo ou não naquela data;
    - receita de tabela usa o preço de tabela atual de cada assinatura/plano
      (o Ghost não guarda histórico de preço de tabela);
    - saída de membro = exclusão registrada em `projetos.deleted_at` (o Ghost
      não guarda membro apagado). Membro apagado também some de "gerais" no
      passado, porque só os atuais têm data de criação.
    O período atual vai até hoje (parcial). `variacao_*` = % sobre o período
    anterior.
    """
    hoje = hoje or datetime.now(timezone.utc).date()
    membros = db.execute(
        text("SELECT id, status, plano_valor_mensal_centavos, criado_em FROM ghost_membro")
    ).all()
    if not membros:
        return {"granularidade": granularidade, "periodos": [], "semanas": []}
    status_ev = db.execute(
        text(
            "SELECT membro_id, de_status, para_status, criado_em FROM ghost_membro_status_evento "
            "ORDER BY criado_em, id"
        )
    ).all()
    assinaturas = db.execute(
        text(
            """
            SELECT id, membro_id, status, intervalo, valor_tabela_centavos, mrr_centavos,
                   oferta, oferta_desconto_tipo, oferta_desconto_valor, oferta_duracao,
                   desconto_fim, inicio
              FROM ghost_assinatura
            """
        )
    ).all()
    fins: dict[str, datetime] = {}
    for e in db.execute(
        text(
            "SELECT assinatura_id, criado_em FROM ghost_assinatura_evento "
            "WHERE tipo IN ('canceled', 'expired') ORDER BY criado_em"
        )
    ).all():
        fins.setdefault(e.assinatura_id, _utc(e.criado_em))
    saidas_membros = _saidas_de_membros(db)

    criado = {m.id: _utc(m.criado_em) for m in membros}
    plano_por_membro = {m.id: float(m.plano_valor_mensal_centavos or 0) for m in membros}
    historico: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    entradas_cortesia: list[datetime] = []
    saidas_cortesia: list[datetime] = []
    for e in status_ev:
        quando = _utc(e.criado_em)
        historico[e.membro_id].append((quando, e.para_status))
        if e.para_status == "comped" and e.de_status != "comped":
            entradas_cortesia.append(quando)
        elif e.de_status == "comped" and e.para_status != "comped":
            saidas_cortesia.append(quando)

    periodos_assinatura = []
    for a in assinaturas:
        inicio = _utc(a.inicio)
        if inicio is None:
            continue
        ativa_hoje = a.status in STATUS_ATIVOS
        fim = None if ativa_hoje else fins.get(a.id)
        if not ativa_hoje and fim is None:
            # Encerrada sem evento de fim: não dá para saber quando valeu, fica fora.
            continue
        periodos_assinatura.append((a, inicio, fim, ativa_hoje))

    def no_periodo(quando: Optional[datetime], de: Optional[datetime], ate: datetime) -> bool:
        return quando is not None and quando <= ate and (de is None or quando > de)

    primeira = min(c for c in criado.values() if c is not None).date()
    pontos: list[dict[str, Any]] = []
    corte_anterior: Optional[datetime] = None
    for fim_periodo in _fins_de_periodo(primeira, hoje, granularidade):
        corte = datetime.combine(min(fim_periodo, hoje), datetime.max.time(), tzinfo=timezone.utc)
        gerais = sum(1 for c in criado.values() if c is not None and c <= corte)

        com_assinatura: set[str] = set()
        pagantes: set[str] = set()
        real = tabela = 0.0
        for a, inicio, fim, ativa_hoje in periodos_assinatura:
            if inicio > corte or (fim is not None and fim <= corte):
                continue
            membro = a.membro_id or a.id
            com_assinatura.add(membro)
            preco = _preco_real(a, corte, ativa_hoje=ativa_hoje)
            real += preco
            tabela += _tabela_mensal(a.valor_tabela_centavos, a.intervalo)
            if preco > 0:
                pagantes.add(membro)

        cortesias = 0
        for membro_id, eventos in historico.items():
            if membro_id in com_assinatura:
                continue
            status = None
            for quando, para in eventos:
                if quando <= corte:
                    status = para
                else:
                    break
            if status == "comped":
                cortesias += 1
                tabela += plano_por_membro.get(membro_id, 0.0)

        com_plano = len(com_assinatura) + cortesias
        anterior = pontos[-1] if pontos else None
        ponto = {
            "periodo": fim_periodo.isoformat(),
            "semana": fim_periodo.isoformat(),  # compatibilidade com a primeira versão da tela
            "parcial": fim_periodo > hoje,
            "gerais": gerais,
            "com_plano": com_plano,
            "pagantes": len(pagantes),
            "isentos": com_plano - len(pagantes),
            "receita_real": _reais(real),
            "receita_tabela": _reais(tabela),
            "membros_entradas": sum(1 for c in criado.values() if no_periodo(c, corte_anterior, corte)),
            "membros_saidas": sum(1 for d in saidas_membros if no_periodo(d, corte_anterior, corte)),
            "plano_entradas": sum(1 for _a, ini, _f, _h in periodos_assinatura if no_periodo(ini, corte_anterior, corte))
            + sum(1 for d in entradas_cortesia if no_periodo(d, corte_anterior, corte)),
            "plano_saidas": sum(1 for _a, _i, f, _h in periodos_assinatura if no_periodo(f, corte_anterior, corte))
            + sum(1 for d in saidas_cortesia if no_periodo(d, corte_anterior, corte)),
        }
        for campo in ("gerais", "com_plano", "pagantes", "receita_real"):
            ponto[f"variacao_{campo}"] = _variacao(ponto[campo], anterior[campo] if anterior else None)
        pontos.append(ponto)
        corte_anterior = corte
    return {"granularidade": granularidade, "periodos": pontos, "semanas": pontos}


__all__ = ["crescimento", "preco_real_por_email", "resumo_financeiro"]
