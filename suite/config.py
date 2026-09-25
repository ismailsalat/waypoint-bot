"""Validated, local scheduler preferences. No credentials are serialized here."""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from pydantic import BaseModel, ConfigDict, Field


class SchedulerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = False
    dry_run: bool = True
    start_offset_min: float = Field(5, ge=0, le=1440)
    max_failures: int = Field(5, ge=1, le=100)
    retry_base_seconds: float = Field(30, ge=1, le=86400)
    retry_max_seconds: float = Field(300, ge=1, le=86400)
    refresh_seconds: int = Field(5, ge=2, le=300)
    default_server_cooldown_min: float = Field(120, gt=0, le=525600)
    default_account_cooldown_min: float = Field(30, ge=0, le=525600)
    default_random_offset_min: float = Field(0, ge=0, le=1440)
    default_message: str = Field("Time to bump this server.", min_length=1, max_length=1800)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Preferences:
    def __init__(self, path: Path):
        self.path = path
        self.lock = RLock()
        self.value = SchedulerSettings()
        if path.exists():
            self.value = SchedulerSettings.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def get(self):
        with self.lock:
            return self.value.model_dump()

    def save(self, value: SchedulerSettings):
        if value.retry_max_seconds < value.retry_base_seconds:
            raise ValueError("Maximum retry delay must be at least the initial retry delay.")
        with self.lock:
            atomic_json(self.path, value.model_dump())
            self.value = value
