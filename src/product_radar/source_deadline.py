"""Outer source-step cancellation, distinct from retryable provider timeouts."""

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import signal
import threading
import time

_TRACE = ContextVar("source_phase_trace", default=None)
_DEADLINE = ContextVar("source_handler_deadline", default=None)


class SourceTrace:
    """One action's measured phases; these are diagnostics, not evidence."""
    def __init__(self, context):
        self.context = {name: context[name] for name in ("task_id", "route", "acquisition_id", "deadline_seconds") if name in context}
        self.phase = "preparation"
        self.phase_deadline = None
        self.durations = {}
        self.response_saved = False
        self.failure_phase = None


@contextmanager
def trace_source(context):
    trace = SourceTrace(context)
    token = _TRACE.set(trace)
    try:
        yield trace
    finally:
        _TRACE.reset(token)


@contextmanager
def source_phase(name):
    trace = _TRACE.get()
    if trace is None:
        yield
        return
    from .operations import record_event
    previous = trace.phase
    previous_deadline = trace.phase_deadline
    trace.phase = name
    # The callback's website allowance does not time the subsequent local
    # receipt save/application. Keep that distinction in advisory status too,
    # rather than describing a slow local write as an overdue website step.
    trace.phase_deadline = (trace.context.get("deadline_seconds")
                            if name in {"handler", "transport", "parse"} else None)
    context = {key: value for key, value in trace.context.items() if key != "deadline_seconds"}
    started = time.monotonic()
    record_event("phase_started", phase=name,
                 **dict(context, deadline_seconds=trace.phase_deadline))
    try:
        yield
    except BaseException:
        trace.failure_phase = trace.failure_phase or name
        raise
    finally:
        trace.durations[name] = trace.durations.get(name, 0) + time.monotonic() - started
        record_event("phase_finished", phase=name, next_phase=previous,
                     elapsed_seconds=trace.durations[name],
                     **dict(context, deadline_seconds=previous_deadline))
        trace.phase = previous
        trace.phase_deadline = previous_deadline


@contextmanager
def suspend_source_deadline():
    """Do not interrupt an atomic local write to meet a website deadline.

    This pauses only our currently owned outer handler timer. HTTP/browser
    timeouts and owner Stop remain unchanged. Nested writes cannot extend an
    unrelated timer or invent a new source allowance.
    """
    owned = _DEADLINE.get()
    if owned is None or owned["suspended"] or not owned["can_suspend"]:
        yield
        return
    owned["suspended"] = True
    remaining = signal.getitimer(signal.ITIMER_REAL)
    signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        yield
    finally:
        owned["suspended"] = False
        if remaining[0]:
            signal.setitimer(signal.ITIMER_REAL, *remaining)


def local_source_work(phase):
    """Measure local work in memory only; no log write under money locks."""
    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            trace = _TRACE.get()
            previous = trace.phase if trace else None
            nested_same_phase = trace is not None and previous == phase
            if trace:
                trace.phase = phase
            started = time.monotonic()
            try:
                with suspend_source_deadline():
                    return function(*args, **kwargs)
            except BaseException:
                if trace:
                    trace.failure_phase = trace.failure_phase or phase
                raise
            finally:
                if trace:
                    # An explicitly measured phase already includes this
                    # write. Do not add the same elapsed time twice.
                    if not nested_same_phase:
                        trace.durations[phase] = trace.durations.get(phase, 0) + time.monotonic() - started
                    trace.phase = previous
        return measured
    return decorate


@contextmanager
def bounded_source_deadline(timeout):
    from .models import SafetyError
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        raise SafetyError("SOURCE_OUTER_DEADLINE_UNAVAILABLE")
    prior_handler = signal.getsignal(signal.SIGALRM)
    prior_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    prior_due = started + prior_timer[0] if prior_timer[0] else None
    own_due = started + timeout
    # Do not suspend an alarm established by someone else (including an
    # enclosing callback). Its remaining time is restored using real elapsed
    # time. The usual single installed callback has no earlier alarm.
    token = _DEADLINE.set({"suspended": False, "can_suspend": not prior_timer[0]})
    def expired(_signum, _frame):
        nonlocal prior_due
        # An earlier alarm keeps its own handler and identity. In particular,
        # never resurrect an already-consumed one-shot alarm on exit.
        if prior_due is not None and prior_due <= own_due:
            prior_due = prior_due + prior_timer[1] if prior_timer[1] else None
            if callable(prior_handler):
                prior_handler(_signum, _frame)
            elif prior_handler == signal.SIG_DFL:
                signal.signal(signal.SIGALRM, prior_handler)
                signal.raise_signal(signal.SIGALRM)
            next_due = min(own_due, prior_due) if prior_due is not None else own_due
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, next_due - time.monotonic()))
            return
        error = SourceActionDeadline()
        trace = _TRACE.get()
        error.phase = trace.phase if trace else "handler"
        raise error
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(timeout, prior_timer[0]) if prior_timer[0] else timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior_handler)
        _DEADLINE.reset(token)
        if prior_due is not None:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, prior_due - time.monotonic()), prior_timer[1])


