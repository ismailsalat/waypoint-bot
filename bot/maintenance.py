"""Background maintenance.

Two loops:

1. Auto repair — walks the feeders on a timer and makes sure the channels,
   the tracking invite and the funnel post still exist.
2. Task worker — the dashboard runs on your laptop and has no Discord
   connection, so it leaves small jobs in the `tasks` table and this loop
   picks them up within a few seconds.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from bot import feeder_setup, funnel_dm
from core import routing, constants, settings as settings_store
from database import crud
from database.database import as_utc, session
from database.models import utcnow

log = logging.getLogger("funnel.maintenance")


class Maintenance(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.repair_loop.start()
        self.task_loop.start()

    async def cog_unload(self) -> None:
        self.repair_loop.cancel()
        self.task_loop.cancel()

    # ------------------------------------------------------------------
    # Auto repair
    # ------------------------------------------------------------------
    @tasks.loop(minutes=5)
    async def repair_loop(self) -> None:
        try:
            await self.run_auto_repair()
        except Exception:  # noqa: BLE001 - must not stop the schedule
            log.exception("Auto repair pass failed; it will run again next cycle")

    async def run_auto_repair(self) -> None:
        async with session() as db:
            values = await settings_store.get_all(db)
            feeders = await crud.list_feeders(db, only_present=True)
            interval = timedelta(minutes=int(values.get("repair_interval_minutes", 30) or 30))
            due = []
            for feeder in feeders:
                last = as_utc(feeder.last_health_check)
                if last is None or datetime.now(timezone.utc) - last >= interval:
                    due.append(feeder.guild_id)

        for guild_id in due:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            async with session() as db:
                server = await crud.get_server(db, guild_id)
                if server is None:
                    continue
                effective = await settings_store.effective(db, server)
                if not effective.get("auto_repair"):
                    server.last_health_check = utcnow()
                    await db.commit()
                    continue
                try:
                    await feeder_setup.ensure_feeder(self.bot, db, guild, "auto repair")
                    await feeder_setup.refresh_public_message(self.bot, db, server)
                except discord.HTTPException as exc:
                    log.warning("Auto repair failed in %s: %s", guild.name, exc)

    @repair_loop.before_loop
    async def before_repair(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Dashboard task queue
    # ------------------------------------------------------------------
    @tasks.loop(seconds=3)
    async def task_loop(self) -> None:
        """Poll for queued work, claim it, run it, record the outcome.

        Nothing is allowed to escape this method. A discord.py task loop stops
        permanently the first time its body raises, with only a traceback on
        stderr to show for it, and the bot stays connected the whole time — so
        an unhandled error here means every queued job sits at PENDING forever
        while everything looks fine. Hence the belt and braces: the whole body
        is guarded, and `task_loop_failed` restarts the loop if one somehow
        gets through.
        """
        try:
            async with session() as db:
                await crud.write_heartbeat(db)
                jobs = [(t.id, t.task_type) for t in await crud.pending_tasks(db)]
        except Exception:  # noqa: BLE001 - a hiccup must not kill the worker
            log.exception("Could not read the task queue; will try again shortly")
            return

        for task_id, task_type in jobs:
            try:
                await self.process_task(task_id, task_type)
            except Exception:  # noqa: BLE001 - one bad job must not stop the rest
                log.exception("Task %s (#%s) could not be processed", task_type, task_id)

    async def process_task(self, task_id: int, task_type: str) -> None:
        """Claim and run one task, always leaving it in a finished state."""
        async with session() as db:
            claimed = await crud.claim_task(db, task_id)
            if claimed is None:
                return  # someone else got there first
            payload = dict(claimed.payload or {})

        try:
            result = await self.run_task(task_type, payload)
        except Exception as exc:  # noqa: BLE001 - reported on the dashboard
            log.exception("Task %s failed", task_type)
            message = f"{type(exc).__name__}: {exc}"
            async with session() as db:
                await crud.fail_task(db, task_id, message)
                await crud.log(
                    db, "task_failed", f"{task_type}: {message}",
                    payload.get("guild_id"), "bot",
                )
            return

        async with session() as db:
            await crud.complete_task(db, task_id, result)

    @task_loop.before_loop
    async def before_tasks(self) -> None:
        await self.bot.wait_until_ready()
        await self.recover_stale_tasks()

    @task_loop.error
    async def task_loop_failed(self, exc: BaseException) -> None:
        """Last line of defence: restart the worker instead of dying quietly."""
        log.exception("Task worker stopped unexpectedly, restarting it", exc_info=exc)
        if not self.task_loop.is_running():
            self.task_loop.restart()

    @repair_loop.error
    async def repair_loop_failed(self, exc: BaseException) -> None:
        log.exception("Auto repair loop stopped unexpectedly, restarting it", exc_info=exc)
        if not self.repair_loop.is_running():
            self.repair_loop.restart()

    async def recover_stale_tasks(self) -> None:
        """A task left RUNNING means the bot stopped mid-job. Rescue it."""
        try:
            async with session() as db:
                notes = await crud.recover_stale_tasks(db, older_than_minutes=10)
                for note in notes:
                    await crud.log(db, "worker_recovered_stale_task", note, source="bot")
            if notes:
                log.info("Recovered stale tasks: %s", "; ".join(notes))
        except Exception:  # noqa: BLE001
            log.exception("Could not recover stale tasks")

    async def run_task(self, task_type: str, payload: dict) -> str:
        guild_id = int(payload["guild_id"]) if payload.get("guild_id") else None

        if task_type == constants.TASK_TEST_DM:
            return await funnel_dm.send_test_dm(
                self.bot,
                int(payload["user_id"]),
                payload.get("kind", constants.KIND_DM),
                payload.get("content"),
                guild_id,
            )

        if task_type in (constants.TASK_REPAIR, constants.TASK_SETUP_FEEDER):
            guild = self.bot.get_guild(guild_id) if guild_id else None
            if guild is None:
                return "The bot is not in that server."
            async with session() as db:
                report = await feeder_setup.ensure_feeder(self.bot, db, guild, "manual repair")
                server = await crud.get_server(db, guild.id)
                if server:
                    await feeder_setup.refresh_public_message(self.bot, db, server)
                await crud.log(
                    db, "repair_completed",
                    "; ".join(report["repairs"]) or "nothing needed fixing", guild.id, "bot",
                )
            repairs = ", ".join(report["repairs"]) or "nothing needed fixing"
            errors = "; ".join(report["errors"])
            return f"{guild.name}: {repairs}." + (f" Problems: {errors}" if errors else "")

        if task_type == constants.TASK_FRESH_SETUP:
            guild = self.bot.get_guild(guild_id) if guild_id else None
            if guild is None:
                return "The bot is not in that server."
            async with session() as db:
                report = await feeder_setup.fresh_setup(
                    self.bot, db, guild, payload.get("actor_id")
                )
            if report["errors"]:
                raise RuntimeError("; ".join(report["errors"]))
            return (
                f"{guild.name}: deleted {len(report['deleted'])} channel(s), "
                f"rebuilt {', '.join(report['created']) or 'nothing'}."
            )

        if task_type == constants.TASK_SYNC_PUBLIC:
            async with session() as db:
                servers = (
                    [await crud.get_server(db, guild_id)]
                    if guild_id
                    else list(await crud.list_feeders(db, only_present=True))
                )
                force = bool(payload.get("force"))
                updated = []
                for server in servers:
                    if server and await feeder_setup.refresh_public_message(
                        self.bot, db, server, force=force
                    ):
                        updated.append(server.name)
            return f"Updated the funnel post in: {', '.join(updated)}" if updated else "Already up to date."

        if task_type == constants.TASK_ROTATE_INVITE:
            return await self.rotate_invite(guild_id)

        return f"Unknown task type {task_type}"

    async def rotate_invite(self, guild_id: int | None) -> str:
        if guild_id is None:
            return "No server given."
        async with session() as db:
            server = await crud.get_server(db, guild_id)
            if server is None:
                return "Server not found."
            for main_id in await routing.destination_ids(db, server):
                old=await crud.active_invite(db,guild_id,main_id)
                if old:
                    old.active=False
                    old.revoked_at=utcnow()
                    await db.commit()
                    main_guild=self.bot.get_guild(main_id)
                    if main_guild:
                        try:
                            for invite in await main_guild.invites():
                                if invite.code==old.code:
                                    await invite.delete(reason="rotated by dashboard")
                        except discord.HTTPException:
                            pass
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return "The bot is not in that server."
            report = await feeder_setup.ensure_feeder(self.bot, db, guild, "invite rotation")
            return f"New tracking invite: {report['tracking_invite']}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Maintenance(bot))
