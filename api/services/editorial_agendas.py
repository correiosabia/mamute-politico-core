"""Regras do vocabulário fechado de pautas editoriais (CS-72).

Três regras vivem aqui, e não na tela, porque são de domínio: o job de
classificação também depende delas e nunca passa pelo front.

1. SLUG DE PAUTA EXISTENTE NÃO MUDA. As classificações já gravadas apontam
   para a pauta por ele, e o filtro do front também. Deixar mudar em silêncio
   quebra a correspondência sem erro nenhum — o pior dos desfechos possíveis.
2. NUNCA APAGAR PAUTA. Some da lista? Vira `active = false`. Mudar
   configuração não destrói trabalho do job, igual às marcações (SPEC-001).
3. MUDANÇA REAL SOBE A `vocabulary_version`. É o gatilho da reclassificação;
   sem ela o admin edita a `description` e nada acontece até alguém rodar
   `--force` na mão.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlalchemy import inspect as sqlalchemy_inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

try:
    from ..db.models.editorial_agenda import EditorialAgenda
except ImportError:  # execução dentro de api/
    from db.models.editorial_agenda import EditorialAgenda

TABELAS = ("editorial_agenda", "parliamentarian_agenda")


class SlugImutavelError(ValueError):
    """Tentativa de trocar o slug de uma pauta que já existe."""

    def __init__(self, *, agenda_id: int, atual: str, novo: str) -> None:
        self.agenda_id = agenda_id
        self.atual = atual
        self.novo = novo
        super().__init__(
            f"O identificador da pauta '{atual}' não pode ser alterado para "
            f"'{novo}': as classificações já gravadas apontam para ele. "
            "Desative esta pauta e crie uma nova."
        )


def tabelas_disponiveis(db: Session, *table_names: str) -> bool:
    """As tabelas da CS-72 já existem no schema conectado?

    O deploy aplica as migrations DEPOIS de subir os containers, então há uma
    janela em que o código novo roda contra o schema velho. Sem esta guarda o
    primeiro deploy derruba a listagem de parlamentares inteira — não só o
    campo novo. Mesma defesa do `_table_has_column` em roll_call_votes.py.

    Todos os nomes num round-trip só: isto roda em toda listagem.

    Inspeciona `db.connection()`, e NÃO o engine: inspecionar o engine puxa uma
    segunda conexão do pool e a devolve com rollback. No meio de uma escrita
    isso descarta o que a sessão ainda não commitou — foi exatamente assim que
    o `replace_agendas` perdeu os INSERTs na primeira versão.

    Devolve True quando não consegue inspecionar — indisponibilidade do banco é
    problema de outra camada, e responder False ali esconderia dado que existe.
    """

    nomes = table_names or TABELAS
    try:
        existentes = set(sqlalchemy_inspect(db.connection()).get_table_names())
    except SQLAlchemyError:
        return True
    return all(nome in existentes for nome in nomes)


def _serializar(agenda: EditorialAgenda) -> dict[str, Any]:
    return {
        "id": int(agenda.id),
        "name": agenda.name,
        "slug": agenda.slug,
        "description": agenda.description,
        "position": int(agenda.position or 0),
        "active": bool(agenda.active),
        "vocabulary_version": int(agenda.vocabulary_version or 1),
    }


def get_agendas(
    db: Session, *, incluir_inativas: bool = False
) -> list[dict[str, Any]]:
    """Vocabulário atual, na ordem em que a tela lista os filtros."""

    if not tabelas_disponiveis(db, "editorial_agenda"):
        return []

    stmt = select(EditorialAgenda)
    if not incluir_inativas:
        stmt = stmt.where(EditorialAgenda.active.is_(True))
    stmt = stmt.order_by(EditorialAgenda.position, EditorialAgenda.id)

    return [_serializar(a) for a in db.execute(stmt).scalars().all()]


def vocabulary_version_atual(db: Session) -> int:
    """Versão corrente = MAX da coluna.

    Mora na própria `editorial_agenda` em vez de numa tabela de linha única
    (ver docstring da migration cs72a1b2c3d4). É o número que o job compara
    com o gravado em cada classificação para saber se precisa refazer.
    """

    if not tabelas_disponiveis(db, "editorial_agenda"):
        return 1
    versoes = db.execute(select(EditorialAgenda.vocabulary_version)).scalars().all()
    return max((int(v or 1) for v in versoes), default=1)


def _normalizar_slug(valor: Any) -> str:
    return " ".join(str(valor or "").split()).strip().lower()


def _normalizar_texto(valor: Any) -> str:
    return " ".join(str(valor or "").split())


def _normalizar_descricao(valor: Any) -> Optional[str]:
    """Preserva as quebras de parágrafo — a description é um texto de prompt."""

    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def replace_agendas(
    db: Session, itens: Iterable[Any]
) -> list[dict[str, Any]]:
    """Substitui o vocabulário inteiro — a tela edita e salva de uma vez.

    Não faz commit: quem chama decide o momento, para que a linha de auditoria
    entre na mesma transação. Mesmo contrato do `replace_terms` da nuvem.

    Levanta `SlugImutavelError` se algum item tentar renomear o slug de uma
    pauta existente.
    """

    existentes = {
        int(a.id): a for a in db.execute(select(EditorialAgenda)).scalars().all()
    }
    por_slug = {a.slug: a for a in existentes.values()}

    mudou = False
    vistos: set[int] = set()

    for posicao, item in enumerate(itens or [], start=1):
        agenda_id = getattr(item, "id", None)
        name = _normalizar_texto(getattr(item, "name", ""))
        slug = _normalizar_slug(getattr(item, "slug", ""))
        description = _normalizar_descricao(getattr(item, "description", None))
        active = bool(getattr(item, "active", True))

        if not name or not slug:
            continue

        atual = existentes.get(int(agenda_id)) if agenda_id is not None else None

        # Item sem id mas com slug conhecido é a mesma pauta: a tela pode ter
        # perdido o id num reload. Casar pelo slug evita criar duplicata e
        # evita violar a UNIQUE.
        if atual is None:
            atual = por_slug.get(slug)

        if atual is not None:
            if atual.slug != slug:
                raise SlugImutavelError(
                    agenda_id=int(atual.id), atual=atual.slug, novo=slug
                )
            vistos.add(int(atual.id))
            if (
                atual.name != name
                or atual.description != description
                or int(atual.position or 0) != posicao
                or bool(atual.active) != active
            ):
                # `position` sozinha não muda classificação, mas as outras três
                # mudam — e distinguir daria uma versão por campo sem ganho.
                atual.name = name
                atual.description = description
                atual.position = posicao
                atual.active = active
                mudou = True
            continue

        db.add(
            EditorialAgenda(
                name=name,
                slug=slug,
                description=description,
                position=posicao,
                active=active,
            )
        )
        mudou = True

    # Pauta omitida do PUT é desativada, nunca apagada: pode já ter
    # classificado gente, e a linha de classificação some junto pelo CASCADE.
    for agenda_id, agenda in existentes.items():
        if agenda_id in vistos:
            continue
        if agenda.active:
            agenda.active = False
            mudou = True

    db.flush()

    if mudou:
        # Só sobe a versão quando algo mudou de verdade. Abrir a tela e salvar
        # sem editar não pode custar uma reclassificação de 500 parlamentares
        # na OpenRouter.
        proxima = vocabulary_version_atual(db) + 1
        for agenda in db.execute(select(EditorialAgenda)).scalars().all():
            agenda.vocabulary_version = proxima
        db.flush()

    return get_agendas(db, incluir_inativas=True)


__all__ = [
    "SlugImutavelError",
    "get_agendas",
    "replace_agendas",
    "tabelas_disponiveis",
    "vocabulary_version_atual",
    "TABELAS",
]
