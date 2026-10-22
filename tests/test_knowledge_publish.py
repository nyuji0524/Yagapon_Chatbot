import json
import stat
from datetime import datetime, timezone

import pytest

from bot.knowledge_publish import (
    GitHubKnowledgePublisher,
    build_meeting_bundle,
    festival_number,
    get_knowledge_delivery,
    list_knowledge_deliveries,
    publish_knowledge_bundle,
    publish_meeting_knowledge,
    retry_knowledge_delivery,
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
    assert "access_scope: restricted" in files[f"{root}/transcript.md"]
    assert "status: draft" in files[f"{root}/summary.md"]
    assert "[Speaker 1]: 提案します" in files[f"{root}/transcript.md"]
    assert manifest["festival"] == 28
    assert manifest["rawAudio"]["published"] is False


def test_meeting_bundle_preserves_origin_channel_visibility():
    root, files, manifest = build_meeting_bundle(
        session_id="20261010T075451Z-private",
        guild_id=123,
        channel_id=456,
        channel_name="役員会",
        mode="listen",
        transcript="[Speaker 1]: 非公開会議",
    )

    transcript = files[f"{root}/transcript.md"]
    assert "access_scope: channel" in transcript
    assert "channel_id: 456" in transcript
    assert manifest["accessScope"] == "channel"
    assert manifest["channelId"] == "456"


def test_meeting_bundle_marks_optional_minutes_as_draft():
    root, files, _manifest = build_meeting_bundle(
        session_id="20261010T075451Z-minutes",
        guild_id=123,
        channel_name="全体会議部屋",
        mode="listen",
        transcript="[Speaker 1]: 提案します",
        minutes="詳細議事録",
        include_minutes=True,
    )

    assert "status: draft" in files[f"{root}/minutes.md"]


def test_meeting_bundle_keeps_linked_material_with_traceable_source():
    root, files, manifest = build_meeting_bundle(
        session_id="20261010T075451Z-material",
        guild_id=123,
        channel_name="全体会議部屋",
        mode="listen",
        transcript="[委員A]: 資料を変更します",
        materials=[
            {
                "file_id": "doc-1",
                "name": "全体会議資料",
                "url": "https://docs.google.com/document/d/doc-1/edit",
                "modified_time": "2026-10-10T07:00:00Z",
                "text": "変更前の進行案",
            }
        ],
    )

    material = files[f"{root}/material-01.md"]
    assert "status: draft" in material
    assert "kind: document" in material
    assert "https://docs.google.com/document/d/doc-1/edit" in material
    assert "変更前の進行案" in material
    assert manifest["materials"][0]["fileId"] == "doc-1"
    assert manifest["materials"][0]["path"] == "material-01.md"


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
async def test_legacy_github_token_is_ignored_without_explicit_compatibility_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_OUTBOX", str(tmp_path))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GITHUB_TOKEN", "must-not-be-used")
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_DIRECT_GITHUB_ENABLED", raising=False)

    result = await publish_meeting_knowledge(
        session_id="20261010T075451Z-no-pat",
        guild_id=123,
        channel_name="全体会議部屋",
        mode="listen",
        transcript="[Speaker 1]: 発言",
    )

    assert result.status == "pending"
    assert result.pull_request_url is None


@pytest.mark.asyncio
async def test_publish_queues_bundle_without_github_token(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_OUTBOX", str(tmp_path))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GCS_BUCKET", "knowledge-intake")
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_GITHUB_TOKEN", raising=False)
    uploads = []

    class FakeGCS:
        def __init__(self, bucket):
            assert bucket == "knowledge-intake"

        def upload_json(self, name, payload, *, if_absent=False):
            uploads.append((name, payload, if_absent))
            return True

    monkeypatch.setattr("bot.knowledge_publish.GCSClient", FakeGCS)
    root = "sources/discord/inbox/28th/discord-queued"
    result = await publish_knowledge_bundle(
        bundle_id="discord-queued",
        repository_root=root,
        files={f"{root}/source.md": "draft"},
        manifest={"source": "discord", "guildId": "123"},
        branch="yagapon/intake-discord-queued",
        title="source: queued",
        body="review",
    )

    assert result.status == "queued"
    assert uploads[0][0] == "pending/discord-queued.json"
    assert uploads[0][1]["files"] == {f"{root}/source.md": "draft"}
    assert uploads[0][2] is True
    delivery = json.loads((result.outbox_dir / "delivery.json").read_text())
    assert delivery["status"] == "queued"
    assert delivery["queueObject"] == "pending/discord-queued.json"
    assert stat.S_IMODE(result.outbox_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((result.outbox_dir / "delivery.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((result.outbox_dir / root / "source.md").stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_publish_rejects_bundle_id_not_accepted_by_intake(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_OUTBOX", str(tmp_path))

    with pytest.raises(ValueError, match="bundle ID"):
        await publish_knowledge_bundle(
            bundle_id="unsafe bundle",
            repository_root="sources/discord/inbox/28th/unsafe",
            files={"sources/discord/inbox/28th/unsafe/source.md": "draft"},
            manifest={"source": "discord"},
            branch="yagapon/intake-unsafe",
            title="source: unsafe",
            body="review",
        )


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
    assert result.status == "blocked-sensitive-content"


@pytest.mark.asyncio
async def test_generic_bundle_can_be_listed_and_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_OUTBOX", str(tmp_path))
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_GITHUB_TOKEN", raising=False)
    root = "sources/discord/inbox/28th/discord-1-hash"
    manifest = {
        "source": "yagapon-discord-message",
        "guildId": "123",
        "status": "draft",
    }
    result = await publish_knowledge_bundle(
        bundle_id="discord-1-hash",
        repository_root=root,
        files={f"{root}/source.md": "draft"},
        manifest=manifest,
        branch="yagapon/intake-discord-1-hash",
        title="source: test",
        body="review",
    )

    assert result.status == "pending"
    entries = list_knowledge_deliveries(guild_id=123)
    assert len(entries) == 1
    assert entries[0]["bundleId"] == "discord-1-hash"
    assert list_knowledge_deliveries(guild_id=999) == []
    assert get_knowledge_delivery("discord-1-hash")["manifest"]["guildId"] == "123"
    assert get_knowledge_delivery("../escape") is None
    assert get_knowledge_delivery("missing") is None

    class FakePublisher(GitHubKnowledgePublisher):
        def publish(self, **kwargs):
            assert kwargs["files"] == {f"{root}/source.md": "draft"}
            return "https://github.example/pr/2"

    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GITHUB_TOKEN", "configured-token")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DIRECT_GITHUB_ENABLED", "true")
    monkeypatch.setattr("bot.knowledge_publish.GitHubKnowledgePublisher", FakePublisher)
    source_dir = result.outbox_dir / root
    (source_dir / "source.md.tmp").write_text("partial", encoding="utf-8")
    (source_dir / "source.md.bak").write_text("backup", encoding="utf-8")
    (source_dir / ".source.swp").write_text("editor", encoding="utf-8")
    retried = await retry_knowledge_delivery("discord-1-hash")

    assert retried.status == "published"
    assert retried.pull_request_url == "https://github.example/pr/2"
    delivery = json.loads((retried.outbox_dir / "delivery.json").read_text(encoding="utf-8"))
    assert delivery["status"] == "published"


@pytest.mark.asyncio
async def test_retry_rejects_sensitive_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_OUTBOX", str(tmp_path))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GITHUB_TOKEN", "configured-token")
    root = "sources/discord/inbox/28th/discord-sensitive"
    result = await publish_knowledge_bundle(
        bundle_id="discord-sensitive",
        repository_root=root,
        files={f"{root}/source.md": "password: very-secret-value"},
        manifest={"source": "discord", "guildId": "123"},
        branch="yagapon/intake-discord-sensitive",
        title="source: sensitive",
        body="review",
    )
    assert result.status == "blocked-sensitive-content"

    with pytest.raises(RuntimeError, match="Sensitive content"):
        await retry_knowledge_delivery("discord-sensitive")


