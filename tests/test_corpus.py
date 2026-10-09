from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot.corpus import DOCUMENT_MAX_MESSAGES, CorpusManager, calculate_incremental_after
from bot.rag_store import RagStore


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
    assert corpus_manager._buffers[(1, 2)].messages[0].endswith("[member]: 失敗しても消えない")


@pytest.mark.asyncio
async def test_shutdown_flushes_with_buffer_store(corpus_manager):
    add_message(corpus_manager)
    corpus_manager._upload_document = AsyncMock(return_value=True)

    await corpus_manager.shutdown()

    assert corpus_manager._upload_document.await_args.args[0] == "fileSearchStores/test"
    assert corpus_manager._buffers == {}


def fake_message(message_id: int, minute: int = 0, *, content: str = "決定事項です"):
    return SimpleNamespace(
        id=message_id,
        content=content,
        created_at=datetime(2026, 10, 5, 0, minute, tzinfo=timezone.utc),
        jump_url=f"https://discord.com/channels/1/2/{message_id}",
        attachments=[],
        author=SimpleNamespace(bot=False, display_name="member"),
    )


class FakeChannel:
    id = 2
    name = "開発"
    topic = "開発の意思決定"
    category = SimpleNamespace(name="IT局")
    guild = SimpleNamespace(id=1)

    def __init__(self, messages):
        self.messages = messages

    def history(self, **kwargs):
        async def iterate():
            for message in self.messages:
                yield message

        return iterate()


def test_knowledge_document_has_traceable_metadata(corpus_manager):
    messages = [
        corpus_manager._knowledge_message(fake_message(1)),
        corpus_manager._knowledge_message(fake_message(2, 1)),
    ]

    documents = corpus_manager._build_knowledge_documents(FakeChannel([]), messages)
    metadata = {item["key"]: item.get("string_value", item.get("numeric_value")) for item in documents[0].metadata}

    assert metadata["channel_id"] == "2"
    assert metadata["schema"] == "discord-v2"
    assert metadata["source_url"].endswith("/1")
    assert metadata["message_count"] == 2.0
    assert metadata["festival"] == 28.0
    assert metadata["status"] == "raw"
    assert metadata["authority"] == "conversation"
    assert "2026-10-05 09:00" in documents[0].text


@pytest.mark.asyncio
async def test_backfill_replaces_old_documents_only_after_all_uploads(corpus_manager):
    channel = FakeChannel([fake_message(1), fake_message(2, 1)])
    corpus_manager._existing_channel_documents = AsyncMock(return_value=["documents/old"])
    corpus_manager._upload_document = AsyncMock(return_value="documents/new")
    corpus_manager._delete_documents = AsyncMock(return_value=1)

    result = await corpus_manager.backfill_channel(channel, "fileSearchStores/test")

    assert result.messages_indexed == 2
    assert result.documents_uploaded == 1
    corpus_manager._delete_documents.assert_awaited_once_with(["documents/old"])


@pytest.mark.asyncio
async def test_backfill_upload_failure_keeps_old_documents(corpus_manager):
    messages = [fake_message(i + 1, content=f"決定事項 {i}") for i in range(DOCUMENT_MAX_MESSAGES + 1)]
    for index, message in enumerate(messages):
        message.created_at = datetime(2026, 10, 5, tzinfo=timezone.utc) + timedelta(minutes=index)
    channel = FakeChannel(messages)
    corpus_manager._existing_channel_documents = AsyncMock(return_value=["documents/old"])
    corpus_manager._upload_document = AsyncMock(side_effect=["documents/new-1", None])
    corpus_manager._delete_documents = AsyncMock(return_value=1)

    with pytest.raises(RuntimeError):
        await corpus_manager.backfill_channel(channel, "fileSearchStores/test")

    corpus_manager._delete_documents.assert_awaited_once_with(["documents/new-1"])


@pytest.mark.asyncio
async def test_empty_backfill_does_not_delete_existing_knowledge(corpus_manager):
    corpus_manager._existing_channel_documents = AsyncMock(return_value=["documents/old"])
    corpus_manager._delete_documents = AsyncMock()

    result = await corpus_manager.backfill_channel(FakeChannel([]), "fileSearchStores/test")

    assert result.messages_indexed == 0
    corpus_manager._delete_documents.assert_not_awaited()


def test_response_sources_deduplicates_discord_links(corpus_manager):
    context = SimpleNamespace(
        title="開発ログ",
        uri=None,
        custom_metadata=[
            {"key": "channel_name", "string_value": "開発"},
            {"key": "start_at", "string_value": "2026-10-05T09:00:00+09:00"},
            {"key": "source_url", "string_value": "https://discord.com/channels/1/2/3"},
        ],
    )
    response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                grounding_metadata=SimpleNamespace(
                    grounding_chunks=[
                        SimpleNamespace(retrieved_context=context),
                        SimpleNamespace(retrieved_context=context),
                    ]
                )
            )
        ]
    )

    assert corpus_manager._response_sources(response) == ["[#開発（2026-10-05）](https://discord.com/channels/1/2/3)"]


