import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from bot.rag_store import RagStore
from knowledge_curation import __main__ as curation
from knowledge_curation.__main__ import (
    CurationPlan,
    EvidenceClaim,
    TopicDraft,
    backfill_checkpoint_status,
    build_topic_bundle,
    collect_topic_evidence,
    expand_curation_documents,
    render_topic_brief,
    validate_topic_draft,
)
from knowledge_curation.topic import (
    BUNDLE_LINEAGE_SCHEMA_VERSION,
    AccessDecision,
    EvidenceItem,
    TopicStateStore,
    deterministic_topic_identity,
    evidence_fingerprint,
    intersect_access_scopes,
)


def evidence(
    evidence_id: str,
    key: str,
    *,
    source_type: str = "discord_conversation",
    access_scope: str = "channel",
    channel_id: int | None = 10,
    content_sha256: str = "a" * 64,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        document_key=key,
        source_type=source_type,
        authority="conversation",
        status="raw",
        access_scope=access_scope,
        channel_id=channel_id,
        source_url=f"https://example.test/{key}",
        excerpt="新歓Webページの根拠",
        content_sha256=content_sha256,
        start_at="2026-10-01T00:00:00+09:00",
        end_at="2026-10-01T01:00:00+09:00",
    )


def test_curation_digest_changes_when_discord_lineage_changes():
    document = {
        "body": "同じ本文",
        "guild_id": 1,
        "channel_id": 10,
        "access_scope": "channel",
        "parent_id": 20,
        "parent_name": "親",
        "category_id": 30,
        "category_name": "会議",
        "category": "会議",
    }

    original = curation._digest(document)
    assert curation._digest(document | {"parent_id": 21}) != original
    assert curation._digest(document | {"category_name": "選考"}) != original
    assert curation._digest(document | {"auto_promote": True}) != original


def test_watch_delay_retries_failed_work_and_durable_delivery_schedule():
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    assert curation.next_watch_delay(
        21_600,
        900,
        {"enabled": True, "checkpoint_ready": True},
    ) == 21_600
    assert curation.next_watch_delay(
        21_600,
        900,
        {"enabled": True, "checkpoint_ready": False},
    ) == 900
    assert curation.next_watch_delay(
        21_600,
        900,
        None,
        cycle_failed=True,
    ) == 900
    assert curation.next_watch_delay(
        21_600,
        900,
        {"enabled": True, "checkpoint_ready": True, "failed": 1},
    ) == 900
    assert curation.next_watch_delay(
        21_600,
        900,
        {
            "enabled": True,
            "checkpoint_ready": True,
            "retryable": 1,
            "next_retry_at": (now + timedelta(seconds=120)).isoformat(),
        },
        now=now,
    ) == 120
    assert curation.next_watch_delay(
        21_600,
        900,
        {
            "enabled": True,
            "checkpoint_ready": True,
            "retryable": 1,
            "next_retry_at": (now + timedelta(microseconds=700_000)).isoformat(),
        },
        now=now,
    ) == 1
    assert curation.next_watch_delay(
        21_600,
        900,
        {
            "enabled": True,
            "checkpoint_ready": True,
            "retryable": 1,
            "next_retry_at": (now + timedelta(seconds=1_200)).isoformat(),
        },
        now=now,
    ) == 900


def topic_draft() -> TopicDraft:
    return TopicDraft(
        title="新歓Webページ",
        summary=[EvidenceClaim(text="新歓向けの案内ページを運用する。", evidence_ids=["D01", "C01"])],
        situation=[EvidenceClaim(text="Discordで公開内容を検討した。", evidence_ids=["D01"])],
        current_state=[EvidenceClaim(text="実装ファイルが存在する。", evidence_ids=["C01"])],
        decisions=[EvidenceClaim(text="公開前にレビューする方針である。", evidence_ids=["K01"])],
        unknowns=[EvidenceClaim(text="本番反映日は未確認である。", evidence_ids=["D01"])],
        systems=["Web"],
        tags=["新歓"],
    )


def source_document() -> dict:
    return {
        "document_key": "discord-primary",
        "guild_id": 123,
        "channel_id": 10,
        "channel_name": "開発",
        "category": "28th IT局",
        "festival": 28,
        "source_type": "discord_conversation",
        "authority": "conversation",
        "status": "raw",
        "access_scope": "channel",
        "source_url": "https://discord.com/channels/123/10/100",
        "start_at": "2026-10-01T00:00:00+09:00",
        "end_at": "2026-10-01T01:00:00+09:00",
        "body": "新歓Webページを公開前にレビューする。" * 20,
    }


def upsert_source_document(store: RagStore, document: dict) -> dict:
    metadata = []
    for key, value in document.items():
        if key == "body" or value is None:
            continue
        field = "numeric_value" if isinstance(value, int) else "string_value"
        metadata.append({"key": key, field: value})
    store.upsert_document(
        f"documents/{document['document_key']}",
        document["body"],
        metadata,
    )
    stored = store.get_document(str(document["document_key"]))
    assert stored is not None
    return stored


def test_long_curation_source_is_split_deterministically_without_dropping_tail():
    document = source_document()
    document["source_type"] = "meeting_transcript"
    document["body"] = "A" * 24_000 + "B" * 24_000 + "末尾の重要な決定"

    first = expand_curation_documents([document])
    second = expand_curation_documents([document])

    assert first == second
    assert len(first) > 3
    assert first[0]["source_document_key"] == document["document_key"]
    assert first[0]["source_segment_start"] == 0
    assert first[-1]["source_segment_end"] == len(document["body"])
    assert "末尾の重要な決定" in first[-1]["body"]
    assert [item["source_segment_index"] for item in first] == list(range(1, len(first) + 1))
    # Every original character belongs to the non-overlap core of one segment.
    cores = [
        document["body"][item["source_segment_start"] : item["source_segment_end"]]
        for item in first
    ]
    assert "".join(cores) == document["body"]


def test_long_curation_uses_small_units_so_distant_topics_do_not_share_one_plan():
    document = source_document()
    document["body"] = "新歓Webの決定" + ("A" * 12_000) + "整理券の決定" + ("B" * 2_000)

    segments = expand_curation_documents([document])
    welcome = [item for item in segments if "新歓Webの決定" in item["body"]]
    standby = [item for item in segments if "整理券の決定" in item["body"]]

    assert welcome and standby
    assert {item["document_key"] for item in welcome}.isdisjoint(
        {item["document_key"] for item in standby}
    )
    assert max(len(item["body"]) for item in segments) <= (
        curation.CURATION_SEGMENT_CHARS + 2 * curation.CURATION_SEGMENT_OVERLAP_CHARS
    )


