from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot.commands.committee_server import (
    BUREAU_ROLES,
    GRADE_ROLES,
    LEGACY_GRADE_ROLES,
    LIAISON_CHANNELS,
    OFFICER_POSITIONS,
    POSITION_ROLES,
    SERVER_ADMIN_ROLE,
    CommitteeGuideView,
    PostRegistrationRoleView,
    ProjectOrganizationSelect,
    RegistrationModal,
    SelfRolePanelView,
    _bot_can_run_committee_setup,
    _bot_guide_embed,
    _committee_email_verification_enabled,
    _embed_field_id,
    _ensure_category,
    _ensure_forum,
    _ensure_setup_actor_is_server_admin,
    _ensure_text,
    _ensure_voice,
    _handle_position_request,
    _has_completed_registration,
    _is_keio_email,
    _migrate_legacy_grade_roles,
    _overwrite_signature,
    _position_role_names,
    _position_role_summary,
    _post_once,
    _queue_local_role_for_firebase,
    _replace_role_group,
    _role_panel_embed,
    _roles_after_position_approval,
    _sync_pending_local_roles,
    _sync_role_decision,
)


def test_role_groups_have_unique_names():
    names = [item[0] for item in BUREAU_ROLES] + list(GRADE_ROLES) + list(POSITION_ROLES)
    assert len(names) == len(set(names))


def test_registration_roles_use_only_bureau_grade_and_declared_positions():
    assert GRADE_ROLES == ("１年生", "２年生", "３年生")
    assert "一般委員" not in POSITION_ROLES
    assert "28th委員" not in POSITION_ROLES
    assert SERVER_ADMIN_ROLE == "サーバー管理者"


def test_all_28_liaison_channels_are_declared_once():
    bureau_names = {item[0] for item in BUREAU_ROLES}
    channel_names = []
    for first_bureau, second_bureau, channel_name in LIAISON_CHANNELS:
        assert {first_bureau, second_bureau} <= bureau_names
        assert first_bureau != second_bureau
        channel_names.append(channel_name)
    assert len(channel_names) == len(set(channel_names)) == 28


def test_email_verification_switch_is_fail_safe(monkeypatch):
    monkeypatch.delenv("YAGAPON_COMMITTEE_EMAIL_VERIFICATION_ENABLED", raising=False)
    assert _committee_email_verification_enabled() is True

    for value in ("false", "0", "off", "NO"):
        monkeypatch.setenv("YAGAPON_COMMITTEE_EMAIL_VERIFICATION_ENABLED", value)
        assert _committee_email_verification_enabled() is False


def test_role_panel_explains_temporary_email_input_only_mode(monkeypatch):
    monkeypatch.setenv("YAGAPON_COMMITTEE_EMAIL_VERIFICATION_ENABLED", "false")

    embed = _role_panel_embed()

    assert "慶應メールを入力（確認コードなし）" in embed.description
    assert embed.fields[0].value == "**所属局・学年 → 慶應メール**"


def test_overwrite_signature_compares_ids_and_permission_bits():
    expected = {
        discord.Object(id=10): discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
        ),
    }
    actual = {
        discord.Object(id=10): discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
        ),
    }
    assert _overwrite_signature(actual) == _overwrite_signature(expected)


def test_registration_accepts_explicit_local_completion_and_legacy_email_record():
    member = SimpleNamespace(id=20)
    records = {
        "local": {
            "bureau": "IT局",
            "grade": "１年生",
            "email": "local@keio.jp",
            "registration_completed": True,
            "email_verified": False,
        },
        "email": {
            "bureau": "IT局",
            "grade": "２年生",
            "email_verified": True,
            "email": "member@keio.jp",
        },
    }
    config = SimpleNamespace(get_member=lambda _guild_id, _member_id: records["local"])
    assert _has_completed_registration(config, 10, member) is True

    config.get_member = lambda _guild_id, _member_id: {
        "bureau": "IT局",
        "grade": "１年生",
        "registration_completed": True,
        "email_verified": False,
    }
    assert _has_completed_registration(config, 10, member) is False

    config.get_member = lambda _guild_id, _member_id: records["email"]
    assert _has_completed_registration(config, 10, member) is True

    config.get_member = lambda _guild_id, _member_id: {
        "bureau": "IT局",
        "grade": "１年生",
        "email_verified": False,
    }
    assert _has_completed_registration(config, 10, member) is False


