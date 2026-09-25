"""
test_circuit_breaker.py
------------------------
KEY TEST: After MAX_CONSECUTIVE_FAILURES, job enters SUSPENDED state.
Successful execution resets the counter.
Manual resume works.
"""
from __future__ import annotations
import time
import pytest

from app.adapters.mock import MockAdapter
from app.scheduler.manager import SchedulerManager
from app.scheduler.models import AccountConfig, ErrorCategory, JobStatus
from app.services.credentials import store_credential, clear_all


def _cfg(i=0):
    return AccountConfig(
        account_id=f"cb-acct-{i:02d}", name=f"CB {i}",
        guild_id=str(111111111 + i), channel_id=str(222222222 + i),
        cooldown_minutes=0.01, offset_max_minutes=0,
    )


def test_suspended_after_5_failures():
    """
    Fail MAX_CONSECUTIVE_FAILURES times on the Job model → SUSPENDED.
    This is a unit test of the model, not the full integration pipeline.
    """
    from app.scheduler.models import Job, JobStatus
    job = Job(account_id="x")
    for i in range(5):
        job.record_failure(max_failures=5)
        if i < 4:
            assert job.status != JobStatus.SUSPENDED, f"Suspended too early at {i+1}"
    assert job.status == JobStatus.SUSPENDED, \
        f"Expected SUSPENDED after 5 failures, got {job.status}"
    assert job.consecutive_failures == 5


def test_success_resets_failure_count():
    from app.scheduler.models import Job
    job = Job(account_id="x")
    job.consecutive_failures = 4
    job.record_success(100.0)
    assert job.consecutive_failures == 0
    assert job.total_successes == 1


def test_resume_clears_suspension():
    from app.scheduler.models import Job, JobStatus
    job = Job(account_id="y")
    for _ in range(5):
        job.record_failure(max_failures=5)
    assert job.status == JobStatus.SUSPENDED
    job.resume()
    assert job.status == JobStatus.WAITING
    assert job.consecutive_failures == 0


def test_failure_count_increments():
    """Unit test: record_failure increments counters correctly."""
    from app.scheduler.models import Job
    job = Job(account_id="y")
    job.record_failure(max_failures=5)
    assert job.total_failures == 1
    assert job.total_runs == 1
    assert job.consecutive_failures == 1
    job.record_failure(max_failures=5)
    assert job.consecutive_failures == 2
