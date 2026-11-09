import io
import json
from types import SimpleNamespace

from bot.recording_import import import_drive_recording
from bot.recording_replay import validate_recording_artifact_for_replay


class FakeReader:
    document = SimpleNamespace(
        file_id="audio-id",
        name="全体会議.m4a",
        mime_type="audio/mp4",
        web_view_link="https://drive.google.com/file/d/audio-id/view",
    )

    def get_document(self, _file_id):
        return self.document

    def is_in_scope(self, _document):
        return True

    def download_binary(self, _document, destination, *, max_bytes):
        assert max_bytes > 0
        destination.write_bytes(b"source")
        return 6


class FakeProcess:
    def __init__(self, *_args, **_kwargs):
        # One second of 48 kHz stereo signed 16-bit PCM.
        self.stdout = io.BytesIO(b"\x01\x00\x01\x00" * 48_000)
        self.returncode = None

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def test_drive_recording_import_creates_verified_durable_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RECORDING_DIR", str(tmp_path / "recordings"))
    monkeypatch.setenv("YAGAPON_RECORDING_FREE_SPACE_RESERVE_BYTES", "0")
    monkeypatch.setattr(
        "bot.recording_import.DriveReader",
        lambda **_kwargs: FakeReader(),
    )
    monkeypatch.setattr("bot.recording_import._probe_duration", lambda _path: 1.0)
    monkeypatch.setattr("bot.recording_import.subprocess.Popen", FakeProcess)
    config = SimpleNamespace(
        get_drive_folder=lambda _guild_id: "https://drive.google.com/drive/folders/root-id"
    )

    imported = import_drive_recording(
        config,
        guild_id=123,
        channel_id=456,
        category_id=789,
        category_name="会議",
        actor_id=42,
        drive_url="https://drive.google.com/file/d/audio-id/view",
        title="全体会議",
    )

    artifact = imported.artifact
    assert artifact.raw_bytes == 192_000
    assert artifact.raw_integrity_valid is True
    assert artifact.raw_manifest_verified is True
    assert artifact.capture_integrity_failed is False
    metadata = json.loads((artifact.session_dir / "metadata.json").read_text())
    assert metadata["status"] == "delivered_recording_retained"
    assert metadata["external_import"] is True
    assert metadata["external_export_reason"] == "operator-explicit-import"
    assert metadata["imported_source_file_id"] == "audio-id"
    validate_recording_artifact_for_replay(
        artifact,
        retranscribe=True,
        config=SimpleNamespace(is_ignored=lambda _guild_id, _channel_id: False),
    )