def test_local_role_queue_is_idempotent():
    record = {}

    for _ in range(2):
        _queue_local_role_for_firebase(
            record,
            application_id="local-1",
            role_name="企画長",
            organization_name="ITラボ",
            approved_by=99,
        )

    assert record["firebase_role_sync_queue"] == [
        {
            "local_application_id": "local-1",
            "role": "企画長",
            "kind": "project_leader",
            "organization_name": "ITラボ",
            "organization_not_listed": True,
            "approved_by_discord_id": "99",
            "status": "pending_registration",
        }
    ]


@pytest.mark.asyncio
async def test_verified_member_replays_local_role_to_firebase():
    record = {
        "email_verified": True,
        "firebase_role_sync_queue": [
            {
                "local_application_id": "local-1",
                "role": "局長",
                "kind": "officer",
                "organization_name": None,
                "organization_not_listed": False,
                "approved_by_discord_id": "99",
                "status": "pending_registration",
            }
        ],
    }
    members = {"20": record}
    backend = SimpleNamespace(
        create_application=AsyncMock(return_value={"applicationId": "firebase-1"}),
        decide_application=AsyncMock(),
        acknowledge_decision=AsyncMock(),
    )
    config = SimpleNamespace(
        get_members=lambda _guild_id: members,
        set_members=AsyncMock(),
    )
    bot = SimpleNamespace(committee_backend=backend, config=config)
    guild = SimpleNamespace(id=10)
    member = SimpleNamespace(id=20, display_name="委員")

    synced = await _sync_pending_local_roles(bot, guild, member)

    assert synced == 1
    backend.create_application.assert_awaited_once_with(
        guildId="10",
        discordUserId="20",
        applicantDisplayName="委員",
        kind="officer",
        role="局長",
        organizationId=None,
        organizationNotListed=False,
        manualReason=None,
    )
    backend.decide_application.assert_awaited_once_with(
        application_id="firebase-1",
        approved=True,
        actor_discord_id=99,
    )
    backend.acknowledge_decision.assert_awaited_once_with(
        guild_id=10,
        application_id="firebase-1",
    )
    assert record["firebase_role_sync_queue"][0]["status"] == "synced"


def test_committee_setup_requires_bot_administrator_permission():
    administrator = SimpleNamespace(
        guild_permissions=SimpleNamespace(administrator=True),
    )
    role_manager_only = SimpleNamespace(
        guild_permissions=SimpleNamespace(administrator=False, manage_roles=True),
    )

    assert _bot_can_run_committee_setup(administrator) is True
    assert _bot_can_run_committee_setup(role_manager_only) is False
    assert _bot_can_run_committee_setup(None) is False


@pytest.mark.asyncio
async def test_setup_actor_receives_explicit_administrator_role():
    role = SimpleNamespace(
        id=30,
        name=SERVER_ADMIN_ROLE,
        permissions=discord.Permissions.none(),
        edit=AsyncMock(),
    )
    guild = SimpleNamespace(roles=[role], create_role=AsyncMock())
    member = SimpleNamespace(roles=[], add_roles=AsyncMock())

    selected, created = await _ensure_setup_actor_is_server_admin(guild, member)

    assert selected is role
    assert created is False
    updated_permissions = role.edit.await_args.kwargs["permissions"]
    assert updated_permissions.administrator is True
    member.add_roles.assert_awaited_once_with(
        role,
        reason="/committee_setup 実行者をサーバー管理者として登録",
    )


@pytest.mark.asyncio
async def test_persistent_role_panel_has_stable_custom_ids():
    view = SelfRolePanelView()
    assert view.timeout is None
    assert {item.custom_id for item in view.children} == {
        "yagapon:committee:bureau:v1",
        "yagapon:committee:grade:v1",
        "yagapon:committee:registration:v1",
    }


