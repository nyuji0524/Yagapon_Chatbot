import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.corpus import BackfillResult
from bot.scheduled_backfill import (
    ScheduledBackfillManager,
    configured_guild_ids,
    scheduler_enabled,
)


def _bot(*, failing_channel_id=None):
    channels = []
    for channel_id in (10, 20, 30):
        channel = SimpleNamespace(
            id=channel_id,
            permissions_for=lambda _member: SimpleNamespace(
                read_message_history=True
            ),
        )
        channels.append(channel)
    guild = SimpleNamespace(id=1, me=object(), text_channels=channels)

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
    corpus = SimpleNamespace(
        start_backfill=Mock(return_value=True),
        finish_backfill=Mock(),
        backfill_channel=AsyncMock(side_effect=backfill),
    )
    return SimpleNamespace(
        config=config,
        corpus=corpus,
        operations=SimpleNamespace(record_event=Mock()),
        get_guild=lambda guild_id: guild if guild_id == 1 else None,
    )


def test_scheduler_requires_feature_gate_and_explicit_guild_allowlist(monkeypatch):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.delenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", raising=False)
    assert scheduler_enabled() is False

    monkeypatch.setenv(
        "YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS",
        "1,invalid,2,1",
    )
    assert configured_guild_ids() == (1, 2)
    assert scheduler_enabled() is True


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
    assert bot.config.set_backfill_cursor.await_count == 2
    assert bot.corpus.finish_backfill.call_args_list == [((1,), {})]
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
    assert guild["failures"] == [
        {"channel_id": "20", "error_type": "RuntimeError"}
    ]
    bot.corpus.finish_backfill.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_scheduled_cycle_defers_when_manual_backfill_owns_lock(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    bot = _bot()
    bot.corpus.start_backfill.return_value = False

    result = await ScheduledBackfillManager(
        bot,
        state_path=tmp_path / "scheduled.json",
    ).run_once()

    guild = result["guilds"]["1"]
    assert result["status"] == "completed"
    assert guild["status"] == "deferred"
    bot.corpus.backfill_channel.assert_not_awaited()
    bot.corpus.finish_backfill.assert_not_called()


@pytest.mark.asyncio
async def test_scheduler_stop_cancels_sleeping_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "1")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_INITIAL_DELAY_SECONDS", "600")
    manager = ScheduledBackfillManager(_bot(), state_path=tmp_path / "state.json")

    assert manager.start() is True
    await asyncio.sleep(0)
    await manager.stop()

    assert manager.running is False
