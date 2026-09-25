"""
Bump Scheduler Pro — Entry point
"""
from __future__ import annotations
import sys
from pathlib import Path

# Make sure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

from app.accounts.store import AccountStore
from app.scheduler.auto_scheduler import AutoScheduler
from app.gui.app import BumpSchedulerGUI
from app.services.logging_service import get_logger, install_exception_hook

log = get_logger("main")


def main():
    install_exception_hook()   # catch all unhandled exceptions → error.log
    log.info("Starting Bump Scheduler Pro")

    # Shared account store
    from app.config.settings import DATA_DIR
    from suite.runtime import Runtime
    runtime = Runtime(DATA_DIR)
    runtime.startup()
    store = runtime.store

    # Scheduler — passes log events to GUI
    gui_ref = [None]
    def _log_cb(level: str, msg: str):
        if gui_ref[0]:
            gui_ref[0].log_event(level, msg)

    scheduler = runtime.scheduler
    scheduler._log_cb = _log_cb

    # GUI
    gui = BumpSchedulerGUI(store, scheduler)
    gui_ref[0] = gui

    try:
        gui.run()
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    main()
