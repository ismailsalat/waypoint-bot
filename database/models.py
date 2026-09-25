"""Database schema.

Written so the exact same models work on PostgreSQL (production) and SQLite
(the test suite). That means plain JSON columns instead of JSONB, and no
Postgres-only types.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# BigInteger autoincrement is not supported by SQLite, so surrogate keys fall
# back to Integer there.
PK = BigInteger().with_variant(Integer, "sqlite")
SNOWFLAKE = BigInteger


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    """Network-wide settings, edited on the dashboard."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Server(Base):
    """Every guild the bot has ever been in."""

    __tablename__ = "servers"

    guild_id: Mapped[int] = mapped_column(SNOWFLAKE, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(200), default="")
    server_type: Mapped[str] = mapped_column(String(16), default="DISABLED")
    # What this server was before it was promoted to MAIN, so demoting it later
    # can put it back rather than guessing.
    pre_main_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    owner_id: Mapped[int | None] = mapped_column(SNOWFLAKE, nullable=True)
    bot_present: Mapped[bool] = mapped_column(Boolean, default=True)

    # Which main server this feeder points at. Null for MAIN/DISABLED.
    destination_guild_id: Mapped[int | None] = mapped_column(SNOWFLAKE, nullable=True)
    destination_guild_ids: Mapped[list[int] | None] = mapped_column(JSON(none_as_null=True), nullable=True)


    # Channels the bot manages here.
    funnel_channel_id: Mapped[int | None] = mapped_column(SNOWFLAKE, nullable=True)
    funnel_channel_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bump_channel_id: Mapped[int | None] = mapped_column(SNOWFLAKE, nullable=True)
    bump_channel_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # The funnel post in the public channel, plus which message version it shows.
    funnel_message_id: Mapped[int | None] = mapped_column(SNOWFLAKE, nullable=True)
    funnel_message_version_id: Mapped[int | None] = mapped_column(PK, nullable=True)

    # Per-feeder overrides. NULL means "use the global default".
    dm_delay_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    funnel_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    auto_repair: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    age_enforcement: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    last_health_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    health: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrackingInvite(Base):
    """A unique invite to the main server, owned by one feeder."""

    __tablename__ = "tracking_invites"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    url: Mapped[str] = mapped_column(String(200))
    feeder_guild_id: Mapped[int] = mapped_column(SNOWFLAKE, index=True)
    main_guild_id: Mapped[int] = mapped_column(SNOWFLAKE, index=True)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PendingInviteUse(Base):
    """A confirmed use of a tracking invite with no member attached yet.

    Two people can join through the same invite between two snapshots, which
    shows up as a delta of 2. The extra uses are banked here and handed to the
    next joins that arrive with no visible invite change. Rows live in the
    database so a restart does not lose them.
    """

    __tablename__ = "pending_invite_uses"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    feeder_guild_id: Mapped[int] = mapped_column(SNOWFLAKE)
    main_guild_id: Mapped[int] = mapped_column(SNOWFLAKE, index=True)
    remaining: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemberJoin(Base):
    """Someone joined one of our guilds."""

    __tablename__ = "member_joins"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(SNOWFLAKE, index=True)
    user_id: Mapped[int] = mapped_column(SNOWFLAKE, index=True)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    server_type: Mapped[str] = mapped_column(String(16), default="FEEDER")
    # Joins made while development mode was on. Kept for debugging, never counted.
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DMEvent(Base):
    """One funnel DM attempt (or a deliberate skip)."""

    __tablename__ = "dm_events"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(SNOWFLAKE, index=True)
    feeder_guild_id: Mapped[int | None] = mapped_column(SNOWFLAKE, index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    message_version_id: Mapped[int | None] = mapped_column(PK, nullable=True)
    tag_experiment_id: Mapped[int | None] = mapped_column(PK, nullable=True)
    invite_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class MessageVersion(Base):
    """A draft or published version of a funnel message."""

    __tablename__ = "message_versions"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))          # DM | PUBLIC
    scope: Mapped[str] = mapped_column(String(16))         # GLOBAL | FEEDER
    guild_id: Mapped[int | None] = mapped_column(SNOWFLAKE, nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    content: Mapped[Any] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TagExperiment(Base):
    """The set of DISBOARD tags a feeder was using during a window of time."""

    __tablename__ = "tag_experiments"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    feeder_guild_id: Mapped[int] = mapped_column(SNOWFLAKE, index=True)
    tags: Mapped[Any] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Conversion(Base):
    """Someone joined the main server. Attributed to a feeder when possible."""

    __tablename__ = "conversions"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(SNOWFLAKE, index=True)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    destination_guild_id: Mapped[int] = mapped_column(SNOWFLAKE, index=True)
    source_guild_id: Mapped[int | None] = mapped_column(SNOWFLAKE, nullable=True, index=True)
    invite_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tag_experiment_id: Mapped[int | None] = mapped_column(PK, nullable=True)
    message_version_id: Mapped[int | None] = mapped_column(PK, nullable=True)
    attribution: Mapped[str] = mapped_column(String(16), default="UNKNOWN")
    # Joins made while development mode is on. Kept, shown, never counted.
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    feeder_join_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dm_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    main_join_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class RoleRule(Base):
    """IF user receives role X THEN do Y. Deliberately tiny."""

    __tablename__ = "role_rules"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(SNOWFLAKE, index=True)
    role_id: Mapped[int] = mapped_column(SNOWFLAKE)
    role_name: Mapped[str] = mapped_column(String(100), default="")
    action: Mapped[str] = mapped_column(String(16), default="LOG_ONLY")
    target_role_id: Mapped[int | None] = mapped_column(SNOWFLAKE, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Plain-language record of anything the bot or dashboard changed."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    guild_id: Mapped[int | None] = mapped_column(SNOWFLAKE, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="bot")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Task(Base):
    """Work the dashboard asks the bot to do (it has no Discord connection)."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[Any] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    guild_id: Mapped[int | None] = mapped_column(SNOWFLAKE, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index("ix_message_versions_lookup", MessageVersion.kind, MessageVersion.scope, MessageVersion.guild_id)
Index("ix_dm_events_user_feeder", DMEvent.user_id, DMEvent.feeder_guild_id)
