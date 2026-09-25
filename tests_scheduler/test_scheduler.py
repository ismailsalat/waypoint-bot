"""
test_scheduler.py
------------------
End-to-end integration: config → manager → worker → adapter → DB → next schedule.
"""
from __future__ import annotations
import time
import pytest

from app.adapters.mock import MockAdapter
from app.scheduler.manager import SchedulerManager
from app.scheduler.models import AccountConfig, JobStatus
from app.services.credentials import store_credential, clear_all


def _cfg(i=0):
    return AccountConfig(
        account_id=f"sched-{i:02d}", name=f"Sched {i}",
        guild_id=str(111111111 + i), channel_id=str(222222222 + i),
        cooldown_minutes=0.01,
        offset_max_minutes=0.005,
    )


def test_full_pipeline_single_account(tmp_db):
    """Full pipeline: add → start → execute → verify in DB."""
    acc     = _cfg(0)
    adapter = MockAdapter()
    store_credential(acc.account_id, "tok")

    mgr = SchedulerManager(db=tmp_db, max_workers=2,
                            adapter_factory=lambda _: adapter)
    mgr.start()
    mgr.add_or_update_account(acc, "tok")
    mgr.start_job(acc.account_id)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = mgr.get_job_for_account(acc.account_id)
        if job and job.total_successes >= 1:
            break
        time.sleep(0.2)

    mgr.shutdown(timeout=5)
    clear_all()

    job = mgr.get_job_for_account(acc.account_id)
    assert job is not None
    assert job.total_successes >= 1, "Expected at least 1 successful execution"
    assert adapter.execute_calls, "Adapter was never called"

    # Check DB
    row = tmp_db.get_job_for_account(acc.account_id)
    assert row is not None
    assert int(row.get("total_successes", 0)) >= 1


def test_health_endpoint(tmp_db):
    mgr = SchedulerManager(db=tmp_db, max_workers=3,
                            adapter_factory=lambda _: MockAdapter())
    mgr.start()
    h = mgr.health()
    mgr.shutdown(timeout=3)

    assert h["running"] is True
    assert h["database"] == "healthy"
    assert "workers" in h
    assert "max_workers" in h


def test_stats_endpoint(tmp_db):
    acc = _cfg(1)
    store_credential(acc.account_id, "tok")
    mgr = SchedulerManager(db=tmp_db, max_workers=2,
                            adapter_factory=lambda _: MockAdapter())
    mgr.start()
    mgr.add_or_update_account(acc, "tok")
    mgr.start_job(acc.account_id)
    time.sleep(0.2)
    stats = mgr.stats()
    mgr.shutdown(timeout=3)
    clear_all()

    assert any(s["account_id"] == acc.account_id for s in stats)
    s = next(s for s in stats if s["account_id"] == acc.account_id)
    assert "status" in s
    assert "next_run_at" in s
    assert "success_pct" in s


def test_remove_account_cleans_up(tmp_db):
    acc = _cfg(2)
    store_credential(acc.account_id, "tok")
    mgr = SchedulerManager(db=tmp_db, max_workers=2,
                            adapter_factory=lambda _: MockAdapter())
    mgr.start()
    mgr.add_or_update_account(acc, "tok")
    mgr.remove_account(acc.account_id)
    time.sleep(0.2)

    mgr.shutdown(timeout=3)
    clear_all()

    assert tmp_db.get_account(acc.account_id) is None


def test_start_all_stop_all(tmp_db):
    accounts = [_cfg(i + 10) for i in range(3)]
    for a in accounts:
        store_credential(a.account_id, "tok")

    mgr = SchedulerManager(db=tmp_db, max_workers=5,
                            adapter_factory=lambda _: MockAdapter())
    mgr.start()
    for a in accounts:
        mgr.add_or_update_account(a, "tok")

    mgr.start_all()
    time.sleep(0.3)
    mgr.stop_all()
    time.sleep(0.2)

    for a in accounts:
        job = mgr.get_job_for_account(a.account_id)
        assert job is not None
        assert job.status in (JobStatus.STOPPED, JobStatus.WAITING, JobStatus.RUNNING)

    mgr.shutdown(timeout=5)
    clear_all()
