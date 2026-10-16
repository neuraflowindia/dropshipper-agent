"""Keep the daily backup copies on the project drive to a fixed number.

The daily copies (``data/backups/daily-v3-YYYY-MM-DD``) share one store of
content-addressed files (``data/backups/objects/v3``). After a new copy has
been saved and fully verified, this module keeps the newest ``keep_daily``
copies plus the newest copy of each of the latest ``keep_weekly`` calendar
weeks, removes the older copy folders, and then removes stored files that no
remaining copy (finished or still in progress) names any more.

Safety rules, in order:

* nothing is removed while a backup run holds its lock, or when the settings
  file is missing or invalid;
* the copy that was just verified must be the newest copy, and it is always
  kept; every kept copy's sealed records must read back correctly (a damaged
  or unreadable copy stops all removal and is reported, never removed);
* only folders and files directly inside the backups folder that have the
  exact backup layout are touched; anything unexpected is left in place;
* a copy folder is first renamed to ``pruning-<name>`` and then emptied, so
  an interruption never leaves a half-removed copy that looks real.

Every attempt is written to a small bounded history in the backup controls
folder. Backup housekeeping never stops research.
"""

from datetime import date
from pathlib import Path
import os
import re
import shutil
import time

from .models import SafetyError, digest, timestamp, utc_now
from .storage import Journal, atomic_json, load_json, safe_path


