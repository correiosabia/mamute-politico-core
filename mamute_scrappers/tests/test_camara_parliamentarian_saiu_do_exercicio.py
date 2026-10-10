"""CS-141: quem sai do exercício também tem o status atualizado.

A lista padrão da Câmara (/deputados) só traz quem está em exercício hoje.
Quem saía da lista ficava "Exercício" na base para sempre (557 contra 513 em
10/10/2026; ex.: Luis Carlos Gomes, de volta à suplência em 25/03).
"""
from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_camara_parliamentarian_saiu_module",
        REPO_ROOT / "mamute_scrappers/camara_crawler/parliamentarian.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cam = _load()


def _preparar(monkeypatch, *, na_lista, marcados_na_base):
    gravados = []
    detalhes_pedidos = []

    @contextmanager
    def _escopo():
        yield object()

    monkeypatch.setattr(cam, "_ensure_db_dependencies", lambda: None)
    monkeypatch.setattr(cam, "_SESSION_SCOPE", _escopo)
    monkeypatch.setattr(cam, "get_camara_ingestion_scope", lambda: cam.CamaraIngestionScope.CURRENT_ONLY)
    monkeypatch.setattr(
        cam,
        "_fetch_parliamentarians",
        lambda **_: iter({"parliamentarian_code": c, "status": "Exercício"} for c in na_lista),
    )
    monkeypatch.setattr(cam, "_codes_marked_in_exercise", lambda session: set(marcados_na_base))

    def _payload(item):
        detalhes_pedidos.append(item["id"])
        return {"parliamentarian_code": item["id"], "status": "Suplência"}

    monkeypatch.setattr(cam, "_build_payload_from_json", _payload)
    monkeypatch.setattr(cam, "_upsert_parliamentarian", lambda session, p: gravados.append(p))
    return gravados, detalhes_pedidos


def test_quem_saiu_da_lista_e_atualizado(monkeypatch) -> None:
    gravados, detalhes = _preparar(monkeypatch, na_lista=[1, 2], marcados_na_base=[1, 2, 3, 4])

    cam.parliamentarian()

    assert detalhes == [3, 4]
    assert [(p["parliamentarian_code"], p["status"]) for p in gravados] == [
        (1, "Exercício"),
        (2, "Exercício"),
        (3, "Suplência"),
        (4, "Suplência"),
    ]


def test_execucao_filtrada_nao_mexe_em_quem_ficou_de_fora(monkeypatch) -> None:
    """Com --uf/--partido/--id a lista é parcial: o resto não saiu do exercício."""
    gravados, detalhes = _preparar(monkeypatch, na_lista=[1], marcados_na_base=[1, 2, 3])

    cam.parliamentarian(uf="SP")

    assert detalhes == []
    assert [p["parliamentarian_code"] for p in gravados] == [1]


def test_lista_vazia_nao_derruba_todo_mundo(monkeypatch) -> None:
    """API fora do ar = lista vazia; não é sinal de que todos saíram."""
    gravados, detalhes = _preparar(monkeypatch, na_lista=[], marcados_na_base=[1, 2, 3])

    cam.parliamentarian()

    assert detalhes == []
    assert gravados == []
