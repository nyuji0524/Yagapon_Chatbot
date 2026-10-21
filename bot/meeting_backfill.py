"""Backfill archived voice sessions into Drive and the meeting search index."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from bot.config import ConfigManager
from bot.corpus import CorpusManager
from bot.gdrive import upload_minutes, upload_transcript
from bot.meeting_index import index_meeting_artifacts
from bot.recording_replay import find_recording_artifact, replay_root


def _latest_completed_replay(session_id: str) -> dict:
    candidates: list[tuple[str, dict]] = []
    root = replay_root()
    for path in root.glob("*/job.json") if root.exists() else []:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("source_session_id") == session_id and value.get("status") == "completed":
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


async def backfill_session(guild_id: int, session_id: str, *, upload_missing: bool = True) -> dict:
    artifact = find_recording_artifact(session_id)
    replay = _latest_completed_replay(session_id)
    transcript = _read_optional(replay.get("transcript_path"))
    if not transcript and artifact.transcript_path:
        transcript = _read_optional(str(artifact.transcript_path))
    if not transcript.strip():
        raise RuntimeError(f"No durable transcript for {session_id}")
    minutes = _read_optional(replay.get("minutes_path"))

    config = ConfigManager()
    store_name = config.get_corpus(guild_id)
    if not store_name:
        raise RuntimeError(f"No File Search store configured for guild {guild_id}")
    transcript_url = str(replay.get("drive_transcript_url") or "")
    minutes_url = str(replay.get("drive_minutes_url") or "")
    if upload_missing and not transcript_url:
        transcript_url = (
            await upload_transcript(
                config,
                guild_id,
                transcript,
                artifact.channel_name,
                meeting_at=artifact.started_at or None,
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
                meeting_at=artifact.started_at or None,
            )
            or ""
        )

    corpus = CorpusManager()
    result = await index_meeting_artifacts(
        corpus,
        store_name,
        guild_id=guild_id,
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
