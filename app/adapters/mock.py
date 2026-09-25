"""
MockAdapter — used for all automated tests and dry-runs.

Configurable failure modes:
  fail_times      — fail this many times before succeeding
  error_category  — which error to raise
  latency_ms      — simulated network latency
"""
from __future__ import annotations

import time
import threading
from typing import Optional

from app.adapters.base import AdapterBase, AdapterError, AdapterInfo
from app.scheduler.models import ErrorCategory, OperationResult, RateLimitState


class MockAdapter(AdapterBase):

    def __init__(
        self,
        fail_times: int = 0,
        error_category: ErrorCategory = ErrorCategory.TRANSIENT,
        latency_ms: float = 0,
        rate_limit_after: int = 0,
        rate_limit_wait: float = 0.01,
        verify_fails: bool = False,     # make verify_result return unverified
    ):
        self._fail_times        = fail_times
        self._error_category    = error_category
        self._latency_ms        = latency_ms
        self._rate_limit_after  = rate_limit_after
        self._rate_limit_wait   = rate_limit_wait
        self._verify_fails      = verify_fails

        self._call_count   = 0
        self._connected    = False
        self._rl_state     = RateLimitState()
        self._lock         = threading.Lock()

        # Counters for inspection
        self.execute_calls:  list[dict] = []
        self.verify_calls:   list[str]  = []
        self.connect_calls:  int        = 0
        self.disconnect_calls: int      = 0

    # ── AdapterBase ───────────────────────────────────────────────────────────

    def connect(self, credential: str, guild_id: str, channel_id: str) -> AdapterInfo:
        with self._lock:
            self.connect_calls += 1
            self._connected = True
        return AdapterInfo(
            user_tag="MockUser#0000",
            user_id="123456789",
            guild_name=f"MockGuild-{guild_id}",
            channel_name=f"mock-channel-{channel_id}",
        )

    def execute(self, guild_id: str, channel_id: str,
                message: str, operation_id: str) -> OperationResult:
        if self._latency_ms:
            time.sleep(self._latency_ms / 1000)

        with self._lock:
            self._call_count += 1
            self.execute_calls.append({
                "guild_id": guild_id,
                "channel_id": channel_id,
                "operation_id": operation_id,
            })

            # Rate limit check
            if self._rate_limit_after and self._call_count > self._rate_limit_after:
                self._rl_state = RateLimitState(
                    limited=True, retry_after=self._rate_limit_wait
                )
                raise AdapterError(
                    "RATE_LIMITED",
                    f"Rate limited after {self._rate_limit_after} calls",
                    retry_after_ms=int(self._rate_limit_wait * 1000),
                    retryable=True,
                )

            # Failure injection
            if self._fail_times > 0:
                self._fail_times -= 1
                retryable = self._error_category in (
                    ErrorCategory.TRANSIENT, ErrorCategory.RATE_LIMIT,
                    ErrorCategory.SERVER_ERROR
                )
                raise AdapterError(
                    self._error_category.value,
                    f"Injected failure ({self._error_category})",
                    retryable=retryable,
                )

        return OperationResult(
            success=True,
            verified=False,     # verify_result() confirms this
            retryable=False,
            message="Mock operation completed",
            external_id=f"mock-{operation_id[:8]}",
            duration_ms=self._latency_ms,
        )

    def verify_result(self, operation_id: str,
                      external_id: Optional[str]) -> OperationResult:
        with self._lock:
            self.verify_calls.append(operation_id)
            if self._verify_fails:
                return OperationResult(success=False, verified=False,
                                       retryable=False, message="Mock verify failed")
        return OperationResult(success=True, verified=True, retryable=False,
                               message="Mock verified")

    def disconnect(self) -> None:
        with self._lock:
            self.disconnect_calls += 1
            self._connected = False

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def rate_limit_state(self) -> RateLimitState:
        with self._lock:
            return self._rl_state

    # ── Convenience ───────────────────────────────────────────────────────────

    def reset(self) -> None:
        with self._lock:
            self._call_count  = 0
            self._fail_times  = 0   # clear injected failures
            self._rl_state    = RateLimitState()
            self.execute_calls    = []
            self.verify_calls     = []
            self.connect_calls    = 0
            self.disconnect_calls = 0
