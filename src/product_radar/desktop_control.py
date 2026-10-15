"""Small local controls for the Windows tray app; never a second research engine.

Status does not replay research history, call a provider, or write a heartbeat.
Stop persists the existing owner pause before interrupting only identified
programme processes. Original attempts and spending allowances stay intact.
The worker watches its own research process: when the saved activity shows
no progress for the configured time, it interrupts only that process (its own
new session) and leaves the owner's switch on for the next scheduled run.
"""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

from .models import SafetyError, canonical, timestamp, utc_now
from .programme import programme_service_state, set_programme_service_state
from .runtime_storage import runtime_path
from .storage import file_lock, safe_path


def process_info(pid, *, proc=Path("/proc")):
    """Include the kernel start identity; an old PID is not permission to stop it."""
    try:
        directory = proc / str(int(pid))
        fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {"pid": int(pid), "parent": int(fields[1]), "start_ticks": fields[19],
                "argv": [part.decode("utf-8", "replace") for part in
                         (directory / "cmdline").read_bytes().split(b"\0") if part],
                "cwd": str((directory / "cwd").resolve(strict=True))}
    except (OSError, ValueError, IndexError):
        return None


def _same_path(left, right):
    """Same file or folder, even when a case-insensitive Windows drive spells it
    differently (the scheduled task launches from a lower-case path)."""
    left, right = Path(left), Path(right)
    if left == right:
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def owned_programmes(root, *, proc=Path("/proc")):
    root = Path(root).resolve()
    script = root / "scripts/run_programme.py"
    result = []
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        row = process_info(entry.name, proc=proc)
        if row is None or not _same_path(row["cwd"], root):
            continue
        # Require the actual Python script argument, not a substring in a
        # shell, diagnostic command, another repository or a model prompt.
        args = row["argv"]
        if len(args) < 2 or not Path(args[0]).name.startswith("python"):
            continue
        position = 1
        while position < len(args) and args[position] in ("-u", "-B", "-I", "-E", "-s", "-S"):
            position += 1
        if position >= len(args) or args[position].startswith("-"):
            continue  # -c/-m and shell commands are never owned script runs.
        candidate = Path(args[position])
        if not candidate.is_absolute():
            candidate = root / candidate
        if _same_path(candidate.resolve(), script) and "--execute" in args:
            result.append(row)
    return result


def _memory(root):
    return SimpleNamespace(root=runtime_path(root, "data/research/captured"))


def _now():
    return datetime.now(timezone.utc)


def _boot_id():
    from .operations import boot_id
    return boot_id()


def _awake_now(now):
    """This computer's awake-time clock, only with the real wall clock (an
    injected test clock is used on its own)."""
    return time.monotonic() if now is _now else None


def _read_activity(root, at, now):
    from .operations import read_operation_status
    return read_operation_status(root, as_of=at.isoformat(timespec="seconds"), awake_now=_awake_now(now))


# How long a warning must stay on screen, counting only time the computer is
# awake, before the window shows its one notification. A late schedule waits
# longer than one 15-minute task interval.
CONFIRM_SECONDS = {"run": 180, "repeat": 180, "schedule": 1200}


def _attention(text, identity, *, notify=True, confirm="run"):
    return {"text": text, "id": identity, "notify": notify, "confirm_seconds": CONFIRM_SECONDS[confirm]}


def _process_age(row, *, proc=Path("/proc")):
    """Seconds since an identified process started, or None."""
    try:
        ticks = int(row["start_ticks"])
        uptime = float((proc / "uptime").read_text().split()[0])
        return max(0.0, uptime - ticks / os.sysconf("SC_CLK_TCK"))
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _owned_quiet(activity, rows):
    """For each identified research process: its quiet time and the saved mark
    it is measured from. The saved activity counts only when it belongs to that
    exact process (PID and start identity), whatever its writer flag says;
    otherwise the process has saved nothing, so its age is its quiet time."""
    result = []
    for row in rows:
        if (activity.get("pid") == row.get("pid") and row.get("start_ticks") is not None
                and activity.get("process_start_ticks") == row.get("start_ticks")
                and type(activity.get("seconds_since_activity")) in (int, float)):
            result.append((row, activity["seconds_since_activity"], activity.get("observed_at")))
        else:
            age = _process_age(row)
            if age is not None:
                result.append((row, age, "started:" + str(row.get("start_ticks"))))
    return result


