"""Shared pytest fixtures."""
from __future__ import annotations
import tempfile
import threading
from pathlib import Path
import pytest

from app.storage.database import Database, reset_db
from app.scheduler.models import AccountConfig
from app.services.credentials import store_credential, clear_all as clear_creds
from app.adapters.mock import MockAdapter


@pytest.fixture()
def tmp_db():
    """Fresh SQLite database in a temp file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    db = Database(path)
    yield db
    db.close()
    path.unlink(missing_ok=True)


@pytest.fixture()
def account():
    """A valid AccountConfig with a credential."""
    cfg = AccountConfig(
        account_id="test-acct-01",
        name="Test Account",
        token_type="bot",
        guild_id="123456789012345678",
        channel_id="987654321098765432",
        bump_message="bump!",
        cooldown_minutes=0.01,    # very short for tests
        offset_max_minutes=0.005,
    )
    store_credential(cfg.account_id, "fake-token-for-tests")
    yield cfg
    clear_creds()


@pytest.fixture()
def mock_adapter():
    return MockAdapter()


@pytest.fixture()
def fast_manager(tmp_db, account):
    """SchedulerManager wired to MockAdapter for speed."""
    from app.scheduler.manager import SchedulerManager

    def _factory(_type):
        return MockAdapter()

    mgr = SchedulerManager(
        db=tmp_db,
        max_workers=3,
        adapter_factory=_factory,
    )
    mgr.start()
    yield mgr
    mgr.shutdown(timeout=5)
