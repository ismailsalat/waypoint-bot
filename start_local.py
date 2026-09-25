"""Start everything locally with one command.

    python start_local.py

If there is no virtual environment yet it makes one, installs the
requirements, and re-launches itself inside it. Then it starts the Discord bot
and the dashboard, streams both logs into this one window with a prefix so you
can tell them apart, opens the dashboard in your browser, and stops both
cleanly on Ctrl+C.

It reads .env the same way the rest of the project does, refuses to start a
second copy of either process, and never prints your token or database URL.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIRED_KEYS = ("DISCORD_BOT_TOKEN", "OWNER_USER_IDS")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# Set on the child so a failed bootstrap can never relaunch itself forever.
BOOTSTRAP_FLAG = "WAYPOINT_LAUNCHER_BOOTSTRAPPED"


def say(message: str) -> None:
    print(f"[waypoint] {message}", flush=True)


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------
def read_env(path: Path | None = None) -> dict[str, str]:
    path = path or (ROOT / ".env")
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def dashboard_address(env: dict[str, str] | None = None) -> tuple[str, int]:
    """Host and port the dashboard will actually bind to.

    Read from .env first, exactly like dashboard.py does, so a custom
    DASHBOARD_PORT is checked, launched, waited for and opened — rather than
    the launcher assuming 8000 while the dashboard listens somewhere else.
    A real environment variable still wins, since that is what the child
    process will see.
    """
    env = read_env() if env is None else env
    host = os.environ.get("DASHBOARD_HOST") or env.get("DASHBOARD_HOST") or DEFAULT_HOST
    raw_port = os.environ.get("DASHBOARD_PORT") or env.get("DASHBOARD_PORT") or str(DEFAULT_PORT)
    try:
        port = int(str(raw_port).strip())
    except (TypeError, ValueError):
        say(f"DASHBOARD_PORT is not a number ({raw_port!r}); using {DEFAULT_PORT}.")
        port = DEFAULT_PORT
    return host.strip(), port


def check_env() -> list[str]:
    """Make sure .env exists and has what a local run needs.

    DATABASE_URL is deliberately not required: leaving it out gives you the
    automatic local SQLite file.
    """
    env_path = ROOT / ".env"
    example = ROOT / ".env.example"
    if not env_path.exists():
        if example.exists():
            shutil.copyfile(example, env_path)
            return [
                "I created .env from .env.example.",
                "Add DISCORD_BOT_TOKEN and OWNER_USER_IDS to .env, then run this again.",
            ]
        return ["No .env file found. Copy .env.example to .env and fill it in."]

    values = read_env(env_path)
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        return [
            f"These are missing from .env: {', '.join(missing)}.",
            "Add them, then run this again. (DATABASE_URL is optional locally.)",
        ]
    return []


# --------------------------------------------------------------------------
# Virtual environment bootstrap
# --------------------------------------------------------------------------
def venv_python(root: Path | None = None) -> Path:
    """Where the project's virtual environment interpreter lives."""
    root = root or ROOT
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def plan_bootstrap(
    venv_exists: bool, running_in_venv: bool, already_relaunched: bool
) -> str:
    """Decide what to do about the virtual environment.

    Returns "continue", "relaunch" or "create". Pure, so the decision can be
    tested without ever creating a venv or downloading a package.
    """
    if running_in_venv:
        return "continue"
    if already_relaunched:
        # We have had our one go. Carry on with whatever interpreter we have
        # rather than bouncing between processes forever.
        return "continue"
    if venv_exists:
        return "relaunch"
    return "create"