def status(root, *, now=_now):
    control = programme_service_state(_memory(root))
    processes = owned_programmes(root)
    enabled, active = control["enabled"], bool(processes)
    state = "running" if enabled and active else "ready" if enabled else "stopping" if active else "stopped"
    message = {"running": "Research is running.",
               "ready": "Research is on. Waiting for the next scheduled run.",
               "stopping": "Stopping the current research step. New work is switched off.",
               "stopped": "Research is stopped. Your saved work is kept."}[state]
    from .operations import no_activity_text, progress_watch_or_defaults
    at = now()
    activity = _read_activity(root, at, now)
    attention = []
    if state == "running" and activity.get("state") == "running":
        message += " " + activity["message"]
        if activity.get("last_useful_result_at") is None:
            message += " No completed result has been recorded in this run yet."
        if activity.get("attention"):
            # One notification per run and kind of warning; "no completed
            # result" is window text only (long healthy runs show it too).
            attention.append(_attention(activity["attention"], activity["run_id"] + ":" + activity["attention_kind"],
                                        notify=activity.get("attention_notify") is True))
    elif active:
        # A research process exists but its saved activity is not a running
        # main step: it may hang before or after that step.
        watch = progress_watch_or_defaults()[0]
        for row, quiet, _ in _owned_quiet(activity, processes):
            if quiet >= watch["attention_no_activity_seconds"]:
                attention.append(_attention(no_activity_text(quiet, watch),
                    f"stale:{row.get('pid')}:{row.get('start_ticks')}:no_activity"))
                break
    if state in ("ready", "stopped"):
        try:
            health = run_health(root, activity, now=at, check_schedule=state == "ready", awake_now=_awake_now(now))
        except (OSError, SafetyError):
            health = {"line": "The earlier research runs could not be read. See the project log.", "attention": []}
        if health["line"]:
            message += "\n" + health["line"]
        attention.extend(health["attention"])
    message += "".join("\nNeeds attention: " + item["text"] for item in attention)
    notify = [item for item in attention if item["notify"]]
    spend = spend_summary(root)
    return {"schema_version": 2, "state": state, "enabled": enabled,
            "active_processes": len(processes), "message": message, "checked_at": utc_now(), "activity": activity,
            "attention": " ".join(item["text"] for item in notify) or None,
            "attention_id": "|".join(item["id"] for item in notify) or None,
            "attention_confirm_seconds": max((item["confirm_seconds"] for item in notify), default=None),
            "sign_in": sign_in_summary(root), "spend": spend["lines"], "spend_alerts": spend["alerts"]}


def desktop_actions(root, *, days=7, max_bytes=2 * 1024 * 1024):
    """The last few days of desktop action receipts, oldest first. Read only;
    unreadable lines are skipped, never trusted."""
    directory = safe_path(runtime_path(root, "data/operations"), "desktop")
    rows = []
    if not directory.is_dir():
        return rows
    files = sorted(path for path in directory.glob("????-??-??.jsonl") if path.is_file() and not path.is_symlink())
    for path in files[-days:]:
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - max_bytes))
                lines = stream.read().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                if isinstance(row, dict) and isinstance(row.get("action"), str):
                    timestamp(row.get("at"))
                    rows.append(row)
            except (ValueError, SafetyError):
                continue
    return sorted(rows, key=lambda row: timestamp(row["at"]))


_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,159}\Z")


def _code(value):
    """A saved reason code, only when it has the plain code shape."""
    return value if isinstance(value, str) and _REASON_CODE.match(value) else None


def _failure_key(row):
    """The failure a finished worker receipt shows, or None for a normal end
    (including the owner's own Stop)."""
    outcome, code = row.get("outcome"), row.get("exit_code")
    if outcome == "watchdog_restarted":
        return "PROGRESS_WATCH_RESTART"
    if outcome in ("finished", "owner_stopped", "interrupted") or code == 0 or outcome is None and code in (130, -2):
        return None
    return _code(row.get("failure_reason")) or "WORKER_EXIT_FAILED"


