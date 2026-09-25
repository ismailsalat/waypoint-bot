"""Which database gets used, and that nothing leaks while showing it.

The selection rules are deliberately boring: local falls back to a SQLite file,
Railway must be told about PostgreSQL or it refuses to start. These tests pin
every branch of that, plus the fact that restarting reuses the same file.
"""
from __future__ import annotations

import pathlib

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from bot import main as bot_main
from core import constants, settings as settings_store
from core.config import config
from database import crud, database as db_module
from database.database import (
    DatabaseNotConfigured,
    database_status,
    init_db,
    normalize_database_url,
    reset_engine,
    resolve_database_url,
    session,
)
from database.models import Setting

POSTGRES_URL = "postgresql://funnel_user:hunter2@containers.railway.app:5432/railway"


@pytest.fixture
def no_railway(monkeypatch):
    for marker in db_module.RAILWAY_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    return True


@pytest.fixture
def on_railway(monkeypatch):
    for marker in db_module.RAILWAY_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    return True


# ==========================================================================
# 1-5. Choosing a database
# ==========================================================================
def test_local_with_no_database_url_uses_sqlite(no_railway, monkeypatch):
    monkeypatch.setattr(config, "database_url", "")
    assert resolve_database_url() == "sqlite+aiosqlite:///./funnel.db"
    assert db_module.running_on_railway() is False


