from types import SimpleNamespace

from bot.client import (
    _clean_rag_query,
    _decorate_rag_answer,
    _guild_yagapon_emojis,
    _is_rag_invocation,
    _mentioned_bot_role_ids,
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
    assert (
        _clean_rag_query("@silent <@&456> Driveの保存先は？", 123, (456,))
        == "Driveの保存先は？"
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
    guild = SimpleNamespace(
        emojis=[FakeEmoji("yagapon_happy", 1), FakeEmoji("yagapon_bow", 2)]
    )

    decorated = _decorate_rag_answer("回答だぽん。", guild, no_answer=False)

    assert decorated.startswith("<:yagapon_happy:1> 回答だぽん。")
    assert decorated.endswith("<:yagapon_bow:2>")


def test_decorate_rag_answer_uses_cry_for_missing_knowledge():
    guild = SimpleNamespace(
        emojis=[FakeEmoji("yagapon_happy", 1), FakeEmoji("yagapon_cry", 2)]
    )

    decorated = _decorate_rag_answer("見つからなかったぽん。", guild, no_answer=True)

    assert decorated.startswith("<:yagapon_cry:2> 見つからなかったぽん。")
