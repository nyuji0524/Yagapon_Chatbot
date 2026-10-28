from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.discord_visibility import (
    channel_audience_has_guildwide_rag_access,
    readable_channel_ids,
    resolve_minutes_notification_channel,
)


class FakeChannel:
    def __init__(self, channel_id, *, private=False, members=()):
        self.id = channel_id
        self._private = private
        self.members = list(members)

    def permissions_for(self, _member):
        return SimpleNamespace(view_channel=True, read_message_history=True)

    def is_private(self):
        return self._private


def test_private_thread_requires_cached_membership_unless_it_is_current():
    member = SimpleNamespace(id=10)
    public = FakeChannel(1)
    private_other = FakeChannel(2, private=True, members=[SimpleNamespace(id=20)])
    private_joined = FakeChannel(3, private=True, members=[member])
    guild = SimpleNamespace(
        channels=[public],
        threads=[private_other, private_joined],
    )

    assert readable_channel_ids(guild, member) == (1, 3)
    assert readable_channel_ids(
        guild,
        member,
        current_channel=private_other,
    ) == (2, 1, 3)


def test_missing_member_fails_closed():
    guild = SimpleNamespace(channels=[FakeChannel(1)], threads=[])

    assert readable_channel_ids(guild, None) == ()


def test_current_channel_without_history_permission_is_not_allowed():
    member = SimpleNamespace(id=10)
    channel = FakeChannel(1)
    channel.permissions_for = lambda _member: SimpleNamespace(
        view_channel=True,
        read_message_history=False,
    )
    guild = SimpleNamespace(channels=[channel], threads=[])

    assert readable_channel_ids(
        guild,
        member,
        current_channel=channel,
        limit=1,
    ) == ()


def test_admin_only_channel_can_use_guildwide_rag_scope():
    owner = SimpleNamespace(
        id=10,
        bot=False,
        guild_permissions=SimpleNamespace(administrator=True),
    )
    administrator = SimpleNamespace(
        id=20,
        bot=False,
        guild_permissions=SimpleNamespace(administrator=True),
    )
    bot_member = SimpleNamespace(id=30, bot=True)
    channel = FakeChannel(1, members=[owner, administrator, bot_member])
    guild = SimpleNamespace(
        owner_id=owner.id,
        chunked=True,
        channels=[channel],
        members=[owner, administrator, bot_member],
    )

    assert channel_audience_has_guildwide_rag_access(guild, channel) is True


def test_public_or_incompletely_cached_channel_cannot_widen_rag_scope():
    administrator = SimpleNamespace(
        id=10,
        bot=False,
        guild_permissions=SimpleNamespace(administrator=True),
    )
    regular_member = SimpleNamespace(
        id=20,
        bot=False,
        guild_permissions=SimpleNamespace(administrator=False),
    )
    # VoiceChannel.members may contain only the connected administrator even
    # though a regular guild member can read its built-in chat.
    channel = FakeChannel(1, members=[administrator])
    guild = SimpleNamespace(
        owner_id=999,
        chunked=True,
        channels=[channel],
        members=[administrator, regular_member],
    )

    assert channel_audience_has_guildwide_rag_access(guild, channel) is False
    guild.chunked = False
    guild.members = [administrator]
    assert channel_audience_has_guildwide_rag_access(guild, channel) is False


def test_minutes_destination_never_falls_back_to_unrelated_public_channel():
    source = SimpleNamespace(id=10, send=AsyncMock())
    public = SimpleNamespace(id=20, send=AsyncMock())
    source.permissions_for = lambda _member: SimpleNamespace(
        view_channel=False,
        send_messages=False,
    )
    public.permissions_for = lambda _member: SimpleNamespace(
        view_channel=True,
        send_messages=True,
    )
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(id=999),
        system_channel=public,
        text_channels=[public],
        get_channel=lambda channel_id: {10: source, 20: public}.get(channel_id),
    )
    config = SimpleNamespace(get_minutes_channel=lambda _guild_id: None)

    assert resolve_minutes_notification_channel(config, guild, source) is None


def test_configured_minutes_channel_never_widens_private_voice_audience():
    source = SimpleNamespace(id=10, send=AsyncMock())
    configured = SimpleNamespace(id=20, send=AsyncMock())
    source.permissions_for = lambda _member: SimpleNamespace(
        view_channel=True,
        send_messages=True,
    )
    configured.permissions_for = lambda _member: SimpleNamespace(
        view_channel=True,
        send_messages=True,
    )
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(id=999),
        get_channel=lambda channel_id: {10: source, 20: configured}.get(channel_id),
    )
    config = SimpleNamespace(get_minutes_channel=lambda _guild_id: 20)

    assert resolve_minutes_notification_channel(config, guild, source) is source
