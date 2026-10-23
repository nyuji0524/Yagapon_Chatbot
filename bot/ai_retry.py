"""Explicit retry policy for Gemini API calls.

The google-genai SDK retries 429 responses internally by default.  That makes a
voice command look stuck for several minutes and then the realtime loop starts
the same request again.  Keep SDK retries disabled and handle rate limits in one
observable, bounded place instead.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from google import genai
from google.genai import types

log = logging.getLogger("yagapon.ai_retry")

T = TypeVar("T")

TRANSIENT_API_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class RateLimitExceeded(RuntimeError):
    """Raised after the configured 429 retries have been exhausted."""

    def __init__(self, message: str, *, retry_after: float | None = None, diagnostic: str = ""):
        super().__init__(message)
        self.retry_after = retry_after
        self.diagnostic = diagnostic


class RateLimitCircuitBreaker:
    """Process-wide backoff for APIs whose quota is shared by all sessions."""

    def __init__(
        self,
        *,
        base_cooldown: float = 300.0,
        max_cooldown: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.base_cooldown = base_cooldown
        self.max_cooldown = max_cooldown
        self._clock = clock
        self._blocked_until = 0.0
        self._failure_count = 0

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def remaining(self) -> float:
        return max(0.0, self._blocked_until - self._clock())

    def trip(self, *, retry_after: float | None = None) -> float:
        self._failure_count += 1
        exponential = self.base_cooldown * (2 ** (self._failure_count - 1))
        cooldown = min(self.max_cooldown, max(exponential, retry_after or 0.0))
        self._blocked_until = self._clock() + cooldown
        return cooldown

    def reset(self) -> None:
        self._blocked_until = 0.0
        self._failure_count = 0


def new_genai_client(api_key: str):
    """Create a client whose retries are controlled by :func:`retry_rate_limit`."""
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=120_000,
            # Interactions uses a separate generated SDK. In google-genai 2.29,
            # attempts=1 still means one *retry*, and a server Retry-After of
            # 180 seconds can collide with our outer timeout. Exclude 429 from
            # that layer and handle it explicitly below. Keep one retry for
            # short-lived network/server failures.
            retry_options=types.HttpRetryOptions(
                attempts=1,
                http_status_codes=[408, 500, 502, 503, 504],
            ),
        ),
    )


def is_rate_limit_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code is None:
        response = getattr(exc, "response", None) or getattr(exc, "raw_response", None)
        code = getattr(response, "status_code", None)
    message = str(exc).lower()
    return code == 429 or "too many requests" in message or "resource_exhausted" in message


def _status_code(exc: BaseException) -> int | None:
    """Return an HTTP-like status code exposed by generated Google clients."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code is None:
        response = getattr(exc, "response", None) or getattr(exc, "raw_response", None)
        code = getattr(response, "status_code", None)
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def is_transient_api_error(exc: BaseException) -> bool:
    """Return whether an API error is safe to retry as a transient failure."""
    return _status_code(exc) in TRANSIENT_API_STATUS_CODES or is_rate_limit_error(exc)


def _retry_after(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None) or getattr(exc, "raw_response", None)
    headers = getattr(exc, "headers", None) or getattr(response, "headers", None)
    value = (headers.get("retry-after") or headers.get("Retry-After")) if headers else None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass

    payload = _error_payload(exc)
    error = payload.get("error")
    if not isinstance(error, dict):
        # google-genai accepts both the usual {"error": {...}} response and
        # an already-unwrapped error object when constructing APIError.
        error = payload
    details = error.get("details", [])
    if not isinstance(details, list):
        details = []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        retry_delay = detail.get("retryDelay")
        if isinstance(retry_delay, str) and retry_delay.endswith("s"):
            try:
                return max(0.0, float(retry_delay[:-1]))
            except ValueError:
                continue
    return None


def _error_payload(exc: BaseException) -> dict:
    # Generated clients expose the same safe error payload under different
    # attributes. In particular, google-genai APIError stores the original
    # response JSON in ``details`` (despite that name also being used inside
    # the payload for google.rpc detail records).
    for attribute in ("body", "response_json", "details"):
        data = getattr(exc, attribute, None)
        if isinstance(data, dict) and data:
            return data
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed:
                return parsed
    return {}


def rate_limit_diagnostic(exc: BaseException) -> str:
    """Extract quota information without logging credentials or request bodies."""
    payload = _error_payload(exc)
    error = payload.get("error")
    if not isinstance(error, dict):
        error = payload
    parts: list[str] = []
    status = error.get("status")
    message = error.get("message")
    if status:
        parts.append(f"status={status}")
    if message:
        parts.append(f"message={str(message)[:500]}")
    details = error.get("details", [])
    if not isinstance(details, list):
        details = []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        retry_delay = detail.get("retryDelay")
        if retry_delay:
            parts.append(f"retry_delay={retry_delay}")
        for violation in detail.get("violations", []):
            if not isinstance(violation, dict):
                continue
            for key in ("quotaMetric", "quotaId", "quotaValue"):
                if violation.get(key):
                    parts.append(f"{key}={violation[key]}")
            dimensions = violation.get("quotaDimensions")
            if isinstance(dimensions, dict):
                safe_dimensions = ",".join(f"{key}:{value}" for key, value in sorted(dimensions.items()))
                if safe_dimensions:
                    parts.append(f"dimensions={safe_dimensions}")
    if not parts:
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        parts.append(f"status_code={status_code or 'unknown'}")
        parts.append(f"message={str(exc)[:500]}")
    return " ".join(parts)


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
                retry_after = _retry_after(exc)
                diagnostic = rate_limit_diagnostic(exc)
                log.error(
                    "Rate limit exhausted operation=%s attempts=%s diagnostic=%s",
                    operation_name,
                    attempts,
                    diagnostic,
                )
                raise RateLimitExceeded(
                    f"{operation_name} remained rate limited after {attempts} attempts",
                    retry_after=retry_after,
                    diagnostic=diagnostic,
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


async def retry_transient_api(
    operation: Callable[[], Awaitable[T]],
    *,
    operation_name: str,
    attempts: int = 5,
    initial_delay: float = 2.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Retry bounded transient HTTP failures without changing 429-only callers.

    File Search maintenance jobs are long-running and should survive a short
    408/5xx outage. Permanent 4xx responses are raised immediately. The final
    transient exception is re-raised unchanged so existing callers retain
    their error handling and diagnostics. Server-provided Retry-After values
    are intentionally capped by ``max_delay`` so one API response cannot make
    the maintenance worker sleep without an application-controlled bound.
    """
    attempts = max(1, attempts)
    if sleep is None:
        sleep = asyncio.sleep
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if not is_transient_api_error(exc):
                raise
            if attempt >= attempts:
                log.error(
                    "Transient API failure exhausted operation=%s attempts=%s status=%s",
                    operation_name,
                    attempts,
                    _status_code(exc) or "unknown",
                )
                raise
            delay = _retry_after(exc)
            if delay is None:
                delay = min(max_delay, initial_delay * (2 ** (attempt - 1)))
            else:
                delay = min(max_delay, delay)
            log.warning(
                "Transient API failure operation=%s attempt=%s/%s status=%s retry_after=%.1fs",
                operation_name,
                attempt,
                attempts,
                _status_code(exc) or "unknown",
                delay,
            )
            await sleep(delay)

    raise AssertionError("unreachable")
