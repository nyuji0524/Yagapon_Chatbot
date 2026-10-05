"""API authentication helpers."""

import hmac
import os

from fastapi import Header, HTTPException, status


async def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """Require a configured bearer token for administrative API routes."""
    expected = os.environ.get("YAGAPON_API_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured",
        )

    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
