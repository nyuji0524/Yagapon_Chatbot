import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.meeting_backfill import backfill_session
from bot.recording import save_transcript_provenance
from bot.recording_publication import UnsafeRecordingPublication


class _Config:
    def __init__(self, *, ignored: bool = False):
        self.ignored = ignored

    def get_corpus(self, _guild_id: int) -> str:
        return "fileSearchStores/test"

    def is_ignored(self, _guild_id: int, _channel_id: int) -> bool:
        return self.ignored


def _source(tmp_path: Path) -> tuple[Path, Path]:
    session = tmp_path / "recordings" / "123" / "safe-session"
    session.mkdir(parents=True)
    metadata = {
        "guild_id": "123",
        "channel_id": "456",
        "channel_name": "全体会議",
        "category_id": "789",
        "category_name": "会議",
        "mode": "listen",
        "status": "delivered_recording_retained",
        "started_at": "2026-10-01T00:00:00+00:00",
        "transcript_complete": True,
        "capture_integrity_failed": False,
        "transcription_failed": False,
        "external_export_expected": True,
        "external_export_reason": "guildwide-channel-audience",
    }
    (session / "metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    transcript = tmp_path / "transcripts" / "123" / "safe-session.md"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("# 文字起こし\n\n[Speaker 1]: 完了", encoding="utf-8")
    save_transcript_provenance(
        transcript,
        guild_id=123,
        session_id="safe-session",
        mode="listen",
        transcript_complete=True,
        capture_integrity_failed=False,
        status="final_reprocess_completed",
    )
    return session, transcript


def _environment(tmp_path: Path, monkeypatch, *, ignored: bool = False):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    monkeypatch.setattr(
        "bot.meeting_backfill.ConfigManager",
        lambda: _Config(ignored=ignored),
    )
    corpus = object()
    monkeypatch.setattr("bot.meeting_backfill.CorpusManager", lambda: corpus)
    upload_transcript = AsyncMock(return_value="https://docs/transcript")
    upload_minutes = AsyncMock(return_value="https://docs/minutes")
    index = AsyncMock(
        return_value=SimpleNamespace(
            transcript_document="transcript",
            minutes_document=None,
        )
    )
    monkeypatch.setattr("bot.meeting_backfill.upload_transcript", upload_transcript)
    monkeypatch.setattr("bot.meeting_backfill.upload_minutes", upload_minutes)
    monkeypatch.setattr("bot.meeting_backfill.index_meeting_artifacts", index)
    return upload_transcript, upload_minutes, index


@pytest.mark.asyncio
async def test_backfill_accepts_only_complete_export_approved_source(
    tmp_path,
    monkeypatch,
):
    _source(tmp_path)
    upload_transcript, upload_minutes, index = _environment(tmp_path, monkeypatch)

    result = await backfill_session(123, "safe-session")

    assert result["transcript_indexed"] is True
    upload_transcript.assert_awaited_once()
    upload_minutes.assert_not_awaited()
    index.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_accepts_completed_replay_only_with_matching_provenance(
    tmp_path,
    monkeypatch,
):
    _source(tmp_path)
    replay_id = "replay-safe"
    replay_dir = tmp_path / "replays" / replay_id
    replay_dir.mkdir(parents=True)
    replay_transcript = tmp_path / "transcripts" / "123" / f"{replay_id}.md"
    replay_transcript.write_text("# 再処理文字起こし", encoding="utf-8")
    save_transcript_provenance(
        replay_transcript,
        guild_id=123,
        session_id=replay_id,
        mode="recording-replay",
        transcript_complete=True,
        capture_integrity_failed=False,
        status="final_reprocess_completed",
    )
    minutes = replay_dir / "minutes.md"
    minutes.write_text("# 再処理議事録", encoding="utf-8")
    (replay_dir / "job.json").write_text(
        json.dumps(
            {
                "id": replay_id,
                "source_session_id": "safe-session",
                "source_guild_id": 123,
                "target_guild_id": 123,
                "status": "completed",
                "stage": "completed",
                "updated_at": "2026-10-24T00:00:00+00:00",
                "transcript_path": str(replay_transcript),
                "minutes_path": str(minutes),
                "drive_transcript_url": "https://docs/replay-transcript",
                "drive_minutes_url": "https://docs/replay-minutes",
            }
        ),
        encoding="utf-8",
    )
    upload_transcript, upload_minutes, index = _environment(tmp_path, monkeypatch)

    result = await backfill_session(123, "safe-session")

    upload_transcript.assert_not_awaited()
    upload_minutes.assert_not_awaited()
    assert index.await_args.kwargs["transcript"] == "# 再処理文字起こし"
    assert index.await_args.kwargs["minutes"] == "# 再処理議事録"
    assert result["transcript_url"] == "https://docs/replay-transcript"


@pytest.mark.asyncio
async def test_backfill_never_indexes_replay_with_unsafe_provenance(
    tmp_path,
    monkeypatch,
):
    _, original = _source(tmp_path)
    original.with_suffix(".provenance.json").unlink()
    replay_id = "replay-unsafe"
    replay_dir = tmp_path / "replays" / replay_id
    replay_dir.mkdir(parents=True)
    replay_transcript = tmp_path / "transcripts" / "123" / f"{replay_id}.md"
    replay_transcript.write_text("# 不完全な再処理文字起こし", encoding="utf-8")
    save_transcript_provenance(
        replay_transcript,
        guild_id=123,
        session_id=replay_id,
        mode="recording-replay",
        transcript_complete=False,
        capture_integrity_failed=False,
        status="final_reprocess_deferred",
    )
    (replay_dir / "job.json").write_text(
        json.dumps(
            {
                "id": replay_id,
                "source_session_id": "safe-session",
                "source_guild_id": 123,
                "target_guild_id": 123,
                "status": "completed",
                "stage": "completed",
                "updated_at": "2026-10-24T00:00:00+00:00",
                "transcript_path": str(replay_transcript),
                "minutes_path": str(replay_dir / "minutes.md"),
            }
        ),
        encoding="utf-8",
    )
    upload_transcript, upload_minutes, index = _environment(tmp_path, monkeypatch)

    with pytest.raises(UnsafeRecordingPublication):
        await backfill_session(123, "safe-session")

    upload_transcript.assert_not_awaited()
    upload_minutes.assert_not_awaited()
    index.assert_not_awaited()


@pytest.mark.parametrize(
    "unsafe_status",
    [
        "capture_suspicious",
        "capture_interrupted",
        "recording_truncated",
        "final_reprocess_deferred",
        "final_reprocess_completed",
        "minutes_generated",
        "delivery_in_progress",
        "recovery_processing",
        "recovery_retry",
        "recovery_dead_letter",
    ],
)
@pytest.mark.asyncio
async def test_backfill_rejects_every_nonfinal_status_without_external_calls(
    tmp_path,
    monkeypatch,
    unsafe_status,
):
    session, _ = _source(tmp_path)
    metadata_path = session / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["status"] = unsafe_status
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    upload_transcript, upload_minutes, index = _environment(tmp_path, monkeypatch)

    with pytest.raises(UnsafeRecordingPublication, match="not finalized"):
        await backfill_session(123, "safe-session")

    upload_transcript.assert_not_awaited()
    upload_minutes.assert_not_awaited()
    index.assert_not_awaited()


@pytest.mark.parametrize(
    "provenance_mutation",
    [
        "missing",
        "incomplete",
        "capture-failed",
        "suspicious-status",
        "wrong-session",
        "wrong-guild",
    ],
)
@pytest.mark.asyncio
async def test_backfill_rejects_untrusted_transcript_provenance_without_external_calls(
    tmp_path,
    monkeypatch,
    provenance_mutation,
):
    _, transcript = _source(tmp_path)
    provenance_path = transcript.with_suffix(".provenance.json")
    if provenance_mutation == "missing":
        provenance_path.unlink()
    else:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance_mutation == "incomplete":
            provenance["transcript_complete"] = False
        elif provenance_mutation == "capture-failed":
            provenance["capture_integrity_failed"] = True
        elif provenance_mutation == "suspicious-status":
            provenance["status"] = "capture_suspicious"
        elif provenance_mutation == "wrong-session":
            provenance["session_id"] = "another-session"
        else:
            provenance["guild_id"] = "999"
        provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    upload_transcript, upload_minutes, index = _environment(tmp_path, monkeypatch)

    with pytest.raises(UnsafeRecordingPublication):
        await backfill_session(123, "safe-session")

    upload_transcript.assert_not_awaited()
    upload_minutes.assert_not_awaited()
    index.assert_not_awaited()


@pytest.mark.parametrize(
    ("mutation", "ignored"),
    [
        ({"external_export_expected": False}, False),
        ({"external_export_reason": "channel-lineage-unresolved"}, False),
        ({"channel_id": ""}, False),
        ({"channel_name": "役員選考"}, False),
        ({}, True),
    ],
)
@pytest.mark.asyncio
async def test_backfill_rejects_unapproved_or_protected_source_without_external_calls(
    tmp_path,
    monkeypatch,
    mutation,
    ignored,
):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", "選考")
    session, _ = _source(tmp_path)
    metadata_path = session / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(mutation)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    upload_transcript, upload_minutes, index = _environment(
        tmp_path,
        monkeypatch,
        ignored=ignored,
    )

    with pytest.raises(UnsafeRecordingPublication):
        await backfill_session(123, "safe-session")

    upload_transcript.assert_not_awaited()
    upload_minutes.assert_not_awaited()
    index.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_rejects_integrity_failure_before_constructing_external_clients(
    tmp_path,
    monkeypatch,
):
    session, _ = _source(tmp_path)
    metadata_path = session / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["capture_integrity_failed"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    upload_transcript, upload_minutes, index = _environment(tmp_path, monkeypatch)
    corpus = Mock(side_effect=AssertionError("external client must not be constructed"))
    monkeypatch.setattr("bot.meeting_backfill.CorpusManager", corpus)

    with pytest.raises(UnsafeRecordingPublication, match="integrity"):
        await backfill_session(123, "safe-session")

    corpus.assert_not_called()
    upload_transcript.assert_not_awaited()
    upload_minutes.assert_not_awaited()
    index.assert_not_awaited()
