"""Network settings.

Nothing in this file is hardcoded to a particular server. Defaults exist so the
bot can run before you have opened the dashboard; every one of them is editable
on the Settings page, and any feeder can override the ones that make sense per
server.

Three settings can also be seeded from the environment on a completely fresh
database: the main server, development mode and the test user. That is a
convenience for the very first boot only. Once a value exists in the database
the database wins, so you never have to edit .env again to change your mind.
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import constants
from core.config import config
from database.models import Server, Setting

DEFAULTS: dict[str, Any] = {
    "network_name": "My Network",
    "dashboard_title": "Waypoint Suite",
    "dashboard_accent": "#6f5bf0",
    "dashboard_theme": "dark",
    "dashboard_compact": False,
    "main_guild_id": None,
    "default_funnel_channel_name": "join-main",
    "default_bump_channel_name": "bump",
    "default_dm_delay_seconds": 5,
    "default_funnel_mode": constants.LIVE,
    "default_auto_repair": True,
    "default_age_enforcement": False,
    "dm_policy": constants.ONCE_PER_FEEDER,
    "dm_cooldown_days": 30,
    "failure_retry_days": 7,
    "repair_interval_minutes": 30,
    "bump_staff_role_names": ["Staff", "Moderator", "Admin"],
    "custom_variables": {},
    "development_mode": None,       # None means "no choice stored yet"
    "admin_test_user_id": None,
    "setup_complete": False,
}

# Settings the environment may seed on a fresh database, and the .env name each
# one is seeded from. After the first boot the stored value is the only one
# consulted.
ENV_BOOTSTRAP = {
    "main_guild_id": "MAIN_GUILD_ID",
    "development_mode": "DEVELOPMENT_MODE",
    "admin_test_user_id": "ADMIN_TEST_USER_ID",
}

# Settings are read often and change rarely, so they are held briefly in
# memory. The dashboard and the bot are separate processes, so the cache has to
# expire on its own for a change made in one to reach the other; a few seconds
# is short enough to feel immediate and long enough to matter.
CACHE_SECONDS = 5.0
_cache: dict[str, Any] | None = None
_cache_time = 0.0


def invalidate_cache() -> None:
    """Drop the cached settings. Called whenever they are written, and when the
    database engine is swapped (which the tests do)."""
    global _cache, _cache_time
    _cache = None
    _cache_time = 0.0

# Keys a feeder may override individually.
OVERRIDABLE = (
    "funnel_channel_name",
    "bump_channel_name",
    "dm_delay_seconds",
    "funnel_mode",
    "auto_repair",
    "age_enforcement",
)


async def get_all(db: AsyncSession, fresh: bool = False) -> dict[str, Any]:
    global _cache, _cache_time
    if not fresh and _cache is not None and (time.monotonic() - _cache_time) < CACHE_SECONDS:
        return dict(_cache)

    rows = (await db.execute(select(Setting))).scalars().all()
    values = dict(DEFAULTS)
    for row in rows:
        values[row.key] = row.value
    _cache, _cache_time = dict(values), time.monotonic()
    return values


async def get(db: AsyncSession, key: str) -> Any:
    row = await db.get(Setting, key)
    if row is None:
        return DEFAULTS.get(key)
    return row.value


async def is_stored(db: AsyncSession, key: str) -> bool:
    """Whether this setting has ever been written. Distinguishes "switched off"
    from "never configured", which is what decides env bootstrap."""
    return (await db.get(Setting, key)) is not None


async def set_many(db: AsyncSession, values: dict[str, Any]) -> None:
    for key, value in values.items():
        row = await db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=value))
        else:
            row.value = value
    await db.commit()
    invalidate_cache()


async def bootstrap_from_env(db: AsyncSession) -> list[str]:
    """Seed settings that have never been written from the environment.

    Runs on every start and does nothing at all once the value exists in the
    database, so a stale MAIN_GUILD_ID or DEVELOPMENT_MODE left in .env cannot
    overwrite a choice made on the dashboard.
    """
    seeded: list[str] = []
    env_values = {
        "main_guild_id": config.main_guild_id,
        "development_mode": config.development_mode_env,
        "admin_test_user_id": config.admin_test_user_id_env,
    }
    to_write: dict[str, Any] = {}
    for key, value in env_values.items():
        if value is None:
            continue
        if await is_stored(db, key):
            continue
        to_write[key] = value
        seeded.append(f"{ENV_BOOTSTRAP[key]} -> {key}")
    if to_write:
        await set_many(db, to_write)
    return seeded


async def main_guild_id(db: AsyncSession) -> int | None:
    """The main server. The database is the only source of truth once set."""
    value = await get(db, "main_guild_id")
    if value is None and not await is_stored(db, "main_guild_id"):
        return config.main_guild_id  # first run, before bootstrap has written it
    return int(value) if value else None


async def development_mode(db: AsyncSession) -> bool:
    """Read the live setting rather than whatever the process started with, so
    the dashboard toggle takes effect without restarting the bot."""
    if await is_stored(db, "development_mode"):
        return bool(await get(db, "development_mode"))
    return bool(config.development_mode_env)


async def admin_test_user_id(db: AsyncSession) -> int | None:
    if await is_stored(db, "admin_test_user_id"):
        value = await get(db, "admin_test_user_id")
        try:
            return int(value) if value else None
        except (TypeError, ValueError):
            return None
    return config.admin_test_user_id_env


async def main_server(db: AsyncSession) -> Server | None:
    guild_id = await main_guild_id(db)
    if guild_id is None:
        return None
    return await db.get(Server, guild_id)


async def main_server_name(db: AsyncSession) -> str:
    server = await main_server(db)
    return server.name if server else "the main server"


def resolve(settings: dict[str, Any], server: Server | None) -> dict[str, Any]:
    """Merge global defaults with a feeder's overrides.

    Pure function so it is easy to test and easy to reason about: an override
    only wins when it is not None.
    """
    resolved = {
        "funnel_channel_name": settings.get("default_funnel_channel_name"),
        "bump_channel_name": settings.get("default_bump_channel_name"),
        "dm_delay_seconds": settings.get("default_dm_delay_seconds"),
        "funnel_mode": settings.get("default_funnel_mode"),
        "auto_repair": settings.get("default_auto_repair"),
        "age_enforcement": settings.get("default_age_enforcement"),
    }
    if server is None:
        return resolved

    overrides = {
        "funnel_channel_name": server.funnel_channel_name,
        "bump_channel_name": server.bump_channel_name,
        "dm_delay_seconds": server.dm_delay_seconds,
        "funnel_mode": server.funnel_mode,
        "auto_repair": server.auto_repair,
        "age_enforcement": server.age_enforcement,
    }
    for key, value in overrides.items():
        if value is not None and value != "":
            resolved[key] = value
    return resolved


async def effective(db: AsyncSession, server: Server | None) -> dict[str, Any]:
    return resolve(await get_all(db), server)