def test_virtual_segment_lineage_reloads_and_hashes_current_source(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", "10")
    rag = RagStore(tmp_path / "rag.sqlite3")
    source = source_document() | {
        "body": "先頭の議題" + ("A" * (curation.CURATION_SEGMENT_CHARS * 2)),
    }
    stored = upsert_source_document(rag, source)
    segment = expand_curation_documents([stored])[0]
    evidence_items = collect_topic_evidence(rag, segment, [])

    versions = curation._bundle_source_versions(
        rag,
        evidence_items,
        {segment["document_key"]: segment},
        current_curation_eligible_channel_ids={10},
        current_public_channel_ids=set(),
    )

    assert versions == [(source["document_key"], curation._digest(stored))]
    upsert_source_document(
        rag,
        source | {"body": "改変された議題" + source["body"][6:]},
    )
    assert curation._bundle_source_versions(
        rag,
        evidence_items,
        {segment["document_key"]: segment},
        current_curation_eligible_channel_ids={10},
        current_public_channel_ids=set(),
    ) is None


def test_topic_identity_is_normalized_deterministic_and_filesystem_safe():
    left = deterministic_topic_identity(28, "IT", " 新歓 Webページ ")
    right = deterministic_topic_identity(28, "it", "新歓　Webページ")

    assert left == right
    assert left.topic_id.startswith("28th-it-")
    assert left.slug.endswith(left.topic_id[-10:])
    assert all(character.isascii() for character in left.topic_id)


def test_evidence_fingerprint_is_order_independent_and_content_sensitive():
    first = evidence("D01", "discord")
    second = evidence("C01", "code", source_type="source_code", access_scope="guild", channel_id=None)

    assert evidence_fingerprint([first, second]) == evidence_fingerprint([second, first])
    changed = evidence(
        "C01",
        "code",
        source_type="source_code",
        access_scope="guild",
        channel_id=None,
        content_sha256="b" * 64,
    )
    assert evidence_fingerprint([first, second]) != evidence_fingerprint([first, changed])


@pytest.mark.parametrize(
    ("items", "scope", "channel_id", "reason"),
    [
        ([evidence("K01", "knowledge", access_scope="guild", channel_id=None)], "guild", None, "guild-evidence-only"),
        (
            [
                evidence("D01", "discord"),
                evidence("C01", "code", source_type="source_code", access_scope="guild", channel_id=None),
            ],
            "channel",
            10,
            "single-private-channel",
        ),
        (
            [evidence("D01", "one", channel_id=10), evidence("D02", "two", channel_id=11)],
            "restricted",
            None,
            "multiple-private-channels",
        ),
        ([evidence("D01", "legacy", access_scope="", channel_id=None)], "restricted", None, "unsupported-scope:legacy"),
    ],
)
def test_access_scope_intersection_is_fail_closed(items, scope, channel_id, reason):
    result = intersect_access_scopes(items)

    assert (result.scope, result.channel_id, result.reason) == (scope, channel_id, reason)


def test_topic_state_tracks_aliases_dirty_updates_and_open_bundle_backpressure(tmp_path):
    store = TopicStateStore(tmp_path / "topics.sqlite3")
    identity = store.resolve_identity(festival=28, bureau="it", topic="新歓Webページ", aliases=["新歓サイト"])
    first = store.observe_topic(
        identity,
        title="新歓Webページ",
        aliases=["新歓サイト"],
        fingerprint="one",
        access=AccessDecision("channel", 10, "single-private-channel"),
    )

    assert first.dirty is True
    queued = store.record_delivery(
        identity.topic_id,
        fingerprint="one",
        bundle_id="bundle-one",
        status="queued",
    )
    assert queued.dirty is False
    assert queued.has_open_bundle is True
    assert store.count_open_bundles() == 1
    assert [item.topic_id for item in store.open_topics()] == [identity.topic_id]

    updated = store.observe_topic(
        identity,
        title="新歓Webページ",
        aliases=["歓迎ページ"],
        fingerprint="two",
        access=AccessDecision("channel", 10, "single-private-channel"),
    )
    assert updated.dirty is True
    assert updated.has_open_bundle is True
    assert "歓迎ページ" in updated.aliases

    # A merged/reviewed article closes the old Draft without discarding the
    # evidence change that arrived while it was open.
    store.mark_accepted(identity.topic_id)
    accepted = store.get_topic(identity.topic_id)
    assert accepted is not None
    assert accepted.has_open_bundle is False
    assert accepted.dirty is True

    resolved = store.resolve_identity(festival=28, bureau="it", topic="歓迎ページ")
    assert resolved.topic_id == identity.topic_id


def test_pending_delivery_remains_dirty_and_retries_with_backoff(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_RETRY_BASE_SECONDS", "60")
    store = TopicStateStore(tmp_path / "topics.sqlite3")
    identity = store.resolve_identity(festival=28, bureau="it", topic="新歓Web")
    store.observe_topic(
        identity,
        title="新歓Web",
        aliases=[],
        fingerprint="one",
        access=AccessDecision("guild", None, "guild-evidence-only"),
    )
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    pending = store.record_delivery(
        identity.topic_id,
        fingerprint="one",
        bundle_id="bundle-one",
        status="pending",
        now=now,
    )

    assert pending.dirty is True
    assert pending.attempts == 1
    assert store.due_retries(now=now) == []
    retryable, next_retry_at = store.retryable_schedule()
    assert retryable == 1
    assert next_retry_at == pending.next_attempt_at
    due = store.due_retries(now=now + timedelta(hours=1))
    assert [item.topic_id for item in due] == [identity.topic_id]


def test_bundle_completion_marks_only_captured_unchanged_source_versions(tmp_path):
    store = TopicStateStore(tmp_path / "topics.sqlite3")
    identity = store.resolve_identity(festival=28, bureau="it", topic="新歓Web")
    store.observe_topic(
        identity,
        title="新歓Web",
        aliases=[],
        fingerprint="evidence-one",
        access=AccessDecision("guild", None, "guild-evidence-only"),
    )
    store.record_source("source-one", "hash-one", status="waiting-topic", topic_id=identity.topic_id)
    store.record_delivery(
        identity.topic_id,
        fingerprint="evidence-one",
        bundle_id="bundle-old",
        status="failed",
        source_versions=[("source-one", "hash-one")],
    )
    # Both a new source and a new version of the captured source arrive while
    # the old bundle is waiting for retry.
    store.record_source("source-two", "hash-two", status="waiting-topic", topic_id=identity.topic_id)
    store.record_source("source-one", "hash-new", status="waiting-topic", topic_id=identity.topic_id)

    store.record_delivery(
        identity.topic_id,
        fingerprint="evidence-one",
        bundle_id="bundle-old",
        status="queued",
    )
    assert store.mark_bundle_sources("bundle-old", "delivered") == 0

    assert store.bundle_source_versions("bundle-old") == [("source-one", "hash-one")]
    assert store.source_observation("source-one")["status"] == "waiting-topic"
    assert store.source_observation("source-two")["status"] == "waiting-topic"
    assert store.source_actionable("source-one", "hash-new") is True
    assert store.source_actionable("source-two", "hash-two") is True


def test_bundle_completion_marks_captured_source_when_digest_is_unchanged(tmp_path):
    store = TopicStateStore(tmp_path / "topics.sqlite3")
    identity = store.resolve_identity(festival=28, bureau="it", topic="新歓Web")
    store.observe_topic(
        identity,
        title="新歓Web",
        aliases=[],
        fingerprint="evidence-one",
        access=AccessDecision("guild", None, "guild-evidence-only"),
    )
    store.record_source("source-one", "hash-one", status="pending-delivery", topic_id=identity.topic_id)
    store.record_delivery(
        identity.topic_id,
        fingerprint="evidence-one",
        bundle_id="bundle-one",
        status="pending",
        source_versions=[("source-one", "hash-one")],
    )

    assert store.mark_bundle_sources("bundle-one", "delivered") == 1
    assert store.source_observation("source-one")["status"] == "delivered"
    assert store.source_actionable("source-one", "hash-one") is False


def test_retry_bundle_authorization_requires_present_unchanged_current_source(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", raising=False)
    store = TopicStateStore(tmp_path / "topics.sqlite3")
    rag = RagStore(tmp_path / "rag.sqlite3")
    identity = store.resolve_identity(festival=28, bureau="it", topic="再送検証")
    store.observe_topic(
        identity,
        title="再送検証",
        aliases=[],
        fingerprint="one",
        access=AccessDecision("channel", 10, "single-private-channel"),
    )
    document = source_document() | {"auto_promote": True}
    stored = upsert_source_document(rag, document)
    digest = curation._digest(stored)
    store.record_delivery(
        identity.topic_id,
        fingerprint="one",
        bundle_id="bundle-one",
        status="failed",
        source_versions=[(document["document_key"], digest)],
        lineage_schema_version=BUNDLE_LINEAGE_SCHEMA_VERSION,
        lineage_complete=True,
    )
    assert store.bundle_lineage_status("bundle-one") == (
        True,
        "lineage-complete",
    )

    assert curation._retry_bundle_sources_authorized(
        store,
        "bundle-one",
        rag,
        {},
        current_curation_eligible_channel_ids={10},
        current_public_channel_ids={10},
    ) is True
    # A stale run-start cache must not override the current RagStore row.
    assert curation._retry_bundle_sources_authorized(
        store,
        "bundle-one",
        rag,
        {document["document_key"]: document | {"body": "stale-cache"}},
        current_curation_eligible_channel_ids={10},
        current_public_channel_ids={10},
    ) is True
    changed = upsert_source_document(rag, document | {"body": "changed" * 100})
    assert curation._digest(changed) != digest
    assert curation._retry_bundle_sources_authorized(
        store,
        "bundle-one",
        rag,
        {document["document_key"]: stored},
        current_curation_eligible_channel_ids={10},
        current_public_channel_ids={10},
    ) is False


def test_bundle_retry_revalidates_every_related_evidence_source(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", "10,20")
    rag = RagStore(tmp_path / "rag.sqlite3")
    related = source_document() | {
        "document_key": "discord-related",
        "channel_id": 20,
        "channel_name": "関連ログ",
        "body": "関連する役職限定ログ" * 30,
    }
    upsert_source_document(rag, related)
    primary = source_document()
    primary_stored = upsert_source_document(rag, primary)
    documents = {primary["document_key"]: primary_stored}
    evidence_items = [
        evidence(
            "D01",
            primary["document_key"],
            channel_id=10,
            content_sha256=hashlib.sha256(primary["body"].encode()).hexdigest(),
        ),
        evidence(
            "D02",
            related["document_key"],
            channel_id=20,
            content_sha256=hashlib.sha256(related["body"].encode()).hexdigest(),
        ),
    ]
    versions = curation._bundle_source_versions(
        rag,
        evidence_items,
        documents,
        current_curation_eligible_channel_ids={10, 20},
        current_public_channel_ids=set(),
    )

    assert versions is not None
    assert {key for key, _digest in versions} == {
        "discord-primary",
        "discord-related",
    }

    ledger = TopicStateStore(tmp_path / "topics.sqlite3")
    identity = ledger.resolve_identity(festival=28, bureau="it", topic="関連再送検証")
    ledger.observe_topic(
        identity,
        title="関連再送検証",
        aliases=[],
        fingerprint="bundle",
        access=AccessDecision("channel", 10, "single-private-channel"),
    )
    ledger.record_delivery(
        identity.topic_id,
        fingerprint="bundle",
        bundle_id="bundle-related",
        status="failed",
        source_versions=versions,
        lineage_schema_version=BUNDLE_LINEAGE_SCHEMA_VERSION,
        lineage_complete=True,
    )

    assert curation._retry_bundle_sources_authorized(
        ledger,
        "bundle-related",
        rag,
        documents,
        current_curation_eligible_channel_ids={10, 20},
        current_public_channel_ids=set(),
    ) is True
    assert curation._retry_bundle_sources_authorized(
        ledger,
        "bundle-related",
        rag,
        documents,
        current_curation_eligible_channel_ids={10},
        current_public_channel_ids=set(),
    ) is False


def test_legacy_json_state_is_backed_up_and_imported(tmp_path):
    path = tmp_path / "knowledge-curation-state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": {"old-document": {"sha256": "abc", "status": "queued"}},
            }
        ),
        encoding="utf-8",
    )

    store = TopicStateStore(path)

    assert store.source_actionable("old-document", "abc") is False
    assert path.with_suffix(".json.legacy.json").is_file()


def test_legacy_json_migration_keeps_original_when_import_crashes(tmp_path, monkeypatch):
    path = tmp_path / "knowledge-curation-state.json"
    original = json.dumps(
        {"schema_version": 1, "documents": {"old": {"sha256": "abc"}}}
    )
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        TopicStateStore,
        "_import_legacy",
        lambda _self, _payload: (_ for _ in ()).throw(RuntimeError("crash")),
    )

    with pytest.raises(RuntimeError, match="crash"):
        TopicStateStore(path)

    assert path.read_text(encoding="utf-8") == original
    assert json.loads(path.read_text(encoding="utf-8"))["documents"]["old"]["sha256"] == "abc"


