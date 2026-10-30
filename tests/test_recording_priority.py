import os

from bot.recording_priority import RecordingActivityLease, recording_is_active


def test_recording_activity_lease_coordinates_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_ACTIVITY_DIR", str(tmp_path))
    lease = RecordingActivityLease(123, "session-1")

    lease.acquire()
    assert recording_is_active()
    lease.heartbeat()
    assert recording_is_active()

    lease.release()
    assert not recording_is_active()


def test_stale_recording_activity_lease_is_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_ACTIVITY_DIR", str(tmp_path))
    monkeypatch.setenv("YAGAPON_RECORDING_ACTIVITY_STALE_SECONDS", "10")
    lease = RecordingActivityLease(123, "session-2")
    lease.acquire()
    os.utime(lease.path, (100, 100))

    assert not recording_is_active(now=111)
    assert not lease.path.exists()
