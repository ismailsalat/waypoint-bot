"""
test_configuration.py
----------------------
Validates AccountConfig.validate() catches bad input.
"""
from __future__ import annotations
import pytest
from app.scheduler.models import AccountConfig


def _valid():
    return AccountConfig(
        account_id="cfg-01", name="Valid",
        guild_id="123456789012345678",
        channel_id="987654321098765432",
        cooldown_minutes=120, offset_max_minutes=30,
    )


def test_valid_config_no_errors():
    assert _valid().validate() == []


def test_missing_name():
    c = _valid(); c.name = ""
    errs = c.validate()
    assert any("name" in e.lower() for e in errs)


def test_missing_guild():
    c = _valid(); c.guild_id = ""
    errs = c.validate()
    assert any("guild" in e.lower() for e in errs)


def test_non_numeric_guild():
    c = _valid(); c.guild_id = "not-a-number"
    errs = c.validate()
    assert any("numeric" in e.lower() or "snowflake" in e.lower() for e in errs)


def test_missing_channel():
    c = _valid(); c.channel_id = ""
    errs = c.validate()
    assert any("channel" in e.lower() for e in errs)


def test_zero_cooldown():
    c = _valid(); c.cooldown_minutes = 0
    errs = c.validate()
    assert any("cooldown" in e.lower() for e in errs)


def test_negative_cooldown():
    c = _valid(); c.cooldown_minutes = -5
    errs = c.validate()
    assert any("cooldown" in e.lower() for e in errs)


def test_offset_exceeds_cooldown():
    c = _valid(); c.offset_max_minutes = 200
    errs = c.validate()
    assert any("offset" in e.lower() for e in errs)


def test_negative_offset():
    c = _valid(); c.offset_max_minutes = -1
    errs = c.validate()
    assert any("offset" in e.lower() for e in errs)