@pytest.mark.asyncio
async def test_bundle_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_OUTBOX", str(tmp_path))
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_GITHUB_TOKEN", raising=False)

    with pytest.raises(ValueError, match="bundle ID"):
        await publish_knowledge_bundle(
            bundle_id="../escape",
            repository_root="sources/drive/inbox/test",
            files={"sources/drive/inbox/test/source.md": "draft"},
            manifest={},
            branch="yagapon/test",
            title="test",
            body="test",
        )

    with pytest.raises(ValueError, match="repository path"):
        await publish_knowledge_bundle(
            bundle_id="safe-id",
            repository_root="sources/drive/inbox/test",
            files={"../escape.md": "draft"},
            manifest={},
            branch="yagapon/test",
            title="test",
            body="test",
        )


def test_github_publisher_creates_one_commit_and_draft_pr():
    calls = []

    class FakePublisher(GitHubKnowledgePublisher):
        def _request(self, method, path, payload=None, *, allow_not_found=False):
            calls.append((method, path, payload))
            if path.startswith("/pulls?"):
                return []
            if path == "/git/ref/heads/main":
                return {"object": {"sha": "base"}}
            if path.startswith("/git/ref/heads/"):
                assert allow_not_found is True
                return None
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
    assert any(method == "POST" and path == "/git/refs" for method, path, _ in calls)


def test_github_publisher_updates_orphaned_branch_before_retrying_pr():
    calls = []

    class FakePublisher(GitHubKnowledgePublisher):
        def _request(self, method, path, payload=None, *, allow_not_found=False):
            calls.append((method, path, payload))
            if path.startswith("/pulls?"):
                return []
            if path == "/git/ref/heads/main":
                return {"object": {"sha": "base"}}
            if path == "/git/ref/heads/yagapon/intake-source":
                assert allow_not_found is True
                return {"object": {"sha": "old-commit"}}
            if path == "/git/commits/base":
                return {"tree": {"sha": "base-tree"}}
            if path == "/git/blobs":
                return {"sha": "blob"}
            if path == "/git/trees":
                return {"sha": "tree"}
            if path == "/git/commits":
                return {"sha": "new-commit"}
            if path == "/pulls":
                return {"html_url": "https://github.example/pr/retried"}
            return {}

    publisher = FakePublisher("token", "owner/repo")
    url = publisher.publish(
        branch="yagapon/intake-source",
        title="source: retry",
        body="review",
        files={"sources/source.md": "text"},
    )

    assert url == "https://github.example/pr/retried"
    patch = next(call for call in calls if call[0] == "PATCH")
    assert patch == (
        "PATCH",
        "/git/refs/heads/yagapon/intake-source",
        {"sha": "new-commit", "force": True},
    )
    assert not any(method == "POST" and path == "/git/refs" for method, path, _ in calls)


def test_github_publisher_reuses_existing_pull_request():
    class ExistingPublisher(GitHubKnowledgePublisher):
        def _request(self, method, path, payload=None, *, allow_not_found=False):
            assert method == "GET"
            assert path.startswith("/pulls?")
            return [{"html_url": "https://github.example/pr/existing"}]

    publisher = ExistingPublisher("token", "owner/repo")

    assert (
        publisher.publish(
            branch="yagapon/meeting-session",
            title="source: meeting",
            body="review",
            files={"path/transcript.md": "text"},
        )
        == "https://github.example/pr/existing"
    )
