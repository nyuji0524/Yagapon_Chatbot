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
    assert "Critical" in payload["comments"][0]["body"]
    assert "<!-- marker -->" in payload["body"]
