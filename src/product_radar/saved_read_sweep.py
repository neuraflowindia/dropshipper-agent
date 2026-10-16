"""Saved reads whose files a power cut emptied, set aside even when nobody asks (R15).

A saved read whose recorded files are proven lost (empty, or only zero bytes)
is set aside when a question next claims it (``claim_source_acquisition``).
A saved read that no question asks for again would otherwise stay as it is,
and the daily backup would find its emptied files and call them unsafe.

So, before the daily backup (once per India day, and once more after each
research start, since a power cut is followed by a restart), every saved read
whose recorded file is empty or begins with only zero bytes is handed to
``set_aside_damaged_acquisition``, which proves the loss file by file. A real
change of saved bytes is never set aside (it keeps blocking, as before), a
missing or unreadable file proves nothing, and a read already set aside is
skipped: the sweep is idempotent and never deletes or rewrites a file.

The outcome is one small sealed record (``CONTROL``) and a run-log event. A
replacement read that was itself lost (a saved read lost a second time is
never read a third time: ``damage_replacement_allowed``) and saved bytes that
were changed get one plain owner line (``sweep_line``, shown with the other
housekeeping lines). Nothing here contacts a website, provider or model.
"""

from zoneinfo import ZoneInfo

from .models import SafetyError, digest, timestamp, utc_now
from .runtime_storage import runtime_path
from .storage import atomic_json, load_json, safe_path

CONTRACT = "saved-read-sweep-day-v1"
CONTROL = "data/operations/housekeeping/saved-reads-day.json"
PROBE_BYTES = 4096
_IDS = 20
_NOT_PROVEN = {"SOURCE_ACQUISITION_DAMAGE_NOT_PROVEN", "SOURCE_ACQUISITION_FILE_UNAVAILABLE", "SAVED_FILE_UNAVAILABLE"}
_swept_in_this_process = False


def _suspect(path):
    """Whether a recorded file looks emptied: no bytes, or zero bytes at its start.

    Only a hint for which reads to prove; a missing or unreadable file is no hint."""
    try:
        with open(safe_path(path), "rb") as handle:
            start = handle.read(PROBE_BYTES)
    except (OSError, SafetyError):
        return False
    return not start.strip(b"\x00")


def _files(stage):
    receipt = stage.get("receipt") if isinstance(stage, dict) else None
    artifacts = stage.get("artifacts") if isinstance(stage, dict) else None
    proofs = [receipt] + (list(artifacts.values()) if isinstance(artifacts, dict) else [])
    return [proof["path"] for proof in proofs if isinstance(proof, dict) and isinstance(proof.get("path"), str)]


def sweep_damaged_reads(memory, *, as_of):
    """Set aside every saved read whose recorded files are proven lost; returns counts."""
    from .source_acquisitions import ACQUISITION_KIND, damage_replacement_allowed, set_aside_damaged_acquisition
    from .source_coverage import _INTEGRITY_ERRORS
    timestamp(as_of)
    state = memory.snapshot()
    checked, set_aside, repeated, changed, unproven = 0, [], [], [], []
    for acquisition_id, row in sorted(state["batch_runs"].items()):
        if row.get("kind") != ACQUISITION_KIND:
            continue
        if row.get("state") == "set_aside":
            if not damage_replacement_allowed(row):
                repeated.append(acquisition_id)  # a replacement lost again: never read a third time
            continue
        stage = state["source_checks"].get(row.get("active_response_id")) if row.get("active_response_id") else None
        if not isinstance(stage, dict) or stage.get("stage_type") != "response":
            continue
        checked += 1
        if not any(_suspect(path) for path in _files(stage)):
            continue
        try:
            set_aside_damaged_acquisition(memory, acquisition_id, as_of=as_of)
        except SafetyError as error:
            if str(error) == "SOURCE_ACQUISITION_ARTIFACT_CHANGED":
                changed.append(acquisition_id)  # not a power cut: it keeps blocking, as before
                continue
            if str(error) in _NOT_PROVEN:
                continue
            if str(error) in _INTEGRITY_ERRORS:
                raise
            unproven.append(acquisition_id)  # this read only (a busy store, an odd record): the next sweep looks again
            continue
        except OSError:
            unproven.append(acquisition_id)
            continue
        set_aside.append(acquisition_id)
        if not damage_replacement_allowed(row):
            repeated.append(acquisition_id)
    return {"state": "done", "checked": checked, "set_aside": len(set_aside), "repeated": len(repeated),
            "changed": len(changed), "unproven": len(unproven), "set_aside_ids": set_aside[:_IDS],
            "repeated_ids": repeated[:_IDS], "changed_ids": changed[:_IDS], "unproven_ids": unproven[:_IDS]}


def _code(error):
    value = str(error) if isinstance(error, SafetyError) else "SAVED_READ_SWEEP_FAILED"
    return value if value.isascii() and value.replace("_", "").isalnum() and value.isupper() and len(value) <= 160 \
        else "SAVED_READ_SWEEP_FAILED"


def _saved(root):
    try:
        wrapper = load_json(safe_path(runtime_path(root, CONTROL)), 65536)
    except SafetyError:
        return None
    if (not isinstance(wrapper, dict) or set(wrapper) != {"value", "hash"} or digest(wrapper["value"]) != wrapper["hash"]
            or not isinstance(wrapper["value"], dict) or wrapper["value"].get("contract") != CONTRACT):
        return None
    return wrapper["value"]


def run_due_saved_read_sweep(memory, *, root, now=None):
    """The sweep, once per India day and once after each research start; never raises."""
    global _swept_in_this_process
    from .operations import record_event
    at = now or utc_now()
    try:
        day = timestamp(at).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
        saved = _saved(root)
        if _swept_in_this_process and saved is not None and saved.get("day") == day:
            return None
        try:
            outcome = sweep_damaged_reads(memory, as_of=at)
        except Exception as error:  # housekeeping never stops research
            outcome = {"state": "failed", "reason": _code(error)}
        value = {"contract": CONTRACT, "day": day, "at": at, "outcome": outcome}
        atomic_json(safe_path(runtime_path(root, CONTROL)), {"value": value, "hash": digest(value)})
        _swept_in_this_process = outcome["state"] == "done"  # a failed sweep is tried again at the next step
    except Exception as error:
        outcome = {"state": "failed", "reason": _code(error)}
    record_event("saved_read_sweep_outcome", state=outcome["state"], reason=outcome.get("reason"), useful_result=False,
                 counts={name: outcome[name] for name in ("checked", "set_aside", "repeated", "changed", "unproven")
                         if type(outcome.get(name)) is int})
    return outcome


def sweep_line(root):
    """One plain owner line when saved reads were lost twice, changed or not checked; else None."""
    value = _saved(root)
    outcome = value.get("outcome") if value else None
    if not isinstance(outcome, dict):
        return None
    if outcome.get("state") == "failed":
        return "The daily check for saved research pages emptied by a power cut stopped with an error. Research continues."
    parts = []
    if type(outcome.get("repeated")) is int and outcome["repeated"]:
        parts.append(f"{outcome['repeated']} saved research page(s) were lost to a power cut a second time; the program "
                     "does not read them a third time. If power cuts keep happening, check the computer's power supply.")
    if type(outcome.get("changed")) is int and outcome["changed"]:
        parts.append(f"{outcome['changed']} saved research page(s) were changed on disk (not by a power cut); "
                     "they are kept as they are and not used.")
    return " ".join(parts) or None
