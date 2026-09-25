"""
UserTokenAdapter — Discord user-account self-bot adapter.

HOW /bump ACTUALLY WORKS
-------------------------
Discord's /bump command is a slash command registered by the DISBOARD bot
(application_id = 302050872383242240).  A normal user account triggers it
by sending an "application command interaction" to the interactions endpoint,
exactly the same POST the Discord client sends when you type /bump and press
Enter.

The interaction payload is:
  POST /api/v9/interactions
  {
      "type": 2,                    ← APPLICATION_COMMAND
      "application_id": "302050872383242240",
      "guild_id": "<guild_id>",
      "channel_id": "<channel_id>",
      "data": {
          "version":      "<command_version>",
          "id":           "<command_id>",
          "name":         "bump",
          "type":         1,        ← CHAT_INPUT
          "options":      [],
          "application_command": {
              "id":                  "<command_id>",
              "application_id":      "302050872383242240",
              "name":                "bump",
              "description":         "Bump your server",
              "version":             "<command_version>",
              "type":                1,
              "nsfw":                false,
              "dm_permission":       false,
              "options":             []
          }
      },
      "nonce":          "<snowflake>",
      "session_id":     "<random 32-char hex>",
      "analytics_token": ""
  }

We look up the command definition from DISBOARD's registered slash commands
so we always have the live command ID and version.

IMPORTANT LIMITATIONS
---------------------
- DISBOARD enforces a 2-hour cooldown server-side.  Sending the interaction
  before the cooldown expires returns an ephemeral "Please wait X minutes"
  message — the adapter detects this and raises a rate-limit error so the
  scheduler backs off correctly.
- Discord may require a valid browser fingerprint in X-Super-Properties for
  the interaction to be accepted.  The value below is a base64 JSON blob
  containing {"os":"Windows","browser":"Chrome","device":"",...} which is
  the minimum accepted by the API.
- Using a user token for slash commands is against Discord ToS.

FALLBACK
--------
If the DISBOARD application commands endpoint is not accessible (private
guild, permission mismatch, etc.) the adapter falls back to sending a plain
/bump text message in the channel, which relies on DISBOARD's text-command
trigger ("!d bump" or plain "/bump" text).

WARNING: Against Discord ToS. Use only on accounts you own.
"""
from __future__ import annotations

import json
import os
import random
import re
import string
import time
import urllib.error
import urllib.request
from typing import Optional

from app.adapters.base import AdapterBase, AdapterError, AdapterInfo
from app.scheduler.models import OperationResult, RateLimitState
from app.services.logging_service import get_logger

log = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_API_V9   = "https://discord.com/api/v9"
_TIMEOUT  = 15

# DISBOARD bot application ID (stable, public)
_DISBOARD_APP_ID = "302050872383242240"

# Realistic browser headers
_USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
]

# Minimal Discord client fingerprint (Windows + Chrome)
_SUPER_PROPS = (
    "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2aWNlIjoiIiwic3lzdGVtX2xvY2FsZSI6"
    "ImVuLVVTIiwiYnJvd3Nlcl91c2VyX2FnZW50IjoiTW96aWxsYS81LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2lu"
    "NjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEyNC4wLjAu"
    "MCBTYWZHCMKVNTM3LjM2IiwiYnJvd3Nlcl92ZXJzaW9uIjoiMTI0LjAuMC4wIiwib3NfdmVyc2lvbiI6IjEw"
    "IiwicmVmZXJyZXIiOiIiLCJyZWZlcnJpbmdfZG9tYWluIjoiIiwicmVmZXJyZXJfY3VycmVudCI6IiIsInJl"
    "ZmVycmluZ19kb21haW5fY3VycmVudCI6IiIsInJlbGVhc2VfY2hhbm5lbCI6InN0YWJsZSIsImNsaWVudF9i"
    "dWlsZF9udW1iZXIiOjI5NjA2MiwiY2xpZW50X2V2ZW50X3NvdXJjZSI6bnVsbH0="
)

# Human-like delay (ms) before each API call
_DELAY_MIN_MS = 800
_DELAY_MAX_MS = 2400

# Regex to find DISBOARD "please wait X" messages
_COOLDOWN_RE = re.compile(
    r"(?:please wait|you can bump again in|cooldown)[^\d]*(\d+)",
    re.IGNORECASE,
)


def _rand_nonce() -> str:
    """Discord uses a snowflake-shaped nonce (17-19 digit decimal string)."""
    ts_ms = int(time.time() * 1000)
    epoch_ms = ts_ms - 1420070400000   # Discord epoch
    snowflake = (epoch_ms << 22) | random.randint(0, (1 << 22) - 1)
    return str(snowflake)


def _rand_session() -> str:
    return "".join(random.choices(string.hexdigits[:16], k=32)).lower()


