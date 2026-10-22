import json
import stat
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.recording import (
    PersistentPCMSink,
    cleanup_old_recordings,
    finalize_recording_replay,
    find_recoverable_recordings,
    load_transcript_content,
    save_external_transcript,
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


def test_receive_timeline_survives_resume_and_preserves_alternating_speakers(tmp_path, monkeypatch):
    received_times = iter([1_000_000_000, 1_100_000_000, 1_200_000_000])
    monkeypatch.setattr("bot.recording.time.time_ns", lambda: next(received_times))
    session_dir = tmp_path / "session-timeline"
    sink = PersistentPCMSink(1, "meeting", "listen", session_dir=session_dir)
    sink.write(b"first-a", SimpleNamespace(id=10))
    sink.write(b"only-b", SimpleNamespace(id=20))
    sink.write(b"second-a", SimpleNamespace(id=10))
    sink.cleanup()

    resumed = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=session_dir,
        resume=True,
    )
    segments = resumed.read_timeline_segments(max_bytes=100, gap_seconds=1.0)

    assert segments is not None
    assert [segment.user_id for segment in segments] == [10, 20, 10]
    assert [resumed.read_timeline_audio(segment) for segment in segments] == [
        b"first-a",
        b"only-b",
        b"second-a",
    ]
    metadata = resumed.read_metadata()
    assert metadata["timeline_schema"] == 1
    assert metadata["timeline_complete"] is True
    resumed.cleanup()


def test_recording_without_timeline_uses_safe_legacy_signal(tmp_path):
    session_dir = tmp_path / "legacy-session"
    session_dir.mkdir()
    (session_dir / "user-10.pcm").write_bytes(b"legacy-audio")

    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=session_dir,
        resume=True,
    )

    assert sink.read_timeline_segments(max_bytes=100) is None
    assert sink.read_chunks({}, chunk_bytes=100)[10] == (b"legacy-audio", 12)
    assert sink.read_metadata()["timeline_complete"] is False
    sink.cleanup()


def test_persistent_sink_preserves_channel_lineage_for_safe_recovery(tmp_path):
    session_dir = tmp_path / "session"
    sink = PersistentPCMSink(
        1,
        "面接室",
        "listen",
        channel_id=10,
        category_id=20,
        category_name="IT局員選考",
        session_dir=session_dir,
    )
    sink.cleanup()

    resumed = PersistentPCMSink(
        1,
        "面接室",
        "listen",
        session_dir=session_dir,
        resume=True,
    )

    assert resumed.channel_id == 10
    assert resumed.category_id == 20
    assert resumed.category_name == "IT局員選考"
    assert resumed.channel_lineage_recorded is True


def test_capture_interruption_is_manual_review_only(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    sink = PersistentPCMSink(1, "会議", "listen", session_dir=tmp_path / "1" / "session")
    sink.write(b"audio", SimpleNamespace(id=7))
    sink.cleanup()
    sink.mark("capture_interrupted")

    assert find_recoverable_recordings() == []


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
    metadata = sink.read_metadata()
    assert metadata["storage_limit_reached"] is True

    resumed = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "session",
        max_bytes=100,
        resume=True,
    )
    assert resumed.limit_reached is True


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


def test_cleanup_removes_expired_terminal_failure_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    old = datetime.now(timezone.utc) - timedelta(days=8)
    interrupted = tmp_path / "1" / "interrupted"
    interrupted.mkdir(parents=True)
    (interrupted / "user-1.pcm").write_bytes(b"audio")
    (interrupted / "metadata.json").write_text(
        json.dumps({"status": "capture_interrupted", "updated_at": old.isoformat()})
    )

    assert cleanup_old_recordings(retention_days=7) == 1
    assert not interrupted.exists()


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
    assert stat.S_IMODE(transcript_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(transcript_path.parent.stat().st_mode) == 0o700
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


def test_uploaded_transcript_uses_same_durable_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))

    path = save_external_transcript(
        99,
        "20261012T000000Z-upload/unsafe",
        "対面全体会議",
        "Speaker 1: 発言",
    )

    assert path.parent == tmp_path / "transcripts" / "99"
    assert "/" not in path.name
    assert load_transcript_content(path) == "Speaker 1: 発言"


def test_successful_replay_archives_transcript_before_deleting_retained_pcm(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session = tmp_path / "recordings" / "1" / "session-1"
    session.mkdir(parents=True)
    (session / "user-10.pcm").write_bytes(b"audio")
    (session / "metadata.json").write_text(
        json.dumps(
            {
                "guild_id": "1",
                "status": "delivered_recording_retained",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    deleted = finalize_recording_replay(
        guild_id=1,
        session_id="session-1",
        session_dir=session,
        channel_name="全体会議",
        transcript="[Speaker 1]: 保存済み",
        replay_job_id="replay-test",
    )

    metadata = json.loads((session / "metadata.json").read_text(encoding="utf-8"))
    transcript = tmp_path / "transcripts" / "1" / "session-1.md"
    assert deleted == 1
    assert list(session.glob("user-*.pcm")) == []
    assert load_transcript_content(transcript) == "[Speaker 1]: 保存済み"
    assert metadata["status"] == "delivered_audio_deleted"
    assert metadata["replay_job_id"] == "replay-test"


def test_replay_does_not_delete_audio_from_non_terminal_session(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session = tmp_path / "recordings" / "1" / "session-1"
    session.mkdir(parents=True)
    pcm = session / "user-10.pcm"
    pcm.write_bytes(b"audio")
    (session / "metadata.json").write_text(
        json.dumps({"guild_id": "1", "status": "recording"}),
        encoding="utf-8",
    )

    deleted = finalize_recording_replay(
        guild_id=1,
        session_id="session-1",
        session_dir=session,
        channel_name="全体会議",
        transcript="[Speaker 1]: 未完了",
        replay_job_id="replay-test",
    )

    assert deleted == 0
    assert pcm.exists()
    assert not (tmp_path / "transcripts" / "1" / "session-1.md").exists()


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


def test_recording_persists_origin_channel_across_resume(tmp_path):
    session_dir = tmp_path / "recordings" / "1" / "session-channel"
    sink = PersistentPCMSink(
        1,
        "役員会",
        "listen",
        channel_id=456,
        session_dir=session_dir,
    )
    sink.cleanup()

    resumed = PersistentPCMSink(
        1,
        "役員会",
        "listen",
        session_dir=session_dir,
        resume=True,
    )

    assert resumed.channel_id == 456
    assert resumed.read_metadata()["channel_id"] == "456"


def test_find_recoverable_recordings_skips_terminal_and_future_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    now = datetime.now(timezone.utc)
    for name, status, retry_at in (
        ("recover", "shutdown_pending", None),
        ("final-pass", "final_reprocess_processing", None),
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

    assert [item.session_dir.name for item in candidates] == ["final-pass", "recover"]
