"""Explicit retry policy for Gemini API calls.

The google-genai SDK retries 429 responses internally by default.  That makes a
voice command look stuck for several minutes and then the realtime loop starts
the same request again.  Keep SDK retries disabled and handle rate limits in one
observable, bounded place instead.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from google import genai
from google.genai import types

log = logging.getLogger("yagapon.ai_retry")

T = TypeVar("T")


class RateLimitExceeded(RuntimeError):
    """Raised after the configured 429 retries have been exhausted."""


def new_genai_client(api_key: str):
    """Create a client whose retries are controlled by :func:`retry_rate_limit`."""
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=120_000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def is_rate_limit_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code is None:
        response = getattr(exc, "response", None)
        code = getattr(response, "status_code", None)
    message = str(exc).lower()
    return code == 429 or "too many requests" in message or "resource_exhausted" in message


def _retry_after(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


async def retry_rate_limit(
    operation: Callable[[], Awaitable[T]],
    *,
    operation_name: str,
    attempts: int = 4,
    initial_delay: float = 5.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Retry only HTTP 429 responses with bounded exponential backoff."""
    attempts = max(1, attempts)
    if sleep is None:
        sleep = asyncio.sleep
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if not is_rate_limit_error(exc):
                raise
            if attempt >= attempts:
                raise RateLimitExceeded(
                    f"{operation_name} remained rate limited after {attempts} attempts"
                ) from exc
            delay = _retry_after(exc)
            if delay is None:
                delay = min(max_delay, initial_delay * (2 ** (attempt - 1)))
            else:
                delay = min(max_delay, delay)
            log.warning(
                "Rate limited operation=%s attempt=%s/%s retry_after=%.1fs",
                operation_name,
                attempt,
                attempts,
                delay,
            )
            await sleep(delay)

    raise AssertionError("unreachable")
