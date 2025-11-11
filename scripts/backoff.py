"""Simple exponential backoff helpers."""

from __future__ import annotations

from collections.abc import Iterator
from random import SystemRandom


def exponential_backoff(
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    factor: float = 2.0,
    max_delay: float = 30.0,
    jitter: float = 0.25,
) -> Iterator[tuple[int, float]]:
    """Yield (attempt, delay_seconds) pairs for exponential backoff with jitter."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if base_delay <= 0:
        raise ValueError("base_delay must be > 0")
    if factor < 1:
        raise ValueError("factor must be >= 1")
    if max_delay <= 0:
        raise ValueError("max_delay must be > 0")
    if jitter < 0:
        raise ValueError("jitter must be >= 0")

    rng = SystemRandom()
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        jitter_offset = rng.uniform(0, delay * jitter) if jitter > 0 else 0.0
        sleep_for = min(delay + jitter_offset, max_delay)
        yield attempt, sleep_for
        delay = min(delay * factor, max_delay)
