"""AI usage windows (flag only) and the Codex paid-credits money guards.

Usage is read with the installed Codex app-server request
``account/rateLimits/read``: no model call, no session file. Only window
percentages, reset times, limit-reached signals, the plan name and the credits
balance are kept; the account id and the local Codex folder are dropped. The
app-server loads the owner's Codex settings (it has no ignore-settings option);
it receives only the shared Codex environment allowlist and is stopped after
each read.

Owner decision A4: research AI never pauses for allowance use as such; every
window Codex reports is flagged at 50/75/90/100 percent. Paid credits are
money. Once a plan limit is reached, Codex draws from the credits balance, so
a call that could draw credits is refused before its job intent is saved
(``AI_PLAN_LIMIT_CREDITS_PROTECTED``) until the full window resets and a fresh
read shows ordinary usage allowed again. If credits still fall across a
research call while a limit was reached, AI work waits (``AI_PAID_CREDITS_USED``)
until the next reset and resumes only if the balance is unchanged. A fall
while ordinary usage was allowed before and after is the owner's own shared
Codex use: a notice, not a pause. No call is admitted without a successful
read (``AI_USAGE_READ_FAILED``, with a growing back-off).
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import time

from .models import SafetyError, canonical, decode_json, timestamp, utc_now
from .spend_watch import alert, clear_wait, load_settings, load_state, observe, set_wait, writing

METHOD = "account/rateLimits/read"
READ_SECONDS = 20
READ_RETRY_SECONDS = 2
MAX_READ_BYTES = 1024 * 1024
CACHE_SECONDS = 60
RESET_JITTER = timedelta(minutes=10)
READ_BACKOFF_MINUTES = (5, 10, 20, 60)
READ_FAILURE_NOTICE = 3
PAUSE_REASON = "AI_PAID_CREDITS_USED"
PROTECT_REASON = "AI_PLAN_LIMIT_CREDITS_PROTECTED"
READ_FAILED = "AI_USAGE_READ_FAILED"
WATCH_UNAVAILABLE = "AI_SPEND_WATCH_UNAVAILABLE"
LABEL = "Codex paid credits"
_LIMIT_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_REACHED = re.compile(r"[a-z_]{1,80}")


def _reset_time(value):
    if value is None:
        return None
    if type(value) is not int or not 0 < value < 2 ** 41:
        raise SafetyError("AI_USAGE_READ_INVALID")
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _percent(value):
    if type(value) is float and value.is_integer():
        value = int(value)  # a whole-number float such as 24.0
    if type(value) is not int or not 0 <= value <= 10000:
        raise SafetyError("AI_USAGE_READ_INVALID")
    return value


def _window(limit_id, slot, value):
    if (not isinstance(value, dict)
            or value.get("windowDurationMins") is not None and (type(value["windowDurationMins"]) is not int
                                                               or not 0 < value["windowDurationMins"] <= 10 ** 6)):
        raise SafetyError("AI_USAGE_READ_INVALID")
    minutes = value.get("windowDurationMins")
    return {"key": f"{limit_id}-{minutes or slot}", "limit_id": limit_id, "slot": slot, "duration_minutes": minutes,
            "used_percent": _percent(value.get("usedPercent")), "resets_at": _reset_time(value.get("resetsAt"))}


def _credits(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or type(value.get("hasCredits")) is not bool
            or type(value.get("unlimited")) is not bool):
        raise SafetyError("AI_USAGE_READ_INVALID")
    balance = value.get("balance")
    if balance is not None:
        try:
            number = Decimal(balance) if isinstance(balance, str) and len(balance) <= 40 else None
        except InvalidOperation:
            number = None
        if number is None or not number.is_finite() or number < 0:
            raise SafetyError("AI_USAGE_READ_INVALID")
        balance = format(number.normalize(), "f") if number else "0"
    return {"balance": balance, "has_credits": value["hasCredits"], "unlimited": value["unlimited"]}


def parse_rate_limits(response):
    """Windows, limit signals, plan and credits from one reply; no identifiers."""
    result = response.get("result") if isinstance(response, dict) else None
    single = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(single, dict):
        raise SafetyError("AI_USAGE_READ_INVALID")
    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        buckets = {single.get("limitId") or "codex": single}
    if len(buckets) > 20:
        raise SafetyError("AI_USAGE_READ_INVALID")
    windows, credits, plan, reached = [], None, None, []
    for limit_id, bucket in sorted(buckets.items()):
        if not isinstance(limit_id, str) or not _LIMIT_ID.fullmatch(limit_id) or not isinstance(bucket, dict):
            raise SafetyError("AI_USAGE_READ_INVALID")
        for slot in ("primary", "secondary"):
            if bucket.get(slot) is not None:
                windows.append(_window(limit_id, slot, bucket[slot]))
        if credits is None or limit_id == "codex":
            credits = _credits(bucket.get("credits")) or credits
        if isinstance(bucket.get("planType"), str) and _LIMIT_ID.fullmatch(bucket["planType"]):
            plan = bucket["planType"]
        kind = bucket.get("rateLimitReachedType")
        if kind is not None:
            if not isinstance(kind, str) or not _REACHED.fullmatch(kind):
                raise SafetyError("AI_USAGE_READ_INVALID")
            reached.append(limit_id + ":" + kind)
    allowed = result.get("ordinaryUsageAllowed")
    return {"windows": windows, "credits": credits, "plan_type": plan, "reached": reached,
            "ordinary_usage_allowed": allowed if type(allowed) is bool else None}


def _send(process, message):
    try:
        process.stdin.write(canonical(message) + b"\n")
        process.stdin.flush()
    except (OSError, ValueError):
        raise SafetyError("AI_USAGE_READ_FAILED") from None


def _receive(process, request_id, deadline, buffer):
    descriptor = process.stdout.fileno()
    while True:
        while b"\n" in buffer:
            line, _, rest = bytes(buffer).partition(b"\n")
            buffer[:] = rest
            if not line.strip():
                continue
            message = decode_json(line)
            # A server request or notification can carry an id too; only a reply counts.
            if isinstance(message, dict) and message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise SafetyError("AI_USAGE_READ_REJECTED")
                return message
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SafetyError("AI_USAGE_READ_TIMEOUT")
        ready, _, _ = select.select([descriptor], [], [], min(remaining, 1))
        if not ready:
            continue
        chunk = os.read(descriptor, 65536)
        if not chunk:
            raise SafetyError("AI_USAGE_READ_FAILED")
        buffer.extend(chunk)
        if len(buffer) > MAX_READ_BYTES:
            raise SafetyError("AI_USAGE_READ_INVALID")


def _stop(process, *, wait_seconds=5):
    """Stop the owned process group; a process that cannot be reaped is a failed read."""
    try:
        process.stdin.close()
    except OSError:
        pass
    for sent in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sent)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=wait_seconds)
            break
        except subprocess.TimeoutExpired:
            continue
    else:
        process.stdout.close()
        raise SafetyError("AI_USAGE_READ_FAILED")
    process.stdout.close()


def read_rate_limits(binary, *, env=None, seconds=READ_SECONDS):
    """One owned app-server process: initialize, read usage, stop it."""
    if not isinstance(binary, str) or not Path(binary).is_absolute():
        raise SafetyError("AI_CODEX_NOT_INSTALLED")
    from .research_ai import codex_environment
    try:
        process = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=codex_environment() if env is None else env,
                                   start_new_session=True)
    except OSError:
        raise SafetyError("AI_USAGE_READ_FAILED") from None
    deadline, buffer, reading = time.monotonic() + seconds, bytearray(), None
    try:
        _send(process, {"id": 1, "method": "initialize",
                        "params": {"clientInfo": {"name": "neuraflow_spend_watch", "version": "1"}}})
        _receive(process, 1, deadline, buffer)
        _send(process, {"method": "initialized"})
        _send(process, {"id": 2, "method": METHOD, "params": {"excludeResetCreditDetails": True}})
        reading = parse_rate_limits(_receive(process, 2, deadline, buffer))
    finally:
        _stop(process)
    return reading


def _when(value):
    return timestamp(value).astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def _window_label(window):
    minutes = window["duration_minutes"]
    name = {300: "5-hour", 1440: "daily", 10080: "weekly"}.get(minutes, f"{minutes}-minute" if minutes else window["slot"])
    prefix = "" if window["limit_id"] == "codex" else f" ({window['limit_id']})"
    return f"AI {name} allowance{prefix}"


def _window_period(old, window, at):
    """A window period lasts until its reset; small shifts in the reported reset are the same period."""
    reset = window["resets_at"]
    previous = (old or {}).get("details", {}).get("resets_at")
    if old and reset and previous and timestamp(at) < timestamp(previous) \
            and abs(timestamp(reset) - timestamp(previous)) <= RESET_JITTER:
        return old["period"]
    if old and not reset and not previous:
        return old["period"]
    return reset or "reset-time-not-reported"


def limit_reached(reading):
    """Codex would draw paid credits: ordinary use refused, a window full, or a limit signal."""
    return (reading["ordinary_usage_allowed"] is False or bool(reading["reached"])
            or any(w["used_percent"] >= 100 for w in reading["windows"]))


def credits_drawable(reading):
    """Only an explicit "no credits" (and no positive balance) is safe."""
    credits = reading["credits"]
    if credits is None or credits["unlimited"] or credits["has_credits"]:
        return True
    return credits["balance"] is not None and Decimal(credits["balance"]) > 0


def _future(times, at, pick):
    times = [t for t in times if t and timestamp(t) > timestamp(at)]
    return pick(times, key=timestamp) if times else None


def _full_window_reset(reading, at):
    """The reset of the full window(s); without one, the earliest reset; else recheck in an hour."""
    full = _future([w["resets_at"] for w in reading["windows"] if w["used_percent"] >= 100], at, max)
    return (full or _future([w["resets_at"] for w in reading["windows"]], at, min)
            or (timestamp(at) + timedelta(hours=1)).isoformat())


def _next_reset(reading, at):
    return (_future([w["resets_at"] for w in reading["windows"] if w["used_percent"] >= 100], at, min)
            or _future([w["resets_at"] for w in reading["windows"]], at, min)
            or (timestamp(at) + timedelta(days=1)).isoformat())


def _balance(reading):
    credits = reading.get("credits")
    return Decimal(credits["balance"]) if credits and credits["balance"] is not None else None


def _pause(state, alerts, reading, at, balance):
    until = _next_reset(reading, at)
    message = (f"Research AI used paid Codex credits while the plan limit was reached. AI work waits until {_when(until)} "
               "and resumes then if no more credits are used; free research continues.")
    old = state["waits"].get("ai")
    set_wait(state, alerts, "ai", reason=PAUSE_REASON, at=at, label=LABEL, message=message,
             until=until, balance=str(balance))
    if old and old["reason"] == PAUSE_REASON:  # an existing pause re-armed for the following reset
        alert(alerts, item="ai", label=LABEL, kind="paused", at=at, message=message)


def _protect(state, alerts, reading, at):
    until = _full_window_reset(reading, at)
    set_wait(state, alerts, "ai", reason=PROTECT_REASON, at=at, label=LABEL, until=until,
             message=f"AI waiting so it doesn't spend paid Codex credits (plan limit reached; resumes at {_when(until)}). "
                     "Free research continues.")


def _compare_pending(state, alerts, reading, at, balance, reached):
    ai = state["ai"]
    pending = ai.get("pending_call")
    if not pending:
        return
    if pending["balance"] is None:
        ai.pop("pending_call")  # there was provably no credit balance to protect
    elif balance is None:
        if not credits_drawable(reading):
            ai.pop("pending_call")
    else:
        before = Decimal(pending["balance"])
        if balance < before:
            if pending["limit_reached"] or reached:
                _pause(state, alerts, reading, at, balance)
            else:
                alert(alerts, item="ai", label=LABEL, kind="notice", period=at, at=at,
                      message=f"The Codex credits balance fell by {before - balance} during a research call while the plan "
                              "allowance was still available, so this is most likely your own Codex use. Research AI continues.")
        ai.pop("pending_call")


def record_ai_reading(state, alerts, reading, at):
    """Flag every reported window; apply the paid-credit guards and their resume rules."""
    keys = set()
    for window in reading["windows"]:
        key = "ai-" + window["key"]
        label = _window_label(window)
        reset = f", resets {_when(window['resets_at'])}" if window["resets_at"] else ""
        observe(state, alerts, key, label=label, period=_window_period(state["items"].get(key), window, at),
                used=window["used_percent"], allowance=100, unit="percent", at=at,
                text=f"{label}: {window['used_percent']}% used{reset}.",
                details={"resets_at": window["resets_at"], "duration_minutes": window["duration_minutes"]})
        keys.add(key)
    for key in [key for key in state["items"] if key.startswith("ai-") and key not in keys]:
        del state["items"][key]  # includes a cleared usage-read failure line
    ai, balance, reached = state["ai"], _balance(reading), limit_reached(reading)
    if balance is not None:
        ai["last_known_balance"] = str(balance)
    _compare_pending(state, alerts, reading, at, balance, reached)
    wait = state["waits"].get("ai")
    if wait and wait["reason"] == PAUSE_REASON and timestamp(wait["until"]) <= timestamp(at):
        if balance is not None and balance == Decimal(wait["balance"]):
            clear_wait(state, alerts, "ai", at=at, label=LABEL,
                       message="No more paid Codex credits were used; research AI work has resumed.")
        else:
            _pause(state, alerts, reading, at, balance if balance is not None else Decimal(wait["balance"]))
    wait = state["waits"].get("ai")
    if reached and credits_drawable(reading):
        if not wait or wait["reason"] == PROTECT_REASON:
            _protect(state, alerts, reading, at)
    elif wait and wait["reason"] == PROTECT_REASON:
        if reading["ordinary_usage_allowed"] is True:
            clear_wait(state, alerts, "ai", at=at, label=LABEL,
                       message="The plan allowance is available again; research AI work has resumed.")
        else:  # recovery is never inferred from percentages alone
            _protect(state, alerts, reading, at)
    credits, wait = reading["credits"], state["waits"].get("ai")
    text = f"{LABEL}: balance {credits['balance']}; " if credits and credits["balance"] is not None else f"{LABEL}: "
    text += ("AI waiting because research used paid credits." if wait and wait["reason"] == PAUSE_REASON else
             f"AI waiting so it doesn't spend paid Codex credits (plan limit reached; resumes at {_when(wait['until'])})."
             if wait else "not used by research.")
    state["items"]["codex-credits"] = {"label": LABEL, "period": "none", "used": "0", "allowance": "0", "unit": "credits",
        "percent": None, "flags": [], "updated_at": at, "text": text,
        "details": {"balance": credits["balance"] if credits else None, "plan_type": reading["plan_type"],
                    "ordinary_usage_allowed": reading["ordinary_usage_allowed"], "limit_reached": reached}}
    ai["last_reading_at"] = at
    ai.pop("read_failures", None)


def record_read_failure(state, alerts, reason, at):
    """Back off 5, 10, 20 then 60 minutes; after three failures in a row, tell the owner."""
    failures = state["ai"].get("read_failures") or {"count": 0, "since": at}
    count = failures["count"] + 1
    delay = READ_BACKOFF_MINUTES[min(count, len(READ_BACKOFF_MINUTES)) - 1]
    retry = (timestamp(at) + timedelta(minutes=delay)).isoformat()
    state["ai"]["read_failures"] = {"count": count, "since": failures["since"], "reason": reason, "next_attempt_at": retry}
    if count >= READ_FAILURE_NOTICE:
        text = (f"Codex usage could not be read {count} times in a row, so research AI waits (next try {_when(retry)}). "
                "Check that Codex is installed and signed in.")
        state["items"]["ai-usage-read"] = {"label": "AI usage reading", "period": failures["since"], "used": "0",
            "allowance": "0", "unit": "reads", "percent": None, "flags": [], "updated_at": at, "text": text,
            "details": {"reason": reason}}
        if count == READ_FAILURE_NOTICE:
            alert(alerts, item="ai-usage-read", label="AI usage reading", kind="notice", period=failures["since"],
                  at=at, message=text)


class AllowanceWatchedRunner:
    """Wraps a Codex runner: flags usage windows and guards paid credits.

    ``check_new_execution`` runs before a job's intent is saved; ``__call__``
    re-admits, marks the call, runs it and takes the after-call reading. A
    failed after-call reading leaves the mark, so the next successful reading
    still compares against the balance from before that call.
    """

    def __init__(self, runner, *, root, reader, clock=utc_now, settings=None, sleep=time.sleep):
        if not callable(runner) or not callable(reader):
            raise SafetyError("AI_CONFIGURATION_CHANGED")
        self.runner, self.root, self.reader, self.clock, self.sleep = runner, Path(root), reader, clock, sleep
        self.identity = getattr(runner, "identity", "codex-chatgpt-high-v1")
        self._settings, self._cached = settings, None

    def _write(self, change):
        try:
            settings = self._settings or load_settings()
            with writing(self.root, settings) as (state, alerts):
                return change(state, alerts)
        except (SafetyError, OSError, KeyError, TypeError, ValueError):
            raise SafetyError(WATCH_UNAVAILABLE) from None

    def _read(self, at):
        """One read, retried once after a short pause; failures back off."""
        try:
            reading = self.reader()
        except SafetyError:
            self.sleep(READ_RETRY_SECONDS)
            try:
                reading = self.reader()
            except SafetyError as error:
                reason = str(error) if str(error).startswith("AI_") else READ_FAILED
                self._write(lambda state, alerts: record_read_failure(state, alerts, reason, at))
                self._cached = None
                raise SafetyError(READ_FAILED) from None
        self._cached = (time.monotonic(), reading)
        return reading

    def _admit(self, *, mark):
        at = self.clock(); timestamp(at)
        try:
            state = load_state(self.root) or {}
        except (SafetyError, OSError):
            raise SafetyError(WATCH_UNAVAILABLE) from None
        wait = state.get("waits", {}).get("ai")
        if wait and timestamp(wait["until"]) > timestamp(at):
            raise SafetyError(wait["reason"])
        failures = state.get("ai", {}).get("read_failures")
        if failures and timestamp(failures["next_attempt_at"]) > timestamp(at):
            raise SafetyError(READ_FAILED)
        cached = self._cached
        fresh = (wait is not None or state.get("ai", {}).get("pending_call") is not None
                 or cached is None or time.monotonic() - cached[0] > CACHE_SECONDS)
        reading = self._read(at) if fresh else cached[1]

        def admit(state, alerts):
            if fresh:
                record_ai_reading(state, alerts, reading, at)
            if "ai" in state["waits"]:
                return state["waits"]["ai"]["reason"]
            balance = _balance(reading)
            if balance is None and credits_drawable(reading):
                known = state["ai"].get("last_known_balance")
                if known is None:
                    return READ_FAILED  # credits may exist but no balance was ever seen: fail closed
                balance = Decimal(known)
            if mark and "pending_call" not in state["ai"]:  # an unresolved earlier mark is the older baseline
                state["ai"]["pending_call"] = {"at": at, "balance": None if balance is None else str(balance),
                                               "limit_reached": limit_reached(reading)}
            return None
        refused = self._write(admit)
        if refused:
            raise SafetyError(refused)

    def check_new_execution(self):
        guard = getattr(self.runner, "check_new_execution", None)
        if guard is not None:
            guard()
        self._admit(mark=False)

    def __call__(self, role, payload, folder):
        self._admit(mark=True)
        try:
            value = self.runner(role, payload, folder)
        except (KeyboardInterrupt, SystemExit):
            raise  # an owner Stop is not delayed; the saved mark is compared later
        except BaseException:
            self._settle()
            raise
        self._settle()
        return value

    def _settle(self):
        at = self.clock()
        try:
            reading = self._read(at)
            self._write(lambda state, alerts: record_ai_reading(state, alerts, reading, at))
        except SafetyError:
            pass  # the mark stays; the next admission reads again and fails closed if it cannot


def watched_codex_runner(runner, config, *, root):
    """A Codex runner with the usage reader bound to its own program."""
    from .research_ai import resolve_codex_executable
    return AllowanceWatchedRunner(runner, root=root,
                                  reader=lambda: read_rate_limits(resolve_codex_executable(config)))


def research_codex_runner(config, *, root, stop_on_interrupt=False):
    """The one way every live command builds its Codex runner: always money-guarded."""
    from .research_ai import CodexRunner
    return watched_codex_runner(CodexRunner(config, stop_on_interrupt=stop_on_interrupt), config, root=root)


def ai_paid_credit_wait(root):
    """Read-only: the saved AI money wait (either guard), or None."""
    state = load_state(root)
    return deepcopy(state["waits"].get("ai")) if state else None
