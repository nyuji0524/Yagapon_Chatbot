import json
from types import SimpleNamespace

import pytest
import yaml

from bot.rag_store import RagStore
from knowledge_curation.__main__ import (
    BatchPending,
    CurationPlan,
    DecisionDraft,
    KnowledgeCurationConfigurationError,
    _generate_json,
    build_bundle,
    classify_bureau,
    curate_once,
    curation_document_allowed,
    render_decision,
)
from knowledge_curation.topic import TopicStateStore


def source_document():
    return {
        "document_key": "discord-document-001",
        "guild_id": 123,
        "channel_name": "仕事に関する雑談",
        "category": "27th IT局",
        "festival": 27,
        "source_type": "discord_conversation",
        "authority": "conversation",
        "status": "raw",
        "source_url": "https://discord.com/channels/123/456/789",
        "start_at": "2026-07-08T09:46:00+09:00",
        "end_at": "2026-07-08T10:50:00+09:00",
        "body": "カレンダー機能は主要機能の後にすることで了承した。" * 20,
    }


def decision():
    return DecisionDraft(
        title="27th：カレンダー追加機能を延期する判断",
        slug="calendar-feature-priority",
        date="2026-07-08",
        decision_state="依頼者が延期を了承したところまで確認できる。",
        facts=["日をまたぐ予定の登録機能について相談した。"],
        contemporary_reasoning=["主要機能を優先する必要があると説明した。"],
        alternatives_and_constraints=["1日ずつ入力する代替案があった。"],
        results=["延期が了承された。"],
        unknowns=["後日の実装状況は未確認。"],
        related_roles=["IT局長"],
        systems=["やがサポート"],
        tags=["優先順位"],
    )


