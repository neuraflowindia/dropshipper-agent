"""One programme round's share of the signed-in research browser (Plan R10).

Every signed-in read (Google Trends, IndiaMART, Dropship India, Dropdash)
uses the one visible research browser. Inside a programme round
(``signed_in_round``) they share ``round.signed_in_seconds`` of browser time
(``config/research/signed_in_reads.json``; 5 minutes), and a site listed in
``round.site_reads`` (IndiaMART: 4) makes at most that many reads, so its
supplier searches cannot fill the round. Before each read the round's
remaining time must cover the read's worst case
(``native_public_browser.signed_in_budget``); otherwise the route waits
calmly for the next round. The owner's own commands, outside a round, have
no round share. Process-local: nothing is saved.
"""

from contextlib import contextmanager
from contextvars import ContextVar

TIME_USED = "SIGNED_IN_ROUND_TIME_USED"
READS_USED = "SIGNED_IN_ROUND_READS_USED"
_ROUND = ContextVar("product_radar_signed_in_round", default=None)


@contextmanager
def signed_in_round(*, settings=None):
    """Open one round's share; a nested call keeps the outer round's share."""
    if _ROUND.get() is not None:
        yield _ROUND.get()
        return
    # Settings are read at the first signed-in read: a damaged settings file
    # fails those reads (as their pacing does), never the whole round.
    share = {"settings": settings, "seconds": 0.0, "reads": {}}
    token = _ROUND.set(share)
    try:
        yield share
    finally:
        _ROUND.reset(token)


def round_wait(site):
    """None when this round leaves room for one more read of ``site``, else its calm wait code."""
    share = _ROUND.get()
    if share is None:
        return None
    from .native_public_browser import signed_in_budget
    if share["settings"] is None:
        from .signed_in_pacing import round_settings
        share["settings"] = round_settings()
    cap = share["settings"]["site_reads"].get(site)
    if cap is not None and share["reads"].get(site, 0) >= cap:
        return READS_USED
    if share["seconds"] + signed_in_budget() > share["settings"]["signed_in_seconds"]:
        return TIME_USED
    return None


def note_round_read(site, seconds):
    """Charge one started read and its browser time to the round (if any)."""
    share = _ROUND.get()
    if share is not None:
        share["seconds"] += max(0.0, float(seconds))
        share["reads"][site] = share["reads"].get(site, 0) + 1
