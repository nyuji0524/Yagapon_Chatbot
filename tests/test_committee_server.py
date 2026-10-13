from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot.commands.committee_server import (
    BUREAU_ROLES,
    GRADE_ROLES,
    LIAISON_CHANNELS,
    POSITION_ROLES,
    CommitteeGuideView,
    NicknameModal,
    SelfRolePanelView,
    _bot_guide_embed,
    _embed_field_id,
    _ensure_category,
    _ensure_forum,
    _ensure_text,
    _ensure_voice,
    _liaison_guide_embed,
    _nickname_block_reason,
    _post_once,
    _replace_role_group,
)


def test_role_groups_have_unique_names():
    names = [item[0] for item in BUREAU_ROLES] + list(GRADE_ROLES) + list(POSITION_ROLES)
    assert len(names) == len(set(names))


def test_liaison_channels_reference_known_bureaus_and_have_unique_names():
    bureau_names = {item[0] for item in BUREAU_ROLES}
    channel_names = []
    for first_bureau, second_bureau, channel_name in LIAISON_CHANNELS:
        assert first_bureau in bureau_names
        assert second_bureau in bureau_names
        assert first_bureau != second_bureau
        channel_names.append(channel_name)
    assert len(channel_names) == len(set(channel_names))


def test_liaison_guide_explains_private_scope_and_pinned_location():
    embed = _liaison_guide_embed("IT局", "総務局")
    assert "IT局・総務局・執行部" in embed.description
    assert "ほかの局からは表示されません" in embed.fields[1].value
    assert "ピン留め" in embed.footer.text


@pytest.mark.asyncio
async def test_persistent_role_panel_has_stable_custom_ids():
    view = SelfRolePanelView()
    assert view.timeout is None
    assert {item.custom_id for item in view.children} == {
        "yagapon:committee:bureau:v1",
        "yagapon:committee:grade:v1",
        "yagapon:committee:nickname:v1",
        "yagapon:committee:position:v1",
    }


@pytest.mark.asyncio
async def test_nickname_modal_limits_server_display_name_length():
    modal = NicknameModal()
    assert modal.custom_id == "yagapon:committee:nickname:modal:v1"
    assert modal.children[0].min_length == 1
    assert modal.children[0].max_length == 32


def test_nickname_permission_guard_covers_owner_permission_and_role_order():
    member = SimpleNamespace(id=1, top_role=10)
    owner_guild = SimpleNamespace(owner_id=1, me=SimpleNamespace())
    assert "サーバー所有者" in _nickname_block_reason(owner_guild, member)

    no_permission_guild = SimpleNamespace(
        owner_id=2,
        me=SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_nicknames=False),
            top_role=20,
        ),
    )
    assert "権限が足りない" in _nickname_block_reason(no_permission_guild, member)

    low_role_guild = SimpleNamespace(
        owner_id=2,
        me=SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_nicknames=True),
            top_role=10,
        ),
    )
    assert "ロール順位" in _nickname_block_reason(low_role_guild, member)

    allowed_guild = SimpleNamespace(
        owner_id=2,
        me=SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_nicknames=True),
            top_role=20,
        ),
    )
    assert _nickname_block_reason(allowed_guild, member) is None


@pytest.mark.asyncio
async def test_bot_guide_embed_and_links_use_created_channels():
    channels = [
        SimpleNamespace(name="ロール選択", id=1, mention="<#1>"),
        SimpleNamespace(name="やがぽん", id=2, mention="<#2>"),
        SimpleNamespace(name="議事録", id=3, mention="<#3>"),
    ]
    guild = SimpleNamespace(id=99, channels=channels)

    embed = _bot_guide_embed(guild)
    view = CommitteeGuideView(guild)

    assert embed.title == "🤖 おしゃべりやがぽん ガイド"
    assert "<#2>" in embed.fields[0].value
    assert [item.url for item in view.children] == [
        "https://discord.com/channels/99/1",
        "https://discord.com/channels/99/2",
        "https://discord.com/channels/99/3",
    ]


@pytest.mark.asyncio
async def test_bot_guide_view_can_be_empty_before_committee_setup():
    guild = SimpleNamespace(id=99, channels=[])
    view = CommitteeGuideView(guild)
    assert view.children == []


def test_embed_field_id_reads_known_field_and_rejects_invalid_value():
    embed = discord.Embed(title="request")
    embed.add_field(name="申請者ID", value="123")
    embed.add_field(name="役職ロールID", value="invalid")
    assert _embed_field_id(embed, "申請者ID") == 123
    assert _embed_field_id(embed, "役職ロールID") is None
    assert _embed_field_id(embed, "missing") is None


@pytest.mark.asyncio
async def test_replace_role_group_removes_old_role_and_adds_selected():
    old = SimpleNamespace(id=1, name="IT局")
    selected = SimpleNamespace(id=2, name="総務局")
    unrelated = SimpleNamespace(id=3, name="1年")
    member = SimpleNamespace(
        roles=[old, unrelated],
        edit=AsyncMock(),
    )

    await _replace_role_group(member, selected, ("IT局", "総務局"))

    member.edit.assert_awaited_once_with(roles=[unrelated, selected], reason="本人によるロール選択")


@pytest.mark.asyncio
async def test_channel_creators_omit_none_overwrites():
    category = object()
    guild = SimpleNamespace(
        categories=[],
        text_channels=[],
        forum_channels=[],
        voice_channels=[],
        create_category=AsyncMock(return_value=category),
        create_text_channel=AsyncMock(return_value=object()),
        create_forum_channel=AsyncMock(return_value=object()),
        create_voice_channel=AsyncMock(return_value=object()),
    )

    await _ensure_category(guild, "category")
    await _ensure_text(guild, "text", category, topic="topic")
    await _ensure_forum(guild, "forum", category, topic="topic", tags=("tag",))
    await _ensure_voice(guild, "voice", category)

    assert "overwrites" not in guild.create_category.await_args.kwargs
    assert "overwrites" not in guild.create_text_channel.await_args.kwargs
    assert "overwrites" not in guild.create_forum_channel.await_args.kwargs
    assert "overwrites" not in guild.create_voice_channel.await_args.kwargs


@pytest.mark.asyncio
async def test_post_once_repins_an_existing_guide():
    message = SimpleNamespace(
        id=30,
        author=SimpleNamespace(id=10),
        content="yagapon-role-panel-v1",
        pinned=False,
        edit=AsyncMock(),
        pin=AsyncMock(),
    )

    async def history(*, limit):
        assert limit == 50
        yield message

    channel = SimpleNamespace(
        id=20,
        guild=SimpleNamespace(id=1, me=SimpleNamespace(id=10)),
        history=history,
        send=AsyncMock(),
    )

    result, created = await _post_once(
        channel,
        "yagapon-role-panel-v1",
        content="updated guide",
    )

    assert result is message
    assert created is False
    message.edit.assert_awaited_once_with(content="updated guide", embed=None, view=None)
    message.pin.assert_awaited_once_with(reason="28thサーバー案内")
    channel.send.assert_not_awaited()
