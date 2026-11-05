from __future__ import annotations

from typing import Iterator

import pytest
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from mamute_scrappers.tse_crawler import electoral_history as eh_mod

# Espelho minimo em SQLite. `details` usa JSON generico (em producao e JSONB).
Base = declarative_base()


class Parliamentarian(Base):
    __tablename__ = "parliamentarian"
    id = Column(Integer, primary_key=True)
    name = Column(Text)
    full_name = Column(Text)
    cpf = Column(Text)
    state_elected = Column(Text)


class Candidacy(Base):
    __tablename__ = "candidacy"
    id = Column(Integer, primary_key=True)
    election_year = Column(Integer)
    tse_candidate_id = Column(Integer)
    parliamentarian_id = Column(Integer, ForeignKey("parliamentarian.id"))
    details = Column(JSON)


class ElectoralHistorySearch(Base):
    __tablename__ = "electoral_history_search"
    parliamentarian_id = Column(Integer, ForeignKey("parliamentarian.id"), primary_key=True)
    outcome = Column(Text, nullable=False)
    searched_at = Column(DateTime, nullable=False)


class ElectoralHistory(Base):
    __tablename__ = "electoral_history"
    __table_args__ = (UniqueConstraint("election_year", "tse_candidate_id"),)
    id = Column(Integer, primary_key=True)
    election_year = Column(Integer, nullable=False)
    tse_candidate_id = Column(Integer, nullable=False)
    tse_election_id = Column(Integer)
    parliamentarian_id = Column(Integer, ForeignKey("parliamentarian.id"))
    candidacy_id = Column(Integer, ForeignKey("candidacy.id"))
    office = Column(Text)
    state = Column(Text)
    locality = Column(Text)
    party = Column(Text)
    ballot_name = Column(Text)
    full_name = Column(Text)
    ballot_number = Column(Integer)
    result = Column(Text)
    declared_assets = Column(Numeric(18, 2))
    assets_count = Column(Integer)
    assets = Column(JSON)
    assets_fetched_at = Column(Text)
    assets_status = Column(Text)
    source_link = Column(Text)


def _details(tse_id: int) -> dict:
    return {
        "totalDeBens": 1000.0,
        "bens": [{"valor": 1000.0}],
        "eleicoesAnteriores": [
            {
                "id": str(tse_id),
                "nrAno": 2026,
                "idEleicao": "20322002026",
                "sgUe": "PR",
                "cargo": "Deputado Federal",
                "situacaoTotalizacao": "Concorrendo",
            },
            {
                "id": str(tse_id + 1_000_000),
                "nrAno": 2022,
                "idEleicao": "2040602022",
                "sgUe": "PR",
                "cargo": "Deputado Federal",
                "situacaoTotalizacao": "Eleito",
            },
        ],
    }


@pytest.fixture()
def session(monkeypatch) -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(eh_mod, "ElectoralHistory", ElectoralHistory)
    monkeypatch.setattr(eh_mod, "Candidacy", Candidacy)
    monkeypatch.setattr(eh_mod, "Parliamentarian", Parliamentarian)
    monkeypatch.setattr(eh_mod, "ElectoralHistorySearch", ElectoralHistorySearch)
    with maker() as s:
        yield s


def test_semeadura_atravessa_commits_intermediarios(session, monkeypatch):
    """Regressao da carga de 2026-08-09 em producao: a fase 1 iterava um
    cursor server-side (yield_per) e comitava no meio — o commit invalida o
    named cursor do Postgres e a execucao morria aos ~1.6k de ~25k registros.
    A semeadura deve paginar por keyset e processar TODAS as candidaturas
    mesmo com commits intermediarios."""
    # Lotes pequenos para forcar varias fronteiras de pagina e de commit.
    monkeypatch.setattr(eh_mod, "SEED_BATCH_SIZE", 7)
    monkeypatch.setattr(eh_mod, "COMMIT_EVERY", 5)

    total = 40
    for i in range(1, total + 1):
        session.add(
            Candidacy(
                id=i,
                election_year=2026,
                tse_candidate_id=i,
                parliamentarian_id=None,
                details=_details(i * 10),
            )
        )
    session.commit()

    counters = eh_mod.seed_from_candidacies(session)

    assert counters["candidacies"] == total
    # 2 entradas por candidatura, ids todos distintos.
    assert session.query(ElectoralHistory).count() == total * 2
    # A linha do ano corrente ja nasce com patrimonio copiado do details.
    com_bens = (
        session.query(ElectoralHistory)
        .filter(ElectoralHistory.assets_fetched_at.isnot(None))
        .count()
    )
    assert com_bens == total


def test_semeadura_e_idempotente(session):
    session.add(
        Candidacy(
            id=1,
            election_year=2026,
            tse_candidate_id=10,
            parliamentarian_id=None,
            details=_details(10),
        )
    )
    session.commit()

    eh_mod.seed_from_candidacies(session)
    eh_mod.seed_from_candidacies(session)

    assert session.query(ElectoralHistory).count() == 2
