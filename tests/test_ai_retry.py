from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.ai_retry import (
    RateLimitCircuitBreaker,
    RateLimitExceeded,
    is_rate_limit_error,
    new_genai_client,
    rate_limit_diagnostic,
    retry_rate_limit,
)


class RateLimitError(RuntimeError):
    code = 429

    def __init__(self, retry_after: str | None = None):
        super().__init__("Too Many Requests")
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        self.response = SimpleNamespace(status_code=429, headers=headers)


class GeneratedInteractionsRateLimit(RuntimeError):
    status_code = 429
    headers = {"Retry-After": "180"}
    body = """{
      "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": "Quota exceeded",
        "details": [
          {
            "retryDelay": "180s",
            "violations": [
              {
                "quotaMetric": "generativelanguage.googleapis.com/transcribe_requests",
                "quotaId": "RequestsPerMinute",
                "quotaValue": "10",
                "quotaDimensions": {"model": "gemini-3.5-transcribe"}
              }
            ]
          }
        ]
      }
    }"""


@pytest.mark.asyncio
async def test_rate_limit_retry_respects_retry_after():
    operation = AsyncMock(side_effect=[RateLimitError("7"), "ok"])
    sleep = AsyncMock()

    result = await retry_rate_limit(
        operation,
        operation_name="test",
        attempts=2,
        sleep=sleep,
    )

    assert result == "ok"
    sleep.assert_awaited_once_with(7.0)


@pytest.mark.asyncio
async def test_rate_limit_retry_is_bounded():
    operation = AsyncMock(side_effect=RateLimitError())
    sleep = AsyncMock()

    with pytest.raises(RateLimitExceeded):
        await retry_rate_limit(
            operation,
            operation_name="test",
            attempts=3,
            initial_delay=2,
            sleep=sleep,
        )

    assert operation.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [2, 4]


@pytest.mark.asyncio
async def test_non_rate_limit_error_is_not_retried():
    operation = AsyncMock(side_effect=RuntimeError("bad request"))

    with pytest.raises(RuntimeError, match="bad request"):
        await retry_rate_limit(operation, operation_name="test")

    assert operation.await_count == 1


def test_generated_interactions_429_is_detected_and_diagnosed():
    exc = GeneratedInteractionsRateLimit("quota")

    assert is_rate_limit_error(exc) is True
    diagnostic = rate_limit_diagnostic(exc)
    assert "status=RESOURCE_EXHAUSTED" in diagnostic
    assert "quotaId=RequestsPerMinute" in diagnostic
    assert "model:gemini-3.5-transcribe" in diagnostic


@pytest.mark.asyncio
async def test_generated_interactions_retry_after_is_bounded_by_application_policy():
    operation = AsyncMock(side_effect=GeneratedInteractionsRateLimit("quota"))
    sleep = AsyncMock()

    with pytest.raises(RateLimitExceeded) as raised:
        await retry_rate_limit(
            operation,
            operation_name="transcription_create",
            attempts=2,
            max_delay=15,
            sleep=sleep,
        )

    sleep.assert_awaited_once_with(15)
    assert raised.value.retry_after == 180
    assert "RESOURCE_EXHAUSTED" in raised.value.diagnostic


def test_transcription_client_excludes_429_from_sdk_internal_retries():
    client = new_genai_client("test-key")
    retry_config = client.aio.interactions.sdk_configuration.retry_config

    assert "429" not in retry_config.status_codes_override
    assert retry_config.status_codes_override == ["408", "500", "502", "503", "504"]


def test_rate_limit_circuit_breaker_is_shared_and_exponential():
    now = [100.0]
    breaker = RateLimitCircuitBreaker(
        base_cooldown=300,
        max_cooldown=1800,
        clock=lambda: now[0],
    )

    assert breaker.trip(retry_after=180) == 300
    assert breaker.remaining() == 300
    now[0] = 400.0
    assert breaker.remaining() == 0
    assert breaker.trip(retry_after=900) == 900
    assert breaker.failure_count == 2
    breaker.reset()
    assert breaker.remaining() == 0
    assert breaker.failure_count == 0
