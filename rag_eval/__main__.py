"""Run a YAML evaluation set against a deployed YagaPon API."""

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

from rag_eval.runner import EvalCase, evaluate_answer


def post_json(url: str, token: str, payload: dict, *, timeout: int = 120) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"RAG API request failed: HTTP {exc.code}: {detail}") from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-pass-rate", type=float, default=1.0)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()

    token = os.environ.get("YAGAPON_API_TOKEN", "")
    if not token:
        raise SystemExit("YAGAPON_API_TOKEN is required")
    cases = [EvalCase.model_validate(item) for item in yaml.safe_load(args.cases.read_text(encoding="utf-8"))]
    results = []
    for case in cases:
        started = time.monotonic()
        payload = post_json(
            args.base_url.rstrip("/") + "/ask",
            token,
            {"guild_id": args.guild_id, "query": case.question},
        )
        payload["latency_ms"] = int((time.monotonic() - started) * 1000)
        judgment = None
        if case.semantic_judge:
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
            )
        results.append(evaluate_answer(case, payload, judgment))

    passed = sum(result["passed"] for result in results)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": passed,
        "total": len(results),
        "pass_rate": round(passed / len(results), 3) if results else 0,
        "average_scores": {
            axis: round(
                sum(result["scores"][axis] for result in results if result["scores"][axis] is not None)
                / max(1, sum(result["scores"][axis] is not None for result in results)),
                2,
            )
            for axis in ("content", "routing", "grounding", "safety", "performance", "semantic")
        },
        "results": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    if args.baseline and args.baseline.exists():
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        if report["pass_rate"] < float(baseline.get("pass_rate", 0)):
            raise SystemExit("RAG evaluation regressed below baseline")
    if report["pass_rate"] < args.min_pass_rate:
        raise SystemExit("RAG evaluation did not meet minimum pass rate")


if __name__ == "__main__":
    main()
