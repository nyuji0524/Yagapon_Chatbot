import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.recording import (
    PersistentPCMSink,
    RecordingLimitExceeded,
    _atomic_write_private_json,
    cleanup_old_recordings,
    degraded_recording_is_processable,
    finalize_recording_replay,
    find_recoverable_recordings,
    find_stale_active_recordings,
    load_transcript_content,
    load_transcript_provenance,
    quarantine_stale_recording,
    save_external_transcript,
    save_transcript_provenance,
    validate_recording_raw,
)


def test_manifest_verified_disconnect_can_be_processed_as_partial(tmp_path):
    session = tmp_path / "recording"
    session.mkdir()
    pcm = b"\x00\x01" * 1024
    (session / "user-42.pcm").write_bytes(pcm)
    metadata = {
        "capture_integrity_failed": True,
        "capture_integrity_reason": "bot-disconnected",
        "capture_ended_at": datetime.now(timezone.utc).isoformat(),
        "timeline_complete": True,
        "track_bytes": {"42": len(pcm)},
        "total_bytes": len(pcm),
        "durable_total_bytes": len(pcm),
        "capture_health": {"suspicious": False},
    }

    assert degraded_recording_is_processable(session, metadata)


def test_existing_manifest_verified_interruption_enters_recovery_queue(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    session = tmp_path / "1" / "interrupted"
    session.mkdir(parents=True)
    pcm = b"\x00\x01" * 1024
    (session / "user-42.pcm").write_bytes(pcm)
    now = datetime.now(timezone.utc)
    metadata = {
        "guild_id": "1",
        "channel_name": "meeting",
        "mode": "listen",
        "status": "capture_interrupted",
        "started_at": now.isoformat(),
        "capture_integrity_failed": True,
        "capture_integrity_reason": "bot-disconnected",
        "capture_ended_at": now.isoformat(),
        "timeline_complete": True,
        "track_bytes": {"42": len(pcm)},
        "total_bytes": len(pcm),
        "durable_total_bytes": len(pcm),
        "capture_health": {"suspicious": False},
    }
    (session / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    candidates = find_recoverable_recordings(now=now)

    assert [item.session_dir for item in candidates] == [session]


@pytest.mark.parametrize(
    "reason",
    ("pcm-write-failed", "timeline-write-failed", "sink-finalization-failed"),
)
def test_storage_or_manifest_failures_never_auto_process(tmp_path, reason):
    session = tmp_path / reason
    session.mkdir()
    pcm = b"\x00\x01" * 1024
    (session / "user-42.pcm").write_bytes(pcm)
    metadata = {
        "capture_integrity_failed": True,
        "capture_integrity_reason": reason,
        "capture_ended_at": datetime.now(timezone.utc).isoformat(),
        "timeline_complete": True,
        "track_bytes": {"42": len(pcm)},
        "total_bytes": len(pcm),
        "durable_total_bytes": len(pcm),
    }

    assert not degraded_recording_is_processable(session, metadata)


def test_atomic_private_json_fsyncs_file_before_replace_and_parent_after(
    tmp_path,
    monkeypatch,
):
    parent = tmp_path / "durable-json"
    parent.mkdir()
    destination = parent / "metadata.json"
    events = []
    real_fsync = os.fsync
    real_replace = os.replace

    def observed_fsync(descriptor):
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        events.append(f"fsync-{kind}")
        return real_fsync(descriptor)

    def observed_replace(source, target):
        events.append("replace")
        return real_replace(source, target)

    monkeypatch.setattr(os, "fsync", observed_fsync)
    monkeypatch.setattr(os, "replace", observed_replace)

    _atomic_write_private_json(destination, {"status": "recording"})

    assert events == ["fsync-file", "replace", "fsync-directory"]
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_atomic_private_json_replace_failure_preserves_previous_file(
    tmp_path,
    monkeypatch,
):
    destination = tmp_path / "metadata.json"
    destination.write_text('{"status":"old"}', encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        _atomic_write_private_json(destination, {"status": "new"})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"status": "old"}
    assert not list(tmp_path.glob(".metadata.json.*.tmp"))


def test_provenance_replace_failure_preserves_last_durable_checkpoint(
    tmp_path,
    monkeypatch,
):
    transcript = tmp_path / "transcript.md"
    transcript.write_text("transcript", encoding="utf-8")
    save_transcript_provenance(
        transcript,
        guild_id=1,
        session_id="session",
        mode="listen",
        transcript_complete=False,
        capture_integrity_failed=False,
        status="processing",
    )
    previous = load_transcript_provenance(transcript)

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_transcript_provenance(
            transcript,
            guild_id=1,
            session_id="session",
            mode="listen",
            transcript_complete=True,
            capture_integrity_failed=False,
            status="completed",
        )

    assert load_transcript_provenance(transcript) == previous


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


def test_pcm_and_timeline_are_visible_before_cleanup_without_explicit_flush(tmp_path):
    session_dir = tmp_path / "unbuffered-session"
    sink = PersistentPCMSink(1, "meeting", "listen", session_dir=session_dir)

    sink.write(b"tiny-pcm", SimpleNamespace(id=42))

    # Read through separate descriptors while the sink's descriptors are still
    # open. This would be empty for a small userspace-buffered write.
    assert (session_dir / "user-42.pcm").read_bytes() == b"tiny-pcm"
    timeline_lines = (session_dir / "timeline.jsonl").read_bytes().splitlines()
    assert len(timeline_lines) == 1
    timeline_event = json.loads(timeline_lines[0])
    assert timeline_event["user_id"] == 42
    assert (timeline_event["start"], timeline_event["end"]) == (0, 8)

    sink.cleanup()


def test_raw_recording_artifacts_have_private_permissions(tmp_path):
    session_dir = tmp_path / "private-session"
    sink = PersistentPCMSink(1, "meeting", "listen", session_dir=session_dir)
    sink.write(b"audio", SimpleNamespace(id=42))

    assert stat.S_IMODE(session_dir.stat().st_mode) == 0o700
    for artifact in ("metadata.json", "user-42.pcm", "timeline.jsonl"):
        assert stat.S_IMODE((session_dir / artifact).stat().st_mode) == 0o600

    sink.cleanup()


def test_new_pcm_and_timeline_directory_entries_are_fsynced(tmp_path, monkeypatch):
    session_dir = tmp_path / "directory-entry-session"
    sink = PersistentPCMSink(1, "meeting", "listen", session_dir=session_dir)
    fsynced = []
    monkeypatch.setattr(
        "bot.recording._fsync_directory",
        lambda path: fsynced.append(Path(path)),
    )

    sink.write(b"audio", SimpleNamespace(id=42))

    assert fsynced.count(session_dir) == 2
    sink.cleanup()


def test_flush_advances_and_persists_durable_total_bytes(tmp_path):
    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "durable-checkpoint",
    )
    sink.write(b"durable-audio", SimpleNamespace(id=42))
    assert sink.durable_total_bytes == 0

    sink.flush()

    assert sink.durable_total_bytes == len(b"durable-audio")
    sink.mark("recording")
    assert sink.read_metadata()["durable_total_bytes"] == len(b"durable-audio")
    sink.cleanup()


def test_capture_integrity_latch_is_sticky_and_cleanup_persists_it(tmp_path):
    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "latched-integrity",
    )
    sink.latch_capture_integrity("voice-receive-disconnected")
    sink.latch_capture_integrity("later-reason", status="capture_suspicious")

    assert sink.capture_integrity_failed is True
    assert sink.capture_integrity_reason == "voice-receive-disconnected"

    sink.cleanup()
    metadata = sink.read_metadata()
    assert metadata["status"] == "capture_interrupted"
    assert metadata["capture_integrity_failed"] is True
    assert metadata["capture_integrity_reason"] == "voice-receive-disconnected"


