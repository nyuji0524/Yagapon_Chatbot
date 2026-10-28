from unittest.mock import AsyncMock

import pytest
from google.auth.exceptions import GoogleAuthError

from bot.committee_backend import CommitteeBackend, CommitteeBackendError


class RecordingBackend(CommitteeBackend):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def _post(self, url, payload):
        self.calls.append((url, payload))
        if payload["action"] == "list_decisions":
            return {"success": True, "applications": [{"id": "application-1"}]}
        if payload["action"] == "list_organizations":
            return {
                "success": True,
                "organizations": [{"id": "organization-1", "name": "ITラボ"}],
            }
        return {"success": True, "applicationId": "application-1"}


def test_emulator_urls_do_not_request_oidc_tokens():
    assert CommitteeBackend._uses_emulator("http://127.0.0.1:5001/function")
    assert CommitteeBackend._uses_emulator("http://localhost:5001/function")
    assert CommitteeBackend._uses_emulator("http://functions:5001/function")
    assert not CommitteeBackend._uses_emulator("https://cloudfunctions.net/function")


@pytest.mark.asyncio
async def test_google_auth_failures_are_exposed_as_backend_unavailable():
    backend = CommitteeBackend()
    backend._headers = AsyncMock(side_effect=GoogleAuthError("metadata unavailable"))

    with pytest.raises(CommitteeBackendError) as caught:
        await backend._post("https://cloudfunctions.net/function", {"action": "test"})

    assert caught.value.status == 503


@pytest.mark.asyncio
async def test_registration_and_application_payloads_are_server_bound():
    backend = RecordingBackend()

    await backend.request_code(
        guild_id=10,
        discord_user_id=20,
        email="student@keio.jp",
        bureau="IT局",
        grade="２年生",
    )
    await backend.create_application(
        guildId="10",
        discordUserId="20",
        applicantDisplayName="矢上 太郎",
        kind="project_leader",
        role="企画長",
        organizationId="organization-1",
        organizationNotListed=False,
    )
    organizations = await backend.list_organizations(guild_id=10, discord_user_id=20)
    decisions = await backend.list_decisions(10)

    assert backend.calls[0][1] == {
        "action": "request_code",
        "guildId": "10",
        "discordUserId": "20",
        "email": "student@keio.jp",
        "bureau": "IT局",
        "grade": "２年生",
    }
    assert backend.calls[1][1]["organizationId"] == "organization-1"
    assert organizations == [{"id": "organization-1", "name": "ITラボ"}]
    assert decisions == [{"id": "application-1"}]
