"""
test_restart_recovery.py
-------------------------
KEY TEST: Restart SchedulerManager with same DB and verify state restored.
"""
from __future__ import annotations
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from app.scheduler.manager import SchedulerManager
from app.scheduler.models import AccountConfig, JobStatus
from app.services.credentials import store_credential, clear_all
from app.storage.database import Database
from app.adapters.mock import MockAdapter


def test_job_state_survives_restart(tmp_path):
    db_path = tmp_path / "restart_test.db"
    store_credential("acct-rst-01", "tok")

    cfg = AccountConfig(
        account_id="acct-rst-01", name="Restart Test",
        guild_id="111111111111111111", channel_id="222222222222222222",
        cooldown_minutes=5, offset_max_minutes=0,
    )

    # ── First run ────────────────────────────────────────────────────
    db1 = Database(db_path)
    mgr1 = SchedulerManager(db=db1, max_workers=2,
                             adapter_factory=lambda _: MockAdapter())
    mgr1.start()
    mgr1.add_or_update_account(cfg, "tok")
    mgr1.start_job(cfg.account_id)

    job1 = mgr1.get_job_for_account(cfg.account_id)
    assert job1 is not None

    # Simulate a successful run by directly writing state
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    future_iso = future.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    db1.upsert_job(
        job1.job_id, cfg.account_id,
        status=JobStatus.WAITING.value,
        next_run_at=future_iso,
        total_runs=3, total_successes=3,
    )
    mgr1.shutdown(timeout=3)
    db1.close()

    # ── Second run (restart) ─────────────────────────────────────────
    store_credential("acct-rst-01", "tok")  # re-supply credential
    db2 = Database(db_path)
    mgr2 = SchedulerManager(db=db2, max_workers=2,
                             adapter_factory=lambda _: MockAdapter())
    mgr2.start()

    time.sleep(0.3)  # let restore complete

    job2 = mgr2.get_job_for_account(cfg.account_id)
    assert job2 is not None, "Job not restored after restart"
    assert job2.total_runs == 3, f"total_runs not restored: {job2.total_runs}"
    assert job2.next_run_at is not None, "next_run_at not restored"
    assert job2.next_run_at.replace(tzinfo=timezone.utc).timestamp() > \
           datetime.now(timezone.utc).timestamp(), \
           "next_run_at should be in the future"

    mgr2.shutdown(timeout=3)
    db2.close()
    clear_all()


def test_overdue_jobs_not_mass_fired(tmp_path):
    """
    If 10 accounts all have overdue next_run_at on startup,
    they should be picked up by the tick — but NOT all fired simultaneously
    beyond the worker pool size.
    """
    db_path = tmp_path / "overdue.db"
    db = Database(db_path)

    accounts = []
    for i in range(10):
        cfg = AccountConfig(
            account_id=f"overdue-{i:02d}", name=f"OD {i}",
            guild_id=str(100000000 + i), channel_id=str(200000000 + i),
            cooldown_minutes=0.01, offset_max_minutes=0,
        )
        store_credential(cfg.account_id, "tok")
        db.upsert_account(cfg.to_db_dict())
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        import uuid
        jid = str(uuid.uuid4())
        db.upsert_job(jid, cfg.account_id, status="WAITING", next_run_at=past)
        accounts.append(cfg)

    peak_active = []

    def factory(_):
        return MockAdapter(latency_ms=200)

    mgr = SchedulerManager(db=db, max_workers=3, adapter_factory=factory)
    mgr.start()

    for _ in range(20):
        time.sleep(0.1)
        peak_active.append(mgr._pool.active_count())

    mgr.shutdown(timeout=5)
    db.close()
    clear_all()

    assert max(peak_active) <= 3, \
        f"Pool exceeded max_workers=3: peak={max(peak_active)}"
