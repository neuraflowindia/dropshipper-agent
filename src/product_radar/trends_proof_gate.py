"""The signed-in Trends route's proof gate and its plain status line (Plan R10 W2/W13).

After the owner signs in (the sign-in window's latest opening), the first
signed-in Trends reads must each save a complete, verified result: a chart
whose CSV export matches it, or Google's own "not enough data" widget. The
gate passes once three reads are complete and at least one of them is a
verified chart export (no-data reads count, but never alone; up to ten
complete reads wait for that export). It fails at the first read that is not
complete; a later read that saves a verified chart export starts a new check
(so a failed line clears once Trends works again, without a new sign-in).
Each counted read is kept with its date and outcome. A refusal
(HTTP 429/403, consent, "unusual traffic", "Something went wrong") is kept
with its exact reason, and a minimised page that never drew its chart as
``TRENDS_MINIMISED_NOT_RENDERED``, so refusals and rendering problems stay
apart. A new sign-in starts a new gate.

The route's current wait (``note_route_wait``) is kept beside it so the
desktop app and the meter can say in plain words why Trends is not being
read and when it is tried again (``proof_gate_line``).
"""

from datetime import datetime, timezone

from .models import SafetyError, timestamp, utc_now
from .signed_in_browser import PROJECT_ROOT, records_lock, records_path, sign_in_window
from .storage import atomic_json, load_json

KIND = "trends-signed-in-proof-gate-v1"
READS = 3
MAX_READS = 10
STATES = ("waiting_for_sign_in", "waiting_for_reads", "in_progress", "passed", "failed")
NO_EXPORT = "TRENDS_NO_VERIFIED_EXPORT"
_ROW = {"acquisition_id", "read_at", "passed", "state", "reason", "http_status", "page_visibility", "window_state",
        "animation_frame", "refused"}
PLAIN = {
    "DIRECT_TRENDS_ACCESS_CHALLENGE": "Google is refusing Trends reads for now",
    "DIRECT_TRENDS_CONSENT_REQUIRED": "Google is asking for a cookie choice before showing Trends",
    "DIRECT_TRENDS_GOOGLE_LIMIT": "Google Trends answered \"Something went wrong\" (its usage limit)",
    "OWNER_SIGN_IN_REQUIRED": "the research Google account needs to sign in again",
    "TRENDS_MINIMISED_NOT_RENDERED": "the minimised Trends page did not draw its chart",
    "TRENDS_RENDERING_PAUSED": "the minimised Trends page did not draw its chart",
    NO_EXPORT: "ten Trends reads in a row had no chart to download",
}


def plain(reason):
    return PLAIN.get(reason, "a Trends read did not give a complete result")


def _clock(at):
    return timestamp(at).astimezone(timezone.utc).strftime("%d %b, %H:%M UTC")


def _path(root):
    return records_path(root, "trends-proof-gate.json")


def _fresh(sign_in_at):
    return {"schema_version": 1, "kind": KIND, "sign_in_at": sign_in_at,
            "state": "waiting_for_reads" if sign_in_at else "waiting_for_sign_in", "reads": [], "decided_at": None}


def _saved(root):
    path = _path(root)
    if not path.exists() and not path.is_symlink():
        return None
    value = load_json(path, 65536)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "kind", "sign_in_at", "state", "reads", "decided_at"}
            or value["schema_version"] != 1 or value["kind"] != KIND or value["state"] not in STATES
            or not isinstance(value["reads"], list) or len(value["reads"]) > MAX_READS
            or any(not isinstance(row, dict) or set(row) != _ROW or type(row["passed"]) is not bool for row in value["reads"])):
        raise SafetyError("TRENDS_PROOF_GATE_RECORD_INVALID")
    for at in [value["sign_in_at"], value["decided_at"]] + [row["read_at"] for row in value["reads"]]:
        if at is not None:
            timestamp(at)
    return value


def proof_gate(root=PROJECT_ROOT):
    """The gate for the owner's latest sign-in (a new sign-in shows a new gate)."""
    sign_in_at = sign_in_window(root)["last_opened_at"]
    saved = _saved(root)
    return saved if saved is not None and saved["sign_in_at"] == sign_in_at else _fresh(sign_in_at)


