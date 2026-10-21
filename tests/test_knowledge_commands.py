from types import SimpleNamespace

from bot.commands.knowledge import _knowledge_guild_allowed, _protected_channel


def test_knowledge_guild_allowlist_uses_explicit_list(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", "1, 2")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GUILD_ID", "3")

    assert _knowledge_guild_allowed(1) is True
    assert _knowledge_guild_allowed(3) is False


def test_knowledge_guild_allowlist_falls_back_to_index_guild(monkeypatch):
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS", raising=False)
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GUILD_ID", "3")

    assert _knowledge_guild_allowed(3) is True
    assert _knowledge_guild_allowed(4) is False


def test_protected_channel_matches_id_parent_and_default_term(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS", "20")
    monkeypatch.delenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", raising=False)
    parent = SimpleNamespace(id=20, name="一般")
    channel = SimpleNamespace(id=21, name="会話", parent=parent)
    assert _protected_channel(channel) is True

    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS", "")
    channel = SimpleNamespace(id=21, name="IT局員選考", parent=None)
    assert _protected_channel(channel) is True


def test_protected_channel_matches_category_for_text_and_thread(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS", "30")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", "選考")
    category = SimpleNamespace(id=30, name="IT局員選考")
    text_channel = SimpleNamespace(id=31, name="応募者対応", parent=category, category=category)
    thread = SimpleNamespace(id=32, name="応募者A", parent=text_channel, category=None)

    assert _protected_channel(text_channel) is True
    assert _protected_channel(thread) is True


def test_protected_channel_allows_unmatched_channel(monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS", "20")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS", "選考,人事")
    channel = SimpleNamespace(id=21, name="開発全般", parent=None)

    assert _protected_channel(channel) is False
