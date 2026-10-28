"""おしゃべりやがぽん v2 - エントリポイント"""

import asyncio
import contextlib
import logging
import os
import signal
import threading
import time
from collections.abc import Callable

import uvicorn
from dotenv import load_dotenv

from bot.shutdown import (
    HardShutdownWatchdog,
    remaining_shutdown_seconds,
    shutdown_deadline_seconds,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("yagapon")
# Pycord logs an INFO line for many normal RTCP packets. During long sessions
# that synchronous log volume can starve the Discord interaction event loop.
logging.getLogger("discord.voice.receive.reader").setLevel(logging.WARNING)

VOICE_SHUTDOWN_BUDGET_SECONDS = 40.0
DEFAULT_API_SHUTDOWN_BUDGET_SECONDS = 5.0
MAX_API_SHUTDOWN_BUDGET_SECONDS = 10.0
DEFAULT_UVICORN_GRACEFUL_TIMEOUT_SECONDS = 10.0
MAX_UVICORN_GRACEFUL_TIMEOUT_SECONDS = 15.0
DEFAULT_EXECUTOR_SHUTDOWN_SECONDS = 5.0
MAX_EXECUTOR_SHUTDOWN_SECONDS = 10.0
_active_process_watchdog: HardShutdownWatchdog | None = None


def _bounded_env_seconds(name: str, default: float, maximum: float) -> float:
    try:
        configured = float(os.environ.get(name, str(default)))
    except ValueError:
        configured = default
    return min(maximum, max(0.05, configured))


class CoordinatedUvicornServer(uvicorn.Server):
    """Let the application own SIGINT/SIGTERM and its one shutdown deadline."""

    @contextlib.contextmanager
    def capture_signals(self):
        # ``Server.serve`` otherwise replaces the loop's handler with its own.
        # The shared coordinator must observe the original signal so voice fsync
        # and the hard watchdog start at the same monotonic instant.
        yield


def _activate_process_watchdog(watchdog: HardShutdownWatchdog) -> None:
    global _active_process_watchdog
    previous = _active_process_watchdog
    if previous is not None and previous is not watchdog:
        previous.disarm()
    _active_process_watchdog = watchdog
    watchdog.start()


def _disarm_process_watchdog() -> None:
    global _active_process_watchdog
    watchdog = _active_process_watchdog
    _active_process_watchdog = None
    if watchdog is not None:
        watchdog.disarm()


def _tighten_watchdog_after_durable_cleanup() -> None:
    """Bound Python executor teardown after all durable application cleanup.

    ``asyncio.run`` waits for default-executor threads after the main coroutine
    returns. Cancelling an ``asyncio.to_thread`` waiter cannot stop its worker,
    so a completed voice checkpoint/corpus flush used to be followed by roughly
    30 seconds of invisible container shutdown. At this point the application
    has already rejected new work, fsynced active recordings, cancelled
    background jobs, closed Discord, and flushed its corpus. It is therefore
    safe to replace the broad signal deadline with a short process-teardown
    deadline. The original (earlier) deadline always wins.

    Direct async callers do not arm the process watchdog and are unaffected.
    """
    current = _active_process_watchdog
    if current is None:
        return
    budget = _bounded_env_seconds(
        "YAGAPON_EXECUTOR_SHUTDOWN_SECONDS",
        DEFAULT_EXECUTOR_SHUTDOWN_SECONDS,
        MAX_EXECUTOR_SHUTDOWN_SECONDS,
    )
    tightened_deadline = min(current.deadline, time.monotonic() + budget)
    if tightened_deadline >= current.deadline:
        return
    log.info(
        "Durable cleanup complete; executor teardown deadline is %.1fs",
        max(0.0, tightened_deadline - time.monotonic()),
    )
    _activate_process_watchdog(HardShutdownWatchdog(tightened_deadline))


class ShutdownCoordinator:
    """Create exactly one signal-origin deadline and process watchdog."""

    def __init__(
        self,
        bot,
        server,
        *,
        deadline_seconds: float | None = None,
    ) -> None:
        self.bot = bot
        self.server = server
        self.deadline_seconds = deadline_seconds
        self.deadline: float | None = None
        self.task: asyncio.Task | None = None
        self.watchdog: HardShutdownWatchdog | None = None
        self._deadline_budget: float | None = None
        self._arm_lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._previous_signal_handlers: dict[
            signal.Signals,
            signal.Handlers,
        ] = {}
        # Keep one stable bound-method object so restoration can verify that it
        # is not overwriting a handler installed by somebody else later.
        self._raw_signal_handler: Callable[[int, object], None] = self._handle_signal

    def _arm_deadline(self) -> float:
        """Start the process watchdog without depending on an asyncio turn.

        Python dispatches ``signal.signal`` handlers on its main thread even if
        that thread is currently stuck in synchronous application code.  The
        event loop may therefore be unable to create the graceful-shutdown
        task, but this deadline and daemon watchdog still start at signal
        receipt.  ``RLock`` makes a repeated signal safe even if it interrupts
        this method on the same main thread.
        """
        with self._arm_lock:
            if self.deadline is None:
                started_at = time.monotonic()
                budget = (
                    shutdown_deadline_seconds()
                    if self.deadline_seconds is None
                    else min(60.0, max(0.05, float(self.deadline_seconds)))
                )
                self.deadline = started_at + budget
                self._deadline_budget = budget
            if self.watchdog is None:
                self.watchdog = HardShutdownWatchdog(self.deadline)
                _activate_process_watchdog(self.watchdog)
            return self.deadline

    def _handle_signal(self, _signum: int, _frame: object) -> None:
        """Raw signal handler: arm first, then ask asyncio to clean up."""
        self._arm_deadline()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.request)

    def install_signal_handlers(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Install handlers that do not wait for an event-loop callback."""
        if self._previous_signal_handlers:
            return
        self._loop = loop
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._previous_signal_handlers[sig] = signal.signal(
                sig,
                self._raw_signal_handler,
            )

    def restore_signal_handlers(self) -> None:
        """Restore handlers owned by the embedding process, if unchanged."""
        previous = self._previous_signal_handlers
        self._previous_signal_handlers = {}
        for sig, handler in previous.items():
            if signal.getsignal(sig) == self._raw_signal_handler:
                signal.signal(sig, handler)
        self._loop = None

    def request(self) -> asyncio.Task:
        if self.task is not None:
            if not self.task.done():
                log.info("Shutdown is already in progress")
            return self.task
        deadline = self._arm_deadline()
        budget = self._deadline_budget
        if budget is None:
            budget = remaining_shutdown_seconds(deadline)
        log.info(
            "Shutdown requested; absolute deadline is %.1fs",
            budget,
        )
        self.task = asyncio.create_task(
            shutdown(self.bot, self.server, deadline=deadline),
            name="graceful-shutdown",
        )
        return self.task

    def disarm(self) -> None:
        if self.watchdog is not None:
            self.watchdog.disarm()


def _prewarm_transcription_sdk() -> None:
    """Resolve Google SDK lazy surfaces before Discord heartbeat starts."""
    from bot.ai_retry import new_genai_client

    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        return
    client = new_genai_client(api_key)
    aio_client = None
    try:
        aio_client = client.aio
        # Accessing Interactions performs a large generated-module/Pydantic
        # initialization synchronously. Do it during startup, not first audio.
        _ = aio_client.interactions
        _ = aio_client.models
    finally:
        try:
            if aio_client is not None:
                asyncio.run(aio_client.aclose())
        finally:
            client.close()


async def main(*, _keep_watchdog_armed: bool = False):
    from api.server import create_app
    from bot.client import create_bot

    bot = create_bot()
    app = create_app(bot)

    # uvicorn設定 (同一イベントループで実行)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("API_PORT", "8000")),
        loop="none",
        log_level="info",
        timeout_graceful_shutdown=_bounded_env_seconds(
            "YAGAPON_UVICORN_GRACEFUL_TIMEOUT_SECONDS",
            DEFAULT_UVICORN_GRACEFUL_TIMEOUT_SECONDS,
            MAX_UVICORN_GRACEFUL_TIMEOUT_SECONDS,
        ),
    )
    server = CoordinatedUvicornServer(config)

    coordinator = ShutdownCoordinator(bot, server)

    loop = asyncio.get_running_loop()
    coordinator.install_signal_handlers(loop)

    # Bot + API を同時起動
    token = os.environ.get("DISCORD_TOKEN", "")
    try:
        if not token:
            log.error("DISCORD_TOKEN is not set!")
            return

        if os.environ.get(
            "YAGAPON_TRANSCRIPTION_PREWARM_ENABLED",
            "false",
        ).lower() == "true":
            try:
                await asyncio.to_thread(_prewarm_transcription_sdk)
                log.info("Transcription SDK prewarm completed")
            except Exception:
                # Startup must remain available for non-voice commands. Per-request
                # STT still runs in an isolated worker as a second line of defense.
                log.exception("Transcription SDK prewarm failed; continuing startup")
        else:
            # Capture-only recording does not need the generated STT client.
            # Loading it before Discord connects added roughly 90 seconds to a
            # production restart on the small VM. Minutes/replay processing
            # still initializes the SDK lazily in its isolated worker.
            log.info("Transcription SDK prewarm skipped; lazy processing enabled")

        log.info("Starting bot + API server...")
        try:
            await asyncio.gather(
                bot.start(token),
                server.serve(),
            )
        finally:
            # ``bot.close`` can release bot.start before its final durable corpus
            # flush finishes. Unexpected task failures must also checkpoint active
            # sessions and flush buffers before the event loop exits.
            await coordinator.request()
    finally:
        coordinator.restore_signal_handlers()
        # Production keeps this armed through asyncio.run's default-executor
        # teardown. Direct async test callers do not own that outer boundary.
        if not _keep_watchdog_armed:
            coordinator.disarm()


def _consume_shutdown_task(task: asyncio.Task) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


async def shutdown(bot, server, *, deadline: float | None = None):
    """Stop every component under one absolute process deadline."""
    if deadline is None:
        deadline = time.monotonic() + shutdown_deadline_seconds()
    server.should_exit = True
    from bot.voice import checkpoint_voice_sessions_for_shutdown

    # Reject new /join claims before the voice checkpoint snapshots sessions.
    # The attribute is process-local and the bot exits after this shutdown.
    setattr(bot, "_voice_shutdown_started", True)

    voice_timeout = remaining_shutdown_seconds(
        deadline,
        cap=VOICE_SHUTDOWN_BUDGET_SECONDS,
    )
    # Schedule voice first. API work may be holding provider locks, but capture
    # stop/fsync must begin on the first event-loop turn after SIGTERM.
    voice_task = asyncio.create_task(
        checkpoint_voice_sessions_for_shutdown(timeout_seconds=voice_timeout),
        name="shutdown-voice-checkpoint",
    )

    shutdown_tasks = {voice_task}
    app = getattr(getattr(server, "config", None), "app", None)
    if app is not None:
        from api.server import shutdown_backfill_tasks

        api_budget = _bounded_env_seconds(
            "YAGAPON_API_SHUTDOWN_TIMEOUT_SECONDS",
            DEFAULT_API_SHUTDOWN_BUDGET_SECONDS,
            MAX_API_SHUTDOWN_BUDGET_SECONDS,
        )
        shutdown_tasks.add(
            asyncio.create_task(
                shutdown_backfill_tasks(
                    app,
                    timeout_seconds=remaining_shutdown_seconds(deadline, cap=api_budget),
                ),
                name="shutdown-api-backfills",
            )
        )

    done, pending = await asyncio.wait(
        shutdown_tasks,
        timeout=remaining_shutdown_seconds(deadline),
    )
    voice_checkpointed = False
    for task in done:
        try:
            result = task.result()
            if task is voice_task and result is True:
                voice_checkpointed = True
            elif task is voice_task:
                log.error(
                    "Voice shutdown checkpoint did not confirm durable completion"
                )
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Shutdown prerequisite failed task=%s", task.get_name())
    for task in pending:
        task.cancel()
        task.add_done_callback(_consume_shutdown_task)
        log.error("Shutdown prerequisite exceeded the process deadline task=%s", task.get_name())
    if pending:
        await asyncio.sleep(0)
    log.info("Closing bot...")
    await bot.close(
        shutdown_deadline=deadline,
        voice_checkpointed=voice_checkpointed,
    )
    _tighten_watchdog_after_durable_cleanup()


def run() -> None:
    """Run until all async and default-executor teardown has completed."""
    try:
        asyncio.run(main(_keep_watchdog_armed=True))
    finally:
        # This line is intentionally outside the coroutine: cancelling a
        # ``to_thread`` waiter does not stop asyncio.run from joining its thread.
        _disarm_process_watchdog()


if __name__ == "__main__":
    run()
