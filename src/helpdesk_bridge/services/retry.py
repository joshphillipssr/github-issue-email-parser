from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable

import httpx

TRANSIENT_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass
class RetryableHttpError(Exception):
    status_code: int
    message: str
    retry_after_seconds: float | None = None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return max(0.0, float(value))
    try:
        retry_at = parsedate_to_datetime(value)
    except Exception:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def should_retry_http_status(status_code: int) -> bool:
    return int(status_code) in TRANSIENT_HTTP_STATUS


def should_retry_exception(exc: Exception) -> tuple[bool, float | None]:
    if isinstance(exc, RetryableHttpError):
        return True, exc.retry_after_seconds
    if isinstance(
        exc,
        (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ),
    ):
        return True, None
    if isinstance(exc, httpx.HTTPStatusError):
        retry_after = _parse_retry_after(exc.response.headers.get("Retry-After"))
        return should_retry_http_status(exc.response.status_code), retry_after
    return False, None


async def with_retry(
    *,
    operation: str,
    call: Callable[[], Awaitable[httpx.Response]],
    max_attempts: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
    logger: logging.Logger,
) -> httpx.Response:
    attempts = max(1, int(max_attempts))
    base_delay = max(0.1, float(base_delay_seconds))
    max_delay = max(base_delay, float(max_delay_seconds))

    for attempt in range(1, attempts + 1):
        try:
            response = await call()
            if should_retry_http_status(response.status_code):
                raise RetryableHttpError(
                    status_code=int(response.status_code),
                    message=f"retryable HTTP status {response.status_code}",
                    retry_after_seconds=_parse_retry_after(response.headers.get("Retry-After")),
                )
            response.raise_for_status()
            return response
        except Exception as exc:
            retryable, retry_after = should_retry_exception(exc)
            if not retryable or attempt >= attempts:
                raise
            backoff = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = retry_after if retry_after is not None else backoff
            logger.warning(
                "Retrying API operation after transient failure",
                extra={
                    "event": "api_retry_scheduled",
                    "operation": operation,
                    "attempt": attempt,
                    "max_attempts": attempts,
                    "delay_seconds": delay,
                    "error": repr(exc),
                },
            )
            await asyncio.sleep(delay)

    raise RuntimeError(f"Retry loop exhausted unexpectedly for operation '{operation}'")
