"""Durable, bounded Discord voice recording storage."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from discord.sinks import Sink

PCM_CHANNELS = 2
PCM_SAMPLE_RATE = 48_000
PCM_SAMPLE_WIDTH = 2
PCM_BYTES_PER_SECOND = PCM_CHANNELS * PCM_SAMPLE_RATE * PCM_SAMPLE_WIDTH
# Do not truncate a long/many-participant meeting at an arbitrary session size.
# ``0`` means disk-limited; the independent free-space reserve and atomic
# admission accounting still reject capture before the host itself is filled.
DEFAULT_MAX_RECORDING_BYTES = 0
DEFAULT_FREE_SPACE_RESERVE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MIN_RECORDING_HEADROOM_BYTES = 64 * 1024 * 1024
DEFAULT_CAPTURE_HEALTH_MIN_WALL_SECONDS = 15 * 60
DEFAULT_CAPTURE_HEALTH_MIN_AUDIO_RATIO = 0.15
DEFAULT_CAPTURE_HEALTH_MAX_SILENCE_SECONDS = 5 * 60
TIMELINE_MANIFEST_NAME = "timeline.jsonl"
TIMELINE_SCHEMA_VERSION = 1
DEFAULT_TIMELINE_TURN_GAP_SECONDS = 1.25
RECOVERABLE_STATUSES = {
    "processing",
    "recorded",
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
    "capture_suspicious",
}
# Successful replay may refresh the archived transcript for these retained
# recordings, but it never changes the raw recording retention contract.
REPLAY_FINALIZATION_STATUSES = {
    "delivered_recording_retained",
    "recovery_dead_letter",
}

# These failures prove that Discord reception ended unexpectedly, but do not
# imply that bytes already acknowledged by the durable sink are corrupt. Such
# recordings may be processed as explicitly partial artifacts after their final
# manifest is verified. Storage/fsync/timeline failures are intentionally absent.
DEGRADED_AUTO_PROCESS_REASONS = {
    "bot-disconnected",
    "bot-moved",
    "voice-connection-lost",
    "voice-recorder-stopped",
    "recorder-stopped-unexpectedly",
    "recorder-restarted-after-gap",
    "recorder-not-running-at-stop",
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


@dataclass(frozen=True)
class RecordingCaptureHealth:
    """Observable capture coverage, independent from transcription success.

    Discord sends decoded PCM only for audio packets that reached the voice
    server.  Consequently ``audio_seconds`` is voiced packet time rather than
    wall-clock duration.  A very low ratio is not proof of data loss, but it is
    strong enough evidence that deleting the only PCM copy would be unsafe.
    """

    wall_seconds: float
    audio_seconds: float
    longest_track_seconds: float
    source_tracks: int
    audio_to_wall_ratio: float
    packet_count: int
    last_audio_at: str
    seconds_since_last_audio: float | None
    suspicious: bool
    reason: str

    def as_metadata(self) -> dict:
        return {
            "wall_seconds": round(self.wall_seconds, 3),
            "audio_seconds": round(self.audio_seconds, 3),
            "longest_track_seconds": round(self.longest_track_seconds, 3),
            "source_tracks": self.source_tracks,
            "audio_to_wall_ratio": round(self.audio_to_wall_ratio, 4),
            "packet_count": self.packet_count,
            "last_audio_at": self.last_audio_at,
            "seconds_since_last_audio": (
                round(self.seconds_since_last_audio, 3)
                if self.seconds_since_last_audio is not None
                else None
            ),
            "suspicious": self.suspicious,
            "reason": self.reason,
        }


log = logging.getLogger("yagapon.recording")
_recording_space_lock = threading.Lock()
_recording_inflight_bytes = 0
_recording_admission_bytes = 0


def _fsync_directory(path: Path) -> None:
    """Durably commit directory-entry changes made below ``path``."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    """Create a private directory hierarchy and persist every new entry."""
    missing: list[Path] = []
    cursor = path
    while not cursor.exists() and cursor != cursor.parent:
        missing.append(cursor)
        cursor = cursor.parent
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)
    for created in reversed(missing):
        created.chmod(0o700)
        # fsync(child) commits its own metadata; fsync(parent) commits the name
        # that makes it discoverable after a sudden power loss.
        _fsync_directory(created)
        _fsync_directory(created.parent)


def _atomic_write_private_json(path: Path, payload: dict) -> None:
    """Atomically replace private JSON with file and directory durability."""
    _ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            os.fchmod(destination.fileno(), 0o600)
            json.dump(payload, destination, ensure_ascii=False, indent=2)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _session_directories(root: Path) -> list[Path]:
    """Return recording session dirs, including PCM whose JSON was lost."""
    if not root.is_dir():
        return []
    sessions: list[Path] = []
    for candidate in root.glob("*/*"):
        if not candidate.is_dir():
            continue
        if (candidate / "metadata.json").exists() or any(
            candidate.glob("user-*.pcm")
        ):
            sessions.append(candidate)
    return sorted(sessions)


def _load_session_metadata(session_dir: Path) -> tuple[dict, str]:
    """Load metadata and classify missing/corrupt JSON without hiding PCM."""
    path = session_dir / "metadata.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("recording metadata must be an object")
        return value, "valid"
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, ValueError, json.JSONDecodeError):
        return {}, "corrupt"


def _synthesized_interrupted_metadata(session_dir: Path, state: str) -> dict:
    """Describe unindexed raw bytes without ever treating them as complete."""
    sizes = _pcm_file_sizes(session_dir)
    try:
        started_at = datetime.fromtimestamp(
            session_dir.stat().st_mtime,
            tz=timezone.utc,
        ).isoformat()
    except OSError:
        started_at = ""
    reason = f"recording-metadata-{state}"
    return {
        "guild_id": session_dir.parent.name,
        "channel_name": "unknown",
        "mode": "listen",
        "status": "capture_interrupted",
        "started_at": started_at,
        "capture_ended_at": "",
        "capture_integrity_failed": True,
        "capture_integrity_reason": reason,
        "capture_interruption_reason": reason,
        "metadata_recovered_from_pcm": True,
        "metadata_recovery_state": state,
        "raw_recording_retained": True,
        "total_bytes": sum(sizes.values()),
        "track_bytes": {str(key): value for key, value in sizes.items()},
        # The bytes are discoverable, but a lost checkpoint cannot prove that
        # the full receive tail reached stable storage.
        "durable_total_bytes": 0,
    }


@dataclass(frozen=True)
class RecordingRawValidation:
    valid: bool
    reason: str
    total_bytes: int
    track_bytes: dict[int, int]
    checked_against_manifest: bool


def validate_recording_raw(
    session_dir: Path,
    metadata: dict,
) -> RecordingRawValidation:
    """Fail closed when finalized PCM differs from its durable size manifest.

    Older archives did not store a per-track manifest. They remain readable
    when all streams are non-empty, while every new archive is checked against
    ``track_bytes``, ``total_bytes`` and its final durability checkpoint.
    """
    sizes = _pcm_file_sizes(session_dir)
    total = sum(sizes.values())
    recovered_manifest = metadata.get("metadata_recovered_from_pcm") is True
    if not sizes:
        return RecordingRawValidation(False, "raw-audio-missing", 0, {}, False)
    if any(size <= 0 for size in sizes.values()):
        return RecordingRawValidation(
            False,
            "raw-track-empty",
            total,
            sizes,
            False,
        )

    has_complete_manifest = bool(
        not recovered_manifest
        and metadata.get("capture_ended_at")
        and isinstance(metadata.get("track_bytes"), dict)
        and "total_bytes" in metadata
        and "durable_total_bytes" in metadata
    )
    checked = False
    stored_tracks = metadata.get("track_bytes")
    if stored_tracks is not None:
        try:
            expected_tracks = {
                int(user_id): int(size)
                for user_id, size in dict(stored_tracks).items()
            }
        except (TypeError, ValueError):
            return RecordingRawValidation(
                False,
                "raw-track-manifest-invalid",
                total,
                sizes,
                True,
            )
        if expected_tracks != sizes:
            return RecordingRawValidation(
                False,
                "raw-track-size-mismatch",
                total,
                sizes,
                True,
            )

    if "total_bytes" in metadata:
        try:
            expected_total = int(metadata["total_bytes"])
        except (TypeError, ValueError):
            return RecordingRawValidation(
                False,
                "raw-total-manifest-invalid",
                total,
                sizes,
                True,
            )
        if expected_total != total:
            return RecordingRawValidation(
                False,
                "raw-total-size-mismatch",
                total,
                sizes,
                True,
            )

    # A cleanly finalized new recording promises that all bytes were fsynced.
    # Do not accept a later-truncated file merely because its JSON survived.
    if metadata.get("capture_ended_at") and "durable_total_bytes" in metadata:
        try:
            durable_total = int(metadata["durable_total_bytes"])
        except (TypeError, ValueError):
            return RecordingRawValidation(
                False,
                "raw-durable-manifest-invalid",
                total,
                sizes,
                True,
            )
        if durable_total != total:
            return RecordingRawValidation(
                False,
                "raw-durable-size-mismatch",
                total,
                sizes,
                True,
            )

    # A partial legacy manifest (for example only total_bytes) cannot prove
    # that every participant track and the final fsync tail survived. It stays
    # available for manual inspection but is never eligible for auto-publish.
    checked = has_complete_manifest
    return RecordingRawValidation(True, "", total, sizes, checked)


def degraded_recording_is_processable(session_dir: Path, metadata: dict) -> bool:
    """Allow only manifest-verified PCM from a known receive interruption.

    This does not declare the meeting complete. It allows transcription and
    minutes generation with a durable partial-recording warning, so a Discord
    disconnect no longer strands otherwise usable audio.
    """
    reason = str(metadata.get("capture_integrity_reason") or "")
    health = metadata.get("capture_health")
    if (
        reason not in DEGRADED_AUTO_PROCESS_REASONS
        or not metadata.get("capture_ended_at")
        or metadata.get("storage_limit_reached")
        or metadata.get("capture_suspicious")
        or metadata.get("timeline_complete") is not True
        or (isinstance(health, dict) and health.get("suspicious") is True)
    ):
        return False
    validation = validate_recording_raw(session_dir, metadata)
    return validation.valid and validation.checked_against_manifest


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
    """Load a complete manifest and coalesce packet streams into speaker turns.

    ``None`` deliberately means "use the legacy per-user fallback". A timeline
    is used only when it covers every byte in every PCM file exactly; this keeps
    a missing/truncated manifest from silently dropping old or crash-tail audio.
    Packets from one Discord track within ``gap_seconds`` are one turn even when
    another track is interleaved (normal for simultaneous receive streams).
    Turns separated by a real per-track pause retain their start-time order.
    """
    manifest = session_dir / TIMELINE_MANIFEST_NAME
    sizes = _pcm_file_sizes(session_dir)
    if not manifest.is_file():
        return None
    if not sizes:
        return []

    expected_offsets: dict[int, int] = {}
    completed: list[RecordingTimelineSegment] = []
    active_by_user: dict[int, RecordingTimelineSegment] = {}
    previous_sequence = -1
    gap_ns = max(0, int(gap_seconds * 1_000_000_000))
    bounded_size = max(1, int(max_bytes))

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

                event = RecordingTimelineSegment(
                    sequence=sequence,
                    user_id=user_id,
                    start=start,
                    end=end,
                    received_at_ns=received_at_ns,
                    ended_at_ns=received_at_ns,
                )
                current = active_by_user.get(user_id)
                if (
                    current is not None
                    and start == current.end
                    and 0 <= received_at_ns - current.ended_at_ns <= gap_ns
                    and end - current.start <= bounded_size
                ):
                    active_by_user[user_id] = RecordingTimelineSegment(
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
                    active_by_user[user_id] = event
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None

    if previous_sequence < 0 or any(expected_offsets.get(user_id, 0) != size for user_id, size in sizes.items()):
        return None

    completed.extend(active_by_user.values())
    completed.sort(key=lambda segment: (segment.received_at_ns, segment.sequence))
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
    return sorted(
        result,
        key=lambda segment: (segment.received_at_ns, segment.sequence, segment.start),
    )


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


def transcript_provenance_path(transcript_path: Path) -> Path:
    """Return the durable integrity sidecar stored beside a transcript."""
    return transcript_path.with_suffix(".provenance.json")


def load_transcript_provenance(transcript_path: Path | None) -> dict:
    """Load transcript integrity provenance, failing closed when unavailable."""
    if transcript_path is None:
        return {}
    try:
        payload = json.loads(
            transcript_provenance_path(transcript_path).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_transcript_provenance(
    transcript_path: Path,
    *,
    guild_id: int,
    session_id: str,
    mode: str,
    transcript_complete: bool,
    capture_integrity_failed: bool,
    status: str = "",
) -> Path:
    """Atomically preserve integrity facts independently from raw PCM metadata."""
    destination = transcript_provenance_path(transcript_path)
    current = load_transcript_provenance(transcript_path)
    payload = {
        **current,
        "schema_version": 1,
        "guild_id": str(guild_id),
        "session_id": session_id,
        "mode": mode,
        # Completeness is opt-in.  A later workflow status must not infer or
        # silently promote a draft transcript to an authoritative final pass.
        "transcript_complete": bool(transcript_complete),
        # Capture integrity is sticky even after a successful transcription of
        # the bytes that happened to reach disk.
        "capture_integrity_failed": bool(
            capture_integrity_failed or current.get("capture_integrity_failed")
        ),
        "status": status or str(current.get("status") or ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_private_json(destination, payload)
    return destination


def save_external_transcript(
    guild_id: int,
    session_id: str,
    channel_name: str,
    content: str,
    *,
    mode: str = "uploaded",
    transcript_complete: bool = False,
    capture_integrity_failed: bool = False,
    status: str = "",
) -> Path | None:
    """Atomically archive a non-VC transcript under the same retention contract."""
    if not content.strip():
        return None
    safe_session_id = re.sub(r"[^A-Za-z0-9_.-]", "-", session_id)[:120]
    destination = transcript_root() / str(guild_id) / f"{safe_session_id}.md"
    _ensure_private_directory(destination.parent)
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
    save_transcript_provenance(
        destination,
        guild_id=guild_id,
        session_id=safe_session_id,
        mode=mode,
        transcript_complete=transcript_complete,
        capture_integrity_failed=capture_integrity_failed,
        status=status,
    )
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
    """Archive a successful replay transcript without changing its source.

    PCM, the receive timeline, and metadata remain available for any later
    replay until an operator performs a separate explicit deletion outside this
    automatic workflow.
    """
    expected_dir = (recording_root() / str(guild_id) / session_id).resolve()
    resolved_dir = session_dir.resolve()
    if resolved_dir != expected_dir:
        raise ValueError("recording replay source is outside the recording archive")
    metadata_path = resolved_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if str(metadata.get("status") or "") not in REPLAY_FINALIZATION_STATUSES:
        return 0
    validation = validate_recording_raw(resolved_dir, metadata)
    if (
        not validation.valid
        or not validation.checked_against_manifest
        or metadata.get("capture_integrity_failed") is True
    ):
        return 0
    archived = save_external_transcript(
        guild_id,
        session_id,
        channel_name,
        transcript,
        mode="recording-replay-finalized",
        transcript_complete=True,
        capture_integrity_failed=bool(metadata.get("capture_integrity_failed")),
        status="delivered_recording_retained",
    )
    if archived is None or not archived.is_file() or archived.stat().st_size <= 0:
        raise RuntimeError("replay transcript could not be archived")
    metadata.update(
        {
            "status": "delivered_recording_retained",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "transcript_path": str(archived),
            "transcript_complete": True,
            "replay_job_id": replay_job_id,
            "raw_recording_retained": True,
            "raw_audio_deleted_after_replay": False,
        }
    )
    _atomic_write_private_json(metadata_path, metadata)
    return 0


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
        continue_capture: bool = False,
    ):
        super().__init__()
        created_at = datetime.now(timezone.utc)
        self.session_dir = session_dir or (
            recording_root() / str(guild_id) / f"{created_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        )
        configured_root = recording_root().resolve()
        resolved_session = self.session_dir.resolve()
        if resolved_session.is_relative_to(configured_root):
            _ensure_private_directory(configured_root)
        # Raw voice is retained until an explicit operator deletion. Persist
        # the session directory entry before the first Discord packet arrives.
        _ensure_private_directory(self.session_dir)
        existing_metadata = self.read_metadata() if resume else {}
        self.started_at = created_at
        stored_started_at = existing_metadata.get("started_at")
        if stored_started_at:
            try:
                self.started_at = datetime.fromisoformat(
                    str(stored_started_at).replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
        self.guild_id = guild_id
        self.channel_name = channel_name
        self.mode = mode
        self._lock = threading.RLock()
        self._files: dict[int, object] = {}
        self._user_bytes = _pcm_file_sizes(self.session_dir)
        self._total_bytes = sum(self._user_bytes.values())
        self._source_track_count = len(self._user_bytes)
        self._longest_track_bytes = max(self._user_bytes.values(), default=0)
        self._durable_total_bytes = int(
            existing_metadata.get("durable_total_bytes") or 0
        )
        # Do not manufacture a trusted per-track manifest by merely opening a
        # legacy archive. New captures own the manifest from byte zero; legacy
        # recordings remain readable but explicitly unverified.
        self._track_manifest_enabled = bool(
            not resume or isinstance(existing_metadata.get("track_bytes"), dict)
        )
        self._total_manifest_enabled = bool(
            not resume or "total_bytes" in existing_metadata
        )
        self._durable_manifest_enabled = bool(
            not resume or "durable_total_bytes" in existing_metadata
        )
        configured_value = (
            max_bytes
            if max_bytes is not None
            else int(
                os.environ.get(
                    "YAGAPON_MAX_RECORDING_BYTES",
                    DEFAULT_MAX_RECORDING_BYTES,
                )
            )
        )
        configured_max = configured_value if configured_value > 0 else (1 << 63) - 1
        reserve = int(
            os.environ.get(
                "YAGAPON_RECORDING_FREE_SPACE_RESERVE_BYTES",
                DEFAULT_FREE_SPACE_RESERVE_BYTES,
            )
        )
        self._free_space_reserve = reserve
        self.max_bytes = self._total_bytes
        self._admission_remaining = 0
        self._admission_released = False
        self._timeline_path = self.session_dir / TIMELINE_MANIFEST_NAME
        self._timeline_file = None
        self._timeline_sequence = 0
        self._timeline_complete = self._prepare_timeline_for_append()
        self._packet_count = int(existing_metadata.get("packet_count") or 0)
        existing_status = str(existing_metadata.get("status") or "")
        self._capture_integrity_status = (
            existing_status
            if existing_status in {"capture_interrupted", "capture_suspicious"}
            else ""
        )
        self._capture_integrity_failed = bool(
            existing_metadata.get("capture_integrity_failed")
            or self._capture_integrity_status
        )
        self._capture_integrity_reason = str(
            existing_metadata.get("capture_integrity_reason") or ""
        )
        self._pcm_write_error = str(existing_metadata.get("pcm_write_error") or "")
        self._timeline_write_error = str(
            existing_metadata.get("timeline_write_error") or ""
        )
        self._metadata_write_error = str(
            existing_metadata.get("metadata_write_error") or ""
        )
        self._last_audio_at: datetime | None = None
        if existing_metadata.get("last_audio_at"):
            try:
                self._last_audio_at = datetime.fromisoformat(
                    str(existing_metadata["last_audio_at"]).replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
        self._capture_ended_at: datetime | None = None
        if existing_metadata.get("capture_ended_at") and not continue_capture:
            try:
                self._capture_ended_at = datetime.fromisoformat(
                    str(existing_metadata["capture_ended_at"]).replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
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
        capture_will_start = not resume or continue_capture
        try:
            if capture_will_start:
                try:
                    minimum_headroom = max(
                        1,
                        int(
                            os.environ.get(
                                "YAGAPON_MIN_RECORDING_HEADROOM_BYTES",
                                DEFAULT_MIN_RECORDING_HEADROOM_BYTES,
                            )
                        ),
                    )
                except ValueError:
                    minimum_headroom = DEFAULT_MIN_RECORDING_HEADROOM_BYTES
                global _recording_admission_bytes
                with _recording_space_lock:
                    free_bytes = shutil.disk_usage(self.session_dir).free
                    available_for_recording = max(
                        0,
                        free_bytes
                        - reserve
                        - _recording_admission_bytes,
                    )
                    if available_for_recording < minimum_headroom:
                        raise RecordingLimitExceeded(
                            "Insufficient durable recording headroom"
                        )
                    self._admission_remaining = minimum_headroom
                    _recording_admission_bytes += minimum_headroom
                self.max_bytes = min(
                    configured_max,
                    self._total_bytes + available_for_recording,
                )
            else:
                available_for_recording = max(
                    0,
                    shutil.disk_usage(self.session_dir).free - reserve,
                )
                self.max_bytes = min(
                    configured_max,
                    self._total_bytes + available_for_recording,
                )
            if resume:
                self._write_metadata(
                    (
                        "recording"
                        if continue_capture
                        else str(existing_metadata.get("status") or "recorded")
                    ),
                    recovery_resumed_at=created_at.isoformat(),
                )
            else:
                self._write_metadata("recording", started_at=created_at.isoformat())
        except BaseException:
            self._release_admission_reservation()
            raise

    def _release_admission_reservation(self) -> None:
        global _recording_admission_bytes
        if self._admission_released:
            return
        with _recording_space_lock:
            _recording_admission_bytes = max(
                0,
                _recording_admission_bytes - self._admission_remaining,
            )
            self._admission_remaining = 0
            self._admission_released = True

    def _prepare_timeline_for_append(self) -> bool:
        """Recover the next event id and trim only a crash-truncated tail."""
        if not self._timeline_path.exists():
            return self._total_bytes == 0
        expected_offsets: dict[int, int] = {}
        previous_sequence = -1
        valid_end = 0
        needs_newline = False
        complete = True
        corrupt_tail_at: int | None = None
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
                        corrupt_tail_at = line_start
                        break
                    previous_sequence = sequence
                    expected_offsets[user_id] = end
                    valid_end = source.tell()
                    needs_newline = not raw_line.endswith(b"\n")
                if corrupt_tail_at is not None:
                    # Never discard even a malformed source byte without first
                    # preserving the exact pre-repair manifest for inspection.
                    backup = self._timeline_path.with_name(
                        f"{self._timeline_path.name}.corrupt-{uuid4().hex[:12]}"
                    )
                    try:
                        shutil.copy2(self._timeline_path, backup)
                        backup.chmod(0o600)
                        with backup.open("rb") as preserved:
                            os.fsync(preserved.fileno())
                        _fsync_directory(self.session_dir)
                    except OSError:
                        log.exception(
                            "Could not preserve corrupt recording timeline before repair"
                        )
                        return False
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

    def capture_health(
        self,
        *,
        now: datetime | None = None,
    ) -> RecordingCaptureHealth:
        """Return capture coverage used for warnings and safe PCM retention."""
        current = (
            self._capture_ended_at
            or now
            or datetime.now(timezone.utc)
        ).astimezone(timezone.utc)
        # Only immutable/scalar snapshots are read here. A durability fsync can
        # legitimately hold the sink RLock inside a worker thread for seconds;
        # health reporting and the Discord watchdog must never block the event
        # loop behind that I/O. CPython scalar reference reads are atomic, and
        # slightly stale diagnostics are safer than a frozen bot.
        wall_seconds = max(0.0, (current - self.started_at).total_seconds())
        total_bytes = self._total_bytes
        longest_track_bytes = self._longest_track_bytes
        source_track_count = self._source_track_count
        packet_count = self._packet_count
        last_audio_at = self._last_audio_at
        audio_seconds = total_bytes / PCM_BYTES_PER_SECOND
        longest_track_seconds = longest_track_bytes / PCM_BYTES_PER_SECOND
        ratio = audio_seconds / wall_seconds if wall_seconds > 0 else 0.0
        try:
            minimum_wall = max(
                60.0,
                float(
                    os.environ.get(
                        "YAGAPON_CAPTURE_HEALTH_MIN_WALL_SECONDS",
                        DEFAULT_CAPTURE_HEALTH_MIN_WALL_SECONDS,
                    )
                ),
            )
        except ValueError:
            minimum_wall = float(DEFAULT_CAPTURE_HEALTH_MIN_WALL_SECONDS)
        try:
            minimum_ratio = min(
                1.0,
                max(
                    0.0,
                    float(
                        os.environ.get(
                            "YAGAPON_CAPTURE_HEALTH_MIN_AUDIO_RATIO",
                            DEFAULT_CAPTURE_HEALTH_MIN_AUDIO_RATIO,
                        )
                    ),
                ),
            )
        except ValueError:
            minimum_ratio = DEFAULT_CAPTURE_HEALTH_MIN_AUDIO_RATIO
        try:
            maximum_silence = max(
                60.0,
                float(
                    os.environ.get(
                        "YAGAPON_CAPTURE_HEALTH_MAX_SILENCE_SECONDS",
                        DEFAULT_CAPTURE_HEALTH_MAX_SILENCE_SECONDS,
                    )
                ),
            )
        except ValueError:
            maximum_silence = float(DEFAULT_CAPTURE_HEALTH_MAX_SILENCE_SECONDS)

        seconds_since_last_audio = (
            max(0.0, (current - last_audio_at).total_seconds())
            if last_audio_at
            else None
        )

        reason = ""
        if wall_seconds >= minimum_wall and audio_seconds <= 0:
            reason = "no-audio"
        elif (
            wall_seconds >= minimum_wall
            and seconds_since_last_audio is not None
            and seconds_since_last_audio >= maximum_silence
        ):
            reason = "audio-stalled"
        elif wall_seconds >= minimum_wall and ratio < minimum_ratio:
            reason = "low-audio-ratio"

        return RecordingCaptureHealth(
            wall_seconds=wall_seconds,
            audio_seconds=audio_seconds,
            longest_track_seconds=longest_track_seconds,
            source_tracks=source_track_count,
            audio_to_wall_ratio=ratio,
            packet_count=packet_count,
            last_audio_at=(last_audio_at.isoformat() if last_audio_at else ""),
            seconds_since_last_audio=seconds_since_last_audio,
            suspicious=bool(reason),
            reason=reason,
        )

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
            "packet_count": self._packet_count,
            "last_audio_at": (
                self._last_audio_at.isoformat() if self._last_audio_at else ""
            ),
            "capture_ended_at": (
                self._capture_ended_at.isoformat() if self._capture_ended_at else ""
            ),
            "capture_health": self.capture_health().as_metadata(),
            "capture_integrity_failed": self._capture_integrity_failed,
            "capture_integrity_reason": self._capture_integrity_reason,
            "pcm_write_error": self._pcm_write_error,
            "timeline_write_error": self._timeline_write_error,
            "metadata_write_error": self._metadata_write_error,
            **extra,
        }
        if self._total_manifest_enabled:
            metadata["total_bytes"] = self._total_bytes
        else:
            metadata.pop("total_bytes", None)
        if self._track_manifest_enabled:
            metadata["track_bytes"] = {
                str(user_id): size
                for user_id, size in sorted(self._user_bytes.items())
            }
        else:
            metadata.pop("track_bytes", None)
        if self._durable_manifest_enabled:
            metadata["durable_total_bytes"] = self._durable_total_bytes
        else:
            metadata.pop("durable_total_bytes", None)
        if self.channel_id is not None:
            metadata["channel_id"] = str(self.channel_id)
        if self.category_id is not None:
            metadata["category_id"] = str(self.category_id)
        metadata["category_name"] = self.category_name
        metadata["channel_lineage_recorded"] = self.channel_lineage_recorded
        metadata["timeline_manifest"] = TIMELINE_MANIFEST_NAME
        metadata["timeline_schema"] = TIMELINE_SCHEMA_VERSION
        metadata["timeline_complete"] = self._timeline_complete
        current.setdefault("transcript_complete", False)
        current.update(metadata)
        try:
            _atomic_write_private_json(path, current)
        except OSError as exc:
            # Keep an in-memory signal even when the medium used for the alert
            # metadata is itself failing. The independent voice monitor can
            # still notify Discord and fail publication closed.
            self._metadata_write_error = type(exc).__name__
            raise

    def read_metadata(self) -> dict:
        path = self.session_dir / "metadata.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def write(self, data, user) -> None:
        global _recording_admission_bytes, _recording_inflight_bytes
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
            requested_bytes = min(len(pcm_data), remaining)
            reserved_bytes = 0
            consumed_admission = 0
            with _recording_space_lock:
                try:
                    free_bytes = shutil.disk_usage(self.session_dir).free
                except OSError as exc:
                    self._pcm_write_error = type(exc).__name__
                    self._capture_integrity_failed = True
                    self._capture_integrity_status = "capture_suspicious"
                    self._capture_integrity_reason = "storage-probe-failed"
                    return
                writable_bytes = max(
                    0,
                    free_bytes
                    - self._free_space_reserve
                    - max(
                        0,
                        _recording_admission_bytes - self._admission_remaining,
                    )
                    - _recording_inflight_bytes,
                )
                reserved_bytes = min(requested_bytes, writable_bytes)
                _recording_inflight_bytes += reserved_bytes
                consumed_admission = min(
                    reserved_bytes,
                    self._admission_remaining,
                )
                self._admission_remaining -= consumed_admission
                _recording_admission_bytes = max(
                    0,
                    _recording_admission_bytes - consumed_admission,
                )
            if reserved_bytes <= 0:
                self._limit_reached = True
                try:
                    self._write_metadata(
                        "storage_limit_reached",
                        total_bytes=self._total_bytes,
                        storage_limit_reached=True,
                    )
                except OSError:
                    pass
                return
            chunk = bytes(pcm_data[:reserved_bytes])
            start = self._user_bytes.get(user_id, 0)
            new_track = user_id not in self._user_bytes
            try:
                file = self._files.get(user_id)
                if file is None:
                    # Keep no PCM tail solely in a Python userspace buffer. A
                    # process-level hard exit can then preserve every completed
                    # write; periodic fsync checkpoints cover the page cache.
                    pcm_path = self._pcm_path(user_id)
                    pcm_entry_is_new = not pcm_path.exists()
                    file = pcm_path.open("ab", buffering=0)
                    try:
                        pcm_path.chmod(0o600)
                        if pcm_entry_is_new:
                            _fsync_directory(self.session_dir)
                    except OSError:
                        file.close()
                        raise
                    self._files[user_id] = file
                written = file.write(chunk)
                if written != len(chunk):
                    raise OSError("short PCM write")
            except OSError as exc:
                if consumed_admission:
                    with _recording_space_lock:
                        self._admission_remaining += consumed_admission
                        _recording_admission_bytes += consumed_admission
                    consumed_admission = 0
                self._pcm_write_error = type(exc).__name__
                self._capture_integrity_failed = True
                self._capture_integrity_status = "capture_suspicious"
                self._capture_integrity_reason = "pcm-write-failed"
                try:
                    self._write_metadata(
                        "capture_suspicious",
                        total_bytes=self._total_bytes,
                        pcm_write_error=type(exc).__name__,
                        capture_integrity_failed=True,
                        capture_integrity_reason="pcm-write-failed",
                    )
                except OSError:
                    pass
                raise
            finally:
                with _recording_space_lock:
                    _recording_inflight_bytes = max(
                        0,
                        _recording_inflight_bytes - reserved_bytes,
                    )
            end = start + len(chunk)
            self._user_bytes[user_id] = end
            if new_track:
                self._source_track_count += 1
            self._longest_track_bytes = max(self._longest_track_bytes, end)
            self._total_bytes += len(chunk)
            self._packet_count += 1
            self._last_audio_at = datetime.now(timezone.utc)
            try:
                if self._timeline_file is None:
                    timeline_entry_is_new = not self._timeline_path.exists()
                    self._timeline_file = self._timeline_path.open("ab", buffering=0)
                    self._timeline_path.chmod(0o600)
                    if timeline_entry_is_new:
                        _fsync_directory(self.session_dir)
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
            except OSError as exc:
                # PCM remains the source of truth. The coverage validator will
                # select the lossless legacy fallback rather than omit this range.
                self._timeline_complete = False
                self._timeline_write_error = type(exc).__name__
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
        return self._total_bytes

    @property
    def durable_total_bytes(self) -> int:
        """Bytes covered by the latest successful all-track fsync checkpoint."""
        return self._durable_total_bytes

    @property
    def limit_reached(self) -> bool:
        return self._limit_reached

    @property
    def capture_integrity_failed(self) -> bool:
        """Whether this recording ever crossed an irreversible capture gap."""
        return self._capture_integrity_failed

    @property
    def capture_integrity_reason(self) -> str:
        return self._capture_integrity_reason

    @property
    def capture_anomaly_reason(self) -> str:
        """Return an alertable storage/integrity anomaly without doing I/O."""
        if self._limit_reached:
            return "storage-limit-reached"
        if self._pcm_write_error:
            return "pcm-write-failed"
        if self._metadata_write_error:
            return "metadata-write-failed"
        if self._timeline_write_error:
            return "timeline-write-failed"
        if self._capture_integrity_failed:
            return self._capture_integrity_reason or "capture-integrity-failed"
        return ""

    def latch_capture_integrity(
        self,
        reason: str,
        status: str = "capture_interrupted",
    ) -> None:
        """Latch a capture gap without locks or storage I/O.

        Pycord's voice receive callback can call this before handing durable
        metadata work to an executor. Scalar assignments keep the callback
        non-blocking; the next checkpoint/cleanup persists the sticky state.
        """
        self._capture_integrity_failed = True
        if status == "capture_interrupted":
            self._capture_integrity_status = status
        elif (
            status == "capture_suspicious"
            and self._capture_integrity_status != "capture_interrupted"
        ):
            self._capture_integrity_status = status
        if reason and not self._capture_integrity_reason:
            self._capture_integrity_reason = str(reason)

    def cleanup(self) -> None:
        with self._lock:
            if self._closed:
                return
            # Stop accepting packets immediately, but make the close itself
            # best-effort across every participant. One damaged descriptor must
            # not strand the buffered tail of all later tracks.
            self._closed = True
            failures: list[Exception] = []
            for file in tuple(self._files.values()):
                try:
                    if not file.closed:
                        file.flush()
                        os.fsync(file.fileno())
                except Exception as exc:
                    failures.append(exc)
                try:
                    if not file.closed:
                        file.close()
                except Exception as exc:
                    failures.append(exc)
            self._files.clear()
            if self._timeline_file is not None:
                try:
                    if not self._timeline_file.closed:
                        self._timeline_file.flush()
                        os.fsync(self._timeline_file.fileno())
                except Exception as exc:
                    failures.append(exc)
                try:
                    if not self._timeline_file.closed:
                        self._timeline_file.close()
                except Exception as exc:
                    failures.append(exc)
                self._timeline_file = None
            self._capture_ended_at = datetime.now(timezone.utc)
            if failures:
                self._capture_integrity_failed = True
                self._capture_integrity_status = "capture_interrupted"
                self._capture_integrity_reason = (
                    self._capture_integrity_reason or "sink-cleanup-failed"
                )
            else:
                self._durable_total_bytes = self._total_bytes
            status = (
                self._capture_integrity_status
                if self._capture_integrity_failed and self._capture_integrity_status
                else ("storage_limit_reached" if self._limit_reached else "recorded")
            )
            try:
                self._write_metadata(
                    status,
                    total_bytes=self._total_bytes,
                    storage_limit_reached=self._limit_reached,
                    cleanup_error=(type(failures[0]).__name__ if failures else ""),
                )
            except Exception as exc:
                failures.append(exc)
            self._release_admission_reservation()
            if failures:
                raise failures[0]

    def flush(self) -> None:
        """Synchronously persist every userspace PCM buffer without closing it."""
        with self._lock:
            failures: list[Exception] = []
            for file in tuple(self._files.values()):
                if file.closed:
                    continue
                try:
                    file.flush()
                    os.fsync(file.fileno())
                except Exception as exc:
                    failures.append(exc)
            if self._timeline_file is not None:
                if not self._timeline_file.closed:
                    try:
                        self._timeline_file.flush()
                        os.fsync(self._timeline_file.fileno())
                    except Exception as exc:
                        failures.append(exc)
            if failures:
                self._capture_integrity_failed = True
                self._capture_integrity_status = "capture_suspicious"
                self._capture_integrity_reason = (
                    self._capture_integrity_reason or "durable-checkpoint-failed"
                )
                try:
                    self._write_metadata(
                        "capture_suspicious",
                        durable_checkpoint_error=type(failures[0]).__name__,
                        capture_integrity_failed=True,
                        capture_integrity_reason=self._capture_integrity_reason,
                    )
                except OSError:
                    pass
                raise failures[0]
            self._durable_total_bytes = self._total_bytes

    def format_audio(self, audio) -> None:  # pragma: no cover - base cleanup is overridden
        return

    def mark(self, status: str, **extra) -> None:
        with self._lock:
            if status in {"capture_interrupted", "capture_suspicious"} or extra.get(
                "capture_integrity_failed"
            ):
                self._capture_integrity_failed = True
                if status in {"capture_interrupted", "capture_suspicious"}:
                    self._capture_integrity_status = status
                self._capture_integrity_reason = str(
                    extra.get("capture_integrity_reason")
                    or extra.get("capture_interruption_reason")
                    or extra.get("capture_warning_reason")
                    or self._capture_integrity_reason
                )
            self._write_metadata(
                status,
                total_bytes=self._total_bytes,
                capture_integrity_failed=self._capture_integrity_failed,
                capture_integrity_reason=self._capture_integrity_reason,
                **extra,
            )
            metadata = self.read_metadata()
            transcript_value = str(metadata.get("transcript_path") or "")
            transcript_path = Path(transcript_value) if transcript_value else (
                transcript_root() / str(self.guild_id) / f"{self.session_dir.name}.md"
            )
            if transcript_path.is_file():
                save_transcript_provenance(
                    transcript_path,
                    guild_id=self.guild_id,
                    session_id=self.session_dir.name,
                    mode=self.mode,
                    transcript_complete=bool(metadata.get("transcript_complete")),
                    capture_integrity_failed=self._capture_integrity_failed,
                    status=status,
                )

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
        metadata = self.read_metadata()
        return save_external_transcript(
            self.guild_id,
            self.session_dir.name,
            self.channel_name,
            content,
            mode=self.mode,
            transcript_complete=bool(metadata.get("transcript_complete")),
            capture_integrity_failed=self._capture_integrity_failed,
            status=str(metadata.get("status") or ""),
        )

    def delete_audio(self) -> int:
        """Explicit deletion primitive; delivery and retention never call it."""
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
            self._release_admission_reservation()
            return deleted


def cleanup_old_recordings(
    *,
    now: datetime | None = None,
    retention_days: int | None = None,
) -> int:
    """Compatibility no-op: recording artifacts require explicit deletion.

    Older deployments called this function at startup. Keeping it non-mutating
    ensures an upgrade cannot erase PCM, the receive timeline, or even metadata
    for legacy ``delivered_audio_deleted`` sessions.
    """
    del now, retention_days
    return 0


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
    for session_dir in _session_directories(root):
        try:
            metadata, metadata_state = _load_session_metadata(session_dir)
            if metadata_state != "valid":
                continue
            status = str(metadata.get("status") or "")
            # A later processing checkpoint must never make a known-incomplete
            # capture eligible for automatic publication. Integrity failures
            # are sticky even if an older build overwrote the visible status.
            degraded_ok = degraded_recording_is_processable(session_dir, metadata)
            if metadata.get("capture_integrity_failed") and not degraded_ok:
                continue
            if status not in RECOVERABLE_STATUSES and not (
                degraded_ok and status == "capture_interrupted"
            ):
                continue
            if status in TERMINAL_STATUSES and not (
                degraded_ok and status == "capture_interrupted"
            ):
                continue
            # A recoverable processing/delivery status is trustworthy only
            # after receiver cleanup durably recorded its end. A process may
            # die immediately after claim_delivery changed the status while
            # capture was still open; publishing that prefix would be unsafe.
            if not metadata.get("capture_ended_at"):
                continue
            raw_validation = validate_recording_raw(session_dir, metadata)
            if (
                not raw_validation.valid
                or not raw_validation.checked_against_manifest
            ):
                continue
            retry_at = metadata.get("next_retry_at")
            if retry_at:
                parsed = datetime.fromisoformat(retry_at)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed > current:
                    continue
            candidates.append(RecoverableRecording(session_dir, metadata))
        except (OSError, ValueError, TypeError):
            continue
    candidates.sort(key=lambda item: str(item.metadata.get("started_at") or item.session_dir.name))
    return candidates


def find_stale_active_recordings() -> list[RecoverableRecording]:
    """Find capture artifacts left active by a previous process.

    These must never enter normal recovery: a process exit necessarily created
    an unknown audio gap. The voice startup reconciler quarantines them as
    interrupted, retains all source files, and warns the original VC instead.
    Pending warnings remain discoverable so a transient Discord outage does not
    permanently suppress the operator notification.
    """
    candidates: list[RecoverableRecording] = []
    root = recording_root()
    if not root.exists():
        return candidates
    for session_dir in _session_directories(root):
        metadata, metadata_state = _load_session_metadata(session_dir)
        if metadata_state != "valid":
            # Raw PCM must stay discoverable even if an abrupt filesystem loss
            # removed or tore only its JSON index. It is quarantined, never
            # handed to normal publication recovery.
            if any(session_dir.glob("user-*.pcm")):
                candidates.append(
                    RecoverableRecording(
                        session_dir,
                        _synthesized_interrupted_metadata(
                            session_dir,
                            metadata_state,
                        ),
                    )
                )
            continue
        status = str(metadata.get("status") or "")
        interrupted_after_exit = bool(
            metadata.get("orphaned_capture")
            and metadata.get("capture_integrity_failed")
        )
        notification_sent = (
            str(metadata.get("orphan_notification_status") or "") == "sent"
        )
        unfinished_recoverable = bool(
            status in RECOVERABLE_STATUSES and not metadata.get("capture_ended_at")
        )
        # The immediately preceding release checkpointed an open capture as
        # ``shutdown_pending`` without a final capture manifest.  Treat that
        # legacy value exactly like an orphaned live recording: process exit
        # introduced an unknown gap, so it must be quarantined for explicit
        # review rather than promoted through automatic recovery.
        legacy_shutdown_pending = bool(
            status == "shutdown_pending" and not metadata.get("capture_ended_at")
        )
        if (
            status == "recording"
            or legacy_shutdown_pending
            or unfinished_recoverable
            or (interrupted_after_exit and not notification_sent)
        ):
            candidates.append(RecoverableRecording(session_dir, metadata))
    candidates.sort(
        key=lambda item: str(item.metadata.get("started_at") or item.session_dir.name)
    )
    return candidates


def update_recording_metadata(session_dir: Path, **fields) -> dict:
    """Atomically update metadata without opening or changing raw audio files."""
    path = session_dir / "metadata.json"
    _ensure_private_directory(session_dir)
    current, metadata_state = _load_session_metadata(session_dir)
    if metadata_state == "corrupt":
        backup = session_dir / f"metadata.corrupt-{uuid4().hex[:12]}.json"
        shutil.copy2(path, backup)
        backup.chmod(0o600)
        with backup.open("rb") as preserved:
            os.fsync(preserved.fileno())
        _fsync_directory(session_dir)
    current.update(fields)
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_private_json(path, current)
    return current


def quarantine_stale_recording(candidate: RecoverableRecording) -> dict:
    """Fail closed after a process exit while leaving PCM/timeline untouched."""
    current = candidate.metadata
    notification_status = str(current.get("orphan_notification_status") or "")
    reason = str(current.get("capture_integrity_reason") or "")
    if not reason:
        reason = "process-exited-during-capture"
    sizes = _pcm_file_sizes(candidate.session_dir)
    fields = {
        **current,
        "status": "capture_interrupted",
        "capture_integrity_failed": True,
        "capture_integrity_reason": reason,
        "capture_interruption_reason": reason,
        "orphaned_capture": True,
        "orphan_detected_at": datetime.now(timezone.utc).isoformat(),
        "orphan_notification_status": (
            notification_status if notification_status == "sent" else "pending"
        ),
        "raw_recording_retained": True,
        "timeline_retained": (candidate.session_dir / TIMELINE_MANIFEST_NAME).exists(),
        "total_bytes": sum(sizes.values()),
        "track_bytes": {
            str(user_id): size for user_id, size in sorted(sizes.items())
        },
    }
    return update_recording_metadata(candidate.session_dir, **fields)
