"""
test_persistence.py
-------------------
Verifies SQLite schema, CRUD, transactions, and uniqueness constraints.
"""
from __future__ import annotations
import pytest
from app.storage.database import Database
from app.scheduler.models import AccountConfig


def test_schema_created(tmp_db):
    tables = {r[0] for r in
              tmp_db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in ("accounts","jobs","job_runs","operations","settings","schema_version"):
        assert t in tables, f"Missing table: {t}"


def test_upsert_and_get_account(tmp_db):
    cfg = AccountConfig(account_id="acc-01", name="MyBot",
                        guild_id="1234567890", channel_id="9876543210")
    tmp_db.upsert_account(cfg.to_db_dict())
    row = tmp_db.get_account("acc-01")
    assert row is not None
    assert row["name"] == "MyBot"


def test_upsert_account_updates_existing(tmp_db):
    cfg = AccountConfig(account_id="acc-02", name="Before",
                        guild_id="111", channel_id="222")
    tmp_db.upsert_account(cfg.to_db_dict())
    cfg.name = "After"
    tmp_db.upsert_account(cfg.to_db_dict())
    row = tmp_db.get_account("acc-02")
    assert row["name"] == "After"


def test_delete_account_cascades_jobs(tmp_db):
    cfg = AccountConfig(account_id="acc-03", name="Del",
                        guild_id="1", channel_id="2")
    tmp_db.upsert_account(cfg.to_db_dict())
    tmp_db.upsert_job("job-03", "acc-03", status="STOPPED")
    tmp_db.delete_account("acc-03")
    assert tmp_db.get_account("acc-03") is None
    assert tmp_db.get_job("job-03") is None


def test_operation_uniqueness(tmp_db):
    cfg = AccountConfig(account_id="acc-04", name="Op",
                        guild_id="1", channel_id="2")
    tmp_db.upsert_account(cfg.to_db_dict())
    tmp_db.upsert_job("job-04", "acc-04", status="STOPPED")

    ok1 = tmp_db.create_operation("op-unique-01", "job-04", "acc-04")
    ok2 = tmp_db.create_operation("op-unique-01", "job-04", "acc-04")  # duplicate
    assert ok1 is True
    assert ok2 is False, "Duplicate operation_id should be rejected"


def test_settings_roundtrip(tmp_db):
    tmp_db.set_setting("theme", "dark")
    assert tmp_db.get_setting("theme") == "dark"
    assert tmp_db.get_setting("missing", "default") == "default"


def test_job_run_insert_and_list(tmp_db):
    cfg = AccountConfig(account_id="acc-05", name="Run",
                        guild_id="1", channel_id="2")
    tmp_db.upsert_account(cfg.to_db_dict())
    tmp_db.upsert_job("job-05", "acc-05", status="STOPPED")
    tmp_db.insert_run({
        "job_id": "job-05", "account_id": "acc-05",
        "operation_id": "op-run-01", "status": "VERIFIED",
        "attempt": 1, "started_at": "2025-01-01T00:00:00.000Z",
    })
    runs = tmp_db.list_runs("job-05")
    assert len(runs) == 1
    assert runs[0]["operation_id"] == "op-run-01"


def test_db_health(tmp_db):
    assert tmp_db.health() == "healthy"
