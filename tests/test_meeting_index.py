from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.meeting_index import index_meeting_artifacts, meeting_document_key
from bot.rag_store import RagStore


@pytest.mark.asyncio
async def test_meeting_index_adds_date_festival_and_low_authority_metadata(tmp_path):
    rag_store = RagStore(tmp_path / "rag.sqlite3")
    corpus = SimpleNamespace(
        rag_store=rag_store,
        _upload_document=AsyncMock(return_value="documents/transcript"),
        _delete_documents=AsyncMock(return_value=0),
    )

    result = await index_meeting_artifacts(
        corpus,
        "fileSearchStores/test",
        guild_id=123,
        session_id="20261009T093251Z-d030a069",
        channel_name="全体会議部屋",
        started_at="2026-10-09T09:32:51Z",
        transcript="IT局へのフィードバックは準備を早めること。",
        transcript_url="https://docs.example/transcript",
    )

    assert result.transcript_document == "documents/transcript"
    args = corpus._upload_document.await_args.args
    assert args[0] == "fileSearchStores/test"
    assert "2026-10-09" in args[2]
    assert "10/9" in args[2]
    metadata = {
        item["key"]: item.get("string_value", item.get("numeric_value"))
        for item in args[3]
    }
    assert metadata["festival"] == 28.0
    assert metadata["source_type"] == "meeting_transcript"
    assert metadata["status"] == "raw"
    assert metadata["authority"] == "conversation"
    assert metadata["source_url"] == "https://docs.example/transcript"


@pytest.mark.asyncio
async def test_meeting_index_replaces_previous_remote_document_after_upload(tmp_path):
    rag_store = RagStore(tmp_path / "rag.sqlite3")
    key = meeting_document_key(123, "session", "transcript")
    rag_store.upsert_document(
        "documents/old",
        "old body",
        [
            {"key": "document_key", "string_value": key},
            {"key": "guild_id", "string_value": "123"},
        ],
    )
    corpus = SimpleNamespace(
        rag_store=rag_store,
        _upload_document=AsyncMock(return_value="documents/new"),
        _delete_documents=AsyncMock(return_value=1),
    )

    result = await index_meeting_artifacts(
        corpus,
        "fileSearchStores/test",
        guild_id=123,
        session_id="session",
        channel_name="役員反省会",
        started_at="2026-10-10T06:14:31Z",
        transcript="新しい文字起こし",
    )

    assert result.transcript_document == "documents/new"
    corpus._delete_documents.assert_awaited_once_with(["documents/old"])


@pytest.mark.asyncio
async def test_meeting_index_skips_identical_document(tmp_path):
    rag_store = RagStore(tmp_path / "rag.sqlite3")
    corpus = SimpleNamespace(
        rag_store=rag_store,
        _upload_document=AsyncMock(),
        _delete_documents=AsyncMock(),
    )
    kwargs = dict(
        guild_id=123,
        session_id="session",
        channel_name="役員反省会",
        started_at="2026-10-10T06:14:31Z",
        transcript="同じ文字起こし",
        transcript_url="https://docs.example/transcript",
    )
    first_corpus = SimpleNamespace(
        rag_store=rag_store,
        _delete_documents=AsyncMock(),
    )

    async def upload(_store, _display_name, body, metadata):
        rag_store.upsert_document("documents/first", body, metadata)
        return "documents/first"

    first_corpus._upload_document = AsyncMock(side_effect=upload)
    await index_meeting_artifacts(first_corpus, "fileSearchStores/test", **kwargs)

    result = await index_meeting_artifacts(corpus, "fileSearchStores/test", **kwargs)

    assert result.transcript_document == "documents/first"
    corpus._upload_document.assert_not_awaited()
