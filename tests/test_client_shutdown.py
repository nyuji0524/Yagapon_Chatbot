import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.client import YagaPon


@pytest.mark.asyncio
async def test_close_flushes_corpus_when_voice_checkpoint_fails(monkeypatch):
    bot = object.__new__(YagaPon)
    bot._close_lock = asyncio.Lock()
    bot._close_completed = False
    bot._recording_recovery_task = None
    bot.recording_replays = SimpleNamespace(shutdown=AsyncMock())
    bot.scheduled_backfill = SimpleNamespace(stop=AsyncMock())
    bot.knowledge_refresh_tasks = set()
    bot.corpus = SimpleNamespace(shutdown=AsyncMock())
    bot._post_durable_cleanup = Mock()
    checkpoint = AsyncMock(side_effect=RuntimeError("checkpoint failed"))
    shutdown_reactions = AsyncMock()
    discord_close = AsyncMock()
    monkeypatch.setattr(
        "bot.voice.checkpoint_voice_sessions_for_shutdown",
        checkpoint,
    )
    monkeypatch.setattr("bot.reactions.shutdown_reactions", shutdown_reactions)
    monkeypatch.setattr(YagaPon, "_close_discord_transport", discord_close)

    await YagaPon.close(bot)

    checkpoint.assert_awaited_once()
    assert 0 < checkpoint.await_args.kwargs["timeout_seconds"] <= 40
    shutdown_reactions.assert_awaited_once_with(bot)
    discord_close.assert_awaited_once_with()
    bot.corpus.shutdown.assert_awaited_once()
    assert bot.corpus.shutdown.await_args.kwargs["timeout_seconds"] > 0
    assert bot._close_completed is True
    bot._post_durable_cleanup.assert_called_once_with()


@pytest.mark.asyncio
async def test_close_enforces_one_end_to_end_shutdown_deadline(monkeypatch):
    bot = object.__new__(YagaPon)
    bot._close_lock = asyncio.Lock()
    bot._close_completed = False
    bot._recording_recovery_task = None
    bot.recording_replays = SimpleNamespace(shutdown=AsyncMock())
    bot.scheduled_backfill = SimpleNamespace(stop=AsyncMock())
    bot.knowledge_refresh_tasks = set()
    bot.corpus = SimpleNamespace(shutdown=AsyncMock())
    entered = asyncio.Event()

    async def stuck_checkpoint(*, timeout_seconds):
        assert timeout_seconds <= 0.15
        entered.set()
        await asyncio.Event().wait()

    shutdown_reactions = AsyncMock()
    discord_close = AsyncMock()
    monkeypatch.setenv("YAGAPON_SHUTDOWN_DEADLINE_SECONDS", "0.15")
    monkeypatch.setattr(
        "bot.voice.checkpoint_voice_sessions_for_shutdown",
        stuck_checkpoint,
    )
    monkeypatch.setattr("bot.reactions.shutdown_reactions", shutdown_reactions)
    monkeypatch.setattr(YagaPon, "_close_discord_transport", discord_close)

    started = time.monotonic()
    await YagaPon.close(bot)
    elapsed = time.monotonic() - started

    assert entered.is_set()
    assert elapsed < 0.5
    assert bot._close_completed is True
    shutdown_reactions.assert_not_awaited()
    discord_close.assert_not_awaited()
    bot.corpus.shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_transport_is_marked_closed_before_http_session(monkeypatch):
    bot = object.__new__(YagaPon)
    bot._closed = False
    order = []

    async def disconnect_voice(*, force):
        assert force is True
        order.append("voice")

    async def close_websocket(*, code):
        assert code == 1000
        order.append("websocket")

    bot._connection = SimpleNamespace(
        voice_clients=[SimpleNamespace(disconnect=disconnect_voice)]
    )
    bot.ws = SimpleNamespace(open=True, close=close_websocket)
    bot._ready = SimpleNamespace(clear=Mock())

    async def close_http():
        assert bot._closed is True
        order.append("http")

    bot.http = SimpleNamespace(close=close_http)

    await YagaPon._close_discord_transport(bot)

    assert bot._closed is True
    assert order == ["voice", "websocket", "http"]
    bot._ready.clear.assert_called_once_with()
