import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

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
    (session / "metadata.json").write_text(json.dumps({
        "guild_id": "123",
        "channel_name": "長時間会議",
        "mode": "listen",
        "status": "delivered_recording_retained",
        "started_at": "2026-10-01T00:00:00+00:00",
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


@pytest.mark.asyncio
async def test_replay_manager_posts_minutes_from_existing_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    _recording(tmp_path, raw=False, transcript=True)

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

        async def _drain_recording(self):
            self.recording_sink.checkpoint(
                offsets={10: 100},
                speaker_labels={10: "Speaker 1"},
            )
            if started is not None:
                started.set()
            if blocker is not None:
                await blocker.wait()
            self.transcript_text = "[Speaker 1]: 提案します"

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
    _recording(tmp_path, raw=False, transcript=True)

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


@pytest.mark.asyncio
async def test_replay_shutdown_checkpoints_interrupted_job(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    _recording(tmp_path, raw=False, transcript=True)
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
