from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.rag_judge import QualityJudgment, judge_answer_quality


async def test_judge_uses_structured_reference_grounded_output():
    expected = QualityJudgment(
        factual_accuracy=4,
        completeness=4,
        directness=4,
        citation_relevance=4,
        safety=4,
        passed=True,
        issues=[],
        rationale="参照事実と一致",
    )
    generated = AsyncMock(return_value=SimpleNamespace(parsed=expected, usage_metadata=None))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generated)))

    result = await judge_answer_quality(
        client,
        {
            "question": "11-21の読みは？",
            "answer": "いちいちのにーいち",
            "sources": ["教室名の読み方"],
            "reference_facts": ["11-21はいちいちのにーいちと読む"],
            "expected_no_answer": False,
        },
    )

    assert result == expected
    config = generated.await_args.kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema == QualityJudgment


async def test_judge_recomputes_pass_from_axis_thresholds():
    unsafe_model_result = QualityJudgment(
        factual_accuracy=4,
        completeness=4,
        directness=4,
        citation_relevance=4,
        safety=2,
        passed=True,
        issues=["個人情報を含む"],
        rationale="安全性違反",
    )
    generated = AsyncMock(return_value=SimpleNamespace(parsed=unsafe_model_result, usage_metadata=None))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generated)))

    result = await judge_answer_quality(client, {"question": "連絡先", "answer": "..."})

    assert result.passed is False
