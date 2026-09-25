"""Tests for the issues raised in review.

Each section maps to one numbered item: historical tag attribution, stricter
invite ambiguity, invite deltas greater than one, age rules on the main server,
single-MAIN switching, development mode staying out of production numbers, the
destination label, and auto repair leaving unrelated overwrites alone.
"""
from __future__ import annotations

import discord
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from bot import age_rules, feeder_setup, funnel_dm, invite_tracker
from core import constants, settings as settings_store
from core.config import config
from database import analytics, crud
from database.database import as_utc
from database.models import RoleRule
from tests.conftest import FakeBot, FakeGuild, FakeInvite, FakeRole, FakeUser, next_id


# ==========================================================================
# 1. A conversion keeps the tags that were live when the person was reached
# ==========================================================================
async def test_conversion_keeps_the_experiment_that_was_live_when_the_dm_went_out(network):
    """The headline case: tags change between the DM and the join."""
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    experiment_a = await crud.set_tags(network.db, network.feeder.id, ["social", "chat"])

    user = FakeUser(name="alex")
    await crud.record_join(network.db, network.feeder.id, user.id, str(user), constants.FEEDER)
    assert await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id) == constants.DM_SENT

    # The owner switches the feeder's DISBOARD tags before the person acts.
    experiment_b = await crud.set_tags(network.db, network.feeder.id, ["fandom", "movies"])

    invite = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    conversion = await crud.record_conversion(
        network.db, user.id, str(user), network.main.id, network.feeder.id,
        invite.code, constants.ATTR_INVITE,
    )

    assert conversion.tag_experiment_id == experiment_a.id
    assert conversion.tag_experiment_id != experiment_b.id
    assert conversion.dm_at is not None
    assert conversion.message_version_id is not None


async def test_the_dm_event_itself_records_the_experiment(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    experiment = await crud.set_tags(network.db, network.feeder.id, ["movies"])
    user = FakeUser(name="alex")

    await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)

    event = await crud.last_dm_for(network.db, user.id, network.feeder.id)
    assert event.tag_experiment_id == experiment.id


async def test_without_a_dm_the_join_time_decides_the_experiment(network):
    experiment_a = await crud.set_tags(network.db, network.feeder.id, ["social"])
    await crud.record_join(network.db, network.feeder.id, 501, "alex", constants.FEEDER)
    await crud.set_tags(network.db, network.feeder.id, ["fandom"])

    conversion = await crud.record_conversion(
        network.db, 501, "alex", network.main.id, network.feeder.id, "AAA", constants.ATTR_INVITE
    )
    assert conversion.tag_experiment_id == experiment_a.id


async def test_a_rejoin_uses_the_latest_join_not_the_first(network):
    await crud.set_tags(network.db, network.feeder.id, ["social"])
    await crud.record_join(network.db, network.feeder.id, 502, "alex", constants.FEEDER)

    experiment_b = await crud.set_tags(network.db, network.feeder.id, ["fandom"])
    # They left and came back while the new tags were live.
    await crud.record_join(network.db, network.feeder.id, 502, "alex", constants.FEEDER)

    conversion = await crud.record_conversion(
        network.db, 502, "alex", network.main.id, network.feeder.id, "AAA", constants.ATTR_INVITE
    )
    assert conversion.tag_experiment_id == experiment_b.id

    latest = await crud.latest_join_at(network.db, network.feeder.id, 502)
    assert as_utc(conversion.feeder_join_at) == latest


async def test_the_current_experiment_is_only_a_last_resort(network):
    """Nobody joined and nobody was DMed, so there is nothing historical."""
    current = await crud.set_tags(network.db, network.feeder.id, ["fandom"])
    conversion = await crud.record_conversion(
        network.db, 503, "walk-in", network.main.id, network.feeder.id, "AAA", constants.ATTR_INVITE
    )
    assert conversion.tag_experiment_id == current.id


