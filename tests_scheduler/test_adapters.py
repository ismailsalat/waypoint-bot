"""
test_adapters.py
-----------------
Verifies MockAdapter behaviour and adapter interface contract.
"""
from __future__ import annotations
import pytest

from app.adapters.mock import MockAdapter
from app.adapters.base import AdapterError
from app.scheduler.models import ErrorCategory


def test_mock_connect():
    a = MockAdapter()
    info = a.connect("tok", "guild1", "ch1")
    assert info.user_tag == "MockUser#0000"
    assert a.is_connected()
    assert a.connect_calls == 1


def test_mock_execute_success():
    a = MockAdapter()
    a.connect("tok", "g", "c")
    r = a.execute("g", "c", "msg", "op-001")
    assert r.success is True
    assert r.verified is False          # verify_result() confirms delivery
    assert len(a.execute_calls) == 1
    # Now verify
    vr = a.verify_result("op-001", r.external_id)
    assert vr.verified is True


def test_mock_failure_injection():
    a = MockAdapter(fail_times=2, error_category=ErrorCategory.TRANSIENT)
    a.connect("tok", "g", "c")

    for _ in range(2):
        with pytest.raises(AdapterError) as exc:
            a.execute("g", "c", "msg", "op-x")
        assert exc.value.retryable is True

    # Third call succeeds
    r = a.execute("g", "c", "msg", "op-x2")
    assert r.success is True


def test_mock_permanent_failure_not_retryable():
    a = MockAdapter(fail_times=1, error_category=ErrorCategory.AUTH)
    a.connect("tok", "g", "c")
    with pytest.raises(AdapterError) as exc:
        a.execute("g", "c", "msg", "op-perm")
    assert exc.value.retryable is False


def test_mock_rate_limit():
    a = MockAdapter(rate_limit_after=1, rate_limit_wait=0.01)
    a.connect("tok", "g", "c")
    a.execute("g", "c", "msg", "op-1")  # first call ok
    with pytest.raises(AdapterError) as exc:
        a.execute("g", "c", "msg", "op-2")  # triggers rate limit
    assert "RATE_LIMITED" in exc.value.code
    assert exc.value.retry_after_ms > 0


def test_mock_disconnect():
    a = MockAdapter()
    a.connect("tok", "g", "c")
    a.disconnect()
    assert not a.is_connected()
    assert a.disconnect_calls == 1


def test_mock_reset():
    a = MockAdapter(fail_times=5)
    a.connect("tok","g","c")
    a.reset()
    assert a.connect_calls == 0
    r = a.execute("g", "c", "msg", "op-r")
    assert r.success is True


def test_adapter_verify_result():
    a = MockAdapter()
    a.connect("tok", "g", "c")
    r = a.verify_result("op-v", "ext-123")
    assert r.verified is True
    assert "op-v" in a.verify_calls
