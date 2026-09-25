"""Invite tracking, conversion attribution, tag experiments and role rules."""
from __future__ import annotations

from bot import age_rules, feeder_setup, funnel_dm, invite_tracker
from core import constants
from database import analytics, crud
from tests.conftest import FakeUser


# --------------------------------------------------------------------------
# Comparing invite counts
# --------------------------------------------------------------------------
def test_invite_comparison_spots_the_used_invite():
    before = {"AAA111": 4, "BBB222": 9}
    after = {"AAA111": 4, "BBB222": 10}
    assert invite_tracker.diff_invites(before, after) == ["BBB222"]


def test_an_invite_created_since_the_last_check_counts_as_increased():
    assert invite_tracker.diff_invites({}, {"NEW1": 1}) == ["NEW1"]
    assert invite_tracker.diff_invites({}, {"NEW1": 0}) == []


def test_attribution_is_clear_when_one_tracking_invite_is_used():
    code, feeder, status = invite_tracker.resolve_attribution(
        ["BBB222"], {"AAA111": 111, "BBB222": 222}
    )
    assert (code, feeder, status) == ("BBB222", 222, constants.ATTR_INVITE)


def test_attribution_is_unknown_for_an_untracked_invite():
    code, feeder, status = invite_tracker.resolve_attribution(["ZZZ999"], {"AAA111": 111})
    assert status == constants.ATTR_UNKNOWN
    assert code is None and feeder is None


def test_attribution_is_unknown_when_nothing_changed():
    assert invite_tracker.resolve_attribution([], {"AAA111": 111})[2] == constants.ATTR_UNKNOWN


def test_attribution_is_ambiguous_when_two_invites_moved():
    _, _, status = invite_tracker.resolve_attribution(
        ["AAA111", "BBB222"], {"AAA111": 111, "BBB222": 222}
    )
    assert status == constants.ATTR_AMBIGUOUS

    # Two unrelated invites also count as ambiguous rather than a guess.
    _, _, status = invite_tracker.resolve_attribution(["X", "Y"], {"AAA111": 111})
    assert status == constants.ATTR_AMBIGUOUS


# --------------------------------------------------------------------------
# Recording conversions
# --------------------------------------------------------------------------
async def test_a_conversion_captures_the_whole_story(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    await crud.set_tags(network.db, network.feeder.id, ["movies", "chat", "fandom"])

    user = FakeUser(name="alex")
    await crud.record_join(network.db, network.feeder.id, user.id, str(user), constants.FEEDER)
    await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)

    invite = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    conversion = await crud.record_conversion(
        network.db,
        user_id=user.id,
        user_name=str(user),
        destination_guild_id=network.main.id,
        source_guild_id=network.feeder.id,
        invite_code=invite.code,
        attribution=constants.ATTR_INVITE,
    )

    assert conversion.source_guild_id == network.feeder.id
    assert conversion.invite_code == invite.code
    assert conversion.destination_guild_id == network.main.id
    assert conversion.attribution == constants.ATTR_INVITE
    assert conversion.feeder_join_at is not None
    assert conversion.dm_at is not None
    assert conversion.message_version_id is not None

    experiment = await crud.active_experiment(network.db, network.feeder.id)
    assert conversion.tag_experiment_id == experiment.id


async def test_an_unknown_conversion_still_gets_recorded(network):
    conversion = await crud.record_conversion(
        network.db,
        user_id=4242,
        user_name="mystery",
        destination_guild_id=network.main.id,
        source_guild_id=None,
        invite_code=None,
        attribution=constants.ATTR_UNKNOWN,
        detail="invites used: none detected",
    )
    assert conversion.source_guild_id is None
    assert conversion.tag_experiment_id is None

    summary = await analytics.network_summary(network.db)
    assert summary["conversions"] == 0
    assert summary["unattributed"] == 1


async def test_leaving_the_feeder_does_not_break_attribution(network):
    """Attribution comes from the invite, never from current membership."""
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    user = FakeUser(name="left-early")
    await crud.record_join(network.db, network.feeder.id, user.id, str(user), constants.FEEDER)
    network.feeder.members.pop(user.id, None)

    invite = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    tracking = await crud.tracking_codes(network.db, network.main.id)
    code, feeder_id, status = invite_tracker.resolve_attribution([invite.code], tracking)

    assert status == constants.ATTR_INVITE
    assert feeder_id == network.feeder.id