def test_collect_topic_evidence_keeps_discord_code_and_knowledge_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", "10")
    store = RagStore(tmp_path / "rag.sqlite3")
    common = [
        {"key": "festival", "numeric_value": 28},
        {"key": "status", "string_value": "active"},
    ]
    records = (
        (
            "discord-related",
            "discord_conversation",
            123,
            "conversation",
            "raw",
            "channel",
            10,
        ),
        ("code-related", "source_code", 0, "deployed_implementation", "active", "guild", None),
        ("code-history", "source_code_history", 0, "repository_history", "historical", "guild", None),
        ("knowledge-related", "knowledge_repository", 0, "curated", "active", "guild", None),
    )
    for key, source_type, guild_id, authority, status, scope, channel_id in records:
        metadata = [
            *common,
            {"key": "document_key", "string_value": key},
            {"key": "source_type", "string_value": source_type},
            {"key": "guild_id", "numeric_value": guild_id},
            {"key": "authority", "string_value": authority},
            {"key": "status", "string_value": status},
            {"key": "access_scope", "string_value": scope},
            {"key": "source_url", "string_value": f"https://example.test/{key}"},
        ]
        if channel_id:
            metadata.append({"key": "channel_id", "numeric_value": channel_id})
        store.upsert_document(f"documents/{key}", "新歓Webページの実装と運用 " * 40, metadata)

    result = collect_topic_evidence(store, source_document(), ["新歓Webページ"])

    assert {item.evidence_id[:1] for item in result} == {"D", "C", "K"}
    assert {item.source_type for item in result} >= {
        "discord_conversation",
        "source_code",
        "source_code_history",
        "knowledge_repository",
    }
    assert intersect_access_scopes(result) == AccessDecision("channel", 10, "single-private-channel")


