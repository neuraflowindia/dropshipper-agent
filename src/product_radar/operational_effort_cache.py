"""Bounded owner-only model measurements, not billing or execution authority.

The single programme writer refreshes at most ``job_limit`` jobs. Status reads
open only this derivative checkpoint and use the supplied verified job state;
they do not reopen model receipts, replay research, or read financial records.
Each finite sweep freezes its job IDs, so later admissions cannot indefinitely
push an earlier job back. Original request/receipt bytes are never modified.
"""

from collections import Counter
from copy import deepcopy
from datetime import timedelta
from math import isfinite
import os
import re
import stat

from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .operational_audit import model_effort
from .storage import atomic_json, load_json, safe_path


VERSION = "owner-model-effort-checkpoint-v1"
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_RETRIES = 20  # Same actual-attempt limit as Jobs/current_attempt.
RECEIPTS = ("intent.json", "runner_execution.json", "result.json", "completion.json", "failure.json")
TOKENS = ("input_tokens", "cached_input_tokens", "output_tokens")
STATES = {"receipt_needs_review", "response_saved", "failed", "execution_or_reconciliation_pending"}
ROW_FIELDS = {"job_id", "attempt", "role", "state", "execution_receipt_saved", "response_saved",
              "usage", "duration_seconds", "failure_reason"}
PRIVATE_COMPONENTS = {"config", ".git", ".codex", ".agents", ".aws", "profiles", "browser-profile", "native-profile"}


def _root(memory):
    root = safe_path(memory.root)
    if {part.casefold() for part in root.parts} & PRIVATE_COMPONENTS:
        raise SafetyError("MODEL_EFFORT_PRIVATE_PATH_REFUSED")
    if memory.evidence_type not in {"captured", "synthetic"}:
        raise SafetyError("MODEL_EFFORT_SCOPE_INVALID")
    return root


def _path(root):
    return safe_path(root, "owner_measurements") / "model_effort.json"


def _scope(root, memory):
    # Neither a raw path nor a prompt belongs in this optional checkpoint.
    return digest([str(root), memory.evidence_type])


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _code(value):
    return value is None or isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,159}", value) is not None


def _jobs(state):
    if not isinstance(state, dict) or not isinstance(state.get("jobs"), dict):
        raise SafetyError("MODEL_EFFORT_STATE_INVALID")
    jobs = {}
    for job_id, job in state["jobs"].items():
        identifier(job_id)
        if not isinstance(job, dict) or not isinstance(job.get("request"), dict):
            raise SafetyError("MODEL_EFFORT_STATE_INVALID")
        try:
            request_hash = digest(job["request"])
        except (TypeError, ValueError, RecursionError):
            raise SafetyError("MODEL_EFFORT_STATE_INVALID") from None
        jobs[job_id] = {"request": job["request"], "request_hash": request_hash}
    return jobs


def _row(row, job_id):
    if (not isinstance(row, dict) or set(row) != ROW_FIELDS or row["job_id"] != job_id
            or row["role"] not in {None, "A", "B", "C", "D", "E", "F", "G"}
            or row["state"] not in STATES or type(row["execution_receipt_saved"]) is not bool
            or type(row["response_saved"]) is not bool or not _code(row["failure_reason"])):
        raise SafetyError("MODEL_EFFORT_CACHE_ROW_INVALID")
    attempt = row["attempt"]
    if attempt != "initial" and (not isinstance(attempt, str) or not re.fullmatch(r"retry-(?:[1-9]|1[0-9]|20)", attempt)):
        raise SafetyError("MODEL_EFFORT_CACHE_ROW_INVALID")
    usage = row["usage"]
    if usage is not None and (not isinstance(usage, dict) or set(usage) != set(TOKENS)
            or any(type(usage.get(name)) is not int or usage[name] < 0 for name in TOKENS)
            or usage["cached_input_tokens"] > usage["input_tokens"] or not row["response_saved"]):
        raise SafetyError("MODEL_EFFORT_CACHE_ROW_INVALID")
    elapsed = row["duration_seconds"]
    if elapsed is not None and (type(elapsed) not in (int, float) or not isfinite(elapsed) or elapsed < 0):
        raise SafetyError("MODEL_EFFORT_CACHE_ROW_INVALID")
    if (row["response_saved"] != (row["state"] == "response_saved")
            or row["state"] == "receipt_needs_review" and
            (row["execution_receipt_saved"] or usage is not None or elapsed is not None)):
        raise SafetyError("MODEL_EFFORT_CACHE_ROW_INVALID")
    return row


