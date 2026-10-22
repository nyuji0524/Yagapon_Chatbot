from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.client import YagaPon


def _context(*, responded: bool):
    return SimpleNamespace(
        command=SimpleNamespace(qualified_name="minutes create"),
        guild_id=123,
        interaction=SimpleNamespace(
            response=SimpleNamespace(is_done=lambda: responded),
        ),
        respond=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_application_command_error_responds_ephemerally_without_error_details():
    bot = object.__new__(YagaPon)
    bot.operations = SimpleNamespace(record_event=Mock())
    ctx = _context(responded=False)

    await YagaPon.on_application_command_error(
        bot,
        ctx,
        RuntimeError("credential=do-not-expose"),
    )

    ctx.respond.assert_awaited_once()
    content = ctx.respond.await_args.args[0]
    assert ctx.respond.await_args.kwargs == {"ephemeral": True}
    assert "do-not-expose" not in content
    ctx.followup.send.assert_not_awaited()
    bot.operations.record_event.assert_called_once_with(
        "discord_command",
        "application_command",
        "failed",
        guild_id=123,
        item_key="minutes create",
        details={
            "error_type": "RuntimeError",
            "responded_before_error": False,
        },
    )


@pytest.mark.asyncio
async def test_application_command_error_uses_followup_after_defer_or_response():
    bot = object.__new__(YagaPon)
    bot.operations = SimpleNamespace(record_event=Mock())
    ctx = _context(responded=True)

    await YagaPon.on_application_command_error(bot, ctx, ValueError("private value"))

    ctx.respond.assert_not_awaited()
    ctx.followup.send.assert_awaited_once()
    assert ctx.followup.send.await_args.kwargs == {"ephemeral": True}
    assert "private value" not in ctx.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_application_command_error_does_not_mask_original_when_ack_fails():
    bot = object.__new__(YagaPon)
    bot.operations = SimpleNamespace(record_event=Mock())
    ctx = _context(responded=True)
    ctx.followup.send.side_effect = RuntimeError("interaction expired")

    await YagaPon.on_application_command_error(bot, ctx, RuntimeError("original"))

    ctx.followup.send.assert_awaited_once()
