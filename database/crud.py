"""Data access helpers shared by the bot and the dashboard.

Everything that touches more than one table lives here so the two processes
can never disagree about what a "published message" or an "active tag
experiment" means.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core import constants, rendering, settings as settings_store
from database.database import as_utc
from database.models import (
    AuditLog,
    Conversion,
    DMEvent,
    MemberJoin,
    MessageVersion,
    PendingInviteUse,
    RoleRule,
    Server,
    TagExperiment,
    Task,
    TrackingInvite,
    utcnow,
)


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------
async def log(
    db: AsyncSession,
    action: str,
    detail: str = "",
    guild_id: int | None = None,
    source: str = "bot",
) -> AuditLog:
    entry = AuditLog(action=action, detail=detail, guild_id=guild_id, source=source)
    db.add(entry)
    await db.commit()
    return entry


async def recent_logs(db: AsyncSession, limit: int = 100) -> Sequence[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
    return (await db.execute(stmt)).scalars().all()


# --------------------------------------------------------------------------
# Servers
# --------------------------------------------------------------------------
async def upsert_server(
    db: AsyncSession,
    guild_id: int,
    name: str,
    owner_id: int | None = None,
    server_type: str | None = None,
    bot_present: bool = True,
) -> Server:
    server = await db.get(Server, guild_id)
    if server is None:
        server = Server(
            guild_id=guild_id,
            name=name,
            owner_id=owner_id,
            server_type=server_type or constants.DISABLED,
            bot_present=bot_present,
        )
        db.add(server)
    else:
        server.name = name or server.name
        if owner_id is not None:
            server.owner_id = owner_id
        if server_type is not None:
            server.server_type = server_type
        server.bot_present = bot_present
    await db.commit()
    return server


async def get_server(db: AsyncSession, guild_id: int) -> Server | None:
    return await db.get(Server, guild_id)


async def list_servers(db: AsyncSession, server_type: str | None = None) -> Sequence[Server]:
    stmt = select(Server)
    if server_type:
        stmt = stmt.where(Server.server_type == server_type)
    return (await db.execute(stmt.order_by(Server.name))).scalars().all()


async def set_main_server(db: AsyncSession, new_main_id: int | None) -> list[str]:
    """Move the MAIN marker to one server and nowhere else.

    Exactly one server is MAIN at any time. A server that stops being MAIN goes
    back to whatever it was before it was promoted, or DISABLED if that is not
    known. No conversion rows are touched, so history keeps pointing at the
    server people actually joined.
    """
    changes: list[str] = []
    for server in await list_servers(db):
        if new_main_id and server.guild_id == new_main_id:
            if server.server_type != constants.MAIN:
                server.pre_main_type = server.server_type
                server.server_type = constants.MAIN
                changes.append(f"{server.name} is now the main server")
            server.destination_guild_id = None
        elif server.server_type == constants.MAIN:
            restored = (
                server.pre_main_type
                if server.pre_main_type in (constants.FEEDER, constants.DISABLED, constants.DESTINATION)
                else constants.DISABLED
            )
            explicit_users = [f for f in await list_feeders(db) if f.destination_guild_ids and server.guild_id in f.destination_guild_ids]
            if explicit_users:
                restored = constants.DESTINATION
            server.server_type = restored
            server.pre_main_type = None
            server.destination_guild_id = new_main_id if restored == constants.FEEDER else None
            changes.append(f"{server.name} is no longer the main server, now {restored}")
        elif server.server_type == constants.FEEDER and server.destination_guild_ids is None:
            if server.destination_guild_id != new_main_id:
                server.destination_guild_id = new_main_id
                server.last_health_check = None  # forces a fresh tracking invite
    await db.commit()
    await settings_store.set_many(db, {"main_guild_id": new_main_id})
    return changes


async def list_feeders(db: AsyncSession, only_present: bool = False) -> Sequence[Server]:
    stmt = select(Server).where(Server.server_type == constants.FEEDER)
    if only_present:
        stmt = stmt.where(Server.bot_present.is_(True))
    return (await db.execute(stmt.order_by(Server.name))).scalars().all()


# --------------------------------------------------------------------------
# Tracking invites
# --------------------------------------------------------------------------
async def active_invite(db: AsyncSession, feeder_guild_id: int, main_guild_id: int) -> TrackingInvite | None:
    stmt = (
        select(TrackingInvite)
        .where(
            TrackingInvite.feeder_guild_id == feeder_guild_id,
            TrackingInvite.main_guild_id == main_guild_id,
            TrackingInvite.active.is_(True),
        )
        .order_by(TrackingInvite.id.desc())
    )
    return (await db.execute(stmt)).scalars().first()


async def save_invite(
    db: AsyncSession, feeder_guild_id: int, main_guild_id: int, code: str, url: str, uses: int = 0
) -> TrackingInvite:
    existing = (
        await db.execute(select(TrackingInvite).where(TrackingInvite.code == code))
    ).scalars().first()
    if existing:
        existing.active = True
        existing.uses = uses
        existing.revoked_at = None
        await db.commit()
        return existing

    # Retire any previous invite for this pair so only one is active.
    old = await active_invite(db, feeder_guild_id, main_guild_id)
    if old:
        old.active = False
        old.revoked_at = utcnow()

    invite = TrackingInvite(
        code=code, url=url, feeder_guild_id=feeder_guild_id, main_guild_id=main_guild_id, uses=uses
    )
    db.add(invite)
    await db.commit()
    return invite


async def invite_by_code(db: AsyncSession, code: str) -> TrackingInvite | None:
    stmt = select(TrackingInvite).where(TrackingInvite.code == code)
    return (await db.execute(stmt)).scalars().first()


async def add_pending_uses(
    db: AsyncSession, code: str, feeder_guild_id: int, main_guild_id: int, count: int
) -> PendingInviteUse | None:
    """Bank invite uses we have seen but not yet matched to a member."""
    if count <= 0:
        return None
    row = PendingInviteUse(
        code=code, feeder_guild_id=feeder_guild_id, main_guild_id=main_guild_id, remaining=count
    )
    db.add(row)
    await db.commit()
    return row


async def pending_uses(db: AsyncSession, main_guild_id: int) -> Sequence[PendingInviteUse]:
    stmt = (
        select(PendingInviteUse)
        .where(PendingInviteUse.main_guild_id == main_guild_id, PendingInviteUse.remaining > 0)
        .order_by(PendingInviteUse.id.asc())
    )
    return (await db.execute(stmt)).scalars().all()


async def consume_pending_use(
    db: AsyncSession, main_guild_id: int
) -> tuple[PendingInviteUse | None, str]:
    """Hand the oldest banked use to a join that showed no invite change.

    If uses from two different invites are waiting we cannot tell which one
    this person used, so nothing is consumed and the caller records the join as
    ambiguous instead of guessing.
    """
    rows = list(await pending_uses(db, main_guild_id))
    if not rows:
        return None, ""
    if len({row.code for row in rows}) > 1:
        return None, "uses from more than one tracking invite are still unmatched"

    row = rows[0]
    row.remaining -= 1
    await db.commit()
    return row, f"matched to a banked use of {row.code}"


async def tracking_codes(db: AsyncSession, main_guild_id: int) -> dict[str, int]:
    """code -> feeder guild id, for the current main server."""
    stmt = select(TrackingInvite).where(
        TrackingInvite.main_guild_id == main_guild_id, TrackingInvite.active.is_(True)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {row.code: row.feeder_guild_id for row in rows}


# --------------------------------------------------------------------------
# Member joins
# --------------------------------------------------------------------------
async def record_join(
    db: AsyncSession,
    guild_id: int,
    user_id: int,
    user_name: str,
    server_type: str,
    is_test: bool | None = None,
) -> MemberJoin:
    """Record someone joining one of our servers.

    Joins made while development mode is on are flagged here, at the moment
    they happen, so later analysis never has to ask what the setting is now.
    """
    join = MemberJoin(
        guild_id=guild_id,
        user_id=user_id,
        user_name=user_name,
        server_type=server_type,
        is_test=(await settings_store.development_mode(db)) if is_test is None else is_test,
    )
    db.add(join)
    await db.commit()
    return join


async def latest_join(
    db: AsyncSession, guild_id: int, user_id: int, before: datetime | None = None
) -> MemberJoin | None:
    """The most recent time this person joined that server.

    People leave and rejoin, so the first join is usually the wrong one to
    reason about. `before` narrows it to the join that led to a given moment,
    such as the DM that was sent to them.
    """
    stmt = select(MemberJoin).where(
        MemberJoin.guild_id == guild_id, MemberJoin.user_id == user_id
    )
    rows = [row for row in (await db.execute(stmt)).scalars().all() if row.joined_at is not None]
    rows.sort(key=lambda row: as_utc(row.joined_at), reverse=True)
    if before is not None:
        cutoff = as_utc(before)
        earlier = [row for row in rows if cutoff is None or as_utc(row.joined_at) <= cutoff]
        if earlier:
            return earlier[0]
    return rows[0] if rows else None


async def latest_join_at(
    db: AsyncSession, guild_id: int, user_id: int, before: datetime | None = None
) -> datetime | None:
    join = await latest_join(db, guild_id, user_id, before)
    return as_utc(join.joined_at) if join else None


# --------------------------------------------------------------------------
# DM events and the send-again policy
# --------------------------------------------------------------------------
async def record_dm_event(
    db: AsyncSession,
    user_id: int,
    feeder_guild_id: int | None,
    status: str,
    message_version_id: int | None = None,
    tag_experiment_id: int | None = None,
    invite_code: str | None = None,
    is_test: bool = False,
    detail: str = "",
) -> DMEvent:
    event = DMEvent(
        user_id=user_id,
        feeder_guild_id=feeder_guild_id,
        status=status,
        message_version_id=message_version_id,
        tag_experiment_id=tag_experiment_id,
        invite_code=invite_code,
        is_test=is_test,
        detail=detail or None,
    )
    db.add(event)
    await db.commit()
    return event


async def dm_attempts(
    db: AsyncSession, user_id: int, include_test: bool = False
) -> Sequence[DMEvent]:
    """Past attempts to reach this person.

    Test sends are excluded from production history; development mode passes
    include_test so its own sends still count for duplicate protection.
    """
    stmt = select(DMEvent).where(
        DMEvent.user_id == user_id, DMEvent.status.in_(constants.DM_ATTEMPTS)
    )
    if not include_test:
        stmt = stmt.where(DMEvent.is_test.is_(False))
    return (await db.execute(stmt.order_by(DMEvent.created_at.desc()))).scalars().all()


def policy_allows(
    attempts: Sequence[DMEvent],
    feeder_guild_id: int,
    policy: str,
    cooldown_days: int,
    failure_retry_days: int,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Decide whether we may DM this person again.

    Pure function: hand it the past attempts and the policy, get back a yes/no
    plus a human-readable reason for the audit log.
    """
    now = now or datetime.now(timezone.utc)

    for attempt in attempts:
        created = as_utc(attempt.created_at) or now
        failed = attempt.status in (constants.DM_FAILED_CLOSED, constants.DM_FAILED_OTHER)
        if failed and now - created < timedelta(days=max(failure_retry_days, 0)):
            return False, "a recent DM to this user failed, waiting before trying again"

    if policy == constants.ONCE_GLOBAL:
        if attempts:
            return False, "already DMed once anywhere in the network"
        return True, ""

    if policy == constants.COOLDOWN_DAYS:
        window = timedelta(days=max(cooldown_days, 0))
        for attempt in attempts:
            created = as_utc(attempt.created_at) or now
            if now - created < window:
                return False, f"DMed within the last {cooldown_days} days"
        return True, ""

    # Default: ONCE_PER_FEEDER
    for attempt in attempts:
        if attempt.feeder_guild_id == feeder_guild_id:
            return False, "already DMed for this feeder"
    return True, ""


