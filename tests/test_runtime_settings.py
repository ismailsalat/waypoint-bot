"""Settings live in the database; .env only seeds a fresh one.

These cover the resolution order for the three bootstrap settings, that a
dashboard change reaches the bot without a restart, that the historical
test/production rule survives the move, and that no secret is ever rendered.
"""
from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from urllib.parse import unquote

from bot import feeder_setup, funnel_dm, invite_tracker, main as bot_main
from core import constants, settings as settings_store
from core.config import config
from database import analytics, crud
from database.database import session
from tests.conftest import FakeBot, FakeGuild, FakeUser, next_id


@pytest_asyncio.fixture
async def client(network):
    from dashboard.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


def _settings_form(**overrides):
    """The settings form as the browser sends it. Unchecked boxes are absent."""
    form = {
        "network_name": "Side Quest Network",
        "main_guild_id": "",
        "admin_test_user_id": "",
        "default_funnel_channel_name": "join-side-quest",
        "default_bump_channel_name": "bump",
        "default_dm_delay_seconds": "0",
        "default_funnel_mode": constants.LIVE,
        "dm_policy": constants.ONCE_PER_FEEDER,
        "dm_cooldown_days": "30",
        "failure_retry_days": "7",
        "repair_interval_minutes": "30",
        "bump_staff_role_names": "Staff",
    }
    form.update(overrides)
    return {k: v for k, v in form.items() if v is not None}


# ==========================================================================
# 1-3. The main server: database first, .env only as a bootstrap
# ==========================================================================
async def test_the_bot_starts_with_no_main_guild_id(engine, monkeypatch):
    """Nothing configured anywhere. Nothing may crash."""
    monkeypatch.setattr(config, "main_guild_id", None)

    await bot_main.bootstrap_defaults()

    async with session() as db:
        assert await settings_store.main_guild_id(db) is None
        # A published message still exists, so the bot is usable.
        content, _ = await crud.message_content(db, constants.KIND_DM)
        assert content["body"]

    # And the bot object itself builds without any main server.
    bot = bot_main.FunnelBot()
    assert bot.intents.members is True
    await bot.close()


async def test_a_feeder_still_gets_its_channels_with_no_main_server(network, monkeypatch):
    """Channels are safe to create; anything needing a destination says so."""
    monkeypatch.setattr(config, "main_guild_id", None)
    await settings_store.set_many(network.db, {"main_guild_id": None})

    report = await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)

    assert {c.name for c in network.feeder.text_channels} == {"join-side-quest", "bump"}
    assert report["funnel_channel"] == "OK"
    assert report["tracking_invite"] == "main server not configured"
    assert any("Main server not configured" in error for error in report["errors"])
    assert network.main.invite_objects == []  # no invite created anywhere

    user = FakeUser(name="early-bird")
    assert await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id) == "NO_MAIN_SERVER"
    assert user.sent == []


async def test_a_fresh_database_takes_the_main_server_from_the_environment(engine, monkeypatch):
    guild_id = 123456789012345678
    monkeypatch.setattr(config, "main_guild_id", guild_id)

    async with session() as db:
        seeded = await settings_store.bootstrap_from_env(db)
        assert any("MAIN_GUILD_ID" in entry for entry in seeded)
        assert await settings_store.main_guild_id(db) == guild_id


async def test_the_stored_main_server_beats_the_environment(network, monkeypatch):
    """A stale MAIN_GUILD_ID must never override a dashboard choice."""
    monkeypatch.setattr(config, "main_guild_id", 999000111222333444)

    async with session() as db:
        assert await settings_store.main_guild_id(db) == network.main.id
        seeded = await settings_store.bootstrap_from_env(db)
        assert not any("MAIN_GUILD_ID" in entry for entry in seeded)
        assert await settings_store.main_guild_id(db) == network.main.id


