"""IAM-authenticated bridge for committee email and role workflows."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import id_token

log = logging.getLogger("yagapon.committee_backend")

DEFAULT_REGISTRATION_URL = (
    "https://us-central1-yagamy26th.cloudfunctions.net/committeeRegistration"
)
DEFAULT_APPLICATION_URL = (
    "https://us-central1-yagamy26th.cloudfunctions.net/committeeRoleApplicationBridge"
)


class CommitteeBackendError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class CommitteeBackend:
    """Call private Functions with the VM service account's OIDC identity."""

    def __init__(self) -> None:
        self.registration_url = os.environ.get(
            "YAGAPON_COMMITTEE_REGISTRATION_URL",
            DEFAULT_REGISTRATION_URL,
        ).strip()
        self.application_url = os.environ.get(
            "YAGAPON_COMMITTEE_ROLE_APPLICATION_URL",
            DEFAULT_APPLICATION_URL,
        ).strip()
        self.timeout_seconds = float(
            os.environ.get("YAGAPON_COMMITTEE_HTTP_TIMEOUT_SECONDS", "35")
        )

    @staticmethod
    def _uses_emulator(url: str) -> bool:
        # Production Functions must use HTTPS. Plain HTTP is reserved for a
        # local/container emulator and cannot receive a Google OIDC token.
        return url.startswith("http://")

    async def _headers(self, url: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if not self._uses_emulator(url):
            token = await asyncio.to_thread(id_token.fetch_id_token, Request(), url)
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            headers = await self._headers(url)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.post(url, json=payload) as response:
                    try:
                        body = await response.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        body = {"error": (await response.text())[:300]}
                    if response.status >= 400 or body.get("success") is not True:
                        raise CommitteeBackendError(
                            response.status,
                            str(body.get("error") or "Committee backend request failed"),
                        )
                    return body
        except CommitteeBackendError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, GoogleAuthError) as exc:
            raise CommitteeBackendError(
                503,
                "Committee backend is temporarily unavailable",
            ) from exc

    async def request_code(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        email: str,
        bureau: str,
        grade: str,
    ) -> dict[str, Any]:
        return await self._post(
            self.registration_url,
            {
                "action": "request_code",
                "guildId": str(guild_id),
                "discordUserId": str(discord_user_id),
                "email": email,
                "bureau": bureau,
                "grade": grade,
            },
        )

    async def verify_code(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        code: str,
    ) -> dict[str, Any]:
        return await self._post(
            self.registration_url,
            {
                "action": "verify_code",
                "guildId": str(guild_id),
                "discordUserId": str(discord_user_id),
                "code": code,
            },
        )

    async def create_application(self, **payload: Any) -> dict[str, Any]:
        return await self._post(
            self.application_url,
            {"action": "create", **payload},
        )

    async def list_organizations(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
    ) -> list[dict[str, str]]:
        result = await self._post(
            self.application_url,
            {
                "action": "list_organizations",
                "guildId": str(guild_id),
                "discordUserId": str(discord_user_id),
            },
        )
        organizations = result.get("organizations")
        return organizations if isinstance(organizations, list) else []

    async def decide_application(
        self,
        *,
        application_id: str,
        approved: bool,
        actor_discord_id: int,
    ) -> dict[str, Any]:
        return await self._post(
            self.application_url,
            {
                "action": "decide",
                "applicationId": application_id,
                "decision": "approved" if approved else "rejected",
                "actorDiscordId": str(actor_discord_id),
            },
        )

    async def list_decisions(self, guild_id: int) -> list[dict[str, Any]]:
        result = await self._post(
            self.application_url,
            {"action": "list_decisions", "guildId": str(guild_id)},
        )
        applications = result.get("applications")
        return applications if isinstance(applications, list) else []

    async def acknowledge_decision(
        self,
        *,
        guild_id: int,
        application_id: str,
    ) -> None:
        await self._post(
            self.application_url,
            {
                "action": "acknowledge_decision",
                "guildId": str(guild_id),
                "applicationId": application_id,
            },
        )
