"""CS-126: o job da timeline eleitoral não pode segurar transação esperando o TSE.

Em 04/10/2026 o tse-electoral-history ficou 8 h "idle in transaction": a fase 3
abria a transação na consulta inicial e chamava o TSE milhares de vezes antes
do commit, travando o autovacuum e segurando lock em electoral_history.
"""
from __future__ import annotations

from typing import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mamute_scrappers.tse_crawler import electoral_history as eh_mod

from test_tse_history_seed import Base, Candidacy, ElectoralHistory, Parliamentarian


@pytest.fixture()
def session(monkeypatch) -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(eh_mod, "ElectoralHistory", ElectoralHistory)
    monkeypatch.setattr(eh_mod, "Candidacy", Candidacy)
    monkeypatch.setattr(eh_mod, "Parliamentarian", Parliamentarian)
    with maker() as s:
        yield s


def _pendentes(session: Session, n: int) -> None:
    for i in range(1, n + 1):
        session.add(
            ElectoralHistory(
                election_year=2022,
                tse_candidate_id=i,
                tse_election_id=2040602022,
                state="PR",
                parliamentarian_id=None,
            )
        )
    session.commit()


class _Cliente:
    def __init__(self, session: Session, respostas=None) -> None:
        self.session = session
        self.respostas = respostas
        self.em_transacao: list[bool] = []
        self.chamadas = 0

    def _registra(self) -> None:
        self.chamadas += 1
        self.em_transacao.append(self.session.in_transaction())

    def get_candidate_detail(self, year, state, election_id, candidate_id):
        self._registra()
        if self.respostas is not None:
            return self.respostas(self.chamadas)
        return {"totalDeBens": 10.0, "bens": [{"valor": 10.0}]}

    def find_general_election_id(self, year):
        self._registra()
        return 2040602022

    def list_candidates(self, year, uf, election_id, office_code):
        self._registra()
        if uf == "PR" and office_code == 6:
            return [{"id": 77, "nomeCompleto": "ANA SOUZA", "nomeUrna": "ANA"}]
        return []


class TestDrenagem:
    def test_nenhuma_transacao_aberta_enquanto_espera_o_tse(self, session) -> None:
        _pendentes(session, 5)
        cliente = _Cliente(session)

        eh_mod.drain_assets(session, cliente, max_details=None)

        assert cliente.chamadas == 5
        assert cliente.em_transacao == [False] * 5

    def test_grava_os_bens_na_linha_existente(self, session) -> None:
        """O state faz parte da chave natural (CS-69): sem ele, o upsert criava
        uma linha nova com state nulo e a original ficava pendente para sempre."""
        _pendentes(session, 3)

        contadores = eh_mod.drain_assets(session, _Cliente(session), max_details=None)

        assert contadores["fetched"] == 3
        assert session.query(ElectoralHistory).count() == 3
        assert session.query(ElectoralHistory).filter(ElectoralHistory.assets_fetched_at.is_(None)).count() == 0
        assert session.query(ElectoralHistory).filter(ElectoralHistory.state.is_(None)).count() == 0

    def test_desiste_depois_de_falhas_seguidas(self, session, monkeypatch) -> None:
        monkeypatch.setattr(eh_mod, "MAX_FALHAS_SEGUIDAS", 3)
        _pendentes(session, 20)
        cliente = _Cliente(session, respostas=lambda n: None)

        contadores = eh_mod.drain_assets(session, cliente, max_details=None)

        assert cliente.chamadas == 3
        assert contadores["interrompido"] == "falhas_seguidas"

    def test_sucesso_zera_a_contagem_de_falhas(self, session, monkeypatch) -> None:
        monkeypatch.setattr(eh_mod, "MAX_FALHAS_SEGUIDAS", 3)
        _pendentes(session, 9)
        # falha, falha, ok, falha, falha, ok...: nunca 3 seguidas.
        cliente = _Cliente(session, respostas=lambda n: None if n % 3 else {"totalDeBens": 1.0, "bens": []})

        contadores = eh_mod.drain_assets(session, cliente, max_details=None)

        assert cliente.chamadas == 9
        assert contadores["fetched"] == 3
        assert "interrompido" not in contadores

    def test_para_no_teto_de_tempo(self, session, monkeypatch) -> None:
        _pendentes(session, 10)
        relogio = iter(range(0, 10_000, 60))
        monkeypatch.setattr(eh_mod.time, "monotonic", lambda: next(relogio))
        cliente = _Cliente(session)

        contadores = eh_mod.drain_assets(session, cliente, max_details=None, prazo_s=150)

        assert cliente.chamadas < 10
        assert contadores["interrompido"] == "prazo"


def test_fase_2_nao_segura_transacao_durante_as_chamadas(session, monkeypatch) -> None:
    monkeypatch.setattr(eh_mod, "SEED_ELECTION_YEARS", (2022,))
    session.add(Parliamentarian(id=1, name="Ana Souza", full_name="Ana Souza", cpf=None, state_elected="PR"))
    session.add(Parliamentarian(id=2, name="Bia Lima", full_name="Bia Lima", cpf=None, state_elected="SP"))
    session.commit()

    class _ComDetalhe(_Cliente):
        def get_candidate_detail(self, year, state, election_id, candidate_id):
            self._registra()
            return {
                "eleicoesAnteriores": [
                    {"id": "77", "nrAno": 2022, "idEleicao": "2040602022", "sgUe": "PR",
                     "cargo": "Deputado Federal", "situacaoTotalizacao": "Eleito"},
                ],
                "totalDeBens": 1.0,
                "bens": [],
            }

    cliente = _ComDetalhe(session)

    contadores = eh_mod.seed_missing_parliamentarians(session, cliente)

    assert contadores["seeded"] == 1
    assert cliente.chamadas > 10
    assert not any(cliente.em_transacao)
