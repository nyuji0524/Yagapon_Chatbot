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