async def may_send_dm(
    db: AsyncSession,
    user_id: int,
    feeder_guild_id: int,
    settings: dict[str, Any],
    include_test: bool = False,
) -> tuple[bool, str]:
    attempts = await dm_attempts(db, user_id, include_test=include_test)
    return policy_allows(
        attempts,
        feeder_guild_id,
        str(settings.get("dm_policy", constants.ONCE_PER_FEEDER)),
        int(settings.get("dm_cooldown_days", 30) or 0),
        int(settings.get("failure_retry_days", 7) or 0),
    )


async def last_dm_for(
    db: AsyncSession, user_id: int, feeder_guild_id: int | None, include_test: bool = False
) -> DMEvent | None:
    stmt = select(DMEvent).where(
        DMEvent.user_id == user_id,
        DMEvent.status.in_((constants.DM_SENT, constants.DM_DRY_RUN)),
    )
    if not include_test:
        stmt = stmt.where(DMEvent.is_test.is_(False))
    if feeder_guild_id is not None:
        stmt = stmt.where(DMEvent.feeder_guild_id == feeder_guild_id)
    stmt = stmt.order_by(DMEvent.created_at.desc())
    return (await db.execute(stmt)).scalars().first()


# --------------------------------------------------------------------------
# Message versions
# --------------------------------------------------------------------------
async def published_message(
    db: AsyncSession, kind: str, guild_id: int | None = None
) -> MessageVersion | None:
    """Feeder override first, then the global message."""
    if guild_id is not None:
        stmt = (
            select(MessageVersion)
            .where(
                MessageVersion.kind == kind,
                MessageVersion.scope == constants.SCOPE_FEEDER,
                MessageVersion.guild_id == guild_id,
                MessageVersion.status == constants.PUBLISHED,
            )
            .order_by(MessageVersion.version.desc())
        )
        override = (await db.execute(stmt)).scalars().first()
        if override:
            return override

    stmt = (
        select(MessageVersion)
        .where(
            MessageVersion.kind == kind,
            MessageVersion.scope == constants.SCOPE_GLOBAL,
            MessageVersion.status == constants.PUBLISHED,
        )
        .order_by(MessageVersion.version.desc())
    )
    return (await db.execute(stmt)).scalars().first()


