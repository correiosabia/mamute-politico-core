"""Comparação 1º x 2º turno de presidente no admin (CS-160)."""
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
from api.services.runoff_comparison import runoff_comparison

ALFA, BETA, GAMA = 101, 102, 201
_ids = itertools.count(1)

DDL = [
    "create table ibge_municipio (cd_ibge integer primary key, nome text, uf text, "
    "regiao_intermediaria_id integer, regiao_intermediaria_nome text, regiao_imediata_id integer, "
    "regiao_imediata_nome text, macrorregiao text)",
    "create table tse_resultado_municipio (id integer primary key, ciclo text, codigo_eleicao integer, "
    "turno smallint, cargo_codigo integer, uf text, cd_tse text, cd_ibge integer, nome text, capital boolean, "
    "eleitorado bigint, eleitorado_apurado bigint, comparecimento bigint, abstencao bigint, brancos bigint, nulos bigint, validos bigint, "
    "percentual_apurado numeric, totalizacao_final boolean, tse_atualizado_em timestamp, coletado_em timestamp)",
    "create table tse_resultado_municipio_candidato (resultado_id integer, sqcand bigint, numero integer, "
    "nome_urna text, votos bigint)",
    "create table comparacao_turnos_estimativa (ciclo text, cargo_codigo integer, recorte text, chave text, "
    "origem text, destino text, proporcao numeric, votos bigint, municipios integer, calculado_em timestamp, "
    "proporcao_min numeric, proporcao_max numeric, proporcao_prior numeric, fonte_prior text)",
]


def _engine():
    return create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)


@pytest.fixture
def vazio() -> Session:
    return sessionmaker(bind=_engine())()


def _municipio(db: Session, cd: str, turno: int, uf: str, ibge, capital: bool, votos: dict, *,
               brancos: int = 0, nulos: int = 0, abstencao: int = 0, final: bool = True,
               eleitorado_apurado=None) -> None:
    validos = sum(votos.values())
    rid = next(_ids)
    db.execute(
        text(
            "insert into tse_resultado_municipio (id, ciclo, codigo_eleicao, turno, cargo_codigo, uf, cd_tse, "
            "cd_ibge, nome, capital, eleitorado, eleitorado_apurado, comparecimento, abstencao, brancos, nulos, validos, "
            "totalizacao_final, tse_atualizado_em) values (:id, 'ele2026', :e, :t, 1, :uf, :cd, :ibge, :nome, "
            ":cap, :el, :ea, :co, :ab, :b, :n, :v, :f, '2026-10-25 21:00:00')"
        ),
        {"id": rid, "e": 6256 + turno, "t": turno, "uf": uf, "cd": cd, "ibge": ibge, "nome": f"M{cd}",
         "cap": capital, "el": validos + brancos + nulos + abstencao,
         "ea": eleitorado_apurado if eleitorado_apurado is not None else validos + brancos + nulos + abstencao, "co": validos + brancos + nulos,
         "ab": abstencao, "b": brancos, "n": nulos, "v": validos, "f": final},
    )
    nomes = {ALFA: "ALFA", BETA: "BETA", GAMA: "GAMA"}
    for sq, v in votos.items():
        db.execute(
            text("insert into tse_resultado_municipio_candidato values (:r, :s, :n, :nm, :v)"),
            {"r": rid, "s": sq, "n": sq, "nm": nomes[sq], "v": v},
        )


