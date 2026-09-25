"""Scheduler API and pages; registered by the existing dashboard."""
from __future__ import annotations

import re
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from app.accounts.models import Account, ServerTarget
from app.services import credentials
from core import settings as network_settings
from database import crud
from suite.config import SchedulerSettings


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class AccountInput(Input):
    name: str = Field(min_length=1, max_length=100)
    token_type: Literal["bot", "user"] = "user"
    enabled: bool = True
    auto_start: bool = False
    server_cooldown_min: float = Field(120, gt=0, le=525600)
    account_cooldown_min: float = Field(30, ge=0, le=525600)
    random_offset_min: float = Field(0, ge=0, le=1440)
    message: str = Field("Time to bump this server.", min_length=1, max_length=1800)
    token: SecretStr | None = None
    clear_token: bool = False


class ServerInput(Input):
    name: str = Field(min_length=1, max_length=100)
    guild_id: str = Field(pattern=r"^[0-9]{1,20}$")
    channel_id: str = Field("", pattern=r"^[0-9]{0,20}$")
    follow_managed_channel: bool = False
    enabled: bool = True
    cooldown_min: float | None = Field(None, gt=0, le=525600)
    random_offset_min: float | None = Field(None, ge=0, le=1440)
    message: str = Field("", max_length=1800)


    @model_validator(mode="after")
    def require_channel(self):
        if not self.channel_id and not self.follow_managed_channel:
            raise ValueError("Enter a channel ID or enable managed channel tracking.")
        return self


class AppearanceInput(Input):
    dashboard_title: str = Field("Waypoint Suite", min_length=1, max_length=60)
    dashboard_accent: str = Field("#6f5bf0", pattern=r"^#[0-9a-fA-F]{6}$")
    dashboard_theme: Literal["dark", "light"] = "dark"
    dashboard_compact: bool = False


class ImportInput(Input):
    accounts: list[dict] = Field(max_length=200)


