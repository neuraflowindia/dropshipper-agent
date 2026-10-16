"""One project-local backup attempt per India day; never research authority.

The existing programme task calls this at saved work boundaries. A small daily
intent prevents repeated full copying after failure or interruption. No provider,
model, credential, publication or second operating-system task is introduced.
"""

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from .backup_retention import disk_space_reason, tidy_after_backup, tidy_when_disk_low
from .models import SafetyError, digest, timestamp, utc_now
from .runtime_storage import runtime_path
from .storage import Journal, atomic_json, load_json, safe_path


VERSION = "project-local-backup-day-v1"
MAX_CONTROL_BYTES = 16 * 1024
TERMINAL = {"saved", "saved_with_limitations", "failed"}


def _reason(error):
    value = str(error) if isinstance(error, SafetyError) else "LOCAL_BACKUP_FILE_FAILED"
    return value if value.isascii() and value.replace("_", "").isalnum() and value.isupper() and len(value) <= 160 else "LOCAL_BACKUP_FAILED"


def _result(state, *, day=None, reason=None, control=None, action_started=False, reused=False):
    control = control or {}
    return {"state": state, "day": day, "reason": reason, "backup_id": control.get("backup_id"),
        "destination": "data/backups", "files": control.get("files"), "bytes": control.get("bytes"),
        "manifest_hash": control.get("manifest_hash"), "completed_at": control.get("completed_at"),
        "manifest_created_at": control.get("manifest_created_at"),
        "limitations": control.get("limitations", []), "backup_action_started": action_started,
        "reused_saved_result": reused, "independent_backup_proved": False,
        "original_layout_resume_proved": False, "provider_calls": 0, "model_calls": 0,
        "financial_writes": 0, "research_writes": 0}


def _load_control(path, *, root_hash, day):
    if not path.exists():
        return None
    wrapper = load_json(path, MAX_CONTROL_BYTES)
    if (not isinstance(wrapper, dict) or set(wrapper) != {"control", "hash"}
            or digest(wrapper["control"]) != wrapper["hash"]):
        raise SafetyError("LOCAL_BACKUP_CONTROL_INVALID")
    value = wrapper["control"]
    fields = {"schema_version", "kind", "root_hash", "day", "backup_id", "attempted_at",
              "completed_at", "manifest_created_at", "state", "manifest_hash", "files", "bytes", "limitations", "reason"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["kind"] != VERSION or value["root_hash"] != root_hash
            or value["day"] != day or value["backup_id"] != "daily-" + day
            or value["state"] not in TERMINAL | {"attempt_started"}
            or not isinstance(value["limitations"], list) or len(value["limitations"]) > 100
            or any(not isinstance(row, str) or len(row) > 1000 for row in value["limitations"])):
        raise SafetyError("LOCAL_BACKUP_CONTROL_INVALID")
    date.fromisoformat(value["day"]); timestamp(value["attempted_at"])
    if value["completed_at"] is not None:
        if timestamp(value["completed_at"]) < timestamp(value["attempted_at"]):
            raise SafetyError("LOCAL_BACKUP_CONTROL_INVALID")
    if value["manifest_created_at"] is not None:
        timestamp(value["manifest_created_at"])
    for field, maximum in (("files", 100000), ("bytes", 512 * 1024 * 1024)):
        if value[field] is not None and (type(value[field]) is not int or not 0 <= value[field] <= maximum):
            raise SafetyError("LOCAL_BACKUP_CONTROL_INVALID")
    if value["state"] in {"saved", "saved_with_limitations"}:
        hashed = value["manifest_hash"]
        if (not isinstance(hashed, str) or len(hashed) != 64 or any(char not in "0123456789abcdef" for char in hashed)
                or value["completed_at"] is None or value["manifest_created_at"] is None
                or value["files"] is None or value["bytes"] is None):
            raise SafetyError("LOCAL_BACKUP_CONTROL_INVALID")
    return value


def _save_control(path, control):
    atomic_json(path, {"control": control, "hash": digest(control)})


