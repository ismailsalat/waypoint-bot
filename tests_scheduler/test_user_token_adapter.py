"""
test_user_token_adapter.py
---------------------------
Tests for UserTokenAdapter:
  1. Manager selects correct adapter type per token_type
  2. No "Bot " prefix in Authorization header
  3. connect() validates user/guild/channel
  4. execute() sends /bump interaction when command found
  5. execute() falls back to !d bump when no command found
  6. Cooldown detection: DISBOARD reply with "please wait" raises RATE_LIMITED
  7. verify_result() confirms delivery
  8. disconnect() clears state
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch, call
from typing import Optional, List, Tuple

import pytest

from app.adapters.user_token import UserTokenAdapter, _rand_nonce, _rand_session
from app.adapters.base import AdapterError
from app.scheduler.models import ErrorCategory
from app.services.credentials import store_credential, clear_all


# ── HTTP mock helpers ─────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, data: dict, status: int = 200):
        self._data = json.dumps(data).encode()
        self.status = status
    def read(self): return self._data
    def __enter__(self): return self
    def __exit__(self, *a): pass


class _HTTPSequence:
    """Returns successive fake responses or raises HTTPError."""
    def __init__(self, responses: list):
        self._q = list(responses)
        self.calls: List[tuple] = []

    def __call__(self, req, timeout=None):
        import urllib.error
        self.calls.append((req.get_method(), req.get_full_url()))
        if not self._q:
            return _FakeResponse({}, 200)
        item = self._q.pop(0)
        if isinstance(item, Exception):
            raise item
        data, code = item
        if code >= 400:
            fp = MagicMock()
            fp.read.return_value = json.dumps(data).encode()
            raise urllib.error.HTTPError(
                url="", code=code, msg=str(code),
                hdrs=MagicMock(), fp=fp)
        return _FakeResponse(data, code)


def _me():    return {"id": "111", "username": "Alice", "discriminator": "0"}
def _guild(): return {"id": "222", "name": "TestServer"}
def _chan():  return {"id": "333", "guild_id": "222", "name": "bumps", "type": 0}
def _cmd():   return {
    "application_commands": [{"id": "99", "version": "100",
                              "application_id": "302050872383242240",
                              "name": "bump"}]
}


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_no_bot_prefix_in_auth_header():
    """Authorization header must NOT start with 'Bot '."""
    captured = []
    original_req = __import__("urllib.request", fromlist=["Request"]).Request

    def capture_req(url, data=None, method="GET", headers=None):
        r = original_req(url, data=data, method=method, headers=headers or {})
        captured.append(dict(r.headers))
        return r

    seq = _HTTPSequence([
        (_me(), 200), (_guild(), 200), (_chan(), 200), (_cmd(), 200),
    ])
    with patch("urllib.request.urlopen", side_effect=seq), \
         patch("urllib.request.Request", side_effect=capture_req):
        a = UserTokenAdapter()
        a._delay = lambda: None
        try:
            a.connect("myusertoken", "222", "333")
        except Exception:
            pass

    for hdrs in captured:
        auth = hdrs.get("Authorization", "")
        assert not auth.lower().startswith("bot "), \
            f"Found 'Bot ' prefix in header: {auth!r}"


def test_connect_success():
    seq = _HTTPSequence([
        (_me(), 200), (_guild(), 200), (_chan(), 200), (_cmd(), 200),
    ])
    with patch("urllib.request.urlopen", side_effect=seq):
        a = UserTokenAdapter()
        a._delay = lambda: None
        info = a.connect("tok", "222", "333")

    assert info.user_tag == "Alice"
    assert info.guild_name == "TestServer"
    assert a.is_connected()
    assert a._bump_cmd_id == "99"
    assert a._bump_cmd_version == "100"


def test_connect_invalid_token():
    import urllib.error
    fp = MagicMock(); fp.read.return_value = b'{"message":"401: Unauthorized"}'
    err = urllib.error.HTTPError("", 401, "Unauthorized", MagicMock(), fp)

    with patch("urllib.request.urlopen", side_effect=err):
        a = UserTokenAdapter()
        a._delay = lambda: None
        with pytest.raises(AdapterError) as exc:
            a.connect("bad", "222", "333")
    assert exc.value.code == "INVALID_TOKEN"
    assert not exc.value.retryable


def test_connect_channel_wrong_guild():
    wrong_chan = {"id": "333", "guild_id": "WRONG", "name": "x", "type": 0}
    seq = _HTTPSequence([
        (_me(), 200), (_guild(), 200), (wrong_chan, 200),
    ])
    with patch("urllib.request.urlopen", side_effect=seq):
        a = UserTokenAdapter()
        a._delay = lambda: None
        with pytest.raises(AdapterError) as exc:
            a.connect("tok", "222", "333")
    assert exc.value.code == "TARGET_NOT_ALLOWED"


def test_execute_sends_slash_interaction():
    """execute() posts to /interactions when bump command is known."""
    posted_bodies = []

    def fake_urlopen(req, timeout=None):
        if "interactions" in req.get_full_url():
            posted_bodies.append(json.loads(req.data))
            return _FakeResponse({}, 204)   # Discord returns 204
        # channel messages poll returns empty list
        return _FakeResponse([], 200)

    a = UserTokenAdapter()
    a._delay = lambda: None
    a._token       = "tok"
    a._guild_id    = "222"
    a._channel_id  = "333"
    a._connected   = True
    a._bump_cmd_id = "99"
    a._bump_cmd_version = "100"

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = a.execute("222", "333", "bump!", "op-001")

    assert result.success is True
    assert len(posted_bodies) == 1
    body = posted_bodies[0]
    assert body["type"] == 2                        # APPLICATION_COMMAND
    assert body["data"]["name"] == "bump"
    assert body["application_id"] == "302050872383242240"
    assert body["guild_id"] == "222"
    assert body["channel_id"] == "333"


def test_execute_cooldown_detection():
    """DISBOARD 'please wait X minutes' triggers RATE_LIMITED."""
    disboard_reply = {
        "id": "msg-disboard",
        "author": {"id": "302050872383242240"},
        "content": "Please wait 78 minutes before bumping again.",
        "embeds": [],
    }

    def fake_urlopen(req, timeout=None):
        url = req.get_full_url()
        if "interactions" in url:
            return _FakeResponse({}, 204)
        if "messages" in url and "limit" in url:
            return _FakeResponse([disboard_reply], 200)
        return _FakeResponse({}, 200)

    a = UserTokenAdapter()
    a._delay = lambda: None
    a._token, a._guild_id, a._channel_id = "tok", "222", "333"
    a._connected = True
    a._bump_cmd_id = "99"
    a._bump_cmd_version = "100"

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(AdapterError) as exc:
            a.execute("222", "333", "bump!", "op-cd")

    assert exc.value.code == "RATE_LIMITED"
    assert exc.value.retry_after_ms == 78 * 60 * 1000
    assert exc.value.retryable is True


def test_execute_fallback_text_when_no_command():
    """When bump command not found, sends !d bump text."""
    posted = []

    def fake_urlopen(req, timeout=None):
        if req.get_method() == "POST" and "messages" in req.get_full_url():
            posted.append(json.loads(req.data))
            return _FakeResponse({"id": "msg-fallback"}, 200)
        return _FakeResponse({}, 200)

    a = UserTokenAdapter()
    a._delay = lambda: None
    a._token, a._guild_id, a._channel_id = "tok", "222", "333"
    a._connected = True
    a._bump_cmd_id = None      # no command found

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = a.execute("222", "333", "bump!", "op-fb")

    assert result.success is True
    assert len(posted) == 1
    assert posted[0]["content"] == "!d bump"
    assert result.external_id == "msg-fallback"


def test_execute_confirmed_by_disboard():
    """DISBOARD 'bumped' reply → verified=True."""
    disboard_ok = {
        "id": "msg-ok",
        "author": {"id": "302050872383242240"},
        "content": "Bump done! Your server has been bumped.",
        "embeds": [],
    }

    def fake_urlopen(req, timeout=None):
        url = req.get_full_url()
        if "interactions" in url:
            return _FakeResponse({}, 204)
        if "messages" in url and "limit" in url:
            return _FakeResponse([disboard_ok], 200)
        return _FakeResponse({}, 200)

    a = UserTokenAdapter()
    a._delay = lambda: None
    a._token, a._guild_id, a._channel_id = "tok", "222", "333"
    a._connected = True
    a._bump_cmd_id = "99"
    a._bump_cmd_version = "100"

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = a.execute("222", "333", "bump!", "op-ok")

    assert result.success is True
    assert result.verified is True
    assert result.external_id == "msg-ok"


def test_verify_result_message_id():
    """verify_result() GETs the message and confirms it."""
    msg = {"id": "msg-777", "content": "!d bump"}

    def fake_urlopen(req, timeout=None):
        if "msg-777" in req.get_full_url():
            return _FakeResponse(msg, 200)
        return _FakeResponse({}, 200)

    a = UserTokenAdapter()
    a._delay = lambda: None
    a._token, a._channel_id = "tok", "333"
    a._connected = True

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        vr = a.verify_result("op-v", "msg-777")

    assert vr.verified is True


def test_disconnect():
    a = UserTokenAdapter()
    a._token = "tok"
    a._connected = True
    a.disconnect()
    assert not a.is_connected()
    assert a._token is None


# ── Integration: manager picks correct adapter ────────────────────────────────

def test_manager_picks_user_token_adapter(tmp_db):
    from app.scheduler.manager import SchedulerManager
    from app.adapters.user_token import UserTokenAdapter as UTA
    from app.scheduler.models import AccountConfig

    chosen = []

    def factory(token_type):
        a = SchedulerManager._default_adapter(token_type)
        chosen.append(type(a).__name__)
        return a

    mgr = SchedulerManager(db=tmp_db, max_workers=1, adapter_factory=factory)
    cfg = AccountConfig(
        account_id="uta-01", name="UserTest", token_type="user",
        guild_id="111111111111111111", channel_id="222222222222222222",
    )
    store_credential(cfg.account_id, "tok")
    mgr.add_or_update_account(cfg, "tok")
    mgr.shutdown(timeout=2)
    clear_all()

    assert "UserTokenAdapter" in chosen, f"Got: {chosen}"
    assert "OfficialBotAdapter" not in chosen, f"Wrong adapter: {chosen}"


def test_manager_picks_bot_adapter(tmp_db):
    from app.scheduler.manager import SchedulerManager
    from app.scheduler.models import AccountConfig

    chosen = []

    def factory(token_type):
        a = SchedulerManager._default_adapter(token_type)
        chosen.append(type(a).__name__)
        return a

    mgr = SchedulerManager(db=tmp_db, max_workers=1, adapter_factory=factory)
    cfg = AccountConfig(
        account_id="bot-02", name="BotTest", token_type="bot",
        guild_id="111111111111111111", channel_id="222222222222222222",
    )
    store_credential(cfg.account_id, "tok")
    mgr.add_or_update_account(cfg, "tok")
    mgr.shutdown(timeout=2)
    clear_all()

    assert "OfficialBotAdapter" in chosen, f"Got: {chosen}"


def test_rand_nonce_is_valid_snowflake():
    for _ in range(20):
        n = _rand_nonce()
        assert n.isdigit()
        assert 16 <= len(n) <= 20, f"Bad nonce length: {n!r}"


def test_rand_session_is_hex():
    s = _rand_session()
    assert len(s) == 32
    assert all(c in "0123456789abcdef" for c in s)
