"""Local dashboard for the feeder network and account scheduler.

Feeder operations are queued through the shared database for the bot worker.
The scheduler runs in this dashboard process and uses its own account transport.
"""
from __future__ import annotations

import json
import asyncio
import os
import anyio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core import routing, constants, rendering, settings as settings_store
from core.config import config
from database import analytics, crud
from database.database import database_status, init_db, session
from database.models import Conversion, MessageVersion, RoleRule, Server, TagExperiment

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    async with session() as db:
        await crud.ensure_default_messages(db)
    from suite.runtime import Runtime
    scheduler_dir = Path(os.getenv("SCHEDULER_DATA_DIR") or str(BASE_DIR.parent / "data")).resolve()
    runtime = Runtime(scheduler_dir)
    from suite.bridge import refresh_catalog, watch_catalog
    await refresh_catalog(runtime)
    await anyio.to_thread.run_sync(runtime.startup)
    _app.state.scheduler = runtime
    catalog_task=asyncio.create_task(watch_catalog(runtime))
    try:
        yield
    finally:
        catalog_task.cancel()
        try:
            await catalog_task
        except asyncio.CancelledError:
            pass
        await anyio.to_thread.run_sync(runtime.shutdown)
        _app.state.scheduler = None


app = FastAPI(title="Waypoint Suite dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


async def get_db() -> AsyncSession:
    async with session() as db:
        yield db


def back(url: str, message: str = "") -> RedirectResponse:
    if message:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}msg={message}"
    return RedirectResponse(url, status_code=303)


async def page_context(request: Request, db: AsyncSession, **extra: Any) -> dict[str, Any]:
    values = await settings_store.get_all(db)
    main = await settings_store.main_server(db)
    ctx = {
        "request": request,
        "settings": values,
        "main_server": main,
        "network_name": values.get("network_name"),
        "development_mode": await settings_store.development_mode(db),
        "database": database_status(connected=True),
        "message": request.query_params.get("msg", ""),
        "constants": constants,
        "path": request.url.path,
    }
    ctx.update(extra)
    return ctx


def _int_or_none(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def _tri_bool(raw: str) -> bool | None:
    """Dashboard selects use "" for 'use the global default'."""
    if raw in ("", "default", None):
        return None
    return raw in ("true", "on", "1", "yes")


# --------------------------------------------------------------------------
# First run
# --------------------------------------------------------------------------
@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, db: AsyncSession = Depends(get_db)):
    servers = await crud.list_servers(db)
    suggested = ""
    values = await settings_store.get_all(db)
    if servers:
        suggested = "join-" + servers[0].name.lower().replace(" ", "-")[:80]
    return templates.TemplateResponse(
        request,
        "setup.html",
        await page_context(
            request, db, servers=servers, suggested_channel=suggested or "join-main", values=values
        ),
    )


@app.post("/setup")
async def save_setup(
    request: Request,
    main_guild_id: str = Form(""),
    network_name: str = Form("My Network"),
    default_funnel_channel_name: str = Form("join-main"),
    default_bump_channel_name: str = Form("bump"),
    default_dm_delay_seconds: int = Form(5),
    db: AsyncSession = Depends(get_db),
):
    guild_id = _int_or_none(main_guild_id)
    await settings_store.set_many(
        db,
        {
            "main_guild_id": guild_id,
            "network_name": network_name.strip() or "My Network",
            "default_funnel_channel_name": default_funnel_channel_name.strip() or "join-main",
            "default_bump_channel_name": default_bump_channel_name.strip() or "bump",
            "default_dm_delay_seconds": max(int(default_dm_delay_seconds), 0),
            "setup_complete": True,
        },
    )
    if guild_id:
        await crud.set_main_server(db, guild_id)
    await crud.log(db, "setup_saved", f"Main server set to {guild_id}", source="dashboard")
    return back("/", "Setup saved")


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def overview(request: Request, db: AsyncSession = Depends(get_db)):
    values = await settings_store.get_all(db)
    if not values.get("setup_complete") and not values.get("main_guild_id"):
        return RedirectResponse("/setup", status_code=303)

    summary = await analytics.network_summary(db)
    logs = await crud.recent_logs(db, limit=12)
    conversions = await crud.list_conversions(db, limit=8)
    names = {s.guild_id: s.name for s in await crud.list_servers(db)}
    return templates.TemplateResponse(
        request,
        "index.html",
        await page_context(
            request, db, summary=summary, logs=logs, conversions=conversions, names=names
        ),
    )


