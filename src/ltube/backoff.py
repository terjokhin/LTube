"""Pacing and retry policy, so a run never hammers YouTube."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class BackoffConfig:
    sleep_min: float = 5.0
    sleep_max: float = 15.0
    request_sleep: float = 1.0
    retries: int = 5
    base: float = 10.0
    cap: float = 600.0
    max_consecutive_failures: int = 3
    rate_limit: int | None = None  # bytes per second, None = unlimited

    def __post_init__(self) -> None:
        if self.sleep_min < 0 or self.sleep_max < self.sleep_min:
            raise ValueError("sleep range must satisfy 0 <= MIN <= MAX")
        if self.base <= 0 or self.cap < self.base:
            raise ValueError("backoff must satisfy 0 < base <= max")
        if self.request_sleep < 0 or self.retries < 0:
            raise ValueError("request pause and retries can't be negative")
        if self.max_consecutive_failures < 1:
            raise ValueError("stop-after-failures must be at least 1")
        if self.rate_limit is not None and self.rate_limit <= 0:
            raise ValueError("rate limit must be positive")

    def track_pause(self) -> float:
        """Random pause between two tracks."""
        return random.uniform(self.sleep_min, self.sleep_max)

    def retry_delay(self, attempt: int) -> float:
        """Delay before retry number `attempt` (0-based).

        Exponential and capped, with "equal jitter": the wait is randomized but never
        drops below half the nominal value, so retries stay polite.
        """
        nominal = min(self.cap, self.base * 2**attempt)
        return nominal / 2 + random.uniform(0, nominal / 2)


def inner_retry_sleep(attempt: int) -> float:
    """Sleep between yt-dlp's own low-level retries (single HTTP requests, fragments).

    yt-dlp retries instantly by default; these are short because whole-track failures
    are retried again, with the user's backoff, one level up.
    """
    return min(2.0 ** (attempt + 1), 30.0)


def parse_range(value: str) -> tuple[float, float]:
    """Parse "5-15" or "10" into (min, max) seconds."""
    lo, sep, hi = value.partition("-")
    try:
        lo_f = float(lo)
        hi_f = float(hi) if sep else lo_f
    except ValueError:
        raise ValueError(f"expected MIN-MAX or a number of seconds, got {value!r}") from None
    if hi_f < lo_f:
        raise ValueError(f"MIN must not exceed MAX in {value!r}")
    return lo_f, hi_f
