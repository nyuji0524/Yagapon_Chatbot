from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.key_management import (
    KEY_NAMES,
    RESTRICTED_KEY,
    KeyManagementManager,
    KeyManagementStore,
    KeyPanelView,
    KeyStateConflict,
    RestrictedKeyApprovalView,
    _selected_key_embed,
    can_approve_restricted_key,
    can_use_restricted_key,
    committee_bureau,
    key_panel_embed,
)


def _member(*role_names: str, administrator: bool = False):
    return SimpleNamespace(
        roles=[SimpleNamespace(name=name) for name in role_names],
        guild_permissions=SimpleNamespace(administrator=administrator),
    )


def test_restricted_key_uses_profile_roles_and_records_approver_roles():
    assert can_use_restricted_key(_member("渉外局"))
    assert can_use_restricted_key(_member("執行部"))
    assert can_use_restricted_key(_member(administrator=True))
    assert not can_use_restricted_key(_member("IT局"))

    assert can_approve_restricted_key(_member("渉外局"))
    assert can_approve_restricted_key(_member("執行部"))
    assert not can_approve_restricted_key(_member("一般委員"))
    user_without_roles = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False))
    assert not can_use_restricted_key(user_without_roles)


def test_committee_bureau_requires_exactly_one_bureau_role():
    assert committee_bureau(_member("IT局", "２年生")) == "IT局"
    assert committee_bureau(_member("IT局", "総務局")) is None
    assert committee_bureau(_member("２年生")) is None


@pytest.mark.asyncio
async def test_key_panel_has_all_keys_and_stable_persistent_ids():
    panel = KeyPanelView()
    assert panel.timeout is None
    assert {child.custom_id for child in panel.children} == {
        "yagapon:key:select:hiyoshi:v1",
        "yagapon:key:select:yagami-212:v1",
        "yagapon:key:select:yagami-209:v1",
        "yagapon:key:select:basement:v1",
        "yagapon:key:select:outdoor:v1",
        "yagapon:key:status:v1",
    }
    assert {child.label for child in panel.children if child.row == 0} == set(KEY_NAMES)

    approval = RestrictedKeyApprovalView()
    assert approval.timeout is None
    assert {child.custom_id for child in approval.children} == {
        "yagapon:key:209:approve:v1",
        "yagapon:key:209:reject:v1",
    }


def test_restricted_key_copy_does_not_frame_the_user_as_unauthorized(tmp_path):
    panel = key_panel_embed()
    selected = _selected_key_embed(
        SimpleNamespace(id=1),
        RESTRICTED_KEY,
        KeyManagementStore(tmp_path / "keys.sqlite3"),
    )
    rendered = "\n".join(
        [panel.description or "", selected.description or ""]
        + [field.value for field in panel.fields]
        + [field.value for field in selected.fields]
    )
    assert "権限がない" not in rendered
    assert "その他の委員" not in rendered
    assert "利用制限" not in rendered


@pytest.mark.asyncio
async def test_key_report_notifications_expire_and_are_not_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KEY_NOTIFICATION_TTL_SECONDS", "90")
    manager = KeyManagementManager(
        SimpleNamespace(),
        KeyManagementStore(tmp_path / "keys.sqlite3"),
    )
    channel = SimpleNamespace(send=AsyncMock())
    embed = key_panel_embed()

    await manager._send_transient_report(channel, embed)

    channel.send.assert_awaited_once_with(embed=embed, delete_after=90)


