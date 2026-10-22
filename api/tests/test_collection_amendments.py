"""Emendas dos membros de uma coleção e linha de base (CS-135)."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api.db.base import Base
from api.db.models import Parliamentarian
from api.db.models.parliamentary_amendment import ParliamentaryAmendment
from api.services import collection_amendments as ca

ANO = date.today().year
PIX = "Emenda Individual - Transferências Especiais"
DEF = "Emenda Individual - Transferências com Finalidade Definida"


@compiles(JSONB, "sqlite")
def _jsonb_no_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_no_sqlite(_type, _compiler, **_kw):  # noqa: ANN001
    return "INTEGER"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Parliamentarian.__table__, ParliamentaryAmendment.__table__])
    s = sessionmaker(bind=engine)()
    ca._cache_base.clear()
    n = 0

    def emenda(parl: int, ano: int, tipo: str, valor: float, local: str, area: str = "Saúde"):
        nonlocal n
        n += 1
        return ParliamentaryAmendment(
            id=n, amendment_code=str(n), year=ano, amendment_type=tipo, parliamentarian_id=parl,
            match_status="matched", spending_locality=local, function=area,
            committed_value=valor, paid_value=valor / 2,
        )

    s.add_all([Parliamentarian(id=i, type="Deputado", name=f"P{i}") for i in (1, 2)])
    s.add(Parliamentarian(id=3, type="Senador", name="P3"))
    s.add_all(
        [
            emenda(1, ANO, PIX, 600, "RECIFE - PE"),
            emenda(1, ANO, DEF, 400, "MÚLTIPLO", "Educação"),
            emenda(1, ANO - 1, DEF, 1000, "OLINDA - PE"),
            # Fora do recorte de 4 anos: não entra.
            emenda(1, ANO - ca.ANOS, PIX, 99999, "RECIFE - PE"),
            emenda(2, ANO, DEF, 100, "NATAL - RN"),
            emenda(3, ANO, DEF, 300, "SALVADOR - BA"),
        ]
    )
    s.commit()
    yield s
    s.close()


def test_resumo_por_parlamentar(session):
    r = ca.resumo_emendas(session, {1})[1]
    assert [(a["year"], a["committed"], a["special"]) for a in r["by_year"]] == [(ANO - 1, 1000, 0), (ANO, 1000, 600)]
    assert r["committed"] == 2000 and r["paid"] == 1000
    assert r["special_share"] == pytest.approx(0.3)
    # "MÚLTIPLO" não diz para onde foi: fica fora dos destinos.
    assert [d["name"] for d in r["destinations"]] == ["OLINDA - PE", "RECIFE - PE"]
    assert r["areas"][0] == {"name": "Saúde", "committed": 1600}
    assert ca.resumo_emendas(session, set()) == {}


def test_linha_de_base(session):
    b = ca.linha_de_base(session)
    assert b["parliamentarians"] == 3
    assert b["median_committed"] == 300
    assert b["median_special_share"] == 0
    assert b["special_share_all"] == pytest.approx(600 / 2400)
    # Por casa: o senador não entra na mediana dos deputados.
    assert b["by_type"]["Deputado"]["parliamentarians"] == 2
    assert b["by_type"]["Deputado"]["median_committed"] == (2000 + 100) / 2
    assert b["by_type"]["Senador"]["median_committed"] == 300
