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
MEETING_KEY_VERSION = "meeting-v1"
MEETING_SCHEMA_VERSION = "meeting-v2"


@dataclass(frozen=True)
class MeetingIndexResult:
    transcript_document: str | None = None
    minutes_document: str | None = None

    @property
    def indexed(self) -> bool:
        return bool(self.transcript_document or self.minutes_document)


def meeting_document_key(guild_id: int, session_id: str, artifact_type: str) -> str:
    # Keep the v1 identity so metadata migrations replace the old remote
    # document instead of orphaning it under a new key.
    seed = f"{guild_id}:{session_id}:{artifact_type}:{MEETING_KEY_VERSION}"
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


async def _finish_pending_deletions(corpus, document_key: str | None = None) -> None:
    """Drain durable replacement cleanup intents globally or for one key."""
    pending = await asyncio.to_thread(
        corpus.rag_store.pending_document_deletions,
        document_key,
    )
    if not pending:
        return
    deleted = await corpus._delete_documents(pending)
    if deleted != len(pending):
        raise RuntimeError(
            f"Could not remove {len(pending) - deleted} superseded meeting documents"
        )
    await asyncio.to_thread(corpus.rag_store.resolve_document_deletions, pending)


async def _reconcile_remote_meeting_documents(
    corpus,
    store_name: str,
    guild_id: int,
) -> set[str] | None:
    """Remove crash-orphaned meeting duplicates and return the live remote names.

    A process can stop after File Search accepted an upload but before SQLite
    commits it.  Only remove a remote-only copy when SQLite still names another
    document for the same stable meeting key; remote records with no local
    counterpart are retained for operator recovery.
    """
    list_remote = getattr(corpus, "list_remote_documents", None)
    if not callable(list_remote):
        # Small test adapters and older integrations do not expose remote
        # listing. Production CorpusManager always does, and therefore never
        # takes a local-only idempotency shortcut.
        return None

    remote_documents = await list_remote(store_name)
    local_references = await asyncio.to_thread(
        corpus.rag_store.document_references,
        guild_id,
    )
    expected = {
        str(item.get("document_key") or ""): str(item.get("remote_name") or "")
        for item in local_references
        if str(item.get("source_type") or "").startswith("meeting_")
        and item.get("document_key")
        and item.get("remote_name")
    }
    expected_names = set(expected.values())
    orphan_names = []
    for item in remote_documents:
        metadata = item.get("metadata") or {}
        if str(metadata.get("guild_id") or "") != str(guild_id):
            continue
        if not str(metadata.get("source_type") or "").startswith("meeting_"):
            continue
        document_key = str(metadata.get("document_key") or "")
        expected_name = expected.get(document_key)
        if (
            expected_name
            and item.get("name") != expected_name
            and item.get("name") not in expected_names
        ):
            orphan_names.append(str(item["name"]))
    if orphan_names:
        deleted = await corpus._delete_documents(orphan_names)
        if deleted != len(orphan_names):
            raise RuntimeError("Could not reconcile crash-orphaned meeting documents")
    return {
        str(item["name"])
        for item in remote_documents
        if item.get("name") and str(item["name"]) not in set(orphan_names)
    }


def _meeting_body(
    *,
    content: str,
    session_id: str,
    channel_name: str,
    category_name: str,
    started_at: datetime | str | None,
    artifact_type: str,
    source_url: str,
) -> str:
    local = meeting_datetime(started_at)
    label = "生文字起こし" if artifact_type == "transcript" else "AI議事録（未確認）"
    fallback_source = (
        "サーバー内の保存済み文字起こし"
        if artifact_type == "transcript"
        else "サーバー内の自動生成議事録"
    )
    return (
        f"資料種別: {label}\n"
        f"会議名: {meeting_name(channel_name)}\n"
        f"カテゴリ: {category_name or 'なし'}\n"
        f"開催日時: {local.isoformat(timespec='seconds')}\n"
        f"開催日: {local:%Y-%m-%d} ({local.month}/{local.day}, {local.month}月{local.day}日)\n"
        f"矢上祭年度: {festival_number(local)}th\n"
        f"セッションID: {session_id}\n"
        f"原本: {source_url or fallback_source}\n\n"
        f"{content.strip()}\n"
    )


