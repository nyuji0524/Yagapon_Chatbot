import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import bot.scheduled_backfill as scheduled_backfill_module
from bot.corpus import BackfillResult, BackfillSession
from bot.scheduled_backfill import (
    ScheduledBackfillConfigurationError,
    ScheduledBackfillManager,
    configured_guild_ids,
    next_backfill_delay,
    scheduler_enabled,
)
from knowledge_curation.__main__ import curation_lock


def _bot(*, failing_channel_id=None):
    default_role = object()
    channels = []
    for channel_id in (10, 20, 30):
        def history(*, limit=None, before=None, oldest_first=False, _channel_id=channel_id, **_kwargs):
            async def iterator():
                if limit == 1 and oldest_first:
                    yield SimpleNamespace(
                        id=_channel_id * 10,
                        created_at=datetime(2024, 10, 5, tzinfo=timezone.utc),
                    )

            return iterator()

        channel = SimpleNamespace(
            id=channel_id,
            name=f"channel-{channel_id}",
            parent=None,
            category=None,
            history=history,
            permissions_for=lambda _member: SimpleNamespace(view_channel=True, read_message_history=True),
        )
        channels.append(channel)
    guild = SimpleNamespace(
        id=1,
        me=object(),
        default_role=default_role,
        text_channels=channels,
        forum_channels=[],
        channels=channels,
    )
    for channel in channels:
        channel.guild = guild
    channels_by_id = {channel.id: channel for channel in channels}
    guild.get_channel_or_thread = channels_by_id.get

    async def backfill(channel, *_args, **_kwargs):
        if channel.id == failing_channel_id:
            raise RuntimeError("File Search unavailable")
        return BackfillResult(
            messages_indexed=channel.id,
            documents_uploaded=1,
            documents_replaced=1,
            latest_message_id=channel.id * 100,
            latest_message_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
        )

    config = SimpleNamespace(
        get_corpus=lambda guild_id: "fileSearchStores/test" if guild_id == 1 else None,
        is_ignored=lambda _guild_id, channel_id: channel_id == 30,
        get_backfill_cursor=lambda _guild_id, _channel_id: None,
        has_backfill_cursors=lambda _guild_id: False,
        set_backfill_cursor=AsyncMock(),
    )
    session = BackfillSession(
        guild_id=1,
        target_channel_ids=frozenset({10, 20}),
        corpus_store_name="fileSearchStores/test",
        cutoff=datetime(2026, 10, 22, tzinfo=timezone.utc),
    )
    session.prepared = True
    corpus = SimpleNamespace(
        prepare_backfill=AsyncMock(return_value=session),
        finish_backfill=AsyncMock(return_value=True),
        backfill_channel=AsyncMock(side_effect=backfill),
    )
    return SimpleNamespace(
        config=config,
        corpus=corpus,
        operations=SimpleNamespace(record_event=Mock()),
        get_guild=lambda guild_id: guild if guild_id == 1 else None,
    )


def _multi_guild_bot():
    guilds = {}
    for guild_id, channel_id in ((1, 10), (2, 20)):
        default_role = object()

        def history(
            *,
            limit=None,
            before=None,
            oldest_first=False,
            _channel_id=channel_id,
            **_kwargs,
        ):
            async def iterator():
                if limit == 1 and oldest_first:
                    yield SimpleNamespace(
                        id=_channel_id * 10,
                        created_at=datetime(2024, 10, 5, tzinfo=timezone.utc),
                    )

            return iterator()

        guild = SimpleNamespace(
            id=guild_id,
            me=object(),
            default_role=default_role,
            forum_channels=[],
        )
        channel = SimpleNamespace(
            id=channel_id,
            name=f"guild-{guild_id}-public",
            guild=guild,
            parent=None,
            category=None,
            history=history,
            permissions_for=lambda _member: SimpleNamespace(
                view_channel=True,
                read_message_history=True,
            ),
        )
        guild.text_channels = [channel]
        guild.channels = [channel]
        guild.get_channel_or_thread = {channel.id: channel}.get
        guilds[guild_id] = guild

    config = SimpleNamespace(
        get_corpus=lambda guild_id: f"fileSearchStores/guild-{guild_id}",
        is_ignored=lambda _guild_id, _channel_id: False,
        get_backfill_cursor=lambda _guild_id, _channel_id: None,
        has_backfill_cursors=lambda _guild_id: False,
        set_backfill_cursor=AsyncMock(),
    )

    async def prepare_backfill(guild_id, target_ids, corpus_store, **kwargs):
        assert kwargs == {"prefer_local_inventory": True}
        session = BackfillSession(
            guild_id=guild_id,
            target_channel_ids=frozenset(target_ids),
            corpus_store_name=corpus_store,
            cutoff=datetime(2026, 10, 22, tzinfo=timezone.utc),
        )
        session.prepared = True
        return session

    async def backfill(channel, *_args, **_kwargs):
        return BackfillResult(
            messages_indexed=1,
            documents_uploaded=1,
            documents_replaced=1,
            latest_message_id=channel.id * 100,
            latest_message_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
        )

    return SimpleNamespace(
        config=config,
        corpus=SimpleNamespace(
            prepare_backfill=AsyncMock(side_effect=prepare_backfill),
            finish_backfill=AsyncMock(return_value=True),
            backfill_channel=AsyncMock(side_effect=backfill),
        ),
        operations=SimpleNamespace(record_event=Mock()),
        get_guild=guilds.get,
    )


