"""Run a YAML evaluation set against a deployed YagaPon API."""

import argparse
import json
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

from rag_eval.runner import EvalCase, evaluate_answer

DEFAULT_REQUEST_TIMEOUT_SECONDS = 180.0
SCORE_AXES = ("content", "routing", "grounding", "safety", "performance", "semantic")


class RequestFailure(RuntimeError):
    """A safely classified API failure whose detail must not enter reports."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"RAG API request failed ({kind})")
        self.kind = kind


def _positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("request timeout must be a number") from exc
    if not 1 <= timeout <= 900:
        raise argparse.ArgumentTypeError("request timeout must be between 1 and 900 seconds")
    return timeout


def _url_error_kind(exc: urllib.error.URLError) -> str:
    reason = exc.reason
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    return "connection_error"


def post_json(
    url: str,
    token: str,
    payload: dict,
    *,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            decoded = json.load(response)
    except urllib.error.HTTPError as exc:
        # Error bodies can echo prompts or server internals, so never read or
        # copy them into an exception or the public Actions report.
        raise RequestFailure("http_error") from exc
    except urllib.error.URLError as exc:
        raise RequestFailure(_url_error_kind(exc)) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RequestFailure("timeout") from exc
    except (ConnectionError, OSError) as exc:
        raise RequestFailure("connection_error") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RequestFailure("invalid_response") from exc
    if not isinstance(decoded, dict):
        raise RequestFailure("invalid_response")
    return decoded


def _build_report(
    results: list[dict],
    *,
    planned_total: int,
    failure: dict[str, str] | None = None,
) -> dict:
    passed = sum(bool(result["passed"]) for result in results)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "failed" if failure else "completed",
        "passed": passed,
        "total": planned_total,
        "completed": len(results),
        "pass_rate": round(passed / planned_total, 3) if planned_total else 0,
        "average_scores": {},
        "results": results,
    }
    for axis in SCORE_AXES:
        values = [result["scores"][axis] for result in results if result["scores"][axis] is not None]
        report["average_scores"][axis] = round(sum(values) / len(values), 2) if values else None
    if failure is not None:
        # This allowlisted structure is the complete failure contract. Never
        # add exception strings, URLs, prompts, answers, sources or tokens.
        report["failure"] = {
            "case_id": str(failure["case_id"]),
            "stage": str(failure["stage"]),
            "kind": str(failure["kind"]),
        }
    return report


def _write_report(report: dict, output: Path | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if output is None:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, output)


def _request_failure(exc: Exception) -> str:
    return exc.kind if isinstance(exc, RequestFailure) else "unexpected_error"


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-pass-rate", type=float, default=1.0)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--request-timeout-seconds",
        type=_positive_timeout,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="timeout for each /ask or /rag/evaluate request (default: 180)",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("YAGAPON_API_TOKEN", "")
    if not token:
        raise SystemExit("YAGAPON_API_TOKEN is required")
    cases = [EvalCase.model_validate(item) for item in yaml.safe_load(args.cases.read_text(encoding="utf-8"))]
    results: list[dict] = []
    failure: dict[str, str] | None = None
    for case in cases:
        started = time.monotonic()
        try:
            payload = post_json(
                args.base_url.rstrip("/") + "/ask",
                token,
                {"guild_id": args.guild_id, "query": case.question},
                timeout=args.request_timeout_seconds,
            )
        except Exception as exc:
            failure = {
                "case_id": case.id,
                "stage": "ask",
                "kind": _request_failure(exc),
            }
            break

        payload["latency_ms"] = int((time.monotonic() - started) * 1000)
        judgment = None
        if case.semantic_judge:
            try:
                judgment = post_json(
                    args.base_url.rstrip("/") + "/rag/evaluate",
                    token,
                    {
                        "question": case.question,
                        "answer": payload.get("response", ""),
                        "sources": payload.get("sources", []),
                        "reference_facts": case.reference_facts,
                        "expected_no_answer": case.expect_no_answer,
                        "category": case.category,
                    },
                    timeout=args.request_timeout_seconds,
                )
            except Exception as exc:
                failure = {
                    "case_id": case.id,
                    "stage": "judge",
                    "kind": _request_failure(exc),
                }
                break
        try:
            results.append(evaluate_answer(case, payload, judgment))
        except Exception:
            failure = {
                "case_id": case.id,
                "stage": "evaluate",
                "kind": "unexpected_error",
            }
            break

    report = _build_report(
        results,
        planned_total=len(cases),
        failure=failure,
    )
    _write_report(report, args.output)

    if failure is not None:
        raise SystemExit(f"RAG evaluation stopped after {failure['stage']} request failure ({failure['kind']})")
    if args.baseline and args.baseline.exists():
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        if report["pass_rate"] < float(baseline.get("pass_rate", 0)):
            raise SystemExit("RAG evaluation regressed below baseline")
    if report["pass_rate"] < args.min_pass_rate:
        raise SystemExit("RAG evaluation did not meet minimum pass rate")


if __name__ == "__main__":
    main()