def test_cleanup_fsyncs_and_closes_every_track_after_one_failure(
    tmp_path,
    monkeypatch,
):
    class FakeFile:
        def __init__(self, descriptor):
            self.descriptor = descriptor
            self.closed = False
            self.flush_count = 0
            self.close_count = 0

        def flush(self):
            self.flush_count += 1

        def fileno(self):
            return self.descriptor

        def close(self):
            self.close_count += 1
            self.closed = True

    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "session-cleanup-error",
    )
    first = FakeFile(101)
    second = FakeFile(102)
    timeline = FakeFile(103)
    sink._files = {1: first, 2: second}
    sink._timeline_file = timeline
    fsynced = []

    failed_once = False

    def failing_fsync(descriptor):
        nonlocal failed_once
        fsynced.append(descriptor)
        # Model one damaged track checkpoint.  Do not key every later fsync
        # failure solely by the integer descriptor: the OS may reuse 101 for
        # the atomic metadata temp file after the fake track is closed.
        if descriptor == 101 and not failed_once:
            failed_once = True
            raise OSError("disk error")

    monkeypatch.setattr(os, "fsync", failing_fsync)

    with pytest.raises(OSError, match="disk error"):
        sink.cleanup()

    assert fsynced[:3] == [101, 102, 103]
    assert first.closed and second.closed and timeline.closed
    assert first.close_count == second.close_count == timeline.close_count == 1
    metadata = sink.read_metadata()
    assert metadata["status"] == "capture_interrupted"
    assert metadata["capture_integrity_failed"] is True
    assert metadata["capture_integrity_reason"] == "sink-cleanup-failed"
    assert metadata["cleanup_error"] == "OSError"
    # Cleanup is idempotent after the first best-effort pass.
    sink.cleanup()