def _last_run_line(row):
    from .operations import plain_reason
    code = row.get("exit_code")
    exit_text = f" (exit code {code})" if type(code) is int else ""
    note = plain_reason(_code(row.get("last_reason")))
    failure = _failure_key(row)
    if row.get("outcome") == "watchdog_restarted":
        quiet = row.get("seconds_since_activity")
        quiet = f" for {round(quiet / 60)} minutes" if type(quiet) in (int, float) and quiet >= 0 else ""
        return (f"Last run stopped making progress{quiet}, so it was stopped{exit_text}. "
                "The next scheduled run starts research again.")
    if row.get("outcome") in ("owner_stopped", "interrupted") or failure is None and code != 0:
        return f"Last run was stopped{exit_text}."
    if failure:
        return f"Last run stopped with a problem: {plain_reason(failure)}{exit_text}."
    return f"Last run finished normally{exit_text}." + (f" Its last note: {note}." if note else "")


def run_health(root, activity=None, *, now=None, check_schedule=True, settings=None, awake_now=None):
    """Plain lines about earlier runs while nothing is running.

    The previous run's ending (reason and exit code), the same failure in
    several runs in a row, and a scheduled run that is overdue. Read only.
    """
    from .operations import plain_reason, progress_watch_or_defaults
    at = now or _now()
    watch = settings or progress_watch_or_defaults()[0]
    try:
        actions = desktop_actions(root)
    except (OSError, SafetyError):
        actions = []
    finished = [row for row in actions if row["action"] == "worker_finished"]
    line, attention = None, []
    if finished:
        line = _last_run_line(finished[-1])
        streak = []
        for row in reversed(finished):
            key = _failure_key(row)
            if key is None or streak and key != streak[0][0]:
                break
            streak.append((key, row))
        if len(streak) >= watch["repeated_failure_runs"]:
            key, first = streak[0][0], streak[-1][1]
            attention.append(_attention(f"The last {len(streak)} research runs all stopped with the same problem: "
                                        f"{plain_reason(key)}. Research keeps retrying, but this probably needs a fix. "
                                        "Open the logs or ask for help.", "repeat:" + key + ":" + first["at"],
                                        confirm="repeat"))
    elif isinstance(activity, dict) and activity.get("state") == "saved_previous_run" and _code(activity.get("last_reason")):
        line = f"Last research note: {plain_reason(activity['last_reason'])}."
    workers = [row for row in actions if row["action"] in ("worker_started", "worker_finished", "watchdog_restart")]
    if check_schedule and workers:
        last = workers[-1]
        waited = (at - timestamp(last["at"])).total_seconds()
        awake = last.get("awake")
        if (awake_now is not None and type(awake) in (int, float) and last.get("boot_id") is not None
                and last.get("boot_id") == _boot_id()):
            # Time the computer slept is not a missed schedule.
            waited = min(waited, max(0.0, awake_now - awake))
        if waited > watch["scheduled_run_overdue_seconds"]:
            attention.append(_attention(f"No scheduled research run has started for {round(waited / 60)} minutes, "
                                        "although research is on. The Windows scheduled task may not be running. "
                                        "Click Stop, then Start, to start research now.", "schedule:" + last["at"],
                                        confirm="schedule"))
    return {"line": line, "attention": attention}


def spend_summary(root, *, alert_hours=24):
    """Spend-watch lines and the last day's alerts, from saved records only.

    The window shows one line per item and one Windows notification for the
    alerts it has not shown yet (it remembers their ``alert_id``).
    """
    from datetime import timedelta
    from .spend_watch import allowance_summary
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=alert_hours)).isoformat()
        summary = allowance_summary(root, alerts_after=since, alert_limit=20)
    except (SafetyError, OSError):
        return {"lines": [{"item": "spend-watch", "text": "The spending records could not be read. See the project log.",
                           "percent": None, "flags": []}], "alerts": []}
    lines = []
    for line in summary["lines"]:
        text = line["text"]
        if line["percent"] is not None and "%" not in text:
            text = text.rstrip(".") + f" ({line['percent']}% used)."
        lines.append({"item": line["item"], "text": text, "percent": line["percent"], "flags": line["flags"]})
    return {"lines": lines, "alerts": [{"alert_id": row["alert_id"], "kind": row["kind"], "message": row["message"]}
                                       for row in summary["alerts"]]}


