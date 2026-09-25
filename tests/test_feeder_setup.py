"""Feeder registration, automatic setup and repair."""
from __future__ import annotations

from bot import feeder_setup
from core import constants, settings as settings_store
from database import crud


async def test_feeder_registration_creates_everything(network):
    report = await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)

    assert report["errors"] == []
    assert report["funnel_channel"] == "OK"
    assert report["bump_channel"] == "OK"
    assert report["tracking_invite"].startswith("https://discord.gg/")

    names = {c.name for c in network.feeder.text_channels}
    assert names == {"join-side-quest", "bump"}

    server = await crud.get_server(network.db, network.feeder.id)
    assert server.funnel_channel_id is not None
    assert server.bump_channel_id is not None
    assert server.destination_guild_id == network.main.id
    assert server.funnel_message_id is not None

    invite = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    assert invite is not None and invite.active


async def test_public_channel_is_readable_but_locked(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    channel = next(c for c in network.feeder.text_channels if c.name == "join-side-quest")
    everyone = channel.overwrites[network.feeder.default_role]
    assert everyone.view_channel is True
    assert everyone.send_messages is False
    assert everyone.create_public_threads is False


async def test_bump_channel_is_hidden_from_members(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    channel = next(c for c in network.feeder.text_channels if c.name == "bump")
    assert channel.overwrites[network.feeder.default_role].view_channel is False
    staff = next(r for r in network.feeder.roles if r.name == "Staff")
    assert channel.overwrites[staff].view_channel is True


async def test_setup_is_idempotent(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    second = await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    assert second["repairs"] == []
    assert len(network.feeder.text_channels) == 2
    assert len(network.main.invite_objects) == 1


async def test_repair_recreates_a_deleted_channel(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    network.feeder.delete_channel("join-side-quest")
    server = await crud.get_server(network.db, network.feeder.id)
    server.funnel_channel_id = None
    await network.db.commit()

    report = await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder, "auto repair")
    assert any("join-side-quest" in repair for repair in report["repairs"])
    assert "join-side-quest" in {c.name for c in network.feeder.text_channels}


async def test_repair_replaces_a_deleted_invite(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    first = await crud.active_invite(network.db, network.feeder.id, network.main.id)

    # Someone deletes the invite in Discord.
    network.main.invite_objects[0].deleted = True

    report = await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder, "auto repair")
    second = await crud.active_invite(network.db, network.feeder.id, network.main.id)

    assert second.code != first.code
    assert any("tracking invite" in repair for repair in report["repairs"])
    await network.db.refresh(first)
    assert first.active is False


async def test_renaming_the_channel_renames_it_in_discord(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    server = await crud.get_server(network.db, network.feeder.id)
    server.funnel_channel_name = "join-main"
    await network.db.commit()

    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    assert "join-main" in {c.name for c in network.feeder.text_channels}


async def test_setup_refuses_when_the_server_is_not_a_feeder(network):
    server = await crud.get_server(network.db, network.feeder.id)
    server.server_type = constants.DISABLED
    await network.db.commit()

    report = await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    assert report["errors"]
    assert network.feeder.text_channels == []


async def test_per_feeder_overrides_beat_global_defaults(network):
    values = await settings_store.get_all(network.db)
    server = await crud.get_server(network.db, network.feeder.id)

    assert settings_store.resolve(values, server)["dm_delay_seconds"] == 0

    server.dm_delay_seconds = 30
    server.funnel_channel_name = "join-main"
    resolved = settings_store.resolve(values, server)
    assert resolved["dm_delay_seconds"] == 30
    assert resolved["funnel_channel_name"] == "join-main"
    assert resolved["bump_channel_name"] == "bump"  # still the global default