def _entry(entry, job_id):
    fields = {"request_hash", "fingerprint_hash", "probed_at", "rows", "reason"}
    if (not isinstance(entry, dict) or set(entry) != fields or not _hash(entry["request_hash"])
            or not _code(entry["reason"]) or not isinstance(entry["rows"], list)
            or len(entry["rows"]) > MAX_RETRIES + 1):
        raise SafetyError("MODEL_EFFORT_CACHE_ENTRY_INVALID")
    timestamp(entry["probed_at"])
    if entry["reason"] is not None:
        if entry["fingerprint_hash"] is not None or entry["rows"]:
            raise SafetyError("MODEL_EFFORT_CACHE_ENTRY_INVALID")
    elif not _hash(entry["fingerprint_hash"]):
        raise SafetyError("MODEL_EFFORT_CACHE_ENTRY_INVALID")
    seen = set()
    for row in entry["rows"]:
        _row(row, job_id)
        if row["attempt"] in seen:
            raise SafetyError("MODEL_EFFORT_CACHE_ROW_INVALID")
        seen.add(row["attempt"])
    return entry


def _load(path, scope):
    """Invalid derivative input is unknown, not zero or canonical authority."""
    try:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return None, [{"reason": "MODEL_EFFORT_CACHE_MISSING"}]
        if stat.S_ISLNK(mode):
            raise SafetyError("SYMLINK_PATH_REFUSED")
        if not stat.S_ISREG(mode):
            raise SafetyError("MODEL_EFFORT_CACHE_INVALID")
        value = load_json(path, MAX_CACHE_BYTES)
        fields = {"schema_version", "version", "scope_hash", "updated_at", "last_job_id", "remaining_ids", "entries", "hash"}
        if (not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int
                or value["schema_version"] != 1 or value["version"] != VERSION
                or value["scope_hash"] != scope
                or value["hash"] != digest({name: item for name, item in value.items() if name != "hash"})
                or not isinstance(value["entries"], dict) or not isinstance(value["remaining_ids"], list)
                or len(set(value["remaining_ids"])) != len(value["remaining_ids"])):
            raise SafetyError("MODEL_EFFORT_CACHE_INVALID")
        timestamp(value["updated_at"])
        if value["last_job_id"] is not None:
            identifier(value["last_job_id"])
        for job_id in value["remaining_ids"]:
            identifier(job_id)
        diagnostics = []; valid = {}
        for job_id, entry in value["entries"].items():
            try:
                identifier(job_id); _entry(entry, job_id)
                valid[job_id] = entry
            except (SafetyError, TypeError, ValueError):
                diagnostics.append({"reason": "MODEL_EFFORT_CACHE_ENTRY_INVALID"})
        value["entries"] = valid
        return value, diagnostics
    except SafetyError as error:
        if str(error) == "SYMLINK_PATH_REFUSED":
            raise
        return None, [{"reason": "MODEL_EFFORT_CACHE_INVALID"}]
    except (OSError, TypeError, ValueError, RecursionError):
        return None, [{"reason": "MODEL_EFFORT_CACHE_INVALID"}]


def _stat(path, counters, *, directory=False):
    counters["metadata_probes"] += 1
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(value.st_mode):
        raise SafetyError("SYMLINK_PATH_REFUSED")
    if not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)):
        raise SafetyError("MODEL_EFFORT_OWNED_FILE_INVALID")
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _fingerprint(root, job_id, counters):
    """Only this job's owned directory and fixed receipt names are examined."""
    base = root / "ai_jobs" / job_id
    parent = _stat(base.parent, counters, directory=True)
    if parent is None:
        return digest({"parent": None})
    own = _stat(base, counters, directory=True)
    if own is None:
        return digest({"base": None})
    retries = []
    with os.scandir(base) as items:
        for item in items:
            if not item.name.startswith("retry-") or not item.name[6:].isdigit():
                continue
            if not re.fullmatch(r"retry-(?:[1-9]|1[0-9]|20)", item.name):
                raise SafetyError("MODEL_EFFORT_RETRY_SCOPE_INVALID")
            metadata = _stat(base / item.name, counters, directory=True)
            if metadata is not None:
                retries.append((item.name, metadata))
            if len(retries) > MAX_RETRIES:
                raise SafetyError("MODEL_EFFORT_RETRY_SCOPE_INVALID")
    folders = [("initial", base, own)] + [(name, base / name, value) for name, value in sorted(retries)]
    result = {"folders": {}, "retry_list": _stat(base / "retries.json", counters)}
    for name, folder, metadata in folders:
        # Directory device/inode bind ownership without unrelated file-time
        # churn. Actual retry names and all five receipt identities are bound.
        result["folders"][name] = {"directory": metadata[:2],
            "receipts": {filename: _stat(folder / filename, counters) for filename in RECEIPTS}}
    return digest(result)


