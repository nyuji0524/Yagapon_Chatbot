"""Shared admission policy for exporting material to the Knowledge repository.

This policy is intentionally independent from Discord command contexts so every
producer (commands, live meetings, recovery, and uploaded recordings) applies
the same fail-closed boundary before content reaches GCS or GitHub.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import discord


@dataclass(frozen=True)
class KnowledgeExportDecision:
    allowed: bool
    reason: str


def _configured_export_ids() -> set[str]:
    return {
        value.strip()
        for value in os.environ.get(
            "YAGAPON_MEETING_EXPORT_CHANNEL_IDS", ""
        ).split(",")
        if value.strip()
    }


def _related_channel_ids(channel, parent=None, category=None) -> set[str]:
    parent = parent or getattr(channel, "parent", None)
    category = (
        category
        or getattr(channel, "category", None)
        or getattr(parent, "category", None)
    )
    return {
        str(value)
        for value in (
            getattr(channel, "id", None),
            getattr(channel, "parent_id", None),
            getattr(channel, "category_id", None),
            getattr(parent, "id", None),
            getattr(parent, "category_id", None),
            getattr(category, "id", None),
        )
        if value is not None
    }


def _guildwide_channel_audience(channel) -> bool:
    private_check = getattr(channel, "is_private", None)
    if callable(private_check):
        try:
            if private_check():
                return False
        except Exception:
            return False
    guild = getattr(channel, "guild", None)
    default_role = getattr(guild, "default_role", None)
    permissions_for = getattr(channel, "permissions_for", None)
    if guild is None or default_role is None or not callable(permissions_for):
        return False
    try:
        permissions = permissions_for(default_role)
    except Exception:
        return False
    if not bool(
        getattr(
            permissions,
            "view_channel",
            getattr(permissions, "read_messages", False),
        )
    ):
        return False
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return bool(getattr(permissions, "connect", False))
    return bool(getattr(permissions, "read_message_history", False))


def evaluate_meeting_export(
    *,
    guild_id: int,
    channel,
    parent=None,
    category=None,
    is_ignored=None,
) -> KnowledgeExportDecision:
    """Prove that an external sink does not widen the Discord audience."""
    if channel is None:
        return KnowledgeExportDecision(False, "channel-unresolved")
    if getattr(channel, "_knowledge_export_lineage_resolved", True) is False:
        return KnowledgeExportDecision(False, "channel-lineage-unresolved")
    parent = parent or getattr(channel, "parent", None)
    if callable(is_ignored):
        related_ids = {
            value
            for value in (
                getattr(channel, "id", None),
                getattr(channel, "parent_id", None),
                getattr(parent, "id", None),
            )
            if value is not None
        }
        if any(is_ignored(guild_id, int(value)) for value in related_ids):
            return KnowledgeExportDecision(False, "ignored-channel")
    if _related_channel_ids(channel, parent=parent, category=category) & (
        _configured_export_ids()
    ):
        return KnowledgeExportDecision(True, "explicit-channel-allowlist")
    if _guildwide_channel_audience(channel):
        return KnowledgeExportDecision(True, "guildwide-channel-audience")
    return KnowledgeExportDecision(False, "channel-not-export-approved")


def knowledge_guild_allowed(guild_id: int) -> bool:
    configured = os.environ.get("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "").strip()
    if not configured:
        configured = os.environ.get("YAGAPON_KNOWLEDGE_GUILD_ID", "").strip()
    if not configured:
        return False
    allowed = {value.strip() for value in configured.split(",") if value.strip()}
    return str(guild_id) in allowed


def protected_channel(channel, *, parent=None, category=None) -> bool:
    channel_ids = {
        value.strip()
        for value in os.environ.get("YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS", "").split(",")
        if value.strip()
    }
    parent = parent or getattr(channel, "parent", None)
    category = (
        category
        or getattr(channel, "category", None)
        or getattr(parent, "category", None)
    )
    related = [channel, parent, category]
    related_ids = {
        str(value)
        for value in (
            getattr(channel, "id", None),
            getattr(channel, "parent_id", None),
            getattr(channel, "category_id", None),
            getattr(parent, "id", None),
            getattr(parent, "category_id", None),
            getattr(category, "id", None),
        )
        if value is not None
    }
    if related_ids & channel_ids:
        return True
    protected_terms = {
        value.strip().casefold()
        for value in os.environ.get(
            "YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", "選考"
        ).split(",")
        if value.strip()
    }
    return any(
        term in str(getattr(item, "name", "")).casefold()
        for item in related
        if item
        for term in protected_terms
    )


def evaluate_knowledge_export(
    *,
    guild_id: int,
    channel,
    parent=None,
    category=None,
    is_ignored=None,
) -> KnowledgeExportDecision:
    """Return a fail-closed decision for a Knowledge export candidate."""
    if not knowledge_guild_allowed(guild_id):
        return KnowledgeExportDecision(False, "guild-not-allowlisted")
    if protected_channel(channel, parent=parent, category=category):
        return KnowledgeExportDecision(False, "protected-channel")
    audience = evaluate_meeting_export(
        guild_id=guild_id,
        channel=channel,
        parent=parent,
        category=category,
        is_ignored=is_ignored,
    )
    if not audience.allowed:
        return audience
    return KnowledgeExportDecision(True, "allowed")
