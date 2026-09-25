"""Automatic feeder configuration.

Invite the bot to a new server you own and this module does the rest: it makes
the public funnel channel, the private bump channel, a unique invite to the
main server, and posts the funnel message. The same code runs again during
health checks, which is what makes auto repair work — everything here is
"make sure this exists", never "assume it exists".
"""
from __future__ import annotations

import logging
from typing import Any

import discord
from sqlalchemy.ext.asyncio import AsyncSession

from bot import messages
from core import routing, constants, rendering, settings as settings_store
from database import crud
from database.models import Server, utcnow

log = logging.getLogger("funnel.setup")


def public_overwrites(guild: discord.Guild) -> dict[Any, discord.PermissionOverwrite]:
    """Everyone can read, nobody but the bot can post."""
    return {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
            add_reactions=False,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            manage_messages=True,
            read_message_history=True,
        ),
    }


def bump_overwrites(
    guild: discord.Guild, staff_role_names: list[str]
) -> dict[Any, discord.PermissionOverwrite]:
    """Hidden from members. Visible to the bot and to staff roles by name.
    The guild owner always sees it through ownership."""
    overwrites: dict[Any, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
    }
    wanted = {name.strip().lower() for name in staff_role_names if name and name.strip()}
    for role in guild.roles:
        if role.name.lower() in wanted:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
    return overwrites


def find_channel(guild: discord.Guild, channel_id: int | None, name: str) -> Any:
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel is not None:
            return channel
    wanted = (name or "").lower()
    for channel in getattr(guild, "text_channels", []):
        if channel.name.lower() == wanted:
            return channel
    return None


async def ensure_channel(
    guild: discord.Guild,
    channel_id: int | None,
    name: str,
    overwrites: dict[Any, discord.PermissionOverwrite],
    reason: str,
) -> tuple[Any, bool]:
    """Return (channel, created). Renames and re-applies permissions if needed.

    On an existing channel the permissions are applied one target at a time
    rather than as a whole replacement set. That way the bot enforces the
    overwrites it owns (@everyone, itself, the staff roles it was told about)
    and leaves any other role overwrites you added by hand exactly where they
    are, every time auto repair runs.
    """
    channel = find_channel(guild, channel_id, name)
    if channel is None:
        channel = await guild.create_text_channel(name=name, overwrites=overwrites, reason=reason)
        return channel, True

    if channel.name.lower() != (name or "").lower():
        await channel.edit(name=name, reason=reason)

    for target, overwrite in overwrites.items():
        current = channel.overwrites_for(target)
        if current != overwrite:
            await channel.set_permissions(target, overwrite=overwrite, reason=reason)
    return channel, False


async def invite_source_channel(guild: discord.Guild) -> Any:
    """Somewhere sensible in the main server to point new people at."""
    candidates = []
    if getattr(guild, "rules_channel", None):
        candidates.append(guild.rules_channel)
    if getattr(guild, "system_channel", None):
        candidates.append(guild.system_channel)
    candidates.extend(getattr(guild, "text_channels", []))
    for channel in candidates:
        if channel is None:
            continue
        perms = channel.permissions_for(guild.me)
        if perms.create_instant_invite:
            return channel
    return None


async def ensure_tracking_invite(
    bot: Any, db: AsyncSession, feeder: Server, main_guild: discord.Guild
) -> tuple[Any, bool]:
    """Return (invite_row, created). Verifies the stored invite still exists."""
    tracker = getattr(bot,"get_cog",lambda _name:None)("InviteTracker")
    if tracker is not None and main_guild.id not in tracker.cache:
        await tracker.prime(main_guild)
    existing = await crud.active_invite(db, feeder.guild_id, main_guild.id)
    if existing:
        try:
            live_codes = {inv.code for inv in await main_guild.invites()}
        except discord.Forbidden:
            log.warning("No manage_guild in main server, cannot verify invites")
            return existing, False
        if existing.code in live_codes:
            return existing, False
        existing.active = False
        existing.revoked_at = utcnow()
        await db.commit()
        await crud.log(
            db,
            "tracking_invite_missing",
            f"Invite {existing.code} for {feeder.name} no longer exists, creating a replacement",
            feeder.guild_id,
        )

    channel = await invite_source_channel(main_guild)
    if channel is None:
        raise RuntimeError(
            f"No channel in {main_guild.name} where the bot can create an invite "
            "(needs the Create Invite permission)."
        )

    invite = await channel.create_invite(
        max_age=0,
        max_uses=0,
        unique=True,
        reason=f"Funnel tracking invite for {feeder.name}",
    )
    row = await crud.save_invite(
        db, feeder.guild_id, main_guild.id, invite.code, invite.url, getattr(invite, "uses", 0) or 0
    )
    await crud.log(
        db, "tracking_invite_created", f"{invite.url} for {feeder.name}", feeder.guild_id
    )
    return row, True


