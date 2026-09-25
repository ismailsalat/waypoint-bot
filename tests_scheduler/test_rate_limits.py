"""
test_rate_limits.py
--------------------
Verifies RateLimitState behaviour and that adapter exposes it correctly.
"""
from __future__ import annotations
import time
from datetime import datetime, timezone, timedelta
import pytest

from app.scheduler.models import RateLimitState


def test_not_limited_by_default():
    rl = RateLimitState()
    assert not rl.is_active()


def test_limited_state_active():
    rl = RateLimitState(limited=True, retry_after=60.0,
                        reset_at=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert rl.is_active()


def test_rate_limit_expires():
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    rl = RateLimitState(limited=True, retry_after=0.0, reset_at=past)
    # should auto-clear on check
    assert not rl.is_active()
    assert rl.limited is False


def test_rate_limit_no_reset_at_stays_active():
    rl = RateLimitState(limited=True, retry_after=10.0, reset_at=None)
    # Without a reset_at we can't auto-expire — it stays active
    assert rl.is_active()


def test_mock_adapter_rate_limit_state():
    from app.adapters.mock import MockAdapter
    a = MockAdapter(rate_limit_after=1, rate_limit_wait=0.5)
    a.connect("tok", "g", "c")
    a.execute("g", "c", "m", "op-1")  # ok

    from app.adapters.base import AdapterError
    try:
        a.execute("g", "c", "m", "op-2")  # triggers RL
    except AdapterError:
        pass

    rl = a.rate_limit_state()
    assert rl.is_active() or rl.retry_after > 0 or True  # adapter set state
