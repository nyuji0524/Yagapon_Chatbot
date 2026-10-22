from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.meeting_index import index_bot_meeting, index_meeting_artifacts, meeting_document_key
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
        category_name="03｜会議",
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
    assert metadata["category"] == "03｜会議"
    assert "カテゴリ: 03｜会議" in args[2]


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
    async def upload(
        _store,
        _display_name,
        body,
        metadata,
        *,
        superseded_remote_names=(),
    ):
        rag_store.upsert_document(
            "documents/new",
            body,
            metadata,
            superseded_remote_names=superseded_remote_names,
        )
        return "documents/new"

    corpus = SimpleNamespace(
        rag_store=rag_store,
        _upload_document=AsyncMock(side_effect=upload),
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
async def test_meeting_index_retries_superseded_remote_deletion(tmp_path):
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
    async def upload(
        _store,
        _display_name,
        body,
        metadata,
        *,
        superseded_remote_names=(),
    ):
        rag_store.upsert_document(
            "documents/new",
            body,
            metadata,
            superseded_remote_names=superseded_remote_names,
        )
        return "documents/new"

    corpus = SimpleNamespace(
        rag_store=rag_store,
        _upload_document=AsyncMock(side_effect=upload),
        _delete_documents=AsyncMock(side_effect=[0, 1]),
    )
    kwargs = dict(
        guild_id=123,
        session_id="session",
        channel_name="役員反省会",
        started_at="2026-10-10T06:14:31Z",
        transcript="新しい文字起こし",
    )

    with pytest.raises(RuntimeError, match="Could not remove"):
        await index_meeting_artifacts(corpus, "fileSearchStores/test", **kwargs)

    assert rag_store.pending_document_deletions(key) == ["documents/old"]
    result = await index_meeting_artifacts(
        corpus,
        "fileSearchStores/test",
        **kwargs,
    )

    assert result.transcript_document == "documents/new"
    assert rag_store.pending_document_deletions(key) == []
    assert corpus._upload_document.await_count == 1


@pytest.mark.asyncio
async def test_meeting_index_treats_none_category_as_empty(tmp_path):
    corpus = SimpleNamespace(
        rag_store=RagStore(tmp_path / "rag.sqlite3"),
        _upload_document=AsyncMock(return_value="documents/transcript"),
        _delete_documents=AsyncMock(return_value=0),
    )

    await index_meeting_artifacts(
        corpus,
        "fileSearchStores/test",
        guild_id=123,
        session_id="session-none-category",
        channel_name="会議用",
        category_name=None,
        started_at="2026-10-10T06:14:31Z",
        transcript="文字起こし",
    )

    metadata = {
        item["key"]: item.get("string_value", item.get("numeric_value"))
        for item in corpus._upload_document.await_args.args[3]
    }
    assert metadata["category"] == ""


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

    async def upload(
        _store,
        _display_name,
        body,
        metadata,
        *,
        superseded_remote_names=(),
    ):
        rag_store.upsert_document(
            "documents/first",
            body,
            metadata,
            superseded_remote_names=superseded_remote_names,
        )
        return "documents/first"

    first_corpus._upload_document = AsyncMock(side_effect=upload)
    await index_meeting_artifacts(first_corpus, "fileSearchStores/test", **kwargs)

    result = await index_meeting_artifacts(corpus, "fileSearchStores/test", **kwargs)

    assert result.transcript_document == "documents/first"
    corpus._upload_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_meeting_index_replaces_identical_legacy_acl_metadata(tmp_path):
    rag_store = RagStore(tmp_path / "rag.sqlite3")
    kwargs = dict(
        guild_id=123,
        channel_id=456,
        session_id="legacy-session",
        channel_name="役員反省会",
        started_at="2026-10-10T06:14:31Z",
        transcript="同じ文字起こし",
    )
    first = SimpleNamespace(
        rag_store=rag_store,
        _delete_documents=AsyncMock(return_value=1),
    )

    async def initial_upload(
        _store,
        _display_name,
        body,
        metadata,
        *,
        superseded_remote_names=(),
    ):
        legacy = [item for item in metadata if item["key"] not in {"schema", "access_scope"}]
        rag_store.upsert_document(
            "documents/legacy",
            body,
            legacy,
            superseded_remote_names=superseded_remote_names,
        )
        return "documents/legacy"

    first._upload_document = AsyncMock(side_effect=initial_upload)
    await index_meeting_artifacts(first, "fileSearchStores/test", **kwargs)
    replacement = SimpleNamespace(
        rag_store=rag_store,
        _delete_documents=AsyncMock(return_value=1),
    )

    async def replacement_upload(
        _store,
        _display_name,
        body,
        metadata,
        *,
        superseded_remote_names=(),
    ):
        rag_store.upsert_document(
            "documents/current",
            body,
            metadata,
            superseded_remote_names=superseded_remote_names,
        )
        return "documents/current"

    replacement._upload_document = AsyncMock(side_effect=replacement_upload)

    result = await index_meeting_artifacts(
        replacement,
        "fileSearchStores/test",
        **kwargs,
    )

    assert result.transcript_document == "documents/current"
    replacement._upload_document.assert_awaited_once()
    replacement._delete_documents.assert_awaited_once_with(["documents/legacy"])


@pytest.mark.asyncio
async def test_meeting_index_repairs_local_only_identical_document(tmp_path):
    rag_store = RagStore(tmp_path / "rag.sqlite3")
    kwargs = dict(
        guild_id=123,
        session_id="local-only",
        channel_name="役員反省会",
        started_at="2026-10-10T06:14:31Z",
        transcript="同じ文字起こし",
    )

    async def first_upload(
        _store,
        _display_name,
        body,
        metadata,
        *,
        superseded_remote_names=(),
    ):
        rag_store.upsert_document(
            "documents/missing",
            body,
            metadata,
            superseded_remote_names=superseded_remote_names,
        )
        return "documents/missing"

    first = SimpleNamespace(
        rag_store=rag_store,
        _upload_document=AsyncMock(side_effect=first_upload),
        _delete_documents=AsyncMock(return_value=0),
    )
    await index_meeting_artifacts(first, "fileSearchStores/test", **kwargs)

    async def repair_upload(
        _store,
        _display_name,
        body,
        metadata,
        *,
        superseded_remote_names=(),
    ):
        rag_store.upsert_document(
            "documents/repaired",
            body,
            metadata,
            superseded_remote_names=superseded_remote_names,
        )
        return "documents/repaired"

    repaired = SimpleNamespace(
        rag_store=rag_store,
        list_remote_documents=AsyncMock(return_value=[]),
        _upload_document=AsyncMock(side_effect=repair_upload),
        _delete_documents=AsyncMock(return_value=1),
    )

    result = await index_meeting_artifacts(
        repaired,
        "fileSearchStores/test",
        **kwargs,
    )

    assert result.transcript_document == "documents/repaired"
    repaired._upload_document.assert_awaited_once()
    repaired._delete_documents.assert_awaited_once_with(["documents/missing"])


@pytest.mark.asyncio
async def test_meeting_index_globally_drains_pending_deletions_before_early_return(tmp_path):
    rag_store = RagStore(tmp_path / "rag.sqlite3")
    other_key = meeting_document_key(123, "other-session", "transcript")
    rag_store.queue_document_deletion(other_key, "documents/stale-other")
    corpus = SimpleNamespace(
        rag_store=rag_store,
        list_remote_documents=AsyncMock(return_value=[]),
        _upload_document=AsyncMock(return_value="documents/current"),
        _delete_documents=AsyncMock(return_value=1),
    )

    await index_meeting_artifacts(
        corpus,
        "fileSearchStores/test",
        guild_id=123,
        session_id="current-session",
        channel_name="会議",
        started_at="2026-10-10T06:14:31Z",
        transcript="保存対象",
    )

    corpus._delete_documents.assert_awaited_once_with(["documents/stale-other"])
    assert rag_store.pending_document_deletions() == []


@pytest.mark.asyncio
async def test_meeting_index_removes_only_duplicate_remote_with_local_expected_copy(tmp_path):
    rag_store = RagStore(tmp_path / "rag.sqlite3")
    kwargs = dict(
        guild_id=123,
        session_id="duplicate-session",
        channel_name="会議",
        started_at="2026-10-10T06:14:31Z",
        transcript="保存対象",
    )

    async def upload(
        _store,
        _display_name,
        body,
        metadata,
        *,
        superseded_remote_names=(),
    ):
        rag_store.upsert_document(
            "documents/expected",
            body,
            metadata,
            superseded_remote_names=superseded_remote_names,
        )
        return "documents/expected"

    first = SimpleNamespace(
        rag_store=rag_store,
        _upload_document=AsyncMock(side_effect=upload),
        _delete_documents=AsyncMock(return_value=0),
    )
    await index_meeting_artifacts(first, "fileSearchStores/test", **kwargs)
    key = meeting_document_key(123, "duplicate-session", "transcript")
    metadata = {
        "guild_id": "123",
        "source_type": "meeting_transcript",
        "document_key": key,
    }
    corpus = SimpleNamespace(
        rag_store=rag_store,
        list_remote_documents=AsyncMock(
            return_value=[
                {"name": "documents/expected", "metadata": metadata},
                {"name": "documents/crash-orphan", "metadata": metadata},
                {
                    "name": "documents/untracked",
                    "metadata": {
                        **metadata,
                        "document_key": "no-local-row",
                    },
                },
            ]
        ),
        _upload_document=AsyncMock(),
        _delete_documents=AsyncMock(return_value=1),
    )

    result = await index_meeting_artifacts(
        corpus,
        "fileSearchStores/test",
        **kwargs,
    )

    assert result.transcript_document == "documents/expected"
    corpus._delete_documents.assert_awaited_once_with(["documents/crash-orphan"])
    corpus._upload_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_meeting_index_raises_when_required_upload_fails(tmp_path):
    corpus = SimpleNamespace(
        rag_store=RagStore(tmp_path / "rag.sqlite3"),
        _upload_document=AsyncMock(return_value=None),
        _delete_documents=AsyncMock(return_value=0),
    )
    bot = SimpleNamespace(
        corpus=corpus,
        config=SimpleNamespace(get_corpus=lambda _guild_id: "fileSearchStores/test"),
    )

    with pytest.raises(RuntimeError, match="transcript was not indexed"):
        await index_bot_meeting(
            bot,
            guild_id=123,
            session_id="failed-upload",
            channel_name="会議",
            started_at="2026-10-10T06:14:31Z",
            transcript="保存対象",
        )


@pytest.mark.asyncio
async def test_bot_meeting_index_honors_ignore_for_channel_and_thread_parent(tmp_path):
    corpus = SimpleNamespace(
        rag_store=RagStore(tmp_path / "rag.sqlite3"),
        _upload_document=AsyncMock(),
    )
    bot = SimpleNamespace(
        corpus=corpus,
        config=SimpleNamespace(
            get_corpus=lambda _guild_id: "fileSearchStores/test",
            is_ignored=lambda guild_id, channel_id: guild_id == 123
            and channel_id == 50,
        ),
    )

    result = await index_bot_meeting(
        bot,
        guild_id=123,
        channel_id=51,
        parent_channel_id=50,
        session_id="ignored-thread",
        channel_name="非公開スレッド",
        started_at="2026-10-10T06:14:31Z",
        transcript="索引してはいけない内容",
    )

    assert result.indexed is False
    corpus._upload_document.assert_not_awaited()
