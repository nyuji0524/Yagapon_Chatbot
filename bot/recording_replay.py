"""Non-destructive replay of archived VC recordings for production-like testing."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import discord

from bot.ai_retry import new_genai_client
from bot.meeting_time import display_meeting_date, meeting_datetime
from bot.minutes_generation import generate_minutes
from bot.recording import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    RecordingTimelineSegment,
    _ensure_private_directory,
    _fsync_directory,
    _load_session_metadata,
    _session_directories,
    _synthesized_interrupted_metadata,
    load_recording_timeline_segments,
    load_transcript_content,
    load_transcript_provenance,
    read_recording_timeline_audio,
    recording_root,
    save_external_transcript,
    transcript_root,
    validate_recording_raw,
)
from bot.recording_publication import (
    UnsafeRecordingPublication,
    load_safe_finalized_recording,
    require_complete_transcript_provenance,
)

log = logging.getLogger("yagapon.recording_replay")

REPLAY_TERMINAL_CHECKPOINTS = {
    "completed": "replay_completed",
    "review_completed": "replay_review_completed",
    "failed": "replay_failed",
    "interrupted": "replay_interrupted",
}

REVIEWABLE_RECORDING_STATUSES = frozenset(
    {
        "capture_interrupted",
        "capture_suspicious",
        "recording_truncated",
        "storage_limit_reached",
        "leave_failed",
        "final_reprocess_deferred",
        "final_reprocess_empty",
        "recovery_dead_letter",
    }
)


def replay_root() -> Path:
    configured = os.environ.get("YAGAPON_REPLAY_DIR")
    return Path(configured) if configured else recording_root().parent / "replays"


def _atomic_json_write(path: Path, payload: dict) -> None:
    """Replace one JSON checkpoint without exposing a partially written file."""
    _ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(payload, destination, ensure_ascii=False, indent=2)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _nonempty_file_within(path_value: str, root: Path) -> Path | None:
    """Resolve a declared artifact only when it is a non-empty file below root."""
    if not path_value:
        return None
    try:
        resolved = Path(path_value).resolve()
        resolved.relative_to(root.resolve())
        if resolved.is_file() and resolved.stat().st_size > 0:
            return resolved
    except (OSError, ValueError):
        return None
    return None


def reconcile_completed_replay_checkpoints(root: Path | None = None) -> dict[str, int]:
    """Repair only proven-complete legacy replay checkpoints.

    Older replay runs persisted ``metadata.json`` as ``processing`` while the
    authoritative ``job.json`` reached ``completed``.  A terminal job alone is
    not sufficient evidence: both durable local artifacts, the archived
    transcript, and the checkpoint/job identity must agree before this function
    atomically changes the checkpoint status.  It never regenerates or deletes
    meeting data.
    """
    root = root or replay_root()
    result = {
        "inspected": 0,
        "reconciled": 0,
        "skipped_non_completed": 0,
        "skipped_non_processing": 0,
        "skipped_invalid": 0,
        "skipped_missing_artifacts": 0,
        "write_failed": 0,
    }
    if not root.is_dir():
        return result

    transcript_archive = transcript_root().resolve()
    for job_path in sorted(root.glob("*/job.json")):
        result["inspected"] += 1
        work_dir = job_path.parent
        metadata_path = work_dir / "metadata.json"
        try:
            stored = json.loads(job_path.read_text(encoding="utf-8"))
            job = ReplayJob(**stored)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            result["skipped_invalid"] += 1
            continue

        if job.status != "completed" or job.stage != "completed":
            result["skipped_non_completed"] += 1
            continue
        if metadata.get("status") != "processing":
            result["skipped_non_processing"] += 1
            continue
        if (
            job.id != work_dir.name
            or str(metadata.get("source_session_id") or "") != job.source_session_id
            or str(metadata.get("source_guild_id") or "") != str(job.source_guild_id)
            or str(metadata.get("target_guild_id") or "") != str(job.target_guild_id)
        ):
            result["skipped_invalid"] += 1
            continue

        work_transcript = _nonempty_file_within(
            str(work_dir / "transcript.md"),
            work_dir,
        )
        work_minutes = _nonempty_file_within(job.minutes_path, work_dir)
        archived_transcript = _nonempty_file_within(
            job.transcript_path,
            transcript_archive,
        )
        expected_archived_transcript = (
            transcript_archive / str(job.target_guild_id) / f"{job.id}.md"
        ).resolve()
        if (
            work_transcript is None
            or work_minutes is None
            or work_minutes != (work_dir / "minutes.md").resolve()
            or archived_transcript is None
            or archived_transcript != expected_archived_transcript
        ):
            result["skipped_missing_artifacts"] += 1
            continue

        now = datetime.now(timezone.utc).isoformat()
        reconciled = {
            **metadata,
            "status": REPLAY_TERMINAL_CHECKPOINTS["completed"],
            "job_status": job.status,
            "job_stage": job.stage,
            "reconciled_from_status": "processing",
            "reconciled_at": now,
            "updated_at": now,
        }
        try:
            _atomic_json_write(metadata_path, reconciled)
        except OSError:
            result["write_failed"] += 1
            log.exception("Could not reconcile completed replay checkpoint")
        else:
            result["reconciled"] += 1
    return result


def _drive_session_id(artifact: "RecordingArtifact", target_guild_id: int) -> str:
    """Return a retry-stable Drive idempotency key for a replay artifact."""
    return (
        f"recording-replay:{target_guild_id}:"
        f"{artifact.source_guild_id}:{artifact.session_id}"
    )


@dataclass(frozen=True)
class RecordingArtifact:
    session_id: str
    source_guild_id: int
    channel_name: str
    mode: str
    status: str
    started_at: str
    raw_bytes: int
    estimated_seconds: int
    transcript_path: Path | None
    transcript_bytes: int
    session_dir: Path
    source_channel_id: int | None = None
    transcript_complete: bool = False
    capture_integrity_failed: bool = False
    provenance_present: bool = False
    raw_integrity_valid: bool = False
    raw_integrity_reason: str = ""
    raw_manifest_verified: bool = False

    def public_dict(self) -> dict:
        value = asdict(self)
        value.pop("transcript_path", None)
        value.pop("session_dir", None)
        value["has_raw_audio"] = self.raw_bytes > 0
        value["has_transcript"] = self.transcript_bytes > 0
        return value


@dataclass
class ReplayJob:
    id: str
    source_session_id: str
    source_guild_id: int
    target_guild_id: int
    target_channel_id: int
    retranscribe: bool
    transcription_quality: str = "high_accuracy"
    review_only: bool = False
    status: str = "queued"
    stage: str = "queued"
    created_at: str = ""
    updated_at: str = ""
    error_type: str = ""
    minutes_path: str = ""
    transcript_path: str = ""
    drive_minutes_url: str = ""
    drive_transcript_url: str = ""

    def public_dict(self) -> dict:
        value = asdict(self)
        value["minutes_saved"] = bool(value.pop("minutes_path"))
        value["transcript_saved"] = bool(value.pop("transcript_path"))
        return value

    def storage_dict(self) -> dict:
        return asdict(self)


def _safe_transcript_path(guild_id: int, session_id: str, metadata: dict) -> Path | None:
    root = transcript_root().resolve()
    deterministic = root / str(guild_id) / f"{session_id}.md"
    candidates = [deterministic]
    configured = str(metadata.get("transcript_path") or "")
    if configured:
        candidates.append(Path(configured))
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
            if resolved.is_file() and resolved.stat().st_size > 0:
                return resolved
        except (OSError, ValueError):
            continue
    return None


def list_recording_artifacts() -> list[RecordingArtifact]:
    """List sessions that still have raw PCM or a durable transcript."""
    artifacts = []
    root = recording_root()
    pcm_bytes_per_second = PCM_CHANNELS * PCM_SAMPLE_RATE * PCM_SAMPLE_WIDTH
    for session_dir in _session_directories(root):
        try:
            metadata, metadata_state = _load_session_metadata(session_dir)
            if metadata_state != "valid":
                metadata = _synthesized_interrupted_metadata(
                    session_dir,
                    metadata_state,
                )
            guild_id = int(metadata.get("guild_id") or session_dir.parent.name)
            pcm_files = list(session_dir.glob("user-*.pcm"))
            raw_bytes = sum(path.stat().st_size for path in pcm_files)
            longest_stream = max((path.stat().st_size for path in pcm_files), default=0)
            transcript = _safe_transcript_path(guild_id, session_dir.name, metadata)
            transcript_bytes = transcript.stat().st_size if transcript else 0
            provenance = load_transcript_provenance(transcript)
            if raw_bytes <= 0 and transcript_bytes <= 0:
                continue
            raw_validation = validate_recording_raw(session_dir, metadata)
            raw_integrity_valid = bool(
                raw_bytes > 0
                and metadata_state == "valid"
                and raw_validation.valid
            )
            raw_integrity_reason = ""
            if raw_bytes > 0 and metadata_state != "valid":
                raw_integrity_reason = f"recording-metadata-{metadata_state}"
            elif raw_bytes > 0 and not raw_validation.valid:
                raw_integrity_reason = raw_validation.reason
            listed_status = str(metadata.get("status") or "unknown")
            if raw_bytes > 0 and not raw_integrity_valid:
                listed_status = "capture_interrupted"
            artifacts.append(RecordingArtifact(
                session_id=session_dir.name,
                source_guild_id=guild_id,
                channel_name=str(metadata.get("channel_name") or "unknown"),
                mode=str(metadata.get("mode") or "listen"),
                status=listed_status,
                started_at=str(metadata.get("started_at") or session_dir.name),
                raw_bytes=raw_bytes,
                estimated_seconds=round(longest_stream / pcm_bytes_per_second),
                transcript_path=transcript,
                transcript_bytes=transcript_bytes,
                session_dir=session_dir,
                source_channel_id=(
                    int(metadata["channel_id"]) if metadata.get("channel_id") else None
                ),
                transcript_complete=bool(
                    metadata.get("transcript_complete") is True
                    and provenance.get("transcript_complete") is True
                ),
                capture_integrity_failed=bool(
                    metadata.get("capture_integrity_failed") is True
                    or provenance.get("capture_integrity_failed") is True
                    or (raw_bytes > 0 and not raw_integrity_valid)
                ),
                provenance_present=bool(provenance),
                raw_integrity_valid=raw_integrity_valid,
                raw_integrity_reason=raw_integrity_reason,
                raw_manifest_verified=bool(
                    raw_integrity_valid
                    and raw_validation.checked_against_manifest
                ),
            ))
        except (OSError, ValueError, json.JSONDecodeError):
            log.exception("Could not inspect recording session path=%s", session_dir)
    known = {(item.source_guild_id, item.session_id) for item in artifacts}
    transcript_archive = transcript_root()
    if transcript_archive.exists():
        for path in transcript_archive.glob("*/*.md"):
            try:
                if path.stem.startswith("replay-"):
                    continue
                guild_id = int(path.parent.name)
                key = (guild_id, path.stem)
                if key in known or path.stat().st_size <= 0:
                    continue
                with path.open(encoding="utf-8", errors="replace") as source:
                    first_line = source.readline().strip()
                provenance = load_transcript_provenance(path)
                channel_name = first_line.removeprefix("# 文字起こし - ").strip() or "archived"
                artifacts.append(RecordingArtifact(
                    session_id=path.stem,
                    source_guild_id=guild_id,
                    channel_name=channel_name,
                    mode="archived-transcript",
                    status="transcript_archived",
                    started_at=path.stem,
                    raw_bytes=0,
                    estimated_seconds=0,
                    transcript_path=path.resolve(),
                    transcript_bytes=path.stat().st_size,
                    session_dir=root / str(guild_id) / path.stem,
                    source_channel_id=None,
                    transcript_complete=provenance.get("transcript_complete") is True,
                    capture_integrity_failed=(
                        provenance.get("capture_integrity_failed") is True
                    ),
                    provenance_present=bool(provenance),
                    raw_integrity_valid=False,
                    raw_integrity_reason="raw-audio-missing",
                    raw_manifest_verified=False,
                ))
            except (OSError, ValueError):
                log.exception("Could not inspect transcript archive path=%s", path)
    artifacts.sort(
        key=lambda item: (
            item.estimated_seconds,
            item.raw_bytes,
            item.transcript_bytes,
            item.started_at,
        ),
        reverse=True,
    )
    return artifacts


def find_recording_artifact(session_id: str) -> RecordingArtifact:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", session_id):
        raise ValueError("invalid session id")
    matches = [item for item in list_recording_artifacts() if item.session_id == session_id]
    if not matches:
        raise FileNotFoundError(session_id)
    if len(matches) > 1:
        raise ValueError("session id is ambiguous")
    return matches[0]


def validate_recording_artifact_for_replay(
    artifact: RecordingArtifact,
    *,
    retranscribe: bool,
    config=None,
) -> dict:
    """Prove a replay source is finalized, complete, and export-approved.

    Even transcript-only replay sends meeting content to the minutes model and
    external destinations, so it requires the retained raw recording and its
    completeness manifest as independent evidence that the source capture is
    safe.  Transcript archives without that evidence stay locally inspectable.
    """
    if artifact.raw_bytes <= 0:
        raise UnsafeRecordingPublication("raw audio is no longer available")
    if not artifact.raw_integrity_valid:
        raise UnsafeRecordingPublication(
            "raw audio failed integrity validation: "
            f"{artifact.raw_integrity_reason or 'unknown'}"
        )
    if not artifact.raw_manifest_verified:
        raise UnsafeRecordingPublication(
            "raw audio has no trusted completeness manifest"
        )
    if artifact.capture_integrity_failed:
        raise UnsafeRecordingPublication("raw audio capture is marked incomplete")

    metadata = load_safe_finalized_recording(
        artifact.session_dir,
        source_guild_id=artifact.source_guild_id,
        config=config,
        require_raw_manifest=True,
        expected_raw_bytes=artifact.raw_bytes,
    )
    if not retranscribe:
        if artifact.transcript_path is None:
            raise UnsafeRecordingPublication(
                "saved transcript is unavailable; retry explicitly with retranscribe=true"
            )
        if not artifact.provenance_present or not artifact.transcript_complete:
            raise UnsafeRecordingPublication(
                "saved transcript may be incomplete; retry explicitly with retranscribe=true"
            )
        require_complete_transcript_provenance(
            artifact.transcript_path,
            expected_guild_id=artifact.source_guild_id,
            expected_session_id=artifact.session_id,
        )
    return metadata


def validate_recording_artifact_for_review(artifact: RecordingArtifact) -> dict:
    """Validate retained partial PCM for an explicit owner-only review.

    This deliberately does not promote the capture to healthy or exportable.
    A partial manifest may be used for human inspection, but every manifest
    fact that is present still has to match the retained files.
    """
    if artifact.raw_bytes <= 0:
        raise UnsafeRecordingPublication("raw audio is no longer available")
    expected_dir = (
        recording_root() / str(artifact.source_guild_id) / artifact.session_id
    ).resolve()
    if artifact.session_dir.resolve() != expected_dir:
        raise UnsafeRecordingPublication("review source is outside the recording archive")
    metadata, metadata_state = _load_session_metadata(artifact.session_dir)
    if metadata_state != "valid":
        raise UnsafeRecordingPublication(
            f"recording metadata is {metadata_state}; review identity is unresolved"
        )
    try:
        metadata_guild_id = int(metadata.get("guild_id"))
    except (TypeError, ValueError) as exc:
        raise UnsafeRecordingPublication("source guild is unresolved") from exc
    if metadata_guild_id != artifact.source_guild_id:
        raise UnsafeRecordingPublication("source guild does not match recording metadata")
    status = str(metadata.get("status") or "")
    if status not in REVIEWABLE_RECORDING_STATUSES:
        raise UnsafeRecordingPublication(
            f"recording is not an interrupted review source: {status or 'unknown'}"
        )
    validation = validate_recording_raw(artifact.session_dir, metadata)
    if not validation.valid:
        raise UnsafeRecordingPublication(
            "partial raw audio failed integrity validation: "
            f"{validation.reason or 'unknown'}"
        )
    if validation.total_bytes != artifact.raw_bytes:
        raise UnsafeRecordingPublication("partial raw audio changed after inventory")
    return metadata


def validate_recording_review_context(
    artifact: RecordingArtifact,
    *,
    bot,
    target_channel,
    review_actor,
) -> dict:
    """Keep partial review inside the exact original Discord audience."""
    metadata = validate_recording_artifact_for_review(artifact)
    source_channel_id = artifact.source_channel_id
    if source_channel_id is None:
        raise UnsafeRecordingPublication("original source channel is unresolved")
    target_guild = getattr(target_channel, "guild", None)
    if getattr(target_guild, "id", None) != artifact.source_guild_id:
        raise UnsafeRecordingPublication("review must run in the original source guild")
    if getattr(target_channel, "id", None) != source_channel_id:
        raise UnsafeRecordingPublication("review must run in the original source channel")

    get_guild = getattr(bot, "get_guild", None)
    source_guild = get_guild(artifact.source_guild_id) if callable(get_guild) else None
    if source_guild is None:
        raise UnsafeRecordingPublication("original source guild is unavailable")
    get_channel = getattr(source_guild, "get_channel_or_thread", None)
    if not callable(get_channel):
        get_channel = getattr(source_guild, "get_channel", None)
    source_channel = get_channel(source_channel_id) if callable(get_channel) else None
    if source_channel is None or getattr(source_channel, "id", None) != source_channel_id:
        raise UnsafeRecordingPublication("original source channel is unavailable")
    actor_id = getattr(review_actor, "id", None)
    get_member = getattr(source_guild, "get_member", None)
    member = get_member(actor_id) if actor_id is not None and callable(get_member) else None
    if member is None:
        actor_guild = getattr(review_actor, "guild", None)
        if getattr(actor_guild, "id", None) == artifact.source_guild_id:
            member = review_actor
    if member is None:
        raise UnsafeRecordingPublication("review operator is not in the source guild")
    permissions_for = getattr(source_channel, "permissions_for", None)
    if not callable(permissions_for):
        raise UnsafeRecordingPublication("source channel permissions are unresolved")
    try:
        permissions = permissions_for(member)
    except Exception as exc:
        raise UnsafeRecordingPublication(
            "source channel permissions are unresolved"
        ) from exc
    if not bool(
        getattr(
            permissions,
            "view_channel",
            getattr(permissions, "read_messages", False),
        )
    ):
        raise UnsafeRecordingPublication("review operator cannot view source channel")
    return metadata


def recording_artifact_is_replayable(
    artifact: RecordingArtifact,
    *,
    retranscribe: bool,
    config=None,
) -> bool:
    """Return whether automatic replay may safely select this artifact."""
    try:
        validate_recording_artifact_for_replay(
            artifact,
            retranscribe=retranscribe,
            config=config,
        )
    except (OSError, UnsafeRecordingPublication):
        return False
    return True


class ReadOnlyReplaySink:
    """VoiceSession-compatible sink that never edits or deletes source PCM."""

    def __init__(
        self,
        artifact: RecordingArtifact,
        target_guild_id: int,
        job_id: str,
        *,
        review_only: bool = False,
    ):
        self.artifact = artifact
        self.guild_id = target_guild_id
        self.channel_name = f"replay-{artifact.channel_name}"
        self.mode = "listen"
        self.session_dir = replay_root() / job_id
        self.review_only = review_only
        _ensure_private_directory(self.session_dir)
        self._metadata_path = self.session_dir / "metadata.json"

    @property
    def total_bytes(self) -> int:
        return self.artifact.raw_bytes

    @property
    def limit_reached(self) -> bool:
        return False

    def read_chunks(self, offsets: dict[int, int], *, chunk_bytes: int):
        chunks = {}
        for path in self.artifact.session_dir.glob("user-*.pcm"):
            try:
                user_id = int(path.stem.removeprefix("user-"))
            except ValueError:
                continue
            start = max(0, int(offsets.get(user_id, 0)))
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
        gap_seconds: float = 1.25,
    ) -> list[RecordingTimelineSegment] | None:
        return load_recording_timeline_segments(
            self.artifact.session_dir,
            max_bytes=max_bytes,
            gap_seconds=gap_seconds,
        )

    def read_timeline_audio(self, segment: RecordingTimelineSegment) -> bytes:
        return read_recording_timeline_audio(self.artifact.session_dir, segment)

    def _write_metadata(self, status: str, **extra) -> None:
        current = {}
        if self._metadata_path.exists():
            try:
                current = json.loads(self._metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {}
        current.update({
            "status": status,
            "source_session_id": self.artifact.session_id,
            "source_guild_id": self.artifact.source_guild_id,
            "target_guild_id": self.guild_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **extra,
        })
        _atomic_json_write(self._metadata_path, current)

    def checkpoint(self, *, offsets, speaker_labels, transcript_path=None, status="processing", **extra):
        if self.review_only:
            status = "review_only_processing"
            extra = {
                **extra,
                "transcript_complete": False,
                "capture_integrity_failed": True,
                "review_only": True,
            }
        self._write_metadata(
            status,
            offsets={str(key): int(value) for key, value in offsets.items()},
            speaker_labels={str(key): value for key, value in speaker_labels.items()},
            transcript_path=str(transcript_path or ""),
            **extra,
        )

    def mark(self, status: str, **extra) -> None:
        self._write_metadata(status, **extra)

    def save_transcript(self, content: str) -> Path | None:
        return save_external_transcript(
            self.guild_id,
            self.session_dir.name,
            self.channel_name,
            content,
            mode="recording-replay",
            transcript_complete=not self.review_only,
            capture_integrity_failed=(
                self.review_only or self.artifact.capture_integrity_failed
            ),
            status=(
                "review_only_partial"
                if self.review_only
                else "final_reprocess_completed"
            ),
        )

    def delete_audio(self) -> int:
        return 0


class RecordingReplayManager:
    def __init__(self, bot):
        self.bot = bot
        self.jobs: dict[str, ReplayJob] = {}
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    def active(self) -> bool:
        return any(job.status in {"queued", "running"} for job in self.jobs.values())

    @staticmethod
    def reconcile_completed_checkpoints() -> dict[str, int]:
        return reconcile_completed_replay_checkpoints()

    @staticmethod
    def _mark_checkpoint(
        sink: ReadOnlyReplaySink | None,
        status: str,
        **details,
    ) -> None:
        """Persist a terminal sink state without masking the replay outcome."""
        if sink is None:
            return
        try:
            sink.mark(status, **details)
        except Exception:
            log.exception("Could not persist replay terminal checkpoint status=%s", status)

    def start(
        self,
        *,
        artifact: RecordingArtifact,
        target_channel,
        retranscribe: bool,
        transcription_quality: str = "high_accuracy",
        review_only: bool = False,
        review_actor=None,
    ) -> ReplayJob:
        if self._closing:
            raise RuntimeError("recording replay manager is shutting down")
        if self.active():
            raise RuntimeError("another recording replay is already running")
        if transcription_quality not in {"standard", "high_accuracy"}:
            raise ValueError("invalid transcription quality")
        if review_only:
            if not retranscribe:
                raise ValueError("review_only requires retranscribe=true")
            validate_recording_review_context(
                artifact,
                bot=self.bot,
                target_channel=target_channel,
                review_actor=review_actor,
            )
        else:
            validate_recording_artifact_for_replay(
                artifact,
                retranscribe=retranscribe,
                config=getattr(self.bot, "config", None),
            )
        now = datetime.now(timezone.utc).isoformat()
        job = ReplayJob(
            id=f"replay-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}",
            source_session_id=artifact.session_id,
            source_guild_id=artifact.source_guild_id,
            target_guild_id=target_channel.guild.id,
            target_channel_id=target_channel.id,
            retranscribe=retranscribe,
            transcription_quality=transcription_quality,
            review_only=review_only,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job.id] = job
        self._persist_job(job)
        task = asyncio.create_task(
            self._run(
                job,
                artifact,
                target_channel,
                review_actor=review_actor,
            ),
            name=job.id,
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def shutdown(self, *, timeout: float = 20.0) -> None:
        """Stop accepting jobs, drain briefly, then checkpoint interrupted work."""
        self._closing = True
        tasks = [task for task in self._tasks if not task.done()]
        if not tasks:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        _done, pending = await asyncio.wait(
            tasks,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        for task in pending:
            task.cancel()
        if pending:
            # A Drive executor call cannot be force-cancelled. Give its
            # checkpoint handler only the remaining caller budget, then let
            # process shutdown end it rather than exceeding Docker's grace.
            _done_after_cancel, stubborn = await asyncio.wait(
                pending,
                timeout=max(0.0, deadline - time.monotonic()),
            )
            if stubborn:
                log.error(
                    "Recording replay shutdown deadline reached; abandoning %s task(s)",
                    len(stubborn),
                )

    def get(self, job_id: str) -> ReplayJob | None:
        in_memory = self.jobs.get(job_id)
        if in_memory:
            return in_memory
        if not re.fullmatch(r"replay-[A-Za-z0-9_.-]{1,120}", job_id):
            return None
        path = replay_root() / job_id / "job.json"
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            job = ReplayJob(**stored)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if job.status in {"queued", "running"}:
            job.status = "interrupted"
            job.stage = "interrupted"
            job.error_type = "ProcessRestarted"
            job.updated_at = datetime.now(timezone.utc).isoformat()
            self._persist_job(job)
        self.jobs[job.id] = job
        return job

    @staticmethod
    def _persist_job(job: ReplayJob) -> None:
        work_dir = replay_root() / job.id
        _ensure_private_directory(work_dir)
        metadata = work_dir / "job.json"
        _atomic_json_write(metadata, job.storage_dict())

    def _stage(self, job: ReplayJob, stage: str) -> None:
        job.stage = stage
        job.updated_at = datetime.now(timezone.utc).isoformat()
        self._persist_job(job)

    async def _run(
        self,
        job: ReplayJob,
        artifact: RecordingArtifact,
        target_channel,
        *,
        review_actor=None,
    ) -> None:
        job.status = "running"
        self._stage(job, "preparing")
        work_dir = replay_root() / job.id
        _ensure_private_directory(work_dir)
        replay_sink: ReadOnlyReplaySink | None = None
        try:
            # Revalidate after task scheduling and before the first external
            # call.  This closes the inventory/start race if retained files or
            # policy are changed between command handling and execution.
            if job.review_only:
                validate_recording_review_context(
                    artifact,
                    bot=self.bot,
                    target_channel=target_channel,
                    review_actor=review_actor,
                )
            else:
                validate_recording_artifact_for_replay(
                    artifact,
                    retranscribe=job.retranscribe,
                    config=getattr(self.bot, "config", None),
                )
            if job.review_only:
                heading = "⚠️ 中断録音の確認用文字起こしを開始します。"
                method = "不完全PCMの確認専用"
            else:
                heading = "🧪 長時間録音のテスト議事録生成を開始します。"
                method = (
                    "PCMから再文字起こし"
                    if job.retranscribe
                    else "保存済み文字起こしを再利用"
                )
            await target_channel.send(
                f"{heading}\n"
                f"ジョブ: `{job.id}` / 元セッション: `{artifact.session_id}`\n"
                f"方式: `{method}`",
                silent=True,
            )
            transcript = ""
            if artifact.transcript_path and not job.retranscribe:
                self._stage(job, "loading_transcript")
                transcript = await asyncio.to_thread(load_transcript_content, artifact.transcript_path)
                archived = await asyncio.to_thread(
                    save_external_transcript,
                    job.target_guild_id,
                    job.id,
                    f"テスト再生 - {artifact.channel_name}",
                    transcript,
                    mode="recording-replay",
                    transcript_complete=True,
                    capture_integrity_failed=False,
                    status="transcript_reused",
                )
                job.transcript_path = str(archived or "")
            else:
                self._stage(job, "transcribing")
                from bot.voice import VoiceMode, VoiceSession

                session = VoiceSession(
                    self.bot,
                    artifact.source_guild_id,
                    SimpleNamespace(
                        id=0,
                        name=f"テスト再生 - {artifact.channel_name}",
                        guild=SimpleNamespace(id=artifact.source_guild_id),
                    ),
                    VoiceMode.LISTEN,
                )
                session.prefer_dedicated_transcription = (
                    job.transcription_quality == "high_accuracy"
                )
                replay_sink = ReadOnlyReplaySink(
                    artifact,
                    job.target_guild_id,
                    job.id,
                    review_only=job.review_only,
                )
                session.recording_sink = replay_sink
                session.started_at = meeting_datetime(artifact.started_at or None)
                completed = await session._reprocess_complete_recording()
                await session._persist_transcript()
                if (
                    not completed
                    or session._transcription_failed
                    or not session.transcript_text.strip()
                ):
                    raise RuntimeError("replay transcription did not complete")
                transcript = session.transcript_text
                job.transcript_path = str(session.transcript_path or "")

            if not transcript.strip():
                raise RuntimeError("recording transcript is empty")
            (work_dir / "transcript.md").write_text(transcript, encoding="utf-8")

            if job.review_only:
                validate_recording_review_context(
                    artifact,
                    bot=self.bot,
                    target_channel=target_channel,
                    review_actor=review_actor,
                )
                embed = discord.Embed(
                    title=f"⚠️ 確認専用・不完全な文字起こし - {artifact.channel_name}",
                    description=(
                        "録音中断前に保存できた範囲だけを文字起こししたものです。"
                        "完全な会議記録ではなく、Drive・Knowledge・検索には公開していません。"
                    ),
                    color=discord.Color.orange(),
                )
                embed.add_field(
                    name="元セッション",
                    value=f"`{artifact.session_id}`",
                    inline=False,
                )
                await target_channel.send(
                    embed=embed,
                    file=discord.File(
                        io.BytesIO(transcript.encode()),
                        filename=f"{job.id}-INCOMPLETE-transcript.md",
                    ),
                    silent=True,
                )
                job.status = "review_completed"
                self._stage(job, "review_completed")
                self._mark_checkpoint(
                    replay_sink,
                    REPLAY_TERMINAL_CHECKPOINTS["review_completed"],
                    job_status=job.status,
                    job_stage=job.stage,
                    review_only=True,
                    transcript_complete=False,
                    capture_integrity_failed=True,
                )
                return

            self._stage(job, "generating_minutes")
            validate_recording_artifact_for_replay(
                artifact,
                retranscribe=job.retranscribe,
                config=getattr(self.bot, "config", None),
            )
            archived_transcript = Path(job.transcript_path) if job.transcript_path else None
            if archived_transcript is None:
                raise UnsafeRecordingPublication("replay transcript was not archived")
            require_complete_transcript_provenance(
                archived_transcript,
                expected_guild_id=job.target_guild_id,
                expected_session_id=job.id,
            )
            minutes = await generate_minutes(
                new_genai_client(os.environ.get("GOOGLE_API_KEY", "")),
                transcript,
                title=f"テスト再生 - {artifact.channel_name}",
                glossary_text=self.bot.config.get_glossary_text(artifact.source_guild_id),
                speaker_roster=self.bot._build_members_info(artifact.source_guild_id),
                meeting_date=display_meeting_date(artifact.started_at or None),
                checkpoint_dir=work_dir / "minutes-checkpoints",
            )
            if not minutes.strip():
                raise RuntimeError("minutes generation returned empty content")
            minutes_path = work_dir / "minutes.md"
            minutes_path.write_text(minutes, encoding="utf-8")
            job.minutes_path = str(minutes_path)

            self._stage(job, "publishing")
            validate_recording_artifact_for_replay(
                artifact,
                retranscribe=job.retranscribe,
                config=getattr(self.bot, "config", None),
            )
            require_complete_transcript_provenance(
                archived_transcript,
                expected_guild_id=job.target_guild_id,
                expected_session_id=job.id,
            )
            from bot.commands.voice_cmd import _summarize_minutes
            from bot.gdrive import upload_minutes, upload_transcript

            title = f"[テスト] {artifact.channel_name} {artifact.session_id}"
            drive_session_id = _drive_session_id(artifact, job.target_guild_id)
            publish_batch = asyncio.gather(
                upload_transcript(
                    self.bot.config,
                    job.target_guild_id,
                    transcript,
                    title,
                    meeting_at=artifact.started_at or None,
                    session_id=drive_session_id,
                ),
                upload_minutes(
                    self.bot.config,
                    job.target_guild_id,
                    minutes,
                    title,
                    meeting_at=artifact.started_at or None,
                    session_id=drive_session_id,
                ),
                _summarize_minutes(minutes),
                return_exceptions=True,
            )
            publish_cancelled = False
            try:
                # Drive writes run in executor threads and cannot be stopped
                # safely once started.  Finish the batch so any successful URL
                # can be checkpointed before interruption/failure is recorded.
                publish_results = await asyncio.shield(publish_batch)
            except asyncio.CancelledError:
                publish_results = await publish_batch
                publish_cancelled = True

            transcript_result, minutes_result, summary_result = publish_results
            if not isinstance(transcript_result, BaseException):
                job.drive_transcript_url = transcript_result or ""
            if not isinstance(minutes_result, BaseException):
                job.drive_minutes_url = minutes_result or ""
            self._persist_job(job)

            if publish_cancelled:
                raise asyncio.CancelledError
            publish_error = next(
                (
                    result
                    for result in publish_results
                    if isinstance(result, BaseException)
                ),
                None,
            )
            if publish_error is not None:
                raise publish_error

            transcript_url = job.drive_transcript_url
            minutes_url = job.drive_minutes_url
            summary = summary_result

            from bot.meeting_index import index_bot_meeting

            await index_bot_meeting(
                self.bot,
                guild_id=job.target_guild_id,
                channel_id=job.target_channel_id,
                session_id=artifact.session_id,
                channel_name=artifact.channel_name,
                started_at=artifact.started_at or None,
                transcript=transcript,
                minutes=minutes,
                transcript_url=transcript_url,
                minutes_url=minutes_url,
            )

            embed = discord.Embed(
                title=f"🧪 テスト議事録 - {artifact.channel_name}",
                description=summary[:4096],
                color=discord.Color.green(),
            )
            embed.add_field(name="元セッション", value=f"`{artifact.session_id}`", inline=False)
            embed.add_field(
                name="処理方式",
                value="PCM再文字起こし" if job.retranscribe else "保存済み文字起こしを再利用",
                inline=True,
            )
            if minutes_url:
                embed.add_field(name="📄 議事録", value=f"[Google Docsで見る]({minutes_url})", inline=False)
            if transcript_url:
                embed.add_field(name="📜 生文字起こし", value=f"[Google Docsで見る]({transcript_url})", inline=False)
            files = []
            if not minutes_url:
                files.append(discord.File(io.BytesIO(minutes.encode()), filename=f"{job.id}-minutes.md"))
            if not transcript_url:
                files.append(discord.File(io.BytesIO(transcript.encode()), filename=f"{job.id}-transcript.md"))
            kwargs = {"embed": embed, "silent": True}
            if files:
                kwargs["files"] = files
            await target_channel.send(**kwargs)

            job.status = "completed"
            self._stage(job, "completed")
            self._mark_checkpoint(
                replay_sink,
                REPLAY_TERMINAL_CHECKPOINTS["completed"],
                job_status=job.status,
                job_stage=job.stage,
            )
            self.bot.operations.record_event(
                "recording_replay", "minutes", "success",
                guild_id=job.target_guild_id, item_key=job.id,
                details={
                    "source_guild_id": artifact.source_guild_id,
                    "source_session_id": artifact.session_id,
                    "retranscribe": job.retranscribe,
                },
            )
        except asyncio.CancelledError:
            interrupted_stage = job.stage
            job.status = "interrupted"
            job.stage = "interrupted"
            job.error_type = "CancelledError"
            job.updated_at = datetime.now(timezone.utc).isoformat()
            self._persist_job(job)
            self._mark_checkpoint(
                replay_sink,
                REPLAY_TERMINAL_CHECKPOINTS["interrupted"],
                job_status=job.status,
                job_stage=job.stage,
                interrupted_stage=interrupted_stage,
                error=job.error_type,
            )
            raise
        except Exception as exc:
            failed_stage = job.stage
            job.status = "failed"
            job.stage = "failed"
            job.error_type = type(exc).__name__
            self._persist_job(job)
            self._mark_checkpoint(
                replay_sink,
                REPLAY_TERMINAL_CHECKPOINTS["failed"],
                job_status=job.status,
                job_stage=job.stage,
                failed_stage=failed_stage,
                error=job.error_type,
            )
            if not isinstance(exc, UnsafeRecordingPublication):
                self.bot.operations.record_event(
                    "recording_replay", "minutes", "failed",
                    guild_id=job.target_guild_id, item_key=job.id,
                    details={"error_type": type(exc).__name__},
                )
            log.exception("Recording replay failed job=%s", job.id)
            if not isinstance(exc, UnsafeRecordingPublication):
                try:
                    await target_channel.send(
                        f"❌ テスト議事録の生成に失敗しました。\n"
                        f"ジョブ: `{job.id}` / 失敗段階: `{failed_stage}` / 種別: `{type(exc).__name__}`",
                        silent=True,
                    )
                except Exception:
                    log.exception("Could not notify replay failure job=%s", job.id)
        finally:
            job.updated_at = datetime.now(timezone.utc).isoformat()
            self._persist_job(job)
