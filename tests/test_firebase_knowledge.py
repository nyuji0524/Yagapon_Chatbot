import json
from urllib.parse import quote

import pytest

from bot.firebase_knowledge import COLLECTIONS, FirebaseDocumentBatch, FirebaseKnowledgeReader
from bot.firestore_readonly import FirestoreListPage
from bot.knowledge_authority import QuestionKind


def _notification_document(
    doc_id: str,
    *,
    title: str,
    published_at: str | None = None,
    created_at: str | None = None,
) -> dict:
    fields = {
        "title": {"stringValue": title},
        "status": {"stringValue": "published"},
    }
    if published_at:
        fields["publishedAt"] = {"timestampValue": published_at}
    if created_at:
        fields["createdAt"] = {"timestampValue": created_at}
    return {
        "name": (
            "projects/demo-project/databases/(default)/documents/"
            f"festival/27/notifications/{doc_id}"
        ),
        "fields": fields,
    }


def _ranking_document(
    doc_id: str,
    *,
    event_name: str,
    category: str,
    score: int,
) -> dict:
    return {
        "name": (
            "projects/demo-project/databases/(default)/documents/"
            f"yagamy/27/ranking/{doc_id}"
        ),
        "fields": {
            "eventId": {"stringValue": doc_id},
            "eventName": {"stringValue": event_name},
            "category": {"stringValue": category},
            "score": {"integerValue": str(score)},
            # A future accidental expansion of the document must not leak.
            "voterIds": {"arrayValue": {"values": [{"stringValue": "private-user"}]}},
        },
    }
def test_allowlist_never_contains_personal_collections():
    rendered = " ".join(str(config["path"]) for config in COLLECTIONS.values())

    assert "users" not in rendered
    assert "staffInfos" not in rendered
    assert "standbyPassRequests" not in rendered
    assert "receipt" not in rendered
    assert COLLECTIONS["ranking"]["fields"] == (
        "eventId",
        "eventName",
        "category",
        "score",
    )
    assert "areaImagePaths" not in COLLECTIONS["maps"]["fields"]


def test_firebase_projection_drops_unapproved_fields(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "emulator")
    reader = FirebaseKnowledgeReader()
    document = {
        "name": "projects/demo/databases/(default)/documents/festival/28/events/ai-tag",
        "fields": {
            "eventName": {"stringValue": "AI鬼ごっこ"},
            "status": {"stringValue": "published"},
            "ownerEmail": {"stringValue": "private@example.com"},
        },
    }

    evidence = reader._rank(
        "AI鬼ごっこの現在の開催状況",
        "events",
        COLLECTIONS["events"]["fields"],
        [document],
        28,
    )

    assert len(evidence) == 1
    assert "AI鬼ごっこ" in evidence[0].body
    assert "private@example.com" not in evidence[0].body
    assert "collection_path: festival/28/events" in evidence[0].body
    assert evidence[0].authority == "live_data"