def create_venv(python: str = sys.executable) -> None:
    say("No .venv found. Creating one…")
    subprocess.run([python, "-m", "venv", str(ROOT / ".venv")], cwd=str(ROOT), check=True)
    say("Installing requirements (this happens once)…")
    interpreter = str(venv_python())
    subprocess.run(
        [interpreter, "-m", "pip", "install", "--upgrade", "pip"],
        cwd=str(ROOT), check=False,
    )
    subprocess.run(
        [interpreter, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
        cwd=str(ROOT), check=True,
    )


def relaunch(interpreter: Path) -> int:
    say(f"Re-launching inside {interpreter}")
    environment = dict(os.environ, **{BOOTSTRAP_FLAG: "1"})
    completed = subprocess.run(
        [str(interpreter), str(Path(__file__).resolve())], cwd=str(ROOT), env=environment
    )
    return completed.returncode


def ensure_environment() -> int | None:
    """Return an exit code if this process handed over, else None to carry on."""
    interpreter = venv_python()
    running_in_venv = Path(sys.executable).resolve() == interpreter.resolve() if interpreter.exists() else False
    action = plan_bootstrap(
        venv_exists=interpreter.exists(),
        running_in_venv=running_in_venv,
        already_relaunched=os.environ.get(BOOTSTRAP_FLAG) == "1",
    )

    if action == "continue":
        return None
    if action == "relaunch":
        return relaunch(interpreter)

    try:
        create_venv()
    except subprocess.CalledProcessError as exc:
        say(f"Could not set up the virtual environment (exit code {exc.returncode}).")
        say("Create it yourself with: python -m venv .venv")
        return 1
    return relaunch(interpreter)


# --------------------------------------------------------------------------
# Database, reported through the project's own resolver
# --------------------------------------------------------------------------
def database_lines() -> list[str]:
    """Describe the database this run will use. Never the URL itself."""
    try:
        from database.database import DatabaseNotConfigured, database_status
    except Exception:  # pragma: no cover - only before dependencies are installed
        return ["Database: resolved at start-up."]

    try:
        status = database_status()
    except DatabaseNotConfigured as exc:
        return [f"Database: not configured. {exc}"]

    if status["error"]:
        return [f"Database: not configured. {status['error']}"]

    where = f" ({status['file']})" if status["file"] else ""
    lines = [
        f"Environment: {status['environment']}",
        f"Database: {status['kind']}{where}",
    ]
    if status["is_production"]:
        lines.append("WARNING: PRODUCTION DATABASE — this is live data, not a local copy.")
    return lines


# --------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------
def launch_kwargs() -> dict:
    """Extra Popen arguments for the platform.

    On Windows the children need their own process group, otherwise
    CTRL_BREAK_EVENT cannot be delivered to them and Ctrl+C on the launcher
    leaves them running.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def bot_already_running() -> bool:
    """Best-effort check for another copy of the bot on this machine."""
    try:
        if os.name == "nt":
            output = subprocess.run(
                ["wmic", "process", "get", "CommandLine"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        else:
            output = subprocess.run(
                ["ps", "-eo", "args"], capture_output=True, text=True, timeout=10
            ).stdout
    except (OSError, subprocess.SubprocessError):
        return False

    mine = str(os.getpid())
    for line in output.splitlines():
        if "bot.main" in line and "start_local" not in line and mine not in line:
            return True
    return False


def stream(process: subprocess.Popen, label: str) -> None:
    for raw in iter(process.stdout.readline, ""):
        if raw:
            print(f"[{label}] {raw.rstrip()}", flush=True)


def launch(python: str, args: list[str], label: str) -> subprocess.Popen:
    process = subprocess.Popen(
        [python, *args],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **launch_kwargs(),
    )
    threading.Thread(target=stream, args=(process, label), daemon=True).start()
    return process


def stop(process: subprocess.Popen) -> None:
    """Ask nicely, wait, and only then use force."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:  # pragma: no cover - last resort
            pass


def wait_for_dashboard(host: str, port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_in_use(host, port):
            return True
        time.sleep(0.4)
    return False


def main() -> int:
    say(f"Project: {ROOT}")

    handed_over = ensure_environment()
    if handed_over is not None:
        return handed_over

    problems = check_env()
    if problems:
        for line in problems:
            say(line)
        return 1

    for line in database_lines():
        say(line)

    host, port = dashboard_address()

    if bot_already_running():
        say("A Waypoint bot process already seems to be running on this machine.")
        say("Stop it first, or you will have two bots answering the same events.")
        return 1

    if port_in_use(host, port):
        say(f"Port {port} is already in use, so the dashboard cannot start.")
        say("Close whatever is using it, or set DASHBOARD_PORT in .env to a free port.")
        return 1

    python = sys.executable
    say(f"Python: {python}")

    # Initialize the shared schema before either child starts. Concurrent first
    # starts must not race to add a column or create the default messages.
    import asyncio
    async def prepare_database():
        from database.database import init_db, session, get_engine
        from database import crud
        from core import settings
        await init_db()
        async with session() as db:
            await settings.bootstrap_from_env(db)
            await crud.ensure_default_messages(db)
        await get_engine().dispose()
    try:
        asyncio.run(prepare_database())
    except Exception:
        say("Database initialization failed. Check DATABASE_URL and database access before starting.")
        return 1

    processes: list[subprocess.Popen] = []
    try:
        say("Starting the Discord bot…")
        processes.append(launch(python, ["-m", "bot.main"], "bot"))
        say("Starting the dashboard…")
        processes.append(launch(python, ["dashboard.py"], "dashboard"))

        url = f"http://{host}:{port}"
        if wait_for_dashboard(host, port):
            say(f"Dashboard ready at {url}")
            try:
                webbrowser.open(url)
            except Exception:  # pragma: no cover - headless machines
                pass
        else:
            say(f"The dashboard has not answered yet; try {url} in a moment.")

        say("Both are running. Press Ctrl+C to stop them.")
        while True:
            for process in processes:
                if process.poll() is not None:
                    say(f"A process exited with code {process.returncode}. Shutting down.")
                    return process.returncode or 1
            time.sleep(1)
    except KeyboardInterrupt:
        say("Stopping…")
        return 0
    finally:
        for process in processes:
            stop(process)
        say("Stopped.")


if __name__ == "__main__":
    raise SystemExit(main())
