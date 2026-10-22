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
