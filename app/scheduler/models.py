"""
Domain models for the scheduler.
Pure dataclasses — no I/O, no threading.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    STOPPED   = "STOPPED"
    WAITING   = "WAITING"
    RUNNING   = "RUNNING"
    RETRYING  = "RETRYING"
    FAILED    = "FAILED"
    SUSPENDED = "SUSPENDED"


class OperationStatus(str, Enum):
    PENDING  = "PENDING"
    RUNNING  = "RUNNING"
    SENT     = "SENT"
    VERIFIED = "VERIFIED"
    FAILED   = "FAILED"
    RETRYING = "RETRYING"
    SUSPENDED = "SUSPENDED"


class ErrorCategory(str, Enum):
    TRANSIENT    = "TRANSIENT"       # network glitch – retry
    RATE_LIMIT   = "RATE_LIMIT"      # 429 – wait retry_after
    AUTH         = "AUTH"            # 401 – stop, user must fix token
    PERMISSION   = "PERMISSION"      # 403 – stop, missing perms
    NOT_FOUND    = "NOT_FOUND"       # 404 – stop, bad config
    SERVER_ERROR = "SERVER_ERROR"    # 5xx – retry
    PERMANENT    = "PERMANENT"       # anything unrecoverable


# ---------------------------------------------------------------------------
# Adapter results
# ---------------------------------------------------------------------------

@dataclass
class OperationResult:
    success:     bool
    verified:    bool        = False
    retryable:   bool        = True
    error_cat:   Optional[ErrorCategory] = None
    message:     str         = ""
    external_id: Optional[str] = None
    duration_ms: float       = 0.0


# ---------------------------------------------------------------------------
# Rate-limit state (generic — not Discord-specific)
# ---------------------------------------------------------------------------

@dataclass
class RateLimitState:
    limited:      bool         = False
    retry_after:  float        = 0.0    # seconds to wait
    scope:        str          = "global"
    reset_at:     Optional[datetime] = None

    def is_active(self) -> bool:
        if not self.limited:
            return False
        if self.reset_at and _utcnow() >= self.reset_at:
            self.limited = False
            return False
        return True


# ---------------------------------------------------------------------------
# Job (runtime representation)
# ---------------------------------------------------------------------------

@dataclass
class Job:
    job_id:       str          = field(default_factory=_new_id)
    account_id:   str          = ""
    status:       JobStatus    = JobStatus.STOPPED
    next_run_at:  Optional[datetime] = None
    last_run_at:  Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_failure_at: Optional[datetime] = None
    consecutive_failures: int  = 0
    total_runs:   int          = 0
    total_successes: int       = 0
    total_failures:  int       = 0
    avg_duration_ms: float     = 0.0
    rate_limit:   RateLimitState = field(default_factory=RateLimitState)
    _lock_held:   bool         = field(default=False, repr=False)

    def is_due(self) -> bool:
        if self.next_run_at is None:
            return True
        return _utcnow() >= self.next_run_at

    def is_suspended(self) -> bool:
        return self.status == JobStatus.SUSPENDED

    def record_success(self, duration_ms: float) -> None:
        self.consecutive_failures = 0
        self.total_runs += 1
        self.total_successes += 1
        self.last_run_at = _utcnow()
        self.last_success_at = _utcnow()
        # rolling average
        n = self.total_successes
        self.avg_duration_ms = ((self.avg_duration_ms * (n - 1)) + duration_ms) / n
        self.status = JobStatus.WAITING

    def record_failure(self, max_failures: int) -> None:
        self.consecutive_failures += 1
        self.total_runs += 1
        self.total_failures += 1
        self.last_run_at = _utcnow()
        self.last_failure_at = _utcnow()
        if self.consecutive_failures >= max_failures:
            self.status = JobStatus.SUSPENDED
        else:
            self.status = JobStatus.FAILED

    def resume(self) -> None:
        self.consecutive_failures = 0
        self.status = JobStatus.WAITING


# ---------------------------------------------------------------------------
# Account config (no credentials stored here)
# ---------------------------------------------------------------------------

@dataclass
class AccountConfig:
    account_id:           str   = field(default_factory=_new_id)
    name:                 str   = "New Account"
    token_type:           str   = "bot"          # "bot" | "user"
    guild_id:             str   = ""
    channel_id:           str   = ""
    bump_message:         str   = "Application bump completed."
    cooldown_minutes:     float = 120.0
    offset_max_minutes:   float = 30.0
    enabled:              bool  = True

    def to_db_dict(self) -> dict:
        return {
            "account_id":           self.account_id,
            "name":                 self.name,
            "token_type":           self.token_type,
            "guild_id":             self.guild_id,
            "channel_id":           self.channel_id,
            "bump_message":         self.bump_message,
            "cooldown_minutes":     self.cooldown_minutes,
            "offset_max_minutes":   self.offset_max_minutes,
            "enabled":              int(self.enabled),
        }

    @classmethod
    def from_db_dict(cls, d: dict) -> "AccountConfig":
        return cls(
            account_id=d["account_id"],
            name=d["name"],
            token_type=d.get("token_type", "bot"),
            guild_id=d.get("guild_id", ""),
            channel_id=d.get("channel_id", ""),
            bump_message=d.get("bump_message", "Application bump completed."),
            cooldown_minutes=float(d.get("cooldown_minutes", 120)),
            offset_max_minutes=float(d.get("offset_max_minutes", 30)),
            enabled=bool(d.get("enabled", 1)),
        )

    def validate(self) -> list[str]:
        errors = []
        if not self.name.strip():
            errors.append("Account name is required.")
        if not self.guild_id.strip():
            errors.append("Guild ID is required.")
        if not self.guild_id.strip().isdigit():
            errors.append("Guild ID must be a numeric snowflake.")
        if not self.channel_id.strip():
            errors.append("Channel ID is required.")
        if not self.channel_id.strip().isdigit():
            errors.append("Channel ID must be a numeric snowflake.")
        if self.cooldown_minutes <= 0:
            errors.append("Cooldown must be greater than 0 minutes.")
        if self.offset_max_minutes < 0:
            errors.append("Random offset cannot be negative.")
        if self.offset_max_minutes > self.cooldown_minutes:
            errors.append("Random offset cannot exceed the cooldown duration.")
        return errors
