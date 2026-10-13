"""Small, private run logs and advisory status, never research authority.

Uses standard logging. Status writes belong to the programme writer only;
reading them never replays research, changes intent, or contacts a provider.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import math
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from .models import SafetyError, timestamp, utc_now
from .runtime_storage import runtime_path
from .storage import atomic_json, load_json, safe_path
from .source_deadline import diagnostic_source_work

_ACTIVE = ContextVar("product_radar_operation_run", default=None)
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,159}\Z")
_TOKEN = re.compile(r"[a-zA-Z0-9_.:-]{1,200}\Z")
_STEPS = {"admission": "Checking whether research can start.",
    "saved_history": "Checking the saved research records.",
    "preparation": "Preparing the next research step.", "profiles": "Choosing useful search words.",
    "fit": "Reviewing how well the products fit the checklist.", "acquire": "Checking product websites.",
    "match": "Checking whether products repeat earlier findings.", "suppliers": "Checking possible suppliers.",
    "photos": "Collecting product photos.", "reports": "Preparing report files.",
    "private_staging": "Saving private reports and checking their downloads.",
    "discovery": "Looking for more kinds of products.", "backup": "Saving a backup copy.",
    "costs": "Updating the spending record."}
_SITES = {"amazon-bestsellers": "Amazon bestsellers", "amazon-movers-and-shakers": "Amazon movers and shakers",
    "amazon-search": "Amazon", "flipkart-search": "Flipkart", "meta-ads": "Facebook ads",
    "google-trends": "Google Trends", "blinkit": "Blinkit", "zepto": "Zepto", "instamart": "Instamart",
    "google-suppliers": "Google supplier results", "google-maps": "Google Maps",
    "indiamart": "IndiaMART", "tradeindia": "TradeIndia", "company-pages": "supplier websites"}
_DETAILS = {"catalogue_seed": "Reviewing saved ideas for future searches.",
    "discovery_questions": "Choosing the next searches and checking their saved results.",
    "discovery_saved_answers": "Reviewing products from saved search results."}
_PHASE_MESSAGES = {"model_call": "Waiting for the AI to finish its answer.",
    "programme_selection": "Choosing which saved research to resume.",
    "source_preparation": "Preparing website searches from the saved product information.",
    "source_saved_recovery": "Checking saved website replies without starting new searches.",
    "research_lookup_save": "Updating the research lookup file after saving the results.",
    "response_save": "Saving the information collected from a website.",
    "apply": "Adding the saved results to the research records."}


def _identity():
    fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": os.getpid(), "process_start_ticks": fields[19]}


def _alive(identity):
    try:
        pid = identity["pid"]
        if type(pid) is not int or pid < 1:
            return False
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0] != "Z" and fields[19] == identity["process_start_ticks"]
    except (OSError, KeyError, IndexError, TypeError, ValueError):
        return False


def _fields(values):
    """No raw responses, exception messages, prompts, environments or URLs."""
    result = {}
    for name in ("programme_id", "review_scope_id", "turn_id", "concept_id", "task_id", "job_id", "acquisition_id",
                 "stage", "phase", "next_phase", "phase_state", "state", "route", "role", "next_action", "error_type",
                 "record_kind", "record_id"):
        value = values.get(name)
        if isinstance(value, str) and _TOKEN.fullmatch(value):
            result[name] = value
    if isinstance(values.get("detail"), str) and values["detail"] in _DETAILS:
        result["detail"] = values["detail"]
    if isinstance(values.get("reason"), str) and _CODE.fullmatch(values["reason"]):
        result["reason"] = values["reason"]
    for name in ("cycle", "supplied_items", "exit_code", "scanned", "remaining", "elapsed_seconds", "deadline_seconds"):
        value = values.get(name)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            result[name] = round(value, 6)
    for name in ("dispatch_started", "dispatch_uncertain", "response_saved", "photo_applied", "useful_result", "stalled",
                 "actual_attempt", "outcome_reused"):
        if type(values.get(name)) is bool:
            result[name] = values[name]
    for name in ("counts", "timings"):
        value = values.get(name)
        if isinstance(value, dict):
            result[name] = {k: round(v, 6) for k, v in list(value.items())[:40]
                if isinstance(k, str) and _TOKEN.fullmatch(k) and type(v) in (int, float)
                and math.isfinite(v) and v >= 0}
    location = values.get("error_location")
    if isinstance(location, dict) and set(location) == {"file", "function", "line"}:
        path, function, line = location["file"], location["function"], location["line"]
        if (isinstance(path, str) and len(path) < 300 and not Path(path).is_absolute() and ".." not in Path(path).parts
                and isinstance(function, str) and (function in {"<module>", "<lambda>"} or _TOKEN.fullmatch(function))
                and type(line) is int and line > 0):
            result["error_location"] = location
    return result


def error_details(error, *, root=None):
    result = {"error_type": type(error).__name__}
    if isinstance(error, SafetyError):
        result.update(_fields({"reason": str(error)}))
    trace = error.__traceback__
    while trace:
        path = Path(trace.tb_frame.f_code.co_filename).absolute()
        if root is not None and path.is_relative_to(Path(root).absolute()):
            result["error_location"] = {"file": str(path.relative_to(Path(root).absolute())),
                "function": trace.tb_frame.f_code.co_name, "line": trace.tb_lineno}
        trace = trace.tb_next
    return result


class _Formatter(logging.Formatter):
    def format(self, record):
        return json.dumps(record.msg, separators=(",", ":"), ensure_ascii=False)


class _Handler(TimedRotatingFileHandler):
    def handleError(self, record):
        self.owner._failed()


class OperationRun:
    def __init__(self, root):
        self.root = safe_path(root)
        self.run_id = "run-" + uuid4().hex
        self.started_at = utc_now()
        self.started = time.monotonic()
        try:
            self.identity = _identity()
        except (OSError, IndexError):
            self.identity = {"pid": os.getpid(), "process_start_ticks": None}
        self.folder = runtime_path(self.root, "data/operations/research-runs")
        self.status_path = self.folder / "current.json"
        self.writer = False
        self.recording_failed = False
        self.notice_written = False
        self.last_useful = None
        self.step = "admission"
        self.detail = None
        self.phase = None
        self.phase_started = None
        self.phase_deadline = None
        self.next_action = "continue_to_next_ready_step"
        self.last_reason = None
        self.counts = {"events": 0, "completed_results": 0, "failures": 0, "deferred": 0,
                       "saved_failed_results": 0}
        self.logger = logging.Logger("product-radar." + self.run_id)
        self.logger.propagate = False
        self.handler = None
        version = sha256()
        for name in ("scripts/run_programme.py", "src/product_radar/programme.py", "src/product_radar/source_coverage.py",
                     "src/product_radar/operations.py", "src/product_radar/source_deadline.py"):
            path = self.root / name
            try:
                if path.is_file():
                    version.update(name.encode()); version.update(path.read_bytes())
            except OSError:
                self._failed()
        self.code_version = version.hexdigest()
        try:
            safe_path(self.folder).mkdir(parents=True, exist_ok=True)
            self.handler = _Handler(safe_path(self.folder / (self.run_id + ".jsonl")), when="H", interval=1,
                backupCount=0, utc=True, encoding="utf-8", delay=True)
            self.handler.owner = self
            self.handler.setFormatter(_Formatter())
            self.logger.addHandler(self.handler)
        except (OSError, SafetyError):
            self._failed()

    def _failed(self):
        self.recording_failed = True
        if not self.notice_written:
            self.notice_written = True
            try:
                sys.stderr.write("Research activity could not be saved. Earlier research records are unchanged.\n")
                sys.stderr.flush()
            except Exception:
                pass

    def emit(self, event, **values):
        try:
            if not _TOKEN.fullmatch(event):
                return
            # Private or malformed error text is filtered below, but its
            # presence must not make a failed event look like harmless reuse.
            error_type_supplied = "error_type" in values
            values = _fields(values)
            if error_type_supplied:
                values["error_reported"] = True
            at = utc_now()
            self.counts["events"] += 1
            if values.get("useful_result"):
                self.last_useful = at
                self.counts["completed_results"] += 1
            # No dispatch alone does not mean historical reuse: a new local
            # or prelaunch failure can also occur before a website request.
            # Only the exact saved-outcome branch identifies an older result.
            reused_failure = (event == "source_outcome" and values.get("state") == "failed"
                and values.get("outcome_reused") is True and values.get("actual_attempt") is False
                and not error_type_supplied)
            if values.get("error_type") or values.get("state") == "failed":
                self.counts["saved_failed_results" if reused_failure else "failures"] += 1
            if values.get("state") in {"waiting", "deferred", "skipped", "not_attempted"}:
                self.counts["deferred"] += 1
            if values.get("stage"):
                self.step = values["stage"]
            if event == "stage_started":
                self.detail = None
            if values.get("detail"):
                self.detail = values["detail"]
            if values.get("next_action"):
                self.next_action = values["next_action"]
            if values.get("reason"):
                self.last_reason = values["reason"]
            if event in {"phase_started", "stage_started"}:
                self.phase = values.get("phase", values.get("stage"))
                self.phase_started = at
                self.phase_deadline = values.get("deadline_seconds")
            elif event == "phase_finished":
                self.phase = values.get("next_phase")
                self.phase_started = at if self.phase else None
                self.phase_deadline = values.get("deadline_seconds") if self.phase else None
            row = {"schema_version": 1, "run_id": self.run_id, **self.identity, "code_version": self.code_version,
                "at": at, "elapsed_seconds": round(time.monotonic() - self.started, 6), "event": event, **values}
            if self.handler is not None:
                try:
                    with diagnostic_source_work():
                        self.logger.info(row)
                except Exception:
                    self._failed()
            if self.writer:
                status = {"schema_version": 1, "run_id": self.run_id, **self.identity, "code_version": self.code_version,
                    "observed_at": at, "started_at": self.started_at, "writer_active": event != "writer_released",
                    "step": self.step, "phase": self.phase, "phase_started_at": self.phase_started,
                    "phase_deadline_seconds": self.phase_deadline, "next_action": self.next_action,
                    "last_reason": self.last_reason,
                    "message": _PHASE_MESSAGES.get(self.phase,
                        _DETAILS.get(self.detail, _STEPS.get(self.step, "Research is working through its saved tasks."))),
                    "last_useful_result_at": self.last_useful, "counts": dict(self.counts),
                    "recording_failed": self.recording_failed, "last_event": row,
                    "log_file": "data/operations/research-runs/" + self.run_id + ".jsonl"}
                if values.get("route") and self.phase not in _PHASE_MESSAGES:
                    status["message"] = "Checking " + _SITES.get(values["route"], "a product website") + "."
                    if reused_failure:
                        status["message"] = ("Reviewed an earlier failed result from "
                            + _SITES.get(values["route"], "a product website")
                            + ". No new website request was made.")
                with diagnostic_source_work():
                    atomic_json(self.status_path, status)
        except Exception:
            self._failed()


@contextmanager
def operation_run(root):
    recorder = OperationRun(root)
    token = _ACTIVE.set(recorder)
    recorder.emit("invocation_started")
    try:
        yield recorder
    finally:
        _ACTIVE.reset(token)
        if recorder.handler is not None:
            try:
                recorder.handler.close()
            except Exception:
                recorder._failed()


@contextmanager
def operation_writer():
    """Enter/exit inside the existing kernel writer lock, not beside it."""
    recorder = _ACTIVE.get()
    if recorder:
        recorder.writer = True
        recorder.emit("writer_admitted", stage="preparation")
    try:
        yield
    finally:
        if recorder:
            recorder.emit("writer_released")
            recorder.writer = False


def record_event(event, **values):
    recorder = _ACTIVE.get()
    if recorder:
        recorder.emit(event, **values)


def record_saved_decision(committed, *, record_kind, record_id, concept_id=None):
    """Credit a new saved judgment, not a model reply or an unchanged replay.

    Call only after the canonical writer returns, outside its lock. These are
    internal research records, not counts of suitable products or reports.
    The parent's phase, deadline and next action stay unchanged.
    """
    recorder = _ACTIVE.get()
    if committed is not True or recorder is None:
        return
    try:
        recorder.emit("research_decision_saved", record_kind=record_kind,
            record_id=record_id, concept_id=concept_id, state="saved", useful_result=True)
    except Exception:
        recorder._failed()


def record_error(event, error, **values):
    recorder = _ACTIVE.get()
    if recorder:
        try:
            recorder.emit(event, **{**values, **error_details(error, root=recorder.root)})
        except Exception:
            recorder._failed()


@contextmanager
def preparation_step(detail, **values):
    """Describe finite local preparation outside financial/canonical locks.

    No deadline, allowance, research result or execution decision is changed.
    The existing logger supplies durable timing; no per-file logging is added.
    """
    recorder = _ACTIVE.get()
    if recorder is None or not isinstance(detail, str) or detail not in _DETAILS:
        yield
        return
    previous = recorder.detail
    started = time.monotonic()
    record_event("preparation_started", detail=detail, **values)
    try:
        yield
    except BaseException as error:
        record_error("preparation_failed", error, detail=detail, **values)
        raise
    finally:
        # Restore nesting without treating preparation as a useful result.
        recorder.detail = previous
        record_event("preparation_finished", phase=detail, elapsed_seconds=time.monotonic() - started, **values)


def _record_progress(row):
    source = row.get("source_progress") if isinstance(row.get("source_progress"), dict) else {}
    values = {**row, **source}
    result = row.get("result")
    if isinstance(result, dict):
        values["counts"] = {name: value for name, value in result.items() if type(value) is int}
    phase = row.get("phase_state")
    # Count actual accepted/saved results, not a model reply, another stage
    # beginning, an empty return or a diagnostic cursor changing.
    completed = result.get("completed_results", result.get("completed", 0)) if isinstance(result, dict) else 0
    if isinstance(result, dict):
        completed += sum(item.get("applied") is True for item in result.get("outcomes", []) if isinstance(item, dict))
        completed += len(result.get("records", []))
        if row.get("stage") == "match":
            # Functional source judgments already report each new canonical
            # scope save, including a partial application before a later wait.
            # Their existing reference-list shape distinguishes them from
            # legacy source reviews, which retain their summary credit.
            completed += sum(item.get("state") == "reviewed"
                and not isinstance(item.get("scoped_evidence_refs"), list)
                for item in result.get("outcomes", []) if isinstance(item, dict))
        elif row.get("stage") == "photos":
            if result.get("item_progress_recorded") is not True:
                completed += sum(item.get("state") == "usable" for item in result.get("outcomes", []) if isinstance(item, dict))
        elif row.get("stage") == "reports":
            completed += sum(item.get("complete") is True and item.get("reused") is not True
                             for item in result.get("outcomes", []) if isinstance(item, dict))
        elif row.get("stage") == "private_staging":
            completed += sum(item.get("state") == "private_bytes_verified"
                             for item in result.get("outcomes", []) if isinstance(item, dict))
        elif row.get("stage") == "discovery":
            # Discovery's list is new relative to its original saved baseline.
            # Replaying that same cycle can return it again after the programme
            # has already queued all those products. Prefer the actual current
            # admission count, so replay cannot reset the useful-progress clock.
            # Smaller/legacy helpers without that field retain their interface.
            if "queued_follow_on_products" in result:
                queued = result["queued_follow_on_products"]
                completed += queued if type(queued) is int and queued >= 0 else 0
            else:
                completed += len(result.get("new_concept_ids", []))
    values["useful_result"] = bool(phase == "source_result" and source.get("state") in
        {"acquired", "completed_no_results", "insufficient", "finished"}
        or row.get("stage") == "photos" and phase == "photo_result"
            and row.get("state") == "usable" and row.get("photo_applied") is True
        or phase == "finished" and type(completed) is int and completed > 0)
    if phase == "runner_call_started":
        from .research_ai import MODEL_DEADLINE_SECONDS
        values.update(phase="model_call", deadline_seconds=MODEL_DEADLINE_SECONDS)
        event = "phase_started"
    elif phase in {"runner_call_returned", "runner_call_failed", "finished"}:
        event = "phase_finished"
    else:
        event = "stage_started" if phase == "started" else "programme_progress"
    record_event(event, **values)


def record_progress(row):
    try:
        _record_progress(row)
    except Exception:
        recorder = _ACTIVE.get()
        if recorder:
            recorder._failed()


def read_operation_status(root, *, as_of=None):
    """Read one small advisory file; stale records never authorize action."""
    at = as_of or utc_now()
    timestamp(at)
    path = safe_path(runtime_path(root, "data/operations/research-runs/current.json"))
    if not path.exists():
        return {"state": "not_recorded", "checked_at": at, "message": "No research activity has been recorded yet."}
    try:
        row = load_json(path, 32768)
        if (not isinstance(row, dict) or row.get("schema_version") != 1
                or not isinstance(row.get("run_id"), str) or not _TOKEN.fullmatch(row["run_id"])
                or type(row.get("writer_active")) is not bool):
            raise SafetyError("OPERATION_STATUS_INVALID")
        observed = timestamp(row["observed_at"])
        active = row["writer_active"] and _alive(row)
        age = max(0, (timestamp(at) - observed).total_seconds())
        result = dict(row, checked_at=at, seconds_since_activity=round(age, 1), stale=not active,
            state="running" if active else "saved_previous_run")
        phase_started = row.get("phase_started_at")
        result["seconds_in_current_phase"] = (round(max(0, (timestamp(at) - timestamp(phase_started)).total_seconds()), 1)
                                               if active and phase_started else None)
        if not active:
            result["message"] = "This is the last saved research activity, not a currently running step."
        useful = row.get("last_useful_result_at")
        result["seconds_since_useful_result"] = (round(max(0, (timestamp(at) - timestamp(useful)).total_seconds()), 1)
                                                 if useful else None)
        result["attention"] = None
        phase_age, deadline = result["seconds_in_current_phase"], row.get("phase_deadline_seconds")
        if active and type(deadline) in (int, float) and phase_age is not None and phase_age > deadline:
            result["attention"] = "The current step has passed its time limit. Its saved attempt needs recovery; other ready work should continue."
        elif active and row.get("phase") not in {"model_call", "transport"}:
            useful_age = result["seconds_since_useful_result"]
            if useful_age is None:
                useful_age = max(0, (timestamp(at) - timestamp(row["started_at"])).total_seconds())
            if useful_age >= 300:
                result["attention"] = "No completed result has been recorded for at least five minutes. The last saved step is still preparation or local processing."
        return result
    except (OSError, SafetyError, ValueError, TypeError, KeyError):
        return {"state": "unreadable", "checked_at": at, "message": "The saved activity could not be read."}


_WAIT_STATES = {"waiting", "deferred", "skipped", "not_attempted"}


def operation_log_summary(root, *, since=None, max_bytes=512 * 1024 * 1024):
    """Read-only totals from saved run logs since one instant; diagnostics only.

    Counts are log records (the same rules as the live counters), not unique
    requests, products or bills. Files last changed before ``since`` are skipped
    unopened. Nothing is written and no record authorizes any action.
    """
    from collections import Counter
    from datetime import datetime, timezone
    start = timestamp(since) if since is not None else None
    folder = safe_path(runtime_path(root, "data/operations/research-runs"))
    totals, failures, waits, runs = Counter(), Counter(), Counter(), set()
    unreadable, scanned, truncated, first, last = 0, 0, False, None, None
    paths = sorted(folder.glob("run-*.jsonl*")) if folder.is_dir() else []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        if start is not None and datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < start:
            continue
        with path.open("rb") as stream:
            for line in stream:
                scanned += len(line)
                if scanned > max_bytes:
                    truncated = True
                    break
                try:
                    row = json.loads(line)
                    at = timestamp(row["at"])
                    if not isinstance(row, dict) or not isinstance(row.get("event"), str):
                        raise ValueError
                except (ValueError, KeyError, TypeError, SafetyError):
                    unreadable += 1
                    continue
                if start is not None and at < start:
                    continue
                first = at if first is None or at < first else first
                last = at if last is None or at > last else last
                if isinstance(row.get("run_id"), str):
                    runs.add(row["run_id"])
                totals["events"] += 1
                event = row["event"]
                if event in {"invocation_started", "invocation_finished", "invocation_interrupted", "invocation_failed"}:
                    totals[event] += 1
                reason = row.get("reason") if isinstance(row.get("reason"), str) else event
                if row.get("useful_result") is True:
                    totals["useful_results"] += 1
                reused = (event == "source_outcome" and row.get("state") == "failed" and row.get("outcome_reused") is True
                          and row.get("actual_attempt") is False and not row.get("error_reported"))
                if row.get("error_reported") or row.get("error_type") or row.get("state") == "failed":
                    if reused:
                        totals["reused_saved_failures"] += 1
                    else:
                        totals["failures"] += 1
                        failures[reason[:120]] += 1
                if row.get("state") in _WAIT_STATES:
                    totals["waits"] += 1
                    waits[reason[:120]] += 1
        if truncated:
            break
    return {"state": "partial" if truncated or unreadable else "known", "since": since, "runs": len(runs),
        "events": totals["events"], "useful_results": totals["useful_results"], "failures": totals["failures"],
        "reused_saved_failures": totals["reused_saved_failures"], "waits": totals["waits"],
        "invocations_started": totals["invocation_started"], "invocations_finished": totals["invocation_finished"],
        "invocations_interrupted_or_failed": totals["invocation_interrupted"] + totals["invocation_failed"],
        "top_failure_reasons": dict(failures.most_common(8)), "top_wait_reasons": dict(waits.most_common(8)),
        "first_event_at": first.isoformat(timespec="seconds") if first else None,
        "last_event_at": last.isoformat(timespec="seconds") if last else None,
        "unreadable_lines": unreadable, "truncated": truncated,
        "note": "Log-record counts, not unique requests, products or bills. Waits include deliberate deferrals."}
