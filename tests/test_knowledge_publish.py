import json
from datetime import datetime, timezone

import pytest

from bot.knowledge_publish import (
    GitHubKnowledgePublisher,
    build_meeting_bundle,
    festival_number,
    publish_meeting_knowledge,
)


def test_festival_number_changes_at_end_of_september():
    assert festival_number(datetime(2025, 10, 1, tzinfo=timezone.utc)) == 27
    assert festival_number(datetime(2026, 9, 30, 14, 59, tzinfo=timezone.utc)) == 27
    assert festival_number(datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)) == 28


def test_meeting_bundle_requires_transcript_and_keeps_minutes_optional():
    root, files, manifest = build_meeting_bundle(
        session_id="20261010T075451Z-7f0cba20",
        guild_id=123,
        channel_name="全体会議部屋",
        mode="listen",
        transcript="[Speaker 1]: 提案します",
        summary="短い要約",
        minutes="詳細議事録",
        include_minutes=False,
    )

    assert root.startswith("sources/meetings/yagapon/28th/2026-10-10/")
    assert f"{root}/transcript.md" in files
    assert f"{root}/summary.md" in files
    assert f"{root}/minutes.md" not in files
    assert "status: draft" in files[f"{root}/transcript.md"]
    assert "[Speaker 1]: 提案します" in files[f"{root}/transcript.md"]
    assert manifest["festival"] == 28
    assert manifest["rawAudio"]["published"] is False


@pytest.mark.asyncio
async def test_publish_without_token_retains_complete_outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_OUTBOX", str(tmp_path))
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_GITHUB_TOKEN", raising=False)

    result = await publish_meeting_knowledge(
        session_id="20261010T075451Z-test",
        guild_id=123,
        channel_name="全体会議部屋",
        mode="listen",
        transcript="[Speaker 1]: 発言",
        summary="要約",
    )

    assert result is not None
    delivery = json.loads((result.outbox_dir / "delivery.json").read_text(encoding="utf-8"))
    assert delivery["status"] == "pending"
    assert list(result.outbox_dir.rglob("transcript.md"))
    assert list(result.outbox_dir.rglob("manifest.json"))


@pytest.mark.asyncio
async def test_sensitive_transcript_is_kept_local_and_blocked_from_github(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_OUTBOX", str(tmp_path))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GITHUB_TOKEN", "configured-token")

    result = await publish_meeting_knowledge(
        session_id="20261010T075451Z-sensitive",
        guild_id=123,
        channel_name="全体会議部屋",
        mode="listen",
        transcript="[Speaker 1]: password: very-secret-value",
    )

    delivery = json.loads((result.outbox_dir / "delivery.json").read_text(encoding="utf-8"))
    assert delivery["status"] == "blocked-sensitive-content"
    assert delivery["detectedPatterns"] == ["credential-assignment"]


def test_github_publisher_creates_one_commit_and_draft_pr():
    calls = []

    class FakePublisher(GitHubKnowledgePublisher):
        def _request(self, method, path, payload=None):
            calls.append((method, path, payload))
            if path.startswith("/pulls?"):
                return []
            if path.startswith("/git/ref/heads/"):
                return {"object": {"sha": "base"}}
            if path == "/git/commits/base":
                return {"tree": {"sha": "base-tree"}}
            if path == "/git/blobs":
                return {"sha": f"blob-{len(calls)}"}
            if path == "/git/trees":
                return {"sha": "tree"}
            if path == "/git/commits":
                return {"sha": "commit"}
            if path == "/pulls":
                return {"html_url": "https://github.example/pr/1"}
            return {}

    publisher = FakePublisher("token", "owner/repo")
    url = publisher.publish(
        branch="yagapon/meeting-session",
        title="source: meeting",
        body="review",
        files={"path/transcript.md": "text", "path/manifest.json": "{}"},
    )

    assert url == "https://github.example/pr/1"
    commit_calls = [call for call in calls if call[1] == "/git/commits"]
    assert len(commit_calls) == 1
    pull = next(payload for _, path, payload in calls if path == "/pulls")
    assert pull["draft"] is True
    assert pull["base"] == "main"


def test_github_publisher_reuses_existing_pull_request():
    class ExistingPublisher(GitHubKnowledgePublisher):
        def _request(self, method, path, payload=None):
            assert method == "GET"
            assert path.startswith("/pulls?")
            return [{"html_url": "https://github.example/pr/existing"}]

    publisher = ExistingPublisher("token", "owner/repo")

    assert publisher.publish(
        branch="yagapon/meeting-session",
        title="source: meeting",
        body="review",
        files={"path/transcript.md": "text"},
    ) == "https://github.example/pr/existing"
