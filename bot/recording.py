"""Durable, bounded Discord voice recording storage."""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from discord.sinks import Sink

PCM_CHANNELS = 2
PCM_SAMPLE_RATE = 48_000
PCM_SAMPLE_WIDTH = 2
DEFAULT_MAX_RECORDING_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_FREE_SPACE_RESERVE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 7


def recording_root() -> Path:
    configured = os.environ.get("YAGAPON_RECORDING_DIR")
    if configured:
        return Path(configured)
    config_path = Path(
        os.environ.get("YAGAPON_CONFIG_PATH", Path(__file__).parent.parent / "data" / "server_config.json")
    )
    return config_path.parent / "recordings"


class RecordingLimitExceeded(RuntimeError):
    """Raised once a session reaches its configured durable storage limit."""


class PersistentPCMSink(Sink):
    """Write decoded PCM directly to disk instead of growing ``BytesIO`` objects.

    Pycord calls ``write`` from its packet-router thread. Reads used by realtime
    transcription happen from the asyncio thread, so all file operations are
    protected by a regular thread lock and are invoked through ``to_thread``.
    """

    def __init__(
        self,
        guild_id: int,
        channel_name: str,
        mode: str,
        *,
        session_dir: Path | None = None,
        max_bytes: int | None = None,
    ):
        super().__init__()
        started_at = datetime.now(timezone.utc)
        self.session_dir = session_dir or (
            recording_root() / str(guild_id) / f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        )
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.guild_id = guild_id
        self.channel_name = channel_name
        self.mode = mode
        configured_max = max_bytes or int(os.environ.get("YAGAPON_MAX_RECORDING_BYTES", DEFAULT_MAX_RECORDING_BYTES))
        reserve = int(
            os.environ.get(
                "YAGAPON_RECORDING_FREE_SPACE_RESERVE_BYTES",
                DEFAULT_FREE_SPACE_RESERVE_BYTES,
            )
        )
        available_for_recording = max(0, shutil.disk_usage(self.session_dir).free - reserve)
        self.max_bytes = min(configured_max, available_for_recording)
        self._lock = threading.RLock()
        self._files: dict[int, object] = {}
        self._total_bytes = sum(path.stat().st_size for path in self.session_dir.glob("user-*.pcm"))
        self._limit_reached = False
        self._closed = False
        self._write_metadata("recording", started_at=started_at.isoformat())

    @staticmethod
    def _user_id(user) -> int | None:
        value = getattr(user, "id", user)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _pcm_path(self, user_id: int) -> Path:
        return self.session_dir / f"user-{user_id}.pcm"

    def _write_metadata(self, status: str, **extra) -> None:
        path = self.session_dir / "metadata.json"
        current = {}
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {}
        current.update(
            {
                "guild_id": str(self.guild_id),
                "channel_name": self.channel_name,
                "mode": self.mode,
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "pcm_format": {
                    "channels": PCM_CHANNELS,
                    "sample_rate": PCM_SAMPLE_RATE,
                    "sample_width": PCM_SAMPLE_WIDTH,
                },
                **extra,
            }
        )
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def write(self, data, user) -> None:
        pcm_data = getattr(data, "pcm", data)
        if not isinstance(pcm_data, (bytes, bytearray, memoryview)):
            return
        user_id = self._user_id(user)
        if user_id is None or not pcm_data:
            return
        with self._lock:
            if self._closed or self._limit_reached:
                return
            remaining = self.max_bytes - self._total_bytes
            if remaining <= 0:
                self._limit_reached = True
                self._write_metadata("storage_limit_reached", total_bytes=self._total_bytes)
                return
            chunk = bytes(pcm_data[:remaining])
            file = self._files.get(user_id)
            if file is None:
                # Discord supplies small packets frequently. A bounded file buffer
                # avoids one disk syscall per packet; read_chunks flushes it before
                # transcription and cleanup fsyncs it before closing.
                file = self._pcm_path(user_id).open("ab", buffering=256 * 1024)
                self._files[user_id] = file
            file.write(chunk)
            self._total_bytes += len(chunk)
            if len(chunk) < len(pcm_data):
                self._limit_reached = True
                self._write_metadata("storage_limit_reached", total_bytes=self._total_bytes)

    def read_chunks(
        self,
        offsets: dict[int, int],
        *,
        chunk_bytes: int,
    ) -> dict[int, tuple[bytes, int]]:
        """Return at most one new chunk per user without changing caller offsets."""
        chunks: dict[int, tuple[bytes, int]] = {}
        with self._lock:
            for file in self._files.values():
                file.flush()
            paths = list(self.session_dir.glob("user-*.pcm"))
            for path in paths:
                try:
                    user_id = int(path.stem.removeprefix("user-"))
                except ValueError:
                    continue
                start = max(0, offsets.get(user_id, 0))
                size = path.stat().st_size
                if size <= start:
                    continue
                with path.open("rb") as source:
                    source.seek(start)
                    data = source.read(chunk_bytes)
                if data:
                    chunks[user_id] = (data, start + len(data))
        return chunks

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def limit_reached(self) -> bool:
        with self._lock:
            return self._limit_reached

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for file in self._files.values():
                try:
                    file.flush()
                    os.fsync(file.fileno())
                finally:
                    file.close()
            self._files.clear()
            status = "storage_limit_reached" if self._limit_reached else "recorded"
            self._write_metadata(status, total_bytes=self._total_bytes)

    def format_audio(self, audio) -> None:  # pragma: no cover - base cleanup is overridden
        return

    def mark(self, status: str, **extra) -> None:
        with self._lock:
            self._write_metadata(status, total_bytes=self._total_bytes, **extra)


def cleanup_old_recordings(*, now: datetime | None = None, retention_days: int | None = None) -> int:
    """Remove completed recordings after the retention window; preserve failures."""
    current = now or datetime.now(timezone.utc)
    keep_days = (
        retention_days
        if retention_days is not None
        else int(os.environ.get("YAGAPON_RECORDING_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    )
    cutoff = current - timedelta(days=max(1, keep_days))
    removed = 0
    root = recording_root()
    if not root.exists():
        return 0
    for metadata_path in root.glob("*/*/metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            status = metadata.get("status")
            updated_at = datetime.fromisoformat(metadata["updated_at"])
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if status == "delivered" and updated_at < cutoff:
                shutil.rmtree(metadata_path.parent)
                removed += 1
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return removed