async def test_choosing_a_main_server_on_the_dashboard_applies_at_once(client, network):
    """No restart: the next funnel DM uses the new destination."""
    new_main = FakeGuild("Second Home", network.owner_id)
    network.bot.guilds.append(new_main)
    channel = type(network.main._channels[0])(new_main, "general")
    new_main._channels.append(channel)
    await crud.upsert_server(
        network.db, new_main.id, new_main.name, network.owner_id, constants.DISABLED
    )

    response = await client.post("/settings", data=_settings_form(main_guild_id=str(new_main.id)))
    assert response.status_code == 303
    assert "Main server changed to Second Home" in unquote(response.headers["location"])

    async with session() as db:
        assert await settings_store.main_guild_id(db) == new_main.id

    # A feeder join now produces an invite to the new main server.
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    invite = await crud.active_invite(network.db, network.feeder.id, new_main.id)
    assert invite is not None
    user = FakeUser(name="alex")
    assert await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id) == constants.DM_SENT
    assert user.sent[0]["view"].children[0].url == invite.url

    # And the old main server was demoted, with its history intact.
    old = await crud.get_server(network.db, network.main.id)
    await network.db.refresh(old)
    assert old.server_type != constants.MAIN


# ==========================================================================
# 5-7. Development mode
# ==========================================================================
async def test_the_environment_seeds_development_mode_on_a_fresh_database(engine, monkeypatch):
    monkeypatch.setattr(config, "development_mode", True)

    async with session() as db:
        assert await settings_store.development_mode(db) is True  # env default
        seeded = await settings_store.bootstrap_from_env(db)
        assert any("DEVELOPMENT_MODE" in entry for entry in seeded)
        assert await settings_store.is_stored(db, "development_mode") is True

        # Once stored, turning the env value off changes nothing.
        monkeypatch.setattr(config, "development_mode", False)
        assert await settings_store.development_mode(db) is True


async def test_turning_development_mode_on_from_the_dashboard_needs_no_restart(client, network, monkeypatch):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    monkeypatch.setattr(config, "development_mode", False)  # the process started with it off

    response = await client.post(
        "/settings", data=_settings_form(development_mode="on", main_guild_id=str(network.main.id))
    )
    assert "Development mode enabled" in unquote(response.headers["location"])

    # The very next join is treated as a test, with no reload of anything.
    user = FakeUser(name="stranger")
    status = await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)
    assert status == constants.DM_SKIPPED_DEV
    assert user.sent == []

    join = await crud.record_join(
        network.db, network.feeder.id, user.id, str(user), constants.FEEDER
    )
    assert join.is_test is True


async def test_turning_development_mode_off_from_the_dashboard_needs_no_restart(client, network, monkeypatch):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    monkeypatch.setattr(config, "development_mode", True)  # the process started with it on
    await settings_store.set_many(network.db, {"development_mode": True})

    response = await client.post(
        "/settings", data=_settings_form(main_guild_id=str(network.main.id))
    )
    assert "Development mode disabled" in unquote(response.headers["location"])

    user = FakeUser(name="real-member")
    assert await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id) == constants.DM_SENT
    join = await crud.record_join(
        network.db, network.feeder.id, 777123, "real", constants.FEEDER
    )
    assert join.is_test is False


async def test_history_still_decides_test_status_when_the_toggle_moves(client, network):
    """The rule we fixed earlier survives the move to a dashboard toggle.

    Test funnel first, then production mode on, and the conversion stays a
    test. The reverse case follows in the same test with a second person.
    """
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    main_id = str(network.main.id)

    # --- development on: a test join and a test DM ---
    await client.post("/settings", data=_settings_form(development_mode="on", main_guild_id=main_id))
    async with session() as db:
        await settings_store.set_many(db, {"admin_test_user_id": 995001})
    tester = FakeUser(user_id=995001, name="me")
    await crud.record_join(network.db, network.feeder.id, tester.id, str(tester), constants.FEEDER)
    assert await funnel_dm.handle_feeder_join(network.bot, tester, network.feeder.id) == constants.DM_SENT

    # --- development off, and only now does the tester join the main server ---
    await client.post("/settings", data=_settings_form(main_guild_id=main_id))
    real = FakeUser(name="real")
    await crud.record_join(network.db, network.feeder.id, real.id, str(real), constants.FEEDER)
    assert await funnel_dm.handle_feeder_join(network.bot, real, network.feeder.id) == constants.DM_SENT

    test_conversion = await crud.record_conversion(
        network.db, tester.id, "me", network.main.id, network.feeder.id, "AAA", constants.ATTR_INVITE
    )
    assert test_conversion.is_test is True  # its DM was a test

    # --- development on again, and only now does the real member join ---
    await client.post("/settings", data=_settings_form(development_mode="on", main_guild_id=main_id))
    production_conversion = await crud.record_conversion(
        network.db, real.id, "real", network.main.id, network.feeder.id, "AAA", constants.ATTR_INVITE
    )
    assert production_conversion.is_test is False  # its DM was production

    summary = await analytics.network_summary(network.db)
    assert summary["conversions"] == 1
    assert summary["feeder_joins"] == 1


