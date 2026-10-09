import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.recording import PersistentPCMSink, cleanup_old_recordings


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
