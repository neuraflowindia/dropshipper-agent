"""What each AI use cost: tokens, time and the allowance before and after.

One small ``accounting.json`` is saved in every AI job attempt folder that
called the model runner, and one ``ai_use_finished`` row goes to the run log.
The layers of one call each add what only they know, through a context-local
collector (no global state, no extra file per layer):

- ``CodexRunner``: when the Codex process started and ended, its exit code and
  the tokens the stream reported (also for a failed or timed-out run, when the
  stream got that far);
- ``AllowanceWatchedRunner``: the allowance readings it already takes before
  and after the call (no extra reading is ever made for accounting);
- ``Jobs.run``: the job, its purpose, mode, items and the outcome.

Accounting is observability only. It never decides, retries or refuses
anything, and a failure to save it never changes the research outcome.
Nothing here holds prompts, answers, page text, account ids or secrets.
"""

from contextvars import ContextVar
import re
import time

from .models import SafetyError, canonical, decode_json, utc_now

ACCOUNTING_FILE = "accounting.json"
KIND = "ai-use-accounting-v1"
MAX_ITEMS = 10
_NOTES = ContextVar("product_radar_ai_accounting", default=None)
_CONCEPT = re.compile(r"concept-[0-9a-f]{40}\Z")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens")
_WINDOW_NAMES = {300: "5-hour", 1440: "daily", 10080: "weekly"}


def note(**fields):
    """Add facts to the call being accounted; a no-op outside a ``CallAccounting``."""
    notes = _NOTES.get()
    if notes is not None:
        notes.update(fields)


def usage_tokens(usage):
    """The three token counts, or None when they are missing or inconsistent."""
    if (not isinstance(usage, dict) or any(type(usage.get(f)) is not int or usage[f] < 0 for f in _TOKEN_FIELDS)
            or usage["cached_input_tokens"] > usage["input_tokens"]):
        return None
    return {"input": usage["input_tokens"], "cached_input": usage["cached_input_tokens"],
            "output": usage["output_tokens"]}


def stream_usage(raw):
    """Usage from a possibly partial or failed ``codex exec --json`` stream.

    The last ``turn.completed`` usage wins; unreadable lines (a line cut in
    half when the process was stopped) are skipped. None when there is none.
    """
    found = None
    for line in (raw or b"").splitlines():
        try:
            event = decode_json(line)
        except (SafetyError, ValueError, TypeError, RecursionError):
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict) and all(type(usage.get(f)) is int and usage[f] >= 0 for f in _TOKEN_FIELDS):
                found = {f: usage[f] for f in _TOKEN_FIELDS}
    return found


def window_name(window):
    minutes = window.get("duration_minutes")
    return _WINDOW_NAMES.get(minutes, f"{minutes}-minute" if minutes else window.get("slot") or "unknown")


def allowance_reading(reading, *, at, source, from_cache=False, age_seconds=0.0):
    """One allowance reading as saved: windows, percent, reset times, when and from where.

    ``reading`` is ``spend_watch_ai.parse_rate_limits`` output (no account id).
    """
    windows = [{"window": window_name(w), "limit_id": w["limit_id"], "duration_minutes": w["duration_minutes"],
                "used_percent": w["used_percent"], "resets_at": w["resets_at"]} for w in reading["windows"]]
    credits = reading.get("credits") or {}
    return {"at": at, "source": source, "from_cache": bool(from_cache),
            "age_seconds": round(max(0.0, float(age_seconds)), 3), "windows": windows,
            "ordinary_usage_allowed": reading.get("ordinary_usage_allowed"),
            "limit_reached_signals": list(reading.get("reached") or []),
            "credits_balance": credits.get("balance")}


def job_ids(payload):
    """Item ids and the concept (product) ids a job's payload names, if any."""
    items = payload.get("items") if isinstance(payload, dict) else None
    item_ids, concepts = [], []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("id"), str) and _ID.fullmatch(item["id"]):
            item_ids.append(item["id"])
        product = item.get("product") if isinstance(item.get("product"), dict) else {}
        for value in (item.get("id"), item.get("concept_id"), product.get("concept_id")):
            if isinstance(value, str) and _CONCEPT.fullmatch(value) and value not in concepts:
                concepts.append(value)
    return {"item_ids": item_ids[:MAX_ITEMS], "concept_ids": concepts[:MAX_ITEMS]}


def _purpose(request, admission):
    if isinstance(admission, dict) and isinstance(admission.get("purpose"), str):
        return admission["purpose"]
    try:
        from .work_turns import model_purpose
        return model_purpose(request)
    except (KeyError, TypeError, AttributeError):
        return None


