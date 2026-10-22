"""Short-lived, read-only GitHub App authentication for source indexing."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable

import jwt

GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
MAX_TOKEN_RESPONSE_BYTES = 2_000_000
TOKEN_REFRESH_SKEW_SECONDS = 300


class GitHubAppTokenProvider:
    """Mint and cache a least-privilege installation access token.

    GitHub installation tokens expire after one hour.  Callers may reuse this
    object across requests; it refreshes the token five minutes before the
    server-provided expiry and serializes refreshes across worker threads.
    """

    def __init__(
        self,
        *,
        app_id: str,
        installation_id: str | int,
        private_key: str,
        repositories: tuple[str, ...],
        api_url: str = GITHUB_API_URL,
        clock: Callable[[], float] | None = None,
        opener: Callable[..., object] | None = None,
    ) -> None:
        normalized_app_id = str(app_id).strip()
        if not re.fullmatch(r"[A-Za-z0-9]+", normalized_app_id):
            raise RuntimeError("GitHub App ID must be an alphanumeric client or app ID")
        try:
            normalized_installation_id = int(str(installation_id).strip())
        except ValueError as exc:
            raise RuntimeError("GitHub App installation ID must be a positive integer") from exc
        if normalized_installation_id <= 0:
            raise RuntimeError("GitHub App installation ID must be a positive integer")
        normalized_key = private_key.strip()
        if "\\n" in normalized_key and "\n" not in normalized_key:
            normalized_key = normalized_key.replace("\\n", "\n")
        if "-----BEGIN" not in normalized_key or "PRIVATE KEY-----" not in normalized_key:
            raise RuntimeError("GitHub App private key is not a PEM private key")
        normalized_repositories = tuple(dict.fromkeys(repositories))
        if not normalized_repositories or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
            for name in normalized_repositories
        ):
            raise RuntimeError("GitHub App repository restriction is empty or invalid")

        self.app_id = normalized_app_id
        self.installation_id = normalized_installation_id
        self.private_key = normalized_key
        self.repositories = normalized_repositories
        self.api_url = api_url.rstrip("/")
        self._clock = clock or time.time
        self._opener = opener or urllib.request.urlopen
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0

    def __call__(self) -> str:
        return self.token()

    def token(self) -> str:
        now = self._clock()
        if self._token and now < self._expires_at - TOKEN_REFRESH_SKEW_SECONDS:
            return self._token
        with self._lock:
            now = self._clock()
            if self._token and now < self._expires_at - TOKEN_REFRESH_SKEW_SECONDS:
                return self._token
            self._token, self._expires_at = self._mint(now)
            return self._token

    def _mint(self, now: float) -> tuple[str, float]:
        app_jwt = jwt.encode(
            {
                "iat": int(now) - 60,
                "exp": int(now) + 540,
                "iss": self.app_id,
            },
            self.private_key,
            algorithm="RS256",
        )
        body = json.dumps(
            {
                "repositories": list(self.repositories),
                "permissions": {"contents": "read"},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_url}/app/installations/{self.installation_id}/access_tokens",
            data=body,
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {app_jwt}",
                "Content-Type": "application/json",
                "User-Agent": "yagapon-source-index",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )
        try:
            with self._opener(request, timeout=30) as response:
                raw = response.read(MAX_TOKEN_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace")
            raise RuntimeError(
                "GitHub App installation token request failed "
                f"with HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"GitHub App installation token request failed: {exc.reason}"
            ) from exc
        if len(raw) > MAX_TOKEN_RESPONSE_BYTES:
            raise RuntimeError("GitHub App installation token response is unexpectedly large")
        try:
            payload = json.loads(raw.decode("utf-8"))
            token = str(payload["token"])
            expires_at = datetime.fromisoformat(
                str(payload["expires_at"]).replace("Z", "+00:00")
            ).timestamp()
            permissions = payload["permissions"]
            returned_repositories = {
                str(item["name"]).casefold()
                for item in payload["repositories"]
                if isinstance(item, dict) and item.get("name")
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("GitHub App installation token response is invalid") from exc
        if not token or expires_at <= now + TOKEN_REFRESH_SKEW_SECONDS:
            raise RuntimeError("GitHub App installation token has an invalid expiry")
        if not isinstance(permissions, dict) or permissions.get("contents") != "read":
            raise RuntimeError("GitHub App installation token lacks contents:read")
        unexpected = {
            str(name): str(value)
            for name, value in permissions.items()
            if str(name) not in {"contents", "metadata"}
            or str(value).lower() != "read"
        }
        if unexpected:
            raise RuntimeError(
                "GitHub App installation has permissions beyond contents:read "
                "and metadata:read; refuse to index"
            )
        missing = {
            name
            for name in self.repositories
            if name.casefold() not in returned_repositories
        }
        if missing:
            raise RuntimeError(
                "GitHub App installation token does not include configured repositories: "
                + ", ".join(sorted(missing))
            )
        return token, expires_at
