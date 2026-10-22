from urllib.parse import quote

import pytest

from bot.firebase_knowledge import COLLECTIONS, FirebaseDocumentBatch, FirebaseKnowledgeReader
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
    assert evidence[0].authority == "live_data"


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

    async def request(method, url, *, payload=None, headers=None):
        captured.update(method=method, url=url, payload=payload, headers=headers)
        return []

    monkeypatch.setattr(reader._directory, "_request", request)

    await reader._collection(
        "standbyPassEvents",
        "yagamy/28/standbyPassEvents",
        ("open", "paused", "closed"),
    )

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/yagamy/28:runQuery")
    assert captured["headers"] == {"X-Goog-Api-Key": "firebase-public-key"}
    field_filter = captured["payload"]["structuredQuery"]["where"]["fieldFilter"]
    assert field_filter["field"]["fieldPath"] == "status"
    assert field_filter["op"] == "IN"
    assert [value["stringValue"] for value in field_filter["value"]["arrayValue"]["values"]] == [
        "open",
        "paused",
        "closed",
    ]


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
    payloads = []

    async def request(_method, _url, *, payload=None, headers=None):
        payloads.append(payload)
        query = payload["structuredQuery"]
        offset = query.get("offset", 0)
        limit = query["limit"]
        return [{"document": item} for item in documents[offset : offset + limit]]

    monkeypatch.setattr(reader._directory, "_request", request)

    batch = await reader._collection(
        "notifications",
        "festival/27/notifications",
        ("published",),
        True,
    )

    assert len(batch) == 3
    assert batch.complete is True
    assert batch.reader_limit == 10
    assert len(payloads) == 2
    assert "offset" not in payloads[0]["structuredQuery"]
    assert payloads[1]["structuredQuery"]["offset"] == 2


async def test_exhaustive_collection_get_follows_page_tokens(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_LIMIT", "2")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_MAX_DOCUMENTS", "10")
    reader = FirebaseKnowledgeReader()
    urls = []
    first = {
        "name": "projects/demo/databases/(default)/documents/festival/27/events/a",
        "fields": {"eventName": {"stringValue": "A"}},
    }
    second = {
        "name": "projects/demo/databases/(default)/documents/festival/27/events/b",
        "fields": {"eventName": {"stringValue": "B"}},
    }

    async def request(_method, url, *, payload=None, headers=None):
        urls.append(url)
        if "pageToken=" not in url:
            return {"documents": [first], "nextPageToken": "next/token"}
        return {"documents": [second]}

    monkeypatch.setattr(reader._directory, "_request", request)

    batch = await reader._collection(
        "events",
        "festival/27/events",
        (),
        True,
    )

    assert [item["name"].rsplit("/", 1)[-1] for item in batch] == ["a", "b"]
    assert batch.complete is True
    assert len(urls) == 2
    assert "pageToken=next%2Ftoken" in urls[1]


async def test_non_config_collection_still_uses_collection_list_get(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    captured = {}

    async def request(method, url, *, payload=None, headers=None):
        captured.update(method=method, url=url, payload=payload, headers=headers)
        return {"documents": []}

    monkeypatch.setattr(reader._directory, "_request", request)

    await reader._collection("events", "festival/28/events", ())

    assert captured["method"] == "GET"
    assert captured["url"].endswith("/festival/28/events?pageSize=100")
    assert captured["payload"] is None


async def test_ranking_config_uses_known_public_document_get(monkeypatch):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    reader = FirebaseKnowledgeReader()
    captured = {}

    async def request(method, url, *, payload=None, headers=None):
        captured.update(method=method, url=url, payload=payload, headers=headers)
        return {
            "name": (
                "projects/demo-project/databases/(default)/documents/"
                "yagamy/27/config/ranking"
            ),
            "fields": {"status": {"stringValue": "result"}},
        }

    monkeypatch.setattr(reader._directory, "_request", request)

    documents = await reader._collection(
        "rankingConfig",
        COLLECTIONS["rankingConfig"]["path"].format(festival=27),
        (),
    )

    assert captured["method"] == "GET"
    assert captured["url"].endswith("/yagamy/27/config/ranking")
    assert documents[0]["fields"]["status"] == {"stringValue": "result"}


@pytest.mark.parametrize("festival", [27, 28])
async def test_config_uses_known_standby_pass_document_get(monkeypatch, festival):
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_BACKEND", "public")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_PROJECT", "demo-project")
    monkeypatch.setenv("YAGAPON_FIREBASE_KNOWLEDGE_API_KEY", "firebase-public-key")
    reader = FirebaseKnowledgeReader()
    captured = {}

    async def request(method, url, *, payload=None, headers=None):
        captured.update(method=method, url=url, payload=payload, headers=headers)
        return {
            "name": (
                "projects/demo-project/databases/(default)/documents/"
                f"yagamy/{festival}/config/standbyPass"
            ),
            "fields": {"isEnabled": {"booleanValue": True}},
        }

    monkeypatch.setattr(reader._directory, "_request", request)

    documents = await reader._collection(
        "config",
        COLLECTIONS["config"]["path"].format(festival=festival),
        (),
    )

    assert captured["method"] == "GET"
    assert captured["url"].endswith(f"/yagamy/{festival}/config/standbyPass")
    assert captured["payload"] is None
    assert captured["headers"] == {"X-Goog-Api-Key": "firebase-public-key"}
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
        "27thのmapのfirestore構造ってどうなってた？",
        source_type="maps",
    ) == []


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

    assert [item.document_key.rsplit("/", 1)[-1] for item in evidence] == [
        "z-last-but-oldest",
        "middle-created-fallback",
        "alphabetically-first-but-newest",
    ]
    assert {item.title for item in evidence} == {"27th 公開通知"}
    assert len({item.source_url for item in evidence}) == 1
    assert evidence[0].source_url.endswith(
        "/data/~2Ffestival~2F27~2Fnotifications"
    )
    assert all(item.document_key.rsplit("/", 1)[-1] not in item.source_url for item in evidence)


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

    assert [item.document_key.rsplit("/", 1)[-1] for item in evidence] == [
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

    assert [item.document_key.rsplit("/", 1)[-1] for item in evidence] == [
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
async def test_ranking_question_fetches_only_public_aggregate_and_status(monkeypatch):
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
                "fields": {"status": {"stringValue": "result"}},
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
    assert any('"status": "result"' in item.body for item in evidence)


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

    ranking = [item for item in evidence if "/ranking/" in item.document_key]
    config = [item for item in evidence if "/config/" in item.document_key]
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
