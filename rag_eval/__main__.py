"""Run a YAML evaluation set against a deployed YagaPon API."""

import argparse
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

from rag_eval.runner import EvalCase, evaluate_answer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    token = os.environ.get("YAGAPON_API_TOKEN", "")
    if not token:
        raise SystemExit("YAGAPON_API_TOKEN is required")
    cases = [EvalCase.model_validate(item) for item in yaml.safe_load(args.cases.read_text(encoding="utf-8"))]
    results = []
    for case in cases:
        body = json.dumps({"guild_id": args.guild_id, "query": case.question}).encode()
        request = urllib.request.Request(
            args.base_url.rstrip("/") + "/ask",
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            payload = json.load(response)
        results.append(evaluate_answer(case, payload))

    passed = sum(result["passed"] for result in results)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": passed,
        "total": len(results),
        "pass_rate": round(passed / len(results), 3) if results else 0,
        "results": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