async def test_tag_analytics_credit_the_older_experiment(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    await crud.set_tags(network.db, network.feeder.id, ["social"])
    user = FakeUser(name="alex")
    await crud.record_join(network.db, network.feeder.id, user.id, str(user), constants.FEEDER)
    await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)
    await crud.set_tags(network.db, network.feeder.id, ["fandom"])
    await crud.record_conversion(
        network.db, user.id, str(user), network.main.id, network.feeder.id,
        "AAA", constants.ATTR_INVITE,
    )

    rows = {row["tag"]: row for row in await analytics.tag_stats(network.db)}
    assert rows["social"]["conversions"] == 1
    assert rows["fandom"]["conversions"] == 0


# ==========================================================================
# 2. Attribution is only confirmed when the evidence is clear
# ==========================================================================
TRACKING = {"AAA": 111, "BBB": 222}


@pytest.mark.parametrize(
    "increased, expected, expected_code",
    [
        (["AAA"], constants.ATTR_INVITE, "AAA"),            # one tracked
        (["XYZ"], constants.ATTR_UNKNOWN, None),            # one untracked
        (["AAA", "BBB"], constants.ATTR_AMBIGUOUS, None),   # two tracked
        (["AAA", "XYZ"], constants.ATTR_AMBIGUOUS, None),   # one of each
        ([], constants.ATTR_UNKNOWN, None),                 # nothing moved
    ],
)
def test_attribution_evidence(increased, expected, expected_code):
    code, feeder, status = invite_tracker.resolve_attribution(increased, TRACKING)
    assert status == expected
    assert code == expected_code
    if expected != constants.ATTR_INVITE:
        assert feeder is None


def test_a_tracked_invite_next_to_an_ordinary_one_is_not_confirmed():
    """The person may well have used the ordinary invite, so no credit."""
    _, feeder, status = invite_tracker.resolve_attribution(["AAA", "randomcode"], TRACKING)
    assert status == constants.ATTR_AMBIGUOUS
    assert feeder is None


# ==========================================================================
# 3. Two people through the same invite between snapshots
# ==========================================================================
def test_deltas_record_how_far_each_invite_moved():
    assert invite_tracker.invite_deltas({"AAA": 10}, {"AAA": 12}) == {"AAA": 2}
    assert invite_tracker.invite_deltas({"AAA": 10}, {"AAA": 10}) == {}
    assert invite_tracker.invite_deltas({}, {"NEW": 3}) == {"NEW": 3}


class FakeMember(FakeUser):
    def __init__(self, guild, name="joiner"):
        super().__init__(name=name)
        self.guild = guild