def test_receive_timeline_coalesces_interleaved_packet_streams_per_speaker(
    tmp_path,
    monkeypatch,
):
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
    assert [segment.user_id for segment in segments] == [10, 20]
    assert [resumed.read_timeline_audio(segment) for segment in segments] == [
        b"first-asecond-a",
        b"only-b",
    ]
    metadata = resumed.read_metadata()
    assert metadata["timeline_schema"] == 1
    assert metadata["timeline_complete"] is True
    resumed.cleanup()


def test_receive_timeline_preserves_turn_order_after_real_speaker_pause(
    tmp_path,
    monkeypatch,
):
    received_times = iter([1_000_000_000, 1_100_000_000, 3_000_000_000])
    monkeypatch.setattr("bot.recording.time.time_ns", lambda: next(received_times))
    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "ordered-turns",
    )
    sink.write(b"first-a", SimpleNamespace(id=10))
    sink.write(b"only-b", SimpleNamespace(id=20))
    sink.write(b"second-a", SimpleNamespace(id=10))
    sink.cleanup()

    segments = sink.read_timeline_segments(max_bytes=100, gap_seconds=1.0)

    assert segments is not None
    assert [segment.user_id for segment in segments] == [10, 20, 10]
    assert [sink.read_timeline_audio(segment) for segment in segments] == [
        b"first-a",
        b"only-b",
        b"second-a",
    ]


def test_long_interleaved_timeline_is_bounded_by_audio_chunks_not_packet_count(
    tmp_path,
    monkeypatch,
):
    packet_count_per_user = 2_700
    timestamps = iter(
        1_000_000_000 + index * 500_000_000
        for index in range(packet_count_per_user * 2)
    )
    monkeypatch.setattr("bot.recording.time.time_ns", lambda: next(timestamps))
    sink = PersistentPCMSink(
        1,
        "long-meeting",
        "listen",
        session_dir=tmp_path / "long-session",
    )
    for _ in range(packet_count_per_user):
        sink.write(b"aa", SimpleNamespace(id=10))
        sink.write(b"bb", SimpleNamespace(id=20))
    sink.cleanup()

    segments = sink.read_timeline_segments(max_bytes=200, gap_seconds=2.0)

    assert segments is not None
    assert len(segments) == 54
    assert sum(segment.end - segment.start for segment in segments) == 10_800
    assert all(segment.end - segment.start <= 200 for segment in segments)
    assert {segment.user_id for segment in segments} == {10, 20}


def test_capture_health_flags_sparse_long_recording_and_persists_diagnostics(
    tmp_path,
):
    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "session",
    )
    sink.started_at = datetime.now(timezone.utc) - timedelta(minutes=60)
    sink.write(b"x" * 192_000, SimpleNamespace(id=42))
    sink.cleanup()

    health = sink.capture_health()
    metadata = sink.read_metadata()

    assert health.suspicious is True
    assert health.reason == "low-audio-ratio"
    assert health.source_tracks == 1
    assert health.audio_seconds == 1
    assert metadata["capture_health"]["suspicious"] is True
    assert metadata["capture_health"]["reason"] == "low-audio-ratio"


def test_capture_health_does_not_flag_short_recording_before_warmup(tmp_path):
    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "session",
    )
    sink.started_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    health = sink.capture_health()

    assert health.suspicious is False
    assert health.reason == ""


def test_capture_health_flags_stalled_pcm_even_when_total_ratio_was_sufficient(
    tmp_path,
):
    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "session",
    )
    now = datetime.now(timezone.utc)
    sink.started_at = now - timedelta(minutes=30)
    sink._user_bytes = {42: 10 * 60 * 192_000}
    sink._total_bytes = sink._user_bytes[42]
    sink._last_audio_at = now - timedelta(minutes=10)

    health = sink.capture_health(now=now)

    assert health.audio_to_wall_ratio > 0.15
    assert health.suspicious is True
    assert health.reason == "audio-stalled"
    assert health.seconds_since_last_audio == 600


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


