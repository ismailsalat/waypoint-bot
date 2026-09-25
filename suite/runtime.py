"""Own the scheduler for the lifetime of ONE local dashboard process."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from app.accounts.store import AccountStore
from app.scheduler.auto_scheduler import AutoScheduler
from app.services import credentials
from suite.config import Preferences, SchedulerSettings
from suite.cooldowns import CooldownBook


class Runtime:
    def __init__(self, directory: Path):
        self.directory = directory
        self.preferences = Preferences(directory / "scheduler-settings.json")
        self.store = AccountStore(directory / "accounts.json")
        self.cooldowns = {False:CooldownBook(directory / "server-cooldowns.json"), True:CooldownBook(directory / "simulation-cooldowns.json")}
        self.catalog = {}
        self.events = deque(maxlen=300)
        self.lock = RLock()
        self.scheduler = AutoScheduler(self.store, log_cb=self.log, preferences=self.preferences.get, cooldowns=self.cooldowns, target_status=self.target_status)
        self._lock_file = None

    def startup(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = (self.directory / "scheduler.lock").open("a+b")
        try:
            if __import__("os").name == "nt":
                import msvcrt
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("Another dashboard is using this scheduler data. Close it first.") from None
        self._lock_file = handle
        # Upgrade existing per-account reservations into the shared server clock.
        now=datetime.now(timezone.utc)
        for account in self.store.list():
            for target in account.servers:
                if target.last_run_at and target.next_run_at and target.next_run_at>now:
                    simulated=target.last_result=="simulated"
                    self.cooldowns[simulated].ensure_later(target.guild_id,target.next_run_at)
        if self.preferences.get()["enabled"]:
            self.scheduler.start(auto_only=True)

    def shutdown(self):
        self.scheduler.stop_all()
        # A request already in flight cannot be unsent. Keep the process lock
        # until every worker exits so a second dashboard cannot race it.
        for account in self.store.list():
            self.scheduler.wait_stopped(account.account_id, timeout=None)
        if self._lock_file:
            self._lock_file.close()
            self._lock_file = None

    def log(self, level, message):
        with self.lock:
            self.events.append({"time": datetime.now(timezone.utc).isoformat(), "level": level, "message": message})

    def require_idle(self, account_id):
        if not self.scheduler.wait_stopped(account_id, 0):
            raise ValueError("Stop this account and wait for its current request to finish before editing.")

    def require_all_idle(self):
        for account in self.store.list():
            self.require_idle(account.account_id)

    def snapshot(self):
        with self.lock:
            accounts = self.scheduler.status()
            for account in accounts:
                for field in ("browser_name", "browser_path", "browser_profile"):
                    account.pop(field, None)
                account["has_credential"] = credentials.has_credential(account["account_id"])
                account["credential_storage"] = credentials.credential_status(account["account_id"])
                for target in account["servers"]:
                    known=self.catalog.get(target["guild_id"],{}) if target.get("follow_managed_channel") else {}
                    target["effective_channel_id"]=known.get("channel_id") or target["channel_id"]
                    target["setup_waiting"]=bool(target.get("follow_managed_channel") and not known.get("ready"))
            return {"accounts": accounts, "settings": self.preferences.get(), "events": list(self.events)}

    def target_status(self, guild_id):
        with self.lock:
            return dict(self.catalog.get(str(guild_id), {}))

    def update_catalog(self, catalog):
        with self.lock:
            self.catalog = dict(catalog)

    def validate_targets(self, account):
        ids=[s.guild_id for s in account.servers]
        if len(ids)!=len(set(ids)):
            raise ValueError("This server is already assigned to this account.")

    def save_settings(self, settings: SchedulerSettings):
        with self.lock:
            self.require_all_idle()
            previous = self.preferences.get()
            self.preferences.save(settings)
            if previous["dry_run"] and not settings.dry_run:
                self.cooldowns[True].clear()
                for account in self.store.list():
                    for server in account.servers:
                        if server.last_result in ("simulated", "simulated_shared_cooldown"):
                            server.next_run_at = None
                    if account.servers and all(s.last_result in ("", "simulated", "simulated_shared_cooldown") for s in account.servers):
                        account.last_action_at = None
                    self.store.upsert(account)
            self.log("INFO", "Scheduler settings saved; accounts remain stopped until started.")

    def action(self, account_id, action):
        with self.lock:
            if not self.store.get(account_id):
                raise KeyError("Account not found")
            if action in ("start", "resume") and not self.preferences.get()["enabled"]:
                raise ValueError("Enable the scheduler in Scheduler settings first.")
            if action not in ("start", "stop", "pause", "resume"):
                raise ValueError("Unknown action")
            getattr(self.scheduler, f"{action}_account")(account_id)