async def _ready_feeder(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    return await crud.active_invite(network.db, network.feeder.id, network.main.id)


def _set_uses(network, code, uses):
    for invite in network.main.invite_objects:
        if invite.code == code:
            invite.uses = uses


async def test_a_double_use_is_banked_and_given_to_the_next_join(network):
    invite = await _ready_feeder(network)
    tracker = invite_tracker.InviteTracker(network.bot)
    tracker.cache[network.main.id] = {invite.code: 0}

    # Two people used the same invite before we looked again.
    _set_uses(network, invite.code, 2)
    first = FakeMember(network.main, "first")
    await tracker.attribute_join(first)

    second = FakeMember(network.main, "second")  # no further change in counts
    await tracker.attribute_join(second)

    conversions = {c.user_name: c for c in await crud.list_conversions(network.db)}
    assert conversions["first"].attribution == constants.ATTR_INVITE
    assert conversions["second"].attribution == constants.ATTR_INVITE
    assert conversions["second"].source_guild_id == network.feeder.id
    assert "banked" in (conversions["first"].detail or "")

    # The bank is now empty, so a third join with no change is unknown.
    third = FakeMember(network.main, "third")
    await tracker.attribute_join(third)
    conversions = {c.user_name: c for c in await crud.list_conversions(network.db)}
    assert conversions["third"].attribution == constants.ATTR_UNKNOWN


async def test_banked_uses_survive_a_bot_restart(network):
    invite = await _ready_feeder(network)
    tracker = invite_tracker.InviteTracker(network.bot)
    tracker.cache[network.main.id] = {invite.code: 0}
    _set_uses(network, invite.code, 3)
    await tracker.attribute_join(FakeMember(network.main, "first"))

    # A restart throws away the in-memory cache; the bank is in the database.
    restarted = invite_tracker.InviteTracker(network.bot)
    await restarted.prime(network.main)
    await restarted.attribute_join(FakeMember(network.main, "after-restart"))

    conversions = {c.user_name: c for c in await crud.list_conversions(network.db)}
    assert conversions["after-restart"].attribution == constants.ATTR_INVITE
    assert conversions["after-restart"].source_guild_id == network.feeder.id

    remaining = await crud.pending_uses(network.db, network.main.id)
    assert sum(row.remaining for row in remaining) == 1


async def test_unmatched_uses_from_two_invites_are_ambiguous(network):
    """We cannot tell which of two banked invites this person used."""
    await _ready_feeder(network)
    other = await crud.upsert_server(
        network.db, 770001, "Rivals HQ", network.owner_id, constants.FEEDER
    )
    await crud.add_pending_uses(network.db, "AAA", network.feeder.id, network.main.id, 1)
    await crud.add_pending_uses(network.db, "BBB", other.guild_id, network.main.id, 1)

    tracker = invite_tracker.InviteTracker(network.bot)
    tracker.cache[network.main.id] = {
        inv.code: inv.uses for inv in network.main.invite_objects
    }
    await tracker.attribute_join(FakeMember(network.main, "unclear"))

    conversion = (await crud.list_conversions(network.db))[0]
    assert conversion.attribution == constants.ATTR_AMBIGUOUS
    assert conversion.source_guild_id is None
    # Nothing was consumed, so both are still waiting.
    assert sum(row.remaining for row in await crud.pending_uses(network.db, network.main.id)) == 2


async def test_two_different_invites_moving_banks_nothing(network):
    invite = await _ready_feeder(network)
    network.main.invite_objects.append(FakeInvite("ordinary", 0))
    tracker = invite_tracker.InviteTracker(network.bot)
    tracker.cache[network.main.id] = {invite.code: 0, "ordinary": 0}

    _set_uses(network, invite.code, 1)
    _set_uses(network, "ordinary", 1)
    await tracker.attribute_join(FakeMember(network.main, "unclear"))

    conversion = (await crud.list_conversions(network.db))[0]
    assert conversion.attribution == constants.ATTR_AMBIGUOUS
    assert await crud.pending_uses(network.db, network.main.id) == []


# ==========================================================================
# 4. Age enforcement on the main server only
# ==========================================================================
class RoleMember(FakeUser):
    def __init__(self, guild, roles):
        super().__init__(name="teenager")
        self.guild = guild
        self.roles = list(roles)
        self.kicked_with = None

    async def kick(self, reason: str = ""):
        self.kicked_with = reason
        self.guild.kicked.append(self.id)


async def test_age_enforcement_can_be_on_for_main_and_off_everywhere_else(network):
    main_server = await crud.get_server(network.db, network.main.id)
    main_server.age_enforcement = True
    await network.db.commit()

    values = await settings_store.get_all(network.db)
    assert values["default_age_enforcement"] is False  # not switched on globally

    feeder = await crud.get_server(network.db, network.feeder.id)
    assert (await settings_store.effective(network.db, main_server))["age_enforcement"] is True
    assert (await settings_store.effective(network.db, feeder))["age_enforcement"] is False


async def test_a_role_rule_on_the_main_server_kicks(network):
    main_server = await crud.get_server(network.db, network.main.id)
    main_server.age_enforcement = True
    await network.db.commit()

    under_18 = FakeRole("Under 18")
    network.main.roles.append(under_18)
    network.db.add(
        RoleRule(
            guild_id=network.main.id,
            role_id=under_18.id,
            role_name="Under 18",
            action=constants.ACTION_KICK,
            reason="age self-attestation",
            enabled=True,
        )
    )
    await network.db.commit()

    cog = age_rules.AgeRules(network.bot)
    before = RoleMember(network.main, [network.main.default_role])
    after = RoleMember(network.main, [network.main.default_role, under_18])
    after.id = before.id

    await cog.on_member_update(before, after)

    assert after.id in network.main.kicked
    assert after.kicked_with == "age self-attestation"


async def test_the_same_rule_does_nothing_where_enforcement_is_off(network):
    feeder_server = await crud.get_server(network.db, network.feeder.id)
    feeder_server.age_enforcement = False
    await network.db.commit()

    under_18 = FakeRole("Under 18")
    network.feeder.roles.append(under_18)
    network.db.add(
        RoleRule(
            guild_id=network.feeder.id, role_id=under_18.id, action=constants.ACTION_KICK, enabled=True
        )
    )
    await network.db.commit()

    cog = age_rules.AgeRules(network.bot)
    before = RoleMember(network.feeder, [])
    after = RoleMember(network.feeder, [under_18])
    after.id = before.id
    await cog.on_member_update(before, after)

    assert network.feeder.kicked == []


# ==========================================================================
# 5. Only ever one MAIN server
# ==========================================================================
async def test_switching_main_leaves_exactly_one_main(network):
    await crud.record_conversion(
        network.db, 601, "historic", network.main.id, network.feeder.id, "AAA", constants.ATTR_INVITE
    )
    new_main = await crud.upsert_server(
        network.db, 880001, "New Main", network.owner_id, constants.DISABLED
    )

    await crud.set_main_server(network.db, new_main.guild_id)

    servers = await crud.list_servers(network.db)
    mains = [s for s in servers if s.server_type == constants.MAIN]
    assert len(mains) == 1
    assert mains[0].guild_id == new_main.guild_id

    old_main = await crud.get_server(network.db, network.main.id)
    assert old_main.server_type == constants.DISABLED  # was DISABLED before it was main
    assert await settings_store.main_guild_id(network.db) == new_main.guild_id

    conversion = (await crud.list_conversions(network.db))[0]
    assert conversion.destination_guild_id == network.main.id  # history untouched


async def test_a_demoted_main_goes_back_to_what_it_was(network):
    """A feeder promoted to main becomes a feeder again when demoted."""
    await crud.set_main_server(network.db, network.feeder.id)

    promoted = await crud.get_server(network.db, network.feeder.id)
    assert promoted.server_type == constants.MAIN
    assert promoted.pre_main_type == constants.FEEDER

    await crud.set_main_server(network.db, network.main.id)
    restored = await crud.get_server(network.db, network.feeder.id)
    assert restored.server_type == constants.FEEDER
    assert restored.destination_guild_id == network.main.id
    mains = [s for s in await crud.list_servers(network.db) if s.server_type == constants.MAIN]
    assert len(mains) == 1


# ==========================================================================
# 6. Development mode stays out of production numbers
# ==========================================================================
async def test_development_mode_sends_are_marked_as_tests(network, monkeypatch):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    monkeypatch.setattr(config, "development_mode", True)
    monkeypatch.setattr(config, "admin_test_user_id", 990001)

    tester = FakeUser(user_id=990001, name="me")
    assert await funnel_dm.handle_feeder_join(network.bot, tester, network.feeder.id) == constants.DM_SENT
    assert len(tester.sent) == 1

    stranger = FakeUser(name="stranger")
    await funnel_dm.handle_feeder_join(network.bot, stranger, network.feeder.id)

    # Nothing from this run counts as production.
    rows = {row["name"]: row for row in await analytics.feeder_rows(network.db)}
    assert rows["Rewind"]["dms"] == 0
    assert await analytics.dm_breakdown(network.db) == []  # no production DM events at all
    # The published version is still listed, with nothing counted against it.
    assert all(
        row["delivered"] == 0 and row["conversions"] == 0
        for row in await analytics.message_stats(network.db)
    )
    assert await crud.dm_attempts(network.db, tester.id) == []


async def test_development_mode_still_protects_against_duplicates(network, monkeypatch):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    monkeypatch.setattr(config, "development_mode", True)
    monkeypatch.setattr(config, "admin_test_user_id", 990002)
    tester = FakeUser(user_id=990002, name="me")

    assert await funnel_dm.handle_feeder_join(network.bot, tester, network.feeder.id) == constants.DM_SENT
    assert await funnel_dm.handle_feeder_join(network.bot, tester, network.feeder.id) == constants.DM_SKIPPED_POLICY
    assert len(tester.sent) == 1


async def test_a_development_dm_does_not_feed_conversion_statistics(network, monkeypatch):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    monkeypatch.setattr(config, "development_mode", True)
    monkeypatch.setattr(config, "admin_test_user_id", 990003)
    tester = FakeUser(user_id=990003, name="me")
    await funnel_dm.handle_feeder_join(network.bot, tester, network.feeder.id)

    conversion = await crud.record_conversion(
        network.db, tester.id, "me", network.main.id, network.feeder.id, "AAA", constants.ATTR_INVITE
    )
    # The test DM is what led here, so the conversion inherits its test status
    # and is linked to it. Being a test is what keeps it out of the numbers.
    assert conversion.is_test is True
    assert conversion.message_version_id is not None
    assert all(
        row["delivered"] == 0 and row["conversions"] == 0
        for row in await analytics.message_stats(network.db)
    )


async def test_send_test_dm_still_works(network, monkeypatch):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    monkeypatch.setattr(config, "development_mode", True)
    monkeypatch.setattr(config, "admin_test_user_id", 990004)
    me = FakeUser(user_id=990004, name="owner")
    network.bot.users[me.id] = me

    result = await funnel_dm.send_test_dm(
        network.bot, me.id, constants.KIND_DM, {"body": "hello {feeder_name}"}, network.feeder.id
    )
    assert "sent" in result
    assert "hello Rewind" in me.sent[0]["content"]
    assert await crud.dm_attempts(network.db, me.id) == []


async def test_a_whole_development_run_leaves_production_numbers_at_zero(network, monkeypatch):
    """The full loop: test DM, tracking invite, join, conversion — all of it
    recorded, none of it counted."""
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    await crud.set_tags(network.db, network.feeder.id, ["movies", "chat"])
    monkeypatch.setattr(config, "development_mode", True)
    monkeypatch.setattr(config, "admin_test_user_id", 991001)

    tester = FakeUser(user_id=991001, name="me")
    await crud.record_join(network.db, network.feeder.id, tester.id, str(tester), constants.FEEDER)
    assert await funnel_dm.handle_feeder_join(network.bot, tester, network.feeder.id) == constants.DM_SENT

    # They use the feeder's tracking invite to join the main server.
    invite = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    tracker = invite_tracker.InviteTracker(network.bot)
    tracker.cache[network.main.id] = {invite.code: 0}
    _set_uses(network, invite.code, 1)

    member = FakeMember(network.main, "me")
    member.id = tester.id
    await tracker.attribute_join(member)

    # The conversion exists, is attributed, and is flagged as a test.
    conversions = await crud.list_conversions(network.db)
    assert len(conversions) == 1
    conversion = conversions[0]
    assert conversion.attribution == constants.ATTR_INVITE
    assert conversion.source_guild_id == network.feeder.id
    assert conversion.is_test is True

    # Not one production figure moved.
    summary = await analytics.network_summary(network.db)
    assert summary["conversions"] == 0
    assert summary["total_main_joins"] == 0
    assert summary["unattributed"] == 0
    assert summary["rate"] == 0.0

    rows = {row["name"]: row for row in summary["rows"]}
    assert rows["Rewind"]["conversions"] == 0
    assert rows["Rewind"]["dms"] == 0
    assert rows["Rewind"]["rate"] == 0.0

    tags = {row["tag"]: row for row in await analytics.tag_stats(network.db)}
    assert tags["movies"]["conversions"] == 0
    assert tags["chat"]["conversions"] == 0

    assert all(
        row["delivered"] == 0 and row["conversions"] == 0
        for row in await analytics.message_stats(network.db)
    )


async def test_production_conversions_are_unaffected_by_the_flag(network, monkeypatch):
    """The same run with development mode off counts normally."""
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    monkeypatch.setattr(config, "development_mode", False)

    user = FakeUser(name="real")
    await crud.record_join(network.db, network.feeder.id, user.id, str(user), constants.FEEDER)
    await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)

    invite = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    tracker = invite_tracker.InviteTracker(network.bot)
    tracker.cache[network.main.id] = {invite.code: 0}
    _set_uses(network, invite.code, 1)
    member = FakeMember(network.main, "real")
    member.id = user.id
    await tracker.attribute_join(member)

    conversion = (await crud.list_conversions(network.db))[0]
    assert conversion.is_test is False

    summary = await analytics.network_summary(network.db)
    assert summary["conversions"] == 1
    rows = {row["name"]: row for row in summary["rows"]}
    assert rows["Rewind"]["conversions"] == 1
    assert rows["Rewind"]["dms"] == 1


