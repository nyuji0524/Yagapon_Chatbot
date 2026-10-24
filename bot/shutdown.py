"""Shared process-shutdown deadline and hard-stop guardrails."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

DEFAULT_SHUTDOWN_DEADLINE_SECONDS = 60.0
MAX_SHUTDOWN_DEADLINE_SECONDS = 60.0
HARD_SHUTDOWN_EXIT_CODE = 70


def shutdown_deadline_seconds() -> float:
    """Return a process budget that always leaves Docker kill headroom."""
    try:
        configured = float(
            os.environ.get(
                "YAGAPON_SHUTDOWN_DEADLINE_SECONDS",
                str(DEFAULT_SHUTDOWN_DEADLINE_SECONDS),
            )
        )
    except ValueError:
        configured = DEFAULT_SHUTDOWN_DEADLINE_SECONDS
    return min(MAX_SHUTDOWN_DEADLINE_SECONDS, max(0.05, configured))


def remaining_shutdown_seconds(
    deadline: float,
    *,
    cap: float | None = None,
) -> float:
    """Return time left on one absolute monotonic deadline."""
    remaining = max(0.0, deadline - time.monotonic())
    return min(remaining, max(0.0, cap)) if cap is not None else remaining


class HardShutdownWatchdog:
    """Terminate the process if cooperative cleanup cannot beat its deadline.

    Cancelling an ``asyncio.to_thread`` waiter does not stop the synchronous
    function.  Python's ``asyncio.run`` subsequently waits up to five minutes
    for the default executor, which is longer than Docker's stop grace.  This
    independent daemon thread therefore remains armed until *after*
    ``asyncio.run`` has completely returned.
    """

    def __init__(
        self,
        deadline: float,
        *,
        exit_code: int = HARD_SHUTDOWN_EXIT_CODE,
        exit_process: Callable[[int], object] | None = None,
    ) -> None:
        self.deadline = deadline
        self.exit_code = exit_code
        self._exit_process = exit_process or os._exit
        self._disarmed = threading.Event()
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name="yagapon-hard-shutdown-watchdog",
            daemon=True,
        )

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def disarm(self) -> None:
        self._disarmed.set()

    def _run(self) -> None:
        timeout = max(0.0, self.deadline - time.monotonic())
        if self._disarmed.wait(timeout):
            return
        # Avoid logging locks and buffered streams: either may be owned by a
        # thread that caused the shutdown stall. A full stderr pipe must not
        # block the only mechanism that guarantees the process exit boundary.
        try:
            os.set_blocking(2, False)
            os.write(
                2,
                (
                    "YagaPon hard shutdown deadline reached; "
                    "terminating before Docker SIGKILL\n"
                ).encode("utf-8"),
            )
        except (AttributeError, OSError):
            pass
        self._exit_process(self.exit_code)