async def _replace_document(
    corpus,
    store_name: str,
    *,
    guild_id: int,
    channel_id: int | None,
    session_id: str,
    channel_name: str,
    category_name: str,
    started_at: datetime | str | None,
    artifact_type: str,
    content: str,
    source_url: str,
    remote_names: set[str] | None,
) -> str | None:
    if not content.strip():
        return None
    local = meeting_datetime(started_at)
    document_key = meeting_document_key(guild_id, session_id, artifact_type)
    body = _meeting_body(
        content=content,
        session_id=session_id,
        channel_name=channel_name,
        category_name=category_name,
        started_at=local,
        artifact_type=artifact_type,
        source_url=source_url,
    )
    existing = corpus.rag_store.get_document(document_key)
    expected_scope = "channel" if channel_id is not None else "restricted"
    existing_channel_id = existing.get("channel_id") if existing else None
    if (
        existing
        and existing.get("body") == body
        and existing.get("remote_name")
        and (
            remote_names is None
            or str(existing.get("remote_name")) in remote_names
        )
        and existing.get("schema") == MEETING_SCHEMA_VERSION
        and existing.get("access_scope") == expected_scope
        and (
            (channel_id is None and existing_channel_id is None)
            or (
                channel_id is not None
                and int(existing_channel_id or 0) == int(channel_id)
            )
        )
    ):
        await _finish_pending_deletions(corpus, document_key)
        return str(existing["remote_name"])

    source_type = f"meeting_{artifact_type}"
    status = "raw" if artifact_type == "transcript" else "draft"
    metadata = [
        {"key": "source", "string_value": "discord_voice"},
        {"key": "source_type", "string_value": source_type},
        {"key": "status", "string_value": status},
        {"key": "authority", "string_value": "conversation"},
        {"key": "access_scope", "string_value": expected_scope},
        {"key": "festival", "numeric_value": float(festival_number(local))},
        {"key": "schema", "string_value": MEETING_SCHEMA_VERSION},
        {"key": "guild_id", "string_value": str(guild_id)},
        {"key": "channel_name", "string_value": meeting_name(channel_name)[:500]},
        {"key": "category", "string_value": str(category_name or "")[:500]},
        {"key": "session_id", "string_value": session_id[:500]},
        {"key": "start_at", "string_value": local.isoformat()},
        {"key": "end_at", "string_value": local.isoformat()},
        {"key": "start_epoch", "numeric_value": local.timestamp()},
        {"key": "end_epoch", "numeric_value": local.timestamp()},
        {"key": "document_key", "string_value": document_key},
        {"key": "source_url", "string_value": source_url[:500]},
        {"key": "content_sha256", "string_value": hashlib.sha256(content.encode()).hexdigest()},
    ]
    if channel_id is not None:
        metadata.append({"key": "channel_id", "string_value": str(channel_id)})
    display_name = (
        f"Meeting {artifact_type} | {local:%Y-%m-%d} | "
        f"{meeting_name(channel_name)} | {session_id}"
    )[:512]
    old_remote = str(existing.get("remote_name") or "") if existing else ""
    remote_name = await corpus._upload_document(
        store_name,
        display_name,
        body,
        metadata,
        superseded_remote_names=(old_remote,) if old_remote else (),
    )
    if not remote_name:
        return None
    if old_remote and old_remote != remote_name:
        pending = await asyncio.to_thread(
            corpus.rag_store.pending_document_deletions,
            document_key,
        )
        if old_remote not in pending:
            raise RuntimeError("Superseded meeting document was not durably queued")
        await _finish_pending_deletions(corpus, document_key)
    return remote_name


