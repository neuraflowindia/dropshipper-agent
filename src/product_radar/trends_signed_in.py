"""Google Trends, read in the owner's signed-in research profile (Plan R10 W2, D5).

Reading only: the Trends export expression (``trends_controller.EXPORT_JS``)
copies the rendered page and clicks only the public time-chart CSV control;
nothing else is clicked, typed or accepted (no consent, sign-in or challenge
is ever answered). A read starts only when the owner has signed in, the
sign-in window is closed, the desktop is available, no rendering pause is on
and pacing allows it (one read every 90 seconds, 200 in any 24 hours;
``signed_in_hold``); otherwise the route waits calmly with the reason
(``SignedInWait``).

Each read records whether the page showed the research account signed in
(``signed_in_marker``): Google's own account avatar, or its sign-in link or
sign-in page. The avatar, or Google's sign-in page, also records the Google
account sign-in itself (``google_account_marker``). A read that showed the sign-in link records the sign-in as
expired, so later reads wait with ``OWNER_SIGN_IN_REQUIRED`` until the owner
signs in again. Google's refusals (HTTP 429/403, consent, "unusual traffic",
its in-chart "Something went wrong") are kept as refusals of the owner's
signed-in identity (``source_coverage.signed_in_access_scope``), with the
escalating waits; nothing is retried around them.

A minimised page that never drew its chart (hidden, no animation frame, no
chart) is a rendering problem, not a refusal: the whole route then pauses
calmly (``TRENDS_RENDERING_PAUSED``) for an hour, or until the owner signs in
again, and the question that met it is retried, not used up.

A read that could not start after its acquisition was marked as dispatching
is set aside unsent and the acquisition reopened (``signed_in_attempts``).
"""

from datetime import timedelta
import re

from . import signed_in_attempts as attempts
from .models import SafetyError, timestamp, utc_now
from .signed_in_attempts import NOT_STARTED, SignedInWait, aside_name, note_not_started, reopen_not_started
from .signed_in_browser import (PROJECT_ROOT, record_site_observation, records_lock, records_path, sign_in_window,
                                signed_in_read, signed_in_recheck_at)
from .storage import atomic_json, load_json, safe_path

SITE = ROUTE = "google-trends"
GOOGLE_SITE = "google"
PROFILE = "signed-in"
RENDER_HOLD = "TRENDS_RENDERING_PAUSED"
RENDER_RECHECK_SECONDS = 3600
PENDING_BOOKKEEPING = "signed-in-bookkeeping-pending.json"
_TRENDS_PAGE = re.compile(r"https://trends\.google\.com/")


def signed_in_marker(snapshot):
    """True for the account avatar, False for Google's sign-in link or sign-in
    page, None without a clear marker (a refusal page, an unread page)."""
    from .trends_controller import sign_in_page
    if sign_in_page(snapshot):
        return False
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("url"), str)
            or not _TRENDS_PAGE.match(snapshot["url"]) or not isinstance(snapshot.get("account"), dict)):
        return None
    if snapshot["account"].get("avatar") is True:
        return True
    return False if snapshot["account"].get("sign_in_link") is True else None


def google_account_marker(snapshot):
    """The Google account itself (the required ``google`` sign-in site): True
    when the Trends page shows the account avatar, False on Google's sign-in
    page, None otherwise (Trends' own sign-in link concerns Trends only)."""
    from .trends_controller import sign_in_page
    if sign_in_page(snapshot):
        return False
    return True if signed_in_marker(snapshot) is True else None


def _render_path(root):
    return records_path(root, "trends-render-hold.json")


def note_render_hold(root, receipt):
    """A minimised page did not draw its chart: pause the route (see the module)."""
    with records_lock(root, "trends-render-hold"):
        atomic_json(_render_path(root), {"schema_version": 1, "at": receipt["observed_at"],
                                         "acquisition_id": receipt["acquisition_id"]})


