from unittest.mock import AsyncMock

import pytest

from bot.support_directory import (
    COMMITTEE_ROLE,
    SupportDirectory,
    _document_fields,
    _grade_matches,
)


def _user_document(*, roles=()):
    return {
        "name": "projects/yagamy26th/databases/(default)/documents/support/functions/users/uid-1",
        "fields": {
            "uid": {"stringValue": "uid-1"},
            "email": {"stringValue": "member@keio.jp"},
            "departments": {
                "arrayValue": {"values": [{"stringValue": "IT局"}]}
            },
            "roles": {
                "arrayValue": {
                    "values": [{"stringValue": role} for role in roles]
                }
            },
        },
    }


def test_firestore_document_fields_decode_arrays():
    fields = _document_fields(_user_document(roles=(COMMITTEE_ROLE,)))

    assert fields["uid"] == "uid-1"
    assert fields["departments"] == ("IT局",)
    assert fields["roles"] == (COMMITTEE_ROLE,)


def test_grade_matching_accepts_staff_info_variants():
    assert _grade_matches("1年", "1")
    assert _grade_matches("2年", "B2")
    assert _grade_matches("3年", "3年")
    assert not _grade_matches("1年", "B2")
    assert not _grade_matches("1年", None)


@pytest.mark.asyncio
async def test_disabled_directory_never_uses_credentials(monkeypatch):
    monkeypatch.delenv("YAGAPON_SUPPORT_FIREBASE_PROJECT", raising=False)
    directory = SupportDirectory()

    identity = await directory.verify("member@keio.jp", "IT局", "2年")

    assert identity.status == "disabled"


@pytest.mark.asyncio
async def test_verified_profile_links_uid_without_writing_by_default(monkeypatch):
    monkeypatch.setenv("YAGAPON_SUPPORT_FIREBASE_PROJECT", "yagamy26th")
    monkeypatch.setenv("YAGAPON_SUPPORT_ROLE_SYNC_ENABLED", "false")
    directory = SupportDirectory()
    directory._find_user = AsyncMock(return_value=_user_document(roles=(COMMITTEE_ROLE,)))
    directory._find_grade = AsyncMock(return_value="B2")
    directory._sync_committee_role = AsyncMock()

    identity = await directory.verify("member@keio.jp", "IT局", "2年")

    assert identity.status == "directory_match"
    assert identity.uid == "uid-1"
    assert identity.role_synced is True
    directory._sync_committee_role.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_sync_requires_trusted_profile_match_and_feature_flag(monkeypatch):
    monkeypatch.setenv("YAGAPON_SUPPORT_FIREBASE_PROJECT", "yagamy26th")
    monkeypatch.setenv("YAGAPON_SUPPORT_ROLE_SYNC_ENABLED", "true")
    directory = SupportDirectory()
    directory._find_user = AsyncMock(return_value=_user_document())
    directory._find_grade = AsyncMock(return_value="2")
    directory._sync_committee_role = AsyncMock()

    identity = await directory.approve_link("member@keio.jp", "IT局", "2年", "uid-1")

    assert identity.status == "verified"
    assert identity.role_synced is True
    directory._sync_committee_role.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_mismatch_never_syncs_role(monkeypatch):
    monkeypatch.setenv("YAGAPON_SUPPORT_FIREBASE_PROJECT", "yagamy26th")
    monkeypatch.setenv("YAGAPON_SUPPORT_ROLE_SYNC_ENABLED", "true")
    directory = SupportDirectory()
    directory._find_user = AsyncMock(return_value=_user_document())
    directory._find_grade = AsyncMock(return_value="1")
    directory._sync_committee_role = AsyncMock()

    identity = await directory.verify("member@keio.jp", "IT局", "2年")

    assert identity.status == "profile_mismatch"
    directory._sync_committee_role.assert_not_awaited()
