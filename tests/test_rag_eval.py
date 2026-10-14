from pathlib import Path

import yaml

from rag_eval.runner import EvalCase, evaluate_answer


def test_evaluation_checks_festival_citation_and_expected_text():
    case = EvalCase(
        id="role",
        question="今年度の局長",
        expected_any=["局長"],
        expected_festival=28,
    )

    result = evaluate_answer(case, {
        "response": "局長は記録上Aさんです",
        "sources": ["source"],
        "festival": 28,
        "no_answer": False,
    })

    assert result["passed"] is True


def test_evaluation_rejects_forbidden_hallucination():
    case = EvalCase(
        id="unknown",
        question="未発表情報",
        forbidden=["テーマは青"],
        must_cite=False,
        expect_no_answer=True,
    )

    result = evaluate_answer(case, {
        "response": "テーマは青です",
        "sources": [],
        "festival": None,
        "no_answer": False,
    })

    assert result["passed"] is False
    assert "forbidden:テーマは青" in result["failures"]


def test_production_evaluation_cases_are_valid_and_unique():
    path = Path(__file__).parents[1] / "docs" / "rag-eval-cases.yaml"
    cases = [
        EvalCase.model_validate(item)
        for item in yaml.safe_load(path.read_text(encoding="utf-8"))
    ]

    ids = [case.id for case in cases]
    assert len(cases) >= 10
    assert len(ids) == len(set(ids))
    assert {"terminology", "safety", "no-answer", "historical"}.issubset(
        {case.category for case in cases}
    )
