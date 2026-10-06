"""Busca de registros da base para o admin anexar a uma coleção (CS-132).

Dado um termo, lista discursos, votações e proposições dos membros da coleção
que falam dele. Serve para duas coisas no editor: achar o id de um registro
sem sair da tela e varrer o que os membros disseram ou votaram sobre um tema.

Regras:

1. SÓ OS MEMBROS DA COLEÇÃO. A busca roda dentro dos parlamentares já
   vinculados (vínculo resolvido na leitura, como no resto das coleções), ou
   de um membro só quando o admin pede.
2. VOTAÇÃO AGRUPADA. Uma votação nominal vira um grupo (proposição + data +
   descrição) com o voto de cada membro: é assim que se vê quem votou junto.
   A descrição entra na chave porque a mesma proposição tem várias votações no
   mesmo dia (texto principal, cada emenda destacada).
3. ADMIN ESCOLHE. Nada aqui grava bloco; o editor manda os escolhidos pela
   rota de blocos.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

try:
    from ..db.models.authors_proposition import AuthorsProposition
    from ..db.models.collection import CollectionMember
    from ..db.models.proposition import Proposition
    from ..db.models.roll_call_votes import RollCallVote
    from ..db.models.speeches_transcripts import SpeechesTranscript
    from .collection import (
        TABELAS,
        CollectionError,
        _buscar_colecao,
        _iso,
        _ref_proposition,
        resolver_vinculos,
    )
    from .editorial_agendas import tabelas_disponiveis
except ImportError:  # execução dentro de api/
    from db.models.authors_proposition import AuthorsProposition
    from db.models.collection import CollectionMember
    from db.models.proposition import Proposition
    from db.models.roll_call_votes import RollCallVote
    from db.models.speeches_transcripts import SpeechesTranscript
    from services.collection import (
        TABELAS,
        CollectionError,
        _buscar_colecao,
        _iso,
        _ref_proposition,
        resolver_vinculos,
    )
    from services.editorial_agendas import tabelas_disponiveis

TIPOS = ("speech", "vote", "proposition")
LIMITE_PADRAO = 30
# Trecho do discurso em volta do termo, para o admin decidir sem abrir a íntegra.
_JANELA = 160


def _trecho(texto: Optional[str], termo: str) -> Optional[str]:
    if not texto:
        return None
    pos = texto.lower().find(termo.lower())
    if pos < 0:
        return None
    inicio, fim = max(pos - _JANELA, 0), pos + len(termo) + _JANELA
    trecho = " ".join(texto[inicio:fim].split())
    return f"{'…' if inicio else ''}{trecho}{'…' if fim < len(texto) else ''}"


def search_records(
    db: Session,
    collection_id: int,
    *,
    termo: str,
    tipos: tuple[str, ...] = TIPOS,
    member_id: Optional[int] = None,
    limite: int = LIMITE_PADRAO,
) -> Optional[dict[str, Any]]:
    """Registros dos membros que citam o termo. None se a coleção não existe."""

    if not tabelas_disponiveis(db, *TABELAS):
        return None
    colecao = _buscar_colecao(db, collection_id=collection_id)
    if colecao is None:
        return None
    termo = " ".join((termo or "").split())
    if len(termo) < 3:
        raise CollectionError("Digite pelo menos 3 letras para buscar.")
    limite = max(1, min(int(limite), 100))

    stmt = select(CollectionMember).where(CollectionMember.collection_id == colecao.id)
    if member_id is not None:
        stmt = stmt.where(CollectionMember.id == member_id)
    membros = db.execute(stmt.order_by(CollectionMember.position)).scalars().all()
    membro_por_parl: dict[int, CollectionMember] = {}
    for m, (parl, _cand) in zip(membros, resolver_vinculos(db, membros)):
        if parl is not None:
            membro_por_parl.setdefault(int(parl.id), m)

    saida: dict[str, Any] = {"term": termo, "speeches": [], "votes": [], "propositions": []}
    if not membro_por_parl:
        return saida
    padrao = f"%{termo}%"

    def _quem(parl_id: Any) -> dict[str, Any]:
        m = membro_por_parl[int(parl_id)]
        return {"member_id": int(m.id), "display_name": m.display_name}

    if "speech" in tipos and tabelas_disponiveis(db, "speeches_transcripts"):
        discursos = db.execute(
            select(SpeechesTranscript)
            .where(
                SpeechesTranscript.parliamentarian_id.in_(membro_por_parl),
                or_(
                    SpeechesTranscript.summary.ilike(padrao),
                    SpeechesTranscript.speech_text.ilike(padrao),
                ),
            )
            .order_by(SpeechesTranscript.date.desc().nullslast(), SpeechesTranscript.id.desc())
            .limit(limite)
        ).scalars()
        saida["speeches"] = [
            {
                "ref_id": int(s.id),
                **_quem(s.parliamentarian_id),
                "date": _iso(s.date),
                "summary": s.summary,
                "excerpt": _trecho(s.speech_text, termo) or _trecho(s.summary, termo),
                "link": s.speech_link or s.publication_link,
            }
            for s in discursos
        ]

    if "vote" in tipos and tabelas_disponiveis(db, "roll_call_votes", "proposition"):
        linhas = db.execute(
            select(RollCallVote, Proposition)
            .join(Proposition, Proposition.id == RollCallVote.proposition_id)
            .where(
                RollCallVote.parliamentarian_id.in_(membro_por_parl),
                or_(
                    Proposition.title.ilike(padrao),
                    Proposition.summary.ilike(padrao),
                    RollCallVote.description.ilike(padrao),
                ),
            )
            .order_by(RollCallVote.vote_date.desc().nullslast(), RollCallVote.id.desc())
            .limit(limite * 20)
        ).all()
        grupos: dict[tuple[int, Optional[str], Optional[str]], dict[str, Any]] = {}
        for voto, prop in linhas:
            chave = (int(prop.id), _iso(voto.vote_date), voto.description)
            grupo = grupos.get(chave)
            if grupo is None:
                if len(grupos) >= limite:
                    continue
                grupo = grupos[chave] = {
                    "date": _iso(voto.vote_date),
                    "description": voto.description,
                    "proposition": _ref_proposition(prop),
                    "votes": [],
                }
            grupo["votes"].append(
                {"ref_id": int(voto.id), **_quem(voto.parliamentarian_id), "vote": voto.vote, "link": voto.link}
            )
        for grupo in grupos.values():
            contagem: dict[str, int] = defaultdict(int)
            for v in grupo["votes"]:
                contagem[v["vote"] or "Sem registro"] += 1
            grupo["tally"] = dict(contagem)
        saida["votes"] = list(grupos.values())

    if "proposition" in tipos and tabelas_disponiveis(db, "authors_proposition", "proposition"):
        linhas = db.execute(
            select(Proposition, AuthorsProposition.parliamentarian_id)
            .join(AuthorsProposition, AuthorsProposition.proposition_id == Proposition.id)
            .where(
                AuthorsProposition.parliamentarian_id.in_(membro_por_parl),
                or_(Proposition.title.ilike(padrao), Proposition.summary.ilike(padrao)),
            )
            .order_by(Proposition.presentation_date.desc().nullslast(), Proposition.id.desc())
            .limit(limite * 5)
        ).all()
        props: dict[int, dict[str, Any]] = {}
        for prop, parl_id in linhas:
            item = props.get(int(prop.id))
            if item is None:
                if len(props) >= limite:
                    continue
                item = props[int(prop.id)] = {
                    "ref_id": int(prop.id),
                    **_ref_proposition(prop),
                    "date": _iso(prop.presentation_date),
                    "authors": [],
                }
            item["authors"].append(_quem(parl_id))
        saida["propositions"] = list(props.values())

    return saida
