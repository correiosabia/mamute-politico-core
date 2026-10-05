from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "test_senado_speeches_module",
    REPO_ROOT / "mamute_scrappers/senado_crawler/speechs_transcipts.py",
)
senado_speeches = importlib.util.module_from_spec(spec)
spec.loader.exec_module(senado_speeches)


def test_portuguese_speech_is_not_binary() -> None:
    text = (
        "Sr. Presidente, Sras. e Srs. Deputados, venho à tribuna hoje (12/09) falar "
        "sobre a saúde pública em Pernambuco: 30% dos municípios não têm UTI. "
    ) * 20
    assert senado_speeches._looks_like_binary(text) is False


def test_docx_pasted_as_text_is_binary() -> None:
    # Começo real do discurso 271305 em prod: .docx inteiro como speech_text.
    text = (
        "DISCURSO NA ÍNTEGRA ENCAMINHADO PELO SR. DEPUTADO (SEM REGISTRO TAQUIGRÁFICO). "
        "PK!?Gj?[Content_Types].xml ?(??TKO1???6???c ??????????6????e?l?l??5}Lo???d?w"
        "??f????X7???x?>?I9?J? k@1???????0?a.fD?IJ?3?f>??J?c???q*??_j ??y??;G)???{?B?"
    ) * 30
    assert senado_speeches._looks_like_binary(text) is True


def test_bytes_replaced_by_question_marks_is_binary() -> None:
    text = "Discurso encaminhado. " + "??x?>?I9?J? k@1???????0?a.fD?" * 200
    assert senado_speeches._looks_like_binary(text) is True


def test_empty_text_is_not_binary() -> None:
    assert senado_speeches._looks_like_binary("") is False
