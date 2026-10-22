"""Durable, bounded Discord voice recording storage."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
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
TIMELINE_MANIFEST_NAME = "timeline.jsonl"
TIMELINE_SCHEMA_VERSION = 1
DEFAULT_TIMELINE_TURN_GAP_SECONDS = 1.25
RECOVERABLE_STATUSES = {
    "processing",
    "recording",
    "recorded",
    "shutdown_pending",
    "leave_failed",
    "automatic_leave_failed",
    "transcription_failed",
    "minutes_generation_failed",
    "minutes_generated",
    "manual_delivery_pending",
    "delivery_in_progress",
    "automatic_leave_failed",
    "final_reprocess_processing",
    "final_reprocess_deferred",
    "final_reprocess_empty",
    "final_reprocess_completed",
    "recovery_retry",
    "recovery_processing",
}
TERMINAL_STATUSES = {
    "delivered",
    "delivered_audio_deleted",
    "delivered_recording_retained",
    "completed_without_minutes",
    "recovered",
    "recovery_dead_letter",
    "start_failed",
    "recording_truncated",
    "storage_limit_reached",
    "capture_interrupted",
}
REPLAY_AUDIO_DELETION_STATUSES = {
    "delivered_recording_retained",
    "recovery_dead_letter",
}


@dataclass(frozen=True)
class RecoverableRecording:
    session_dir: Path
    metadata: dict


@dataclass(frozen=True)
class RecordingTimelineSegment:
    """A chronological utterance slice referencing one user's durable PCM."""

    sequence: int
    user_id: int
    start: int
    end: int
    received_at_ns: int
    ended_at_ns: int


log = logging.getLogger("yagapon.recording")


def _pcm_file_sizes(session_dir: Path) -> dict[int, int]:
    sizes: dict[int, int] = {}
    for path in session_dir.glob("user-*.pcm"):
        try:
            user_id = int(path.stem.removeprefix("user-"))
            sizes[user_id] = path.stat().st_size
        except (OSError, ValueError):
            continue
    return sizes


def load_recording_timeline_segments(
    session_dir: Path,
    *,
    max_bytes: int,
    gap_seconds: float = DEFAULT_TIMELINE_TURN_GAP_SECONDS,
) -> list[RecordingTimelineSegment] | None:
    """Load a complete receive-order manifest and coalesce it into utterances.

    ``None`` deliberately means "use the legacy per-user fallback". A timeline
    is used only when it covers every byte in every PCM file exactly; this keeps
    a missing/truncated manifest from silently dropping old or crash-tail audio.
    """
    manifest = session_dir / TIMELINE_MANIFEST_NAME
    sizes = _pcm_file_sizes(session_dir)
    if not manifest.is_file():
        return None
    if not sizes:
        return []

    expected_offsets: dict[int, int] = {}
    completed: list[RecordingTimelineSegment] = []
    current: RecordingTimelineSegment | None = None
    previous_sequence = -1
    gap_ns = max(0, int(gap_seconds * 1_000_000_000))

    try:
        with manifest.open("rb") as source:
            for raw_line in source:
                if not raw_line.strip():
                    continue
                item = json.loads(raw_line)
                if int(item.get("v", 0)) != TIMELINE_SCHEMA_VERSION:
                    return None
                sequence = int(item["sequence"])
                user_id = int(item["user_id"])
                start = int(item["start"])
                end = int(item["end"])
                received_at_ns = int(item["received_at_ns"])
                if (
                    sequence != previous_sequence + 1
                    or user_id not in sizes
                    or start != expected_offsets.get(user_id, 0)
                    or end <= start
                    or end > sizes[user_id]
                    or received_at_ns <= 0
                ):
                    return None
                previous_sequence = sequence
                expected_offsets[user_id] = end

                if (
                    current is not None
                    and user_id == current.user_id
                    and start == current.end
                    and 0 <= received_at_ns - current.ended_at_ns <= gap_ns
                ):
                    current = RecordingTimelineSegment(
                        sequence=current.sequence,
                        user_id=user_id,
                        start=current.start,
                        end=end,
                        received_at_ns=current.received_at_ns,
                        ended_at_ns=received_at_ns,
                    )
                else:
                    if current is not None:
                        completed.append(current)
                    current = RecordingTimelineSegment(
                        sequence=sequence,
                        user_id=user_id,
                        start=start,
                        end=end,
                        received_at_ns=received_at_ns,
                        ended_at_ns=received_at_ns,
                    )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None

    if previous_sequence < 0 or any(expected_offsets.get(user_id, 0) != size for user_id, size in sizes.items()):
        return None

    if current is not None:
        completed.append(current)
    bounded_size = max(1, int(max_bytes))
    result: list[RecordingTimelineSegment] = []
    for segment in completed:
        start = segment.start
        while start < segment.end:
            end = min(segment.end, start + bounded_size)
            result.append(
                RecordingTimelineSegment(
                    sequence=segment.sequence,
                    user_id=segment.user_id,
                    start=start,
                    end=end,
                    received_at_ns=segment.received_at_ns,
                    ended_at_ns=segment.ended_at_ns,
                )
            )
            start = end
    return result