@pytest.mark.asyncio
async def test_expired_key_notices_are_cleaned_after_restart_but_status_remains(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KEY_NOTIFICATION_TTL_SECONDS", "90")
    manager = KeyManagementManager(
        SimpleNamespace(user=SimpleNamespace(id=10)),
        KeyManagementStore(tmp_path / "keys.sqlite3"),
    )
    old_notice = SimpleNamespace(
        id=1,
        author=SimpleNamespace(id=10),
        pinned=False,
        content="",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        delete=AsyncMock(),
    )
    status = SimpleNamespace(
        id=2,
        author=SimpleNamespace(id=10),
        pinned=True,
        content="yagapon-key-status-v1",
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
        delete=AsyncMock(),
    )

    async def history(*, limit):
        assert limit == 500
        yield old_notice
        yield status

    channel = SimpleNamespace(name="報告", history=history)
    guild = SimpleNamespace(
        id=1,
        me=SimpleNamespace(id=10),
        text_channels=[channel],
    )

    await manager.cleanup_expired_notifications(guild)

    old_notice.delete.assert_awaited_once_with(reason="期限切れの鍵管理通知を整理")
    status.delete.assert_not_awaited()


def test_key_state_machine_borrow_transfer_and_return(tmp_path):
    store = KeyManagementStore(tmp_path / "keys.sqlite3")

    borrowed = store.apply_action(1, "日吉部室", "borrow", 10)
    assert borrowed.previous_holder_id is None
    assert borrowed.new_holder_id == 10
    assert store.get_holding(1, "日吉部室").holder_id == 10

    with pytest.raises(KeyStateConflict, match="ほかの人"):
        store.apply_action(1, "日吉部室", "borrow", 20)
    with pytest.raises(KeyStateConflict, match="現在の所持者"):
        store.apply_action(1, "日吉部室", "return", 20)

    transferred = store.apply_action(1, "日吉部室", "transfer", 10, target_id=20)
    assert transferred.previous_holder_id == 10
    assert transferred.new_holder_id == 20
    assert store.get_holding(1, "日吉部室").holder_id == 20

    returned = store.apply_action(1, "日吉部室", "return", 20)
    assert returned.previous_holder_id == 20
    assert returned.new_holder_id is None
    assert store.get_holding(1, "日吉部室") is None

    assert [event.id for event in store.list_unreported_events()] == [
        borrowed.id,
        transferred.id,
        returned.id,
    ]
    store.mark_event_reported(borrowed.id)
    assert borrowed.id not in {event.id for event in store.list_unreported_events()}


@pytest.mark.parametrize("key_name", ["矢上部室（212）", "矢上部室（209）"])
def test_yagami_room_key_transfer_is_limited_to_borrowing_bureau(tmp_path, key_name):
    store = KeyManagementStore(tmp_path / "keys.sqlite3")
    store.apply_action(1, key_name, "borrow", 10, actor_bureau="IT局")

    with pytest.raises(KeyStateConflict, match="借りたIT局"):
        store.apply_action(
            1,
            key_name,
            "transfer",
            10,
            target_id=20,
            actor_bureau="IT局",
            target_bureau="総務局",
        )

    transferred = store.apply_action(
        1,
        key_name,
        "transfer",
        10,
        target_id=30,
        actor_bureau="IT局",
        target_bureau="IT局",
    )
    assert transferred.new_holder_id == 30
    assert store.get_holding(1, key_name).borrowing_bureau == "IT局"


def test_non_yagami_key_transfer_keeps_cross_bureau_behavior(tmp_path):
    store = KeyManagementStore(tmp_path / "keys.sqlite3")
    store.apply_action(1, "日吉部室", "borrow", 10, actor_bureau="IT局")
    event = store.apply_action(
        1,
        "日吉部室",
        "transfer",
        10,
        target_id=20,
        actor_bureau="IT局",
        target_bureau="総務局",
    )
    assert event.new_holder_id == 20


def test_legacy_yagami_holding_adopts_current_holders_bureau(tmp_path):
    store = KeyManagementStore(tmp_path / "keys.sqlite3")
    store.apply_action(1, "矢上部室（212）", "borrow", 10)

    store.apply_action(
        1,
        "矢上部室（212）",
        "transfer",
        10,
        target_id=20,
        actor_bureau="IT局",
        target_bureau="IT局",
    )
    assert store.get_holding(1, "矢上部室（212）").borrowing_bureau == "IT局"


@pytest.mark.asyncio
async def test_key_notification_displays_borrowing_bureau(tmp_path):
    store = KeyManagementStore(tmp_path / "keys.sqlite3")
    event = store.apply_action(
        1,
        "矢上部室（212）",
        "borrow",
        10,
        actor_bureau="IT局",
    )
    channel = SimpleNamespace(name="報告", send=AsyncMock())
    member = SimpleNamespace(id=10, roles=[SimpleNamespace(name="IT局")])
    guild = SimpleNamespace(
        id=1,
        text_channels=[channel],
        get_member=lambda member_id: member if member_id == 10 else None,
    )
    manager = KeyManagementManager(SimpleNamespace(), store)

    await manager._publish_event(guild, event)

    embed = channel.send.await_args.kwargs["embed"]
    assert next(field.value for field in embed.fields if field.name == "局") == "IT局"


def test_admin_can_force_return_a_stale_record(tmp_path):
    store = KeyManagementStore(tmp_path / "keys.sqlite3")
    store.apply_action(1, "屋外倉庫", "borrow", 10)

    corrected = store.apply_action(
        1,
        "屋外倉庫",
        "return",
        99,
        reason="管理者による記録修正",
        approved_by=99,
        force=True,
    )

    assert corrected.previous_holder_id == 10
    assert corrected.approved_by == 99
    assert store.get_holding(1, "屋外倉庫") is None


def test_restricted_request_is_deduplicated_and_approved_atomically(tmp_path):
    store = KeyManagementStore(tmp_path / "keys.sqlite3")
    request, created = store.create_request(
        1,
        RESTRICTED_KEY,
        "borrow",
        10,
        target_id=None,
        reason="荷物を取り出すため",
    )
    duplicate, duplicate_created = store.create_request(
        1,
        RESTRICTED_KEY,
        "borrow",
        10,
        target_id=None,
        reason="別の文章でも同じ未処理申請",
    )
    assert created is True
    assert duplicate_created is False
    assert duplicate.id == request.id

    decided, event = store.decide_request(request.id, 99, approve=True)
    assert decided.status == "approved"
    assert decided.decided_by == 99
    assert event.approved_by == 99
    assert store.get_holding(1, RESTRICTED_KEY).holder_id == 10

    with pytest.raises(KeyStateConflict, match="処理済み"):
        store.decide_request(request.id, 98, approve=False)


def test_approval_rechecks_current_key_state(tmp_path):
    store = KeyManagementStore(tmp_path / "keys.sqlite3")
    request, _ = store.create_request(
        1,
        RESTRICTED_KEY,
        "borrow",
        10,
        target_id=None,
        reason="利用申請",
    )
    store.apply_action(1, RESTRICTED_KEY, "borrow", 20)

    with pytest.raises(KeyStateConflict, match="ほかの人"):
        store.decide_request(request.id, 99, approve=True)
    assert store.get_request(request.id).status == "pending"


def test_209_transfer_approval_rechecks_bureau_boundary(tmp_path):
    store = KeyManagementStore(tmp_path / "keys.sqlite3")
    store.apply_action(1, RESTRICTED_KEY, "borrow", 10, actor_bureau="IT局")
    request, _ = store.create_request(
        1,
        RESTRICTED_KEY,
        "transfer",
        10,
        target_id=20,
        reason="引き継ぎのため",
    )

    with pytest.raises(KeyStateConflict, match="借りたIT局"):
        store.decide_request(
            request.id,
            99,
            approve=True,
            actor_bureau="IT局",
            target_bureau="総務局",
        )
    assert store.get_request(request.id).status == "pending"


def test_reminders_repeat_only_after_interval(tmp_path):
    store = KeyManagementStore(tmp_path / "keys.sqlite3")
    store.apply_action(1, "地下倉庫", "borrow", 10)
    now = datetime.now(timezone.utc)

    assert store.due_reminders(now=now + timedelta(hours=9), after_hours=8, repeat_hours=12)
    store.mark_reminded(1, "地下倉庫", at=now + timedelta(hours=9))
    assert not store.due_reminders(now=now + timedelta(hours=10), after_hours=8, repeat_hours=12)
    assert store.due_reminders(now=now + timedelta(hours=22), after_hours=8, repeat_hours=12)


def test_status_message_id_is_persisted(tmp_path):
    store = KeyManagementStore(tmp_path / "keys.sqlite3")
    assert store.get_status_message_id(1) is None
    store.set_status_message_id(1, 123)
    assert store.get_status_message_id(1) == 123
    store.set_status_message_id(1, 456)
    assert store.get_status_message_id(1) == 456
