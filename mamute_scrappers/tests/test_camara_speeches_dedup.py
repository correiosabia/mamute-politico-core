from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    ForeignKey,
    Integer,
    Text,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


camara_speeches = load_module(
    "test_camara_speeches_module",
    "mamute_scrappers/camara_crawler/speeches_transcripts.py",
)

Base = declarative_base()


class Parliamentarian(Base):
    __tablename__ = "parliamentarian"

    id = Column(Integer, primary_key=True)
    type = Column(Text)
    parliamentarian_code = Column(BigInteger)
    name = Column(Text)

    speeches = relationship("SpeechesTranscript", back_populates="parliamentarian")


class SpeechesTranscript(Base):
    __tablename__ = "speeches_transcripts"

    id = Column(Integer, primary_key=True)
    parliamentarian_id = Column(
        Integer, ForeignKey("parliamentarian.id"), nullable=False
    )
    date = Column(Date)
    session_number = Column(Text)
    type = Column(Text)
    speech_link = Column(Text)
    speech_text = Column(Text)
    summary = Column(Text)
    hour_minute = Column(Text)
    publication_link = Column(Text)
    publication_text = Column(Text)

    parliamentarian = relationship("Parliamentarian", back_populates="speeches")


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True)
    with SessionLocal() as db_session:
        db_session.add(
            Parliamentarian(
                id=1, type="Deputado", parliamentarian_code=123, name="Fulano"
            )
        )
        db_session.flush()
        yield db_session


@pytest.fixture(autouse=True)
def patch_models() -> Iterator[None]:
    original_parliamentarian = camara_speeches.Parliamentarian
    original_speech = camara_speeches.SpeechesTranscript

    camara_speeches.Parliamentarian = Parliamentarian
    camara_speeches.SpeechesTranscript = SpeechesTranscript
    yield
    camara_speeches.Parliamentarian = original_parliamentarian
    camara_speeches.SpeechesTranscript = original_speech


def _payload(**overrides):
    payload = {
        "parliamentarian_code": 123,
        "date": date(2026, 6, 17),
        "session_number": None,
        "type": "PELA ORDEM",
        "speech_link": None,
        "speech_text": "Senhor presidente, peço a palavra pela ordem.",
        "summary": None,
        "hour_minute": "15:12",
        "publication_link": None,
        "publication_text": None,
    }
    payload.update(overrides)
    return payload


def test_upsert_without_link_is_idempotent(session: Session) -> None:
    """Discursos sem speech_link (ex.: 'PELA ORDEM') não podem duplicar a cada run."""
    _, created_first = camara_speeches._upsert_speech(session, _payload())
    session.flush()
    _, created_second = camara_speeches._upsert_speech(session, _payload())
    session.flush()

    assert created_first is True
    assert created_second is False
    assert session.query(SpeechesTranscript).count() == 1


def test_distinct_speeches_without_link_are_kept_separate(session: Session) -> None:
    """Discursos diferentes (texto/hora distintos) no mesmo dia não podem colapsar."""
    camara_speeches._upsert_speech(session, _payload(speech_text="Primeiro discurso."))
    session.flush()
    camara_speeches._upsert_speech(
        session, _payload(speech_text="Segundo discurso, outro assunto.")
    )
    session.flush()
    camara_speeches._upsert_speech(
        session, _payload(hour_minute="16:40", speech_text="Primeiro discurso.")
    )
    session.flush()

    assert session.query(SpeechesTranscript).count() == 3


def test_upsert_without_link_and_null_text_is_idempotent(session: Session) -> None:
    """Mesmo sem texto (speech_text nulo), não pode duplicar a cada run."""
    _, created_first = camara_speeches._upsert_speech(
        session, _payload(speech_text=None)
    )
    session.flush()
    _, created_second = camara_speeches._upsert_speech(
        session, _payload(speech_text=None)
    )
    session.flush()

    assert created_first is True
    assert created_second is False
    assert session.query(SpeechesTranscript).count() == 1


def test_upsert_with_link_still_idempotent(session: Session) -> None:
    """Caminho com link preserva o comportamento existente (idempotente)."""
    link = "https://www.camara.leg.br/diario/123"
    _, created_first = camara_speeches._upsert_speech(
        session, _payload(speech_link=link)
    )
    session.flush()
    _, created_second = camara_speeches._upsert_speech(
        session, _payload(speech_link=link)
    )
    session.flush()

    assert created_first is True
    assert created_second is False
    assert session.query(SpeechesTranscript).count() == 1


def test_speech_text_changed_detects_only_real_text_changes(session: Session) -> None:
    """A análise textual só deve rodar de novo quando o texto do discurso muda."""
    record, _ = camara_speeches._upsert_speech(session, _payload())
    session.flush()

    record, _ = camara_speeches._upsert_speech(session, _payload())
    assert camara_speeches._speech_text_changed(record) is False
    session.flush()

    record, _ = camara_speeches._upsert_speech(
        session, _payload(speech_text="Senhor presidente, peço a palavra pela ordem.")
    )
    assert camara_speeches._speech_text_changed(record) is False

    record, _ = camara_speeches._upsert_speech(
        session, _payload(speech_text="Texto corrigido pela taquigrafia.")
    )
    assert camara_speeches._speech_text_changed(record) is True


def test_sync_analyzes_new_speeches_and_skips_unchanged(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discurso novo da Câmara precisa gerar palavras-chave (nuvem de temas)."""
    from contextlib import contextmanager

    @contextmanager
    def fake_scope():
        yield session

    payloads = [_payload(), _payload(hour_minute="16:00", speech_text="Outro tema.")]
    analyzed: list = []

    monkeypatch.setattr(camara_speeches, "_SESSION_SCOPE", fake_scope)
    monkeypatch.setattr(camara_speeches, "REQUEST_DELAY", 0)
    monkeypatch.setattr(
        camara_speeches, "_iter_speeches_paginated", lambda *a, **k: list(payloads)
    )
    monkeypatch.setattr(camara_speeches, "_build_speech_payload", lambda _c, p: p)
    monkeypatch.setattr(
        camara_speeches,
        "_analyze_speech_text",
        lambda _s, record: analyzed.append(record.speech_text),
    )

    camara_speeches.speeches_transcripts(deputado_id=123, data_inicio="2026-06-01")
    session.flush()
    assert analyzed == [payloads[0]["speech_text"], "Outro tema."]

    analyzed.clear()
    camara_speeches.speeches_transcripts(deputado_id=123, data_inicio="2026-06-01")
    assert analyzed == []
