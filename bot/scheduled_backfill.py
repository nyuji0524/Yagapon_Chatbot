"""Periodic, resumable Discord history refresh for the RAG corpus.

The scheduler is opt-in and requires an immutable guild-ID allowlist.  Inside
an allowed guild it discovers every readable text channel and public thread,
then applies the shared ``/ignore`` and protected-channel policy.  An optional
channel list remains as a backwards-compatible narrowing override, but is no
longer required.  The same per-guild lock, per-channel cursors, and durable
checkpoint are shared by scheduled and administrator-triggered refreshes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

from bot.corpus import (
    backfill_channel_concurrency,
    calculate_resumable_incremental_after,
    run_backfill_channels,
)
from bot.festival import JST, festival_number, festival_period
from bot.knowledge_pipeline import KnowledgePipelineBusy, knowledge_pipeline_lock
from bot.knowledge_policy import (
    automatic_knowledge_promotion_allowed,
    protected_channel,
)

log = logging.getLogger("yagapon.scheduled_backfill")

DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60
MIN_INTERVAL_SECONDS = 6 * 60 * 60
DEFAULT_INITIAL_DELAY_SECONDS = 5 * 60
DEFAULT_RETRY_SECONDS = 15 * 60
CHECKPOINT_WRITER_RETRY_SECONDS = 0.1
CHECKPOINT_SCHEMA_VERSION = 2
DEFAULT_STATE_PATH = Path("/data/scheduled-backfill-state.json")


class ScheduledBackfillConfigurationError(ValueError):
    """Raised when an immutable scheduler allowlist is malformed."""


def _positive_int_env(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        log.warning("Invalid %s; using %s", name, default)
        return default
    return max(minimum, value)


def _configured_ids(name: str) -> tuple[int, ...]:
    """Parse one immutable Discord-ID allowlist without accepting a subset."""
    result: list[int] = []
    invalid = False
    for raw in os.environ.get(name, "").split(","):
        value = raw.strip()
        if not value:
            continue
        if not value.isdecimal() or int(value) <= 0:
            invalid = True
            continue
        parsed = int(value)
        if parsed not in result:
            result.append(parsed)
    if invalid:
        raise ScheduledBackfillConfigurationError(
            f"{name} must contain only positive decimal Discord IDs"
        )
    return tuple(result)


def configured_guild_ids() -> tuple[int, ...]:
    """Return the exact explicit guild allowlist; malformed input is rejected."""
    return _configured_ids("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS")


def configured_channel_ids() -> frozenset[int]:
    """Return the exact immutable channel/forum/thread allowlist."""
    return frozenset(_configured_ids("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS"))


def scheduler_requested() -> bool:
    """Return whether the operator explicitly requested the scheduler."""
    return (
        os.environ.get("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "false")
        .strip()
        .casefold()
        == "true"
    )


def configured_inventory() -> tuple[tuple[int, ...], frozenset[int]]:
    """Return the two validated immutable allowlists."""
    return configured_guild_ids(), configured_channel_ids()


def scheduled_backfill_state_path() -> Path:
    """Return the producer checkpoint path used by the live scheduler."""
    configured = os.environ.get("YAGAPON_SCHEDULED_BACKFILL_STATE", "").strip()
    return Path(configured) if configured else DEFAULT_STATE_PATH


def scheduler_enabled() -> bool:
    if not scheduler_requested():
        return False
    try:
        guild_ids, channel_ids = configured_inventory()
    except ScheduledBackfillConfigurationError as exc:
        log.error("Scheduled Discord backfill configuration is invalid: %s", exc)
        return False
    return bool(guild_ids)


def _festival_bounds(number: int, cutoff: datetime) -> tuple[datetime, datetime]:
    """Return one JST festival window as an exclusive UTC history range."""
    period = festival_period(number)
    starts_at = datetime.combine(period.starts_on, time.min, JST)
    ends_at = datetime.combine(period.ends_on + timedelta(days=1), time.min, JST)
    # Discord.py/Pycord treats both datetime boundaries as exclusive. Moving
    # ``after`` back by one microsecond includes exactly 10/1 00:00 JST while
    # ``before`` remains the exclusive beginning of the next festival year.
    return (
        (starts_at - timedelta(microseconds=1)).astimezone(timezone.utc),
        min(ends_at.astimezone(timezone.utc), cutoff),
    )


async def _oldest_message_at(channel, *, before: datetime) -> datetime | None:
    """Read only the oldest message needed to bound an all-history rebuild."""
    history = getattr(channel, "history", None)
    if not callable(history):
        raise RuntimeError("channel-history-unavailable")
    async for message in history(limit=1, before=before, oldest_first=True):
        created_at = getattr(message, "created_at", None)
        if not isinstance(created_at, datetime):
            raise RuntimeError("message-created-at-unavailable")
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at.astimezone(timezone.utc)
    return None


def _historical_festivals(oldest: datetime | None, cutoff: datetime) -> tuple[int, ...]:
    """Return every festival year that can contain a channel message."""
    latest = festival_number(cutoff)
    earliest = festival_number(oldest) if oldest is not None else latest
    if earliest > latest:
        earliest = latest
    return tuple(range(max(1, earliest), latest + 1))


def next_backfill_delay(interval: int, retry_interval: int, result: dict | None) -> int:
    """Keep the six-hour cadence after success and retry failed handoffs sooner."""
    if not result or result.get("status") != "completed":
        return min(interval, retry_interval)
    return interval


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Multiple bot processes can briefly coexist during a restart.  A fixed
    # ``.tmp`` name lets their atomic writers corrupt or delete one another's
    # staging file even when the final publication is otherwise serialized.
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def _cached_guild_channel(guild, channel_id: int):
    """Resolve a currently cached message target, or fail closed if deleted.

    Captured channel objects can outlive a Discord delete event. The final ACL
    snapshot therefore requires the guild's current cache to resolve the ID.
    Archived public threads and explicitly selected private threads are handled
    separately by re-listing their live parent's archive rather than trusting
    this cache lookup.
    """
    if guild is None:
        return None
    for method_name in ("get_channel_or_thread", "get_channel", "get_thread"):
        method = getattr(guild, method_name, None)
        if not callable(method):
            continue
        try:
            resolved = method(channel_id)
        except Exception:
            return None
        if resolved is not None:
            return resolved
    return None


async def _resolve_current_message_target(
    guild,
    channel,
    captured_parent,
    archived_threads_cache: dict[tuple[int, int, bool], dict[int, object]],
):
    """Resolve a target from current guild state, including archived threads.

    Pycord does not generally retain ``ThreadIterator`` results in the guild
    cache. Re-list the matching public/private archive from the currently
    cached parent so those targets can still be authorized, while a deleted
    thread or parent fails closed instead of falling back to a stale captured
    object.
    """
    channel_id = int(channel.id)
    current_channel = _cached_guild_channel(guild, channel_id)
    parent_id = getattr(current_channel or channel, "parent_id", None)
    if parent_id is None:
        parent_id = getattr(captured_parent, "id", None)

    if current_channel is not None:
        if parent_id is None:
            return current_channel, None
        current_parent = _cached_guild_channel(guild, int(parent_id))
        if current_parent is None:
            return None, None
        return current_channel, current_parent

    if captured_parent is None:
        return None, None
    try:
        parent_id = int(parent_id)
    except (TypeError, ValueError):
        return None, None
    current_parent = _cached_guild_channel(guild, parent_id)
    archived_threads = getattr(current_parent, "archived_threads", None)
    if current_parent is None or not callable(archived_threads):
        return None, None

    private = _private_thread(channel)
    cache_key = (int(guild.id), parent_id, private)
    if cache_key not in archived_threads_cache:
        try:
            current_archives = await _materialize_threads(
                archived_threads(
                    private=private,
                    joined=private,
                    limit=None,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Scheduled backfill could not revalidate archived threads "
                "guild=%s parent=%s",
                guild.id,
                parent_id,
            )
            current_archives = []
        archived_threads_cache[cache_key] = {
            int(thread.id): thread
            for thread in current_archives
            if getattr(thread, "id", None) is not None
            and _private_thread(thread) is private
        }
    current_channel = archived_threads_cache[cache_key].get(channel_id)
    if current_channel is None:
        return None, None
    return current_channel, current_parent


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
        self.state_path = state_path or scheduled_backfill_state_path()
        self._task: asyncio.Task | None = None
        # The scheduled loop and an administrator-triggered refresh share this
        # manager. Only one of them may own the durable checkpoint at a time.
        self._run_lock = asyncio.Lock()
        # Ephemeral Discord objects used to take one final ACL snapshot after
        # every configured guild finishes. They are never serialized.
        self._completed_message_targets: dict[
            int,
            list[tuple[object, object | None]],
        ] = {}
        # Invalidate a previous process's successful checkpoint while the bot
        # is still being constructed. The HTTP server can become healthy
        # before Discord's on_ready event, so waiting until start() would let a
        # curation worker consume stale evidence during that startup window.
        if scheduler_requested():
            try:
                guild_ids, channel_ids = configured_inventory()
            except ScheduledBackfillConfigurationError:
                self._write_configuration_error_checkpoint("invalid-allowlist")
            else:
                if not guild_ids:
                    self._write_configuration_error_checkpoint("missing-allowlist")
                else:
                    self._write_waiting_checkpoint(
                        "bot-starting",
                        guild_ids=guild_ids,
                        channel_ids=channel_ids,
                    )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        if not scheduler_enabled() or self.running:
            return False
        waiting_checkpoint_ready = self._write_waiting_checkpoint("initial-delay")
        self._task = asyncio.create_task(
            self._run_loop(waiting_checkpoint_ready=waiting_checkpoint_ready),
            name="scheduled-discord-backfill",
        )
        return True

    def _write_checkpoint(
        self,
        *,
        status: str,
        reason: str,
        guild_ids: Iterable[int] = (),
        channel_ids: Iterable[int] = (),
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "started_at": now,
            "completed_at": None,
            "status": status,
            "reason": reason,
            "configured_guild_ids": [str(value) for value in guild_ids],
            "configured_channel_ids": [str(value) for value in sorted(channel_ids)],
            "discovered_channel_ids": [],
            "eligible_channel_ids": [],
            "current_curation_eligible_channel_ids": [],
            "current_public_curation_channel_ids": [],
            "container_channel_ids": [],
            "missing_channel_ids": [],
            "guilds": {},
        }
        try:
            # Startup/waiting/configuration-error markers are checkpoint
            # writers too.  They must share the same publication lock as
            # running and terminal states; otherwise a newly constructed bot
            # can replace ``running`` after an older cycle's ownership check
            # and that older cycle can then publish ``completed`` over it.
            with knowledge_pipeline_lock():
                try:
                    _write_state(self.state_path, payload)
                except Exception:
                    # If replacement itself fails, remove the exact checkpoint
                    # while still owning the writer lock rather than leaving a
                    # previous successful cycle available during boot.
                    try:
                        self.state_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
        except KnowledgePipelineBusy:
            log.debug(
                "Scheduled backfill checkpoint writer deferred status=%s reason=%s",
                status,
                reason,
            )
            return False
        return True

    def _write_waiting_checkpoint(
        self,
        reason: str,
        *,
        guild_ids: Iterable[int] | None = None,
        channel_ids: Iterable[int] | None = None,
    ) -> bool:
        if guild_ids is None or channel_ids is None:
            configured_guilds, configured_channels = configured_inventory()
            guild_ids = configured_guilds
            channel_ids = configured_channels
        return self._write_checkpoint(
            status="waiting",
            reason=reason,
            guild_ids=guild_ids,
            channel_ids=channel_ids,
        )

    def _write_configuration_error_checkpoint(self, reason: str) -> bool:
        return self._write_checkpoint(status="failed", reason=reason)

    def _owns_running_checkpoint(self, cycle_id: str) -> bool:
        """Return whether this cycle still owns the durable running marker."""
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("status") == "running"
            and payload.get("cycle_id") == cycle_id
        )

    def _write_terminal_checkpoint_if_owned(self, result: dict) -> bool:
        """CAS one failed/interrupted terminal state under the pipeline lock."""
        try:
            with knowledge_pipeline_lock():
                if not self._owns_running_checkpoint(str(result["cycle_id"])):
                    return False
                _write_state(self.state_path, result)
                return True
        except KnowledgePipelineBusy:
            return False

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

    async def _run_loop(self, *, waiting_checkpoint_ready: bool) -> None:
        # A curator or an older scheduler process can own the publication lock
        # during startup.  Do not enter the initial delay while its old success
        # checkpoint is still reusable: publish our waiting marker as soon as
        # the shared lock becomes available, then begin the normal cadence.
        while not waiting_checkpoint_ready:
            await asyncio.sleep(CHECKPOINT_WRITER_RETRY_SECONDS)
            waiting_checkpoint_ready = self._write_waiting_checkpoint(
                "initial-delay"
            )
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

    async def run_once(
        self,
        *,
        full_history: bool = False,
        history_promotion_guild_id: int | None = None,
    ) -> dict:
        if history_promotion_guild_id is not None and not full_history:
            raise ValueError(
                "history_promotion_guild_id requires full_history=True"
            )
        if history_promotion_guild_id is not None:
            history_promotion_guild_id = int(history_promotion_guild_id)
            if history_promotion_guild_id <= 0:
                raise ValueError("history_promotion_guild_id must be positive")
        if self._run_lock.locked():
            now = datetime.now(timezone.utc).isoformat()
            return {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "started_at": now,
                "completed_at": now,
                "status": "deferred",
                "reason": "backfill-already-running",
                "mode": "historical" if full_history else "incremental",
                "history_promotion_guild_id": (
                    str(history_promotion_guild_id)
                    if history_promotion_guild_id is not None
                    else None
                ),
                "configured_guild_ids": [str(value) for value in configured_guild_ids()],
                "configured_channel_ids": [str(value) for value in sorted(configured_channel_ids())],
                "discovered_channel_ids": [],
                "eligible_channel_ids": [],
                "current_curation_eligible_channel_ids": [],
                "current_public_curation_channel_ids": [],
                "missing_channel_ids": [],
                "guilds": {},
            }
        async with self._run_lock:
            return await self._run_once_locked(
                full_history=full_history,
                history_promotion_guild_id=history_promotion_guild_id,
            )

    async def _run_once_locked(
        self,
        *,
        full_history: bool = False,
        history_promotion_guild_id: int | None = None,
    ) -> dict:
        if history_promotion_guild_id is not None and not full_history:
            raise ValueError(
                "history_promotion_guild_id requires full_history=True"
            )
        started_at = datetime.now(timezone.utc).isoformat()
        guild_ids = configured_guild_ids()
        selected_channel_ids = configured_channel_ids()
        if (
            history_promotion_guild_id is not None
            and history_promotion_guild_id not in guild_ids
        ):
            raise ValueError(
                "history_promotion_guild_id must be in the configured guild allowlist"
            )
        result = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "cycle_id": uuid.uuid4().hex,
            "started_at": started_at,
            "completed_at": None,
            "status": "running",
            "mode": "historical" if full_history else "incremental",
            "history_promotion_guild_id": (
                str(history_promotion_guild_id)
                if history_promotion_guild_id is not None
                else None
            ),
            "configured_guild_ids": [str(value) for value in guild_ids],
            "configured_channel_ids": [str(value) for value in sorted(selected_channel_ids)],
            "discovered_channel_ids": [],
            "eligible_channel_ids": [],
            "current_curation_eligible_channel_ids": [],
            "current_public_curation_channel_ids": [],
            "missing_channel_ids": [],
            "guilds": {},
        }
        self._completed_message_targets.clear()
        # Persist the in-progress marker before touching Discord. A crash or
        # forced stop must never leave yesterday's successful checkpoint looking
        # current to the downstream curation worker.
        try:
            # Serialize the completed -> running handoff with curation's corpus
            # snapshot. A curator that already validated the completed state
            # must finish before this checkpoint can be invalidated.
            with knowledge_pipeline_lock():
                _write_state(self.state_path, result)
        except KnowledgePipelineBusy:
            return result | {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "status": "deferred",
                "reason": "knowledge-pipeline-busy",
            }
        try:
            if not guild_ids:
                raise RuntimeError("no-configured-guilds")
            for guild_id in guild_ids:
                if full_history:
                    guild_result = await self._refresh_guild(
                        guild_id,
                        full_history=True,
                        history_promotion_guild_id=history_promotion_guild_id,
                    )
                else:
                    guild_result = await self._refresh_guild(guild_id)
                result["guilds"][str(guild_id)] = guild_result

            # Final ACL validation and checkpoint publication are one critical
            # section. A second scheduler cannot publish a new running marker
            # between this snapshot and our completed write, and this cycle
            # never overwrites a marker whose cycle_id it no longer owns.
            with knowledge_pipeline_lock():
                if not self._owns_running_checkpoint(str(result["cycle_id"])):
                    self._completed_message_targets.clear()
                    return result | {
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "status": "deferred",
                        "reason": "checkpoint-ownership-lost",
                    }

                # Guilds are refreshed sequentially and one guild can take
                # hours. Re-evaluate every successful target together now so
                # an ACL or /ignore change in an earlier guild is reflected in
                # the checkpoint carrying this completed_at timestamp.
                archived_threads_cache: dict[
                    tuple[int, int, bool], dict[int, object]
                ] = {}
                for guild_id, guild_result in result["guilds"].items():
                    guild = self.bot.get_guild(int(guild_id))
                    member = getattr(guild, "me", None)
                    completed_ids = {
                        int(value)
                        for value in guild_result.get("completed_channel_ids", [])
                    }
                    current_eligible_ids = []
                    current_public_ids = []
                    for channel, captured_parent in self._completed_message_targets.get(
                        int(guild_id),
                        [],
                    ):
                        if int(channel.id) not in completed_ids:
                            continue
                        current_channel, parent = await _resolve_current_message_target(
                            guild,
                            channel,
                            captured_parent,
                            archived_threads_cache,
                        )
                        if current_channel is None:
                            continue
                        # The resolver returns current lineage so moving a
                        # parent into a protected category is observed.
                        category = (
                            getattr(current_channel, "category", None)
                            or getattr(parent, "category", None)
                        )
                        currently_eligible = _readable_channel(
                            self.bot.config,
                            int(guild_id),
                            current_channel,
                            member,
                            parent=parent,
                        )
                        if not currently_eligible:
                            continue
                        current_eligible_ids.append(str(current_channel.id))
                        if automatic_knowledge_promotion_allowed(
                            current_channel, parent=parent, category=category
                        ):
                            current_public_ids.append(str(current_channel.id))
                    guild_result["current_curation_eligible_channel_ids"] = (
                        current_eligible_ids
                    )
                    guild_result["current_public_curation_channel_ids"] = (
                        current_public_ids
                    )
                self._completed_message_targets.clear()

                discovered = {
                    value
                    for item in result["guilds"].values()
                    for value in item.get("discovered_channel_ids", [])
                }
                eligible = {
                    value
                    for item in result["guilds"].values()
                    for value in item.get("eligible_channel_ids", [])
                }
                current_eligible = {
                    value
                    for item in result["guilds"].values()
                    for value in item.get(
                        "current_curation_eligible_channel_ids",
                        [],
                    )
                }
                current_public = {
                    value
                    for item in result["guilds"].values()
                    for value in item.get(
                        "current_public_curation_channel_ids",
                        [],
                    )
                }
                configured = {str(value) for value in selected_channel_ids}
                result["discovered_channel_ids"] = sorted(discovered, key=int)
                result["eligible_channel_ids"] = sorted(eligible, key=int)
                result["current_curation_eligible_channel_ids"] = sorted(
                    current_eligible,
                    key=int,
                )
                result["current_public_curation_channel_ids"] = sorted(
                    current_public,
                    key=int,
                )
                result["missing_channel_ids"] = sorted(
                    configured - eligible,
                    key=int,
                )
                failed = any(
                    item.get("status") != "completed"
                    for item in result["guilds"].values()
                )
                if result["missing_channel_ids"]:
                    failed = True
                result["status"] = "failed" if failed else "completed"
                result["completed_at"] = datetime.now(timezone.utc).isoformat()
                _write_state(self.state_path, result)
                return result
        except KnowledgePipelineBusy:
            self._completed_message_targets.clear()
            return result | {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "status": "deferred",
                "reason": "final-checkpoint-busy",
            }
        except asyncio.CancelledError:
            self._completed_message_targets.clear()
            result["status"] = "interrupted"
            result["error_type"] = "CancelledError"
            result["completed_at"] = datetime.now(timezone.utc).isoformat()
            self._write_terminal_checkpoint_if_owned(result)
            raise
        except Exception as exc:
            self._completed_message_targets.clear()
            result["status"] = "failed"
            result["error_type"] = type(exc).__name__
            result["completed_at"] = datetime.now(timezone.utc).isoformat()
            self._write_terminal_checkpoint_if_owned(result)
            raise

    async def _refresh_guild(
        self,
        guild_id: int,
        *,
        full_history: bool = False,
        history_promotion_guild_id: int | None = None,
    ) -> dict:
        if history_promotion_guild_id is not None and not full_history:
            raise ValueError(
                "history_promotion_guild_id requires full_history=True"
            )
        authorize_history_promotion = (
            full_history and history_promotion_guild_id == guild_id
        )
        guild = self.bot.get_guild(guild_id)
        corpus_store = self.bot.config.get_corpus(guild_id)
        selected_ids = configured_channel_ids()
        base_summary = {
            "status": "failed",
            "reason": None,
            "configured_channel_ids": [],
            "discovered_channel_ids": [],
            "eligible_channel_ids": [],
            "current_curation_eligible_channel_ids": [],
            "current_public_curation_channel_ids": [],
            "container_channel_ids": [],
            "missing_channel_ids": [],
            "channels_total": 0,
            "channels_completed": 0,
            "completed_channel_ids": [],
            "messages_indexed": 0,
            "documents_uploaded": 0,
            "documents_replaced": 0,
            "mode": "historical" if full_history else "incremental",
            "festivals": {},
            "failures": [],
        }
        if guild is None or not corpus_store:
            return base_summary | {
                "reason": "guild-or-corpus-unavailable",
                "missing_channel_ids": [str(value) for value in sorted(selected_ids)],
            }
        summary = dict(base_summary)
        self._completed_message_targets[guild_id] = []
        summary_lock = asyncio.Lock()
        session = None

        async def process_channel(channel) -> None:
            try:
                latest_message_id = None
                latest_message_at = None
                if full_history:
                    oldest = await _oldest_message_at(channel, before=session.cutoff)
                    festivals = _historical_festivals(oldest, session.cutoff)
                    for index, festival in enumerate(festivals):
                        after, before = _festival_bounds(festival, session.cutoff)
                        if before <= after:
                            continue
                        channel_result = await self.bot.corpus.backfill_channel(
                            channel,
                            corpus_store,
                            after=after,
                            before=before,
                            replace_existing=True,
                            dry_run=False,
                            session=session,
                            complete_session_channel=index == len(festivals) - 1,
                            full_history=True,
                            authorize_history_promotion=authorize_history_promotion,
                        )
                        if channel_result.latest_message_id and channel_result.latest_message_at:
                            latest_message_id = channel_result.latest_message_id
                            latest_message_at = channel_result.latest_message_at
                        async with summary_lock:
                            summary["messages_indexed"] += channel_result.messages_indexed
                            summary["documents_uploaded"] += channel_result.documents_uploaded
                            summary["documents_replaced"] += channel_result.documents_replaced
                            annual = summary["festivals"].setdefault(
                                f"{festival}th",
                                {
                                    "messages_indexed": 0,
                                    "documents_uploaded": 0,
                                    "documents_replaced": 0,
                                    "channel_slices": 0,
                                },
                            )
                            annual["messages_indexed"] += channel_result.messages_indexed
                            annual["documents_uploaded"] += channel_result.documents_uploaded
                            annual["documents_replaced"] += channel_result.documents_replaced
                            annual["channel_slices"] += 1
                else:
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
                    latest_message_id = channel_result.latest_message_id
                    latest_message_at = channel_result.latest_message_at
                    async with summary_lock:
                        summary["messages_indexed"] += channel_result.messages_indexed
                        summary["documents_uploaded"] += channel_result.documents_uploaded
                        summary["documents_replaced"] += channel_result.documents_replaced
                if latest_message_id and latest_message_at:
                    await self.bot.config.set_backfill_cursor(
                        guild_id,
                        channel.id,
                        latest_message_id,
                        latest_message_at,
                    )
                async with summary_lock:
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
            text_by_id = {
                int(channel.id): channel
                for channel in text_channels
                if getattr(channel, "id", None) is not None
            }
            thread_parents_by_id = text_by_id | forums_by_id
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
            readable_text_by_id = {
                int(channel.id): channel for channel in readable_text
            }
            readable_thread_parents = readable_text_by_id | readable_forums
            channels_by_id = {
                int(channel.id): channel
                for channel in readable_text
                if not selected_ids or int(channel.id) in selected_ids
            }
            # A forum has no message history of its own. Mark its immutable ID
            # eligible, then expand it into independently scoped thread targets.
            eligible_ids = set(channels_by_id)
            requested_forum_ids = selected_ids & set(readable_forums)
            container_ids = set(readable_forums) if not selected_ids else set(requested_forum_ids)
            eligible_ids.update(container_ids)

            undiscovered_selected_ids = selected_ids - discovered_ids
            thread_archive_parents = {
                channel_id: channel
                for channel_id, channel in readable_thread_parents.items()
                if callable(getattr(channel, "archived_threads", None))
            }
            active_threads_method = getattr(guild, "active_threads", None)
            discover_threads = bool(readable_thread_parents) and (
                (not selected_ids and (bool(thread_archive_parents) or callable(active_threads_method)))
                or bool(requested_forum_ids)
                or bool(undiscovered_selected_ids)
            )
            active_threads: list[object] = []
            if discover_threads:
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
                elif selected_ids:
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
                private = _private_thread(thread)
                if private and thread_id not in selected_ids:
                    # A bot with Administrator can enumerate private threads,
                    # but a parent forum ID must never authorize their import.
                    return
                discovered_ids.add(thread_id)
                if parent_id not in readable_thread_parents:
                    return
                if not _readable_channel(
                    self.bot.config,
                    guild_id,
                    thread,
                    guild.me,
                    parent=parent,
                ):
                    return
                parent_expands_public_threads = (
                    not private and parent_id in requested_forum_ids
                )
                if (
                    selected_ids
                    and thread_id not in selected_ids
                    and not parent_expands_public_threads
                ):
                    return
                channels_by_id[thread_id] = thread
                eligible_ids.add(thread_id)

            for thread in active_threads:
                parent = _channel_parent(thread, thread_parents_by_id)
                if parent is not None:
                    admit_thread(thread, parent)

            undiscovered_selected_ids = selected_ids - discovered_ids
            if not selected_ids:
                # Public threads inherit their parent's read boundary. Include
                # both Forum posts and ordinary public TextChannel threads;
                # private threads still fail closed in ``admit_thread``.
                archive_parent_ids = set(thread_archive_parents)
            else:
                archive_parent_ids = set(requested_forum_ids)
                # An explicitly allowlisted archived thread cannot be resolved
                # from Guild.active_threads(). Search every readable thread
                # parent, including ordinary TextChannels, while still admitting
                # only the exact requested thread ID.
                if undiscovered_selected_ids:
                    archive_parent_ids.update(thread_archive_parents)

            for parent_id in sorted(archive_parent_ids):
                parent_channel = thread_archive_parents[parent_id]
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
                            parent_channel.archived_threads(
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
                                "channel_id": str(parent_id),
                                "stage": stage,
                                "error_type": type(exc).__name__,
                            }
                        )
                        log.exception(
                            "Scheduled backfill could not list archived threads guild=%s forum=%s stage=%s",
                            guild_id,
                            parent_id,
                            stage,
                        )
                        continue
                    for thread in archived:
                        parent = _channel_parent(thread, thread_parents_by_id)
                        if int(getattr(parent, "id", 0) or 0) != parent_id:
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
                        str(value) for value in sorted(container_ids)
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
                prefer_local_inventory=True,
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
            completed_ids = {int(value) for value in summary["completed_channel_ids"]}
            completed_targets = []
            for channel in channels:
                if int(channel.id) not in completed_ids:
                    continue
                parent = _channel_parent(channel, thread_parents_by_id)
                completed_targets.append((channel, parent))
            self._completed_message_targets[guild_id] = completed_targets
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