async def message_content(db: AsyncSession, kind: str, guild_id: int | None = None) -> tuple[dict[str, Any], int | None]:
    """The content that should actually be used right now, plus its version id."""
    version = await published_message(db, kind, guild_id)
    if version is None:
        return rendering.default_content(kind), None
    content = dict(rendering.default_content(kind))
    content.update(version.content or {})
    return content, version.id


async def list_versions(
    db: AsyncSession, kind: str, scope: str, guild_id: int | None = None
) -> Sequence[MessageVersion]:
    stmt = select(MessageVersion).where(MessageVersion.kind == kind, MessageVersion.scope == scope)
    stmt = stmt.where(MessageVersion.guild_id == guild_id) if guild_id else stmt.where(
        MessageVersion.guild_id.is_(None)
    )
    return (await db.execute(stmt.order_by(MessageVersion.version.desc()))).scalars().all()


async def next_version_number(db: AsyncSession, kind: str, scope: str, guild_id: int | None) -> int:
    stmt = select(func.max(MessageVersion.version)).where(
        MessageVersion.kind == kind, MessageVersion.scope == scope
    )
    stmt = stmt.where(MessageVersion.guild_id == guild_id) if guild_id else stmt.where(
        MessageVersion.guild_id.is_(None)
    )
    current = (await db.execute(stmt)).scalar()
    return int(current or 0) + 1


