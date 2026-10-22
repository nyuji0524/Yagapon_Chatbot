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
