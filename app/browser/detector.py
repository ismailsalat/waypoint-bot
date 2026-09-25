"""
Browser profile detector.

Supports: Chrome, Edge, Brave (Windows, macOS, Linux) + Incognito mode.

Key behaviours:
- Discovers all persistent profiles (Default, Profile 1 … Profile 30)
- Adds a virtual Incognito entry per browser (launched with --incognito /
  --inprivate) — incognito has no separate profile dir, uses Default
- Reads the human-readable profile name from Preferences JSON
- login status checked via LevelDB token scan (token_check.py)
- launch_discord() opens Discord in the exact profile
- Incognito is always marked "login unknown" (no persistent storage)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Generator

from app.services.logging_service import get_logger

log = get_logger(__name__)

SUPPORTED_BROWSERS = ["Chrome", "Edge", "Brave"]


@dataclass
class BrowserProfile:
    browser:      str        # "Chrome" | "Edge" | "Brave"
    profile_name: str        # "Default" | "Profile 1" | "Incognito"
    profile_path: Path       # path to profile dir (or User Data for Incognito)
    display_name: str = ""   # friendly name from Preferences
    is_incognito: bool = False
    exe_path:     Optional[Path] = None

    @property
    def id(self) -> str:
        return f"{self.browser}::{self.profile_name}"

    @property
    def label(self) -> str:
        if self.is_incognito:
            return f"{self.browser} — Incognito"
        dn = f" ({self.display_name})" if self.display_name else ""
        return f"{self.browser} — {self.profile_name}{dn}"

    @property
    def short_label(self) -> str:
        if self.is_incognito:
            return "Incognito"
        return self.display_name or self.profile_name


# ── Executable lookup ─────────────────────────────────────────────────────────

def _find_exe(browser: str) -> Optional[Path]:
    if sys.platform == "win32":
        L  = Path(os.environ.get("LOCALAPPDATA",  ""))
        P  = Path(os.environ.get("PROGRAMFILES",   "C:/Program Files"))
        P86= Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        candidates = {
            "Chrome": [
                L   / "Google/Chrome/Application/chrome.exe",
                P   / "Google/Chrome/Application/chrome.exe",
                P86 / "Google/Chrome/Application/chrome.exe",
            ],
            "Edge": [
                L   / "Microsoft/Edge/Application/msedge.exe",
                P   / "Microsoft/Edge/Application/msedge.exe",
                P86 / "Microsoft/Edge/Application/msedge.exe",
            ],
            "Brave": [
                L   / "BraveSoftware/Brave-Browser/Application/brave.exe",
                P   / "BraveSoftware/Brave-Browser/Application/brave.exe",
            ],
        }
    elif sys.platform == "darwin":
        candidates = {
            "Chrome": [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")],
            "Edge":   [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")],
            "Brave":  [Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")],
        }
    else:
        candidates = {
            "Chrome": [Path("/usr/bin/google-chrome"),
                       Path("/usr/bin/google-chrome-stable"),
                       Path("/snap/bin/chromium")],
            "Edge":   [Path("/usr/bin/microsoft-edge"),
                       Path("/usr/bin/microsoft-edge-stable")],
            "Brave":  [Path("/usr/bin/brave-browser"),
                       Path("/usr/bin/brave-browser-stable")],
        }
    for p in candidates.get(browser, []):
        if p.exists():
            return p
    return None


# ── User data dir lookup ──────────────────────────────────────────────────────

def _user_data_dir(browser: str) -> Optional[Path]:
    if sys.platform == "win32":
        L = Path(os.environ.get("LOCALAPPDATA",  ""))
        R = Path(os.environ.get("APPDATA",        ""))
        dirs = {
            "Chrome": L / "Google/Chrome/User Data",
            "Edge":   L / "Microsoft/Edge/User Data",
            "Brave":  L / "BraveSoftware/Brave-Browser/User Data",
        }
    elif sys.platform == "darwin":
        A = Path.home() / "Library/Application Support"
        dirs = {
            "Chrome": A / "Google/Chrome",
            "Edge":   A / "Microsoft Edge",
            "Brave":  A / "BraveSoftware/Brave-Browser",
        }
    else:
        C = Path.home() / ".config"
        dirs = {
            "Chrome": C / "google-chrome",
            "Edge":   C / "microsoft-edge",
            "Brave":  C / "BraveSoftware/Brave-Browser",
        }
    d = dirs.get(browser)
    return d if d and d.is_dir() else None


# ── Profile metadata ──────────────────────────────────────────────────────────

def _read_display_name(profile_dir: Path) -> str:
    """Read the human-readable name from Preferences JSON."""
    try:
        prefs = profile_dir / "Preferences"
        if prefs.is_file():
            data = json.loads(prefs.read_text(encoding="utf-8", errors="ignore"))
            return data.get("profile", {}).get("name", "")
    except Exception:
        pass
    return ""


# ── Public API ────────────────────────────────────────────────────────────────

def detect_profiles() -> List[BrowserProfile]:
    """
    Return all detected browser profiles across all supported browsers.
    Includes one virtual Incognito entry per installed browser.
    """
    results: List[BrowserProfile] = []

    for browser in SUPPORTED_BROWSERS:
        exe = _find_exe(browser)
        udd = _user_data_dir(browser)

        if not exe:
            log.debug("%s: not installed (exe not found)", browser)
            continue
        if not udd:
            log.debug("%s: user data dir not found", browser)
            continue

        # Persistent profiles
        added = 0
        for name in ["Default"] + [f"Profile {i}" for i in range(1, 31)]:
            pdir = udd / name
            if not pdir.is_dir():
                continue
            display = _read_display_name(pdir)
            results.append(BrowserProfile(
                browser      = browser,
                profile_name = name,
                profile_path = pdir,
                display_name = display,
                is_incognito = False,
                exe_path     = exe,
            ))
            added += 1

        # Incognito — virtual entry (launches browser with --incognito flag)
        # Uses Default as the base profile dir reference
        default_dir = udd / "Default"
        if default_dir.is_dir():
            results.append(BrowserProfile(
                browser      = browser,
                profile_name = "Incognito",
                profile_path = default_dir,
                display_name = "",
                is_incognito = True,
                exe_path     = exe,
            ))

        log.info("Browser %s: %d profile(s) found", browser, added)

    log.info("Total profiles detected: %d", len(results))
    return results


def check_login(profile: BrowserProfile) -> tuple[bool, str]:
    """
    Check whether this profile is logged into Discord.
    Returns (is_logged_in, status_string).
    
    Incognito: always returns (False, "Incognito — no persistent session")
    """
    if profile.is_incognito:
        return False, "Incognito — no persistent session"

    try:
        from app.browser.token_check import has_valid_token
        logged_in = has_valid_token(profile.profile_path)
        status    = "✓ Logged In" if logged_in else "○ Login Required"
        return logged_in, status
    except Exception as e:
        log.debug("Login check error for %s: %s", profile.id, e)
        return False, "? Unknown"


def extract_token(profile: BrowserProfile) -> Optional[str]:
    """
    Extract the Discord token from a browser profile.
    Returns None for Incognito or if not logged in.
    """
    if profile.is_incognito:
        return None
    try:
        from app.browser.token_check import extract_token_for_profile
        return extract_token_for_profile(profile.profile_path)
    except Exception:
        return None


def launch_discord(profile: BrowserProfile,
                   url: str = "https://discord.com/app") -> tuple[bool, str]:
    """
    Open Discord in a specific browser profile.
    Returns (success, message).
    
    Incognito:   opens with --incognito / --inprivate flag
    Regular:     opens with --profile-directory=<name>
    """
    exe = profile.exe_path or _find_exe(profile.browser)
    if not exe or not exe.exists():
        msg = f"{profile.browser} executable not found. Is it installed?"
        log.error(msg)
        return False, msg

    args = [str(exe)]

    if profile.is_incognito:
        # Edge uses --inprivate, Chrome/Brave use --incognito
        flag = "--inprivate" if profile.browser == "Edge" else "--incognito"
        args += [flag, "--new-window", url]
    else:
        # --profile-directory is the folder name relative to User Data,
        # not the display name. e.g. "Default" or "Profile 1"
        args += [
            f"--profile-directory={profile.profile_name}",
            "--new-window",
            url,
        ]

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Windows: don't flash a console window
            **({
                "creationflags": subprocess.CREATE_NO_WINDOW
            } if sys.platform == "win32" else {}),
        )
        msg = (f"Opened Discord in {profile.label} (pid {proc.pid})")
        log.info("Launched %s pid=%d", profile.label, proc.pid)
        return True, msg
    except FileNotFoundError:
        msg = f"{profile.browser} not found at {exe}"
        log.error(msg)
        return False, msg
    except Exception as e:
        msg = f"Launch failed: {e}"
        log.error(msg)
        return False, msg