# --------------------------------------------------------------------------
# Servers
# --------------------------------------------------------------------------
@app.get("/servers", response_class=HTMLResponse)
async def servers_page(request: Request, db: AsyncSession = Depends(get_db)):
    servers = await crud.list_servers(db)
    names = {s.guild_id: s.name for s in servers}
    destination_names={s.guild_id:", ".join(d["name"] for d in await routing.rows(db,s)) for s in servers if s.server_type==constants.FEEDER}
    return templates.TemplateResponse(request,"servers.html",await page_context(request,db,servers=servers,names=names,destination_names=destination_names))


@app.post("/servers/{guild_id}/type")
async def set_server_type(
    guild_id: int, server_type: str = Form(...), db: AsyncSession = Depends(get_db)
):
    server = await crud.get_server(db, guild_id)
    if server is None or server_type not in constants.SERVER_TYPES:
        return back("/servers", "That server type is not valid")

    if server_type not in constants.MAIN_TYPES and server.server_type in constants.MAIN_TYPES:
        users = await routing.feeders_using(db, guild_id)
        if users:
            return back("/servers", "Change the feeders using this main server before changing its type.")

    if server_type == constants.MAIN:
        # Promoting one server demotes whichever server was MAIN before, so
        # there is never more than one.
        changes = await crud.set_main_server(db, guild_id)
        await crud.log(db, "main_server_changed", "; ".join(changes), guild_id, "dashboard")
        return back("/servers", f"{server.name} is now the main server")

    if server.server_type == constants.MAIN:
        await crud.set_main_server(db,None)
    server.server_type = server_type
    if server_type == constants.FEEDER:
        server.destination_guild_id = await settings_store.main_guild_id(db)
    else:
        server.destination_guild_id = None
    await db.commit()
    await crud.log(db, "server_type_changed", f"{server.name} -> {server_type}", guild_id, "dashboard")

    if server_type == constants.FEEDER:
        await crud.queue_task(db, constants.TASK_SETUP_FEEDER, {"guild_id": guild_id})
        return back("/servers", f"{server.name} is now a feeder. Setup queued.")
    return back("/servers", f"{server.name} is now {server_type}")


# --------------------------------------------------------------------------
# Feeders
# --------------------------------------------------------------------------
@app.get("/feeders", response_class=HTMLResponse)
async def feeders_page(request: Request, db: AsyncSession = Depends(get_db)):
    feeders = await crud.list_feeders(db)
    rows = []
    main_id = await settings_store.main_guild_id(db)
    for feeder in feeders:
        destination_rows = await routing.rows(db, feeder)
        invite = next((d["invite"] for d in destination_rows if d["invite"]), None)
        experiment = await crud.active_experiment(db, feeder.guild_id)
        rows.append(
            {
                "server": feeder,
                "destinations": destination_rows,
                "effective": await settings_store.effective(db, feeder),
                "invite": invite,
                "tags": (experiment.tags if experiment else []) or [],
            }
        )
    return templates.TemplateResponse(
        request,
        "feeders.html", await page_context(request, db, rows=rows)
    )


@app.get("/feeders/{guild_id}", response_class=HTMLResponse)
@app.get("/servers/{guild_id}/configure", response_class=HTMLResponse)
async def feeder_page(request: Request, guild_id: int, db: AsyncSession = Depends(get_db)):
    """Settings for one server. Feeders get the funnel panels; the main server
    gets the age-rule panels, so age enforcement can be switched on there
    without switching it on everywhere."""
    server = await crud.get_server(db, guild_id)
    if server is None:
        return back("/feeders", "Server not found")
    main_id = await settings_store.main_guild_id(db)
    destination_rows = await routing.rows(db, server)
    invite = next((d["invite"] for d in destination_rows if d["invite"]), None)
    destination_options = [s for s in await crud.list_servers(db) if s.server_type in constants.MAIN_TYPES]
    experiment = await crud.active_experiment(db, guild_id)
    experiments = await crud.list_experiments(db, guild_id)
    rules = (
        await db.execute(select(RoleRule).where(RoleRule.guild_id == guild_id))
    ).scalars().all()
    overrides = await crud.list_versions(db, constants.KIND_DM, constants.SCOPE_FEEDER, guild_id)
    return templates.TemplateResponse(
        request,
        "feeder.html",
        await page_context(
            request,
            db,
            server=server,
            effective=await settings_store.effective(db, server),
            invite=invite,
            destinations=destination_rows,
            selected_destinations=await routing.destination_ids(db,server),
            destination_options=destination_options,
            tags=", ".join((experiment.tags if experiment else []) or []),
            experiments=experiments,
            rules=rules,
            overrides=overrides,
        ),
    )