def test_curation_deny_policy_wins_over_dynamic_and_explicit_admission(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", raising=False)
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS", "10")
    denied_by_id = source_document() | {"auto_promote": True}

    assert curation.curation_document_allowed(denied_by_id) is False

    denied_by_term = source_document() | {
        "channel_id": 20,
        "access_scope": "guild",
        "category": "28th 選考会議",
    }
    assert curation.curation_document_allowed(denied_by_term) is False


def test_collect_topic_evidence_excludes_related_protected_source(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", "10")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", "選考")
    store = RagStore(tmp_path / "rag.sqlite3")
    for key, category in (("safe", "28th IT局"), ("protected", "選考委員会")):
        store.upsert_document(
            f"documents/{key}",
            "新歓Webページの証拠 " * 30,
            [
                {"key": "document_key", "string_value": key},
                {"key": "source_type", "string_value": "discord_conversation"},
                {"key": "guild_id", "numeric_value": 123},
                {"key": "channel_id", "numeric_value": 10},
                {"key": "channel_name", "string_value": key},
                {"key": "category", "string_value": category},
                {"key": "festival", "numeric_value": 28},
                {"key": "authority", "string_value": "conversation"},
                {"key": "status", "string_value": "raw"},
                {"key": "access_scope", "string_value": "channel"},
            ],
        )

    result = collect_topic_evidence(store, source_document(), ["新歓Webページ"])

    assert "safe" in {item.document_key for item in result}
    assert "protected" not in {item.document_key for item in result}


def test_rag_store_finds_only_reviewed_approved_topic(tmp_path):
    store = RagStore(tmp_path / "rag.sqlite3")
    topic_id = "28th-it-welcome-1234567890"
    for key, status, authority in (
        ("draft", "draft", "curated"),
        ("approved", "active", "reviewed_record"),
    ):
        store.upsert_document(
            f"documents/{key}",
            f"---\ntopic_id: {topic_id}\n---\n本文",
            [
                {"key": "document_key", "string_value": key},
                {"key": "source_type", "string_value": "knowledge_repository"},
                {"key": "guild_id", "numeric_value": 0},
                {"key": "festival", "numeric_value": 28},
                {"key": "authority", "string_value": authority},
                {"key": "status", "string_value": status},
                {"key": "access_scope", "string_value": "guild"},
            ],
        )

    matched = store.find_approved_topic(topic_id, festival=28)

    assert matched is not None
    assert matched["document_key"] == "approved"
    assert store.find_approved_topic(topic_id, festival=27) is None


def test_topic_brief_has_detailed_sections_evidence_ids_and_draft_only_bundle():
    identity = deterministic_topic_identity(28, "it", "新歓Webページ")
    items = [
        evidence("D01", "discord"),
        evidence("C01", "code", source_type="source_code", access_scope="guild", channel_id=None),
        evidence(
            "K01",
            "knowledge",
            source_type="knowledge_repository",
            access_scope="guild",
            channel_id=None,
        ),
    ]
    fingerprint = evidence_fingerprint(items)
    draft = topic_draft()
    validate_topic_draft(draft, items)

    rendered = render_topic_brief(
        document=source_document(),
        identity=identity,
        candidate=draft,
        evidence=items,
        fingerprint=fingerprint,
        access=AccessDecision("channel", 10, "single-private-channel"),
        aliases=["新歓サイト"],
    )
    metadata = yaml.safe_load(rendered[4:].split("\n---\n", 1)[0])

    assert metadata["status"] == "draft"
    assert metadata["topic_id"] == identity.topic_id
    assert metadata["evidence_fingerprint"] == fingerprint
    assert "## コードマップ・実装箇所" in rendered
    assert "## 問題・原因・対応・結果" in rendered
    assert "[D01] [C01]" in rendered
    assert "| D01 |" in rendered

    bundle = build_topic_bundle(
        document=source_document(),
        identity=identity,
        candidate=draft,
        evidence=items,
        fingerprint=fingerprint,
        access=AccessDecision("channel", 10, "single-private-channel"),
        aliases=["新歓サイト"],
    )
    assert bundle[0].endswith(fingerprint[:12])
    assert "/decisions/candidates/" in bundle[1]
    assert bundle[3]["source"] == "yagapon-continuous-curation"
    assert bundle[3]["humanReviewed"] is False
    assert "Draft" in bundle[6]


def test_topic_draft_rejects_unknown_evidence_id():
    draft = TopicDraft(
        title="誤った草案",
        summary=[EvidenceClaim(text="根拠が存在しない。", evidence_ids=["X99"])],
    )

    with pytest.raises(ValueError, match="unknown evidence IDs"):
        validate_topic_draft(draft, [evidence("D01", "discord")])


def test_restricted_topic_cannot_be_rendered():
    identity = deterministic_topic_identity(28, "it", "新歓Webページ")

    with pytest.raises(ValueError, match="Restricted evidence"):
        render_topic_brief(
            document=source_document(),
            identity=identity,
            candidate=topic_draft(),
            evidence=[evidence("D01", "discord")],
            fingerprint="abc",
            access=AccessDecision("restricted", None, "multiple-private-channels"),
            aliases=[],
        )


def checkpoint_payload(now: datetime) -> dict:
    return {
        "schema_version": 2,
        "started_at": (now - timedelta(minutes=5)).isoformat(),
        "completed_at": now.isoformat(),
        "status": "completed",
        "configured_guild_ids": ["123"],
        "configured_channel_ids": ["10", "20", "30", "40"],
        "discovered_channel_ids": ["10", "20", "30", "40"],
        "eligible_channel_ids": ["10", "20", "30", "40"],
        "current_curation_eligible_channel_ids": ["10", "20", "30", "40"],
        "current_public_curation_channel_ids": ["10", "20", "30", "40"],
        "missing_channel_ids": [],
        "guilds": {
            "123": {
                "status": "completed",
                "reason": "",
                "channels_total": 4,
                "channels_completed": 4,
                "messages_indexed": 100,
                "documents_uploaded": 3,
                "documents_replaced": 1,
                "failures": [],
                "configured_channel_ids": ["10", "20", "30", "40"],
                "discovered_channel_ids": ["10", "20", "30", "40"],
                "eligible_channel_ids": ["10", "20", "30", "40"],
                "current_curation_eligible_channel_ids": [
                    "10",
                    "20",
                    "30",
                    "40",
                ],
                "current_public_curation_channel_ids": [
                    "10",
                    "20",
                    "30",
                    "40",
                ],
                "container_channel_ids": [],
                "missing_channel_ids": [],
                "completed_channel_ids": ["10", "20", "30", "40"],
            }
        },
    }


def test_backfill_checkpoint_gate_allows_unset_or_fresh_complete_state(tmp_path, monkeypatch):
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", raising=False)
    assert backfill_checkpoint_status(now=now) == (True, "not-configured")

    path = tmp_path / "scheduled-backfill-state.json"
    path.write_text(json.dumps(checkpoint_payload(now)), encoding="utf-8")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(path))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    assert backfill_checkpoint_status(now=now) == (True, "fresh-success")

    monkeypatch.delenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", raising=False)
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    assert backfill_checkpoint_status(now=now) == (
        False,
        "checkpoint-path-unconfigured",
    )


