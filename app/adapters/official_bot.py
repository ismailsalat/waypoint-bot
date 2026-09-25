"""
OfficialBotAdapter — Discord REST API v10 with a Bot token.

Uses only Python stdlib (urllib).  No external HTTP libraries required.
Credentials are NEVER logged.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from app.adapters.base import AdapterBase, AdapterError, AdapterInfo
from app.scheduler.models import ErrorCategory, OperationResult, RateLimitState
from app.services.logging_service import get_logger

log = get_logger(__name__)

_API_BASE = "https://discord.com/api/v10"
_TIMEOUT  = 10


def _api(method: str, path: str, token: str, body=None) -> dict:
    """HTTP call — token is never included in log output."""
    url  = _API_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type":  "application/json",
            "User-Agent":    "BumpSchedulerPro/3.0",
        },
    )
    log.debug("→ %s %s", method, path)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        code = exc.code
        log.debug("← HTTP %d from %s", code, path)
        if code == 401:
            raise AdapterError("INVALID_TOKEN",  "Token rejected",      retryable=False)
        if code == 403:
            raise AdapterError("FORBIDDEN",       payload.get("message","Forbidden"), retryable=False)
        if code == 404:
            raise AdapterError("NOT_FOUND",       payload.get("message","Not found"), retryable=False)
        if code == 429:
            ra = int(float(payload.get("retry_after", 1)) * 1000)
            raise AdapterError("RATE_LIMITED", "Rate limited", retry_after_ms=ra, retryable=True)
        if code in (500, 502, 503, 504):
            raise AdapterError("SERVER_ERROR", f"HTTP {code}", retryable=True)
        raise AdapterError("PERMANENT", payload.get("message", f"HTTP {code}"), retryable=False)
    except OSError as exc:
        raise AdapterError("NETWORK", str(exc), retryable=True)


class OfficialBotAdapter(AdapterBase):

    def __init__(self):
        self._token:      Optional[str] = None
        self._guild_id:   Optional[str] = None
        self._channel_id: Optional[str] = None
        self._connected = False
        self._last_msg_id: Optional[str] = None
        self._rl = RateLimitState()

    def connect(self, credential: str, guild_id: str, channel_id: str) -> AdapterInfo:
        if not credential:
            raise AdapterError("MISSING_TOKEN", "No token provided", retryable=False)

        me = _api("GET", "/users/@me", credential)
        if "id" not in me:
            raise AdapterError("INVALID_TOKEN", "Could not verify identity", retryable=False)

        try:
            guild = _api("GET", f"/guilds/{guild_id}", credential)
        except AdapterError as e:
            if not e.retryable:
                raise AdapterError("TARGET_NOT_ALLOWED",
                                   f"Guild {guild_id} not accessible", retryable=False)
            raise

        try:
            channel = _api("GET", f"/channels/{channel_id}", credential)
        except AdapterError as e:
            if not e.retryable:
                raise AdapterError("INVALID_CHANNEL",
                                   f"Channel {channel_id} not found", retryable=False)
            raise

        if channel.get("guild_id") != guild_id:
            raise AdapterError("TARGET_NOT_ALLOWED",
                               "Channel does not belong to specified guild", retryable=False)
        if channel.get("type", -1) not in (0, 5, 10, 11, 12):
            raise AdapterError("INVALID_CHANNEL", "Not a text channel", retryable=False)

        self._token      = credential
        self._guild_id   = guild_id
        self._channel_id = channel_id
        self._connected  = True

        username     = me.get("username", "Unknown")
        discriminator = me.get("discriminator", "0")
        tag = f"{username}#{discriminator}" if discriminator != "0" else username
        log.info("Connected as %s to guild %s", tag, guild.get("name"))
        return AdapterInfo(
            user_tag=tag, user_id=me["id"],
            guild_name=guild.get("name", guild_id),
            channel_name=channel.get("name", channel_id),
        )

    def execute(self, guild_id: str, channel_id: str,
                message: str, operation_id: str) -> OperationResult:
        if not self._connected or not self._token:
            raise AdapterError("NOT_CONNECTED", "Call connect() first", retryable=False)

        start = time.monotonic()
        now_ts = int(time.time())
        content = (
            f"{message}\n\n"
            f"Last bump: <t:{now_ts}:R>\n"
            f"op: `{operation_id[:8]}`"
        )
        payload = {"content": content, "allowed_mentions": {"parse": []}}

        action = "created"
        msg    = None

        if self._last_msg_id:
            try:
                msg    = _api("PATCH",
                              f"/channels/{channel_id}/messages/{self._last_msg_id}",
                              self._token, payload)
                action = "updated"
            except AdapterError:
                self._last_msg_id = None

        if msg is None:
            msg    = _api("POST", f"/channels/{channel_id}/messages",
                          self._token, payload)
            action = "created"

        self._last_msg_id = msg.get("id")
        duration_ms = (time.monotonic() - start) * 1000
        url = f"https://discord.com/channels/{guild_id}/{channel_id}/{msg['id']}"
        log.info("Bump %s  op=%s  msg=%s  action=%s",
                 channel_id, operation_id[:8], msg['id'], action)
        return OperationResult(
            success=True,
            verified=False,     # verify_result() will confirm delivery
            retryable=False,
            message=f"Message {action}",
            external_id=msg["id"],
            duration_ms=duration_ms,
        )

    def verify_result(self, operation_id: str,
                      external_id: Optional[str]) -> OperationResult:
        if not self._connected or not self._token or not external_id:
            return OperationResult(success=False, verified=False, retryable=False,
                                   message="Cannot verify — not connected or no message ID")
        try:
            _api("GET", f"/channels/{self._channel_id}/messages/{external_id}", self._token)
            return OperationResult(success=True, verified=True, retryable=False,
                                   message="Message confirmed", external_id=external_id)
        except AdapterError as e:
            return OperationResult(success=False, verified=False,
                                   retryable=e.retryable, message=str(e))

    def disconnect(self) -> None:
        self._token     = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def rate_limit_state(self) -> RateLimitState:
        return self._rl
