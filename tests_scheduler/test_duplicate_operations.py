"""
test_duplicate_operations.py
-----------------------------
Ensures operation IDs are unique and duplicates are rejected.
"""
from __future__ import annotations
import pytest
from app.storage.database import Database


def test_duplicate_op_id_rejected(tmp_db):
    from app.scheduler.models import AccountConfig
    cfg = AccountConfig(account_id="dup-01", name="Dup",
                        guild_id="1", channel_id="2")
    tmp_db.upsert_account(cfg.to_db_dict())
    tmp_db.upsert_job("job-dup-01", "dup-01", status="STOPPED")

    op_id = "op-dup-abc-123"
    r1 = tmp_db.create_operation(op_id, "job-dup-01", "dup-01")
    r2 = tmp_db.create_operation(op_id, "job-dup-01", "dup-01")
    assert r1 is True
    assert r2 is False, "Duplicate operation must be rejected"


def test_different_op_ids_accepted(tmp_db):
    from app.scheduler.models import AccountConfig
    cfg = AccountConfig(account_id="dup-02", name="Dup2",
                        guild_id="1", channel_id="2")
    tmp_db.upsert_account(cfg.to_db_dict())
    tmp_db.upsert_job("job-dup-02", "dup-02", status="STOPPED")

    for i in range(5):
        ok = tmp_db.create_operation(f"op-{i:04d}", "job-dup-02", "dup-02")
        assert ok is True, f"op-{i} rejected unexpectedly"


def test_completed_operation_not_re_executed(tmp_db):
    """
    Once an operation is VERIFIED, attempting to create the same ID again
    must be blocked.
    """
    from app.scheduler.models import AccountConfig
    cfg = AccountConfig(account_id="dup-03", name="Dup3",
                        guild_id="1", channel_id="2")
    tmp_db.upsert_account(cfg.to_db_dict())
    tmp_db.upsert_job("job-dup-03", "dup-03", status="STOPPED")

    op_id = "op-completed-xyz"
    tmp_db.create_operation(op_id, "job-dup-03", "dup-03")
    tmp_db.complete_operation(op_id, "VERIFIED", external_id="ext-123")

    # Try again — should be blocked
    ok = tmp_db.create_operation(op_id, "job-dup-03", "dup-03")
    assert ok is False
