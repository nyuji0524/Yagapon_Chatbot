from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.commands.backfill import BackfillStatus


@pytest.mark.asyncio
async def test_status_failure_does_not_escape_and_final_message_falls_back():
    expired_message = SimpleNamespace(edit=AsyncMock(side_effect=RuntimeError("expired webhook")))
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace()))
    status = BackfillStatus(channel, expired_message)

    assert await status.update("進捗") is False
    await status.finish("完了")

    channel.send.assert_awaited_once_with("完了", silent=True)