def test_backfill_checkpoint_must_match_current_scheduler_inventory(
    tmp_path,
    monkeypatch,
):
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    path = tmp_path / "scheduled-backfill-state.json"
    path.write_text(json.dumps(checkpoint_payload(now)), encoding="utf-8")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(path))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_STATE", str(path))
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "123")
    monkeypatch.setenv(
        "YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS",
        "10,20,30,40",
    )

    assert backfill_checkpoint_status(now=now) == (True, "fresh-success")

    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "false")
    assert backfill_checkpoint_status(now=now) == (
        False,
        "checkpoint-invalid-current-configuration",
    )

    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "10")
    assert backfill_checkpoint_status(now=now) == (
        False,
        "checkpoint-channel-configuration-changed",
    )

    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "not-a-channel")
    assert backfill_checkpoint_status(now=now) == (
        False,
        "checkpoint-invalid-current-configuration",
    )

    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "")
    assert backfill_checkpoint_status(now=now) == (
        False,
        "checkpoint-channel-configuration-changed",
    )

    monkeypatch.setenv(
        "YAGAPON_SCHEDULED_BACKFILL_STATE",
        str(tmp_path / "other-state.json"),
    )
    assert backfill_checkpoint_status(now=now) == (
        False,
        "checkpoint-invalid-current-configuration",
    )


def test_backfill_checkpoint_accepts_dynamic_all_readable_channel_inventory(
    tmp_path,
    monkeypatch,
):
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    payload = checkpoint_payload(now)
    payload["configured_channel_ids"] = []
    payload["guilds"]["123"]["configured_channel_ids"] = []
    path = tmp_path / "scheduled-backfill-state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(path))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_STATE", str(path))
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS", "")

    assert backfill_checkpoint_status(now=now) == (True, "fresh-success")


def test_backfill_checkpoint_accepts_forum_parent_container_plus_completed_threads(
    tmp_path,
    monkeypatch,
):
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    payload = checkpoint_payload(now)
    payload["configured_channel_ids"] = ["40"]
    payload["discovered_channel_ids"] = ["40", "41", "42"]
    payload["eligible_channel_ids"] = ["40", "41", "42"]
    payload["current_curation_eligible_channel_ids"] = ["41", "42"]
    payload["current_public_curation_channel_ids"] = ["41", "42"]
    payload["guilds"]["123"].update(
        configured_channel_ids=["40"],
        discovered_channel_ids=["40", "41", "42"],
        eligible_channel_ids=["40", "41", "42"],
        current_curation_eligible_channel_ids=["41", "42"],
        current_public_curation_channel_ids=["41", "42"],
        container_channel_ids=["40"],
        completed_channel_ids=["41", "42"],
        channels_total=2,
        channels_completed=2,
    )
    path = tmp_path / "scheduled-backfill-state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(path))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", raising=False)

    assert backfill_checkpoint_status(now=now) == (True, "fresh-success")


def test_backfill_checkpoint_accepts_a_readable_empty_forum(tmp_path, monkeypatch):
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    payload = checkpoint_payload(now)
    payload["configured_channel_ids"] = ["40"]
    payload["discovered_channel_ids"] = ["40"]
    payload["eligible_channel_ids"] = ["40"]
    payload["current_curation_eligible_channel_ids"] = []
    payload["current_public_curation_channel_ids"] = []
    payload["guilds"]["123"].update(
        configured_channel_ids=["40"],
        discovered_channel_ids=["40"],
        eligible_channel_ids=["40"],
        current_curation_eligible_channel_ids=[],
        current_public_curation_channel_ids=[],
        container_channel_ids=["40"],
        completed_channel_ids=[],
        channels_total=0,
        channels_completed=0,
    )
    path = tmp_path / "scheduled-backfill-state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(path))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", raising=False)

    assert backfill_checkpoint_status(now=now) == (True, "fresh-success")


def test_backfill_checkpoint_requires_curation_and_candidate_channels(tmp_path, monkeypatch):
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    path = tmp_path / "scheduled-backfill-state.json"
    path.write_text(json.dumps(checkpoint_payload(now)), encoding="utf-8")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(path))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", "10,99")

    assert backfill_checkpoint_status(now=now) == (
        False,
        "checkpoint-missing-curation-channel:99",
    )

    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", "10")
    assert backfill_checkpoint_status(now=now, required_channel_ids={20}) == (
        True,
        "fresh-success",
    )
    assert backfill_checkpoint_status(now=now, required_channel_ids={99}) == (
        False,
        "checkpoint-missing-curation-channel:99",
    )

    # A fixed-ID override can admit a role-limited channel, but not one that
    # became ignored/protected/unreadable before the producer's final snapshot.
    payload = checkpoint_payload(now)
    payload["current_curation_eligible_channel_ids"].remove("10")
    payload["current_public_curation_channel_ids"].remove("10")
    payload["guilds"]["123"]["current_curation_eligible_channel_ids"].remove(
        "10"
    )
    payload["guilds"]["123"]["current_public_curation_channel_ids"].remove(
        "10"
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert backfill_checkpoint_status(now=now) == (
        False,
        "checkpoint-missing-curation-channel:10",
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value.update(status="failed"), "checkpoint-cycle-failed"),
        (
            lambda value: value["guilds"]["123"].update(status="deferred"),
            "checkpoint-failed-guild:123",
        ),
        (
            lambda value: value["guilds"]["123"].update(
                failures=[{"channel_id": 10, "error_type": "HTTPException"}]
            ),
            "checkpoint-incomplete-guild:123",
        ),
        (
            lambda value: value["guilds"]["123"].update(channels_completed=3),
            "checkpoint-incomplete-guild:123",
        ),
        (lambda value: value.update(schema_version=1), "checkpoint-unsupported-schema"),
        (
            lambda value: value.pop("current_curation_eligible_channel_ids"),
            "checkpoint-missing-current-eligible-inventory",
        ),
        (
            lambda value: value.pop("current_public_curation_channel_ids"),
            "checkpoint-missing-current-public-inventory",
        ),
        (
            lambda value: value["guilds"]["123"].update(
                current_public_curation_channel_ids=["99"]
            ),
            "checkpoint-invalid-current-public-inventory:123",
        ),
        (lambda value: value.pop("started_at"), "checkpoint-missing-time-range"),
        (
            lambda value: value.update(missing_channel_ids=["99"]),
            "checkpoint-missing-configured-channels",
        ),
    ],
)
def test_backfill_checkpoint_gate_fails_closed_for_partial_or_failed_cycles(
    tmp_path, monkeypatch, mutation, reason
):
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    payload = checkpoint_payload(now)
    mutation(payload)
    path = tmp_path / "scheduled-backfill-state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(path))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")

    assert backfill_checkpoint_status(now=now) == (False, reason)