def _forum_bot(
    *,
    ignored_ids=(),
    fail_active=False,
    fail_public_archive=False,
    fail_private_archive=False,
):
    member = object()
    default_role = object()
    guild = SimpleNamespace(
        id=1,
        me=member,
        default_role=default_role,
        text_channels=[],
        forum_channels=[],
        channels=[],
    )

    def channel(channel_id, name, *, parent=None, readable=True, private=False):
        value = SimpleNamespace(
            id=channel_id,
            name=name,
            guild=guild,
            parent=parent,
            parent_id=getattr(parent, "id", None),
            category=None,
            category_id=None,
            permissions_for=lambda _member: SimpleNamespace(
                view_channel=readable,
                read_message_history=readable,
            ),
        )
        if parent is not None:
            value.is_private = lambda: private
        return value

    archive_calls = []
    forum = channel(40, "タスク・議題")
    active = channel(41, "進行中", parent=forum)
    private_active = channel(47, "非公開進行中", parent=forum, private=True)
    public_archived = channel(42, "完了済み", parent=forum)
    private_joined = channel(43, "限定共有", parent=forum, private=True)
    private_unreadable = channel(
        44,
        "閲覧不可",
        parent=forum,
        readable=False,
        private=True,
    )
    ignored = channel(45, "無視", parent=forum)
    protected = channel(46, "選考記録", parent=forum)
    # Match Pycord: ThreadIterator results for archived threads are not added
    # to the guild cache. The scheduler must re-list them for its final ACL
    # snapshot instead of trusting the captured object.
    channels_by_id = {
        item.id: item
        for item in (
            forum,
            active,
            private_active,
        )
    }
    guild.get_channel_or_thread = channels_by_id.get

    def archived_threads(*, private=False, joined=False, limit=50, before=None):
        archive_calls.append(
            {
                "private": private,
                "joined": joined,
                "limit": limit,
                "before": before,
            }
        )
        if (private and fail_private_archive) or (not private and fail_public_archive):
            raise PermissionError("sensitive Discord detail")
        items = [private_joined, private_unreadable] if private else [public_archived, ignored, protected]

        async def iterator():
            for item in items:
                yield item

        return iterator()

    forum.archived_threads = archived_threads
    guild.forum_channels = [forum]
    guild.channels = [forum]
    guild.active_threads = AsyncMock(
        side_effect=(RuntimeError("sensitive Discord detail") if fail_active else None),
        return_value=[active, private_active],
    )

    ignored_ids = {int(value) for value in ignored_ids} | {45}
    config = SimpleNamespace(
        get_corpus=lambda guild_id: "fileSearchStores/test" if guild_id == 1 else None,
        is_ignored=lambda _guild_id, channel_id: int(channel_id) in ignored_ids,
        get_backfill_cursor=lambda _guild_id, _channel_id: None,
        has_backfill_cursors=lambda _guild_id: False,
        set_backfill_cursor=AsyncMock(),
    )
    prepared_target_ids = []

    async def prepare_backfill(guild_id, target_ids, corpus_store, **kwargs):
        assert kwargs == {"prefer_local_inventory": True}
        target_ids = frozenset(int(value) for value in target_ids)
        prepared_target_ids.append(target_ids)
        session = BackfillSession(
            guild_id=guild_id,
            target_channel_ids=target_ids,
            corpus_store_name=corpus_store,
            cutoff=datetime(2026, 10, 22, tzinfo=timezone.utc),
        )
        session.prepared = True
        return session

    async def backfill(thread, *_args, **_kwargs):
        return BackfillResult(
            messages_indexed=1,
            documents_uploaded=1,
            documents_replaced=0,
            latest_message_id=thread.id * 100,
            latest_message_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
        )

    corpus = SimpleNamespace(
        prepare_backfill=AsyncMock(side_effect=prepare_backfill),
        finish_backfill=AsyncMock(return_value=True),
        backfill_channel=AsyncMock(side_effect=backfill),
    )
    bot = SimpleNamespace(
        config=config,
        corpus=corpus,
        operations=SimpleNamespace(record_event=Mock()),
        get_guild=lambda guild_id: guild if guild_id == 1 else None,
    )
    return bot, archive_calls, prepared_target_ids


