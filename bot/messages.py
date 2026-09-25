"""Turn a rendered message blob into real Discord objects.

Two kinds of join button, because Discord does not let you have both at once:

* LINK — carries the tracking invite directly, one click, no round trip. Its
  colour is fixed by Discord and cannot be changed.
* INTERACTIVE — a coloured button (primary, secondary, success or danger)
  that calls back into the bot, which replies privately with that feeder's
  current tracking invite. Attribution is identical, because the same invite
  is handed over either way.

The interactive button is a dynamic item: its custom_id carries the feeder's
guild id, so a message posted weeks ago still resolves the right invite after
a restart, and picks up a rotated invite automatically.
"""
from __future__ import annotations

from typing import Any

import discord


def _color(value: str | None) -> discord.Colour:
    try:
        return discord.Colour(int(str(value or "#5865F2").lstrip("#"), 16))
    except (ValueError, TypeError):
        return discord.Colour(0x5865F2)


def build_embed(content: dict[str, Any]) -> discord.Embed | None:
    if not content.get("use_embed"):
        return None
    embed = discord.Embed(
        title=content.get("embed_title") or None,
        description=content.get("body") or None,
        colour=_color(content.get("embed_color")),
    )
    if content.get("image_url"):
        embed.set_image(url=content["image_url"])
    if content.get("footer"):
        embed.set_footer(text=content["footer"])
    return embed


STYLE_MAP = {
    "PRIMARY": discord.ButtonStyle.primary,
    "SECONDARY": discord.ButtonStyle.secondary,
    "SUCCESS": discord.ButtonStyle.success,
    "DANGER": discord.ButtonStyle.danger,
}


def button_emoji(content: dict[str, Any]):
    raw = (content.get("button_emoji") or "").strip()
    if not raw:
        return None
    try:
        return discord.PartialEmoji.from_str(raw)
    except Exception:
        return None


class InviteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"waypoint:invite:(?P<guild_id>\d+)",
):
    """A coloured button that hands over the feeder's current invite."""

    def __init__(self, guild_id: int, label: str, style: discord.ButtonStyle, emoji=None):
        self.guild_id = guild_id
        super().__init__(
            discord.ui.Button(
                label=label[:80] or "Join",
                style=style,
                emoji=emoji,
                custom_id=f"waypoint:invite:{guild_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # pragma: no cover - discord hook
        return cls(int(match["guild_id"]), item.label or "Join", item.style, item.emoji)

    async def callback(self, interaction: discord.Interaction) -> None:
        await send_current_destinations(interaction, self.guild_id)


async def send_current_destinations(interaction, feeder_id, target_id=None):
    # Acknowledge before any database access to avoid Discord's response timeout.
    await interaction.response.defer(ephemeral=True, thinking=True)
    from core import constants, routing
    from database.database import session
    from database import crud
    try:
        async with session() as db:
            feeder = await crud.get_server(db, feeder_id)
            destinations = await routing.rows(db, feeder) if feeder and feeder.server_type == constants.FEEDER else []
        if target_id is not None:
            destinations = [d for d in destinations if d["guild_id"] == target_id]
        ready = [d for d in destinations if d["invite_url"]]
        if not ready:
            await interaction.followup.send("That destination is no longer selected or its invite is not ready. Please check the current welcome post.", ephemeral=True)
            return
        content = "\n".join(f"**{d['name']}**: {d['invite_url']}" for d in ready)
        await interaction.followup.send(content[:2000], ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        await interaction.followup.send("The invite could not be loaded. Please try again shortly.", ephemeral=True)


class DestinationButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"waypoint:destination:(?P<feeder_id>\d+):(?P<target_id>\d+)",
):
    def __init__(self, feeder_id, target_id, label, style, emoji=None):
        self.feeder_id, self.target_id = int(feeder_id), int(target_id)
        super().__init__(discord.ui.Button(label=label[:80] or "Join", style=style, emoji=emoji,
                         custom_id=f"waypoint:destination:{feeder_id}:{target_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["feeder_id"]), int(match["target_id"]), item.label or "Join", item.style, item.emoji)

    async def callback(self, interaction):
        await send_current_destinations(interaction, self.feeder_id, self.target_id)


def build_view(
    content: dict[str, Any], invite_url: str, feeder_guild_id: int | None = None, destinations: list[dict] | None = None
) -> discord.ui.View | None:
    mode = str(content.get("button_mode") or "LINK").upper()
    label = (content.get("button_label") or "Join")[:80]
    emoji = button_emoji(content)

    if destinations is not None:
        ready = [d for d in destinations if d.get("invite_url")]
        if not ready:
            return None
        view = discord.ui.View(timeout=None)
        style = STYLE_MAP.get(str(content.get("button_style") or "PRIMARY").upper(), discord.ButtonStyle.primary)
        for destination in ready[:25]:
            target_label = (destination.get("label") or (label if len(ready) == 1 else f"Join {destination['name']}"))[:80]
            if mode == "INTERACTIVE" and feeder_guild_id:
                item = DestinationButton(feeder_guild_id, destination["guild_id"], target_label, style, emoji)
            else:
                item = discord.ui.Button(style=discord.ButtonStyle.link, label=target_label,
                                         url=destination["invite_url"], emoji=emoji)
            view.add_item(item)
        return view

    if mode == "INTERACTIVE" and feeder_guild_id:
        style = STYLE_MAP.get(str(content.get("button_style") or "PRIMARY").upper(),
                              discord.ButtonStyle.primary)
        view = discord.ui.View(timeout=None)
        view.add_item(InviteButton(int(feeder_guild_id), label, style, emoji))
        return view

    if not invite_url:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.link, label=label, url=invite_url, emoji=emoji
        )
    )
    return view


def build_payload(
    content: dict[str, Any], invite_url: str, feeder_guild_id: int | None = None, destinations: list[dict] | None = None
) -> dict[str, Any]:
    """Kwargs for `send` / `edit`."""
    embed = build_embed(content)
    payload: dict[str, Any] = {
        "content": None if embed else (content.get("body") or None),
        "embed": embed,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    view = build_view(content, invite_url, feeder_guild_id, destinations)
    payload["view"] = view if view else None
    return payload


def destination_labels(content, context, destinations):
    from core import rendering
    labeled=[]
    for destination in destinations:
        per_target=dict(context, main_server_name=destination["name"], invite_url=destination["invite_url"])
        label=rendering.render_content(content, per_target).get("button_label") or "Join"
        if len(destinations)>1 and destination["name"] not in label:
            label=f"{label} · {destination['name']}"
        labeled.append(dict(destination, label=label[:80]))
    return labeled