def _finished(control, manifest, at, *, root):
    from .operational_backup import ESSENTIAL_BACKUP_VERSION, _validate_manifest
    manifest = _validate_manifest(manifest)
    if manifest["backup_id"] != control["backup_id"]:
        raise SafetyError("LOCAL_BACKUP_ID_CONFLICT")
    if manifest["kind"] != ESSENTIAL_BACKUP_VERSION:
        raise SafetyError("LOCAL_BACKUP_ESSENTIAL_MANIFEST_REQUIRED")
    selection = manifest["essential_selection"]
    if selection["project_root"] != str(root) or selection["project_root_hash"] != control["root_hash"]:
        raise SafetyError("LOCAL_BACKUP_PROJECT_BINDING_CHANGED")
    limited = selection["essential_files_complete"] is not True
    limitations = ["This project-local copy does not protect against loss or failure of the drive.",
                  "Sign-in files and credentials are excluded; relocated application resumption is not proved."]
    if limited:
        limitations.append("Some files could not be included; see the saved manifest for the exact reasons.")
    return dict(control, state="saved_with_limitations" if limited else "saved", completed_at=at,
        manifest_created_at=manifest["created_at"],
        manifest_hash=manifest["hash"], files=len(manifest["files"]),
        bytes=sum(row["bytes"] for row in manifest["files"]), limitations=limitations, reason=None)


def run_due_operational_backup(memory, *, root, now=None, settings=None, pending_controls=(), creator=None):
    """One optional daily action. Missing/disabled configuration performs none.

    The caller owns the existing programme guard; a separate small backup guard
    also prevents two manual housekeeping callers from copying simultaneously.
    A failed or unfinished day's intent is never a licence for a new full copy.
    """
    at = now or utc_now()
    try:
        timestamp(at); root = safe_path(root)
        from .operational_backup_selection import validate_backup_settings
        if settings is None:
            try:
                settings = load_json(root / "config/operations/backup.json", 4096)
            except SafetyError as error:
                if str(error) == "SAVED_FILE_UNAVAILABLE":
                    return _result("not_configured", reason="LOCAL_BACKUP_DESTINATION_NOT_CONFIGURED")
                raise
        validate_backup_settings(settings)
        if not settings["enabled"]:
            return _result("disabled", reason="LOCAL_BACKUP_DISABLED")
        if settings["schema_version"] == 2:
            return _run_due_multipart_backup(memory, root=root, at=at, pending_controls=pending_controls,
                creator=creator, completion_clock=lambda: now or utc_now())
        if (safe_path(memory.root) != safe_path(runtime_path(root, "data/research/captured"))
                or safe_path(memory.journal.root) != safe_path(runtime_path(root, "data/benchmarks/state/apify"))):
            raise SafetyError("LOCAL_BACKUP_STORAGE_BINDING_CHANGED")
        day = timestamp(at).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
        # Copies stay in the project folder even when working records are
        # relocated (config/local/runtime_storage.json); controls stay with them.
        destination = safe_path(root, "data", "backups")
        control_root = safe_path(runtime_path(root, "data/operations/backup_controls"))
        root_hash = digest(str(root))
        with Journal(control_root).locked():
            path = safe_path(control_root, "days") / (day + ".json")
            control = _load_control(path, root_hash=root_hash, day=day)
            if control is not None and control["state"] in TERMINAL:
                return _result(control["state"], day=day, reason=control["reason"], control=control, reused=True)
            if control is not None:
                # Recover the original ready receipt, not a second copy or a
                # full restore rehearsal. Its original verification stays dated.
                ready = safe_path(destination, control["backup_id"]) / "manifest.json"
                if not ready.exists():
                    return _result("waiting", day=day, reason="LOCAL_BACKUP_ATTEMPT_INCOMPLETE", control=control, reused=True)
                from .operational_backup import MAX_MANIFEST_BYTES
                manifest = load_json(ready, MAX_MANIFEST_BYTES)
                control = _finished(control, manifest, now or utc_now(), root=root)
                control["limitations"].append("Recovered the original ready-manifest receipt; this daily check did not reread every copied file.")
                _save_control(path, control)
                return _result(control["state"], day=day, control=control, reused=True)
            low = disk_space_reason(destination, root)
            if low is not None:
                return _result("waiting", day=day, reason=low)
            control = {"schema_version": 1, "kind": VERSION, "root_hash": root_hash, "day": day,
                "backup_id": "daily-" + day, "attempted_at": at, "completed_at": None, "manifest_created_at": None,
                "state": "attempt_started", "manifest_hash": None, "files": None, "bytes": None,
                "limitations": [], "reason": None}
            _save_control(path, control)  # Original intent must exist before copying.
            try:
                if creator is None:
                    from .operational_backup import create_essential_operational_backup
                    creator = create_essential_operational_backup
                saved = creator(memory, destination, backup_id=control["backup_id"], root=root,
                                pending_controls=pending_controls)
                control = _finished(control, saved["manifest"], now or utc_now(), root=root)
                try:
                    _save_control(path, control)
                except (SafetyError, OSError):
                    # The verified original ready manifest exists. Leave the
                    # initial attempt intent for metadata-only reconciliation,
                    # never replace it with a terminal copy-failed claim.
                    return _result("waiting", day=day, reason="LOCAL_BACKUP_CONTROL_SAVE_FAILED",
                        control=control, action_started=True)
                return _result(control["state"], day=day, control=control, action_started=True,
                               reused=saved.get("replayed") is True)
            except Exception as error:
                failed = dict(control, state="failed", reason=_reason(error), completed_at=now or utc_now())
                try:
                    _save_control(path, failed)
                except (SafetyError, OSError):
                    # The original started intent still stops repeated copying.
                    pass
                return _result("failed", day=day, reason=failed["reason"], control=failed, action_started=True)
    except (SafetyError, OSError, KeyError, TypeError, ValueError) as error:
        return _result("waiting", reason=_reason(error))