def test_corrupt_timeline_tail_is_backed_up_before_repair(tmp_path):
    session_dir = tmp_path / "corrupt-timeline"
    session_dir.mkdir()
    (session_dir / "user-10.pcm").write_bytes(b"audio")
    valid = json.dumps(
        {
            "v": 1,
            "sequence": 0,
            "user_id": 10,
            "start": 0,
            "end": 5,
            "received_at_ns": 1,
        },
        separators=(",", ":"),
    ).encode()
    original = valid + b"\n{truncated"
    timeline = session_dir / "timeline.jsonl"
    timeline.write_bytes(original)

    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=session_dir,
        resume=True,
    )

    backups = list(session_dir.glob("timeline.jsonl.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert timeline.read_bytes() == valid + b"\n"
    sink.cleanup()


def test_pcm_write_failure_latches_alertable_integrity_anomaly(tmp_path):
    class BrokenPCM:
        def write(self, _data):
            raise OSError("disk full")

    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "pcm-write-failure",
    )
    sink._files[42] = BrokenPCM()

    with pytest.raises(OSError, match="disk full"):
        sink.write(b"audio", 42)

    assert sink.capture_integrity_failed is True
    assert sink.capture_integrity_reason == "pcm-write-failed"
    assert sink.capture_anomaly_reason == "pcm-write-failed"


def test_timeline_write_failure_is_alertable_while_pcm_remains(tmp_path):
    class PCMFile:
        def write(self, data):
            return len(data)

    class BrokenTimeline:
        def write(self, _data):
            raise OSError("timeline disk error")

    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "timeline-write-failure",
    )
    sink._files[42] = PCMFile()
    sink._timeline_file = BrokenTimeline()

    sink.write(b"audio", 42)

    assert sink.total_bytes == 5
    assert sink.capture_anomaly_reason == "timeline-write-failed"


def test_metadata_write_failure_latches_in_memory_alert(tmp_path, monkeypatch):
    sink = PersistentPCMSink(
        1,
        "meeting",
        "listen",
        session_dir=tmp_path / "metadata-write-failure",
    )
    original_replace = os.replace

    def fail_metadata_replace(source, destination):
        if Path(destination).name == "metadata.json":
            raise OSError("metadata disk error")
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_metadata_replace)

    with pytest.raises(OSError, match="metadata disk error"):
        sink.mark("recording")

    assert sink.capture_anomaly_reason == "metadata-write-failed"
    monkeypatch.setattr(os, "replace", original_replace)
    sink.mark("capture_suspicious")
    assert sink.read_metadata()["metadata_write_error"] == "OSError"


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


def test_capture_interruption_survives_pycord_callback_then_cleanup(
    tmp_path,
    monkeypatch,
):
    """Pycord invokes the callback before Sink.cleanup; the gap must stay sticky."""
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    sink = PersistentPCMSink(
        1,
        "会議",
        "listen",
        session_dir=tmp_path / "1" / "callback-order",
    )
    sink.write(b"audio", SimpleNamespace(id=7))

    # This is the real Pycord order: our completion callback marks the gap,
    # then Pycord itself calls Sink.cleanup().
    sink.mark(
        "capture_interrupted",
        capture_interruption_reason="recorder-stopped-unexpectedly",
    )
    sink.cleanup()

    metadata = sink.read_metadata()
    assert metadata["status"] == "capture_interrupted"
    assert metadata["capture_integrity_failed"] is True
    assert metadata["capture_integrity_reason"] == "recorder-stopped-unexpectedly"
    # The interruption remains sticky, but its fsynced manifest is complete.
    # It is therefore eligible only for visibly partial automatic processing;
    # the raw bytes remain retained throughout that path.
    assert [item.session_dir for item in find_recoverable_recordings()] == [
        sink.session_dir
    ]