# ==========================================================================
# 8-9. The test user
# ==========================================================================
async def test_the_environment_seeds_the_test_user_on_a_fresh_database(engine, monkeypatch):
    monkeypatch.setattr(config, "admin_test_user_id", 996001)

    async with session() as db:
        assert await settings_store.admin_test_user_id(db) == 996001
        seeded = await settings_store.bootstrap_from_env(db)
        assert any("ADMIN_TEST_USER_ID" in entry for entry in seeded)

        await settings_store.set_many(db, {"admin_test_user_id": 996002})
        monkeypatch.setattr(config, "admin_test_user_id", 996001)
        assert await settings_store.admin_test_user_id(db) == 996002  # database wins


async def test_changing_the_test_user_affects_the_next_test_send(client, network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    old_user = FakeUser(user_id=996100, name="old")
    new_user = FakeUser(user_id=996200, name="new")
    network.bot.users[old_user.id] = old_user
    network.bot.users[new_user.id] = new_user

    await client.post(
        "/settings",
        data=_settings_form(admin_test_user_id=str(old_user.id), main_guild_id=str(network.main.id)),
    )
    assert "sent" in await funnel_dm.send_test_dm(
        network.bot, old_user.id, constants.KIND_DM, {"body": "hi"}, network.feeder.id
    )

    # Point it at a different account: no restart, and the old one is refused.
    await client.post(
        "/settings",
        data=_settings_form(admin_test_user_id=str(new_user.id), main_guild_id=str(network.main.id)),
    )
    assert "sent" in await funnel_dm.send_test_dm(
        network.bot, new_user.id, constants.KIND_DM, {"body": "hi"}, network.feeder.id
    )
    refused = await funnel_dm.send_test_dm(
        network.bot, old_user.id, constants.KIND_DM, {"body": "hi"}, network.feeder.id
    )
    assert "Settings" in refused
    assert len(old_user.sent) == 1


async def test_a_non_numeric_test_user_is_rejected(client, network):
    await client.post(
        "/settings",
        data=_settings_form(admin_test_user_id="123", main_guild_id=str(network.main.id)),
    )
    response = await client.post(
        "/settings",
        data=_settings_form(admin_test_user_id="not-an-id", main_guild_id=str(network.main.id)),
    )
    assert "numbers only" in unquote(response.headers["location"])
    async with session() as db:
        assert await settings_store.admin_test_user_id(db) == 123  # unchanged


# ==========================================================================
# 10. Nothing sensitive is ever rendered
# ==========================================================================
async def test_secrets_never_appear_in_the_dashboard(client, network, monkeypatch):
    monkeypatch.setattr(config, "discord_bot_token", "MTIzNDU2Nzg5.SUPERSECRETTOKEN.abcdef")
    monkeypatch.setattr(
        config, "database_url", "postgresql://funnel_user:hunter2@db.railway.internal:5432/railway"
    )

    for path in ["/", "/servers", "/feeders", "/messages", "/tags", "/conversions",
                 "/analytics", "/health", "/settings", "/setup"]:
        response = await client.get(path)
        assert response.status_code in (200, 303), path
        body = response.text
        assert "SUPERSECRETTOKEN" not in body, path
        assert "hunter2" not in body, path
        assert "funnel_user" not in body, path
        assert "db.railway.internal" not in body, path

    settings_page = (await client.get("/settings")).text
    assert "configured" in settings_page  # status only
    assert "PostgreSQL" in settings_page  # the kind, never the URL


async def test_owner_ids_stay_out_of_the_dashboard_form(client, network, monkeypatch):
    """Owner IDs remain an .env security setting, shown only as a status."""
    monkeypatch.setattr(config, "owner_user_ids", {424242424242424242})
    page = (await client.get("/settings")).text
    assert "424242424242424242" not in page
    assert "Approved owners" in page
    assert 'name="owner_user_ids"' not in page
