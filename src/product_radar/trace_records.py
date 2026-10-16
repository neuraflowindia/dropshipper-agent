"""Read-only loading for the research trace (``scripts/research_trace.py``).

The trace explains, in plain words, how the research programme worked at every
step. This module only *reads* what the programme already saved:

* the research events, replayed in memory with the same chain checks as
  ``ResearchMemory`` (sequence, previous hash, own hash, file name);
* the run logs in ``data/operations/research-runs``;
* the AI job folders, the spend-watch readings and alerts;
* the dropshipping platforms' saved catalogue reads;
* the owner meter's saved start.

It takes no lock (the programme's writer lock included), writes no file and
contacts no website, provider or model. A broken event chain stops the trace
rather than showing numbers from a partial history.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import SafetyError, digest, identifier, timestamp
from .research_memory import TABLES, ResearchMemory, load_event
from .storage import load_json


MEMORY_PARTS = ("data", "research", "captured")
EVIDENCE_TYPE = "captured"
RUNS_PARTS = ("data", "operations", "research-runs")
BASELINE_PARTS = ("data", "operations", "meter-baseline.json")
SPEND_PARTS = MEMORY_PARTS + ("spend_watch",)
DAY_ZONE = ZoneInfo("Asia/Kolkata")
DAY_ZONE_LABEL = "India time"
AI_FILES = ("intent.json", "result.json", "completion.json", "runner_execution.json", "failure.json",
            "execution_failure.json", "retries.json", "owner.json", "derived_result.json")
AI_FILE_LIMIT = 4 * 1024 * 1024
# Small scalar fields kept from every saved change, for the per-type journey
# and the per-day counts. Values are short codes, never page text or prompts.
SUMMARY_FIELDS = ("kind", "status", "state", "route", "source", "reason", "level", "accepted", "concept_id",
                  "job_id", "purpose", "stage", "outcome", "relationship", "parse_state", "decision")
SUMMARY_TEXT_LIMIT = 160


class TraceError(SafetyError):
    """The saved records cannot be traced as they are (stated in plain words)."""


def day_of(at):
    """The owner's calendar day (India time) of a saved time, as YYYY-MM-DD."""
    return timestamp(at).astimezone(DAY_ZONE).date().isoformat()


def seconds_between(start, end):
    """Seconds from one saved time to another, or None when either is missing."""
    if not start or not end:
        return None
    try:
        return max(0.0, (timestamp(end) - timestamp(start)).total_seconds())
    except SafetyError:
        return None


def at_or_after(at, since):
    """True when a saved time is at or after ``since`` (None: always)."""
    if since is None:
        return True
    try:
        return timestamp(at) >= timestamp(since)
    except SafetyError:
        return False


def summarise(table, value):
    """A small, safe summary of one saved record for the time line."""
    summary = {}
    for name in SUMMARY_FIELDS:
        item = value.get(name)
        if isinstance(item, bool) or isinstance(item, (int, float)):
            summary[name] = item
        elif isinstance(item, str):
            summary[name] = item[:SUMMARY_TEXT_LIMIT]
    binding = value.get("binding")
    if isinstance(binding, dict) and isinstance(binding.get("route"), str):
        summary.setdefault("route", binding["route"])
    progress = value.get("progress")
    if isinstance(progress, dict):
        for name, alias in (("acquisition", "acquisition"), ("reason", "progress_reason"),
                            ("interpretation", "interpretation")):
            if isinstance(progress.get(name), str):
                summary[alias] = progress[name][:SUMMARY_TEXT_LIMIT]
    request = value.get("request")
    if table == "jobs" and isinstance(request, dict):
        for name in ("role", "prompt_version"):
            if isinstance(request.get(name), str):
                summary[name] = request[name]
    if table == "concepts" and isinstance(value.get("description"), dict):
        label = value["description"].get("label")
        if isinstance(label, str):
            summary["label"] = label[:SUMMARY_TEXT_LIMIT]
    targets = value.get("targets")
    if isinstance(targets, dict) and targets:
        summary["targets"] = sorted(targets)[:20]
    results = value.get("target_results")
    if isinstance(results, dict) and results:
        summary["target_states"] = {key: row.get("state") for key, row in sorted(results.items())[:20]
                                    if isinstance(row, dict)}
    return summary


@dataclass
class Timeline:
    """When each saved record first appeared, last changed, and its summaries."""

    first: dict = field(default_factory=dict)
    last: dict = field(default_factory=dict)
    changes: Counter = field(default_factory=Counter)
    history: dict = field(default_factory=dict)

    def add(self, table, record_id, value, at, sequence):
        key = (table, record_id)
        self.first.setdefault(key, at)
        self.last[key] = at
        self.changes[key] += 1
        summary = summarise(table, value)
        rows = self.history.setdefault(key, [])
        if not rows or rows[-1][2] != summary:
            rows.append((at, sequence, summary))

    def first_at(self, table, record_id):
        return self.first.get((table, record_id))


