import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="requires POSIX signals")
def test_signal_deadline_survives_stuck_api_backfill_and_default_executor_thread():
    """The real PID must exit before Docker grace even when asyncio cannot."""
    root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        import asyncio
        import os
        import signal
        import threading
        import time
        from types import SimpleNamespace

        import main
        import bot.voice as voice

        started_at = time.monotonic()
        provider_started = threading.Event()
        provider_release = threading.Event()

        def emit(label):
            os.write(1, f"{label} {time.monotonic() - started_at:.3f}\\n".encode())

        async def checkpoint_voice_sessions_for_shutdown(*, timeout_seconds):
            assert timeout_seconds > 0
            emit("VOICE")
            return True

        voice.checkpoint_voice_sessions_for_shutdown = checkpoint_voice_sessions_for_shutdown

        class FakeBot:
            operations = SimpleNamespace(update_job=lambda *_args, **_kwargs: None)
            corpus = SimpleNamespace()

            async def close(self, **kwargs):
                assert kwargs["voice_checkpointed"] is True
                emit("BOT_CLOSE")

        async def stubborn_backfill():
            try:
                provider_started.set()
                # Cancellation stops only this waiter. The synchronous provider
                # thread remains blocked and asyncio.run would join it for 300s.
                await asyncio.to_thread(provider_release.wait)
            except asyncio.CancelledError:
                await asyncio.Event().wait()

        async def program():
            bot = FakeBot()
            backfill = asyncio.create_task(stubborn_backfill())
            app = SimpleNamespace(
                state=SimpleNamespace(
                    accepting_backfills=True,
                    backfill_tasks={backfill},
                    backfill_jobs={
                        "blocked": {
                            "job_id": "blocked",
                            "status": "running",
                            "guild_id": 1,
                            "finished_at": None,
                        }
                    },
                    backfill_sessions={},
                    backfill_shutdown_task=None,
                    bot=bot,
                )
            )
            server = SimpleNamespace(
                should_exit=False,
                config=SimpleNamespace(app=app),
            )
            coordinator = main.ShutdownCoordinator(
                bot,
                server,
                deadline_seconds=0.6,
            )
            loop = asyncio.get_running_loop()
            coordinator.install_signal_handlers(loop)
            while not provider_started.is_set():
                await asyncio.sleep(0)
            os.kill(os.getpid(), signal.SIGTERM)
            while coordinator.task is None:
                await asyncio.sleep(0)
            await coordinator.task
            emit("ASYNC_DONE")

        os.environ["YAGAPON_API_SHUTDOWN_TIMEOUT_SECONDS"] = "0.1"
        asyncio.run(program())
        # This is reachable only after the default executor thread exits. It is
        # intentionally never released; the watchdog must terminate the PID.
        main._disarm_process_watchdog()
        raise SystemExit(99)
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 70, completed.stderr
    assert elapsed < 2
    lines = completed.stdout.splitlines()
    assert [line.split()[0] for line in lines] == [
        "VOICE",
        "BOT_CLOSE",
        "ASYNC_DONE",
    ]
    # Shared CI runners can take more than 200 ms to schedule the signal
    # callback even though checkpointing still begins well before the 600 ms
    # hard deadline. Keep the assertion strict enough to catch sequencing
    # regressions without treating a few milliseconds of scheduler jitter as a
    # shutdown failure.
    assert float(lines[0].split()[1]) < 0.35
    assert float(lines[1].split()[1]) < 0.4
    assert "hard shutdown deadline reached" in completed.stderr


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="requires POSIX signals")
def test_signal_starts_hard_deadline_while_event_loop_is_synchronously_blocked():
    """A raw handler must not wait for asyncio's next event-loop turn."""
    root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        import asyncio
        import os
        import signal
        import threading
        import time
        from types import SimpleNamespace

        import main

        def send_signal_after_loop_blocks(blocked):
            assert blocked.wait(1)
            # Ensure the main thread has entered the synchronous sleep below.
            time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        async def program():
            blocked = threading.Event()
            coordinator = main.ShutdownCoordinator(
                SimpleNamespace(),
                SimpleNamespace(should_exit=False),
                deadline_seconds=0.35,
            )
            coordinator.install_signal_handlers(asyncio.get_running_loop())
            threading.Thread(
                target=send_signal_after_loop_blocks,
                args=(blocked,),
                daemon=True,
            ).start()
            blocked.set()
            # ``loop.add_signal_handler`` would only enqueue its callback and
            # leave this process asleep. The raw handler must arm the watchdog
            # immediately, without this coroutine ever yielding again.
            time.sleep(10)
            raise SystemExit(99)

        asyncio.run(program())
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.monotonic()

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 70, completed.stderr
    assert elapsed < 1.5
    assert "hard shutdown deadline reached" in completed.stderr
