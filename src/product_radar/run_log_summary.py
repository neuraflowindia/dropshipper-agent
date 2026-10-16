"""Owner-meter totals from the saved run logs, bounded at any age.

Every run log is summarised once. A small speed copy keeps each log's totals
keyed by the file's identity (inode, size, change time), so a later meter
reading only reads logs that are new or still growing. The speed copy is
disposable: when it is missing, stale or damaged the logs are simply read
again, and the numbers are the same either way.

Very old raw logs may be removed by the daily housekeeping (see
``retire_old_run_logs``). Their totals are first moved into one small, sealed
"retired" summary, which is kept, so the meter still counts them. Nothing here
reads research records, contacts a website, provider or model, or authorises
any action. Log-record counts only, as before.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from .models import SafetyError, digest, timestamp
from .runtime_storage import runtime_path
from .storage import atomic_json, load_json, safe_path


FOLDER = "data/operations/research-runs"
CACHE_NAME = "meter-summary-cache-v1.json"
RETIRED_NAME = "retired-summary-v1.json"
# v2 (17 October 2026): one failure per incident and honest access blocks;
# a v1 speed copy counted by the older rules, so it is simply read again.
CACHE_CONTRACT = "run-log-summary-cache-v2"
RETIRED_CONTRACT = "run-log-retired-summary-v1"
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_RETIRED_BYTES = 32 * 1024 * 1024
_COUNTS = ("events", "useful_results", "failures", "reused_saved_failures", "waits", "access_blocks",
           "invocation_started", "invocation_finished", "invocation_interrupted", "invocation_failed", "unreadable")
_PAIRS = ("failure_reasons", "wait_reasons", "block_routes")
_INVOCATION = {"invocation_started", "invocation_finished", "invocation_interrupted", "invocation_failed"}


def empty_summary():
    """Totals of no log lines."""
    return {**dict.fromkeys(_COUNTS, 0), **{name: [] for name in _PAIRS}, "runs": [], "first": None, "last": None}


def _row_into(totals, counters, runs, row):
    """Add one already-filtered log record, with the live counter rules.

    One failure per incident, access blocks only for real refusals, and no
    harmless diagnostic as a wait (``operations.classify_row``).
    """
    from .operations import classify_row
    if isinstance(row.get("run_id"), str):
        runs.add(row["run_id"])
    totals["events"] += 1
    event = row["event"]
    if event in _INVOCATION:
        totals[event] += 1
    reason = row.get("reason") if isinstance(row.get("reason"), str) else event
    if row.get("useful_result") is True:
        totals["useful_results"] += 1
    counted = classify_row(event, row)
    if counted["reused_failure"]:
        totals["reused_saved_failures"] += 1
    elif counted["failure"]:
        totals["failures"] += 1
        counters["failure_reasons"][reason[:120]] += 1
    if counted["block"]:
        # A website actually refused a request sent now (401/403/429, a
        # challenge page or a sign-in wall). Neither a local failure nor a wait.
        totals["access_blocks"] += 1
        counters["block_routes"][row.get("route") if isinstance(row.get("route"), str) else "unknown"] += 1
    if counted["wait"]:
        totals["waits"] += 1
        counters["wait_reasons"][reason[:120]] += 1


def summarise_lines(lines, start, budget=None):
    """Totals of one log's lines at or after ``start`` (``None``: all lines).

    ``budget`` is the most bytes this call may still read. Returns
    ``(summary, bytes_read, complete)``; ``complete`` is False only when the
    budget ran out inside this log (its partial totals are still returned).
    """
    totals = dict.fromkeys(_COUNTS, 0)
    counters = {name: Counter() for name in _PAIRS}
    runs, first, last, scanned, complete = set(), None, None, 0, True
    for line in lines:
        scanned += len(line)
        if budget is not None and scanned > budget:
            complete = False
            break
        try:
            row = json.loads(line)
            at = timestamp(row["at"])
            if not isinstance(row, dict) or not isinstance(row.get("event"), str):
                raise ValueError
        except (ValueError, KeyError, TypeError, SafetyError):
            totals["unreadable"] += 1
            continue
        if start is not None and at < start:
            continue
        first = at if first is None or at < first else first
        last = at if last is None or at > last else last
        _row_into(totals, counters, runs, row)
    summary = {**totals, **{name: [[key, count] for key, count in counters[name].items()] for name in _PAIRS},
               "runs": sorted(runs), "first": first.isoformat() if first else None,
               "last": last.isoformat() if last else None}
    return summary, scanned, complete


def merge(summaries):
    """Add per-log totals in the given order (ties keep first-seen order)."""
    totals = dict.fromkeys(_COUNTS, 0)
    counters = {name: Counter() for name in _PAIRS}
    runs, first, last = set(), None, None
    for summary in summaries:
        for name in _COUNTS:
            totals[name] += summary[name]
        for name in _PAIRS:
            for key, count in summary[name]:
                counters[name][key] += count
        runs.update(summary["runs"])
        for value, pick in ((summary["first"], "first"), (summary["last"], "last")):
            if value is None:
                continue
            instant = timestamp(value)
            if pick == "first":
                first = instant if first is None or instant < first else first
            else:
                last = instant if last is None or instant > last else last
    return {**totals, **{name: [[key, count] for key, count in counters[name].items()] for name in _PAIRS},
            "runs": sorted(runs), "first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None}


def _validate_summary(value):
    if (not isinstance(value, dict) or set(value) != set(_COUNTS) | set(_PAIRS) | {"runs", "first", "last"}
            or any(type(value[name]) is not int or value[name] < 0 for name in _COUNTS)
            or any(not isinstance(value[name], list) for name in _PAIRS) or not isinstance(value["runs"], list)
            or any(not isinstance(run, str) for run in value["runs"])):
        raise SafetyError("RUN_LOG_SUMMARY_INVALID")
    for name in _PAIRS:
        for pair in value[name]:
            if (not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
                    or type(pair[1]) is not int or pair[1] < 1):
                raise SafetyError("RUN_LOG_SUMMARY_INVALID")
    for name in ("first", "last"):
        if value[name] is not None:
            timestamp(value[name])
    return value


def _identity(stat):
    return [stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _sealed(value):
    return {"value": value, "hash": digest(value)}


def _unseal(wrapper, code):
    if not isinstance(wrapper, dict) or set(wrapper) != {"value", "hash"} or digest(wrapper["value"]) != wrapper["hash"]:
        raise SafetyError(code)
    return wrapper["value"]


def _load_cache(folder, since):
    """The disposable speed copy for this exact start, or an empty one."""
    try:
        value = _unseal(load_json(folder / CACHE_NAME, MAX_CACHE_BYTES), "RUN_LOG_SUMMARY_CACHE_INVALID")
        if (not isinstance(value, dict) or set(value) != {"contract", "since", "files"}
                or value["contract"] != CACHE_CONTRACT or value["since"] != since or not isinstance(value["files"], dict)):
            return {}
        files = {}
        for name, row in value["files"].items():
            if (isinstance(row, dict) and set(row) == {"identity", "summary"} and isinstance(row["identity"], list)
                    and len(row["identity"]) == 3 and all(type(part) is int for part in row["identity"])):
                files[name] = {"identity": row["identity"], "summary": _validate_summary(row["summary"])}
        return files
    except (SafetyError, OSError, TypeError, ValueError, KeyError):
        return {}


def load_retired(folder):
    """The kept totals of removed old logs, or ``None`` when none were removed.

    Unlike the speed copy this is the only remaining record of those logs, so
    a damaged file is reported (``RUN_LOG_RETIRED_SUMMARY_INVALID``), never
    silently replaced.
    """
    path = safe_path(folder) / RETIRED_NAME
    if not path.exists() and not path.is_symlink():
        return None
    value = _unseal(load_json(path, MAX_RETIRED_BYTES), "RUN_LOG_RETIRED_SUMMARY_INVALID")
    if (not isinstance(value, dict) or set(value) != {"contract", "since", "files", "newest_mtime_ns", "summary"}
            or value["contract"] != RETIRED_CONTRACT or not isinstance(value["files"], list)
            or any(not isinstance(name, str) for name in value["files"]) or len(set(value["files"])) != len(value["files"])
            or type(value["newest_mtime_ns"]) is not int or value["newest_mtime_ns"] < 0
            or value["since"] is not None and not isinstance(value["since"], str)):
        raise SafetyError("RUN_LOG_RETIRED_SUMMARY_INVALID")
    if value["since"] is not None:
        timestamp(value["since"])
    _validate_summary(value["summary"])
    return value


def _retired_part(retired, since, start):
    """How the kept totals of removed logs apply to this reading's start.

    Returns ``(summary_or_None, counted)``. The totals were made for one start
    instant; a later start that is after every removed log simply excludes
    them (the meter skips such old logs anyway). Any other start cannot be
    answered from the kept totals, so the reading is marked partial.
    """
    if retired is None:
        return None, True
    if retired["since"] == since:
        return retired["summary"], True
    newest = datetime.fromtimestamp(retired["newest_mtime_ns"] / 1e9, timezone.utc)
    if start is not None and newest < start:
        return None, True
    return None, False


def _log_paths(folder):
    return sorted(folder.glob("run-*.jsonl*")) if folder.is_dir() else []


class _RetiredChanged(Exception):
    """The retired summary changed during one reading; read again."""


def _retired_identity(folder):
    try:
        return _identity((folder / RETIRED_NAME).lstat())
    except FileNotFoundError:
        return None


def summarise_folder(root, *, since=None, max_bytes=512 * 1024 * 1024, save_cache=True):
    """The meter's run-log totals since ``since``; reads only new log bytes.

    Logs last changed before ``since`` are skipped unopened. ``max_bytes`` bounds
    the bytes read in this call; logs already summarised cost nothing, so a
    reading cut short is completed by later readings (each log is at most an
    hour of records, far below the default bound). When the daily
    housekeeping retires logs during a reading (a log vanishes or the retired
    summary changes), the reading starts again, so no log is counted twice or
    missed.
    """
    for attempt in range(3):
        try:
            return _summarise_folder(root, since=since, max_bytes=max_bytes, save_cache=save_cache)
        except (FileNotFoundError, _RetiredChanged):
            if attempt == 2:
                raise SafetyError("RUN_LOG_SUMMARY_CHANGING") from None


def _summarise_folder(root, *, since, max_bytes, save_cache):
    start = timestamp(since) if since is not None else None
    folder = safe_path(runtime_path(root, FOLDER))
    if not folder.is_dir():
        return merge([]), False, {"files": 0, "state": "none"}
    before = _retired_identity(folder)
    cache = _load_cache(folder, since)
    pruned = {"files": 0, "state": "none"}
    retired = None
    try:
        retired = load_retired(folder)
    except (SafetyError, OSError):
        pruned["state"] = "summary_unreadable"
    retired_names = set(retired["files"]) if retired else set()
    retired_summary, counted = _retired_part(retired, since, start)
    if retired is not None:
        pruned = {"files": len(retired["files"]), "state": "counted" if counted else "not_counted"}
    parts, kept, budget, truncated = ([retired_summary] if retired_summary else []), {}, max_bytes, False
    for path in _log_paths(folder):
        if path.name in retired_names or path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        if start is not None and datetime.fromtimestamp(stat.st_mtime, timezone.utc) < start:
            continue
        identity = _identity(stat)
        saved = cache.get(path.name)
        if saved is not None and saved["identity"] == identity:
            parts.append(saved["summary"]); kept[path.name] = saved
            continue
        with path.open("rb") as stream:
            summary, scanned, complete = summarise_lines(stream, start, budget)
        budget -= scanned
        parts.append(summary)
        if not complete:
            truncated = True
            break
        # Re-check the identity: a log written while it was read is counted
        # now but summarised again next time, never cached half-read.
        if _identity(path.stat()) == identity:
            kept[path.name] = {"identity": identity, "summary": summary}
    if _retired_identity(folder) != before:
        raise _RetiredChanged()
    if save_cache and kept != cache:
        try:
            atomic_json(folder / CACHE_NAME, _sealed({"contract": CACHE_CONTRACT, "since": since, "files": kept}))
        except (SafetyError, OSError):
            pass  # A speed copy only; the reading above is complete without it.
    return merge(parts), truncated, pruned


def owner_totals(root, *, since=None, max_bytes=512 * 1024 * 1024):
    """The owner meter's run-log reading (same fields and rules as before)."""
    totals, truncated, pruned = summarise_folder(root, since=since, max_bytes=max_bytes)
    unreadable = totals["unreadable"]
    top = lambda name: dict(Counter(dict(totals[name])).most_common(8)) if totals[name] else {}
    partial = truncated or unreadable or pruned["state"] in {"not_counted", "summary_unreadable"}
    return {"state": "partial" if partial else "known", "since": since, "runs": len(totals["runs"]),
        "events": totals["events"], "useful_results": totals["useful_results"], "failures": totals["failures"],
        "reused_saved_failures": totals["reused_saved_failures"], "waits": totals["waits"],
        "access_blocks": totals["access_blocks"], "top_access_block_routes": top("block_routes"),
        "invocations_started": totals["invocation_started"], "invocations_finished": totals["invocation_finished"],
        "invocations_interrupted_or_failed": totals["invocation_interrupted"] + totals["invocation_failed"],
        "top_failure_reasons": top("failure_reasons"), "top_wait_reasons": top("wait_reasons"),
        "first_event_at": _seconds(totals["first"]), "last_event_at": _seconds(totals["last"]),
        "unreadable_lines": unreadable, "truncated": truncated, "pruned_logs": pruned,
        "note": ("Log-record counts, not unique requests, products or bills. Waits include deliberate deferrals "
                 "and questions held behind a website's earlier refusal; access blocks are refusals of sent requests.")}


