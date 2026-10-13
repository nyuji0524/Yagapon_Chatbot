from types import SimpleNamespace

from bot.client import _clean_rag_query, _decorate_rag_answer, _guild_yagapon_emojis


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