@contextmanager
def diagnostic_source_work():
    """Diagnostic file I/O is not part of a website's answer time."""
    token = _TRACE.set(None)
    try:
        with suspend_source_deadline():
            yield
    finally:
        _TRACE.reset(token)


class SourceActionDeadline(BaseException):
    """Stop the owned source callback without entering ordinary fallbacks.

    This is an outer execution-control signal, like cancellation, not an
    OSError or provider exception. Transport ``finally`` blocks still run;
    only the source controller consumes it after owned cleanup/recovery proof.
    KeyboardInterrupt and SystemExit remain separate unchanged signals.
    """

    def __init__(self):
        super().__init__("SOURCE_ACTION_TIMEOUT")
        self.native_result = None
        self.phase = "handler"


def interrupted_transport(error, *, folder=None, requested_url=None, completed_transport=None):
    """Retain proved native dispatch/cleanup, otherwise preserve uncertainty."""
    result = (deepcopy(error.native_result) if error.native_result is not None else
              {"state": "failed", "reason": "SOURCE_ACTION_TIMEOUT", "stage": "outer_deadline",
               "snapshot": None, "navigation_started": None, "navigation_started_at": None,
               "navigation_status": None, "owned_process_exited": None})
    result.update(outer_deadline=True, requested_url=requested_url or result.get("requested_url"),
                  completed_at=datetime.now(timezone.utc).isoformat())
    if error.native_result is None and isinstance(completed_transport, dict):
        # Extraction may be cancelled after the child has already returned.
        # Keep only execution metadata here, never its DOM/chart as an answer.
        result.update({field: completed_transport.get(field) for field in
            ("navigation_started", "navigation_started_at", "navigation_status", "owned_process_exited")})
    if folder is not None and requested_url is not None:
        from .models import SafetyError, timestamp
        from .storage import load_json, safe_path
        path = safe_path(safe_path(folder) / "native_dispatch.json")
        if path.is_file():
            try:
                dispatch = load_json(path, 16384)
                if (isinstance(dispatch, dict) and set(dispatch) == {"url", "navigation_started", "navigation_started_at"}
                        and dispatch["url"] == requested_url and dispatch["navigation_started"] is True
                        and timestamp(dispatch["navigation_started_at"]) <= timestamp(result["completed_at"])):
                    result.update(navigation_started=True, navigation_started_at=dispatch["navigation_started_at"])
            except (SafetyError, OSError, TypeError, ValueError):
                result["dispatch_proof_error"] = "SOURCE_DEADLINE_DISPATCH_UNVERIFIED"
    return result


def validate_interrupted_transport(value, *, requested_url):
    """Validate the marked cancellation shape, never accept source results."""
    from .models import SafetyError, timestamp
    if (not isinstance(value, dict) or not {"navigation_started", "owned_process_exited", "completed_at"} <= set(value)
            or value.get("outer_deadline") is not True
            or value.get("requested_url") != requested_url or value.get("snapshot") is not None
            or value.get("state") != "failed" or value.get("reason") != "SOURCE_ACTION_TIMEOUT"
            or value.get("stage") != "outer_deadline"
            or value.get("navigation_started") is not None and type(value.get("navigation_started")) is not bool
            or value.get("owned_process_exited") is not None and type(value.get("owned_process_exited")) is not bool):
        raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
    completed = timestamp(value.get("completed_at"))
    if value["navigation_started"] is True:
        if timestamp(value.get("navigation_started_at")) > completed:
            raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
    elif value.get("navigation_started_at") is not None:
        raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
    return value


