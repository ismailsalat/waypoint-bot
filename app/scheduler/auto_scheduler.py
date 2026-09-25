"""One interruptible worker per account, with persistent per-server timers."""
from __future__ import annotations

import random
import threading
import uuid
from datetime import datetime, timedelta, timezone
from app.accounts.store import AccountStore
from app.adapters.base import AdapterError
from app.adapters.user_token import UserTokenAdapter
from app.adapters.official_bot import OfficialBotAdapter
from app.services.credentials import get_credential
from app.services.logging_service import get_logger
from suite.config import SchedulerSettings

log = get_logger(__name__)


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat() if dt else None


class AccountRunner:
    def __init__(self, account, store, log_cb=None, preferences=None, cooldowns=None, target_status=None):
        self._cooldowns = cooldowns
        self._target_status = target_status or (lambda gid: {})
        self._account = account
        self._store = store
        self._log_cb = log_cb or (lambda level, msg: None)
        self._preferences = preferences or (lambda: SchedulerSettings().model_dump())
        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._thread = None
        self._consec_failures = {}
        self.status = "stopped"

    def start(self, start_delay_s=0):
        if self.is_running():
            return
        self._stop_evt.clear()
        self.status = "waiting"
        self._thread = threading.Thread(target=self._run, args=(start_delay_s,), daemon=True,
                                        name=f"runner-{self._account.account_id}")
        self._thread.start()

    def stop(self):
        self.status = "stopping" if self.is_running() else "stopped"
        self._stop_evt.set()

    def pause(self):
        self._pause_evt.set()
        self.status = "paused"

    def resume(self):
        self._pause_evt.clear()
        if self.is_running():
            self.status = "waiting"

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def _sleep(self, seconds):
        self._stop_evt.wait(max(0.1, min(seconds, 1.0)))

    def _run(self, start_delay_s):
        try:
            if self._stop_evt.wait(start_delay_s):
                return
            while not self._stop_evt.is_set():
                if self._pause_evt.is_set():
                    self._sleep(1)
                    continue
                account = self._store.get(self._account.account_id)
                if not account or not account.enabled:
                    break
                servers = [s for s in account.servers if s.enabled and s.guild_id and (s.channel_id or s.follow_managed_channel)]
                now = _now()
                if account.last_action_at:
                    remaining = account.account_cooldown_min * 60 - (now - account.last_action_at).total_seconds()
                    if remaining > 0:
                        self._sleep(remaining)
                        continue
                due = [s for s in servers if s.next_run_at is None or s.next_run_at <= now]
                if not due:
                    self._sleep(1)
                    continue
                eligible=[]
                for candidate in due:
                    if candidate.follow_managed_channel:
                        known=self._target_status(candidate.guild_id)
                        if not known or not known.get("ready"):
                            continue
                        if known.get("channel_id"):
                            candidate.channel_id=known["channel_id"]
                    eligible.append(candidate)
                if not eligible:
                    self.status="waiting_for_setup"
                    self._sleep(1)
                    continue
                target=min(eligible,key=lambda s:s.next_run_at or datetime.min.replace(tzinfo=timezone.utc))
                if not self._stop_evt.is_set() and not self._pause_evt.is_set():
                    self._execute_server(account, target)
        except Exception:
            self.status = "error"
            self._log("Worker stopped unexpectedly; check the local error log.", "ERROR")
            log.exception("Scheduler worker stopped")
        finally:
            if self.status != "error":
                self.status = "stopped"

    def _execute_server(self, account, server):
        prefs = self._preferences()
        self.status = "running"
        book=self._cooldowns[prefs["dry_run"]] if self._cooldowns else None
        adapter = None
        now = _now()
        # Persist a conservative reservation BEFORE an external request. If the
        # process crashes after send, a restart must not immediately send again.
        cooldown = server.cooldown_min if server.cooldown_min is not None else account.server_cooldown_min
        jitter = server.random_offset_min if server.random_offset_min is not None else account.random_offset_min
        next_time=now + timedelta(minutes=cooldown + random.uniform(0, jitter))
        if book:
            claimed,next_time=book.claim(server.guild_id,next_time)
            if not claimed:
                server.next_run_at=next_time
                server.last_result="simulated_shared_cooldown" if prefs["dry_run"] else "waiting_shared_cooldown"
                self._store.upsert(account)
                self.status="waiting"
                return
        account.last_action_at=now
        server.last_run_at=now
        server.next_run_at=next_time
        server.last_result = "in_progress"
        server.last_error = ""
        self._store.upsert(account)
        try:
            if prefs["dry_run"]:
                server.total_simulated += 1
                server.last_result = "simulated"
                self._log(f"Simulation: {server.name}; no Discord request made")
            else:
                credential = get_credential(account.account_id)
                if not credential:
                    raise AdapterError("MISSING_TOKEN", "Add a credential to this account.", retryable=False)
                adapter = self._get_adapter(account.token_type)
                adapter.connect(credential, server.guild_id, server.channel_id)
                known=self._target_status(server.guild_id) if server.follow_managed_channel else {}
                if known and (not known.get("ready") or known.get("channel_id") != server.channel_id):
                    server.last_result="waiting_for_setup"
                    server.next_run_at=_now()+timedelta(seconds=5)
                    if book:
                        book.defer(server.guild_id,server.next_run_at)
                    return
                if self._stop_evt.is_set() or self._pause_evt.is_set():
                    server.last_result = "cancelled_before_send"
                    return
                operation_id = str(uuid.uuid4())
                result = adapter.execute(server.guild_id, server.channel_id,
                                         server.message or account.message, operation_id)
                if not result.success:
                    raise AdapterError("FAILED", result.message, retryable=result.retryable)
                # Bot mode can verify the exact message. Legacy user transport's
                # response lookup is not correlated reliably: report it as sent.
                verified = False
                if account.token_type == "bot" and result.external_id:
                    check = adapter.verify_result(operation_id, result.external_id)
                    verified = check.success and check.verified
                server.total_runs += 1
                if verified:
                    server.total_ok += 1
                    server.last_result = "confirmed"
                else:
                    server.total_sent += 1
                    server.last_result = "sent_unconfirmed"
                self._consec_failures[server.server_id] = 0
                self._log(f"{server.name}: {server.last_result}")
        except AdapterError as exc:
            failures = self._consec_failures.get(server.server_id, 0) + 1
            self._consec_failures[server.server_id] = failures
            server.total_runs += 1
            server.total_fail += 1
            server.last_result = "failed"
            # Error codes only: third-party response bodies may contain secrets.
            server.last_error = str(exc.code)[:100]
            if not exc.retryable or (not exc.retry_after_ms and failures >= prefs["max_failures"]):
                server.enabled = False
                self._log(f"{server.name}: {exc.code}; target disabled until you re-enable it", "ERROR")
            else:
                wait = (exc.retry_after_ms / 1000 if exc.retry_after_ms else
                        min(prefs["retry_base_seconds"] * 2 ** min(failures - 1, 20), prefs["retry_max_seconds"]))
                wait = max(wait, account.account_cooldown_min * 60, 1)
                server.next_run_at = _now() + timedelta(seconds=wait)
                if book:
                    book.defer(server.guild_id,server.next_run_at)
                self._log(f"{server.name}: {exc.code}; retry scheduled", "WARN")
        except Exception:
            server.total_runs += 1
            server.total_fail += 1
            server.last_result = "failed"
            server.last_error = "Unexpected adapter failure; target disabled"
            server.enabled = False
            self._log(f"{server.name}: unexpected error; target disabled", "ERROR")
        finally:
            if adapter:
                try:
                    adapter.disconnect()
                except Exception:
                    pass
            self._store.upsert(account)
            if not self._stop_evt.is_set():
                self.status = "paused" if self._pause_evt.is_set() else "waiting"

    def _get_adapter(self, token_type):
        return UserTokenAdapter() if token_type == "user" else OfficialBotAdapter()

    def _log(self, message, level="INFO"):
        log.info("[%s] %s", self._account.name, message)
        self._log_cb(level, f"[{self._account.name}] {message}")


