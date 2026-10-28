from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.client import (
    YagaPon,
    _clean_rag_query,
    _decorate_rag_answer,
    _discord_channel_lineage,
    _guild_yagapon_emojis,
    _is_ignored_with_parent,
    _is_rag_invocation,
    _is_voice_reference_channel,
    _mentioned_bot_role_ids,
    _rag_confidence_line,
    _rag_visibility_scope,
    _split_discord_text,
)


class FakeEmoji:
    def __init__(self, name: str, emoji_id: int, *, available: bool = True, usable: bool = True):
        self.name = name
        self.id = emoji_id
        self.available = available
        self._usable = usable

    def __str__(self):
        return f"<:{self.name}:{self.id}>"

    def is_usable(self):
        return self._usable


def test_clean_rag_query_removes_mention_variants_and_literal_silent_marker():
    assert _clean_rag_query("@silent <@123> 27th の整理券は？", 123) == "27th の整理券は？"
    assert _clean_rag_query("<@!123> ITラボについて", 123) == "ITラボについて"


def test_clean_rag_query_removes_bot_role_mention():
    assert _clean_rag_query("@silent <@&456> Driveの保存先は？", 123, (456,)) == "Driveの保存先は？"


def test_clean_rag_query_preserves_numbered_multiline_structure():
    content = "<@123> 次を詳しく教えてください。\n\n1. 権限\n2. 運用上の問題\n@silent"

    assert _clean_rag_query(content, 123) == (
        "次を詳しく教えてください。\n\n1. 権限\n2. 運用上の問題"
    )


def test_same_name_role_assigned_to_bot_is_an_invocation():
    role = SimpleNamespace(id=456, name="おしゃべりやがぽん")
    bot_member = SimpleNamespace(display_name="おしゃべりやがぽん", roles=[role])
    message = SimpleNamespace(
        guild=SimpleNamespace(me=bot_member),
        role_mentions=[role],
    )
    bot_user = SimpleNamespace(
        name="おしゃべりやがぽん",
        display_name="おしゃべりやがぽん",
        mentioned_in=lambda _message: False,
    )

    assert _mentioned_bot_role_ids(message, bot_user) == (456,)
    assert _is_rag_invocation(message, bot_user) is True


def test_unassigned_or_differently_named_role_does_not_invoke_bot():
    assigned_role = SimpleNamespace(id=456, name="bot")
    mentioned_role = SimpleNamespace(id=789, name="おしゃべりやがぽん")
    bot_member = SimpleNamespace(
        display_name="おしゃべりやがぽん",
        roles=[assigned_role],
    )
    message = SimpleNamespace(
        guild=SimpleNamespace(me=bot_member),
        role_mentions=[mentioned_role],
    )
    bot_user = SimpleNamespace(
        name="おしゃべりやがぽん",
        display_name="おしゃべりやがぽん",
        mentioned_in=lambda _message: False,
    )

    assert _mentioned_bot_role_ids(message, bot_user) == ()
    assert _is_rag_invocation(message, bot_user) is False


def test_voice_reference_collection_requires_active_vc_identity():
    category = SimpleNamespace(id=50)
    voice_channel = SimpleNamespace(id=10, name="会議用", category=category)

    assert _is_voice_reference_channel(
        SimpleNamespace(id=10, name="会議用", category=category),
        voice_channel,
    )
    assert _is_voice_reference_channel(
        SimpleNamespace(id=11, parent_id=10, name="資料スレッド"),
        voice_channel,
    )

    # Names and categories are presentation details, not channel identity.
    assert not _is_voice_reference_channel(
        SimpleNamespace(id=20, name="会議用", category=category),
        voice_channel,
    )
    assert not _is_voice_reference_channel(
        SimpleNamespace(id=21, name="別チャンネル", category=category),
        voice_channel,
    )


def test_ignore_is_inherited_by_threads():
    config = SimpleNamespace(is_ignored=lambda guild_id, channel_id: (guild_id, channel_id) == (1, 10))

    assert _is_ignored_with_parent(
        config,
        1,
        SimpleNamespace(id=11, parent_id=10),
    )
    assert not _is_ignored_with_parent(
        config,
        1,
        SimpleNamespace(id=12, parent_id=20),
    )


def test_discord_channel_lineage_resolves_thread_parent_and_category():
    category = SimpleNamespace(id=30, name="03会議")
    parent = SimpleNamespace(id=20, name="タスク・議題", category=category, category_id=30)
    thread = SimpleNamespace(
        id=10,
        name="新歓Web",
        parent=parent,
        parent_id=20,
        category=None,
        category_id=None,
    )

    assert _discord_channel_lineage(thread) == {
        "parent_id": 20,
        "parent_name": "タスク・議題",
        "category_id": 30,
        "category_name": "03会議",
    }


def test_guild_yagapon_emojis_only_returns_available_usable_custom_emojis():
    guild = SimpleNamespace(
        emojis=[
            FakeEmoji("yagapon_happy", 1),
            FakeEmoji("yagapon_cry", 2, available=False),
            FakeEmoji("yagapon_bow", 3, usable=False),
            FakeEmoji("other", 4),
        ]
    )

    assert _guild_yagapon_emojis(guild) == {"yagapon_happy": "<:yagapon_happy:1>"}


