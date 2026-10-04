"""Perfil dos eleitos de 2026 no admin (CS-107)."""
from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api import main
from api.dependencies import get_db
from api.security import require_ghost_admin, verify_token
from api.services.elected_profile import UFS, elected_profile

SENADOR, DEP_FEDERAL = 5, 6
_ids = itertools.count(1)


def _make_session(*, com_eleitos: bool = True) -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "create table candidacy (id integer primary key, election_year integer not null, "
            "tse_candidate_id bigint not null, office_code integer, state text, "
            "gender text, race text, education text, parliamentarian_id integer)"
        )
        conn.exec_driver_sql(
            "create table parliamentarian (id integer primary key, type text, status text)"
        )
        conn.exec_driver_sql(
            "create table candidacy_result (id integer primary key autoincrement, "
            "candidacy_id integer not null, turno smallint not null, codigo_eleicao integer not null, "
            "situacao text, eleito boolean, totalizacao_final boolean not null default 0, "
            "unique (candidacy_id, turno))"
        )
        conn.exec_driver_sql(
            "create table tse_result_file (id integer primary key autoincrement, ciclo text not null, "
            "codigo_eleicao integer not null, turno smallint not null, uf text not null, "
            "cargo_codigo integer not null, totalizacao_final boolean not null default 0, "
            "tse_atualizado_em timestamp, candidatos_no_arquivo integer not null default 0, "
            "candidatos_casados integer not null default 0"
            + (", eleitos_no_arquivo integer, percentual_apurado numeric" if com_eleitos else "")
            + ", unique (codigo_eleicao, uf, cargo_codigo))"
        )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _arquivo(
    db: Session,
    uf: str,
    cargo: int,
    *,
    final: bool = True,
    eleitos: int | None = None,
    apurado: float | None = None,
    atualizado: str = "2026-10-04 21:30:00",
    ciclo: str = "ele2026",
) -> None:
    colunas = "ciclo, codigo_eleicao, turno, uf, cargo_codigo, totalizacao_final, tse_atualizado_em"
    valores = ":ciclo, 6259, 1, :uf, :cargo, :final, :atualizado"
    if eleitos is not None:
        colunas += ", eleitos_no_arquivo"
        valores += ", :eleitos"
    if apurado is not None:
        colunas += ", percentual_apurado"
        valores += ", :apurado"
    db.execute(
        text(f"insert into tse_result_file ({colunas}) values ({valores})"),
        {"ciclo": ciclo, "uf": uf.lower(), "cargo": cargo, "final": final,
         "atualizado": atualizado, "eleitos": eleitos, "apurado": apurado},
    )
    db.commit()


def _candidato(
    db: Session,
    uf: str,
    cargo: int,
    *,
    genero: str | None = "MASCULINO",
    raca: str | None = "BRANCA",
    escolaridade: str | None = "SUPERIOR COMPLETO",
    parlamentar: tuple[str, str] | None = None,
    situacao: str = "Eleito",
    eleito: bool = True,
    final: bool = True,
    turno: int = 1,
    ano: int = 2026,
) -> None:
    cid = next(_ids)
    parlamentar_id = None
    if parlamentar is not None:  # (type, status) do parlamentar vinculado
        parlamentar_id = 50000 + cid
        db.execute(
            text("insert into parliamentarian (id, type, status) values (:id, :t, :s)"),
            {"id": parlamentar_id, "t": parlamentar[0], "s": parlamentar[1]},
        )
    db.execute(
        text(
            "insert into candidacy (id, election_year, tse_candidate_id, office_code, state, gender, race, "
            "education, parliamentarian_id) values (:id, :ano, :sq, :cargo, :uf, :g, :r, :e, :p)"
        ),
        {"id": cid, "ano": ano, "sq": 900000 + cid, "cargo": cargo, "uf": uf, "g": genero, "r": raca,
         "e": escolaridade, "p": parlamentar_id},
    )
    db.execute(
        text(
            "insert into candidacy_result (candidacy_id, turno, codigo_eleicao, situacao, eleito, totalizacao_final) "
            "values (:id, :turno, 6259, :st, :e, :final)"
        ),
        {"id": cid, "turno": turno, "st": situacao, "e": eleito, "final": final},
    )
    db.commit()


