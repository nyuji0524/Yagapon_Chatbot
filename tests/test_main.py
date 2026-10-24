import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from main import (
    CoordinatedUvicornServer,
    ShutdownCoordinator,
    _prewarm_transcription_sdk,
    shutdown,
)


def test_transcription_sdk_prewarm_resolves_and_closes_both_clients(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    aio_client = SimpleNamespace(
        interactions=object(),
        models=object(),
        aclose=AsyncMock(),
    )
    client = SimpleNamespace(aio=aio_client, close=Mock())
    create = Mock(return_value=client)
    monkeypatch.setattr("bot.ai_retry.new_genai_client", create)

    _prewarm_transcription_sdk()

    create.assert_called_once_with("test-key")
    aio_client.aclose.assert_awaited_once_with()
    client.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_shutdown_delegates_resource_cleanup_to_bot_once(monkeypatch):
    bot = SimpleNamespace(
        close=AsyncMock(),
        corpus=SimpleNamespace(shutdown=AsyncMock()),
    )
    server = SimpleNamespace(should_exit=False)
    monkeypatch.setattr(
        "bot.voice.checkpoint_voice_sessions_for_shutdown",
        AsyncMock(return_value=True),
    )

    await shutdown(bot, server)

    assert server.should_exit is True
    bot.close.assert_awaited_once()
    assert bot.close.await_args.kwargs["shutdown_deadline"] > time.monotonic()
    assert bot.close.await_args.kwargs["voice_checkpointed"] is True
    bot.corpus.shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_starts_voice_checkpoint_before_bounded_api_drain(monkeypatch):
    bot = SimpleNamespace(close=AsyncMock())
    app = object()
    server = SimpleNamespace(
        should_exit=False,
        config=SimpleNamespace(app=app),
    )
    voice_started = asyncio.Event()
    release_voice = asyncio.Event()

    async def checkpoint(*, timeout_seconds):
        assert timeout_seconds > 0
        voice_started.set()
        await release_voice.wait()
        return True

    async def drain(_app, *, timeout_seconds):
        assert _app is app
        assert timeout_seconds > 0
        await asyncio.wait_for(voice_started.wait(), timeout=1)
        release_voice.set()

    monkeypatch.setattr(
        "bot.voice.checkpoint_voice_sessions_for_shutdown",
        checkpoint,
    )
    monkeypatch.setattr("api.server.shutdown_backfill_tasks", drain)

    await shutdown(bot, server)

    assert voice_started.is_set()
    bot.close.assert_awaited_once()
    assert bot.close.await_args.kwargs["voice_checkpointed"] is True


@pytest.mark.asyncio
async def test_shutdown_retries_voice_checkpoint_in_bot_close_after_prerequisite_failure(
    monkeypatch,
):
    bot = SimpleNamespace(close=AsyncMock())
    server = SimpleNamespace(should_exit=False)
    checkpoint = AsyncMock(side_effect=RuntimeError("checkpoint failed"))
    monkeypatch.setattr(
        "bot.voice.checkpoint_voice_sessions_for_shutdown",
        checkpoint,
    )

    await shutdown(bot, server)

    assert bot.close.await_args.kwargs["voice_checkpointed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_result", [None, False])
async def test_shutdown_requires_explicit_durable_checkpoint_confirmation(
    monkeypatch,
    checkpoint_result,
):
    bot = SimpleNamespace(close=AsyncMock())
    server = SimpleNamespace(should_exit=False)
    checkpoint = AsyncMock(return_value=checkpoint_result)
    monkeypatch.setattr(
        "bot.voice.checkpoint_voice_sessions_for_shutdown",
        checkpoint,
    )

    await shutdown(bot, server)

    assert bot.close.await_args.kwargs["voice_checkpointed"] is False


@pytest.mark.asyncio
async def test_repeated_raw_signals_share_one_deadline_watchdog_and_task(monkeypatch):
    bot = SimpleNamespace(close=AsyncMock())
    server = SimpleNamespace(should_exit=False)
    checkpoint = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "bot.voice.checkpoint_voice_sessions_for_shutdown",
        checkpoint,
    )
    coordinator = ShutdownCoordinator(bot, server, deadline_seconds=1)
    loop = asyncio.get_running_loop()
    coordinator._loop = loop

    coordinator._handle_signal(15, None)
    deadline = coordinator.deadline
    watchdog = coordinator.watchdog
    coordinator._handle_signal(15, None)
    await asyncio.sleep(0)
    task = coordinator.task
    await asyncio.sleep(0)
    coordinator.request()

    assert coordinator.deadline == deadline
    assert coordinator.watchdog is watchdog
    assert coordinator.task is task
    await task
    coordinator.disarm()


def test_embedded_uvicorn_does_not_replace_the_shared_signal_handler(monkeypatch):
    replace_signal = Mock()
    monkeypatch.setattr("signal.signal", replace_signal)
    server = object.__new__(CoordinatedUvicornServer)

    with server.capture_signals():
        pass

    replace_signal.assert_not_called()