def render_hold(root, *, as_of=None):
    """The route's rendering pause, or None once it ended or the owner signed in again."""
    path = _render_path(root)
    if not path.exists() and not path.is_symlink():
        return None
    value = load_json(path, 16384)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "at", "acquisition_id"}
            or value["schema_version"] != 1):
        raise SafetyError("TRENDS_RENDER_HOLD_INVALID")
    at, now = timestamp(value["at"]), timestamp(as_of or utc_now())
    opened = sign_in_window(root)["last_opened_at"]
    until = at + timedelta(seconds=RENDER_RECHECK_SECONDS)
    if now >= until or opened is not None and timestamp(opened) > at:
        return None
    # Questions look again every couple of minutes (a local check), so a new
    # sign-in ends the pause at once; ``until`` is when it ends by itself.
    recheck = timestamp(signed_in_recheck_at(root, RENDER_HOLD, as_of=now.isoformat()))
    return {"reason": RENDER_HOLD, "next_retry_at": min(until, recheck).isoformat(), "until": until.isoformat()}


def signed_in_hold(root, *, as_of=None):
    """None when a signed-in Trends read may start, else its calm wait."""
    return render_hold(root, as_of=as_of) or attempts.signed_in_hold(root, SITE, as_of=as_of)


def begin_signed_in_read(root, *, as_of=None):
    """Ask (rendering pause, owner, window, desktop), then count one read just
    before it starts; pacing is judged by that count, on its own clock."""
    hold = render_hold(root, as_of=as_of)
    if hold:
        raise SignedInWait(hold["reason"], next_retry_at=hold["next_retry_at"])
    return attempts.begin_signed_in_read(root, SITE, as_of=as_of)


def release_unsent_read(root, counted):
    attempts.release_unsent_read(root, SITE, counted)


def signed_in_snapshot(url, expression, folder, *, root, timeout, ready_expression, download_folder):
    """One gated signed-in browser read for ``capture_direct_trends``.

    A read the browser proved it never started for one of the signed-in
    browser's own reasons (sign-in needed, window open, desktop locked,
    browser busy) raises ``SignedInWait``; any other refusal is a failure.
    """
    started = utc_now(timespec="microseconds")
    try:
        result = signed_in_read(SITE, url, expression, folder, marker=signed_in_marker, root=root, route=ROUTE,
                                timeout=timeout, ready_expression=ready_expression, download_folder=download_folder)
    except SafetyError as error:
        if str(error) not in attempts.CALM_CODES or any(
                (folder / name).exists() for name in ("owned_process.json", "native_dispatch.json")):
            raise  # a real failure, or the browser started: recovery proves what happened
        raise SignedInWait(str(error), next_retry_at=signed_in_recheck_at(root, str(error)),
                           started_at=started, finished_at=utc_now(timespec="microseconds")) from None
    if (result.get("navigation_started") is False and result.get("state") != "captured"
            and result.get("reason") in attempts.CALM_CODES):
        raise SignedInWait(result["reason"], next_retry_at=signed_in_recheck_at(root, result["reason"]),
                           started_at=started, finished_at=utc_now(timespec="microseconds"))
    if result.get("state") == "captured":
        # The same read also confirms (or not) the Google account sign-in.
        google = google_account_marker(result.get("snapshot"))
        if google is not None:
            record_site_observation(root, GOOGLE_SITE, google, route=ROUTE)
    return result


def _bookkeeping(root, folder, receipt):
    from .trends_controller import MINIMISED_NOT_RENDERED
    from .trends_proof_gate import record_proof_read
    from .trends_rising import save_rising_queries
    if receipt["reason"] == MINIMISED_NOT_RENDERED:
        note_render_hold(root, receipt)
    save_rising_queries(folder, receipt)
    record_proof_read(root, folder, receipt)


def _settle(root, folder, receipt):
    """Bookkeeping after a real read never fails the read: a busy or unreadable
    record leaves a pending note in the read's folder, retried by later reads."""
    from .operations import record_event
    try:
        _bookkeeping(root, folder, receipt)
    except (SafetyError, OSError) as error:
        code = str(error) if isinstance(error, SafetyError) else "TRENDS_BOOKKEEPING_UNSAVED"
        record_event("trends_bookkeeping_deferred", state="waiting", reason=code)
        atomic_json(folder / PENDING_BOOKKEEPING, {"reason": code})
        return False
    (folder / PENDING_BOOKKEEPING).unlink(missing_ok=True)
    return True