def sign_in_summary(root, *, runner=subprocess.run, recheck_seconds=30):
    """Saved sign-in needs for the window. Only an open sign-in window is
    re-verified (local processes, no site visit), at most every 30 seconds."""
    from .signed_in_browser import check_sign_in_window, sign_in_line, sign_in_needs, sign_in_window
    try:
        window = sign_in_window(root)
        if window["open"] and (datetime.now(timezone.utc) - datetime.fromisoformat(window["checked_at"])).total_seconds() >= recheck_seconds:
            window = check_sign_in_window(root, runner=runner)
        summary = {"window_open": window["open"], "line": sign_in_line(root), "needs": sign_in_needs(root)}
    except (SafetyError, OSError):
        summary = {"window_open": False, "line": "The sign-in records could not be read. See the project log.", "needs": []}
    # Under the sign-in needs: one line per dropshipping platform waiting on
    # something the owner can see (W4), the signed-in Trends check (W2), then
    # the opportunity score tally and its one-time calibration alert (W7).
    # Their own records failing never hides the sign-in needs.
    return dict(summary, platform_lines=_platform_lines(root), trends_line=_trends_line(root),
                score_line=_score_line(root))


def _platform_lines(root):
    from .dropship_platforms import platform_lines
    try:
        return platform_lines(_memory(root).root)
    except (SafetyError, OSError):
        return ["The dropshipping platform records could not be read. See the project log."]


def _trends_line(root):
    from .trends_proof_gate import proof_gate_line
    try:
        return proof_gate_line(root)
    except (SafetyError, OSError):
        return "The Google Trends check records could not be read. See the project log."


def _score_line(root):
    from .opportunity_score import score_line
    try:
        return score_line(_memory(root).root)
    except (SafetyError, OSError):
        return "The opportunity score records could not be read. See the project log."


def open_sign_in(root):
    """The owner's "Sign in to research sites" button."""
    from .signed_in_browser import open_sign_in_window
    with command_lock(root):
        try:
            opened = open_sign_in_window(root)
        except SafetyError as error:
            if str(error) != "SIGNED_IN_BROWSER_BUSY":
                raise
            record_action(root, "sign_in_busy")
            result = status(root)
            # Shown until the owner's next action, not replaced by the next refresh.
            result["sign_in"]["notice"] = "A research read is using the browser. Try again in a minute."
            return result
        record_action(root, "sign_in_window_" + opened)
    return status(root)


def command_lock(root):
    # Commands are short. A stuck command must not freeze the graphical UI.
    return file_lock(safe_path(runtime_path(root, "data/operations"), "desktop") / "commands.lock", 5, "DESKTOP_CONTROL_BUSY")


