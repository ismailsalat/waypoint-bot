"""
Centralised logging setup.

* Rotating file handler  →  logs/app.log (max 5 MB × 3 backups)
* Console handler        →  INFO and above
* Secret redaction       →  tokens / auth headers never reach log files
"""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config.settings import LOG_DIR, LOG_MAX_BYTES, LOG_BACKUP_COUNT

# ── Secret-redaction filter ───────────────────────────────────────────────────

_SECRET_PATTERNS = [
    # Discord bot tokens:  MTk…  / OD…  (base64 segments)
    re.compile(r"([A-Za-z0-9_-]{24,26}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,110})"),
    # Authorization header value
    re.compile(r"(Authorization:\s*(?:Bot\s+|Bearer\s+))\S+", re.IGNORECASE),
    # "token": "…" in JSON-like strings
    re.compile(r'("token"\s*:\s*")[^"]{10,}(")', re.IGNORECASE),
]

_REDACTED = "[REDACTED]"


class _SecretRedactor(logging.Filter):
    """Strip credential-like strings from every log record.

    IMPORTANT: Only scrub string args, never coerce int/float to str.
    Coercing breaks %-style format specifiers like %d and %.1f.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(str(record.msg))
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._scrub(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    self._scrub(a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True

    @staticmethod
    def _scrub(text: str) -> str:
        for pat in _SECRET_PATTERNS:
            text = pat.sub(_REDACTED, text)
        return text


# ── Public setup ──────────────────────────────────────────────────────────────

def setup_logging(level: int = logging.DEBUG) -> None:
    """Call once at application startup."""
    root = logging.getLogger()
    if root.handlers:
        return  # already configured

    root.setLevel(logging.DEBUG)
    redactor = _SecretRedactor()
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    # File handler
    fh = RotatingFileHandler(
        LOG_DIR / "app.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    fh.addFilter(redactor)
    root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    ch.addFilter(redactor)
    root.addHandler(ch)

    # Dedicated error log — easy to find and share
    eh = RotatingFileHandler(
        LOG_DIR / "error.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    eh.setLevel(logging.ERROR)
    eh.setFormatter(fmt)
    eh.addFilter(redactor)
    root.addHandler(eh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def install_exception_hook():
    """Catch ALL unhandled exceptions and write them to error.log."""
    import sys, traceback
    _log = get_logger("unhandled")

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        _log.critical(
            "Unhandled exception:\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        )

    sys.excepthook = _hook

    # Also catch exceptions in threads
    import threading
    _orig = threading.excepthook

    def _thread_hook(args):
        if args.exc_type and not issubclass(args.exc_type, SystemExit):
            _log.critical(
                "Unhandled thread exception in %s:\n%s",
                args.thread.name if args.thread else "?",
                "".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_tb))
            )
        _orig(args)

    threading.excepthook = _thread_hook
