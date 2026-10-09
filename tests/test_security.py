from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.security import require_api_token
from bot.authorization import can_manage_member, is_guild_admin, require_guild_admin


@pytest.mark.asyncio
async def test_api_token_fails_closed_when_not_configured(monkeypatch):
    monkeypatch.delenv("YAGAPON_API_TOKEN", raising=False)

    with pytest.raises(HTTPException) as error:
        await require_api_token("Bearer anything")

    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_api_token_requires_exact_bearer_token(monkeypatch):
    monkeypatch.setenv("YAGAPON_API_TOKEN", "correct-token")

    with pytest.raises(HTTPException) as error:
        await require_api_token("Bearer wrong-token")

    assert error.value.status_code == 401
    assert await require_api_token("Bearer correct-token") is None


class FakeContext:
    def __init__(self, *, administrator: bool):
        self.guild = object()
        self.author = SimpleNamespace(
            id=10,
            guild_permissions=SimpleNamespace(administrator=administrator),
        )
        self.responses = []

    async def respond(self, message, **kwargs):
        self.responses.append((message, kwargs))


@pytest.mark.asyncio
async def test_runtime_admin_check_rejects_non_admin():
    context = FakeContext(administrator=False)

    assert is_guild_admin(context) is False
    assert await require_guild_admin(context) is False
    assert context.responses[0][1]["ephemeral"] is True


def test_member_can_manage_self_but_not_another_member():
    context = FakeContext(administrator=False)

    assert can_manage_member(context, SimpleNamespace(id=10)) is True
    assert can_manage_member(context, SimpleNamespace(id=11)) is False


def test_admin_can_manage_another_member():
    context = FakeContext(administrator=True)

    assert can_manage_member(context, SimpleNamespace(id=11)) is True
