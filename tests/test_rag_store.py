import json
import sqlite3
import threading
from pathlib import Path

import pytest

import bot.rag_store as rag_store_module
from bot.rag_store import RagStore


def metadata(festival: int = 27):
    return [
        {"key": "guild_id", "string_value": "1"},
        {"key": "channel_id", "string_value": "2"},
        {"key": "channel_name", "string_value": "開発"},
        {"key": "access_scope", "string_value": "guild"},
        {"key": "auto_promote", "numeric_value": 1},
        {"key": "festival", "numeric_value": festival},
        {"key": "document_key", "string_value": f"doc-{festival}"},
        {"key": "source_url", "string_value": "https://discord.example/message"},
        {"key": "end_at", "string_value": "2026-09-01T00:00:00+09:00"},
    ]


def test_discord_lineage_survives_upsert_and_get_document(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    lineage = metadata(28) + [
        {"key": "parent_id", "string_value": "101"},
        {"key": "parent_name", "string_value": "応募者対応"},
        {"key": "category_id", "string_value": "202"},
        {"key": "category_name", "string_value": "選考"},
        {"key": "source_type", "string_value": "discord_conversation"},
    ]

    store.upsert_document("documents/lineage", "十分な長さの会話 " * 30, lineage)

    document = store.get_document("doc-28")
    assert document is not None
    assert document["parent_id"] == 101
    assert document["parent_name"] == "応募者対応"
    assert document["category_id"] == 202
    assert document["category_name"] == "選考"
    # The original name-only field remains populated for existing consumers.
    assert document["category"] == "選考"
    assert document["auto_promote"] == 1
    curated_source = store.list_curation_documents()
    assert curated_source[0]["parent_id"] == 101
    assert curated_source[0]["category_id"] == 202


def test_legacy_database_migrates_category_to_explicit_lineage(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE documents (
                document_key TEXT PRIMARY KEY,
                remote_name TEXT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER,
                channel_name TEXT,
                category TEXT,
                festival INTEGER,
                source_type TEXT,
                status TEXT,
                authority TEXT,
                access_scope TEXT,
                schema TEXT,
                source_url TEXT,
                start_at TEXT,
                end_at TEXT,
                body TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents (
                document_key, remote_name, guild_id, channel_id, channel_name,
                category, body, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy",
                "documents/legacy",
                1,
                2,
                "過去ログ",
                "27th IT局",
                "legacy body",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    store = RagStore(path)

    document = store.get_document("legacy")
    assert document is not None
    assert document["category"] == "27th IT局"
    assert document["category_name"] == "27th IT局"
    assert document["parent_id"] is None
    assert document["parent_name"] is None
    assert document["category_id"] is None
    assert document["auto_promote"] == 0
    assert document["upload_payload_sha256"] is None
    assert store.search_exact(
        1,
        ["legacy body"],
        allow_unrestricted_conversations=True,
    )[0].document_key == "legacy"

    with sqlite3.connect(path) as connection:
        pending_upload_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'pending_upload_operations'
            """
        ).fetchone()
        fts_version = connection.execute(
            "SELECT value FROM rag_store_meta WHERE key = 'documents_fts_version'"
        ).fetchone()
    assert pending_upload_table is not None
    assert fts_version == ("1",)


def test_trigram_index_tracks_document_replacements(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    store.upsert_document(
        "documents/current",
        "総務系申請の提出方法を説明する",
        metadata(27),
    )

    assert store.search_exact(1, ["総務系申請"], festival=27)

    store.upsert_document(
        "documents/current",
        "新歓Webの更新方法を説明する",
        metadata(27),
    )

    assert store.search_exact(1, ["総務系申請"], festival=27) == []
    assert store.search_exact(1, ["新歓Web"], festival=27)


def test_large_legacy_index_build_does_not_block_startup_or_search(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "rag.sqlite3"
    seed = RagStore(path)
    seed.upsert_document(
        "documents/current",
        "総務系申請の提出方法を説明する",
        metadata(27),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM rag_store_meta WHERE key = 'documents_fts_version'"
        )

    rebuild_started = threading.Event()
    release_rebuild = threading.Event()

    def blocked_rebuild(_store):
        rebuild_started.set()
        release_rebuild.wait(5)

    monkeypatch.setattr(
        rag_store_module,
        "DOCUMENTS_FTS_SYNC_REBUILD_MAX_CHARS",
        0,
    )
    monkeypatch.setattr(RagStore, "_rebuild_fts_in_background", blocked_rebuild)

    store = RagStore(path)
    assert rebuild_started.wait(1)
    assert store.wait_for_fts_index(0) is False
    # An index that has not atomically committed must never hide documents.
    assert store.search_exact(1, ["総務系申請"], festival=27)

    release_rebuild.set()
    store._fts_rebuild_thread.join(timeout=1)


def test_large_legacy_index_build_commits_in_background(tmp_path, monkeypatch):
    path = tmp_path / "rag.sqlite3"
    seed = RagStore(path)
    seed.upsert_document(
        "documents/current",
        "新歓Webの更新方法を説明する",
        metadata(27),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM rag_store_meta WHERE key = 'documents_fts_version'"
        )

    monkeypatch.setattr(
        rag_store_module,
        "DOCUMENTS_FTS_SYNC_REBUILD_MAX_CHARS",
        0,
    )
    store = RagStore(path)

    assert store.wait_for_fts_index(5)
    assert store.search_exact(1, ["新歓Web"], festival=27)
    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM rag_store_meta WHERE key = 'documents_fts_version'"
        ).fetchone()
    assert version == ("1",)


def test_upload_operation_survives_restart_and_completes_atomically(tmp_path):
    path = tmp_path / "rag.sqlite3"
    store = RagStore(path)
    upload_metadata = metadata(28)
    pending, claimed = store.claim_upload_operation(
        document_key="doc-28",
        store_name="fileSearchStores/test",
        display_name="document",
        text="new body",
        metadata=upload_metadata,
        superseded_remote_names=("documents/old",),
    )
    assert claimed is True
    store.record_upload_operation(
        "doc-28",
        pending["payload_sha256"],
        "fileSearchStores/test/operations/upload-1",
    )

    reopened = RagStore(path)
    durable = reopened.get_upload_operation("doc-28")
    assert durable is not None
    assert durable["state"] == "accepted"
    assert durable["operation_name"] == (
        "fileSearchStores/test/operations/upload-1"
    )

    reopened.record_upload_result(
        "doc-28",
        durable["payload_sha256"],
        "documents/new",
    )
    reopened.upsert_document(
        "documents/new",
        "new body",
        upload_metadata,
        superseded_remote_names=("documents/old",),
        completed_upload_content_sha256=durable["content_sha256"],
        upload_payload_sha256=durable["payload_sha256"],
    )

    assert reopened.get_upload_operation("doc-28") is None
    completed = reopened.get_document("doc-28")
    assert completed["remote_name"] == "documents/new"
    assert completed["upload_payload_sha256"] == durable["payload_sha256"]
    assert reopened.pending_document_deletions("doc-28") == ["documents/old"]


@pytest.mark.parametrize("transition", ["operation", "result"])
def test_late_upload_worker_cannot_overwrite_new_same_body_payload(
    tmp_path,
    transition,
):
    store = RagStore(tmp_path / "rag.sqlite3")
    old, claimed = store.claim_upload_operation(
        document_key="same-body",
        store_name="fileSearchStores/test",
        display_name="document",
        text="same body",
        metadata=[
            {"key": "document_key", "string_value": "same-body"},
            {"key": "access_scope", "string_value": "public"},
        ],
    )
    assert claimed is True
    assert store.resolve_upload_operation(
        "same-body",
        old["payload_sha256"],
    ) is True
    new, claimed = store.claim_upload_operation(
        document_key="same-body",
        store_name="fileSearchStores/test",
        display_name="document",
        text="same body",
        metadata=[
            {"key": "document_key", "string_value": "same-body"},
            {"key": "access_scope", "string_value": "restricted"},
        ],
    )
    assert claimed is True
    assert new["payload_sha256"] != old["payload_sha256"]

    with pytest.raises(RuntimeError, match="missing or changed"):
        if transition == "operation":
            store.record_upload_operation(
                "same-body",
                old["payload_sha256"],
                "operations/stale",
            )
        else:
            store.record_upload_result(
                "same-body",
                old["payload_sha256"],
                "documents/stale",
            )

    durable = store.get_upload_operation("same-body")
    assert durable is not None
    assert durable["payload_sha256"] == new["payload_sha256"]
    assert durable["operation_name"] is None
    assert durable["remote_name"] is None


def test_connection_sets_busy_timeout(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")

    with store._connect() as connection:
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert busy_timeout == 5_000


def test_mirror_write_retries_only_lock_contention(tmp_path, monkeypatch):
    store = RagStore(tmp_path / "rag.sqlite3")
    real_connect = store._connect
    attempts = 0

    def intermittently_locked():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise sqlite3.OperationalError("database is locked")
        return real_connect()

    monkeypatch.setattr(store, "_connect", intermittently_locked)
    monkeypatch.setattr("bot.rag_store.time.sleep", lambda _seconds: None)

    store.delete_source_documents("source_code")

    assert attempts == 3


def test_mirror_write_does_not_retry_non_lock_errors(tmp_path, monkeypatch):
    store = RagStore(tmp_path / "rag.sqlite3")
    attempts = 0

    def broken_database():
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_connect", broken_database)

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        store.delete_source_documents("source_code")

    assert attempts == 1


def test_exact_search_honors_festival_filter(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    store.upsert_document("documents/27", "棒金申請の担当を確認する", metadata(27))
    store.upsert_document("documents/28", "棒金申請は廃止した", metadata(28))

    hits = store.search_exact(1, ["棒金申請"], festival=27)

    assert [hit.document_key for hit in hits] == ["doc-27"]
    assert "担当を確認" in hits[0].excerpt


def test_exact_search_excerpt_prefers_dense_feature_section_in_long_source(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    body = (
        "Firestore import and general setup. "
        + ("unrelated preface " * 100)
        + "ranking implementation reads Firestore ranking documents. "
        + ("ranking score category eventName " * 20)
    )
    store.upsert_document("documents/ranking", body, metadata(27))

    hits = store.search_exact(
        1,
        ["ranking", "Firestore"],
        festival=27,
        allow_unrestricted_conversations=True,
    )

    assert len(hits) == 1
    assert "ranking implementation reads Firestore" in hits[0].excerpt
    assert "ranking score category eventName" in hits[0].excerpt


def test_exact_search_enforces_channel_visibility(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    for channel_id in (10, 20):
        store.upsert_document(
            f"documents/{channel_id}",
            "非公開チャンネルの決定事項",
            [
                {"key": "guild_id", "string_value": "1"},
                {"key": "channel_id", "string_value": str(channel_id)},
                {"key": "document_key", "string_value": f"private-{channel_id}"},
                {"key": "access_scope", "string_value": "channel"},
                {"key": "authority", "string_value": "conversation"},
                {"key": "status", "string_value": "raw"},
            ],
        )

    hits = store.search_exact(
        1,
        ["決定事項"],
        allowed_channel_ids=(10,),
    )

    assert [hit.document_key for hit in hits] == ["private-10"]


def test_document_references_include_guild_and_global_records(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    store.upsert_document("documents/guild", "guild", metadata(28))
    global_metadata = metadata(28)
    for item in global_metadata:
        if item["key"] == "guild_id":
            item["string_value"] = "0"
        if item["key"] == "document_key":
            item["string_value"] = "global"
    store.upsert_document("documents/global", "global", global_metadata)

    references = store.document_references(1)

    assert {item["document_key"] for item in references} == {"doc-28", "global"}


def test_backfill_inventory_uses_only_locally_mirrored_discord_documents(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    discord_metadata = metadata(28) + [
        {"key": "source_type", "string_value": "discord_conversation"},
        {"key": "status", "string_value": "raw"},
        {"key": "authority", "string_value": "conversation"},
        {"key": "schema", "string_value": "discord-v3"},
        {"key": "start_at", "string_value": "2026-08-31T23:00:00+09:00"},
    ]
    store.upsert_document(
        "documents/discord",
        "会話本文",
        discord_metadata,
    )
    knowledge_metadata = metadata(28) + [
        {"key": "document_key", "string_value": "knowledge-doc"},
        {"key": "source_type", "string_value": "knowledge_repository"},
    ]
    store.upsert_document(
        "documents/knowledge",
        "確定仕様",
        knowledge_metadata,
    )

    inventory = store.backfill_inventory(1)

    assert len(inventory) == 1
    assert inventory[0]["name"] == "documents/discord"
    assert inventory[0]["metadata"]["guild_id"] == 1
    assert inventory[0]["metadata"]["channel_id"] == 2
    assert inventory[0]["metadata"]["source_type"] == "discord_conversation"
    assert inventory[0]["metadata"]["start_at"] == "2026-08-31T23:00:00+09:00"
    assert inventory[0]["metadata"]["end_at"] == "2026-09-01T00:00:00+09:00"


def test_restore_document_snapshots_replaces_partial_generation_atomically(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    old_metadata = metadata(27)
    store.upsert_document("documents/old", "old body", old_metadata)
    old_snapshot = store.get_document("doc-27")

    store.upsert_document("documents/new", "new body", old_metadata)
    new_only_metadata = metadata(28)
    store.upsert_document("documents/new-only", "partial body", new_only_metadata)

    store.restore_document_snapshots(
        ["doc-27", "doc-28"],
        [old_snapshot],
    )

    restored = store.get_document("doc-27")
    assert restored["remote_name"] == "documents/old"
    assert restored["body"] == "old body"
    assert store.get_document("doc-28") is None


def test_exact_search_honors_calendar_date_filter(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    first = metadata(28) + [
        {"key": "document_key", "string_value": "meeting-2025"},
        {"key": "start_at", "string_value": "2025-10-09T18:00:00+09:00"},
    ]
    second = metadata(28) + [
        {"key": "document_key", "string_value": "meeting-2026"},
        {"key": "start_at", "string_value": "2026-10-09T18:00:00+09:00"},
    ]
    store.upsert_document("documents/2025", "10/9のIT局反省会", first)
    store.upsert_document("documents/2026", "10/9のIT局反省会", second)

    hits = store.search_exact(1, ["IT局反省会"], festival=28, target_date="2026-10-09")

    assert [hit.document_key for hit in hits] == ["meeting-2026"]


def test_feedback_is_aggregated_without_storing_content_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("YAGAPON_RAG_TRACE_CONTENT", raising=False)
    store = RagStore(tmp_path / "rag.sqlite3")
    query_id = store.record_query(
        guild_id=1,
        actor_id=10,
        channel_id=20,
        question="秘密の質問",
        answer="回答",
        festival=27,
        metadata_filter="festival = 27",
        citations=["source"],
        lexical_keys=[],
        latency_ms=123,
        no_answer=False,
    )
    store.bind_response_message(query_id, 1, 20, 30)

    assert store.record_feedback(30, 10, "✅", "positive") is True
    assert store.recent_queries(1)[0]["question_preview"] is None
    assert store.summary(1) == {
        "queries": 1,
        "no_answer": 0,
        "average_latency_ms": 123,
        "feedback": {"positive": 1},
        "positive_rate": 1.0,
        "local_documents": 0,
        "documents_by_festival": {},
        "documents_by_source": [],
    }

    assert store.remove_feedback(30, 10, "⚠️") is False
    assert store.remove_feedback(30, 10, "✅") is True
    assert store.summary(1)["feedback"] == {}


def test_exact_search_reranks_curated_documents(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    raw = metadata(27) + [
        {"key": "document_key", "string_value": "raw"},
        {"key": "authority", "string_value": "conversation"},
        {"key": "status", "string_value": "raw"},
    ]
    curated = metadata(27) + [
        {"key": "document_key", "string_value": "curated"},
        {"key": "authority", "string_value": "curated"},
        {"key": "status", "string_value": "approved"},
    ]
    store.upsert_document("documents/raw", "棒金申請の記録", raw)
    store.upsert_document("documents/curated", "棒金申請の記録", curated)

    hits = store.search_exact(1, ["棒金申請"], festival=27)

    assert [hit.document_key for hit in hits] == ["curated", "raw"]
    assert hits[0].score > hits[1].score
    assert hits[0].authority == "curated"
    assert hits[0].status == "approved"


def test_exact_search_ranks_canonical_above_curated(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    for authority in ("curated", "canonical"):
        store.upsert_document(
            f"documents/{authority}",
            "整理券の正式な権限契約",
            metadata(27)
            + [
                {"key": "document_key", "string_value": authority},
                {"key": "authority", "string_value": authority},
                {"key": "status", "string_value": "verified"},
            ],
        )

    hits = store.search_exact(1, ["整理券"], festival=27)

    assert [hit.document_key for hit in hits] == ["canonical", "curated"]
    assert hits[0].score > hits[1].score


def test_exact_search_includes_global_trusted_documents(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    global_metadata = [
        {"key": "guild_id", "numeric_value": 0},
        {"key": "document_key", "string_value": "builtin:drive"},
        {"key": "channel_name", "string_value": "Bot運用仕様"},
        {"key": "authority", "string_value": "curated"},
        {"key": "status", "string_value": "current"},
        {"key": "source_type", "string_value": "builtin_reference"},
        {"key": "access_scope", "string_value": "guild"},
    ]
    store.upsert_document("builtin://drive", "文字起こしは専用フォルダへ保存する", global_metadata)

    hits = store.search_exact(999, ["文字起こし"], festival=28)

    assert [hit.document_key for hit in hits] == ["builtin:drive"]


def test_year_filter_still_excludes_unlabelled_guild_documents(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    store.upsert_document(
        "documents/legacy",
        "文字起こしは昔の場所へ保存する",
        [
            {"key": "guild_id", "numeric_value": 1},
            {"key": "document_key", "string_value": "legacy"},
        ],
    )

    assert store.search_exact(1, ["文字起こし"], festival=28) == []


def test_year_filter_includes_timeless_curated_repository_document(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    store.upsert_document(
        "knowledge://retry",
        "PR作成に失敗した草案は次回の再試行まで保存する",
        [
            {"key": "guild_id", "numeric_value": 1},
            {"key": "document_key", "string_value": "knowledge:retry"},
            {"key": "authority", "string_value": "curated"},
            {"key": "status", "string_value": "active"},
            {"key": "source_type", "string_value": "knowledge_repository"},
            {"key": "access_scope", "string_value": "guild"},
        ],
    )

    hits = store.search_exact(1, ["再試行"], festival=28)

    assert [hit.document_key for hit in hits] == ["knowledge:retry"]


def test_year_filter_excludes_inactive_timeless_curated_document(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    store.upsert_document(
        "knowledge://deprecated",
        "再試行についての古い運用",
        [
            {"key": "guild_id", "numeric_value": 1},
            {"key": "document_key", "string_value": "knowledge:deprecated"},
            {"key": "authority", "string_value": "curated"},
            {"key": "status", "string_value": "deprecated"},
            {"key": "source_type", "string_value": "knowledge_repository"},
        ],
    )

    assert store.search_exact(1, ["再試行"], festival=28) == []


def test_year_filter_excludes_timeless_document_from_unapproved_source_type(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    store.upsert_document(
        "source://unlabelled-code",
        "再試行についてのコードコメント",
        [
            {"key": "guild_id", "numeric_value": 1},
            {"key": "document_key", "string_value": "source:unlabelled-code"},
            {"key": "authority", "string_value": "curated"},
            {"key": "status", "string_value": "current"},
            {"key": "source_type", "string_value": "source_code"},
        ],
    )

    assert store.search_exact(1, ["再試行"], festival=28) == []


def test_curated_global_document_survives_sql_candidate_limit(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    store.upsert_document(
        "builtin://access",
        "整理券の確定した権限契約",
        [
            {"key": "guild_id", "numeric_value": 0},
            {"key": "document_key", "string_value": "builtin:access"},
            {"key": "festival", "numeric_value": 27},
            {"key": "authority", "string_value": "curated"},
            {"key": "status", "string_value": "verified"},
            {"key": "access_scope", "string_value": "guild"},
        ],
    )
    for index in range(20):
        store.upsert_document(
            f"documents/raw-{index}",
            f"整理券についての会話 {index}",
            [
                {"key": "guild_id", "numeric_value": 1},
                {"key": "document_key", "string_value": f"raw-{index}"},
                {"key": "festival", "numeric_value": 27},
                {"key": "authority", "string_value": "conversation"},
                {"key": "status", "string_value": "raw"},
                {"key": "end_at", "string_value": f"2026-09-{index + 1:02d}"},
            ],
        )

    hits = store.search_exact(1, ["整理券"], festival=27, limit=4)

    assert hits[0].document_key == "builtin:access"


def test_query_terms_extracts_japanese_subjects_from_natural_questions():
    assert "ITラボ" in RagStore.query_terms("ITラボではどんなことをしていましたか？")

    terms = RagStore.query_terms(
        "27thの整理券機能で、企画メンバーと整理券管理者の権限はどう分かれていましたか？"
    )

    assert "整理券機能" in terms
    assert "企画メンバー" in terms
    assert "整理券管理者" in terms
    assert "27th" not in terms

    assert "26th" in RagStore.query_terms("26thの年度区分はいつからいつまでですか")

    assert RagStore.query_terms("YILの読み方と施設概要を教えてください") == ["YIL"]

    assert RagStore.query_terms("今日はこれについて教えてください") == []


@pytest.mark.parametrize(
    ("question", "expected"),
    (
        ("通知は？", "通知"),
        ("投票は？", "投票"),
        ("会場は？", "会場"),
        ("地図は？", "地図"),
        ("権限は？", "権限"),
        ("担当は？", "担当"),
    ),
)
def test_query_terms_keep_high_information_two_kanji_subjects(question, expected):
    assert expected in RagStore.query_terms(question)


def test_query_terms_split_common_operational_compounds():
    terms = RagStore.query_terms("27thの整理券運用で問題になった点は何ですか？")

    assert "整理券運用" in terms
    assert "整理券" in terms
    assert "運用" not in terms
    assert "なった点" not in terms


def test_query_terms_drop_generic_history_noise_but_keep_subject():
    terms = RagStore.query_terms(
        "10/9の会議内容では、IT局の運用で問題になった点は何ですか？"
    )

    assert "IT局" in terms
    assert not {"会議内容", "運用", "問題", "問題点", "なった点"} & set(terms)


def test_history_search_reserves_conversation_before_repository_history_limit(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    common = [
        {"key": "guild_id", "numeric_value": 1},
        {"key": "festival", "numeric_value": 27},
        {"key": "access_scope", "string_value": "guild"},
    ]
    for index in range(100):
        store.upsert_document(
            f"documents/history-{index}",
            f"新歓Webの変更履歴。外部からの要求を実装したコミット {index}",
            [
                *common,
                {"key": "document_key", "string_value": f"history-{index}"},
                {"key": "authority", "string_value": "repository_history"},
                {"key": "status", "string_value": "historical"},
                {"key": "source_type", "string_value": "source_code_history"},
                {"key": "end_at", "string_value": f"2026-09-{(index % 28) + 1:02d}"},
            ],
        )
    store.upsert_document(
        "documents/meeting",
        "新歓Webについて他局から要求された内容は、申請期限の表示だった。",
        [
            *common,
            {"key": "document_key", "string_value": "meeting"},
            {"key": "authority", "string_value": "conversation"},
            {"key": "status", "string_value": "raw"},
            {"key": "source_type", "string_value": "meeting_transcript"},
        ],
    )

    hits = store.search_exact(
        1,
        ["新歓Web", "他局", "要求"],
        festival=27,
        question_kind="history",
        limit=4,
    )

    assert "meeting" in {hit.document_key for hit in hits}
    assert [hit.score for hit in hits] == sorted(
        (hit.score for hit in hits), reverse=True
    )


def test_history_reservation_does_not_bypass_channel_acl(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    for key, channel_id in (("allowed", 10), ("private", 99)):
        store.upsert_document(
            f"documents/{key}",
            f"新歓Webについて他局から要求された内容 {key}",
            [
                {"key": "guild_id", "numeric_value": 1},
                {"key": "document_key", "string_value": key},
                {"key": "festival", "numeric_value": 27},
                {"key": "access_scope", "string_value": "channel"},
                {"key": "channel_id", "numeric_value": channel_id},
                {"key": "authority", "string_value": "conversation"},
                {"key": "status", "string_value": "raw"},
                {"key": "source_type", "string_value": "discord_conversation"},
            ],
        )

    hits = store.search_exact(
        1,
        ["新歓Web", "他局", "要求"],
        festival=27,
        allowed_channel_ids=(10,),
        question_kind="history",
        limit=4,
    )

    assert [hit.document_key for hit in hits] == ["allowed"]


def test_sql_candidate_limit_uses_relevance_before_recency(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    common = [
        {"key": "guild_id", "numeric_value": 1},
        {"key": "festival", "numeric_value": 27},
        {"key": "access_scope", "string_value": "guild"},
        {"key": "authority", "string_value": "repository_history"},
        {"key": "status", "string_value": "historical"},
        {"key": "source_type", "string_value": "source_code_history"},
    ]
    for index in range(100):
        store.upsert_document(
            f"documents/generic-{index}",
            f"新歓Webの一般的な変更 {index}",
            [
                *common,
                {"key": "document_key", "string_value": f"generic-{index}"},
                {"key": "end_at", "string_value": "2026-09-30"},
            ],
        )
    store.upsert_document(
        "documents/specific",
        "新歓Webについて外部と他局から受けた要求をまとめた古い記録。",
        [
            *common,
            {"key": "document_key", "string_value": "specific"},
            {"key": "end_at", "string_value": "2025-01-01"},
        ],
    )

    hits = store.search_exact(
        1,
        ["新歓Web", "外部", "他局", "要求"],
        festival=27,
        limit=4,
    )

    assert hits[0].document_key == "specific"


def test_data_contract_filters_non_direct_history_before_candidate_limit(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    common = [
        {"key": "guild_id", "numeric_value": 1},
        {"key": "festival", "numeric_value": 27},
        {"key": "access_scope", "string_value": "guild"},
        {"key": "source_type", "string_value": "source_code_history"},
    ]
    for index in range(100):
        store.upsert_document(
            f"documents/history-{index}",
            f"ranking Firestore 構造の古い変更履歴 {index}",
            [
                *common,
                {"key": "document_key", "string_value": f"history-{index}"},
                {"key": "authority", "string_value": "repository_history"},
                {"key": "status", "string_value": "historical"},
                {"key": "end_at", "string_value": "2026-09-30"},
            ],
        )
    store.upsert_document(
        "documents/current-contract",
        "ranking Firestore 構造の現行コレクション契約",
        [
            {"key": "guild_id", "numeric_value": 1},
            {"key": "festival", "numeric_value": 27},
            {"key": "access_scope", "string_value": "guild"},
            {"key": "document_key", "string_value": "current-contract"},
            {"key": "authority", "string_value": "repository_context"},
            {"key": "status", "string_value": "current"},
            {"key": "source_type", "string_value": "source_code"},
            {"key": "end_at", "string_value": "2025-01-01"},
        ],
    )

    hits = store.search_exact(
        1,
        ["ranking", "Firestore", "構造"],
        festival=27,
        question_kind="data_contract",
        limit=4,
    )

    assert [hit.document_key for hit in hits] == ["current-contract"]


def test_deployed_contract_survives_wordier_repository_candidate_crowd_out(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    common = [
        {"key": "guild_id", "numeric_value": 1},
        {"key": "festival", "numeric_value": 27},
        {"key": "access_scope", "string_value": "guild"},
        {"key": "source_type", "string_value": "source_code"},
    ]
    for index in range(100):
        store.upsert_document(
            f"documents/repository-{index}",
            f"ranking Firestore 構造 権限 repository snapshot {index}",
            [
                *common,
                {"key": "document_key", "string_value": f"repository-{index}"},
                {"key": "authority", "string_value": "repository_context"},
                {"key": "status", "string_value": "current"},
            ],
        )
    store.upsert_document(
        "documents/deployed-contract",
        "ranking Firestore deployed contract",
        [
            *common,
            {"key": "document_key", "string_value": "deployed-contract"},
            {"key": "authority", "string_value": "deployed_implementation"},
            {"key": "status", "string_value": "deployed"},
        ],
    )

    hits = store.search_exact(
        1,
        ["ranking", "Firestore", "構造", "権限"],
        festival=27,
        question_kind="data_contract",
        limit=4,
    )

    assert hits[0].document_key == "deployed-contract"
    assert hits[0].score < hits[1].score
    assert len(hits) == 4


def test_strict_search_preserves_curated_contract_among_deployed_fragments(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    common = [
        {"key": "guild_id", "numeric_value": 1},
        {"key": "festival", "numeric_value": 27},
        {"key": "access_scope", "string_value": "guild"},
    ]
    for index in range(100):
        store.upsert_document(
            f"documents/deployed-{index}",
            f"Google Drive 議事録 source fragment {index}",
            [
                *common,
                {"key": "document_key", "string_value": f"deployed-{index}"},
                {"key": "authority", "string_value": "deployed_implementation"},
                {"key": "status", "string_value": "deployed"},
                {"key": "source_type", "string_value": "source_code"},
            ],
        )
    store.upsert_document(
        "documents/curated-drive-contract",
        "Google Drive GoogleDrive 議事録の保存先は会議フォルダです",
        [
            *common,
            {"key": "document_key", "string_value": "curated-drive-contract"},
            {"key": "authority", "string_value": "curated"},
            {"key": "status", "string_value": "active"},
            {"key": "source_type", "string_value": "builtin_reference"},
        ],
    )

    hits = store.search_exact(
        1,
        ["Google", "Drive", "GoogleDrive", "議事録"],
        festival=27,
        question_kind="data_contract",
        limit=4,
    )

    assert hits[0].authority == "deployed_implementation"
    assert any(hit.document_key == "curated-drive-contract" for hit in hits)


def test_current_question_excludes_stale_implementation_before_limit(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    common = [
        {"key": "guild_id", "numeric_value": 1},
        {"key": "festival", "numeric_value": 27},
        {"key": "access_scope", "string_value": "guild"},
        {"key": "source_type", "string_value": "source_code"},
    ]
    cases = (
        ("repository_context", "historical"),
        ("repository_context", "unknown"),
        ("repository_context", "current"),
        ("deployed_implementation", "historical"),
        ("deployed_implementation", "unknown"),
        ("deployed_implementation", "deployed"),
    )
    for authority, status in cases:
        key = f"{authority}-{status}"
        store.upsert_document(
            f"documents/{key}",
            f"整理券 権限 implementation {key}",
            [
                *common,
                {"key": "document_key", "string_value": key},
                {"key": "authority", "string_value": authority},
                {"key": "status", "string_value": status},
            ],
        )

    hits = store.search_exact(
        1,
        ["整理券", "権限"],
        festival=27,
        question_kind="authorization",
        limit=4,
    )

    assert [hit.document_key for hit in hits] == [
        "deployed_implementation-deployed",
        "repository_context-current",
    ]


def test_query_terms_keep_cross_team_and_data_contract_cues():
    shinkan = RagStore.query_terms("新歓webで外部や他局から要求された内容をまとめて")
    ranking = RagStore.query_terms("27thのrankingの構造と票数を教えて")

    assert {"新歓web", "外部", "他局", "要求"}.issubset(
        {term.casefold() for term in shinkan}
    )
    assert {"ranking", "構造", "票数"}.issubset(
        {term.casefold() for term in ranking}
    )


def test_query_terms_decompose_compound_request_and_implementation_question():
    terms = RagStore.query_terms(
        "新歓Webで外部や他局から要求があった内容と、その後コードでどう対応したかを、"
        "時系列と根拠付きでまとめてください"
    )

    assert {"新歓web", "外部", "他局", "要求", "コード", "対応"}.issubset(
        {term.casefold() for term in terms}
    )
    assert not {"時系列", "根拠付き", "外部や他局", "後コード"} & set(terms)


def test_query_terms_expand_request_and_response_history_vocabulary():
    terms = RagStore.query_terms(
        "新歓Webで外部や他局から要求された内容と対応をまとめて"
    )

    assert "Web" not in terms
    assert "要求された内容" not in terms
    assert {
        "新歓web",
        "外部",
        "他局",
        "要求",
        "依頼",
        "要望",
        "規制",
        "削除",
        "修正",
        "変更",
    }.issubset({term.casefold() for term in terms})


def test_query_terms_decompose_connectors_and_predicates_without_domain_allowlist():
    terms = RagStore.query_terms(
        "スポンサーCRMで総務や渉外から要望が出たあと、実装でどのように反映したか"
    )

    assert {"スポンサーcrm", "総務", "渉外", "要望", "実装", "反映"}.issubset(
        {term.casefold() for term in terms}
    )


def test_query_terms_do_not_split_product_name_containing_ya():
    terms = RagStore.query_terms("やがサポについて教えてください")

    assert "やがサポ" in terms


def test_date_window_can_retrieve_activity_without_a_topic_term(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    for key, starts_at, body in (
        ("before", "2025-09-30T23:00:00+09:00", "前月の作業"),
        ("inside", "2025-10-12T18:00:00+09:00", "新歓Webの準備を始めた"),
        ("after", "2025-11-01T00:00:00+09:00", "翌月の作業"),
    ):
        store.upsert_document(
            f"documents/{key}",
            body,
            [
                {"key": "document_key", "string_value": key},
                {"key": "guild_id", "numeric_value": 1},
                {"key": "channel_id", "numeric_value": 10},
                {"key": "access_scope", "string_value": "channel"},
                {"key": "festival", "numeric_value": 27},
                {"key": "start_at", "string_value": starts_at},
                {"key": "end_at", "string_value": starts_at},
                {"key": "authority", "string_value": "conversation"},
                {"key": "status", "string_value": "raw"},
            ],
        )

    hits = store.search_exact(
        1,
        [],
        festival=27,
        date_from="2025-10-01",
        date_to="2025-10-31",
        allowed_channel_ids=(10,),
    )

    assert [hit.document_key for hit in hits] == ["inside"]


def test_date_window_includes_document_spanning_both_bounds(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    store.upsert_document(
        "documents/spanning",
        "長期間にわたる会議記録",
        [
            {"key": "document_key", "string_value": "spanning"},
            {"key": "guild_id", "numeric_value": 1},
            {"key": "channel_id", "numeric_value": 10},
            {"key": "access_scope", "string_value": "channel"},
            {"key": "festival", "numeric_value": 27},
            {"key": "start_at", "string_value": "2025-09-20T00:00:00+09:00"},
            {"key": "end_at", "string_value": "2025-11-10T00:00:00+09:00"},
            {"key": "authority", "string_value": "conversation"},
            {"key": "status", "string_value": "raw"},
        ],
    )

    hits = store.search_exact(
        1,
        [],
        festival=27,
        date_from="2025-10-01",
        date_to="2025-10-31",
        allowed_channel_ids=(10,),
    )

    assert [hit.document_key for hit in hits] == ["spanning"]


def test_date_window_rejects_invalid_or_conflicting_bounds(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")

    with pytest.raises(ValueError, match="mutually exclusive"):
        store.search_exact(
            1,
            ["会議"],
            target_date="2025-10-09",
            date_from="2025-10-01",
        )
    with pytest.raises(ValueError, match="date_from must be YYYY-MM-DD"):
        store.search_exact(1, ["会議"], date_from="last-month")
    with pytest.raises(ValueError, match="must not be after"):
        store.search_exact(
            1,
            ["会議"],
            date_from="2025-11-01",
            date_to="2025-10-01",
        )


def test_trusted_terminology_is_retrievable_for_room_and_yil_questions(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    knowledge_path = Path(__file__).parents[1] / "bot" / "trusted_knowledge.json"
    records = json.loads(knowledge_path.read_text(encoding="utf-8"))
    for record in records:
        trusted_metadata = [
            {"key": "document_key", "string_value": record["document_key"]},
            {"key": "guild_id", "numeric_value": 0},
            {"key": "channel_name", "string_value": record["title"]},
            {"key": "authority", "string_value": "curated"},
            {"key": "status", "string_value": record["status"]},
            {"key": "source_type", "string_value": "builtin_reference"},
            {"key": "access_scope", "string_value": "guild"},
            {"key": "source_url", "string_value": record["source_url"]},
        ]
        if record.get("festival") is not None:
            trusted_metadata.append(
                {"key": "festival", "numeric_value": record["festival"]}
            )
        store.upsert_document(
            f"builtin://{record['document_key']}",
            record["body"],
            trusted_metadata,
        )

    room_hits = store.search_exact(
        999,
        RagStore.query_terms("11-21はどのように読みますか"),
        festival=28,
    )
    yil_hits = store.search_exact(
        999,
        RagStore.query_terms("YILとは何ですか"),
        festival=28,
    )

    assert room_hits[0].document_key == "builtin:yagami-room-pronunciation"
    assert "いちいちのにーいち" in room_hits[0].excerpt
    assert yil_hits[0].document_key == "builtin:yil-definition"
    assert "Yagami Innovation Laboratory" in yil_hits[0].excerpt

    year_hits = store.search_exact(
        999,
        RagStore.query_terms("26thの年度区分はいつからいつまでですか"),
        festival=26,
    )
    assert year_hits[0].document_key == "builtin:festival-operating-year"
    assert "2024年10月1日から2025年9月30日" in year_hits[0].excerpt
