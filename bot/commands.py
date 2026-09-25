"""Slash commands.

Four commands under /waypoint, all owner-only. They are application commands
rather than text commands on purpose: text commands would need the Message
Content intent, which this bot has no other reason to ask for.

    /waypoint setup    non-destructive, makes what is missing
    /waypoint repair   non-destructive, fixes what is broken
    /waypoint fresh    destructive, wipes the channels and rebuilds
    /waypoint status   read-only diagnosis
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot import feeder_setup
from core import routing, constants, settings as settings_store
from core.config import config
from database import crud
from database.database import session

log = logging.getLogger("funnel.commands")

CONFIRM_TIMEOUT_SECONDS = 30


def summarise(report: dict) -> str:
    """Turn a setup/repair report into something readable in Discord."""
    lines = []
    repairs = report.get("repairs") or []
    lines.append("**Repaired:**\n" + ("\n".join(f"- {r}" for r in repairs) if repairs else "- nothing needed fixing"))

    already = [
        name
        for key, name in (
            ("funnel_channel", "public funnel channel"),
            ("bump_channel", "bump channel"),
        )
        if report.get(key) == "OK" and not any(name.split()[0] in r for r in repairs)
    ]
    invite = report.get("tracking_invite") or ""
    if invite.startswith("https://") and not any("invite" in r for r in repairs):
        already.append("tracking invite")
    lines.append("**Already OK:**\n" + ("\n".join(f"- {a}" for a in already) if already else "- none"))

    errors = report.get("errors") or []
    lines.append("**Errors:**\n" + ("\n".join(f"- {e}" for e in errors) if errors else "- none"))
    return "\n\n".join(lines)


class ConfirmFresh(discord.ui.View):
    """Two-step confirmation for the destructive rebuild."""

    def __init__(self, author_id: int):
        super().__init__(timeout=CONFIRM_TIMEOUT_SECONDS)
        self.author_id = author_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This confirmation belongs to someone else.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm Fresh Setup", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.confirmed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Rebuilding…", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.confirmed = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=self)
        self.stop()


class WaypointCommands(commands.Cog):
    group = app_commands.Group(
        name="waypoint",
        description="Set up and look after this feeder server",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # Shared checks
    # ------------------------------------------------------------------
    async def deny_reason(self, interaction: discord.Interaction, destructive: bool = False) -> str:
        """Why this person may not run this here, or an empty string."""
        guild = interaction.guild
        if guild is None:
            return "Run this inside the server you want to configure."
        if not config.is_approved_owner(interaction.user.id):
            return "Only an approved owner can use Waypoint commands here."
        if destructive:
            # Manage Server is not enough: the destructive command needs the
            # guild's actual owner, who is also on the approved list.
            if interaction.user.id != guild.owner_id:
                return "Fresh Setup can only be run by the owner of this server."
            async with session() as db:
                main_id = await settings_store.main_guild_id(db)
                server = await crud.get_server(db, guild.id)
            if main_id == guild.id:
                return "Fresh Setup is disabled on the MAIN server."
            if server is None or server.server_type != constants.FEEDER:
                return "Fresh Setup only runs on feeder servers."
        return ""

    # ------------------------------------------------------------------
    # /waypoint setup
    # ------------------------------------------------------------------
    @group.command(name="setup", description="Create anything Waypoint needs, keeping your channels")
    async def setup_command(self, interaction: discord.Interaction) -> None:
        denied = await self.deny_reason(interaction)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        missing = feeder_setup.missing_permissions(guild)
        if missing:
            await interaction.followup.send(
                "I need these permissions first: " + ", ".join(missing), ephemeral=True
            )
            return

        async with session() as db:
            server = await crud.upsert_server(db, guild.id, guild.name, guild.owner_id)
            if server.server_type not in constants.MAIN_TYPES:
                server.server_type = constants.FEEDER
                await db.commit()
            await crud.log(db, "manual_setup_requested", f"by user {interaction.user.id}", guild.id)
            report = await feeder_setup.ensure_feeder(self.bot, db, guild, "slash command setup")

        await interaction.followup.send(summarise(report), ephemeral=True)

    # ------------------------------------------------------------------
    # /waypoint repair
    # ------------------------------------------------------------------
    @group.command(name="repair", description="Fix anything Waypoint manages that is missing or broken")
    async def repair_command(self, interaction: discord.Interaction) -> None:
        denied = await self.deny_reason(interaction)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        missing = feeder_setup.missing_permissions(guild)
        if missing:
            await interaction.followup.send(
                "I need these permissions first: " + ", ".join(missing), ephemeral=True
            )
            return

        async with session() as db:
            main_problems = await feeder_setup.main_server_problems(self.bot, db, await crud.get_server(db, guild.id))
            await crud.log(db, "manual_repair_requested", f"by user {interaction.user.id}", guild.id)
            report = await feeder_setup.ensure_feeder(self.bot, db, guild, "slash command repair")
            server = await crud.get_server(db, guild.id)
            if server:
                await feeder_setup.refresh_public_message(self.bot, db, server)
            await crud.log(db, "repair_completed", "; ".join(report["repairs"]) or "nothing to fix", guild.id)

        report["errors"] = list(report["errors"]) + main_problems
        await interaction.followup.send(summarise(report), ephemeral=True)

    # ------------------------------------------------------------------
    # /waypoint fresh
    # ------------------------------------------------------------------
    @group.command(name="fresh", description="DESTRUCTIVE: delete every channel and rebuild the feeder layout")
    async def fresh_command(self, interaction: discord.Interaction) -> None:
        denied = await self.deny_reason(interaction, destructive=True)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return

        guild = interaction.guild
        # Full preflight before the warning is even shown: feeder permissions
        # first, then whether the MAIN server can actually issue the tracking
        # invite this rebuild will need.
        missing = feeder_setup.missing_permissions(guild, destructive=True)
        if missing:
            await interaction.response.send_message(
                "I need these permissions before I can do this: " + ", ".join(missing),
                ephemeral=True,
            )
            return

        async with session() as db:
            main_problems = await feeder_setup.main_server_problems(self.bot, db, await crud.get_server(db, guild.id))
        if main_problems:
            await interaction.response.send_message(
                "Fresh Setup cancelled: Waypoint cannot create tracking invites in the "
                "MAIN server. " + " ".join(main_problems) + "\n\nNothing was deleted.",
                ephemeral=True,
            )
            return

        async with session() as db:
            await crud.log(
                db, "fresh_setup_requested", f"by user {interaction.user.id}", guild.id
            )

        view = ConfirmFresh(interaction.user.id)
        warning = (
            f"**WARNING**\nThis will delete every channel and category in **{guild.name}** "
            "and rebuild the Waypoint feeder layout.\n\n"
            "It will **not** delete roles, members, emojis or server settings.\n\n"
            f"This confirmation expires in {CONFIRM_TIMEOUT_SECONDS} seconds."
        )
        await interaction.response.send_message(warning, view=view, ephemeral=True)
        await view.wait()

        if not view.confirmed:
            if view.confirmed is None:
                await interaction.followup.send(
                    "Confirmation timed out. Nothing was changed.", ephemeral=True
                )
            return

        async with session() as db:
            report = await feeder_setup.fresh_setup(self.bot, db, guild, interaction.user.id)

        lines = [
            f"**Deleted:** {len(report['deleted'])} channel(s)",
            "**Created:** " + (", ".join(report["created"]) or "nothing"),
        ]
        if report["kept"]:
            lines.append("**Could not delete:** " + ", ".join(report["kept"]))
        if report["tracking_invite"]:
            lines.append(f"**Tracking invite:** {report['tracking_invite']}")
        lines.append("**Errors:** " + ("; ".join(report["errors"]) or "none"))
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ------------------------------------------------------------------
    # /waypoint status
    # ------------------------------------------------------------------
    @group.command(name="status", description="Show what Waypoint knows about this server")
    async def status_command(self, interaction: discord.Interaction) -> None:
        denied = await self.deny_reason(interaction)
        if denied:
            await interaction.response.send_message(denied, ephemeral=True)
            return

        guild = interaction.guild
        async with session() as db:
            server = await crud.get_server(db, guild.id)
            main = await settings_store.main_server(db)
            values = await settings_store.get_all(db)
            effective = settings_store.resolve(values, server)
            invite = (
                await crud.active_invite(db, guild.id, main.guild_id) if (server and main) else None
            )
            worker = await crud.worker_status(db)
            destination_rows = await routing.rows(db,server) if server else []

        if server is None:
            await interaction.response.send_message(
                "This server is not registered yet. Run /waypoint setup.", ephemeral=True
            )
            return

        health = server.health or {}
        lines = [
            f"**Type:** {server.server_type}",
            f"**Network:** {values.get('network_name')}",
            f"**Main servers:** {', '.join(d['name'] for d in destination_rows) or 'not configured'}",
            f"**Funnel channel:** #{effective['funnel_channel_name']} ({health.get('funnel_channel', 'not checked yet')})",
            f"**Bump channel:** #{effective['bump_channel_name']} ({health.get('bump_channel', 'not checked yet')})",
            f"**Tracking invites:** {', '.join(d['invite_url'] for d in destination_rows if d['invite_url']) or 'none yet'}",
            f"**Funnel mode:** {effective['funnel_mode']}",
            f"**Auto repair:** {'on' if effective['auto_repair'] else 'off'}",
            f"**Worker:** {'online' if worker['online'] else 'offline'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WaypointCommands(bot))
