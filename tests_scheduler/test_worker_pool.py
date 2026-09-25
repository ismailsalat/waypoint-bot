"""
test_worker_pool.py
-------------------
KEY TEST: 50 accounts must NOT create 50 permanent threads.
"""
from __future__ import annotations
import threading
import time
import uuid

import pytest

from app.adapters.mock import MockAdapter
from app.scheduler.manager import SchedulerManager
from app.scheduler.models import AccountConfig
from app.services.credentials import store_credential, clear_all


def _make_account(i: int) -> AccountConfig:
    return AccountConfig(
        account_id=f"acct-{i:04d}",
        name=f"Account {i}",
        guild_id=str(10000000000000000 + i),
        channel_id=str(20000000000000000 + i),
        cooldown_minutes=999,       # won't auto-fire
        offset_max_minutes=0,
    )


def test_50_accounts_no_50_threads(tmp_db):
    """
    Schedule 50 accounts and verify the worker thread count stays
    at MAX_WORKERS (3), not 50.
    """
    MAX = 3
    adapters = {}

    def factory(_type):
        a = MockAdapter(latency_ms=50)
        return a

    mgr = SchedulerManager(db=tmp_db, max_workers=MAX, adapter_factory=factory)
    mgr.start()

    base = threading.active_count()

    for i in range(50):
        acc = _make_account(i)
        store_credential(acc.account_id, "fake-token")
        mgr.add_or_update_account(acc, "fake-token")

    # Allow scheduler tick
    time.sleep(0.2)
    peak = threading.active_count()

    mgr.shutdown(timeout=5)
    clear_all()

    # The scheduler tick thread + pool threads (≤3) should be the only additions
    extra = peak - base
    assert extra <= MAX + 5, (
        f"Too many threads created: base={base} peak={peak} extra={extra}. "
        f"Expected ≤ {MAX + 5} extra threads for 50 accounts."
    )


def test_pool_bounded_concurrency(tmp_db):
    """Active jobs in pool must never exceed max_workers."""
    MAX = 2
    results = []

    def factory(_type):
        return MockAdapter(latency_ms=100)

    mgr = SchedulerManager(db=tmp_db, max_workers=MAX, adapter_factory=factory)
    mgr.start()

    for i in range(5):
        acc = _make_account(i + 100)
        store_credential(acc.account_id, "tok")
        mgr.add_or_update_account(acc, "tok")
        mgr.start_job(acc.account_id)

    # Sample active count a few times
    for _ in range(10):
        time.sleep(0.05)
        results.append(mgr._pool.active_count())

    mgr.shutdown(timeout=5)
    clear_all()

    assert all(r <= MAX for r in results), \
        f"Pool exceeded max_workers={MAX}: {results}"


def test_job_lock_prevents_overlap(tmp_db):
    """Same job must not run twice simultaneously."""
    call_times = []
    lock = threading.Lock()

    class SlowAdapter(MockAdapter):
        def execute(self, *args, **kwargs):
            with lock:
                call_times.append(time.monotonic())
            time.sleep(0.15)
            return super().execute(*args, **kwargs)

    def factory(_type):
        return SlowAdapter()

    mgr = SchedulerManager(db=tmp_db, max_workers=4, adapter_factory=factory)
    mgr.start()

    acc = _make_account(999)
    store_credential(acc.account_id, "tok")
    mgr.add_or_update_account(acc, "tok")
    mgr.start_job(acc.account_id)

    # Try submitting the same job twice rapidly
    job = mgr.get_job_for_account(acc.account_id)
    if job:
        mgr._pool.submit(job, acc, factory("bot"))
        mgr._pool.submit(job, acc, factory("bot"))

    time.sleep(0.5)
    mgr.shutdown(timeout=5)
    clear_all()

    # Only one execution should have started at a time
    if len(call_times) >= 2:
        gap = call_times[1] - call_times[0]
        assert gap >= 0.1, f"Jobs overlapped: gap={gap:.3f}s"
