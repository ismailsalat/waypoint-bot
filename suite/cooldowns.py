"""Shared server reservations prevent overlapping accounts from double-bumping."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from threading import RLock
from suite.config import atomic_json


class CooldownBook:
    def __init__(self,path):
        self.path=path
        self.lock=RLock()
        self.values=json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(self.values,dict):
            raise ValueError('Server cooldown file must contain an object')

    def claim(self,guild_id,until):
        with self.lock:
            raw=self.values.get(str(guild_id))
            previous=datetime.fromisoformat(raw) if raw else None
            if previous and previous>datetime.now(timezone.utc):
                return False,previous
            value=dict(self.values)
            value[str(guild_id)]=until.isoformat()
            atomic_json(self.path,value)
            self.values=value
            return True,until

    def defer(self,guild_id,until):
        with self.lock:
            value=dict(self.values)
            value[str(guild_id)]=until.isoformat()
            atomic_json(self.path,value)
            self.values=value

    def ensure_later(self,guild_id,until):
        with self.lock:
            raw=self.values.get(str(guild_id))
            if not raw or datetime.fromisoformat(raw)<until:
                self.defer(guild_id,until)

    def clear(self):
        with self.lock:
            atomic_json(self.path,{})
            self.values={}
