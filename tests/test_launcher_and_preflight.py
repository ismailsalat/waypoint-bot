"""Launcher behaviour and the split permission preflight.

The launcher tests never create a virtual environment or download anything:
the decisions are pure functions, and the subprocess calls are mocked.
"""
from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import start_local
from bot import commands as waypoint_commands, feeder_setup
from core import constants, settings as settings_store
from core.config import config
from database import crud
from database.database import session
from tests.test_worker_and_commands import FakeInteraction


# ==========================================================================
# 1. DASHBOARD_HOST and DASHBOARD_PORT come from .env
# ==========================================================================
def test_the_launcher_reads_the_dashboard_port_from_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(start_local, "ROOT", tmp_path)
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)
    monkeypatch.delenv("DASHBOARD_HOST", raising=False)
    (tmp_path / ".env").write_text(
        "DISCORD_BOT_TOKEN=x\nOWNER_USER_IDS=1\nDASHBOARD_HOST=127.0.0.1\nDASHBOARD_PORT=9000\n"
    )

    host, port = start_local.dashboard_address()

    assert (host, port) == ("127.0.0.1", 9000)


def test_a_custom_port_is_used_everywhere_the_launcher_touches_it(tmp_path, monkeypatch):
    """Checked, waited for and opened — not just read."""
    monkeypatch.setattr(start_local, "ROOT", tmp_path)
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)
    (tmp_path / ".env").write_text("DISCORD_BOT_TOKEN=x\nOWNER_USER_IDS=1\nDASHBOARD_PORT=9000\n")
    (tmp_path / "requirements.txt").write_text("")

    checked: list[tuple[str, int]] = []
    opened: list[str] = []
    monkeypatch.setattr(start_local, "ensure_environment", lambda: None)
    monkeypatch.setattr(start_local, "bot_already_running", lambda: False)
    monkeypatch.setattr(start_local, "database_lines", lambda: ["Database: SQLite (funnel.db)"])
    monkeypatch.setattr(
        start_local, "port_in_use", lambda host, port: checked.append((host, port)) or False
    )

    def fake_wait(host, port, timeout=25.0):
        checked.append((host, port))
        return True

    monkeypatch.setattr(start_local, "wait_for_dashboard", fake_wait)
    monkeypatch.setattr(start_local.webbrowser, "open", lambda url: opened.append(url))

    class DoneProcess:
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(start_local, "launch", lambda *a, **k: DoneProcess())
    monkeypatch.setattr(start_local, "stop", lambda process: None)

    start_local.main()

    assert ("127.0.0.1", 9000) in checked  # the free-port check
    assert checked.count(("127.0.0.1", 9000)) >= 2  # and the readiness wait
    assert opened == ["http://127.0.0.1:9000"]
    assert all(port != 8000 for _, port in checked)


def test_a_real_environment_variable_still_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(start_local, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("DASHBOARD_PORT=9000\n")
    monkeypatch.setenv("DASHBOARD_PORT", "7777")

    assert start_local.dashboard_address()[1] == 7777


def test_a_nonsense_port_falls_back_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(start_local, "ROOT", tmp_path)
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)
    (tmp_path / ".env").write_text("DASHBOARD_PORT=not-a-port\n")

    assert start_local.dashboard_address()[1] == start_local.DEFAULT_PORT


# ==========================================================================
# 2. The database line tells the truth
# ==========================================================================
def test_the_launcher_reports_local_sqlite(monkeypatch):
    monkeypatch.setattr(config, "database_url", "")
    for marker in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID"):
        monkeypatch.delenv(marker, raising=False)

    lines = start_local.database_lines()

    assert "Environment: Local" in lines
    assert "Database: SQLite (funnel.db)" in lines
    assert not any("PRODUCTION" in line for line in lines)


def test_the_launcher_warns_when_pointed_at_production(monkeypatch):
    url = "postgresql://funnel_user:hunter2@containers.railway.app:5432/railway"
    monkeypatch.setattr(config, "database_url", url)
    for marker in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID"):
        monkeypatch.delenv(marker, raising=False)

    lines = start_local.database_lines()

    assert "Environment: Local" in lines
    assert "Database: PostgreSQL" in lines
    assert any("PRODUCTION DATABASE" in line for line in lines)
    joined = " ".join(lines)
    for secret in ("hunter2", "funnel_user", "containers.railway.app", url):
        assert secret not in joined


def test_the_launcher_explains_a_misconfigured_railway_deploy(monkeypatch):
    monkeypatch.setattr(config, "database_url", "")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

    lines = start_local.database_lines()

    assert any("not configured" in line for line in lines)
    assert any("Railway" in line for line in lines)


# ==========================================================================
# 3. One command really means one command
# ==========================================================================
@pytest.mark.parametrize(
    "venv_exists, in_venv, relaunched, expected",
    [
        (False, False, False, "create"),     # first ever run
        (True, False, False, "relaunch"),    # venv there, wrong interpreter
        (True, True, False, "continue"),     # already inside it
        (False, False, True, "continue"),    # had our one go: never loop
        (True, False, True, "continue"),     # ditto
    ],
)
def test_the_bootstrap_decision(venv_exists, in_venv, relaunched, expected):
    assert start_local.plan_bootstrap(venv_exists, in_venv, relaunched) == expected