SETTINGS = "config/operations/durability.json"
SETTINGS_VERSION = "long-run-durability-v1"
DEFAULT_MINIMUM_FREE_GIB = 10
HISTORY_NAME = "retention-history.json"
HISTORY_CONTRACT = "backup-retention-history-v1"
MAX_HISTORY = 100
_DAILY = re.compile(r"daily-v3-([0-9]{4}-[0-9]{2}-[0-9]{2})\Z")
_TOMBSTONE = re.compile(r"pruning-daily-v3-[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_OBJECT = re.compile(r"[a-f0-9]{64}\.(json|bin)\Z")
_PART_FILE = re.compile(r"[0-9]{8}\.json\Z")
_TEMPORARY = re.compile(r"\.save-[A-Za-z0-9_]{1,64}\.tmp\Z")
_SET_FILES = {"pending.json", "cursor.json", "manifest.json"}
_STALE_TEMPORARY_SECONDS = 24 * 3600
_LIMITS = {"backup_keep_daily": (1, 366), "backup_keep_weekly": (0, 520), "backup_minimum_free_gib": (1, 100000),
           "run_log_keep_days": (7, 3660), "research_object_grace_hours": (1, 720)}


def validate_settings(value):
    """The checked-in long-run settings, or ``DURABILITY_SETTINGS_INVALID``."""
    if (not isinstance(value, dict) or set(value) != {"schema_version", "kind", *_LIMITS}
            or value["schema_version"] != 1 or value["kind"] != SETTINGS_VERSION
            or any(type(value[name]) is not int or not low <= value[name] <= high for name, (low, high) in _LIMITS.items())):
        raise SafetyError("DURABILITY_SETTINGS_INVALID")
    return dict(value)


def load_settings(root):
    """Read ``config/operations/durability.json`` from the project folder."""
    try:
        return validate_settings(load_json(safe_path(root) / SETTINGS, 4096))
    except SafetyError as error:
        if str(error) == "SAVED_FILE_UNAVAILABLE":
            raise SafetyError("DURABILITY_SETTINGS_MISSING") from None
        raise SafetyError("DURABILITY_SETTINGS_INVALID") from None


def minimum_free_bytes(root):
    """The free-space floor for backups; the built-in floor when unreadable."""
    try:
        gib = load_settings(root)["backup_minimum_free_gib"]
    except SafetyError:
        gib = DEFAULT_MINIMUM_FREE_GIB
    return gib * 1024 ** 3


def free_bytes(path):
    """Free bytes on the drive holding ``path`` (its nearest existing folder)."""
    path = Path(path).absolute()
    while not path.exists() and path.parent != path:
        path = path.parent
    return shutil.disk_usage(path).free


def disk_space_reason(destination, root):
    """``LOCAL_BACKUP_DISK_SPACE_LOW`` when a copy would eat into the floor.

    ``None`` when there is room, or when free space cannot be measured (the
    copy itself then fails or succeeds on its own; this check only prevents
    filling the drive).
    """
    try:
        return "LOCAL_BACKUP_DISK_SPACE_LOW" if free_bytes(destination) < minimum_free_bytes(root) else None
    except OSError:
        return None


def keep_days(days, *, keep_daily, keep_weekly):
    """Which backup days to keep: the newest ``keep_daily`` days plus the
    newest day of each of the latest ``keep_weekly`` calendar weeks."""
    ordered = sorted(set(days), reverse=True)
    if not ordered:
        return set()
    keep = set(ordered[:max(1, keep_daily)])
    newest_of_week = {}
    for day in ordered:
        newest_of_week.setdefault(day.isocalendar()[:2], day)
    for week in sorted(newest_of_week, reverse=True)[:keep_weekly]:
        keep.add(newest_of_week[week])
    return keep


class _References:
    """Stored-file names named by backup sets, walked once per shared page."""

    def __init__(self, destination, counts=None):
        self.destination = destination
        # ``counts`` (page name -> rows up to it) may be shared so a later
        # walk stops at pages an earlier walk already checked and recorded.
        self.live, self.required, self.counts = set(), set(), ({} if counts is None else counts)

    def chain(self, root, contract, rows):
        from .operational_backup_parts import MAX_PAGE_ROWS, _object_path, _object_read
        if (not isinstance(root, dict) or set(root) != {"last", "rows"}
                or type(root["rows"]) is not int or root["rows"] < 0):
            raise SafetyError("BACKUP_PART_PAGE_ROOT_INVALID")
        pages, seen, reference = [], set(), root["last"]
        while reference is not None:
            _object_path(self.destination, reference, json=True)
            name = reference["sha256"] + ".json"
            if name in self.counts:
                break
            if name in seen or len(pages) > root["rows"]:
                raise SafetyError("BACKUP_PART_PAGE_CYCLE")
            seen.add(name)
            page = _object_read(self.destination, reference, json=True)
            if (not isinstance(page, dict) or set(page) != {"contract", "previous", "rows"}
                    or page["contract"] != contract or not isinstance(page["rows"], list)
                    or not 1 <= len(page["rows"]) <= MAX_PAGE_ROWS):
                raise SafetyError("BACKUP_PART_PAGE_INVALID")
            pages.append((name, page)); reference = page["previous"]
        count = self.counts[reference["sha256"] + ".json"] if reference is not None else 0
        for name, page in reversed(pages):
            count += len(page["rows"]); self.counts[name] = count
            self.live.add(name); rows.extend(page["rows"])
        last = root["last"]
        if (self.counts[last["sha256"] + ".json"] if last is not None else 0) != root["rows"]:
            raise SafetyError("BACKUP_PART_PAGE_COUNT_CHANGED")

    def binary(self, sha, size, *, required=False):
        if not isinstance(sha, str) or re.fullmatch(r"[a-f0-9]{64}", sha) is None or type(size) is not int:
            raise SafetyError("BACKUP_PART_REFERENCE_INVALID")
        self.live.add(sha + ".bin")
        if required:
            self.required.add(sha + ".bin")

    def parts(self, reference, plan):
        """Walk a part chain; returns the parts newest first."""
        from .operational_backup_parts import _PART_FIELDS, _object_path, _object_read, _validate_seal
        chain, seen = [], set()
        while reference is not None:
            _object_path(self.destination, reference, json=True)
            name = reference["sha256"] + ".json"
            if name in seen:
                raise SafetyError("BACKUP_PART_CYCLE")
            seen.add(name)
            part = _validate_seal(_object_read(self.destination, reference, json=True),
                                  fields=_PART_FIELDS, contract="backup-part-v3")
            if part["plan_hash"] != plan["hash"]:
                raise SafetyError("BACKUP_PART_INVALID")
            self.live.add(name)
            outcomes = []
            self.chain(part["outcomes"], "backup-outcome-page-v3", outcomes)
            chain.append(part); reference = part["previous"]
        return chain


def _set_references(refs, folder, *, ready):
    """Record every stored file one backup set names; check a ready set.

    Raises a ``SafetyError`` when the set's sealed records do not read back.
    An unfinished folder without its plan yet names nothing.
    """
    from .operational_backup_parts import (_CURSOR_FIELDS, _MANIFEST_FIELDS, VERSION, _validate_descriptor,
                                           _validate_plan, _validate_seal)
    if not ready and not (folder / "pending.json").exists():
        return None
    plan = _validate_plan(load_json(folder / "pending.json", 65536), backup_id=folder.name)
    rows, selected = [], []
    refs.chain(plan["descriptors"], "backup-descriptor-page-v3", rows)
    for row in rows:
        _validate_descriptor(row)
        refs.binary(row["sha256"], row["bytes"], required=ready and row["role"] != "attachment")
    refs.chain(plan["selection"]["pages"], "backup-selection-page-v3", selected)
    for row in selected:
        value = row.get("value") if isinstance(row, dict) else None
        if isinstance(row, dict) and row.get("field") == "files" and isinstance(value, dict):
            refs.binary(value.get("expected_sha256"), value.get("bytes"))
    heads = []
    if (folder / "cursor.json").exists():
        cursor = _validate_seal(load_json(folder / "cursor.json", 16384), fields=_CURSOR_FIELDS,
                                contract="backup-part-cursor-v3")
        if cursor["plan_hash"] != plan["hash"]:
            raise SafetyError("BACKUP_PART_CURSOR_INVALID")
        heads.append(cursor["last_part"])
    parts = folder / "parts"
    if parts.is_dir() and not parts.is_symlink():
        for path in sorted(parts.iterdir()):
            if _PART_FILE.fullmatch(path.name) and not path.is_symlink():
                heads.append(load_json(path, 4096))
    for head in heads:
        refs.parts(head, plan)
    if not ready:
        return None
    manifest = _validate_seal(load_json(folder / "manifest.json", 65536), fields=_MANIFEST_FIELDS, contract=VERSION)
    chain = refs.parts(manifest["last_part"], plan)
    if (manifest["backup_id"] != folder.name or manifest["plan_hash"] != plan["hash"]
            or manifest["project_root"] != plan["project_root"] or len(chain) != manifest["parts"]
            or not chain or chain[0]["end"] != plan["files"]):
        raise SafetyError("BACKUP_PART_MANIFEST_INVALID")
    timestamp(manifest["completed_at"])
    return manifest


def _scan(destination):
    """Backup folders by kind: daily ready/in progress, other sets, leftovers."""
    daily, other_sets, tombstones = {}, [], []
    for entry in sorted(destination.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        match = _DAILY.fullmatch(entry.name)
        if _TOMBSTONE.fullmatch(entry.name):
            tombstones.append(entry)
        elif match:
            try:
                day = date.fromisoformat(match.group(1))
            except ValueError:
                other_sets.append(entry)
                continue
            daily[entry] = day
        elif (entry / "pending.json").exists():
            other_sets.append(entry)
    return daily, other_sets, tombstones


def _empty_set_folder(folder):
    """Remove one renamed copy folder, only if it has the exact layout."""
    def known(path, *, top):
        if path.is_symlink():
            return False
        if path.is_dir():
            return top and path.name == "parts"
        return (top and path.name in _SET_FILES) or _TEMPORARY.fullmatch(path.name) is not None or (
            not top and _PART_FILE.fullmatch(path.name) is not None)
    entries = list(folder.iterdir())
    inner = list((folder / "parts").iterdir()) if (folder / "parts").is_dir() and not (folder / "parts").is_symlink() else []
    if not all(known(path, top=True) for path in entries) or not all(known(path, top=False) for path in inner):
        return False
    for path in inner:
        os.unlink(path)
    for path in entries:
        if path.is_dir():
            os.rmdir(path)
        else:
            os.unlink(path)
    os.rmdir(folder)
    return True


def _sweep(objects, live, now):
    """Remove stored files no remaining set names, and stale temporary files.

    Only candidates are inspected further, so a large folder on a slow drive
    costs one listing, not one status read per kept file.
    """
    removed = size = 0
    if not objects.is_dir():
        return removed, size
    with os.scandir(objects) as entries:
        for entry in entries:
            unused = _OBJECT.fullmatch(entry.name) is not None and entry.name not in live
            temporary = _TEMPORARY.fullmatch(entry.name) is not None
            if not (unused or temporary) or not entry.is_file(follow_symlinks=False):
                continue
            stat = entry.stat(follow_symlinks=False)
            if temporary and stat.st_mtime >= now - _STALE_TEMPORARY_SECONDS:
                continue
            os.unlink(entry.path)
            removed += 1; size += stat.st_size
    return removed, size


def _object_names(objects):
    if not objects.is_dir():
        return set()
    with os.scandir(objects) as entries:
        return {entry.name for entry in entries if _OBJECT.fullmatch(entry.name)}


def prune_backups(destination, settings, *, verified_backup_id, now=None, trigger="after_backup"):
    """Apply the retention numbers once the newest copy is a verified one.

    ``verified_backup_id`` is the copy just saved and verified; ``None`` means
    the newest finished copy (a copy's ready record is written only after its
    full verification), as used when a full drive blocks new copies. Returns a
    record: state (``removed``, ``nothing_to_remove`` or ``kept`` with a
    reason), the copies kept and removed (``removed_damaged``: copies past
    retention that no longer read back), stored files and bytes removed, and
    free space before and after.
    """
    settings = validate_settings(settings)
    destination = safe_path(destination)
    at = now or utc_now(); timestamp(at)
    record = {"at": at, "trigger": trigger, "state": "kept", "reason": None, "verified_backup_id": verified_backup_id,
              "kept": [], "removed": [], "removed_damaged": [], "removed_objects": 0, "removed_bytes": 0,
              "free_bytes_before": None, "free_bytes_after": None}
    try:
        record["free_bytes_before"] = free_bytes(destination)
    except OSError:
        pass
    try:
        if not destination.is_dir():
            raise SafetyError("BACKUP_PRUNE_NEWEST_NOT_VERIFIED")  # nothing saved here yet; create nothing
        with Journal(destination / "multipart-control").locked():
            _prune_locked(destination, settings, verified_backup_id, record)
    except SafetyError as error:
        record.update(state="kept", reason=str(error) if str(error) != "RUNNER_ALREADY_ACTIVE"
                      else "BACKUP_PRUNE_BACKUP_RUNNING")
    try:
        record["free_bytes_after"] = free_bytes(destination)
    except OSError:
        pass
    return record


def _complete(folder):
    """Whether a finished copy says all its essential files were saved."""
    from .operational_backup_parts import _MANIFEST_FIELDS, VERSION, _validate_seal
    try:
        manifest = _validate_seal(load_json(folder / "manifest.json", 65536), fields=_MANIFEST_FIELDS, contract=VERSION)
        return manifest["essential_files_complete"] is True and manifest["coverage_state"] == "essential_files_verified"
    except SafetyError:
        return False


def _keep(ready, settings):
    """The days kept: 14 daily and 8 weekly, plus the newest complete copy."""
    keep = keep_days(ready.values(), keep_daily=settings["backup_keep_daily"], keep_weekly=settings["backup_keep_weekly"])
    complete = [day for folder, day in ready.items() if _complete(folder)]
    if complete:
        keep.add(max(complete))
    return keep


def _prune_locked(destination, settings, verified_backup_id, record):
    daily, other_sets, tombstones = _scan(destination)
    ready = {folder: day for folder, day in daily.items() if (folder / "manifest.json").exists()}
    newest = max(ready, key=lambda folder: (ready[folder], folder.name)) if ready else None
    if newest is None or verified_backup_id is not None and newest.name != verified_backup_id:
        raise SafetyError("BACKUP_PRUNE_NEWEST_NOT_VERIFIED")
    record["verified_backup_id"] = newest.name
    keep = _keep(ready, settings)
    kept = sorted((folder for folder in ready if ready[folder] in keep), key=lambda folder: ready[folder], reverse=True)
    doomed = sorted(folder for folder in ready if ready[folder] not in keep)
    refs = _References(destination)
    # Newest copy first: shared pages are then checked as part of it. Every
    # kept or unfinished copy must read back, or nothing at all is removed.
    for folder in kept:
        _set_references(refs, folder, ready=True)
    for folder in sorted(set(daily) - set(ready)) + other_sets:
        _set_references(refs, folder, ready=(folder / "manifest.json").exists())
    damaged = []
    for folder in doomed:
        # Their own files are not kept: only pages shared with kept copies are
        # skipped. A copy past retention that no longer reads back cannot be
        # restored anyway, so it is removed too (and named in the record).
        try:
            _set_references(_References(destination, counts=refs.counts), folder, ready=True)
        except (SafetyError, OSError, KeyError, TypeError, ValueError):
            damaged.append(folder.name)
    missing = refs.required - _object_names(destination / "objects" / "v3")
    if missing:
        raise SafetyError("BACKUP_PRUNE_KEPT_SET_DAMAGED")
    record["kept"] = [folder.name for folder in kept]
    leftovers = []
    for folder in doomed:
        tombstone = destination / ("pruning-" + folder.name)
        os.rename(folder, tombstone)
        tombstones.append(tombstone)
        record["removed_damaged" if folder.name in damaged else "removed"].append(folder.name)
    for tombstone in tombstones:
        if not _empty_set_folder(tombstone):
            leftovers.append(tombstone.name)
    removed, size = _sweep(destination / "objects" / "v3", refs.live, time.time())
    record.update(removed_objects=removed, removed_bytes=size,
                  state="removed" if record["removed"] or record["removed_damaged"] or removed else "nothing_to_remove",
                  reason="BACKUP_PRUNE_UNKNOWN_FILES_KEPT" if leftovers else None)


def save_history(control_root, record):
    """Keep the latest ``MAX_HISTORY`` retention records (sealed)."""
    path = safe_path(control_root) / HISTORY_NAME
    records = []
    if path.exists():
        try:
            wrapper = load_json(path, 4 * 1024 * 1024)
            if (isinstance(wrapper, dict) and set(wrapper) == {"value", "hash"} and digest(wrapper["value"]) == wrapper["hash"]
                    and isinstance(wrapper["value"], dict) and wrapper["value"].get("contract") == HISTORY_CONTRACT
                    and isinstance(wrapper["value"].get("records"), list)):
                records = wrapper["value"]["records"]
        except SafetyError:
            records = []  # a damaged history is replaced; it never authorises anything
    value = {"contract": HISTORY_CONTRACT, "records": (records + [record])[-MAX_HISTORY:]}
    atomic_json(path, {"value": value, "hash": digest(value)})
    return value


def _failed_record(at, trigger, code, verified_backup_id=None):
    return {"at": at, "trigger": trigger, "state": "failed", "reason": code, "verified_backup_id": verified_backup_id,
            "kept": [], "removed": [], "removed_damaged": [], "removed_objects": 0, "removed_bytes": 0,
            "free_bytes_before": None, "free_bytes_after": None}


def _tidy(destination, *, root, control_root, verified_backup_id, now, trigger, disk_low=False):
    """One retention attempt, recorded in the history and the run log."""
    from .operations import record_event
    at = now or utc_now()
    try:
        settings = load_settings(root)
    except Exception as error:  # no valid settings: nothing is removed
        code = str(error) if isinstance(error, SafetyError) else "DURABILITY_SETTINGS_INVALID"
        record = dict(_failed_record(at, trigger, code), state="kept")
        if code == "DURABILITY_SETTINGS_MISSING":
            # Not configured (like backups without their settings): no record.
            record_event("backup_retention_outcome", stage="backup", state="kept", reason=code, useful_result=False)
            return record
    else:
        try:
            record = prune_backups(destination, settings, verified_backup_id=verified_backup_id, now=at, trigger=trigger)
        except Exception as error:  # housekeeping must never stop research
            record = _failed_record(at, trigger, str(error) if isinstance(error, SafetyError) else "BACKUP_PRUNE_FAILED",
                                    verified_backup_id)
    record["disk_low"] = disk_low
    if disk_low:
        record["minimum_free_bytes"] = minimum_free_bytes(root)
    try:
        save_history(control_root, record)
    except (SafetyError, OSError):
        pass
    record_event("backup_retention_outcome", stage="backup", state=record["state"], reason=record["reason"],
                 useful_result=False, counts={"kept_copies": len(record["kept"]),
                     "removed_copies": len(record["removed"]) + len(record["removed_damaged"]),
                     "removed_files": record["removed_objects"], "removed_bytes": record["removed_bytes"]})
    return record


def tidy_after_backup(destination, *, root, control_root, verified_backup_id, now=None):
    """The daily retention step after a saved backup; never raises."""
    return _tidy(destination, root=root, control_root=control_root, verified_backup_id=verified_backup_id,
                 now=now, trigger="after_backup")


def tidy_when_disk_low(destination, *, root, control_root, now=None):
    """While a full drive blocks new copies: tidy against the newest finished
    copy, at most once per India day, so space can free itself. Never raises."""
    from zoneinfo import ZoneInfo
    try:
        at = now or utc_now()
        day = timestamp(at).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
        latest = latest_record(control_root)
        if (latest is not None and latest.get("trigger") == "low_disk" and isinstance(latest.get("at"), str)
                and timestamp(latest["at"]).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat() == day):
            return None
    except Exception:
        return None
    return _tidy(destination, root=root, control_root=control_root, verified_backup_id=None, now=at,
                 trigger="low_disk", disk_low=True)


def latest_record(control_root):
    """The newest saved retention record, or ``None`` (none or unreadable)."""
    path = safe_path(control_root) / HISTORY_NAME
    if not path.exists():
        return None
    try:
        wrapper = load_json(path, 4 * 1024 * 1024)
    except SafetyError:
        return None
    if (not isinstance(wrapper, dict) or set(wrapper) != {"value", "hash"} or digest(wrapper["value"]) != wrapper["hash"]
            or not isinstance(wrapper["value"], dict) or not isinstance(wrapper["value"].get("records"), list)
            or not wrapper["value"]["records"] or not isinstance(wrapper["value"]["records"][-1], dict)):
        return None
    return wrapper["value"]["records"][-1]
