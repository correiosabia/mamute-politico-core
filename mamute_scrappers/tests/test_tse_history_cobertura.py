"""CS-95: a timeline separa "ainda nao buscamos" de "buscamos e nao havia".

Em 04/11/2026 os 10 parlamentares sem nenhuma linha eram senadores que
assumiram como 1o suplente: a fase 2 nao varria os cargos de suplente, e o
banco nao guardava que a busca tinha acontecido.
"""
from __future__ import annotations

from typing import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mamute_scrappers.tse_crawler import electoral_history as eh_mod
from mamute_scrappers.tse_crawler.client import IncompleteListingError

from test_tse_history_seed import (
    Base,
    Candidacy,
    ElectoralHistory,
    ElectoralHistorySearch,
    Parliamentarian,
)

ELEICAO_2022 = 2040602022


@pytest.fixture()
def session(monkeypatch) -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(eh_mod, "ElectoralHistory", ElectoralHistory)
    monkeypatch.setattr(eh_mod, "ElectoralHistorySearch", ElectoralHistorySearch)
    monkeypatch.setattr(eh_mod, "Candidacy", Candidacy)
    monkeypatch.setattr(eh_mod, "Parliamentarian", Parliamentarian)
    monkeypatch.setattr(eh_mod, "SEED_ELECTION_YEARS", (2022,))
    monkeypatch.setattr(eh_mod, "UFS", ("AL", "MA"))
    with maker() as s:
        yield s


def _detalhe(tse_id: int, cpf: str | None = None) -> dict:
    return {
        "cpf": cpf,
        "totalDeBens": 0,
        "bens": [],
        "eleicoesAnteriores": [
            {"id": str(tse_id), "nrAno": 2022, "idEleicao": str(ELEICAO_2022), "sgUe": "MA",
             "cargo": "1º Suplente", "situacaoTotalizacao": "Suplente"},
            {"id": "5", "nrAno": 2004, "idEleicao": "1", "sgUe": "12345",
             "cargo": "Vereador", "situacaoTotalizacao": "Eleito"},
        ],
    }


class _TSE:
    """Listagens por (uf, cargo) e detalhes por id."""

    def __init__(self, listagens=None, detalhes=None, eleicao=ELEICAO_2022, quebra=None):
        self.listagens = listagens or {}
        self.detalhes = detalhes or {}
        self.eleicao = eleicao
        self.quebra = quebra

    def find_general_election_id(self, year):
        return self.eleicao

    def list_candidates(self, year, uf, election_id, office_code):
        if (uf, office_code) == self.quebra:
            raise IncompleteListingError("listagem ilegivel")
        return self.listagens.get((uf, office_code), [])

    def get_candidate_detail(self, year, state, election_id, candidate_id):
        return self.detalhes.get(int(candidate_id))


def _parlamentar(session, pid, nome, uf, cpf=None):
    session.add(Parliamentarian(id=pid, name=nome, full_name=nome, cpf=cpf, state_elected=uf))
    session.commit()


def _busca(session, pid):
    registro = session.get(ElectoralHistorySearch, pid)
    return registro.outcome if registro else None