async def save_draft(
    db: AsyncSession,
    kind: str,
    scope: str,
    content: dict[str, Any],
    guild_id: int | None = None,
    note: str = "",
) -> MessageVersion:
    version = MessageVersion(
        kind=kind,
        scope=scope,
        guild_id=guild_id,
        version=await next_version_number(db, kind, scope, guild_id),
        status=constants.DRAFT,
        content=content,
        note=note or None,
    )
    db.add(version)
    await db.commit()
    return version


async def publish_version(db: AsyncSession, version_id: int) -> MessageVersion | None:
    version = await db.get(MessageVersion, version_id)
    if version is None:
        return None

    stmt = select(MessageVersion).where(
        MessageVersion.kind == version.kind,
        MessageVersion.scope == version.scope,
        MessageVersion.status == constants.PUBLISHED,
        MessageVersion.id != version.id,
    )
    stmt = (
        stmt.where(MessageVersion.guild_id == version.guild_id)
        if version.guild_id
        else stmt.where(MessageVersion.guild_id.is_(None))
    )
    for other in (await db.execute(stmt)).scalars().all():
        other.status = constants.ARCHIVED

    version.status = constants.PUBLISHED
    version.published_at = utcnow()
    await db.commit()
    return version


async def version_is_referenced(db: AsyncSession, version_id: int) -> bool:
    """Whether any historical record points at this message version.

    A referenced version can be archived but never deleted: DM events and
    conversions name it, and the message performance table reads it back.
    """
    dm = (
        await db.execute(
            select(DMEvent.id).where(DMEvent.message_version_id == version_id).limit(1)
        )
    ).scalars().first()
    if dm is not None:
        return True
    conversion = (
        await db.execute(
            select(Conversion.id).where(Conversion.message_version_id == version_id).limit(1)
        )
    ).scalars().first()
    return conversion is not None