def saved_completed_transport(folder, receipt, *, snapshot_file, requested_url, snapshot_limit):
    """Recover only data whose original interrupted receipt bound its bytes.

    The original failed receipt remains failed. A caller can make a separate
    saved-only parsing attribution from this captured/closed child outcome.
    An orphan native file, a changed hash or an uncertain child is not success.
    """
    from hashlib import sha256
    import re
    from datetime import timedelta
    from .models import SafetyError, timestamp
    from .storage import load_json, safe_path
    folder = safe_path(folder)
    if receipt.get("outer_deadline") is not True or receipt.get("reason") != "SOURCE_ACTION_TIMEOUT":
        return None
    artifacts = receipt.get("artifacts", {})
    def original(name, limit):
        if name not in artifacts:
            return None
        path = safe_path(folder / name)
        value = load_json(path, limit)
        if sha256(path.read_bytes()).hexdigest() != artifacts[name]:
            raise SafetyError("SOURCE_DEADLINE_ORIGINAL_HASH_MISMATCH")
        return value
    native = original("native_result.json", 12 * 1024 * 1024)
    transport = original("transport.json", snapshot_limit)
    if native is not None:
        actual = deepcopy(native)
    elif isinstance(transport, dict) and snapshot_file in artifacts:
        actual = deepcopy(transport)
        actual["snapshot"] = original(snapshot_file, snapshot_limit)
    else:
        return None
    if (not isinstance(actual, dict) or actual.get("state") != "captured"
            or actual.get("navigation_started") is not True
            or actual.get("owned_process_exited") is not True or not isinstance(actual.get("snapshot"), dict)):
        return None
    if not isinstance(transport, (dict, type(None))):
        raise SafetyError("SOURCE_DEADLINE_ORIGINAL_INVALID")
    for field in ("state", "navigation_started", "navigation_started_at", "navigation_status", "owned_process_exited", "completed_at"):
        if transport is not None and actual.get(field) != transport.get(field):
            raise SafetyError("SOURCE_DEADLINE_ORIGINAL_INVALID")
    if "native_dispatch.json" in artifacts:
        dispatch = original("native_dispatch.json", 16384)
        if (not isinstance(dispatch, dict) or dispatch.get("url") != requested_url
                or dispatch.get("navigation_started") is not True
                or dispatch.get("navigation_started_at") != actual.get("navigation_started_at")):
            raise SafetyError("SOURCE_DEADLINE_ORIGINAL_INVALID")
    elif not receipt.get("transport_attempts") or receipt["transport_attempts"][-1] != "synthetic_fixture":
        return None
    started, completed = timestamp(actual.get("navigation_started_at")), timestamp(actual.get("completed_at"))
    end = timestamp(receipt["completed_at"])
    # The old marketplace wrapper clock explicitly stores whole seconds.
    # Preserve its declared second interval; no general clock-drift allowance.
    whole_second = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})", receipt["completed_at"]))
    if whole_second:
        end += timedelta(seconds=1)
    if (not timestamp(receipt["started_at"]) <= started <= completed
            or (completed >= end if whole_second else completed > end)):
        raise SafetyError("SOURCE_DEADLINE_ORIGINAL_TIME_INVALID")
    snapshot = deepcopy(actual["snapshot"])
    html = snapshot.pop("html", None)
    if isinstance(html, str) and "rendered.html" in artifacts and sha256(html.encode("utf-8")).hexdigest() != artifacts["rendered.html"]:
        raise SafetyError("SOURCE_DEADLINE_ORIGINAL_INVALID")
    if snapshot_file in artifacts and snapshot != original(snapshot_file, snapshot_limit):
        raise SafetyError("SOURCE_DEADLINE_ORIGINAL_INVALID")
    return actual


def replay_native_deadline(folder, *, requested_url):
    """Read only an original, URL-bound native cancellation and dispatch proof.

    This cannot supply DOM/results or turn an uncertain launch into a proved
    untried action. Callers retain these exact bytes in their aggregate receipt.
    """
    from .models import SafetyError
    from .storage import load_json, safe_path
    folder = safe_path(folder)
    value = load_json(folder / "native_outer_deadline.json", 16384)
    validate_interrupted_transport(value, requested_url=requested_url)
    if type(value.get("launch_invoked")) is not bool:
        raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
    if value["navigation_started"] is False:
        if value["launch_invoked"] is not False or value["owned_process_exited"] is not True:
            raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
    elif value["launch_invoked"] is not True:
        raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
    dispatch_path = safe_path(folder / "native_dispatch.json")
    if dispatch_path.exists():
        dispatch = load_json(dispatch_path, 16384)
        if (not isinstance(dispatch, dict)
                or set(dispatch) != {"url", "navigation_started", "navigation_started_at"}
                or dispatch["url"] != requested_url or dispatch["navigation_started"] is not True
                or value["navigation_started"] is not True
                or dispatch["navigation_started_at"] != value["navigation_started_at"]):
            raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
    elif value["navigation_started"] is True:
        raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
    return value
