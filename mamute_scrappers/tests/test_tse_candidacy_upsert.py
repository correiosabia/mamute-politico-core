from __future__ import annotations

from typing import Iterator

import pytest
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from mamute_scrappers.tse_crawler import candidacy as candidacy_mod

# Base local espelhando so as colunas usadas pelo upsert, para rodar em SQLite
# em memoria sem depender do modelo real (que exige DATABASE_URL no import).
# Mesma abordagem de test_emendas_upsert.py.
Base = declarative_base()


class Parliamentarian(Base):
    __tablename__ = "parliamentarian"
    id = Column(Integer, primary_key=True)


class Candidacy(Base):
    __tablename__ = "candidacy"
    __table_args__ = (UniqueConstraint("election_year", "tse_candidate_id"),)
    id = Column(Integer, primary_key=True)
    election_year = Column(Integer, nullable=False)
    tse_candidate_id = Column(Integer, nullable=False)
    office_code = Column(Integer)
    office = Column(Text)
    state = Column(Text)
    ballot_number = Column(Integer)
    ballot_name = Column(Text)
    full_name = Column(Text)
    party = Column(Text)
    coalition = Column(Text)
    status = Column(Text)
    totalization_status = Column(Text)
    cpf = Column(Text)
    voter_id = Column(Text)
    photo_url = Column(Text)
    tse_last_update = Column(Text)
    listing_fingerprint = Column(Text)
    parliamentarian_id = Column(Integer, ForeignKey("parliamentarian.id"))
    match_status = Column(Text, nullable=False)


@pytest.fixture()
def session(monkeypatch) -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(candidacy_mod, "Candidacy", Candidacy)
    with maker() as s:
        s.add(Parliamentarian(id=1))
        s.commit()
        yield s


def payload(**overrides):
    base = {
        "election_year": 2026,
        "tse_candidate_id": 10002536710,
        "office_code": 5,
        "office": "Senador",
        "state": "AC",
        "ballot_number": 277,
        "ballot_name": "DR. JUNIOR FEITOSA",
        "full_name": "RIBAMAR DE SOUSA FEITOZA JÚNIOR",
        "party": "DC",
        "coalition": "DC",
        "status": "Aguardando julgamento",
        "totalization_status": "Concorrendo",
        "cpf": "67146902234",
        "voter_id": "003576712402",
        "photo_url": "https://x/foto.jpg",
        "tse_last_update": None,
        "listing_fingerprint": "abc123",
        "parliamentarian_id": None,
        "match_status": "unmatched",
    }
    base.update(overrides)
    return base


def test_primeira_gravacao_cria(session):
    record, created = candidacy_mod.upsert_candidacy(session, payload())
    session.commit()
    assert created is True
    assert session.query(Candidacy).count() == 1
    assert record.listing_fingerprint == "abc123"


def test_upsert_e_idempotente(session):
    candidacy_mod.upsert_candidacy(session, payload())
    session.commit()
    _, created = candidacy_mod.upsert_candidacy(
        session, payload(status="Deferido", listing_fingerprint="def456")
    )
    session.commit()
    assert created is False
    record = session.query(Candidacy).one()
    assert record.status == "Deferido"
    assert record.listing_fingerprint == "def456"


def test_payload_sem_detalhe_nao_apaga_detalhe_anterior(session):
    candidacy_mod.upsert_candidacy(session, payload())
    session.commit()

    # Detalhe falhou nesta execucao: payload so tem campos de listagem.
    sem_detalhe = payload(status="Deferido")
    for campo in (
        "cpf",
        "voter_id",
        "photo_url",
        "tse_last_update",
        "listing_fingerprint",
    ):
        sem_detalhe.pop(campo)
    candidacy_mod.upsert_candidacy(session, sem_detalhe)
    session.commit()

    record = session.query(Candidacy).one()
    assert record.status == "Deferido"
    assert record.cpf == "67146902234"
    # O fingerprint anterior fica: ele ja nao casa com a listagem nova, e e
    # exatamente isso que forca o refetch do detalhe na proxima execucao.
    assert record.listing_fingerprint == "abc123"


def test_correcao_manual_nao_e_sobrescrita(session):
    candidacy_mod.upsert_candidacy(session, payload())
    session.commit()
    record = session.query(Candidacy).one()
    record.parliamentarian_id = 1
    record.match_status = "manual"
    session.commit()

    candidacy_mod.upsert_candidacy(
        session,
        payload(parliamentarian_id=None, match_status="unmatched", status="Deferido"),
    )
    session.commit()

    record = session.query(Candidacy).one()
    assert record.parliamentarian_id == 1
    assert record.match_status == "manual"
    assert record.status == "Deferido"


def test_repeticao_no_mesmo_lote_nao_duplica(session):
    candidacy_mod.upsert_candidacy(session, payload())
    # Sem commit no meio, como dentro do lote de COMMIT_EVERY.
    _, created = candidacy_mod.upsert_candidacy(session, payload(status="Deferido"))
    session.commit()
    assert created is False
    assert session.query(Candidacy).count() == 1


def test_coleta_nao_segura_transacao_durante_as_chamadas(session, monkeypatch):
    """CS-126: com commit só a cada 200, a transação ficava aberta enquanto o
    job esperava o TSE (até ~4 min por detalhe que falha)."""
    from contextlib import contextmanager

    import mamute_scrappers.db as db_mod

    @contextmanager
    def _escopo():
        yield session
        session.commit()

    monkeypatch.setattr(db_mod, "session_scope", _escopo)
    monkeypatch.setattr(candidacy_mod, "_load_env_file", lambda: None)
    monkeypatch.setattr(candidacy_mod, "_ensure_model", lambda: None)
    monkeypatch.setattr(candidacy_mod, "_load_parliamentarian_index", lambda: candidacy_mod.build_index([]))
    monkeypatch.setattr(candidacy_mod, "_load_known_fingerprints", lambda ano: {})
    monkeypatch.setattr(candidacy_mod, "OFFICES", ((6, "Deputado Federal", ("PR", "SP")),))

    em_transacao = []

    class _Cliente:
        def find_general_election_id(self, ano):
            return 20322002026

        def list_candidates(self, ano, uf, election_id, office_code):
            em_transacao.append(session.in_transaction())
            base = 1 if uf == "PR" else 100
            return [
                {"id": base + i, "nomeCompleto": f"PESSOA {base + i}", "nomeUrna": f"P{base + i}",
                 "numero": 1000 + i, "partido": {"sigla": "XX"}}
                for i in range(3)
            ]

        def get_candidate_detail(self, ano, uf, election_id, candidate_id):
            em_transacao.append(session.in_transaction())
            return None

    monkeypatch.setattr(candidacy_mod, "DivulgaCandClient", _Cliente)

    candidacy_mod.run(ano=2026)

    assert session.query(Candidacy).count() == 6
    assert len(em_transacao) == 8
    assert not any(em_transacao)
