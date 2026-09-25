"""
Account domain models.

An Account owns one token and bumps one-or-more Servers.
A Server has its own cooldown clock, independent of the account cooldown.

Hierarchy:
  Account → [Server, Server, Server]

Timing model (per account):
  server_cooldown_min   — how long before the same server can be bumped again
  account_cooldown_min  — minimum gap between any two bumps from this account
  start_offset_min      — how far apart different accounts begin (global setting)
  random_offset_min     — optional jitter added per execution
"""
from __future__ import annotations

import uuid
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _now() -> datetime:
    return datetime.now(timezone.utc)

def _new_id() -> str:
    return str(uuid.uuid4())[:8]


@dataclass
class ServerTarget:
    """One Discord server that an account bumps."""
    server_id:   str = field(default_factory=_new_id)
    name:        str = "Server"
    guild_id:    str = ""
    channel_id:  str = ""
    follow_managed_channel: bool = False
    enabled:     bool = True
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    total_runs:  int = 0
    total_ok:    int = 0
    total_fail:  int = 0
    total_sent: int = 0
    total_simulated: int = 0
    cooldown_min: Optional[float] = None
    random_offset_min: Optional[float] = None
    message: str = ""
    last_result: str = ""
    last_error: str = ""

    def to_dict(self) -> dict:
        return {
            "server_id":   self.server_id,
            "name":        self.name,
            "guild_id":    self.guild_id,
            "channel_id":  self.channel_id,
            "follow_managed_channel": self.follow_managed_channel,
            "enabled":     int(self.enabled),
            "last_run_at": _iso(self.last_run_at),
            "next_run_at": _iso(self.next_run_at),
            "total_runs":  self.total_runs,
            "total_ok":    self.total_ok,
            "total_fail":  self.total_fail,
            "total_sent": self.total_sent,
            "total_simulated": self.total_simulated,
            "cooldown_min": self.cooldown_min,
            "random_offset_min": self.random_offset_min,
            "message": self.message,
            "last_result": self.last_result,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ServerTarget":
        s = cls()
        s.server_id   = d.get("server_id",  _new_id())
        s.name        = d.get("name",        "Server")
        s.guild_id    = d.get("guild_id",    "")
        s.channel_id  = d.get("channel_id",  "")
        s.enabled     = bool(d.get("enabled", 1))
        s.follow_managed_channel = bool(d.get("follow_managed_channel", False))
        s.last_run_at = _parse(d.get("last_run_at"))
        s.next_run_at = _parse(d.get("next_run_at"))
        s.total_runs  = int(d.get("total_runs", 0))
        s.total_ok    = int(d.get("total_ok",   0))
        s.total_fail  = int(d.get("total_fail",  0))
        s.total_sent = int(d.get("total_sent", 0))
        s.total_simulated = int(d.get("total_simulated", 0))
        s.cooldown_min = None if d.get("cooldown_min") is None else float(d["cooldown_min"])
        s.random_offset_min = None if d.get("random_offset_min") is None else float(d["random_offset_min"])
        s.message = d.get("message", "")
        s.last_result = d.get("last_result", "")
        s.last_error = d.get("last_error", "")
        return s

    def validate(self) -> list[str]:
        e = []
        if not self.name.strip():       e.append("Server name is required.")
        if not self.guild_id.strip():   e.append("Guild ID is required.")
        if not self.guild_id.strip().isdigit(): e.append("Guild ID must be numeric.")
        if not self.channel_id.strip(): e.append("Channel ID is required.")
        if not self.channel_id.strip().isdigit(): e.append("Channel ID must be numeric.")
        if self.cooldown_min is not None and (not math.isfinite(self.cooldown_min) or self.cooldown_min <= 0):
            e.append("Server cooldown must be a finite positive number.")
        if self.random_offset_min is not None and (not math.isfinite(self.random_offset_min) or self.random_offset_min < 0):
            e.append("Random offset must be a finite nonnegative number.")
        return e


@dataclass
class Account:
    """One Discord user account — owns multiple servers."""
    account_id:           str   = field(default_factory=_new_id)
    name:                 str   = "Account"
    token_type:           str   = "user"        # "user" | "bot"
    enabled:              bool  = True

    message: str = "Time to bump this server."

    # Timing (per account)
    server_cooldown_min:  float = 120.0
    account_cooldown_min: float = 30.0
    random_offset_min:    float = 0.0

    # Browser profile association (optional)
    browser_name:         str   = ""   # "Chrome", "Edge", "Brave"
    browser_profile:      str   = ""   # "Default", "Profile 1", …
    browser_path:         str   = ""   # full path to profile dir

    # Runtime
    servers:              list  = field(default_factory=list)  # [ServerTarget]
    status:               str   = "stopped"  # stopped / running / paused / error
    last_action_at:       Optional[datetime] = None

    # Auto-start
    auto_start:           bool  = False

    def to_dict(self) -> dict:
        return {
            "account_id":           self.account_id,
            "name":                 self.name,
            "token_type":           self.token_type,
            "enabled":              int(self.enabled),
            "server_cooldown_min":  self.server_cooldown_min,
            "account_cooldown_min": self.account_cooldown_min,
            "random_offset_min":    self.random_offset_min,
            "browser_name":         self.browser_name,
            "browser_profile":      self.browser_profile,
            "browser_path":         self.browser_path,
            "auto_start":           int(self.auto_start),
            "servers":              [s.to_dict() for s in self.servers],
            "last_action_at": _iso(self.last_action_at),
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        a = cls()
        a.account_id           = d.get("account_id",           _new_id())
        a.name                 = d.get("name",                 "Account")
        a.token_type           = d.get("token_type",           "user")
        a.enabled              = bool(d.get("enabled",          1))
        a.server_cooldown_min  = float(d.get("server_cooldown_min",  120.0))
        a.account_cooldown_min = float(d.get("account_cooldown_min",  30.0))
        a.random_offset_min    = float(d.get("random_offset_min",      0.0))
        a.browser_name         = d.get("browser_name",         "")
        a.browser_profile      = d.get("browser_profile",      "")
        a.browser_path         = d.get("browser_path",         "")
        a.auto_start           = bool(d.get("auto_start",       0))
        a.last_action_at = _parse(d.get("last_action_at"))
        a.message = d.get("message", "Time to bump this server.")
        a.servers              = [ServerTarget.from_dict(s)
                                  for s in d.get("servers", [])]
        return a

    def validate(self) -> list[str]:
        e = []
        if not self.name.strip():         e.append("Account name is required.")
        if self.server_cooldown_min <= 0: e.append("Server cooldown must be > 0.")
        if self.account_cooldown_min < 0: e.append("Account cooldown cannot be negative.")
        if self.random_offset_min < 0:    e.append("Random offset cannot be negative.")
        if self.token_type not in ("bot", "user"):
            e.append("Account type must be bot or user.")
        for v in (self.server_cooldown_min, self.account_cooldown_min, self.random_offset_min):
            if not math.isfinite(v):
                e.append("Timing must use finite numbers.")
        return e

    def next_bump_server(self) -> Optional["ServerTarget"]:
        """Return the enabled server with the earliest next_run_at."""
        candidates = [s for s in self.servers
                      if s.enabled and s.guild_id and s.channel_id]
        if not candidates:
            return None
        return min(candidates,
                   key=lambda s: s.next_run_at or datetime.min.replace(
                       tzinfo=timezone.utc))


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None: return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def _parse(s: Optional[str]) -> Optional[datetime]:
    if not s: return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except Exception:
        return None