def _popular(db: Session, *, parcial: bool = False, sem_exterior_t2: bool = False) -> None:
    for ddl in DDL:
        db.execute(text(ddl))
    db.execute(text(
        "insert into ibge_municipio (cd_ibge, nome, uf, regiao_intermediaria_id, regiao_intermediaria_nome, macrorregiao) values "
        "(1, 'Florianópolis', 'SC', 4201, 'Florianópolis', 'Sul'), (2, 'Blumenau', 'SC', 4203, 'Blumenau', 'Sul'), "
        "(3, 'Salvador', 'BA', 2901, 'Salvador', 'Nordeste'), (4, 'Feira', 'BA', 2902, 'Feira de Santana', 'Nordeste')"
    ))
    # 1º turno
    _municipio(db, "1", 1, "sc", 1, True, {ALFA: 40, BETA: 50, GAMA: 10}, brancos=5, nulos=5, abstencao=20)
    _municipio(db, "2", 1, "sc", 2, False, {ALFA: 20, BETA: 70, GAMA: 10}, brancos=5, nulos=5, abstencao=10)
    _municipio(db, "3", 1, "ba", 3, True, {ALFA: 70, BETA: 20, GAMA: 10}, abstencao=30)
    _municipio(db, "4", 1, "ba", 4, False, {ALFA: 0, BETA: 0, GAMA: 0})
    _municipio(db, "9", 1, "zz", None, False, {ALFA: 8, GAMA: 2}, abstencao=90)
    # 2º turno
    _municipio(db, "1", 2, "sc", 1, True, {ALFA: 55, BETA: 45}, brancos=4, nulos=6, abstencao=20)
    _municipio(db, "2", 2, "sc", 2, False, {ALFA: 25, BETA: 75}, brancos=5, nulos=5, abstencao=10)
    # parcial: metade das seções de Salvador apuradas (eleitorado total 130, apurado 65)
    _municipio(db, "3", 2, "ba", 3, True, {ALFA: 45, BETA: 35}, abstencao=50, final=not parcial,
               eleitorado_apurado=65 if parcial else None)
    _municipio(db, "4", 2, "ba", 4, False, {ALFA: 0, BETA: 0})
    if not sem_exterior_t2:
        _municipio(db, "9", 2, "zz", None, False, {ALFA: 5, BETA: 5}, abstencao=90)
    db.execute(text(
        "insert into comparacao_turnos_estimativa values "
        "('ele2026', 1, 'brasil', 'BR', '201', '101', 0.6, 19000, 5, '2026-10-26 01:00:00', 0.55, 0.65, 0.5, 'Pesquisa X'), "
        "('ele2026', 1, 'brasil', 'BR', '201', '102', 0.4, 13000, 5, '2026-10-26 01:00:00', 0.2, 0.6, 0.4, 'Pesquisa X'), "
        "('ele2026', 1, 'brasil', 'BR', 'abstencao', 'abstencao', 0.9, 135000, 5, '2026-10-26 01:00:00', null, null, null, null), "
        "('ele2026', 1, 'brasil', 'BR', 'outros_candidatos', '101', 0.4, 1000, 5, '2026-10-26 01:00:00', 0.35, 0.45, 0.4, null)"
    ))
    db.commit()


@pytest.fixture
def db() -> Session:
    s = sessionmaker(bind=_engine())()
    _popular(s)
    return s


def _recorte(resp: dict, tipo: str, chave: str) -> dict:
    return next(r for r in resp["recortes"] if r["tipo"] == tipo and r["chave"] == chave)


def test_sem_tabelas_responde_indisponivel(vazio: Session) -> None:
    resp = runoff_comparison(vazio)
    assert resp["disponivel"] is False
    assert resp["recortes"] == [] and resp["destaques"] == []


def test_finalistas_vem_do_segundo_turno(db: Session) -> None:
    resp = runoff_comparison(db)
    assert resp["disponivel"] is True
    assert [f["nome"] for f in resp["finalistas"]] == ["BETA", "ALFA"]


def test_soma_brasil_inclui_exterior(db: Session) -> None:
    brasil = _recorte(runoff_comparison(db), "brasil", "BR")
    assert brasil["turno1"]["finalistas"][str(ALFA)]["votos"] == 138
    assert brasil["turno1"]["validos"] == 310
    assert brasil["turno1"]["demais_votos"] == 32
    assert brasil["turno2"]["finalistas"][str(BETA)]["votos"] == 160
    assert brasil["turno2"]["validos"] == 290
    assert brasil["turno1"]["abstencao"] == 150


