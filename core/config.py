"""Environment configuration.

Two kinds of value live here.

Required, and deliberately not editable from the dashboard, because they are
secrets or decide who is trusted:

    DISCORD_BOT_TOKEN, DATABASE_URL, OWNER_USER_IDS

Optional first-run defaults, read only while the matching setting has never
been written to the database:

    MAIN_GUILD_ID, DEVELOPMENT_MODE, ADMIN_TEST_USER_ID

Everything else the owner might want to change day to day lives in the database
and is edited on the dashboard.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int_or_none(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _id_set(raw: str | None) -> set[int]:
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    discord_bot_token: str = ""
    # Blank means "not set". Locally that resolves to a SQLite file; on Railway
    # it is an error. See database.database.resolve_database_url.
    database_url: str = ""
    owner_user_ids: set[int] = field(default_factory=set)
    admin_test_user_id: int | None = None
    main_guild_id: int | None = None
    development_mode: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000

    # The three values below are bootstrap defaults. The `_env` names make it
    # obvious at the call site that the database takes priority; see
    # core.settings for the resolution.
    @property
    def development_mode_env(self) -> bool:
        return self.development_mode

    @property
    def admin_test_user_id_env(self) -> int | None:
        return self.admin_test_user_id

    def is_approved_owner(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.owner_user_ids

    # Status for the dashboard. Never the values themselves.
    @property
    def token_configured(self) -> bool:
        return bool(self.discord_bot_token)

    @property
    def owners_configured(self) -> bool:
        return bool(self.owner_user_ids)

    @property
    def database_configured(self) -> bool:
        """Whether DATABASE_URL was supplied at all. The value itself is never
        exposed; see database.database.database_status for what is shown."""
        return bool((self.database_url or "").strip())


def load_config() -> Config:
    return Config(
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", "").strip(),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        owner_user_ids=_id_set(os.getenv("OWNER_USER_IDS")),
        admin_test_user_id=_int_or_none(os.getenv("ADMIN_TEST_USER_ID")),
        main_guild_id=_int_or_none(os.getenv("MAIN_GUILD_ID")),
        development_mode=_bool(os.getenv("DEVELOPMENT_MODE"), False),
        dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1").strip(),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8000")),
    )


config = load_config()
