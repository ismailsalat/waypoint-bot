"""
test_shutdown.py
-----------------
KEY TEST: Stopping a job wakes it immediately instead of waiting full cooldown.
"""
from __future__ import annotations
import time
import threading
import pytest

from app.adapters.mock import MockAdapter
from app.scheduler.manager import SchedulerManager
from app.scheduler.models import AccountConfig, JobStatus
from app.services.credentials import store_credential, clear_all


def test_stop_wakes_sleeping_job(tmp_db):
    """
    Job with long cooldown should stop quickly when interrupted,
    not wait for the full cooldown.
    """
    acc = AccountConfig(
        account_id="shutdown-01", name="Shutdown Test",
        guild_id="111111111111111111", channel_id="222222222222222222",
        cooldown_minutes=10,  # 10 minutes — would block forever
        offset_max_minutes=0,
    )
    store_credential(acc.account_id, "tok")

    mgr = SchedulerManager(
        db=tmp_db, max_workers=2,
        adapter_factory=lambda _: MockAdapter(),
    )
    mgr.start()
    mgr.add_or_update_account(acc, "tok")
    mgr.start_job(acc.account_id)

    # Let it run once so it enters cooldown
    time.sleep(0.3)

    t0 = time.monotonic()
    mgr.stop_job(acc.account_id)
    elapsed = time.monotonic() - t0

    mgr.shutdown(timeout=5)
    clear_all()

    assert elapsed < 3.0, \
        f"stop_job took {elapsed:.2f}s — job was not interrupted quickly"


def test_graceful_shutdown_cleans_up(tmp_db):
    """Shutdown must not raise and must stop the tick thread."""
    acc = AccountConfig(
        account_id="shutdown-02", name="GS Test",
        guild_id="111111111111111111", channel_id="222222222222222222",
        cooldown_minutes=999, offset_max_minutes=0,
    )
    store_credential(acc.account_id, "tok")

    mgr = SchedulerManager(
        db=tmp_db, max_workers=2,
        adapter_factory=lambda _: MockAdapter(),
    )
    mgr.start()
    mgr.add_or_update_account(acc, "tok")
    mgr.start_job(acc.account_id)
    time.sleep(0.1)

    t0 = time.monotonic()
    mgr.shutdown(timeout=5)
    elapsed = time.monotonic() - t0
    clear_all()

    assert elapsed < 8.0, f"Shutdown took too long: {elapsed:.2f}s"
    assert not mgr._running, "Manager still running after shutdown"


def test_shutdown_persists_state(tmp_db):
    """After shutdown, job state should be in the database."""
    from datetime import datetime, timezone, timedelta

    acc = AccountConfig(
        account_id="shutdown-03", name="Persist Test",
        guild_id="333333333333333333", channel_id="444444444444444444",
        cooldown_minutes=999, offset_max_minutes=0,
    )
    store_credential(acc.account_id, "tok")

    mgr = SchedulerManager(
        db=tmp_db, max_workers=2,
        adapter_factory=lambda _: MockAdapter(),
    )
    mgr.start()
    mgr.add_or_update_account(acc, "tok")
    mgr.start_job(acc.account_id)
    time.sleep(0.2)
    mgr.shutdown(timeout=5)
    clear_all()

    row = tmp_db.get_job_for_account(acc.account_id)
    assert row is not None, "Job not found in DB after shutdown"