def test_backfill_checkpoint_gate_rejects_stale_state(tmp_path, monkeypatch):
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    path = tmp_path / "scheduled-backfill-state.json"
    path.write_text(json.dumps(checkpoint_payload(now - timedelta(hours=2))), encoding="utf-8")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(path))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_MAX_AGE_SECONDS", "3600")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")

    assert backfill_checkpoint_status(now=now) == (False, "checkpoint-stale")


@pytest.mark.asyncio
async def test_curate_once_stops_before_rag_when_checkpoint_is_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(tmp_path / "missing.json"))

    result = await curation.curate_once(state_path=tmp_path / "curation.sqlite3", limit=3)

    assert result["checkpoint_ready"] is False
    assert result["checkpoint_reason"] == "checkpoint-unreadable"
    assert not (tmp_path / "curation.sqlite3").exists()


@pytest.mark.parametrize(
    "configured_channels",
    ["invalid", "0", "10,invalid"],
)
@pytest.mark.asyncio
async def test_curate_once_rejects_invalid_explicit_channel_configuration(
    tmp_path,
    monkeypatch,
    configured_channels,
):
    state = tmp_path / "curation.sqlite3"
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_ENABLED", "true")
    monkeypatch.setenv(
        "YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS",
        configured_channels,
    )

    result = await curation.curate_once(state_path=state, limit=3)

    assert result["checkpoint_ready"] is False
    assert result["checkpoint_reason"] == (
        "curation-channel-configuration-invalid"
    )
    assert result["processed"] == 0
    assert not state.exists()


@pytest.mark.asyncio
async def test_curate_once_revokes_stale_auto_promotion_when_channel_is_no_longer_public(
    tmp_path,
    monkeypatch,
):
    now = datetime.now(timezone.utc)
    payload = checkpoint_payload(now)
    payload["current_public_curation_channel_ids"] = ["20", "30", "40"]
    payload["guilds"]["123"]["current_public_curation_channel_ids"] = [
        "20",
        "30",
        "40",
    ]
    checkpoint = tmp_path / "scheduled-backfill-state.json"
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    database = tmp_path / "rag.sqlite3"
    monkeypatch.setenv("YAGAPON_RAG_DB_PATH", str(database))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(checkpoint))
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", raising=False)
    RagStore(database).upsert_document(
        "documents/formerly-public",
        "公開後に役職限定へ変更された会話" * 100,
        [
            {"key": "document_key", "string_value": "formerly-public"},
            {"key": "guild_id", "numeric_value": 123},
            {"key": "channel_id", "numeric_value": 10},
            {"key": "channel_name", "string_value": "旧公開チャンネル"},
            {"key": "category", "string_value": "28th IT局"},
            {"key": "festival", "numeric_value": 28},
            {"key": "source_type", "string_value": "discord_conversation"},
            {"key": "authority", "string_value": "conversation"},
            {"key": "status", "string_value": "raw"},
            {"key": "access_scope", "string_value": "channel"},
            {"key": "auto_promote", "string_value": "true"},
        ],
    )

    result = await curation.curate_once(
        state_path=tmp_path / "curation.sqlite3",
        limit=3,
    )

    assert result["checkpoint_ready"] is True
    assert result["checkpoint_reason"] == "fresh-success"
    assert result["source"] == 1
    assert result["eligible"] == 0
    assert result["policy_excluded"] == 1
    assert result["processed"] == 0


@pytest.mark.asyncio
async def test_retry_is_quarantined_when_bundle_source_loses_public_access(
    tmp_path,
    monkeypatch,
):
    now = datetime.now(timezone.utc)
    payload = checkpoint_payload(now)
    payload["current_public_curation_channel_ids"] = ["20", "30", "40"]
    payload["guilds"]["123"]["current_public_curation_channel_ids"] = [
        "20",
        "30",
        "40",
    ]
    checkpoint = tmp_path / "scheduled-backfill-state.json"
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    database = tmp_path / "rag.sqlite3"
    state = tmp_path / "curation.sqlite3"
    monkeypatch.setenv("YAGAPON_RAG_DB_PATH", str(database))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(checkpoint))
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", raising=False)
    rag = RagStore(database)
    rag.upsert_document(
        "documents/retry-source",
        "再送待ちの公開会話が後から役職限定になった" * 100,
        [
            {"key": "document_key", "string_value": "retry-source"},
            {"key": "guild_id", "numeric_value": 123},
            {"key": "channel_id", "numeric_value": 10},
            {"key": "channel_name", "string_value": "旧公開チャンネル"},
            {"key": "category", "string_value": "28th IT局"},
            {"key": "festival", "numeric_value": 28},
            {"key": "source_type", "string_value": "discord_conversation"},
            {"key": "authority", "string_value": "conversation"},
            {"key": "status", "string_value": "raw"},
            {"key": "access_scope", "string_value": "channel"},
            {"key": "auto_promote", "string_value": "true"},
        ],
    )
    stored = rag.list_curation_documents()[0]
    digest = curation._digest(stored)
    ledger = TopicStateStore(state)
    identity = ledger.resolve_identity(
        festival=28,
        bureau="it",
        topic="再送ACL検証",
    )
    ledger.observe_topic(
        identity,
        title="再送ACL検証",
        aliases=[],
        fingerprint="bundle-fingerprint",
        access=AccessDecision("channel", 10, "single-private-channel"),
    )
    ledger.record_source(
        "retry-source",
        digest,
        status="pending-delivery",
        topic_id=identity.topic_id,
    )
    ledger.record_delivery(
        identity.topic_id,
        fingerprint="bundle-fingerprint",
        bundle_id="retry-bundle",
        status="failed",
        now=now - timedelta(hours=1),
        source_versions=[("retry-source", digest)],
        lineage_schema_version=BUNDLE_LINEAGE_SCHEMA_VERSION,
        lineage_complete=True,
    )
    retry = AsyncMock()
    monkeypatch.setattr(curation, "retry_knowledge_delivery", retry)

    result = await curation.curate_once(state_path=state, limit=3)

    retry.assert_not_awaited()
    assert result["retried"] == 0
    assert result["retry_blocked"] == 1
    assert result["open_bundles"] == 0
    quarantined = TopicStateStore(state).get_topic(identity.topic_id)
    assert quarantined is not None
    assert quarantined.delivery_status == "blocked-access"