def test_pp_margem_e_lider(db: Session) -> None:
    resp = runoff_comparison(db)
    sc = _recorte(resp, "uf", "SC")
    assert sc["nome"] == "Santa Catarina"
    assert sc["turno1"]["finalistas"][str(ALFA)]["pct"] == pytest.approx(30.0)
    assert sc["turno2"]["finalistas"][str(ALFA)]["pct"] == pytest.approx(40.0)
    assert sc["diferenca"]["finalistas"][str(ALFA)]["pp"] == pytest.approx(10.0)
    assert sc["diferenca"]["finalistas"][str(ALFA)]["votos"] == 20
    assert sc["turno2"]["margem"] == {"votos": 40, "pp": pytest.approx(20.0)}
    floripa = _recorte(resp, "regiao_intermediaria", "4201")
    assert (floripa["lider_turno1"], floripa["vencedor_turno2"], floripa["mudou_lider"]) == (
        str(BETA), str(ALFA), True
    )
    assert _recorte(resp, "uf", "ZZ")["nome"] == "Exterior"
    assert _recorte(resp, "macrorregiao", "Sul")["turno2"]["validos"] == 200


def test_capital_e_interior_por_uf(db: Session) -> None:
    resp = runoff_comparison(db)
    assert _recorte(resp, "capital", "SC")["turno1"]["validos"] == 100
    assert _recorte(resp, "interior", "SC")["turno1"]["validos"] == 100
    assert not [r for r in resp["recortes"] if r["tipo"] in ("capital", "interior") and r["chave"] == "ZZ"]


def test_percentual_null_sem_validos(db: Session) -> None:
    interior_ba = _recorte(runoff_comparison(db), "interior", "BA")
    assert interior_ba["turno1"]["finalistas"][str(ALFA)]["pct"] is None
    assert interior_ba["diferenca"]["finalistas"][str(ALFA)]["pp"] is None


def test_finalista_sem_voto_no_t1_e_zero(db: Session) -> None:
    exterior = _recorte(runoff_comparison(db), "uf", "ZZ")
    assert exterior["turno1"]["finalistas"][str(BETA)] == {"votos": 0, "pct": 0.0}


def test_abstencao_x_margem(db: Session) -> None:
    resp = runoff_comparison(db)
    ba = next(a for a in resp["abstencao_x_margem"] if a["tipo"] == "uf" and a["chave"] == "BA")
    assert (ba["variacao_abstencao"], ba["margem_votos"], ba["maior_que_margem"]) == (20, 10, True)
    sc = next(a for a in resp["abstencao_x_margem"] if a["tipo"] == "uf" and a["chave"] == "SC")
    assert sc["maior_que_margem"] is False


def test_estimativa_com_nomes(db: Session) -> None:
    resp = runoff_comparison(db)
    brasil = resp["estimativa"]["recortes"][0]
    assert brasil["recorte"] == "brasil"
    linha = next(l for l in brasil["linhas"] if l["origem"] == str(GAMA) and l["destino"] == str(ALFA))
    assert linha["origem_nome"] == "GAMA" and linha["destino_nome"] == "ALFA"
    assert linha["proporcao"] == pytest.approx(0.6)
    absten = next(l for l in brasil["linhas"] if l["origem"] == "abstencao")
    assert absten["origem_nome"] == "Abstenção"


def test_destaques_brasil_e_sem_travessao(db: Session) -> None:
    resp = runoff_comparison(db)
    textos = [d["texto"] for d in resp["destaques"]]
    assert "No Brasil, ALFA foi de 44,5% para 44,8% dos votos válidos (+0,3 pp)." in textos
    assert "No Brasil, BETA foi de 45,2% para 55,2% dos votos válidos (+10,0 pp)." in textos
    assert any("Na região de Florianópolis (SC), BETA liderou o 1º turno e ALFA venceu o 2º." == t for t in textos)
    assert any(t.startswith("Na Bahia, a abstenção variou +20 eleitores") for t in textos)
    assert all("—" not in t for t in textos)
    assert not any(t.startswith("Parcial") for t in textos)


