"""Periodic, resumable Discord history refresh for the RAG corpus.

The scheduler is opt-in and requires immutable guild/channel-ID allowlists.  It
reuses the same per-guild lock and per-channel cursors as the manual/API
backfill paths, so a scheduled cycle never races an operator run.  A durable
cycle checkpoint lets downstream curation wait for a completed refresh rather
than reading a half-updated corpus.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from bot.corpus import (
    backfill_channel_concurrency,
    calculate_resumable_incremental_after,
    run_backfill_channels,
)
from bot.knowledge_policy import protected_channel

log = logging.getLogger("yagapon.scheduled_backfill")

DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60
MIN_INTERVAL_SECONDS = 6 * 60 * 60
DEFAULT_INITIAL_DELAY_SECONDS = 5 * 60
DEFAULT_RETRY_SECONDS = 15 * 60


def _positive_int_env(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        log.warning("Invalid %s; using %s", name, default)
        return default
    return max(minimum, value)


def configured_guild_ids() -> tuple[int, ...]:
    """Return the explicit guild allowlist; an empty value disables all work."""
    result: list[int] = []
    for raw in os.environ.get("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "").split(","):
        value = raw.strip()
        if value.isdecimal() and int(value) > 0 and int(value) not in result:
            result.append(int(value))
    return tuple(result)


def configured_channel_ids() -> frozenset[int]:
    """Return the immutable channel/forum allowlist required by the scheduler."""
    values = {
        int(raw.strip())
        for raw in os.environ.get("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "").split(",")
        if raw.strip().isdecimal() and int(raw.strip()) > 0
    }
    return frozenset(values)


def scheduler_enabled() -> bool:
    return (
        os.environ.get("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "false").lower()
        == "true"
        and bool(configured_guild_ids())
        and bool(configured_channel_ids())
    )


def next_backfill_delay(interval: int, retry_interval: int, result: dict | None) -> int:
    """Keep the six-hour cadence after success and retry failed handoffs sooner."""
    if not result or result.get("status") != "completed":
        return min(interval, retry_interval)
    return interval


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _channel_parent(channel, parents_by_id: dict[int, object]):
    parent = getattr(channel, "parent", None)
    if parent is not None:
        return parent
    parent_id = getattr(channel, "parent_id", None)
    if parent_id is None:
        return None
    try:
        return parents_by_id.get(int(parent_id))
    except (TypeError, ValueError):
        return None


def _readable_channel(config, guild_id: int, channel, member, *, parent=None) -> bool:
    """Apply one fail-closed admission check to text channels and threads."""
    channel_id = getattr(channel, "id", None)
    parent_id = getattr(channel, "parent_id", None) or getattr(parent, "id", None)
    if channel_id is None or member is None:
        return False
    try:
        if config.is_ignored(guild_id, int(channel_id)):
            return False
        if parent_id is not None and config.is_ignored(guild_id, int(parent_id)):
            return False
    except (TypeError, ValueError):
        return False

    category = getattr(channel, "category", None) or getattr(parent, "category", None)
    if protected_channel(channel, parent=parent, category=category):
        return False

    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return False
    try:
        permissions = permissions_for(member)
        can_view = bool(
            getattr(
                permissions,
                "view_channel",
                getattr(permissions, "read_messages", False),
            )
        )
        can_read_history = bool(getattr(permissions, "read_message_history", False))
    except Exception:
        return False
    return can_view and can_read_history


async def _materialize_threads(value) -> list[object]:
    """Normalize Pycord coroutine/async-iterator thread APIs for testability."""
    if inspect.isawaitable(value):
        value = await value
    if hasattr(value, "__aiter__"):
        return [thread async for thread in value]
    return list(value or ())


def _private_thread(thread) -> bool:
    """Treat an unresolved thread privacy type as private (fail closed)."""
    is_private = getattr(thread, "is_private", None)
    if callable(is_private):
        try:
            return bool(is_private())
        except Exception:
            return True
    channel_type = getattr(thread, "type", None)
    type_name = str(getattr(channel_type, "name", channel_type) or "").casefold()
    if type_name:
        return "private" in type_name
    # Pycord Thread instances always expose is_private(). Test doubles or an
    # unknown future channel type must not inherit a forum parent's authority.
    return True


class ScheduledBackfillManager:
    """Own one background refresh loop for a connected Discord bot."""

    def __init__(self, bot, *, state_path: Path | None = None):
        self.bot = bot
        self.state_path = state_path or Path(
            os.environ.get(
                "YAGAPON_SCHEDULED_BACKFILL_STATE",
                "/data/scheduled-backfill-state.json",
            )
        )
        self._task: asyncio.Task | None = None
        # The scheduled loop and an administrator-triggered refresh share this
        # manager. Only one of them may own the durable checkpoint at a time.
        self._run_lock = asyncio.Lock()
        # Invalidate a previous process's successful checkpoint while the bot
        # is still being constructed. The HTTP server can become healthy
        # before Discord's on_ready event, so waiting until start() would let a
        # curation worker consume stale evidence during that startup window.
        if scheduler_enabled():
            self._write_waiting_checkpoint("bot-starting")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        if not scheduler_enabled() or self.running:
            return False
        self._write_waiting_checkpoint("initial-delay")
        self._task = asyncio.create_task(
            self._run_loop(),
            name="scheduled-discord-backfill",
        )
        return True

    def _write_waiting_checkpoint(self, reason: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        waiting = {
            "schema_version": 1,
            "started_at": now,
            "completed_at": None,
            "status": "waiting",
            "reason": reason,
            "configured_guild_ids": [str(value) for value in configured_guild_ids()],
            "configured_channel_ids": [str(value) for value in sorted(configured_channel_ids())],
            "discovered_channel_ids": [],
            "eligible_channel_ids": [],
            "container_channel_ids": [],
            "missing_channel_ids": [],
            "guilds": {},
        }
        try:
            _write_state(self.state_path, waiting)
        except Exception:
            # If replacement itself fails, remove the exact checkpoint rather
            # than leaving a previous successful cycle available during boot.
            try:
                self.state_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    async def stop(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run_loop(self) -> None:
        initial_delay = _positive_int_env(
            "YAGAPON_SCHEDULED_BACKFILL_INITIAL_DELAY_SECONDS",
            DEFAULT_INITIAL_DELAY_SECONDS,
        )
        interval = _positive_int_env(
            "YAGAPON_SCHEDULED_BACKFILL_INTERVAL_SECONDS",
            DEFAULT_INTERVAL_SECONDS,
            minimum=MIN_INTERVAL_SECONDS,
        )
        retry_interval = _positive_int_env(
            "YAGAPON_SCHEDULED_BACKFILL_RETRY_SECONDS",
            DEFAULT_RETRY_SECONDS,
            minimum=300,
        )
        if initial_delay:
            await asyncio.sleep(initial_delay)
        while True:
            result = None
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The loop must remain available for the next interval. Each
                # guild/channel failure is also isolated inside run_once.
                log.exception("Scheduled Discord backfill cycle failed")
            await asyncio.sleep(
                next_backfill_delay(interval, retry_interval, result)
            )

    async def run_once(self) -> dict:
        if self._run_lock.locked():
            now = datetime.now(timezone.utc).isoformat()
            return {
                "schema_version": 1,
                "started_at": now,
                "completed_at": now,
                "status": "deferred",
                "reason": "backfill-already-running",
                "configured_guild_ids": [str(value) for value in configured_guild_ids()],
                "configured_channel_ids": [str(value) for value in sorted(configured_channel_ids())],
                "discovered_channel_ids": [],
                "eligible_channel_ids": [],
                "missing_channel_ids": [],
                "guilds": {},
            }
        async with self._run_lock:
            return await self._run_once_locked()

    async def _run_once_locked(self) -> dict:
        started_at = datetime.now(timezone.utc).isoformat()
        guild_ids = configured_guild_ids()
        selected_channel_ids = configured_channel_ids()
        result = {
            "schema_version": 1,
            "started_at": started_at,
            "completed_at": None,
            "status": "running",
            "configured_guild_ids": [str(value) for value in guild_ids],
            "configured_channel_ids": [str(value) for value in sorted(selected_channel_ids)],
            "discovered_channel_ids": [],
            "eligible_channel_ids": [],
            "missing_channel_ids": [],
            "guilds": {},
        }
        # Persist the in-progress marker before touching Discord. A crash or
        # forced stop must never leave yesterday's successful checkpoint looking
        # current to the downstream curation worker.
        _write_state(self.state_path, result)
        try:
            if not guild_ids:
                raise RuntimeError("no-configured-guilds")
            for guild_id in guild_ids:
                result["guilds"][str(guild_id)] = await self._refresh_guild(guild_id)

            discovered = {
                value for item in result["guilds"].values() for value in item.get("discovered_channel_ids", [])
            }
            eligible = {value for item in result["guilds"].values() for value in item.get("eligible_channel_ids", [])}
            configured = {str(value) for value in selected_channel_ids}
            result["discovered_channel_ids"] = sorted(discovered, key=int)
            result["eligible_channel_ids"] = sorted(eligible, key=int)
            result["missing_channel_ids"] = sorted(configured - eligible, key=int)
            failed = any(item.get("status") != "completed" for item in result["guilds"].values())
            if result["missing_channel_ids"]:
                failed = True
            result["status"] = "failed" if failed else "completed"
        except asyncio.CancelledError:
            result["status"] = "interrupted"
            result["error_type"] = "CancelledError"
            result["completed_at"] = datetime.now(timezone.utc).isoformat()
            _write_state(self.state_path, result)
            raise
        except Exception as exc:
            result["status"] = "failed"
            result["error_type"] = type(exc).__name__
            result["completed_at"] = datetime.now(timezone.utc).isoformat()
            _write_state(self.state_path, result)
            raise
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_state(self.state_path, result)
        return result

    async def _refresh_guild(self, guild_id: int) -> dict:
        guild = self.bot.get_guild(guild_id)
        corpus_store = self.bot.config.get_corpus(guild_id)
        selected_ids = configured_channel_ids()
        base_summary = {
            "status": "failed",
            "reason": None,
            "configured_channel_ids": [],
            "discovered_channel_ids": [],
            "eligible_channel_ids": [],
            "container_channel_ids": [],
            "missing_channel_ids": [],
            "channels_total": 0,
            "channels_completed": 0,
            "completed_channel_ids": [],
            "messages_indexed": 0,
            "documents_uploaded": 0,
            "documents_replaced": 0,
            "failures": [],
        }
        if guild is None or not corpus_store:
            return base_summary | {
                "reason": "guild-or-corpus-unavailable",
                "missing_channel_ids": [str(value) for value in sorted(selected_ids)],
            }
        summary = dict(base_summary)
        summary_lock = asyncio.Lock()
        session = None

        async def process_channel(channel) -> None:
            try:
                after = calculate_resumable_incremental_after(
                    self.bot.config,
                    guild_id,
                    channel.id,
                    now=session.cutoff,
                )
                channel_result = await self.bot.corpus.backfill_channel(
                    channel,
                    corpus_store,
                    after=after,
                    before=session.cutoff,
                    replace_existing=True,
                    dry_run=False,
                    session=session,
                )
                if channel_result.latest_message_id and channel_result.latest_message_at:
                    await self.bot.config.set_backfill_cursor(
                        guild_id,
                        channel.id,
                        channel_result.latest_message_id,
                        channel_result.latest_message_at,
                    )
                async with summary_lock:
                    summary["messages_indexed"] += channel_result.messages_indexed
                    summary["documents_uploaded"] += channel_result.documents_uploaded
                    summary["documents_replaced"] += channel_result.documents_replaced
                    summary["completed_channel_ids"].append(str(channel.id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception(
                    "Scheduled backfill failed guild=%s channel=%s",
                    guild_id,
                    channel.id,
                )
                async with summary_lock:
                    summary["failures"].append(
                        {
                            "channel_id": str(channel.id),
                            "error_type": type(exc).__name__,
                        }
                    )
            finally:
                async with summary_lock:
                    summary["channels_completed"] += 1

        try:
            text_channels = list(getattr(guild, "text_channels", ()) or ())
            forum_channels = list(getattr(guild, "forum_channels", ()) or ())
            if not forum_channels:
                # Keep compatibility with test doubles and older Pycord guild
                # caches where ``forum_channels`` is not exposed separately.
                forum_channels = [
                    channel
                    for channel in (getattr(guild, "channels", ()) or ())
                    if callable(getattr(channel, "archived_threads", None))
                    and not callable(getattr(channel, "history", None))
                ]
            forums_by_id = {
                int(channel.id): channel for channel in forum_channels if getattr(channel, "id", None) is not None
            }
            discovered_ids = {
                int(channel.id)
                for channel in [*text_channels, *forum_channels]
                if getattr(channel, "id", None) is not None
            }
            readable_text = [
                channel
                for channel in text_channels
                if _readable_channel(
                    self.bot.config,
                    guild_id,
                    channel,
                    guild.me,
                )
            ]
            readable_forums = {
                channel_id: channel
                for channel_id, channel in forums_by_id.items()
                if _readable_channel(
                    self.bot.config,
                    guild_id,
                    channel,
                    guild.me,
                )
            }
            channels_by_id = {
                int(channel.id): channel
                for channel in readable_text
                if not selected_ids or int(channel.id) in selected_ids
            }
            # A forum has no message history of its own. Mark its immutable ID
            # eligible, then expand it into independently scoped thread targets.
            eligible_ids = set(channels_by_id)
            requested_forum_ids = selected_ids & set(readable_forums)
            eligible_ids.update(readable_forums if not selected_ids else requested_forum_ids)

            undiscovered_selected_ids = selected_ids - discovered_ids
            discover_threads = bool(readable_forums) and (
                not selected_ids or bool(requested_forum_ids) or bool(undiscovered_selected_ids)
            )
            active_threads: list[object] = []
            if discover_threads:
                active_threads_method = getattr(guild, "active_threads", None)
                if callable(active_threads_method):
                    try:
                        active_threads = await _materialize_threads(active_threads_method())
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        summary["failures"].append(
                            {
                                "channel_id": None,
                                "stage": "active-thread-discovery",
                                "error_type": type(exc).__name__,
                            }
                        )
                        log.exception(
                            "Scheduled backfill could not list active threads guild=%s",
                            guild_id,
                        )
                else:
                    summary["failures"].append(
                        {
                            "channel_id": None,
                            "stage": "active-thread-discovery",
                            "error_type": "UnsupportedOperation",
                        }
                    )

            def admit_thread(thread, parent) -> None:
                thread_id = getattr(thread, "id", None)
                if thread_id is None or parent is None:
                    return
                thread_id = int(thread_id)
                parent_id = int(getattr(parent, "id", 0) or 0)
                if _private_thread(thread) and thread_id not in selected_ids:
                    # A bot with Administrator can enumerate private threads,
                    # but a parent forum ID must never authorize their import.
                    return
                discovered_ids.add(thread_id)
                if parent_id not in readable_forums:
                    return
                if not _readable_channel(
                    self.bot.config,
                    guild_id,
                    thread,
                    guild.me,
                    parent=parent,
                ):
                    return
                if selected_ids and thread_id not in selected_ids and parent_id not in selected_ids:
                    return
                channels_by_id[thread_id] = thread
                eligible_ids.add(thread_id)

            for thread in active_threads:
                parent = _channel_parent(thread, forums_by_id)
                if parent is not None:
                    admit_thread(thread, parent)

            undiscovered_selected_ids = selected_ids - discovered_ids
            if not selected_ids:
                archive_forum_ids = set(readable_forums)
            else:
                archive_forum_ids = set(requested_forum_ids)
                # An explicitly allowlisted archived thread cannot be resolved
                # from Guild.active_threads(). Search readable forum parents,
                # while still admitting only the exact requested thread ID.
                if undiscovered_selected_ids:
                    archive_forum_ids.update(readable_forums)

            for forum_id in sorted(archive_forum_ids):
                forum = readable_forums[forum_id]
                for private, joined, stage in (
                    (False, False, "archived-public-thread-discovery"),
                    (True, True, "archived-private-thread-discovery"),
                ):
                    if private and not (selected_ids - discovered_ids):
                        # Enumerate joined private archives only to resolve an
                        # explicitly configured thread ID that remains unknown.
                        continue
                    try:
                        archived = await _materialize_threads(
                            forum.archived_threads(
                                private=private,
                                joined=joined,
                                limit=None,
                            )
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        summary["failures"].append(
                            {
                                "channel_id": str(forum_id),
                                "stage": stage,
                                "error_type": type(exc).__name__,
                            }
                        )
                        log.exception(
                            "Scheduled backfill could not list archived threads guild=%s forum=%s stage=%s",
                            guild_id,
                            forum_id,
                            stage,
                        )
                        continue
                    for thread in archived:
                        parent = _channel_parent(thread, forums_by_id)
                        if int(getattr(parent, "id", 0) or 0) != forum_id:
                            continue
                        admit_thread(thread, parent)

            channels = list(channels_by_id.values())
            local_configured = selected_ids & discovered_ids
            local_missing = local_configured - eligible_ids
            summary.update(
                {
                    "status": "running",
                    "configured_channel_ids": [str(value) for value in sorted(local_configured)],
                    "discovered_channel_ids": [str(value) for value in sorted(discovered_ids)],
                    "eligible_channel_ids": [str(value) for value in sorted(eligible_ids)],
                    "container_channel_ids": [
                        str(value) for value in sorted(requested_forum_ids)
                    ],
                    "missing_channel_ids": [str(value) for value in sorted(local_missing)],
                    "channels_total": len(channels),
                }
            )
            if local_missing:
                summary["status"] = "failed"
                summary["reason"] = "configured-channels-not-readable"
                return summary
            if not channels:
                if eligible_ids and not summary["failures"]:
                    # A readable but empty forum is a valid completed refresh.
                    summary["status"] = "completed"
                    return summary
                summary["status"] = "failed"
                summary["reason"] = "thread-discovery-failed" if summary["failures"] else "no-eligible-channels"
                return summary
            session = await self.bot.corpus.prepare_backfill(
                guild_id,
                (channel.id for channel in channels),
                corpus_store,
            )
            if session is None:
                summary["status"] = "deferred"
                summary["reason"] = "backfill-already-running"
                return summary
            await run_backfill_channels(
                channels,
                process_channel,
                concurrency=min(
                    max(1, len(channels)),
                    backfill_channel_concurrency(),
                ),
            )
            summary["status"] = "failed" if summary["failures"] else "completed"
        except asyncio.CancelledError:
            summary["status"] = "interrupted"
            raise
        except Exception as exc:
            summary["status"] = "failed"
            summary["reason"] = "guild-refresh-error"
            summary["failures"].append({"channel_id": None, "error_type": type(exc).__name__})
            log.exception("Scheduled backfill failed while preparing guild=%s", guild_id)
        finally:
            if session is not None:
                await self.bot.corpus.finish_backfill(session)
            operations = getattr(self.bot, "operations", None)
            if operations is not None and hasattr(operations, "record_event"):
                try:
                    operations.record_event(
                        "backfill",
                        "scheduled_refresh",
                        summary["status"],
                        guild_id=guild_id,
                        details={key: value for key, value in summary.items() if key not in {"failures", "reason"}}
                        | {"failure_count": len(summary["failures"])},
                    )
                except Exception:
                    log.exception("Could not record scheduled backfill telemetry")
        return summary