def _casa(resultado: dict, chave: str) -> dict:
    return next(c for c in resultado["casas"] if c["casa"] == chave)


def _uf(casa: dict, uf: str) -> dict:
    return next(item for item in casa["por_uf"] if item["uf"] == uf)


def _valores(distribuicao: list[dict]) -> dict:
    return {item["valor"]: (item["eleitos"], item["percentual"]) for item in distribuicao}


def test_sem_resultado_coletado_nada_aparece_como_zero() -> None:
    resultado = elected_profile(_make_session())
    assert resultado["ano"] == 2026
    for casa in resultado["casas"]:
        assert casa["ufs_encerradas"] == 0
        assert casa["ufs_aguardando"] == list(UFS)
        assert casa["brasil"] is None
        assert all(item["perfil"] is None and not item["encerrada"] for item in casa["por_uf"])


def test_percentuais_da_uf_encerrada() -> None:
    db = _make_session()
    _arquivo(db, "SP", DEP_FEDERAL, eleitos=5)
    _candidato(db, "SP", DEP_FEDERAL, genero="FEMININO", raca="PRETA")
    _candidato(db, "SP", DEP_FEDERAL, genero="FEMININO", raca="PARDA", situacao="Eleito por QP")
    _candidato(db, "SP", DEP_FEDERAL, raca="BRANCA", situacao="Eleito por média")
    _candidato(db, "SP", DEP_FEDERAL, raca="BRANCA")
    _candidato(db, "SP", DEP_FEDERAL, genero=None, raca=None)

    camara = _casa(elected_profile(db), "camara")
    sp = _uf(camara, "SP")
    assert sp["encerrada"] is True
    perfil = sp["perfil"]
    assert perfil["eleitos"] == 5 and perfil["eleitos_no_tse"] == 5
    assert _valores(perfil["genero"]) == {"FEMININO": (2, 40.0), "MASCULINO": (2, 40.0), None: (1, 20.0)}
    # Ordem fixa: feminino, masculino; sem informação por último.
    assert [item["valor"] for item in perfil["genero"]] == ["FEMININO", "MASCULINO", None]
    assert [item["valor"] for item in perfil["cor_raca"]] == ["BRANCA", "PRETA", "PARDA", None]
    assert perfil["pretos_e_pardos"] == {"eleitos": 2, "percentual": 40.0}
    assert camara["categorias"]["cor_raca"] == ["BRANCA", "PRETA", "PARDA", None]
    assert camara["ufs_encerradas"] == 1
    assert camara["tse_atualizado_em"].startswith("2026-10-04")


def test_escolaridade_em_ordem_de_nivel_e_superior_completo() -> None:
    db = _make_session()
    _arquivo(db, "SP", DEP_FEDERAL)
    _candidato(db, "SP", DEP_FEDERAL, escolaridade="ENSINO MÉDIO COMPLETO")
    _candidato(db, "SP", DEP_FEDERAL, escolaridade="SUPERIOR COMPLETO")
    _candidato(db, "SP", DEP_FEDERAL, escolaridade="SUPERIOR COMPLETO")
    _candidato(db, "SP", DEP_FEDERAL, escolaridade="LÊ E ESCREVE")

    camara = _casa(elected_profile(db), "camara")
    perfil = _uf(camara, "SP")["perfil"]
    assert [item["valor"] for item in perfil["escolaridade"]] == [
        "SUPERIOR COMPLETO", "ENSINO MÉDIO COMPLETO", "LÊ E ESCREVE",
    ]
    assert perfil["superior_completo"] == {"eleitos": 2, "percentual": 50.0}
    assert camara["categorias"]["escolaridade"] == [
        "SUPERIOR COMPLETO", "ENSINO MÉDIO COMPLETO", "LÊ E ESCREVE",
    ]


