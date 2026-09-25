"""Invite tracking: the source of truth for attribution.

We keep a cached count of how many times every invite to the main server has
been used. When someone joins, we fetch the counts again and see what moved.

The bar for crediting a feeder is deliberately high: one tracking invite went
up and nothing else moved. Anything else — two invites moving, a tracking
invite moving alongside an ordinary one, nothing moving at all — is recorded as
ambiguous or unknown rather than guessed at.

The one exception is a single invite that jumped by more than one, which
happens when two people join between snapshots. Those extra uses are banked in
the database and handed to the joins that follow.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from core import constants, settings as settings_store
from database import crud
from database.database import session

log = logging.getLogger("funnel.invites")


def invite_deltas(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """How far each invite moved. Invites we had not seen before count from
    zero, so one created and used between snapshots still shows up."""
    deltas: dict[str, int] = {}
    for code, uses in after.items():
        change = uses - before.get(code, 0)
        if change > 0:
            deltas[code] = change
    return deltas


def diff_invites(before: dict[str, int], after: dict[str, int]) -> list[str]:
    """Codes whose use count went up."""
    return sorted(invite_deltas(before, after))


def resolve_attribution(
    increased: list[str], tracking: dict[str, int]
) -> tuple[str | None, int | None, str]:
    """Map the invites that moved onto a feeder.

    Returns (invite_code, feeder_guild_id, attribution status).

    More than one invite moving is ambiguous even when only one of them is a
    tracking invite, because the person may well have used the other one.
    """
    if len(increased) > 1:
        return None, None, constants.ATTR_AMBIGUOUS
    if len(increased) == 1:
        code = increased[0]
        if code in tracking:
            return code, tracking[code], constants.ATTR_INVITE
        return None, None, constants.ATTR_UNKNOWN
    return None, None, constants.ATTR_UNKNOWN


class InviteTracker(commands.Cog):
    """Keeps the invite cache warm and attributes main-server joins."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cache: dict[int, dict[str, int]] = {}
        self.locks: dict[int, asyncio.Lock] = {}

    def _lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self.locks:
            self.locks[guild_id] = asyncio.Lock()
        return self.locks[guild_id]

    async def snapshot(self, guild: discord.Guild) -> dict[str, int] | None:
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            log.warning("Missing Manage Server in %s, cannot track invites", guild.name)
            return None
        except discord.HTTPException as exc:
            log.warning("Could not fetch invites for %s: %s", guild.name, exc)
            return None
        return {invite.code: invite.uses or 0 for invite in invites}

    async def prime(self, guild: discord.Guild) -> None:
        counts = await self.snapshot(guild)
        if counts is not None:
            self.cache[guild.id] = counts
            log.info("Cached %s invites for %s", len(counts), guild.name)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        self.cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        self.cache.get(invite.guild.id, {}).pop(invite.code, None)
        async with session() as db:
            row = await crud.invite_by_code(db, invite.code)
            if row and row.active:
                row.active = False
                await db.commit()
                await crud.log(
                    db,
                    "tracking_invite_deleted",
                    f"{invite.code} was deleted; auto repair will create a new one",
                    row.feeder_guild_id,
                )

    async def attribute_join(self, member: discord.Member) -> None:
        """Called when someone joins the main server."""
        guild = member.guild
        async with self._lock(guild.id):
            before = self.cache.get(guild.id, {})
            after = await self.snapshot(guild)
            if after is None:
                deltas: dict[str, int] = {}
            else:
                deltas = invite_deltas(before, after)
                self.cache[guild.id] = after
            increased = sorted(deltas)

            async with session() as db:
                tracking = await crud.tracking_codes(db, guild.id)
                code, feeder_id, status = resolve_attribution(increased, tracking)

                if after is None:
                    detail = "could not read invites (missing Manage Server permission)"
                elif increased:
                    detail = "invites used: " + ", ".join(f"{c} +{deltas[c]}" for c in increased)
                else:
                    detail = "no invite use was detected"

                if status == constants.ATTR_INVITE and code is not None and feeder_id is not None:
                    # Several people came through the same invite between two
                    # snapshots. This join takes one use; the rest are banked
                    # for the joins that follow.
                    extra = deltas.get(code, 1) - 1
                    if extra > 0:
                        await crud.add_pending_uses(db, code, feeder_id, guild.id, extra)
                        detail += f"; banked {extra} further use(s) of {code}"
                elif not increased and after is not None:
                    # Nothing moved, which is exactly what a banked use looks
                    # like from here. Only take one if there is no doubt.
                    pending, note = await crud.consume_pending_use(db, guild.id)
                    if pending is not None:
                        code = pending.code
                        feeder_id = pending.feeder_guild_id
                        status = constants.ATTR_INVITE
                        detail = note
                    elif note:
                        status = constants.ATTR_AMBIGUOUS
                        detail = note

                # Keep the stored use count in sync for the dashboard.
                if code and after and code in after:
                    row = await crud.invite_by_code(db, code)
                    if row:
                        row.uses = after[code]
                        await db.commit()

                conversion = await crud.record_conversion(
                    db,
                    user_id=member.id,
                    user_name=str(member),
                    destination_guild_id=guild.id,
                    source_guild_id=feeder_id,
                    invite_code=code,
                    attribution=status,
                    detail=detail,
                    # Whether this counts as a test is decided by the funnel
                    # history behind it, not by the setting right now.
                )
                if feeder_id:
                    feeder = await crud.get_server(db, feeder_id)
                    name = feeder.name if feeder else str(feeder_id)
                    await crud.log(
                        db, "conversion", f"{member} joined via {code} — credit to {name}", guild.id
                    )
                else:
                    await crud.log(
                        db, f"conversion_{status.lower()}", f"{member} joined. {detail}", guild.id
                    )
                log.info("Conversion %s for %s (%s)", status, member, conversion.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        async with session() as db:
            from core import routing
            main_ids = await routing.main_ids(db)
        for main_id in main_ids:
            guild = self.bot.get_guild(main_id)
            if guild:
                await self.prime(guild)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InviteTracker(bot))