async def post_funnel_message(
    db: AsyncSession, feeder: Server, channel: Any, invite_url: str, destinations=None
) -> None:
    """Create or update the public funnel post, and remember which version it
    is showing so a publish on the dashboard can refresh it later."""
    content, version_id = await crud.message_content(db, constants.KIND_PUBLIC, feeder.guild_id)
    context = await crud.network_context(db, feeder, invite_url)
    rendered = rendering.render_content(content, context)
    if destinations is not None:
        destinations = messages.destination_labels(content, context, destinations)
    payload = messages.build_payload(rendered, invite_url, feeder.guild_id, destinations)

    message = None
    if feeder.funnel_message_id:
        try:
            message = await channel.fetch_message(feeder.funnel_message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    if message is None:
        message = await channel.send(**payload)
        feeder.funnel_message_id = message.id
    else:
        await message.edit(**payload)

    feeder.funnel_message_version_id = version_id
    await db.commit()


async def ensure_feeder(
    bot: Any, db: AsyncSession, guild: discord.Guild, reason: str = "funnel setup"
) -> dict[str, Any]:
    """Make a feeder match its configuration. Safe to call any number of times.

    Returns a health report describing what was found and what was fixed.
    """
    server = await crud.get_server(db, guild.id)
    if server is None:
        server = await crud.upsert_server(db, guild.id, guild.name, guild.owner_id)

    report: dict[str, Any] = {
        "guild_id": guild.id,
        "name": guild.name,
        "bot": "OK",
        "funnel_channel": "missing",
        "bump_channel": "missing",
        "tracking_invite": "missing",
        "repairs": [],
        "errors": [],
    }

    if server.server_type != constants.FEEDER:
        report["errors"].append(f"{guild.name} is not marked as a feeder")
        return report

    values = await settings_store.get_all(db)
    effective = settings_store.resolve(values, server)

    # The channels are safe to create with no main server chosen yet. Anything
    # that needs a destination is skipped and clearly reported instead, so a
    # brand new install configures itself as far as it sensibly can.
    main_ids = await routing.destination_ids(db, server)
    server.destination_guild_id = main_ids[0] if main_ids else None

    # 1. Public funnel channel
    try:
        channel, created = await ensure_channel(
            guild,
            server.funnel_channel_id,
            str(effective["funnel_channel_name"]),
            public_overwrites(guild),
            reason,
        )
        server.funnel_channel_id = channel.id
        report["funnel_channel"] = "OK"
        if created:
            report["repairs"].append(f"created #{channel.name}")
            await crud.log(db, "funnel_channel_created", f"#{channel.name}", guild.id)
    except discord.Forbidden:
        report["funnel_channel"] = "no permission"
        report["errors"].append("The bot needs Manage Channels here.")
        channel = None

    # 2. Private bump channel
    try:
        bump, created = await ensure_channel(
            guild,
            server.bump_channel_id,
            str(effective["bump_channel_name"]),
            bump_overwrites(guild, list(values.get("bump_staff_role_names") or [])),
            reason,
        )
        server.bump_channel_id = bump.id
        report["bump_channel"] = "OK"
        if created:
            report["repairs"].append(f"created #{bump.name}")
            await crud.log(db, "bump_channel_created", f"#{bump.name}", guild.id)
    except discord.Forbidden:
        report["bump_channel"] = "no permission"
        report["errors"].append("The bot needs Manage Channels here.")

    # 3. One tracking invite per feeder/destination pair.
    destinations=[]
    report["destinations"]=[]
    if not main_ids:
        report["tracking_invite"] = "main server not configured"
        report["errors"].append("Main server not configured. Choose destinations on this feeder's page.")
    for main_id in main_ids:
        main_guild=bot.get_guild(main_id)
        destination={"guild_id":main_id, "name":getattr(main_guild,"name",str(main_id)), "invite_url":""}
        if main_guild is None:
            report["errors"].append(f"The bot is not in destination {main_id}.")
        else:
            try:
                invite_row,created=await ensure_tracking_invite(bot,db,server,main_guild)
                destination["invite_url"]=invite_row.url
                if created:
                    report["repairs"].append(f"created tracking invite for {main_guild.name}")
            except (discord.HTTPException,RuntimeError) as exc:
                report["errors"].append(f"{main_guild.name}: {exc}")
        destinations.append(destination)
        report["destinations"].append(dict(destination))
    ready=[d for d in destinations if d["invite_url"]]
    invite_url=ready[0]["invite_url"] if ready else ""
    if ready:
        report["tracking_invite"] = ", ".join(d["invite_url"] for d in ready)
    elif main_ids:
        report["tracking_invite"] = "main server unavailable"

    # 4. Update all welcome buttons together. If every destination is down,
    # remove stale buttons from an existing post while preserving the body.
    if channel is not None and (ready or server.funnel_message_id):
        try:
            await post_funnel_message(db,server,channel,invite_url,destinations)
        except discord.Forbidden:
            report["errors"].append(f"The bot cannot post in #{channel.name}.")

    # 5. A tag experiment so conversions always have something to attribute to
    if await crud.active_experiment(db, guild.id) is None:
        await crud.set_tags(db, guild.id, [], note="auto-created on setup")

    server.last_health_check = utcnow()
    server.health = {k: v for k, v in report.items() if k != "repairs"}
    await db.commit()

    if report["repairs"]:
        await crud.log(db, "auto_repair", "; ".join(report["repairs"]), guild.id)
    return report


async def refresh_public_message(
    bot: Any, db: AsyncSession, server: Server, force: bool = False
) -> bool:
    """Re-post the public funnel message if a newer version was published.

    `force` reposts regardless, which is what the Repost button does after an
    invite rotation.
    """
    if server.server_type != constants.FEEDER:
        return False
    _, version_id = await crud.message_content(db, constants.KIND_PUBLIC, server.guild_id)
    if version_id == server.funnel_message_version_id and not force:
        return False

    guild = bot.get_guild(server.guild_id)
    if guild is None:
        return False
    channel = find_channel(guild, server.funnel_channel_id, server.funnel_channel_name or "")
    if channel is None:
        return False

    destinations = await routing.rows(db, server)
    ready = [d for d in destinations if d["invite_url"]]
    await post_funnel_message(db, server, channel, ready[0]["invite_url"] if ready else "", destinations)
    await crud.log(db, "funnel_message_updated", f"in {server.name}", server.guild_id)
    return True


# --------------------------------------------------------------------------
# Permission preflight and destructive rebuild
# --------------------------------------------------------------------------
# What the bot actually does inside a feeder: make and manage its two
# channels and post in one of them. Notably NOT Create Invite — the tracking
# invite is created in the MAIN server, so requiring it here would block
# perfectly good feeders for no reason.
FEEDER_PERMISSIONS = (
    ("manage_channels", "Manage Channels"),
    ("manage_roles", "Manage Roles"),
    ("view_channel", "View Channels"),
    ("send_messages", "Send Messages"),
    ("embed_links", "Embed Links"),
    ("read_message_history", "Read Message History"),
)

# What the bot needs in the MAIN server: create the tracking invite, and read
# invite use counts, which is how every conversion is attributed.
MAIN_PERMISSIONS = (
    ("manage_guild", "Manage Server"),
    ("create_instant_invite", "Create Invite"),
)

# Kept for compatibility with anything that imported the old name.
REQUIRED_PERMISSIONS = FEEDER_PERMISSIONS


def _guild_permissions(guild: Any):
    me = getattr(guild, "me", None)
    return getattr(me, "guild_permissions", None)


def missing_permissions(guild: Any, destructive: bool = False) -> list[str]:
    """Permissions the bot still needs *in this feeder*, in plain names.

    Always run before touching anything, so a fresh setup cannot delete half a
    server and then discover it cannot rebuild it.
    """
    perms = _guild_permissions(guild)
    if perms is None:
        return []
    missing = [label for attr, label in FEEDER_PERMISSIONS if not getattr(perms, attr, False)]
    if destructive and not getattr(perms, "manage_channels", False):
        missing.append("Manage Channels (needed to delete channels)")
    return missing


async def main_server_problems(bot: Any, db: AsyncSession, feeder: Server | None = None) -> list[str]:
    main_ids = await routing.destination_ids(db, feeder)
    if not main_ids:
        return ["No main server is configured. Choose destinations on the dashboard first."]
    problems=[]
    for main_id in main_ids:
        main_guild = bot.get_guild(main_id)
        if main_guild is None:
            problems.append(f"Waypoint is not in the main server {main_id}.")
            continue
        perms=_guild_permissions(main_guild)
        if perms is not None:
            for attr,label in MAIN_PERMISSIONS:
                if not getattr(perms,attr,False):
                    problems.append(f"Waypoint needs {label} in {main_guild.name}" + (" to read invite counts for attribution" if attr=="manage_guild" else ""))
        if await invite_source_channel(main_guild) is None:
            problems.append(f"No channel in {main_guild.name} where Waypoint can create an invite.")
    return problems


async def fresh_setup(
    bot: Any, db: AsyncSession, guild: Any, actor_id: int | None = None
) -> dict[str, Any]:
    """Delete this feeder's channels and rebuild the Waypoint layout.

    Destructive by design, and guarded by the caller: never the main server,
    never an unapproved guild, never without confirmation. Roles, members,
    emojis and server settings are all left alone; only channels and
    categories are removed.
    """
    report: dict[str, Any] = {
        "deleted": [],
        "kept": [],
        "created": [],
        "errors": [],
        "tracking_invite": "",
    }

    # ------------------------------------------------------------------
    # Full preflight. Every check below runs before a single channel is
    # deleted, and any failure means nothing is touched at all.
    # ------------------------------------------------------------------
    server = await crud.get_server(db, guild.id)
    main_id = await settings_store.main_guild_id(db)

    if main_id == guild.id or (server is not None and server.server_type in constants.MAIN_TYPES):
        report["errors"].append("Fresh Setup is disabled on the MAIN server.")
        return report

    if server is None or server.server_type != constants.FEEDER:
        report["errors"].append("Fresh setup only runs on feeder servers.")
        return report

    blocked = missing_permissions(guild, destructive=True)
    if blocked:
        report["errors"].append("Missing permissions here: " + ", ".join(blocked))
        return report  # nothing has been touched

    # The rebuild needs a tracking invite, which is created in the MAIN
    # server. Check that now: a feeder wiped clean with no invite to hand out
    # is worse than one left alone.
    main_problems = await main_server_problems(bot, db, server)
    if main_problems:
        report["errors"].append(
            "Fresh Setup cancelled: Waypoint cannot create tracking invites in the "
            "MAIN server. " + " ".join(main_problems)
        )
        await crud.log(
            db, "fresh_setup_cancelled",
            f"{guild.name}: {' '.join(main_problems)}", guild.id,
        )
        return report  # still nothing has been touched

    await crud.log(
        db, "fresh_setup_confirmed",
        f"Rebuilding {guild.name} (requested by user {actor_id})", guild.id,
    )

    # Channels first, then the categories they sat in.
    for channel in list(getattr(guild, "channels", [])):
        try:
            await channel.delete(reason="Waypoint fresh setup")
            report["deleted"].append(getattr(channel, "name", str(channel)))
        except (discord.Forbidden, discord.HTTPException) as exc:
            report["kept"].append(f"{getattr(channel, 'name', channel)} ({exc})")

    # Forget the channels we just deleted so setup does not look for them.
    server.funnel_channel_id = None
    server.bump_channel_id = None
    server.funnel_message_id = None
    server.funnel_message_version_id = None
    await db.commit()

    setup = await ensure_feeder(bot, db, guild, "fresh setup")
    report["created"] = setup["repairs"]
    report["errors"].extend(setup["errors"])
    report["tracking_invite"] = setup["tracking_invite"]
    report["health"] = setup

    await crud.log(
        db, "fresh_setup_completed",
        f"{guild.name}: deleted {len(report['deleted'])} channel(s), "
        f"rebuilt {len(report['created'])} resource(s)",
        guild.id,
    )
    return report
