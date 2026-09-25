"""
Adapter interface contract.

All adapters implement AdapterBase.
The scheduler knows nothing about Discord.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from app.scheduler.models import OperationResult, RateLimitState


@dataclass
class AdapterInfo:
    user_tag:     str
    user_id:      str
    guild_name:   str
    channel_name: str


class AdapterBase(abc.ABC):

    @abc.abstractmethod
    def connect(self, credential: str, guild_id: str, channel_id: str) -> AdapterInfo:
        """Authenticate and validate target.  Raises AdapterError on failure."""

    @abc.abstractmethod
    def execute(self, guild_id: str, channel_id: str,
                message: str, operation_id: str) -> OperationResult:
        """Execute one bump operation."""

    @abc.abstractmethod
    def verify_result(self, operation_id: str, external_id: Optional[str]) -> OperationResult:
        """Verify that a previously sent operation actually landed."""

    @abc.abstractmethod
    def disconnect(self) -> None:
        """Clean up connection."""

    @abc.abstractmethod
    def is_connected(self) -> bool:
        """Return True if connected."""

    def rate_limit_state(self) -> RateLimitState:
        """Return current rate-limit state.  Defaults to not limited."""
        return RateLimitState()


class AdapterError(Exception):
    def __init__(self, code: str, message: str = "",
                 retry_after_ms: int = 0, retryable: bool = True):
        super().__init__(message or code)
        self.code = code
        self.retry_after_ms = retry_after_ms
        self.retryable = retryable
