import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.recording_replay import (
    ReadOnlyReplaySink,
    RecordingReplayManager,
    find_recording_artifact,
    list_recording_artifacts,
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


@pytest.mark.asyncio
async def test_replay_manager_posts_minutes_from_existing_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    monkeypatch.setenv("YAGAPON_REPLAY_DIR", str(tmp_path / "replays"))
    _recording(tmp_path, raw=False, transcript=True)

    generated = AsyncMock(return_value="# 議事録\n\n## 話題別まとめ\n- 提案")
    monkeypatch.setattr("bot.recording_replay.generate_minutes", generated)
    monkeypatch.setattr("bot.recording_replay.new_genai_client", lambda _key: object())
    monkeypatch.setattr("bot.gdrive.upload_minutes", AsyncMock(return_value="https://docs/minutes"))
    monkeypatch.setattr("bot.gdrive.upload_transcript", AsyncMock(return_value="https://docs/transcript"))
    monkeypatch.setattr(
        "bot.commands.voice_cmd._summarize_minutes", AsyncMock(return_value="要約")
    )
    bot = SimpleNamespace(
        config=SimpleNamespace(get_glossary_text=lambda _guild_id: "用語"),
        _build_members_info=lambda _guild_id: "中山 | IT局長",
        operations=SimpleNamespace(record_event=Mock()),
    )
    channel = SimpleNamespace(id=50, guild=SimpleNamespace(id=999), send=AsyncMock())
    manager = RecordingReplayManager(bot)

    job = manager.start(
        session_id="session-long", target_channel=channel, retranscribe=False
    )
    await asyncio.gather(*list(manager._tasks))

    assert job.status == "completed"
    assert Path(job.minutes_path).read_text(encoding="utf-8").startswith("# 議事録")
    assert channel.send.await_count == 2
    assert generated.await_args.kwargs["speaker_roster"] == "中山 | IT局長"
    bot.operations.record_event.assert_called_once()