async def test_the_conversions_page_marks_test_rows(network):
    from dashboard.app import app
    from httpx import ASGITransport, AsyncClient

    await crud.record_conversion(
        network.db, 992001, "tester", network.main.id, network.feeder.id,
        "AAA", constants.ATTR_INVITE, is_test=True,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/conversions")
    assert response.status_code == 200
    assert "tester" in response.text
    assert ">test<" in response.text  # labelled, not hidden


# ==========================================================================
# 7 & 4b. Dashboard: destination labelling and configuring the main server
# ==========================================================================
@pytest_asyncio.fixture
async def client(network):
    from dashboard.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def test_the_main_server_has_a_configure_page_with_role_rules(client, network):
    response = await client.get(f"/servers/{network.main.id}/configure")
    assert response.status_code == 200
    assert "Role rules" in response.text
    assert "Age enforcement" in response.text
    # Funnel-only settings do not appear on the main server.
    assert "Public funnel channel" not in response.text


async def test_the_feeder_page_marks_destination_as_inherited(client, network):
    response = await client.get(f"/feeders/{network.feeder.id}")
    assert response.status_code == 200
    assert "inherited" in response.text


async def test_promoting_a_server_from_the_dropdown_demotes_the_old_main(client, network):
    new_main = await crud.upsert_server(
        network.db, 880002, "New Main", network.owner_id, constants.DISABLED
    )
    await client.post(f"/servers/{new_main.guild_id}/type", data={"server_type": constants.MAIN})

    servers = await crud.list_servers(network.db)
    for server in servers:
        await network.db.refresh(server)
    mains = [s.guild_id for s in servers if s.server_type == constants.MAIN]
    assert mains == [new_main.guild_id]


# ==========================================================================
# 8. Auto repair leaves unrelated overwrites alone
# ==========================================================================
async def test_repair_keeps_manually_added_role_overwrites(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    channel = next(c for c in network.feeder.text_channels if c.name == "join-side-quest")

    # The owner gives a helper role permission to post in the funnel channel.
    helper = FakeRole("Helper")
    network.feeder.roles.append(helper)
    await channel.set_permissions(helper, overwrite=discord.PermissionOverwrite(send_messages=True))

    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder, "auto repair")

    assert helper in channel.overwrites
    assert channel.overwrites[helper].send_messages is True
    # And the overwrites the bot owns are still enforced.
    assert channel.overwrites[network.feeder.default_role].send_messages is False
    assert channel.overwrites[network.feeder.me].send_messages is True


# ==========================================================================
# 9. Test status follows the funnel that produced it, not the current setting
# ==========================================================================
async def _join_main_through_invite(network, user_id: int, name: str):
    """Walk the real attribution path for one person."""
    invite = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    tracker = invite_tracker.InviteTracker(network.bot)
    tracker.cache[network.main.id] = {
        inv.code: inv.uses for inv in network.main.invite_objects
    }
    _set_uses(network, invite.code, invite.uses + 1)
    for inv in network.main.invite_objects:
        if inv.code == invite.code:
            invite.uses = inv.uses
    member = FakeMember(network.main, name)
    member.id = user_id
    await tracker.attribute_join(member)
    return (await crud.list_conversions(network.db))[0]


async def test_a_test_funnel_stays_a_test_after_development_mode_is_switched_off(
    network, monkeypatch
):
    """A: the whole source history is a test, so the conversion is too."""
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    await crud.set_tags(network.db, network.feeder.id, ["movies", "chat"])

    monkeypatch.setattr(config, "development_mode", True)
    monkeypatch.setattr(config, "admin_test_user_id", 993001)
    tester = FakeUser(user_id=993001, name="me")
    join = await crud.record_join(
        network.db, network.feeder.id, tester.id, str(tester), constants.FEEDER
    )
    assert join.is_test is True
    assert await funnel_dm.handle_feeder_join(network.bot, tester, network.feeder.id) == constants.DM_SENT

    # Days later the owner turns development mode off and forgets about it.
    monkeypatch.setattr(config, "development_mode", False)
    conversion = await _join_main_through_invite(network, tester.id, "me")

    assert conversion.attribution == constants.ATTR_INVITE
    assert conversion.source_guild_id == network.feeder.id
    assert conversion.is_test is True  # the DM behind it was a test

    summary = await analytics.network_summary(network.db)
    assert summary["conversions"] == 0
    assert summary["feeder_joins"] == 0
    assert summary["total_main_joins"] == 0
    assert summary["rate"] == 0.0
    tags = {row["tag"]: row for row in await analytics.tag_stats(network.db)}
    assert tags["movies"]["joins"] == 0 and tags["movies"]["conversions"] == 0
    assert all(
        row["delivered"] == 0 and row["conversions"] == 0
        for row in await analytics.message_stats(network.db)
    )


async def test_a_production_funnel_stays_production_after_development_mode_is_switched_on(
    network, monkeypatch
):
    """B: the mirror image. Turning testing on later must not erase real data."""
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    await crud.set_tags(network.db, network.feeder.id, ["movies"])

    monkeypatch.setattr(config, "development_mode", False)
    user = FakeUser(name="real")
    join = await crud.record_join(
        network.db, network.feeder.id, user.id, str(user), constants.FEEDER
    )
    assert join.is_test is False
    await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)

    # The owner starts a testing session before this person acts on the DM.
    monkeypatch.setattr(config, "development_mode", True)
    monkeypatch.setattr(config, "admin_test_user_id", 993002)
    conversion = await _join_main_through_invite(network, user.id, "real")

    assert conversion.is_test is False

    summary = await analytics.network_summary(network.db)
    assert summary["conversions"] == 1
    assert summary["feeder_joins"] == 1
    rows = {row["name"]: row for row in summary["rows"]}
    assert rows["Rewind"]["conversions"] == 1
    assert rows["Rewind"]["dms"] == 1
    assert rows["Rewind"]["rate"] == 100.0
    tags = {row["tag"]: row for row in await analytics.tag_stats(network.db)}
    assert tags["movies"]["joins"] == 1 and tags["movies"]["conversions"] == 1


