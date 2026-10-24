"""Backfill archived voice sessions into Drive and the meeting search index."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from bot.config import ConfigManager
from bot.corpus import CorpusManager
from bot.gdrive import upload_minutes, upload_transcript
from bot.meeting_index import index_meeting_artifacts
from bot.meeting_time import meeting_datetime
from bot.recording import transcript_root
from bot.recording_publication import (
    UnsafeRecordingPublication,
    load_safe_finalized_recording,
    require_complete_transcript_provenance,
)
from bot.recording_replay import find_recording_artifact, replay_root


@dataclass(frozen=True)
class _BackfillTranscript:
    content: str
    minutes: str
    transcript_url: str
    minutes_url: str


def _latest_completed_replay(session_id: str) -> dict:
    candidates: list[tuple[str, dict]] = []
    root = replay_root()
    for path in root.glob("*/job.json") if root.exists() else []:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            value.get("source_session_id") == session_id
            and value.get("status") == "completed"
            and value.get("stage") == "completed"
            and value.get("id") == path.parent.name
        ):
            value["_job_dir"] = str(path.parent)
            candidates.append((str(value.get("updated_at") or ""), value))
    return max(candidates, default=("", {}), key=lambda item: item[0])[1]


def _read_optional(path_value: str | None) -> str:
    if not path_value:
        return ""
    path = Path(path_value)
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def _path_is_exact_file(path_value: str | None, expected: Path) -> Path | None:
    if not path_value:
        return None
    try:
        resolved = Path(path_value).resolve()
        if resolved == expected.resolve() and resolved.is_file() and resolved.stat().st_size > 0:
            return resolved
    except OSError:
        return None
    return None


def _safe_backfill_transcript(
    *,
    artifact,
    replay: dict,
    target_guild_id: int,
) -> _BackfillTranscript:
    """Select only a transcript whose identity and completeness are proven."""
    replay_id = str(replay.get("id") or "")
    if replay_id:
        try:
            replay_source_guild = int(replay.get("source_guild_id"))
            replay_target_guild = int(replay.get("target_guild_id"))
        except (TypeError, ValueError):
            replay_source_guild = replay_target_guild = 0
        expected_transcript = (
            transcript_root() / str(target_guild_id) / f"{replay_id}.md"
        )
        replay_transcript = _path_is_exact_file(
            replay.get("transcript_path"),
            expected_transcript,
        )
        expected_job_dir = replay_root() / replay_id
        replay_minutes = _path_is_exact_file(
            replay.get("minutes_path"),
            expected_job_dir / "minutes.md",
        )
        if (
            replay_source_guild == artifact.source_guild_id
            and replay_target_guild == target_guild_id
            and Path(str(replay.get("_job_dir") or "")).resolve()
            == expected_job_dir.resolve()
            and replay_transcript is not None
        ):
            try:
                require_complete_transcript_provenance(
                    replay_transcript,
                    expected_guild_id=target_guild_id,
                    expected_session_id=replay_id,
                )
            except UnsafeRecordingPublication:
                pass
            else:
                return _BackfillTranscript(
                    content=_read_optional(str(replay_transcript)),
                    minutes=_read_optional(str(replay_minutes)) if replay_minutes else "",
                    transcript_url=str(replay.get("drive_transcript_url") or ""),
                    minutes_url=str(replay.get("drive_minutes_url") or ""),
                )

    if artifact.transcript_path is None:
        raise UnsafeRecordingPublication("no transcript with complete provenance")
    require_complete_transcript_provenance(
        artifact.transcript_path,
        expected_guild_id=artifact.source_guild_id,
        expected_session_id=artifact.session_id,
    )
    return _BackfillTranscript(
        content=_read_optional(str(artifact.transcript_path)),
        minutes="",
        transcript_url="",
        minutes_url="",
    )


async def backfill_session(guild_id: int, session_id: str, *, upload_missing: bool = True) -> dict:
    artifact = find_recording_artifact(session_id)
    if artifact.source_guild_id != int(guild_id):
        raise UnsafeRecordingPublication("source guild does not match backfill guild")
    config = ConfigManager()
    load_safe_finalized_recording(
        artifact.session_dir,
        source_guild_id=artifact.source_guild_id,
        config=config,
        require_raw_manifest=False,
    )
    replay = _latest_completed_replay(session_id)
    selected = _safe_backfill_transcript(
        artifact=artifact,
        replay=replay,
        target_guild_id=guild_id,
    )
    transcript = selected.content
    if not transcript.strip():
        raise UnsafeRecordingPublication(f"No durable transcript for {session_id}")
    minutes = selected.minutes

    store_name = config.get_corpus(guild_id)
    if not store_name:
        raise RuntimeError(f"No File Search store configured for guild {guild_id}")
    transcript_url = selected.transcript_url
    minutes_url = selected.minutes_url
    meeting_at = meeting_datetime(artifact.started_at or None)
    if upload_missing and not transcript_url:
        transcript_url = (
            await upload_transcript(
                config,
                guild_id,
                transcript,
                artifact.channel_name,
                meeting_at=meeting_at,
                session_id=session_id,
            )
            or ""
        )
    if upload_missing and minutes.strip() and not minutes_url:
        minutes_url = (
            await upload_minutes(
                config,
                guild_id,
                minutes,
                artifact.channel_name,
                meeting_at=meeting_at,
                session_id=session_id,
            )
            or ""
        )

    corpus = CorpusManager()
    result = await index_meeting_artifacts(
        corpus,
        store_name,
        guild_id=guild_id,
        channel_id=(
            artifact.source_channel_id
            if artifact.source_guild_id == guild_id
            else None
        ),
        session_id=session_id,
        channel_name=artifact.channel_name,
        started_at=artifact.started_at or None,
        transcript=transcript,
        minutes=minutes or None,
        transcript_url=transcript_url,
        minutes_url=minutes_url,
    )
    return {
        "session_id": session_id,
        "transcript_indexed": bool(result.transcript_document),
        "minutes_indexed": bool(result.minutes_document),
        "transcript_url": transcript_url,
        "minutes_url": minutes_url,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--session-id", action="append", required=True)
    parser.add_argument("--no-drive-upload", action="store_true")
    args = parser.parse_args()
    for session_id in args.session_id:
        result = asyncio.run(
            backfill_session(
                args.guild_id,
                session_id,
                upload_missing=not args.no_drive_upload,
            )
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