def _text_thread_bot():
    """Reuse the thread fixture with an ordinary TextChannel parent."""
    bot, archive_calls, prepared_target_ids = _forum_bot()
    guild = bot.get_guild(1)
    parent = guild.forum_channels[0]
    # The production type exposes both history() and archived_threads(). The
    # callable history marker also prevents the old-Pycord forum fallback from
    # treating this test double as a ForumChannel.
    parent.history = Mock()
    guild.text_channels = [parent]
    guild.forum_channels = []
    guild.channels = [parent]
    return bot, archive_calls, prepared_target_ids


def test_scheduler_requires_feature_gate_and_explicit_guild_allowlist(monkeypatch):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.delenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", raising=False)
    monkeypatch.delenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", raising=False)
    assert scheduler_enabled() is False

    monkeypatch.setenv(
        "YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS",
        "1,invalid,2,1",
    )
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "10,20")
    with pytest.raises(ScheduledBackfillConfigurationError):
        configured_guild_ids()
    assert scheduler_enabled() is False
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1,2,1")
    assert configured_guild_ids() == (1, 2)
    assert scheduler_enabled() is True

    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "")
    assert scheduler_enabled() is True


def test_requested_scheduler_invalidates_success_when_channel_filter_is_invalid(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "not-a-channel")
    state_path = tmp_path / "scheduled.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    manager = ScheduledBackfillManager(_bot(), state_path=state_path)

    checkpoint = json.loads(state_path.read_text(encoding="utf-8"))
    assert checkpoint["status"] == "failed"
    assert checkpoint["reason"] == "invalid-allowlist"
    assert checkpoint["configured_channel_ids"] == []
    assert manager.start() is False


def test_requested_scheduler_accepts_dynamic_all_readable_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "")
    state_path = tmp_path / "scheduled.json"

    manager = ScheduledBackfillManager(_bot(), state_path=state_path)

    checkpoint = json.loads(state_path.read_text(encoding="utf-8"))
    assert checkpoint["status"] == "waiting"
    assert checkpoint["configured_channel_ids"] == []
    assert manager.running is False
    assert scheduler_enabled() is True


def test_backfill_retry_keeps_six_hour_success_cadence():
    six_hours = 6 * 60 * 60
    fifteen_minutes = 15 * 60

    assert next_backfill_delay(
        six_hours,
        fifteen_minutes,
        {"status": "completed"},
    ) == six_hours
    assert next_backfill_delay(
        six_hours,
        fifteen_minutes,
        {"status": "failed"},
    ) == fifteen_minutes
    assert next_backfill_delay(six_hours, fifteen_minutes, None) == fifteen_minutes