def test_evidence_path_comes_from_registry_not_fetched_document_name(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    document = {
        # Treat the entire fetched name as untrusted. It may select ordering,
        # but it must not redefine the path rendered as the contract.
        "name": (
            "projects/demo-project/databases/(default)/documents/"
            "users/private.person@example.com/secrets/map"
        ),
        "fields": {
            "mapId": {"stringValue": "public-map"},
            "status": {"stringValue": "published"},
        },
    }

    evidence = reader._rank(
        "27thのmapのFirestore構造はどうなっていた？",
        "maps",
        COLLECTIONS["maps"]["fields"],
        [document],
        27,
    )

    assert len(evidence) == 1
    assert "collection_path: yagamy/27/maps" in evidence[0].body
    assert "private.person@example.com" not in repr(evidence[0])
    assert evidence[0].source_url.endswith("/data/~2Fyagamy~2F27~2Fmaps")


def test_known_document_evidence_uses_registry_document_path(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    document = {
        "name": (
            "projects/demo-project/databases/(default)/documents/"
            "wrong/path/from-fetched-data"
        ),
        "fields": {"status": {"stringValue": "result"}},
    }

    evidence = reader._rank(
        "27thのrankingの構造はどうなっていた？",
        "rankingConfig",
        COLLECTIONS["rankingConfig"]["fields"],
        [document],
        27,
    )

    assert len(evidence) == 1
    assert "document_path: yagamy/27/config/ranking" in evidence[0].body
    assert "wrong/path" not in repr(evidence[0])


def test_nested_projection_drops_unknown_children_and_sensitive_urls(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    document = {
        "name": (
            "projects/demo/databases/(default)/documents/"
            "festival/27/notifications/notice-1"
        ),
        "fields": {
            "title": {"stringValue": "公開通知"},
            "status": {"stringValue": "published"},
            "action": {
                "mapValue": {
                    "fields": {
                        "type": {"stringValue": "web"},
                        "label": {"stringValue": "開く"},
                        "url": {
                            "stringValue": (
                                "https://example.com/open?X-Goog-Signature=private-signature"
                            )
                        },
                        "voterIds": {
                            "arrayValue": {"values": [{"stringValue": "private-user"}]}
                        },
                        "accountNumber": {"stringValue": "1234567"},
                    }
                }
            },
        },
    }

    evidence = reader._rank(
        "公開通知を教えて",
        "notifications",
        COLLECTIONS["notifications"]["fields"],
        [document],
        27,
    )

    assert len(evidence) == 1
    assert '"type": "web"' in evidence[0].body
    assert '"label": "開く"' in evidence[0].body
    assert "X-Goog-Signature" not in evidence[0].body
    assert "private-signature" not in evidence[0].body
    assert "private-user" not in evidence[0].body
    assert "accountNumber" not in evidence[0].body


@pytest.mark.parametrize(
    "private_body",
    [
        "担当: 山田太郎 / 学籍番号: 12345678",
        "連絡先 private.person@example.com",
        "電話 090-1234-5678",
        "api_key=ActualSecretValue982734",
    ],
)
def test_allowed_scalar_private_data_is_dropped_before_model(monkeypatch, private_body):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    document = {
        "name": (
            "projects/demo-project/databases/(default)/documents/"
            "festival/27/notifications/private-body"
        ),
        "fields": {
            "title": {"stringValue": "安全な公開通知"},
            "body": {"stringValue": private_body},
            "status": {"stringValue": "published"},
        },
    }

    evidence = reader._rank(
        "公開通知を教えて",
        "notifications",
        COLLECTIONS["notifications"]["fields"],
        [document],
        27,
    )

    assert len(evidence) == 1
    assert "安全な公開通知" in evidence[0].body
    assert private_body not in evidence[0].body
    assert '"body"' not in evidence[0].body


def test_harmless_format_characters_are_removed_without_dropping_public_text():
    value = "安全な文\u200b字と\ufeff告知"

    assert FirebaseKnowledgeReader._safe_public_text(value) == "安全な文字と告知"


@pytest.mark.parametrize(
    "private_value",
    [
        "private\u200b@example.com",
        "api_\u2060key=ActualSecretValue982734",
    ],
)
def test_format_characters_cannot_obfuscate_private_data(private_value):
    assert not isinstance(FirebaseKnowledgeReader._safe_public_text(private_value), str)


@pytest.mark.parametrize(
    "public_body",
    [
        "開催します☀️",
        "家族向け👨‍👩‍👧",
    ],
)
def test_public_emoji_text_is_preserved_before_model(monkeypatch, public_body):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    document = {
        "name": (
            "projects/demo-project/databases/(default)/documents/"
            "festival/27/notifications/public-emoji"
        ),
        "fields": {
            "title": {"stringValue": "公開通知"},
            "body": {"stringValue": public_body},
            "status": {"stringValue": "published"},
        },
    }

    evidence = reader._rank(
        "公開通知を教えて",
        "notifications",
        COLLECTIONS["notifications"]["fields"],
        [document],
        27,
    )

    assert len(evidence) == 1
    assert public_body in evidence[0].body


@pytest.mark.parametrize(
    "private_body",
    [
        "A" * 495 + "private@example.com",
        "A" * 495 + "api_key=ActualSecretValue982734",
    ],
)
def test_private_data_beyond_output_boundary_drops_entire_scalar(monkeypatch, private_body):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    document = {
        "name": (
            "projects/demo-project/databases/(default)/documents/"
            "festival/27/notifications/private-boundary"
        ),
        "fields": {
            "title": {"stringValue": "公開通知"},
            "body": {"stringValue": private_body},
            "status": {"stringValue": "published"},
        },
    }

    evidence = reader._rank(
        "公開通知を教えて",
        "notifications",
        COLLECTIONS["notifications"]["fields"],
        [document],
        27,
    )

    assert len(evidence) == 1
    assert '"body"' not in evidence[0].body


def test_sensitive_document_id_never_reaches_evidence(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    sensitive_id = "private.person@example.com"
    document = {
        "name": (
            "projects/demo-project/databases/(default)/documents/"
            f"festival/27/notifications/{sensitive_id}"
        ),
        "fields": {
            "title": {"stringValue": "公開通知"},
            "status": {"stringValue": "published"},
        },
    }

    evidence = reader._rank(
        "公開通知一覧",
        "notifications",
        COLLECTIONS["notifications"]["fields"],
        [document],
        27,
        exhaustive=True,
    )

    assert len(evidence) == 1
    rendered = repr(evidence[0])
    assert evidence[0].document_key == "firebase:notifications:1"
    assert sensitive_id not in rendered
    assert "document:" not in evidence[0].body
    assert reader._rank(
        sensitive_id,
        "notifications",
        COLLECTIONS["notifications"]["fields"],
        [document],
        27,
    ) == []


def test_wire_field_mask_uses_only_reviewed_nested_leaves():
    fields = FirebaseKnowledgeReader._wire_field_paths("notifications")

    assert "action" not in fields
    assert {"action.type", "action.url", "action.label", "action.targetId"}.issubset(fields)
    assert "ownerEmail" not in fields

    event_fields = FirebaseKnowledgeReader._wire_field_paths("events")
    assert "coupon" not in event_fields
    assert "coupon.discount" in event_fields
    assert "coupon.itemIds" in event_fields
    assert "fcmTokens" not in event_fields


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/open?recipient=private%40example.com",
        "https://example.com/open?token=secret-value",
        "https://example.com/open#access_token=secret-value",
        "https://example.com/open?next=https%3A%2F%2Fstorage.example%2Fa%3Ftoken%3Dsecret-value",
        "https://example.com/open?next=private%2540example.com",
        "javascript:alert(1)",
        "http://example.com/insecure",
    ],
)
def test_nested_projection_rejects_private_or_unsafe_urls(url):
    assert FirebaseKnowledgeReader._safe_public_url(url) != url


@pytest.mark.parametrize("private_value", ["private@example.com", "access_token=secret-value"])
def test_nested_projection_rejects_arbitrarily_nested_encoded_secrets(private_value):
    encoded = private_value
    for _ in range(40):
        encoded = quote(encoded, safe="")
    url = f"https://example.com/open?next={encoded}"

    assert FirebaseKnowledgeReader._safe_public_url(url) != url


async def test_authorization_questions_never_query_firebase(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "emulator")
    reader = FirebaseKnowledgeReader()

    result = await reader.evidence_for(
        "整理券を操作できる権限は誰ですか",
        QuestionKind.AUTHORIZATION,
        festival=28,
    )

    assert result == []


async def test_public_backend_uses_rules_compatible_status_query(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_API_KEY", "firebase-public-key")
    reader = FirebaseKnowledgeReader()
    captured = {}

    async def run_status_query(parent, collection_id, fields, statuses, limit, offset):
        captured.update(
            parent=parent,
            collection_id=collection_id,
            fields=fields,
            statuses=statuses,
            limit=limit,
            offset=offset,
        )
        return ()

    monkeypatch.setattr(reader._client, "run_status_query", run_status_query)

    await reader._collection(
        "standbyPassEvents",
        "yagamy/28/standbyPassEvents",
        ("open", "paused", "closed"),
    )

    assert captured["parent"] == "yagamy/28"
    assert captured["collection_id"] == "standbyPassEvents"
    assert captured["statuses"] == ("open", "paused", "closed")
    assert captured["limit"] == 100
    assert captured["offset"] == 0
    assert "status" in captured["fields"]
    assert "eventId" in captured["fields"]


async def test_exhaustive_status_query_pages_until_complete(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_LIMIT", "2")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_MAX_DOCUMENTS", "10")
    reader = FirebaseKnowledgeReader()
    documents = [
        _notification_document(f"notice-{index}", title=f"通知{index}")
        for index in range(3)
    ]
    calls = []

    async def run_status_query(_parent, _collection_id, _fields, _statuses, limit, offset):
        calls.append((limit, offset))
        return tuple(documents[offset : offset + limit])

    monkeypatch.setattr(reader._client, "run_status_query", run_status_query)

    batch = await reader._collection(
        "notifications",
        "festival/27/notifications",
        ("published",),
        True,
    )

    assert len(batch) == 3
    assert batch.complete is True
    assert batch.reader_limit == 10
    assert calls == [(2, 0), (2, 2)]


async def test_exhaustive_collection_get_follows_page_tokens(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_LIMIT", "2")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_MAX_DOCUMENTS", "10")
    reader = FirebaseKnowledgeReader()
    calls = []
    first = {
        "name": "projects/demo/databases/(default)/documents/festival/27/events/a",
        "fields": {"eventName": {"stringValue": "A"}},
    }
    second = {
        "name": "projects/demo/databases/(default)/documents/festival/27/events/b",
        "fields": {"eventName": {"stringValue": "B"}},
    }

    async def list_collection(path, fields, page_size, page_token):
        calls.append((path, fields, page_size, page_token))
        if page_token is None:
            return FirestoreListPage((first,), "next/token")
        return FirestoreListPage((second,), None)

    monkeypatch.setattr(reader._client, "list_collection", list_collection)

    batch = await reader._collection(
        "events",
        "festival/27/events",
        (),
        True,
    )

    assert [item["name"].rsplit("/", 1)[-1] for item in batch] == ["a", "b"]
    assert batch.complete is True
    assert len(calls) == 2
    assert calls[0][3] is None
    assert calls[1][3] == "next/token"


async def test_non_config_collection_still_uses_collection_list_get(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    captured = {}

    async def list_collection(path, fields, page_size, page_token):
        captured.update(
            path=path,
            fields=fields,
            page_size=page_size,
            page_token=page_token,
        )
        return FirestoreListPage((), None)

    monkeypatch.setattr(reader._client, "list_collection", list_collection)

    await reader._collection("events", "festival/28/events", ())

    assert captured["path"] == "festival/28/events"
    assert captured["page_size"] == 100
    assert captured["page_token"] is None
    assert "eventName" in captured["fields"]


async def test_ranking_config_uses_known_public_document_get(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    captured = {}

    async def get_document(path, fields):
        captured.update(path=path, fields=fields)
        return {
            "name": (
                "projects/demo-project/databases/(default)/documents/"
                "yagamy/27/config/ranking"
            ),
            "fields": {"status": {"stringValue": "result"}},
        }

    monkeypatch.setattr(reader._client, "get_document", get_document)

    documents = await reader._collection(
        "rankingConfig",
        COLLECTIONS["rankingConfig"]["path"].format(festival=27),
        (),
    )

    assert captured["path"] == "yagamy/27/config/ranking"
    assert captured["fields"] == ("status", "votingPhase", "updatedAt")
    assert documents[0]["fields"]["status"] == {"stringValue": "result"}


@pytest.mark.parametrize("festival", [27, 28])
async def test_config_uses_known_standby_pass_document_get(monkeypatch, festival):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_API_KEY", "firebase-public-key")
    reader = FirebaseKnowledgeReader()
    captured = {}

    async def get_document(path, fields):
        captured.update(path=path, fields=fields)
        return {
            "name": (
                "projects/demo-project/databases/(default)/documents/"
                f"yagamy/{festival}/config/standbyPass"
            ),
            "fields": {"isEnabled": {"booleanValue": True}},
        }

    monkeypatch.setattr(reader._client, "get_document", get_document)

    documents = await reader._collection(
        "config",
        COLLECTIONS["config"]["path"].format(festival=festival),
        (),
    )

    assert captured["path"] == f"yagamy/{festival}/config/standbyPass"
    assert "isEnabled" in captured["fields"]
    assert documents[0]["fields"]["isEnabled"] == {"booleanValue": True}


async def test_one_firebase_collection_failure_keeps_other_evidence(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    document = {
        "name": "projects/demo/databases/(default)/documents/yagamy/28/config/standbyPass",
        "fields": {"isEnabled": {"booleanValue": True}},
    }

    async def collection(name, _path, _statuses):
        if name == "standbyPassEvents":
            raise RuntimeError("temporary failure")
        return [document]

    monkeypatch.setattr(reader, "_collection", collection)

    result = await reader.evidence_for(
        "整理券の公開設定",
        QuestionKind.RUNTIME,
        festival=28,
    )

    assert len(result) == 1
    assert "isEnabled" in result[0].body


async def test_missing_festival_uses_current_operating_year(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    monkeypatch.setattr("bot.firebase_knowledge.current_festival", lambda: 28)
    reader = FirebaseKnowledgeReader()
    captured_paths = []

    async def collection(_name, path, _statuses):
        captured_paths.append(path)
        return []

    monkeypatch.setattr(reader, "_collection", collection)

    result = await reader.evidence_for(
        "整理券のFirestore構造を教えて",
        QuestionKind.DATA_CONTRACT,
        festival=None,
    )

    assert result == []
    assert captured_paths == ["yagamy/28/standbyPassEvents"]


def test_generic_festival_runtime_question_does_not_become_a_false_search_term():
    assert FirebaseKnowledgeReader._terms("27thの現在の整理券") == []
    assert FirebaseKnowledgeReader._terms("最新の通知をすべて一覧で確認") == []
    assert FirebaseKnowledgeReader._terms("27thの最新の公開通知を一覧で確認したい") == []
    assert FirebaseKnowledgeReader._terms("公開通知を全件教えて") == []
    assert FirebaseKnowledgeReader._terms(
        "firebaseを読んで、27thの大賞企画の結果を教えてください",
        source_type="ranking",
    ) == []
    assert FirebaseKnowledgeReader._terms(
        "firebaseを読んで、27thの大賞企画の結果を票数も含めて教えて",
        source_type="ranking",
    ) == []
    assert FirebaseKnowledgeReader._terms(
        "27thのmapのFirestore構造をフィールドも含めて教えて",
        source_type="maps",
    ) == []
    assert FirebaseKnowledgeReader._terms(
        "27thのmapのfirestore構造ってどうなってた？",
        source_type="maps",
    ) == []


def test_query_modifiers_do_not_remove_real_firebase_value_filters():
    assert FirebaseKnowledgeReader._terms(
        "ITラボの票数も含めて教えて",
        source_type="ranking",
    ) == ["ITラボ"]
    assert FirebaseKnowledgeReader._terms(
        "schemaVersionも含めてmapを教えて",
        source_type="maps",
    ) == ["schemaVersion"]


@pytest.mark.asyncio
async def test_ranking_include_modifier_does_not_filter_every_public_result(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()

    async def collection(name, _path, _statuses):
        if name == "ranking":
            return [
                _ranking_document(
                    "winner",
                    event_name="優勝企画",
                    category="exhibition",
                    score=50,
                )
            ]
        return [{
            "name": (
                "projects/demo-project/databases/(default)/documents/"
                "yagamy/27/config/ranking"
            ),
            "fields": {"status": {"stringValue": "result"}},
        }]

    monkeypatch.setattr(reader, "_collection", collection)

    evidence = await reader.evidence_for(
        "firebaseを読んで、27thの大賞企画の結果を票数も含めて教えて",
        QuestionKind.RUNTIME,
        festival=27,
    )

    assert any("優勝企画" in item.body for item in evidence)
    assert any('"score": 50' in item.body for item in evidence)


@pytest.mark.parametrize("phrase", ["一覧", "すべて", "全て", "全部", "全件"])
def test_exhaustive_query_intent_is_explicit(phrase):
    assert FirebaseKnowledgeReader._is_exhaustive_query(f"27thの通知を{phrase}教えて")


@pytest.mark.parametrize(
    "question",
    [
        "整理券のFAQ一覧",
        "整理券の機能一覧",
        "イベントに必要な持ち物一覧",
        "通知機能の一覧",
        "整理券についてすべて教えて",
    ],
)
def test_exhaustive_query_rejects_explanatory_lists(question):
    assert not FirebaseKnowledgeReader._is_exhaustive_query(question)


@pytest.mark.asyncio
async def test_explicit_notification_list_returns_every_document_up_to_reader_limit(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    documents = [
        _notification_document(
            f"notice-{index}",
            title=f"通知{index}",
            published_at=f"2026-09-{index + 1:02d}T00:00:00Z",
        )
        for index in range(8)
    ]
    async def collection(*_args):
        return documents

    monkeypatch.setattr(reader, "_collection", collection)

    evidence = await reader.evidence_for(
        "27thの公開通知一覧を教えて",
        QuestionKind.GENERAL,
        festival=27,
    )

    assert len(evidence) == 8
    assert "returned=8" in evidence[0].body
    assert "matched=8" in evidence[0].body
    assert "firestore_fetched=8" in evidence[0].body
    assert "completeness=complete" in evidence[0].body
    assert "item: 1/8" in evidence[0].body
    assert "list_result:" not in evidence[1].body
    assert "item: 8/8" in evidence[-1].body


@pytest.mark.asyncio
async def test_non_exhaustive_notification_question_keeps_five_item_limit(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    documents = [
        _notification_document(
            f"notice-{index}",
            title=f"通知{index}",
            published_at=f"2026-09-{index + 1:02d}T00:00:00Z",
        )
        for index in range(8)
    ]
    async def collection(*_args):
        return documents

    monkeypatch.setattr(reader, "_collection", collection)

    evidence = await reader.evidence_for(
        "27thの通知を教えて",
        QuestionKind.GENERAL,
        festival=27,
    )

    assert len(evidence) == 5
    assert "list_result:" not in evidence[0].body


def test_notification_list_uses_real_timestamp_oldest_first_and_shared_collection_citation(
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    documents = [
        _notification_document(
            "alphabetically-first-but-newest",
            title="最新",
            published_at="2026-09-20T12:00:00Z",
        ),
        _notification_document(
            "middle-created-fallback",
            title="中間",
            created_at="2026-09-19T12:00:00Z",
        ),
        _notification_document(
            "z-last-but-oldest",
            title="最古",
            published_at="2026-09-18T12:00:00+00:00",
        ),
    ]

    evidence = reader._rank(
        "27thの公開通知一覧を教えて",
        "notifications",
        COLLECTIONS["notifications"]["fields"],
        documents,
        27,
        exhaustive=True,
    )

    assert [item.document_key for item in evidence] == [
        "firebase:notifications:1",
        "firebase:notifications:2",
        "firebase:notifications:3",
    ]
    assert [json.loads(item.body.split("fields: ", 1)[1])["title"] for item in evidence] == [
        "最古",
        "中間",
        "最新",
    ]
    assert {item.title for item in evidence} == {"27th 公開通知"}
    assert len({item.source_url for item in evidence}) == 1
    assert evidence[0].source_url.endswith(
        "/data/~2Ffestival~2F27~2Fnotifications"
    )
    assert all("notice" not in item.source_url for item in evidence)


def test_exhaustive_non_notification_collection_has_stable_document_order(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    documents = [
        {
            "name": f"projects/demo/databases/(default)/documents/festival/27/events/{doc_id}",
            "fields": {"eventName": {"stringValue": doc_id}},
        }
        for doc_id in ("z-event", "a-event", "m-event")
    ]

    evidence = reader._rank(
        "27thの企画一覧",
        "events",
        COLLECTIONS["events"]["fields"],
        documents,
        27,
        exhaustive=True,
    )

    assert [item.document_key for item in evidence] == [
        "firebase:events:1",
        "firebase:events:2",
        "firebase:events:3",
    ]
    assert [json.loads(item.body.split("fields: ", 1)[1])["eventName"] for item in evidence] == [
        "a-event",
        "m-event",
        "z-event",
    ]


def test_exhaustive_header_does_not_claim_completeness_at_reader_limit(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_LIMIT", "2")
    reader = FirebaseKnowledgeReader()
    documents = [
        _notification_document(
            f"notice-{index}",
            title=f"通知{index}",
            published_at=f"2026-09-{index + 1:02d}T00:00:00Z",
        )
        for index in range(2)
    ]

    evidence = reader._rank(
        "27thの公開通知一覧",
        "notifications",
        COLLECTIONS["notifications"]["fields"],
        documents,
        27,
        exhaustive=True,
    )

    assert len(evidence) == 2
    assert "reader_limit=2" in evidence[0].body
    assert "completeness=unknown-limit-reached" in evidence[0].body


def test_exhaustive_rank_does_not_truncate_at_page_size(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_LIMIT", "2")
    reader = FirebaseKnowledgeReader()
    documents = FirebaseDocumentBatch(
        [
            _notification_document(f"notice-{index}", title=f"通知{index}")
            for index in range(3)
        ],
        complete=True,
        reader_limit=10,
    )

    evidence = reader._rank(
        "27thの公開通知一覧",
        "notifications",
        COLLECTIONS["notifications"]["fields"],
        documents,
        27,
        exhaustive=True,
    )

    assert len(evidence) == 3
    assert "returned=3" in evidence[0].body
    assert "completeness=complete" in evidence[0].body


def test_ranking_result_returns_each_category_podium_and_no_private_vote_data(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    documents = [
        _ranking_document("stall-a", event_name="模擬店A", category="stall", score=30),
        _ranking_document("stall-b", event_name="模擬店B", category="stall", score=20),
        _ranking_document("stall-c", event_name="模擬店C", category="stall", score=20),
        _ranking_document("stall-d", event_name="模擬店D", category="stall", score=10),
        _ranking_document("ex-a", event_name="展示A", category="exhibition", score=40),
        _ranking_document("ex-b", event_name="展示B", category="exhibition", score=25),
        _ranking_document("ex-c", event_name="展示C", category="exhibition", score=15),
        _ranking_document("ex-d", event_name="展示D", category="exhibition", score=5),
    ]

    evidence = reader._rank(
        "Firebaseを読んで、27thの大賞企画の結果を票数つきで教えて",
        "ranking",
        COLLECTIONS["ranking"]["fields"],
        documents,
        27,
    )

    assert [item.document_key for item in evidence] == [
        "firebase:ranking:1",
        "firebase:ranking:2",
        "firebase:ranking:3",
        "firebase:ranking:4",
        "firebase:ranking:5",
        "firebase:ranking:6",
    ]
    assert [json.loads(item.body.split("fields: ", 1)[1])["eventId"] for item in evidence] == [
        "ex-a",
        "ex-b",
        "ex-c",
        "stall-a",
        "stall-b",
        "stall-c",
    ]
    assert [
        next(line for line in item.body.splitlines() if line.startswith("computed_rank:"))
        for item in evidence
    ] == [
        "computed_rank: 1",
        "computed_rank: 2",
        "computed_rank: 3",
        "computed_rank: 1",
        "computed_rank: 2",
        "computed_rank: 2",
    ]
    assert all("private-user" not in item.body for item in evidence)
    assert {item.title for item in evidence} == {"27th 大賞企画の公開集計"}


@pytest.mark.asyncio
@pytest.mark.parametrize("public_status", ["result", "結果発表"])
async def test_ranking_question_fetches_only_public_aggregate_and_status(
    monkeypatch,
    public_status,
):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    requested = []

    async def collection(name, path, _statuses):
        requested.append((name, path))
        if name == "ranking":
            return [
                _ranking_document(
                    "winner",
                    event_name="優勝企画",
                    category="exhibition",
                    score=50,
                )
            ]
        if name == "rankingConfig":
            return [{
                "name": (
                    "projects/demo-project/databases/(default)/documents/"
                    "yagamy/27/config/ranking"
                ),
                "fields": {"status": {"stringValue": public_status}},
            }]
        return []

    monkeypatch.setattr(reader, "_collection", collection)

    evidence = await reader.evidence_for(
        "27thのrankingの結果と票数を教えて",
        QuestionKind.RUNTIME,
        festival=27,
    )

    assert requested == [
        ("ranking", "yagamy/27/ranking"),
        ("rankingConfig", "yagamy/27/config/ranking"),
    ]
    assert any("優勝企画" in item.body and '"score": 50' in item.body for item in evidence)
    assert any(f'"status": "{public_status}"' in item.body for item in evidence)


@pytest.mark.asyncio
async def test_data_contract_emits_value_free_reviewed_schema_evidence(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()

    async def collection(name, _path, _statuses):
        if name == "ranking":
            return [
                _ranking_document(
                    "winner",
                    event_name="優勝企画",
                    category="exhibition",
                    score=50,
                )
            ]
        return [{
            "name": (
                "projects/demo-project/databases/(default)/documents/"
                "yagamy/27/config/ranking"
            ),
            "fields": {"status": {"stringValue": "result"}},
        }]

    monkeypatch.setattr(reader, "_collection", collection)

    evidence = await reader.evidence_for(
        "27thのrankingの構造はどうなっていた？",
        QuestionKind.DATA_CONTRACT,
        festival=27,
    )

    schema = [item for item in evidence if item.document_key.startswith("firebase:schema:")]
    assert [item.document_key for item in schema] == [
        "firebase:schema:ranking",
        "firebase:schema:rankingConfig",
    ]
    assert "collection_path: yagamy/27/ranking" in schema[0].body
    assert 'allowlisted_fields: ["eventId", "eventName", "category", "score"]' in schema[0].body
    assert 'observed_fields: ["eventId", "eventName", "category", "score"]' in schema[0].body
    assert "document_path: yagamy/27/config/ranking" in schema[1].body
    assert all("優勝企画" not in item.body and '"score": 50' not in item.body for item in schema)


@pytest.mark.asyncio
async def test_data_contract_schema_keeps_unpublished_ranking_values_withheld(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()

    async def collection(name, _path, _statuses):
        if name == "ranking":
            return [
                _ranking_document(
                    "winner",
                    event_name="未公開の優勝企画",
                    category="exhibition",
                    score=99,
                )
            ]
        return [{
            "name": (
                "projects/demo-project/databases/(default)/documents/"
                "yagamy/27/config/ranking"
            ),
            "fields": {"status": {"stringValue": "voting"}},
        }]

    monkeypatch.setattr(reader, "_collection", collection)

    evidence = await reader.evidence_for(
        "27thのrankingの構造はどうなっていた？",
        QuestionKind.DATA_CONTRACT,
        festival=27,
    )

    ranking_schema = next(
        item for item in evidence if item.document_key == "firebase:schema:ranking"
    )
    assert "observation: withheld-until-public-result" in ranking_schema.body
    assert "observed_fields: []" in ranking_schema.body
    assert all("未公開の優勝企画" not in item.body for item in evidence)
    assert all('"score": 99' not in item.body for item in evidence)


@pytest.mark.parametrize("config_status", ["voting", "draft", "", None])
@pytest.mark.asyncio
async def test_ranking_scores_are_withheld_until_result_is_public(
    monkeypatch,
    config_status,
):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()

    async def collection(name, _path, _statuses):
        if name == "ranking":
            return [
                _ranking_document(
                    "winner",
                    event_name="未公開の優勝企画",
                    category="exhibition",
                    score=99,
                )
            ]
        if config_status is None:
            return []
        return [{
            "name": (
                "projects/demo-project/databases/(default)/documents/"
                "yagamy/27/config/ranking"
            ),
            "fields": {"status": {"stringValue": config_status}},
        }]

    monkeypatch.setattr(reader, "_collection", collection)

    evidence = await reader.evidence_for(
        "27thの大賞企画の結果と票数を教えて",
        QuestionKind.RUNTIME,
        festival=27,
    )

    assert all("未公開の優勝企画" not in item.body for item in evidence)
    assert all('"score": 99' not in item.body for item in evidence)


@pytest.mark.asyncio
async def test_ranking_categories_and_config_are_not_globally_truncated(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    ranking_documents = [
        _ranking_document(
            f"{category}-{index}",
            event_name=f"{category}{index}",
            category=category,
            score=40 - index,
        )
        for category in ("exhibition", "stall")
        for index in range(1, 5)
    ]

    async def collection(name, _path, _statuses):
        if name == "ranking":
            return ranking_documents
        return [{
            "name": (
                "projects/demo-project/databases/(default)/documents/"
                "yagamy/27/config/ranking"
            ),
            "fields": {"status": {"stringValue": "result"}},
        }]

    monkeypatch.setattr(reader, "_collection", collection)

    evidence = await reader.evidence_for(
        "27thの大賞企画の結果を票数もあわせて教えてください",
        QuestionKind.RUNTIME,
        festival=27,
    )

    ranking = [item for item in evidence if item.title.endswith("公開集計")]
    config = [item for item in evidence if item.title.endswith("公開状態")]
    assert len(ranking) == 6
    assert len(config) == 1


def test_map_projection_keeps_public_schema_but_drops_storage_tokens(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    document = {
        "name": (
            "projects/demo-project/databases/(default)/documents/"
            "yagamy/27/maps/yagami-campus"
        ),
        "fields": {
            "mapId": {"stringValue": "yagami-campus"},
            "kind": {"stringValue": "campus"},
            "status": {"stringValue": "published"},
            "schemaVersion": {"integerValue": "1"},
            "mapVersion": {"stringValue": "2026-01"},
            "areaImagePaths": {
                "mapValue": {
                    "fields": {
                        "stage": {
                            "stringValue": (
                                "https://storage.example/stage.webp?token=secret-download-token"
                            )
                        }
                    }
                }
            },
        },
    }

    evidence = reader._rank(
        "27thのmapのFirestore構造はどうなっていた？",
        "maps",
        COLLECTIONS["maps"]["fields"],
        [document],
        27,
    )

    assert len(evidence) == 1
    assert '"schemaVersion": 1' in evidence[0].body
    assert "secret-download-token" not in evidence[0].body
