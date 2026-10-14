"""IndiaMART search, read in the owner's signed-in research profile (D2, D6).

Reading only: the supplier routes' page snapshot (``_DOM``) copies the
rendered HTML, text and links; nothing on the page is clicked, typed,
submitted or scrolled - contact, "View number", "Get best price", "Send
enquiry" and any paid or membership prompt included. A read starts only when
the owner has signed in, the sign-in window is closed, the desktop is
available and pacing allows it (``signed_in_hold``); otherwise the route waits
calmly with the reason (``SignedInWait``). A sign-in wall seen in the
signed-in profile is recorded as an expired sign-in, so the route then waits
with ``OWNER_SIGN_IN_REQUIRED`` until the owner signs in again.

The research account's own details are removed from the page before it is
judged, returned or kept, and from the saved browser result even when the
read is interrupted (``indiamart_redaction``).
"""

import json

from .indiamart_redaction import protect_saved, redact_output
from .models import SafetyError, utc_now
from .signed_in_browser import signed_in_read, signed_in_wait
from .signed_in_pacing import signed_in_pace

SITE = ROUTE = "indiamart"
PROFILE = "signed-in"


class SignedInWait(SafetyError):
    """A calm wait: no page was requested. The controller records it as
    waiting, not as a failure. ``started_at``/``finished_at`` are set when the
    read had begun (its acquisition must be reopened before the next read)."""

    calm_wait = True

    def __init__(self, reason, *, next_retry_at=None, started_at=None, finished_at=None):
        super().__init__(reason)
        self.next_retry_at, self.started_at, self.finished_at = next_retry_at, started_at, finished_at


def signed_in_marker(snapshot):
    """False for IndiaMART's sign-in wall; True when the page shows the visitor
    signed in or carries its search response; None without a clear marker."""
    from .supplier_route_acquisition import NEXT_DATA, indiamart_login_wall
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("html"), str) or not isinstance(snapshot.get("text"), str):
        return None
    if indiamart_login_wall(snapshot):
        return False
    data = NEXT_DATA.search(snapshot["html"])
    try:
        props = json.loads(data[1])["props"]["pageProps"] if data else None
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(props, dict) or props.get("dummyOtpPage") is True:
        return None
    visitor = props.get("imeshVObject")
    if isinstance(visitor, dict) and isinstance(visitor.get("glid"), str) and visitor["glid"].strip():
        return True
    return True if isinstance(props.get("searchResponse"), (dict, list)) and props["searchResponse"] else None


def signed_in_hold(root, *, as_of=None):
    """None when a signed-in IndiaMART read may start, else its calm wait."""
    wait = signed_in_wait(root, SITE, as_of=as_of)
    if wait:
        return {"reason": wait, "next_retry_at": None}
    return signed_in_pace(root, SITE, as_of=as_of)


def fetch_indiamart_signed_in_page(item, *, folder, root):
    """One gated, read-only signed-in search page as a supplier receipt.

    The caller has already counted this read for pacing. A read that could not
    start (sign-in needed, window open, desktop locked, browser busy or not
    startable) raises ``SignedInWait`` with its reason; nothing was navigated.
    """
    from .supplier_route_acquisition import fetch_supplier_native_page
    asked, answered = [], []

    def cleaned(url, expression, target, **options):
        from .native_public_browser import native_snapshot
        asked.append(target)
        try:
            output = native_snapshot(url, expression, target, **options)
            answered.append(output)
        finally:
            protect_saved(target / "native_result.json")  # also when interrupted
        return redact_output(output)

    def capture(url, expression, target, **options):
        started = utc_now()
        try:
            result = signed_in_read(SITE, url, expression, target, marker=signed_in_marker, root=root, route=ROUTE,
                                    browser=cleaned, **options)
        except SafetyError as error:
            if answered:
                raise  # the browser answered: whether it navigated stays for recovery to prove
            # The browser raises its own refusals only before it launches anything.
            raise SignedInWait(str(error), started_at=started, finished_at=utc_now()) from None
        except OSError as error:
            if (answered or isinstance(error, TimeoutError) or asked and any(
                    (target / name).exists() for name in ("owned_process.json", "native_dispatch.json", "native_result.json"))):
                raise
            raise SignedInWait("SIGNED_IN_BROWSER_START_FAILED", started_at=started, finished_at=utc_now()) from None
        if result.get("state") == "waiting" and result.get("navigation_started") is False:
            raise SignedInWait(result.get("reason") or "OWNER_SIGN_IN_REQUIRED", started_at=started, finished_at=utc_now())
        return result

    return dict(fetch_supplier_native_page(item, route=ROUTE, folder=folder, native_capture=capture), profile=PROFILE)
