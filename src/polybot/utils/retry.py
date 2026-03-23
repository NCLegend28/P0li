"""
retry.py — async exponential backoff decorator.

Usage:
    from polybot.utils.retry import async_retry

    @async_retry(max_attempts=3, base_delay=1.0, exceptions=(httpx.HTTPError,))
    async def fetch():
        ...
"""
from __future__ import annotations

import asyncio
import functools
from typing import Type

from loguru import logger


def async_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: tuple[Type[BaseException], ...] = (Exception,),
):
    """
    Decorator that retries an async function with exponential backoff.

    Args:
        max_attempts:   Total number of attempts (including the first).
        base_delay:     Seconds to wait after the first failure.
        backoff_factor: Multiplier applied to delay on each retry.
        exceptions:     Exception types that trigger a retry.
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except exceptions as exc:
                    if attempt == max_attempts:
                        raise
                    logger.warning(
                        f"{fn.__qualname__} attempt {attempt}/{max_attempts} failed: "
                        f"{exc!r}. Retrying in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    delay *= backoff_factor
        return wrapper
    return decorator
