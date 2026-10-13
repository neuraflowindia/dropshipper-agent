"""Explicit local reader-first adoption; no source, model, spending or release work.

Preview inventories files without creating a Journal or a lock. Execution uses
the existing programme writer, real migration functions and fresh readers. A
failed store stays named separately; no rollback deletes originals or resets
money. The small last-operation receipt is diagnostic, never storage authority.
"""

from hashlib import sha256
import stat
from datetime import date

from .models import SafetyError, canonical, digest, timestamp, utc_now
from .runtime_storage import runtime_path
from .storage import Journal, atomic_json, load_json, safe_path

VERSION = "local-storage-adoption-v1"
RESEARCH = "data/research/captured"
FINANCE = "data/benchmarks/state/apify"
SETTINGS = "config/operations/backup.json"
RECEIPT = "data/operations/storage-adoption.json"
ZERO = "0" * 64


def _reason(error):
    value = str(error) if isinstance(error, SafetyError) else "LOCAL_STORAGE_FILE_UNAVAILABLE"
    return value if value.isascii() and value.isupper() and value.replace("_", "").isalnum() and len(value) <= 160 else "LOCAL_STORAGE_ADOPTION_FAILED"


def _fingerprint(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise SafetyError("LOCAL_STORAGE_ORIGINAL_NOT_REGULAR")
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _file(path, *, contents=False):
    safe_path(path)
    if not path.exists():
        return None
    before = _fingerprint(path)
    result = {"bytes": before[2], "fingerprint": before}
    if contents:
        hashed = sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hashed.update(chunk)
        if _fingerprint(path) != before:
            raise SafetyError("LOCAL_STORAGE_ORIGINAL_CHANGED")
        result["sha256"] = hashed.hexdigest()
    return result


def _research_originals(root, *, contents=False):
    directory = safe_path(runtime_path(root, RESEARCH) / "events")
    if not directory.exists():
        return {"present": False, "files": 0, "bytes": 0, "manifest_hash": digest([])}
    if not directory.is_dir():
        raise SafetyError("LOCAL_STORAGE_RESEARCH_PATH_INVALID")
    before = sorted(path.name for path in directory.iterdir() if path.name.endswith(".json"))
    hashed, total = sha256(), 0
    for name in before:
        info = _file(directory / name, contents=contents)
        if info is None:
            raise SafetyError("LOCAL_STORAGE_ORIGINAL_CHANGED")
        hashed.update(canonical([name, info]) + b"\n"); total += info["bytes"]
    if sorted(path.name for path in directory.iterdir() if path.name.endswith(".json")) != before:
        raise SafetyError("LOCAL_STORAGE_ORIGINAL_CHANGED")
    return {"present": True, "files": len(before), "bytes": total, "manifest_hash": hashed.hexdigest()}


def preview_local_storage_adoption(root):
    """Actual read-only inventory, not an unearned canonical verification claim."""
    root = safe_path(root)
    from .financial_segments import POINTER as FINANCE_POINTER, VERSION as FINANCE_VERSION
    from .research_segments import POINTER as RESEARCH_POINTER, MARKER, VERSION as RESEARCH_VERSION
    from .operational_backup_selection import validate_backup_settings
    stores = {}
    for name, inspect in (
        ("research", lambda: {"originals": _research_originals(root),
            "pointer": _file(runtime_path(root, RESEARCH) / RESEARCH_POINTER), "marker": _file(runtime_path(root, RESEARCH) / MARKER),
            "target_contract": RESEARCH_VERSION}),
        ("finance", lambda: {"originals": _file(runtime_path(root, FINANCE) / "events.jsonl"),
            "pointer": _file(runtime_path(root, FINANCE) / FINANCE_POINTER), "target_contract": FINANCE_VERSION}),
    ):
        try: stores[name] = dict(inspect(), state="inspected", canonical_verified=False)
        except (SafetyError, OSError) as error:
            stores[name] = {"state": "waiting", "reason": _reason(error), "canonical_verified": False}
    path = safe_path(root / SETTINGS)
    try:
        settings = validate_backup_settings(load_json(path, 4096)) if path.exists() else None
        backup = {"state": "inspected" if settings else "not_configured", "settings": settings,
            "original": _file(path, contents=True), "target_schema_version": 2,
            "enabled_will_be_preserved": True, "backup_copy_verified": False}
    except (SafetyError, OSError) as error:
        backup = {"state": "waiting", "reason": _reason(error), "backup_copy_verified": False}
    plan = {"contract": VERSION, "project_root_hash": digest(str(root)), "stores": stores, "backup": backup,
        "actions": ["Verify and adopt research pages, retaining original events.",
            "Verify and adopt finance segments, retaining original money and attempt records.",
            "Only with a separate explicit option, preserve backup enablement and adopt multipart settings."],
        "canonical_verification": "Execution uses actual migrations and fresh readers; this preview only inventories files."}
    return {"state": "preview", "execute": False, "plan": plan, "plan_hash": digest(plan),
        "provider_calls": 0, "model_calls": 0, "publication_attempted": False, "files_written": False}


def _research(memory, root):
    from .research_memory import ResearchMemory
    from .research_segments import enabled, POINTER
    originals = _research_originals(root, contents=True)
    if not originals["present"]:
        raise SafetyError("LOCAL_STORAGE_RESEARCH_ORIGINALS_MISSING")
    before = memory.snapshot_with_provenance()
    error, result = None, None
    try: result = memory.migrate_segments()
    except (SafetyError, OSError) as caught: error = _reason(caught)
    # Reconcile the authoritative pointer first, including a lost publish reply.
    if not enabled(memory):
        raise SafetyError(error or "LOCAL_STORAGE_RESEARCH_POINTER_MISSING")
    cold = ResearchMemory(memory.root, Journal(memory.journal.root), evidence_type=memory.evidence_type)
    actual = cold.snapshot_with_provenance()
    if (actual != before or cold.last_checkpoint_diagnostic is not None
            or getattr(cold, "_research_pages_verified", None) is None
            or _research_originals(root, contents=True) != originals):
        raise SafetyError("LOCAL_STORAGE_RESEARCH_READBACK_FAILED")
    return {"state": "already_adopted" if result and result["reused"] else "adopted", "verified": True,
        "head": actual["head"], "event_count": actual["event_count"], "state_hash": digest(actual["state"]),
        "originals": originals, "originals_preserved": True, "cold_readback_verified": True,
        "pointer_hash": digest(load_json(memory.root / POINTER, 16384)),
        "recovered_committed_reply": error is not None, "operation_diagnostic": error}


def _financial_baseline(journal):
    from . import financial_segments as segments
    if segments.enabled(journal):
        events = journal.events(); state = journal.state()
    else:
        # The migration's existing bounded-line reader supports a legacy file
        # beyond the old 8 MiB limit; do not route it through that old limit.
        events, ids, head = [], set(), ZERO
        segments._legacy(journal, events=events)
        for sequence, event in enumerate(events, 1):
            head = segments._event(event, sequence=sequence, head=head, ids=ids); ids.add(event["event_id"])
        state = journal._reduce(events)
    return {"head": events[-1]["hash"] if events else ZERO, "event_count": len(events), "state_hash": digest(state)}


def _finance(journal):
    from .financial_segments import enabled, POINTER
    if not journal.path.exists() and not enabled(journal):
        raise SafetyError("LOCAL_STORAGE_FINANCE_ORIGINALS_MISSING")
    error, result = None, None
    with journal.locked(timeout=5):
        originals = _file(journal.path, contents=True)
        before = _financial_baseline(journal)
        try: result = journal.migrate_segments()
        except (SafetyError, OSError) as caught: error = _reason(caught)
    if not enabled(journal):
        raise SafetyError(error or "LOCAL_STORAGE_FINANCE_POINTER_MISSING")
    cold = Journal(journal.root)
    with cold.locked(timeout=5):
        actual = _financial_baseline(cold)
        if actual != before or _file(cold.path, contents=True) != originals:
            raise SafetyError("LOCAL_STORAGE_FINANCE_READBACK_FAILED")
        pointer_hash = digest(load_json(cold.root / POINTER, 16384))
    return {"state": "already_adopted" if result and result["reused"] else "adopted", "verified": True,
        **actual, "originals": originals, "originals_preserved": True, "cold_readback_verified": True,
        "pointer_hash": pointer_hash, "recovered_committed_reply": error is not None, "operation_diagnostic": error}


def _backup(root, *, requested, stores_verified):
    if not requested: return {"state": "not_requested", "changed": False, "backup_copy_verified": False}
    if not stores_verified:
        return {"state": "waiting", "reason": "LOCAL_STORAGE_BACKUP_REQUIRES_VERIFIED_STORES", "changed": False}
    if not safe_path(root / SETTINGS).exists():
        return {"state": "not_configured", "changed": False, "backup_copy_verified": False}
    # Match the ordinary backup scheduler's lock order: programme writer, then
    # backup controls. A manual backup cannot start a v1 intent between our
    # pending-intent inspection and the explicit schema2 setting change.
    with Journal(safe_path(runtime_path(root, "data/operations/backup_controls"))).locked(timeout=5):
        try:
            return _backup_locked(root)
        except (ValueError, TypeError, KeyError):
            raise SafetyError("LOCAL_BACKUP_CONTROL_INVALID") from None


def _backup_locked(root):
    from .operational_backup_selection import validate_backup_settings
    from .operational_backup_schedule import _load_control, MAX_CONTROL_BYTES
    from .operational_backup_parts import VERSION as BACKUP_VERSION
    path = safe_path(root / SETTINGS)
    if not path.exists(): return {"state": "not_configured", "changed": False, "backup_copy_verified": False}
    original = _file(path, contents=True)
    settings = validate_backup_settings(load_json(path, 4096))
    # An unfinished v1 copy must retain its original continuation route rather
    # than silently becoming an abandoned older-schema attempt.
    days = safe_path(runtime_path(root, "data/operations/backup_controls/days"))
    pending = []
    if days.exists():
        for control_path in sorted(days.iterdir()):
            if control_path.suffix != ".json": continue
            control = _load_control(safe_path(control_path), root_hash=digest(str(root)), day=control_path.stem)
            if control is not None and control["state"] == "attempt_started": pending.append(control["backup_id"])
    if pending and settings["schema_version"] == 1:
        return {"state": "waiting", "reason": "LOCAL_STORAGE_BACKUP_LEGACY_ATTEMPT_PENDING", "changed": False,
            "pending_attempt_count": len(pending), "pending_attempt_ids": pending[:20], "backup_copy_verified": False}
    multipart = safe_path(runtime_path(root, "data/operations/backup_controls/multipart-active.json"))
    if multipart.exists():
        wrapper = load_json(multipart, MAX_CONTROL_BYTES)
        if (not isinstance(wrapper, dict) or set(wrapper) != {"control", "hash"}
                or wrapper["hash"] != digest(wrapper["control"]) or not isinstance(wrapper["control"], dict)
                or wrapper["control"].get("contract") != BACKUP_VERSION
                or wrapper["control"].get("root_hash") != digest(str(root))):
            raise SafetyError("LOCAL_BACKUP_CONTROL_INVALID")
        control = wrapper["control"]
        if (set(control) != {"contract", "root_hash", "day", "backup_id", "state", "attempted_at", "completed_at", "manifest_hash", "files", "bytes"}
                or control["state"] not in {"attempt_started", "saved", "saved_with_limitations"}
                or not isinstance(control["day"], str) or control["backup_id"] != "daily-v3-" + control["day"]):
            raise SafetyError("LOCAL_BACKUP_CONTROL_INVALID")
        date.fromisoformat(control["day"]); timestamp(control["attempted_at"])
        if control["state"] != "attempt_started":
            timestamp(control["completed_at"])
            if (not isinstance(control["manifest_hash"], str) or len(control["manifest_hash"]) != 64
                    or any(type(control[field]) is not int or not 0 <= control[field] <= 9007199254740991 for field in ("files", "bytes"))):
                raise SafetyError("LOCAL_BACKUP_CONTROL_INVALID")
    if settings["schema_version"] == 2:
        return {"state": "already_adopted", "changed": False, "settings": settings, "backup_copy_verified": False}
    updated = validate_backup_settings(dict(settings, schema_version=2, backup_contract=BACKUP_VERSION))
    if _file(path, contents=True) != original:
        raise SafetyError("LOCAL_STORAGE_BACKUP_SETTINGS_CHANGED")
    diagnostic = None
    try: atomic_json(path, updated)
    except (SafetyError, OSError) as error: diagnostic = _reason(error)
    if load_json(path, 4096) != updated:
        raise SafetyError(diagnostic or "LOCAL_STORAGE_BACKUP_READBACK_FAILED")
    return {"state": "adopted", "changed": True, "settings": updated, "previous_settings": settings,
        "enabled_preserved": updated["enabled"] == settings["enabled"], "backup_copy_verified": False,
        "recovered_committed_reply": diagnostic is not None, "operation_diagnostic": diagnostic}


def adopt_local_storage(root, *, execute=False, adopt_backup_settings=False, expected_plan_hash=None,
                        memory=None, at=None):
    """Preview by default; explicit execution enters the same programme guard."""
    if type(execute) is not bool or type(adopt_backup_settings) is not bool:
        raise SafetyError("LOCAL_STORAGE_ADOPTION_OPTIONS_INVALID")
    if adopt_backup_settings and not execute:
        raise SafetyError("LOCAL_STORAGE_BACKUP_EXECUTION_REQUIRED")
    root = safe_path(root); preview = preview_local_storage_adoption(root)
    if not execute: return preview
    now = at or utc_now(); timestamp(now)
    if expected_plan_hash is not None and expected_plan_hash != preview["plan_hash"]:
        raise SafetyError("LOCAL_STORAGE_ADOPTION_INPUT_CHANGED")
    if not (runtime_path(root, RESEARCH)).is_dir() or not (runtime_path(root, FINANCE)).is_dir():
        raise SafetyError("LOCAL_STORAGE_PROJECT_STORES_MISSING")
    from .research_memory import ResearchMemory
    if memory is None:
        memory = ResearchMemory(runtime_path(root, RESEARCH), Journal(runtime_path(root, FINANCE)), evidence_type="captured")
    if safe_path(memory.root) != safe_path(runtime_path(root, RESEARCH)) or safe_path(memory.journal.root) != safe_path(runtime_path(root, FINANCE)):
        raise SafetyError("LOCAL_STORAGE_PROJECT_BINDING_CHANGED")
    with Journal(safe_path(memory.root / "programme_writer")).locked():
        current = preview_local_storage_adoption(root)
        if current["plan_hash"] != preview["plan_hash"]:
            raise SafetyError("LOCAL_STORAGE_ADOPTION_INPUT_CHANGED")
        results = {}
        for name, operation in (("research", lambda: _research(memory, root)), ("finance", lambda: _finance(memory.journal))):
            try: results[name] = operation()
            except (SafetyError, OSError) as error:
                results[name] = {"state": "waiting", "reason": _reason(error), "verified": False,
                    "cold_readback_verified": False, "originals_preserved": None}
        stores_verified = all(row["verified"] for row in results.values())
        try: backup = _backup(root, requested=adopt_backup_settings, stores_verified=stores_verified)
        except (SafetyError, OSError, ValueError, TypeError, KeyError) as error:
            backup = {"state": "waiting", "reason": _reason(error), "changed": False, "backup_copy_verified": False}
        complete = stores_verified and backup["state"] in {"not_requested", "adopted", "already_adopted"}
        receipt = {"contract": VERSION, "at": now, "project_root_hash": digest(str(root)),
            "plan_hash": current["plan_hash"], "state": "complete" if complete else "partial", "execute": True,
            "contracts": {name: current["plan"]["stores"].get(name, {}).get("target_contract") for name in ("research", "finance")},
            "stores": results, "backup": backup, "provider_calls": 0, "model_calls": 0,
            "publication_attempted": False, "independent_backup_proved": False}
        receipt["hash"] = digest(receipt)
        receipt_diagnostic = None
        try:
            atomic_json(safe_path(runtime_path(root, RECEIPT)), receipt)
        except (SafetyError, OSError) as error:
            receipt_diagnostic = _reason(error)
        try:
            if load_json(runtime_path(root, RECEIPT), 65536) != receipt:
                raise SafetyError("LOCAL_STORAGE_ADOPTION_RECEIPT_READBACK_FAILED")
            return dict(receipt, receipt_saved=True, receipt_path=RECEIPT,
                receipt_recovered_committed_reply=receipt_diagnostic is not None,
                receipt_operation_diagnostic=receipt_diagnostic)
        except (SafetyError, OSError) as error:
            return dict(receipt, receipt_saved=False, receipt_reason=receipt_diagnostic or _reason(error), receipt_path=RECEIPT)