def _multipart_result(state, **kwargs):
    result = _result(state, **kwargs)
    # Keep the sealed control and its compatibility field unchanged. Unlike
    # the older format, multipart `files` counts selected descriptors, which
    # can include failed copies. A cached control cannot certify stored bytes.
    result.update(file_count_basis="planned_files", planned_files=result["files"])
    result["limitations"] = ["This project-local copy does not protect against loss or failure of the drive.",
        "Sign-in files are excluded; original-layout application resumption is not proved."]
    if state == "saved_with_limitations":
        result["limitations"].append("Some files could not be included; see the saved manifest for the exact reasons.")
    return result


def _run_due_multipart_backup(memory, *, root, at, pending_controls, creator, completion_clock):
    """Resume the original set before starting a different day's checkpoint."""
    from .operational_backup_parts import create_multipart_operational_backup, VERSION as PART_VERSION
    if (safe_path(memory.root) != safe_path(runtime_path(root, "data/research/captured"))
            or safe_path(memory.journal.root) != safe_path(runtime_path(root, "data/benchmarks/state/apify"))):
        raise SafetyError("LOCAL_BACKUP_STORAGE_BINDING_CHANGED")
    day = timestamp(at).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
    control_root = safe_path(runtime_path(root, "data/operations/backup_controls"))
    destination = safe_path(root, "data", "backups")  # project folder, not the relocated records
    path = control_root / "multipart-active.json"
    with Journal(control_root).locked():
        control = None
        if path.exists():
            wrapper = load_json(path, MAX_CONTROL_BYTES)
            if (not isinstance(wrapper, dict) or set(wrapper) != {"control", "hash"}
                    or wrapper["hash"] != digest(wrapper["control"])):
                raise SafetyError("LOCAL_BACKUP_CONTROL_INVALID")
            control = wrapper["control"]
            if (not isinstance(control, dict) or set(control) != {"contract", "root_hash", "day", "backup_id", "state", "attempted_at", "completed_at", "manifest_hash", "files", "bytes"}
                    or control["contract"] != PART_VERSION or control["root_hash"] != digest(str(root))
                    or control["state"] not in {"attempt_started", "saved", "saved_with_limitations"}
                    or control["backup_id"] != "daily-v3-" + control["day"]):
                raise SafetyError("LOCAL_BACKUP_CONTROL_INVALID")
            date.fromisoformat(control["day"]); timestamp(control["attempted_at"])
            if control["state"] != "attempt_started":
                timestamp(control["completed_at"])
                if (not isinstance(control["manifest_hash"], str) or len(control["manifest_hash"]) != 64
                        or any(type(control[field]) is not int or not 0 <= control[field] <= 9007199254740991 for field in ("files", "bytes"))):
                    raise SafetyError("LOCAL_BACKUP_CONTROL_INVALID")
                if control["day"] == day:
                    return _multipart_result(control["state"], day=day, control=control, reused=True)
        unfinished = control if control is not None and control["state"] == "attempt_started" else None
        # Never fill the project drive: below the configured free-space floor
        # no new copy starts and an unfinished one waits (research continues).
        low = disk_space_reason(destination, root)
        if low is not None:
            # Older copies beyond the kept numbers may still go, so space can
            # free itself (once a day, against the newest finished copy).
            tidy_when_disk_low(destination, root=root, control_root=control_root, now=at)
            return _multipart_result("waiting", day=unfinished["day"] if unfinished else day, reason=low,
                control=unfinished)
        if unfinished is None:
            control = {"contract": PART_VERSION, "root_hash": digest(str(root)), "day": day, "backup_id": "daily-v3-" + day,
                "state": "attempt_started", "attempted_at": at, "completed_at": None, "manifest_hash": None, "files": None, "bytes": None}
            _save_control(path, control)
        try:
            saved = (creator or create_multipart_operational_backup)(memory, destination,
                backup_id=control["backup_id"], root=root, pending_controls=pending_controls, max_parts=1)
            if saved["manifest"] is None:
                return _multipart_result("waiting", day=control["day"], reason="LOCAL_BACKUP_PARTS_IN_PROGRESS", control=control,
                    action_started=True, reused=saved.get("replayed") is True)
            manifest = saved["manifest"]
            # The creator already verified bytes and canonical replay. Bind its
            # immutable ready root, not another selection from today's state.
            from .operational_backup_parts import _validate_seal, _MANIFEST_FIELDS
            _validate_seal(manifest, fields=_MANIFEST_FIELDS, contract=PART_VERSION)
            if manifest["backup_id"] != control["backup_id"] or manifest["project_root"] != str(root):
                raise SafetyError("LOCAL_BACKUP_PROJECT_BINDING_CHANGED")
            limited = manifest["essential_files_complete"] is not True
            completed_at = completion_clock()
            timestamp(completed_at)
            control = dict(control, state="saved_with_limitations" if limited else "saved", completed_at=completed_at,
                manifest_hash=manifest["hash"], files=manifest["files"], bytes=manifest["bytes"])
            _save_control(path, control)
            result = _multipart_result(control["state"], day=control["day"], control=control, action_started=True,
                reused=saved.get("replayed") is True)
            # Only now, with the new copy verified and recorded, older copies
            # beyond the configured numbers may go. This never raises.
            tidy_after_backup(destination, root=root, control_root=control_root,
                              verified_backup_id=control["backup_id"], now=completed_at)
            return result
        except Exception as error:
            # Retain the exact pending set and cursor. The next existing task
            # turn may continue it; no repeated full copy and no lost old backup.
            return _multipart_result("waiting", day=control["day"], reason=_reason(error), control=control, action_started=True)


def create_backup_housekeeping_step(root):
    """Configure a local callback, without making a copy or loading any key."""
    root = Path(root)
    def step(memory, record, context):
        from .programme import _control_path, programme_service_state
        if not programme_service_state(memory)["enabled"]:
            return _result("owner_paused", reason="LOCAL_BACKUP_OWNER_PAUSED")
        # The last source stage may have taken minutes. Decide the India day
        # from the present time, not the earlier cycle's entry timestamp.
        result = run_due_operational_backup(memory, root=root,
            pending_controls=(_control_path(memory, record["programme_id"]),))
        # Once a day: drop unused lookup pages and very old raw run logs.
        # Its own small record; it never changes or fails the backup result.
        from .store_housekeeping import run_due_store_housekeeping
        run_due_store_housekeeping(memory, root=root)
        return result
    return step
