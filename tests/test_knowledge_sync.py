import yaml

from knowledge_sync.draft import (
    StructuredDraft,
    redact_credentials,
    render_source_draft,
    stable_filename,
)
from knowledge_sync.drive import DriveDocument


def document():
    return DriveDocument(
        file_id="drive-file-id",
        name="第28回 運用メモ",
        mime_type="application/vnd.google-apps.document",
        modified_time="2026-10-05T01:00:00Z",
        web_view_link="https://drive.google.com/file/d/drive-file-id/view",
        parents=["folder"],
    )


def structured():
    return StructuredDraft(
        summary="運用に関する確認待ちのメモです。",
        facts=["受付を実施したとの記載がある。"],
        decision_candidates=[],
        action_items=["担当者に実施日を確認する。"],
        unknowns=["正式決定か未確認。"],
        suggested_festival=28,
        suggested_knowledge_types=["years/28th/sources"],
    )


def test_credentials_are_redacted_before_model_input():
    text, count = redact_credentials("key=AIza12345678901234567890123456789012345")

    assert "AIza" not in text
    assert count == 1


def test_filename_is_stable_and_contains_drive_id():
    assert stable_filename(document()) == "drive-drive-file-id.md"


def test_rendered_document_is_always_unverified_draft():
    output = render_source_draft(document(), "原文", structured(), 0)
    front_matter = output[4:].split("\n---\n", 1)[0]
    metadata = yaml.safe_load(front_matter)

    assert metadata["status"] == "draft"
    assert metadata["type"] == "source"
    assert metadata["sources"][0]["locator"].endswith("drive-file-id")
    assert "確定情報や現在の推奨として扱いません" in output