def _aggregate(entries):
    rows = [deepcopy(row) for job_id in sorted(entries) for row in entries[job_id]["rows"]]
    usage = [row["usage"] for row in rows if row["usage"] is not None]
    durations = [row["duration_seconds"] for row in rows if row["duration_seconds"] is not None]
    return {"saved_job_attempts": len(rows), "rows": rows, "states": dict(Counter(row["state"] for row in rows)),
        "usage_receipts": len(usage), "attempts_without_usable_token_counts": len(rows) - len(usage),
        "recorded_tokens": {name: sum(row[name] for row in usage) for name in TOKENS},
        "duration_receipts": len(durations), "recorded_duration_sum_seconds": round(sum(durations), 3),
        "incremental_model_cost_usd": None,
        "note": "Only cached original model receipts are measured. Missing tokens/time remain unknown; duration sums are effort, not wall-clock time or an invoice. Coverage and probe dates are separate."}


def _project(value, jobs, diagnostics, at):
    entries = value["entries"] if value is not None else {}
    matched = {job_id: entry for job_id, entry in entries.items()
               if job_id in jobs and entry["request_hash"] == jobs[job_id]["request_hash"]}
    usable = {job_id: entry for job_id, entry in matched.items() if entry["reason"] is None}
    unavailable = {job_id: entry["reason"] for job_id, entry in matched.items() if entry["reason"] is not None}
    unprocessed = len(jobs) - len(matched)
    complete = unprocessed == 0 and not unavailable
    dates = sorted((entry["probed_at"] for entry in matched.values()), key=timestamp)
    age = (timestamp(at) - timestamp(value["updated_at"])).total_seconds() if value is not None else None
    diagnostics = list(diagnostics)
    if age is not None and age < 0:
        diagnostics.append({"reason": "MODEL_EFFORT_CACHE_CLOCK_AHEAD"})
    return {"version": VERSION, "owner_only": True,
        "state": "complete_cached" if complete else "partial_cached" if usable else "unknown",
        "checked_at": at, "cache_checked_at": value["updated_at"] if value is not None else None,
        "cache_age_seconds": round(age, 3) if age is not None and age >= 0 else None,
        "coverage": {"total_jobs": len(jobs), "current_request_bound_jobs": len(matched),
            "measured_jobs": len(usable), "unprocessed_jobs": unprocessed, "unavailable_jobs": len(unavailable),
            "all_current_jobs_probed": unprocessed == 0,
            "oldest_probe_at": dates[0] if dates else None, "latest_probe_at": dates[-1] if dates else None},
        "model": _aggregate(usable) if usable or not jobs else None,
        "unavailable": unavailable, "diagnostics": diagnostics,
        "receipt_freshness": "Cached measurements only; status does not recheck original files.",
        "provider_calls": 0, "model_calls": 0, "financial_writes": 0, "canonical_writes": 0}


def load_cached_model_effort(memory, state, *, as_of=None):
    """Read one derivative file; no receipt probe, refresh or save occurs."""
    at = as_of or utc_now(); timestamp(at)
    root = _root(memory); jobs = _jobs(state)
    value, diagnostics = _load(_path(root), _scope(root, memory))
    return _project(value, jobs, diagnostics, at)


