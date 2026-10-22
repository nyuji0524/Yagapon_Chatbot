"""Periodic, resumable Discord history refresh for the RAG corpus.

The scheduler is opt-in and requires an immutable guild-ID allowlist.  It
reuses the same per-guild lock and per-channel cursors as the manual/API
backfill paths, so a scheduled cycle never races an operator run.  A durable
cycle checkpoint lets downstream curation wait for a completed refresh rather
than reading a half-updated corpus.
"""

from __future__ import annotations

import asyncio
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

log = logging.getLogger("yagapon.scheduled_backfill")

DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60
MIN_INTERVAL_SECONDS = 6 * 60 * 60
DEFAULT_INITIAL_DELAY_SECONDS = 5 * 60


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
    """Optionally narrow scheduled refreshes to immutable channel IDs."""
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
    )


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


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

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        if not scheduler_enabled() or self.running:
            return False
        now = datetime.now(timezone.utc).isoformat()
        waiting = {
            "schema_version": 1,
            "started_at": now,
            "completed_at": None,
            "status": "waiting",
            "reason": "initial-delay",
            "configured_guild_ids": [str(value) for value in configured_guild_ids()],
            "configured_channel_ids": [str(value) for value in sorted(configured_channel_ids())],
            "discovered_channel_ids": [],
            "eligible_channel_ids": [],
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
        self._task = asyncio.create_task(
            self._run_loop(),
            name="scheduled-discord-backfill",
        )
        return True

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
        if initial_delay:
            await asyncio.sleep(initial_delay)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The loop must remain available for the next interval. Each
                # guild/channel failure is also isolated inside run_once.
                log.exception("Scheduled Discord backfill cycle failed")
            await asyncio.sleep(interval)

    async def run_once(self) -> dict:
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
                value
                for item in result["guilds"].values()
                for value in item.get("discovered_channel_ids", [])
            }
            eligible = {
                value
                for item in result["guilds"].values()
                for value in item.get("eligible_channel_ids", [])
            }
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
            discovered_channels = list(guild.text_channels)
            discovered_ids = {int(channel.id) for channel in discovered_channels}
            readable_channels = [
                channel
                for channel in discovered_channels
                if channel.permissions_for(guild.me).read_message_history
                and not self.bot.config.is_ignored(guild_id, channel.id)
            ]
            channels = [
                channel
                for channel in readable_channels
                if not selected_ids or channel.id in selected_ids
            ]
            eligible_ids = {int(channel.id) for channel in channels}
            local_configured = selected_ids & discovered_ids
            local_missing = local_configured - eligible_ids
            summary.update(
                {
                    "status": "running",
                    "configured_channel_ids": [str(value) for value in sorted(local_configured)],
                    "discovered_channel_ids": [str(value) for value in sorted(discovered_ids)],
                    "eligible_channel_ids": [str(value) for value in sorted(eligible_ids)],
                    "missing_channel_ids": [str(value) for value in sorted(local_missing)],
                    "channels_total": len(channels),
                }
            )
            if not channels:
                summary["status"] = "failed"
                summary["reason"] = "no-eligible-channels"
                return summary
            if local_missing:
                summary["status"] = "failed"
                summary["reason"] = "configured-channels-not-readable"
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
                        details={
                            key: value
                            for key, value in summary.items()
                            if key not in {"failures", "reason"}
                        }
                        | {"failure_count": len(summary["failures"])},
                    )
                except Exception:
                    log.exception("Could not record scheduled backfill telemetry")
        return summary
