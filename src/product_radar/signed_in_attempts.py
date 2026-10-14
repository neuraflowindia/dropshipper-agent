"""Signed-in reads that could not start: a calm wait, and the acquisition reopened.

A signed-in route asks before each read whether it may start (the owner's
sign-in, a closed sign-in window, the desktop, the one signed-in browser,
pacing). When the answer is no before anything was sent, the route raises
``SignedInWait``; the controller records the question as waiting, not failed.

When the wait is found only after the acquisition was marked as dispatching
(the browser itself answered "busy", "desktop locked" or "window open"), the
route notes it (``note_not_started``) and returns the acquisition to
"reserved" (``reopen_not_started``), so the next read starts fresh instead of
waiting for an original that was never sent.
"""

from hashlib import sha256
import os

from .models import SafetyError, timestamp
from .storage import atomic_json, load_json, safe_path

NOT_STARTED = "not-started.json"


class SignedInWait(SafetyError):
    """A calm wait: no page was requested. The controller records it as
    waiting, not as a failure. ``started_at``/``finished_at`` are set when the
    read had begun (its acquisition must be reopened before the next read)."""

    calm_wait = True

    def __init__(self, reason, *, next_retry_at=None, started_at=None, finished_at=None):
        super().__init__(reason)
        self.next_retry_at, self.started_at, self.finished_at = next_retry_at, started_at, finished_at


# The only refusals a signed-in read turns into a calm wait: the signed-in
# browser's own waits, this round's share of it spent, and a busy local
# record. Anything else (invalid settings or records, a leftover artefact) is
# a failure that must be seen.
CALM_CODES = ("OWNER_SIGN_IN_REQUIRED", "OWNER_SIGN_IN_WINDOW_OPEN", "OWNER_DESKTOP_UNAVAILABLE",
              "SIGNED_IN_BROWSER_BUSY", "SIGNED_IN_RECORD_BUSY", "SIGNED_IN_ROUND_TIME_USED",
              "SIGNED_IN_ROUND_READS_USED")


def signed_in_hold(root, site, *, as_of=None):
    """None when a signed-in read of ``site`` may start, else its calm wait
    (the owner's sign-in, the window, the desktop, pacing) with its recheck time."""
    from .signed_in_browser import signed_in_recheck_at, signed_in_wait
    from .signed_in_pacing import signed_in_pace
    wait = signed_in_wait(root, site, as_of=as_of)
    if wait:
        return {"reason": wait, "next_retry_at": signed_in_recheck_at(root, wait, as_of=as_of)}
    return signed_in_pace(root, site, as_of=as_of)


def count_signed_in_read(root, site):
    """Count one read just before it starts. Pacing is judged by the
    reservation itself, on the same clock as the count; a pacing refusal or a
    busy record is a calm wait, anything else is raised."""
    from .signed_in_browser import signed_in_recheck_at
    from .signed_in_pacing import SignedInPaced, reserve_signed_in_read
    try:
        return reserve_signed_in_read(root, site)
    except SignedInPaced as error:
        raise SignedInWait(str(error), next_retry_at=error.next_retry_at) from None
    except SafetyError as error:
        if str(error) != "SIGNED_IN_RECORD_BUSY":
            raise
        raise SignedInWait(str(error), next_retry_at=signed_in_recheck_at(root, str(error))) from None


def begin_signed_in_read(root, site, *, as_of=None):
    """The owner's sign-in, window and desktop first, then ``count_signed_in_read``."""
    from .signed_in_browser import signed_in_recheck_at, signed_in_wait
    wait = signed_in_wait(root, site, as_of=as_of)
    if wait:
        raise SignedInWait(wait, next_retry_at=signed_in_recheck_at(root, wait, as_of=as_of))
    return count_signed_in_read(root, site)


def release_unsent_read(root, site, counted):
    """Give back a counted read that never navigated; a busy record keeps it
    counted (pacing only errs on the side of fewer reads)."""
    from .signed_in_pacing import release_signed_in_read
    if counted is None:
        return
    try:
        release_signed_in_read(root, site, counted)
    except SafetyError as error:
        if str(error) != "SIGNED_IN_RECORD_BUSY":
            raise


