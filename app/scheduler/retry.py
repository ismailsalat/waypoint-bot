"""
Retry policy with exponential backoff and jitter.

Usage:
    policy = RetryPolicy()
    delay  = policy.next_delay(attempt=1)   # ~2 s
    delay  = policy.next_delay(attempt=2)   # ~4 s
    delay  = policy.next_delay(attempt=3)   # ~8 s
"""
from __future__ import annotations

import random
from app.config.settings import (
    RETRY_BASE_DELAY, RETRY_MAX_DELAY,
    RETRY_MAX_ATTEMPTS, RETRY_JITTER_FACTOR,
)
from app.scheduler.models import ErrorCategory


class RetryPolicy:

    def __init__(
        self,
        base_delay:   float = RETRY_BASE_DELAY,
        max_delay:    float = RETRY_MAX_DELAY,
        max_attempts: int   = RETRY_MAX_ATTEMPTS,
        jitter:       float = RETRY_JITTER_FACTOR,
    ):
        self.base_delay   = base_delay
        self.max_delay    = max_delay
        self.max_attempts = max_attempts
        self.jitter       = jitter

    def should_retry(self, attempt: int, error_cat: ErrorCategory,
                     retryable: bool) -> bool:
        if not retryable:
            return False
        if error_cat in (ErrorCategory.AUTH, ErrorCategory.PERMISSION,
                         ErrorCategory.NOT_FOUND, ErrorCategory.PERMANENT):
            return False
        return attempt <= self.max_attempts

    def next_delay(self, attempt: int, rate_limit_after: float = 0.0) -> float:
        """Return seconds to wait before next attempt."""
        if rate_limit_after > 0:
            return min(rate_limit_after, self.max_delay)
        raw    = self.base_delay * (2 ** (attempt - 1))
        capped = min(raw, self.max_delay)
        jitter = capped * self.jitter * (random.random() * 2 - 1)
        return max(self.base_delay, capped + jitter)
