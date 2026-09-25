"""Engine and session handling.

Which database you get is decided here, from the environment, with no code
changes between your laptop and Railway:

    local, no DATABASE_URL          -> SQLite in ./funnel.db, created on start
    local, sqlite DATABASE_URL      -> that SQLite file
    local, postgres DATABASE_URL    -> that PostgreSQL server (deliberate)
    Railway, postgres DATABASE_URL  -> that PostgreSQL server
    Railway, no DATABASE_URL        -> a clear error, never a SQLite fallback

The last rule matters: Railway's filesystem is replaced on every deploy, so a
funnel.db there would look like it was working while quietly losing your data
each time you shipped.

Railway hands out a DATABASE_URL like `postgresql://...`. SQLAlchemy's async
engine needs the asyncpg driver spelled out, so we fix the URL here instead of
asking you to remember it. The transformed URL is never logged or displayed.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core.config import config
from database.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

DEFAULT_SQLITE_URL = "sqlite+aiosqlite:///./funnel.db"

# Railway sets several of these in every deployment. Any one of them is enough
# to tell us we are not on someone's laptop.
RAILWAY_MARKERS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_DEPLOYMENT_ID",
    "RAILWAY_PUBLIC_DOMAIN",
)


class DatabaseNotConfigured(RuntimeError):
    """Raised instead of quietly falling back to a database you did not mean."""


def running_on_railway() -> bool:
    return any(os.getenv(marker) for marker in RAILWAY_MARKERS)


def normalize_database_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return DEFAULT_SQLITE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("sqlite:///"):
        url = "sqlite+aiosqlite:///" + url[len("sqlite:///"):]
    return url


def resolve_database_url(raw: str | None = None, on_railway: bool | None = None) -> str:
    """The database URL this process should use, ready for SQLAlchemy.

    Raises DatabaseNotConfigured on Railway when there is nothing usable, so a
    misconfigured deploy stops loudly rather than inventing a local file.
    """
    raw = (config.database_url if raw is None else raw or "").strip()
    on_railway = running_on_railway() if on_railway is None else on_railway

    if not raw:
        if on_railway:
            raise DatabaseNotConfigured(
                "Railway environment detected but PostgreSQL DATABASE_URL is not "
                "configured. Add a PostgreSQL service and set "
                "DATABASE_URL=${{Postgres.DATABASE_URL}} on this service. "
                "SQLite is not used on Railway, because its filesystem is "
                "replaced on every deploy."
            )
        return DEFAULT_SQLITE_URL

    url = normalize_database_url(raw)
    if on_railway and url.startswith("sqlite"):
        raise DatabaseNotConfigured(
            "Railway environment detected but DATABASE_URL points at SQLite. "
            "Production must use PostgreSQL; set "
            "DATABASE_URL=${{Postgres.DATABASE_URL}} on this service."
        )
    return url


def database_status(connected: bool | None = None) -> dict[str, object]:
    """What to show on the dashboard. Never includes host, user or password."""
    on_railway = running_on_railway()
    explicit = bool((config.database_url or "").strip())
    try:
        url = resolve_database_url()
        error = ""
    except DatabaseNotConfigured as exc:
        url, error = "", str(exc)

    is_sqlite = url.startswith("sqlite")
    file_name = ""
    if is_sqlite:
        file_name = Path(url.split("///", 1)[-1]).name or "funnel.db"

    return {
        "environment": "Railway" if on_railway else "Local",
        "on_railway": on_railway,
        "kind": "SQLite" if is_sqlite else ("PostgreSQL" if url else "not configured"),
        "file": file_name,
        # A PostgreSQL connection is the production database, wherever the
        # dashboard happens to be running from.
        "is_production": bool(url) and not is_sqlite,
        "explicitly_configured": explicit,
        "connected": bool(connected) if connected is not None else bool(url),
        "error": error,
    }


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        url = resolve_database_url()
        kwargs: dict = {"echo": False, "future": True}
        if url.startswith("sqlite"):
            # The bot and the dashboard are two processes on one file. WAL lets
            # them read while the other writes, and the busy timeout makes a
            # brief collision wait instead of raising "database is locked".
            kwargs["connect_args"] = {"timeout": 30}
        else:
            kwargs.update(pool_pre_ping=True, pool_size=5, max_overflow=5)
        _engine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            _enable_sqlite_concurrency(_engine)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _enable_sqlite_concurrency(engine: AsyncEngine) -> None:
    """Turn on WAL and a busy timeout for every new SQLite connection."""
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def reset_engine() -> None:
    """Forget the current engine so the next call resolves the URL again.

    Used when simulating a restart; it never touches the database itself.
    """
    global _engine, _session_factory
    from core import settings as settings_store

    _engine = None
    _session_factory = None
    settings_store.invalidate_cache()


def set_engine(engine: AsyncEngine) -> None:
    """Used by the tests to point everything at a throwaway SQLite database."""
    global _engine, _session_factory
    from core import settings as settings_store

    _engine = engine
    _session_factory = async_sessionmaker(engine, expire_on_commit=False)
    settings_store.invalidate_cache()


# Columns added after the first release. create_all only makes missing tables,
# so a database that already exists needs these added by hand. Both statements
# are valid on PostgreSQL and SQLite, and adding a column that is already there
# is skipped rather than retried.
LATER_COLUMNS: dict[str, dict[str, str]] = {
    "dm_events": {"tag_experiment_id": "BIGINT"},
    "servers": {"pre_main_type": "VARCHAR(16)", "destination_guild_ids": "JSON"},
    "conversions": {"is_test": "BOOLEAN DEFAULT FALSE"},
    "member_joins": {"is_test": "BOOLEAN DEFAULT FALSE"},
    "tasks": {
        "error": "TEXT",
        "attempts": "INTEGER DEFAULT 0",
        "guild_id": "BIGINT",
        "started_at": "TIMESTAMP",
    },
}


def _add_missing_columns(sync_conn) -> list[str]:
    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    for table, columns in LATER_COLUMNS.items():
        if table not in existing_tables:
            continue
        present = {column["name"] for column in inspector.get_columns(table)}
        for name, sql_type in columns.items():
            if name not in present:
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
                added.append(f"{table}.{name}")
    return added


async def init_db() -> None:
    """Create any missing tables and columns. Safe to run on every start."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        added = await conn.run_sync(_add_missing_columns)
    if added:
        import logging

        logging.getLogger("funnel").info("Added new columns: %s", ", ".join(added))


def session() -> AsyncSession:
    return get_session_factory()()


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite gives back naive datetimes; Postgres gives aware ones. Normalise
    so comparisons never raise."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
