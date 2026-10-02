"""Schema SQLite minimo das tabelas do CS-106, compartilhado pelos testes de
coleta e de envio. Espelha a migration cs106a1b2c3d4 so no que os testes usam."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

DDL = [
    """CREATE TABLE tiers (id INTEGER PRIMARY KEY, nome TEXT)""",
    """CREATE TABLE projetos (
        id INTEGER PRIMARY KEY, email TEXT, nome TEXT, tier_id INTEGER,
        deleted_at TIMESTAMP)""",
    """CREATE TABLE candidacy (
        id INTEGER PRIMARY KEY, election_year INTEGER NOT NULL,
        tse_candidate_id BIGINT NOT NULL, office_code INTEGER, office TEXT,
        state TEXT, ballot_number INTEGER, ballot_name TEXT, full_name TEXT,
        party TEXT)""",
    """CREATE TABLE projetos_candidacy (
        id INTEGER PRIMARY KEY, projeto_id INTEGER NOT NULL,
        candidacy_id INTEGER NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (projeto_id, candidacy_id))""",
    """CREATE TABLE feature_flag (key TEXT PRIMARY KEY, state TEXT NOT NULL)""",
    """CREATE TABLE feature_flag_tier (
        flag_key TEXT NOT NULL, tier_id INTEGER NOT NULL, mode TEXT NOT NULL,
        PRIMARY KEY (flag_key, tier_id))""",
    """CREATE TABLE tse_result_file (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ciclo TEXT NOT NULL,
        codigo_eleicao INTEGER NOT NULL, turno SMALLINT NOT NULL, uf TEXT NOT NULL,
        cargo_codigo INTEGER NOT NULL, totalizacao_final BOOLEAN NOT NULL DEFAULT 0,
        tse_atualizado_em TIMESTAMP, candidatos_no_arquivo INTEGER NOT NULL DEFAULT 0,
        candidatos_casados INTEGER NOT NULL DEFAULT 0,
        coletado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (codigo_eleicao, uf, cargo_codigo))""",
    """CREATE TABLE candidacy_result (
        id INTEGER PRIMARY KEY AUTOINCREMENT, candidacy_id INTEGER NOT NULL,
        turno SMALLINT NOT NULL, codigo_eleicao INTEGER NOT NULL, situacao TEXT,
        eleito BOOLEAN, votos BIGINT, percentual NUMERIC, destinacao_voto TEXT,
        totalizacao_final BOOLEAN NOT NULL DEFAULT 0, tse_atualizado_em TIMESTAMP,
        coletado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (candidacy_id, turno))""",
    """CREATE TABLE election_result_notice (
        id INTEGER PRIMARY KEY AUTOINCREMENT, projeto_id INTEGER NOT NULL,
        ciclo TEXT NOT NULL, turno SMALLINT NOT NULL, payload JSON NOT NULL,
        email_status TEXT NOT NULL DEFAULT 'pending',
        tentativas SMALLINT NOT NULL DEFAULT 0, ultimo_erro TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, sent_at TIMESTAMP,
        seen_at TIMESTAMP, UNIQUE (projeto_id, ciclo, turno))""",
    """CREATE TABLE email_send_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, projeto_id BIGINT NOT NULL,
        email TEXT NOT NULL, periodicidade TEXT NOT NULL, status TEXT NOT NULL,
        detail TEXT, subject TEXT, stats JSON, period_start DATE, period_end DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
]


def make_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        for ddl in DDL:
            conn.exec_driver_sql(ddl)
    return Session(engine)


def add_candidacy(
    session: Session,
    cid: int,
    sqcand: int,
    *,
    office_code: int = 6,
    state: str = "SP",
    name: str | None = None,
    party: str = "PX",
    year: int = 2026,
) -> None:
    session.execute(
        text(
            "INSERT INTO candidacy (id, election_year, tse_candidate_id, office_code, "
            "office, state, ballot_number, ballot_name, party) "
            "VALUES (:id, :y, :sq, :oc, :office, :st, :num, :name, :party)"
        ),
        {
            "id": cid,
            "y": year,
            "sq": sqcand,
            "oc": office_code,
            "office": {1: "Presidente", 3: "Governador", 5: "Senador", 6: "Deputado Federal"}.get(office_code, "Cargo"),
            "st": state,
            "num": 1000 + cid,
            "name": name or f"CANDIDATO {cid}",
            "party": party,
        },
    )
    session.commit()


def follow(session: Session, projeto_id: int, candidacy_id: int) -> None:
    session.execute(
        text("INSERT INTO projetos_candidacy (projeto_id, candidacy_id) VALUES (:p, :c)"),
        {"p": projeto_id, "c": candidacy_id},
    )
    session.commit()


def add_projeto(session: Session, pid: int, email: str | None, tier_id: int | None = 1) -> None:
    session.execute(
        text("INSERT INTO projetos (id, email, nome, tier_id) VALUES (:id, :email, :nome, :tier)"),
        {"id": pid, "email": email, "nome": f"Pessoa {pid}", "tier": tier_id},
    )
    session.commit()


def liberar_no_plano(session: Session, tier_id: int = 1) -> None:
    session.execute(
        text(
            "INSERT INTO feature_flag_tier (flag_key, tier_id, mode) "
            "VALUES ('resultado_eleicao', :t, 'liberado')"
        ),
        {"t": tier_id},
    )
    session.commit()


def set_flag(session: Session, state: str) -> None:
    session.execute(
        text(
            "INSERT INTO feature_flag (key, state) VALUES ('resultado_eleicao', :s) "
            "ON CONFLICT (key) DO UPDATE SET state = excluded.state"
        ),
        {"s": state},
    )
    session.commit()
