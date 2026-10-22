from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.client import YagaPon
from bot.commands import voice_cmd
from bot.voice import VoiceMode


def _automatic_leave_context():
    channel = SimpleNamespace(
        id=10,
        name="会議用",
        category=SimpleNamespace(name="会議"),
        members=[],
    )
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(id=999),
        system_channel=None,
        text_channels=[],
        get_channel=lambda _channel_id: None,
    )
    member = SimpleNamespace(id=42, guild=guild)
    session = SimpleNamespace(
        channel=channel,
        voice_client=SimpleNamespace(is_connected=Mock(return_value=True)),
        mode=VoiceMode.LISTEN,
        recording_sink=SimpleNamespace(mark=Mock()),
        transcript_text="[中山]: 確認します",
        started_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
        session_id="20261022T000000Z-test",
        claim_delivery=Mock(return_value=True),
        external_export_decision=Mock(return_value=SimpleNamespace(allowed=True, reason="test")),
        publish_knowledge=AsyncMock(return_value="https://github.example/pr/1"),
        checkpoint_delivery_progress=Mock(),
        transcript_path=None,
        mark_delivered=Mock(),
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        config=SimpleNamespace(get_minutes_channel=lambda _guild_id: None),
    )
    before = SimpleNamespace(channel=channel)
    after = SimpleNamespace(channel=None)
    return bot, member, before, after, session


@pytest.mark.asyncio
async def test_automatic_leave_delivers_minutes_without_notification_channel(monkeypatch):
    bot, member, before, after, session = _automatic_leave_context()
    leave = AsyncMock(return_value="# 議事録")
    upload_transcript = AsyncMock(return_value="https://docs.example/transcript")
    upload_minutes = AsyncMock(return_value="https://docs.example/minutes")
    summarize = AsyncMock(return_value="要約だぽん")
    index = AsyncMock()
    monkeypatch.setattr("bot.voice.get_session", lambda _guild_id: session)
    monkeypatch.setattr("bot.voice.leave_voice", leave)
    monkeypatch.setattr("bot.gdrive.upload_transcript", upload_transcript)
    monkeypatch.setattr("bot.gdrive.upload_minutes", upload_minutes)
    monkeypatch.setattr("bot.commands.voice_cmd._summarize_minutes", summarize)
    monkeypatch.setattr("bot.meeting_index.index_bot_meeting", index)

    await YagaPon.on_voice_state_update(bot, member, before, after)

    leave.assert_awaited_once_with(1)
    upload_transcript.assert_awaited_once()
    upload_minutes.assert_awaited_once()
    assert upload_transcript.await_args.kwargs["session_id"] == session.session_id
    assert upload_minutes.await_args.kwargs["session_id"] == session.session_id
    session.publish_knowledge.assert_awaited_once_with(
        summary="要約だぽん",
        minutes="# 議事録",
    )
    index.assert_awaited_once()
    session.mark_delivered.assert_called_once_with(
        drive_url="https://docs.example/minutes",
        transcript_url="https://docs.example/transcript",
    )


@pytest.mark.asyncio
async def test_restricted_meeting_notification_distinguishes_policy_skip_from_drive_failure(monkeypatch):
    bot, member, before, after, session = _automatic_leave_context()
    channel = session.channel
    channel.send = AsyncMock()
    channel.permissions_for = Mock(
        return_value=SimpleNamespace(
            view_channel=True,
            send_messages=True,
            send_messages_in_threads=False,
        )
    )
    member.guild.get_channel = lambda channel_id: channel if channel_id == channel.id else None
    session.external_export_decision = Mock(return_value=SimpleNamespace(allowed=False, reason="restricted-channel"))
    session.publish_knowledge = AsyncMock(return_value=None)
    upload_transcript = AsyncMock()
    upload_minutes = AsyncMock()
    monkeypatch.setattr("bot.voice.get_session", lambda _guild_id: session)
    monkeypatch.setattr("bot.voice.leave_voice", AsyncMock(return_value="# 議事録"))
    monkeypatch.setattr("bot.gdrive.upload_transcript", upload_transcript)
    monkeypatch.setattr("bot.gdrive.upload_minutes", upload_minutes)
    monkeypatch.setattr(
        "bot.commands.voice_cmd._summarize_minutes",
        AsyncMock(return_value="要約だぽん"),
    )
    monkeypatch.setattr("bot.meeting_index.index_bot_meeting", AsyncMock())

    await YagaPon.on_voice_state_update(bot, member, before, after)

    upload_transcript.assert_not_awaited()
    upload_minutes.assert_not_awaited()
    sent = channel.send.await_args.kwargs
    assert sent["embed"].footer.text == "🔒 限定チャンネルのため、外部Driveへの保存を行わず添付します"
    assert len(sent["files"]) == 2


