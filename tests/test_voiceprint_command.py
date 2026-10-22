import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.commands import voiceprint


def _register_command(bot):
    commands = {}

    class Group:
        def command(self, *, name, **_kwargs):
            def decorate(function):
                commands[name] = function
                return function

            return decorate

    bot.create_group = Mock(return_value=Group())
    voiceprint.register(bot)
    return commands["register"]


def test_voiceprint_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(voiceprint, "VOICEPRINT_DIR", str(tmp_path))
    path = voiceprint._get_voiceprint_path(1, 42)

    voiceprint._save_voiceprint(path, b"voice")

    assert stat.S_IMODE(tmp_path.joinpath("1").stat().st_mode) == 0o700
    assert stat.S_IMODE(tmp_path.joinpath("1", "42.wav").stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_voiceprint_rejects_during_meeting_recording(monkeypatch):
    bot = SimpleNamespace(config=SimpleNamespace(), voice_clients=[])
    command = _register_command(bot)
    author = SimpleNamespace(id=42)
    ctx = SimpleNamespace(
        guild_id=1,
        author=author,
        respond=AsyncMock(),
    )
    monkeypatch.setattr(voiceprint, "can_manage_member", lambda _ctx, _target: True)
    monkeypatch.setattr("bot.voice.get_session", lambda _guild_id: object())

    await command(ctx)

    ctx.respond.assert_awaited_once()
    assert ctx.respond.await_args.kwargs["ephemeral"] is True
    assert "会議の録音中" in ctx.respond.await_args.args[0]


@pytest.mark.asyncio
async def test_voiceprint_does_not_move_or_reuse_busy_voice_client(monkeypatch):
    target_channel = SimpleNamespace(id=10)
    member = SimpleNamespace(
        id=42,
        display_name="委員A",
        voice=SimpleNamespace(channel=target_channel),
    )
    guild = SimpleNamespace(id=1, get_member=lambda _user_id: member)
    existing = SimpleNamespace(
        guild=guild,
        channel=SimpleNamespace(id=11),
        is_recording=Mock(return_value=True),
        is_playing=Mock(return_value=False),
        move_to=AsyncMock(),
    )
    config = SimpleNamespace(get_members=lambda _guild_id: {"42": {"name": "委員A"}})
    bot = SimpleNamespace(config=config, voice_clients=[existing])
    command = _register_command(bot)
    ctx = SimpleNamespace(
        guild_id=1,
        guild=guild,
        author=member,
        respond=AsyncMock(),
        defer=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    monkeypatch.setattr(voiceprint, "can_manage_member", lambda _ctx, _target: True)
    monkeypatch.setattr("bot.voice.get_session", lambda _guild_id: None)

    await command(ctx)

    existing.move_to.assert_not_awaited()
    assert ctx.followup.send.await_args.kwargs["ephemeral"] is True
    assert 1 not in voiceprint._active_registrations
