"""Fail-closed Discord channel visibility helpers for RAG retrieval."""

from __future__ import annotations

from collections.abc import Iterable

MAX_RAG_CHANNEL_IDS = 64


def readable_channel_ids(
    guild,
    member,
    *,
    current_channel=None,
    limit: int = MAX_RAG_CHANNEL_IDS,
) -> tuple[int, ...]:
    """Return a bounded allowlist of channels a member can currently read.

    The channel where the user invoked the bot is considered first. Missing
    guild/member state is deliberately not treated as unrestricted access.
    Archived/private threads that Discord has not cached are omitted rather
    than guessed.
    """
    if guild is None or member is None:
        return ()

    candidates: list[object] = []
    if current_channel is not None and getattr(current_channel, "guild", guild) == guild:
        candidates.append(current_channel)
    candidates.extend(getattr(guild, "channels", ()) or ())
    candidates.extend(getattr(guild, "threads", ()) or ())

    allowed: list[int] = []
    seen: set[int] = set()
    for channel in candidates:
        channel_id = getattr(channel, "id", None)
        permissions_for = getattr(channel, "permissions_for", None)
        if channel_id is None or channel_id in seen or not callable(permissions_for):
            continue
        is_current = (
            current_channel is not None
            and int(channel_id) == int(getattr(current_channel, "id", -1))
        )
        is_private_thread = False
        private_check = getattr(channel, "is_private", None)
        if callable(private_check):
            try:
                is_private_thread = bool(private_check())
            except Exception:
                is_private_thread = True
        if is_private_thread and not is_current:
            thread_members = getattr(channel, "members", ()) or ()
            if not any(
                int(getattr(thread_member, "id", -1)) == int(getattr(member, "id", -2))
                for thread_member in thread_members
            ):
                # Pycord inherits parent permissions for threads and does not
                # prove private-thread membership. Cached membership is the
                # minimum evidence required for cross-channel retrieval.
                continue
        try:
            permissions = permissions_for(member)
            can_view = bool(
                getattr(
                    permissions,
                    "view_channel",
                    getattr(permissions, "read_messages", False),
                )
            )
            can_read_history = bool(
                getattr(permissions, "read_message_history", False)
            )
        except Exception:
            continue
        if not (can_view and can_read_history):
            continue
        seen.add(int(channel_id))
        allowed.append(int(channel_id))
        if len(allowed) >= max(1, limit):
            break
    return tuple(allowed)


def normalized_channel_ids(values: Iterable[int] | None) -> tuple[int, ...]:
    """Normalize caller-provided IDs without accepting arbitrary filter text."""
    if values is None:
        return ()
    result = []
    seen = set()
    for value in values:
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            continue
        if channel_id <= 0 or channel_id in seen:
            continue
        seen.add(channel_id)
        result.append(channel_id)
        if len(result) >= MAX_RAG_CHANNEL_IDS:
            break
    return tuple(result)


def has_guildwide_rag_access(guild, member) -> bool:
    """Trust only Discord's resolved owner/Administrator permission."""
    if guild is None or member is None:
        return False
    if int(getattr(guild, "owner_id", 0) or 0) == int(getattr(member, "id", -1)):
        return True
    permissions = getattr(member, "guild_permissions", None)
    return bool(getattr(permissions, "administrator", False))


def channel_audience_has_guildwide_rag_access(guild, channel) -> bool:
    """Return true only when every cached human viewer is an administrator.

    A normal Discord answer is visible to the whole destination channel, not
    only to the requester.  Guild-wide retrieval is therefore safe only for a
    fully cached, non-thread channel whose complete visible audience already
    has Administrator access.  Missing cache or permission evidence fails
    closed to current-channel retrieval.
    """
    if guild is None or channel is None or not bool(getattr(guild, "chunked", False)):
        return False
    if channel not in (getattr(guild, "channels", ()) or ()):
        return False
    members = tuple(getattr(channel, "members", ()) or ())
    human_members = [member for member in members if not bool(getattr(member, "bot", False))]
    return bool(human_members) and all(
        has_guildwide_rag_access(guild, member) for member in human_members
    )


def resolve_minutes_notification_channel(config, guild, source_channel):
    """Choose a writable destination without widening the meeting audience.

    Only the original voice channel's built-in chat is eligible. A configured
    global minutes channel can have a broader audience than an officer/private
    VC, so it is intentionally ignored here. Missing permission state fails
    closed and the invoking user receives an ephemeral fallback instead.
    """

    def writable(channel):
        if channel is None or not callable(getattr(channel, "send", None)):
            return False
        permissions_for = getattr(channel, "permissions_for", None)
        bot_member = getattr(guild, "me", None)
        if not callable(permissions_for) or bot_member is None:
            return False
        try:
            permissions = permissions_for(bot_member)
        except Exception:
            return False
        can_view = bool(
            getattr(
                permissions,
                "view_channel",
                getattr(permissions, "read_messages", False),
            )
        )
        can_send = bool(
            getattr(permissions, "send_messages", False)
            or getattr(permissions, "send_messages_in_threads", False)
        )
        return can_view and can_send

    get_channel = getattr(guild, "get_channel", None)
    source_id = getattr(source_channel, "id", None)
    resolved_source = get_channel(source_id) if source_id and callable(get_channel) else None
    source = resolved_source or source_channel
    if writable(source):
        return source
    return None
