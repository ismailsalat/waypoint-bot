"""Test fixtures.

Discord is mocked here and nowhere else: the bot itself always uses real
discord.py calls. The database is a throwaway in-memory SQLite file.
"""
from __future__ import annotations

import itertools
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from core import constants, settings as settings_store
from core.config import config
from database import crud
from database.database import set_engine
from database.models import Base

_ids = itertools.count(1000)


def next_id() -> int:
    return next(_ids)


# --------------------------------------------------------------------------
# Fake Discord objects
# --------------------------------------------------------------------------
class FakeUser:
    def __init__(self, user_id: int | None = None, name: str = "tester", bot: bool = False):
        self.id = user_id or next_id()
        self.name = name
        self.display_name = name.title()
        self.bot = bot
        self.sent: list[dict] = []
        self.dms_closed = False

    async def send(self, **kwargs):
        if self.dms_closed:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "DMs closed")
        self.sent.append(kwargs)
        return SimpleNamespace(id=next_id())

    def __str__(self) -> str:
        return self.name


class FakeMessage:
    def __init__(self, channel, payload):
        self.id = next_id()
        self.channel = channel
        self.payload = payload

    async def edit(self, **kwargs):
        self.payload = kwargs


class FakeInvite:
    def __init__(self, code: str, uses: int = 0):
        self.code = code
        self.url = f"https://discord.gg/{code}"
        self.uses = uses
        self.deleted = False

    async def delete(self, reason: str = ""):
        self.deleted = True


class FakeChannel:
    def __init__(self, guild, name: str, deletable: bool = True):
        self.id = next_id()
        self.guild = guild
        self.name = name
        self.deletable = deletable
        self.deleted = False
        self.overwrites: dict = {}
        self.messages: dict[int, FakeMessage] = {}
        self.can_invite = True

    def permissions_for(self, _member):
        return SimpleNamespace(create_instant_invite=self.can_invite)

    async def create_invite(self, **kwargs):
        invite = FakeInvite(f"code{next_id()}")
        self.guild.invite_objects.append(invite)
        return invite

    async def send(self, **kwargs):
        message = FakeMessage(self, kwargs)
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id: int):
        if message_id not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        return self.messages[message_id]

    async def delete(self, reason: str = ""):
        if not self.deletable:
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "cannot delete"
            )
        self.deleted = True
        self.guild._channels = [c for c in self.guild._channels if c is not self]

    async def edit(self, **kwargs):
        if "name" in kwargs:
            self.name = kwargs["name"]
        if "overwrites" in kwargs:
            self.overwrites = kwargs["overwrites"]

    def overwrites_for(self, target):
        return self.overwrites.get(target, discord.PermissionOverwrite())

    async def set_permissions(self, target, overwrite=None, reason: str = ""):
        """Per-target update, like the real thing: other targets are untouched."""
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = overwrite


class FakeRole:
    def __init__(self, name: str, role_id: int | None = None):
        self.id = role_id or next_id()
        self.name = name


class FakeGuild:
    def __init__(self, name: str, owner_id: int, guild_id: int | None = None):
        self.id = guild_id or next_id()
        self.name = name
        self.owner_id = owner_id
        self.default_role = FakeRole("@everyone")
        self.me = FakeUser(name="funnelbot", bot=True)
        # Everything the bot needs, so tests opt in to missing permissions.
        # Everything a correctly invited bot has. Discord requires Manage
        # Roles to edit channel permission overwrites, which is what the
        # funnel and bump channels are built from.
        self.me.guild_permissions = SimpleNamespace(
            manage_guild=True,
            manage_channels=True,
            manage_roles=True,
            create_instant_invite=True,
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
        )
        self.roles = [self.default_role, FakeRole("Staff")]
        self._channels: list[FakeChannel] = []
        self.invite_objects: list[FakeInvite] = []
        self.rules_channel = None
        self.system_channel = None
        self.members: dict[int, FakeUser] = {}
        self.kicked: list[int] = []

    # discord.Guild surface used by the bot
    @property
    def text_channels(self):
        return list(self._channels)

    @property
    def channels(self):
        return list(self._channels)

    def add_channel(self, name: str, deletable: bool = True) -> "FakeChannel":
        channel = FakeChannel(self, name, deletable=deletable)
        self._channels.append(channel)
        return channel

    def get_channel(self, channel_id: int):
        for channel in self._channels:
            if channel.id == channel_id:
                return channel
        return None

    def get_role(self, role_id: int):
        for role in self.roles:
            if role.id == role_id:
                return role
        return None

    async def create_text_channel(self, name: str, overwrites=None, reason: str = ""):
        channel = FakeChannel(self, name)
        channel.overwrites = overwrites or {}
        self._channels.append(channel)
        return channel

    async def invites(self):
        return [inv for inv in self.invite_objects if not inv.deleted]

    def get_member(self, user_id: int):
        return self.members.get(user_id)

    async def fetch_member(self, user_id: int):
        member = self.members.get(user_id)
        if member is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")
        return member

    def delete_channel(self, name: str) -> None:
        """Simulate someone deleting a channel."""
        self._channels = [c for c in self._channels if c.name != name]


class FakeBot:
    def __init__(self, *guilds: FakeGuild):
        self.guilds = list(guilds)
        self.users: dict[int, FakeUser] = {}

    def get_guild(self, guild_id: int):
        for guild in self.guilds:
            if guild.id == guild_id:
                return guild
        return None

    def get_user(self, user_id: int):
        return self.users.get(user_id)

    async def fetch_user(self, user_id: int):
        return self.users.get(user_id)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def neutral_environment(monkeypatch):
    """Run every test against a blank environment.

    The bootstrap values in a real .env (a main guild, a test user,
    DEVELOPMENT_MODE=true) would otherwise change what the suite sees on one
    machine and not another. Tests that care about them set them explicitly.
    """
    monkeypatch.setattr(config, "main_guild_id", None)
    monkeypatch.setattr(config, "development_mode", False)
    monkeypatch.setattr(config, "admin_test_user_id", None)
    settings_store.invalidate_cache()
    yield
    settings_store.invalidate_cache()


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    set_engine(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db(engine):
    from database.database import session

    async with session() as db:
        yield db


@pytest_asyncio.fixture
async def network(db):
    """A main server, one feeder, and a bot that is in both."""
    owner_id = next_id()
    main_guild = FakeGuild("Side Quest", owner_id)
    main_channel = FakeChannel(main_guild, "general")
    main_guild._channels.append(main_channel)
    feeder_guild = FakeGuild("Rewind", owner_id)
    bot = FakeBot(main_guild, feeder_guild)

    await crud.upsert_server(db, main_guild.id, main_guild.name, owner_id, constants.MAIN)
    await crud.upsert_server(db, feeder_guild.id, feeder_guild.name, owner_id, constants.FEEDER)
    await settings_store.set_many(
        db,
        {
            "main_guild_id": main_guild.id,
            "network_name": "Side Quest Network",
            "default_funnel_channel_name": "join-side-quest",
            "default_bump_channel_name": "bump",
            "default_dm_delay_seconds": 0,
            "setup_complete": True,
        },
    )
    await crud.ensure_default_messages(db)
    return SimpleNamespace(
        bot=bot, main=main_guild, feeder=feeder_guild, owner_id=owner_id, db=db
    )
