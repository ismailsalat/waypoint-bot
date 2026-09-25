"""
Central configuration constants.
All tunable values live here so they can be changed in one place.
"""
from __future__ import annotations
import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent.parent.parent   # project root
DATA_DIR  = BASE_DIR / "data"
LOG_DIR   = BASE_DIR / "logs"
DB_PATH   = DATA_DIR / "scheduler.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Scheduler ─────────────────────────────────────────────────────────────────
MAX_CONCURRENT_JOBS       = int(os.getenv("MAX_CONCURRENT_JOBS", "5"))
MAX_CONSECUTIVE_FAILURES  = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "5"))
SCHEDULER_TICK_INTERVAL   = 5          # seconds between scheduler wakeups
SHUTDOWN_TIMEOUT          = 15         # seconds to wait for clean shutdown

# ── Retry ─────────────────────────────────────────────────────────────────────
RETRY_BASE_DELAY          = 2.0        # seconds
RETRY_MAX_DELAY           = 300.0      # seconds (5 min cap)
RETRY_MAX_ATTEMPTS        = 5
RETRY_JITTER_FACTOR       = 0.3        # ± 30 % jitter

# ── Cooldown defaults ─────────────────────────────────────────────────────────
DEFAULT_COOLDOWN_MINUTES  = 120
DEFAULT_OFFSET_MAX_MINUTES = 30

# ── History / audit ───────────────────────────────────────────────────────────
JOB_HISTORY_RETENTION_DAYS = 30

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_MAX_BYTES    = 5 * 1024 * 1024    # 5 MB per file
LOG_BACKUP_COUNT = 3

# ── Database ──────────────────────────────────────────────────────────────────
DB_SCHEMA_VERSION = 1
