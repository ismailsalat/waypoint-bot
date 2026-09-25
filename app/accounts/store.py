"""Thread-safe atomic account persistence shared by dashboard and runners."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from threading import RLock
from app.accounts.models import Account, ServerTarget
from app.config.settings import DATA_DIR
from suite.config import atomic_json

ACCOUNTS_FILE = DATA_DIR / "accounts.json"


class AccountStore:
    def __init__(self, path: Path = ACCOUNTS_FILE):
        self._path = path
        self._lock = RLock()
        self._accounts = []
        if path.exists():
            # Do not silently erase malformed existing data.
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("accounts.json must contain a list of accounts")
            self._accounts = [Account.from_dict(d) for d in data]

    def save(self):
        with self._lock:
            atomic_json(self._path, [a.to_dict() for a in self._accounts])

    def list(self):
        with self._lock:
            return deepcopy(self._accounts)

    def get(self, account_id):
        with self._lock:
            return deepcopy(next((a for a in self._accounts if a.account_id == account_id), None))

    def _commit(self, accounts):
        atomic_json(self._path, [a.to_dict() for a in accounts])
        self._accounts = accounts

    def upsert(self, account: Account):
        with self._lock:
            accounts = deepcopy(self._accounts)
            idx = next((i for i,a in enumerate(accounts) if a.account_id == account.account_id), None)
            if idx is None:
                accounts.append(deepcopy(account))
            else:
                accounts[idx] = deepcopy(account)
            self._commit(accounts)

    def delete(self, account_id):
        with self._lock:
            self._commit([a for a in self._accounts if a.account_id != account_id])

    def update_server(self, account_id, server: ServerTarget):
        with self._lock:
            account = self.get(account_id)
            if account is None:
                return
            idx = next((i for i,s in enumerate(account.servers) if s.server_id == server.server_id), None)
            if idx is None:
                account.servers.append(deepcopy(server))
            else:
                account.servers[idx] = deepcopy(server)
            self.upsert(account)

    def delete_server(self, account_id, server_id):
        with self._lock:
            account = self.get(account_id)
            if account:
                account.servers = [s for s in account.servers if s.server_id != server_id]
                self.upsert(account)

    def replace_all(self, accounts):
        with self._lock:
            self._commit(deepcopy(accounts))