@app.post("/feeders/{guild_id}/settings")
async def save_feeder(
    guild_id: int,
    funnel_channel_name: str = Form(""),
    bump_channel_name: str = Form(""),
    dm_delay_seconds: str = Form(""),
    funnel_mode: str = Form(""),
    auto_repair: str = Form(""),
    age_enforcement: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    server = await crud.get_server(db, guild_id)
    if server is None:
        return back("/feeders", "Server not found")

    server.funnel_channel_name = funnel_channel_name.strip() or None
    server.bump_channel_name = bump_channel_name.strip() or None
    server.dm_delay_seconds = _int_or_none(dm_delay_seconds)
    server.funnel_mode = funnel_mode if funnel_mode in constants.FUNNEL_MODES else None
    server.auto_repair = _tri_bool(auto_repair)
    server.age_enforcement = _tri_bool(age_enforcement)
    await db.commit()
    await crud.log(db, "server_settings_saved", server.name, guild_id, "dashboard")
    if server.server_type == constants.FEEDER:
        await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": guild_id})
        return back(f"/feeders/{guild_id}", "Saved. The bot will apply the changes shortly.")
    return back(f"/feeders/{guild_id}", "Saved.")



@app.post("/feeders/{guild_id}/destinations")
async def save_destinations(request: Request, guild_id: int, db: AsyncSession = Depends(get_db)):
    server=await crud.get_server(db,guild_id)
    if server is None or server.server_type!=constants.FEEDER:
        return back("/feeders","Choose a feeder server first.")
    form=await request.form()
    mode=str(form.get("destination_mode","selected"))
    if mode not in {"default","selected"}:
        return back(f"/feeders/{guild_id}","Invalid destination mode.")
    try:
        selected=None if mode=="default" else [int(v) for v in form.getlist("destination_guild_ids")]
        await routing.validate_selection(db,server,selected)
    except (ValueError,TypeError) as exc:
        return back(f"/feeders/{guild_id}",str(exc))
    server.destination_guild_ids=selected
    effective=await routing.destination_ids(db,server)
    server.destination_guild_id=effective[0] if effective else None
    server.last_health_check=None
    server.funnel_message_version_id=None
    await db.commit()
    await crud.log(db,"destinations_saved",f"{server.name}: {effective}",guild_id,"dashboard")
    await crud.queue_task(db,constants.TASK_REPAIR,{"guild_id":guild_id})
    return back(f"/feeders/{guild_id}","Destinations saved. Welcome buttons and future DMs will use these main servers.")

@app.post("/feeders/{guild_id}/tags")
async def save_tags(
    guild_id: int, tags: str = Form(""), note: str = Form(""), db: AsyncSession = Depends(get_db)
):
    parsed = [t.strip() for t in tags.replace("\n", ",").split(",") if t.strip()]
    experiment = await crud.set_tags(db, guild_id, parsed, note)
    await crud.log(
        db, "tags_changed", f"New tag experiment: {', '.join(experiment.tags) or 'none'}", guild_id, "dashboard"
    )
    return back(f"/feeders/{guild_id}", "New tag experiment started. The old one is kept.")


@app.post("/feeders/{guild_id}/repair")
async def repair_feeder(guild_id: int, db: AsyncSession = Depends(get_db)):
    await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": guild_id})
    return back(f"/feeders/{guild_id}", "Repair queued")


@app.post("/feeders/{guild_id}/fresh")
async def fresh_setup(
    request: Request, guild_id: int, confirm: str = Form(""), db: AsyncSession = Depends(get_db)
):
    """Destructive rebuild. Guarded three ways before anything is queued."""
    server = await crud.get_server(db, guild_id)
    main_id = await settings_store.main_guild_id(db)
    if server is None:
        return back("/feeders", "Server not found")
    if guild_id == main_id or server.server_type in constants.MAIN_TYPES:
        return back(f"/feeders/{guild_id}", "Fresh Setup is disabled on the MAIN server")
    if server.server_type != constants.FEEDER:
        return back(f"/feeders/{guild_id}", "Fresh Setup only runs on feeder servers")
    if confirm.strip().upper() != "FRESH":
        return back(f"/feeders/{guild_id}", "Type FRESH to confirm. Nothing was changed.")

    await crud.log(db, "fresh_setup_requested", f"{server.name} (dashboard)", guild_id, "dashboard")
    await crud.queue_task(db, constants.TASK_FRESH_SETUP, {"guild_id": guild_id})
    if getattr(request.app.state,"scheduler",None):
        from suite.bridge import refresh_catalog
        await refresh_catalog(request.app.state.scheduler)
    return back(f"/feeders/{guild_id}", f"Fresh Setup queued for {server.name}")


@app.post("/feeders/{guild_id}/repost")
async def repost_funnel_message(guild_id: int, db: AsyncSession = Depends(get_db)):
    await crud.queue_task(
        db, constants.TASK_SYNC_PUBLIC, {"guild_id": guild_id, "force": True}
    )
    return back(f"/feeders/{guild_id}", "Repost queued")


@app.post("/feeders/{guild_id}/rotate-invite")
async def rotate_invite(guild_id: int, db: AsyncSession = Depends(get_db)):
    await crud.queue_task(db, constants.TASK_ROTATE_INVITE, {"guild_id": guild_id})
    return back(f"/feeders/{guild_id}", "New tracking invite queued")


@app.post("/feeders/{guild_id}/rules")
async def add_rule(
    guild_id: int,
    role_id: str = Form(...),
    role_name: str = Form(""),
    action: str = Form(constants.ACTION_LOG_ONLY),
    target_role_id: str = Form(""),
    reason: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    parsed_role = _int_or_none(role_id)
    if parsed_role is None or action not in constants.ROLE_ACTIONS:
        return back(f"/feeders/{guild_id}", "Enter a numeric role ID and a valid action")
    db.add(
        RoleRule(
            guild_id=guild_id,
            role_id=parsed_role,
            role_name=role_name.strip(),
            action=action,
            target_role_id=_int_or_none(target_role_id),
            reason=reason.strip() or None,
        )
    )
    await db.commit()
    await crud.log(db, "role_rule_added", f"{role_name or role_id} -> {action}", guild_id, "dashboard")
    return back(f"/feeders/{guild_id}", "Rule added")


@app.post("/rules/{rule_id}/delete")
async def delete_rule(rule_id: int, guild_id: int = Form(...), db: AsyncSession = Depends(get_db)):
    rule = await db.get(RoleRule, rule_id)
    if rule:
        await db.delete(rule)
        await db.commit()
    return back(f"/feeders/{guild_id}", "Rule removed")


# --------------------------------------------------------------------------
# Message Studio
# --------------------------------------------------------------------------
@app.get("/messages", response_class=HTMLResponse)
async def message_studio(
    request: Request,
    kind: str = constants.KIND_DM,
    guild_id: int | None = None,
    version_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    kind = kind if kind in constants.MESSAGE_KINDS else constants.KIND_DM
    scope = constants.SCOPE_FEEDER if guild_id else constants.SCOPE_GLOBAL
    versions = await crud.list_versions(db, kind, scope, guild_id)

    if version_id:
        current = await db.get(MessageVersion, version_id)
    else:
        current = await crud.published_message(db, kind, guild_id) if guild_id else None
        if current is None:
            current = versions[0] if versions else None

    content = rendering.default_content(kind)
    if current and current.content:
        content.update(current.content)

    feeders = await crud.list_feeders(db)
    preview_feeder = await crud.get_server(db, guild_id) if guild_id else (feeders[0] if feeders else None)

    return templates.TemplateResponse(
        request,
        "messages.html",
        await page_context(
            request,
            db,
            kind=kind,
            scope=scope,
            guild_id=guild_id,
            versions=versions,
            current=current,
            content=content,
            feeders=feeders,
            preview_feeder=preview_feeder,
            variables=rendering.VARIABLES,
            variable_help=rendering.VARIABLE_HELP,
            custom_variables=(await settings_store.get_all(db)).get("custom_variables") or {},
            button_modes=constants.BUTTON_MODES,
            button_styles=constants.BUTTON_STYLES,
            admin_test_user_id=await settings_store.admin_test_user_id(db),
        ),
    )


def _studio_url(version: Any) -> str:
    if version is None:
        return "/messages"
    url = f"/messages?kind={version.kind}"
    if version.guild_id:
        url += f"&guild_id={version.guild_id}"
    return url


def _content_from_form(form: dict[str, Any]) -> dict[str, Any]:
    return {
        "body": form.get("body", ""),
        "use_embed": str(form.get("use_embed", "")).lower() in ("on", "true", "1"),
        "embed_title": form.get("embed_title", ""),
        "embed_color": form.get("embed_color", "#5865F2"),
        "image_url": form.get("image_url", ""),
        "footer": form.get("footer", ""),
        "button_label": form.get("button_label", "Join"),
        "button_emoji": form.get("button_emoji", ""),
        "button_mode": rendering.normalise_button_mode(form.get("button_mode")),
        "button_style": rendering.normalise_button_style(form.get("button_style")),
    }


@app.post("/api/preview")
async def api_preview(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.json()
    content = _content_from_form(payload.get("content", {}))
    guild_id = _int_or_none(str(payload.get("preview_guild_id") or ""))
    feeder = await crud.get_server(db, guild_id) if guild_id else None

    destination_rows = await routing.rows(db,feeder)
    invite_url=next((d["invite_url"] for d in destination_rows if d["invite_url"]),"https://discord.gg/example")

    context = await crud.network_context(db, feeder, invite_url)
    context["user_name"] = "newmember"
    context["user_display_name"] = "New Member"
    rendered = rendering.render_content(content, context)
    rendered["invite_url"] = invite_url
    from bot.messages import destination_labels
    preview_destinations=[dict(d,invite_url=d["invite_url"] or "https://discord.gg/example") for d in destination_rows]
    rendered["buttons"]=[{"label":d["label"],"invite_url":d["invite_url"]} for d in destination_labels(content,context,preview_destinations)]
    rendered["unknown_variables"] = rendering.unknown_variables(content, known=context.keys())
    rendered["bot_name"] = f"{await settings_store.main_server_name(db)} Funnel Bot"
    return JSONResponse(rendered)


@app.post("/messages/draft")
async def save_draft(request: Request, db: AsyncSession = Depends(get_db)):
    form = dict(await request.form())
    kind = form.get("kind", constants.KIND_DM)
    guild_id = _int_or_none(str(form.get("guild_id") or ""))
    scope = constants.SCOPE_FEEDER if guild_id else constants.SCOPE_GLOBAL
    version = await crud.save_draft(
        db, kind, scope, _content_from_form(form), guild_id, str(form.get("note", ""))
    )
    await crud.log(db, "message_draft_saved", f"{kind} version {version.version}", guild_id, "dashboard")
    target = f"/messages?kind={kind}&version_id={version.id}" + (f"&guild_id={guild_id}" if guild_id else "")
    return back(target, f"Draft saved as version {version.version}")


@app.post("/messages/{version_id}/publish")
async def publish(version_id: int, db: AsyncSession = Depends(get_db)):
    version = await crud.publish_version(db, version_id)
    if version is None:
        return back("/messages", "Version not found")
    await crud.log(
        db, "message_published", f"{version.kind} version {version.version}", version.guild_id, "dashboard"
    )
    if version.kind == constants.KIND_PUBLIC:
        await crud.queue_task(db, constants.TASK_SYNC_PUBLIC, {"guild_id": version.guild_id})
    target = f"/messages?kind={version.kind}&version_id={version.id}" + (
        f"&guild_id={version.guild_id}" if version.guild_id else ""
    )
    return back(target, f"Published version {version.version}")


@app.post("/messages/{version_id}/restore")
async def restore(version_id: int, db: AsyncSession = Depends(get_db)):
    new_version = await crud.restore_version(db, version_id)
    if new_version is None:
        return back("/messages", "Version not found")
    target = f"/messages?kind={new_version.kind}&version_id={new_version.id}" + (
        f"&guild_id={new_version.guild_id}" if new_version.guild_id else ""
    )
    return back(target, f"Copied into a new draft, version {new_version.version}")


@app.post("/messages/{version_id}/delete")
async def delete_version(version_id: int, db: AsyncSession = Depends(get_db)):
    version = await db.get(MessageVersion, version_id)
    target = _studio_url(version)
    ok, message = await crud.delete_version(db, version_id)
    if ok:
        await crud.log(db, "draft_deleted", message, source="dashboard")
    return back(target, message)


@app.post("/messages/{version_id}/archive")
async def archive_version(version_id: int, db: AsyncSession = Depends(get_db)):
    version = await db.get(MessageVersion, version_id)
    target = _studio_url(version)
    ok, message = await crud.archive_version(db, version_id)
    if ok:
        await crud.log(db, "version_archived", message, source="dashboard")
    return back(target, message)


@app.post("/messages/{version_id}/duplicate")
async def duplicate_version(version_id: int, db: AsyncSession = Depends(get_db)):
    copy = await crud.duplicate_version(db, version_id)
    if copy is None:
        return back("/messages", "Version not found")
    return back(_studio_url(copy), f"Copied into draft version {copy.version}")


@app.post("/messages/{version_id}/rename")
async def rename_version(version_id: int, note: str = Form(""), db: AsyncSession = Depends(get_db)):
    version = await db.get(MessageVersion, version_id)
    await crud.rename_version(db, version_id, note)
    return back(_studio_url(version), "Note updated")


@app.post("/messages/test")
async def test_dm(request: Request, db: AsyncSession = Depends(get_db)):
    form = dict(await request.form())
    test_user_id = await settings_store.admin_test_user_id(db)
    if not test_user_id:
        return back("/messages", "Set a test DM user ID on the Settings page first")
    kind = form.get("kind", constants.KIND_DM)
    guild_id = _int_or_none(str(form.get("preview_guild_id") or ""))
    await crud.queue_task(
        db,
        constants.TASK_TEST_DM,
        {
            "user_id": test_user_id,
            "kind": kind,
            "guild_id": guild_id,
            "content": _content_from_form(form),
        },
    )
    target = f"/messages?kind={kind}" + (f"&guild_id={guild_id}" if guild_id else "")
    return back(target, "Test message queued. It arrives within a few seconds.")


# --------------------------------------------------------------------------
# Tag experiments, conversions, analytics
# --------------------------------------------------------------------------
@app.get("/tags", response_class=HTMLResponse)
async def tags_page(request: Request, db: AsyncSession = Depends(get_db)):
    experiments = (
        await db.execute(select(TagExperiment).order_by(TagExperiment.started_at.desc()))
    ).scalars().all()
    names = {s.guild_id: s.name for s in await crud.list_servers(db)}
    conversions = (
        await db.execute(select(Conversion).where(Conversion.is_test.is_(False)))
    ).scalars().all()
    per_experiment: dict[int, int] = {}
    for conv in conversions:
        if conv.tag_experiment_id and conv.attribution == constants.ATTR_INVITE:
            per_experiment[conv.tag_experiment_id] = per_experiment.get(conv.tag_experiment_id, 0) + 1
    return templates.TemplateResponse(
        request,
        "tags.html",
        await page_context(
            request, db, experiments=experiments, names=names, per_experiment=per_experiment
        ),
    )


@app.get("/conversions", response_class=HTMLResponse)
async def conversions_page(request: Request, db: AsyncSession = Depends(get_db)):
    conversions = await crud.list_conversions(db, limit=300)
    names = {s.guild_id: s.name for s in await crud.list_servers(db)}
    experiments = {e.id: e for e in await crud.list_experiments(db)}
    versions = {
        v.id: v for v in (await db.execute(select(MessageVersion))).scalars().all()
    }
    return templates.TemplateResponse(
        request,
        "conversions.html",
        await page_context(
            request, db, conversions=conversions, names=names, experiments=experiments, versions=versions
        ),
    )


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request, db: AsyncSession = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "analytics.html",
        await page_context(
            request,
            db,
            summary=await analytics.network_summary(db),
            tags=await analytics.tag_stats(db),
            messages=await analytics.message_stats(db),
            overlap=await analytics.cross_membership(db),
            dm_breakdown=await analytics.dm_breakdown(db),
        ),
    )


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------
@app.get("/health", response_class=HTMLResponse)
async def health_page(request: Request, db: AsyncSession = Depends(get_db)):
    feeders = await crud.list_feeders(db)
    main_id = await settings_store.main_guild_id(db)
    rows = []
    for feeder in feeders:
        destination_rows = await routing.rows(db, feeder)
        invite = next((d["invite"] for d in destination_rows if d["invite"]), None)
        _, published_id = await crud.message_content(db, constants.KIND_PUBLIC, feeder.guild_id)
        rows.append(
            {
                "server": feeder,
                "destinations": destination_rows,
                "health": feeder.health or {},
                "effective": await settings_store.effective(db, feeder),
                "invite": invite,
                "outdated": bool(
                    feeder.funnel_message_id
                    and published_id
                    and feeder.funnel_message_version_id != published_id
                ),
            }
        )
    names = {s.guild_id: s.name for s in await crud.list_servers(db)}
    return templates.TemplateResponse(
        request,
        "health.html",
        await page_context(
            request,
            db,
            rows=rows,
            worker=await crud.worker_status(db),
            names=names,
            tasks=await crud.recent_tasks(db, 20),
            logs=await crud.recent_logs(db, 60),
        ),
    )


@app.post("/tasks/{task_id}/retry")
async def retry_task(task_id: int, db: AsyncSession = Depends(get_db)):
    task = await crud.retry_task(db, task_id)
    if task is None:
        return back("/health", "Only finished jobs can be retried")
    return back("/health", f"{task.task_type.lower().replace('_', ' ')} queued again")


@app.post("/tasks/clear")
async def clear_tasks(db: AsyncSession = Depends(get_db)):
    removed = await crud.clear_finished_tasks(db)
    return back("/health", f"Cleared {removed} finished job(s)")


@app.post("/health/repair-all")
async def repair_all(db: AsyncSession = Depends(get_db)):
    for feeder in await crud.list_feeders(db, only_present=True):
        await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": feeder.guild_id})
    return back("/health", "Repair queued for every feeder")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    servers = await crud.list_servers(db)
    # The dropdown offers the servers the bot is actually in.
    available = [s for s in servers if s.bot_present] or list(servers)
    # Proves the connection rather than assuming it.
    await db.execute(text("SELECT 1"))
    return templates.TemplateResponse(
        request,
        "settings.html",
        await page_context(
            request,
            db,
            servers=available,
            admin_test_user_id=await settings_store.admin_test_user_id(db),
            custom_variables_text="\n".join(
                f"{k} = {v}" for k, v in sorted(
                    (await settings_store.get_all(db)).get("custom_variables", {}).items()
                )
            ),
            owner_count=len(config.owner_user_ids),
            owners_configured=config.owners_configured,
            token_configured=config.token_configured,
            database=database_status(connected=True),
        ),
    )


@app.post("/settings")
async def save_settings(request: Request, db: AsyncSession = Depends(get_db)):
    form = dict(await request.form())
    previous_main = await settings_store.main_guild_id(db)
    previous_dev = await settings_store.development_mode(db)
    previous_test_user = await settings_store.admin_test_user_id(db)

    new_main = _int_or_none(str(form.get("main_guild_id") or ""))
    new_dev = str(form.get("development_mode", "")).lower() in ("on", "true", "1")

    notes: list[str] = []

    # Custom variables: "name = value", one per line.
    raw_variables: dict[str, str] = {}
    for line in str(form.get("custom_variables", "")).splitlines():
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        raw_variables[name.strip()] = value.strip()
    custom_variables, variable_problems = rendering.validate_custom_variables(raw_variables)
    notes.extend(variable_problems)

    # The test user is not a secret, but it has to be a Discord ID.
    raw_test_user = str(form.get("admin_test_user_id", "")).strip()
    test_user_id = previous_test_user
    if raw_test_user == "":
        test_user_id = None
    elif raw_test_user.isdigit():
        test_user_id = int(raw_test_user)
    else:
        notes.append("Test DM user ID must be numbers only, so it was left unchanged")

    values: dict[str, Any] = {
        "network_name": str(form.get("network_name", "")).strip() or "My Network",
        "default_funnel_channel_name": str(form.get("default_funnel_channel_name", "")).strip() or "join-main",
        "default_bump_channel_name": str(form.get("default_bump_channel_name", "")).strip() or "bump",
        "default_dm_delay_seconds": int(str(form.get("default_dm_delay_seconds", "5")) or 5),
        "default_funnel_mode": str(form.get("default_funnel_mode", constants.LIVE)),
        "default_auto_repair": str(form.get("default_auto_repair", "")).lower() in ("on", "true"),
        "default_age_enforcement": str(form.get("default_age_enforcement", "")).lower() in ("on", "true"),
        "dm_policy": str(form.get("dm_policy", constants.ONCE_PER_FEEDER)),
        "dm_cooldown_days": int(str(form.get("dm_cooldown_days", "30")) or 30),
        "failure_retry_days": int(str(form.get("failure_retry_days", "7")) or 7),
        "repair_interval_minutes": int(str(form.get("repair_interval_minutes", "30")) or 30),
        "bump_staff_role_names": [
            r.strip() for r in str(form.get("bump_staff_role_names", "")).split(",") if r.strip()
        ],
        "custom_variables": custom_variables,
        "development_mode": new_dev,
        "admin_test_user_id": test_user_id,
        "setup_complete": True,
    }
    # main_guild_id is written by set_main_server below, which also moves the
    # MAIN marker; writing it here as well would fight with that.
    if new_main == previous_main:
        values["main_guild_id"] = new_main
    await settings_store.set_many(db, values)

    if new_main != previous_main:
        # Conversion rows are never touched: they keep pointing at the server
        # people actually joined. Only future tracking invites change.
        changes = await crud.set_main_server(db, new_main)
        target = await crud.get_server(db, new_main) if new_main else None
        notes.append(
            f"Main server changed to {target.name}" if target else "Main server cleared"
        )
        await crud.log(
            db,
            "main_server_changed",
            f"Main server changed from {previous_main} to {new_main}. "
            + ("; ".join(changes) if changes else "")
            + " Past conversions are unchanged; new tracking invites will be created.",
            source="dashboard",
        )
        for feeder in await crud.list_feeders(db, only_present=True):
            await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": feeder.guild_id})

    if new_dev != previous_dev:
        notes.append("Development mode enabled" if new_dev else "Development mode disabled")
        await crud.log(
            db,
            "development_mode_changed",
            "Development mode is now " + ("on" if new_dev else "off"),
            source="dashboard",
        )

    if test_user_id != previous_test_user and raw_test_user.isdigit():
        notes.append("Test DM user updated")
    elif test_user_id is None and previous_test_user is not None and raw_test_user == "":
        notes.append("Test DM user cleared")

    await crud.log(db, "settings_saved", "Network settings updated", source="dashboard")
    return back("/settings", ". ".join(notes) + "." if notes else "Settings saved")


@app.get("/api/health")
async def api_health(db: AsyncSession = Depends(get_db)):
    """Tiny JSON endpoint so you can check the database from a terminal."""
    summary = await analytics.network_summary(db)
    status = database_status(connected=True)
    return JSONResponse(
        {
            "feeders": summary["feeders"],
            "feeder_joins": summary["feeder_joins"],
            "conversions": summary["conversions"],
            # The live setting, not whatever this process started with.
            "development_mode": await settings_store.development_mode(db),
            "environment": status["environment"],
            "database": status["kind"],
            "production_database": status["is_production"],
        }
    )


@app.get("/api/servers")
async def api_servers(db: AsyncSession = Depends(get_db)):
    servers = (await db.execute(select(Server))).scalars().all()
    return JSONResponse(
        [
            {
                "guild_id": str(s.guild_id),
                "name": s.name,
                "type": s.server_type,
                "bot_present": s.bot_present,
            }
            for s in servers
        ]
    )


def _format_dt(value: Any) -> str:
    if not value:
        return "—"
    try:
        return value.strftime("%d %b %Y, %H:%M")
    except AttributeError:
        return str(value)


templates.env.filters["dt"] = _format_dt
templates.env.filters["tojson_pretty"] = lambda value: json.dumps(value, indent=2, default=str)


# Local dashboard boundary: prevent remote access, DNS rebinding and cross-site writes.
def _is_local_url(value: str | None) -> bool:
    if not value or value == "null":
        return False

    try:
        parsed = urlsplit(value)
    except Exception:
        return False

    return parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


@app.middleware("http")
async def local_dashboard_guard(request: Request, call_next):
    local_hosts = {
        "localhost",
        "127.0.0.1",
        "::1",
        "testserver",
    }

    local_peers = {
        "127.0.0.1",
        "::1",
        "testclient",
    }

    host = request.url.hostname
    peer = request.client.host if request.client else ""

    # The dashboard itself must only be reachable from this computer.
    if host not in local_hosts or peer not in local_peers:
        return JSONResponse(
            {"detail": "This dashboard is local-only. Open it on this computer."},
            status_code=403,
        )

    # Protect state-changing requests from actual non-local websites.
    # Do not reject solely on Sec-Fetch-Site, because browsers may label
    # localhost <-> 127.0.0.1 navigation as cross-site.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")

        if origin and origin != "null" and not _is_local_url(origin):
            return JSONResponse(
                {"detail": "Blocked request from a non-local website."},
                status_code=403,
            )

        if not origin and referer and not _is_local_url(referer):
            return JSONResponse(
                {"detail": "Blocked request from a non-local website."},
                status_code=403,
            )

    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


from fastapi.exceptions import RequestValidationError

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Do not echo invalid request bodies: account forms contain credentials.
    return JSONResponse({"detail": [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                                     for e in exc.errors()]}, status_code=422)


from dashboard.scheduler_routes import install as install_scheduler
install_scheduler(app, templates, page_context, get_db)