def test_decorate_rag_answer_uses_happy_and_bow_for_supported_answer():
    guild = SimpleNamespace(emojis=[FakeEmoji("yagapon_happy", 1), FakeEmoji("yagapon_bow", 2)])

    decorated = _decorate_rag_answer("回答だぽん。", guild, no_answer=False)

    assert decorated.startswith("<:yagapon_happy:1> 回答だぽん。")
    assert decorated.endswith("<:yagapon_bow:2>")


def test_decorate_rag_answer_uses_cry_for_missing_knowledge():
    guild = SimpleNamespace(emojis=[FakeEmoji("yagapon_happy", 1), FakeEmoji("yagapon_cry", 2)])

    decorated = _decorate_rag_answer("見つからなかったぽん。", guild, no_answer=True)

    assert decorated.startswith("<:yagapon_cry:2> 見つからなかったぽん。")


def test_rag_confidence_line_explains_server_side_search_audit():
    assert _rag_confidence_line(82, "高", "根拠3件 / 2系統 / 監査済み") == (
        "**根拠信頼度（検索・監査）: 82/100（高）** — 根拠3件 / 2系統 / 監査済み"
    )
    assert _rag_confidence_line(None, None, None) == ""


def test_admin_only_channel_uses_all_admin_readable_channels_for_rag():
    administrator = SimpleNamespace(
        id=10,
        bot=False,
        guild_permissions=SimpleNamespace(administrator=True),
    )
    bot_member = SimpleNamespace(id=20, bot=True)

    def channel(channel_id, members):
        return SimpleNamespace(
            id=channel_id,
            guild=None,
            members=members,
            permissions_for=lambda _member: SimpleNamespace(
                view_channel=True,
                read_message_history=True,
            ),
        )

    destination = channel(1, [administrator, bot_member])
    evidence_channel = channel(2, [administrator, bot_member])
    guild = SimpleNamespace(
        owner_id=999,
        chunked=True,
        channels=[destination, evidence_channel],
        threads=[],
        members=[administrator, bot_member],
        me=bot_member,
    )
    destination.guild = guild
    evidence_channel.guild = guild

    visible_channels, unrestricted = _rag_visibility_scope(
        guild,
        administrator,
        destination,
    )

    assert visible_channels == (1, 2)
    assert unrestricted is True


def test_split_discord_text_hard_splits_a_single_oversized_line_without_loss():
    text = "x" * 5000

    chunks = _split_discord_text(text)

    assert len(chunks) >= 3
    assert all(chunks)
    assert all(len(chunk) <= 1900 for chunk in chunks)
    assert "".join(chunks) == text


def test_split_discord_text_reopens_code_blocks_without_losing_code():
    text = "```python\n" + "x" * 5000 + "\n```"

    chunks = _split_discord_text(text)

    assert len(chunks) >= 3
    assert all(chunks)
    assert all(len(chunk) <= 1900 for chunk in chunks)
    assert chunks[0].startswith("```python\n")
    assert all(chunk.rstrip().endswith("```") for chunk in chunks)
    assert sum(chunk.count("x") for chunk in chunks) == 5000


def test_split_discord_text_never_splits_a_citation_line_that_fits():
    citation = "- [Firebase現在値: 公開通知](https://console.firebase.google.com/project/example/firestore/databases/-default-/data/festival~2F27~2Fnotifications)\n"
    text = ("説明だぽん。\n" * 180) + citation

    chunks = _split_discord_text(text)

    assert sum(citation in chunk for chunk in chunks) == 1
    assert "".join(chunks) == text


@pytest.mark.asyncio
async def test_send_split_message_never_sends_empty_payload_for_long_line():
    destination = SimpleNamespace(send=AsyncMock(side_effect=lambda value, **_: value))

    sent = await YagaPon.send_split_message(None, destination, "z" * 5000)

    assert sent
    assert all(call.args[0] for call in destination.send.await_args_list)
    assert sum(call.args[0].count("z") for call in destination.send.await_args_list) == 5000
    assert all(call.kwargs["suppress_embeds"] is True for call in destination.send.await_args_list)


@pytest.mark.asyncio
async def test_send_split_message_uses_one_complete_markdown_attachment_for_very_long_answer():
    destination = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=1)))
    answer = "長い回答だぽん。\n" + "根拠の詳細\n" * 2500
    text = f"{answer}\n\n回答の品質を評価してください：✅ 正しい / ⚠️ 一部不正確 / ❌ 誤り（対応した絵文字でリアクション）"

    sent = await YagaPon.send_split_message(None, destination, text)

    assert len(sent) == 1
    destination.send.assert_awaited_once()
    call = destination.send.await_args
    assert "Markdownファイル" in call.args[0]
    attachment = call.kwargs["file"]
    attachment.fp.seek(0)
    uploaded = attachment.fp.read().decode("utf-8")
    assert uploaded == answer
    assert attachment.filename == "yagapon-answer.md"
    assert call.kwargs["suppress_embeds"] is True
