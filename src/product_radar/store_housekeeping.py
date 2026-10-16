"""Once-a-day tidying of the working-records folder; never research authority.

Called after the daily backup step at the end of a research cycle. On the
first call of each India day it

* removes research lookup pages no published checkpoint uses any more
  (``research_object_prune``), and
* moves the totals of raw run logs untouched for ``run_log_keep_days`` into
  the kept retired summary and removes those logs (``run_log_summary``).

Settings come from ``config/operations/durability.json``; without valid
settings nothing is removed. The day's outcome is saved in one small sealed
file (``data/operations/housekeeping/store-day.json``) and in the run log.
Any failure is recorded and research continues; nothing here contacts a
website, provider or model.
"""

from zoneinfo import ZoneInfo

from .models import SafetyError, digest, timestamp, utc_now
from .runtime_storage import runtime_path
from .storage import atomic_json, load_json, safe_path


CONTRACT = "store-housekeeping-day-v1"
CONTROL = "data/operations/housekeeping/store-day.json"


def _code(error, fallback):
    value = str(error) if isinstance(error, SafetyError) else fallback
    return value if value.isascii() and value.replace("_", "").isalnum() and value.isupper() and len(value) <= 160 else fallback


def _done_today(path, day):
    try:
        wrapper = load_json(path, 65536)
        return (isinstance(wrapper, dict) and set(wrapper) == {"value", "hash"} and digest(wrapper["value"]) == wrapper["hash"]
                and isinstance(wrapper["value"], dict) and wrapper["value"].get("contract") == CONTRACT
                and wrapper["value"].get("day") == day)
    except SafetyError:
        return False


def _research_objects(memory, settings, now_seconds):
    from .research_object_prune import prune_research_objects
    try:
        return prune_research_objects(memory, grace_seconds=settings["research_object_grace_hours"] * 3600,
                                      now=now_seconds)
    except Exception as error:  # housekeeping must never stop research
        return {"state": "failed", "reason": _code(error, "RESEARCH_PRUNE_FAILED")}


def _run_logs(root, settings, at):
    from .run_log_summary import retire_old_run_logs
    try:
        from .programme import load_meter_baseline
        try:
            since = load_meter_baseline(root)["started_at"]
        except SafetyError as error:
            if str(error) == "SAVED_FILE_UNAVAILABLE":
                # Without a meter start there is no reading to keep totals
                # for; keep every raw log rather than guess one.
                return {"state": "kept", "reason": "RUN_LOG_RETENTION_NO_METER_START"}
            raise
        return retire_old_run_logs(root, since=since, keep_days=settings["run_log_keep_days"], now=at)
    except Exception as error:
        return {"state": "failed", "reason": _code(error, "RUN_LOG_RETENTION_FAILED")}


def run_due_store_housekeeping(memory, *, root, now=None):
    """The day's tidying, once per India day; returns its record, never raises."""
    from .backup_retention import load_settings
    from .operations import record_event
    try:
        at = now or utc_now()
        day = timestamp(at).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
        path = safe_path(runtime_path(root, CONTROL))
        if _done_today(path, day):
            return None
        try:
            settings = load_settings(root)
        except SafetyError as error:
            if str(error) == "DURABILITY_SETTINGS_MISSING":
                return None  # not configured: nothing is removed
            raise
        value = {"contract": CONTRACT, "day": day, "at": at,
                 "research_objects": _research_objects(memory, settings, timestamp(at).timestamp()),
                 "run_logs": _run_logs(root, settings, at)}
        atomic_json(path, {"value": value, "hash": digest(value)})
    except Exception as error:
        code = _code(error, "STORE_HOUSEKEEPING_FAILED")
        record_event("store_housekeeping_outcome", state="failed", reason=code, useful_result=False)
        try:  # keep the failure visible in the owner status line
            failed = {"state": "failed", "reason": code}
            value = {"contract": CONTRACT, "day": day, "at": at, "research_objects": failed, "run_logs": failed}
            atomic_json(path, {"value": value, "hash": digest(value)})
        except Exception:
            pass
        return {"state": "failed", "reason": code}
    for name in ("research_objects", "run_logs"):
        part = value[name]
        counts = {key: part[key] for key in ("removed_objects", "removed_bytes", "live_objects", "removed_files")
                  if type(part.get(key)) is int}
        record_event("store_housekeeping_outcome", phase=name, state=part.get("state"), reason=part.get("reason"),
                     useful_result=False, counts=counts)
    return value