async def delete_version(db: AsyncSession, version_id: int) -> tuple[bool, str]:
    """Delete a draft, or an unreferenced version. Never history."""
    version = await db.get(MessageVersion, version_id)
    if version is None:
        return False, "That version no longer exists."
    if version.status == constants.PUBLISHED:
        return False, "Unpublish or archive this version before deleting it."
    if await version_is_referenced(db, version_id):
        return False, (
            "This version was used for real messages, so it is kept for the "
            "analytics that reference it. Archive it instead."
        )
    label = f"{version.kind} version {version.version}"
    await db.delete(version)
    await db.commit()
    return True, f"Deleted {label}."


async def archive_version(db: AsyncSession, version_id: int) -> tuple[bool, str]:
    """Hide a version from the active list while keeping every reference."""
    version = await db.get(MessageVersion, version_id)
    if version is None:
        return False, "That version no longer exists."
    if version.status == constants.PUBLISHED:
        other = await published_message(db, version.kind, version.guild_id)
        if other is not None and other.id == version.id:
            return False, "Publish another version before archiving the live one."
    version.status = constants.ARCHIVED
    await db.commit()
    return True, f"Archived {version.kind} version {version.version}."


async def duplicate_version(db: AsyncSession, version_id: int) -> MessageVersion | None:
    old = await db.get(MessageVersion, version_id)
    if old is None:
        return None
    return await save_draft(
        db, old.kind, old.scope, dict(old.content or {}), old.guild_id,
        f"copy of version {old.version}",
    )


async def rename_version(db: AsyncSession, version_id: int, note: str) -> bool:
    version = await db.get(MessageVersion, version_id)
    if version is None:
        return False
    version.note = (note or "").strip()[:200] or None
    await db.commit()
    return True


async def restore_version(db: AsyncSession, version_id: int, note: str = "") -> MessageVersion | None:
    """Copy an old version into a new draft rather than rewriting history."""
    old = await db.get(MessageVersion, version_id)
    if old is None:
        return None
    return await save_draft(
        db,
        old.kind,
        old.scope,
        dict(old.content or {}),
        old.guild_id,
        note or f"restored from version {old.version}",
    )


async def ensure_default_messages(db: AsyncSession) -> None:
    """Make sure a published message exists for every kind.

    Runs on bot and dashboard start so the network always has something to
    send, and so every delivered DM can be traced back to a version number.
    """
    for kind in (constants.KIND_DM, constants.KIND_PUBLIC):
        if await published_message(db, kind) is None:
            version = await save_draft(
                db, kind, constants.SCOPE_GLOBAL, rendering.default_content(kind),
                note="default message",
            )
            await publish_version(db, version.id)


# --------------------------------------------------------------------------
# Tag experiments
# --------------------------------------------------------------------------
async def active_experiment(db: AsyncSession, feeder_guild_id: int) -> TagExperiment | None:
    stmt = (
        select(TagExperiment)
        .where(TagExperiment.feeder_guild_id == feeder_guild_id, TagExperiment.ended_at.is_(None))
        .order_by(TagExperiment.started_at.desc())
    )
    return (await db.execute(stmt)).scalars().first()


