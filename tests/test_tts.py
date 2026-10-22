import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.tts import speak_in_vc


def _message(guild, channel):
    member = SimpleNamespace(voice=SimpleNamespace(channel=channel))
    guild.get_member = lambda _member_id: member
    return SimpleNamespace(
        guild=guild,
        author=SimpleNamespace(id=42),
        channel=channel,
    )


@pytest.mark.asyncio
async def test_text_rag_tts_never_relays_private_text_answer_to_other_voice_channel(
    monkeypatch,
):
    voice_channel = SimpleNamespace(id=10)
    private_text_channel = SimpleNamespace(id=20)
    guild = SimpleNamespace(id=1)
    message = _message(guild, voice_channel)
    message.channel = private_text_channel
    session = SimpleNamespace(
        channel=voice_channel,
        voice_client=SimpleNamespace(is_connected=Mock(return_value=True)),
        speak=AsyncMock(),
    )
    monkeypatch.setattr("bot.voice.get_session", lambda _guild_id: session)

    await speak_in_vc(SimpleNamespace(voice_clients=[]), message, "役員向け回答だぽん")

    session.speak.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_rag_tts_delegates_to_managed_voice_session(monkeypatch):
    channel = SimpleNamespace(id=10)
    guild = SimpleNamespace(id=1)
    message = _message(guild, channel)
    voice_client = SimpleNamespace(
        is_connected=Mock(return_value=True),
        disconnect=AsyncMock(),
    )
    session = SimpleNamespace(
        channel=channel,
        voice_client=voice_client,
        speak=AsyncMock(),
    )
    monkeypatch.setattr("bot.voice.get_session", lambda _guild_id: session)

    await speak_in_vc(SimpleNamespace(voice_clients=[voice_client]), message, "回答だぽん")

    session.speak.assert_awaited_once_with("回答だぽん")
    voice_client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_rag_tts_does_not_move_managed_session_in_another_channel(monkeypatch):
    member_channel = SimpleNamespace(id=10)
    session_channel = SimpleNamespace(id=20)
    guild = SimpleNamespace(id=1)
    message = _message(guild, member_channel)
    voice_client = SimpleNamespace(
        is_connected=Mock(return_value=True),
        disconnect=AsyncMock(),
    )
    session = SimpleNamespace(
        channel=session_channel,
        voice_client=voice_client,
        speak=AsyncMock(),
    )
    monkeypatch.setattr("bot.voice.get_session", lambda _guild_id: session)

    await speak_in_vc(SimpleNamespace(voice_clients=[voice_client]), message, "回答だぽん")

    session.speak.assert_not_awaited()
    voice_client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_rag_tts_does_not_disconnect_borrowed_voice_client(monkeypatch):
    channel = SimpleNamespace(id=10)
    guild = SimpleNamespace(id=1)
    message = _message(guild, channel)
    voice_client = SimpleNamespace(
        guild=guild,
        channel=channel,
        is_playing=Mock(side_effect=[False, False]),
        play=Mock(),
        disconnect=AsyncMock(),
    )

    class FakeCommunicate:
        def __init__(self, *_args, **_kwargs):
            pass

        async def save(self, _path):
            return None

    monkeypatch.setattr("bot.voice.get_session", lambda _guild_id: None)
    monkeypatch.setitem(
        sys.modules,
        "edge_tts",
        SimpleNamespace(Communicate=FakeCommunicate),
    )
    monkeypatch.setattr("bot.tts.discord.FFmpegPCMAudio", lambda _path: object())

    await speak_in_vc(SimpleNamespace(voice_clients=[voice_client]), message, "回答だぽん")

    voice_client.play.assert_called_once()
    voice_client.disconnect.assert_not_awaited()
