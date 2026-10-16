"""Small, private run logs and advisory status, never research authority.

Uses standard logging. Status writes belong to the programme writer only;
reading them never replays research, changes intent, or contacts a provider.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
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
PACKAGE = Path(__file__).resolve().parent
PROGRESS_WATCH_SETTINGS = PACKAGE.parents[1] / "config/research/progress_watch.json"
_WATCH_FIELDS = ("attention_no_activity_seconds", "attention_no_result_seconds", "watchdog_no_activity_seconds",
                 "watchdog_poll_seconds", "watchdog_interrupt_grace_seconds", "watchdog_terminate_grace_seconds",
                 "scheduled_run_overdue_seconds", "repeated_failure_runs")
# Used only when the checked-in settings file is missing or invalid, so a bad
# edit can never stop research or hide its status; the worker records that.
PROGRESS_WATCH_DEFAULTS = {"attention_no_activity_seconds": 1800, "attention_no_result_seconds": 1800,
                           "watchdog_no_activity_seconds": 2700, "watchdog_poll_seconds": 60,
                           "watchdog_interrupt_grace_seconds": 300, "watchdog_terminate_grace_seconds": 60,
                           "scheduled_run_overdue_seconds": 1200, "repeated_failure_runs": 3}
# Time limits for local steps that never contact a website. They only decide
# when the window says a step is over time; they never cancel the step.
LOCAL_PHASE_DEADLINES = {"readiness": 1800, "programme_selection": 1800, "research_lookup_save": 600}
# At most one heartbeat per run in this many seconds, so a long local check is
# visibly alive in the saved activity without filling the run log.
HEARTBEAT_SECONDS = 30
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,159}\Z")
_TOKEN = re.compile(r"[a-zA-Z0-9_.:-]{1,200}\Z")
_STEPS = {"admission": "Checking whether research can start.",
    "saved_history": "Checking the saved research records.",
    "preparation": "Preparing the next research step.", "profiles": "Choosing useful search words.",
    "fit": "Reviewing how well the products fit the checklist.", "acquire": "Checking product websites.",
    "match": "Checking whether products repeat earlier findings.",
    "crowding": "Checking how crowded products already are on Amazon and Flipkart.",
    "suppliers": "Checking possible suppliers.",
    "photos": "Collecting product photos.", "reports": "Preparing report files.",
    "private_staging": "Saving private reports and checking their downloads.",
    "discovery": "Looking for more kinds of products.", "backup": "Saving a backup copy.",
    "platforms": "Checking the dropshipping platforms.",
    "costs": "Updating the spending record."}
_SITES = {"amazon-bestsellers": "Amazon bestsellers",
    "amazon-search": "Amazon", "flipkart-search": "Flipkart", "meta-ads": "Facebook ads",
    "google-trends": "Google Trends", "blinkit": "Blinkit", "zepto": "Zepto", "instamart": "Instamart",
    "google-suppliers": "Google supplier results", "google-maps": "Google Maps",
    "indiamart": "IndiaMART", "tradeindia": "TradeIndia", "company-pages": "supplier websites",
    "deodap": "DeoDap", "baapstore": "BaapStore", "dropdash": "Dropdash",
    "meta-ads-depth": "Facebook ads (deeper read)"}
_DETAILS = {"product_map": "Growing the product map from platform, Trends, Meta and supplier material.",
    "discovery_questions": "Choosing the next searches and checking their saved results.",
    "discovery_saved_answers": "Reviewing products from saved search results."}
_PHASE_MESSAGES = {"model_call": "Waiting for the AI to finish its answer.",
    "programme_selection": "Choosing which saved research to resume.",
    "source_preparation": "Preparing website searches from the saved product information.",
    "source_saved_recovery": "Checking saved website replies without starting new searches.",
    "research_lookup_save": "Updating the research lookup file after saving the results.",
    "response_save": "Saving the information collected from a website.",
    "apply": "Adding the saved results to the research records."}


def boot_id():
    """This computer's current boot, so awake-time clocks are only compared
    within one boot."""
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return value if _TOKEN.fullmatch(value) else None
    except OSError:
        return None


# Notifications are for a stuck or over-time run only. "No completed result"
# also happens in long healthy runs, so it stays as window text.
NOTIFY_KINDS = frozenset(("no_activity", "phase_over_time"))


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


_WAIT_STATES = {"waiting", "deferred", "skipped", "not_attempted"}


def _access_outcome(event, values):
    """A source outcome refused by the website ("block") or held unsent ("held").

    "blocked" with an actual attempt is a real access refusal of a sent
    request. "blocked" without one is a question held behind an earlier
    refusal or route pause: nothing was sent, so it is a wait, not a failure.
    """
    if event != "source_outcome" or values.get("state") != "blocked":
        return None
    if values.get("actual_attempt") is True:
        return "block"
    return "held" if values.get("actual_attempt") is False else None


def _fields(values):
    """No raw responses, exception messages, prompts, environments or URLs."""
    result = {}
    for name in ("programme_id", "review_scope_id", "turn_id", "concept_id", "task_id", "job_id", "acquisition_id",
                 "stage", "phase", "next_phase", "phase_state", "state", "route", "role", "next_action", "error_type",
                 "record_kind", "record_id", "checkpoint"):
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
    # "Review group 5 of 16": a whole position inside a known count, or nothing.
    position, count = values.get("group_position"), values.get("group_count")
    if type(position) is int and type(count) is int and 1 <= position <= count <= 10000:
        result.update(group_position=position, group_count=count)
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


@lru_cache(maxsize=1)
def code_version():
    """One fingerprint of the code this process runs, computed once per process.

    Every ``.py`` file of the running ``product_radar`` package plus the
    programme entry script, by content (not dates), so any code change shows
    up in the run records. Git is not needed. Returns ``(hex, complete)``;
    an unreadable file is named in the hash and makes ``complete`` false.
    """
    version, complete = sha256(), True
    files = sorted(PACKAGE.rglob("*.py"))
    entry = PACKAGE.parents[1] / "scripts/run_programme.py"
    for path in [*files, entry] if entry.is_file() else files:
        name = path.relative_to(PACKAGE.parents[1]).as_posix()
        version.update(name.encode() + b"\0")
        try:
            version.update(sha256(path.read_bytes()).digest())
        except OSError:
            complete = False
            version.update(b"unreadable")
    return version.hexdigest(), complete


def progress_watch_settings(path=None):
    """The checked-in progress watch thresholds (seconds and a run count)."""
    value = load_json(path or PROGRESS_WATCH_SETTINGS, 16384)
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("kind") != "progress-watch-settings-v1"
            or not set(_WATCH_FIELDS) <= set(value) or set(value) - {*_WATCH_FIELDS, "schema_version", "kind", "note"}
            or any(type(value[name]) is not int or not 1 <= value[name] <= 7 * 86400 for name in _WATCH_FIELDS)
            or value["attention_no_activity_seconds"] >= value["watchdog_no_activity_seconds"]
            or value["watchdog_poll_seconds"] >= value["watchdog_no_activity_seconds"]
            or value["repeated_failure_runs"] < 2):
        raise SafetyError("PROGRESS_WATCH_SETTINGS_INVALID")
    return {name: value[name] for name in _WATCH_FIELDS}


def progress_watch_or_defaults():
    """The settings, or the built-in defaults when the file cannot be used.
    Returns ``(settings, from_file)``."""
    try:
        return progress_watch_settings(), True
    except SafetyError:
        return dict(PROGRESS_WATCH_DEFAULTS), False


# Plain words for the reason codes the window can show. Unknown codes are
# shown as the code itself, so nothing is hidden.
REASON_TEXT = {
    "WORK_TURN_SOURCE_LIMIT": "it used this round's allowance of website checks (normal)",
    "WORK_TURN_MODEL_LIMIT": "it used this round's allowance of AI answers (normal)",
    "SOURCE_ACCESS_WAIT": "a website asked us to wait before checking it again",
    "SOURCE_ACTION_TIMEOUT": "a website took too long to answer",
    "SUPPLIER_COMPANY_ORIGINAL_PENDING": "a supplier website check is still waiting to be finished",
    "SUPPLIER_BROWSER_FALLBACK_PROOF_REQUIRED": "a supplier website needs a browser check that is not ready yet",
    "PRODUCT_IMAGE_USABLE_SAVED": "a usable product photo was saved",
    "RESEARCH_INDEX_SIZE_LIMIT": "the research lookup file reached its size limit",
    "RESEARCH_INDEX_WRITE_UNAVAILABLE": "the research lookup file could not be updated",
    "DIRECT_TRENDS_RECEIPT_MISMATCH": "a Google Trends answer did not match its question",
    "AI_PROVIDER_REQUEST_FAILED": "the AI service did not answer",
    "SOURCE_ACQUISITION_SOURCE_DATE_INVALID": "a website gave a date that could not be read",
    "RUNNER_ALREADY_ACTIVE": "another research run was already working",
    "PROGRAMME_LOCAL_FILE_FAILED": "a saved research file could not be read or written",
    "PROGRESS_WATCH_RESTART": "it stopped making progress, so it was stopped to be restarted automatically",
    "WORKER_EXIT_FAILED": "it stopped with an error",
    "LOCAL_BACKUP_DISK_SPACE_LOW": "the project drive is nearly full, so the daily backup copy waits (research continues)",
    "BACKUP_PRUNE_KEPT_SET_DAMAGED": "a kept backup copy is damaged, so no older copy was removed",
}


def plain_reason(code):
    """Plain words for one reason code, or the code itself when unknown."""
    if not isinstance(code, str) or not code:
        return None
    return REASON_TEXT.get(code, "code " + code)


def _minutes(seconds):
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute" + ("" if minutes == 1 else "s")


def no_activity_text(seconds, watch):
    """The warning for a run that has recorded nothing, consistent with the
    watchdog however long the quiet time already is."""
    limit = watch["watchdog_no_activity_seconds"]
    text = f"Research has not recorded any progress for {_minutes(seconds)}."
    if seconds < limit:
        return text + (f" If this reaches {_minutes(limit)}, the run is stopped and the next scheduled run "
                       "starts research again.")
    return text + " A run this quiet is stopped automatically and the next scheduled run starts research again."


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
        self.boot_id = boot_id()
        self.last_useful_awake = self.phase_started_awake = None
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
        self.group = None
        self.next_action = "continue_to_next_ready_step"
        self.last_reason = None
        self.counts = {"events": 0, "completed_results": 0, "failures": 0, "deferred": 0,
                       "saved_failed_results": 0, "access_blocks": 0}
        self.logger = logging.Logger("product-radar." + self.run_id)
        self.logger.propagate = False
        self.handler = None
        self.last_heartbeat = None
        self.code_version, complete = code_version()
        if not complete:
            self._failed()
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

    def _track_group(self, event, values):
        """Which review group a step is serving ("group 5 of 16"), if any.

        A position belongs to the step and phase it arrived with: it ends when
        a new stage starts, the step changes, that group's stage pass finishes
        or that phase finishes (an AI call inside the group keeps it)."""
        group = self.group
        if group is not None and (event == "stage_started" or self.step != group["step"]
                or event == "phase_finished" and (values.get("phase_state") == "finished"
                                                  or values.get("phase") == group["phase"])):
            self.group = None
        if "group_position" in values:
            self.group = {"step": self.step, "phase": self.phase,
                          "position": values["group_position"], "total": values["group_count"]}

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
            at, awake = utc_now(), time.monotonic()
            self.counts["events"] += 1
            if values.get("useful_result"):
                self.last_useful, self.last_useful_awake = at, awake
                self.counts["completed_results"] += 1
            # No dispatch alone does not mean historical reuse: a new local
            # or prelaunch failure can also occur before a website request.
            # Only the exact saved-outcome branch identifies an older result.
            reused_failure = (event == "source_outcome" and values.get("state") == "failed"
                and values.get("outcome_reused") is True and values.get("actual_attempt") is False
                and not error_type_supplied)
            if values.get("error_type") or values.get("state") == "failed":
                self.counts["saved_failed_results" if reused_failure else "failures"] += 1
            access = _access_outcome(event, values)
            if access == "block":
                self.counts["access_blocks"] += 1
            if values.get("state") in _WAIT_STATES or access == "held":
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
                self.phase_started, self.phase_started_awake = at, awake
                self.phase_deadline = values.get("deadline_seconds")
            elif event == "phase_finished":
                self.phase = values.get("next_phase")
                self.phase_started = at if self.phase else None
                self.phase_started_awake = awake if self.phase else None
                self.phase_deadline = values.get("deadline_seconds") if self.phase else None
            self._track_group(event, values)
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
                    # The computer's awake-time clock (it stops while it sleeps)
                    # at the same instants, so a reader can leave sleep out.
                    "awake_clock": {"boot_id": self.boot_id, "observed": awake, "started": self.started,
                                    "phase_started": self.phase_started_awake,
                                    "last_useful": self.last_useful_awake},
                    "log_file": "data/operations/research-runs/" + self.run_id + ".jsonl"}
                if values.get("route") and self.phase not in _PHASE_MESSAGES:
                    status["message"] = "Checking " + _SITES.get(values["route"], "a product website") + "."
                    if reused_failure:
                        status["message"] = ("Reviewed an earlier failed result from "
                            + _SITES.get(values["route"], "a product website")
                            + ". No new website request was made.")
                status["group_progress"] = (None if self.group is None else
                    {"step": self.group["step"], "position": self.group["position"], "total": self.group["total"]})
                if self.group is not None:
                    status["message"] += f" Review group {self.group['position']} of {self.group['total']}."
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


def record_heartbeat(event, **values):
    """A throttled "still working" event inside a long local step.

    At most one per run every ``HEARTBEAT_SECONDS``. It refreshes the saved
    activity time only: no phase, deadline, step, result or count changes.
    """
    recorder = _ACTIVE.get()
    if recorder is None:
        return
    now = time.monotonic()
    if recorder.last_heartbeat is not None and now - recorder.last_heartbeat < HEARTBEAT_SECONDS:
        return
    recorder.last_heartbeat = now
    for name in ("stage", "phase", "next_phase", "detail", "next_action", "reason", "useful_result", "state"):
        values.pop(name, None)
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
        elif row.get("stage") == "platforms":
            # Products newly queued from the platforms' lists (W4).
            queued = result.get("queued_follow_on_products")
            completed += queued if type(queued) is int and queued >= 0 else 0
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


def read_operation_status(root, *, as_of=None, awake_now=None):
    """Read one small advisory file; stale records never authorize action.

    Ages leave out time the computer slept: when the saved awake-time clock
    is from this boot, each age is the smaller of the wall-clock and the
    awake-time difference. ``awake_now`` defaults to this computer's awake
    clock only when ``as_of`` is not given (an explicit instant is a test or
    diagnostic and is used as is unless ``awake_now`` is also given).
    """
    if as_of is None and awake_now is None:
        awake_now = time.monotonic()
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
        clocks = row.get("awake_clock") if isinstance(row.get("awake_clock"), dict) else {}
        same_boot = awake_now is not None and clocks.get("boot_id") is not None and clocks.get("boot_id") == boot_id()
        def since(instant, clock):
            wall = max(0, (timestamp(at) - timestamp(instant)).total_seconds())
            mark = clocks.get(clock)
            if same_boot and type(mark) in (int, float) and math.isfinite(mark):
                return min(wall, max(0, awake_now - mark))
            return wall
        age = since(row["observed_at"], "observed")
        result = dict(row, checked_at=at, seconds_since_activity=round(age, 1), stale=not active,
            state="running" if active else "saved_previous_run")
        phase_started = row.get("phase_started_at")
        result["seconds_in_current_phase"] = (round(since(phase_started, "phase_started"), 1)
                                               if active and phase_started else None)
        if not active:
            result["message"] = "This is the last saved research activity, not a currently running step."
        useful = row.get("last_useful_result_at")
        result["seconds_since_useful_result"] = round(since(useful, "last_useful"), 1) if useful else None
        result["attention"] = result["attention_kind"] = None
        watch = progress_watch_or_defaults()[0]
        phase_age, deadline = result["seconds_in_current_phase"], row.get("phase_deadline_seconds")
        useful_age = result["seconds_since_useful_result"]
        if useful_age is None:
            useful_age = since(row["started_at"], "started")
        if active and age >= watch["attention_no_activity_seconds"]:
            result["attention_kind"] = "no_activity"
            result["attention"] = no_activity_text(age, watch)
        elif active and type(deadline) in (int, float) and phase_age is not None and phase_age > deadline:
            result["attention_kind"] = "phase_over_time"
            result["attention"] = "The current step has passed its time limit. Its saved attempt needs recovery; other ready work should continue."
        elif (active and row.get("phase") not in {"model_call", "transport"}
                and useful_age >= watch["attention_no_result_seconds"]):
            result["attention_kind"] = "no_result"
            result["attention"] = (f"No completed result has been recorded for at least {_minutes(useful_age)}. "
                "Research is still recording activity, but only preparation or local checks.")
        result["attention_notify"] = result["attention_kind"] in NOTIFY_KINDS
        return result
    except (OSError, SafetyError, ValueError, TypeError, KeyError):
        return {"state": "unreadable", "checked_at": at, "message": "The saved activity could not be read."}


def _error_code(error_type):
    if error_type in {"OSError", "PermissionError", "FileNotFoundError", "IsADirectoryError", "NotADirectoryError"}:
        return "PROGRAMME_LOCAL_FILE_FAILED"
    if error_type:
        return ("PROGRAMME_ERROR_" + re.sub(r"[^A-Z0-9_]", "_", error_type.upper()))[:160]
    return "WORKER_EXIT_FAILED"


def run_outcome(root, pid, *, since, tail_bytes=262144):
    """How one finished research process ended, from its own saved run log.

    Finds the run log started by process ``pid`` at or after ``since`` (an
    ISO instant) and reads only its last part. Returns the run id, the last
    recorded reason code, the failure reason (when the run stopped with an
    error) and its error type, or ``None`` when no such log exists. Read only.
    """
    from datetime import datetime, timezone
    start = timestamp(since)
    folder = safe_path(runtime_path(root, "data/operations/research-runs"))
    if type(pid) is not int or not folder.is_dir():
        return None
    candidates = []
    for path in folder.glob("run-*.jsonl"):
        try:
            if not path.is_symlink() and path.is_file() and datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) >= start:
                candidates.append(path)
        except OSError:
            continue
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            with path.open("rb") as stream:
                first = json.loads(stream.readline())
                if not isinstance(first, dict) or first.get("pid") != pid or timestamp(first.get("at")) < start:
                    continue
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - tail_bytes))
                lines = stream.read().splitlines()
        except (OSError, ValueError, SafetyError):
            continue
        result = {"run_id": first.get("run_id") if _TOKEN.fullmatch(str(first.get("run_id"))) else None,
                  "last_reason": None, "failure_reason": None, "error_type": None, "interrupted": False}
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue  # the first line of a tail can be cut in half
            if not isinstance(row, dict):
                continue
            reason = row.get("reason") if isinstance(row.get("reason"), str) and _CODE.fullmatch(row["reason"]) else None
            if reason:
                result["last_reason"] = reason
            if row.get("event") in {"invocation_started", "invocation_finished", "invocation_interrupted", "invocation_failed"}:
                result["interrupted"] = row["event"] == "invocation_interrupted"
            if row.get("event") in {"programme_stopped_for_recovery", "invocation_failed"}:
                error_type = row.get("error_type") if isinstance(row.get("error_type"), str) and _TOKEN.fullmatch(row["error_type"]) else None
                result["error_type"] = error_type
                result["failure_reason"] = reason or _error_code(error_type)
        return result
    return None


def operation_log_summary(root, *, since=None, max_bytes=512 * 1024 * 1024):
    """Read-only totals from saved run logs since one instant; diagnostics only.

    Counts are log records (the same rules as the live counters), not unique
    requests, products or bills. Files last changed before ``since`` are skipped
    unopened. Each log is read once: later readings reuse a small disposable
    speed copy of each unchanged log's totals (``run_log_summary``), so the
    reading stays quick and bounded however old the logs get. The speed copy
    is the only file written; no record authorizes any action.
    """
    from .run_log_summary import owner_totals
    return owner_totals(root, since=since, max_bytes=max_bytes)
