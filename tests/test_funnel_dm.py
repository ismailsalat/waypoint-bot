"""The funnel DM: who gets one, who does not, and how often."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot import feeder_setup, funnel_dm
from core import constants, settings as settings_store
from core.config import config
from database import crud
from tests.conftest import FakeUser


async def _ready(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)


async def test_a_new_member_gets_one_dm(network):
    await _ready(network)
    user = FakeUser(name="alex")

    status = await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)

    assert status == constants.DM_SENT
    assert len(user.sent) == 1
    payload = user.sent[0]
    assert "Rewind" in payload["content"]
    assert "Side Quest" in payload["content"]
    invite = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    assert payload["view"].children[0].url == invite.url


async def test_bots_are_ignored(network):
    await _ready(network)
    robot = FakeUser(name="another-bot", bot=True)

    status = await funnel_dm.handle_feeder_join(network.bot, robot, network.feeder.id)

    assert status == "IGNORED_BOT"
    assert robot.sent == []
    assert await crud.dm_attempts(network.db, robot.id) == []


async def test_members_of_the_main_server_are_skipped(network):
    await _ready(network)
    user = FakeUser(name="already-here")
    network.main.members[user.id] = user

    status = await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)

    assert status == constants.DM_SKIPPED_IN_MAIN
    assert user.sent == []


async def test_the_same_person_is_not_dmed_twice_for_one_feeder(network):
    await _ready(network)
    user = FakeUser(name="alex")

    first = await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)
    second = await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)

    assert first == constants.DM_SENT
    assert second == constants.DM_SKIPPED_POLICY
    assert len(user.sent) == 1


async def test_once_globally_blocks_other_feeders(network):
    await _ready(network)
    await settings_store.set_many(network.db, {"dm_policy": constants.ONCE_GLOBAL})
    other = await crud.upsert_server(
        network.db, 777001, "Rivals HQ", network.owner_id, constants.FEEDER
    )
    user = FakeUser(name="alex")

    await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)
    status = await funnel_dm.handle_feeder_join(network.bot, user, other.guild_id)

    assert status == constants.DM_SKIPPED_POLICY
    assert len(user.sent) == 1


async def test_cooldown_policy_expires(network):
    attempt = SimpleNamespace(
        status=constants.DM_SENT,
        feeder_guild_id=1,
        created_at=datetime.now(timezone.utc) - timedelta(days=40),
    )
    allowed, _ = crud.policy_allows([attempt], 1, constants.COOLDOWN_DAYS, 30, 7)
    assert allowed is True

    recent = SimpleNamespace(
        status=constants.DM_SENT,
        feeder_guild_id=1,
        created_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    allowed, reason = crud.policy_allows([recent], 1, constants.COOLDOWN_DAYS, 30, 7)
    assert allowed is False and "30 days" in reason


async def test_closed_dms_are_recorded_and_not_retried(network):
    await _ready(network)
    user = FakeUser(name="private")
    user.dms_closed = True

    first = await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)
    assert first == constants.DM_FAILED_CLOSED

    user.dms_closed = False
    second = await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)
    assert second == constants.DM_SKIPPED_POLICY
    assert user.sent == []


async def test_dry_run_records_but_sends_nothing(network):
    await _ready(network)
    server = await crud.get_server(network.db, network.feeder.id)
    server.funnel_mode = constants.DRY_RUN
    await network.db.commit()
    user = FakeUser(name="alex")

    status = await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)

    assert status == constants.DM_DRY_RUN
    assert user.sent == []
    events = (await crud.dm_attempts(network.db, user.id))
    assert events == []  # a dry run is not an attempt
    last = await crud.last_dm_for(network.db, user.id, network.feeder.id)
    assert last.status == constants.DM_DRY_RUN
    assert "Rewind" in (last.detail or "")


async def test_off_sends_nothing_at_all(network):
    await _ready(network)
    server = await crud.get_server(network.db, network.feeder.id)
    server.funnel_mode = constants.OFF
    await network.db.commit()
    user = FakeUser(name="alex")

    status = await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)

    assert status == "FUNNEL_OFF"
    assert user.sent == []


async def test_development_mode_only_lets_the_test_user_through(network, monkeypatch):
    await _ready(network)
    monkeypatch.setattr(config, "development_mode", True)
    monkeypatch.setattr(config, "admin_test_user_id", 999999)

    stranger = FakeUser(name="stranger")
    status = await funnel_dm.handle_feeder_join(network.bot, stranger, network.feeder.id)
    assert status == constants.DM_SKIPPED_DEV
    assert stranger.sent == []

    tester = FakeUser(user_id=999999, name="me")
    status = await funnel_dm.handle_feeder_join(network.bot, tester, network.feeder.id)
    assert status == constants.DM_SENT


async def test_test_dm_only_goes_to_the_admin_and_is_not_counted(network, monkeypatch):
    await _ready(network)
    monkeypatch.setattr(config, "admin_test_user_id", 555001)
    me = FakeUser(user_id=555001, name="owner")
    network.bot.users[me.id] = me

    result = await funnel_dm.send_test_dm(
        network.bot, me.id, constants.KIND_DM, {"body": "preview {feeder_name}"}, network.feeder.id
    )
    assert "Test DM message sent" in result
    assert "TEST MESSAGE" in me.sent[0]["content"]
    assert "preview Rewind" in me.sent[0]["content"]

    # Test sends never affect production statistics.
    assert await crud.dm_attempts(network.db, me.id) == []

    refused = await funnel_dm.send_test_dm(
        network.bot, 12345, constants.KIND_DM, None, network.feeder.id
    )
    assert "test user set in Settings" in refused  # the ID now lives in the database


async def test_the_dm_uses_the_feeder_override_when_there_is_one(network):
    await _ready(network)
    override = await crud.save_draft(
        network.db,
        constants.KIND_DM,
        constants.SCOPE_FEEDER,
        {"body": "Rewind people get this one", "button_label": "Go"},
        network.feeder.id,
    )
    await crud.publish_version(network.db, override.id)
    user = FakeUser(name="alex")

    await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)

    assert user.sent[0]["content"] == "Rewind people get this one"
    last = await crud.last_dm_for(network.db, user.id, network.feeder.id)
    assert last.message_version_id == override.id
