import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.corpus import BackfillResult, BackfillSession
from bot.scheduled_backfill import (
    ScheduledBackfillConfigurationError,
    ScheduledBackfillManager,
    configured_guild_ids,
    next_backfill_delay,
    scheduler_enabled,
)


def _bot(*, failing_channel_id=None):
    channels = []
    for channel_id in (10, 20, 30):
        channel = SimpleNamespace(
            id=channel_id,
            permissions_for=lambda _member: SimpleNamespace(view_channel=True, read_message_history=True),
        )
        channels.append(channel)
    guild = SimpleNamespace(
        id=1,
        me=object(),
        text_channels=channels,
        forum_channels=[],
        channels=channels,
    )

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


def _forum_bot(
    *,
    ignored_ids=(),
    fail_active=False,
    fail_public_archive=False,
    fail_private_archive=False,
):
    member = object()
    guild = SimpleNamespace(
        id=1,
        me=member,
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

    async def prepare_backfill(guild_id, target_ids, corpus_store):
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


def test_scheduler_requires_feature_gate_and_explicit_guild_and_channel_allowlists(monkeypatch):
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


@pytest.mark.parametrize(
    ("channel_ids", "reason"),
    (("not-a-channel", "invalid-allowlist"), ("", "missing-allowlist")),
)
def test_requested_scheduler_invalidates_success_when_allowlist_is_invalid_or_empty(
    tmp_path,
    monkeypatch,
    channel_ids,
    reason,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", channel_ids)
    state_path = tmp_path / "scheduled.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    manager = ScheduledBackfillManager(_bot(), state_path=state_path)

    checkpoint = json.loads(state_path.read_text(encoding="utf-8"))
    assert checkpoint["status"] == "failed"
    assert checkpoint["reason"] == reason
    assert checkpoint["configured_channel_ids"] == []
    assert manager.start() is False


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
    guild = result["guilds"]["1"]
    assert guild["channels_total"] == 2
    assert guild["channels_completed"] == 2
    assert guild["messages_indexed"] == 30
    assert guild["discovered_channel_ids"] == ["10", "20", "30"]
    assert guild["eligible_channel_ids"] == ["10", "20"]
    assert guild["completed_channel_ids"] == ["10", "20"]
    assert bot.config.set_backfill_cursor.await_count == 2
    bot.corpus.finish_backfill.assert_awaited_once_with(bot.corpus.prepare_backfill.return_value)
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "completed"
    assert not state_path.with_suffix(".json.tmp").exists()


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
        json.dumps({"schema_version": 1, "status": "completed", "completed_at": "old"}),
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
    assert prepared_target_ids == [frozenset({41, 42})]
    cursor_channel_ids = {call.args[1] for call in bot.config.set_backfill_cursor.await_args_list}
    assert cursor_channel_ids == {41, 42}
    assert archive_calls == [
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
    assert prepared_target_ids == [frozenset({43})]
    assert archive_calls == [
        {"private": False, "joined": False, "limit": None, "before": None},
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
