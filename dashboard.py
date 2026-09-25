"""Start the local dashboard.

    python dashboard.py   ->   http://127.0.0.1:8000
"""
from __future__ import annotations

import uvicorn

from core.config import config
from database.database import DatabaseNotConfigured, database_status

if __name__ == "__main__":
    if config.dashboard_host not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Dashboard must bind to localhost, 127.0.0.1, or ::1.")
    try:
        status = database_status()
    except DatabaseNotConfigured as exc:
        raise SystemExit(str(exc)) from exc
    if status["error"]:
        raise SystemExit(status["error"])

    where = f" ({status['file']})" if status["file"] else ""
    print(f"{status['environment']} environment, {status['kind']} database{where}")
    if status["is_production"]:
        print("PRODUCTION DATABASE — this dashboard is connected to live data.")
    print(f"Dashboard running at http://{config.dashboard_host}:{config.dashboard_port}")
    uvicorn.run(
        "dashboard.app:app",
        host=config.dashboard_host,
        port=config.dashboard_port,
        reload=False,
        proxy_headers=False,
        log_level="info",
    )
