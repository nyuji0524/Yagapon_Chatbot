import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.recording import (
    cleanup_old_recordings,
    load_transcript_provenance,
    save_transcript_provenance,
)
from bot.recording_replay import (
    ReadOnlyReplaySink,
    RecordingReplayManager,
    ReplayJob,
    find_recording_artifact,
    list_recording_artifacts,
    reconcile_completed_replay_checkpoints,
)


def _recording(tmp_path: Path, *, raw=True, transcript=True):
    session = tmp_path / "recordings" / "123" / "session-long"
    session.mkdir(parents=True)
    expected_tracks = {"10": 384_000, "11": 192_000} if raw else {}
    expected_total = sum(expected_tracks.values())
    (session / "metadata.json").write_text(json.dumps({
        "guild_id": "123",
        "channel_name": "長時間会議",
        "channel_id": "456",
        "mode": "listen",
        "status": "delivered_recording_retained",
        "started_at": "2026-10-01T00:00:00+00:00",
        "capture_ended_at": "2026-10-01T01:00:00+00:00",
        "total_bytes": expected_total,
        "durable_total_bytes": expected_total,
        "track_bytes": expected_tracks,
        "transcript_complete": True,
        "capture_integrity_failed": False,
        "transcription_failed": False,
        "external_export_expected": True,
        "external_export_reason": "explicit-channel-allowlist",
    }), encoding="utf-8")
    if raw:
        (session / "user-10.pcm").write_bytes(b"a" * 384_000)
        (session / "user-11.pcm").write_bytes(b"b" * 192_000)
    transcript_path = tmp_path / "transcripts" / "123" / "session-long.md"
    if transcript:
        transcript_path.parent.mkdir(parents=True)
        transcript_path.write_text(
            "# 文字起こし\n\n---\n\n[Speaker 1]: 提案します\n",
            encoding="utf-8",
        )
        save_transcript_provenance(
            transcript_path,
            guild_id=123,
            session_id="session-long",
            mode="listen",
            transcript_complete=True,
            capture_integrity_failed=False,
            status="final_reprocess_completed",
        )
    return session, transcript_path


def test_recording_inventory_finds_raw_audio_and_durable_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    _recording(tmp_path)

    artifact = list_recording_artifacts()[0]

    assert artifact.session_id == "session-long"
    assert artifact.raw_bytes == 576_000
    assert artifact.estimated_seconds == 2
    assert artifact.transcript_bytes > 0
    assert find_recording_artifact("session-long") == artifact
    with pytest.raises(ValueError):
        find_recording_artifact("../../etc/passwd")


