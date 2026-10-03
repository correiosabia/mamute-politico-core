"""Acompanhamento de candidaturas (projetos_candidacy) + métricas novas.

O vínculo é só o registro da escolha do assinante — nenhuma feature consome o
dado ainda. Regras cobertas: dono só enxerga o próprio vínculo, duplicata é
409 amigável, candidatura inexistente é 404, desmarcar apaga a linha, e a cota
`qtd_candidatos` do plano (seed = 10) barra o excedente com mensagem em
português. As métricas admin agregam sem nunca expor quem marcou o quê.
"""

from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.routers import projects
from api.services.admin_metrics import (
    metrics_candidacy_favorites,
    metrics_mamutometro,
)


def _make_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            create table projetos (
                id integer primary key, nome text not null, cliente text,
                email text not null, tier_id integer, tag_ghost text,
                qtd_termos integer not null default 0,
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp,
                deleted_at datetime
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table parliamentarian (
                id integer primary key, type text, parliamentarian_code integer,
                name text, full_name text, status text, party text,
                state_elected text, details text,
                created_at datetime not null, updated_at datetime not null
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table candidacy (
                id integer primary key, election_year integer not null,
                tse_candidate_id integer not null, office_code integer,
                office text, state text, ballot_number integer,
                ballot_name text, full_name text, party text, coalition text,
                status text, totalization_status text, cpf text, voter_id text,
                photo_url text, tse_last_update datetime,
                birth_date date, gender text, race text, education text,
                occupation text, marital_status text, nationality text,
                federation text, profile_source text,
                listing_fingerprint text, parliamentarian_id integer,
                match_status text not null default 'unmatched', details text,
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table projetos_candidacy (
                id integer primary key, projeto_id integer not null,
                candidacy_id integer not null,
                created_at datetime not null default current_timestamp,
                unique (projeto_id, candidacy_id)
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table candidacy_result (
                id integer primary key, candidacy_id integer not null,
                turno integer not null, codigo_eleicao integer not null,
                situacao text, eleito boolean, votos integer, percentual numeric,
                destinacao_voto text, totalizacao_final boolean not null default 0,
                tse_atualizado_em datetime, coletado_em datetime,
                unique (candidacy_id, turno)
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table election_result_notice (
                id integer primary key, projeto_id integer not null,
                ciclo text not null, turno integer not null,
                disparo text not null default 'completo', payload json not null,
                email_status text not null default 'pending',
                tentativas integer not null default 0, ultimo_erro text,
                created_at datetime, sent_at datetime, seen_at datetime,
                unique (projeto_id, ciclo, turno, disparo)
            )
            """
        )
        conn.exec_driver_sql(
            """
            create table project_mamutometro (
                id integer primary key, projeto_id integer not null,
                parliamentarian_id integer not null, level integer not null,
                created_at datetime not null default current_timestamp,
                updated_at datetime not null default current_timestamp,
                unique (projeto_id, parliamentarian_id)
            )
            """
        )
        for projeto_id, email in (
            (10, "assinante@example.com"),
            (20, "outro@example.com"),
        ):
            conn.execute(
                text(
                    """
                    insert into projetos (id, nome, email, qtd_termos)
                    values (:id, :nome, :email, 10)
                    """
                ),
                {"id": projeto_id, "nome": f"Projeto {projeto_id}", "email": email},
            )
        # 15 candidaturas de cargos/UFs variados para lista, cota e métricas.
        for cid in range(1, 16):
            conn.execute(
                text(
                    """
                    insert into candidacy
                        (id, election_year, tse_candidate_id, office_code,
                         office, state, ballot_name, party, match_status)
                    values (:id, 2026, :tse, :cargo, :office, :uf,
                            :nome, 'XPTO', 'unmatched')
                    """
                ),
                {
                    "id": cid,
                    "tse": 1000 + cid,
                    "cargo": 5 if cid % 2 else 6,
                    "office": "Senador" if cid % 2 else "Deputado Federal",
                    "uf": "CE" if cid <= 8 else "SP",
                    "nome": f"CANDIDATO {cid}",
                },
            )
    return Session(engine)


def _client(db: Session, *, token_email: str = "assinante@example.com") -> TestClient:
    app = main.create_app()

    def fake_verify_token(request: Request) -> dict[str, str]:
        request.state.token_email = token_email
        return {"sub": token_email}

    def fake_get_db():
        yield db

    app.dependency_overrides[main.verify_token] = fake_verify_token
    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[projects.get_db] = fake_get_db
    return TestClient(app)


@pytest.fixture()
def db() -> Session:
    return _make_session()


def _acompanhar(client: TestClient, candidacy_id: int):
    return client.post(
        "/api/projects/me/candidacy-favorites", json={"candidacy_id": candidacy_id}
    )


def test_registra_lista_e_remove(db: Session) -> None:
    client = _client(db)

    criado = _acompanhar(client, 1)
    assert criado.status_code == 201
    assert criado.json()["candidacy_id"] == 1

    listagem = client.get("/api/projects/me/candidacy-favorites").json()
    assert [f["candidacy_id"] for f in listagem] == [1]

    removido = client.delete("/api/projects/me/candidacy-favorites/1")
    assert removido.status_code == 204
    assert client.get("/api/projects/me/candidacy-favorites").json() == []


def test_duplicata_e_409_amigavel(db: Session) -> None:
    client = _client(db)
    assert _acompanhar(client, 1).status_code == 201
    duplicata = _acompanhar(client, 1)
    assert duplicata.status_code == 409
    assert "acompanha" in duplicata.json()["detail"]


def test_candidatura_inexistente_e_404(db: Session) -> None:
    client = _client(db)
    assert _acompanhar(client, 999).status_code == 404


def test_remover_o_que_nao_acompanha_e_404(db: Session) -> None:
    client = _client(db)
    assert client.delete("/api/projects/me/candidacy-favorites/1").status_code == 404


def test_lista_e_escopada_por_assinante(db: Session) -> None:
    dono = _client(db)
    outro = _client(db, token_email="outro@example.com")
    _acompanhar(dono, 1)
    _acompanhar(outro, 2)
    assert [f["candidacy_id"] for f in dono.get("/api/projects/me/candidacy-favorites").json()] == [1]
    assert [f["candidacy_id"] for f in outro.get("/api/projects/me/candidacy-favorites").json()] == [2]


def test_cota_do_plano_barra_o_decimo_primeiro(db: Session) -> None:
    """Projeto sem tier cai no default do seed (10)."""
    client = _client(db)
    for cid in range(1, 11):
        assert _acompanhar(client, cid).status_code == 201
    barrado = _acompanhar(client, 11)
    assert barrado.status_code == 403
    assert "10/10" in barrado.json()["detail"]


def _resultado(db: Session, candidacy_id: int, *, eleito: bool, situacao: str, final: bool = True) -> None:
    db.execute(
        text(
            "insert into candidacy_result (candidacy_id, turno, codigo_eleicao, situacao, "
            "eleito, totalizacao_final) values (:c, 1, 6259, :s, :e, :f)"
        ),
        {"c": candidacy_id, "s": situacao, "e": eleito, "f": final},
    )
    db.commit()


def test_cs108_nao_eleito_libera_vaga_na_cota(db: Session) -> None:
    """Candidato que saiu da disputa continua acompanhado, mas nao ocupa vaga."""
    client = _client(db)
    for cid in range(1, 11):
        assert _acompanhar(client, cid).status_code == 201
    assert _acompanhar(client, 11).status_code == 403

    _resultado(db, 1, eleito=False, situacao="Não eleito")
    _resultado(db, 2, eleito=True, situacao="2º turno")  # segue na disputa: conta
    assert _acompanhar(client, 11).status_code == 201
    assert _acompanhar(client, 12).status_code == 403
    # o nao eleito continua na lista de acompanhados
    ids = [f["candidacy_id"] for f in client.get("/api/projects/me/candidacy-favorites").json()]
    assert 1 in ids and 11 in ids


def test_cs108_resultado_parcial_nao_libera_vaga(db: Session) -> None:
    client = _client(db)
    for cid in range(1, 11):
        _acompanhar(client, cid)
    _resultado(db, 1, eleito=False, situacao="Não eleito", final=False)
    assert _acompanhar(client, 11).status_code == 403


def _aviso(
    db: Session, projeto_id: int, *, turno: int = 1, status: str = "sent", disparo: str = "completo"
) -> int:
    db.execute(
        text(
            "insert into election_result_notice (projeto_id, ciclo, turno, disparo, payload, email_status) "
            "values (:p, 'ele2026', :t, :d, :payload, :s)"
        ),
        {
            "p": projeto_id,
            "t": turno,
            "d": disparo,
            "payload": '{"turno": %d, "itens": [{"nome": "FULANO", "situacao": "Eleito"}]}' % turno,
            "s": status,
        },
    )
    db.commit()
    return db.execute(text("select max(id) from election_result_notice")).scalar_one()


def test_cs106_sem_aviso_devolve_204(db: Session) -> None:
    client = _client(db)
    assert client.get("/api/projects/me/election-result-notice").status_code == 204


def test_cs106_aviso_pendente_de_envio_nao_aparece(db: Session) -> None:
    _aviso(db, 10, status="pending")
    client = _client(db)
    assert client.get("/api/projects/me/election-result-notice").status_code == 204


def test_cs106_aviso_aparece_ate_ser_visto(db: Session) -> None:
    aviso_id = _aviso(db, 10)
    _aviso(db, 20)
    client = _client(db)
    resposta = client.get("/api/projects/me/election-result-notice")
    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["id"] == aviso_id and corpo["turno"] == 1
    assert corpo["payload"]["itens"][0]["situacao"] == "Eleito"

    assert client.post(f"/api/projects/me/election-result-notice/{aviso_id}/seen").status_code == 204
    assert client.get("/api/projects/me/election-result-notice").status_code == 204
    # marcar de novo e idempotente
    assert client.post(f"/api/projects/me/election-result-notice/{aviso_id}/seen").status_code == 204


def test_cs106_segundo_turno_vem_antes(db: Session) -> None:
    _aviso(db, 10, turno=1)
    segundo = _aviso(db, 10, turno=2)
    client = _client(db)
    assert client.get("/api/projects/me/election-result-notice").json()["id"] == segundo


def test_cs119_ver_o_completo_marca_o_de_majoritarios_como_visto(db: Session) -> None:
    """Fechar o modal do resumo completo nao pode trazer de volta o parcial."""
    _aviso(db, 10, disparo="majoritarios")
    completo = _aviso(db, 10, disparo="completo")
    alheio = _aviso(db, 20, disparo="majoritarios")
    client = _client(db)
    assert client.get("/api/projects/me/election-result-notice").json()["id"] == completo
    assert client.post(f"/api/projects/me/election-result-notice/{completo}/seen").status_code == 204
    assert client.get("/api/projects/me/election-result-notice").status_code == 204
    outro = _client(db, token_email="outro@example.com")
    assert outro.get("/api/projects/me/election-result-notice").json()["id"] == alheio


def test_cs106_nao_marca_aviso_de_outra_pessoa(db: Session) -> None:
    alheio = _aviso(db, 20)
    client = _client(db)
    assert client.post(f"/api/projects/me/election-result-notice/{alheio}/seen").status_code == 404
    outro = _client(db, token_email="outro@example.com")
    assert outro.get("/api/projects/me/election-result-notice").json()["id"] == alheio


def test_metrics_candidacy_favorites_agrega_por_cargo_e_uf(db: Session) -> None:
    client = _client(db)
    outro = _client(db, token_email="outro@example.com")
    _acompanhar(client, 1)   # Senador CE
    _acompanhar(client, 2)   # Dep. Federal CE
    _acompanhar(outro, 1)    # Senador CE — 2a pessoa no mesmo candidato

    data = metrics_candidacy_favorites(db)
    assert data["totals"] == {"links": 3, "candidacies": 2, "users": 2}
    assert data["top"][0]["candidacy_id"] == 1
    assert data["top"][0]["monitors"] == 2
    assert data["by_office"][0] == {"office": "Senador", "monitors": 2}
    assert data["by_state"][0] == {"state": "CE", "monitors": 3}


def test_metrics_mamutometro_tres_leituras(db: Session) -> None:
    """3 mamutes de um assinante + 2 de outro = 2 pessoas, 5 no total, média 2.5."""
    db.execute(
        text(
            """
            insert into parliamentarian (id, type, name, state_elected, created_at, updated_at)
            values (7, 'Deputado', 'Fulano', 'CE', '2026-01-01', '2026-01-01')
            """
        )
    )
    db.execute(
        text(
            """
            insert into project_mamutometro (projeto_id, parliamentarian_id, level)
            values (10, 7, 3), (20, 7, 2)
            """
        )
    )
    db.commit()

    data = metrics_mamutometro(db)
    assert data["totals"] == {"parliamentarians": 1, "marks": 2, "mamutinhos": 5}
    linha = data["top"][0]
    assert linha["people"] == 2
    assert linha["total"] == 5
    assert linha["average"] == 2.5
