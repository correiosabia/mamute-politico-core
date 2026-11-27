"""Missões do dia: consultas, ideias, enquetes e abaixo-assinados abertos (CS-179).

A coleta (`mamute_scrappers.participacao_crawler`) enche a fila; um admin aprova
o que vai ao ar, escolhe o tema e pode escrever uma nota. O leitor vê só o
aprovado, em duas abas:

* HOJE: aprovado, aberto na fonte e com prazo a partir de hoje (ou sem prazo:
  consulta do Senado e enquete da Câmara ficam abertas enquanto a proposição
  tramita, e a tela diz isso em vez de inventar uma data).
* JÁ PASSOU: aprovado que encerrou (prazo vencido ou a fonte deixou de mostrar
  aberto), do mais recente para o mais antigo.

O placar (votos, apoios) só sai quando quem chama tem a flag `missoes_placar`:
é participação espontânea, sem rigor estatístico, e a tela precisa do aviso.

A curadoria cadastra à mão o que não tem coleta automática (Change.org, Avaaz,
campanhas): esses itens nascem aprovados e só a curadoria os encerra.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import Session

try:
    from ..db.models.participation_item import ParticipationItem
except ImportError:  # execução dentro de api/
    from db.models.participation_item import ParticipationItem

STATUS = ("pendente", "aprovado", "descartado")
KINDS = {
    "consulta": "Consulta pública",
    "ideia": "Ideia legislativa",
    "enquete": "Enquete",
    "peticao": "Abaixo-assinado",
    "audiencia": "Audiência pública",
    "outro": "Participação",
}
FONTES = {
    "brasil_participativo": "Brasil Participativo",
    "senado_consulta": "e-Cidadania do Senado",
    "senado_ideia": "e-Cidadania do Senado",
    "camara_enquete": "Portal da Câmara",
    "manual": "Cadastro da curadoria",
}
MANUAL = "manual"
LIMITE_PASSOU = 100
LIMITE_ADMIN = 200
MAX_TITULO, MAX_RESUMO, MAX_TEMA, MAX_NOTA = 300, 2000, 60, 500
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)


class ParticipacaoError(ValueError):
    """Erro de validação com mensagem que vai para a tela do admin."""


def _aberto(hoje: date):
    return and_(
        ParticipationItem.source_open.is_(True),
        or_(ParticipationItem.closes_on.is_(None), ParticipationItem.closes_on >= hoje),
    )


def _iso(valor: Any) -> Optional[str]:
    return valor.isoformat() if valor is not None else None


def esta_aberto(item: ParticipationItem, hoje: date) -> bool:
    return bool(item.source_open) and (item.closes_on is None or item.closes_on >= hoje)


def _publico(item: ParticipationItem, hoje: date, *, com_placar: bool) -> dict[str, Any]:
    saida = {
        "id": item.id,
        "kind": item.kind,
        "kind_label": KINDS.get(item.kind, KINDS["outro"]),
        "title": item.title,
        "summary": item.summary,
        "reference": item.reference,
        "organization": item.organization,
        "source_label": FONTES.get(item.source),
        "url": item.url,
        "opens_on": _iso(item.opens_on),
        "closes_on": _iso(item.closes_on),
        "sem_prazo": item.closes_on is None,
        "aberto": esta_aberto(item, hoje),
        "theme": item.theme,
        "note": item.note,
    }
    if com_placar:
        saida["score"] = item.score
        saida["score_updated_at"] = _iso(item.score_updated_at)
    return saida


def listar_publico(
    db: Session,
    *,
    aba: str = "hoje",
    tema: Optional[str] = None,
    hoje: Optional[date] = None,
    com_placar: bool = False,
    limite: Optional[int] = None,
) -> dict[str, Any]:
    hoje = hoje or date.today()
    base = select(ParticipationItem).where(ParticipationItem.status == "aprovado")
    if aba == "passou":
        consulta = base.where(not_(_aberto(hoje))).order_by(
            func.coalesce(ParticipationItem.closes_on, func.date(ParticipationItem.closed_at)).desc().nullslast(),
            ParticipationItem.id.desc(),
        )
        limite = limite or LIMITE_PASSOU
    else:
        # Prazo mais curto primeiro; sem prazo no fim, o mais recente antes.
        consulta = base.where(_aberto(hoje)).order_by(
            ParticipationItem.closes_on.asc().nullslast(), ParticipationItem.first_seen_at.desc()
        )
    temas = sorted(
        t
        for (t,) in db.execute(
            select(ParticipationItem.theme)
            .where(ParticipationItem.status == "aprovado", ParticipationItem.theme.is_not(None))
            .where(_aberto(hoje) if aba != "passou" else not_(_aberto(hoje)))
            .distinct()
        )
        if t
    )
    if tema:
        consulta = consulta.where(ParticipationItem.theme == tema)
    if limite:
        consulta = consulta.limit(limite)
    itens = db.execute(consulta).scalars().all()
    return {
        "aba": "passou" if aba == "passou" else "hoje",
        "hoje": hoje.isoformat(),
        "temas": temas,
        "items": [_publico(i, hoje, com_placar=com_placar) for i in itens],
        "com_placar": com_placar,
    }


def _admin(item: ParticipationItem, hoje: date) -> dict[str, Any]:
    saida = _publico(item, hoje, com_placar=True)
    saida.update(
        {
            "source": item.source,
            "external_id": item.external_id,
            "status": item.status,
            "source_open": bool(item.source_open),
            "manual": item.source == MANUAL,
            "reviewed_by": item.reviewed_by,
            "reviewed_at": _iso(item.reviewed_at),
            "first_seen_at": _iso(item.first_seen_at),
            "last_seen_at": _iso(item.last_seen_at),
        }
    )
    return saida


def listar_admin(
    db: Session,
    *,
    status: str = "pendente",
    fonte: Optional[str] = None,
    busca: Optional[str] = None,
    hoje: Optional[date] = None,
    limite: int = LIMITE_ADMIN,
) -> dict[str, Any]:
    if status not in STATUS:
        raise ParticipacaoError("Situação inválida.")
    hoje = hoje or date.today()
    consulta = select(ParticipationItem).where(ParticipationItem.status == status)
    if status == "pendente":
        # Pendente encerrado não interessa à curadoria: não dá mais para participar.
        consulta = consulta.where(_aberto(hoje))
    if fonte:
        consulta = consulta.where(ParticipationItem.source == fonte)
    if busca and busca.strip():
        termo = f"%{busca.strip()}%"
        consulta = consulta.where(
            or_(
                ParticipationItem.title.ilike(termo),
                ParticipationItem.reference.ilike(termo),
                ParticipationItem.summary.ilike(termo),
            )
        )
    consulta = consulta.order_by(
        ParticipationItem.closes_on.asc().nullslast(), ParticipationItem.first_seen_at.desc()
    ).limit(min(max(limite, 1), LIMITE_ADMIN))
    contagem = {s: 0 for s in STATUS}
    for s, n in db.execute(
        select(ParticipationItem.status, func.count())
        .where(or_(ParticipationItem.status != "pendente", _aberto(hoje)))
        .group_by(ParticipationItem.status)
    ):
        contagem[s] = n
    temas = sorted(
        t
        for (t,) in db.execute(
            select(ParticipationItem.theme).where(ParticipationItem.theme.is_not(None)).distinct()
        )
        if t
    )
    return {
        "items": [_admin(i, hoje) for i in db.execute(consulta).scalars().all()],
        "contagem": contagem,
        "temas": temas,
        "fontes": FONTES,
        "kinds": KINDS,
    }


def _texto(valor: Any, limite: int, nome: str, *, obrigatorio: bool = False) -> Optional[str]:
    texto = " ".join(str(valor).split()) if valor is not None else ""
    if not texto:
        if obrigatorio:
            raise ParticipacaoError(f"Preencha {nome}.")
        return None
    if len(texto) > limite:
        raise ParticipacaoError(f"{nome.capitalize()} pode ter no máximo {limite} caracteres.")
    return texto


def _url(valor: Any) -> str:
    url = str(valor or "").strip()
    if not _URL.match(url):
        raise ParticipacaoError("Informe o link completo, começando com https://.")
    return url


def _data(valor: Any, nome: str) -> Optional[date]:
    if valor in (None, ""):
        return None
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError as exc:
        raise ParticipacaoError(f"Data de {nome} inválida.") from exc


def _kind(valor: Any) -> str:
    kind = str(valor or "peticao")
    if kind not in KINDS:
        raise ParticipacaoError("Tipo de participação inválido.")
    return kind


def _buscar(db: Session, item_id: int) -> ParticipationItem:
    item = db.get(ParticipationItem, item_id)
    if item is None:
        raise LookupError("Item não encontrado.")
    return item


def foto(item: ParticipationItem) -> dict[str, Any]:
    """Estado editável do item, para o log de auditoria."""
    return {
        "status": item.status,
        "theme": item.theme,
        "note": item.note,
        "title": item.title,
        "url": item.url,
        "closes_on": _iso(item.closes_on),
        "source_open": bool(item.source_open),
    }


def revisar(db: Session, item_id: int, dados: dict[str, Any], *, admin_email: str) -> ParticipationItem:
    """Aplica a decisão da curadoria. Campos da fonte só mudam em item manual."""
    item = _buscar(db, item_id)
    agora = datetime.now(timezone.utc)
    if "status" in dados and dados["status"] is not None:
        if dados["status"] not in STATUS:
            raise ParticipacaoError("Situação inválida.")
        if dados["status"] != item.status:
            item.status = dados["status"]
            item.reviewed_by, item.reviewed_at = admin_email, agora
    if "theme" in dados:
        item.theme = _texto(dados["theme"], MAX_TEMA, "o tema")
    if "note" in dados:
        item.note = _texto(dados["note"], MAX_NOTA, "a nota")

    da_fonte = {"title", "summary", "url", "closes_on", "opens_on", "organization", "kind", "reference", "source_open"}
    # `dados` vem só com o que o admin mandou (exclude_unset na rota).
    if da_fonte & set(dados) and item.source != MANUAL:
        raise ParticipacaoError("Só dá para editar título, link e prazo de item cadastrado à mão.")
    if "title" in dados and dados["title"] is not None:
        item.title = _texto(dados["title"], MAX_TITULO, "o título", obrigatorio=True)
    if "summary" in dados:
        item.summary = _texto(dados["summary"], MAX_RESUMO, "o resumo")
    if "url" in dados and dados["url"] is not None:
        item.url = _url(dados["url"])
    if "closes_on" in dados:
        item.closes_on = _data(dados["closes_on"], "encerramento")
    if "opens_on" in dados:
        item.opens_on = _data(dados["opens_on"], "abertura")
    if "organization" in dados and dados["organization"] is not None:
        item.organization = _texto(dados["organization"], MAX_TEMA * 2, "quem organiza")
    if "reference" in dados and dados["reference"] is not None:
        item.reference = _texto(dados["reference"], MAX_TEMA, "a referência")
    if "kind" in dados and dados["kind"] is not None:
        item.kind = _kind(dados["kind"])
    if "source_open" in dados and dados["source_open"] is not None:
        item.source_open = bool(dados["source_open"])
        item.closed_at = None if item.source_open else agora
    return item


def revisar_lote(db: Session, ids: Iterable[int], status: str, *, admin_email: str) -> int:
    if status not in STATUS:
        raise ParticipacaoError("Situação inválida.")
    agora = datetime.now(timezone.utc)
    n = 0
    for item in db.execute(select(ParticipationItem).where(ParticipationItem.id.in_(list(ids)))).scalars():
        if item.status != status:
            item.status, item.reviewed_by, item.reviewed_at = status, admin_email, agora
            n += 1
    return n


def criar_manual(db: Session, dados: dict[str, Any], *, admin_email: str) -> ParticipationItem:
    """Cadastro da curadoria (Change.org, Avaaz, campanha): nasce aprovado."""
    agora = datetime.now(timezone.utc)
    item = ParticipationItem(
        source=MANUAL,
        external_id=uuid.uuid4().hex,
        kind=_kind(dados.get("kind")),
        title=_texto(dados.get("title"), MAX_TITULO, "o título", obrigatorio=True),
        summary=_texto(dados.get("summary"), MAX_RESUMO, "o resumo"),
        reference=_texto(dados.get("reference"), MAX_TEMA, "a referência"),
        organization=_texto(dados.get("organization"), MAX_TEMA * 2, "quem organiza"),
        url=_url(dados.get("url")),
        opens_on=_data(dados.get("opens_on"), "abertura"),
        closes_on=_data(dados.get("closes_on"), "encerramento"),
        theme=_texto(dados.get("theme"), MAX_TEMA, "o tema"),
        note=_texto(dados.get("note"), MAX_NOTA, "a nota"),
        status="aprovado",
        source_open=True,
        reviewed_by=admin_email,
        reviewed_at=agora,
        first_seen_at=agora,
        last_seen_at=agora,
    )
    db.add(item)
    db.flush()
    return item


def para_admin(item: ParticipationItem, hoje: Optional[date] = None) -> dict[str, Any]:
    return _admin(item, hoje or date.today())