def install(app, templates, page_context, get_db):
    router = APIRouter()

    def runtime(request: Request):
        value = getattr(request.app.state, "scheduler", None)
        if value is None:
            raise HTTPException(503, "Scheduler is not running in this dashboard process.")
        return value

    def account_or_404(rt, account_id):
        account = rt.store.get(account_id)
        if account is None:
            raise HTTPException(404, "Account not found")
        return account

    def idle(rt, account_id):
        try:
            rt.require_idle(account_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    def save_account(rt, account_id, data):
        with rt.lock:
            if account_id:
                idle(rt, account_id)
            account = account_or_404(rt, account_id) if account_id else Account()
            if not account_id and len(rt.store.list()) >= 200:
                raise HTTPException(400, "Maximum 200 accounts")
            for key, value in data.model_dump(exclude={"token", "clear_token"}).items():
                setattr(account, key, value)
            rt.store.upsert(account)
            if data.clear_token:
                credentials.delete_credential(account.account_id)
            elif data.token and data.token.get_secret_value().strip():
                credentials.store_credential(account.account_id, data.token.get_secret_value().strip())
            return {"account_id": account.account_id}

    @router.get("/scheduler")
    async def scheduler_page(request: Request, db=Depends(get_db)):
        known = await crud.list_servers(db)
        servers = [{"name": s.name, "guild_id": str(s.guild_id),
                    "channel_id": str(s.bump_channel_id or ""), "type": s.server_type}
                   for s in known if s.bot_present]
        return templates.TemplateResponse(request, "scheduler.html",
                   await page_context(request, db, known_servers=servers))

    @router.get("/customize")
    async def customize_page(request: Request, db=Depends(get_db)):
        return templates.TemplateResponse(request, "customize.html", await page_context(request, db))

    @router.post("/api/suite/appearance")
    async def save_appearance(data: AppearanceInput, db=Depends(get_db)):
        await network_settings.set_many(db, data.model_dump())
        return {"saved": True}

    @router.get("/api/scheduler")
    def status(rt=Depends(runtime)):
        return rt.snapshot()

    @router.put("/api/scheduler/settings")
    def save_settings(data: SchedulerSettings, rt=Depends(runtime)):
        try:
            rt.save_settings(data)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"saved": True}

    @router.post("/api/scheduler/accounts")
    def create_account(data: AccountInput, rt=Depends(runtime)):
        return save_account(rt, None, data)

    @router.put("/api/scheduler/accounts/{account_id}")
    def edit_account(account_id: str, data: AccountInput, rt=Depends(runtime)):
        return save_account(rt, account_id, data)

    @router.delete("/api/scheduler/accounts/{account_id}")
    def delete_account(account_id: str, rt=Depends(runtime)):
        with rt.lock:
            account_or_404(rt, account_id)
            idle(rt, account_id)
            rt.scheduler.remove_account(account_id)
            rt.store.delete(account_id)
            credentials.delete_credential(account_id)
        return {"deleted": True}

    @router.post("/api/scheduler/accounts/{account_id}/actions/{action}")
    def account_action(account_id: str, action: str, rt=Depends(runtime)):
        try:
            rt.action(account_id, action)
        except KeyError:
            raise HTTPException(404, "Account not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"accepted": True}

    @router.post("/api/scheduler/control/{action}")
    def all_action(action: Literal["start-all", "stop-all"], rt=Depends(runtime)):
        with rt.lock:
            if action == "stop-all":
                rt.scheduler.stop_all()
            else:
                if not rt.preferences.get()["enabled"]:
                    raise HTTPException(409, "Enable the scheduler in Scheduler settings first.")
                rt.scheduler.start(auto_only=False)
        return {"accepted": True}

    def save_server(rt, account_id, server_id, data):
        with rt.lock:
            idle(rt, account_id)
            account = account_or_404(rt, account_id)
            server = next((s for s in account.servers if s.server_id == server_id), None) if server_id else ServerTarget()
            if server is None:
                raise HTTPException(404, "Server target not found")
            if not server_id and len(account.servers) >= 200:
                raise HTTPException(400, "Maximum 200 targets per account")
            # Moving to another channel/guild preserves timing/counters but changes only configuration.
            for key, value in data.model_dump().items():
                setattr(server, key, value)
            if not server_id:
                account.servers.append(server)
            try:
                rt.validate_targets(account)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            rt.store.upsert(account)
            return {"server_id": server.server_id}

    @router.post("/api/scheduler/accounts/{account_id}/targets")
    def add_target(account_id: str, data: ServerInput, rt=Depends(runtime)):
        return save_server(rt, account_id, None, data)

    @router.put("/api/scheduler/accounts/{account_id}/targets/{server_id}")
    def edit_target(account_id: str, server_id: str, data: ServerInput, rt=Depends(runtime)):
        return save_server(rt, account_id, server_id, data)

    @router.delete("/api/scheduler/accounts/{account_id}/targets/{server_id}")
    def delete_target(account_id: str, server_id: str, rt=Depends(runtime)):
        with rt.lock:
            idle(rt, account_id)
            account = account_or_404(rt, account_id)
            if not any(s.server_id == server_id for s in account.servers):
                raise HTTPException(404, "Server target not found")
            rt.store.delete_server(account_id, server_id)
        return {"deleted": True}

    @router.get("/api/scheduler/export")
    def export(rt=Depends(runtime)):
        # Configuration only: never export tokens, browser paths or activity logs.
        accounts = []
        for account in rt.store.list():
            values = {k: v for k, v in account.to_dict().items() if k in AccountInput.model_fields}
            values["account_id"] = account.account_id
            values["servers"] = [{k: v for k, v in s.to_dict().items() if k in ServerInput.model_fields}
                                 for s in account.servers]
            accounts.append(values)
        return JSONResponse({"format": "waypoint-suite-scheduler-v1", "accounts": accounts},
                            headers={"Content-Disposition": 'attachment; filename="scheduler-accounts.json"'})

    @router.post("/api/scheduler/import/accounts")
    def import_accounts(data: ImportInput, rt=Depends(runtime)):
        with rt.lock:
            try:
                rt.require_all_idle()
                existing = rt.store.list()
                ids = {a.account_id for a in existing}
                added = []
                if len(existing) + len(data.accounts) > 200:
                    raise ValueError("Maximum 200 accounts")
                for raw in data.accounts:
                    # Legacy accounts.json is supported. Explicitly discard any
                    # secret, browser association and autostart setting.
                    clean = {k: v for k, v in raw.items() if k in AccountInput.model_fields and k not in ("token", "clear_token")}
                    clean["auto_start"] = False
                    clean["enabled"] = False
                    fields = AccountInput.model_validate(clean)
                    account = Account()
                    for key, value in fields.model_dump(exclude={"token", "clear_token"}).items():
                        setattr(account, key, value)
                    candidate = raw.get("account_id", account.account_id)
                    if not isinstance(candidate, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", candidate):
                        raise ValueError("Invalid account ID")
                    if candidate in ids:
                        raise ValueError("An imported account already exists. No accounts were imported.")
                    account.account_id = candidate
                    ids.add(candidate)
                    targets = set()
                    server_rows = raw.get("servers", [])
                    if not isinstance(server_rows, list) or len(server_rows) > 200:
                        raise ValueError("Invalid server list")
                    for server_raw in server_rows:
                        fields = ServerInput.model_validate({k: v for k, v in server_raw.items() if k in ServerInput.model_fields})
                        server = ServerTarget(**fields.model_dump())
                        if server.guild_id in targets:
                            raise ValueError("A server is assigned more than once. No accounts were imported.")
                        targets.add(server.guild_id)
                        account.servers.append(server)
                    added.append(account)
                rt.store.replace_all(existing + added)
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(400, "Import rejected: check names, numeric IDs, timing, duplicate accounts/servers, and stop all accounts first. Nothing was imported.") from None
        return {"imported": len(added), "note": "Imported accounts are disabled with auto-start off. Review and enable each account."}

    app.include_router(router)
