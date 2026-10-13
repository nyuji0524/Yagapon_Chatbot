import os

import pytest

from bot.support_directory import COMMITTEE_ROLE, SupportDirectory
from bot.support_emulator_seed import seed

pytestmark = pytest.mark.skipif(
    os.environ.get("YAGAPON_SUPPORT_BACKEND") != "emulator",
    reason="Firestore Emulator integration test",
)


@pytest.mark.asyncio
async def test_emulator_lookup_approval_and_role_sync(monkeypatch):
    host = os.environ.get("YAGAPON_SUPPORT_FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
    project = os.environ.get("YAGAPON_SUPPORT_FIREBASE_PROJECT", "demo-yagapon-support")
    email = "emulator-member@keio.jp"
    uid = "emulator-support-uid"
    await seed(
        host=host,
        project=project,
        email=email,
        uid=uid,
        bureau="IT局",
        grade="2",
    )
    monkeypatch.setenv("YAGAPON_SUPPORT_ROLE_SYNC_ENABLED", "true")
    directory = SupportDirectory()

    candidate = await directory.verify(email, "IT局", "2年")
    approved = await directory.approve_link(email, "IT局", "2年", uid)
    persisted = await directory.verify(email, "IT局", "2年")

    assert candidate.status == "directory_match"
    assert candidate.uid == uid
    assert candidate.role_synced is False
    assert approved.status == "verified"
    assert approved.role_synced is True
    assert COMMITTEE_ROLE in persisted.roles