@pytest.mark.asyncio
async def test_batch_generation_is_durable_across_curation_cycles(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_USE_BATCH_API", "true")
    store = TopicStateStore(tmp_path / "state.sqlite3")
    response = SimpleNamespace(
        text=json.dumps(
            {
                "has_knowledge_value": True,
                "topic": "新歓Web",
                "aliases": [],
                "search_terms": ["新歓"],
                "reason": "継続的な実装記録",
            },
            ensure_ascii=False,
        ),
        candidates=[],
        usage_metadata=None,
    )
    create_calls = 0
    get_calls = 0

    async def create(**kwargs):
        nonlocal create_calls
        create_calls += 1
        assert kwargs["model"] == "gemini-3.8-flash"
        assert len(kwargs["src"]) == 1
        return SimpleNamespace(name="batches/knowledge-1", state=SimpleNamespace(name="JOB_STATE_PENDING"))

    async def get_batch(*, name):
        nonlocal get_calls
        get_calls += 1
        assert name == "batches/knowledge-1"
        return SimpleNamespace(
            name=name,
            state=SimpleNamespace(name="JOB_STATE_SUCCEEDED"),
            dest=SimpleNamespace(
                inlined_responses=[SimpleNamespace(response=response, error=None)]
            ),
        )

    client = SimpleNamespace(
        aio=SimpleNamespace(batches=SimpleNamespace(create=create, get=get_batch))
    )
    kwargs = {
        "prompt": "新歓Webについて整理する",
        "schema": CurationPlan,
        "operation": "knowledge_curation_plan",
        "batch_store": store,
    }

    with pytest.raises(BatchPending) as pending:
        await _generate_json(client, **kwargs)
    assert pending.value.submitted is True
    assert create_calls == 1

    result = await _generate_json(client, **kwargs)
    assert result.topic == "新歓Web"
    assert get_calls == 1

    # A crash after result retrieval must not submit or retrieve the same paid
    # request again; the parsed response remains in the durable SQLite ledger.
    cached = await _generate_json(client, **kwargs)
    assert cached == result
    assert create_calls == 1
    assert get_calls == 1


def test_rendered_curation_draft_separates_evidence_and_uncertainty():
    rendered = render_decision(
        source_document(),
        decision(),
        [
            {
                "document_key": "curated-history",
                "source_url": "https://github.com/YagamiFes-IT/YagamiFes-IT-Knowledge/blob/main/history.md",
                "authority": "curated",
                "status": "historical",
                "excerpt": "過去の記録",
            }
        ],
    )
    metadata = yaml.safe_load(rendered[4:].split("\n---\n", 1)[0])

    assert metadata["type"] == "decision"
    assert metadata["status"] == "draft"
    assert metadata["festival"] == 27
    assert metadata["bureau"] == "it"
    assert metadata["sources"][0]["ref"].startswith("https://discord.com/")
    assert "## 確認できる事実 / Fact" in rendered
    assert "## 当時の判断理由 / Contemporary reasoning" in rendered
    assert "## 推測 / Inference" in rendered
    assert "後日の実装状況は未確認" in rendered
    assert "人による確認後" in rendered


def test_curation_bundle_targets_review_only_candidate_area():
    args = build_bundle(source_document(), decision(), [])
    bundle_id, root, files, manifest, branch, title, body = args

    assert bundle_id.startswith("curation-discord-document-001-")
    assert root.startswith("years/27th/bureaus/it/decisions/candidates/")
    assert any(path.endswith("calendar-feature-priority.md") for path in files)
    assert json.loads(files[f"{root}/manifest.json"]) == manifest
    assert branch == f"yagapon/{bundle_id}"
    assert "確認待ち草案" in title
    assert "原文" in body


@pytest.mark.parametrize(
    ("category", "channel", "expected"),
    [
        ("28th 総務局", "一般", ("general-affairs", "総務局")),
        ("09連絡チャット", "it総務連絡チャット", ("cross-bureau", "局間連携")),
        ("01全体", "全体チャット", ("committee", "全体")),
        ("10その他", "雑談", ("unclassified", "未分類")),
    ],
)
def test_bureau_classification_uses_discord_structure(category, channel, expected):
    assert classify_bureau({"category": category, "channel_name": channel}) == expected


def test_bureau_classification_recovers_category_from_existing_document_header():
    document = {
        "channel_name": "一般",
        "body": "文書種別: Discord会話記録\nチャンネル: #一般\nカテゴリ: 27th 装飾局\n\n本文",
    }

    assert classify_bureau(document) == ("decoration", "装飾局")


def test_rag_store_lists_only_raw_curation_sources(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    raw_metadata = [
        {"key": "document_key", "string_value": "raw-discord"},
        {"key": "source_type", "string_value": "discord_conversation"},
        {"key": "authority", "string_value": "conversation"},
        {"key": "status", "string_value": "raw"},
        {"key": "guild_id", "string_value": "123"},
        {"key": "category", "string_value": "27th IT局"},
    ]
    store.upsert_document("documents/raw", "会話記録" * 100, raw_metadata)
    store.upsert_document(
        "documents/curated",
        "整理済み" * 100,
        [
            {"key": "document_key", "string_value": "curated"},
            {"key": "source_type", "string_value": "decision"},
            {"key": "authority", "string_value": "curated"},
            {"key": "guild_id", "string_value": "0"},
        ],
    )

    result = store.list_curation_documents()

    assert [item["document_key"] for item in result] == ["raw-discord"]
    assert result[0]["category"] == "27th IT局"


def test_rag_store_migrates_existing_database_with_category(tmp_path):
    database = tmp_path / "old.sqlite3"
    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE documents (
                document_key TEXT PRIMARY KEY, remote_name TEXT, guild_id INTEGER NOT NULL,
                channel_id INTEGER, channel_name TEXT, festival INTEGER, source_type TEXT,
                status TEXT, authority TEXT, source_url TEXT, start_at TEXT, end_at TEXT,
                body TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )

    store = RagStore(database)

    with store._connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(documents)")}
    assert "category" in columns


@pytest.mark.asyncio
async def test_curation_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_ENABLED", raising=False)

    result = await curate_once(state_path=tmp_path / "state.json", limit=3)

    assert result == {"enabled": False, "processed": 0, "queued": 0, "skipped": 0}


@pytest.mark.parametrize("value", [True, 1, 1.0, "true", " TRUE ", "yes", "on", "1"])
def test_autonomous_curation_accepts_trusted_auto_promote_metadata(monkeypatch, value):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", raising=False)
    document = {
        "guild_id": 123,
        "channel_id": 456,
        "access_scope": "channel",
        "auto_promote": value,
    }

    assert curation_document_allowed(document) is True
    assert curation_document_allowed(
        document,
        current_public_channel_ids={456},
    ) is True
    assert curation_document_allowed(
        document,
        current_public_channel_ids=set(),
    ) is False
    assert curation_document_allowed({**document, "channel_id": 0}) is False
    assert curation_document_allowed({**document, "guild_id": 999}) is False
    assert curation_document_allowed({**document, "access_scope": "restricted"}) is False


@pytest.mark.parametrize("value", [None, False, 0, 2, -1, 0.5, "", "false", "0", "anything"])
def test_autonomous_curation_rejects_unapproved_dynamic_channel_scope(
    monkeypatch,
    value,
):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", raising=False)

    assert curation_document_allowed(
        {
            "guild_id": 123,
            "channel_id": 456,
            "access_scope": "channel",
            "auto_promote": value,
        }
    ) is False


@pytest.mark.asyncio
async def test_curate_once_excludes_unapproved_channel_source(tmp_path, monkeypatch):
    database = tmp_path / "rag.sqlite3"
    monkeypatch.setenv("YAGAPON_RAG_DB_PATH", str(database))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", raising=False)
    RagStore(database).upsert_document(
        "documents/private",
        "チャンネル限定の会議記録" * 100,
        [
            {"key": "document_key", "string_value": "private"},
            {"key": "source_type", "string_value": "discord_conversation"},
            {"key": "authority", "string_value": "conversation"},
            {"key": "status", "string_value": "raw"},
            {"key": "guild_id", "numeric_value": 123},
            {"key": "channel_id", "numeric_value": 456},
            {"key": "access_scope", "string_value": "channel"},
        ],
    )

    result = await curate_once(state_path=tmp_path / "state.sqlite3", limit=3)

    assert result["source"] == 1
    assert result["eligible"] == 0
    assert result["policy_excluded"] == 1
    assert result["actionable"] == 0
    assert result["processed"] == 0
    assert result["open_bundles"] == 0
    assert result["max_open_bundles"] == 5


def test_autonomous_curation_rejects_mixed_invalid_channel_ids(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", "456,invalid")
    document = {
        "guild_id": 123,
        "channel_id": 456,
        "access_scope": "channel",
    }

    with pytest.raises(KnowledgeCurationConfigurationError):
        curation_document_allowed(document)


def test_explicit_channel_subset_does_not_expand_from_auto_promote(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", "456")
    document = {
        "guild_id": 123,
        "channel_id": 457,
        "access_scope": "channel",
        "auto_promote": True,
    }

    assert curation_document_allowed(document) is False
    assert curation_document_allowed({**document, "channel_id": 456}) is True