def build_record(*, job_id, attempt, request, admission, notes, outcome, started_at, finished_at, call_seconds,
                 returned_usage=None):
    """The saved accounting of one runner call. ``outcome`` is "completed" or a failure code."""
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    mode = payload.get("mode") if isinstance(payload.get("mode"), str) else None
    tokens = usage_tokens(returned_usage) or usage_tokens(notes.get("usage"))
    model_started = notes.get("model_started_at")
    if tokens is not None:
        tokens_missing = None
    elif model_started is None:
        tokens_missing = "model_not_started"
    elif notes.get("usage_source") is None and "model_finished_at" in notes:
        tokens_missing = "stream_reported_no_usage"
    else:
        tokens_missing = "not_reported_by_runner"
    model = None
    if model_started is not None:
        model = {"started_at": model_started, "finished_at": notes.get("model_finished_at"),
                 "seconds": notes.get("model_seconds"), "exit_code": notes.get("exit_code"),
                 "usage_source": notes.get("usage_source") if tokens is not None else None}
    allowance = {"before": notes.get("allowance_before"), "after": notes.get("allowance_after")}
    if allowance["before"] is None:
        allowance["before_missing_reason"] = notes.get("allowance_before_missing") or "not_read_by_runner"
    if allowance["after"] is None:
        allowance["after_missing_reason"] = notes.get("allowance_after_missing") or "not_read_by_runner"
    ids = job_ids(payload)
    return {"schema_version": 1, "kind": KIND, "job_id": job_id, "attempt": attempt,
            "role": request.get("role"), "purpose": _purpose(request, admission), "mode": mode,
            "prompt_version": request.get("prompt_version"), "runner": request.get("runner"),
            "model_name": request.get("model"),
            "turn_id": admission.get("origin_turn_id") if isinstance(admission, dict) else None,
            "item_count": len(payload.get("items") or []), **ids,
            "started_at": started_at, "finished_at": finished_at, "call_seconds": round(call_seconds, 3),
            "model": model, "model_missing_reason": None if model else "model_not_started",
            "tokens": tokens, "tokens_missing_reason": tokens_missing,
            "outcome": outcome, "allowance": allowance}


def log_values(record):
    """The run-log row of one AI use: ids, purpose and numbers only."""
    tokens = record["tokens"] or {}
    model = record["model"] or {}
    values = {"job_id": record["job_id"], "role": record["role"], "purpose": record["purpose"],
              "mode": record["mode"], "prompt_version": record["prompt_version"], "attempt": record["attempt"],
              "item_count": record["item_count"], "call_seconds": record["call_seconds"],
              "state": "completed" if record["outcome"] == "completed" else "failed"}
    if record["outcome"] != "completed":
        values["reason"] = record["outcome"]
    if len(record["concept_ids"]) == 1:
        values["concept_id"] = record["concept_ids"][0]
    if type(model.get("seconds")) in (int, float):
        values["model_seconds"] = model["seconds"]
    for name, field in (("input", "input_tokens"), ("cached_input", "cached_input_tokens"), ("output", "output_tokens")):
        if type(tokens.get(name)) is int:
            values[field] = tokens[name]
    return values


def save_and_log(folder, record):
    """Save the record beside the job's other files, then add its run-log row.

    Best effort by design: accounting is never a reason for research to stop.
    Returns True when the file was saved.
    """
    from .operations import record_event
    from .storage import atomic_json
    saved = False
    try:
        canonical(record)
        atomic_json(folder / ACCOUNTING_FILE, record)
        saved = True
    except (OSError, SafetyError, TypeError, ValueError):
        saved = False
    record_event("ai_use_finished", accounting_saved=saved, **log_values(record))
    return saved


class CallAccounting:
    """The accounting of one runner call, from just before it to just after.

    Created right before ``Jobs.run`` calls its runner (it starts collecting
    the layers' notes); ``finish`` is called once in a ``finally`` and saves
    the record and its run-log row. It never raises.
    """

    def __init__(self):
        self.notes = {}
        self.token = _NOTES.set(self.notes)
        self.started_at, self.clock = utc_now(), time.monotonic()
        self.outcome = "AI_LOCAL_EXECUTION_FAILED"  # replaced by "completed" or the saved failure code
        self.usage = None

    def finish(self, *, job_id, base, folder, request):
        try:
            _NOTES.reset(self.token)
        except ValueError:  # finished from another context: the notes are still complete
            pass
        from .storage import load_json
        try:
            intent = load_json(folder / "intent.json", 256 * 1024)
        except (OSError, SafetyError, ValueError):
            intent = None  # the purpose then comes from the request itself
        try:
            admission = intent.get("turn_admission") if isinstance(intent, dict) else None
            record = build_record(job_id=job_id, attempt="original" if folder == base else folder.name,
                                  request=request, admission=admission, notes=self.notes, outcome=self.outcome,
                                  started_at=self.started_at, finished_at=utc_now(),
                                  call_seconds=time.monotonic() - self.clock, returned_usage=self.usage)
            return save_and_log(folder, record)
        except Exception:  # observability never changes a research outcome
            return False
