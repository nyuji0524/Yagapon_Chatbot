"""Immediate, low-authority indexing for generated meeting artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime

from bot.festival import festival_number
from bot.meeting_time import meeting_datetime, meeting_name

log = logging.getLogger("yagapon.meeting_index")
MEETING_SCHEMA_VERSION = "meeting-v1"


@dataclass(frozen=True)
class MeetingIndexResult:
    transcript_document: str | None = None
    minutes_document: str | None = None

    @property
    def indexed(self) -> bool:
        return bool(self.transcript_document or self.minutes_document)


def meeting_document_key(guild_id: int, session_id: str, artifact_type: str) -> str:
    seed = f"{guild_id}:{session_id}:{artifact_type}:{MEETING_SCHEMA_VERSION}"
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


def _meeting_body(
    *,
    content: str,
    session_id: str,
    channel_name: str,
    started_at: datetime | str | None,
    artifact_type: str,
    source_url: str,
) -> str:
    local = meeting_datetime(started_at)
    label = "生文字起こし" if artifact_type == "transcript" else "AI議事録（未確認）"
    return (
        f"資料種別: {label}\n"
        f"会議名: {meeting_name(channel_name)}\n"
        f"開催日時: {local.isoformat(timespec='seconds')}\n"
        f"開催日: {local:%Y-%m-%d} ({local.month}/{local.day}, {local.month}月{local.day}日)\n"
        f"矢上祭年度: {festival_number(local)}th\n"
        f"セッションID: {session_id}\n"
        f"原本: {source_url or 'サーバー内の保存済み文字起こし'}\n\n"
        f"{content.strip()}\n"
    )


async def _replace_document(
    corpus,
    store_name: str,
    *,
    guild_id: int,
    session_id: str,
    channel_name: str,
    started_at: datetime | str | None,
    artifact_type: str,
    content: str,
    source_url: str,
) -> str | None:
    if not content.strip():
        return None
    local = meeting_datetime(started_at)
    document_key = meeting_document_key(guild_id, session_id, artifact_type)
    body = _meeting_body(
        content=content,
        session_id=session_id,
        channel_name=channel_name,
        started_at=local,
        artifact_type=artifact_type,
        source_url=source_url,
    )
    existing = corpus.rag_store.get_document(document_key)
    if existing and existing.get("body") == body and existing.get("remote_name"):
        return str(existing["remote_name"])

    source_type = f"meeting_{artifact_type}"
    status = "raw" if artifact_type == "transcript" else "draft"
    metadata = [
        {"key": "source", "string_value": "discord_voice"},
        {"key": "source_type", "string_value": source_type},
        {"key": "status", "string_value": status},
        {"key": "authority", "string_value": "conversation"},
        {"key": "festival", "numeric_value": float(festival_number(local))},
        {"key": "schema", "string_value": MEETING_SCHEMA_VERSION},
        {"key": "guild_id", "string_value": str(guild_id)},
        {"key": "channel_name", "string_value": meeting_name(channel_name)[:500]},
        {"key": "session_id", "string_value": session_id[:500]},
        {"key": "start_at", "string_value": local.isoformat()},
        {"key": "end_at", "string_value": local.isoformat()},
        {"key": "start_epoch", "numeric_value": local.timestamp()},
        {"key": "document_key", "string_value": document_key},
        {"key": "source_url", "string_value": source_url[:500]},
        {"key": "content_sha256", "string_value": hashlib.sha256(content.encode()).hexdigest()},
    ]
    display_name = (
        f"Meeting {artifact_type} | {local:%Y-%m-%d} | "
        f"{meeting_name(channel_name)} | {session_id}"
    )[:512]
    remote_name = await corpus._upload_document(store_name, display_name, body, metadata)
    if not remote_name:
        return None
    old_remote = str(existing.get("remote_name") or "") if existing else ""
    if old_remote and old_remote != remote_name:
        await corpus._delete_documents([old_remote])
    return remote_name


async def index_meeting_artifacts(
    corpus,
    store_name: str,
    *,
    guild_id: int,
    session_id: str,
    channel_name: str,
    started_at: datetime | str | None,
    transcript: str,
    minutes: str | None = None,
    transcript_url: str | None = None,
    minutes_url: str | None = None,
) -> MeetingIndexResult:
    """Index raw transcript and draft minutes without granting either formal authority."""
    transcript_task = _replace_document(
        corpus,
        store_name,
        guild_id=guild_id,
        session_id=session_id,
        channel_name=channel_name,
        started_at=started_at,
        artifact_type="transcript",
        content=transcript,
        source_url=transcript_url or "",
    )
    minutes_task = (
        _replace_document(
            corpus,
            store_name,
            guild_id=guild_id,
            session_id=session_id,
            channel_name=channel_name,
            started_at=started_at,
            artifact_type="minutes",
            content=minutes,
            source_url=minutes_url or "",
        )
        if minutes and minutes.strip()
        else None
    )
    if minutes_task:
        transcript_document, minutes_document = await asyncio.gather(
            transcript_task, minutes_task
        )
    else:
        transcript_document = await transcript_task
        minutes_document = None
    return MeetingIndexResult(transcript_document, minutes_document)


async def index_bot_meeting(bot, **kwargs) -> MeetingIndexResult:
    """Best-effort adapter used by Discord commands and replay jobs."""
    guild_id = int(kwargs["guild_id"])
    corpus = getattr(bot, "corpus", None)
    config = getattr(bot, "config", None)
    store_name = config.get_corpus(guild_id) if config and hasattr(config, "get_corpus") else None
    if corpus is None or not store_name:
        log.warning("Meeting indexing skipped: guild=%s has no corpus", guild_id)
        return MeetingIndexResult()
    try:
        return await index_meeting_artifacts(corpus, store_name, **kwargs)
    except Exception:
        log.exception("Meeting indexing failed: guild=%s session=%s", guild_id, kwargs.get("session_id"))
        return MeetingIndexResult()
