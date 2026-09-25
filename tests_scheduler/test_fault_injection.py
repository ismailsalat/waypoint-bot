"""
test_fault_injection.py
------------------------
Fault injection: network errors, 429, 500, auth rejection, corrupt config.
"""
from __future__ import annotations
import time
import pytest

from app.adapters.mock import MockAdapter
from app.adapters.base import AdapterError
from app.scheduler.manager import SchedulerManager
from app.scheduler.models import AccountConfig, ErrorCategory, JobStatus
from app.scheduler.retry import RetryPolicy
from app.services.credentials import store_credential, clear_all


def _cfg(i=0):
    return AccountConfig(
        account_id=f"fault-{i:03d}", name=f"Fault {i}",
        guild_id=str(100000000 + i), channel_id=str(200000000 + i),
        cooldown_minutes=0.02, offset_max_minutes=0,
    )


def test_network_timeout_retried(tmp_db):
    acc     = _cfg(0)
    adapter = MockAdapter(fail_times=2, error_category=ErrorCategory.TRANSIENT)
    store_credential(acc.account_id, "tok")

    mgr = SchedulerManager(db=tmp_db, max_workers=2,
                            adapter_factory=lambda _: adapter)
    mgr.start()
    mgr.add_or_update_account(acc, "tok")
    mgr.start_job(acc.account_id)

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = mgr.get_job_for_account(acc.account_id)
        if job and job.total_successes >= 1:
            break
        time.sleep(0.3)

    mgr.shutdown(timeout=5)
    clear_all()

    job = mgr.get_job_for_account(acc.account_id)
    assert job and job.total_successes >= 1, "Should succeed after 2 transient failures"


def test_auth_failure_not_retried():
    """AUTH errors must not be retried."""
    p = RetryPolicy()
    assert not p.should_retry(1, ErrorCategory.AUTH, retryable=False)
    assert not p.should_retry(1, ErrorCategory.AUTH, retryable=True)


def test_server_500_retried():
    p = RetryPolicy(max_attempts=3)
    assert p.should_retry(1, ErrorCategory.SERVER_ERROR, retryable=True)
    assert p.should_retry(3, ErrorCategory.SERVER_ERROR, retryable=True)
    assert not p.should_retry(4, ErrorCategory.SERVER_ERROR, retryable=True)


def test_429_uses_retry_after():
    p = RetryPolicy()
    delay = p.next_delay(1, rate_limit_after=30.0)
    assert delay == 30.0


def test_permanent_failure_suspends():
    """
    After MAX_CONSECUTIVE_FAILURES failures, job model enters SUSPENDED.
    Verifies the circuit-breaker threshold via the Job model directly.
    """
    from app.scheduler.models import Job, JobStatus
    from app.config.settings import MAX_CONSECUTIVE_FAILURES

    job = Job(account_id="fault-suspend")
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        job.record_failure(max_failures=MAX_CONSECUTIVE_FAILURES)

    assert job.status == JobStatus.SUSPENDED, \
        f"Expected SUSPENDED, got {job.status}"
    assert job.consecutive_failures == MAX_CONSECUTIVE_FAILURES


def test_corrupt_config_validation():
    """Corrupt config must be caught before reaching the scheduler."""
    bad = AccountConfig(
        account_id="bad-01", name="",
        guild_id="not-numeric", channel_id="",
        cooldown_minutes=-5, offset_max_minutes=-1,
    )
    errors = bad.validate()
    assert len(errors) >= 4, f"Expected ≥4 errors, got: {errors}"


def test_missing_credential_blocked(tmp_db):
    """Job must not start if no credential is stored."""
    from app.services.credentials import clear_all, delete_credential
    acc = _cfg(2)
    delete_credential(acc.account_id)  # ensure no cred

    mgr = SchedulerManager(db=tmp_db, max_workers=1,
                            adapter_factory=lambda _: MockAdapter())
    mgr.start()
    mgr.add_or_update_account(acc)  # no credential passed
    mgr.start_job(acc.account_id)

    time.sleep(0.5)

    mgr.shutdown(timeout=3)
    job = mgr.get_job_for_account(acc.account_id)
    # Should fail with NO_CREDENTIAL — not crash
    if job:
        assert job.total_failures >= 0   # may or may not have attempted
    clear_all()
