from reviewer.review import (
    Finding,
    ReviewResult,
    added_lines_from_patch,
    bounded_excerpt,
    build_review_context,
    build_review_payload,
    review_instruction,
    review_request,
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


def test_bounded_excerpt_marks_omission_and_keeps_file_tail():
    content = "A" * 50 + "SECTION-5.2\nSECTION-6\nSECTION-7"

    excerpt = bounded_excerpt(content, 40, "head file migration.md")

    assert excerpt.startswith("A" * 20)
    assert "[REVIEW INPUT OMITTED:" in excerpt
    assert "Do not treat either excerpt boundary as the file boundary" in excerpt
    assert excerpt.endswith("SECTION-6\nSECTION-7")


class _FakeGitHub:
    def __init__(self, head_content: str):
        self.head_content = head_content

    def get_file_text(self, repository: str, path: str, ref: str):
        if path == "docs/ai/ai-dev-migration.md" and ref == "head":
            return self.head_content
        return None


def test_review_context_does_not_present_a_truncated_patch_as_file_end():
    head_content = "\n".join(["# migration", *(f"line {i}" for i in range(2, 295)), "## 7"])
    pull_request = {
        "number": 65,
        "title": "migration",
        "body": "sections 5.2 through 7 are included",
        "head": {"sha": "head"},
        "base": {"sha": "base"},
    }
    files = [
        {
            "filename": "docs/ai/ai-dev-migration.md",
            "status": "added",
            "additions": 295,
            "deletions": 0,
            "patch": "@@ -0,0 +1,2 @@\n+# migration\n+line 2 cut off",
        }
    ]

    context, _ = build_review_context(_FakeGitHub(head_content), "owner/repo", pull_request, files)

    assert "GitHub PR patch (the API may truncate this field" in context
    assert "Head file content" in context
    assert context.endswith("## 7")
    assert "Never infer that a file\nends" in context


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


def test_review_payload_keeps_clean_model_summary_when_nothing_was_discarded():
    result = ReviewResult(summary="変更内容を確認し、問題はありませんぽん。", findings=[])

    payload = build_review_payload(result, "abc123", "<!-- marker -->")

    assert "変更内容を確認し、問題はありませんぽん。" in payload["body"]
    assert "✅ LGTM" in payload["body"]


def test_review_payload_discards_unsubstantiated_summary_when_no_findings_survive():
    result = ReviewResult(
        summary="重大な欠落があるためマージできませんぽん。",
        findings=[],
    )

    payload = build_review_payload(
        result,
        "abc123",
        "<!-- marker -->",
        discarded_findings=True,
    )

    assert "重大な欠落" not in payload["body"]
    assert "有効なインライン指摘はありませんでしたぽん。" in payload["body"]
    assert "✅ LGTM" in payload["body"]


def test_review_payload_does_not_duplicate_model_supplied_severity_labels():
    result = ReviewResult(
        summary="確認しましたぽん。",
        findings=[
            Finding(
                path="app.py",
                line=11,
                severity="minor",
                title="[🟦imho] [imho] 境界値テストを追加する",
                body="ゼロ件も確認すると安全だぽん。",
            ),
            Finding(
                path="app.py",
                line=12,
                severity="trivial",
                title="[🟩nits] [nits] 名前を揃える",
                body="命名規則を揃えると読みやすいぽん。",
            ),
        ],
    )

    payload = build_review_payload(result, "abc123", "<!-- marker -->")

    assert payload["comments"][0]["body"].count("[🟦imho]") == 1
    assert "[imho]" not in payload["comments"][0]["body"]
    assert payload["comments"][1]["body"].count("[🟩nits]") == 1
    assert "[nits]" not in payload["comments"][1]["body"]


def test_up_to_ten_nits_can_be_posted_inline():
    findings = [
        Finding(path="app.py", line=line, severity="trivial", title=f"nit {line}", body="説明")
        for line in range(1, 13)
    ]

    result = validate_findings(findings, {"app.py": set(range(1, 13))})

    assert len(result) == 10


def test_knowledge_profile_reviews_structure_instead_of_content_truth():
    instruction = review_instruction("knowledge-structure")
    request = review_request("knowledge-structure", "PR context")

    assert "not whether meeting statements" in instruction
    assert "front matter" in instruction
    assert "内容の真偽" in request
    assert "正本の配置" in request
    assert request.endswith("PR context")