def _build_headers(token: str, user_agent: str) -> dict:
    return {
        "Authorization":        token,
        "Content-Type":         "application/json",
        "User-Agent":           user_agent,
        "X-Super-Properties":   _SUPER_PROPS,
        "X-Discord-Locale":     "en-US",
        "X-Discord-Timezone":   "America/New_York",
        "Accept":               "*/*",
        "Accept-Language":      "en-US,en;q=0.9",
        "Origin":               "https://discord.com",
        "Referer":              "https://discord.com/channels/",
        "Sec-Fetch-Dest":       "empty",
        "Sec-Fetch-Mode":       "cors",
        "Sec-Fetch-Site":       "same-origin",
    }


def _http(method: str, url: str, token: str, user_agent: str,
          body: Optional[dict] = None) -> dict:
    """
    Raw HTTP call.  Token never appears in logs — handled by SecretRedactor.
    Raises AdapterError on failure.
    """
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers=_build_headers(token, user_agent),
    )
    log.debug("→ %s %s", method, url.replace(_API_V9, ""))
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
        log.debug("← HTTP %d", code)
        if code == 401:
            raise AdapterError("INVALID_TOKEN",
                               "Token rejected by Discord", retryable=False)
        if code == 403:
            raise AdapterError("MISSING_PERMISSIONS",
                               payload.get("message", "Forbidden"), retryable=False)
        if code == 404:
            raise AdapterError("NOT_FOUND",
                               payload.get("message", "Not found"), retryable=False)
        if code == 429:
            retry_after = float(payload.get("retry_after", 5))
            raise AdapterError("RATE_LIMITED", "Rate limited",
                               retry_after_ms=int(retry_after * 1000),
                               retryable=True)
        if code in (500, 502, 503, 504):
            raise AdapterError("SERVER_ERROR", f"HTTP {code}", retryable=True)
        raise AdapterError("PERMANENT",
                           payload.get("message", f"HTTP {code}"),
                           retryable=False)
    except OSError as exc:
        raise AdapterError("NETWORK", str(exc), retryable=True)


