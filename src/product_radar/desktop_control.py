"""Small local controls for the Windows tray app; never a second research engine.

Status does not replay research history, call a provider, or write a heartbeat.
Stop persists the existing owner pause before interrupting only identified
programme processes. Original attempts and spending allowances stay intact.
"""

from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

from .models import SafetyError, canonical, utc_now
from .programme import programme_service_state, set_programme_service_state
from .runtime_storage import runtime_path
from .storage import safe_path


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


def status(root):
    control = programme_service_state(_memory(root))
    processes = owned_programmes(root)
    enabled, active = control["enabled"], bool(processes)
    state = "running" if enabled and active else "ready" if enabled else "stopping" if active else "stopped"
    message = {"running": "Research is running.",
               "ready": "Research is on. Waiting for the next scheduled run.",
               "stopping": "Stopping the current research step. New work is switched off.",
               "stopped": "Research is stopped. Your saved work is kept."}[state]
    from .operations import read_operation_status
    activity = read_operation_status(root)
    if state == "running" and activity.get("state") == "running":
        message += " " + activity["message"]
        if activity.get("last_useful_result_at") is None:
            message += " No completed result has been recorded in this run yet."
    spend = spend_summary(root)
    return {"schema_version": 2, "state": state, "enabled": enabled,
            "active_processes": len(processes), "message": message, "checked_at": utc_now(), "activity": activity,
            "sign_in": sign_in_summary(root), "spend": spend["lines"], "spend_alerts": spend["alerts"]}


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
        return {"window_open": window["open"], "line": sign_in_line(root), "needs": sign_in_needs(root)}
    except (SafetyError, OSError):
        return {"window_open": False, "line": "The sign-in records could not be read. See the project log.", "needs": []}


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
    from .signed_in_browser import _file_lock
    # Commands are short. A stuck command must not freeze the graphical UI.
    return _file_lock(safe_path(runtime_path(root, "data/operations"), "desktop") / "commands.lock", 5, "DESKTOP_CONTROL_BUSY")


def record_action(root, action, **fields):
    """Small action receipts only; no raw commands, account data or status spam."""
    directory = safe_path(runtime_path(root, "data/operations"), "desktop")
    directory.mkdir(parents=True, exist_ok=True)
    record = {"at": utc_now(), "action": action, **fields}
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


def run_worker(root, research_arguments):
    """The existing scheduled command, hidden by the native Windows parent."""
    root = Path(root).resolve()
    allowed = {"--tick", "--execute", "--live-model", "--funded-quickcommerce-fallback", "--cooperative-turns"}
    if (not isinstance(research_arguments, list) or len(set(research_arguments)) != len(research_arguments)
            or not {"--tick", "--execute"} <= set(research_arguments)
            or not set(research_arguments) <= allowed):
        raise SafetyError("DESKTOP_RESEARCH_ARGUMENTS_INVALID")
    lock_path = safe_path(runtime_path(root, "data/operations"), "desktop") / "worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "already_running"}
        with command_lock(root):
            if not programme_service_state(_memory(root))["enabled"]:
                return {"state": "stopped"}
            if owned_programmes(root):
                return {"state": "already_running"}
            child = subprocess.Popen([sys.executable, "-u", str(root / "scripts/run_programme.py"),
                                      *research_arguments], cwd=root, start_new_session=True)
            record_action(root, "worker_started", pid=child.pid)
        try:
            exit_code = child.wait()
        finally:
            if child.poll() is None:
                # Only this child's session, never a shared terminal or WSL.
                os.killpg(child.pid, signal.SIGINT)
                child.wait()
        record_action(root, "worker_finished", pid=child.pid, exit_code=exit_code)
        result = status(root)
        result["exit_code"] = exit_code
        return result
