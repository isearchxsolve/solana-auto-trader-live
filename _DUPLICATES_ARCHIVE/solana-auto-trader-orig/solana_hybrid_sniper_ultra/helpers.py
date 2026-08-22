"""
helpers.py -- Generic utility functions (retry, unit conversions, etc.)
"""

import time
import asyncio
import logging
from functools import wraps
from typing import Callable, Any

logger = logging.getLogger(__name__)


def retry(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Synchronous retry with exponential backoff."""
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> Any:
            wait = delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if attempt == max_attempts:
                        raise
                    logger.warning(f"[retry] {fn.__name__} attempt {attempt} failed: {exc}. Retrying in {wait:.1f}s...")
                    time.sleep(wait)
                    wait *= backoff
        return wrapper
    return decorator


def async_retry(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Async retry with exponential backoff."""
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        async def wrapper(*args, **kwargs) -> Any:
            wait = delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    if attempt == max_attempts:
                        raise
                    logger.warning(f"[async_retry] {fn.__name__} attempt {attempt} failed: {exc}. Retrying in {wait:.1f}s...")
                    await asyncio.sleep(wait)
                    wait *= backoff
        return wrapper
    return decorator


def lamports_to_sol(lamports: int) -> float:
    return lamports / 1_000_000_000


def sol_to_lamports(sol: float) -> int:
    return int(sol * 1_000_000_000)


def safe_divide(a: float, b: float, fallback: float = 0.0) -> float:
    return a / b if b != 0 else fallback


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def now_ms() -> int:
    return int(time.time() * 1000)