def test_parcial_segura_abstencao_estimativa_e_marca_frases() -> None:
    s = sessionmaker(bind=_engine())()
    _popular(s, parcial=True)
    resp = runoff_comparison(s)
    assert resp["situacao"]["parcial"] is True
    assert resp["situacao"]["turno2"] == {"municipios": 5, "totalizados": 4}
    assert resp["abstencao_x_margem"] == []
    assert resp["estimativa"]["recortes"] == []
    assert resp["destaques"] and all(d["texto"].startswith("Parcial: ") for d in resp["destaques"])
    assert not any("abstenção variou" in d["texto"] for d in resp["destaques"])


def test_rota_admin(db: Session) -> None:
    main.app.dependency_overrides[get_db] = lambda: db
    main.app.dependency_overrides[require_ghost_admin] = lambda: "admin@mamute.com"
    main.app.dependency_overrides[verify_token] = lambda: None
    try:
        resp = TestClient(main.app).get("/api/admin/runoff-comparison")
    finally:
        main.app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["disponivel"] is True


def test_rota_e_404_para_quem_nao_e_admin(db: Session) -> None:
    main.app.dependency_overrides[get_db] = lambda: db
    main.app.dependency_overrides[verify_token] = lambda: None
    try:
        resp = TestClient(main.app).get("/api/admin/runoff-comparison")
    finally:
        main.app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_parcial_mede_abstencao_sobre_eleitorado_apurado() -> None:
    s = sessionmaker(bind=_engine())()
    _popular(s, parcial=True)
    capital_ba = _recorte(runoff_comparison(s), "capital", "BA")
    # 50 de abstenção sobre 65 eleitores das seções apuradas, não sobre os 130 do total
    assert capital_ba["turno2"]["abstencao_pct"] == pytest.approx(100 * 50 / 65, rel=1e-3)


def test_parcial_quando_falta_municipio_no_segundo_turno() -> None:
    s = sessionmaker(bind=_engine())()
    _popular(s, sem_exterior_t2=True)
    resp = runoff_comparison(s)
    assert resp["situacao"]["parcial"] is True
    assert resp["estimativa"]["recortes"] == []


def test_parcial_nao_afirma_quem_venceu() -> None:
    s = sessionmaker(bind=_engine())()
    _popular(s, parcial=True)
    textos = [d["texto"] for d in runoff_comparison(s)["destaques"]]
    assert not any("venceu" in t for t in textos)
    assert not any("abstenção foi de" in t for t in textos)


def test_hora_do_tse_ignora_o_exterior(db: Session) -> None:
    # O TSE grava a hora dos arquivos do exterior no fuso de cada país (veio "no futuro" em 25/10/2026).
    db.execute(text("update tse_resultado_municipio set tse_atualizado_em = '2026-10-26 13:05:23' where uf = 'zz'"))
    db.commit()
    assert runoff_comparison(db)["situacao"]["tse_atualizado_em"].startswith("2026-10-25")


def test_estimativa_traz_faixa_prior_fonte_e_incerto(db: Session) -> None:
    linhas = runoff_comparison(db)["estimativa"]["recortes"][0]["linhas"]
    firme = next(l for l in linhas if l["origem"] == str(GAMA) and l["destino"] == str(ALFA))
    assert (firme["proporcao_min"], firme["proporcao_max"]) == (pytest.approx(0.55), pytest.approx(0.65))
    assert firme["proporcao_prior"] == pytest.approx(0.5) and firme["fonte_prior"] == "Pesquisa X"
    assert firme["incerto"] is False
    larga = next(l for l in linhas if l["origem"] == str(GAMA) and l["destino"] == str(BETA))
    assert larga["incerto"] is True
    sem_faixa = next(l for l in linhas if l["origem"] == "abstencao")
    assert sem_faixa["proporcao_min"] is None and sem_faixa["incerto"] is False
    outros = next(l for l in linhas if l["origem"] == "outros_candidatos")
    assert outros["origem_nome"] == "Outros candidatos"
