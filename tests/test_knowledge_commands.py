from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.auth.exceptions import RefreshError

from bot.commands.knowledge import (
    _channel_lineage,
    _curation_batch_limit,
    _discord_messages,
    _knowledge_guild_allowed,
    _knowledge_refresh_status,
    _permission_channel,
    _protected_channel,
    _read_drive_source,
    _run_knowledge_refresh,
)
from bot.google_credentials import GoogleCredentialScopeError
from bot.knowledge_policy import evaluate_knowledge_export, evaluate_meeting_export


def test_curation_batch_limit_is_bounded(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_BATCH", "99")
    assert _curation_batch_limit() == 10

    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_BATCH", "invalid")
    assert _curation_batch_limit() == 3


@pytest.mark.asyncio
async def test_full_knowledge_refresh_runs_backfill_before_curation(monkeypatch):
    calls = []

    class Backfill:
        async def run_once(self):
            calls.append("backfill")
            return {"status": "completed", "guilds": {}}

    async def curate():
        calls.append("curation")
        return {
            "enabled": True,
            "checkpoint_ready": True,
            "processed": 2,
            "failed": 0,
        }

    monkeypatch.setattr("bot.commands.knowledge._run_curation_cycle", curate)
    result = await _run_knowledge_refresh(
        SimpleNamespace(scheduled_backfill=Backfill()),
        mode="full",
    )

    assert calls == ["backfill", "curation"]
    assert _knowledge_refresh_status(result) == "completed"


@pytest.mark.asyncio
async def test_full_knowledge_refresh_does_not_curate_failed_checkpoint(monkeypatch):
    class Backfill:
        async def run_once(self):
            return {"status": "failed", "guilds": {}}

    curate = AsyncMock()
    monkeypatch.setattr("bot.commands.knowledge._run_curation_cycle", curate)
    result = await _run_knowledge_refresh(
        SimpleNamespace(scheduled_backfill=Backfill()),
        mode="full",
    )

    curate.assert_not_awaited()
    assert result["curation"]["checkpoint_reason"] == "manual-backfill-not-completed"
    assert _knowledge_refresh_status(result) == "failed"


@pytest.mark.asyncio
async def test_curation_only_refresh_does_not_run_discord_backfill(monkeypatch):
    backfill = AsyncMock()
    curate = AsyncMock(
        return_value={
            "enabled": True,
            "checkpoint_ready": False,
            "checkpoint_reason": "checkpoint-stale",
        }
    )
    monkeypatch.setattr("bot.commands.knowledge._run_curation_cycle", curate)

    result = await _run_knowledge_refresh(
        SimpleNamespace(scheduled_backfill=SimpleNamespace(run_once=backfill)),
        mode="curation",
    )

    backfill.assert_not_awaited()
    curate.assert_awaited_once()
    assert _knowledge_refresh_status(result) == "deferred"


def test_knowledge_guild_allowlist_uses_explicit_list(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "1, 2")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GUILD_ID", "3")

    assert _knowledge_guild_allowed(1) is True
    assert _knowledge_guild_allowed(3) is False


def test_knowledge_guild_allowlist_falls_back_to_index_guild(monkeypatch):
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", raising=False)
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GUILD_ID", "3")

    assert _knowledge_guild_allowed(3) is True
    assert _knowledge_guild_allowed(4) is False


def test_knowledge_guild_allowlist_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", raising=False)
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_GUILD_ID", raising=False)

    assert _knowledge_guild_allowed(1) is False


def test_protected_channel_matches_id_parent_and_default_term(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS", "20")
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", raising=False)
    parent = SimpleNamespace(id=20, name="一般")
    channel = SimpleNamespace(id=21, name="会話", parent=parent)
    assert _protected_channel(channel) is True

    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS", "")
    channel = SimpleNamespace(id=21, name="IT局員選考", parent=None)
    assert _protected_channel(channel) is True


def test_protected_channel_matches_category_for_text_and_thread(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS", "30")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", "選考")
    category = SimpleNamespace(id=30, name="IT局員選考")
    text_channel = SimpleNamespace(id=31, name="応募者対応", parent=category, category=category)
    thread = SimpleNamespace(id=32, name="応募者A", parent=text_channel, category=None)

    assert _protected_channel(text_channel) is True
    assert _protected_channel(thread) is True


def test_protected_channel_matches_uncached_parent_and_category_ids(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS", "30,31")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", "")
    uncached_thread = SimpleNamespace(
        id=32,
        name="応募者A",
        parent=None,
        parent_id=31,
        category=None,
        category_id=30,
    )

    assert _protected_channel(uncached_thread) is True


def test_protected_channel_allows_unmatched_channel(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS", "20")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", "選考,人事")
    channel = SimpleNamespace(id=21, name="開発全般", parent=None)

    assert _protected_channel(channel) is False


def test_shared_export_policy_fails_closed_for_unapproved_or_unresolved_context(
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "1")
    normal = SimpleNamespace(id=10, name="会議", parent=None, category=None)
    unresolved = SimpleNamespace(
        id=10,
        name="会議",
        parent=None,
        category=None,
        _knowledge_export_lineage_resolved=False,
    )

    assert evaluate_knowledge_export(guild_id=2, channel=normal).reason == (
        "guild-not-allowlisted"
    )
    assert evaluate_knowledge_export(guild_id=1, channel=unresolved).reason == (
        "channel-lineage-unresolved"
    )


def test_shared_export_policy_rejects_protected_category_and_ignored_parent(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", "選考")
    category = SimpleNamespace(id=30, name="IT局員選考")
    protected = SimpleNamespace(id=10, name="面接室", parent=None, category=category)
    assert evaluate_knowledge_export(guild_id=1, channel=protected).reason == (
        "protected-channel"
    )

    parent = SimpleNamespace(id=20, name="非公開")
    thread = SimpleNamespace(id=21, name="会議", parent=parent, category=None)

    def ignored(guild_id, channel_id):
        return guild_id == 1 and channel_id == 20

    assert evaluate_knowledge_export(
        guild_id=1,
        channel=thread,
        is_ignored=ignored,
    ).reason == "ignored-channel"


def test_meeting_external_export_requires_same_audience_or_explicit_channel(monkeypatch):
    monkeypatch.delenv("YAGAPON_MEETING_EXPORT_CHANNEL_IDS", raising=False)
    default_role = SimpleNamespace(id=1)
    guild = SimpleNamespace(default_role=default_role)
    private = SimpleNamespace(
        id=10,
        name="役員会",
        guild=guild,
        parent=None,
        category=None,
        permissions_for=lambda _role: SimpleNamespace(
            view_channel=False,
            read_message_history=False,
        ),
    )
    assert evaluate_meeting_export(guild_id=1, channel=private).reason == (
        "channel-not-export-approved"
    )

    monkeypatch.setenv("YAGAPON_MEETING_EXPORT_CHANNEL_IDS", "10")
    assert evaluate_meeting_export(guild_id=1, channel=private).allowed is True

    monkeypatch.setenv("YAGAPON_MEETING_EXPORT_CHANNEL_IDS", "")
    public = SimpleNamespace(
        id=11,
        name="全体会議",
        guild=guild,
        parent=None,
        category=None,
        permissions_for=lambda _role: SimpleNamespace(
            view_channel=True,
            read_message_history=True,
        ),
    )
    assert evaluate_meeting_export(guild_id=1, channel=public).allowed is True


def test_thread_permissions_use_resolved_parent_when_thread_parent_is_uncached():
    parent = SimpleNamespace(id=31)
    thread = SimpleNamespace(id=32, parent=None, parent_id=31)

    assert _permission_channel(thread, parent) is parent


def test_text_channel_permissions_remain_on_the_channel():
    category = SimpleNamespace(id=30)
    channel = SimpleNamespace(id=31, category=category, category_id=30)

    assert _permission_channel(channel, category) is channel


def test_drive_source_falls_back_when_readonly_scope_was_not_granted(monkeypatch):
    calls = []
    document = SimpleNamespace(file_id="doc")

    class FakeReader:
        def __init__(self, _credentials, _drive_id, *, scopes=None):
            calls.append(scopes)
            if scopes is None:
                raise GoogleCredentialScopeError("missing drive.readonly")

        def get_document(self, file_id):
            assert file_id == "doc"
            return document

        def read_text(self, source):
            assert source is document
            return "本文"

    monkeypatch.setattr("bot.commands.knowledge.DriveReader", FakeReader)

    assert _read_drive_source("doc") == (document, "本文", "drive.file")
    assert calls[0] is None
    assert len(calls[1]) == 2


def test_drive_source_falls_back_when_token_refresh_reports_invalid_scope(monkeypatch):
    calls = []
    document = SimpleNamespace(file_id="doc")

    class FakeReader:
        def __init__(self, _credentials, _drive_id, *, scopes=None):
            calls.append(scopes)

        def get_document(self, file_id):
            assert file_id == "doc"
            if len(calls) == 1:
                raise RefreshError("invalid_scope: drive.readonly was not granted")
            return document

        def read_text(self, source):
            assert source is document
            return "本文"

    monkeypatch.setattr("bot.commands.knowledge.DriveReader", FakeReader)

    assert _read_drive_source("doc") == (document, "本文", "drive.file")
    assert calls[0] is None
    assert len(calls[1]) == 2


def test_drive_source_does_not_mask_unrelated_refresh_errors(monkeypatch):
    class FakeReader:
        def __init__(self, _credentials, _drive_id, *, scopes=None):
            pass

        def get_document(self, _file_id):
            raise RefreshError("invalid_grant: token was revoked")

    monkeypatch.setattr("bot.commands.knowledge.DriveReader", FakeReader)

    with pytest.raises(RefreshError, match="invalid_grant"):
        _read_drive_source("doc")


@pytest.mark.asyncio
async def test_discord_messages_keeps_empty_selected_message_without_oldest_first():
    selected = SimpleNamespace(
        id=1,
        created_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
        content="",
        attachments=[],
        author=SimpleNamespace(id=2, display_name="委員"),
        jump_url="https://discord.com/channels/1/2/1",
    )

    class FakeChannel:
        def history(self, **kwargs):
            assert "oldest_first" not in kwargs

            async def values():
                yield selected

            return values()

    messages = await _discord_messages(FakeChannel(), selected, 21)

    assert len(messages) == 1
    assert messages[0].message_id == 1
    assert messages[0].content == ""


@pytest.mark.asyncio
async def test_channel_lineage_fetches_uncached_parent_and_category():
    category = SimpleNamespace(id=30, name="IT局員選考")
    parent = SimpleNamespace(id=31, name="応募者対応", category=None, category_id=30)
    thread = SimpleNamespace(parent=None, parent_id=31, category=None, category_id=None)
    resolved = {31: parent, 30: category}
    guild = SimpleNamespace(
        get_channel=lambda channel_id: None,
        get_thread=lambda channel_id: None,
    )

    class FakeBot:
        async def fetch_channel(self, channel_id):
            return resolved[channel_id]

    assert await _channel_lineage(FakeBot(), guild, thread) == (parent, category)