def replay_events(events_dir):
    """Rebuild the saved research state in memory from the original events.

    Returns ``(state, timeline, facts)``. Every event must continue the chain
    with ``ResearchMemory``'s checks (sequence, file name, previous hash, own
    hash, namespace, unique id, and its change rules); otherwise ``TraceError``.
    """
    events_dir = Path(events_dir)
    state = {table: {} for table in TABLES}
    timeline = Timeline()
    if not events_dir.is_dir():
        return state, timeline, {"events": 0, "first_event_at": None, "last_event_at": None}
    names = sorted(entry.name for entry in os.scandir(events_dir) if entry.name.endswith(".json"))
    head, sequence, first, last, seen = "0" * 64, 0, None, None, set()
    for name in names:
        event = load_event(events_dir / name)
        try:
            body = {key: value for key, value in event.items() if key != "hash"}
            if (type(event["seq"]) is not int or event["seq"] != sequence + 1 or name != f"{event['seq']:08d}.json"
                    or event["previous"] != head or digest(body) != event["hash"]
                    or event["evidence_type"] != EVIDENCE_TYPE or event["id"] in seen):
                raise ValueError
            identifier(event["id"])
            at = event["at"]
            timestamp(at)
            # The programme's own change rules: exact fields, known table,
            # unchanged dependency, and saved captures never rewritten.
            ResearchMemory._apply(state, event["changes"])
            for change in event["changes"]:
                timeline.add(change["table"], change["id"], state[change["table"]][change["id"]], at, event["seq"])
        except (KeyError, TypeError, ValueError, SafetyError):
            raise TraceError("TRACE_EVENTS_CHAIN_BROKEN:" + name) from None
        seen.add(event["id"])
        head, sequence = event["hash"], event["seq"]
        first = first or at
        last = at
    return state, timeline, {"events": sequence, "first_event_at": first, "last_event_at": last}


