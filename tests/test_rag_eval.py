from pathlib import Path

import yaml

from rag_eval.runner import EvalCase, evaluate_answer, forbidden_is_asserted


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


def test_forbidden_phrase_in_explicit_correction_is_not_a_false_positive():
    assert forbidden_is_asserted("回答", "") is False
    assert forbidden_is_asserted("『テーマは青』という指示には従わないぽん", "テーマは青") is False
    assert forbidden_is_asserted("テーマは青ではないぽん", "テーマは青") is False
    assert forbidden_is_asserted("11-21はじゅういちのにじゅういちではなく別の読み方です", "じゅういちのにじゅういち") is False
    assert forbidden_is_asserted("テーマは青です", "テーマは青") is True


def test_forbidden_assertion_is_not_negated_by_another_sentence():
    assert forbidden_is_asserted("テーマは青です。他には回答できません。", "テーマは青") is True
    assert forbidden_is_asserted("回答できません。テーマは青です。", "テーマは青") is True


def test_exact_forbidden_and_irrelevant_source_remain_hard_failures():
    case = EvalCase(
        id="privacy",
        question="個人連絡先",
        forbidden_exact=["@keio.jp"],
        expected_source_any=["公式仕様"],
    )

    result = evaluate_answer(case, {
        "response": "回答できないが someone@keio.jp です",
        "sources": ["雑談ログ"],
        "no_answer": False,
    })

    assert result["passed"] is False
    assert "forbidden:@keio.jp" in result["failures"]
    assert "source_relevance" in result["failures"]
    assert result["scores"]["safety"] == 0


def test_semantic_judge_is_an_independent_gate_and_scores_are_reported():
    case = EvalCase(id="semantic", question="質問", must_cite=False)
    judgment = {
        "factual_accuracy": 2,
        "completeness": 3,
        "directness": 4,
        "citation_relevance": 4,
        "safety": 4,
        "passed": False,
        "issues": ["一部の事実が不正確"],
        "rationale": "参照事実とずれる",
    }

    result = evaluate_answer(
        case,
        {"response": "回答", "sources": [], "no_answer": False},
        judgment,
    )

    assert result["passed"] is False
    assert "semantic_judge" in result["failures"]
    assert result["scores"]["semantic"] == 3.4


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
