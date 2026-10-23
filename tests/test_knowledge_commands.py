import asyncio
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from google.auth.exceptions import RefreshError

from bot.commands.knowledge import (
    _channel_lineage,
    _curation_batch_limit,
    _discord_messages,
    _knowledge_guild_allowed,
    _knowledge_refresh_embed,
    _knowledge_refresh_status,
    _permission_channel,
    _protected_channel,
    _read_drive_source,
    _run_knowledge_refresh,
    register,
)
from bot.google_credentials import GoogleCredentialScopeError
from bot.knowledge_policy import (
    automatic_knowledge_promotion_allowed,
    evaluate_knowledge_export,
    evaluate_meeting_export,
)


def test_curation_batch_limit_is_bounded(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_BATCH", "99")
    assert _curation_batch_limit() == 10

    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_BATCH", "invalid")
    assert _curation_batch_limit() == 3


def test_knowledge_refresh_embed_never_splits_annual_result_lines():
    festivals = {
        f"{number}th": {
            "messages_indexed": number * 100,
            "documents_uploaded": number * 10,
            "documents_replaced": number,
        }
        for number in range(1, 80)
    }
    result = {
        "mode": "historical",
        "backfill": {
            "status": "completed",
            "guilds": {
                "1": {
                    "channels_completed": 1,
                    "channels_total": 1,
                    "messages_indexed": 1,
                    "documents_uploaded": 1,
                    "documents_replaced": 0,
                    "festivals": festivals,
                }
            },
        },
        "curation": {},
    }

    embed = _knowledge_refresh_embed(result)
    value = next(
        field.value for field in embed.fields if field.name == "年度別結果（10月〜翌9月）"
    )

    assert len(value) <= 1024
    assert "省略" in value
    for line in value.splitlines():
        assert line.startswith("…（残り") or re.fullmatch(
            r"\*\*\d+th\*\* — 発言 \d+ / 文書 \d+ / 置換 \d+",
            line,
        )


def test_policy_excluded_sources_are_reported_as_orange_policy_hold():
    result = {
        "mode": "historical",
        "backfill": {
            "status": "completed",
            "history_promotion_guild_id": "1",
            "guilds": {},
        },
        "curation": {
            "enabled": True,
            "checkpoint_ready": True,
            "checkpoint_reason": "fresh-success",
            "source": 4,
            "eligible": 0,
            "policy_excluded": 4,
            "actionable": 0,
            "processed": 0,
            "queued": 0,
            "skipped": 0,
            "failed": 0,
            "open_bundles": 1,
            "max_open_bundles": 5,
            "backpressured": 0,
        },
    }

    assert _knowledge_refresh_status(result) == "policy-held"
    embed = _knowledge_refresh_embed(result)
    curation_value = next(
        field.value for field in embed.fields if field.name == "議題別Knowledge整理"
    )

    assert embed.color == discord.Color.orange()
    assert "ポリシーで保留" in embed.title
    assert "候補元: 4 / 対象: 0 / ポリシー除外: 4 / 開始時未処理: 0" in curation_value
    assert "未確認bundle: 1 / 上限: 5 / 今回保留: 0" in curation_value
    assert "PR待ち" not in curation_value


def test_access_blocked_retry_is_reported_as_orange_policy_hold():
    result = {
        "mode": "full",
        "backfill": {"status": "completed", "guilds": {}},
        "curation": {
            "enabled": True,
            "checkpoint_ready": True,
            "checkpoint_reason": "fresh-success",
            "source": 1,
            "eligible": 1,
            "policy_excluded": 0,
            "actionable": 0,
            "processed": 0,
            "queued": 0,
            "skipped": 0,
            "failed": 0,
            "retried": 0,
            "retry_blocked": 1,
            "open_bundles": 1,
            "max_open_bundles": 5,
            "backpressured": 0,
        },
    }

    assert _knowledge_refresh_status(result) == "policy-held"
    embed = _knowledge_refresh_embed(result)
    curation_value = next(
        field.value for field in embed.fields if field.name == "議題別Knowledge整理"
    )

    assert embed.color == discord.Color.orange()
    assert "権限再確認で再送保留: 1" in curation_value


def test_blocked_evidence_is_reported_as_orange_policy_hold():
    result = {
        "mode": "full",
        "backfill": {"status": "completed", "guilds": {}},
        "curation": {
            "enabled": True,
            "checkpoint_ready": True,
            "processed": 1,
            "blocked_access": 1,
            "failed": 0,
        },
    }

    assert _knowledge_refresh_status(result) == "policy-held"
    embed = _knowledge_refresh_embed(result)
    curation_value = next(
        field.value for field in embed.fields if field.name == "議題別Knowledge整理"
    )

    assert embed.color == discord.Color.orange()
    assert "権限保留: 1" in curation_value


def test_failed_curation_takes_precedence_over_access_blocked_retry():
    result = {
        "mode": "full",
        "backfill": {"status": "completed", "guilds": {}},
        "curation": {
            "enabled": True,
            "checkpoint_ready": True,
            "failed": 1,
            "retry_blocked": 1,
        },
    }

    assert _knowledge_refresh_status(result) == "partial"

    result["curation"]["failed"] = 0
    result["curation"]["retry_failed"] = 1
    assert _knowledge_refresh_status(result) == "partial"


def test_deferred_refresh_does_not_report_unknown_candidate_counts_as_zero():
    embed = _knowledge_refresh_embed(
        {
            "mode": "full",
            "backfill": {"status": "failed", "guilds": {}},
            "curation": {
                "enabled": True,
                "checkpoint_ready": False,
                "checkpoint_reason": "manual-backfill-not-completed",
            },
        }
    )
    value = next(
        field.value
        for field in embed.fields
        if field.name == "議題別Knowledge整理"
    )

    assert "候補元:" not in value
    assert "ポリシー除外:" not in value


def test_failed_backfill_reports_success_and_failure_counts_separately():
    embed = _knowledge_refresh_embed(
        {
            "mode": "historical",
            "backfill": {
                "status": "failed",
                "guilds": {
                    "1": {
                        "channels_completed": 128,
                        "channels_total": 128,
                        "messages_indexed": 100,
                        "documents_uploaded": 20,
                        "documents_replaced": 18,
                        "failures": [
                            {"channel_id": "10", "error_type": "ServerError"},
                            {"channel_id": "11", "error_type": "ServerError"},
                            {"channel_id": "12", "error_type": "TimeoutError"},
                        ],
                    }
                },
            },
            "curation": {
                "enabled": True,
                "checkpoint_ready": False,
                "checkpoint_reason": "manual-backfill-not-completed",
            },
        }
    )

    backfill_value = next(
        field.value
        for field in embed.fields
        if field.name == "Discord全期間取得"
    )
    failure_value = next(
        field.value
        for field in embed.fields
        if field.name == "取得失敗の内訳"
    )

    assert "チャンネル: 成功 125 / 失敗 3 / 未処理 0（対象 128）" in backfill_value
    assert "チャンネル処理: `ServerError`: 2件" in failure_value
    assert "`TimeoutError`: 1件" in failure_value
    assert "探索・基盤:" not in failure_value
    assert "再実行" in failure_value


def test_backfill_reporting_does_not_count_discovery_errors_as_channel_failures():
    embed = _knowledge_refresh_embed(
        {
            "mode": "historical",
            "backfill": {
                "status": "failed",
                "guilds": {
                    "1": {
                        "status": "failed",
                        "channels_completed": 3,
                        "channels_total": 4,
                        "completed_channel_ids": ["10", "11"],
                        "messages_indexed": 100,
                        "documents_uploaded": 20,
                        "documents_replaced": 18,
                        "failures": [
                            {"channel_id": "12", "error_type": "ServerError"},
                            {
                                "channel_id": None,
                                "stage": "active-thread-discovery",
                                "error_type": "HTTPException",
                            },
                            {
                                "channel_id": "20",
                                "stage": "archived-public-thread-discovery",
                                "error_type": "TimeoutError",
                            },
                        ],
                    }
                },
            },
            "curation": {
                "enabled": True,
                "checkpoint_ready": False,
                "checkpoint_reason": "manual-backfill-not-completed",
            },
        }
    )

    backfill_value = next(
        field.value
        for field in embed.fields
        if field.name == "Discord全期間取得"
    )
    failure_value = next(
        field.value
        for field in embed.fields
        if field.name == "取得失敗の内訳"
    )

    assert "チャンネル: 成功 2 / 失敗 1 / 未処理 1（対象 4）" in backfill_value
    assert "チャンネル処理: `ServerError`: 1件" in failure_value
    assert "探索・基盤: `HTTPException`: 1件 / `TimeoutError`: 1件" in failure_value


def test_registered_refresh_exposes_runtime_option_type_to_pycord():
    """Pycord invokes ``issubclass`` on option annotations at command runtime."""

    class Group:
        def __init__(self):
            self.callbacks = {}

        def command(self, *_args, **kwargs):
            def decorator(function):
                self.callbacks[kwargs.get("name", function.__name__)] = function
                return function

            return decorator

    group = Group()
    bot = SimpleNamespace(
        create_group=Mock(return_value=group),
        knowledge_refresh_tasks=set(),
    )

    register(bot)

    option = group.callbacks["refresh"].__annotations__["mode"]
    assert option._raw_type is str


@pytest.mark.asyncio
async def test_registered_refresh_acknowledges_then_reports_to_channel(
    tmp_path,
    monkeypatch,
):
    class Group:
        def __init__(self):
            self.callbacks = {}

        def command(self, *_args, **kwargs):
            def decorator(function):
                self.callbacks[kwargs.get("name", function.__name__)] = function
                return function

            return decorator

    group = Group()
    tasks = set()
    bot = SimpleNamespace(
        create_group=Mock(return_value=group),
        knowledge_refresh_tasks=tasks,
        operations=SimpleNamespace(record_event=Mock()),
        scheduled_backfill=SimpleNamespace(
            state_path=tmp_path / "scheduled.json"
        ),
    )
    register(bot)
    result = {
        "mode": "historical",
        "backfill": {
            "status": "completed",
            "history_promotion_guild_id": "1",
            "guilds": {
                "1": {
                    "status": "completed",
                    "channels_total": 2,
                    "channels_completed": 2,
                    "completed_channel_ids": ["10", "11"],
                    "failures": [],
                }
            },
        },
        "curation": {
            "enabled": True,
            "checkpoint_ready": True,
            "source": 2,
            "eligible": 2,
            "policy_excluded": 0,
            "actionable": 1,
            "processed": 1,
            "queued": 1,
            "failed": 0,
        },
    }
    monkeypatch.setattr(
        "bot.commands.knowledge.require_guild_admin",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "bot.commands.knowledge._require_knowledge_guild",
        AsyncMock(return_value=True),
    )
    run_refresh = AsyncMock(return_value=result)
    monkeypatch.setattr(
        "bot.commands.knowledge._run_knowledge_refresh",
        run_refresh,
    )
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "10")
    monkeypatch.setenv(
        "YAGAPON_SCHEDULED_BACKFILL_STATE",
        str(tmp_path / "scheduled.json"),
    )
    monkeypatch.setenv(
        "YAGAPON_KNOWLEDGE_BACKFILL_STATE",
        str(tmp_path / "scheduled.json"),
    )
    channel = SimpleNamespace(send=AsyncMock())
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        guild_id=1,
        channel=channel,
        author=SimpleNamespace(id=999, display_name="管理者", name="admin"),
        respond=AsyncMock(),
    )

    await group.callbacks["refresh"](ctx, mode="historical")
    await asyncio.gather(*tuple(tasks))

    ctx.respond.assert_awaited_once()
    assert ctx.respond.await_args.kwargs["ephemeral"] is True
    assert "明示的なチャンネルoverrideを除き" in ctx.respond.await_args.args[0]
    channel.send.assert_awaited_once()
    assert channel.send.await_args.kwargs["silent"] is True
    assert channel.send.await_args.kwargs["embed"].title.startswith("✅")
    curation_value = next(
        field.value
        for field in channel.send.await_args.kwargs["embed"].fields
        if field.name == "議題別Knowledge整理"
    )
    assert "開始時未処理: 1" in curation_value
    backfill_value = next(
        field.value
        for field in channel.send.await_args.kwargs["embed"].fields
        if field.name == "Discord全期間取得"
    )
    assert "明示overrideを除き" in backfill_value
    bot.operations.record_event.assert_called_once()
    run_refresh.assert_awaited_once_with(bot, mode="historical", guild_id=1)
    assert bot.operations.record_event.call_args.kwargs["details"][
        "requester_user_id"
    ] == "999"
    assert bot.operations.record_event.call_args.kwargs["details"][
        "history_promotion_guild_id"
    ] == "1"
    assert bot.operations.record_event.call_args.kwargs["details"][
        "backfill_failure_count"
    ] == 0
    assert bot.operations.record_event.call_args.kwargs["details"][
        "backfill_failure_types"
    ] == {}
    assert bot.operations.record_event.call_args.kwargs["details"][
        "backfill_channel_total"
    ] == 2
    assert bot.operations.record_event.call_args.kwargs["details"][
        "backfill_channel_attempted_count"
    ] == 2
    assert bot.operations.record_event.call_args.kwargs["details"][
        "backfill_channel_success_count"
    ] == 2
    assert bot.operations.record_event.call_args.kwargs["details"][
        "backfill_channel_failure_count"
    ] == 0
    assert bot.operations.record_event.call_args.kwargs["details"][
        "backfill_channel_unprocessed_count"
    ] == 0
    assert bot.operations.record_event.call_args.kwargs["details"][
        "backfill_infrastructure_failure_count"
    ] == 0
    assert bot.operations.record_event.call_args.kwargs["details"][
        "backfill_channel_failure_types"
    ] == {}
    assert bot.operations.record_event.call_args.kwargs["details"][
        "backfill_infrastructure_failure_types"
    ] == {}


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
        guild_id=123,
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
        guild_id=123,
    )

    curate.assert_not_awaited()
    assert result["curation"]["checkpoint_reason"] == "manual-backfill-not-completed"
    assert _knowledge_refresh_status(result) == "failed"


