from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot.commands.committee_server import (
    BUREAU_ROLES,
    GRADE_ROLES,
    POSITION_ROLES,
    SelfRolePanelView,
    _embed_field_id,
    _replace_role_group,
)


def test_role_groups_have_unique_names():
    names = [item[0] for item in BUREAU_ROLES] + list(GRADE_ROLES) + list(POSITION_ROLES)
    assert len(names) == len(set(names))


@pytest.mark.asyncio
async def test_persistent_role_panel_has_stable_custom_ids():
    view = SelfRolePanelView()
    assert view.timeout is None
    assert {item.custom_id for item in view.children} == {
        "yagapon:committee:bureau:v1",
        "yagapon:committee:grade:v1",
        "yagapon:committee:position:v1",
    }


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
