"""Cross-process recording priority lease.

The Discord bot, source indexer, Drive sync and Knowledge curator share the
same ``/data`` volume in production.  A live recording publishes a short
lease there so optional/background work can yield disk, memory and API load
until capture has stopped.  Leases expire automatically after a hard crash.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STALE_SECONDS = 90.0
DEFAULT_POLL_SECONDS = 5.0


def activity_root() -> Path:
    return Path(
        os.environ.get(
            "YAGAPON_RECORDING_ACTIVITY_DIR",
            "/data/recording-activity",
        )
    )


def _positive_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def recording_is_active(*, now: float | None = None) -> bool:
    """Return whether any fresh capture lease exists.

    Stale files are best-effort removed so a killed bot cannot permanently
    suspend background workers.
    """

    root = activity_root()
    try:
        leases = tuple(root.glob("*.json"))
    except OSError:
        return False
    current = time.time() if now is None else now
    stale_after = _positive_float(
        "YAGAPON_RECORDING_ACTIVITY_STALE_SECONDS",
        DEFAULT_STALE_SECONDS,
    )
    active = False
    for lease in leases:
        try:
            age = max(0.0, current - lease.stat().st_mtime)
        except FileNotFoundError:
            continue
        except OSError:
            # A lease that cannot safely be inspected is treated as active.
            active = True
            continue
        if age <= stale_after:
            active = True
            continue
        try:
            lease.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Fail safe: do not start optional I/O while lease state is unclear.
            active = True
    return active


def wait_for_recording_idle(*, poll_seconds: float | None = None) -> None:
    delay = poll_seconds or _positive_float(
        "YAGAPON_RECORDING_ACTIVITY_POLL_SECONDS",
        DEFAULT_POLL_SECONDS,
    )
    while recording_is_active():
        time.sleep(delay)


async def wait_for_recording_idle_async(
    *,
    poll_seconds: float | None = None,
) -> None:
    delay = poll_seconds or _positive_float(
        "YAGAPON_RECORDING_ACTIVITY_POLL_SECONDS",
        DEFAULT_POLL_SECONDS,
    )
    while recording_is_active():
        await asyncio.sleep(delay)


@dataclass
class RecordingActivityLease:
    guild_id: int
    session_id: str

    @property
    def path(self) -> Path:
        safe_session = "".join(
            character
            for character in self.session_id
            if character.isalnum() or character in {"-", "_"}
        )[:96]
        return activity_root() / f"{int(self.guild_id)}-{safe_session}.json"

    def acquire(self) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".json.{os.getpid()}.tmp")
        payload = {
            "guild_id": str(self.guild_id),
            "session_id": self.session_id,
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def heartbeat(self) -> None:
        try:
            os.utime(self.path, None)
        except FileNotFoundError:
            # Recreate a lease removed by a transient mount or cleanup race.
            self.acquire()

    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