async def test_the_join_decides_when_there_was_no_dm(network, monkeypatch):
    """Second in the order of preference: the feeder join."""
    monkeypatch.setattr(config, "development_mode", True)
    await crud.record_join(network.db, network.feeder.id, 993003, "tester", constants.FEEDER)

    monkeypatch.setattr(config, "development_mode", False)
    conversion = await crud.record_conversion(
        network.db, 993003, "tester", network.main.id, network.feeder.id,
        "AAA", constants.ATTR_INVITE,
    )
    assert conversion.is_test is True


async def test_the_current_mode_is_only_used_with_no_history_at_all(network, monkeypatch):
    """Third and last: nothing to go on, so the setting decides."""
    monkeypatch.setattr(config, "development_mode", True)
    conversion = await crud.record_conversion(
        network.db, 993004, "walk-in", network.main.id, None, None, constants.ATTR_UNKNOWN
    )
    assert conversion.is_test is True

    monkeypatch.setattr(config, "development_mode", False)
    production = await crud.record_conversion(
        network.db, 993005, "walk-in", network.main.id, None, None, constants.ATTR_UNKNOWN
    )
    assert production.is_test is False


# ==========================================================================
# 10. Test joins stay out of every join-based figure
# ==========================================================================
async def test_development_joins_do_not_move_any_join_total(network, monkeypatch):
    """C: joins, tag joins, network totals and overlap all ignore test rows."""
    other = await crud.upsert_server(
        network.db, 994001, "Rivals HQ", network.owner_id, constants.FEEDER
    )
    await crud.set_tags(network.db, network.feeder.id, ["movies"])
    await crud.set_tags(network.db, other.guild_id, ["movies"])

    monkeypatch.setattr(config, "development_mode", True)
    # One person joins both feeders during a testing session.
    await crud.record_join(network.db, network.feeder.id, 994100, "tester", constants.FEEDER)
    await crud.record_join(network.db, other.guild_id, 994100, "tester", constants.FEEDER)

    summary = await analytics.network_summary(network.db)
    assert summary["feeder_joins"] == 0
    rows = {row["name"]: row for row in summary["rows"]}
    assert rows["Rewind"]["joins"] == 0
    assert rows["Rivals HQ"]["joins"] == 0

    tags = {row["tag"]: row for row in await analytics.tag_stats(network.db)}
    assert tags["movies"]["joins"] == 0
    assert await analytics.cross_membership(network.db) == []

    # The rows are still there for debugging.
    stored = await crud.latest_join(network.db, network.feeder.id, 994100)
    assert stored is not None and stored.is_test is True