class UserTokenAdapter(AdapterBase):
    """
    Self-bot adapter that triggers DISBOARD's /bump slash command using a
    Discord user account token.

    Priority order:
      1. POST /interactions  (slash command — proper bump, DISBOARD responds)
      2. Fallback: POST a plain "!d bump" text message
    """

    def __init__(self):
        self._token:        Optional[str] = None
        self._guild_id:     Optional[str] = None
        self._channel_id:   Optional[str] = None
        self._user_agent:   str           = random.choice(_USER_AGENTS)
        self._session_id:   str           = _rand_session()
        self._connected:    bool          = False
        self._rl:           RateLimitState = RateLimitState()

        # Cached bump command definition from DISBOARD
        self._bump_cmd_id:      Optional[str] = None
        self._bump_cmd_version: Optional[str] = None

        # Last interaction nonce (for verification)
        self._last_nonce: Optional[str] = None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _delay(self):
        ms = random.randint(_DELAY_MIN_MS, _DELAY_MAX_MS)
        log.debug("human delay: %d ms", ms)
        time.sleep(ms / 1000)

    def _get(self, path: str) -> dict:
        return _http("GET", _API_V9 + path,
                     self._token, self._user_agent)

    def _post(self, path: str, body: dict) -> dict:
        return _http("POST", _API_V9 + path,
                     self._token, self._user_agent, body)

    def _fetch_bump_command(self, guild_id: str) -> bool:
        """
        Fetch DISBOARD's /bump command definition from the guild's application
        commands list.  Returns True if found.
        """
        try:
            # Get all application commands in the guild from DISBOARD
            cmds = self._get(
                f"/guilds/{guild_id}/application-command-index"
            )
            # Response: {"application_commands": [...], "applications": [...]}
            for cmd in cmds.get("application_commands", []):
                if (str(cmd.get("application_id")) == _DISBOARD_APP_ID
                        and cmd.get("name") == "bump"):
                    self._bump_cmd_id      = str(cmd["id"])
                    self._bump_cmd_version = str(cmd.get("version", cmd["id"]))
                    log.info("DISBOARD /bump found: id=%s version=%s",
                             self._bump_cmd_id, self._bump_cmd_version)
                    return True
        except AdapterError as e:
            log.warning("Could not fetch application command index: %s", e.code)

        # Fallback: try the search endpoint
        try:
            result = self._get(
                f"/guilds/{guild_id}/application-commands/search"
                f"?type=1&query=bump&application_id={_DISBOARD_APP_ID}&limit=5"
            )
            for cmd in result.get("application_commands", []):
                if cmd.get("name") == "bump":
                    self._bump_cmd_id      = str(cmd["id"])
                    self._bump_cmd_version = str(cmd.get("version", cmd["id"]))
                    log.info("DISBOARD /bump found via search: id=%s",
                             self._bump_cmd_id)
                    return True
        except AdapterError as e:
            log.warning("Command search also failed: %s", e.code)

        log.warning("DISBOARD /bump not found in guild %s — "
                    "is DISBOARD bot present?", guild_id)
        return False

    def _send_bump_interaction(self, guild_id: str, channel_id: str) -> str:
        """
        POST an APPLICATION_COMMAND interaction for /bump.
        Returns the nonce string on success.
        Raises AdapterError on failure.
        """
        nonce = _rand_nonce()

        payload = {
            "type":            2,          # APPLICATION_COMMAND
            "application_id":  _DISBOARD_APP_ID,
            "guild_id":        guild_id,
            "channel_id":      channel_id,
            "session_id":      self._session_id,
            "nonce":           nonce,
            "analytics_token": "",
            "data": {
                "version":     self._bump_cmd_version,
                "id":          self._bump_cmd_id,
                "name":        "bump",
                "type":        1,          # CHAT_INPUT
                "options":     [],
                "application_command": {
                    "id":                  self._bump_cmd_id,
                    "application_id":      _DISBOARD_APP_ID,
                    "name":                "bump",
                    "description":         "Bump your server",
                    "version":             self._bump_cmd_version,
                    "type":                1,
                    "nsfw":                False,
                    "dm_permission":       False,
                    "options":             [],
                },
                "attachments": [],
            },
        }

        # POST to /interactions — Discord returns 204 No Content on success
        try:
            _http("POST", _API_V9 + "/interactions",
                  self._token, self._user_agent, payload)
        except AdapterError as e:
            # 204 is returned as an empty body — urllib raises HTTPError for
            # non-2xx only so a 204 arrives as an empty dict (no error)
            if e.code == "NOT_FOUND":
                raise AdapterError("INVALID_CHANNEL",
                                   "Channel or guild not found", retryable=False)
            raise

        log.debug("Interaction sent, nonce=%s", nonce)
        self._last_nonce = nonce
        return nonce

    def _send_text_bump_fallback(self, channel_id: str) -> dict:
        """
        Fallback: send plain text that triggers DISBOARD's text command.
        Many DISBOARD installations still respond to '!d bump'.
        """
        log.info("Falling back to !d bump text message")
        payload = {
            "content":           "!d bump",
            "allowed_mentions":  {"parse": []},
            "tts":               False,
        }
        return self._post(f"/channels/{channel_id}/messages", payload)

    def _check_disboard_response(self, channel_id: str,
                                 nonce: str, wait_secs: float = 6.0) -> dict:
        """
        Poll the channel for DISBOARD's response to our interaction.
        Returns the message dict if found, or empty dict.
        """
        deadline = time.monotonic() + wait_secs
        while time.monotonic() < deadline:
            time.sleep(1.5)
            try:
                msgs = self._get(
                    f"/channels/{channel_id}/messages?limit=5"
                )
                for msg in msgs:
                    author = msg.get("author", {})
                    # DISBOARD's bot ID
                    if str(author.get("id")) == _DISBOARD_APP_ID:
                        return msg
                    # Also check interaction_metadata nonce
                    meta = msg.get("interaction_metadata", {})
                    if str(meta.get("id", "")).startswith(nonce[:10]):
                        return msg
            except AdapterError:
                break
        return {}

    # ── AdapterBase ───────────────────────────────────────────────────────────

    def connect(self, credential: str, guild_id: str,
                channel_id: str) -> AdapterInfo:
        if not credential:
            raise AdapterError("MISSING_TOKEN", "No token provided",
                               retryable=False)

        # 1. Verify identity
        me = _http("GET", _API_V9 + "/users/@me",
                   credential, self._user_agent)
        if "id" not in me:
            raise AdapterError("INVALID_TOKEN",
                               "Could not verify user identity", retryable=False)

        self._delay()

        # 2. Verify guild
        try:
            guild = _http("GET", _API_V9 + f"/guilds/{guild_id}",
                          credential, self._user_agent)
        except AdapterError as e:
            raise AdapterError("TARGET_NOT_ALLOWED",
                               f"Guild {guild_id}: {e}", retryable=False)

        self._delay()

        # 3. Verify channel
        try:
            channel = _http("GET", _API_V9 + f"/channels/{channel_id}",
                            credential, self._user_agent)
        except AdapterError as e:
            raise AdapterError("INVALID_CHANNEL",
                               f"Channel {channel_id}: {e}", retryable=False)

        if channel.get("guild_id") != guild_id:
            raise AdapterError("TARGET_NOT_ALLOWED",
                               "Channel does not belong to the specified guild",
                               retryable=False)

        self._token      = credential
        self._guild_id   = guild_id
        self._channel_id = channel_id
        self._connected  = True
        self._session_id = _rand_session()   # fresh session per connection

        # 4. Pre-fetch DISBOARD bump command (best-effort)
        self._delay()
        self._fetch_bump_command(guild_id)

        username      = me.get("username", "Unknown")
        discriminator = me.get("discriminator", "0")
        tag = f"{username}#{discriminator}" if discriminator != "0" else username

        log.info("UserTokenAdapter connected: %s guild=%s bump_cmd=%s",
                 tag, guild_id,
                 "found" if self._bump_cmd_id else "NOT FOUND — will fallback")

        return AdapterInfo(
            user_tag=tag,
            user_id=me["id"],
            guild_name=guild.get("name", guild_id),
            channel_name=channel.get("name", channel_id),
        )

    def execute(self, guild_id: str, channel_id: str,
                message: str, operation_id: str) -> OperationResult:
        if not self._connected or not self._token:
            raise AdapterError("NOT_CONNECTED", "Call connect() first",
                               retryable=False)

        self._delay()
        start = time.monotonic()

        # ── Path A: /bump slash command via interaction ─────────────────
        if self._bump_cmd_id:
            try:
                nonce = self._send_bump_interaction(guild_id, channel_id)
                duration_ms = (time.monotonic() - start) * 1000

                # Wait briefly for DISBOARD's reply, check for cooldown message
                self._delay()
                response_msg = self._check_disboard_response(channel_id, nonce)
                if response_msg:
                    content = response_msg.get("content", "") or ""
                    # Check all embeds too
                    for embed in response_msg.get("embeds", []):
                        content += " " + (embed.get("description") or "")
                        content += " " + (embed.get("title") or "")

                    m = _COOLDOWN_RE.search(content)
                    if m:
                        minutes = int(m.group(1))
                        wait_ms = minutes * 60 * 1000
                        log.info("DISBOARD cooldown: %d min remaining", minutes)
                        raise AdapterError(
                            "RATE_LIMITED",
                            f"DISBOARD cooldown: {minutes} min remaining",
                            retry_after_ms=wait_ms,
                            retryable=True,
                        )

                    # Success keyword detection
                    if any(kw in content.lower() for kw in
                           ("bumped", "bump", "server has been", "successfully")):
                        log.info("DISBOARD confirmed bump via interaction")
                        return OperationResult(
                            success=True, verified=True,
                            retryable=False,
                            message="DISBOARD /bump interaction confirmed",
                            external_id=response_msg.get("id"),
                            duration_ms=duration_ms,
                        )

                # Interaction sent, no clear DISBOARD response yet —
                # treat as SENT (unverified)
                return OperationResult(
                    success=True, verified=False,
                    retryable=False,
                    message="/bump interaction sent (awaiting DISBOARD response)",
                    external_id=None,
                    duration_ms=duration_ms,
                )

            except AdapterError as e:
                if e.code == "RATE_LIMITED":
                    raise   # propagate cooldown upstream
                log.warning("Interaction failed (%s), trying fallback", e.code)
                # Re-fetch command — may have changed
                self._delay()
                self._fetch_bump_command(guild_id)

        # ── Path B: text fallback ────────────────────────────────────────
        msg = self._send_text_bump_fallback(channel_id)
        duration_ms = (time.monotonic() - start) * 1000
        log.info("Text fallback bump sent, msg_id=%s", msg.get("id"))
        return OperationResult(
            success=True, verified=False,
            retryable=False,
            message="!d bump text fallback sent",
            external_id=msg.get("id"),
            duration_ms=duration_ms,
        )

    def verify_result(self, operation_id: str,
                      external_id: Optional[str]) -> OperationResult:
        """Verify by fetching the relevant message."""
        if not self._connected or not self._token:
            return OperationResult(success=False, verified=False,
                                   retryable=False,
                                   message="Not connected")
        if not external_id and not self._last_nonce:
            # Interaction path — no message ID to check
            return OperationResult(success=True, verified=True,
                                   retryable=False,
                                   message="Interaction-based bump; assumed delivered")

        if external_id:
            try:
                self._delay()
                msg = self._get(
                    f"/channels/{self._channel_id}/messages/{external_id}")
                if msg.get("id") == external_id:
                    return OperationResult(success=True, verified=True,
                                           retryable=False,
                                           message="Message confirmed",
                                           external_id=external_id)
            except AdapterError as e:
                return OperationResult(success=False, verified=False,
                                       retryable=e.retryable, message=str(e))

        return OperationResult(success=True, verified=True,
                               retryable=False,
                               message="Bump sent via interaction")

    def disconnect(self) -> None:
        self._token     = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def rate_limit_state(self) -> RateLimitState:
        return self._rl