@pytest.mark.asyncio
async def test_post_registration_role_view_separates_project_leader_and_officer_claims():
    view = PostRegistrationRoleView()

    assert {item.custom_id for item in view.children} == {
        "yagapon:committee:project-leader:v1",
        "yagapon:committee:officer:v1",
    }
    officer_select = next(
        item for item in view.children if item.custom_id == "yagapon:committee:officer:v1"
    )
    assert tuple(option.value for option in officer_select.options) == OFFICER_POSITIONS


def test_project_leader_approval_preserves_existing_officer_roles():
    project_leader = SimpleNamespace(id=1, name="企画長")
    officer = SimpleNamespace(id=2, name="役員")
    director = SimpleNamespace(id=3, name="局長")
    general = SimpleNamespace(id=4, name="一般委員")

    roles = _roles_after_position_approval(
        [general, officer, director],
        project_leader,
    )

    assert {role.name for role in roles} == {"企画長", "役員", "局長"}


def test_officer_approval_preserves_project_leader_and_replaces_officer_title():
    project_leader = SimpleNamespace(id=1, name="企画長")
    officer = SimpleNamespace(id=2, name="役員")
    deputy = SimpleNamespace(id=3, name="副局長")
    director = SimpleNamespace(id=4, name="局長")

    roles = _roles_after_position_approval(
        [project_leader, officer, deputy],
        director,
        officer,
    )

    assert {role.name for role in roles} == {"企画長", "役員", "局長"}


def test_position_role_summary_is_stable_for_combined_roles_and_empty_input():
    roles = [
        SimpleNamespace(name="企画長"),
        SimpleNamespace(name="IT局"),
        SimpleNamespace(name="役員"),
        SimpleNamespace(name="局長"),
    ]

    assert _position_role_names(roles) == ["局長", "企画長", "役員"]
    assert _position_role_summary(roles) == "局長, 企画長, 役員"
    assert _position_role_names([]) == []
    assert _position_role_summary([]) == "なし"


def test_project_organizations_are_sorted_before_discord_option_limit():
    select = ProjectOrganizationSelect(
        [
            {"id": "z", "name": "屋外企画"},
            {"id": "a", "name": "ITラボ"},
        ]
    )

    assert [option.label for option in select.options] == [
        "ITラボ",
        "屋外企画",
        "この中にない",
    ]


@pytest.mark.asyncio
async def test_registration_modal_limits_email_length():
    modal = RegistrationModal()
    assert modal.custom_id == "yagapon:committee:registration:modal:v1"
    assert modal.children[0].min_length == 6
    assert modal.children[0].max_length == 254


def test_keio_email_validation_accepts_subdomains_only():
    assert _is_keio_email("student@keio.jp")
    assert _is_keio_email("student@a2.keio.jp")
    assert _is_keio_email(" Student@KEIO.JP ")
    assert not _is_keio_email("student@gmail.com")
    assert not _is_keio_email("student@fakekeio.jp")


