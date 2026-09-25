"""The repair queue, the slash commands, and the rest of this patch.

The first section is the important one: it drives the real producer/consumer
flow — dashboard writes a task, the real worker loop claims and runs it —
rather than testing the helpers in isolation, because that flow is what was
broken.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from bot import commands as waypoint_commands, feeder_setup, funnel_dm, maintenance, messages
from core import constants, rendering, settings as settings_store
from core.config import config
from database import crud
from database.database import session
from database.models import Task, utcnow
from tests.conftest import FakeUser, next_id


@pytest_asyncio.fixture
async def client(network):
    from dashboard.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


@pytest_asyncio.fixture
async def worker(network):
    """The real Maintenance cog, with the loops not started."""
    return maintenance.Maintenance(network.bot)


async def tick(worker) -> None:
    """One pass of the real task loop, exactly as the timer would run it."""
    await worker.task_loop.coro(worker)


# ==========================================================================
# 1. The stuck-PENDING bug: dashboard -> tasks table -> worker -> DONE
# ==========================================================================
async def test_a_queued_repair_is_claimed_and_completed(client, network, worker):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)

    # The dashboard queues the job, exactly as pressing Repair does.
    response = await client.post(f"/feeders/{network.feeder.id}/repair")
    assert response.status_code == 303

    queued = list(await crud.pending_tasks(network.db))
    assert len(queued) == 1
    task_id = queued[0].id
    assert queued[0].status == constants.TASK_PENDING
    assert queued[0].guild_id == network.feeder.id

    # The worker runs one pass.
    await tick(worker)

    task = await network.db.get(Task, task_id)
    await network.db.refresh(task)
    assert task.status == constants.TASK_DONE
    assert task.started_at is not None
    assert task.finished_at is not None
    assert task.attempts == 1
    assert "Rewind" in (task.result or "")
    assert task.error is None

    # And the Health page shows it as done rather than pending.
    page = (await client.get("/health")).text
    assert "DONE" in page
    assert "Waiting — bot worker appears offline" not in page


async def test_a_task_moves_through_running(network, worker):
    async with session() as db:
        task = await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": network.feeder.id})

    seen = {}

    async def slow_run(task_type, payload):
        async with session() as db:
            current = await db.get(Task, task.id)
            seen["status"] = current.status
            seen["started_at"] = current.started_at
        return "done"

    worker.run_task = slow_run
    await tick(worker)

    assert seen["status"] == constants.TASK_RUNNING  # claimed before running
    assert seen["started_at"] is not None


async def test_a_failing_task_is_marked_failed_and_the_worker_carries_on(network, worker):
    async with session() as db:
        first = await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": 1})
        second = await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": 2})

    async def flaky(task_type, payload):
        if payload.get("guild_id") == 1:
            raise RuntimeError("Discord said no")
        return "second job fine"

    worker.run_task = flaky
    await tick(worker)

    async with session() as db:
        bad = await db.get(Task, first.id)
        good = await db.get(Task, second.id)
        assert bad.status == constants.TASK_FAILED
        assert "Discord said no" in (bad.error or "")
        assert good.status == constants.TASK_DONE  # the failure did not stop the queue
        assert any(e.action == "task_failed" for e in await crud.recent_logs(db))


async def test_the_worker_survives_a_database_error_mid_pass(network, worker, monkeypatch):
    """The original bug: one exception killed the loop and everything after it
    stayed PENDING forever."""
    calls = {"n": 0}
    real_pending = crud.pending_tasks

    async def flaky_pending(db, limit=20):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return await real_pending(db, limit)

    monkeypatch.setattr(crud, "pending_tasks", flaky_pending)
    async with session() as db:
        task = await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": network.feeder.id})

    await tick(worker)  # must not raise
    monkeypatch.setattr(crud, "pending_tasks", real_pending)
    worker.run_task = lambda task_type, payload: _ok()
    await tick(worker)

    async with session() as db:
        assert (await db.get(Task, task.id)).status == constants.TASK_DONE


async def _ok():
    return "fine"


async def test_a_task_is_only_claimed_once(network, worker):
    async with session() as db:
        task = await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": network.feeder.id})
        first = await crud.claim_task(db, task.id)
        second = await crud.claim_task(db, task.id)
    assert first is not None
    assert second is None  # a second worker cannot take the same job


async def test_stale_running_tasks_are_recovered_on_restart(network, worker):
    async with session() as db:
        retryable = await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": 1})
        exhausted = await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": 2})
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        for task, attempts in ((retryable, 1), (exhausted, constants.TASK_MAX_ATTEMPTS)):
            row = await db.get(Task, task.id)
            row.status = constants.TASK_RUNNING
            row.started_at = old
            row.attempts = attempts
        await db.commit()

    await worker.recover_stale_tasks()

    async with session() as db:
        assert (await db.get(Task, retryable.id)).status == constants.TASK_PENDING
        failed = await db.get(Task, exhausted.id)
        assert failed.status == constants.TASK_FAILED
        assert "Abandoned" in (failed.error or "")
        assert any(e.action == "worker_recovered_stale_task" for e in await crud.recent_logs(db))


async def test_a_fresh_running_task_is_left_alone(network, worker):
    async with session() as db:
        task = await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": 1})
        row = await db.get(Task, task.id)
        row.status = constants.TASK_RUNNING
        row.started_at = utcnow()
        await db.commit()

    await worker.recover_stale_tasks()

    async with session() as db:
        assert (await db.get(Task, task.id)).status == constants.TASK_RUNNING


async def test_the_worker_heartbeat_tells_the_dashboard_it_is_alive(network, worker, client):
    status = await crud.worker_status(network.db)
    assert status["online"] is False  # nothing has run yet

    await tick(worker)

    status = await crud.worker_status(network.db)
    assert status["online"] is True
    assert status["seconds_ago"] is not None and status["seconds_ago"] < 5

    page = (await client.get("/health")).text
    assert "ONLINE" in page


async def test_an_old_heartbeat_reads_as_offline(network, client):
    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
    await settings_store.set_many(network.db, {crud.HEARTBEAT_KEY: stale.isoformat()})

    assert (await crud.worker_status(network.db))["online"] is False
    async with session() as db:
        await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": network.feeder.id})
    page = (await client.get("/health")).text
    assert "OFFLINE" in page
    assert "Waiting — bot worker appears offline" in page


async def test_failed_tasks_can_be_retried_and_finished_ones_cleared(client, network):
    async with session() as db:
        task = await crud.queue_task(db, constants.TASK_REPAIR, {"guild_id": network.feeder.id})
        await crud.fail_task(db, task.id, "boom")

    await client.post(f"/tasks/{task.id}/retry")
    async with session() as db:
        assert (await db.get(Task, task.id)).status == constants.TASK_PENDING

    # Clearing never removes work that is still queued.
    await client.post("/tasks/clear")
    async with session() as db:
        assert (await db.get(Task, task.id)) is not None
        await crud.complete_task(db, task.id, "done")
    await client.post("/tasks/clear")
    async with session() as db:
        assert (await db.get(Task, task.id)) is None


async def test_auto_repair_uses_the_same_implementation(network, worker):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    network.feeder.delete_channel("join-side-quest")
    async with session() as db:
        server = await crud.get_server(db, network.feeder.id)
        server.funnel_channel_id = None
        server.last_health_check = None
        await db.commit()

    await worker.run_auto_repair()

    assert "join-side-quest" in {c.name for c in network.feeder.text_channels}


# ==========================================================================
# 2. Slash commands
# ==========================================================================
class FakeResponse:
    def __init__(self):
        self.messages: list[dict] = []
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    async def defer(self, **kwargs):
        self.deferred = True

    async def edit_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeFollowup:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, content=None, **kwargs):
        self.messages.append(content or "")


class FakeInteraction:
    def __init__(self, guild, user_id: int):
        self.guild = guild
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    def said(self) -> str:
        parts = [str(m.get("content") or "") for m in self.response.messages]
        return " ".join(parts + self.followup.messages)


@pytest.fixture
def approved(monkeypatch, network):
    monkeypatch.setattr(config, "owner_user_ids", {network.owner_id})
    return network.owner_id


async def test_setup_command_keeps_unrelated_channels(network, approved, worker):
    cog = waypoint_commands.WaypointCommands(network.bot)
    keep = network.feeder.add_channel("general-chat")
    interaction = FakeInteraction(network.feeder, approved)

    await cog.setup_command.callback(cog, interaction)

    names = {c.name for c in network.feeder.text_channels}
    assert "general-chat" in names  # untouched
    assert keep.deleted is False
    assert "join-side-quest" in names
    assert "bump" in names
    invite = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    assert invite is not None
    server = await crud.get_server(network.db, network.feeder.id)
    await network.db.refresh(server)
    assert server.funnel_message_id is not None
    assert "Repaired" in interaction.said()


async def test_repair_command_fixes_only_what_is_missing(network, approved):
    cog = waypoint_commands.WaypointCommands(network.bot)
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    keep = network.feeder.add_channel("memes")
    network.feeder.delete_channel("bump")
    async with session() as db:
        server = await crud.get_server(db, network.feeder.id)
        server.bump_channel_id = None
        await db.commit()

    interaction = FakeInteraction(network.feeder, approved)
    await cog.repair_command.callback(cog, interaction)

    names = {c.name for c in network.feeder.text_channels}
    assert "bump" in names
    assert "memes" in names and keep.deleted is False
    assert "Errors" in interaction.said()


async def test_commands_refuse_an_unapproved_user(network, monkeypatch):
    monkeypatch.setattr(config, "owner_user_ids", set())
    cog = waypoint_commands.WaypointCommands(network.bot)
    interaction = FakeInteraction(network.feeder, 999)

    await cog.setup_command.callback(cog, interaction)

    assert "approved owner" in interaction.said()
    assert network.feeder.text_channels == []  # nothing was created


async def test_fresh_is_refused_on_the_main_server(network, approved):
    cog = waypoint_commands.WaypointCommands(network.bot)
    keep = network.main.add_channel("announcements")
    interaction = FakeInteraction(network.main, approved)

    reason = await cog.deny_reason(interaction, destructive=True)
    await cog.fresh_command.callback(cog, interaction)

    assert "disabled on the MAIN server" in reason
    assert "disabled on the MAIN server" in interaction.said()
    assert keep.deleted is False


async def test_fresh_is_refused_for_someone_who_is_not_the_guild_owner(network, monkeypatch):
    monkeypatch.setattr(config, "owner_user_ids", {555})
    cog = waypoint_commands.WaypointCommands(network.bot)
    interaction = FakeInteraction(network.feeder, 555)  # approved, but not the owner

    reason = await cog.deny_reason(interaction, destructive=True)

    assert "owner of this server" in reason


async def test_fresh_waits_for_confirmation_before_deleting(network, approved, monkeypatch):
    cog = waypoint_commands.WaypointCommands(network.bot)
    keep = network.feeder.add_channel("lobby")
    interaction = FakeInteraction(network.feeder, approved)

    # Simulate the confirmation timing out.
    async def never_confirmed(self):
        return None

    monkeypatch.setattr(waypoint_commands.ConfirmFresh, "wait", never_confirmed)
    await cog.fresh_command.callback(cog, interaction)

    assert "WARNING" in interaction.said()
    assert "timed out" in interaction.said()
    assert keep.deleted is False  # nothing deleted without confirmation


async def test_fresh_rebuilds_the_server_after_confirmation(network, approved):
    keep_role = network.feeder.roles[1]
    network.feeder.add_channel("old-general")
    network.feeder.add_channel("old-memes")
    stubborn = network.feeder.add_channel("locked-channel", deletable=False)

    async with session() as db:
        report = await feeder_setup.fresh_setup(network.bot, db, network.feeder, approved)

    assert set(report["deleted"]) == {"old-general", "old-memes"}
    assert any("locked-channel" in kept for kept in report["kept"])
    names = {c.name for c in network.feeder.text_channels}
    assert "join-side-quest" in names and "bump" in names
    assert "old-general" not in names
    assert report["tracking_invite"].startswith("https://discord.gg/")
    assert keep_role in network.feeder.roles  # roles are never touched
    async with session() as db:
        actions = {e.action for e in await crud.recent_logs(db)}
        assert "fresh_setup_confirmed" in actions
        assert "fresh_setup_completed" in actions


async def test_fresh_checks_permissions_before_deleting_anything(network, approved):
    network.feeder.add_channel("precious")
    network.feeder.me.guild_permissions.manage_channels = False

    async with session() as db:
        report = await feeder_setup.fresh_setup(network.bot, db, network.feeder, approved)

    assert report["deleted"] == []
    assert any("Missing permissions" in e for e in report["errors"])
    assert "precious" in {c.name for c in network.feeder.text_channels}


async def test_dashboard_fresh_setup_needs_the_typed_word(client, network):
    response = await client.post(f"/feeders/{network.feeder.id}/fresh", data={"confirm": "yes"})
    assert "Type FRESH" in response.headers["location"].replace("%20", " ")
    assert list(await crud.pending_tasks(network.db)) == []

    await client.post(f"/feeders/{network.feeder.id}/fresh", data={"confirm": "FRESH"})
    queued = list(await crud.pending_tasks(network.db))
    assert [t.task_type for t in queued] == [constants.TASK_FRESH_SETUP]


async def test_dashboard_fresh_setup_is_refused_on_main(client, network):
    response = await client.post(
        f"/feeders/{network.main.id}/fresh", data={"confirm": "FRESH"}
    )
    assert "disabled on the MAIN server" in response.headers["location"].replace("%20", " ")
    assert list(await crud.pending_tasks(network.db)) == []


async def test_status_command_reports_without_changing_anything(network, approved):
    cog = waypoint_commands.WaypointCommands(network.bot)
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    interaction = FakeInteraction(network.feeder, approved)

    await cog.status_command.callback(cog, interaction)

    said = interaction.said()
    assert "FEEDER" in said and "Side Quest" in said
    assert "Worker:" in said


# ==========================================================================
# 3. Buttons
# ==========================================================================
def test_a_direct_link_button_carries_the_invite():
    view = messages.build_view(
        {"button_mode": "LINK", "button_label": "Join"}, "https://discord.gg/AAA111", 222
    )
    button = view.children[0]
    assert button.style is discord.ButtonStyle.link
    assert button.url == "https://discord.gg/AAA111"


def test_an_interactive_button_uses_a_real_discord_style():
    view = messages.build_view(
        {"button_mode": "INTERACTIVE", "button_style": "SUCCESS", "button_label": "Join"}, "", 222
    )
    item = view.children[0]
    assert item.item.style is discord.ButtonStyle.success
    assert item.item.custom_id == "waypoint:invite:222"
    assert item.item.url is None


def test_unsupported_button_values_fall_back_instead_of_being_sent():
    assert rendering.normalise_button_style("#ff00ff") == "PRIMARY"
    assert rendering.normalise_button_style("rainbow") == "PRIMARY"
    assert rendering.normalise_button_mode("something") == "LINK"
    content = rendering.render_content(
        {"button_style": "#ff00ff", "button_mode": "nonsense"}, rendering.build_context()
    )
    assert content["button_style"] in constants.BUTTON_STYLES
    assert content["button_mode"] in constants.BUTTON_MODES


async def test_the_interactive_button_hands_over_the_current_invite(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    invite = await crud.active_invite(network.db, network.feeder.id, network.main.id)

    button = messages.InviteButton(
        network.feeder.id, "Join", discord.ButtonStyle.primary
    )
    interaction = FakeInteraction(network.feeder, 1)
    await button.callback(interaction)

    assert invite.url in interaction.said()
    assert "Side Quest" in interaction.said()


async def test_the_interactive_button_follows_a_rotated_invite(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    old = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    new = await crud.save_invite(
        network.db, network.feeder.id, network.main.id, "NEW999", "https://discord.gg/NEW999"
    )

    button = messages.InviteButton(network.feeder.id, "Join", discord.ButtonStyle.primary)
    interaction = FakeInteraction(network.feeder, 1)
    await button.callback(interaction)

    assert new.url in interaction.said()
    assert old.url not in interaction.said()


# ==========================================================================
# 4. Draft and version management
# ==========================================================================
async def test_an_unused_draft_can_be_deleted(client, network):
    draft = await crud.save_draft(
        network.db, constants.KIND_DM, constants.SCOPE_GLOBAL, {"body": "junk"}
    )
    await client.post(f"/messages/{draft.id}/delete")

    versions = await crud.list_versions(network.db, constants.KIND_DM, constants.SCOPE_GLOBAL)
    assert draft.id not in [v.id for v in versions]


async def test_a_referenced_version_cannot_be_deleted_but_can_be_archived(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    user = FakeUser(name="alex")
    await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)
    event = await crud.last_dm_for(network.db, user.id, network.feeder.id)
    version_id = event.message_version_id
    assert version_id is not None

    assert await crud.version_is_referenced(network.db, version_id) is True

    # Publishing a replacement, then the old one can be archived but not deleted.
    replacement = await crud.save_draft(
        network.db, constants.KIND_DM, constants.SCOPE_GLOBAL, {"body": "newer"}
    )
    await crud.publish_version(network.db, replacement.id)

    ok, message = await crud.delete_version(network.db, version_id)
    assert ok is False and "Archive it instead" in message

    ok, message = await crud.archive_version(network.db, version_id)
    assert ok is True
    archived = await network.db.get(type(replacement), version_id)
    assert archived.status == constants.ARCHIVED

    # The DM event still resolves to it, so analytics keep working.
    event = await crud.last_dm_for(network.db, user.id, network.feeder.id)
    assert event.message_version_id == version_id


async def test_the_live_published_version_cannot_be_archived_by_accident(network):
    published = await crud.save_draft(
        network.db, constants.KIND_DM, constants.SCOPE_GLOBAL, {"body": "live"}
    )
    await crud.publish_version(network.db, published.id)

    ok, message = await crud.archive_version(network.db, published.id)

    assert ok is False and "Publish another version" in message


async def test_a_draft_can_be_duplicated(network):
    original = await crud.save_draft(
        network.db, constants.KIND_DM, constants.SCOPE_GLOBAL, {"body": "starting point"}
    )
    copy = await crud.duplicate_version(network.db, original.id)

    assert copy.id != original.id
    assert copy.content["body"] == "starting point"
    assert copy.status == constants.DRAFT
    assert "copy of version" in (copy.note or "")


# ==========================================================================
# 5. Invite manager
# ==========================================================================
async def test_rotating_an_invite_keeps_old_conversions_intact(network, worker):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    old = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    old_code = old.code

    await crud.record_conversion(
        network.db, 5001, "early", network.main.id, network.feeder.id,
        old_code, constants.ATTR_INVITE,
    )

    result = await worker.rotate_invite(network.feeder.id)

    new = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    assert new.code != old_code
    assert new.active is True
    assert new.url in result
    await network.db.refresh(old)
    assert old.active is False and old.revoked_at is not None

    # History keeps the old code.
    conversion = (await crud.list_conversions(network.db))[0]
    assert conversion.invite_code == old_code

    # Future DMs use the replacement.
    user = FakeUser(name="later")
    await funnel_dm.handle_feeder_join(network.bot, user, network.feeder.id)
    assert user.sent[0]["view"].children[0].url == new.url

    # And the public post was rebuilt with it.
    server = await crud.get_server(network.db, network.feeder.id)
    channel = next(c for c in network.feeder.text_channels if c.name == "join-side-quest")
    message = channel.messages[server.funnel_message_id]
    assert message.payload["view"].children[0].url == new.url


async def test_repair_replaces_a_dead_invite_and_updates_the_message(network, worker):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    old = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    network.main.invite_objects[0].deleted = True  # someone deleted it in Discord

    async with session() as db:
        task = await crud.queue_task(
            db, constants.TASK_REPAIR, {"guild_id": network.feeder.id}
        )
    await tick(worker)

    async with session() as db:
        assert (await db.get(Task, task.id)).status == constants.TASK_DONE
    new = await crud.active_invite(network.db, network.feeder.id, network.main.id)
    assert new.code != old.code


async def test_repost_is_queued_with_force(client, network):
    await client.post(f"/feeders/{network.feeder.id}/repost")
    queued = list(await crud.pending_tasks(network.db))
    assert queued[0].task_type == constants.TASK_SYNC_PUBLIC
    assert queued[0].payload["force"] is True


# ==========================================================================
# 6. Renaming managed channels
# ==========================================================================
async def test_renaming_a_managed_channel_does_not_create_a_duplicate(network):
    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    await settings_store.set_many(network.db, {"default_funnel_channel_name": "join-us"})

    await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)

    names = [c.name for c in network.feeder.text_channels]
    assert names.count("join-us") == 1
    assert "join-side-quest" not in names  # renamed, not duplicated
    assert len(names) == 2


# ==========================================================================
# 7. Custom variables
# ==========================================================================
async def test_custom_variables_render_but_cannot_shadow_built_ins(network):
    await settings_store.set_many(
        network.db, {"custom_variables": {"community_type": "Gaming", "region": "NA"}}
    )
    context = await crud.network_context(network.db, None, "https://discord.gg/AAA")

    assert context["community_type"] == "Gaming"
    assert rendering.render_text("A {community_type} server in {region}", context) == (
        "A Gaming server in NA"
    )

    accepted, problems = rendering.validate_custom_variables({"invite_url": "https://evil"})
    assert accepted == {} and problems


async def test_the_settings_page_rejects_a_bad_variable_name(client, network):
    response = await client.post(
        "/settings",
        data={
            "network_name": "Side Quest Network",
            "main_guild_id": str(network.main.id),
            "custom_variables": "Bad Name = x\nregion = NA",
            "default_funnel_channel_name": "join-side-quest",
            "default_bump_channel_name": "bump",
            "default_dm_delay_seconds": "0",
            "default_funnel_mode": constants.LIVE,
            "dm_policy": constants.ONCE_PER_FEEDER,
            "dm_cooldown_days": "30",
            "failure_retry_days": "7",
            "repair_interval_minutes": "30",
            "bump_staff_role_names": "Staff",
        },
    )
    assert "not a valid name" in response.headers["location"].replace("%20", " ")
    stored = (await settings_store.get_all(network.db, fresh=True))["custom_variables"]
    assert stored == {"region": "NA"}  # the good one was still saved


# ==========================================================================
# 8. The local launcher
# ==========================================================================
def test_the_launcher_does_not_require_a_database_url():
    import start_local

    assert "DATABASE_URL" not in start_local.REQUIRED_KEYS
    assert set(start_local.REQUIRED_KEYS) == {"DISCORD_BOT_TOKEN", "OWNER_USER_IDS"}


def test_the_launcher_reports_missing_values_without_printing_them(tmp_path, monkeypatch, capsys):
    import start_local

    monkeypatch.setattr(start_local, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("DISCORD_BOT_TOKEN=supersecrettoken\nOWNER_USER_IDS=\n")

    problems = start_local.check_env()

    assert any("OWNER_USER_IDS" in p for p in problems)
    assert all("supersecrettoken" not in p for p in problems)


def test_the_launcher_creates_an_env_file_from_the_example(tmp_path, monkeypatch):
    import start_local

    monkeypatch.setattr(start_local, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text("DISCORD_BOT_TOKEN=\nOWNER_USER_IDS=\n")

    problems = start_local.check_env()

    assert (tmp_path / ".env").exists()
    assert any("run this again" in p for p in problems)


def test_the_launcher_notices_an_occupied_port():
    import socket

    import start_local

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert start_local.port_in_use("127.0.0.1", port) is True
    assert start_local.port_in_use("127.0.0.1", port) is False
