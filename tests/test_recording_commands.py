from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.commands.recording import _require_operator


@pytest.mark.asyncio
async def test_recording_review_requires_bot_owner_even_for_guild_admin(monkeypatch):
    monkeypatch.setattr(
        "bot.commands.recording.require_guild_admin",
        AsyncMock(return_value=True),
    )
    ctx = SimpleNamespace(author=object(), respond=AsyncMock())
    bot = SimpleNamespace(is_owner=AsyncMock(return_value=False))

    allowed = await _require_operator(ctx, bot)

    assert allowed is False
    ctx.respond.assert_awaited_once()


@pytest.mark.asyncio
async def test_recording_review_accepts_guild_admin_who_is_bot_owner(monkeypatch):
    monkeypatch.setattr(
        "bot.commands.recording.require_guild_admin",
        AsyncMock(return_value=True),
    )
    ctx = SimpleNamespace(author=object(), respond=AsyncMock())
    bot = SimpleNamespace(is_owner=AsyncMock(return_value=True))

    allowed = await _require_operator(ctx, bot)

    assert allowed is True
    ctx.respond.assert_not_awaited()
