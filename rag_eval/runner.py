"""Evaluation cases and deterministic, explainable answer checks."""

import re

from pydantic import BaseModel, Field


class EvalCase(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=4000)
    expected_any: list[str] = Field(default_factory=list)
    expected_all: list[str] = Field(default_factory=list)
    reference_facts: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    forbidden_exact: list[str] = Field(default_factory=list)
    expected_festival: int | None = None
    must_cite: bool = True
    expect_no_answer: bool = False
    min_sources: int = Field(default=0, ge=0)
    expected_source_any: list[str] = Field(default_factory=list)
    max_latency_ms: int | None = Field(default=None, ge=1)
    category: str = "general"
    semantic_judge: bool = True


NEGATION_PATTERN = re.compile(
    r"(?:ではな(?:い|く)|ではありません|ではございません|じゃな(?:い|く)|"
    r"とは読(?:まない|みません)|読(?:まない|みません)|"
    r"誤り|間違|否定|従(?:わない|いません|わず)|回答できない|答え(?:ない|ません))"
)


def forbidden_is_asserted(answer: str, phrase: str) -> bool:
    """Return false when a forbidden phrase only appears in a nearby correction."""
    folded_answer = answer.casefold()
    folded_phrase = phrase.casefold()
    if not folded_phrase:
        return False
    start = 0
    while True:
        index = folded_answer.find(folded_phrase, start)
        if index < 0:
            return False
        tail = folded_answer[index + len(folded_phrase):index + len(folded_phrase) + 40]
        head = folded_answer[max(0, index - 24):index]
        if not (NEGATION_PATTERN.search(head) or NEGATION_PATTERN.search(tail)):
            return True
        start = index + len(folded_phrase)


def _score(passed: int, total: int) -> int:
    if total == 0:
        return 4
    return round(4 * passed / total)


def evaluate_answer(case: EvalCase, response: dict, judgment: dict | None = None) -> dict:
    answer = str(response.get("response") or "")
    folded_answer = answer.casefold()
    sources = [str(source) for source in (response.get("sources") or [])]
    folded_sources = "\n".join(sources).casefold()
    failures: list[str] = []
    content_checks = 0
    content_passed = 0
    if case.expected_any:
        content_checks += 1
        if any(value.casefold() in folded_answer for value in case.expected_any):
            content_passed += 1
        else:
            failures.append("expected_any")
    if case.expected_all:
        content_checks += len(case.expected_all)
        matched_all = [value for value in case.expected_all if value.casefold() in folded_answer]
        content_passed += len(matched_all)
        if len(matched_all) != len(case.expected_all):
            failures.append("expected_all")
    matched_forbidden = [value for value in case.forbidden if forbidden_is_asserted(answer, value)]
    matched_forbidden.extend(
        value for value in case.forbidden_exact if value.casefold() in folded_answer
    )
    if matched_forbidden:
        failures.append("forbidden:" + ",".join(matched_forbidden))

    routing_checks = 1 + int(case.expected_festival is not None)
    routing_passed = 0
    if bool(response.get("no_answer")) == case.expect_no_answer:
        routing_passed += 1
    else:
        failures.append("no_answer")
    if case.expected_festival is not None:
        if response.get("festival") == case.expected_festival:
            routing_passed += 1
        else:
            failures.append("festival")

    grounding_checks = int(case.must_cite) + int(case.min_sources > 0) + int(bool(case.expected_source_any))
    grounding_passed = 0
    if case.must_cite:
        if sources:
            grounding_passed += 1
        else:
            failures.append("citations")
    if case.min_sources > 0:
        if len(sources) >= case.min_sources:
            grounding_passed += 1
        else:
            failures.append("min_sources")
    if case.expected_source_any:
        if any(term.casefold() in folded_sources for term in case.expected_source_any):
            grounding_passed += 1
        else:
            failures.append("source_relevance")

    latency_ms = int(response.get("latency_ms") or 0)
    performance_score = 4
    if case.max_latency_ms is not None and latency_ms > case.max_latency_ms:
        failures.append("latency")
        performance_score = max(0, round(4 * case.max_latency_ms / max(latency_ms, 1)))

    semantic_score = None
    if judgment is not None:
        axes = [
            int(judgment.get(name, 0))
            for name in (
                "factual_accuracy",
                "completeness",
                "directness",
                "citation_relevance",
                "safety",
            )
        ]
        semantic_score = round(sum(axes) / len(axes), 2)
        if not judgment.get("passed"):
            failures.append("semantic_judge")

    safety_score = 0 if matched_forbidden else 4
    scores = {
        "content": _score(content_passed, content_checks),
        "routing": _score(routing_passed, routing_checks),
        "grounding": _score(grounding_passed, grounding_checks),
        "safety": safety_score,
        "performance": performance_score,
        "semantic": semantic_score,
    }
    return {
        "id": case.id,
        "passed": not failures,
        "failures": failures,
        "source_count": len(sources),
        "festival": response.get("festival"),
        "no_answer": bool(response.get("no_answer")),
        "query_id": response.get("query_id"),
        "category": case.category,
        "latency_ms": latency_ms,
        "scores": scores,
        "judgment": judgment,
    }
