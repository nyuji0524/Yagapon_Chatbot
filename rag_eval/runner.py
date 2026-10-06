"""Evaluation cases and deterministic answer checks."""

from pydantic import BaseModel, Field


class EvalCase(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=4000)
    expected_any: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    expected_festival: int | None = None
    must_cite: bool = True
    expect_no_answer: bool = False


def evaluate_answer(case: EvalCase, response: dict) -> dict:
    answer = str(response.get("response") or "")
    sources = response.get("sources") or []
    failures = []
    if case.expected_any and not any(value.casefold() in answer.casefold() for value in case.expected_any):
        failures.append("expected_any")
    matched_forbidden = [value for value in case.forbidden if value.casefold() in answer.casefold()]
    if matched_forbidden:
        failures.append("forbidden:" + ",".join(matched_forbidden))
    if case.expected_festival is not None and response.get("festival") != case.expected_festival:
        failures.append("festival")
    if case.must_cite and not sources:
        failures.append("citations")
    if bool(response.get("no_answer")) != case.expect_no_answer:
        failures.append("no_answer")
    return {
        "id": case.id,
        "passed": not failures,
        "failures": failures,
        "source_count": len(sources),
        "festival": response.get("festival"),
        "no_answer": bool(response.get("no_answer")),
        "query_id": response.get("query_id"),
    }