def _seconds(value):
    return timestamp(value).isoformat(timespec="seconds") if value else None


def _save_retired(folder, value):
    atomic_json(folder / RETIRED_NAME, _sealed(value))
    if load_retired(folder) != value:
        raise SafetyError("RUN_LOG_RETIRED_SUMMARY_READBACK_FAILED")


def retire_old_run_logs(root, *, since, keep_days, now):
    """Remove raw run logs untouched for ``keep_days``, keeping their totals.

    Only ``run-*.jsonl*`` files directly in the run-log folder are touched,
    never ``current.json`` or anything else. The totals of the removed logs
    (for the meter's start ``since``) are saved in the sealed retired summary
    before any file is removed, so an interruption can only leave a log that
    is skipped by the meter and removed next time. Returns a small record.
    """
    if type(keep_days) is not int or keep_days < 7:
        raise SafetyError("RUN_LOG_RETENTION_SETTINGS_INVALID")
    start = timestamp(since) if since is not None else None
    cutoff = timestamp(now) - timedelta(days=keep_days)
    folder = safe_path(runtime_path(root, FOLDER))
    result = {"state": "nothing_to_remove", "removed_files": 0, "removed_bytes": 0, "reason": None}
    if not folder.is_dir():
        return result
    retired, resealed = load_retired(folder), False
    if retired is not None and retired["since"] != since:
        newest = datetime.fromtimestamp(retired["newest_mtime_ns"] / 1e9, timezone.utc)
        if start is None or newest >= start:
            # The kept totals belong to another meter start and cover logs at
            # or after the new one: they cannot be re-counted for it. Keep
            # every raw log (the owner status names this).
            return dict(result, state="kept", reason="RUN_LOG_RETIRED_SUMMARY_START_CHANGED")
        # A later meter start (a fresh start) after every retired log: those
        # totals no longer count, so the summary starts afresh for it.
        retired, resealed = {"contract": RETIRED_CONTRACT, "since": since, "files": [], "newest_mtime_ns": 0,
                             "summary": empty_summary()}, True
    names = list(retired["files"]) if retired else []
    old = []
    for path in _log_paths(folder):
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        if datetime.fromtimestamp(stat.st_mtime, timezone.utc) < cutoff:
            old.append((path, stat))
    if not old:
        if resealed:
            _save_retired(folder, retired)
            return dict(result, reason="RUN_LOG_RETIRED_SUMMARY_RESEALED")
        return result
    parts = [retired["summary"]] if retired else []
    newest = retired["newest_mtime_ns"] if retired else 0
    for path, stat in old:
        if path.name in names:
            continue  # already counted; an earlier removal was interrupted
        if start is not None and datetime.fromtimestamp(stat.st_mtime, timezone.utc) < start:
            continue  # the meter never reads logs from before its start
        with path.open("rb") as stream:
            summary, _, _ = summarise_lines(stream, start)
        if _identity(path.stat()) != _identity(stat):
            raise SafetyError("RUN_LOG_CHANGED_WHILE_RETIRING")
        parts.append(summary); names.append(path.name); newest = max(newest, stat.st_mtime_ns)
    value = {"contract": RETIRED_CONTRACT, "since": since, "files": names, "newest_mtime_ns": newest,
             "summary": merge(parts) if parts else empty_summary()}
    if retired is None or resealed or value != retired:
        _save_retired(folder, value)
    removed, size = 0, 0
    for path, stat in old:
        current = path.lstat()
        if current.st_ino != stat.st_ino or current.st_size != stat.st_size or current.st_mtime_ns != stat.st_mtime_ns:
            continue  # changed since it was summarised: keep it
        os.unlink(path)
        removed += 1; size += stat.st_size
    return dict(result, state="removed" if removed else "nothing_to_remove", removed_files=removed, removed_bytes=size,
                reason="RUN_LOG_RETIRED_SUMMARY_RESEALED" if resealed else None)