def refresh_model_effort_checkpoint(memory, state, *, job_limit=25, as_of=None,
                                    minimum_interval_seconds=0):
    """Advance one finite sweep slice using the existing per-job audit.

    ``job_limit`` bounds JOB probes, not individual filesystem operations.
    Each job has five fixed receipts in its initial folder and at most twenty
    actual retry folders. Only changed jobs read receipt bodies; unchanged
    validated rows reuse their request- and metadata-bound reduction.

    A positive ``minimum_interval_seconds`` permits a cheap recurring hook.
    A recent valid checkpoint skips all original receipt probes and writes.
    A missing, malformed or clock-ahead checkpoint never delays repair; newly
    admitted/changed requests stay visibly unprocessed until a due slice.
    """
    if type(job_limit) is not int or job_limit < 1:
        raise SafetyError("MODEL_EFFORT_JOB_LIMIT_INVALID")
    at = as_of or utc_now(); timestamp(at)
    try:
        if (type(minimum_interval_seconds) not in (int, float) or not isfinite(minimum_interval_seconds)
                or minimum_interval_seconds < 0):
            raise SafetyError("MODEL_EFFORT_REFRESH_INTERVAL_INVALID")
        interval = timedelta(seconds=minimum_interval_seconds)
        next_due_after_refresh = (timestamp(at) + interval).isoformat()
    except (OverflowError, ValueError):
        raise SafetyError("MODEL_EFFORT_REFRESH_INTERVAL_INVALID") from None
    root = _root(memory); jobs = _jobs(state); path = _path(root); scope = _scope(root, memory)
    value, diagnostics = _load(path, scope)
    age = (timestamp(at) - timestamp(value["updated_at"])).total_seconds() if value is not None else None
    if value is not None and not diagnostics and age is not None and 0 <= age < interval.total_seconds():
        result = _project(value, jobs, diagnostics, at)
        result["refresh"] = {"probed_jobs": 0, "reduced_jobs": 0, "reused_jobs": 0,
            "metadata_probes": 0, "saved": False, "skipped": "not_due",
            "next_refresh_at": (timestamp(value["updated_at"]) + interval).isoformat(),
            "last_job_id": value["last_job_id"],
            "remaining_sweep_jobs": len([job_id for job_id in value["remaining_ids"] if job_id in jobs])}
        return result
    if value is None:
        value = {"schema_version": 1, "version": VERSION, "scope_hash": scope, "updated_at": at,
                 "last_job_id": None, "remaining_ids": [], "entries": {}}
    value["entries"] = {job_id: entry for job_id, entry in value["entries"].items() if job_id in jobs}
    remaining = [job_id for job_id in value["remaining_ids"] if job_id in jobs]
    if not remaining:
        cursor = value["last_job_id"]
        ordered = sorted(jobs)
        remaining = ([job_id for job_id in ordered if cursor is not None and job_id > cursor]
                     + [job_id for job_id in ordered if cursor is None or job_id <= cursor])
    selected = remaining[:job_limit]
    value["remaining_ids"] = remaining[len(selected):]
    counters = {"probed_jobs": 0, "reduced_jobs": 0, "reused_jobs": 0, "metadata_probes": 0}
    for job_id in selected:
        counters["probed_jobs"] += 1
        value["last_job_id"] = job_id
        request_hash = jobs[job_id]["request_hash"]
        try:
            before = _fingerprint(root, job_id, counters)
            old = value["entries"].get(job_id)
            if old and old["reason"] is None and old["request_hash"] == request_hash and old["fingerprint_hash"] == before:
                entry = dict(old, probed_at=at)
                counters["reused_jobs"] += 1
            else:
                measured = model_effort(memory, {"jobs": {job_id: {"request": jobs[job_id]["request"]}}})
                after = _fingerprint(root, job_id, counters)
                if before != after:
                    raise SafetyError("MODEL_EFFORT_RECEIPTS_CHANGED")
                entry = {"request_hash": request_hash, "fingerprint_hash": after, "probed_at": at,
                         "rows": measured["rows"], "reason": None}
                _entry(entry, job_id)
                counters["reduced_jobs"] += 1
            value["entries"][job_id] = entry
        except (SafetyError, OSError, TypeError, ValueError, KeyError) as error:
            code = str(error) if isinstance(error, SafetyError) and _code(str(error)) else "MODEL_EFFORT_JOB_UNREADABLE"
            value["entries"][job_id] = {"request_hash": request_hash, "fingerprint_hash": None,
                                       "probed_at": at, "rows": [], "reason": code}
    value["updated_at"] = at
    value["hash"] = digest({name: item for name, item in value.items() if name != "hash"})
    saved = False
    try:
        if len(canonical(value)) + 1 > MAX_CACHE_BYTES:
            raise SafetyError("MODEL_EFFORT_CACHE_SIZE_LIMIT")
        atomic_json(path, value)
        saved = True
    except (SafetyError, OSError) as error:
        code = str(error) if isinstance(error, SafetyError) and _code(str(error)) else "MODEL_EFFORT_CACHE_SAVE_FAILED"
        diagnostics.append({"reason": code})
    result = _project(value, jobs, diagnostics, at)
    result["refresh"] = {**counters, "saved": saved, "last_job_id": value["last_job_id"],
                         "remaining_sweep_jobs": len(value["remaining_ids"]), "skipped": None,
                         "next_refresh_at": next_due_after_refresh}
    return result
