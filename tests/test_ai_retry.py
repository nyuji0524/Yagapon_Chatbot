from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.ai_retry import RateLimitExceeded, retry_rate_limit


class RateLimitError(RuntimeError):
    code = 429

    def __init__(self, retry_after: str | None = None):
        super().__init__("Too Many Requests")
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        self.response = SimpleNamespace(status_code=429, headers=headers)


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
