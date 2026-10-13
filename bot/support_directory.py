"""Trusted, optional bridge to the Yaga Support Firestore directory."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp
import google.auth
from google.auth.transport.requests import Request

log = logging.getLogger("yagapon.support_directory")

COMMITTEE_ROLE = "矢上祭実行委員"
FIRESTORE_SCOPE = "https://www.googleapis.com/auth/datastore"


@dataclass(frozen=True)
class SupportIdentity:
    status: str
    uid: str | None = None
    departments: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    grade: str | None = None
    role_synced: bool = False


def _field_value(value: dict[str, Any] | None) -> Any:
    if not value:
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return value["integerValue"]
    if "arrayValue" in value:
        return tuple(_field_value(item) for item in value["arrayValue"].get("values", ()))
    return None


def _document_fields(document: dict[str, Any]) -> dict[str, Any]:
    return {
        name: _field_value(value)
        for name, value in document.get("fields", {}).items()
    }


def _grade_matches(selected_grade: str, stored_grade: str | None) -> bool:
    if stored_grade is None:
        return False
    normalized = str(stored_grade).strip().upper()
    expected = selected_grade.removesuffix("年")
    return normalized in {expected, f"B{expected}", selected_grade.upper()}


class SupportDirectory:
    """Look up Support identities with server credentials, never client claims."""

    def __init__(self) -> None:
        self.project_id = os.environ.get("YAGAPON_SUPPORT_FIREBASE_PROJECT", "").strip()
        self.role_sync_enabled = (
            os.environ.get("YAGAPON_SUPPORT_ROLE_SYNC_ENABLED", "false").lower() == "true"
        )
        self.timeout_seconds = float(os.environ.get("YAGAPON_SUPPORT_LOOKUP_TIMEOUT_SECONDS", "8"))
        self._credentials = None
        self._credentials_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.project_id)

    async def _token(self) -> str:
        async with self._credentials_lock:
            if self._credentials is None:
                credentials, _project = await asyncio.to_thread(
                    google.auth.default,
                    scopes=[FIRESTORE_SCOPE],
                )
                self._credentials = credentials
            if not self._credentials.valid or not self._credentials.token:
                await asyncio.to_thread(self._credentials.refresh, Request())
            return self._credentials.token

    async def _request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        token = await self._token()
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.request(method, url, json=payload) as response:
                if response.status == 404:
                    return None
                if response.status >= 400:
                    detail = (await response.text())[:300]
                    raise RuntimeError(f"Firestore request failed: {response.status} {detail}")
                return await response.json()

    def _documents_url(self, suffix: str = "") -> str:
        base = (
            "https://firestore.googleapis.com/v1/projects/"
            f"{quote(self.project_id, safe='')}/databases/(default)/documents"
        )
        return f"{base}{suffix}"

    async def _find_user(self, email: str) -> dict[str, Any] | None:
        payload = {
            "structuredQuery": {
                "from": [{"collectionId": "users"}],
                "where": {
                    "fieldFilter": {
                        "field": {"fieldPath": "email"},
                        "op": "EQUAL",
                        "value": {"stringValue": email},
                    }
                },
                "limit": 2,
            }
        }
        rows = await self._request(
            "POST",
            self._documents_url("/support/functions:runQuery"),
            payload=payload,
        )
        documents = [row["document"] for row in rows or () if "document" in row]
        if len(documents) != 1:
            return None
        return documents[0]

    async def _find_grade(self, email: str) -> str | None:
        document = await self._request(
            "GET",
            self._documents_url(f"/support/functions/staffInfos/{quote(email, safe='')}"),
        )
        return _document_fields(document or {}).get("grade")

    async def _sync_committee_role(self, document_name: str) -> None:
        payload = {
            "writes": [
                {
                    "transform": {
                        "document": document_name,
                        "fieldTransforms": [
                            {
                                "fieldPath": "roles",
                                "appendMissingElements": {
                                    "values": [{"stringValue": COMMITTEE_ROLE}]
                                },
                            }
                        ],
                    }
                }
            ]
        }
        await self._request("POST", self._documents_url(":commit"), payload=payload)

    async def verify(self, email: str, bureau: str, grade: str) -> SupportIdentity:
        if not self.enabled:
            return SupportIdentity(status="disabled")
        try:
            user_document, stored_grade = await asyncio.gather(
                self._find_user(email),
                self._find_grade(email),
            )
            if user_document is None:
                return SupportIdentity(status="not_found", grade=stored_grade)
            fields = _document_fields(user_document)
            departments = tuple(fields.get("departments") or ())
            roles = tuple(fields.get("roles") or ())
            uid = str(fields.get("uid") or user_document["name"].rsplit("/", 1)[-1])
            if bureau not in departments or not _grade_matches(grade, stored_grade):
                return SupportIdentity(
                    status="profile_mismatch",
                    uid=uid,
                    departments=departments,
                    roles=roles,
                    grade=str(stored_grade) if stored_grade is not None else None,
                )
            return SupportIdentity(
                status="directory_match",
                uid=uid,
                departments=departments,
                roles=roles,
                grade=str(stored_grade),
                role_synced=COMMITTEE_ROLE in roles,
            )
        except Exception:
            log.exception("Yaga Support directory verification failed")
            return SupportIdentity(status="unavailable")

    async def approve_link(
        self,
        email: str,
        bureau: str,
        grade: str,
        expected_uid: str,
    ) -> SupportIdentity:
        """Re-verify an administrator-approved link and optionally sync its role."""
        identity = await self.verify(email, bureau, grade)
        if identity.status != "directory_match" or identity.uid != expected_uid:
            return SupportIdentity(status="approval_mismatch")
        if identity.role_synced or not self.role_sync_enabled:
            return SupportIdentity(
                status="verified",
                uid=identity.uid,
                departments=identity.departments,
                roles=identity.roles,
                grade=identity.grade,
                role_synced=identity.role_synced,
            )
        try:
            user_document = await self._find_user(email)
            if user_document is None or user_document["name"].rsplit("/", 1)[-1] != expected_uid:
                return SupportIdentity(status="approval_mismatch")
            await self._sync_committee_role(user_document["name"])
        except Exception:
            log.exception("Yaga Support role synchronization failed uid=%s", expected_uid)
            return SupportIdentity(status="unavailable")
        return SupportIdentity(
            status="verified",
            uid=identity.uid,
            departments=identity.departments,
            roles=(*identity.roles, COMMITTEE_ROLE),
            grade=identity.grade,
            role_synced=True,
        )