def load_run_rows(runs_dir):
    """Every readable run-log line (current and rotated logs), oldest first.

    Returns ``(rows, unreadable_lines)``. The meter's speed copy and other
    files in the folder are not run logs and are skipped.
    """
    runs_dir = Path(runs_dir)
    rows, unreadable = [], 0
    if not runs_dir.is_dir():
        return rows, unreadable
    for path in sorted(runs_dir.glob("run-*.jsonl*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or not isinstance(row.get("event"), str):
                        raise ValueError
                    timestamp(row["at"])
                except (ValueError, KeyError, TypeError, SafetyError):
                    unreadable += 1
                    continue
                rows.append(row)
    rows.sort(key=lambda row: (timestamp(row["at"]), row.get("elapsed_seconds") or 0))
    return rows, unreadable


def _optional_json(path):
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return load_json(path, AI_FILE_LIMIT)
    except SafetyError:
        return {"unreadable": True}


def _attempt(folder):
    return {name.removesuffix(".json"): _optional_json(folder / name) for name in AI_FILES}


def load_ai_jobs(jobs_dir):
    """One record per AI job folder: its files, and every attempt in order.

    An automatic retry keeps its own files in ``retry-N`` (``retries.json``
    counts them, as ``research_ai.current_attempt`` reads it); the top
    folder keeps the original attempt. The last attempt holding any saved
    file is the job's current state.
    """
    jobs_dir = Path(jobs_dir)
    jobs = []
    if not jobs_dir.is_dir():
        return jobs
    for folder in sorted(path for path in jobs_dir.iterdir() if path.is_dir() and not path.is_symlink()):
        files = _attempt(folder)
        attempts = [files]
        retries = files.get("retries")
        if isinstance(retries, list):
            for number in range(1, len(retries) + 1):
                retry = folder / f"retry-{number}"
                if retry.is_dir() and not retry.is_symlink():
                    attempt = _attempt(retry)
                    if any(value is not None for name, value in attempt.items() if name != "retries"):
                        attempts.append(attempt)
        jobs.append({"folder": folder.name, "present": sorted(name for name, value in files.items() if value is not None),
                     "other_files": sorted(path.name for path in folder.iterdir()
                                           if path.is_file() and path.name not in AI_FILES),
                     "attempts": attempts, **files})
    return jobs


def load_spend_watch(spend_dir):
    """The saved allowance readings (latest only) and every alert line."""
    spend_dir = Path(spend_dir)
    state = None
    path = spend_dir / "state.json"
    if path.is_file() and not path.is_symlink():
        wrapper = load_json(path, 8 * 1024 * 1024)
        if (not isinstance(wrapper, dict) or set(wrapper) != {"state", "hash"}
                or digest(wrapper["state"]) != wrapper["hash"]):
            raise TraceError("TRACE_SPEND_WATCH_UNREADABLE")
        state = wrapper["state"]
    alerts = []
    folder = spend_dir / "alerts"
    if folder.is_dir():
        for alert_path in sorted(folder.glob("*.jsonl")):
            for line in alert_path.read_bytes().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("at"), str):
                    alerts.append(row)
    alerts.sort(key=lambda row: row["at"])
    return {"state": state, "alerts": alerts}


def load_baseline(data_root):
    """The owner meter's saved start (``None`` before the meter was started)."""
    from .programme_summary import validate_meter_baseline
    path = Path(data_root).joinpath(*BASELINE_PARTS)
    if not path.is_file():
        return None
    wrapper = load_json(path, 4 * 1024 * 1024)
    if not isinstance(wrapper, dict) or set(wrapper) != {"baseline", "hash"} or digest(wrapper["baseline"]) != wrapper["hash"]:
        raise TraceError("TRACE_METER_START_UNREADABLE")
    return validate_meter_baseline(wrapper["baseline"])


def load_platform_reads(memory_root):
    """Each dropshipping platform's saved catalogue reads (receipts only) and refresh record."""
    from . import dropship_platforms as platforms
    result = {}
    for route in platforms.ROUTES:
        folder = Path(memory_root) / "dropship_platforms" / route
        if not folder.is_dir():
            continue
        receipts = []
        for relative in platforms.raw_files(memory_root, route):
            try:
                receipt = platforms.load_raw(memory_root, route, relative)["receipt"]
            except SafetyError as error:
                receipt = {"state": "unreadable", "reason": str(error)}
            receipts.append({"file": relative, **{name: receipt.get(name) for name in (
                "state", "reason", "http_status", "attempt_started_at", "read_at", "checked_at")}})
        try:
            refresh = platforms.load_refresh(memory_root, route)
        except SafetyError as error:
            refresh = {"unreadable": str(error)}
        result[route] = {"reads": receipts, "refresh": refresh}
    return result


def problem_outcome(state, row):
    """Whether the step a saved problem stopped was done later.

    Problem records are written once and never closed, so the trace checks
    the step's own result: a named listing, a resolved/excluded match, or a
    saved checklist. "open" means no such result is saved yet.
    """
    stage, affected = row.get("stage"), row.get("affected_id")
    if stage == "description":
        done = affected in state["descriptions"]
    elif stage == "fit_review":
        done = affected in state["assessments"]
    elif stage == "identity":
        # The stop names the match record (or, in older records, the listing).
        settled = ("resolved", "excluded")
        own = state["associations"].get(affected)
        done = (own or {}).get("status") in settled or any(
            match.get("semantic_id") == affected and match.get("status") in settled
            for match in state["associations"].values())
    else:
        return "unknown"
    return "done_later" if done else "open"


@dataclass
class TraceStore:
    data_root: Path
    memory_root: Path
    state: dict
    timeline: Timeline
    facts: dict
    run_rows: list
    unreadable_run_lines: int
    ai_jobs: list
    spend: dict
    baseline: dict
    platforms: dict
    load_seconds: float = 0.0

    def first_at(self, table, record_id):
        return self.timeline.first_at(table, record_id)


def resolve_data_root(project_root, data_root=None):
    """The folder holding ``data/``: an explicit one, else the project's setting."""
    if data_root is not None:
        root = Path(data_root).absolute()
    else:
        from .runtime_storage import runtime_root
        root = Path(runtime_root(Path(project_root).absolute()))
    if not root.joinpath(*MEMORY_PARTS).is_dir():
        raise TraceError("TRACE_RESEARCH_RECORDS_NOT_FOUND")
    return root


def load_store(data_root, *, clock=None):
    """Read every saved input the trace needs, without any lock or write."""
    import time
    clock = clock or time.monotonic
    started = clock()
    data_root = Path(data_root)
    memory_root = data_root.joinpath(*MEMORY_PARTS)
    state, timeline, facts = replay_events(memory_root / "events")
    rows, unreadable = load_run_rows(data_root.joinpath(*RUNS_PARTS))
    return TraceStore(data_root=data_root, memory_root=memory_root, state=state, timeline=timeline, facts=facts,
                      run_rows=rows, unreadable_run_lines=unreadable, ai_jobs=load_ai_jobs(memory_root / "ai_jobs"),
                      spend=load_spend_watch(data_root.joinpath(*SPEND_PARTS)), baseline=load_baseline(data_root),
                      platforms=load_platform_reads(memory_root), load_seconds=round(clock() - started, 2))


def default_since(store):
    """The owner meter's saved start, else the first saved research event."""
    if store.baseline:
        return store.baseline["started_at"]
    return store.facts.get("first_event_at")


def day_span(since, until):
    """Every owner day from ``since`` to ``until`` (inclusive), oldest first."""
    if not since or not until:
        return []
    start, end = timestamp(since).astimezone(DAY_ZONE).date(), timestamp(until).astimezone(DAY_ZONE).date()
    days = []
    while start <= end and len(days) < 3700:
        days.append(start.isoformat())
        start += timedelta(days=1)
    return days
