"""Resultado da busca de um parlamentar no TSE para a timeline — CS-95.

Parlamentar sem nenhuma linha em electoral_history pode estar em duas
situacoes que a tela precisa separar: a coleta ainda nao o procurou, ou
procurou nas listagens das gerais e nao o encontrou. A fase 2 do
tse_crawler.electoral_history grava aqui o resultado de cada varredura
completa (`encontrado`, `nao_encontrado` ou `cpf_divergente`, constantes
SEARCH_* do crawler); sem linha aqui = ainda nao buscado.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Text

from ..base import Base


class ElectoralHistorySearch(Base):
    __tablename__ = "electoral_history_search"

    parliamentarian_id = Column(
        BigInteger,
        ForeignKey("parliamentarian.id", ondelete="CASCADE"),
        primary_key=True,
    )
    outcome = Column(Text, nullable=False)
    searched_at = Column(DateTime, nullable=False)


__all__ = ["ElectoralHistorySearch"]
