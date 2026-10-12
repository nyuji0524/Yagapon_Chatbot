"""Non-destructive replay of archived VC recordings for production-like testing."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import discord

from bot.ai_retry import new_genai_client
from bot.minutes_generation import generate_minutes
from bot.recording import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    load_transcript_content,
    recording_root,
    save_external_transcript,
    transcript_root,
)

log = logging.getLogger("yagapon.recording_replay")


def replay_root() -> Path:
    configured = os.environ.get("YAGAPON_REPLAY_DIR")
    return Path(configured) if configured else recording_root().parent / "replays"


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
    metadata_paths = root.glob("*/*/metadata.json") if root.exists() else []
    for metadata_path in metadata_paths:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            guild_id = int(metadata.get("guild_id") or metadata_path.parent.parent.name)
            pcm_files = list(metadata_path.parent.glob("user-*.pcm"))
            raw_bytes = sum(path.stat().st_size for path in pcm_files)
            longest_stream = max((path.stat().st_size for path in pcm_files), default=0)
            transcript = _safe_transcript_path(guild_id, metadata_path.parent.name, metadata)
            transcript_bytes = transcript.stat().st_size if transcript else 0
            if raw_bytes <= 0 and transcript_bytes <= 0:
                continue
            artifacts.append(RecordingArtifact(
                session_id=metadata_path.parent.name,
                source_guild_id=guild_id,
                channel_name=str(metadata.get("channel_name") or "unknown"),
                mode=str(metadata.get("mode") or "listen"),
                status=str(metadata.get("status") or "unknown"),
                started_at=str(metadata.get("started_at") or metadata_path.parent.name),
                raw_bytes=raw_bytes,
                estimated_seconds=round(longest_stream / pcm_bytes_per_second),
                transcript_path=transcript,
                transcript_bytes=transcript_bytes,
                session_dir=metadata_path.parent,
            ))
        except (OSError, ValueError, json.JSONDecodeError):
            log.exception("Could not inspect recording metadata path=%s", metadata_path)
    known = {(item.source_guild_id, item.session_id) for item in artifacts}
    transcript_archive = transcript_root()
    if transcript_archive.exists():
        for path in transcript_archive.glob("*/*.md"):
            try:
                guild_id = int(path.parent.name)
                key = (guild_id, path.stem)
                if key in known or path.stat().st_size <= 0:
                    continue
                with path.open(encoding="utf-8", errors="replace") as source:
                    first_line = source.readline().strip()
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


class ReadOnlyReplaySink:
    """VoiceSession-compatible sink that never edits or deletes source PCM."""

    def __init__(self, artifact: RecordingArtifact, target_guild_id: int, job_id: str):
        self.artifact = artifact
        self.guild_id = target_guild_id
        self.channel_name = f"replay-{artifact.channel_name}"
        self.mode = "listen"
        self.session_dir = replay_root() / job_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
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
        temporary = self._metadata_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self._metadata_path)

    def checkpoint(self, *, offsets, speaker_labels, transcript_path=None, status="processing", **extra):
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
        )

    def delete_audio(self) -> int:
        return 0


class RecordingReplayManager:
    def __init__(self, bot):
        self.bot = bot
        self.jobs: dict[str, ReplayJob] = {}
        self._tasks: set[asyncio.Task] = set()

    def active(self) -> bool:
        return any(job.status in {"queued", "running"} for job in self.jobs.values())

    def start(
        self,
        *,
        session_id: str,
        target_channel,
        retranscribe: bool,
    ) -> ReplayJob:
        if self.active():
            raise RuntimeError("another recording replay is already running")
        artifact = find_recording_artifact(session_id)
        if retranscribe and artifact.raw_bytes <= 0:
            raise ValueError("raw audio is no longer available")
        if not retranscribe and artifact.transcript_path is None and artifact.raw_bytes <= 0:
            raise ValueError("neither transcript nor raw audio is available")
        now = datetime.now(timezone.utc).isoformat()
        job = ReplayJob(
            id=f"replay-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}",
            source_session_id=artifact.session_id,
            source_guild_id=artifact.source_guild_id,
            target_guild_id=target_channel.guild.id,
            target_channel_id=target_channel.id,
            retranscribe=retranscribe,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job.id] = job
        task = asyncio.create_task(self._run(job, artifact, target_channel), name=job.id)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def get(self, job_id: str) -> ReplayJob | None:
        return self.jobs.get(job_id)

    async def _run(self, job: ReplayJob, artifact: RecordingArtifact, target_channel) -> None:
        job.status = "running"
        job.stage = "preparing"
        job.updated_at = datetime.now(timezone.utc).isoformat()
        work_dir = replay_root() / job.id
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            await target_channel.send(
                f"🧪 長時間録音のテスト議事録生成を開始します。\n"
                f"ジョブ: `{job.id}` / 元セッション: `{artifact.session_id}`\n"
                f"方式: `{'PCMから再文字起こし' if job.retranscribe else '保存済み文字起こしを再利用'}`",
                silent=True,
            )
            transcript = ""
            if artifact.transcript_path and not job.retranscribe:
                job.stage = "loading_transcript"
                transcript = await asyncio.to_thread(load_transcript_content, artifact.transcript_path)
                archived = await asyncio.to_thread(
                    save_external_transcript,
                    job.target_guild_id,
                    job.id,
                    f"テスト再生 - {artifact.channel_name}",
                    transcript,
                    mode="recording-replay",
                )
                job.transcript_path = str(archived or "")
            else:
                job.stage = "transcribing"
                from bot.voice import VoiceMode, VoiceSession

                session = VoiceSession(
                    self.bot,
                    artifact.source_guild_id,
                    SimpleNamespace(name=f"テスト再生 - {artifact.channel_name}"),
                    VoiceMode.LISTEN,
                )
                session.recording_sink = ReadOnlyReplaySink(
                    artifact, job.target_guild_id, job.id
                )
                await session._drain_recording()
                await session._persist_transcript()
                if session._transcription_failed or not session.transcript_text.strip():
                    raise RuntimeError("replay transcription did not complete")
                transcript = session.transcript_text
                job.transcript_path = str(session.transcript_path or "")

            if not transcript.strip():
                raise RuntimeError("recording transcript is empty")
            (work_dir / "transcript.md").write_text(transcript, encoding="utf-8")

            job.stage = "generating_minutes"
            minutes = await generate_minutes(
                new_genai_client(os.environ.get("GOOGLE_API_KEY", "")),
                transcript,
                title=f"テスト再生 - {artifact.channel_name}",
                glossary_text=self.bot.config.get_glossary_text(artifact.source_guild_id),
                speaker_roster=self.bot._build_members_info(artifact.source_guild_id),
                checkpoint_dir=work_dir / "minutes-checkpoints",
            )
            if not minutes.strip():
                raise RuntimeError("minutes generation returned empty content")
            minutes_path = work_dir / "minutes.md"
            minutes_path.write_text(minutes, encoding="utf-8")
            job.minutes_path = str(minutes_path)

            job.stage = "publishing"
            from bot.commands.voice_cmd import _summarize_minutes
            from bot.gdrive import upload_minutes, upload_transcript

            title = f"[テスト] {artifact.channel_name} {artifact.session_id}"
            transcript_url, minutes_url, summary = await asyncio.gather(
                upload_transcript(self.bot.config, job.target_guild_id, transcript, title),
                upload_minutes(self.bot.config, job.target_guild_id, minutes, title),
                _summarize_minutes(minutes),
            )
            job.drive_transcript_url = transcript_url or ""
            job.drive_minutes_url = minutes_url or ""

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
            job.stage = "completed"
            self.bot.operations.record_event(
                "recording_replay", "minutes", "success",
                guild_id=job.target_guild_id, item_key=job.id,
                details={
                    "source_guild_id": artifact.source_guild_id,
                    "source_session_id": artifact.session_id,
                    "retranscribe": job.retranscribe,
                },
            )
        except Exception as exc:
            failed_stage = job.stage
            job.status = "failed"
            job.stage = "failed"
            job.error_type = type(exc).__name__
            self.bot.operations.record_event(
                "recording_replay", "minutes", "failed",
                guild_id=job.target_guild_id, item_key=job.id,
                details={"error_type": type(exc).__name__},
            )
            log.exception("Recording replay failed job=%s", job.id)
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
            metadata = work_dir / "job.json"
            temporary = metadata.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(job.storage_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, metadata)
