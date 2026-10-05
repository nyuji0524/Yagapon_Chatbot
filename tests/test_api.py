import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from api.server import create_app
from bot.corpus import BackfillResult


class FakeConfig:
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
    bot.corpus.query.assert_awaited_once_with(
        "質問",
        "fileSearchStores/test",
        guild_id=1,
        members_info="メンバー情報",
        glossary_text="用語集",
    )


def test_github_webhook_fails_closed_without_secret(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    client, _ = make_client()

    response = client.post(
        "/webhook/github/1",
        headers={"X-GitHub-Event": "push"},
        content=b"{}",
    )

    assert response.status_code == 503


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