class AutoScheduler:
    def __init__(self, store: AccountStore, log_cb=None, start_offset_min=5, preferences=None, cooldowns=None, target_status=None):
        self._store = store
        self._cooldowns = cooldowns
        self._target_status = target_status
        self._log_cb = log_cb
        self._start_offset_s = start_offset_min * 60
        self._preferences = preferences
        self._runners = {}
        self._lock = threading.RLock()

    def start(self, auto_only=True):
        delay = 0
        offset = self._preferences()["start_offset_min"] * 60 if self._preferences else self._start_offset_s
        for account in self._store.list():
            if account.enabled and (not auto_only or account.auto_start):
                self._start_account(account, delay)
                delay += offset

    def _start_account(self, account, start_delay_s=0):
        if not account.enabled:
            return
        with self._lock:
            existing = self._runners.get(account.account_id)
            if existing and existing.is_running():
                return
            runner = AccountRunner(account, self._store, self._log_cb, self._preferences, self._cooldowns, self._target_status)
            self._runners[account.account_id] = runner
            runner.start(start_delay_s)

    def start_account(self, account_id):
        account = self._store.get(account_id)
        if account:
            self._start_account(account)

    def stop_account(self, account_id):
        with self._lock:
            runner = self._runners.get(account_id)
            if runner:
                runner.stop()

    def wait_stopped(self, account_id, timeout=0):
        with self._lock:
            runner = self._runners.get(account_id)
        if runner and runner._thread:
            runner._thread.join(timeout)
            return not runner.is_running()
        return True

    def pause_account(self, account_id):
        with self._lock:
            if account_id in self._runners:
                self._runners[account_id].pause()

    def resume_account(self, account_id):
        with self._lock:
            if account_id in self._runners:
                self._runners[account_id].resume()

    def stop_all(self):
        with self._lock:
            for runner in self._runners.values():
                runner.stop()

    def remove_account(self, account_id):
        self.stop_account(account_id)
        if not self.wait_stopped(account_id, 0):
            raise ValueError("Account is stopping; wait for its current request to finish.")
        with self._lock:
            self._runners.pop(account_id, None)

    def status(self):
        with self._lock:
            runners = dict(self._runners)
        result = []
        for account in self._store.list():
            runner = runners.get(account.account_id)
            item = account.to_dict()
            item.update(status=runner.status if runner else "stopped",
                        is_running=runner.is_running() if runner else False)
            for server in item["servers"]:
                server["consec_fail"] = runner._consec_failures.get(server["server_id"], 0) if runner else 0
            result.append(item)
        return result

    def health(self):
        statuses = self.status()
        return {"active": sum(s["is_running"] for s in statuses), "total_runners": len(self._runners)}