@pytest.mark.asyncio
async def test_pre_upgrade_retry_without_lineage_metadata_is_blocked(
    tmp_path,
    monkeypatch,
):
    now = datetime.now(timezone.utc)
    checkpoint = tmp_path / "scheduled-backfill-state.json"
    checkpoint.write_text(
        json.dumps(checkpoint_payload(now)),
        encoding="utf-8",
    )
    database = tmp_path / "rag.sqlite3"
    state = tmp_path / "curation.sqlite3"
    monkeypatch.setenv("YAGAPON_RAG_DB_PATH", str(database))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(checkpoint))
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", raising=False)
    rag = RagStore(database)
    rag.upsert_document(
        "documents/legacy-retry-source",
        "旧bundleの主資料だけが記録されている公開会話" * 100,
        [
            {"key": "document_key", "string_value": "legacy-retry-source"},
            {"key": "guild_id", "numeric_value": 123},
            {"key": "channel_id", "numeric_value": 10},
            {"key": "channel_name", "string_value": "公開チャンネル"},
            {"key": "category", "string_value": "28th IT局"},
            {"key": "festival", "numeric_value": 28},
            {"key": "source_type", "string_value": "discord_conversation"},
            {"key": "authority", "string_value": "conversation"},
            {"key": "status", "string_value": "raw"},
            {"key": "access_scope", "string_value": "channel"},
            {"key": "auto_promote", "string_value": "true"},
        ],
    )
    stored = rag.list_curation_documents()[0]
    digest = curation._digest(stored)
    ledger = TopicStateStore(state)
    identity = ledger.resolve_identity(
        festival=28,
        bureau="it",
        topic="旧再送lineage検証",
    )
    ledger.observe_topic(
        identity,
        title="旧再送lineage検証",
        aliases=[],
        fingerprint="legacy-bundle-fingerprint",
        access=AccessDecision("channel", 10, "single-private-channel"),
    )
    ledger.record_source(
        "legacy-retry-source",
        digest,
        status="delivered",
        topic_id=identity.topic_id,
    )
    ledger.record_delivery(
        identity.topic_id,
        fingerprint="legacy-bundle-fingerprint",
        bundle_id="legacy-retry-bundle",
        status="failed",
        now=now - timedelta(hours=1),
        # This is the old primary-only shape. Simulate upgrade by removing the
        # metadata table while retaining its bundle_sources row.
        source_versions=[("legacy-retry-source", digest)],
    )
    with ledger._connect() as connection:
        connection.execute("DROP TABLE bundle_lineage")
    upgraded = TopicStateStore(state)
    assert upgraded.bundle_source_versions("legacy-retry-bundle") == [
        ("legacy-retry-source", digest)
    ]
    assert upgraded.bundle_lineage_status("legacy-retry-bundle") == (
        False,
        "lineage-metadata-missing",
    )
    authorized, reason = curation._retry_bundle_sources_authorization(
        upgraded,
        "legacy-retry-bundle",
        rag,
        {"legacy-retry-source": stored},
        current_curation_eligible_channel_ids={10},
        current_public_channel_ids={10},
    )
    assert authorized is False
    assert reason == "lineage-metadata-missing"
    retry = AsyncMock()
    monkeypatch.setattr(curation, "retry_knowledge_delivery", retry)

    result = await curation.curate_once(state_path=state, limit=3)

    retry.assert_not_awaited()
    assert result["retried"] == 0
    assert result["retry_blocked"] == 1
    assert TopicStateStore(state).get_topic(identity.topic_id).delivery_status == (
        "blocked-access"
    )


@pytest.mark.asyncio
async def test_retry_exception_is_reported_as_retry_failure(
    tmp_path,
    monkeypatch,
):
    now = datetime.now(timezone.utc)
    checkpoint = tmp_path / "scheduled-backfill-state.json"
    checkpoint.write_text(
        json.dumps(checkpoint_payload(now)),
        encoding="utf-8",
    )
    database = tmp_path / "rag.sqlite3"
    state = tmp_path / "curation.sqlite3"
    monkeypatch.setenv("YAGAPON_RAG_DB_PATH", str(database))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(checkpoint))
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", raising=False)
    rag = RagStore(database)
    rag.upsert_document(
        "documents/retry-source",
        "再送時の一時障害を報告する公開会話" * 100,
        [
            {"key": "document_key", "string_value": "retry-source"},
            {"key": "guild_id", "numeric_value": 123},
            {"key": "channel_id", "numeric_value": 10},
            {"key": "channel_name", "string_value": "公開チャンネル"},
            {"key": "category", "string_value": "28th IT局"},
            {"key": "festival", "numeric_value": 28},
            {"key": "source_type", "string_value": "discord_conversation"},
            {"key": "authority", "string_value": "conversation"},
            {"key": "status", "string_value": "raw"},
            {"key": "access_scope", "string_value": "channel"},
            {"key": "auto_promote", "string_value": "true"},
        ],
    )
    stored = rag.list_curation_documents()[0]
    digest = curation._digest(stored)
    ledger = TopicStateStore(state)
    identity = ledger.resolve_identity(
        festival=28,
        bureau="it",
        topic="再送失敗検証",
    )
    ledger.observe_topic(
        identity,
        title="再送失敗検証",
        aliases=[],
        fingerprint="bundle-fingerprint",
        access=AccessDecision("channel", 10, "single-private-channel"),
    )
    ledger.record_source(
        "retry-source",
        digest,
        status="delivered",
        topic_id=identity.topic_id,
    )
    ledger.record_delivery(
        identity.topic_id,
        fingerprint="bundle-fingerprint",
        bundle_id="retry-bundle",
        status="failed",
        now=now - timedelta(hours=1),
        source_versions=[("retry-source", digest)],
        lineage_schema_version=BUNDLE_LINEAGE_SCHEMA_VERSION,
        lineage_complete=True,
    )
    retry = AsyncMock(side_effect=RuntimeError("temporary"))
    monkeypatch.setattr(curation, "retry_knowledge_delivery", retry)

    result = await curation.curate_once(state_path=state, limit=3)

    retry.assert_awaited_once_with("retry-bundle")
    assert result["retried"] == 1
    assert result["retry_failed"] == 1
    assert result["failed"] == 0