def read_recording_timeline_audio(
    session_dir: Path,
    segment: RecordingTimelineSegment,
) -> bytes:
    """Read one validated timeline slice, failing closed if PCM changed."""
    path = session_dir / f"user-{segment.user_id}.pcm"
    expected = segment.end - segment.start
    with path.open("rb") as source:
        source.seek(segment.start)
        data = source.read(expected)
    if len(data) != expected:
        raise OSError("recording timeline references incomplete PCM")
    return data


def recording_root() -> Path:
    configured = os.environ.get("YAGAPON_RECORDING_DIR")
    if configured:
        return Path(configured)
    config_path = Path(
        os.environ.get("YAGAPON_CONFIG_PATH", Path(__file__).parent.parent / "data" / "server_config.json")
    )
    return config_path.parent / "recordings"


def transcript_root() -> Path:
    """Permanent transcript archive, intentionally separate from raw audio."""
    configured = os.environ.get("YAGAPON_TRANSCRIPT_DIR")
    if configured:
        return Path(configured)
    return recording_root().parent / "transcripts"


def save_external_transcript(
    guild_id: int,
    session_id: str,
    channel_name: str,
    content: str,
    *,
    mode: str = "uploaded",
) -> Path | None:
    """Atomically archive a non-VC transcript under the same retention contract."""
    if not content.strip():
        return None
    safe_session_id = re.sub(r"[^A-Za-z0-9_.-]", "-", session_id)[:120]
    destination = transcript_root() / str(guild_id) / f"{safe_session_id}.md"
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    destination.parent.chmod(0o700)
    document = (
        f"# 文字起こし - {channel_name}\n\n"
        f"- セッション: `{safe_session_id}`\n"
        f"- モード: `{mode}`\n\n"
        "---\n\n"
        f"{content.rstrip()}\n"
    )
    temporary = destination.with_suffix(".md.tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, destination)
    return destination


def finalize_recording_replay(
    *,
    guild_id: int,
    session_id: str,
    session_dir: Path,
    channel_name: str,
    transcript: str,
    replay_job_id: str,
) -> int:
    """Archive a successful replay transcript, then remove its source PCM.

    Only sessions explicitly retained after a failed transcription (or moved to
    the recovery dead letter queue) are eligible. This prevents a replay racing
    with an active recording from deleting audio.
    """
    expected_dir = (recording_root() / str(guild_id) / session_id).resolve()
    resolved_dir = session_dir.resolve()
    if resolved_dir != expected_dir:
        raise ValueError("recording replay source is outside the recording archive")
    metadata_path = resolved_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if str(metadata.get("status") or "") not in REPLAY_AUDIO_DELETION_STATUSES:
        return 0
    archived = save_external_transcript(
        guild_id,
        session_id,
        channel_name,
        transcript,
        mode="recording-replay-finalized",
    )
    if archived is None or not archived.is_file() or archived.stat().st_size <= 0:
        raise RuntimeError("replay transcript could not be archived before audio deletion")
    deleted = 0
    for path in resolved_dir.glob("user-*.pcm"):
        try:
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            continue
    try:
        (resolved_dir / TIMELINE_MANIFEST_NAME).unlink()
    except FileNotFoundError:
        pass
    metadata.update(
        {
            "status": "delivered_audio_deleted",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "transcript_path": str(archived),
            "replay_job_id": replay_job_id,
            "raw_audio_deleted_after_replay": True,
            "deleted_pcm_files": deleted,
        }
    )
    temporary = metadata_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, metadata_path)
    return deleted


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
        channel_id: int | None = None,
        category_id: int | None = None,
        category_name: str = "",
        session_dir: Path | None = None,
        max_bytes: int | None = None,
        resume: bool = False,
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
        self._user_bytes = _pcm_file_sizes(self.session_dir)
        self._total_bytes = sum(self._user_bytes.values())
        self._timeline_path = self.session_dir / TIMELINE_MANIFEST_NAME
        self._timeline_file = None
        self._timeline_sequence = 0
        self._timeline_complete = self._prepare_timeline_for_append()
        existing_metadata = self.read_metadata() if resume else {}
        stored_channel_id = channel_id or existing_metadata.get("channel_id")
        try:
            self.channel_id = int(stored_channel_id) if stored_channel_id else None
        except (TypeError, ValueError):
            self.channel_id = None
        stored_category_id = category_id or existing_metadata.get("category_id")
        try:
            self.category_id = int(stored_category_id) if stored_category_id else None
        except (TypeError, ValueError):
            self.category_id = None
        self.category_name = str(category_name or existing_metadata.get("category_name") or "")
        self.channel_lineage_recorded = bool(not resume or existing_metadata.get("channel_lineage_recorded"))
        self._limit_reached = bool(existing_metadata.get("storage_limit_reached")) or (
            str(existing_metadata.get("status") or "") == "storage_limit_reached"
        )
        self._closed = False
        if resume:
            self._write_metadata(
                str(existing_metadata.get("status") or "recorded"),
                recovery_resumed_at=started_at.isoformat(),
            )
        else:
            self._write_metadata("recording", started_at=started_at.isoformat())

    def _prepare_timeline_for_append(self) -> bool:
        """Recover the next event id and trim only a crash-truncated tail."""
        if not self._timeline_path.exists():
            return self._total_bytes == 0
        expected_offsets: dict[int, int] = {}
        previous_sequence = -1
        valid_end = 0
        needs_newline = False
        complete = True
        try:
            with self._timeline_path.open("rb+") as source:
                while True:
                    line_start = source.tell()
                    raw_line = source.readline()
                    if not raw_line:
                        break
                    if not raw_line.strip():
                        valid_end = source.tell()
                        continue
                    try:
                        item = json.loads(raw_line)
                        sequence = int(item["sequence"])
                        user_id = int(item["user_id"])
                        start = int(item["start"])
                        end = int(item["end"])
                        if (
                            int(item.get("v", 0)) != TIMELINE_SCHEMA_VERSION
                            or sequence != previous_sequence + 1
                            or start != expected_offsets.get(user_id, 0)
                            or end <= start
                        ):
                            raise ValueError("invalid recording timeline event")
                    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                        complete = False
                        source.truncate(line_start)
                        break
                    previous_sequence = sequence
                    expected_offsets[user_id] = end
                    valid_end = source.tell()
                    needs_newline = not raw_line.endswith(b"\n")
                source.truncate(valid_end)
                if needs_newline:
                    source.seek(0, os.SEEK_END)
                    source.write(b"\n")
        except OSError:
            return False
        self._timeline_sequence = previous_sequence + 1
        if any(expected_offsets.get(user_id, 0) != size for user_id, size in self._user_bytes.items()):
            complete = False
        return complete

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
        metadata = {
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
        if self.channel_id is not None:
            metadata["channel_id"] = str(self.channel_id)
        if self.category_id is not None:
            metadata["category_id"] = str(self.category_id)
        metadata["category_name"] = self.category_name
        metadata["channel_lineage_recorded"] = self.channel_lineage_recorded
        metadata["timeline_manifest"] = TIMELINE_MANIFEST_NAME
        metadata["timeline_schema"] = TIMELINE_SCHEMA_VERSION
        metadata["timeline_complete"] = self._timeline_complete
        current.update(metadata)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def read_metadata(self) -> dict:
        path = self.session_dir / "metadata.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

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
                self._write_metadata(
                    "storage_limit_reached",
                    total_bytes=self._total_bytes,
                    storage_limit_reached=True,
                )
                return
            chunk = bytes(pcm_data[:remaining])
            file = self._files.get(user_id)
            if file is None:
                # Discord supplies small packets frequently. A bounded file buffer
                # avoids one disk syscall per packet; read_chunks flushes it before
                # transcription and cleanup fsyncs it before closing.
                file = self._pcm_path(user_id).open("ab", buffering=256 * 1024)
                self._files[user_id] = file
            start = self._user_bytes.get(user_id, 0)
            file.write(chunk)
            end = start + len(chunk)
            self._user_bytes[user_id] = end
            self._total_bytes += len(chunk)
            try:
                if self._timeline_file is None:
                    self._timeline_file = self._timeline_path.open("ab", buffering=256 * 1024)
                event = {
                    "v": TIMELINE_SCHEMA_VERSION,
                    "sequence": self._timeline_sequence,
                    "user_id": user_id,
                    "start": start,
                    "end": end,
                    "received_at_ns": time.time_ns(),
                }
                self._timeline_file.write(
                    json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
                )
                self._timeline_sequence += 1
            except OSError:
                # PCM remains the source of truth. The coverage validator will
                # select the lossless legacy fallback rather than omit this range.
                self._timeline_complete = False
                log.exception("Could not persist recording receive timeline")
            if len(chunk) < len(pcm_data):
                self._limit_reached = True
                self._write_metadata(
                    "storage_limit_reached",
                    total_bytes=self._total_bytes,
                    storage_limit_reached=True,
                )

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
            if self._timeline_file is not None:
                self._timeline_file.flush()
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

    def read_timeline_segments(
        self,
        *,
        max_bytes: int,
        gap_seconds: float = DEFAULT_TIMELINE_TURN_GAP_SECONDS,
    ) -> list[RecordingTimelineSegment] | None:
        """Return chronological utterance slices, or ``None`` for old recordings."""
        with self._lock:
            for file in self._files.values():
                file.flush()
            if self._timeline_file is not None:
                self._timeline_file.flush()
            return load_recording_timeline_segments(
                self.session_dir,
                max_bytes=max_bytes,
                gap_seconds=gap_seconds,
            )

    def read_timeline_audio(self, segment: RecordingTimelineSegment) -> bytes:
        with self._lock:
            file = self._files.get(segment.user_id)
            if file is not None:
                file.flush()
            return read_recording_timeline_audio(self.session_dir, segment)

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
            if self._timeline_file is not None:
                try:
                    self._timeline_file.flush()
                    os.fsync(self._timeline_file.fileno())
                finally:
                    self._timeline_file.close()
                    self._timeline_file = None
            status = "storage_limit_reached" if self._limit_reached else "recorded"
            self._write_metadata(
                status,
                total_bytes=self._total_bytes,
                storage_limit_reached=self._limit_reached,
            )

    def flush(self) -> None:
        """Synchronously persist every userspace PCM buffer without closing it."""
        with self._lock:
            for file in self._files.values():
                file.flush()
                os.fsync(file.fileno())
            if self._timeline_file is not None:
                self._timeline_file.flush()
                os.fsync(self._timeline_file.fileno())

    def format_audio(self, audio) -> None:  # pragma: no cover - base cleanup is overridden
        return

    def mark(self, status: str, **extra) -> None:
        with self._lock:
            self._write_metadata(status, total_bytes=self._total_bytes, **extra)

    def checkpoint(
        self,
        *,
        offsets: dict[int, int],
        speaker_labels: dict[int, str],
        transcript_path: Path | None = None,
        status: str = "processing",
        **extra,
    ) -> None:
        """Atomically persist restart-safe progress without transcript contents."""
        self.mark(
            status,
            offsets={str(key): int(value) for key, value in offsets.items()},
            speaker_labels={str(key): value for key, value in speaker_labels.items()},
            transcript_path=str(transcript_path or ""),
            **extra,
        )

    def save_transcript(self, content: str) -> Path | None:
        """Atomically persist a transcript outside the raw recording lifecycle."""
        return save_external_transcript(
            self.guild_id,
            self.session_dir.name,
            self.channel_name,
            content,
            mode=self.mode,
        )

    def delete_audio(self) -> int:
        """Delete raw PCM after the transcript has been durably preserved."""
        with self._lock:
            for file in self._files.values():
                try:
                    file.flush()
                    os.fsync(file.fileno())
                finally:
                    file.close()
            self._files.clear()
            if self._timeline_file is not None:
                try:
                    self._timeline_file.flush()
                    os.fsync(self._timeline_file.fileno())
                finally:
                    self._timeline_file.close()
                    self._timeline_file = None
            deleted = 0
            for path in self.session_dir.glob("user-*.pcm"):
                try:
                    path.unlink()
                    deleted += 1
                except FileNotFoundError:
                    continue
            try:
                self._timeline_path.unlink()
            except FileNotFoundError:
                pass
            return deleted


def cleanup_old_recordings(*, now: datetime | None = None, retention_days: int | None = None) -> int:
    """Remove terminal recording sessions after retention; keep retryable work."""
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
            if status in TERMINAL_STATUSES and updated_at < cutoff:
                shutil.rmtree(metadata_path.parent)
                removed += 1
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return removed


def load_transcript_content(path: Path | None) -> str:
    """Read only the body of the separately archived transcript."""
    if path is None or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = "\n---\n\n"
    return text.split(marker, 1)[1].strip() if marker in text else text.strip()


def find_recoverable_recordings(*, now: datetime | None = None) -> list[RecoverableRecording]:
    """Find interrupted sessions whose retry time has arrived and PCM remains."""
    current = now or datetime.now(timezone.utc)
    candidates = []
    root = recording_root()
    if not root.exists():
        return candidates
    for metadata_path in root.glob("*/*/metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            status = str(metadata.get("status") or "")
            if status not in RECOVERABLE_STATUSES or status in TERMINAL_STATUSES:
                continue
            if not any(metadata_path.parent.glob("user-*.pcm")):
                continue
            retry_at = metadata.get("next_retry_at")
            if retry_at:
                parsed = datetime.fromisoformat(retry_at)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed > current:
                    continue
            candidates.append(RecoverableRecording(metadata_path.parent, metadata))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    candidates.sort(key=lambda item: str(item.metadata.get("started_at") or item.session_dir.name))
    return candidates
