from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from bot.corpus import CorpusManager


@pytest.fixture
def corpus_manager():
    with patch("bot.corpus.genai.Client"):
        yield CorpusManager()


def add_message(manager: CorpusManager, *, content: str = "テストメッセージ", timestamp=None):
    manager.add_message(
        guild_id=1,
        channel_id=2,
        channel_name="general",
        author="member",
        content=content,
        timestamp=timestamp or datetime.now(timezone.utc),
        corpus_store_name="fileSearchStores/test",
    )


@pytest.mark.asyncio
async def test_time_flush_uses_store_saved_with_buffer(corpus_manager):
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    add_message(corpus_manager, timestamp=old)
    corpus_manager._upload_document = AsyncMock(return_value=True)

    await corpus_manager._check_time_flushes()

    corpus_manager._upload_document.assert_awaited_once()
    assert corpus_manager._upload_document.await_args.args[0] == "fileSearchStores/test"
    assert corpus_manager._buffers == {}


@pytest.mark.asyncio
async def test_failed_upload_restores_messages(corpus_manager):
    add_message(corpus_manager, content="失敗しても消えない")
    corpus_manager._upload_document = AsyncMock(return_value=False)

    uploaded = await corpus_manager._flush_buffer((1, 2))

    assert uploaded is False
    assert corpus_manager._buffers[(1, 2)].messages[0].endswith(
        "[member]: 失敗しても消えない"
    )


@pytest.mark.asyncio
async def test_shutdown_flushes_with_buffer_store(corpus_manager):
    add_message(corpus_manager)
    corpus_manager._upload_document = AsyncMock(return_value=True)

    await corpus_manager.shutdown()

    assert corpus_manager._upload_document.await_args.args[0] == "fileSearchStores/test"
    assert corpus_manager._buffers == {}