def record_proof_read(root, folder, receipt):
    """Count one finished signed-in read in the gate, if it belongs there:
    it navigated after the owner's latest sign-in, and its page did not show
    the sign-in link. Returns the gate."""
    from .trends_controller import MAX_JSON
    sign_in_at = sign_in_window(root)["last_opened_at"]
    transport = (load_json(folder / "transport.json", MAX_JSON)
                 if "transport.json" in receipt.get("artifacts", {}) else {})
    if (sign_in_at is None or receipt.get("dispatch_started") is not True or transport.get("signed_in") is False
            or timestamp(receipt["observed_at"]) < timestamp(sign_in_at)):
        return proof_gate(root)
    with records_lock(root, "trends-proof-gate"):
        gate = proof_gate(root)
        if gate["state"] == "passed" or any(row["acquisition_id"] == receipt["acquisition_id"] for row in gate["reads"]):
            return gate
        observed = timestamp(receipt["observed_at"])
        if gate["state"] == "failed":
            if receipt["state"] != "usable" or observed <= timestamp(gate["decided_at"]):
                return gate
            # A later read saved a verified chart export: the failed check no longer
            # describes Trends, so a new check starts with this read (same sign-in). It
            # keeps the time the failed check ended, and a read observed before then
            # (its bookkeeping settled late) never counts in the new check.
            gate = dict(_fresh(sign_in_at), decided_at=gate["decided_at"])
        elif gate["decided_at"] is not None and observed <= timestamp(gate["decided_at"]):
            return gate
        complete = receipt["state"] in ("usable", "no_data")
        gate["reads"].append({"acquisition_id": receipt["acquisition_id"], "read_at": receipt["observed_at"],
                              "passed": complete, "state": receipt["state"], "reason": receipt["reason"],
                              "http_status": receipt.get("navigation_status"),
                              "refused": receipt["state"] == "blocked" and receipt["reason"] != "OWNER_SIGN_IN_REQUIRED",
                              "page_visibility": transport.get("page_visibility"),
                              "window_state": transport.get("window_state"),
                              "animation_frame": transport.get("animation_frame")})
        exported = any(row["state"] == "usable" for row in gate["reads"])
        if not complete:
            gate.update(state="failed", decided_at=receipt["observed_at"])
        elif len(gate["reads"]) >= READS and exported:
            gate.update(state="passed", decided_at=receipt["observed_at"])
        elif len(gate["reads"]) >= MAX_READS:
            gate["reads"][-1].update(passed=False, reason=NO_EXPORT)
            gate.update(state="failed", decided_at=receipt["observed_at"])
        else:
            gate["state"] = "in_progress"
        atomic_json(_path(root), gate)
    return gate


def _wait_path(root):
    return records_path(root, "trends-route-wait.json")


def note_route_wait(root, reason, next_retry_at):
    """Keep the route's current access wait for the plain line (None clears it)."""
    if next_retry_at is not None:
        timestamp(next_retry_at)
    with records_lock(root, "trends-route-wait"):
        atomic_json(_wait_path(root), {"schema_version": 1, "reason": reason, "next_retry_at": next_retry_at})


def route_wait(root, *, as_of=None):
    """The route's saved access wait while it lasts, else None."""
    path = _wait_path(root)
    if not path.exists() and not path.is_symlink():
        return None
    value = load_json(path, 16384)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "reason", "next_retry_at"}
            or value["schema_version"] != 1 or not isinstance(value["reason"], str)):
        raise SafetyError("TRENDS_ROUTE_WAIT_INVALID")
    if value["next_retry_at"] is None or timestamp(value["next_retry_at"]) <= timestamp(as_of or utc_now()):
        return None
    return value


def proof_gate_line(root=PROJECT_ROOT, *, as_of=None):
    """One plain line for the desktop app and the meter."""
    from .trends_signed_in import render_hold
    as_of = as_of or datetime.now(timezone.utc).isoformat()
    gate = proof_gate(root)
    if gate["state"] == "waiting_for_sign_in":
        return "Google Trends: waiting for the owner to sign in."
    hold = render_hold(root, as_of=as_of)
    if hold:
        return ("Google Trends is paused: %s. Trying again at %s, or sooner after you sign in again."
                % (plain(hold["reason"]), _clock(hold["until"])))
    wait = route_wait(root, as_of=as_of)
    if wait:
        return ("Google Trends is waiting: %s. Trying again at %s. Research continues without Trends."
                % (plain(wait["reason"]), _clock(wait["next_retry_at"])))
    done = sum(row["passed"] for row in gate["reads"])
    if gate["state"] in ("waiting_for_reads", "in_progress"):
        return "Google Trends check: %d of %d signed-in reads saved complete results." % (min(done, READS), READS)
    if gate["state"] == "passed":
        return "Google Trends check passed: the first signed-in reads saved complete results (%s)." % _clock(gate["decided_at"])
    last = gate["reads"][-1]
    if last["refused"]:
        return ("Google Trends refused the signed-in read on %s: %s. Research continues without Trends."
                % (_clock(last["read_at"]), plain(last["reason"])))
    return "Google Trends check failed on %s: %s." % (_clock(last["read_at"]), plain(last["reason"]))