def test_sticky_capture_integrity_is_not_recoverable_after_later_checkpoint(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    sink = PersistentPCMSink(
        1,
        "会議",
        "listen",
        session_dir=tmp_path / "1" / "sticky-gap",
    )
    sink.write(b"audio", SimpleNamespace(id=7))
    sink.mark("capture_suspicious", capture_warning_reason="audio-stalled")
    # Processing checkpoints may change the visible status, but they cannot
    # make a known-partial capture eligible for automatic publication.
    sink.mark("final_reprocess_processing")

    metadata = sink.read_metadata()
    assert metadata["status"] == "final_reprocess_processing"
    assert metadata["capture_integrity_failed"] is True
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
    monkeypatch.setattr("bot.recording._recording_admission_bytes", 0)
    monkeypatch.setenv("YAGAPON_MIN_RECORDING_HEADROOM_BYTES", "1")
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


def test_recording_admission_rejects_before_capture_when_headroom_is_unavailable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("bot.recording._recording_admission_bytes", 0)
    monkeypatch.setenv("YAGAPON_RECORDING_FREE_SPACE_RESERVE_BYTES", "100")
    monkeypatch.setenv("YAGAPON_MIN_RECORDING_HEADROOM_BYTES", "20")
    monkeypatch.setattr(
        "bot.recording.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=119, used=0, free=119),
    )

    with pytest.raises(RecordingLimitExceeded, match="headroom"):
        PersistentPCMSink(
            1,
            "meeting",
            "listen",
            session_dir=tmp_path / "insufficient",
        )


def test_recording_admission_reservation_prevents_concurrent_overcommit(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("bot.recording._recording_admission_bytes", 0)
    monkeypatch.setenv("YAGAPON_RECORDING_FREE_SPACE_RESERVE_BYTES", "100")
    monkeypatch.setenv("YAGAPON_MIN_RECORDING_HEADROOM_BYTES", "20")
    monkeypatch.setattr(
        "bot.recording.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=135, used=0, free=135),
    )
    first = PersistentPCMSink(
        1,
        "first",
        "listen",
        session_dir=tmp_path / "first",
    )

    with pytest.raises(RecordingLimitExceeded, match="headroom"):
        PersistentPCMSink(
            2,
            "second",
            "listen",
            session_dir=tmp_path / "second",
        )

    first.cleanup()
    replacement = PersistentPCMSink(
        2,
        "replacement",
        "listen",
        session_dir=tmp_path / "replacement",
    )
    replacement.cleanup()


def test_zero_session_limit_uses_disk_headroom_instead_of_truncating(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("bot.recording._recording_admission_bytes", 0)
    monkeypatch.setenv("YAGAPON_MAX_RECORDING_BYTES", "0")
    monkeypatch.setenv("YAGAPON_RECORDING_FREE_SPACE_RESERVE_BYTES", "100")
    monkeypatch.setenv("YAGAPON_MIN_RECORDING_HEADROOM_BYTES", "20")
    monkeypatch.setattr(
        "bot.recording.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1_100, used=0, free=1_100),
    )
    sink = PersistentPCMSink(
        1,
        "long meeting",
        "listen",
        session_dir=tmp_path / "disk-limited",
    )

    sink.write(b"x" * 900, SimpleNamespace(id=7))
    sink.cleanup()

    assert sink.total_bytes == 900
    assert sink.limit_reached is False


def test_cleanup_never_removes_expired_recording_or_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    old = datetime.now(timezone.utc) - timedelta(days=8)
    delivered = tmp_path / "1" / "delivered"
    failed = tmp_path / "1" / "failed"
    delivered.mkdir(parents=True)
    failed.mkdir(parents=True)
    (delivered / "metadata.json").write_text(
        json.dumps({"status": "delivered_audio_deleted", "updated_at": old.isoformat()})
    )
    (failed / "metadata.json").write_text(json.dumps({"status": "transcription_failed", "updated_at": old.isoformat()}))

    assert cleanup_old_recordings(retention_days=7) == 0
    assert delivered.exists()
    assert (delivered / "metadata.json").exists()
    assert failed.exists()


def test_cleanup_quarantines_expired_incomplete_audio_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    old = datetime.now(timezone.utc) - timedelta(days=8)
    interrupted = tmp_path / "1" / "interrupted"
    interrupted.mkdir(parents=True)
    (interrupted / "user-1.pcm").write_bytes(b"audio")
    (interrupted / "metadata.json").write_text(
        json.dumps({"status": "capture_interrupted", "updated_at": old.isoformat()})
    )

    assert cleanup_old_recordings(retention_days=7) == 0
    assert interrupted.exists()
    assert (interrupted / "user-1.pcm").read_bytes() == b"audio"


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


def test_authoritative_checkpoint_promotes_transcript_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    sink = PersistentPCMSink(
        1,
        "全体会議",
        "listen",
        session_dir=tmp_path / "recordings" / "1" / "session-verified",
    )
    sink.write(b"audio", SimpleNamespace(id=42))
    transcript = sink.save_transcript("[Speaker 1]: 発言です")

    assert load_transcript_provenance(transcript)["transcript_complete"] is False
    sink.checkpoint(
        offsets={42: 5},
        speaker_labels={42: "Speaker 1"},
        transcript_path=transcript,
        status="final_reprocess_completed",
        transcript_complete=True,
    )

    assert sink.read_metadata()["transcript_complete"] is True
    assert load_transcript_provenance(transcript)["transcript_complete"] is True


def test_cleanup_keeps_recording_metadata_and_separate_transcript(tmp_path, monkeypatch):
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

    assert cleanup_old_recordings(retention_days=7) == 0
    assert session.exists()
    assert (session / "metadata.json").exists()
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


def test_successful_replay_archives_transcript_and_retains_pcm_timeline(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session = tmp_path / "recordings" / "1" / "session-1"
    session.mkdir(parents=True)
    (session / "user-10.pcm").write_bytes(b"audio")
    (session / "timeline.jsonl").write_text("timeline", encoding="utf-8")
    (session / "metadata.json").write_text(
        json.dumps(
                {
                    "guild_id": "1",
                    "status": "delivered_recording_retained",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "capture_ended_at": datetime.now(timezone.utc).isoformat(),
                    "total_bytes": len(b"audio"),
                    "durable_total_bytes": len(b"audio"),
                    "track_bytes": {"10": len(b"audio")},
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
    assert deleted == 0
    assert (session / "user-10.pcm").read_bytes() == b"audio"
    assert (session / "timeline.jsonl").read_text(encoding="utf-8") == "timeline"
    assert load_transcript_content(transcript) == "[Speaker 1]: 保存済み"
    provenance = load_transcript_provenance(transcript)
    assert provenance["transcript_complete"] is True
    assert metadata["status"] == "delivered_recording_retained"
    assert metadata["raw_recording_retained"] is True
    assert metadata["raw_audio_deleted_after_replay"] is False
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


def test_replay_finalization_does_not_publish_unverified_legacy_pcm(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session = tmp_path / "recordings" / "1" / "legacy-session"
    session.mkdir(parents=True)
    (session / "user-10.pcm").write_bytes(b"legacy-audio")
    (session / "metadata.json").write_text(
        json.dumps(
            {
                "guild_id": "1",
                "status": "delivered_recording_retained",
                "capture_ended_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    finalize_recording_replay(
        guild_id=1,
        session_id="legacy-session",
        session_dir=session,
        channel_name="全体会議",
        transcript="[Speaker 1]: 未検証",
        replay_job_id="replay-test",
    )

    assert not (tmp_path / "transcripts" / "1" / "legacy-session.md").exists()
    metadata = json.loads((session / "metadata.json").read_text(encoding="utf-8"))
    assert "replay_job_id" not in metadata


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


def test_live_capture_resume_never_inherits_recorded_terminal_state(tmp_path):
    session_dir = tmp_path / "session-live-resume"
    sink = PersistentPCMSink(1, "会議", "listen", session_dir=session_dir)
    sink.write(b"audio", SimpleNamespace(id=1))
    sink.cleanup()
    assert sink.read_metadata()["status"] == "recorded"

    resumed = PersistentPCMSink(
        1,
        "会議",
        "listen",
        session_dir=session_dir,
        resume=True,
        continue_capture=True,
    )

    assert resumed.read_metadata()["status"] == "recording"
    assert resumed.capture_health().wall_seconds >= 0


def test_live_capture_resume_counts_existing_pcm_before_free_space_headroom(
    tmp_path,
    monkeypatch,
):
    session_dir = tmp_path / "session-resume-headroom"
    session_dir.mkdir()
    (session_dir / "user-1.pcm").write_bytes(b"a" * 80)
    (session_dir / "metadata.json").write_text(
        json.dumps(
            {
                "guild_id": "1",
                "status": "capture_interrupted",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("YAGAPON_RECORDING_FREE_SPACE_RESERVE_BYTES", "100")
    monkeypatch.setenv("YAGAPON_MIN_RECORDING_HEADROOM_BYTES", "1")
    monkeypatch.setattr("bot.recording._recording_admission_bytes", 0)
    monkeypatch.setattr(
        "bot.recording.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=130),
    )

    resumed = PersistentPCMSink(
        1,
        "会議",
        "listen",
        session_dir=session_dir,
        max_bytes=100,
        resume=True,
        continue_capture=True,
    )
    resumed.write(b"b" * 20, SimpleNamespace(id=1))

    assert resumed.total_bytes == 100
    assert resumed.limit_reached is False


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
        ("recover", "recorded", None),
        ("crashed-live", "recording", None),
        ("shutdown", "shutdown_pending", None),
        ("final-pass", "final_reprocess_processing", None),
        ("done", "delivered_audio_deleted", None),
        ("later", "recovery_retry", (now + timedelta(hours=1)).isoformat()),
    ):
        session = tmp_path / "1" / name
        session.mkdir(parents=True)
        (session / "user-1.pcm").write_bytes(b"audio")
        (session / "metadata.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "updated_at": now.isoformat(),
                    "next_retry_at": retry_at,
                    "capture_ended_at": (
                        now.isoformat()
                        if name in {"recover", "final-pass", "later"}
                        else ""
                    ),
                    "total_bytes": len(b"audio"),
                    "durable_total_bytes": len(b"audio"),
                    "track_bytes": {"1": len(b"audio")},
                }
            )
        )

    candidates = find_recoverable_recordings(now=now)

    assert [item.session_dir.name for item in candidates] == ["final-pass", "recover"]


def test_legacy_recording_without_size_manifest_is_manual_review_only(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    session = tmp_path / "1" / "legacy-without-manifest"
    session.mkdir(parents=True)
    (session / "user-1.pcm").write_bytes(b"legacy-audio")
    now = datetime.now(timezone.utc)
    (session / "metadata.json").write_text(
        json.dumps(
            {
                "guild_id": "1",
                "channel_name": "legacy meeting",
                "mode": "listen",
                "status": "recorded",
                "started_at": now.isoformat(),
                "capture_ended_at": now.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    assert find_recoverable_recordings(now=now) == []

    resumed = PersistentPCMSink(
        1,
        "legacy meeting",
        "listen",
        session_dir=session,
        resume=True,
    )
    resumed.mark(
        "recovery_retry",
        next_retry_at=(now - timedelta(seconds=1)).isoformat(),
    )
    metadata = resumed.read_metadata()

    assert "track_bytes" not in metadata
    assert "total_bytes" not in metadata
    assert "durable_total_bytes" not in metadata
    # It remains retained and manually replayable, but cannot be promoted by
    # automatic Drive/Knowledge/Discord recovery without a trusted manifest.
    assert find_recoverable_recordings(now=now) == []


def test_partial_legacy_manifest_never_becomes_auto_recovery_trusted(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    session = tmp_path / "1" / "partial-manifest"
    session.mkdir(parents=True)
    pcm = b"legacy-audio"
    (session / "user-1.pcm").write_bytes(pcm)
    now = datetime.now(timezone.utc)
    metadata = {
        "guild_id": "1",
        "channel_name": "legacy meeting",
        "mode": "listen",
        "status": "recovery_retry",
        "started_at": now.isoformat(),
        "capture_ended_at": now.isoformat(),
        # Old builds sometimes persisted only this aggregate. It cannot prove
        # per-track completeness or that the final fsync tail survived.
        "total_bytes": len(pcm),
        "next_retry_at": (now - timedelta(seconds=1)).isoformat(),
    }
    (session / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    validation = validate_recording_raw(session, metadata)

    assert validation.valid is True
    assert validation.checked_against_manifest is False
    assert find_recoverable_recordings(now=now) == []


def test_quarantine_preserves_stale_active_source_bytes_and_fails_closed(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    session = tmp_path / "1" / "orphaned-recording"
    session.mkdir(parents=True)
    pcm_bytes = b"pcm-prefix-must-survive"
    timeline_bytes = b'{"v":1,"sequence":0,"user_id":1,"start":0,"end":23}\n'
    (session / "user-1.pcm").write_bytes(pcm_bytes)
    (session / "timeline.jsonl").write_bytes(timeline_bytes)
    (session / "metadata.json").write_text(
        json.dumps(
            {
                "status": "recording",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    stale = find_stale_active_recordings()
    assert [item.session_dir for item in stale] == [session]

    metadata = quarantine_stale_recording(stale[0])

    assert metadata["status"] == "capture_interrupted"
    assert metadata["capture_integrity_failed"] is True
    assert (session / "user-1.pcm").read_bytes() == pcm_bytes
    assert (session / "timeline.jsonl").read_bytes() == timeline_bytes
    assert find_recoverable_recordings() == []


def test_legacy_shutdown_pending_is_quarantined_for_review_not_auto_recovery(
    tmp_path,
    monkeypatch,
):
    recording_dir = tmp_path / "recordings"
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(recording_dir))
    monkeypatch.setenv("YAGAPON_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    session = recording_dir / "1" / "legacy-shutdown"
    session.mkdir(parents=True)
    pcm_bytes = b"legacy-shutdown-pcm-prefix"
    (session / "user-1.pcm").write_bytes(pcm_bytes)
    (session / "metadata.json").write_text(
        json.dumps(
            {
                "guild_id": "1",
                "channel_id": "123",
                "channel_name": "legacy meeting",
                "mode": "listen",
                "status": "shutdown_pending",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "capture_ended_at": "",
            }
        ),
        encoding="utf-8",
    )

    assert find_recoverable_recordings() == []
    stale = find_stale_active_recordings()
    assert [item.session_dir for item in stale] == [session]

    quarantined = quarantine_stale_recording(stale[0])

    assert quarantined["status"] == "capture_interrupted"
    assert quarantined["orphan_notification_status"] == "pending"
    assert (session / "user-1.pcm").read_bytes() == pcm_bytes
    assert find_recoverable_recordings() == []

    from bot.recording_replay import (
        list_recording_artifacts,
        validate_recording_artifact_for_review,
    )

    artifact = next(
        item
        for item in list_recording_artifacts()
        if item.session_id == "legacy-shutdown"
    )
    assert artifact.status == "capture_interrupted"
    assert artifact.capture_integrity_failed is True
    assert artifact.raw_manifest_verified is False
    assert validate_recording_artifact_for_review(artifact)["status"] == (
        "capture_interrupted"
    )


@pytest.mark.parametrize("metadata_state", ["missing", "corrupt"])
def test_pcm_without_valid_metadata_is_discovered_and_quarantined(
    tmp_path,
    monkeypatch,
    metadata_state,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    session = tmp_path / "123" / f"orphan-{metadata_state}"
    session.mkdir(parents=True)
    pcm = b"irreplaceable-raw-pcm"
    (session / "user-42.pcm").write_bytes(pcm)
    if metadata_state == "corrupt":
        (session / "metadata.json").write_text("{torn", encoding="utf-8")

    stale = find_stale_active_recordings()

    assert [item.session_dir for item in stale] == [session]
    candidate = stale[0]
    assert candidate.metadata["capture_integrity_failed"] is True
    assert candidate.metadata["capture_integrity_reason"] == (
        f"recording-metadata-{metadata_state}"
    )
    assert find_recoverable_recordings() == []

    quarantined = quarantine_stale_recording(candidate)

    assert (session / "user-42.pcm").read_bytes() == pcm
    assert quarantined["status"] == "capture_interrupted"
    assert quarantined["capture_integrity_failed"] is True
    assert quarantined["track_bytes"] == {"42": len(pcm)}
    assert stat.S_IMODE((session / "metadata.json").stat().st_mode) == 0o600
    if metadata_state == "corrupt":
        backups = list(session.glob("metadata.corrupt-*.json"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "{torn"
    assert find_recoverable_recordings() == []


def test_raw_validation_rejects_truncation_and_zero_length_track(tmp_path):
    session = tmp_path / "recording"
    session.mkdir()
    first = session / "user-1.pcm"
    second = session / "user-2.pcm"
    first.write_bytes(b"first-track")
    second.write_bytes(b"second-track")
    expected_total = first.stat().st_size + second.stat().st_size
    metadata = {
        "capture_ended_at": datetime.now(timezone.utc).isoformat(),
        "total_bytes": expected_total,
        "durable_total_bytes": expected_total,
        "track_bytes": {
            "1": first.stat().st_size,
            "2": second.stat().st_size,
        },
    }

    complete = validate_recording_raw(session, metadata)
    assert complete.valid is True
    assert complete.checked_against_manifest is True

    first.write_bytes(b"cut")
    truncated = validate_recording_raw(session, metadata)
    assert truncated.valid is False
    assert truncated.reason == "raw-track-size-mismatch"

    metadata["track_bytes"]["1"] = 0
    metadata["total_bytes"] -= len(b"first-track")
    metadata["durable_total_bytes"] = metadata["total_bytes"]
    first.write_bytes(b"")
    empty = validate_recording_raw(session, metadata)
    assert empty.valid is False
    assert empty.reason == "raw-track-empty"


def test_recoverable_recording_rejects_pcm_changed_after_final_checkpoint(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    session = tmp_path / "1" / "truncated-final"
    session.mkdir(parents=True)
    pcm = session / "user-1.pcm"
    pcm.write_bytes(b"complete-audio")
    now = datetime.now(timezone.utc).isoformat()
    (session / "metadata.json").write_text(
        json.dumps(
            {
                "status": "recorded",
                "capture_ended_at": now,
                "total_bytes": len(b"complete-audio"),
                "durable_total_bytes": len(b"complete-audio"),
                "track_bytes": {"1": len(b"complete-audio")},
            }
        ),
        encoding="utf-8",
    )
    pcm.write_bytes(b"cut")

    assert find_recoverable_recordings() == []


def test_unfinished_delivery_is_stale_not_normally_recoverable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path))
    session = tmp_path / "1" / "delivery-claimed-before-capture-ended"
    session.mkdir(parents=True)
    (session / "user-1.pcm").write_bytes(b"captured-prefix")
    (session / "metadata.json").write_text(
        json.dumps(
            {
                "status": "delivery_in_progress",
                "started_at": datetime.now(timezone.utc).isoformat(),
                # Deliberately no capture_ended_at: the old process may have
                # died while its receiver was still open.
            }
        ),
        encoding="utf-8",
    )

    assert find_recoverable_recordings() == []
    stale = find_stale_active_recordings()
    assert [item.session_dir for item in stale] == [session]

    quarantine_stale_recording(stale[0])
    assert find_recoverable_recordings() == []
