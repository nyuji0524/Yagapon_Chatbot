import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.recording import (
    PersistentPCMSink,
    cleanup_old_recordings,
    find_recoverable_recordings,
    load_transcript_content,
)


def test_persistent_sink_reads_only_new_bounded_pcm(tmp_path):
    sink = PersistentPCMSink(1, "meeting", "listen", session_dir=tmp_path / "session")
    user = SimpleNamespace(id=42)
    sink.write(b"a" * 12, user)

    first = sink.read_chunks({}, chunk_bytes=5)
    assert first[42] == (b"a" * 5, 5)

    sink.write(b"b" * 4, user)
    second = sink.read_chunks({42: 5}, chunk_bytes=20)
    assert second[42] == (b"a" * 7 + b"b" * 4, 16)

    sink.cleanup()
    assert (tmp_path / "session" / "user-42.pcm").read_bytes() == b"a" * 12 + b"b" * 4
    metadata = json.loads((tmp_path / "session" / "metadata.json").read_text())
    assert metadata["status"] == "recorded"


def test_persistent_sink_stops_at_storage_limit(tmp_path):
    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "session",
        max_bytes=10,
    )
    sink.write(b"x" * 20, SimpleNamespace(id=7))
    sink.write(b"ignored", SimpleNamespace(id=7))
    sink.cleanup()

    assert sink.total_bytes == 10
    assert sink.limit_reached is True
    assert (tmp_path / "session" / "user-7.pcm").stat().st_size == 10


def test_persistent_sink_preserves_free_space_reserve(tmp_path, monkeypatch):
    reserve = 2 * 1024 * 1024 * 1024
    monkeypatch.setattr(
        "bot.recording.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=reserve + 20, used=0, free=reserve + 20),
    )
    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "session",
        max_bytes=100,
    )
    sink.write(b"x" * 50, SimpleNamespace(id=7))
    sink.cleanup()

    assert sink.total_bytes == 20
    assert sink.limit_reached is True


def test_cleanup_only_removes_expired_delivered_recordings(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    old = datetime.now(timezone.utc) - timedelta(days=8)
    delivered = tmp_path / "1" / "delivered"
    failed = tmp_path / "1" / "failed"
    delivered.mkdir(parents=True)
    failed.mkdir(parents=True)
    (delivered / "metadata.json").write_text(json.dumps({"status": "delivered", "updated_at": old.isoformat()}))
    (failed / "metadata.json").write_text(json.dumps({"status": "transcription_failed", "updated_at": old.isoformat()}))

    assert cleanup_old_recordings(retention_days=7) == 1
    assert not delivered.exists()
    assert failed.exists()


def test_transcript_is_archived_separately_before_raw_audio_deletion(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session_dir = tmp_path / "recordings" / "1" / "session-1"
    sink = PersistentPCMSink(1, "全体会議", "listen", session_dir=session_dir)
    sink.write(b"raw-audio", SimpleNamespace(id=42))

    transcript_path = sink.save_transcript("[Speaker 1]: 発言です")
    deleted = sink.delete_audio()

    assert deleted == 1
    assert transcript_path == tmp_path / "transcripts" / "1" / "session-1.md"
    assert "[Speaker 1]: 発言です" in transcript_path.read_text(encoding="utf-8")
    assert list(session_dir.glob("user-*.pcm")) == []


def test_cleanup_of_recording_metadata_keeps_separate_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    old = datetime.now(timezone.utc) - timedelta(days=8)
    session = tmp_path / "recordings" / "1" / "session-1"
    transcript = tmp_path / "transcripts" / "1" / "session-1.md"
    session.mkdir(parents=True)
    transcript.parent.mkdir(parents=True)
    transcript.write_text("文字起こし", encoding="utf-8")
    (session / "metadata.json").write_text(
        json.dumps({"status": "delivered_audio_deleted", "updated_at": old.isoformat()})
    )

    assert cleanup_old_recordings(retention_days=7) == 1
    assert not session.exists()
    assert transcript.read_text(encoding="utf-8") == "文字起こし"


def test_recording_checkpoint_can_be_resumed(tmp_path):
    session_dir = tmp_path / "recordings" / "1" / "session-1"
    sink = PersistentPCMSink(1, "会議", "listen", session_dir=session_dir)
    sink.write(b"audio", SimpleNamespace(id=42))
    transcript = sink.save_transcript("[Speaker 1]: 保存済み")
    sink.checkpoint(
        offsets={42: 5},
        speaker_labels={42: "Speaker 1"},
        transcript_path=transcript,
        status="shutdown_pending",
    )
    sink.cleanup()

    resumed = PersistentPCMSink(1, "会議", "listen", session_dir=session_dir, resume=True)
    metadata = resumed.read_metadata()

    assert metadata["offsets"] == {"42": 5}
    assert metadata["speaker_labels"] == {"42": "Speaker 1"}
    assert resumed.total_bytes == 5
    assert load_transcript_content(transcript) == "[Speaker 1]: 保存済み"


def test_find_recoverable_recordings_skips_terminal_and_future_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    now = datetime.now(timezone.utc)
    for name, status, retry_at in (
        ("recover", "shutdown_pending", None),
        ("done", "delivered_audio_deleted", None),
        ("later", "recovery_retry", (now + timedelta(hours=1)).isoformat()),
    ):
        session = tmp_path / "1" / name
        session.mkdir(parents=True)
        (session / "user-1.pcm").write_bytes(b"audio")
        (session / "metadata.json").write_text(
            json.dumps({"status": status, "updated_at": now.isoformat(), "next_retry_at": retry_at})
        )

    candidates = find_recoverable_recordings(now=now)

    assert [item.session_dir.name for item in candidates] == ["recover"]