async def set_tags(
    db: AsyncSession, feeder_guild_id: int, tags: list[str], note: str = ""
) -> TagExperiment:
    """Close the running experiment and start a new one. Old tags are kept."""
    tags = [t.strip().lower() for t in tags if t and t.strip()]
    current = await active_experiment(db, feeder_guild_id)
    if current and list(current.tags or []) == tags:
        return current
    if current:
        current.ended_at = utcnow()
    experiment = TagExperiment(feeder_guild_id=feeder_guild_id, tags=tags, note=note or None)
    db.add(experiment)
    await db.commit()
    return experiment


async def experiment_at(
    db: AsyncSession, feeder_guild_id: int, when: datetime | None
) -> TagExperiment | None:
    """The experiment that was live on that feeder at that moment."""
    if when is None:
        return None
    moment = as_utc(when)
    stmt = select(TagExperiment).where(TagExperiment.feeder_guild_id == feeder_guild_id)
    for experiment in (await db.execute(stmt)).scalars().all():
        started = as_utc(experiment.started_at)
        ended = as_utc(experiment.ended_at)
        if started is not None and moment >= started and (ended is None or moment < ended):
            return experiment
    return None


async def list_experiments(db: AsyncSession, feeder_guild_id: int | None = None) -> Sequence[TagExperiment]:
    stmt = select(TagExperiment)
    if feeder_guild_id:
        stmt = stmt.where(TagExperiment.feeder_guild_id == feeder_guild_id)
    return (await db.execute(stmt.order_by(TagExperiment.started_at.desc()))).scalars().all()


# --------------------------------------------------------------------------
# Conversions
# --------------------------------------------------------------------------
async def conversion_history(db: AsyncSession, user_id: int, source_guild_id: int) -> dict[str, Any]:
    """Work out what this conversion belongs to, from its own history.

    Two things are decided here, and both look backwards rather than at the
    current settings:

    * which tag experiment and message version it belongs to — the ones live
      when the person was reached, not the ones live now;
    * whether it is a test — taken from the DM that reached them, or failing
      that from their feeder join. Development mode can be switched off between
      the test DM and the join days later, and that must not turn a test run
      into production data (or the reverse).

    The DM is searched without filtering on the test flag, so the most recent
    real history wins instead of whichever kind we happen to be looking for.
    """
    dm_event = await last_dm_for(db, user_id, source_guild_id, include_test=True)
    if dm_event is not None:
        experiment_id = dm_event.tag_experiment_id
        if experiment_id is None:
            # Older rows predate the column; fall back to the time it was sent.
            experiment = await experiment_at(db, source_guild_id, dm_event.created_at)
            experiment_id = experiment.id if experiment else None
        join = await latest_join(db, source_guild_id, user_id, before=dm_event.created_at)
        return {
            "tag_experiment_id": experiment_id,
            "message_version_id": dm_event.message_version_id,
            "dm_at": dm_event.created_at,
            "feeder_join_at": as_utc(join.joined_at) if join else None,
            "is_test": bool(dm_event.is_test),
            "basis": "the DM that was sent to them",
        }

    # No DM. The join itself is the next best anchor.
    join = await latest_join(db, source_guild_id, user_id)
    if join is not None:
        experiment = await experiment_at(db, source_guild_id, join.joined_at)
        return {
            "tag_experiment_id": experiment.id if experiment else None,
            "message_version_id": None,
            "dm_at": None,
            "feeder_join_at": as_utc(join.joined_at),
            "is_test": bool(join.is_test),
            "basis": "the tags live when they joined the feeder",
        }

    # Genuinely nothing historical to go on: the current mode is all we have.
    current = await active_experiment(db, source_guild_id)
    return {
        "tag_experiment_id": current.id if current else None,
        "message_version_id": None,
        "dm_at": None,
        "feeder_join_at": None,
        "is_test": await settings_store.development_mode(db),
        "basis": "the current tags, with no earlier record to use",
    }