@pytest.mark.asyncio
async def test_historical_knowledge_refresh_requests_all_history_before_curation(
    tmp_path,
    monkeypatch,
):
    backfill = AsyncMock(
        return_value={
            "status": "completed",
            "mode": "historical",
            "guilds": {},
        }
    )
    curate = AsyncMock(
        return_value={
            "enabled": True,
            "checkpoint_ready": True,
            "processed": 0,
            "failed": 0,
        }
    )
    monkeypatch.setattr("bot.commands.knowledge._run_curation_cycle", curate)
    checkpoint = tmp_path / "scheduled.json"
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_STATE", str(checkpoint))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(checkpoint))

    result = await _run_knowledge_refresh(
        SimpleNamespace(
            scheduled_backfill=SimpleNamespace(
                run_once=backfill,
                state_path=checkpoint,
            )
        ),
        mode="historical",
        guild_id=123,
    )

    backfill.assert_awaited_once_with(
        full_history=True,
        history_promotion_guild_id=123,
    )
    curate.assert_awaited_once()
    assert result["mode"] == "historical"
    assert _knowledge_refresh_status(result) == "completed"


@pytest.mark.asyncio
async def test_historical_refresh_fails_before_backfill_when_checkpoint_paths_differ(
    tmp_path,
    monkeypatch,
):
    backfill = AsyncMock()
    producer = tmp_path / "producer.json"
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_STATE", str(producer))
    monkeypatch.setenv(
        "YAGAPON_KNOWLEDGE_BACKFILL_STATE",
        str(tmp_path / "other.json"),
    )

    with pytest.raises(RuntimeError, match="checkpoint path"):
        await _run_knowledge_refresh(
            SimpleNamespace(
                scheduled_backfill=SimpleNamespace(
                    run_once=backfill,
                    state_path=producer,
                )
            ),
            mode="historical",
            guild_id=123,
        )

    backfill.assert_not_awaited()


@pytest.mark.asyncio
async def test_knowledge_refresh_rejects_unknown_internal_mode():
    with pytest.raises(ValueError, match="Unsupported Knowledge refresh mode"):
        await _run_knowledge_refresh(
            SimpleNamespace(scheduled_backfill=SimpleNamespace()),
            mode="everything",
            guild_id=123,
        )


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
        guild_id=123,
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


def test_automatic_promotion_requires_guildwide_audience_and_respects_deny(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", "選考")
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
    protected = SimpleNamespace(
        id=12,
        name="選考連絡",
        guild=guild,
        parent=None,
        category=None,
        permissions_for=public.permissions_for,
    )

    assert automatic_knowledge_promotion_allowed(private) is False
    assert automatic_knowledge_promotion_allowed(public) is True
    assert automatic_knowledge_promotion_allowed(protected) is False


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