def test_reeleito_e_quem_exerce_mandato_na_mesma_casa() -> None:
    db = _make_session()
    _arquivo(db, "SP", DEP_FEDERAL)
    _arquivo(db, "SP", SENADOR)
    _candidato(db, "SP", DEP_FEDERAL, parlamentar=("Deputado", "Exercício"))  # reeleito
    _candidato(db, "SP", DEP_FEDERAL, parlamentar=("Deputado", "Fora de exercício"))  # ex-deputado
    _candidato(db, "SP", DEP_FEDERAL)  # novato, sem vínculo
    _candidato(db, "SP", DEP_FEDERAL, parlamentar=("Senador", "Exercício"))  # senador virando deputado
    _candidato(db, "SP", SENADOR, parlamentar=("Deputado", "Exercício"))  # deputado virando senador
    _candidato(db, "SP", SENADOR, parlamentar=("Senador", "Exercício"))  # reeleito (inclui suplente)

    resultado = elected_profile(db)
    assert _uf(_casa(resultado, "camara"), "SP")["perfil"]["reeleitos"] == {"eleitos": 1, "percentual": 25.0}
    assert _uf(_casa(resultado, "senado"), "SP")["perfil"]["reeleitos"] == {"eleitos": 1, "percentual": 50.0}


def test_so_conta_eleito_de_fato_no_resultado_encerrado() -> None:
    db = _make_session()
    _arquivo(db, "SP", DEP_FEDERAL)
    _arquivo(db, "SP", SENADOR)
    _candidato(db, "SP", DEP_FEDERAL)  # conta
    _candidato(db, "SP", DEP_FEDERAL, situacao="Suplente", eleito=False)
    _candidato(db, "SP", DEP_FEDERAL, situacao="Não eleito", eleito=False)
    # O TSE marca e="s" para o 2o turno; nao e eleito.
    _candidato(db, "SP", DEP_FEDERAL, situacao="2º turno", eleito=True)
    _candidato(db, "SP", DEP_FEDERAL, final=False)  # linha ainda parcial
    _candidato(db, "SP", DEP_FEDERAL, ano=2022)
    _candidato(db, "SP", 3)  # governador nao entra
    _candidato(db, "SP", SENADOR, genero="FEMININO")

    resultado = elected_profile(db)
    assert _uf(_casa(resultado, "camara"), "SP")["perfil"]["eleitos"] == 1
    senado_sp = _uf(_casa(resultado, "senado"), "SP")["perfil"]
    assert senado_sp["eleitos"] == 1
    assert _valores(senado_sp["genero"]) == {"FEMININO": (1, 100.0)}


def test_uf_com_totalizacao_aberta_nao_entra_nem_no_brasil() -> None:
    db = _make_session()
    _arquivo(db, "RJ", DEP_FEDERAL, final=False, apurado=47.26)
    _candidato(db, "RJ", DEP_FEDERAL)  # mesmo marcada final, o arquivo esta aberto
    _arquivo(db, "SP", DEP_FEDERAL)
    _candidato(db, "SP", DEP_FEDERAL)

    camara = _casa(elected_profile(db), "camara")
    rj = _uf(camara, "RJ")
    # CS-127: UF aberta mostra o andamento da apuracao, sem perfil.
    assert rj["encerrada"] is False and rj["perfil"] is None
    assert rj["percentual_apurado"] == 47.26
    assert rj["tse_atualizado_em"] is not None
    assert _uf(camara, "MG")["percentual_apurado"] is None  # sem arquivo coletado
    assert "RJ" in camara["ufs_aguardando"] and "SP" not in camara["ufs_aguardando"]
    assert camara["brasil"] is None


