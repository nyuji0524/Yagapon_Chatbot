from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.key_management import (
    KEY_NAMES,
    RESTRICTED_KEY,
    KeyManagementStore,
    KeyPanelView,
    KeyStateConflict,
    RestrictedKeyApprovalView,
    can_approve_restricted_key,
    can_use_restricted_key,
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


@pytest.mark.asyncio
async def test_key_panel_has_all_keys_and_stable_persistent_ids():
    panel = KeyPanelView()
    assert panel.timeout is None
    select = next(child for child in panel.children if child.custom_id == "yagapon:key:select:v1")
    assert [option.value for option in select.options] == list(KEY_NAMES)
    assert {child.custom_id for child in panel.children} == {
        "yagapon:key:select:v1",
        "yagapon:key:status:v1",
    }

    approval = RestrictedKeyApprovalView()
    assert approval.timeout is None
    assert {child.custom_id for child in approval.children} == {
        "yagapon:key:209:approve:v1",
        "yagapon:key:209:reject:v1",
    }


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


def test_reminders_repeat_only_after_interval(tmp_path):
    store = KeyManagementStore(tmp_path / "keys.sqlite3")
    store.apply_action(1, "地下倉庫", "borrow", 10)
    now = datetime.now(timezone.utc)

    assert store.due_reminders(now=now + timedelta(hours=9), after_hours=8, repeat_hours=12)
    store.mark_reminded(1, "地下倉庫", at=now + timedelta(hours=9))
    assert not store.due_reminders(now=now + timedelta(hours=10), after_hours=8, repeat_hours=12)
    assert store.due_reminders(now=now + timedelta(hours=22), after_hours=8, repeat_hours=12)