async def index_meeting_artifacts(
    corpus,
    store_name: str,
    *,
    guild_id: int,
    channel_id: int | None = None,
    session_id: str,
    channel_name: str,
    category_name: str = "",
    started_at: datetime | str | None,
    transcript: str,
    minutes: str | None = None,
    transcript_url: str | None = None,
    minutes_url: str | None = None,
) -> MeetingIndexResult:
    """Index raw transcript and draft minutes without granting either formal authority."""
    # Cleanup intents belong to the process, not one session. Drain all of them
    # before taking any idempotency shortcut, then compare SQLite with File
    # Search so a local-only row is never mistaken for a completed upload.
    await _finish_pending_deletions(corpus)
    remote_names = await _reconcile_remote_meeting_documents(
        corpus,
        store_name,
        guild_id,
    )
    transcript_task = _replace_document(
        corpus,
        store_name,
        guild_id=guild_id,
        channel_id=channel_id,
        session_id=session_id,
        channel_name=channel_name,
        category_name=category_name,
        started_at=started_at,
        artifact_type="transcript",
        content=transcript,
        source_url=transcript_url or "",
        remote_names=remote_names,
    )
    minutes_task = (
        _replace_document(
            corpus,
            store_name,
            guild_id=guild_id,
            channel_id=channel_id,
            session_id=session_id,
            channel_name=channel_name,
            category_name=category_name,
            started_at=started_at,
            artifact_type="minutes",
            content=minutes,
            source_url=minutes_url or "",
            remote_names=remote_names,
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
    parent_channel_id = kwargs.pop("parent_channel_id", None)
    category_id = kwargs.pop("category_id", None)

    # File Search is an external sink. The source Discord channel and every
    # declared parent/category edge must still resolve in the originating guild
    # before content may cross that boundary. IDs and cached names alone cannot
    # prove the current audience (or a protected parent/category), so unresolved
    # lineage fails closed rather than being represented by a synthetic object.
    guild = None
    get_guild = getattr(bot, "get_guild", None)
    if callable(get_guild):
        guild = get_guild(guild_id)
    if guild is None or int(getattr(guild, "id", 0) or 0) != guild_id:
        log.warning(
            "Meeting indexing skipped: source guild is unresolved guild=%s",
            guild_id,
        )
        return MeetingIndexResult()
    resolve_channel = None
    resolve_channel = getattr(guild, "get_channel_or_thread", None)
    if not callable(resolve_channel):
        resolve_channel = getattr(guild, "get_channel", None)
    if not callable(resolve_channel):
        log.warning(
            "Meeting indexing skipped: source channel resolver is unavailable guild=%s",
            guild_id,
        )
        return MeetingIndexResult()

    def resolve(channel_id):
        if channel_id is None or not callable(resolve_channel):
            return None
        try:
            return resolve_channel(int(channel_id))
        except (TypeError, ValueError):
            return None

    requested_channel_id = kwargs.get("channel_id")
    source_channel = resolve(requested_channel_id)
    if (
        source_channel is None
        or int(getattr(source_channel, "id", 0) or 0)
        != int(requested_channel_id or 0)
    ):
        log.warning(
            "Meeting indexing skipped: source channel is unresolved guild=%s channel=%s",
            guild_id,
            requested_channel_id,
        )
        return MeetingIndexResult()

    actual_parent_id = getattr(source_channel, "parent_id", None)
    if (
        parent_channel_id is not None
        and actual_parent_id is not None
        and int(parent_channel_id) != int(actual_parent_id)
    ):
        log.warning(
            "Meeting indexing skipped: source parent lineage changed guild=%s channel=%s",
            guild_id,
            requested_channel_id,
        )
        return MeetingIndexResult()
    expected_parent_id = actual_parent_id or parent_channel_id
    source_parent = getattr(source_channel, "parent", None) or resolve(
        expected_parent_id
    )
    if expected_parent_id is not None and (
        source_parent is None
        or int(getattr(source_parent, "id", 0) or 0) != int(expected_parent_id)
    ):
        log.warning(
            "Meeting indexing skipped: source parent is unresolved guild=%s channel=%s parent=%s",
            guild_id,
            requested_channel_id,
            expected_parent_id,
        )
        return MeetingIndexResult()

    actual_category_id = (
        getattr(source_channel, "category_id", None)
        or getattr(source_parent, "category_id", None)
    )
    if (
        category_id is not None
        and actual_category_id is not None
        and int(category_id) != int(actual_category_id)
    ):
        log.warning(
            "Meeting indexing skipped: source category lineage changed guild=%s channel=%s",
            guild_id,
            requested_channel_id,
        )
        return MeetingIndexResult()
    expected_category_id = actual_category_id or category_id
    source_category = (
        getattr(source_channel, "category", None)
    ) or getattr(source_parent, "category", None) or resolve(expected_category_id)
    if expected_category_id is not None and (
        source_category is None
        or int(getattr(source_category, "id", 0) or 0)
        != int(expected_category_id)
    ):
        log.warning(
            "Meeting indexing skipped: source category is unresolved guild=%s channel=%s category=%s",
            guild_id,
            requested_channel_id,
            expected_category_id,
        )
        return MeetingIndexResult()
    if source_category is None and str(kwargs.get("category_name") or "").strip():
        log.warning(
            "Meeting indexing skipped: named source category is unresolved guild=%s channel=%s",
            guild_id,
            requested_channel_id,
        )
        return MeetingIndexResult()
    if source_category is not None:
        kwargs["category_name"] = str(
            getattr(source_category, "name", "") or kwargs.get("category_name") or ""
        )

    from bot.knowledge_policy import protected_channel

    if protected_channel(
        source_channel,
        parent=source_parent,
        category=source_category,
    ):
        log.warning(
            "Meeting indexing skipped for protected source: guild=%s channel=%s",
            guild_id,
            kwargs.get("channel_id"),
        )
        return MeetingIndexResult()

    is_ignored = getattr(config, "is_ignored", None)
    if callable(is_ignored):
        channel_ids = {
            value
            for value in (kwargs.get("channel_id"), parent_channel_id)
            if value is not None
        }
        if any(is_ignored(guild_id, int(value)) for value in channel_ids):
            log.info(
                "Meeting indexing skipped by /ignore: guild=%s channel=%s",
                guild_id,
                kwargs.get("channel_id"),
            )
            return MeetingIndexResult()
    store_name = config.get_corpus(guild_id) if config and hasattr(config, "get_corpus") else None
    if corpus is None or not store_name:
        log.warning("Meeting indexing skipped: guild=%s has no corpus", guild_id)
        return MeetingIndexResult()
    try:
        result = await index_meeting_artifacts(corpus, store_name, **kwargs)
        if str(kwargs.get("transcript") or "").strip() and not result.transcript_document:
            raise RuntimeError("Meeting transcript was not indexed")
        if str(kwargs.get("minutes") or "").strip() and not result.minutes_document:
            raise RuntimeError("Meeting minutes were not indexed")
        return result
    except Exception:
        log.exception("Meeting indexing failed: guild=%s session=%s", guild_id, kwargs.get("session_id"))
        raise