@pytest.mark.asyncio
async def test_automatic_leave_retains_recording_when_delivery_fails(monkeypatch):
    bot, member, before, after, session = _automatic_leave_context()
    monkeypatch.setattr("bot.voice.get_session", lambda _guild_id: session)
    monkeypatch.setattr("bot.voice.leave_voice", AsyncMock(return_value="# 議事録"))
    monkeypatch.setattr(
        "bot.gdrive.upload_transcript",
        AsyncMock(return_value="https://docs.example/transcript"),
    )
    monkeypatch.setattr(
        "bot.gdrive.upload_minutes",
        AsyncMock(return_value="https://docs.example/minutes"),
    )
    monkeypatch.setattr(
        "bot.commands.voice_cmd._summarize_minutes",
        AsyncMock(return_value="要約だぽん"),
    )
    monkeypatch.setattr(
        "bot.meeting_index.index_bot_meeting",
        AsyncMock(side_effect=RuntimeError("index unavailable")),
    )

    await YagaPon.on_voice_state_update(bot, member, before, after)

    session.mark_delivered.assert_not_called()
    session.recording_sink.mark.assert_called_once_with(
        "automatic_leave_failed",
        stage="delivery",
        error="RuntimeError",
    )


@pytest.mark.asyncio
async def test_automatic_listen_leave_does_not_deliver_empty_minutes(monkeypatch):
    bot, member, before, after, session = _automatic_leave_context()
    monkeypatch.setattr("bot.voice.get_session", lambda _guild_id: session)
    monkeypatch.setattr("bot.voice.leave_voice", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "bot.gdrive.upload_transcript",
        AsyncMock(return_value="https://docs.example/transcript"),
    )

    await YagaPon.on_voice_state_update(bot, member, before, after)

    session.mark_delivered.assert_not_called()
    session.recording_sink.mark.assert_called_once_with(
        "automatic_leave_failed",
        stage="delivery",
        error="RuntimeError",
    )


@pytest.mark.asyncio
async def test_bot_move_stops_capture_and_never_publishes_under_old_channel(monkeypatch):
    bot, _member, before, _after, session = _automatic_leave_context()
    member = SimpleNamespace(id=999, guild=_member.guild)
    moved_to = SimpleNamespace(id=99, name="別のVC")
    session._capture_interrupted = False
    session._transcription_failed = False
    session.is_active = True
    monkeypatch.setattr("bot.voice.get_session", lambda _guild_id: session)
    monkeypatch.setattr(
        "bot.voice.leave_voice",
        AsyncMock(side_effect=RuntimeError("capture interrupted")),
    )

    await YagaPon.on_voice_state_update(
        bot,
        member,
        before,
        SimpleNamespace(channel=moved_to),
    )

    assert session._capture_interrupted is True
    assert session.is_active is False
    session.publish_knowledge.assert_not_awaited()
    assert session.recording_sink.mark.call_args_list[0].args == ("capture_interrupted",)
    assert session.recording_sink.mark.call_args_list[-1].args == ("capture_interrupted",)


