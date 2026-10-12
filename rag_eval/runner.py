"""Evaluation cases and deterministic answer checks."""

from pydantic import BaseModel, Field


class EvalCase(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=4000)
    expected_any: list[str] = Field(default_factory=list)
    expected_all: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    expected_festival: int | None = None
    must_cite: bool = True
    expect_no_answer: bool = False
    min_sources: int = Field(default=0, ge=0)
    max_latency_ms: int | None = Field(default=None, ge=1)
    category: str = "general"


def evaluate_answer(case: EvalCase, response: dict) -> dict:
    answer = str(response.get("response") or "")
    sources = response.get("sources") or []
    failures = []
    if case.expected_any and not any(value.casefold() in answer.casefold() for value in case.expected_any):
        failures.append("expected_any")
    if case.expected_all and not all(value.casefold() in answer.casefold() for value in case.expected_all):
        failures.append("expected_all")
    matched_forbidden = [value for value in case.forbidden if value.casefold() in answer.casefold()]
    if matched_forbidden:
        failures.append("forbidden:" + ",".join(matched_forbidden))
    if case.expected_festival is not None and response.get("festival") != case.expected_festival:
        failures.append("festival")
    if case.must_cite and not sources:
        failures.append("citations")
    if bool(response.get("no_answer")) != case.expect_no_answer:
        failures.append("no_answer")
    if len(sources) < case.min_sources:
        failures.append("min_sources")
    latency_ms = int(response.get("latency_ms") or 0)
    if case.max_latency_ms is not None and latency_ms > case.max_latency_ms:
        failures.append("latency")
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
    }
