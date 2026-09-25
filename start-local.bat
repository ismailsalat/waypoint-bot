@echo off
REM Start Waypoint locally: bot + dashboard in one window.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [waypoint] No .venv found. Creating one...
    python -m venv .venv
    if errorlevel 1 (
        echo [waypoint] Could not create the virtual environment. Is Python installed and on PATH?
        pause
        exit /b 1
    )
    echo [waypoint] Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [waypoint] Dependency installation failed.
        pause
        exit /b 1
    )
)

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [waypoint] Created .env from .env.example.
    )
    echo [waypoint] Add DISCORD_BOT_TOKEN and OWNER_USER_IDS to .env, then run this again.
    echo [waypoint] You do not need DATABASE_URL locally; SQLite is automatic.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" start_local.py
pause