def aside_name(name, at, sequence=None):
    """One naming style for an attempt set aside unsent:
    ``<name>-not-started-[<sequence>-]<time to the microsecond>``."""
    return name + "-not-started-" + (sequence + "-" if sequence else "") + timestamp(at).isoformat(
        timespec="microseconds").replace(":", "").replace("+0000", "Z")


def collectable(state, owner):
    """A reserved owner that has never been sent, or whose every earlier start
    proved it never navigated (``reopen_not_started``)."""
    from .source_acquisitions import _stage
    if owner["state"] != "reserved":
        return False
    kinds = [_stage(state, owner, stage_id)["stage_type"] for stage_id in owner["stage_ids"]]
    return not kinds or kinds[-1] == "prelaunch_zero" and set(kinds) <= {"dispatch_intent", "prelaunch_zero"}


def note_not_started(folder, error):
    """Keep why and when a signed-in read did not start until its owner is reopened."""
    atomic_json(folder / NOT_STARTED, {"reason": str(error), "started_at": error.started_at,
                                       "finished_at": error.finished_at})


def reopen_not_started(memory, acquisition_id, note_path, as_of, *, set_aside):
    """Return a signed-in acquisition to "reserved" after a start that never navigated.

    ``recover_source_prelaunch`` reopens only the same acquisition, and only
    from a hash-bound "owned-source-process-exit-v1" proof. Here the owned
    process is this research process (``os.getpid()``): the read adapter runs
    inside it and had returned (``owned_process_exited`` refers to that
    finished attempt, including any PowerShell child it ran), and the signed-in
    browser proved no page was requested. ``set_aside(sequence, note)`` first
    moves the attempt's files away (so the next read starts in a fresh folder)
    and returns the folder for the proof and where the note now is. The note
    is removed only on success, so a failed reopening is retried later.
    """
    from .source_acquisitions import _record, _stage, recover_source_prelaunch
    note_path = safe_path(note_path)
    note = load_json(note_path, 16384)
    if (not isinstance(note, dict) or set(note) != {"reason", "started_at", "finished_at"}
            or not isinstance(note["reason"], str)):
        raise SafetyError("SIGNED_IN_NOT_STARTED_NOTE_INVALID")
    timestamp(note["started_at"]); timestamp(note["finished_at"])
    state = memory.snapshot(); owner = _record(state, acquisition_id); binding = owner["binding"]
    if owner["state"] == "dispatching":
        intent_id = owner.get("active_dispatch_intent_id")
        intent = _stage(state, owner, intent_id)
        target, note_path = set_aside(str(intent["intent_sequence"]), note)
        target = safe_path(target)
        proof = {"contract": "owned-source-process-exit-v1", "acquisition_id": acquisition_id,
                 "attempt_id": binding["original_attempt_id"], "physical_request_fingerprint": binding["physical_request_fingerprint"],
                 "owned_process_exited": True, "request_sent": False, "process_id": os.getpid(),
                 "dispatch_intent_id": intent_id, "intent_sequence": intent["intent_sequence"],
                 "ownership_token_hash": intent["process_ownership_token_hash"],
                 "started_at": note["started_at"], "exited_at": note["finished_at"], "reason": note["reason"]}
        atomic_json(target / "process-exit.json", proof)
        receipt = {"route": binding["route"], "request": binding["request"], "acquisition_id": acquisition_id,
                   "attempt_id": binding["original_attempt_id"], "dispatch_started": False, "observed_at": note["finished_at"],
                   "state": "failed", "reason": note["reason"],
                   "artifacts": {"process-exit.json": sha256((target / "process-exit.json").read_bytes()).hexdigest()}}
        atomic_json(target / "receipt.json", receipt)
        recover_source_prelaunch(memory, acquisition_id, receipt_path=target / "receipt.json",
                                 as_of=max(as_of, note["finished_at"], key=timestamp))
    elif owner["state"] != "reserved":
        raise SafetyError("SIGNED_IN_NOT_STARTED_OWNER_CHANGED")
    note_path.unlink()
