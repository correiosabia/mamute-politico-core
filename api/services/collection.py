"""Coleções curadas de políticos: leitura com os vínculos resolvidos e escrita admin (CS-132).

Regras que moram aqui, e não na tela:

1. O VÍNCULO DA PESSOA É RESOLVIDO NA LEITURA. A coleção guarda o que o admin
   informou (`parliamentarian_id`, `candidacy_id`, `cpf`); quem a pessoa é hoje
   na base sai daqui a cada leitura. Assim um candidato que assume mandato, ou
   alguém que muda de casa ou de esfera, passa a apontar para o perfil certo sem
   ninguém regravar a coleção.
2. O CPF É A CHAVE QUE ATRAVESSA ELEIÇÕES. Candidatura mais recente pelo CPF;
   parlamentar pela candidatura ou pelo próprio CPF. Senador não tem CPF na
   base, por isso o `parliamentarian_id` informado tem precedência.
3. RASCUNHO NÃO VAZA. A leitura pública só enxerga coleção publicada; a do
   admin enxerga tudo.
4. SALVAR SUBSTITUI A LISTA. Membros e blocos são gravados como lista completa
   (mesmo contrato das pautas editoriais): o que não veio, sai.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

try:
    from ..db.models.candidacy import Candidacy
    from ..db.models.collection import (
        BLOCK_KIND_EXPENSE,
        BLOCK_KIND_PROPOSITION,
        BLOCK_KIND_SPEECH,
        BLOCK_KIND_VOTE,
        BLOCK_KINDS,
        REF_BLOCK_KINDS,
        STATUS_DRAFT,
        STATUS_PUBLISHED,
        Collection,
        CollectionBlock,
        CollectionMember,
    )
    from ..db.models.election_result import CandidacyResult
    from ..db.models.electoral_history import ElectoralHistory
    from ..db.models.parliamentarian import Parliamentarian
    from ..db.models.parliamentary_expense import ParliamentaryExpense
    from ..db.models.proposition import Proposition
    from ..db.models.roll_call_votes import RollCallVote
    from ..db.models.speeches_transcripts import SpeechesTranscript
    from ..routers.parliamentarians import _extract_photo_url_from_details
    from .editorial_agendas import tabelas_disponiveis
except ImportError:  # execução dentro de api/
    from db.models.candidacy import Candidacy
    from db.models.collection import (
        BLOCK_KIND_EXPENSE,
        BLOCK_KIND_PROPOSITION,
        BLOCK_KIND_SPEECH,
        BLOCK_KIND_VOTE,
        BLOCK_KINDS,
        REF_BLOCK_KINDS,
        STATUS_DRAFT,
        STATUS_PUBLISHED,
        Collection,
        CollectionBlock,
        CollectionMember,
    )
    from db.models.election_result import CandidacyResult
    from db.models.electoral_history import ElectoralHistory
    from db.models.parliamentarian import Parliamentarian
    from db.models.parliamentary_expense import ParliamentaryExpense
    from db.models.proposition import Proposition
    from db.models.roll_call_votes import RollCallVote
    from db.models.speeches_transcripts import SpeechesTranscript
    from routers.parliamentarians import _extract_photo_url_from_details
    from services.editorial_agendas import tabelas_disponiveis

TABELAS = ("collection", "collection_member", "collection_block")

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Quantos anos de cota entram no resumo de cada pessoa.
ANOS_COTA = 4


class CollectionError(ValueError):
    """Dado inválido vindo do admin. A mensagem vai para a tela."""


# --------------------------------------------------------------------------
# normalização
# --------------------------------------------------------------------------


def normalizar_cpf(valor: Any) -> Optional[str]:
    digitos = re.sub(r"\D", "", str(valor or ""))
    return digitos if len(digitos) == 11 else None


def _texto(valor: Any) -> Optional[str]:
    texto = " ".join(str(valor or "").split())
    return texto or None


def _iso(valor: Any) -> Optional[str]:
    return valor.isoformat() if valor is not None else None


def _num(valor: Any) -> Optional[float]:
    if valor is None:
        return None
    return float(valor) if isinstance(valor, (Decimal, int, float)) else None


def _tier_labels(valor: Any) -> dict[str, str]:
    if not isinstance(valor, dict):
        return {}
    labels: dict[str, str] = {}
    for chave, rotulo in valor.items():
        try:
            tier = int(chave)
        except (TypeError, ValueError):
            continue
        texto = _texto(rotulo)
        if 1 <= tier <= 5 and texto:
            labels[str(tier)] = texto
    return labels


def _fontes(valor: Any) -> list[dict[str, Optional[str]]]:
    if not isinstance(valor, list):
        return []
    fontes = []
    for item in valor:
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict):
            continue
        label, url = _texto(item.get("label")), _texto(item.get("url"))
        if label or url:
            fontes.append({"label": label, "url": url})
    return fontes


# --------------------------------------------------------------------------
# leitura
# --------------------------------------------------------------------------


def _resumo_colecao(c: Collection, membros: int) -> dict[str, Any]:
    return {
        "id": int(c.id),
        "slug": c.slug,
        "title": c.title,
        "subtitle": c.subtitle,
        "summary": c.summary,
        "status": c.status,
        "published_at": _iso(c.published_at),
        "updated_at": _iso(c.updated_at),
        "member_count": membros,
    }


def list_collections(db: Session, *, incluir_rascunhos: bool = False) -> list[dict[str, Any]]:
    if not tabelas_disponiveis(db, *TABELAS):
        return []

    contagem = dict(
        db.execute(
            select(CollectionMember.collection_id, func.count(CollectionMember.id)).group_by(
                CollectionMember.collection_id
            )
        ).all()
    )
    stmt = select(Collection)
    if not incluir_rascunhos:
        stmt = stmt.where(Collection.status == STATUS_PUBLISHED)
    stmt = stmt.order_by(Collection.published_at.desc().nullslast(), Collection.id.desc())
    return [
        _resumo_colecao(c, int(contagem.get(c.id, 0)))
        for c in db.execute(stmt).scalars().all()
    ]


def _buscar_colecao(
    db: Session, *, slug: Optional[str] = None, collection_id: Optional[int] = None
) -> Optional[Collection]:
    stmt = select(Collection)
    stmt = stmt.where(Collection.slug == slug) if slug else stmt.where(Collection.id == collection_id)
    return db.execute(stmt).scalars().first()


def get_collection_meta(
    db: Session, *, slug: str, incluir_rascunhos: bool = False
) -> Optional[dict[str, Any]]:
    """Só o cabeçalho da coleção, sem resolver membros: para rotas derivadas."""

    if not tabelas_disponiveis(db, *TABELAS):
        return None
    colecao = _buscar_colecao(db, slug=slug)
    if colecao is None or (colecao.status != STATUS_PUBLISHED and not incluir_rascunhos):
        return None
    return {
        "id": int(colecao.id),
        "slug": colecao.slug,
        "status": colecao.status,
        "settings": colecao.settings if isinstance(colecao.settings, dict) else {},
    }


def get_collection(
    db: Session,
    *,
    slug: Optional[str] = None,
    collection_id: Optional[int] = None,
    incluir_rascunhos: bool = False,
) -> Optional[dict[str, Any]]:
    """Coleção completa: membros com vínculos resolvidos e blocos com o registro de origem."""

    if not tabelas_disponiveis(db, *TABELAS):
        return None
    colecao = _buscar_colecao(db, slug=slug, collection_id=collection_id)
    if colecao is None:
        return None
    if colecao.status != STATUS_PUBLISHED and not incluir_rascunhos:
        return None

    membros = (
        db.execute(
            select(CollectionMember)
            .where(CollectionMember.collection_id == colecao.id)
            .order_by(CollectionMember.position, CollectionMember.id)
        )
        .scalars()
        .all()
    )
    blocos = (
        db.execute(
            select(CollectionBlock)
            .where(CollectionBlock.collection_id == colecao.id)
            .order_by(CollectionBlock.position, CollectionBlock.id)
        )
        .scalars()
        .all()
    )

    resultado = _resumo_colecao(colecao, len(membros))
    resultado["tier_labels"] = _tier_labels(colecao.tier_labels)
    resultado["settings"] = colecao.settings if isinstance(colecao.settings, dict) else {}
    resultado["members"] = resolve_members(db, membros)
    resultado["blocks"] = resolve_blocks(db, blocos)
    return resultado


# --------------------------------------------------------------------------
# resolução dos vínculos
# --------------------------------------------------------------------------


def _por_id(db: Session, modelo: Any, ids: Iterable[int]) -> dict[int, Any]:
    ids = {int(i) for i in ids if i is not None}
    if not ids:
        return {}
    return {int(r.id): r for r in db.execute(select(modelo).where(modelo.id.in_(ids))).scalars()}


def _candidatura_mais_recente_por_cpf(db: Session, cpfs: set[str]) -> dict[str, Candidacy]:
    if not cpfs:
        return {}
    escolhida: dict[str, Candidacy] = {}
    linhas = db.execute(select(Candidacy).where(Candidacy.cpf.in_(cpfs))).scalars()
    for c in linhas:
        atual = escolhida.get(c.cpf)
        if atual is None or (c.election_year or 0, c.id) > (atual.election_year or 0, atual.id):
            escolhida[c.cpf] = c
    return escolhida


def _parlamentar_por_cpf(db: Session, cpfs: set[str]) -> dict[str, Parliamentarian]:
    if not cpfs:
        return {}
    return {
        p.cpf: p
        for p in db.execute(select(Parliamentarian).where(Parliamentarian.cpf.in_(cpfs))).scalars()
    }


def _resultados(db: Session, candidacy_ids: set[int]) -> dict[int, CandidacyResult]:
    """Último turno de cada candidatura (o 2º, quando houver)."""

    if not candidacy_ids or not tabelas_disponiveis(db, "candidacy_result"):
        return {}
    escolhido: dict[int, CandidacyResult] = {}
    for r in db.execute(
        select(CandidacyResult).where(CandidacyResult.candidacy_id.in_(candidacy_ids))
    ).scalars():
        atual = escolhido.get(r.candidacy_id)
        if atual is None or (r.turno or 0) > (atual.turno or 0):
            escolhido[r.candidacy_id] = r
    return escolhido


def _cota(db: Session, parl_ids: set[int]) -> dict[int, dict[str, Any]]:
    """Gasto de cota por ano, com o fretamento de aeronaves separado."""

    if not parl_ids or not tabelas_disponiveis(db, "parliamentary_expense"):
        return {}
    ano_corte = datetime.now(timezone.utc).year - ANOS_COTA + 1
    aeronave = func.lower(ParliamentaryExpense.expense_type).like("%aeronave%")
    linhas = db.execute(
        select(
            ParliamentaryExpense.parliamentarian_id,
            ParliamentaryExpense.year,
            func.sum(ParliamentaryExpense.net_value),
            func.sum(ParliamentaryExpense.net_value).filter(aeronave),
        )
        .where(
            ParliamentaryExpense.parliamentarian_id.in_(parl_ids),
            ParliamentaryExpense.year >= ano_corte,
        )
        .group_by(ParliamentaryExpense.parliamentarian_id, ParliamentaryExpense.year)
    ).all()
    por_parl: dict[int, dict[str, Any]] = defaultdict(lambda: {"by_year": []})
    for parl_id, ano, total, fretamento in linhas:
        por_parl[int(parl_id)]["by_year"].append(
            {"year": int(ano), "total": _num(total) or 0.0, "aircraft": _num(fretamento) or 0.0}
        )
    for resumo in por_parl.values():
        resumo["by_year"].sort(key=lambda a: a["year"])
    return dict(por_parl)


def _patrimonio(
    db: Session, parl_ids: set[int], candidacy_ids: set[int]
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
    """Bens declarados ao TSE por eleição, pelo parlamentar e pela candidatura."""

    if not (parl_ids or candidacy_ids) or not tabelas_disponiveis(db, "electoral_history"):
        return {}, {}
    filtros = []
    if parl_ids:
        filtros.append(ElectoralHistory.parliamentarian_id.in_(parl_ids))
    if candidacy_ids:
        filtros.append(ElectoralHistory.candidacy_id.in_(candidacy_ids))
    por_parl: dict[int, list[dict[str, Any]]] = defaultdict(list)
    por_cand: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for h in db.execute(select(ElectoralHistory).where(or_(*filtros))).scalars():
        item = {
            "year": int(h.election_year),
            "office": h.office,
            "result": h.result,
            "declared_assets": _num(h.declared_assets),
        }
        if h.parliamentarian_id in parl_ids:
            por_parl[int(h.parliamentarian_id)].append(item)
        if h.candidacy_id in candidacy_ids:
            por_cand[int(h.candidacy_id)].append(item)
    return dict(por_parl), dict(por_cand)


def _serie_patrimonio(*listas: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    por_ano: dict[int, dict[str, Any]] = {}
    for lista in listas:
        for item in lista or []:
            por_ano.setdefault(item["year"], item)
    return [por_ano[a] for a in sorted(por_ano)]


def _parlamentar_out(p: Parliamentarian) -> dict[str, Any]:
    return {
        "id": int(p.id),
        "name": p.name,
        "type": p.type,
        "party": p.party,
        "state": p.state_elected,
        "status": p.status,
        "photo_url": _extract_photo_url_from_details(p.details),
    }


def _candidatura_out(c: Candidacy, r: Optional[CandidacyResult]) -> dict[str, Any]:
    return {
        "id": int(c.id),
        "election_year": c.election_year,
        "office": c.office,
        "state": c.state,
        "party": c.party,
        "ballot_name": c.ballot_name,
        "ballot_number": c.ballot_number,
        "photo_url": c.photo_url,
        "result": None
        if r is None
        else {
            "round": r.turno,
            "status": r.situacao,
            "elected": r.eleito,
            "votes": r.votos,
            "percent": _num(r.percentual),
            "final": bool(r.totalizacao_final),
        },
    }


def resolver_vinculos(
    db: Session, membros: list[CollectionMember]
) -> list[tuple[Optional[Parliamentarian], Optional[Candidacy]]]:
    """Quem é cada pessoa hoje na base: (parlamentar, candidatura), na ordem dos membros."""

    cpfs = {c for c in (normalizar_cpf(m.cpf) for m in membros) if c}
    parl_por_id = _por_id(db, Parliamentarian, (m.parliamentarian_id for m in membros))
    cand_por_id = _por_id(db, Candidacy, (m.candidacy_id for m in membros))
    cand_por_cpf = _candidatura_mais_recente_por_cpf(db, cpfs)
    parl_por_cpf = _parlamentar_por_cpf(db, cpfs)

    # Primeiro passo: quem é cada pessoa hoje.
    vinculos: list[tuple[Optional[Parliamentarian], Optional[Candidacy]]] = []
    ids_via_candidatura: set[int] = set()
    for m in membros:
        cpf = normalizar_cpf(m.cpf)
        cand = cand_por_id.get(m.candidacy_id) if m.candidacy_id else None
        if cand is None and cpf:
            cand = cand_por_cpf.get(cpf)
        parl = parl_por_id.get(m.parliamentarian_id) if m.parliamentarian_id else None
        if parl is None and cpf:
            parl = parl_por_cpf.get(cpf)
        if parl is None and cand is not None and cand.parliamentarian_id:
            ids_via_candidatura.add(int(cand.parliamentarian_id))
        vinculos.append((parl, cand))

    parl_por_id.update(_por_id(db, Parliamentarian, ids_via_candidatura - set(parl_por_id)))
    return [
        (parl or (parl_por_id.get(int(cand.parliamentarian_id)) if cand and cand.parliamentarian_id else None), cand)
        for parl, cand in vinculos
    ]


def resolve_members(db: Session, membros: list[CollectionMember]) -> list[dict[str, Any]]:
    vinculos = resolver_vinculos(db, membros)
    parl_ids = {int(p.id) for p, _ in vinculos if p is not None}
    cand_ids = {int(c.id) for _, c in vinculos if c is not None}
    resultados = _resultados(db, cand_ids)
    cota = _cota(db, parl_ids)
    patr_parl, patr_cand = _patrimonio(db, parl_ids, cand_ids)

    saida = []
    for m, (parl, cand) in zip(membros, vinculos):
        saida.append(
            {
                "id": int(m.id),
                "display_name": m.display_name,
                "role_label": m.role_label,
                "tier": m.tier,
                "context": m.context,
                "sources": _fontes(m.sources),
                "position": int(m.position or 0),
                "parliamentarian": _parlamentar_out(parl) if parl else None,
                "candidacy": _candidatura_out(cand, resultados.get(int(cand.id))) if cand else None,
                "expenses": cota.get(int(parl.id)) if parl else None,
                "assets": _serie_patrimonio(
                    patr_parl.get(int(parl.id)) if parl else None,
                    patr_cand.get(int(cand.id)) if cand else None,
                ),
            }
        )
    return saida


def _ref_speech(s: SpeechesTranscript) -> dict[str, Any]:
    return {
        "date": _iso(s.date),
        "type": s.type,
        "summary": s.summary,
        "link": s.speech_link or s.publication_link,
        "parliamentarian_id": s.parliamentarian_id,
    }


def _ref_vote(v: RollCallVote, props: dict[int, Proposition]) -> dict[str, Any]:
    prop = props.get(v.proposition_id) if v.proposition_id else None
    return {
        "date": _iso(v.vote_date),
        "vote": v.vote,
        "description": v.description,
        "link": v.link,
        "parliamentarian_id": v.parliamentarian_id,
        "proposition": _ref_proposition(prop) if prop else None,
    }


def _ref_proposition(p: Proposition) -> dict[str, Any]:
    return {
        "id": int(p.id),
        "acronym": p.proposition_acronym,
        "number": p.proposition_number,
        "year": p.presentation_year,
        "title": p.title,
        "summary": p.summary,
        "status": p.current_status,
        "link": p.link,
    }


def _ref_expense(e: ParliamentaryExpense) -> dict[str, Any]:
    return {
        "date": _iso(e.document_date),
        "year": e.year,
        "month": e.month,
        "type": e.expense_type,
        "supplier": e.supplier_name,
        "value": _num(e.net_value),
        "link": e.document_url,
        "parliamentarian_id": e.parliamentarian_id,
    }


def resolve_blocks(db: Session, blocos: list[CollectionBlock]) -> list[dict[str, Any]]:
    ids: dict[str, set[int]] = defaultdict(set)
    for b in blocos:
        if b.kind in REF_BLOCK_KINDS and b.ref_id is not None:
            ids[b.kind].add(int(b.ref_id))

    discursos = _por_id(db, SpeechesTranscript, ids[BLOCK_KIND_SPEECH])
    votos = _por_id(db, RollCallVote, ids[BLOCK_KIND_VOTE])
    props = _por_id(
        db,
        Proposition,
        ids[BLOCK_KIND_PROPOSITION] | {int(v.proposition_id) for v in votos.values() if v.proposition_id},
    )
    gastos = _por_id(db, ParliamentaryExpense, ids[BLOCK_KIND_EXPENSE])

    saida = []
    for b in blocos:
        ref: Optional[dict[str, Any]] = None
        if b.ref_id is not None:
            alvo = int(b.ref_id)
            if b.kind == BLOCK_KIND_SPEECH and alvo in discursos:
                ref = _ref_speech(discursos[alvo])
            elif b.kind == BLOCK_KIND_VOTE and alvo in votos:
                ref = _ref_vote(votos[alvo], props)
            elif b.kind == BLOCK_KIND_PROPOSITION and alvo in props:
                ref = _ref_proposition(props[alvo])
            elif b.kind == BLOCK_KIND_EXPENSE and alvo in gastos:
                ref = _ref_expense(gastos[alvo])
        saida.append(
            {
                "id": int(b.id),
                "kind": b.kind,
                "member_id": b.member_id,
                "ref_id": b.ref_id,
                "title": b.title,
                "body": b.body,
                "url": b.url,
                "payload": b.payload if isinstance(b.payload, dict) else {},
                "position": int(b.position or 0),
                # None quando o registro de origem sumiu da base: a tela mostra
                # o texto do admin e esconde o cartão do registro.
                "ref": ref,
            }
        )
    return saida


# --------------------------------------------------------------------------
# escrita (admin)
# --------------------------------------------------------------------------


def _validar_meta(dados: dict[str, Any], *, atual: Optional[Collection] = None) -> dict[str, Any]:
    slug = _texto(dados.get("slug", atual.slug if atual else None))
    titulo = _texto(dados.get("title", atual.title if atual else None))
    status = dados.get("status", atual.status if atual else STATUS_DRAFT)
    if not slug or not _SLUG_RE.match(slug):
        raise CollectionError(
            "O endereço da coleção precisa usar só letras minúsculas, números e hífens."
        )
    if not titulo:
        raise CollectionError("A coleção precisa de um título.")
    if status not in (STATUS_DRAFT, STATUS_PUBLISHED):
        raise CollectionError("Situação inválida: use rascunho ou publicada.")
    return {"slug": slug, "title": titulo, "status": status}


def _slug_em_uso(db: Session, slug: str, *, exceto: Optional[int] = None) -> bool:
    stmt = select(Collection.id).where(Collection.slug == slug)
    if exceto is not None:
        stmt = stmt.where(Collection.id != exceto)
    return db.execute(stmt).first() is not None


def _aplicar_meta(c: Collection, dados: dict[str, Any], validado: dict[str, Any]) -> None:
    c.slug = validado["slug"]
    c.title = validado["title"]
    for campo in ("subtitle", "summary"):
        if campo in dados:
            setattr(c, campo, _texto(dados[campo]))
    if "tier_labels" in dados:
        c.tier_labels = _tier_labels(dados["tier_labels"])
    if "settings" in dados:
        c.settings = dados["settings"] if isinstance(dados["settings"], dict) else {}
    if validado["status"] == STATUS_PUBLISHED and c.published_at is None:
        c.published_at = datetime.now(timezone.utc)
    c.status = validado["status"]


def create_collection(db: Session, dados: dict[str, Any]) -> dict[str, Any]:
    validado = _validar_meta(dados)
    if _slug_em_uso(db, validado["slug"]):
        raise CollectionError(f"Já existe uma coleção com o endereço '{validado['slug']}'.")
    c = Collection(tier_labels={}, settings={})
    _aplicar_meta(c, dados, validado)
    db.add(c)
    db.flush()
    return get_collection(db, collection_id=int(c.id), incluir_rascunhos=True)


def update_collection(db: Session, collection_id: int, dados: dict[str, Any]) -> Optional[dict[str, Any]]:
    c = _buscar_colecao(db, collection_id=collection_id)
    if c is None:
        return None
    validado = _validar_meta(dados, atual=c)
    if _slug_em_uso(db, validado["slug"], exceto=int(c.id)):
        raise CollectionError(f"Já existe uma coleção com o endereço '{validado['slug']}'.")
    _aplicar_meta(c, dados, validado)
    db.flush()
    return get_collection(db, collection_id=int(c.id), incluir_rascunhos=True)


def delete_collection(db: Session, collection_id: int) -> bool:
    c = _buscar_colecao(db, collection_id=collection_id)
    if c is None:
        return False
    # Sem depender do ON DELETE CASCADE: o SQLite dos testes não liga FK.
    db.query(CollectionBlock).filter(CollectionBlock.collection_id == c.id).delete()
    db.query(CollectionMember).filter(CollectionMember.collection_id == c.id).delete()
    db.delete(c)
    db.flush()
    return True


def replace_members(
    db: Session, collection_id: int, itens: list[dict[str, Any]]
) -> Optional[list[dict[str, Any]]]:
    """Grava a lista completa de pessoas. Item com `id` atualiza; sem `id`, cria.

    Vínculo (`cpf`, `parliamentarian_id`, `candidacy_id`) que não vem no item
    de uma pessoa existente fica como está: a leitura não devolve o CPF, e o
    editor não pode apagar o vínculo só por salvar o texto.
    """

    c = _buscar_colecao(db, collection_id=collection_id)
    if c is None:
        return None
    existentes = {
        int(m.id): m
        for m in db.execute(
            select(CollectionMember).where(CollectionMember.collection_id == c.id)
        ).scalars()
    }
    # Os vínculos que já estavam gravados também entram: o item pode não trazê-los.
    parl_validos = set(
        _por_id(
            db,
            Parliamentarian,
            [i.get("parliamentarian_id") for i in itens] + [m.parliamentarian_id for m in existentes.values()],
        )
    )
    cand_validas = set(
        _por_id(
            db, Candidacy, [i.get("candidacy_id") for i in itens] + [m.candidacy_id for m in existentes.values()]
        )
    )
    mantidos: set[int] = set()
    for posicao, item in enumerate(itens):
        nome = _texto(item.get("display_name"))
        if not nome:
            raise CollectionError(f"A pessoa na posição {posicao + 1} está sem nome.")
        tier = item.get("tier")
        if tier is not None and (not isinstance(tier, int) or not 1 <= tier <= 5):
            raise CollectionError(f"Nível inválido para {nome}: use um número de 1 a 5.")
        item_id = item.get("id")
        atual = existentes.get(int(item_id)) if item_id is not None else None
        if atual is not None:
            item = {
                "cpf": atual.cpf,
                "parliamentarian_id": atual.parliamentarian_id,
                "candidacy_id": atual.candidacy_id,
                **item,
            }
        cpf_bruto = item.get("cpf")
        cpf = normalizar_cpf(cpf_bruto)
        if cpf_bruto and not cpf:
            raise CollectionError(f"CPF inválido para {nome}: são 11 dígitos.")

        parl_id, cand_id = item.get("parliamentarian_id"), item.get("candidacy_id")
        if parl_id is not None and int(parl_id) not in parl_validos:
            raise CollectionError(f"{nome}: parlamentar {parl_id} não existe na base.")
        if cand_id is not None and int(cand_id) not in cand_validas:
            raise CollectionError(f"{nome}: candidatura {cand_id} não existe na base.")

        if atual is not None:
            m = atual
            mantidos.add(int(item_id))
        else:
            m = CollectionMember(collection_id=c.id)
            db.add(m)
        m.display_name = nome
        m.role_label = _texto(item.get("role_label"))
        m.cpf = cpf
        m.parliamentarian_id = parl_id
        m.candidacy_id = cand_id
        m.tier = tier
        m.context = (item.get("context") or "").strip() or None
        m.sources = _fontes(item.get("sources"))
        m.position = posicao

    removidos = set(existentes) - mantidos
    if removidos:
        db.query(CollectionBlock).filter(CollectionBlock.member_id.in_(removidos)).update(
            {CollectionBlock.member_id: None}, synchronize_session=False
        )
        db.query(CollectionMember).filter(CollectionMember.id.in_(removidos)).delete(
            synchronize_session=False
        )
    db.flush()
    return get_collection(db, collection_id=int(c.id), incluir_rascunhos=True)["members"]


def replace_blocks(
    db: Session, collection_id: int, itens: list[dict[str, Any]]
) -> Optional[list[dict[str, Any]]]:
    """Grava a lista completa de blocos, na ordem recebida."""

    c = _buscar_colecao(db, collection_id=collection_id)
    if c is None:
        return None
    membros = set(
        db.execute(
            select(CollectionMember.id).where(CollectionMember.collection_id == c.id)
        ).scalars()
    )
    existentes = {
        int(b.id): b
        for b in db.execute(
            select(CollectionBlock).where(CollectionBlock.collection_id == c.id)
        ).scalars()
    }
    mantidos: set[int] = set()
    for posicao, item in enumerate(itens):
        tipo = item.get("kind")
        if tipo not in BLOCK_KINDS:
            raise CollectionError(f"Tipo de bloco desconhecido na posição {posicao + 1}.")
        ref_id = item.get("ref_id")
        if tipo in REF_BLOCK_KINDS and ref_id is None:
            raise CollectionError(
                f"O bloco na posição {posicao + 1} precisa apontar para um registro da base."
            )
        member_id = item.get("member_id")
        if member_id is not None and int(member_id) not in membros:
            raise CollectionError(
                f"O bloco na posição {posicao + 1} aponta para uma pessoa que não está na coleção."
            )

        item_id = item.get("id")
        if item_id is not None and int(item_id) in existentes:
            b = existentes[int(item_id)]
            mantidos.add(int(item_id))
        else:
            b = CollectionBlock(collection_id=c.id)
            db.add(b)
        b.kind = tipo
        b.ref_id = ref_id if tipo in REF_BLOCK_KINDS else None
        b.member_id = member_id
        b.title = _texto(item.get("title"))
        b.body = (item.get("body") or "").strip() or None
        b.url = _texto(item.get("url"))
        b.payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        b.position = posicao

    removidos = set(existentes) - mantidos
    if removidos:
        db.query(CollectionBlock).filter(CollectionBlock.id.in_(removidos)).delete(
            synchronize_session=False
        )
    db.flush()
    return get_collection(db, collection_id=int(c.id), incluir_rascunhos=True)["blocks"]
