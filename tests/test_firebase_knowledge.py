from bot.firebase_knowledge import COLLECTIONS, FirebaseKnowledgeReader
from bot.knowledge_authority import QuestionKind


def test_allowlist_never_contains_personal_collections():
    rendered = " ".join(str(config["path"]) for config in COLLECTIONS.values())

    assert "users" not in rendered
    assert "staffInfos" not in rendered
    assert "standbyPassRequests" not in rendered
    assert "receipt" not in rendered


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
