"""
test_logging.py
----------------
Verifies that secrets are redacted from log output.
"""
from __future__ import annotations
import logging
import io
import pytest

from app.services.logging_service import _SecretRedactor


def _filtered(msg: str) -> str:
    record = logging.LogRecord("test", logging.INFO, "", 0, msg, (), None)
    f = _SecretRedactor()
    f.filter(record)
    return str(record.msg)


def test_token_redacted():
    token = "TEST_DISCORD_TOKEN"
    out = _filtered(f"Connecting with token {token}")
    assert token not in out
    assert "[REDACTED]" in out


def test_authorization_header_redacted():
    out = _filtered("Authorization: Bot MySecretToken12345")
    assert "MySecretToken12345" not in out
    assert "[REDACTED]" in out


def test_json_token_redacted():
    out = _filtered('{"token": "supersecretvalue123456789abcdef"}')
    assert "supersecretvalue123456789abcdef" not in out


def test_normal_message_unchanged():
    msg = "Job sched-01 completed successfully"
    out = _filtered(msg)
    assert out == msg


def test_partial_token_not_matched():
    """Short strings that don't match the token pattern should be untouched."""
    short = "abc.def.ghi"
    out = _filtered(f"Some value: {short}")
    assert short in out  # too short to match token regex
