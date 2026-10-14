"""Saved results of signed-in reads: cleaned in place, or quarantined unread.

Every signed-in route (Trends, IndiaMART, the dropshipping platforms) keeps
the browser's own result file. The research account's details are removed
from it with the route's redactor before anything reads it. A saved result
that cannot be read or cleaned is never kept where it could be read or
backed up: it is moved, unread, into a quarantine folder beside the
acquisition folders (``QUARANTINE``, excluded from backups by name), or
deleted if it cannot be moved.
"""

from datetime import datetime, timezone
import json

from .models import SafetyError
from .storage import atomic_json, safe_path

QUARANTINE = "signed-in-quarantine"
MAX_SAVED = 12 * 1024 * 1024


def quarantine(path):
    """Move a saved result away unread; delete it if it cannot be moved."""
    folder = safe_path(path.parent.parent / QUARANTINE)
    target = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + path.parent.name + "-" + path.name + ".unread")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.rename(target)
    except OSError:
        try:
            path.unlink()
        except OSError:
            raise SafetyError("SIGNED_IN_REDACTION_FAILED") from None


def protect_saved(path, redact):
    """Clean a saved browser result in place with ``redact``; one that cannot
    be read or cleaned is quarantined. Returns "absent", "redacted" or "quarantined"."""
    path = safe_path(path)
    if not path.exists() and not path.is_symlink():
        return "absent"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SAVED:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError
        clean = redact(value)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        quarantine(path)
        return "quarantined"
    if clean != value:
        try:
            atomic_json(path, clean)
        except (SafetyError, OSError):
            quarantine(path)  # the cleaned copy could not be saved: the raw file never stays readable
            return "quarantined"
    return "redacted"
