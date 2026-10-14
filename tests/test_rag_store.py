import json
from pathlib import Path

from bot.rag_store import RagStore


def metadata(festival: int = 27):
    return [
        {"key": "guild_id", "string_value": "1"},
        {"key": "channel_id", "string_value": "2"},
        {"key": "channel_name", "string_value": "開発"},
        {"key": "festival", "numeric_value": festival},
        {"key": "document_key", "string_value": f"doc-{festival}"},
        {"key": "source_url", "string_value": "https://discord.example/message"},
        {"key": "end_at", "string_value": "2026-09-01T00:00:00+09:00"},
    ]


def test_exact_search_honors_festival_filter(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    store.upsert_document("documents/27", "棒金申請の担当を確認する", metadata(27))
    store.upsert_document("documents/28", "棒金申請は廃止した", metadata(28))

    hits = store.search_exact(1, ["棒金申請"], festival=27)

    assert [hit.document_key for hit in hits] == ["doc-27"]
    assert "担当を確認" in hits[0].excerpt


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


def test_exact_search_includes_global_trusted_documents(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    global_metadata = [
        {"key": "guild_id", "numeric_value": 0},
        {"key": "document_key", "string_value": "builtin:drive"},
        {"key": "channel_name", "string_value": "Bot運用仕様"},
        {"key": "authority", "string_value": "curated"},
        {"key": "status", "string_value": "current"},
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

    assert RagStore.query_terms("今日はこれについて教えてください") == []


def test_trusted_terminology_is_retrievable_for_room_and_yil_questions(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    knowledge_path = Path(__file__).parents[1] / "bot" / "trusted_knowledge.json"
    records = json.loads(knowledge_path.read_text(encoding="utf-8"))
    for record in records:
        store.upsert_document(
            f"builtin://{record['document_key']}",
            record["body"],
            [
                {"key": "document_key", "string_value": record["document_key"]},
                {"key": "guild_id", "numeric_value": 0},
                {"key": "channel_name", "string_value": record["title"]},
                {"key": "authority", "string_value": "curated"},
                {"key": "status", "string_value": record["status"]},
                {"key": "source_url", "string_value": record["source_url"]},
            ],
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
