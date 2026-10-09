import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from api.server import create_app
from bot.corpus import BackfillResult, RagAnswer


class FakeConfig:
    def __init__(self):
        self.glossary = {}

    def get_corpus(self, guild_id):
        return "fileSearchStores/test" if guild_id == 1 else None

    def get_glossary_text(self, guild_id):
        return "用語集"

    def is_ignored(self, guild_id, channel_id):
        return False

    def get_backfill_cursor(self, guild_id, channel_id):
        return None

    async def set_backfill_cursor(self, guild_id, channel_id, message_id, message_at):
        return None

    def get_glossary(self, guild_id):
        return self.glossary

    async def set_glossary(self, guild_id, glossary):
        self.glossary = glossary


def make_client():
    channel = SimpleNamespace(
        id=20,
        name="general",
        permissions_for=lambda member: SimpleNamespace(read_message_history=True),
    )
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(),
        text_channels=[channel],
        get_channel=lambda channel_id: channel if channel_id == 20 else None,
    )
    corpus = SimpleNamespace(
        query=AsyncMock(return_value="回答"),
        query_with_trace=AsyncMock(return_value=RagAnswer(
            text="回答",
            query_id="query-1",
            citations=("source",),
            festival=28,
        )),
        rag_store=SimpleNamespace(
            summary=Mock(return_value={"queries": 0}),
            recent_queries=Mock(return_value=[]),
        ),
        start_backfill=Mock(return_value=True),
        finish_backfill=Mock(),
        backfill_channel=AsyncMock(return_value=BackfillResult(
            messages_indexed=2,
            documents_uploaded=1,
        )),
    )
    bot = SimpleNamespace(
        is_closed=lambda: False,
        user="YagaPon",
        guilds=[],
        config=FakeConfig(),
        corpus=corpus,
        get_guild=lambda guild_id: guild if guild_id == 1 else None,
        _build_members_info=lambda guild_id: "メンバー情報",
    )
    return TestClient(create_app(bot)), bot


def test_health_is_public(monkeypatch):
    monkeypatch.delenv("YAGAPON_API_TOKEN", raising=False)
    client, _ = make_client()

    assert client.get("/health").status_code == 200


def test_status_fails_closed_without_configured_token(monkeypatch):
    monkeypatch.delenv("YAGAPON_API_TOKEN", raising=False)
    client, _ = make_client()

    assert client.get("/status").status_code == 503


def test_ask_requires_token_and_passes_guild_context(monkeypatch):
    monkeypatch.setenv("YAGAPON_API_TOKEN", "secret")
    client, bot = make_client()

    assert client.post("/ask", json={"guild_id": 1, "query": "質問"}).status_code == 401
    response = client.post(
        "/ask",
        headers={"Authorization": "Bearer secret"},
        json={"guild_id": 1, "query": "質問"},
    )

    assert response.status_code == 200
    bot.corpus.query_with_trace.assert_awaited_once_with(
        "質問",
        "fileSearchStores/test",
        guild_id=1,
        members_info="メンバー情報",
        glossary_text="用語集",
        glossary={},
    )
    assert response.json()["query_id"] == "query-1"
    assert response.json()["sources"] == ["source"]


def test_backfill_exposes_job_progress(monkeypatch):
    monkeypatch.setenv("YAGAPON_API_TOKEN", "secret")
    client, bot = make_client()
    headers = {"Authorization": "Bearer secret"}

    with client:
        response = client.post("/backfill", headers=headers, json={"guild_id": 1})
        assert response.status_code == 200
        job_id = response.json()["job_id"]

        for _ in range(20):
            status = client.get(f"/backfill/{job_id}", headers=headers)
            if status.json()["status"] != "running":
                break
            time.sleep(0.01)

    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    assert status.json()["messages_indexed"] == 2
    assert status.json()["documents_uploaded"] == 1
    bot.corpus.finish_backfill.assert_called_once_with(1)


def test_catalog_candidate_can_be_approved_for_glossary(monkeypatch, tmp_path):
    from knowledge_catalog.store import CatalogStore

    monkeypatch.setenv("YAGAPON_API_TOKEN", "secret")
    client, bot = make_client()
    client.app.state.catalog = CatalogStore(tmp_path / "catalog.json")
    headers = {"Authorization": "Bearer secret"}

    created = client.post(
        "/admin/catalog",
        headers=headers,
        json={
            "guild_id": 1,
            "kind": "term",
            "label": "やがサポ",
            "description": "矢上祭の運営用アプリ",
            "aliases": ["Yaga Support"],
        },
    )
    assert created.status_code == 201

    entry = created.json()
    approved = client.patch(
        f"/admin/catalog/{entry['id']}?guild_id=1",
        headers=headers,
        json={"expected_revision": 1, "status": "approved"},
    )

    assert approved.status_code == 200
    assert bot.config.glossary["やがサポ"]["definition"] == "矢上祭の運営用アプリ"
    listed = client.get("/admin/catalog?guild_id=1&status=approved", headers=headers)
    assert listed.json()["total"] == 1


def test_rag_quality_dashboard_api_requires_token(monkeypatch):
    monkeypatch.setenv("YAGAPON_API_TOKEN", "secret")
    client, bot = make_client()

    assert client.get("/admin/rag/summary?guild_id=1").status_code == 401
    response = client.get(
        "/admin/rag/summary?guild_id=1",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json() == {"queries": 0}
    bot.corpus.rag_store.summary.assert_called_once_with(1)


def test_rag_dashboard_html_contains_no_embedded_token():
    client, _ = make_client()

    response = client.get("/admin/rag-ui")

    assert response.status_code == 200
    assert "API token（保存しません）" in response.text
    assert "localStorage" not in response.text