def record_action(root, action, **fields):
    """Small action receipts only; no raw commands, account data or status spam."""
    directory = safe_path(runtime_path(root, "data/operations"), "desktop")
    directory.mkdir(parents=True, exist_ok=True)
    # The awake-time clock and boot let readers leave out time the computer slept.
    record = {"at": utc_now(), "action": action, "awake": round(time.monotonic(), 3), "boot_id": _boot_id(), **fields}
    try:
        with (directory / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl")).open("ab") as stream:
            stream.write(canonical(record) + b"\n")
            stream.flush()
    except OSError:
        # Diagnostic failure must not undo a successfully saved owner Stop.
        pass


def start(root):
    with command_lock(root):
        set_programme_service_state(_memory(root), enabled=True)
        record_action(root, "start_requested")
    return status(root)


def signal_identified(row, sig, *, proc=Path("/proc")):
    """Signal this identity only; never kill by process name or a stale PID."""
    current = process_info(row["pid"], proc=proc)
    if current is None or current["start_ticks"] != row["start_ticks"]:
        return False
    descriptor = None
    try:
        descriptor = os.pidfd_open(row["pid"])
        # Recheck after opening: a PID reused between the two reads is refused.
        current = process_info(row["pid"], proc=proc)
        if current is None or current["start_ticks"] != row["start_ticks"]:
            return False
        signal.pidfd_send_signal(descriptor, sig)
        return True
    except ProcessLookupError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def stop(root):
    with command_lock(root):
        set_programme_service_state(_memory(root), enabled=False)
        rows = owned_programmes(root)
        signalled = [row["pid"] for row in rows if signal_identified(row, signal.SIGINT)]
        record_action(root, "stop_requested", identified_processes=len(rows), signalled_pids=signalled)
    # No forced SIGKILL and no broad WSL/task shutdown. A process stuck in a
    # kernel operation remains honestly "stopping", not falsely "stopped".
    return status(root)


def _activity_age(root, pid, since, at):
    """Seconds since this worker's process last recorded activity, counted
    from its launch when it has saved nothing yet, plus the saved activity."""
    from .operations import read_operation_status
    activity = read_operation_status(root, as_of=at.isoformat(timespec="seconds"))
    last = since
    if activity.get("pid") == pid and isinstance(activity.get("observed_at"), str):
        try:
            last = max(last, timestamp(activity["observed_at"]))
        except SafetyError:
            pass
    return max(0.0, (at - last).total_seconds()), activity


def _interrupt_stuck_child(root, child, idle, activity, watch):
    """The owner Stop's interrupt, for this worker's own child only.

    SIGINT to the child's own new session (as the existing worker cleanup
    does), so it can save and release its locks. Only if it is still there
    after the grace periods: SIGTERM, then SIGKILL, to that same session.
    The owner's research switch is left on.
    """
    record_action(root, "watchdog_restart", pid=child.pid, reason="PROGRESS_WATCH_RESTART",
                  seconds_since_activity=round(idle), run_id=activity.get("run_id") if activity.get("pid") == child.pid else None,
                  phase=activity.get("phase") if activity.get("pid") == child.pid else None)
    signals = []
    for sig, grace in ((signal.SIGINT, watch["watchdog_interrupt_grace_seconds"]),
                       (signal.SIGTERM, watch["watchdog_terminate_grace_seconds"]), (signal.SIGKILL, None)):
        if child.poll() is not None:
            break
        try:
            # The child was started with start_new_session=True and is not
            # yet reaped, so this group is exactly its own session.
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            break
        signals.append(sig.name)
        try:
            return child.wait(timeout=grace if grace is not None else watch["watchdog_terminate_grace_seconds"]), signals
        except subprocess.TimeoutExpired:
            continue
    try:
        return child.wait(timeout=watch["watchdog_terminate_grace_seconds"]), signals
    except subprocess.TimeoutExpired:
        # Even SIGKILL was not confirmed (a process stuck in the kernel). Say
        # so and return, so this worker ends and releases its lock.
        record_action(root, "watchdog_stop_unconfirmed", pid=child.pid, signals=signals)
        return None, signals


def _supervise(root, child, launched_at, *, now, watch):
    """Wait for the child, checking its saved activity every poll."""
    poll = watch["watchdog_poll_seconds"]
    baseline = previous = launched_at
    while True:
        try:
            return child.wait(timeout=poll), None
        except subprocess.TimeoutExpired:
            pass
        at = now()
        if (at - previous).total_seconds() > 3 * poll + 60:
            # The computer slept (or its clock jumped): quiet time is
            # counted again from now, never across the sleep.
            baseline = at
        previous = at
        try:
            idle, activity = _activity_age(root, child.pid, baseline, at)
        except Exception:
            continue  # A status read problem never stops or restarts research by itself.
        if idle >= watch["watchdog_no_activity_seconds"]:
            exit_code, signals = _interrupt_stuck_child(root, child, idle, activity, watch)
            return exit_code, {"seconds_since_activity": round(idle), "signals": signals}


def _watch_adopted(root, rows, *, now, watch):
    """A research process this worker did not start (its supervisor ended).

    Its quiet time is measured from its own saved activity (matched by PID
    and kernel start identity, whatever the writer flag says) or, when it has
    saved nothing, from its start. The first scheduled run that sees one
    quiet past the watchdog time only notes it, so a computer that just woke
    from sleep is never mistaken for a stuck run. Each later run that still
    sees the very same mark sends a harder signal to that identified process
    only: SIGINT (as Stop does), then SIGTERM, then SIGKILL. The owner's
    switch stays on.
    """
    activity = _read_activity(root, now(), now)
    found = next(((row, quiet, mark) for row, quiet, mark in _owned_quiet(activity, rows)
                  if quiet >= watch["watchdog_no_activity_seconds"]), None)
    if found is None:
        return None
    row, quiet, mark = found
    own = mark == activity.get("observed_at")
    seen = sum(1 for earlier in desktop_actions(root, days=2)
               if earlier["action"] in ("watchdog_observed", "watchdog_restart") and earlier.get("pid") == row["pid"]
               and earlier.get("observed_at") == mark)
    fields = {"pid": row["pid"], "reason": "PROGRESS_WATCH_RESTART", "adopted": True,
              "run_id": activity.get("run_id") if own else None, "observed_at": mark,
              "phase": activity.get("phase") if own else None, "seconds_since_activity": round(quiet)}
    if seen == 0:
        record_action(root, "watchdog_observed", **fields)
        return None
    sig = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)[min(seen, 3) - 1]
    sent = signal_identified(row, sig)
    record_action(root, "watchdog_restart", signals=[sig.name] if sent else [], **fields)
    return sig.name if sent else None


