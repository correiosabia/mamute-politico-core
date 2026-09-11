"""Classifica cada parlamentar em 1 a 3 pautas editoriais (CS-72).

Quem acompanha política procura parlamentar por assunto, não por nome. Este job
é quem preenche esse eixo: lê o que a pessoa efetivamente falou e propôs, e
grava as pautas dominantes em `parliamentarian_agenda`.

TRÊS REGRAS QUE NÃO SÃO DETALHE DE IMPLEMENTAÇÃO:

1. VOCABULÁRIO FECHADO. O prompt recebe a lista ativa e slug que não estiver
   nela é DESCARTADO, nunca criado. Sem esta linha a feature vira tag livre
   gerada por IA — exatamente o que o brief proíbe.
2. SEM EVIDÊNCIA, SEM PAUTA. Se sobrar zero slug válido, o parlamentar fica sem
   classificação e o job loga o motivo. Não existe "Outros": uma pauta genérica
   de escape faria a tela mentir para todo mundo que não tem discurso.
3. A ESCADA DE ENTRADA NÃO COMEÇA NAS KEYWORDS DO SENADO.
   `speeches_transcripts_keywords` só tem Senado; usá-la como fonte primária
   deixaria 513 deputados de fora. A ordem é sumário → palavras-chave →
   íntegra → ementas de autoria.

4. TENTATIVA SE REGISTRA, MESMO QUANDO NAO DA EM NADA.
   `parliamentarian_agenda_run` grava "olhei fulano em tal dia". Sem isso, zero
   linhas de pauta significa ao mesmo tempo "nunca analisei" e "analisei e nada
   encaixou", e quem caiu no segundo caso volta ao modelo em TODA rodada, para
   sempre, sem nunca gravar nada — gasto que nao deixa rastro para alguem
   notar.

Idempotente: a escrita é delete-then-insert das linhas do parlamentar dentro da
mesma transação, então rodar duas vezes seguidas não muda nada no banco e nunca
deixa `rank` duplicado.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from mamute_scrappers.db.models.authors_proposition import AuthorsProposition
from mamute_scrappers.db.models.editorial_agenda import (
    MAX_PAUTAS_POR_PARLAMENTAR,
    RUN_CLASSIFICADO,
    RUN_SEM_MATERIAL,
    RUN_SEM_PAUTA,
    EditorialAgenda,
    ParliamentarianAgenda,
    ParliamentarianAgendaRun,
)
from mamute_scrappers.db.models.parliamentarian import Parliamentarian
from mamute_scrappers.db.models.proposition import Proposition
from mamute_scrappers.db.models.speeches_transcripts import SpeechesTranscript
from mamute_scrappers.db.session import session_scope

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - depende de pacote opcional
    OpenAI = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Mesmo modelo do chatbot. Vale a pena manter igual: é barato, tem janela
# grande o suficiente para o material de um parlamentar e já está pago pela
# mesma chave.
DEFAULT_MODEL = "google/gemini-2.5-flash"

# ~6k tokens de material por parlamentar. Acima disso o custo cresce sem que a
# classificação melhore — as pautas dominantes já apareceram muito antes.
# 4 caracteres por token é a régua grosseira usual para português.
MAX_CARACTERES_ENTRADA = 6000 * 4

# Quantos discursos, no máximo, entram na montagem do texto. Corta o caso
# patológico de quem tem milhares.
MAX_DISCURSOS = 60
MAX_EMENTAS = 40

# Palavra-chave curta demais é ruído de tokenização, não assunto.
TAMANHO_MINIMO_TERMO = 3


@dataclass(frozen=True)
class Pauta:
    """Uma entrada do vocabulário ativo, como o prompt a enxerga."""

    id: int
    name: str
    slug: str
    description: Optional[str]


@dataclass(frozen=True)
class Classificacao:
    """Uma pauta atribuída, já validada contra o vocabulário."""

    agenda_id: int
    slug: str
    rank: int
    confidence: Optional[float]
    evidence: Optional[str]


# --------------------------------------------------------------------------
# Vocabulário
# --------------------------------------------------------------------------


def carregar_vocabulario(session: Session) -> list[Pauta]:
    """Pautas ativas, na ordem em que o admin as organizou."""

    linhas = (
        session.execute(
            select(
                EditorialAgenda.id,
                EditorialAgenda.name,
                EditorialAgenda.slug,
                EditorialAgenda.description,
            )
            .where(EditorialAgenda.active.is_(True))
            .order_by(EditorialAgenda.position, EditorialAgenda.id)
        )
        .all()
    )
    return [Pauta(id=i, name=n, slug=s, description=d) for i, n, s, d in linhas]


def vocabulary_version_atual(session: Session) -> int:
    """Versão corrente = MAX da coluna em `editorial_agenda`.

    É o número que o PUT do admin sobe quando o vocabulário muda de verdade, e
    é comparado com o gravado em cada classificação para saber o que refazer.
    """

    return int(
        session.execute(select(func.max(EditorialAgenda.vocabulary_version))).scalar()
        or 1
    )

def carregar_stopwords(session: Session) -> set[str]:

    try:
        linhas = session.execute(text("select term from word_cloud_terms")).scalars()
        return {str(t).strip().lower() for t in linhas if str(t).strip()}
    except SQLAlchemyError:
        logger.debug("word_cloud_terms indisponível; seguindo sem filtro de termos.")
        session.rollback()
        return set()


def _limpar_termos(bruto: Optional[str], stopwords: set[str]) -> list[str]:
    """Quebra a lista de palavras-chave e tira o que é ruído."""

    if not bruto:
        return []

    termos: list[str] = []
    vistos: set[str] = set()
    for pedaco in str(bruto).replace(";", ",").split(","):
        termo = " ".join(pedaco.split()).strip()
        if len(termo) < TAMANHO_MINIMO_TERMO:
            continue
        chave = termo.lower()
        if chave in stopwords or chave in vistos:
            continue
        vistos.add(chave)
        termos.append(termo)
    return termos


def montar_texto(
    session: Session, parliamentarian_id: int, stopwords: set[str]
) -> tuple[str, str]:
    partes: list[str] = []
    origens: list[str] = []
    tamanho = 0

    def _cabe(texto: str) -> bool:
        nonlocal tamanho
        if not texto or tamanho >= MAX_CARACTERES_ENTRADA:
            return False
        partes.append(texto)
        tamanho += len(texto)
        return True

    discursos = (
        session.execute(
            select(
                SpeechesTranscript.summary,
                SpeechesTranscript.publication_text,
                SpeechesTranscript.speech_text,
            )
            .where(SpeechesTranscript.parliamentarian_id == parliamentarian_id)
            .order_by(
                SpeechesTranscript.date.desc().nullslast(),
                SpeechesTranscript.id.desc(),
            )
            .limit(MAX_DISCURSOS)
        )
        .all()
    )

    # Degrau 1 — sumários.
    sumarios = [s for s, _, _ in discursos if s and s.strip()]
    if sumarios:
        origens.append("summary")
        for sumario in sumarios:
            if not _cabe(sumario.strip()):
                break

    # Degrau 2 — palavras-chave (Câmara via publication_text, Senado via tabela).
    if tamanho < MAX_CARACTERES_ENTRADA:
        termos: list[str] = []
        for _, publication_text, _ in discursos:
            termos.extend(_limpar_termos(publication_text, stopwords))
        termos.extend(_keywords_do_senado(session, parliamentarian_id, stopwords))
        if termos:
            origens.append("keywords")
            _cabe("Palavras-chave: " + ", ".join(dict.fromkeys(termos)))

    # Degrau 3 — íntegra, só se ainda faltar material.
    if tamanho < MAX_CARACTERES_ENTRADA:
        integras = [t for _, _, t in discursos if t and t.strip()]
        if integras:
            origens.append("speech_text")
            for integra in integras:
                if not _cabe(integra.strip()[: MAX_CARACTERES_ENTRADA - tamanho]):
                    break

    # Degrau 4 — quem não discursa, mas legisla.
    if not partes:
        ementas = (
            session.execute(
                select(Proposition.proposition_description)
                .join(
                    AuthorsProposition,
                    AuthorsProposition.proposition_id == Proposition.id,
                )
                .where(
                    AuthorsProposition.parliamentarian_id == parliamentarian_id,
                    Proposition.proposition_description.isnot(None),
                )
                .order_by(Proposition.presentation_date.desc().nullslast())
                .limit(MAX_EMENTAS)
            )
            .scalars()
            .all()
        )
        ementas = [e.strip() for e in ementas if e and e.strip()]
        if ementas:
            origens.append("ementas")
            for ementa in ementas:
                if not _cabe(ementa):
                    break

    texto = "\n\n".join(partes)[:MAX_CARACTERES_ENTRADA]
    return texto, "+".join(origens)


def _keywords_do_senado(
    session: Session, parliamentarian_id: int, stopwords: set[str]
) -> list[str]:
    """Keywords primárias do Senado, por rank.

    Tabela só do Senado — por isso é complemento do degrau 2, e nunca a fonte
    primária. Ausente ou vazia, devolve lista vazia sem derrubar o job.
    """

    try:
        linhas = session.execute(
            text(
                """
                SELECT k.term
                FROM speeches_transcripts_keywords k
                JOIN speeches_transcripts s ON s.id = k.speeches_transcripts_id
                WHERE s.parliamentarian_id = :pid AND k.is_primary
                ORDER BY k.rank
                LIMIT 200
                """
            ),
            {"pid": parliamentarian_id},
        ).scalars()
        return _limpar_termos(",".join(str(t) for t in linhas), stopwords)
    except SQLAlchemyError:
        session.rollback()
        return []

def montar_prompt(vocabulario: Sequence[Pauta], texto: str) -> tuple[str, str]:

    system_prompt = (
        "Você classifica parlamentares brasileiros nas pautas editoriais de uma "
        "redação de política. Responda apenas com JSON válido."
    )

    catalogo = "\n".join(
        f"- {p.slug} ({p.name}): {p.description or 'sem descrição'}"
        for p in vocabulario
    )
    slugs = ", ".join(p.slug for p in vocabulario)

    user_prompt = (
        "Vocabulário fechado de pautas (slug, nome e critério de fronteira):\n"
        f"{catalogo}\n\n"
        "Material do parlamentar (discursos e/ou ementas de autoria):\n"
        f"{texto}\n\n"
        "Retorne um JSON no formato:\n"
        '{"agendas": [{"slug": "<slug>", "confidence": <0.0 a 1.0>, '
        '"evidence": "<trecho curto que sustenta a escolha>"}]}\n\n'
        "Regras:\n"
        f"- Use SOMENTE estes slugs: {slugs}.\n"
        f"- No máximo {MAX_PAUTAS_POR_PARLAMENTAR} pautas, da mais dominante "
        "para a menos dominante.\n"
        "- Se o material não sustentar nenhuma pauta com clareza, devolva uma "
        'lista vazia: {"agendas": []}. Não escolha uma pauta genérica só para '
        "preencher.\n"
        "- `evidence` deve ser um trecho curto do próprio material, não uma "
        "paráfrase sua.\n"
        "- Não invente slug novo e não devolva comentários.\n"
    )
    return system_prompt, user_prompt


def construir_cliente(
    api_key: Optional[str] = None, base_url: Optional[str] = None
) -> Any:

    if OpenAI is None:
        raise RuntimeError(
            "Dependência 'openai' não instalada. Execute 'pip install openai'."
        )

    resolved_key = api_key or os.getenv("OPENAI_API_KEY")
    if not resolved_key:
        raise RuntimeError("OPENAI_API_KEY não configurada.")

    resolved_base_url = base_url or os.getenv("OPENAI_BASE_URL")
    if resolved_base_url:
        return OpenAI(api_key=resolved_key, base_url=resolved_base_url)
    return OpenAI(api_key=resolved_key)


def chamar_modelo(
    client: Any, system_prompt: str, user_prompt: str, *, model: str
) -> str:
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )
    if not completion.choices:
        return ""
    return completion.choices[0].message.content or ""


def interpretar_resposta(
    bruto: str, vocabulario: Sequence[Pauta]
) -> list[Classificacao]:
    if not bruto or not bruto.strip():
        return []

    conteudo = bruto.strip()
    # Modelos costumam embrulhar em bloco de código mesmo quando proibidos.
    if conteudo.startswith("```"):
        conteudo = conteudo.split("```")[1]
        if conteudo.lstrip().lower().startswith("json"):
            conteudo = conteudo.lstrip()[4:]

    try:
        payload = json.loads(conteudo)
    except json.JSONDecodeError:
        logger.warning("Resposta do modelo não é JSON válido; descartada.")
        return []

    if isinstance(payload, list):
        itens = payload
    elif isinstance(payload, dict):
        itens = payload.get("agendas") or []
    else:
        return []

    por_slug = {p.slug: p for p in vocabulario}
    classificacoes: list[Classificacao] = []
    ja_vistos: set[str] = set()

    for item in itens:
        if not isinstance(item, dict):
            continue
        slug = " ".join(str(item.get("slug") or "").split()).strip().lower()

        pauta = por_slug.get(slug)
        if pauta is None:
            # Fora do vocabulário: descarta, nunca cria.
            if slug:
                logger.info("Slug fora do vocabulário descartado: %s", slug)
            continue
        if slug in ja_vistos:
            continue
        ja_vistos.add(slug)

        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None:
            confidence = max(0.0, min(1.0, confidence))

        evidence = item.get("evidence")
        evidence = str(evidence).strip() if evidence else None

        classificacoes.append(
            Classificacao(
                agenda_id=pauta.id,
                slug=slug,
                rank=len(classificacoes) + 1,
                confidence=confidence,
                evidence=evidence or None,
            )
        )
        if len(classificacoes) >= MAX_PAUTAS_POR_PARLAMENTAR:
            break

    return classificacoes

def gravar(
    session: Session,
    parliamentarian_id: int,
    classificacoes: Sequence[Classificacao],
    *,
    model: str,
    vocabulary_version: int,
) -> None:
    session.execute(
        ParliamentarianAgenda.__table__.delete().where(
            ParliamentarianAgenda.parliamentarian_id == parliamentarian_id
        )
    )
    for c in classificacoes:
        session.add(
            ParliamentarianAgenda(
                parliamentarian_id=parliamentarian_id,
                agenda_id=c.agenda_id,
                rank=c.rank,
                confidence=c.confidence,
                evidence=c.evidence,
                model=model,
                vocabulary_version=vocabulary_version,
            )
        )
    session.flush()


def registrar_rodada(
    session: Session,
    parliamentarian_id: int,
    *,
    outcome: str,
    source: Optional[str],
    model: Optional[str],
    vocabulary_version: int,
) -> None:

    atual = session.get(ParliamentarianAgendaRun, parliamentarian_id)
    if atual is None:
        session.add(
            ParliamentarianAgendaRun(
                parliamentarian_id=parliamentarian_id,
                outcome=outcome,
                source=source,
                model=model,
                vocabulary_version=vocabulary_version,
            )
        )
    else:
        atual.outcome = outcome
        atual.source = source
        atual.model = model
        atual.vocabulary_version = vocabulary_version
        atual.computed_at = func.now()
    session.flush()


def _ultima_rodada(
    session: Session, parliamentarian_id: int
) -> tuple[Optional[Any], int]:
    linha = session.execute(
        select(
            ParliamentarianAgendaRun.computed_at,
            ParliamentarianAgendaRun.vocabulary_version,
        ).where(ParliamentarianAgendaRun.parliamentarian_id == parliamentarian_id)
    ).first()
    if linha is not None and linha[0] is not None:
        return linha[0], int(linha[1] or 1)

    linhas = session.execute(
        select(
            ParliamentarianAgenda.vocabulary_version,
            ParliamentarianAgenda.computed_at,
        ).where(ParliamentarianAgenda.parliamentarian_id == parliamentarian_id)
    ).all()
    datas = [c for _, c in linhas if c is not None]
    if not datas:
        return None, 0
    return max(datas), min(int(v or 1) for v, _ in linhas)


def _material_mudou_desde(
    session: Session, parliamentarian_id: int, quando: Any
) -> bool:

    discursos = session.execute(
        select(func.count(SpeechesTranscript.id)).where(
            SpeechesTranscript.parliamentarian_id == parliamentarian_id,
            or_(
                SpeechesTranscript.created_at > quando,
                SpeechesTranscript.updated_at > quando,
            ),
        )
    ).scalar_one()
    if discursos:
        return True

    autorias = session.execute(
        select(func.count(AuthorsProposition.id)).where(
            AuthorsProposition.parliamentarian_id == parliamentarian_id,
            or_(
                AuthorsProposition.created_at > quando,
                AuthorsProposition.updated_at > quando,
            ),
        )
    ).scalar_one()
    return bool(autorias)


def precisa_reclassificar(
    session: Session, parliamentarian_id: int, *, vocabulary_version: int
) -> bool:
    """Reclassifica quando QUALQUER uma valer.

    O ponto disto e o resultado esperado da CS-72: material novo entra, a
    proxima rodada refaz so quem mudou, sem ninguem rodar nada na mao.
    """

    computed_at, versao = _ultima_rodada(session, parliamentarian_id)

    # 1. Nunca foi olhado.
    if computed_at is None:
        return True

    # 2. O vocabulario mudou desde a ultima rodada.
    if versao != vocabulary_version:
        return True

    # 3. Chegou material novo — ou material velho foi completado.
    return _material_mudou_desde(session, parliamentarian_id, computed_at)


def parlamentares_alvo(
    session: Session, *, parliamentarian_id: Optional[int], limit: Optional[int]
) -> list[int]:
    stmt = select(Parliamentarian.id).order_by(Parliamentarian.id)
    if parliamentarian_id is not None:
        stmt = stmt.where(Parliamentarian.id == parliamentarian_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [int(i) for i in session.execute(stmt).scalars().all()]


# --------------------------------------------------------------------------
# Orquestração
# --------------------------------------------------------------------------


def classificar(
    *,
    parliamentarian_id: Optional[int] = None,
    limit: Optional[int] = None,
    force: bool = False,
    batch_size: int = 20,
    model: Optional[str] = None,
    client: Any = None,
    session: Optional[Session] = None,
) -> dict[str, int]:
    """Roda o job. `client` e `session` injetáveis para o teste não usar rede."""

    if session is not None:
        return _classificar_na_sessao(
            session,
            parliamentarian_id=parliamentarian_id,
            limit=limit,
            force=force,
            batch_size=batch_size,
            model=model,
            client=client,
        )

    with session_scope() as sessao:
        return _classificar_na_sessao(
            sessao,
            parliamentarian_id=parliamentarian_id,
            limit=limit,
            force=force,
            batch_size=batch_size,
            model=model,
            client=client,
        )


def _classificar_na_sessao(
    session: Session,
    *,
    parliamentarian_id: Optional[int],
    limit: Optional[int],
    force: bool,
    batch_size: int,
    model: Optional[str],
    client: Any,
) -> dict[str, int]:
    resolved_model = model or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL

    vocabulario = carregar_vocabulario(session)
    if not vocabulario:
        logger.warning("Vocabulário de pautas vazio; nada a classificar.")
        return {"considerados": 0, "classificados": 0, "sem_pauta": 0, "pulados": 0}

    versao = vocabulary_version_atual(session)
    stopwords = carregar_stopwords(session)
    alvos = parlamentares_alvo(
        session, parliamentarian_id=parliamentarian_id, limit=limit
    )

    contadores = {
        "considerados": len(alvos),
        "classificados": 0,
        "sem_pauta": 0,
        "pulados": 0,
        "sem_material": 0,
        "erros": 0,
    }

    for indice, pid in enumerate(alvos, start=1):
        if not force and not precisa_reclassificar(
            session, pid, vocabulary_version=versao
        ):
            contadores["pulados"] += 1
            continue

        texto, origem = montar_texto(session, pid, stopwords)
        if not texto.strip():
            # Sem discurso e sem autoria: fica sem pauta, e a tela dirá que a
            # análise está pendente.
            logger.info("Parlamentar %s sem material de entrada; sem pauta.", pid)
            # Registra mesmo sem chamar o modelo: e o que evita remontar o
            # texto desta pessoa em toda rodada so para concluir o mesmo.
            registrar_rodada(
                session,
                pid,
                outcome=RUN_SEM_MATERIAL,
                source=None,
                model=None,
                vocabulary_version=versao,
            )
            contadores["sem_material"] += 1
            continue

        if client is None:
            client = construir_cliente()

        system_prompt, user_prompt = montar_prompt(vocabulario, texto)
        try:
            bruto = chamar_modelo(
                client, system_prompt, user_prompt, model=resolved_model
            )
        except Exception:  # pragma: no cover - falha de rede/provedor
            # De proposito NAO registra rodada: erro de provedor nao e
            # "analisei e nada encaixou". Sem registro, a proxima rodada tenta
            # de novo, que e o comportamento certo para falha transitoria.
            logger.exception("Falha ao classificar o parlamentar %s.", pid)
            contadores["erros"] += 1
            continue

        classificacoes = interpretar_resposta(bruto, vocabulario)
        gravar(
            session,
            pid,
            classificacoes,
            model=resolved_model,
            vocabulary_version=versao,
        )
        registrar_rodada(
            session,
            pid,
            outcome=RUN_CLASSIFICADO if classificacoes else RUN_SEM_PAUTA,
            source=origem or None,
            model=resolved_model,
            vocabulary_version=versao,
        )

        if classificacoes:
            contadores["classificados"] += 1
            logger.info(
                "Parlamentar %s classificado (%s) a partir de %s.",
                pid,
                ", ".join(c.slug for c in classificacoes),
                origem or "nenhuma fonte",
            )
        else:
            contadores["sem_pauta"] += 1
            logger.info(
                "Parlamentar %s ficou sem pauta: nenhum slug válido na resposta.",
                pid,
            )

        if batch_size and indice % batch_size == 0:
            session.commit()

    session.commit()
    logger.info(
        "Pautas editoriais: %s considerados, %s classificados, %s sem pauta, "
        "%s sem material, %s pulados, %s erros.",
        contadores["considerados"],
        contadores["classificados"],
        contadores["sem_pauta"],
        contadores["sem_material"],
        contadores["pulados"],
        contadores["erros"],
    )
    return contadores


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Classifica parlamentares em 1 a 3 pautas editoriais do vocabulário "
            "fechado (CS-72)."
        )
    )
    parser.add_argument(
        "--parliamentarian",
        type=int,
        dest="parliamentarian_id",
        help="Classifica apenas este parlamentar (id interno).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limita quantos parlamentares são considerados na rodada.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reclassifica mesmo quem já está em dia.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Parlamentares processados antes de cada commit (padrão: 20).",
    )
    parser.add_argument(
        "--model",
        help=f"Modelo no OpenRouter (padrão: OPENAI_MODEL ou {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=LOG_LEVEL_CHOICES,
        help="Define o nível de log geral (padrão: INFO).",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level))
    classificar(
        parliamentarian_id=args.parliamentarian_id,
        limit=args.limit,
        force=args.force,
        batch_size=args.batch_size,
        model=args.model,
    )


if __name__ == "__main__":
    main()
