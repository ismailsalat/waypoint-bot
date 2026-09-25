"""
SchedulerManager — top-level orchestrator.

Responsibilities
----------------
* Load / persist jobs from SQLite
* Tick every SCHEDULER_TICK_INTERVAL seconds
* Submit due jobs to WorkerPool
* Expose health / statistics
* Graceful shutdown

Thread model
------------
  Main thread  →  GUI
  Tick thread  →  SchedulerManager._tick_loop()
  Worker pool  →  ThreadPoolExecutor (bounded)
  Job threads  →  spawned by pool, at most MAX_CONCURRENT_JOBS at once
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from concurrent.futures import wait as _futures_wait, ALL_COMPLETED
from typing import Callable, Dict, List, Optional

from app.adapters.base import AdapterBase
from app.adapters.mock import MockAdapter
from app.adapters.official_bot import OfficialBotAdapter
from app.config.settings import (
    MAX_CONCURRENT_JOBS, MAX_CONSECUTIVE_FAILURES,
    SCHEDULER_TICK_INTERVAL, SHUTDOWN_TIMEOUT,
)
from app.scheduler.models import AccountConfig, Job, JobStatus
from app.scheduler.workers import WorkerPool
from app.services.credentials import get_credential, store_credential
from app.services.logging_service import get_logger
from app.storage.database import Database

log = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SchedulerManager:

    def __init__(
        self,
        db: Database,
        max_workers: int = MAX_CONCURRENT_JOBS,
        adapter_factory: Optional[Callable[[str], AdapterBase]] = None,
        on_status_change: Optional[Callable[[str, str], None]] = None,
        on_log: Optional[Callable[[str, str, str], None]] = None,
    ):
        self._db              = db
        self._adapter_factory = adapter_factory or self._default_adapter
        self._on_status       = on_status_change or (lambda jid, s: None)
        self._on_log          = on_log or (lambda jid, lvl, msg: None)

        self._jobs:     Dict[str, Job]           = {}
        self._configs:  Dict[str, AccountConfig] = {}
        self._adapters: Dict[str, AdapterBase]   = {}
        self._lock      = threading.RLock()
        self._stop_evt  = threading.Event()
        self._tick_thread: Optional[threading.Thread] = None
        self._running   = False

        self._pool = WorkerPool(
            db=db,
            max_workers=max_workers,
            on_status_change=self._on_job_status,
            on_log=self._on_job_log,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_evt.clear()
        self._restore_state()
        self._tick_thread = threading.Thread(
            target=self._tick_loop, name="scheduler-tick", daemon=True
        )
        self._tick_thread.start()
        log.info("SchedulerManager started (max_workers=%d)", self._pool._max_workers)

    def shutdown(self, timeout: float = SHUTDOWN_TIMEOUT) -> None:
        if not self._running:
            return
        log.info("SchedulerManager shutting down (timeout=%.0fs)...", timeout)
        self._running = False
        self._stop_evt.set()
        deadline = time.monotonic() + timeout

        # Interrupt all sleeping jobs immediately
        with self._lock:
            for job_id in list(self._jobs):
                self._pool.interrupt(job_id)

        # Wait for tick thread (budget: 3 s or remaining timeout)
        if self._tick_thread:
            tick_budget = min(3.0, max(0.1, deadline - time.monotonic()))
            self._tick_thread.join(timeout=tick_budget)
            if self._tick_thread.is_alive():
                log.warning("Tick thread did not stop within %.1fs", tick_budget)

        # Shutdown pool within remaining timeout budget
        pool_budget = max(0.5, deadline - time.monotonic())
        self._pool.shutdown(wait=True, timeout=pool_budget)
        # Disconnect adapters
        with self._lock:
            for adapter in self._adapters.values():
                try:
                    adapter.disconnect()
                except Exception:
                    pass
        # Persist final state (don't close DB — caller owns it)
        self._persist_all()
        log.info("SchedulerManager stopped cleanly")

    # ── Account / job management ──────────────────────────────────────────────

    def add_or_update_account(self, config: AccountConfig, credential: str = "") -> None:
        """Register or update an account.  Credential stored in memory only."""
        errors = config.validate()
        if errors:
            raise ValueError("; ".join(errors))

        if credential:
            store_credential(config.account_id, credential)

        self._db.upsert_account(config.to_db_dict())

        with self._lock:
            self._configs[config.account_id] = config

            existing_job = self._db.get_job_for_account(config.account_id)
            if existing_job:
                job = self._restore_job(existing_job)
            else:
                job = Job(
                    job_id=str(uuid.uuid4()),
                    account_id=config.account_id,
                    status=JobStatus.STOPPED,
                )
                self._db.upsert_job(
                    job.job_id, config.account_id,
                    status=job.status.value,
                )
            self._jobs[job.job_id] = job

            if config.account_id not in self._adapters:
                self._adapters[config.account_id] = self._adapter_factory(config.token_type)

        log.info("Account %s (%s) registered", config.account_id, config.name)

    def start_job(self, account_id: str) -> None:
        with self._lock:
            job = self._job_for_account(account_id)
            if job is None:
                raise KeyError(f"No job for account {account_id}")
            if job.status == JobStatus.SUSPENDED:
                raise RuntimeError("Job is suspended. Resume it first.")
            job.status = JobStatus.WAITING
            self._db.upsert_job(job.job_id, account_id, status=job.status.value)
        log.info("Job started for account %s", account_id)

    def stop_job(self, account_id: str) -> None:
        with self._lock:
            job = self._job_for_account(account_id)
            if job is None:
                return
            job.status = JobStatus.STOPPED
            self._pool.interrupt(job.job_id)
            self._db.upsert_job(job.job_id, account_id, status=job.status.value)
        log.info("Job stopped for account %s", account_id)

    def resume_job(self, account_id: str) -> None:
        with self._lock:
            job = self._job_for_account(account_id)
            if job is None:
                return
            job.resume()
            self._db.upsert_job(job.job_id, account_id,
                                 status=job.status.value,
                                 consecutive_failures=0)
        log.info("Job resumed for account %s", account_id)

    def remove_account(self, account_id: str) -> None:
        self.stop_job(account_id)
        self._db.delete_account(account_id)
        with self._lock:
            self._configs.pop(account_id, None)
            job = self._job_for_account(account_id)
            if job:
                self._jobs.pop(job.job_id, None)
            self._adapters.pop(account_id, None)

    def start_all(self) -> None:
        with self._lock:
            account_ids = list(self._configs.keys())
        for aid in account_ids:
            try:
                self.start_job(aid)
            except Exception as e:
                log.warning("Could not start %s: %s", aid, e)

    def stop_all(self) -> None:
        with self._lock:
            account_ids = list(self._configs.keys())
        for aid in account_ids:
            self.stop_job(aid)

    # ── Tick loop ─────────────────────────────────────────────────────────────

    def _tick_loop(self) -> None:
        log.debug("Tick loop started")
        while not self._stop_evt.wait(timeout=SCHEDULER_TICK_INTERVAL):
            try:
                self._tick()
            except Exception as exc:
                log.exception("Tick error: %s", exc)
        log.debug("Tick loop exited")

    def _tick(self) -> None:
        with self._lock:
            due_jobs = [
                (job, self._configs.get(job.account_id))
                for job in self._jobs.values()
                if job.status == JobStatus.WAITING and job.is_due()
                and job.account_id in self._configs
            ]

        for job, config in due_jobs:
            if config is None:
                continue
            adapter = self._adapters.get(config.account_id)
            if adapter is None:
                continue
            if not get_credential(config.account_id):
                log.warning("No credential for %s — skipping tick", config.account_id)
                continue
            submitted = self._pool.submit(job, config, adapter)
            if submitted:
                with self._lock:
                    job.status = JobStatus.RUNNING
                log.debug("Submitted job %s for account %s", job.job_id[:8], config.name)

        # Periodic history cleanup
        self._db.purge_old_runs()

    # ── State persistence / restore ───────────────────────────────────────────

    def _restore_state(self) -> None:
        accounts = self._db.list_accounts()
        log.info("Restoring %d account(s) from database", len(accounts))
        for a in accounts:
            config = AccountConfig.from_db_dict(a)
            self._configs[config.account_id] = config
            self._adapters[config.account_id] = self._adapter_factory(config.token_type)

            row = self._db.get_job_for_account(config.account_id)
            if row:
                job = self._restore_job(row)
                # Restore WAITING state if it was running/waiting before restart
                if row.get("status") in (JobStatus.WAITING.value, JobStatus.RUNNING.value):
                    job.status = JobStatus.WAITING
                    log.info("Restored job %s as WAITING (was %s)",
                             job.job_id[:8], row.get("status"))
            else:
                job = Job(job_id=str(uuid.uuid4()),
                          account_id=config.account_id,
                          status=JobStatus.STOPPED)
                self._db.upsert_job(job.job_id, config.account_id,
                                    status=job.status.value)

            self._jobs[job.job_id] = job

    def _restore_job(self, row: dict) -> Job:
        job                      = Job()
        job.job_id               = row["job_id"]
        job.account_id           = row["account_id"]
        job.status               = JobStatus(row.get("status", "STOPPED"))
        job.next_run_at          = _parse_iso(row.get("next_run_at"))
        job.last_run_at          = _parse_iso(row.get("last_run_at"))
        job.last_success_at      = _parse_iso(row.get("last_success_at"))
        job.last_failure_at      = _parse_iso(row.get("last_failure_at"))
        job.consecutive_failures = int(row.get("consecutive_failures", 0))
        job.total_runs           = int(row.get("total_runs", 0))
        job.total_successes      = int(row.get("total_successes", 0))
        job.total_failures       = int(row.get("total_failures", 0))
        job.avg_duration_ms      = float(row.get("avg_duration_ms") or 0)
        return job

    def _persist_all(self) -> None:
        with self._lock:
            for job in self._jobs.values():
                # Sync from DB first to avoid overwriting with stale in-memory zeros/nulls
                row = self._db.get_job(job.job_id)
                if row:
                    total_runs      = max(job.total_runs,      int(row.get("total_runs", 0)))
                    total_successes = max(job.total_successes,  int(row.get("total_successes", 0)))
                    total_failures  = max(job.total_failures,   int(row.get("total_failures", 0)))
                    # Keep DB next_run_at if memory has None (job never ran this session)
                    next_run_at = _iso(job.next_run_at) or row.get("next_run_at")
                else:
                    total_runs      = job.total_runs
                    total_successes = job.total_successes
                    total_failures  = job.total_failures
                    next_run_at     = _iso(job.next_run_at)
                self._db.upsert_job(
                    job.job_id, job.account_id,
                    status=job.status.value,
                    next_run_at=next_run_at,
                    last_run_at=_iso(job.last_run_at) or (row.get("last_run_at") if row else None),
                    consecutive_failures=job.consecutive_failures,
                    total_runs=total_runs,
                    total_successes=total_successes,
                    total_failures=total_failures,
                )

    # ── Callbacks from WorkerPool ─────────────────────────────────────────────

    def _on_job_status(self, job_id: str, status: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                try:
                    job.status = JobStatus(status)
                except ValueError:
                    pass
                # Sync stats from DB so in-memory job reflects reality
                row = self._db.get_job(job_id)
                if row:
                    job.consecutive_failures = int(row.get("consecutive_failures", 0))
                    job.total_runs           = int(row.get("total_runs", 0))
                    job.total_successes      = int(row.get("total_successes", 0))
                    job.total_failures       = int(row.get("total_failures", 0))
                    job.avg_duration_ms      = float(row.get("avg_duration_ms") or 0)
                    job.last_run_at          = _parse_iso(row.get("last_run_at"))
                    job.last_success_at      = _parse_iso(row.get("last_success_at"))
                    job.last_failure_at      = _parse_iso(row.get("last_failure_at"))
                    job.next_run_at          = _parse_iso(row.get("next_run_at"))
        self._on_status(job_id, status)

    def _on_job_log(self, job_id: str, level: str, msg: str) -> None:
        self._on_log(job_id, level, msg)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _job_for_account(self, account_id: str) -> Optional[Job]:
        for job in self._jobs.values():
            if job.account_id == account_id:
                return job
        return None

    @staticmethod
    def _default_adapter(token_type: str) -> AdapterBase:
        if token_type == "user":
            from app.adapters.user_token import UserTokenAdapter
            return UserTokenAdapter()
        return OfficialBotAdapter()

    # ── Statistics / health ───────────────────────────────────────────────────

    def health(self) -> dict:
        with self._lock:
            jobs = list(self._jobs.values())
        return {
            "running":      self._running,
            "database":     self._db.health(),
            "workers":      self._pool.active_count(),
            "max_workers":  self._pool._max_workers,
            "queued_jobs":  sum(1 for j in jobs if j.status == JobStatus.WAITING),
            "active_jobs":  sum(1 for j in jobs if j.status == JobStatus.RUNNING),
            "failed_jobs":  sum(1 for j in jobs if j.status == JobStatus.FAILED),
            "suspended_jobs": sum(1 for j in jobs if j.status == JobStatus.SUSPENDED),
        }

    def stats(self) -> List[dict]:
        """Per-account statistics for the GUI."""
        result = []
        with self._lock:
            for config in self._configs.values():
                job = self._job_for_account(config.account_id)
                if not job:
                    continue
                result.append({
                    "account_id":          config.account_id,
                    "name":                config.name,
                    "status":              job.status.value,
                    "next_run_at":         _iso(job.next_run_at),
                    "last_run_at":         _iso(job.last_run_at),
                    "last_success_at":     _iso(job.last_success_at),
                    "consecutive_failures": job.consecutive_failures,
                    "total_runs":          job.total_runs,
                    "total_successes":     job.total_successes,
                    "total_failures":      job.total_failures,
                    "success_pct":         (
                        round(job.total_successes / job.total_runs * 100, 1)
                        if job.total_runs else 0.0
                    ),
                    "avg_duration_ms":     round(job.avg_duration_ms, 1),
                    "job_id":              job.job_id,
                })
        return result

    def get_job_for_account(self, account_id: str) -> Optional[Job]:
        with self._lock:
            return self._job_for_account(account_id)
