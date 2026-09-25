"""Per-feeder routing. None inherits the default; a list pins destinations."""
from __future__ import annotations
from sqlalchemy import select
from core import constants, settings
from database.models import Server, TrackingInvite

MAX_DESTINATIONS = 25  # Discord's component limit: five rows of five buttons.


async def destination_ids(db, feeder: Server | None) -> list[int]:
    if feeder is not None and feeder.destination_guild_ids is not None:
        return list(dict.fromkeys(int(g) for g in feeder.destination_guild_ids))
    default = await settings.main_guild_id(db)
    return [default] if default else []


async def main_ids(db) -> set[int]:
    ids = set()
    default = await settings.main_guild_id(db)
    if default:
        ids.add(default)
    for server in (await db.execute(select(Server))).scalars():
        if server.server_type in constants.MAIN_TYPES:
            ids.add(server.guild_id)
        if server.server_type == constants.FEEDER and server.destination_guild_ids:
            ids.update(server.destination_guild_ids)
    return ids


async def rows(db, feeder: Server | None) -> list[dict]:
    result = []
    for gid in await destination_ids(db, feeder):
        main = await db.get(Server, gid)
        invite = None
        if feeder:
            invite = (await db.execute(select(TrackingInvite).where(
                TrackingInvite.feeder_guild_id == feeder.guild_id,
                TrackingInvite.main_guild_id == gid,
                TrackingInvite.active.is_(True)).order_by(TrackingInvite.id.desc()))).scalars().first()
        result.append({'guild_id':gid, 'name':main.name if main else str(gid),
                       'server':main, 'invite':invite, 'invite_url':invite.url if invite else ''})
    return result


async def validate_selection(db, feeder, selected):
    if selected is None:
        return
    if not selected or len(selected) > MAX_DESTINATIONS or len(set(selected)) != len(selected):
        raise ValueError('Choose between 1 and 25 distinct main servers, or use the default.')
    for gid in selected:
        server = await db.get(Server, gid)
        if gid == feeder.guild_id or not server or server.server_type not in constants.MAIN_TYPES:
            raise ValueError('Destinations must be main servers. Mark additional main servers on the Servers page first.')
        if not server.bot_present:
            raise ValueError('The management bot must be in each selected main server.')


async def feeders_using(db, guild_id):
    result=[]
    for feeder in (await db.execute(select(Server).where(Server.server_type == constants.FEEDER))).scalars():
        if guild_id in await destination_ids(db, feeder):
            result.append(feeder)
    return result
