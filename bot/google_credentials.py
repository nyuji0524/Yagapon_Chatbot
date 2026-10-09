"""Shared Google credential loading for Drive read/write integrations."""

import json
import os
from pathlib import Path

import google.auth
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials


def _load_json(value: str, label: str) -> tuple[dict, str]:
    """Load an inline JSON object or a JSON file without logging secret contents."""
    if value.startswith("{"):
        return json.loads(value), f"{label}-json"

    path = Path(value)
    if not path.is_file():
        raise RuntimeError(
            f"{label} points to a file that is not available inside the container: {path}"
        )
    return json.loads(path.read_text()), f"{label}-file"


def load_google_credentials(scopes: list[str], configured: str | None = None):
    """Load user OAuth, legacy service-account, or Application Default Credentials."""
    user_oauth = os.environ.get("GOOGLE_OAUTH_AUTHORIZED_USER_JSON", "").strip()
    if user_oauth:
        info, source = _load_json(user_oauth, "user-oauth")
        if info.get("type") not in (None, "authorized_user"):
            raise RuntimeError("GOOGLE_OAUTH_AUTHORIZED_USER_JSON must contain authorized-user credentials")
        return Credentials.from_authorized_user_info(info, scopes=scopes), source

    configured = (
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if configured is None
        else configured
    ).strip()
    if configured:
        info, source = _load_json(configured, "service-account")
        return (
            service_account.Credentials.from_service_account_info(info, scopes=scopes),
            source,
        )

    credentials, _ = google.auth.default(scopes=scopes)
    return credentials, "application-default-credentials"