async def record_conversion(
    db: AsyncSession,
    user_id: int,
    user_name: str,
    destination_guild_id: int,
    source_guild_id: int | None,
    invite_code: str | None,
    attribution: str,
    detail: str = "",
    is_test: bool | None = None,
) -> Conversion:
    """Record a main-server join.

    `is_test` is normally left alone: it is taken from the funnel history that
    led here. Pass it only to force a value.
    """
    history: dict[str, Any] = {}
    if source_guild_id:
        history = await conversion_history(db, user_id, source_guild_id)

    if is_test is None:
        if "is_test" in history:
            # Step 1 or 2: the DM, or the feeder join, behind this conversion.
            is_test = bool(history["is_test"])
        else:
            # Step 3: no source history at all, so the live setting decides.
            is_test = await settings_store.development_mode(db)

    conversion = Conversion(
        user_id=user_id,
        user_name=user_name,
        destination_guild_id=destination_guild_id,
        source_guild_id=source_guild_id,
        invite_code=invite_code,
        tag_experiment_id=history.get("tag_experiment_id"),
        message_version_id=history.get("message_version_id"),
        attribution=attribution,
        is_test=is_test,
        feeder_join_at=history.get("feeder_join_at"),
        dm_at=history.get("dm_at"),
        detail=detail or None,
    )
    db.add(conversion)
    await db.commit()
    return conversion


async def list_conversions(db: AsyncSession, limit: int = 200) -> Sequence[Conversion]:
    stmt = select(Conversion).order_by(Conversion.main_join_at.desc(), Conversion.id.desc()).limit(limit)
    return (await db.execute(stmt)).scalars().all()


# --------------------------------------------------------------------------
# Role rules
# --------------------------------------------------------------------------
async def rules_for_guild(db: AsyncSession, guild_id: int) -> Sequence[RoleRule]:
    stmt = select(RoleRule).where(RoleRule.guild_id == guild_id, RoleRule.enabled.is_(True))
    return (await db.execute(stmt)).scalars().all()


# --------------------------------------------------------------------------
# Task queue (dashboard -> bot)
# --------------------------------------------------------------------------
async def queue_task(
    db: AsyncSession, task_type: str, payload: dict[str, Any] | None = None
) -> Task:
    payload = payload or {}
    guild_id = payload.get("guild_id")
    task = Task(
        task_type=task_type,
        payload=payload,
        guild_id=int(guild_id) if guild_id else None,
        status=constants.TASK_PENDING,
    )
    db.add(task)
    await db.commit()
    return task


async def pending_tasks(db: AsyncSession, limit: int = 20) -> Sequence[Task]:
    stmt = (
        select(Task)
        .where(Task.status == constants.TASK_PENDING)
        .order_by(Task.id.asc())
        .limit(limit)
    )
    return (await db.execute(stmt)).scalars().all()


async def claim_task(db: AsyncSession, task_id: int) -> Task | None:
    """Move one task from PENDING to RUNNING, and only if it is still PENDING.

    The update is conditional, so if anything else already took this task the
    claim returns None instead of two workers running the same job.
    """
    result = await db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == constants.TASK_PENDING)
        .values(
            status=constants.TASK_RUNNING,
            started_at=utcnow(),
            attempts=Task.attempts + 1,
            error=None,
        )
    )
    await db.commit()
    if result.rowcount == 0:
        return None
    return await db.get(Task, task_id)


async def finish_task(
    db: AsyncSession, task: Task, status: str, result: str = "", error: str = ""
) -> None:
    task.status = status
    task.result = result[:2000] if result else None
    task.error = error[:2000] if error else None
    task.finished_at = utcnow()
    await db.commit()


async def complete_task(db: AsyncSession, task_id: int, result: str) -> None:
    task = await db.get(Task, task_id)
    if task:
        await finish_task(db, task, constants.TASK_DONE, result=result)


async def fail_task(db: AsyncSession, task_id: int, error: str) -> None:
    task = await db.get(Task, task_id)
    if task:
        await finish_task(db, task, constants.TASK_FAILED, error=error)