@pytest.mark.asyncio
async def test_bot_guide_embed_and_links_use_created_channels():
    channels = [
        SimpleNamespace(name="ロール選択", id=1, mention="<#1>"),
        SimpleNamespace(name="おしゃべりやがぽん", id=2, mention="<#2>"),
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
    category = SimpleNamespace(id=1)
    guild = SimpleNamespace(
        categories=[],
        text_channels=[],
        forum_channels=[],
        voice_channels=[],
        create_category=AsyncMock(return_value=category),
        create_text_channel=AsyncMock(return_value=SimpleNamespace()),
        create_forum_channel=AsyncMock(return_value=SimpleNamespace()),
        create_voice_channel=AsyncMock(return_value=SimpleNamespace()),
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
async def test_legacy_grade_roles_are_migrated_then_deleted():
    desired = SimpleNamespace(id=2, name="１年生")
    unrelated = SimpleNamespace(id=3, name="IT局")
    member = SimpleNamespace(roles=[], edit=AsyncMock())
    legacy = SimpleNamespace(id=1, name="1年", members=[member], delete=AsyncMock())
    member.roles = [legacy, unrelated]
    guild = SimpleNamespace(roles=[legacy, desired, unrelated])

    migrated, deleted = await _migrate_legacy_grade_roles(guild)

    assert LEGACY_GRADE_ROLES["1年"] == "１年生"
    assert (migrated, deleted) == (1, 1)
    member.edit.assert_awaited_once_with(
        roles=[unrelated, desired],
        reason="学年ロールを １年生 へ移行",
    )
    legacy.delete.assert_awaited_once_with(reason="１年生 への移行完了")


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


@pytest.mark.asyncio
async def test_sync_acknowledges_decision_for_member_who_left_guild():
    response = SimpleNamespace(status=404, reason="Not Found")
    guild = SimpleNamespace(
        id=10,
        get_member=lambda _member_id: None,
        fetch_member=AsyncMock(side_effect=discord.NotFound(response, "missing")),
    )
    backend = SimpleNamespace(acknowledge_decision=AsyncMock())
    bot = SimpleNamespace(committee_backend=backend)

    await _sync_role_decision(
        bot,
        guild,
        {
            "id": "application-1",
            "discordUserId": "20",
            "status": "approved",
            "role": "企画長",
        },
    )

    backend.acknowledge_decision.assert_awaited_once_with(
        guild_id=10,
        application_id="application-1",
    )


@pytest.mark.asyncio
async def test_officer_approval_requires_officer_role_before_backend_decision():
    applicant = SimpleNamespace(id=20, roles=[])
    director = SimpleNamespace(id=30, name="局長")
    guild = SimpleNamespace(
        id=10,
        roles=[director],
        get_member=lambda member_id: applicant if member_id == 20 else None,
        get_role=lambda role_id: director if role_id == 30 else None,
    )
    embed = discord.Embed(title="役職申請")
    embed.add_field(name="申請者ID", value="20")
    embed.add_field(name="役職ロールID", value="30")
    embed.add_field(name="申請ID", value="application-1")
    backend = SimpleNamespace(decide_application=AsyncMock())
    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=99,
            guild_permissions=SimpleNamespace(manage_roles=True),
        ),
        guild=guild,
        message=SimpleNamespace(embeds=[embed]),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
        client=SimpleNamespace(committee_backend=backend),
    )

    await _handle_position_request(interaction, approve=True)

    backend.decide_application.assert_not_awaited()
    response.send_message.assert_awaited_once()
    assert "役員ロールが見つからない" in response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_local_position_approval_skips_backend_and_updates_discord():
    applicant = SimpleNamespace(id=20, roles=[], edit=AsyncMock(), send=AsyncMock())
    project_leader = SimpleNamespace(id=30, name="企画長")
    guild = SimpleNamespace(
        id=10,
        roles=[project_leader],
        get_member=lambda member_id: applicant if member_id == 20 else None,
        get_role=lambda role_id: project_leader if role_id == 30 else None,
    )
    embed = discord.Embed(title="企画長申請")
    embed.add_field(name="申請者ID", value="20")
    embed.add_field(name="役職ロールID", value="30")
    embed.add_field(name="申請ID", value="local-test")
    embed.add_field(name="処理方式", value="Discord内の暫定申請")
    backend = SimpleNamespace(
        decide_application=AsyncMock(),
        acknowledge_decision=AsyncMock(),
    )
    config = SimpleNamespace(
        get_members=lambda _guild_id: {},
        set_members=AsyncMock(),
    )
    message = SimpleNamespace(embeds=[embed], edit=AsyncMock())
    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=99,
            mention="<@99>",
            guild_permissions=SimpleNamespace(manage_roles=True),
        ),
        guild=guild,
        message=message,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        client=SimpleNamespace(committee_backend=backend, config=config),
    )

    await _handle_position_request(interaction, approve=True)

    backend.decide_application.assert_not_awaited()
    backend.acknowledge_decision.assert_not_awaited()
    applicant.edit.assert_awaited_once()
    config.set_members.assert_awaited_once()
    message.edit.assert_awaited_once()
    saved_record = config.set_members.await_args.args[1]["20"]
    assert saved_record["firebase_role_sync_queue"][0]["role"] == "企画長"
