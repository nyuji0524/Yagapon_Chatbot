from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import reactions


@pytest.fixture(autouse=True)
def reset_reaction_globals():
    reactions._buffer = []
    reactions._flush_task = None
    reactions._client = None
    yield
    reactions._buffer = []
    reactions._flush_task = None
    reactions._client = None


def fake_bot():
    return SimpleNamespace(
        config=SimpleNamespace(
            get_reactions=lambda _guild_id: {
                "enabled": True,
                "interesting": "💡",
                "surprised": "😲",
                "funny": "😂",
            }
        ),
        get_guild=lambda _guild_id: None,
    )


@pytest.mark.asyncio
async def test_shutdown_flushes_final_partial_reaction_batch():
    generate = AsyncMock(return_value=SimpleNamespace(text='["none"]'))
    reactions._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    reactions._buffer.append(
        reactions.PendingMessage(
            message_id=1,
            channel_id=2,
            guild_id=3,
            content="最後の一件もタイマー待ちのまま失わないメッセージです",
        )
    )

    await reactions.shutdown_reactions(fake_bot())

    generate.assert_awaited_once()
    assert reactions._buffer == []


@pytest.mark.asyncio
async def test_failed_reaction_flush_requeues_batch_for_retry():
    generate = AsyncMock(side_effect=RuntimeError("temporary quota error"))
    reactions._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    pending = reactions.PendingMessage(1, 2, 3, "失敗しても消えてはいけない十分に長いメッセージです")
    reactions._buffer.append(pending)

    flushed = await reactions._flush_reactions(fake_bot())

    assert flushed is False
    assert reactions._buffer == [pending]


@pytest.mark.asyncio
async def test_reaction_flush_is_deferred_during_recording(monkeypatch):
    generate = AsyncMock(return_value=SimpleNamespace(text='["none"]'))
    reactions._client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    )
    pending = reactions.PendingMessage(1, 2, 3, "録音中は処理しない十分に長いメッセージです")
    reactions._buffer.append(pending)
    monkeypatch.setattr("bot.reactions.recording_is_active", lambda: True)

    flushed = await reactions._flush_reactions(fake_bot())

    assert flushed is False
    assert reactions._buffer == [pending]
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_optional_ai_reactions_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("YAGAPON_AI_REACTIONS_ENABLED", raising=False)
    bot = fake_bot()
    bot.config.get_reactions = lambda _guild_id: {"enabled": True}
    message = SimpleNamespace(
        id=1,
        content="Geminiを呼ばずに無視される十分に長い通常メッセージです",
        guild=SimpleNamespace(id=3),
        channel=SimpleNamespace(id=2),
    )

    await reactions.maybe_react(bot, message)

    assert reactions._buffer == []
    assert reactions._flush_task is None
