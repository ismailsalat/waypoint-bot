"""Keep local scheduler targets aware of managed channel changes and rebuilds."""
from __future__ import annotations
import asyncio
import logging
from sqlalchemy import select
from core import constants
from database.database import session
from database.models import Server, Task

log=logging.getLogger(__name__)

async def refresh_catalog(runtime):
    async with session() as db:
        feeders=(await db.execute(select(Server).where(Server.server_type==constants.FEEDER))).scalars().all()
        tasks=(await db.execute(select(Task).where(
            Task.task_type.in_([constants.TASK_FRESH_SETUP,constants.TASK_SETUP_FEEDER]),
            Task.status.in_([constants.TASK_PENDING,constants.TASK_RUNNING])))).scalars().all()
        blocked={str(t.guild_id or (t.payload or {}).get('guild_id')) for t in tasks}
        catalog={str(s.guild_id):{'channel_id':str(s.bump_channel_id or ''),
                                 'ready':bool(s.bot_present and s.bump_channel_id and str(s.guild_id) not in blocked)} for s in feeders}
    runtime.update_catalog(catalog)

async def watch_catalog(runtime):
    while True:
        try:
            await refresh_catalog(runtime)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Could not refresh scheduler channel assignments')
        await asyncio.sleep(2)
