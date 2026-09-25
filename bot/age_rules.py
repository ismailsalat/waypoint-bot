"""Role rules.

Deliberately small: "if someone gets this role, do this one thing". The point
is age self-attestation (no birth dates are ever collected), but the same rule
shape works for any role you want to react to.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

import discord
from discord.ext import commands

from core import constants, settings as settings_store
from database import crud
from database.database import session
from database.models import RoleRule

log = logging.getLogger("funnel.roles")


def triggered_rules(
    before_role_ids: Iterable[int], after_role_ids: Iterable[int], rules: Sequence[RoleRule]
) -> list[RoleRule]:
    """Rules whose role was just added. Pure, so it is easy to test."""
    added = set(after_role_ids) - set(before_role_ids)
    return [rule for rule in rules if rule.enabled is not False and rule.role_id in added]


async def apply_rule(member: Any, rule: RoleRule) -> str:
    """Run one rule against one member. Returns a human-readable outcome."""
    reason = rule.reason or "funnel bot role rule"

    if rule.action == constants.ACTION_KICK:
        await member.kick(reason=reason)
        return f"kicked {member} (role {rule.role_name})"

    if rule.action == constants.ACTION_ADD_ROLE and rule.target_role_id:
        role = member.guild.get_role(rule.target_role_id)
        if role is None:
            return f"target role {rule.target_role_id} not found"
        await member.add_roles(role, reason=reason)
        return f"added {role.name} to {member}"

    if rule.action == constants.ACTION_REMOVE_ROLE and rule.target_role_id:
        role = member.guild.get_role(rule.target_role_id)
        if role is None:
            return f"target role {rule.target_role_id} not found"
        await member.remove_roles(role, reason=reason)
        return f"removed {role.name} from {member}"

    return f"{member} received {rule.role_name} (log only)"


class AgeRules(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles == after.roles:
            return

        async with session() as db:
            server = await crud.get_server(db, after.guild.id)
            if server is None:
                return
            effective = await settings_store.effective(db, server)
            if not effective.get("age_enforcement"):
                return

            rules = await crud.rules_for_guild(db, after.guild.id)
            matches = triggered_rules(
                [r.id for r in before.roles], [r.id for r in after.roles], rules
            )
            for rule in matches:
                try:
                    outcome = await apply_rule(after, rule)
                    await crud.log(db, "role_rule", outcome, after.guild.id)
                    log.info("Role rule in %s: %s", after.guild.name, outcome)
                except discord.Forbidden:
                    await crud.log(
                        db,
                        "role_rule_failed",
                        f"Missing permission to {rule.action} {after}",
                        after.guild.id,
                    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AgeRules(bot))
