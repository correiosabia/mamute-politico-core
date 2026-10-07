"""e-Agendas (CGU): filtro por termo e montagem das linhas (CS-135)."""

from __future__ import annotations

from datetime import date

from mamute_scrappers.agenda_crawler.eagendas import build_row, filtra


def _linha(**extra) -> dict[str, str]:
    base = {
        "Nome": "FULANO DE TAL",
        "Cargo/Função": "MINISTRO DE ESTADO DO EXEMPLO",
        "Órgão/Entidade": "Ministério do Exemplo",
        "ID do registro": "100",
        "Data de início": "18-03-2024",
        "Hora de início": "17:00",
        "Hora de término": "18:00",
        "Forma de realização": "Presencial",
        "Assunto do Compromisso": "Apresentação da diretoria do Banco Exemplar",
        "Local": "Esplanada, Bloco F",
        "Participantes": "Agentes públicos participantes: FULANO DE TAL (CPF: ***.111.222-**) / MINISTRO",
        "Detalhes": "ND",
        "Informações complementares": "ND",
    }
    base.update(extra)
    return base


def test_build_row():
    r = build_row(_linha())
    assert r["event_date"] == date(2024, 3, 18) and r["starts_at"] == "17:00"
    assert r["authority_name"] == "Fulano de Tal"
    assert r["office_label"] == "Ministro de Estado do Exemplo"
    assert r["organization"] == "Ministério do Exemplo" and r["remote"] is False
    assert r["authority_id"] == "100:fulano de tal"
    assert build_row(_linha(**{"Forma de realização": "Virtual"}))["remote"] is True
    assert build_row(_linha(**{"Data de início": "ND"})) is None


def test_filtra_por_termo_sem_acento_e_tira_repetido():
    linhas = [
        _linha(),
        # O mesmo compromisso para outro agente: entra (outra pessoa).
        _linha(Nome="BELTRANO"),
        # Repetido para o mesmo agente: sai.
        _linha(),
        # Termo só nos participantes, com acento diferente.
        _linha(**{"ID do registro": "200", "Assunto do Compromisso": "Programa habitacional",
                  "Participantes": "Agentes privados participantes: Sicrano representando BANCO EXEMPLÁR"}),
        _linha(**{"ID do registro": "300", "Assunto do Compromisso": "Outra reunião"}),
    ]
    rows = filtra(linhas, ["banco exemplar", "abc"])
    assert sorted(r["authority_id"] for r in rows) == ["100:beltrano", "100:fulano de tal", "200:fulano de tal"]
    assert filtra(linhas, ["abc"]) == []
