from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from main import shutdown


@pytest.mark.asyncio
async def test_shutdown_delegates_resource_cleanup_to_bot_once():
    bot = SimpleNamespace(
        close=AsyncMock(),
        corpus=SimpleNamespace(shutdown=AsyncMock()),
    )
    server = SimpleNamespace(should_exit=False)

    await shutdown(bot, server)

    assert server.should_exit is True
    bot.close.assert_awaited_once_with()
    bot.corpus.shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_drains_api_backfills_before_closing_bot(monkeypatch):
    bot = SimpleNamespace(close=AsyncMock())
    app = object()
    server = SimpleNamespace(
        should_exit=False,
        config=SimpleNamespace(app=app),
    )
    drain = AsyncMock()
    monkeypatch.setattr("api.server.shutdown_backfill_tasks", drain)

    await shutdown(bot, server)

    drain.assert_awaited_once_with(app)
    bot.close.assert_awaited_once_with()