def test_brasil_aparece_so_com_as_27_encerradas() -> None:
    db = _make_session()
    for uf in UFS:
        _arquivo(db, uf, SENADOR, eleitos=2)
        _candidato(db, uf, SENADOR, genero="FEMININO" if uf in ("AC", "BA") else "MASCULINO")
        _candidato(db, uf, SENADOR, raca="PARDA" if uf == "AC" else "BRANCA")

    senado = _casa(elected_profile(db), "senado")
    assert senado["ufs_encerradas"] == 27 and senado["ufs_aguardando"] == []
    brasil = senado["brasil"]
    assert brasil["eleitos"] == 54 and brasil["eleitos_no_tse"] == 54
    assert _valores(brasil["genero"]) == {"FEMININO": (2, 3.7), "MASCULINO": (52, 96.3)}
    assert brasil["pretos_e_pardos"] == {"eleitos": 1, "percentual": 1.9}
    # Categoria ausente numa UF encerrada e zero de verdade: a tela usa as
    # categorias da casa como colunas.
    assert senado["categorias"]["genero"] == ["FEMININO", "MASCULINO"]
    assert _valores(_uf(senado, "SP")["perfil"]["genero"]) == {"MASCULINO": (2, 100.0)}


def test_eleito_do_tse_que_nao_esta_na_base_fica_visivel() -> None:
    db = _make_session()
    for uf in UFS:
        # SP: TSE elegeu 3, a base so tem 2; DF coletado antes da contagem.
        _arquivo(db, uf, DEP_FEDERAL, eleitos=None if uf == "DF" else (3 if uf == "SP" else 1))
        _candidato(db, uf, DEP_FEDERAL)
    _candidato(db, "SP", DEP_FEDERAL)

    camara = _casa(elected_profile(db), "camara")
    sp = _uf(camara, "SP")["perfil"]
    assert sp["eleitos"] == 2 and sp["eleitos_no_tse"] == 3
    assert _uf(camara, "DF")["perfil"]["eleitos_no_tse"] is None
    # Sem a contagem de alguma UF, o total do Brasil nao finge que conferiu.
    assert camara["brasil"]["eleitos"] == 28 and camara["brasil"]["eleitos_no_tse"] is None


def test_uf_encerrada_sem_eleito_na_base_nao_divide_por_zero() -> None:
    db = _make_session()
    _arquivo(db, "AC", DEP_FEDERAL, eleitos=8)
    perfil = _uf(_casa(elected_profile(db), "camara"), "AC")["perfil"]
    assert perfil["eleitos"] == 0 and perfil["eleitos_no_tse"] == 8
    assert perfil["genero"] == [] and perfil["pretos_e_pardos"] == {"eleitos": 0, "percentual": None}


def test_ignora_outro_ciclo() -> None:
    db = _make_session()
    _arquivo(db, "SP", DEP_FEDERAL, ciclo="ele2022")
    _candidato(db, "SP", DEP_FEDERAL)
    assert _uf(_casa(elected_profile(db), "camara"), "SP")["encerrada"] is False


def test_antes_da_migration_da_contagem_responde_sem_conferencia() -> None:
    db = _make_session(com_eleitos=False)
    _arquivo(db, "SP", DEP_FEDERAL)
    _candidato(db, "SP", DEP_FEDERAL)
    perfil = _uf(_casa(elected_profile(db), "camara"), "SP")["perfil"]
    assert perfil["eleitos"] == 1 and perfil["eleitos_no_tse"] is None


@pytest.fixture()
def session() -> Session:
    s = _make_session()
    yield s
    s.close()


def test_rota_admin(session: Session) -> None:
    _arquivo(session, "SP", DEP_FEDERAL, eleitos=1)
    _candidato(session, "SP", DEP_FEDERAL, genero="FEMININO")
    main.app.dependency_overrides[get_db] = lambda: session
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@mamute.com"
    main.app.dependency_overrides[verify_token] = lambda: None
    try:
        resp = TestClient(main.app).get("/api/admin/elected-profile")
    finally:
        main.app.dependency_overrides.clear()
    assert resp.status_code == 200
    sp = _uf(_casa(resp.json(), "camara"), "SP")
    assert sp["perfil"]["genero"] == [{"valor": "FEMININO", "eleitos": 1, "percentual": 100.0}]


def test_rota_e_404_para_quem_nao_e_admin(session: Session) -> None:
    main.app.dependency_overrides[get_db] = lambda: session
    main.app.dependency_overrides[verify_token] = lambda: None
    try:
        resp = TestClient(main.app).get("/api/admin/elected-profile")
    finally:
        main.app.dependency_overrides.clear()
    assert resp.status_code == 404
