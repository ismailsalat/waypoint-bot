"""
Thread-safe SQLite storage layer.

Schema
------
  schema_version  — single-row version table for migrations
  accounts        — persistent account config (NO credentials)
  jobs            — scheduler job state per account
  job_runs        — history of every execution attempt
  operations      — idempotency / dedup table (UUID per operation)
  settings        — key-value application settings
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from app.config.settings import DB_PATH, DB_SCHEMA_VERSION, JOB_HISTORY_RETENTION_DAYS
from app.services.logging_service import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _new_uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Database:
    """
    Single SQLite connection per instance, protected by a reentrant lock.
    Use one instance per process (singleton via get_db()).
    """

    def __init__(self, path: Path = DB_PATH):
        self._path = path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()
        self._init_schema()

    # ── Connection ────────────────────────────────────────────────────────────

    def _connect(self) -> None:
        path = str(self._path)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        log.debug("SQLite connected: %s", path)

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager that yields a cursor inside a transaction."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._tx() as c:
            # Version table
            c.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version  INTEGER PRIMARY KEY
                )
            """)
            row = c.execute("SELECT version FROM schema_version").fetchone()
            current = row["version"] if row else 0

            if current < 1:
                self._migrate_v1(c)
                c.execute("DELETE FROM schema_version")
                c.execute("INSERT INTO schema_version VALUES (?)", (DB_SCHEMA_VERSION,))
                log.info("Database schema initialised at version %d", DB_SCHEMA_VERSION)

    def _migrate_v1(self, c: sqlite3.Cursor) -> None:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id       TEXT PRIMARY KEY,
                name             TEXT NOT NULL,
                token_type       TEXT NOT NULL DEFAULT 'bot',
                guild_id         TEXT NOT NULL DEFAULT '',
                channel_id       TEXT NOT NULL DEFAULT '',
                bump_message     TEXT NOT NULL DEFAULT 'Application bump completed.',
                cooldown_minutes REAL NOT NULL DEFAULT 120,
                offset_max_minutes REAL NOT NULL DEFAULT 30,
                enabled          INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id               TEXT PRIMARY KEY,
                account_id           TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
                status               TEXT NOT NULL DEFAULT 'STOPPED',
                next_run_at          TEXT,
                last_run_at          TEXT,
                last_success_at      TEXT,
                last_failure_at      TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                total_runs           INTEGER NOT NULL DEFAULT 0,
                total_successes      INTEGER NOT NULL DEFAULT 0,
                total_failures       INTEGER NOT NULL DEFAULT 0,
                avg_duration_ms      REAL,
                updated_at           TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_next_run ON jobs(next_run_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_account  ON jobs(account_id);

            CREATE TABLE IF NOT EXISTS job_runs (
                run_id        TEXT PRIMARY KEY,
                job_id        TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                account_id    TEXT NOT NULL,
                operation_id  TEXT NOT NULL,
                status        TEXT NOT NULL,
                attempt       INTEGER NOT NULL DEFAULT 1,
                started_at    TEXT NOT NULL,
                finished_at   TEXT,
                duration_ms   REAL,
                error_code    TEXT,
                error_message TEXT,
                result_data   TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_runs_job     ON job_runs(job_id);
            CREATE INDEX IF NOT EXISTS idx_runs_started ON job_runs(started_at);

            CREATE TABLE IF NOT EXISTS operations (
                operation_id  TEXT PRIMARY KEY,
                job_id        TEXT NOT NULL,
                account_id    TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'PENDING',
                created_at    TEXT NOT NULL,
                completed_at  TEXT,
                external_id   TEXT,
                result_data   TEXT,
                UNIQUE(operation_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

    # ── Account CRUD ──────────────────────────────────────────────────────────

    def upsert_account(self, data: dict) -> None:
        now = _utcnow()
        with self._tx() as c:
            c.execute("""
                INSERT INTO accounts
                    (account_id, name, token_type, guild_id, channel_id,
                     bump_message, cooldown_minutes, offset_max_minutes, enabled,
                     created_at, updated_at)
                VALUES
                    (:account_id,:name,:token_type,:guild_id,:channel_id,
                     :bump_message,:cooldown_minutes,:offset_max_minutes,:enabled,
                     :created_at,:updated_at)
                ON CONFLICT(account_id) DO UPDATE SET
                    name=excluded.name, token_type=excluded.token_type,
                    guild_id=excluded.guild_id, channel_id=excluded.channel_id,
                    bump_message=excluded.bump_message,
                    cooldown_minutes=excluded.cooldown_minutes,
                    offset_max_minutes=excluded.offset_max_minutes,
                    enabled=excluded.enabled, updated_at=excluded.updated_at
            """, {**data, "created_at": now, "updated_at": now})

    def list_accounts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]

    def get_account(self, account_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            return dict(row) if row else None

    def delete_account(self, account_id: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM accounts WHERE account_id=?", (account_id,))

    # ── Job CRUD ──────────────────────────────────────────────────────────────

    def upsert_job(self, job_id: str, account_id: str, **kwargs) -> None:
        now = _utcnow()
        with self._tx() as c:
            existing = c.execute(
                "SELECT job_id FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if existing:
                sets = ", ".join(f"{k}=:{k}" for k in kwargs)
                c.execute(
                    f"UPDATE jobs SET {sets}, updated_at=:updated_at WHERE job_id=:job_id",
                    {**kwargs, "updated_at": now, "job_id": job_id},
                )
            else:
                cols = ["job_id", "account_id", "updated_at"] + list(kwargs)
                vals = [":" + x for x in cols]
                c.execute(
                    f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join(vals)})",
                    {"job_id": job_id, "account_id": account_id,
                     "updated_at": now, **kwargs},
                )

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_job_for_account(self, account_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE account_id=?", (account_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_enabled_jobs(self) -> list[dict]:
        """Return all jobs whose account is enabled."""
        with self._lock:
            rows = self._conn.execute("""
                SELECT j.* FROM jobs j
                JOIN accounts a ON a.account_id = j.account_id
                WHERE a.enabled = 1
                ORDER BY j.next_run_at ASC NULLS FIRST
            """).fetchall()
            return [dict(r) for r in rows]

    # ── Job run history ───────────────────────────────────────────────────────

    def insert_run(self, run: dict) -> None:
        with self._tx() as c:
            c.execute("""
                INSERT OR IGNORE INTO job_runs
                    (run_id, job_id, account_id, operation_id, status, attempt,
                     started_at, finished_at, duration_ms, error_code, error_message, result_data)
                VALUES
                    (:run_id,:job_id,:account_id,:operation_id,:status,:attempt,
                     :started_at,:finished_at,:duration_ms,:error_code,:error_message,:result_data)
            """, {
                "run_id": _new_uuid(),
                "finished_at": None, "duration_ms": None,
                "error_code": None, "error_message": None, "result_data": None,
                **run,
            })

    def update_run(self, operation_id: str, **kwargs) -> None:
        with self._tx() as c:
            sets = ", ".join(f"{k}=:{k}" for k in kwargs)
            c.execute(
                f"UPDATE job_runs SET {sets} WHERE operation_id=:operation_id",
                {**kwargs, "operation_id": operation_id},
            )

    def list_runs(self, job_id: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM job_runs WHERE job_id=? ORDER BY started_at DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def purge_old_runs(self) -> int:
        cutoff = _utcnow()[:10]  # date portion only; crude but sufficient
        with self._tx() as c:
            c.execute(
                f"DELETE FROM job_runs WHERE started_at < date('now','-{JOB_HISTORY_RETENTION_DAYS} days')"
            )
            return c.rowcount

    # ── Operations (idempotency) ──────────────────────────────────────────────

    def create_operation(self, operation_id: str, job_id: str, account_id: str) -> bool:
        """Returns False if operation_id already exists (duplicate guard)."""
        try:
            with self._tx() as c:
                c.execute("""
                    INSERT INTO operations (operation_id, job_id, account_id, status, created_at)
                    VALUES (?,?,?,'PENDING',?)
                """, (operation_id, job_id, account_id, _utcnow()))
            return True
        except sqlite3.IntegrityError:
            return False

    def complete_operation(self, operation_id: str, status: str,
                           external_id: Optional[str] = None,
                           result_data: Optional[str] = None) -> None:
        with self._tx() as c:
            c.execute("""
                UPDATE operations SET status=?, completed_at=?, external_id=?, result_data=?
                WHERE operation_id=?
            """, (status, _utcnow(), external_id, result_data, operation_id))

    def operation_exists(self, operation_id: str) -> bool:
        """Return True only if this operation_id exists AND is completed (VERIFIED)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM operations "
                "WHERE operation_id=? AND status IN ('VERIFIED','SENT')",
                (operation_id,)
            ).fetchone()
            return row is not None

    # ── Settings ──────────────────────────────────────────────────────────────

    def set_setting(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                (key, value),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else default

    # ── Health ────────────────────────────────────────────────────────────────

    def health(self) -> str:
        try:
            with self._lock:
                self._conn.execute("SELECT 1")
            return "healthy"
        except Exception as e:
            return f"unhealthy: {e}"

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None


# ── Module-level singleton ────────────────────────────────────────────────────

_db_instance: Optional[Database] = None
_db_lock = threading.Lock()


def get_db(path: Path = DB_PATH) -> Database:
    global _db_instance
    with _db_lock:
        if _db_instance is None:
            _db_instance = Database(path)
    return _db_instance


def reset_db() -> None:
    """For testing only — resets singleton."""
    global _db_instance
    with _db_lock:
        if _db_instance:
            _db_instance.close()
        _db_instance = None