# --------------------------------------------------------------------------
# Tag experiments
# --------------------------------------------------------------------------
async def test_changing_tags_starts_a_new_experiment_and_keeps_the_old(network):
    first = await crud.set_tags(network.db, network.feeder.id, ["social", "chat", "movies"])
    second = await crud.set_tags(network.db, network.feeder.id, ["fandom", "chat", "movies"])

    assert first.id != second.id
    await network.db.refresh(first)
    assert first.ended_at is not None
    assert first.tags == ["social", "chat", "movies"]
    assert second.ended_at is None

    experiments = await crud.list_experiments(network.db, network.feeder.id)
    assert len(experiments) == 2
    assert (await crud.active_experiment(network.db, network.feeder.id)).id == second.id


async def test_saving_the_same_tags_does_not_start_a_new_experiment(network):
    first = await crud.set_tags(network.db, network.feeder.id, ["chat"])
    again = await crud.set_tags(network.db, network.feeder.id, ["chat"])
    assert first.id == again.id


async def test_conversions_stay_attached_to_the_tags_that_were_live(network):
    old = await crud.set_tags(network.db, network.feeder.id, ["social"])
    await crud.record_conversion(
        network.db, 1, "early", network.main.id, network.feeder.id, "AAA", constants.ATTR_INVITE
    )
    new = await crud.set_tags(network.db, network.feeder.id, ["fandom"])
    await crud.record_conversion(
        network.db, 2, "later", network.main.id, network.feeder.id, "AAA", constants.ATTR_INVITE
    )

    conversions = await crud.list_conversions(network.db)
    by_user = {c.user_id: c.tag_experiment_id for c in conversions}
    assert by_user[1] == old.id
    assert by_user[2] == new.id


async def test_tag_analytics_counts_joins_inside_each_window(network):
    await crud.set_tags(network.db, network.feeder.id, ["movies"])
    await crud.record_join(network.db, network.feeder.id, 1, "a", constants.FEEDER)
    await crud.record_join(network.db, network.feeder.id, 2, "b", constants.FEEDER)
    await crud.record_conversion(
        network.db, 1, "a", network.main.id, network.feeder.id, "AAA", constants.ATTR_INVITE
    )

    stats = {row["tag"]: row for row in await analytics.tag_stats(network.db)}
    assert stats["movies"]["joins"] == 2
    assert stats["movies"]["conversions"] == 1
    assert stats["movies"]["rate"] == 50.0


# --------------------------------------------------------------------------
# Role rules
# --------------------------------------------------------------------------
class FakeMember:
    def __init__(self, guild):
        self.guild = guild
        self.id = 9001
        self.kick_reason = None
        self.roles_added: list[str] = []
        self.roles_removed: list[str] = []

    async def kick(self, reason: str = ""):
        self.kick_reason = reason
        self.guild.kicked.append(self.id)

    async def add_roles(self, role, reason: str = ""):
        self.roles_added.append(role.name)

    async def remove_roles(self, role, reason: str = ""):
        self.roles_removed.append(role.name)

    def __str__(self) -> str:
        return "member"


async def test_a_role_rule_fires_only_when_the_role_is_added(network):
    from database.models import RoleRule

    rule = RoleRule(
        guild_id=network.main.id, role_id=55, role_name="Under 18", action=constants.ACTION_KICK
    )
    assert age_rules.triggered_rules([], [55], [rule]) == [rule]
    assert age_rules.triggered_rules([55], [55], [rule]) == []
    assert age_rules.triggered_rules([], [56], [rule]) == []

    rule.enabled = False
    assert age_rules.triggered_rules([], [55], [rule]) == []


async def test_the_kick_action_kicks(network):
    from database.models import RoleRule

    member = FakeMember(network.main)
    rule = RoleRule(
        guild_id=network.main.id,
        role_id=55,
        role_name="Under 18",
        action=constants.ACTION_KICK,
        reason="age self-attestation",
    )
    outcome = await age_rules.apply_rule(member, rule)

    assert member.id in network.main.kicked
    assert member.kick_reason == "age self-attestation"
    assert "kicked" in outcome


async def test_the_add_role_action_adds_a_role(network):
    from database.models import RoleRule

    member = FakeMember(network.main)
    target = network.main.roles[1]
    rule = RoleRule(
        guild_id=network.main.id,
        role_id=55,
        action=constants.ACTION_ADD_ROLE,
        target_role_id=target.id,
    )
    await age_rules.apply_rule(member, rule)
    assert member.roles_added == [target.name]


async def test_log_only_changes_nothing(network):
    from database.models import RoleRule

    member = FakeMember(network.main)
    rule = RoleRule(guild_id=network.main.id, role_id=55, action=constants.ACTION_LOG_ONLY)
    outcome = await age_rules.apply_rule(member, rule)

    assert network.main.kicked == []
    assert member.roles_added == []
    assert "log only" in outcome