def settle_pending(root, data_root):
    """Retry the bookkeeping of earlier reads that could not save it."""
    from .trends_controller import replay_direct_trends
    settled = 0
    for note in sorted(safe_path(data_root).glob("*/" + PENDING_BOOKKEEPING)):
        try:
            receipt = replay_direct_trends(note.parent)
        except (SafetyError, OSError):
            continue  # its own replay reports the problem when the question is reconciled
        if not _settle(root, note.parent, receipt):
            break  # still busy: later
        settled += 1
        if settled >= 20:
            break  # a bounded catch-up per read


def capture_signed_in_trends(request, acquisition_id, *, data_root, timeout=45, signed_in_root=None):
    """The controller's Trends capture: one signed-in read, then its rising
    queries (``trends_rising``), its place in the proof gate
    (``trends_proof_gate``) and, for a page that did not draw, the route's
    rendering pause."""
    from .trends_controller import capture_direct_trends
    root = PROJECT_ROOT if signed_in_root is None else signed_in_root
    folder = safe_path(data_root, acquisition_id)
    fresh = not folder.exists()
    receipt = capture_direct_trends(request, acquisition_id, data_root=data_root, timeout=timeout,
                                    transport="signed-in", signed_in_root=root)
    if fresh:
        settle_pending(root, data_root)
        _settle(root, folder, receipt)
    return receipt


def set_aside_unsent(folder):
    """Move an unshared read's folder away after a start that never navigated,
    so the question's next try (same acquisition id) starts fresh."""
    folder = safe_path(folder)
    if folder.exists():
        folder.rename(safe_path(folder.parent / aside_name(folder.name, utc_now(timespec="microseconds"))))


def _sequence(state, owner):
    from .source_acquisitions import _stage
    return str(_stage(state, owner, owner["active_dispatch_intent_id"])["intent_sequence"])


def _set_aside_notes(root, acquisition_id, sequence):
    return sorted(safe_path(root).glob(acquisition_id + "-not-started-" + sequence + "-*/" + NOT_STARTED))


def not_started_pending(root, acquisition_id, state, owner):
    """A dispatching shared acquisition whose read never started, not yet reopened."""
    if owner["state"] != "dispatching" or not owner.get("active_dispatch_intent_id"):
        return False
    return (safe_path(root / acquisition_id / NOT_STARTED).is_file()
            or bool(_set_aside_notes(root, acquisition_id, _sequence(state, owner))))


def reopen_unsent(memory, root, acquisition_id, as_of):
    """Return a shared acquisition to "reserved" after a read that never
    started: its whole capture folder is moved to
    ``<id>-not-started-<sequence>-<time>`` (with the not-sent proof inside),
    so the next read starts fresh."""
    from .source_acquisitions import _record
    folder = safe_path(root / acquisition_id)
    state = memory.snapshot(); owner = _record(state, acquisition_id)
    note = folder / NOT_STARTED
    if not note.is_file() and owner["state"] == "dispatching":
        note = (_set_aside_notes(root, acquisition_id, _sequence(state, owner)) or [note])[-1]

    def set_aside(sequence, found):
        aside = (note.parent if note.parent != folder
                 else safe_path(root / aside_name(acquisition_id, found["finished_at"], sequence)))
        if folder.exists():
            folder.rename(aside)
        return aside / "not-started", aside / NOT_STARTED
    reopen_not_started(memory, acquisition_id, note, as_of, set_aside=set_aside)


def unsent_read(memory, root, acquisition_id, error, *, signed_in_root, as_of):
    """Note and reopen a shared read that raised ``SignedInWait`` mid-start."""
    try:
        note_not_started(safe_path(root / acquisition_id), error)
        reopen_unsent(memory, root, acquisition_id, as_of)
    except (SafetyError, OSError):
        # Retried by the next resolution, still without a read.
        raise SignedInWait("SIGNED_IN_START_RECOVERY_PENDING", next_retry_at=signed_in_recheck_at(
            signed_in_root, "SIGNED_IN_START_RECOVERY_PENDING")) from None