def run_worker(root, research_arguments, *, now=_now, settings=None):
    """The existing scheduled command, hidden by the native Windows parent."""
    root = Path(root).resolve()
    allowed = {"--tick", "--execute", "--live-model", "--funded-quickcommerce-fallback", "--cooperative-turns"}
    if (not isinstance(research_arguments, list) or len(set(research_arguments)) != len(research_arguments)
            or not {"--tick", "--execute"} <= set(research_arguments)
            or not set(research_arguments) <= allowed):
        raise SafetyError("DESKTOP_RESEARCH_ARGUMENTS_INVALID")
    lock_path = safe_path(runtime_path(root, "data/operations"), "desktop") / "worker.lock"
    with ExitStack() as stack:
        try:
            stack.enter_context(file_lock(lock_path, 0, "DESKTOP_WORKER_RUNNING"))
        except SafetyError as error:
            if str(error) != "DESKTOP_WORKER_RUNNING":
                raise
            return {"state": "already_running"}
        from .operations import progress_watch_or_defaults, run_outcome
        watch, from_file = (settings, True) if settings else progress_watch_or_defaults()
        if not from_file:
            # A broken settings file never stops research; the defaults apply.
            record_action(root, "progress_watch_settings_invalid")
        with command_lock(root):
            if not programme_service_state(_memory(root))["enabled"]:
                return {"state": "stopped"}
            rows = owned_programmes(root)
            if rows:
                interrupted = _watch_adopted(root, rows, now=now, watch=watch)
                return {"state": "already_running"} if interrupted is None else {"state": "watchdog_interrupted",
                                                                                  "signal": interrupted}
            launched_at = now()
            child = subprocess.Popen([sys.executable, "-u", str(root / "scripts/run_programme.py"),
                                      *research_arguments], cwd=root, start_new_session=True)
            record_action(root, "worker_started", pid=child.pid)
        exit_code = None
        try:
            exit_code, restarted = _supervise(root, child, launched_at, now=now, watch=watch)
        finally:
            if child.poll() is None:
                # Only this child's session, never a shared terminal or WSL.
                os.killpg(child.pid, signal.SIGINT)
                try:
                    exit_code = child.wait(timeout=watch["watchdog_interrupt_grace_seconds"])
                except subprocess.TimeoutExpired:
                    # Never wait forever here: this worker must end and release its lock.
                    record_action(root, "worker_cleanup_unconfirmed", pid=child.pid)
        try:
            ended = run_outcome(root, child.pid, since=(launched_at - timedelta(seconds=5)).isoformat()) or {}
        except (OSError, SafetyError):
            ended = {}
        # An interrupt (Ctrl+C, exit 130) is the owner's Stop, or a Stop then
        # Start before the old run had ended; neither is a failure.
        interrupted = exit_code in (130, -2) or ended.get("interrupted") is True
        outcome = ("watchdog_restarted" if restarted else "finished" if exit_code == 0 else
                   "owner_stopped" if interrupted and not programme_service_state(_memory(root))["enabled"] else
                   "interrupted" if interrupted else "failed")
        record_action(root, "worker_finished", pid=child.pid, exit_code=exit_code, outcome=outcome,
                      run_id=ended.get("run_id"), last_reason=ended.get("last_reason"),
                      failure_reason=ended.get("failure_reason") if outcome == "failed" else None,
                      **(restarted or {}))
        result = status(root)
        result["exit_code"] = exit_code
        if restarted:
            result["watchdog"] = restarted
        return result
