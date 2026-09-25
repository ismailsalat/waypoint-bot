"""The funnel DM.

One DM, once, to a person who joined a feeder and is not already in the main
server. Every branch records why it happened so the dashboard can show you the
truth instead of a guess.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from bot import feeder_setup, messages
from core import routing, constants, rendering, settings as settings_store
from database import crud
from database.database import session
from database.models import Server

log = logging.getLogger("funnel.dm")


async def is_in_main_server(bot: Any, main_guild_id: int, user_id: int) -> bool:
    guild = bot.get_guild(main_guild_id)
    if guild is None:
        return False
    if guild.get_member(user_id) is not None:
        return True
    try:
        await guild.fetch_member(user_id)
        return True
    except discord.NotFound:
        return False
    except discord.HTTPException as exc:
        log.warning("Could not check main-server membership for %s: %s", user_id, exc)
        return False


async def resolve_invite_url(bot: Any, db, server: Server, main_guild_id: int) -> str:
    invite = await crud.active_invite(db, server.guild_id, main_guild_id)
    if invite:
        return invite.url
    main_guild = bot.get_guild(main_guild_id)
    if main_guild is None:
        return ""
    try:
        invite, _ = await feeder_setup.ensure_tracking_invite(bot, db, server, main_guild)
        return invite.url
    except Exception as exc:  # noqa: BLE001 - reported to the audit log
        await crud.log(
            db, "tracking_invite_error", f"Could not create an invite for {server.name}: {exc}",
            server.guild_id,
        )
        return ""


async def build_dm_bundle(db, bot, user, server, selected=None, content=None, kind=constants.KIND_DM):
    ids = selected if selected is not None else await routing.destination_ids(db, server)
    destinations=[]
    for gid in ids:
        main=await crud.get_server(db,gid)
        url=await resolve_invite_url(bot,db,server,gid) if server else ""
        if url:
            destinations.append({"guild_id":gid,"name":main.name if main else str(gid),"invite_url":url})
    version_id=None
    if content is None:
        content,version_id=await crud.message_content(db,kind,server.guild_id if server else None)
    else:
        defaults=rendering.default_content(kind)
        defaults.update(content)
        content=defaults
    first=destinations[0]["invite_url"] if destinations else ""
    context=await crud.network_context(db,server,first)
    if destinations:
        context["main_server_name"]=", ".join(d["name"] for d in destinations)
    context["user_name"]=getattr(user,"name","there")
    context["user_display_name"]=getattr(user,"display_name",context["user_name"])
    return rendering.render_content(content,context),first,version_id,messages.destination_labels(content,context,destinations)


async def build_dm(db, bot, user, server, main_guild_id):
    rendered,url,version,_=await build_dm_bundle(db,bot,user,server,[main_guild_id])
    return rendered,url,version


async def handle_feeder_join(bot: Any, user: Any, feeder_guild_id: int, wait: bool = True) -> str:
    """Send one DM containing the destinations the member has not joined."""
    if getattr(user,"bot",False):
        return "IGNORED_BOT"
    async with session() as db:
        server=await crud.get_server(db,feeder_guild_id)
        if server is None or server.server_type != constants.FEEDER:
            return "NOT_A_FEEDER"
        effective=await settings_store.effective(db,server)
    if wait and effective.get("dm_delay_seconds"):
        await asyncio.sleep(int(effective["dm_delay_seconds"]))
    # Configuration is deliberately re-read AFTER the delay.
    async with session() as db:
        server=await crud.get_server(db,feeder_guild_id)
        if server is None or server.server_type != constants.FEEDER:
            return "NOT_A_FEEDER"
        values=await settings_store.get_all(db,fresh=True)
        effective=settings_store.resolve(values,server)
        mode=effective.get("funnel_mode") or constants.LIVE
        if mode==constants.OFF:
            return "FUNNEL_OFF"
        main_ids=await routing.destination_ids(db,server)
        if not main_ids:
            return "NO_MAIN_SERVER"
        is_test=await settings_store.development_mode(db)
        test_user=await settings_store.admin_test_user_id(db)
        if is_test and user.id != test_user:
            await crud.record_dm_event(db,user.id,feeder_guild_id,constants.DM_SKIPPED_DEV,is_test=True,detail="development mode is on")
            return constants.DM_SKIPPED_DEV
        selected=[]
        for gid in main_ids:
            if not await is_in_main_server(bot,gid,user.id):
                selected.append(gid)
        if not selected:
            await crud.record_dm_event(db,user.id,feeder_guild_id,constants.DM_SKIPPED_IN_MAIN,is_test=is_test,detail="already a member of every selected main server")
            return constants.DM_SKIPPED_IN_MAIN
        allowed,reason=await crud.may_send_dm(db,user.id,feeder_guild_id,values,include_test=is_test)
        if not allowed:
            await crud.record_dm_event(db,user.id,feeder_guild_id,constants.DM_SKIPPED_POLICY,is_test=is_test,detail=reason)
            return constants.DM_SKIPPED_POLICY
        rendered,invite_url,version_id,destinations=await build_dm_bundle(db,bot,user,server,selected)
        if not destinations:
            await crud.record_dm_event(db,user.id,feeder_guild_id,constants.DM_FAILED_OTHER,is_test=is_test,detail="no tracking invite available")
            return constants.DM_FAILED_OTHER
        experiment=await crud.active_experiment(db,feeder_guild_id)
        metadata={"message_version_id":version_id,"tag_experiment_id":experiment.id if experiment else None,"is_test":is_test}
        if mode==constants.DRY_RUN:
            await crud.record_dm_event(db,user.id,feeder_guild_id,constants.DM_DRY_RUN,**metadata,
                invite_code=invite_url.rsplit("/",1)[-1],detail=f"would have sent: {rendered.get('body','')[:300]}")
            return constants.DM_DRY_RUN
        try:
            await user.send(**messages.build_payload(rendered,invite_url,server.guild_id,destinations))
        except discord.Forbidden:
            await crud.record_dm_event(db,user.id,feeder_guild_id,constants.DM_FAILED_CLOSED,**metadata,detail="the user has DMs closed")
            return constants.DM_FAILED_CLOSED
        except discord.HTTPException as exc:
            await crud.record_dm_event(db,user.id,feeder_guild_id,constants.DM_FAILED_OTHER,**metadata,detail=str(exc)[:500])
            return constants.DM_FAILED_OTHER
        await crud.record_dm_event(db,user.id,feeder_guild_id,constants.DM_SENT,**metadata,
            invite_code=invite_url.rsplit("/",1)[-1],detail="Destinations offered: "+", ".join(str(d["guild_id"]) for d in destinations))
        log.info("Funnel DM sent to %s from %s",user,server.name)
        return constants.DM_SENT


async def send_test_dm(bot: Any, target_user_id: int, kind: str, content: dict[str, Any] | None, feeder_guild_id: int | None) -> str:
    async with session() as db:
        allowed_user_id=await settings_store.admin_test_user_id(db)
    if not allowed_user_id:
        return "No test user is set. Add one on the Settings page."
    if target_user_id != allowed_user_id:
        return "Test messages can only go to the test user set in Settings."
    user=bot.get_user(target_user_id)
    if user is None:
        try:
            user=await bot.fetch_user(target_user_id)
        except discord.HTTPException:
            return f"Could not find the user {target_user_id}."
    if user is None:
        return f"Could not find the user {target_user_id}."
    async with session() as db:
        server=await crud.get_server(db,feeder_guild_id) if feeder_guild_id else None
        rendered,invite_url,_,destinations=await build_dm_bundle(db,bot,user,server,content=content,kind=kind)
        payload=messages.build_payload(rendered,invite_url,server.guild_id if server else None,destinations)
        payload["content"]="**TEST MESSAGE — not sent to anyone else**\n"+(payload.get("content") or "")
        try:
            await user.send(**payload)
        except discord.Forbidden:
            return "Your DMs are closed, so the test could not be delivered."
        except discord.HTTPException as exc:
            return f"Discord rejected the test message: {exc}"
        await crud.record_dm_event(db,target_user_id,feeder_guild_id,constants.DM_SENT,is_test=True,detail=f"test send ({kind})")
        return f"Test {kind} message sent to {user}."