@pytest.mark.asyncio
async def test_scheduled_cycle_defers_while_recording(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setattr(
        "bot.scheduled_backfill.recording_is_active",
        lambda: True,
    )
    bot = _bot()
    manager = ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    )

    result = await manager.run_once()

    assert result["status"] == "deferred"
    assert result["reason"] == "recording-active"
    bot.corpus.prepare_backfill.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduled_cycle_is_incremental_and_checkpoints_after_completion(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    bot = _bot()
    state_path = tmp_path / "scheduled.json"
    manager = ScheduledBackfillManager(bot, state_path=state_path)

    result = await manager.run_once()

    assert result["status"] == "completed"
    assert result["history_promotion_guild_id"] is None
    assert result["current_public_curation_channel_ids"] == ["10", "20"]
    guild = result["guilds"]["1"]
    assert guild["channels_total"] == 2
    assert guild["channels_completed"] == 2
    assert guild["messages_indexed"] == 30
    assert guild["discovered_channel_ids"] == ["10", "20", "30"]
    assert guild["eligible_channel_ids"] == ["10", "20"]
    assert guild["completed_channel_ids"] == ["10", "20"]
    assert guild["current_public_curation_channel_ids"] == ["10", "20"]
    assert bot.config.set_backfill_cursor.await_count == 2
    assert all(
        "authorize_history_promotion" not in call.kwargs
        for call in bot.corpus.backfill_channel.await_args_list
    )
    bot.corpus.finish_backfill.assert_awaited_once_with(bot.corpus.prepare_backfill.return_value)
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "completed"
    assert not state_path.with_suffix(".json.tmp").exists()


@pytest.mark.asyncio
async def test_manual_historical_cycle_rebuilds_each_festival_separately(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "")
    bot = _bot()

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once(full_history=True)

    guild = result["guilds"]["1"]
    assert result["status"] == "completed"
    assert result["history_promotion_guild_id"] is None
    assert result["mode"] == "historical"
    assert guild["mode"] == "historical"
    assert set(guild["festivals"]) == {"26th", "27th", "28th"}
    assert guild["messages_indexed"] == 90
    assert guild["documents_uploaded"] == 6
    assert guild["documents_replaced"] == 6
    assert guild["festivals"]["26th"] == {
        "messages_indexed": 30,
        "documents_uploaded": 2,
        "documents_replaced": 2,
        "channel_slices": 2,
    }
    assert bot.corpus.backfill_channel.await_count == 6
    completion_flags = [
        call.kwargs["complete_session_channel"]
        for call in bot.corpus.backfill_channel.await_args_list
    ]
    assert completion_flags.count(True) == 2
    assert completion_flags.count(False) == 4
    assert all(
        call.kwargs["authorize_history_promotion"] is False
        for call in bot.corpus.backfill_channel.await_args_list
    )
    assert bot.config.set_backfill_cursor.await_count == 2


@pytest.mark.asyncio
async def test_authorized_historical_cycle_propagates_promotion_authority(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "")
    bot = _bot()

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once(
        full_history=True,
        history_promotion_guild_id=1,
    )

    assert result["status"] == "completed"
    assert result["history_promotion_guild_id"] == "1"
    assert bot.corpus.backfill_channel.await_count == 6
    assert all(
        call.kwargs["authorize_history_promotion"] is True
        for call in bot.corpus.backfill_channel.await_args_list
    )


@pytest.mark.asyncio
async def test_historical_promotion_authority_is_scoped_to_invoking_guild(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1,2")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "")
    bot = _multi_guild_bot()

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once(
        full_history=True,
        history_promotion_guild_id=1,
    )

    assert result["status"] == "completed"
    assert result["history_promotion_guild_id"] == "1"
    by_guild = {1: [], 2: []}
    for call in bot.corpus.backfill_channel.await_args_list:
        by_guild[call.args[0].guild.id].append(
            call.kwargs["authorize_history_promotion"]
        )
    assert by_guild[1] == [True, True, True]
    assert by_guild[2] == [False, False, False]


@pytest.mark.asyncio
async def test_final_public_inventory_rechecks_earlier_guild_after_later_refresh(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1,2")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "")
    bot = _multi_guild_bot()
    guild_one_channel = bot.get_guild(1).text_channels[0]
    backfill = bot.corpus.backfill_channel.side_effect

    async def revoke_earlier_guild_while_refreshing_later(channel, *args, **kwargs):
        if channel.guild.id == 2:
            guild_one_channel.permissions_for = lambda _member: SimpleNamespace(
                view_channel=False,
                read_message_history=False,
            )
        return await backfill(channel, *args, **kwargs)

    bot.corpus.backfill_channel.side_effect = (
        revoke_earlier_guild_while_refreshing_later
    )

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once(
        full_history=True,
        history_promotion_guild_id=1,
    )

    assert result["status"] == "completed"
    assert result["guilds"]["1"]["current_public_curation_channel_ids"] == []
    assert result["guilds"]["2"]["current_public_curation_channel_ids"] == [
        "20"
    ]
    assert result["current_public_curation_channel_ids"] == ["20"]


@pytest.mark.asyncio
async def test_final_public_inventory_excludes_deleted_cached_channel(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1,2")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "")
    bot = _multi_guild_bot()
    guild_one = bot.get_guild(1)
    original_resolver = guild_one.get_channel_or_thread
    backfill = bot.corpus.backfill_channel.side_effect

    async def delete_earlier_channel_while_refreshing_later(
        channel,
        *args,
        **kwargs,
    ):
        if channel.guild.id == 2:
            guild_one.get_channel_or_thread = lambda channel_id: (
                None if channel_id == 10 else original_resolver(channel_id)
            )
        return await backfill(channel, *args, **kwargs)

    bot.corpus.backfill_channel.side_effect = (
        delete_earlier_channel_while_refreshing_later
    )

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once(full_history=True)

    assert result["status"] == "completed"
    assert result["guilds"]["1"]["current_public_curation_channel_ids"] == []
    assert result["current_public_curation_channel_ids"] == ["20"]


@pytest.mark.asyncio
async def test_final_override_inventory_rechecks_ignore_after_earlier_guild(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1,2")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "")
    bot = _multi_guild_bot()
    ignored: set[tuple[int, int]] = set()
    bot.config.is_ignored = lambda guild_id, channel_id: (
        int(guild_id), int(channel_id)
    ) in ignored
    backfill = bot.corpus.backfill_channel.side_effect

    async def ignore_earlier_channel_while_refreshing_later(
        channel,
        *args,
        **kwargs,
    ):
        if channel.guild.id == 2:
            ignored.add((1, 10))
        return await backfill(channel, *args, **kwargs)

    bot.corpus.backfill_channel.side_effect = (
        ignore_earlier_channel_while_refreshing_later
    )

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once(full_history=True)

    assert result["status"] == "completed"
    assert result["guilds"]["1"]["eligible_channel_ids"] == ["10"]
    assert result["guilds"]["1"]["current_curation_eligible_channel_ids"] == []
    assert result["guilds"]["1"]["current_public_curation_channel_ids"] == []


@pytest.mark.asyncio
async def test_history_promotion_authority_requires_full_history(tmp_path):
    manager = ScheduledBackfillManager(
        _bot(),
        state_path=tmp_path / "scheduled.json",
    )

    with pytest.raises(ValueError, match="requires full_history=True"):
        await manager.run_once(history_promotion_guild_id=1)


@pytest.mark.asyncio
async def test_dynamic_inventory_expands_public_forum_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "")
    bot, archive_calls, prepared_target_ids = _forum_bot()

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once()

    guild = result["guilds"]["1"]
    assert result["status"] == "completed"
    assert guild["configured_channel_ids"] == []
    assert guild["container_channel_ids"] == ["40"]
    assert set(guild["eligible_channel_ids"]) == {"40", "41", "42"}
    assert set(guild["completed_channel_ids"]) == {"41", "42"}
    assert set(guild["current_public_curation_channel_ids"]) == {"41", "42"}
    assert prepared_target_ids == [frozenset({41, 42})]
    assert archive_calls == [
        {"private": False, "joined": False, "limit": None, "before": None},
        {"private": False, "joined": False, "limit": None, "before": None},
    ]


@pytest.mark.asyncio
async def test_scheduled_cycle_keeps_other_channels_when_one_fails(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    bot = _bot(failing_channel_id=20)

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once()

    guild = result["guilds"]["1"]
    assert result["status"] == "failed"
    assert guild["channels_completed"] == 2
    assert guild["messages_indexed"] == 10
    assert guild["current_public_curation_channel_ids"] == ["10"]
    assert guild["failures"] == [{"channel_id": "20", "error_type": "RuntimeError"}]
    bot.corpus.finish_backfill.assert_awaited_once_with(bot.corpus.prepare_backfill.return_value)


@pytest.mark.asyncio
async def test_scheduled_cycle_defers_when_manual_backfill_owns_lock(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    bot = _bot()
    bot.corpus.prepare_backfill.return_value = None

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once()

    guild = result["guilds"]["1"]
    assert result["status"] == "failed"
    assert guild["status"] == "deferred"
    bot.corpus.backfill_channel.assert_not_awaited()
    bot.corpus.finish_backfill.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_stop_cancels_sleeping_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "10")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_INITIAL_DELAY_SECONDS", "600")
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps({"schema_version": 2, "status": "completed", "completed_at": "old"}),
        encoding="utf-8",
    )
    manager = ScheduledBackfillManager(_bot(), state_path=state_path)

    # Construction happens before the API server is started, so yesterday's
    # success is already unusable even if Discord on_ready is delayed.
    booting = json.loads(state_path.read_text(encoding="utf-8"))
    assert booting["status"] == "waiting"
    assert booting["reason"] == "bot-starting"
    assert booting["completed_at"] is None

    assert manager.start() is True
    # start() invalidates yesterday's success synchronously, before the task can
    # enter its configured initial sleep.
    waiting = json.loads(state_path.read_text(encoding="utf-8"))
    assert waiting["status"] == "waiting"
    assert waiting["reason"] == "initial-delay"
    assert waiting["completed_at"] is None
    await asyncio.sleep(0)
    await manager.stop()

    assert manager.running is False


@pytest.mark.asyncio
async def test_scheduled_cycle_rejects_missing_configured_channel_and_releases_lock(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "10,999")
    bot = _bot()

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once()

    assert result["status"] == "failed"
    assert result["configured_channel_ids"] == ["10", "999"]
    assert result["discovered_channel_ids"] == ["10", "20", "30"]
    assert result["eligible_channel_ids"] == ["10"]
    assert result["current_public_curation_channel_ids"] == ["10"]
    assert result["missing_channel_ids"] == ["999"]
    bot.corpus.prepare_backfill.assert_awaited_once()
    bot.corpus.finish_backfill.assert_awaited_once_with(bot.corpus.prepare_backfill.return_value)


@pytest.mark.asyncio
async def test_scheduled_cycle_persists_running_then_interrupted_atomically(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    state_path = tmp_path / "scheduled.json"
    manager = ScheduledBackfillManager(_bot(), state_path=state_path)
    entered = asyncio.Event()

    async def wait_forever(_guild_id):
        assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "running"
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_refresh_guild", wait_forever)
    task = asyncio.create_task(manager.run_once())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    checkpoint = json.loads(state_path.read_text(encoding="utf-8"))
    assert checkpoint["status"] == "interrupted"
    assert checkpoint["completed_at"]
    assert checkpoint["error_type"] == "CancelledError"
    assert not state_path.with_suffix(".json.tmp").exists()


@pytest.mark.asyncio
async def test_forum_parent_allowlist_expands_active_and_archived_threads(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "40")
    bot, archive_calls, prepared_target_ids = _forum_bot()

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once()

    guild = result["guilds"]["1"]
    assert result["status"] == "completed"
    assert guild["configured_channel_ids"] == ["40"]
    assert guild["missing_channel_ids"] == []
    assert guild["channels_total"] == 2
    assert set(guild["completed_channel_ids"]) == {"41", "42"}
    assert set(guild["eligible_channel_ids"]) == {"40", "41", "42"}
    assert guild["container_channel_ids"] == ["40"]
    assert set(guild["current_public_curation_channel_ids"]) == {"41", "42"}
    assert prepared_target_ids == [frozenset({41, 42})]
    cursor_channel_ids = {call.args[1] for call in bot.config.set_backfill_cursor.await_args_list}
    assert cursor_channel_ids == {41, 42}
    assert archive_calls == [
        {"private": False, "joined": False, "limit": None, "before": None},
        {"private": False, "joined": False, "limit": None, "before": None},
    ]


@pytest.mark.asyncio
async def test_explicit_private_archived_thread_id_is_resolved_without_allowing_siblings(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "43")
    bot, archive_calls, prepared_target_ids = _forum_bot()

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once()

    guild = result["guilds"]["1"]
    assert result["status"] == "completed"
    assert guild["completed_channel_ids"] == ["43"]
    assert guild["current_curation_eligible_channel_ids"] == ["43"]
    assert guild["current_public_curation_channel_ids"] == []
    assert prepared_target_ids == [frozenset({43})]
    assert archive_calls == [
        {"private": False, "joined": False, "limit": None, "before": None},
        {"private": True, "joined": True, "limit": None, "before": None},
        {"private": True, "joined": True, "limit": None, "before": None},
    ]


@pytest.mark.parametrize(
    ("thread_id", "expected_archive_calls"),
    (
        (47, []),
        (
            43,
                [
                    {"private": False, "joined": False, "limit": None, "before": None},
                    {"private": True, "joined": True, "limit": None, "before": None},
                    {"private": True, "joined": True, "limit": None, "before": None},
                ],
        ),
    ),
)
@pytest.mark.asyncio
async def test_explicit_private_thread_under_text_channel_is_resolved(
    tmp_path,
    monkeypatch,
    thread_id,
    expected_archive_calls,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", str(thread_id))
    bot, archive_calls, prepared_target_ids = _text_thread_bot()

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once()

    guild = result["guilds"]["1"]
    assert result["status"] == "completed"
    assert guild["completed_channel_ids"] == [str(thread_id)]
    assert guild["current_curation_eligible_channel_ids"] == [str(thread_id)]
    assert guild["current_public_curation_channel_ids"] == []
    assert prepared_target_ids == [frozenset({thread_id})]
    assert archive_calls == expected_archive_calls


@pytest.mark.asyncio
async def test_forum_parent_ignore_is_inherited_without_fetching_threads(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "40")
    bot, archive_calls, prepared_target_ids = _forum_bot(ignored_ids={40})

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once()

    guild = result["guilds"]["1"]
    assert result["status"] == "failed"
    assert guild["reason"] == "configured-channels-not-readable"
    assert guild["missing_channel_ids"] == ["40"]
    assert archive_calls == []
    assert prepared_target_ids == []


@pytest.mark.asyncio
async def test_archived_thread_discovery_failure_is_fail_closed_and_sanitized(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "40")
    bot, _archive_calls, _prepared_target_ids = _forum_bot(fail_public_archive=True)

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once()

    guild = result["guilds"]["1"]
    assert result["status"] == "failed"
    assert {
        "channel_id": "40",
        "stage": "archived-public-thread-discovery",
        "error_type": "PermissionError",
    } in guild["failures"]
    persisted = (tmp_path / "scheduled.json").read_text(encoding="utf-8")
    assert "sensitive Discord detail" not in persisted


@pytest.mark.asyncio
async def test_active_thread_discovery_failure_is_reported_without_error_detail(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "40")
    bot, archive_calls, _prepared_target_ids = _forum_bot(fail_active=True)

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once()

    guild = result["guilds"]["1"]
    assert result["status"] == "failed"
    assert {
        "channel_id": None,
        "stage": "active-thread-discovery",
        "error_type": "RuntimeError",
    } in guild["failures"]
    assert archive_calls == [
        {"private": False, "joined": False, "limit": None, "before": None},
        {"private": False, "joined": False, "limit": None, "before": None},
    ]
    persisted = (tmp_path / "scheduled.json").read_text(encoding="utf-8")
    assert "sensitive Discord detail" not in persisted


@pytest.mark.asyncio
async def test_concurrent_manager_run_is_deferred_without_replacing_checkpoint(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    state_path = tmp_path / "scheduled.json"
    manager = ScheduledBackfillManager(_bot(), state_path=state_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused_refresh(_guild_id):
        entered.set()
        await release.wait()
        return {
            "status": "completed",
            "discovered_channel_ids": [],
            "eligible_channel_ids": [],
        }

    monkeypatch.setattr(manager, "_refresh_guild", paused_refresh)
    first = asyncio.create_task(manager.run_once())
    await entered.wait()
    checkpoint_before = state_path.read_text(encoding="utf-8")

    deferred = await manager.run_once()

    assert deferred["status"] == "deferred"
    assert deferred["reason"] == "backfill-already-running"
    assert state_path.read_text(encoding="utf-8") == checkpoint_before
    release.set()
    assert (await first)["status"] == "completed"


@pytest.mark.asyncio
async def test_curation_pipeline_lock_prevents_running_checkpoint_transition(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv(
        "YAGAPON_KNOWLEDGE_PIPELINE_LOCK",
        str(tmp_path / "knowledge-pipeline.lock"),
    )
    state_path = tmp_path / "scheduled.json"
    previous_checkpoint = '{"schema_version":2,"status":"completed"}\n'
    state_path.write_text(previous_checkpoint, encoding="utf-8")
    bot = _bot()
    manager = ScheduledBackfillManager(bot, state_path=state_path)

    with curation_lock(tmp_path / "curation.sqlite3"):
        result = await manager.run_once()

    assert result["status"] == "deferred"
    assert result["reason"] == "knowledge-pipeline-busy"
    assert state_path.read_text(encoding="utf-8") == previous_checkpoint
    bot.corpus.prepare_backfill.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_retries_waiting_checkpoint_before_initial_delay(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_INITIAL_DELAY_SECONDS", "600")
    monkeypatch.setenv(
        "YAGAPON_KNOWLEDGE_PIPELINE_LOCK",
        str(tmp_path / "knowledge-pipeline.lock"),
    )
    state_path = tmp_path / "scheduled.json"
    previous_checkpoint = '{"schema_version":2,"status":"completed"}\n'
    state_path.write_text(previous_checkpoint, encoding="utf-8")

    with curation_lock(tmp_path / "curation.sqlite3"):
        manager = ScheduledBackfillManager(_bot(), state_path=state_path)
        assert manager.start() is True
        await asyncio.sleep(0)
        assert state_path.read_text(encoding="utf-8") == previous_checkpoint

    for _ in range(20):
        checkpoint = json.loads(state_path.read_text(encoding="utf-8"))
        if checkpoint.get("status") == "waiting":
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("startup waiting checkpoint was not retried")

    assert checkpoint["reason"] == "initial-delay"
    assert checkpoint["completed_at"] is None
    await manager.stop()


@pytest.mark.asyncio
async def test_completed_cycle_does_not_overwrite_newer_running_checkpoint(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv(
        "YAGAPON_KNOWLEDGE_PIPELINE_LOCK",
        str(tmp_path / "knowledge-pipeline.lock"),
    )
    state_path = tmp_path / "scheduled.json"
    manager = ScheduledBackfillManager(_bot(), state_path=state_path)

    async def lose_checkpoint_ownership(_guild_id):
        replacement = json.loads(state_path.read_text(encoding="utf-8"))
        replacement["cycle_id"] = "newer-cycle"
        state_path.write_text(json.dumps(replacement), encoding="utf-8")
        return {
            "status": "completed",
            "configured_channel_ids": [],
            "discovered_channel_ids": [],
            "eligible_channel_ids": [],
            "current_public_curation_channel_ids": [],
            "container_channel_ids": [],
            "missing_channel_ids": [],
            "channels_total": 0,
            "channels_completed": 0,
            "completed_channel_ids": [],
            "failures": [],
        }

    monkeypatch.setattr(manager, "_refresh_guild", lose_checkpoint_ownership)

    result = await manager.run_once()

    assert result["status"] == "deferred"
    assert result["reason"] == "checkpoint-ownership-lost"
    assert json.loads(state_path.read_text(encoding="utf-8"))["cycle_id"] == (
        "newer-cycle"
    )


@pytest.mark.asyncio
async def test_startup_writer_cannot_replace_running_after_final_ownership_check(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv(
        "YAGAPON_KNOWLEDGE_PIPELINE_LOCK",
        str(tmp_path / "knowledge-pipeline.lock"),
    )
    state_path = tmp_path / "scheduled.json"
    manager = ScheduledBackfillManager(_bot(), state_path=state_path)
    ownership_checked = asyncio.Event()
    continue_acl_snapshot = asyncio.Event()
    original_resolver = scheduled_backfill_module._resolve_current_message_target

    async def pause_after_ownership_check(*args, **kwargs):
        ownership_checked.set()
        await continue_acl_snapshot.wait()
        return await original_resolver(*args, **kwargs)

    monkeypatch.setattr(
        scheduled_backfill_module,
        "_resolve_current_message_target",
        pause_after_ownership_check,
    )
    cycle = asyncio.create_task(manager.run_once())
    await ownership_checked.wait()
    running = json.loads(state_path.read_text(encoding="utf-8"))
    assert running["status"] == "running"

    # A replacement process is constructed while the older process is inside
    # its final ACL await, after it has checked ownership. Its boot marker must
    # defer on the common writer lock instead of replacing ``running``.
    replacement = ScheduledBackfillManager(_bot(), state_path=state_path)
    assert json.loads(state_path.read_text(encoding="utf-8")) == running

    continue_acl_snapshot.set()
    result = await cycle

    assert result["status"] == "completed"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "completed"
    assert persisted["cycle_id"] == running["cycle_id"]
    assert replacement._write_waiting_checkpoint("replacement-ready") is True
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "waiting"
    assert not list(tmp_path.glob(".scheduled.json.*.tmp"))
