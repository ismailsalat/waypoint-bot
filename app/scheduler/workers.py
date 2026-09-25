"""
Bounded worker pool for job execution.

Architecture
------------
  SchedulerManager  →  WorkerPool.submit(job)
                               ↓
                     ThreadPoolExecutor (max_workers=MAX_CONCURRENT_JOBS)
                               ↓
                         _execute_job(job, ...)
                               ↓
                         adapter.execute()
                               ↓
                         database update + next_run schedule

Key properties
--------------
* Fixed thread pool — NOT one thread per account.
* Per-job lock prevents overlapping executions of the same account.
* Interruptible sleep via threading.Event.
* Secrets never logged.
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait as _futures_wait
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from app.adapters.base import AdapterBase, AdapterError
from app.config.settings import MAX_CONCURRENT_JOBS, MAX_CONSECUTIVE_FAILURES
from app.scheduler.models import (
    AccountConfig, ErrorCategory, Job, JobStatus, OperationResult, OperationStatus,
)
from app.scheduler.retry import RetryPolicy
from app.services.logging_service import get_logger
from app.storage.database import Database

log = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class WorkerPool:
    """
    Fixed ThreadPoolExecutor wrapper.
    Enforces one-in-flight-per-job via per-job locks.
    """

    def __init__(
        self,
        db: Database,
        max_workers: int = MAX_CONCURRENT_JOBS,
        on_status_change: Optional[Callable[[str, str], None]] = None,
        on_log: Optional[Callable[[str, str, str], None]] = None,
    ):
        self._db              = db
        self._executor        = ThreadPoolExecutor(max_workers=max_workers,
                                                   thread_name_prefix="bumper")
        self._max_workers     = max_workers
        self._job_locks:  Dict[str, threading.Lock] = {}
        self._job_events: Dict[str, threading.Event] = {}  # for interruptible waits
        self._active:     Dict[str, Future] = {}
        self._pool_lock   = threading.Lock()
        self._policy      = RetryPolicy()
        self._shutdown    = False
        self._on_status   = on_status_change or (lambda jid, s: None)
        self._on_log      = on_log or (lambda jid, lvl, msg: None)

    # ── Public ────────────────────────────────────────────────────────────────

    def submit(self, job: Job, config: AccountConfig,
               adapter: AdapterBase) -> bool:
        """
        Submit a job for execution.
        Returns False if the job is already running or pool is shut down.
        """
        if self._shutdown:
            return False
        with self._pool_lock:
            if job.job_id in self._active:
                log.debug("Job %s already in-flight, skipping", job.job_id)
                return False
            lock  = self._job_locks.setdefault(job.job_id, threading.Lock())
            event = self._job_events.setdefault(job.job_id, threading.Event())
            event.clear()
            future = self._executor.submit(
                self._run, job, config, adapter, lock, event
            )
            self._active[job.job_id] = future
            future.add_done_callback(
                lambda f, jid=job.job_id: self._on_done(jid, f)
            )
        return True

    def interrupt(self, job_id: str) -> None:
        """Wake any sleeping retry/wait for this job."""
        with self._pool_lock:
            ev = self._job_events.get(job_id)
        if ev:
            ev.set()

    def active_count(self) -> int:
        with self._pool_lock:
            return len(self._active)

    def shutdown(self, wait: bool = True, timeout: float = 15.0) -> None:
        self._shutdown = True
        # Wake all sleeping jobs immediately
        with self._pool_lock:
            for ev in self._job_events.values():
                ev.set()
        if wait and timeout > 0:
            # Collect active futures and wait with a real deadline
            with self._pool_lock:
                futures = list(self._active.values())
            if futures:
                _futures_wait(futures, timeout=timeout)
                still = [f for f in futures if not f.done()]
                if still:
                    log.warning("Pool shutdown: %d task(s) still running after %.1fs timeout",
                                len(still), timeout)
        self._executor.shutdown(wait=False)   # don't block again

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_done(self, job_id: str, future: Future) -> None:
        with self._pool_lock:
            self._active.pop(job_id, None)
        exc = future.exception()
        if exc:
            log.error("Job %s raised unhandled exception: %s", job_id, exc)

    def _run(self, job: Job, config: AccountConfig,
             adapter: AdapterBase, lock: threading.Lock,
             stop_event: threading.Event) -> None:
        """Entry point for each worker thread."""
        if not lock.acquire(blocking=False):
            log.debug("Job %s: could not acquire lock, skipping", job.job_id)
            return
        try:
            self._execute_with_retry(job, config, adapter, stop_event)
        except Exception as exc:
            log.exception("Job %s: unhandled error in worker: %s", job.job_id, exc)
            self._update_job_failure(job, "WORKER_ERROR", str(exc))
        finally:
            lock.release()

    def _execute_with_retry(self, job: Job, config: AccountConfig,
                             adapter: AdapterBase,
                             stop_event: threading.Event) -> None:
        operation_id = str(uuid.uuid4())

        # Duplicate guard
        if not self._db.create_operation(operation_id, job.job_id, config.account_id):
            log.warning("Operation %s already exists, skipping", operation_id)
            return

        self._log(job.job_id, "INFO",
                  f"Starting op={operation_id[:8]} account={config.name}")
        self._set_status(job.job_id, JobStatus.RUNNING)

        # Connect if needed
        if not adapter.is_connected():
            cred = self._get_credential(config.account_id)
            if not cred:
                self._log(job.job_id, "ERROR", "No credential found — cannot start")
                self._update_job_failure(job, "NO_CREDENTIAL", "Missing token")
                self._db.complete_operation(operation_id, OperationStatus.FAILED.value)
                return
            try:
                adapter.connect(cred, config.guild_id, config.channel_id)
            except AdapterError as e:
                self._log(job.job_id, "ERROR", f"Connect failed: {e.code}")
                self._update_job_failure(job, e.code, str(e))
                self._db.complete_operation(operation_id, OperationStatus.FAILED.value)
                return

        # Start run record
        started_at = _utcnow()
        self._db.insert_run({
            "job_id":       job.job_id,
            "account_id":   config.account_id,
            "operation_id": operation_id,
            "status":       OperationStatus.RUNNING.value,
            "attempt":      1,
            "started_at":   _iso(started_at),
        })

        result: Optional[OperationResult] = None
        last_error: Optional[AdapterError] = None

        for attempt in range(1, self._policy.max_attempts + 2):
            if stop_event.is_set() or self._shutdown:
                self._log(job.job_id, "INFO", "Interrupted — stopping")
                self._db.complete_operation(operation_id, OperationStatus.FAILED.value)
                self._set_status(job.job_id, JobStatus.STOPPED)
                return

            # Rate-limit check
            rl = adapter.rate_limit_state()
            if rl.is_active():
                wait = rl.retry_after
                self._log(job.job_id, "WARN",
                          f"Rate limited — waiting {wait:.1f}s")
                stop_event.wait(timeout=wait)
                continue

            try:
                t0 = time.monotonic()
                result = adapter.execute(
                    config.guild_id, config.channel_id,
                    config.bump_message, operation_id,
                )
                duration_ms = (time.monotonic() - t0) * 1000
                result.duration_ms = duration_ms
                break  # success

            except AdapterError as e:
                last_error = e
                cat = ErrorCategory(e.code) if e.code in ErrorCategory.__members__ \
                      else (ErrorCategory.TRANSIENT if e.retryable else ErrorCategory.PERMANENT)

                if not self._policy.should_retry(attempt, cat, e.retryable):
                    self._log(job.job_id, "ERROR",
                              f"Permanent failure after attempt {attempt}: {e.code}")
                    break

                delay = self._policy.next_delay(attempt, e.retry_after_ms / 1000)
                self._log(job.job_id, "WARN",
                          f"Attempt {attempt} failed ({e.code}) — retry in {delay:.1f}s")
                self._set_status(job.job_id, JobStatus.RETRYING)
                self._db.update_run(operation_id, attempt=attempt,
                                    status=OperationStatus.RETRYING.value)
                stop_event.wait(timeout=delay)

        # ── Process outcome ───────────────────────────────────────────────────
        finished_at = _utcnow()
        duration_ms = (finished_at - started_at).total_seconds() * 1000

        if result and result.success:
            # ── Verify the operation actually landed ──────────────────
            verify_status = OperationStatus.SENT
            if result.external_id and not stop_event.is_set():
                try:
                    vr = adapter.verify_result(operation_id, result.external_id)
                    if vr.verified:
                        verify_status = OperationStatus.VERIFIED
                        self._log(job.job_id, "SUCCESS",
                                  f"Verified  op={operation_id[:8]}  {result.message}")
                    else:
                        self._log(job.job_id, "WARN",
                                  f"Sent but unverified  op={operation_id[:8]}")
                except Exception as ve:
                    self._log(job.job_id, "WARN",
                              f"Verify error (treating as SENT): {ve}")
            else:
                self._log(job.job_id, "SUCCESS",
                          f"Sent  op={operation_id[:8]}  {result.message}")

            job.record_success(result.duration_ms)
            self._schedule_next(job, config)
            self._db.complete_operation(
                operation_id, verify_status.value,
                external_id=result.external_id,
            )
            self._db.update_run(
                operation_id,
                status=verify_status.value,
                finished_at=_iso(finished_at),
                duration_ms=duration_ms,
                result_data=result.message,
            )
            self._persist_job(job)
        else:
            err_code = last_error.code if last_error else "UNKNOWN"
            err_msg  = str(last_error) if last_error else "No result"
            self._log(job.job_id, "ERROR", f"Failed: {err_code} — {err_msg}")
            self._update_job_failure(job, err_code, err_msg)
            self._db.complete_operation(operation_id, OperationStatus.FAILED.value)
            self._db.update_run(
                operation_id,
                status=OperationStatus.FAILED.value,
                finished_at=_iso(finished_at),
                duration_ms=duration_ms,
                error_code=err_code,
                error_message=err_msg,
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _schedule_next(self, job: Job, config: AccountConfig) -> None:
        """Calculate a NEW randomised next_run_at after every completed cycle."""
        import random
        base_s   = config.cooldown_minutes * 60
        offset_s = random.uniform(0, config.offset_max_minutes * 60)
        total_s  = base_s + offset_s
        from datetime import timedelta
        job.next_run_at = _utcnow() + timedelta(seconds=total_s)
        log.debug("Job %s: next run in %.0f s at %s",
                  job.job_id, total_s, _iso(job.next_run_at))

    def _update_job_failure(self, job: Job, code: str, msg: str) -> None:
        job.record_failure(MAX_CONSECUTIVE_FAILURES)
        status = job.status
        self._set_status(job.job_id, status)
        if status == JobStatus.SUSPENDED:
            self._log(job.job_id, "ERROR",
                      f"SUSPENDED after {job.consecutive_failures} failures")
        self._persist_job(job)

    def _persist_job(self, job: Job) -> None:
        self._db.upsert_job(
            job.job_id, job.account_id,
            status=job.status.value,
            next_run_at=_iso(job.next_run_at),
            last_run_at=_iso(job.last_run_at),
            last_success_at=_iso(job.last_success_at),
            last_failure_at=_iso(job.last_failure_at),
            consecutive_failures=job.consecutive_failures,
            total_runs=job.total_runs,
            total_successes=job.total_successes,
            total_failures=job.total_failures,
            avg_duration_ms=job.avg_duration_ms,
        )

    def _set_status(self, job_id: str, status: JobStatus) -> None:
        self._on_status(job_id, status.value)

    def _log(self, job_id: str, level: str, msg: str) -> None:
        log.info("[%s] %s: %s", job_id[:8], level, msg)
        self._on_log(job_id, level, msg)

    def _get_credential(self, account_id: str) -> Optional[str]:
        from app.services.credentials import get_credential
        return get_credential(account_id)
