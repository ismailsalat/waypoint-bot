"""Funnel Bot — entry point.

Run locally with:   python -m bot.main
On Railway this is the worker process (see the Procfile).
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from core import constants, settings as settings_store
from core.config import config
from database import crud
from database.database import DatabaseNotConfigured, database_status, init_db, session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("funnel")

COGS = (
    "bot.invite_tracker",
    "bot.member_events",
    "bot.age_rules",
    "bot.maintenance",
    "bot.commands",
)


class FunnelBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True  # required to see joins and role changes
        intents.guilds = True
        intents.invites = True
        super().__init__(command_prefix="!funnel ", intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        await init_db()
        from bot.messages import InviteButton, DestinationButton

        # Lets buttons on messages posted before this restart keep working.
        self.add_dynamic_items(InviteButton, DestinationButton)
        await bootstrap_defaults()
        for cog in COGS:
            await self.load_extension(cog)
            log.info("Loaded %s", cog)

    async def on_ready(self) -> None:
        log.info("Signed in as %s (%s guilds)", self.user, len(self.guilds))
        async with session() as db:
            if await settings_store.development_mode(db):
                log.warning("DEVELOPMENT MODE is on — production funnel DMs are suppressed")
        await self.sync_guilds()
        try:
            synced = await self.tree.sync()
            log.info("Synced %s slash command(s)", len(synced))
        except discord.HTTPException as exc:
            log.warning("Could not sync slash commands: %s", exc)

    async def sync_guilds(self) -> None:
        """Make the database match reality on every start.

        The main server comes from the database. If nothing is stored yet the
        bootstrap value from .env has already been written by
        bootstrap_defaults; if there was none either, the bot runs happily
        unconfigured and waits for you to pick one on the dashboard.
        """
        async with session() as db:
            main_id = await settings_store.main_guild_id(db)
            if main_id is None:
                log.warning(
                    "No main server configured yet. Open the dashboard and choose one; "
                    "feeders will be finished off automatically once you do."
                )

            known = {s.guild_id: s for s in await crud.list_servers(db)}
            for guild in self.guilds:
                existing = known.get(guild.id)
                server_type = None
                if existing is None:
                    if main_id == guild.id:
                        server_type = constants.MAIN
                    elif config.is_approved_owner(guild.owner_id):
                        server_type = constants.FEEDER
                    else:
                        server_type = constants.DISABLED
                await crud.upsert_server(db, guild.id, guild.name, guild.owner_id, server_type)

            # Exactly one server may be MAIN, whatever the database picked up.
            if main_id:
                await crud.set_main_server(db, main_id)

            for guild_id, server in known.items():
                if self.get_guild(guild_id) is None and server.bot_present:
                    server.bot_present = False
            await db.commit()

        tracker = self.get_cog("InviteTracker")
        from core import routing
        async with session() as db:
            main_ids = await routing.main_ids(db)
        if tracker:
            for destination_id in main_ids:
                guild = self.get_guild(destination_id)
                if guild:
                    await tracker.prime(guild)


async def bootstrap_defaults() -> None:
    """First-run set-up: seed settings from .env, then make sure there is a
    message to send. Both are no-ops once the database has its own values."""
    async with session() as db:
        seeded = await settings_store.bootstrap_from_env(db)
        if seeded:
            log.info("Seeded first-run settings from the environment: %s", ", ".join(seeded))
            await crud.log(
                db, "settings_bootstrapped", "; ".join(seeded), source="system"
            )
        await crud.ensure_default_messages(db)


def preflight() -> dict:
    """Check the environment before connecting to anything.

    Returns the database status so it can be logged. Raises SystemExit with a
    readable message rather than a stack trace when something is missing.
    """
    try:
        status = database_status()
    except DatabaseNotConfigured as exc:  # pragma: no cover - defensive
        raise SystemExit(str(exc)) from exc
    if status["error"]:
        raise SystemExit(status["error"])
    if not config.discord_bot_token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    return status


async def main() -> None:
    status = preflight()
    log.info(
        "%s environment, %s database%s",
        status["environment"],
        status["kind"],
        f" ({status['file']})" if status["file"] else "",
    )
    bot = FunnelBot()
    async with bot:
        await bot.start(config.discord_bot_token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down")