async def test_production_joins_still_count_normally(network, monkeypatch):
    """D: the same actions with development mode off."""
    other = await crud.upsert_server(
        network.db, 994002, "Rivals HQ", network.owner_id, constants.FEEDER
    )
    await crud.set_tags(network.db, network.feeder.id, ["movies"])
    await crud.set_tags(network.db, other.guild_id, ["movies"])

    monkeypatch.setattr(config, "development_mode", False)
    await crud.record_join(network.db, network.feeder.id, 994200, "real", constants.FEEDER)
    await crud.record_join(network.db, other.guild_id, 994200, "real", constants.FEEDER)
    await crud.record_join(network.db, network.feeder.id, 994201, "second", constants.FEEDER)

    summary = await analytics.network_summary(network.db)
    assert summary["feeder_joins"] == 3
    rows = {row["name"]: row for row in summary["rows"]}
    assert rows["Rewind"]["joins"] == 2
    assert rows["Rivals HQ"]["joins"] == 1

    tags = {row["tag"]: row for row in await analytics.tag_stats(network.db)}
    assert tags["movies"]["joins"] == 3  # both feeders carry the tag

    overlap = await analytics.cross_membership(network.db)
    assert len(overlap) == 1
    assert overlap[0]["shared"] == 1


async def test_a_mixed_run_counts_only_the_production_half(network, monkeypatch):
    monkeypatch.setattr(config, "development_mode", False)
    await crud.record_join(network.db, network.feeder.id, 994300, "real", constants.FEEDER)
    monkeypatch.setattr(config, "development_mode", True)
    await crud.record_join(network.db, network.feeder.id, 994301, "tester", constants.FEEDER)

    rows = {row["name"]: row for row in await analytics.feeder_rows(network.db)}
    assert rows["Rewind"]["joins"] == 1