async def recover_stale_tasks(db: AsyncSession, older_than_minutes: int = 10) -> list[str]:
    """Rescue tasks left RUNNING by a bot that stopped mid-job.

    Anything still runnable goes back to PENDING so the worker picks it up on
    the next tick; anything that has already had several goes is failed, so a
    genuinely broken job cannot loop forever.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(older_than_minutes, 0))
    stmt = select(Task).where(Task.status == constants.TASK_RUNNING)
    notes: list[str] = []
    for task in (await db.execute(stmt)).scalars().all():
        started = as_utc(task.started_at) or as_utc(task.created_at)
        if started is not None and started > cutoff:
            continue
        if (task.attempts or 0) >= constants.TASK_MAX_ATTEMPTS:
            task.status = constants.TASK_FAILED
            task.error = "Abandoned after the bot stopped during this job too many times."
            task.finished_at = utcnow()
            notes.append(f"{task.task_type} #{task.id} failed after {task.attempts} attempts")
        else:
            task.status = constants.TASK_PENDING
            task.started_at = None
            notes.append(f"{task.task_type} #{task.id} returned to the queue")
    if notes:
        await db.commit()
    return notes


async def retry_task(db: AsyncSession, task_id: int) -> Task | None:
    """Put a finished task back in the queue, keeping its history."""
    task = await db.get(Task, task_id)
    if task is None or task.status not in constants.TASK_FINISHED_STATES:
        return None
    task.status = constants.TASK_PENDING
    task.started_at = None
    task.finished_at = None
    task.error = None
    task.result = None
    task.attempts = 0
    await db.commit()
    return task


async def clear_finished_tasks(db: AsyncSession) -> int:
    """Tidy the queue view. Never touches work that is pending or running."""
    stmt = select(Task).where(Task.status.in_(constants.TASK_FINISHED_STATES))
    rows = (await db.execute(stmt)).scalars().all()
    for row in rows:
        await db.delete(row)
    await db.commit()
    return len(rows)


async def recent_tasks(db: AsyncSession, limit: int = 20) -> Sequence[Task]:
    stmt = select(Task).order_by(Task.id.desc()).limit(limit)
    return (await db.execute(stmt)).scalars().all()


# --------------------------------------------------------------------------
# Worker heartbeat
# --------------------------------------------------------------------------
HEARTBEAT_KEY = "worker_heartbeat"
# How long without a beat before the dashboard calls the worker offline.
HEARTBEAT_TIMEOUT_SECONDS = 30


async def write_heartbeat(db: AsyncSession) -> None:
    await settings_store.set_many(
        db, {HEARTBEAT_KEY: datetime.now(timezone.utc).isoformat()}
    )


async def worker_status(db: AsyncSession) -> dict[str, Any]:
    """Whether the bot worker is alive, for the Health page."""
    raw = await settings_store.get(db, HEARTBEAT_KEY)
    if not raw:
        return {"online": False, "seconds_ago": None, "last_beat": None}
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return {"online": False, "seconds_ago": None, "last_beat": None}
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    seconds = max((datetime.now(timezone.utc) - last).total_seconds(), 0)
    return {
        "online": seconds <= HEARTBEAT_TIMEOUT_SECONDS,
        "seconds_ago": int(seconds),
        "last_beat": last,
    }


# --------------------------------------------------------------------------
# Convenience
# --------------------------------------------------------------------------
async def network_context(db: AsyncSession, feeder: Server | None, invite_url: str = "") -> dict[str, str]:
    values = await settings_store.get_all(db)
    from core import routing
    destinations = await routing.rows(db, feeder)
    destination_names = ", ".join(d["name"] for d in destinations) or "the main server"
    context = rendering.build_context(
        feeder_name=feeder.name if feeder else "this server",
        main_server_name=destination_names,
        network_name=str(values.get("network_name") or "our network"),
        invite_url=invite_url or "https://discord.gg/",
    )
    # Custom variables are added around the built-ins, never over them.
    custom, _ = rendering.validate_custom_variables(values.get("custom_variables") or {})
    for key, value in custom.items():
        context.setdefault(key, value)
    return context
