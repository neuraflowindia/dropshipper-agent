"""Load one private, draft-only service credential; no login or release rights.

The factory is local-only. Actual requests happen only in the later private
staging stage. Credentials never enter the saved programme or model context.
"""

import os
from pathlib import Path
import stat

from .models import SafetyError, decode_json, identifier, timestamp, utc_now
from .portal_delivery_check import ORIGIN
from .portal_storage import DraftAutomationStorage
from .private_drafts import stage_private_draft
from .storage import safe_path

CONFIG_NAME = "draft_automation.json"
MAX_CONFIG = 4096


def load_draft_automation_storage(root, *, transport=None, as_of=None):
    path = safe_path(Path(root) / "config" / "local" / CONFIG_NAME)
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_CONFIG:
            raise ValueError
        raw = os.read(descriptor, MAX_CONFIG + 1)
        after = os.fstat(descriptor)
        if (len(raw) != before.st_size or len(raw) > MAX_CONFIG
                or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError
        value = decode_json(raw)
        required = {"schema_version", "origin", "client_id", "service_token_id", "service_client_id", "service_client_secret", "expires_at"}
        if (not isinstance(value, dict) or set(value) != required or type(value["schema_version"]) is not int
                or value["schema_version"] != 1 or value["origin"] != ORIGIN):
            raise ValueError
        identifier(value["client_id"])
        identifier(value["service_token_id"])
        if timestamp(value["expires_at"]) <= timestamp(as_of or utc_now()):
            raise SafetyError("PORTAL_DRAFT_SERVICE_CREDENTIAL_EXPIRED")
        return DraftAutomationStorage(value["origin"], value["client_id"], value["service_client_id"], value["service_client_secret"], transport=transport)
    except SafetyError as error:
        if str(error) == "PORTAL_DRAFT_SERVICE_CREDENTIAL_EXPIRED":
            raise
        raise SafetyError("PORTAL_DRAFT_SERVICE_CREDENTIALS_UNAVAILABLE") from None
    except (OSError, ValueError, TypeError, KeyError):
        raise SafetyError("PORTAL_DRAFT_SERVICE_CREDENTIALS_UNAVAILABLE") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def private_staging_callback(root, storage):
    """Return a draft-only callback; the stage owns its locked receipt write."""
    root = Path(root)

    def stage(issue_id, revision):
        # Do not rewrite the receipt after the helper releases its issue lock.
        # A simultaneous waiting caller must not replace another writer's
        # successful immutable-file proof. Programme checkpoints own waits.
        return stage_private_draft(root, issue_id, revision, storage=storage)

    return stage
