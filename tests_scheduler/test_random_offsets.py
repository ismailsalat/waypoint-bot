"""
test_random_offsets.py
-----------------------
KEY TEST: Randomised delay is recalculated EVERY cycle (not reused).
"""
from __future__ import annotations
import time
import pytest

from app.adapters.mock import MockAdapter
from app.scheduler.models import AccountConfig
from app.scheduler.workers import WorkerPool
from app.services.credentials import store_credential, clear_all


def _acc(cooldown_min=0.05, offset_min=0.02):
    return AccountConfig(
        account_id="rand-test-01",
        name="Rand Test",
        guild_id="111111111111111111",
        channel_id="222222222222222222",
        cooldown_minutes=cooldown_min,
        offset_max_minutes=offset_min,
    )


def test_next_run_recalculated_each_cycle(tmp_db):
    """
    Run at least 3 scheduling cycles and confirm that next_run_at
    is different for each cycle (not a fixed repeated value).
    """
    import threading
    from app.scheduler.models import Job, JobStatus

    next_runs = []

    def capture_status(job_id, status):
        pass

    def capture_log(job_id, lvl, msg):
        pass

    pool = WorkerPool(
        db=tmp_db,
        max_workers=2,
        on_status_change=capture_status,
        on_log=capture_log,
    )

    acc = _acc()
    store_credential(acc.account_id, "fake")

    # Patch _schedule_next to capture values
    original = pool._schedule_next

    def capturing_schedule_next(job, config):
        original(job, config)
        next_runs.append(job.next_run_at)

    pool._schedule_next = capturing_schedule_next

    adapter = MockAdapter()
    tmp_db.upsert_account(acc.to_db_dict())
    tmp_db.upsert_job("job-rand-01", acc.account_id, status="WAITING")
    from app.scheduler.models import Job, JobStatus
    job = Job(job_id="job-rand-01", account_id=acc.account_id, status=JobStatus.WAITING)

    # Run 3 cycles
    for _ in range(3):
        evt = threading.Event()
        pool.submit(job, acc, adapter)
        time.sleep(0.5)  # enough for MockAdapter to complete

    pool.shutdown(wait=True)
    clear_all()

    assert len(next_runs) >= 2, f"Expected ≥2 scheduled times, got {len(next_runs)}"
    # All scheduled times should be different
    unique = set(str(t) for t in next_runs)
    assert len(unique) > 1, (
        f"next_run_at was the SAME for every cycle: {next_runs}. "
        "Random offset is not being recalculated."
    )


def test_offset_within_configured_range(tmp_db):
    """Calculated delay must fall within [base, base+offset] seconds."""
    import random
    from app.scheduler.workers import WorkerPool
    from app.scheduler.models import Job, JobStatus
    from datetime import datetime, timezone, timedelta

    acc    = _acc(cooldown_min=2.0, offset_min=1.0)
    pool   = WorkerPool(db=tmp_db, max_workers=1)
    job    = Job(job_id="job-range-01", account_id=acc.account_id)
    delays = []

    for _ in range(20):
        before = datetime.now(timezone.utc)
        pool._schedule_next(job, acc)
        after = datetime.now(timezone.utc)
        delta = (job.next_run_at - before).total_seconds()
        delays.append(delta)

    pool.shutdown(wait=False)

    base   = acc.cooldown_minutes * 60
    offset = acc.offset_max_minutes * 60
    for d in delays:
        assert base <= d <= base + offset + 1, \
            f"Delay {d:.2f}s outside [{base},{base+offset}]"

    # Spread must exceed 0.5 s (randomness confirmed)
    spread = max(delays) - min(delays)
    assert spread > 0.5, f"All delays too similar (spread={spread:.2f}s) — offset may be zero"