def test_local_with_an_explicit_sqlite_url_uses_it(no_railway, monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite:///./mine.db")
    url = resolve_database_url()
    assert url.startswith("sqlite+aiosqlite:///")
    assert url.endswith("mine.db")


def test_local_with_a_postgres_url_uses_postgres(no_railway, monkeypatch):
    """Connecting your laptop to production is allowed, but only on purpose."""
    monkeypatch.setattr(config, "database_url", POSTGRES_URL)
    url = resolve_database_url()
    assert url.startswith("postgresql+asyncpg://")
    status = database_status()
    assert status["environment"] == "Local"
    assert status["kind"] == "PostgreSQL"
    assert status["is_production"] is True


def test_railway_with_a_postgres_url_uses_postgres(on_railway, monkeypatch):
    monkeypatch.setattr(config, "database_url", POSTGRES_URL)
    assert resolve_database_url().startswith("postgresql+asyncpg://")
    status = database_status()
    assert status["environment"] == "Railway"
    assert status["kind"] == "PostgreSQL"
    assert status["is_production"] is True


def test_railway_without_a_database_url_fails_safely(on_railway, monkeypatch):
    monkeypatch.setattr(config, "database_url", "")
    with pytest.raises(DatabaseNotConfigured) as caught:
        resolve_database_url()
    assert "Railway environment detected" in str(caught.value)
    assert "DATABASE_URL" in str(caught.value)

    # No silent SQLite anywhere in the reported status either.
    status = database_status()
    assert status["kind"] == "not configured"
    assert status["file"] == ""
    assert "Railway" in str(status["error"])


def test_railway_refuses_a_sqlite_url(on_railway, monkeypatch):
    monkeypatch.setattr(config, "database_url", "sqlite+aiosqlite:///./funnel.db")
    with pytest.raises(DatabaseNotConfigured):
        resolve_database_url()


def test_the_bot_stops_before_connecting_when_railway_has_no_database(on_railway, monkeypatch):
    monkeypatch.setattr(config, "database_url", "")
    monkeypatch.setattr(config, "discord_bot_token", "a-token")
    with pytest.raises(SystemExit) as caught:
        bot_main.preflight()
    assert "Railway environment detected" in str(caught.value)


def test_railway_url_forms_are_both_accepted():
    assert normalize_database_url("postgres://u:p@h:5432/db").startswith("postgresql+asyncpg://")
    assert normalize_database_url("postgresql://u:p@h:5432/db").startswith("postgresql+asyncpg://")


# ==========================================================================
# 6-8. The local file is created, reused, and shared by both processes
# ==========================================================================
@pytest_asyncio.fixture
async def local_sqlite(tmp_path, no_railway, monkeypatch):
    """Point the whole application at a throwaway SQLite file, as a local run
    would, and put the real engine back afterwards."""
    previous_engine = db_module._engine
    previous_factory = db_module._session_factory
    path = tmp_path / "funnel.db"
    monkeypatch.setattr(config, "database_url", f"sqlite+aiosqlite:///{path}")
    reset_engine()
    yield path
    engine = db_module._engine
    if engine is not None:
        await engine.dispose()
    db_module._engine = previous_engine
    db_module._session_factory = previous_factory
    settings_store.invalidate_cache()


async def test_the_local_database_file_is_created_automatically(local_sqlite):
    assert not local_sqlite.exists()

    await init_db()  # what start-up does, with no separate command

    assert local_sqlite.exists()
    async with session() as db:
        await crud.ensure_default_messages(db)
        content, _ = await crud.message_content(db, constants.KIND_DM)
        assert content["body"]


async def test_restarting_reuses_the_same_file_and_keeps_everything(local_sqlite):
    await init_db()
    async with session() as db:
        await settings_store.set_many(db, {"network_name": "Side Quest Network"})
        await crud.upsert_server(db, 111, "Side Quest", 222, constants.MAIN)
        await settings_store.set_many(db, {"main_guild_id": 111})
        await crud.set_tags(db, 111, ["movies"])
        await crud.log(db, "before_restart", "written before the restart")

    # Simulate stopping and starting the process.
    await db_module._engine.dispose()
    reset_engine()
    await init_db()

    async with session() as db:
        assert await settings_store.main_guild_id(db) == 111
        assert (await settings_store.get_all(db, fresh=True))["network_name"] == "Side Quest Network"
        server = await crud.get_server(db, 111)
        assert server is not None and server.server_type == constants.MAIN
        assert [e.tags for e in await crud.list_experiments(db, 111)] == [["movies"]]
        assert any(entry.action == "before_restart" for entry in await crud.recent_logs(db))
        # Nothing was reset or duplicated.
        assert len((await db.execute(select(Setting))).scalars().all()) >= 2


async def test_the_bot_and_the_dashboard_resolve_the_same_database(local_sqlite):
    """Both entry points go through the same resolver, so they cannot drift."""
    await init_db()
    async with session() as db:
        await crud.log(db, "written_by_bot", "from the bot process")

    from dashboard.app import get_db

    # The dashboard's dependency uses the same session factory.
    agen = get_db()
    dashboard_db = await agen.__anext__()
    try:
        assert any(e.action == "written_by_bot" for e in await crud.recent_logs(dashboard_db))
    finally:
        await agen.aclose()

    assert bot_main.preflight.__module__ == "bot.main"
    assert database_status()["file"] == "funnel.db"


# ==========================================================================
# 9. Status without credentials
# ==========================================================================
@pytest_asyncio.fixture
async def client(network):
    from dashboard.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def test_the_status_panel_shows_the_kind_but_never_the_url(client, monkeypatch, no_railway):
    monkeypatch.setattr(config, "database_url", POSTGRES_URL)

    page = (await client.get("/settings")).text

    assert "PostgreSQL" in page
    assert "PRODUCTION DATABASE" in page  # the banner is loud on purpose
    for secret in ("hunter2", "funnel_user", "containers.railway.app", POSTGRES_URL):
        assert secret not in page


async def test_a_local_run_is_labelled_local(client, no_railway, monkeypatch):
    monkeypatch.setattr(config, "database_url", "")
    page = (await client.get("/settings")).text
    assert "LOCAL DATABASE" in page
    assert "PRODUCTION DATABASE" not in page
    assert "funnel.db" in page


# ==========================================================================
# 13 & 15. The two regressions
# ==========================================================================
async def test_api_health_returns_exactly_this_shape(client, network, no_railway, monkeypatch):
    """Pin the response, key by key.

    A dict literal with the same key twice is legal Python: the second one
    silently wins and the first value never reaches the caller. Asserting the
    full key set is what makes that visible from a test.
    """
    monkeypatch.setattr(config, "database_url", "")
    body = (await client.get("/api/health")).json()

    assert set(body) == {
        "feeders",
        "feeder_joins",
        "conversions",
        "development_mode",
        "environment",
        "database",
        "production_database",
    }
    assert body["feeders"] == 1
    assert body["feeder_joins"] == 0
    assert body["conversions"] == 0
    assert body["development_mode"] is False
    assert body["environment"] == "Local"
    assert body["database"] == "SQLite"  # the kind, not a status object
    assert isinstance(body["database"], str)
    assert body["production_database"] is False

    # And nothing resembling a connection string anywhere in it.
    for value in body.values():
        assert "://" not in str(value)


async def test_api_health_reports_the_current_development_mode(client, network):
    assert (await client.get("/api/health")).json()["development_mode"] is False

    async with session() as db:
        await settings_store.set_many(db, {"development_mode": True})
    assert (await client.get("/api/health")).json()["development_mode"] is True

    async with session() as db:
        await settings_store.set_many(db, {"development_mode": False})
    body = (await client.get("/api/health")).json()
    assert body["development_mode"] is False
    assert body["environment"] in ("Local", "Railway")


def test_no_dictionary_in_the_project_repeats_a_key():
    """Catch the whole class of bug, not just this one endpoint.

    Python accepts a repeated key and quietly drops the earlier value, so this
    walks every dict literal in the source and fails on any duplicate.
    """
    import ast

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "release_env" in path.parts or "clean_test_env" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            duplicates = {k for k in keys if keys.count(k) > 1}
            if duplicates:
                offenders.append(f"{path.relative_to(root)}:{node.lineno} {sorted(duplicates)}")

    assert offenders == [], "duplicate keys found: " + "; ".join(offenders)


async def test_message_studio_points_at_settings_not_the_env_file(client, network):
    page = (await client.get("/messages")).text
    assert "Set the Test DM User ID in" in page
    assert "ADMIN_TEST_USER_ID" not in page
    assert ".env" not in page
