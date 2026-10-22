import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot.client import YagaPon


@pytest.mark.asyncio
async def test_close_flushes_corpus_when_voice_checkpoint_fails(monkeypatch):
    bot = object.__new__(YagaPon)
    bot._close_lock = asyncio.Lock()
    bot._close_completed = False
    bot._recording_recovery_task = None
    bot.corpus = SimpleNamespace(shutdown=AsyncMock())
    checkpoint = AsyncMock(side_effect=RuntimeError("checkpoint failed"))
    discord_close = AsyncMock()
    monkeypatch.setattr(
        "bot.voice.checkpoint_voice_sessions_for_shutdown",
        checkpoint,
    )
    monkeypatch.setattr(discord.Bot, "close", discord_close)

    await YagaPon.close(bot)

    checkpoint.assert_awaited_once_with()
    discord_close.assert_awaited_once_with()
    bot.corpus.shutdown.assert_awaited_once_with()
    assert bot._close_completed is True
