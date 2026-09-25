"""Credentials stay in the OS keychain or process memory, never JSON exports."""
from __future__ import annotations

from threading import RLock
try:
    import keyring
    _HAS_KEYRING = True
except ImportError:
    _HAS_KEYRING = False

_KEYRING_SERVICE = "bump-scheduler"
_MEMORY_STORE = {}
_STATUS = {}
_LOCK = RLock()


def store_credential(account_id, token):
    with _LOCK:
        _MEMORY_STORE[account_id] = token
        _STATUS[account_id] = "session_only"
        if _HAS_KEYRING:
            try:
                keyring.set_password(_KEYRING_SERVICE, account_id, token)
                _STATUS[account_id] = "os_keychain"
            except Exception:
                pass
        return _STATUS[account_id]


def get_credential(account_id):
    with _LOCK:
        if account_id in _MEMORY_STORE:
            return _MEMORY_STORE[account_id]
        if account_id in _STATUS:
            return None
        if _HAS_KEYRING:
            try:
                value = keyring.get_password(_KEYRING_SERVICE, account_id)
                if value:
                    _MEMORY_STORE[account_id] = value
                    _STATUS[account_id] = "os_keychain"
                    return value
            except Exception:
                pass
        _STATUS[account_id] = "missing"
        return None


def delete_credential(account_id):
    with _LOCK:
        if _HAS_KEYRING:
            try:
                keyring.delete_password(_KEYRING_SERVICE, account_id)
            except Exception:
                # A missing password is harmless, but failure to delete an
                # existing one must be reported rather than silently retained.
                try:
                    remaining = keyring.get_password(_KEYRING_SERVICE, account_id)
                except Exception:
                    remaining = None
                if remaining:
                    raise RuntimeError("Could not remove the saved credential from the OS keychain.") from None
        _MEMORY_STORE.pop(account_id, None)
        _STATUS[account_id] = "missing"


def has_credential(account_id):
    return get_credential(account_id) is not None


def credential_status(account_id):
    get_credential(account_id)
    with _LOCK:
        return _STATUS.get(account_id, "missing")


def clear_all():
    """Clear the process cache (test helper); never erase OS keychain entries."""
    with _LOCK:
        _MEMORY_STORE.clear()
        _STATUS.clear()
