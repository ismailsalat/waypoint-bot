"""Guild and member listeners.

This is the entry point for everything that happens automatically: the bot
being invited to a new server, and people joining feeders or the main server.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from bot import feeder_setup, funnel_dm
from core import constants, settings as settings_store
from core.config import config
from database import crud
from database.database import session

log = logging.getLogger("funnel.members")


class MemberEvents(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # Joining and leaving servers
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Invited to a new server. Configure it only if you own it."""
        approved = config.is_approved_owner(guild.owner_id)

        async with session() as db:
            existing = await crud.get_server(db, guild.id)
            main_id = await settings_store.main_guild_id(db)

            if existing and existing.server_type != constants.DISABLED:
                server_type = existing.server_type
            elif main_id == guild.id:
                server_type = constants.MAIN
            elif approved:
                server_type = constants.FEEDER
            else:
                server_type = constants.DISABLED

            await crud.upsert_server(db, guild.id, guild.name, guild.owner_id, server_type)
            await crud.log(
                db,
                "guild_joined",
                f"Joined {guild.name} as {server_type}"
                + ("" if approved else " (owner is not in OWNER_USER_IDS, left unconfigured)"),
                guild.id,
            )

        if server_type == constants.FEEDER and approved:
            await self.configure(guild, "automatic feeder setup")
        else:
            log.info("Joined %s as %s, no automatic configuration", guild.name, server_type)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        async with session() as db:
            server = await crud.get_server(db, guild.id)
            if server:
                server.bot_present = False
                await db.commit()
                await crud.log(db, "guild_left", f"Removed from {guild.name}", guild.id)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if before.name != after.name:
            async with session() as db:
                server = await crud.get_server(db, after.id)
                if server:
                    server.name = after.name
                    await db.commit()

    async def configure(self, guild: discord.Guild, reason: str) -> dict:
        async with session() as db:
            report = await feeder_setup.ensure_feeder(self.bot, db, guild, reason)
        if report["errors"]:
            log.warning("Setup issues in %s: %s", guild.name, report["errors"])
        return report

    # ------------------------------------------------------------------
    # People joining
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return

        async with session() as db:
            server = await crud.get_server(db, member.guild.id)
            if server is None:
                return
            await crud.record_join(
                db, member.guild.id, member.id, str(member), server.server_type
            )
            server_type = server.server_type

        if server_type in constants.MAIN_TYPES:
            tracker = self.bot.get_cog("InviteTracker")
            if tracker:
                await tracker.attribute_join(member)
            return

        if server_type == constants.FEEDER:
            # Runs in the background so the delay never blocks the gateway.
            self.bot.loop.create_task(self._funnel(member))

    async def _funnel(self, member: discord.Member) -> None:
        try:
            status = await funnel_dm.handle_feeder_join(self.bot, member, member.guild.id)
            log.info("Funnel result for %s in %s: %s", member, member.guild.name, status)
        except Exception:  # noqa: BLE001 - a bad DM must never kill the listener
            log.exception("Funnel DM failed for %s", member)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemberEvents(bot))
