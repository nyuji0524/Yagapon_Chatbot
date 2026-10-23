from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai.errors import ServerError

from bot.ai_retry import (
    RateLimitCircuitBreaker,
    RateLimitExceeded,
    is_rate_limit_error,
    is_transient_api_error,
    new_genai_client,
    rate_limit_diagnostic,
    retry_rate_limit,
    retry_transient_api,
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


class ApiError(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


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


@pytest.mark.asyncio
async def test_transient_api_retry_recovers_from_503():
    operation = AsyncMock(side_effect=[ApiError(503), "ok"])
    sleep = AsyncMock()

    result = await retry_transient_api(
        operation,
        operation_name="file_search_test",
        attempts=3,
        initial_delay=2,
        sleep=sleep,
    )

    assert result == "ok"
    assert operation.await_count == 2
    sleep.assert_awaited_once_with(2)


@pytest.mark.asyncio
async def test_transient_api_retry_reads_google_genai_retry_info_with_bounded_delay():
    failure = ServerError(
        503,
        {
            "error": {
                "code": 503,
                "status": "UNAVAILABLE",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "90s",
                    }
                ],
            }
        },
    )
    operation = AsyncMock(side_effect=[failure, "ok"])
    sleep = AsyncMock()

    result = await retry_transient_api(
        operation,
        operation_name="file_search_test",
        attempts=2,
        max_delay=7,
        sleep=sleep,
    )

    assert result == "ok"
    sleep.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_transient_api_retry_is_bounded_and_reraises_original_error():
    failure = ApiError(503)
    operation = AsyncMock(side_effect=failure)
    sleep = AsyncMock()

    with pytest.raises(ApiError) as raised:
        await retry_transient_api(
            operation,
            operation_name="file_search_test",
            attempts=3,
            initial_delay=2,
            sleep=sleep,
        )

    assert raised.value is failure
    assert operation.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [2, 4]


@pytest.mark.asyncio
async def test_transient_api_retry_does_not_retry_permanent_4xx():
    operation = AsyncMock(side_effect=ApiError(400))

    with pytest.raises(ApiError):
        await retry_transient_api(operation, operation_name="file_search_test")

    assert operation.await_count == 1


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_api_error_statuses(status):
    assert is_transient_api_error(ApiError(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_permanent_api_error_statuses(status):
    assert is_transient_api_error(ApiError(status)) is False


def test_generated_interactions_429_is_detected_and_diagnosed():
    exc = GeneratedInteractionsRateLimit("quota")

    assert is_rate_limit_error(exc) is True
    diagnostic = rate_limit_diagnostic(exc)
    assert "status=RESOURCE_EXHAUSTED" in diagnostic
    assert "quotaId=RequestsPerMinute" in diagnostic
    assert "model:gemini-3.5-transcribe" in diagnostic


def test_google_genai_unwrapped_error_details_are_diagnosed():
    exc = ServerError(
        429,
        {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "Quota exceeded",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "42s",
                },
                {
                    "violations": [
                        {
                            "quotaId": "RequestsPerMinute",
                            "quotaDimensions": {"model": "gemini-test"},
                        }
                    ]
                },
            ],
        },
    )

    diagnostic = rate_limit_diagnostic(exc)

    assert "status=RESOURCE_EXHAUSTED" in diagnostic
    assert "retry_delay=42s" in diagnostic
    assert "quotaId=RequestsPerMinute" in diagnostic
    assert "model:gemini-test" in diagnostic


def test_google_genai_malformed_retry_details_are_ignored_safely():
    exc = ServerError(
        429,
        {
            "status": "RESOURCE_EXHAUSTED",
            "message": "Quota exceeded",
            "details": None,
        },
    )

    diagnostic = rate_limit_diagnostic(exc)

    assert "status=RESOURCE_EXHAUSTED" in diagnostic
    assert "message=Quota exceeded" in diagnostic


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