# Plain words for the owner when tidying cannot go ahead. Routine outcomes
# (busy for a moment, nothing to do, no meter start yet) show nothing.
_ROUTINE = {None, "BACKUP_PRUNE_BACKUP_RUNNING", "RESEARCH_CHECKPOINT_CHANGED", "RUNNER_ALREADY_ACTIVE",
            "RUN_LOG_RETENTION_NO_METER_START", "RUN_LOG_RETIRED_SUMMARY_RESEALED"}
_PLAIN = {
    "BACKUP_PRUNE_KEPT_SET_DAMAGED": "a kept backup copy is damaged",
    "BACKUP_PRUNE_NEWEST_NOT_VERIFIED": "the newest backup copy is not one the program verified",
    "BACKUP_PRUNE_UNKNOWN_FILES_KEPT": "an old backup folder holds files the program did not make",
    "BACKUP_PRUNE_FAILED": "it stopped with an error",
    "DURABILITY_SETTINGS_INVALID": "its settings file (config/operations/durability.json) is not valid",
    "RUN_LOG_RETIRED_SUMMARY_INVALID": "the kept totals of old run logs are damaged",
    "RUN_LOG_RETIRED_SUMMARY_START_CHANGED": "the kept totals of old run logs belong to an earlier meter start",
    "RESEARCH_PRUNE_FAILED": "it stopped with an error",
    "RUN_LOG_RETENTION_FAILED": "it stopped with an error",
    "STORE_HOUSEKEEPING_FAILED": "it stopped with an error",
}


def _plain(code):
    return _PLAIN.get(code, "code " + str(code))


def _problem(part):
    """The reason of a tidying outcome that needs the owner, or ``None``."""
    if not isinstance(part, dict):
        return None
    reason = part.get("reason")
    if part.get("state") == "failed":
        return reason or "STORE_HOUSEKEEPING_FAILED"
    if part.get("state") == "kept" and reason not in _ROUTINE:
        return reason
    return None


def housekeeping_line(root):
    """One short plain line when backup or store tidying is stuck, else ``None``.

    Read from the latest backup clean-up record, the latest daily store
    tidying record and the latest saved-read check (``saved_read_sweep``:
    pages lost to a power cut twice, or changed on disk) only; nothing is
    changed.
    """
    from .backup_retention import latest_record
    parts = []
    backup = latest_record(safe_path(runtime_path(root, "data/operations/backup_controls")))
    if isinstance(backup, dict) and backup.get("disk_low") is True:
        free, floor = backup.get("free_bytes_after"), backup.get("minimum_free_bytes")
        if type(free) is int and type(floor) is int:
            parts.append(f"Daily backups are paused: the project drive has {free / 1024 ** 3:.1f} GB free "
                         f"and needs at least {floor / 1024 ** 3:.0f} GB. Research continues.")
        else:
            parts.append("Daily backups are paused: the project drive is nearly full. Research continues.")
        problem = _problem(backup) if backup.get("reason") != "BACKUP_PRUNE_NEWEST_NOT_VERIFIED" else None
    else:
        problem = _problem(backup)
    if problem:
        parts.append(f"Backup clean-up is paused: {_plain(problem)}.")
    try:
        wrapper = load_json(safe_path(runtime_path(root, CONTROL)), 65536)
        store = wrapper["value"] if isinstance(wrapper, dict) and digest(wrapper.get("value")) == wrapper.get("hash") else None
    except (SafetyError, KeyError, TypeError):
        store = None
    if isinstance(store, dict):
        for name, label in (("research_objects", "Research page clean-up"), ("run_logs", "Old run-log clean-up")):
            problem = _problem(store.get(name))
            if problem:
                parts.append(f"{label} is paused: {_plain(problem)}.")
    from .saved_read_sweep import sweep_line
    lost = sweep_line(root)
    if lost:
        parts.append(lost)
    return " ".join(parts) or None