def test_a_missing_venv_is_created_then_handed_over(tmp_path, monkeypatch):
    """No packages are downloaded here: the subprocess calls are recorded."""
    monkeypatch.setattr(start_local, "ROOT", tmp_path)
    monkeypatch.delenv(start_local.BOOTSTRAP_FLAG, raising=False)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append([str(a) for a in args])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(start_local.subprocess, "run", fake_run)
    monkeypatch.setattr(start_local, "venv_python", lambda root=None: tmp_path / ".venv" / "python")

    result = start_local.ensure_environment()

    assert result == 0  # this process handed over rather than carrying on
    joined = [" ".join(call) for call in calls]
    assert any("-m venv" in call for call in joined)
    assert any("install -r" in call and "requirements.txt" in call for call in joined)
    assert any(call.endswith("start_local.py") for call in joined)  # the relaunch


def test_the_relaunch_marks_the_child_so_it_cannot_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(start_local, "ROOT", tmp_path)
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs.get("env") or {})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(start_local.subprocess, "run", fake_run)
    start_local.relaunch(tmp_path / ".venv" / "bin" / "python")

    assert seen.get(start_local.BOOTSTRAP_FLAG) == "1"


def test_a_failed_bootstrap_stops_rather_than_retrying(tmp_path, monkeypatch):
    monkeypatch.setattr(start_local, "ROOT", tmp_path)
    monkeypatch.delenv(start_local.BOOTSTRAP_FLAG, raising=False)
    monkeypatch.setattr(start_local, "venv_python", lambda root=None: tmp_path / ".venv" / "python")

    def explode(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(start_local.subprocess, "run", explode)

    assert start_local.ensure_environment() == 1


def test_a_missing_env_file_is_created_and_the_run_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(start_local, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text("DISCORD_BOT_TOKEN=\nOWNER_USER_IDS=\n")

    problems = start_local.check_env()

    assert (tmp_path / ".env").exists()
    assert any("run this again" in p for p in problems)
    assert any("DISCORD_BOT_TOKEN" in p for p in problems)


# ==========================================================================
# 5. Windows shutdown
# ==========================================================================
def test_children_get_their_own_process_group_on_windows(monkeypatch):
    monkeypatch.setattr(start_local.os, "name", "nt")
    monkeypatch.setattr(
        start_local.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
    )

    kwargs = start_local.launch_kwargs()

    # Without this flag CTRL_BREAK_EVENT cannot reach the children at all.
    assert kwargs["creationflags"] == start_local.subprocess.CREATE_NEW_PROCESS_GROUP
    assert "start_new_session" not in kwargs


def test_children_get_their_own_session_elsewhere(monkeypatch):
    monkeypatch.setattr(start_local.os, "name", "posix")
    assert start_local.launch_kwargs() == {"start_new_session": True}


def test_launch_passes_the_platform_flags_through(monkeypatch):
    recorded: dict = {}

    class FakePopen:
        stdout = iter(())

        def __init__(self, args, **kwargs):
            recorded["args"] = args
            recorded.update(kwargs)

        def poll(self):
            return None

    monkeypatch.setattr(start_local.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(start_local.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(start_local.os, "name", "posix")

    start_local.launch("python", ["-m", "bot.main"], "bot")

    assert recorded["args"] == ["python", "-m", "bot.main"]
    assert recorded["start_new_session"] is True


def test_shutdown_asks_before_it_kills(monkeypatch):
    events: list[str] = []

    class PolitelyStops:
        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def send_signal(self, sig):
            events.append(f"signal:{sig}")

        def wait(self, timeout=None):
            events.append("waited")
            return 0

        def kill(self):
            events.append("kill")

    monkeypatch.setattr(start_local.os, "name", "posix")
    start_local.stop(PolitelyStops())

    assert events == ["terminate", "waited"]
    assert "kill" not in events


def test_shutdown_falls_back_to_kill_if_it_has_to(monkeypatch):
    events: list[str] = []

    class Stubborn:
        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            if "kill" in events:
                return 0
            raise subprocess.TimeoutExpired("cmd", timeout or 0)

        def kill(self):
            events.append("kill")

    monkeypatch.setattr(start_local.os, "name", "posix")
    start_local.stop(Stubborn())

    assert events == ["terminate", "kill"]


def test_a_finished_process_is_left_alone():
    class Finished:
        def poll(self):
            return 0

        def terminate(self):  # pragma: no cover - must never be called
            raise AssertionError("should not touch a finished process")

    start_local.stop(Finished())


# ==========================================================================
# 4. Fresh Setup preflight covers the MAIN server
# ==========================================================================
@pytest.fixture
def approved(monkeypatch, network):
    monkeypatch.setattr(config, "owner_user_ids", {network.owner_id})
    return network.owner_id


async def test_a_feeder_does_not_need_create_invite(network):
    """The tracking invite is made in MAIN, so requiring it here would block
    feeders for no reason."""
    network.feeder.me.guild_permissions.create_instant_invite = False

    assert feeder_setup.missing_permissions(network.feeder) == []
    assert feeder_setup.missing_permissions(network.feeder, destructive=True) == []

    report = await feeder_setup.ensure_feeder(network.bot, network.db, network.feeder)
    assert report["errors"] == []
    assert report["tracking_invite"].startswith("https://discord.gg/")


async def test_the_feeder_check_still_catches_what_it_needs(network):
    network.feeder.me.guild_permissions.manage_channels = False
    network.feeder.me.guild_permissions.embed_links = False

    missing = feeder_setup.missing_permissions(network.feeder)

    assert "Manage Channels" in missing
    assert "Embed Links" in missing
    assert "Create Invite" not in missing


async def test_main_problems_are_reported_separately(network):
    async with session() as db:
        assert await feeder_setup.main_server_problems(network.bot, db) == []

        network.main.me.guild_permissions.create_instant_invite = False
        problems = await feeder_setup.main_server_problems(network.bot, db)
    assert any("Create Invite" in p and "Side Quest" in p for p in problems)


async def test_main_problems_notice_a_missing_manage_server(network):
    network.main.me.guild_permissions.manage_guild = False
    async with session() as db:
        problems = await feeder_setup.main_server_problems(network.bot, db)
    assert any("Manage Server" in p and "attribution" in p for p in problems)


async def test_main_problems_notice_no_usable_channel(network):
    for channel in network.main.text_channels:
        channel.can_invite = False
    async with session() as db:
        problems = await feeder_setup.main_server_problems(network.bot, db)
    assert any("create an invite" in p for p in problems)


async def test_fresh_refuses_and_deletes_nothing_when_main_cannot_invite(network, approved):
    network.feeder.add_channel("old-general")
    network.feeder.add_channel("old-memes")
    network.main.me.guild_permissions.create_instant_invite = False

    async with session() as db:
        report = await feeder_setup.fresh_setup(network.bot, db, network.feeder, approved)

    assert report["deleted"] == []  # zero channels deleted
    assert {"old-general", "old-memes"} <= {c.name for c in network.feeder.text_channels}
    assert any("cannot create tracking invites" in e for e in report["errors"])
    async with session() as db:
        assert any(e.action == "fresh_setup_cancelled" for e in await crud.recent_logs(db))


async def test_fresh_refuses_when_the_bot_is_not_in_main(network, approved):
    network.feeder.add_channel("old-general")
    network.bot.guilds = [g for g in network.bot.guilds if g.id != network.main.id]

    async with session() as db:
        report = await feeder_setup.fresh_setup(network.bot, db, network.feeder, approved)

    assert report["deleted"] == []
    assert any("not in the main server" in e for e in report["errors"])


async def test_fresh_works_when_both_sides_are_in_order(network, approved):
    network.feeder.add_channel("old-general")
    network.feeder.me.guild_permissions.create_instant_invite = False  # not needed here

    async with session() as db:
        report = await feeder_setup.fresh_setup(network.bot, db, network.feeder, approved)

    assert report["deleted"] == ["old-general"]
    assert report["errors"] == []
    assert report["tracking_invite"].startswith("https://discord.gg/")
    assert {"join-side-quest", "bump"} == {c.name for c in network.feeder.text_channels}


async def test_the_slash_command_stops_before_the_warning_if_main_is_not_ready(network, approved, monkeypatch):
    cog = waypoint_commands.WaypointCommands(network.bot)
    keep = network.feeder.add_channel("lobby")
    network.main.me.guild_permissions.create_instant_invite = False
    interaction = FakeInteraction(network.feeder, approved)

    async def never_called(self):  # pragma: no cover - the point is it is not reached
        raise AssertionError("confirmation should not be offered")

    monkeypatch.setattr(waypoint_commands.ConfirmFresh, "wait", never_called)
    await cog.fresh_command.callback(cog, interaction)

    said = interaction.said()
    assert "cannot create tracking invites" in said
    assert "Nothing was deleted" in said
    assert "WARNING" not in said
    assert keep.deleted is False


async def test_the_dashboard_worker_runs_the_same_preflight(network, approved):
    """Typing FRESH on the dashboard does not skip the MAIN checks."""
    from bot import maintenance

    network.feeder.add_channel("old-general")
    network.main.me.guild_permissions.create_instant_invite = False
    worker = maintenance.Maintenance(network.bot)

    async with session() as db:
        task = await crud.queue_task(
            db, constants.TASK_FRESH_SETUP, {"guild_id": network.feeder.id}
        )
    await worker.task_loop.coro(worker)

    from database.models import Task

    async with session() as db:
        finished = await db.get(Task, task.id)
    assert finished.status == constants.TASK_FAILED
    assert "cannot create tracking invites" in (finished.error or "")
    assert "old-general" in {c.name for c in network.feeder.text_channels}