def test_incremental_boundary_is_previous_jst_midnight():
    cursor = {"message_at": "2026-10-05T15:30:00+00:00"}

    assert calculate_incremental_after(cursor) == datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)


def test_documents_do_not_cross_jst_date_boundary(corpus_manager):
    before_midnight = corpus_manager._knowledge_message(fake_message(1))
    after_midnight = corpus_manager._knowledge_message(fake_message(2))
    before_midnight = before_midnight.__class__(
        **{**before_midnight.__dict__, "timestamp": datetime(2026, 10, 5, 14, 59, tzinfo=timezone.utc)}
    )
    after_midnight = after_midnight.__class__(
        **{**after_midnight.__dict__, "timestamp": datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)}
    )

    groups = corpus_manager._split_knowledge_messages([before_midnight, after_midnight])

    assert len(groups) == 2


@pytest.mark.asyncio
async def test_upload_waits_for_indexing_completion(corpus_manager):
    pending = SimpleNamespace(done=False, error=None, response=None)
    complete = SimpleNamespace(
        done=True,
        error=None,
        response=SimpleNamespace(document_name="documents/indexed"),
    )
    corpus_manager._client.file_search_stores.upload_to_file_search_store.return_value = pending
    corpus_manager._client.operations.get.return_value = complete

    with patch("bot.corpus.asyncio.sleep", new=AsyncMock()):
        result = await corpus_manager._upload_document(
            "fileSearchStores/test",
            "document",
            "本文",
            [{"key": "source", "string_value": "discord"}],
        )

    assert result == "documents/indexed"
    corpus_manager._client.operations.get.assert_called_once_with(pending)


@pytest.mark.asyncio
async def test_delete_corpus_force_deletes_documents_without_retry_loop(corpus_manager):
    document = SimpleNamespace(name="documents/one")
    corpus_manager._client.file_search_stores.documents.list.side_effect = [[document], []]

    await corpus_manager.delete_corpus("fileSearchStores/test")

    corpus_manager._client.file_search_stores.documents.delete.assert_called_once_with(
        name="documents/one",
        config={"force": True},
    )
    corpus_manager._client.file_search_stores.delete.assert_called_once_with(name="fileSearchStores/test")


@pytest.mark.asyncio
async def test_delete_corpus_aborts_when_page_makes_no_progress(corpus_manager):
    document = SimpleNamespace(name="documents/stuck")
    corpus_manager._client.file_search_stores.documents.list.return_value = [document]
    corpus_manager._client.file_search_stores.documents.delete.side_effect = RuntimeError("blocked")

    with pytest.raises(RuntimeError, match="made no progress"):
        await corpus_manager.delete_corpus("fileSearchStores/test")

    assert corpus_manager._client.file_search_stores.documents.list.call_count == 1


@pytest.mark.asyncio
async def test_query_filters_explicit_festival_and_records_trace(corpus_manager, tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RAG_FESTIVAL_FILTER_ENABLED", "true")
    corpus_manager.rag_store = RagStore(tmp_path / "rag.sqlite3")
    context = SimpleNamespace(
        title="整理券記録",
        uri=None,
        custom_metadata=[
            {"key": "channel_name", "string_value": "整理券"},
            {"key": "festival", "numeric_value": 27},
            {"key": "status", "string_value": "raw"},
        ],
    )
    response = SimpleNamespace(
        text="27thの回答",
        usage_metadata=None,
        candidates=[
            SimpleNamespace(
                grounding_metadata=SimpleNamespace(grounding_chunks=[SimpleNamespace(retrieved_context=context)])
            )
        ],
    )
    corpus_manager._client.aio.models.generate_content = AsyncMock(return_value=response)

    result = await corpus_manager.query_with_trace(
        "27thの整理券について",
        "fileSearchStores/test",
        guild_id=1,
    )

    config = corpus_manager._client.aio.models.generate_content.await_args.kwargs["config"]
    assert config.tools[0].file_search.metadata_filter == "festival = 27"
    assert result.festival == 27
    assert result.query_id
    assert "27thの回答" in result.text


@pytest.mark.asyncio
async def test_festival_filter_stays_off_during_legacy_migration(corpus_manager, tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_RAG_FESTIVAL_FILTER_ENABLED", "false")
    corpus_manager.rag_store = RagStore(tmp_path / "rag.sqlite3")
    response = SimpleNamespace(text="回答", usage_metadata=None, candidates=[])
    corpus_manager._client.aio.models.generate_content = AsyncMock(return_value=response)

    result = await corpus_manager.query_with_trace(
        "27thについて",
        "fileSearchStores/test",
        guild_id=1,
    )

    config = corpus_manager._client.aio.models.generate_content.await_args.kwargs["config"]
    assert config.tools[0].file_search.metadata_filter is None
    assert result.festival == 27