class TestFase2:
    def test_acha_suplente_de_senador(self, session) -> None:
        _parlamentar(session, 516, "Ana Paula Lobato", "MA")
        tse = _TSE(
            listagens={("MA", 9): [{"id": 100001607044, "nomeUrna": "ANA PAULA LOBATO",
                                    "nomeCompleto": "ANA PAULA DIAS LOBATO NOVA ALVES"}]},
            detalhes={100001607044: _detalhe(100001607044)},
        )

        contadores = eh_mod.seed_missing_parliamentarians(session, tse)

        assert contadores["seeded"] == 1
        assert session.query(ElectoralHistory).filter_by(parliamentarian_id=516).count() == 2
        assert _busca(session, 516) == "encontrado"

    def test_casa_pelo_id_da_candidatura_ja_vinculada(self, session) -> None:
        """O nome de urna "DRA EUDOCIA" nao casa com "Dra. Eudócia"; o id do
        TSE da candidatura que a carga ja vinculou casa."""
        _parlamentar(session, 533, "Dra. Eudócia", "AL")
        session.add(Candidacy(id=1, election_year=2022, tse_candidate_id=20000616691,
                              parliamentarian_id=533))
        session.commit()
        tse = _TSE(
            listagens={("AL", 9): [{"id": 20000616691, "nomeUrna": "DRA EUDOCIA",
                                    "nomeCompleto": "EUDOCIA MARIA HOLANDA DE ALBUQUERQUE CALDAS"}]},
            detalhes={20000616691: _detalhe(20000616691)},
        )

        eh_mod.seed_missing_parliamentarians(session, tse)

        assert _busca(session, 533) == "encontrado"

    def test_varredura_completa_sem_achar_registra_nao_encontrado(self, session) -> None:
        _parlamentar(session, 1, "Fulano de Tal", "MA")
        _parlamentar(session, 2, "Beltrano", "AL", cpf="11122233344")
        tse = _TSE(
            listagens={("AL", 6): [{"id": 77, "nomeUrna": "BELTRANO"}]},
            detalhes={77: _detalhe(77, cpf="99988877766")},
        )

        contadores = eh_mod.seed_missing_parliamentarians(session, tse)

        assert contadores["still_pending"] == 2
        assert _busca(session, 1) == "nao_encontrado"
        assert _busca(session, 2) == "cpf_divergente"
        assert session.query(ElectoralHistory).count() == 0

    def test_detalhe_que_falha_nao_vira_nao_encontrado(self, session) -> None:
        _parlamentar(session, 516, "Ana Paula Lobato", "MA")
        tse = _TSE(listagens={("MA", 9): [{"id": 100001607044, "nomeUrna": "ANA PAULA LOBATO"}]})

        eh_mod.seed_missing_parliamentarians(session, tse)

        assert _busca(session, 516) is None

    def test_eleicao_nao_achada_nao_registra_nada(self, session) -> None:
        _parlamentar(session, 1, "Fulano de Tal", "MA")

        eh_mod.seed_missing_parliamentarians(session, _TSE(eleicao=None))

        assert _busca(session, 1) is None

    def test_listagem_ilegivel_nao_registra_nada(self, session) -> None:
        _parlamentar(session, 1, "Fulano de Tal", "MA")

        with pytest.raises(IncompleteListingError):
            eh_mod.seed_missing_parliamentarians(session, _TSE(quebra=("MA", 6)))

        assert _busca(session, 1) is None

    def test_busca_seguinte_atualiza_o_resultado(self, session) -> None:
        _parlamentar(session, 516, "Ana Paula Lobato", "MA")
        eh_mod.seed_missing_parliamentarians(session, _TSE())
        assert _busca(session, 516) == "nao_encontrado"

        tse = _TSE(
            listagens={("MA", 9): [{"id": 100001607044, "nomeUrna": "ANA PAULA LOBATO"}]},
            detalhes={100001607044: _detalhe(100001607044)},
        )
        eh_mod.seed_missing_parliamentarians(session, tse)

        assert _busca(session, 516) == "encontrado"
        assert session.query(ElectoralHistorySearch).count() == 1


def test_fase_3_grava_o_que_o_tse_tinha(session) -> None:
    for tse_id, ano in ((1, 2022), (2, 2018), (3, 2004)):
        session.add(ElectoralHistory(election_year=ano, tse_candidate_id=tse_id,
                                     tse_election_id=9, state="MA"))
    session.commit()
    detalhes = {
        1: {"totalDeBens": 500.0, "bens": [{"valor": 500.0}]},
        2: {"totalDeBens": 0, "bens": []},
        3: {"totalDeBens": 0, "bens": []},
    }

    eh_mod.drain_assets(session, _TSE(detalhes=detalhes), max_details=None)

    linhas = {
        row.tse_candidate_id: (row.assets_status, row.declared_assets)
        for row in session.query(ElectoralHistory)
    }
    assert linhas[1][0] == "declarado" and linhas[1][1] == 500
    assert linhas[2] == ("sem_bens", None)
    assert linhas[3] == ("nao_divulgado", None)


def test_cobertura(session) -> None:
    _parlamentar(session, 1, "Com Timeline", "MA")
    _parlamentar(session, 2, "Nunca Buscado", "MA")
    _parlamentar(session, 3, "Nao Achado", "MA")
    session.add(ElectoralHistory(election_year=2022, tse_candidate_id=1, state="MA",
                                 parliamentarian_id=1, assets_fetched_at="2026-11-04",
                                 assets_status="declarado"))
    session.add(ElectoralHistory(election_year=2018, tse_candidate_id=2, state="MA",
                                 parliamentarian_id=1))
    session.commit()
    eh_mod._record_search(session, 3, "nao_encontrado")
    session.commit()

    status = eh_mod.coverage(session)

    assert status["parlamentares"] == 3
    assert status["com_timeline"] == 1
    assert status["sem_timeline"] == {"nao_buscado": 1, "nao_encontrado": 1}
    assert status["patrimonio_por_linha"] == {"declarado": 1, "nao_buscado": 1}