@pytest.mark.asyncio
async def test_curate_once_requires_every_candidate_channel_in_successful_cycle(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    checkpoint = tmp_path / "scheduled-backfill-state.json"
    checkpoint.write_text(json.dumps(checkpoint_payload(now)), encoding="utf-8")
    database = tmp_path / "rag.sqlite3"
    state = tmp_path / "curation.sqlite3"
    monkeypatch.setenv("YAGAPON_RAG_DB_PATH", str(database))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BACKFILL_STATE", str(checkpoint))
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", raising=False)
    document = source_document() | {"channel_id": 99, "access_scope": "guild"}
    RagStore(database).upsert_document(
        "documents/candidate-99",
        document["body"],
        [
            {"key": "document_key", "string_value": "candidate-99"},
            {"key": "guild_id", "numeric_value": 123},
            {"key": "channel_id", "numeric_value": 99},
            {"key": "channel_name", "string_value": "全体公開"},
            {"key": "category", "string_value": "28th IT局"},
            {"key": "festival", "numeric_value": 28},
            {"key": "source_type", "string_value": "discord_conversation"},
            {"key": "authority", "string_value": "conversation"},
            {"key": "status", "string_value": "raw"},
            {"key": "access_scope", "string_value": "guild"},
        ],
    )

    result = await curation.curate_once(state_path=state, limit=3)

    assert result["checkpoint_ready"] is False
    assert result["checkpoint_reason"] == "checkpoint-missing-curation-channel:99"
    assert result["processed"] == 0
    assert not state.exists()


@pytest.mark.parametrize("mutation", ["body", "acl"])
@pytest.mark.asyncio
async def test_curate_once_revalidates_live_evidence_before_initial_publish(
    tmp_path,
    monkeypatch,
    mutation,
):
    database = tmp_path / "rag.sqlite3"
    state = tmp_path / "curation.sqlite3"
    monkeypatch.setenv("YAGAPON_RAG_DB_PATH", str(database))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", "10")
    store = RagStore(database)
    document = source_document()
    upsert_source_document(store, document)
    plan = CurationPlan(
        has_knowledge_value=True,
        topic="新歓Webページ",
        aliases=["新歓サイト"],
        search_terms=["新歓Webページ"],
    )
    draft = TopicDraft(
        title="新歓Webページ",
        summary=[EvidenceClaim(text="公開前レビューを検討した。", evidence_ids=["D01"])],
    )

    async def mutate_during_generation(*_args, **_kwargs):
        if mutation == "body":
            replacement = document | {"body": "生成中に差し替えられた会話" * 30}
        else:
            replacement = document | {"category": "選考"}
        upsert_source_document(store, replacement)
        return draft

    monkeypatch.setattr(curation, "new_genai_client", lambda _key: object())
    monkeypatch.setattr(curation, "plan_document", AsyncMock(return_value=plan))
    monkeypatch.setattr(curation, "draft_topic", mutate_during_generation)
    publish_mock = AsyncMock()
    monkeypatch.setattr(curation, "publish_knowledge_bundle", publish_mock)

    result = await curation.curate_once(state_path=state, limit=3)

    publish_mock.assert_not_awaited()
    assert result["processed"] == 1
    assert result["queued"] == 0
    assert result["failed"] == 0
    assert result["blocked_access"] == 1
    assert result["open_bundles"] == 0


@pytest.mark.asyncio
async def test_curate_once_keeps_pending_topic_retryable_without_regeneration(tmp_path, monkeypatch):
    database = tmp_path / "rag.sqlite3"
    state = tmp_path / "curation.sqlite3"
    monkeypatch.setenv("YAGAPON_RAG_DB_PATH", str(database))
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS", "10")
    store = RagStore(database)
    document = source_document()
    store.upsert_document(
        "documents/primary",
        document["body"],
        [
            {"key": "document_key", "string_value": document["document_key"]},
            {"key": "guild_id", "numeric_value": 123},
            {"key": "channel_id", "numeric_value": 10},
            {"key": "channel_name", "string_value": "開発"},
            {"key": "category", "string_value": "28th IT局"},
            {"key": "festival", "numeric_value": 28},
            {"key": "source_type", "string_value": "discord_conversation"},
            {"key": "authority", "string_value": "conversation"},
            {"key": "status", "string_value": "raw"},
            {"key": "access_scope", "string_value": "channel"},
            {"key": "source_url", "string_value": document["source_url"]},
        ],
    )
    plan = CurationPlan(
        has_knowledge_value=True,
        topic="新歓Webページ",
        aliases=["新歓サイト"],
        search_terms=["新歓Webページ"],
    )
    draft = TopicDraft(
        title="新歓Webページ",
        summary=[EvidenceClaim(text="公開前レビューを検討した。", evidence_ids=["D01"])],
    )
    monkeypatch.setattr(curation, "new_genai_client", lambda _key: object())
    monkeypatch.setattr(curation, "plan_document", AsyncMock(return_value=plan))
    draft_mock = AsyncMock(return_value=draft)
    monkeypatch.setattr(curation, "draft_topic", draft_mock)
    publish_mock = AsyncMock(
        return_value=SimpleNamespace(session_id="curation-pending", status="pending")
    )
    monkeypatch.setattr(curation, "publish_knowledge_bundle", publish_mock)

    first = await curation.curate_once(state_path=state, limit=3)

    assert first["processed"] == 1
    assert first["open_bundles"] == 1
    assert first["retryable"] == 1
    assert first["next_retry_at"]
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_CURATION_MAX_OPEN_BUNDLES", "1")
    second = await curation.curate_once(state_path=state, limit=3)

    assert second["processed"] == 0
    assert second["source"] == 1
    assert second["eligible"] == 1
    assert second["policy_excluded"] == 0
    assert second["actionable"] == 1
    assert second["backpressured"] == 1
    assert second["open_bundles"] == 1
    assert second["max_open_bundles"] == 1
    assert second["retryable"] == 1
    assert second["next_retry_at"] == first["next_retry_at"]
    assert draft_mock.await_count == 1
    assert publish_mock.await_count == 1


def test_failed_delivery_remains_open_and_cannot_be_replaced_by_new_evidence(tmp_path):
    store = TopicStateStore(tmp_path / "topics.sqlite3")
    identity = store.resolve_identity(festival=28, bureau="it", topic="新歓Web")
    store.observe_topic(
        identity,
        title="新歓Web",
        aliases=[],
        fingerprint="one",
        access=AccessDecision("guild", None, "guild-evidence-only"),
    )
    failed = store.record_delivery(
        identity.topic_id,
        fingerprint="one",
        bundle_id="bundle-one",
        status="failed",
    )

    assert failed.has_open_bundle is True
    assert store.count_open_bundles() == 1
    updated = store.observe_topic(
        identity,
        title="新歓Web",
        aliases=[],
        fingerprint="two",
        access=AccessDecision("guild", None, "guild-evidence-only"),
    )
    assert updated.bundle_id == "bundle-one"
    assert updated.delivery_status == "failed"
    assert updated.dirty is True
