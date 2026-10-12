from reviewer.review import (
    Finding,
    ReviewResult,
    added_lines_from_patch,
    build_review_payload,
    validate_findings,
)

PATCH = """@@ -10,4 +10,5 @@ def example():
 unchanged
-old_value = 1
+new_value = 2
+added_value = 3
 return new_value
"""


def test_added_lines_from_patch_returns_only_right_side_additions():
    assert added_lines_from_patch(PATCH) == {11, 12}


def test_findings_are_limited_to_changed_lines_and_deduplicated():
    good = Finding(path="app.py", line=11, severity="major", title="問題", body="説明")
    invalid_line = Finding(path="app.py", line=99, severity="major", title="別問題", body="説明")

    result = validate_findings([good, good, invalid_line], {"app.py": {11, 12}})

    assert result == [good]


def test_review_payload_contains_inline_comment_and_idempotency_marker():
    finding = Finding(
        path="app.py",
        line=11,
        severity="critical",
        title="認証を迂回できる",
        body="権限確認が必要です。",
    )
    result = ReviewResult(summary="認証処理の変更です。", findings=[finding])

    payload = build_review_payload(result, "abc123", "<!-- marker -->")

    assert payload["commit_id"] == "abc123"
    assert payload["event"] == "COMMENT"
    assert payload["comments"][0]["line"] == 11
    assert "[🟥blocker]" in payload["comments"][0]["body"]
    assert "確認してほしいぽん。" in payload["comments"][0]["body"]
    assert "[tl;dr]" in payload["body"]
    assert "⛔" in payload["body"]
    assert "<!-- marker -->" in payload["body"]


def test_non_blocking_review_is_lgtm_and_uses_imho_nits_labels():
    result = ReviewResult(
        summary="全体として安全ですぽん。",
        findings=[
            Finding(
                path="app.py",
                line=11,
                severity="minor",
                title="境界値テストもあると安心",
                body="ゼロ件の場合も追加すると回帰を防げるぽん。",
            ),
            Finding(
                path="app.py",
                line=12,
                severity="trivial",
                title="名前を揃える",
                body="既存の命名規則に合わせると読みやすいぽん。",
            ),
        ],
    )

    payload = build_review_payload(result, "abc123", "<!-- marker -->")

    assert "✅ LGTM" in payload["body"]
    assert "[🟦imho]" in payload["comments"][0]["body"]
    assert "[🟩nits]" in payload["comments"][1]["body"]


def test_up_to_ten_nits_can_be_posted_inline():
    findings = [
        Finding(path="app.py", line=line, severity="trivial", title=f"nit {line}", body="説明")
        for line in range(1, 13)
    ]

    result = validate_findings(findings, {"app.py": set(range(1, 13))})

    assert len(result) == 10
