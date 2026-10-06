"""Shared Google credential loading for Drive read/write integrations."""

import json
import os
from pathlib import Path

import google.auth
from google.oauth2 import service_account


def load_google_credentials(scopes: list[str], configured: str | None = None):
    """Load legacy explicit credentials or Application Default Credentials."""
    configured = (
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if configured is None
        else configured
    ).strip()
    if configured:
        if configured.startswith("{"):
            info = json.loads(configured)
            return (
                service_account.Credentials.from_service_account_info(info, scopes=scopes),
                "service-account-json",
            )
        path = Path(configured)
        if not path.is_file():
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON points to a file that is not available "
                f"inside the container: {path}"
            )
        return (
            service_account.Credentials.from_service_account_file(path, scopes=scopes),
            "service-account-file",
        )

    credentials, _ = google.auth.default(scopes=scopes)
    return credentials, "application-default-credentials"