@pytest.mark.parametrize("metadata_state", ["missing", "corrupt"])
def test_recording_inventory_exposes_unindexed_pcm_as_interrupted_only(
    tmp_path,
    monkeypatch,
    metadata_state,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session = tmp_path / "recordings" / "123" / f"raw-{metadata_state}"
    session.mkdir(parents=True)
    (session / "user-10.pcm").write_bytes(b"preserved-pcm")
    if metadata_state == "corrupt":
        (session / "metadata.json").write_text("{broken", encoding="utf-8")

    artifact = find_recording_artifact(f"raw-{metadata_state}")

    assert artifact.raw_bytes == len(b"preserved-pcm")
    assert artifact.status == "capture_interrupted"
    assert artifact.capture_integrity_failed is True
    assert artifact.raw_integrity_valid is False
    assert artifact.raw_integrity_reason == f"recording-metadata-{metadata_state}"
    with pytest.raises(ValueError, match="failed integrity validation"):
        RecordingReplayManager(_replay_bot()).start(
            artifact=artifact,
            target_channel=SimpleNamespace(id=50, guild=SimpleNamespace(id=999)),
            retranscribe=True,
        )


def test_recording_inventory_skips_one_invalid_session_without_hiding_others(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    _recording(tmp_path, raw=True, transcript=True)
    invalid = tmp_path / "recordings" / "not-a-guild" / "broken-session"
    invalid.mkdir(parents=True)
    (invalid / "user-1.pcm").write_bytes(b"preserved-but-unindexable")
    (invalid / "metadata.json").write_text("{broken", encoding="utf-8")

    artifacts = list_recording_artifacts()

    assert [item.session_id for item in artifacts] == ["session-long"]


def test_replay_rejects_raw_truncated_after_final_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session, _ = _recording(tmp_path, raw=True, transcript=True)
    metadata_path = session / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(
        capture_ended_at="2026-10-01T01:00:00+00:00",
        total_bytes=576_000,
        durable_total_bytes=576_000,
        track_bytes={"10": 384_000, "11": 192_000},
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    originally_valid = find_recording_artifact("session-long")
    assert originally_valid.raw_integrity_valid is True
    (session / "user-10.pcm").write_bytes(b"truncated")

    with pytest.raises(ValueError, match="raw-track-size-mismatch"):
        RecordingReplayManager(_replay_bot()).start(
            artifact=originally_valid,
            target_channel=SimpleNamespace(id=50, guild=SimpleNamespace(id=999)),
            retranscribe=True,
        )

    artifact = find_recording_artifact("session-long")

    assert artifact.raw_integrity_valid is False
    assert artifact.capture_integrity_failed is True
    assert artifact.raw_integrity_reason == "raw-track-size-mismatch"
    with pytest.raises(ValueError, match="raw-track-size-mismatch"):
        RecordingReplayManager(_replay_bot()).start(
            artifact=artifact,
            target_channel=SimpleNamespace(id=50, guild=SimpleNamespace(id=999)),
            retranscribe=True,
        )


def test_replay_sink_reads_without_deleting_source(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    session, _ = _recording(tmp_path)
    artifact = list_recording_artifacts()[0]
    sink = ReadOnlyReplaySink(artifact, 999, "replay-test")

    chunk = sink.read_chunks({}, chunk_bytes=100)[10]

    assert chunk == (b"a" * 100, 100)
    assert sink.delete_audio() == 0
    assert (session / "user-10.pcm").exists()


def test_recording_inventory_keeps_orphaned_permanent_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    transcript = tmp_path / "transcripts" / "321" / "archived-session.md"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "# 文字起こし - 全体会議\n\n---\n\n[Speaker 1]: 発言",
        encoding="utf-8",
    )

    artifact = list_recording_artifacts()[0]

    assert artifact.session_id == "archived-session"
    assert artifact.channel_name == "全体会議"
    assert artifact.status == "transcript_archived"


def test_recording_inventory_excludes_previous_replay_transcripts(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    transcript = tmp_path / "transcripts" / "321" / "replay-old-job.md"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("# 文字起こし\n\n---\n\nテスト", encoding="utf-8")

    assert list_recording_artifacts() == []


def _completed_replay_checkpoint(tmp_path: Path, monkeypatch):
    replay_dir = tmp_path / "replays" / "replay-completed"
    replay_dir.mkdir(parents=True)
    transcript_archive = tmp_path / "transcripts" / "999" / "replay-completed.md"
    transcript_archive.parent.mkdir(parents=True)
    transcript_archive.write_text("# archived transcript", encoding="utf-8")
    work_transcript = replay_dir / "transcript.md"
    work_transcript.write_text("# replay transcript", encoding="utf-8")
    minutes = replay_dir / "minutes.md"
    minutes.write_text("# minutes", encoding="utf-8")
    job = ReplayJob(
        id="replay-completed",
        source_session_id="source-session",
        source_guild_id=123,
        target_guild_id=999,
        target_channel_id=50,
        retranscribe=True,
        status="completed",
        stage="completed",
        transcript_path=str(transcript_archive),
        minutes_path=str(minutes),
    )
    (replay_dir / "job.json").write_text(
        json.dumps(job.storage_dict()),
        encoding="utf-8",
    )
    (replay_dir / "metadata.json").write_text(
        json.dumps({
            "status": "processing",
            "source_session_id": "source-session",
            "source_guild_id": 123,
            "target_guild_id": 999,
            "offsets": {"10": 200},
            "speaker_labels": {"10": "Speaker 1"},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    return replay_dir


def test_reconcile_completed_replay_checkpoint_is_atomic_and_preserves_state(
    tmp_path,
    monkeypatch,
):
    replay_dir = _completed_replay_checkpoint(tmp_path, monkeypatch)

    result = reconcile_completed_replay_checkpoints(tmp_path / "replays")

    metadata = json.loads(
        (replay_dir / "metadata.json").read_text(encoding="utf-8")
    )
    assert result["reconciled"] == 1
    assert metadata["status"] == "replay_completed"
    assert metadata["job_status"] == "completed"
    assert metadata["job_stage"] == "completed"
    assert metadata["reconciled_from_status"] == "processing"
    assert metadata["offsets"] == {"10": 200}
    assert metadata["speaker_labels"] == {"10": "Speaker 1"}
    assert not list(replay_dir.glob(".metadata.json.*.tmp"))

    repeated = reconcile_completed_replay_checkpoints(tmp_path / "replays")
    assert repeated["reconciled"] == 0
    assert repeated["skipped_non_processing"] == 1


@pytest.mark.parametrize(
    ("mutation", "skip_key"),
    [
        ("failed_job", "skipped_non_completed"),
        ("missing_minutes", "skipped_missing_artifacts"),
        ("mismatched_identity", "skipped_invalid"),
    ],
)
def test_reconcile_completed_replay_checkpoint_fails_closed(
    tmp_path,
    monkeypatch,
    mutation,
    skip_key,
):
    replay_dir = _completed_replay_checkpoint(tmp_path, monkeypatch)
    if mutation == "failed_job":
        job_path = replay_dir / "job.json"
        job = json.loads(job_path.read_text(encoding="utf-8"))
        job.update({"status": "failed", "stage": "failed"})
        job_path.write_text(json.dumps(job), encoding="utf-8")
    elif mutation == "missing_minutes":
        (replay_dir / "minutes.md").unlink()
    else:
        metadata_path = replay_dir / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["target_guild_id"] = 1000
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    result = reconcile_completed_replay_checkpoints(tmp_path / "replays")

    metadata = json.loads(
        (replay_dir / "metadata.json").read_text(encoding="utf-8")
    )
    assert result["reconciled"] == 0
    assert result[skip_key] == 1
    assert metadata["status"] == "processing"


def test_reconcile_completed_replay_rejects_another_jobs_archived_transcript(
    tmp_path,
    monkeypatch,
):
    replay_dir = _completed_replay_checkpoint(tmp_path, monkeypatch)
    other = tmp_path / "transcripts" / "999" / "replay-other.md"
    other.write_text("# another replay transcript", encoding="utf-8")
    job_path = replay_dir / "job.json"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    job["transcript_path"] = str(other)
    job_path.write_text(json.dumps(job), encoding="utf-8")

    result = reconcile_completed_replay_checkpoints(tmp_path / "replays")

    metadata = json.loads(
        (replay_dir / "metadata.json").read_text(encoding="utf-8")
    )
    assert result["reconciled"] == 0
    assert result["skipped_missing_artifacts"] == 1
    assert metadata["status"] == "processing"


@pytest.mark.asyncio
async def test_replay_manager_posts_minutes_from_existing_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    _recording(tmp_path, raw=True, transcript=True)

    generated = AsyncMock(return_value="# 議事録\n\n## 話題別まとめ\n- 提案")
    monkeypatch.setattr("bot.recording_replay.generate_minutes", generated)
    monkeypatch.setattr("bot.recording_replay.new_genai_client", lambda _key: object())
    upload_minutes = AsyncMock(return_value="https://docs/minutes")
    upload_transcript = AsyncMock(return_value="https://docs/transcript")
    monkeypatch.setattr("bot.gdrive.upload_minutes", upload_minutes)
    monkeypatch.setattr("bot.gdrive.upload_transcript", upload_transcript)
    monkeypatch.setattr(
        "bot.commands.voice_cmd._summarize_minutes", AsyncMock(return_value="要約")
    )
    monkeypatch.setattr(
        "bot.meeting_index.index_bot_meeting", AsyncMock()
    )
    bot = SimpleNamespace(
        config=SimpleNamespace(get_glossary_text=lambda _guild_id: "用語"),
        _build_members_info=lambda _guild_id: "中山 | IT局長",
        operations=SimpleNamespace(record_event=Mock()),
    )
    channel = SimpleNamespace(id=50, guild=SimpleNamespace(id=999), send=AsyncMock())
    manager = RecordingReplayManager(bot)
    artifact = find_recording_artifact("session-long")

    job = manager.start(
        artifact=artifact, target_channel=channel, retranscribe=False
    )
    await asyncio.gather(*list(manager._tasks))

    assert job.status == "completed"
    assert Path(job.minutes_path).read_text(encoding="utf-8").startswith("# 議事録")
    assert channel.send.await_count == 2
    assert generated.await_args.kwargs["speaker_roster"] == "中山 | IT局長"
    stable_drive_session = "recording-replay:999:123:session-long"
    assert upload_minutes.await_args.kwargs["session_id"] == stable_drive_session
    assert upload_transcript.await_args.kwargs["session_id"] == stable_drive_session
    bot.operations.record_event.assert_called_once()

    restored = RecordingReplayManager(bot).get(job.id)
    assert restored is not None
    assert restored.status == "completed"


def _patch_fake_retranscription(monkeypatch, *, started=None, blocker=None):
    class FakeVoiceSession:
        def __init__(self, *_args, **_kwargs):
            self.recording_sink = None
            self.started_at = None
            self._transcription_failed = False
            self.transcript_text = ""
            self.transcript_path = None

        async def _reprocess_complete_recording(self):
            self.recording_sink.checkpoint(
                offsets={10: 100},
                speaker_labels={10: "Speaker 1"},
            )
            if started is not None:
                started.set()
            if blocker is not None:
                await blocker.wait()
            self.transcript_text = "[Speaker 1]: 提案します"
            return True

        async def _persist_transcript(self):
            self.transcript_path = self.recording_sink.save_transcript(
                self.transcript_text
            )

    monkeypatch.setattr("bot.voice.VoiceSession", FakeVoiceSession)


def _replay_bot():
    return SimpleNamespace(
        config=SimpleNamespace(get_glossary_text=lambda _guild_id: "用語"),
        _build_members_info=lambda _guild_id: "中山 | IT局長",
        operations=SimpleNamespace(record_event=Mock()),
    )


@pytest.mark.asyncio
async def test_retranscribed_replay_marks_completed_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    _recording(tmp_path, raw=True, transcript=True)
    _patch_fake_retranscription(monkeypatch)
    monkeypatch.setattr(
        "bot.recording_replay.generate_minutes",
        AsyncMock(return_value="# 議事録"),
    )
    monkeypatch.setattr("bot.recording_replay.new_genai_client", lambda _key: object())
    monkeypatch.setattr(
        "bot.gdrive.upload_minutes",
        AsyncMock(return_value="https://docs/minutes"),
    )
    monkeypatch.setattr(
        "bot.gdrive.upload_transcript",
        AsyncMock(return_value="https://docs/transcript"),
    )
    monkeypatch.setattr(
        "bot.commands.voice_cmd._summarize_minutes",
        AsyncMock(return_value="要約"),
    )
    monkeypatch.setattr("bot.meeting_index.index_bot_meeting", AsyncMock())
    channel = SimpleNamespace(id=50, guild=SimpleNamespace(id=999), send=AsyncMock())
    bot = _replay_bot()
    manager = RecordingReplayManager(bot)

    job = manager.start(
        artifact=find_recording_artifact("session-long"),
        target_channel=channel,
        retranscribe=True,
    )
    await asyncio.gather(*list(manager._tasks))

    metadata = json.loads(
        (tmp_path / "replays" / job.id / "metadata.json").read_text(encoding="utf-8")
    )
    assert job.status == "completed"
    assert metadata["status"] == "replay_completed"
    assert metadata["job_status"] == "completed"
    assert metadata["job_stage"] == "completed"


@pytest.mark.asyncio
async def test_retranscribed_replay_marks_failed_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    _recording(tmp_path, raw=True, transcript=True)
    _patch_fake_retranscription(monkeypatch)
    monkeypatch.setattr(
        "bot.recording_replay.generate_minutes",
        AsyncMock(side_effect=RuntimeError("generation failed")),
    )
    monkeypatch.setattr("bot.recording_replay.new_genai_client", lambda _key: object())
    channel = SimpleNamespace(id=50, guild=SimpleNamespace(id=999), send=AsyncMock())
    manager = RecordingReplayManager(_replay_bot())

    job = manager.start(
        artifact=find_recording_artifact("session-long"),
        target_channel=channel,
        retranscribe=True,
    )
    await asyncio.gather(*list(manager._tasks))

    metadata = json.loads(
        (tmp_path / "replays" / job.id / "metadata.json").read_text(encoding="utf-8")
    )
    assert job.status == "failed"
    assert metadata["status"] == "replay_failed"
    assert metadata["failed_stage"] == "generating_minutes"
    assert metadata["error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_retranscribed_replay_marks_interrupted_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    _recording(tmp_path, raw=True, transcript=True)
    started = asyncio.Event()
    blocker = asyncio.Event()
    _patch_fake_retranscription(monkeypatch, started=started, blocker=blocker)
    channel = SimpleNamespace(id=50, guild=SimpleNamespace(id=999), send=AsyncMock())
    manager = RecordingReplayManager(_replay_bot())

    job = manager.start(
        artifact=find_recording_artifact("session-long"),
        target_channel=channel,
        retranscribe=True,
    )
    await started.wait()
    await manager.shutdown(timeout=0)

    metadata = json.loads(
        (tmp_path / "replays" / job.id / "metadata.json").read_text(encoding="utf-8")
    )
    assert job.status == "interrupted"
    assert metadata["status"] == "replay_interrupted"
    assert metadata["interrupted_stage"] == "transcribing"
    assert metadata["error"] == "CancelledError"


@pytest.mark.asyncio
async def test_replay_checkpoints_partial_drive_success_with_retry_stable_key(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    _recording(tmp_path, raw=True, transcript=True)

    monkeypatch.setattr(
        "bot.recording_replay.generate_minutes",
        AsyncMock(return_value="# 議事録\n\n## 話題別まとめ\n- 提案"),
    )
    monkeypatch.setattr("bot.recording_replay.new_genai_client", lambda _key: object())

    async def delayed_transcript_upload(*_args, **_kwargs):
        await asyncio.sleep(0.02)
        return "https://docs/transcript"

    upload_transcript = AsyncMock(side_effect=delayed_transcript_upload)
    upload_minutes = AsyncMock(side_effect=RuntimeError("minutes upload failed"))
    monkeypatch.setattr("bot.gdrive.upload_transcript", upload_transcript)
    monkeypatch.setattr("bot.gdrive.upload_minutes", upload_minutes)
    monkeypatch.setattr(
        "bot.commands.voice_cmd._summarize_minutes", AsyncMock(return_value="要約")
    )
    index_meeting = AsyncMock()
    monkeypatch.setattr("bot.meeting_index.index_bot_meeting", index_meeting)
    bot = SimpleNamespace(
        config=SimpleNamespace(get_glossary_text=lambda _guild_id: "用語"),
        _build_members_info=lambda _guild_id: "中山 | IT局長",
        operations=SimpleNamespace(record_event=Mock()),
    )
    channel = SimpleNamespace(id=50, guild=SimpleNamespace(id=999), send=AsyncMock())
    manager = RecordingReplayManager(bot)

    job = manager.start(
        artifact=find_recording_artifact("session-long"),
        target_channel=channel,
        retranscribe=False,
    )
    await asyncio.gather(*list(manager._tasks))

    assert job.status == "failed"
    assert job.drive_transcript_url == "https://docs/transcript"
    assert job.drive_minutes_url == ""
    stable_drive_session = "recording-replay:999:123:session-long"
    assert upload_transcript.await_args.kwargs["session_id"] == stable_drive_session
    assert upload_minutes.await_args.kwargs["session_id"] == stable_drive_session
    stored = json.loads(
        (tmp_path / "replays" / job.id / "job.json").read_text(encoding="utf-8")
    )
    assert stored["drive_transcript_url"] == "https://docs/transcript"
    index_meeting.assert_not_awaited()


def test_replay_requires_explicit_retranscribe_when_only_pcm_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    _recording(tmp_path, raw=True, transcript=False)
    bot = SimpleNamespace()
    channel = SimpleNamespace(id=50, guild=SimpleNamespace(id=999))

    with pytest.raises(ValueError, match="retranscribe=true"):
        RecordingReplayManager(bot).start(
            artifact=find_recording_artifact("session-long"),
            target_channel=channel,
            retranscribe=False,
        )


def test_replay_rejects_known_partial_saved_transcript_without_retranscription(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session, transcript_path = _recording(tmp_path, raw=True, transcript=True)
    metadata_path = session / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["status"] = "final_reprocess_deferred"
    metadata["transcript_complete"] = False
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    save_transcript_provenance(
        transcript_path,
        guild_id=123,
        session_id="session-long",
        mode="listen",
        transcript_complete=False,
        capture_integrity_failed=False,
        status="final_reprocess_deferred",
    )
    artifact = find_recording_artifact("session-long")
    manager = RecordingReplayManager(_replay_bot())
    channel = SimpleNamespace(id=50, guild=SimpleNamespace(id=999))

    with pytest.raises(ValueError, match="not finalized"):
        manager.start(
            artifact=artifact,
            target_channel=channel,
            retranscribe=False,
        )


def test_replay_fails_closed_when_transcript_provenance_is_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    _, transcript = _recording(tmp_path, raw=True, transcript=True)
    transcript.with_suffix(".provenance.json").unlink()
    artifact = find_recording_artifact("session-long")

    with pytest.raises(ValueError, match="retranscribe=true"):
        RecordingReplayManager(_replay_bot()).start(
            artifact=artifact,
            target_channel=SimpleNamespace(id=50, guild=SimpleNamespace(id=999)),
            retranscribe=False,
        )


def test_replay_rejects_suspicious_transcript_provenance_before_external_calls(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    _, transcript = _recording(tmp_path, raw=True, transcript=True)
    provenance_path = transcript.with_suffix(".provenance.json")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["status"] = "capture_suspicious"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    channel = SimpleNamespace(
        id=50,
        guild=SimpleNamespace(id=999),
        send=AsyncMock(),
    )

    with pytest.raises(ValueError, match="provenance is not finalized"):
        RecordingReplayManager(_replay_bot()).start(
            artifact=find_recording_artifact("session-long"),
            target_channel=channel,
            retranscribe=False,
        )

    channel.send.assert_not_awaited()


def test_retention_cannot_turn_incomplete_capture_into_replayable_orphan(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session, transcript = _recording(tmp_path, raw=True, transcript=True)
    metadata_path = session / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(
        status="capture_suspicious",
        capture_integrity_failed=True,
        updated_at="2026-01-01T00:00:00+00:00",
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    save_transcript_provenance(
        transcript,
        guild_id=123,
        session_id="session-long",
        mode="listen",
        transcript_complete=True,
        capture_integrity_failed=True,
        status="capture_suspicious",
    )

    assert cleanup_old_recordings(
        now=datetime(2026, 10, 24, tzinfo=timezone.utc),
        retention_days=7,
    ) == 0
    assert session.exists()
    artifact = find_recording_artifact("session-long")
    assert artifact.capture_integrity_failed is True
    with pytest.raises(ValueError, match="marked incomplete"):
        RecordingReplayManager(_replay_bot()).start(
            artifact=artifact,
            target_channel=SimpleNamespace(id=50, guild=SimpleNamespace(id=999)),
            retranscribe=False,
        )
    with pytest.raises(ValueError, match="marked incomplete"):
        RecordingReplayManager(_replay_bot()).start(
            artifact=artifact,
            target_channel=SimpleNamespace(id=50, guild=SimpleNamespace(id=999)),
            retranscribe=True,
        )


def test_replay_rejects_sticky_capture_gap_even_after_status_changes(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session, _ = _recording(tmp_path, raw=True, transcript=True)
    metadata_path = session / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["status"] = "delivered_recording_retained"
    metadata["capture_integrity_failed"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    artifact = find_recording_artifact("session-long")
    manager = RecordingReplayManager(_replay_bot())
    channel = SimpleNamespace(id=50, guild=SimpleNamespace(id=999))

    with pytest.raises(ValueError, match="marked incomplete"):
        manager.start(
            artifact=artifact,
            target_channel=channel,
            retranscribe=False,
        )


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
def test_replay_rejects_nonfinalized_source_before_external_calls(
    tmp_path,
    monkeypatch,
    unsafe_status,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session, _ = _recording(tmp_path, raw=True, transcript=True)
    metadata_path = session / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["status"] = unsafe_status
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    channel = SimpleNamespace(
        id=50,
        guild=SimpleNamespace(id=999),
        send=AsyncMock(),
    )

    with pytest.raises(ValueError, match="not finalized"):
        RecordingReplayManager(_replay_bot()).start(
            artifact=find_recording_artifact("session-long"),
            target_channel=channel,
            retranscribe=True,
        )

    channel.send.assert_not_awaited()


def test_replay_rejects_protected_source_before_external_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", "選考")
    session, _ = _recording(tmp_path, raw=True, transcript=True)
    metadata_path = session / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["channel_name"] = "役員選考"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    channel = SimpleNamespace(
        id=50,
        guild=SimpleNamespace(id=999),
        send=AsyncMock(),
    )

    with pytest.raises(ValueError, match="protected"):
        RecordingReplayManager(_replay_bot()).start(
            artifact=find_recording_artifact("session-long"),
            target_channel=channel,
            retranscribe=False,
        )

    channel.send.assert_not_awaited()


def test_replay_does_not_externalize_transcript_when_raw_manifest_is_unavailable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    _recording(tmp_path, raw=False, transcript=True)
    channel = SimpleNamespace(
        id=50,
        guild=SimpleNamespace(id=999),
        send=AsyncMock(),
    )

    with pytest.raises(ValueError, match="raw audio is no longer available"):
        RecordingReplayManager(_replay_bot()).start(
            artifact=find_recording_artifact("session-long"),
            target_channel=channel,
            retranscribe=False,
        )

    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_revalidates_source_before_first_external_call(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    session, _ = _recording(tmp_path, raw=True, transcript=True)
    channel = SimpleNamespace(
        id=50,
        guild=SimpleNamespace(id=999),
        send=AsyncMock(),
    )
    bot = _replay_bot()
    manager = RecordingReplayManager(bot)
    job = manager.start(
        artifact=find_recording_artifact("session-long"),
        target_channel=channel,
        retranscribe=False,
    )
    metadata_path = session / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["status"] = "capture_interrupted"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    await asyncio.gather(*list(manager._tasks))

    assert job.status == "failed"
    channel.send.assert_not_awaited()
    bot.operations.record_event.assert_not_called()


@pytest.mark.asyncio
async def test_review_only_transcribes_interrupted_pcm_without_any_publisher(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    session_dir, _ = _recording(tmp_path, raw=True, transcript=False)
    metadata_path = session_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(
        status="capture_interrupted",
        capture_ended_at="",
        capture_integrity_failed=True,
        capture_integrity_reason="process-exited-during-capture",
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _patch_fake_retranscription(monkeypatch)
    generate_minutes = AsyncMock(return_value="# must not run")
    upload_transcript = AsyncMock(return_value="https://must-not-run/transcript")
    upload_minutes = AsyncMock(return_value="https://must-not-run/minutes")
    index_meeting = AsyncMock()
    summarize = AsyncMock(return_value="must not run")
    monkeypatch.setattr("bot.recording_replay.generate_minutes", generate_minutes)
    monkeypatch.setattr("bot.gdrive.upload_transcript", upload_transcript)
    monkeypatch.setattr("bot.gdrive.upload_minutes", upload_minutes)
    monkeypatch.setattr("bot.meeting_index.index_bot_meeting", index_meeting)
    monkeypatch.setattr("bot.commands.voice_cmd._summarize_minutes", summarize)
    actor = SimpleNamespace(id=777)
    channel = SimpleNamespace(
        id=456,
        send=AsyncMock(),
        permissions_for=lambda _member: SimpleNamespace(view_channel=True),
    )
    source_guild = SimpleNamespace(
        id=123,
        get_member=lambda user_id: actor if user_id == actor.id else None,
        get_channel_or_thread=lambda channel_id: (
            channel if channel_id == channel.id else None
        ),
    )
    actor.guild = source_guild
    channel.guild = source_guild
    bot = _replay_bot()
    bot.get_guild = lambda guild_id: source_guild if guild_id == 123 else None
    manager = RecordingReplayManager(bot)
    artifact = find_recording_artifact("session-long")
    assert artifact.raw_manifest_verified is False
    assert artifact.capture_integrity_failed is True

    with pytest.raises(ValueError):
        manager.start(
            artifact=artifact,
            target_channel=channel,
            retranscribe=True,
        )
    job = manager.start(
        artifact=artifact,
        target_channel=channel,
        retranscribe=True,
        review_only=True,
        review_actor=actor,
    )
    await asyncio.gather(*list(manager._tasks))

    assert job.status == "review_completed"
    assert job.stage == "review_completed"
    assert job.drive_transcript_url == ""
    assert job.drive_minutes_url == ""
    assert job.minutes_path == ""
    assert channel.send.await_count == 2
    result_kwargs = channel.send.await_args_list[-1].kwargs
    assert "file" in result_kwargs
    assert "INCOMPLETE" in result_kwargs["file"].filename
    assert "不完全" in result_kwargs["embed"].title
    generate_minutes.assert_not_awaited()
    upload_transcript.assert_not_awaited()
    upload_minutes.assert_not_awaited()
    index_meeting.assert_not_awaited()
    summarize.assert_not_awaited()
    bot.operations.record_event.assert_not_called()
    source_after = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert source_after["status"] == "capture_interrupted"
    assert source_after["capture_integrity_failed"] is True
    review_provenance = load_transcript_provenance(Path(job.transcript_path))
    assert review_provenance["transcript_complete"] is False
    assert review_provenance["capture_integrity_failed"] is True
    assert review_provenance["status"] == "review_only_partial"


def test_review_only_requires_retranscription(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session_dir, _ = _recording(tmp_path, raw=True, transcript=False)
    metadata_path = session_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(
        status="capture_interrupted",
        capture_ended_at="",
        capture_integrity_failed=True,
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="requires retranscribe=true"):
        RecordingReplayManager(_replay_bot()).start(
            artifact=find_recording_artifact("session-long"),
            target_channel=SimpleNamespace(id=50, guild=SimpleNamespace(id=999)),
            retranscribe=False,
            review_only=True,
        )


@pytest.mark.parametrize("context", ["other-guild", "other-channel", "no-view"])
def test_review_only_rejects_context_outside_original_audience_without_send(
    tmp_path,
    monkeypatch,
    context,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session_dir, _ = _recording(tmp_path, raw=True, transcript=False)
    metadata_path = session_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(
        status="capture_interrupted",
        capture_ended_at="",
        capture_integrity_failed=True,
        channel_name="役員選考",
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    actor = SimpleNamespace(id=777)
    source_channel = SimpleNamespace(
        id=456,
        send=AsyncMock(),
        permissions_for=lambda _member: SimpleNamespace(
            view_channel=context != "no-view"
        ),
    )
    source_guild = SimpleNamespace(
        id=123,
        get_member=lambda user_id: actor if user_id == actor.id else None,
        get_channel_or_thread=lambda channel_id: (
            source_channel if channel_id == source_channel.id else None
        ),
    )
    actor.guild = source_guild
    source_channel.guild = source_guild
    if context == "other-guild":
        target_channel = SimpleNamespace(
            id=456,
            guild=SimpleNamespace(id=999),
            send=AsyncMock(),
        )
    elif context == "other-channel":
        target_channel = SimpleNamespace(
            id=999,
            guild=source_guild,
            send=AsyncMock(),
        )
    else:
        target_channel = source_channel
    bot = _replay_bot()
    bot.get_guild = lambda guild_id: source_guild if guild_id == 123 else None

    with pytest.raises(ValueError):
        RecordingReplayManager(bot).start(
            artifact=find_recording_artifact("session-long"),
            target_channel=target_channel,
            retranscribe=True,
            review_only=True,
            review_actor=actor,
        )

    target_channel.send.assert_not_awaited()
    source_channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_shutdown_checkpoints_interrupted_job(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    _recording(tmp_path, raw=True, transcript=True)
    blocker = asyncio.Event()

    async def blocked_send(*_args, **_kwargs):
        await blocker.wait()

    bot = SimpleNamespace()
    channel = SimpleNamespace(
        id=50,
        guild=SimpleNamespace(id=999),
        send=blocked_send,
    )
    manager = RecordingReplayManager(bot)
    artifact = find_recording_artifact("session-long")
    job = manager.start(artifact=artifact, target_channel=channel, retranscribe=False)
    await asyncio.sleep(0)

    await manager.shutdown(timeout=0)

    assert job.status == "interrupted"
    stored = json.loads(
        (tmp_path / "replays" / job.id / "job.json").read_text(encoding="utf-8")
    )
    assert stored["status"] == "interrupted"
    with pytest.raises(RuntimeError, match="shutting down"):
        manager.start(artifact=artifact, target_channel=channel, retranscribe=False)
