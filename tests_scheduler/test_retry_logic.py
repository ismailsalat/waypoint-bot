"""
test_retry_logic.py
--------------------
Verifies exponential backoff, jitter, and permanent-error short-circuit.
"""
from __future__ import annotations
import pytest
from app.scheduler.retry import RetryPolicy
from app.scheduler.models import ErrorCategory


def test_exponential_backoff():
    p = RetryPolicy(base_delay=2.0, max_delay=300.0, jitter=0.0)
    delays = [p.next_delay(i) for i in range(1, 6)]
    for i in range(1, len(delays)):
        assert delays[i] >= delays[i-1], \
            f"Delay not increasing: {delays}"


def test_jitter_produces_variation():
    p = RetryPolicy(base_delay=2.0, max_delay=300.0, jitter=0.3)
    delays = {p.next_delay(2) for _ in range(30)}
    assert len(delays) > 1, "Jitter produced identical delays — RNG not applied"


def test_max_delay_cap():
    p = RetryPolicy(base_delay=2.0, max_delay=10.0, jitter=0.0)
    for attempt in range(1, 20):
        assert p.next_delay(attempt) <= 10.0 + 0.01


def test_permanent_error_no_retry():
    p = RetryPolicy()
    for cat in (ErrorCategory.AUTH, ErrorCategory.PERMISSION,
                ErrorCategory.NOT_FOUND, ErrorCategory.PERMANENT):
        assert not p.should_retry(1, cat, retryable=False), \
            f"Should NOT retry {cat}"


def test_transient_error_retries():
    p = RetryPolicy(max_attempts=3)
    assert p.should_retry(1, ErrorCategory.TRANSIENT, retryable=True)
    assert p.should_retry(3, ErrorCategory.TRANSIENT, retryable=True)
    assert not p.should_retry(4, ErrorCategory.TRANSIENT, retryable=True)


def test_rate_limit_uses_retry_after():
    p = RetryPolicy()
    delay = p.next_delay(1, rate_limit_after=42.0)
    assert delay == 42.0


def test_rate_limit_capped_at_max():
    p = RetryPolicy(max_delay=10.0)
    delay = p.next_delay(1, rate_limit_after=9999.0)
    assert delay == 10.0
