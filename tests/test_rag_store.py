import json
import sqlite3
from pathlib import Path

import pytest

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


def test_query_terms_split_common_operational_compounds():
    terms = RagStore.query_terms("27thの整理券運用で問題になった点は何ですか？")

    assert "整理券運用" in terms
    assert "整理券" in terms
    assert "運用" in terms


def test_query_terms_keep_cross_team_and_data_contract_cues():
    shinkan = RagStore.query_terms("新歓webで外部や他局から要求された内容をまとめて")
    ranking = RagStore.query_terms("27thのrankingの構造と票数を教えて")

    assert {"新歓web", "外部", "他局", "要求"}.issubset(
        {term.casefold() for term in shinkan}
    )
    assert {"ranking", "構造", "票数"}.issubset(
        {term.casefold() for term in ranking}
    )


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