@pytest.mark.asyncio
async def test_intentional_bot_disconnect_is_not_capture_interruption(monkeypatch):
    bot, _member, before, after, session = _automatic_leave_context()
    member = SimpleNamespace(id=999, guild=_member.guild)
    session._capture_interrupted = False
    session._intentional_stop = True
    session.is_active = False
    monkeypatch.setattr("bot.voice.get_session", lambda _guild_id: session)

    await YagaPon.on_voice_state_update(bot, member, before, after)

    assert session._capture_interrupted is False
    session.recording_sink.mark.assert_not_called()
    session.claim_delivery.assert_not_called()


def _registered_voice_commands():
    commands = {}

    class Bot:
        config = SimpleNamespace()

        def slash_command(self, *, name, **_kwargs):
            def decorate(function):
                commands[name] = function
                return function

            return decorate

    voice_cmd.register(Bot())
    return commands


@pytest.mark.asyncio
async def test_leave_rejects_member_outside_recorded_voice_channel(monkeypatch):
    leave = _registered_voice_commands()["leave"]
    member = SimpleNamespace(
        id=42,
        voice=SimpleNamespace(channel=SimpleNamespace(id=11)),
        guild_permissions=SimpleNamespace(administrator=False),
    )
    guild = SimpleNamespace(id=1, owner_id=99, get_member=lambda _user_id: member)
    ctx = SimpleNamespace(
        guild_id=1,
        guild=guild,
        author=member,
        defer=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    session = SimpleNamespace(channel=SimpleNamespace(id=10), claim_delivery=Mock())
    monkeypatch.setattr(voice_cmd, "require_registered_member", AsyncMock(return_value=True))
    monkeypatch.setattr(voice_cmd, "get_session", lambda _guild_id: session)

    await leave(ctx)

    ctx.defer.assert_awaited_once_with(ephemeral=True)
    session.claim_delivery.assert_not_called()
    assert ctx.followup.send.await_args.kwargs["ephemeral"] is True
    assert "参加者かサーバー管理者" in ctx.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_leave_allows_recorded_voice_channel_member_to_claim(monkeypatch):
    leave = _registered_voice_commands()["leave"]
    channel = SimpleNamespace(id=10)
    member = SimpleNamespace(
        id=42,
        voice=SimpleNamespace(channel=channel),
        guild_permissions=SimpleNamespace(administrator=False),
    )
    guild = SimpleNamespace(id=1, owner_id=99, get_member=lambda _user_id: member)
    ctx = SimpleNamespace(
        guild_id=1,
        guild=guild,
        author=member,
        defer=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    session = SimpleNamespace(channel=channel, claim_delivery=Mock(return_value=False))
    monkeypatch.setattr(voice_cmd, "require_registered_member", AsyncMock(return_value=True))
    monkeypatch.setattr(voice_cmd, "get_session", lambda _guild_id: session)

    await leave(ctx)

    session.claim_delivery.assert_called_once_with()
    assert ctx.followup.send.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_manual_leave_preserves_capture_interrupted_terminal_status(monkeypatch):
    leave = _registered_voice_commands()["leave"]
    channel = SimpleNamespace(id=10, name="会議用", category=None)
    member = SimpleNamespace(
        id=42,
        voice=SimpleNamespace(channel=channel),
        guild_permissions=SimpleNamespace(administrator=False),
    )
    guild = SimpleNamespace(id=1, owner_id=99, get_member=lambda _user_id: member)
    ctx = SimpleNamespace(
        guild_id=1,
        guild=guild,
        author=member,
        defer=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    sink = SimpleNamespace(mark=Mock())
    session = SimpleNamespace(
        channel=channel,
        mode=VoiceMode.LISTEN,
        claim_delivery=Mock(return_value=True),
        recording_sink=sink,
        _capture_interrupted=True,
    )
    monkeypatch.setattr(voice_cmd, "require_registered_member", AsyncMock(return_value=True))
    monkeypatch.setattr(voice_cmd, "get_session", lambda _guild_id: session)
    monkeypatch.setattr(
        voice_cmd,
        "leave_voice",
        AsyncMock(side_effect=RuntimeError("capture interrupted")),
    )

    await leave(ctx)

    sink.mark.assert_called_once_with("capture_interrupted", error="RuntimeError")
