"""The shared source record of each wider-discovery read (Plan R10 W8).

Every read of the wider lanes is a source acquisition like the other lanes'
reads (``source_acquisitions``): claimed for one physical request, marked
dispatching before its browser starts, and closed with its response. That
response feeds the shared access wait of the site and reader identity
(``source_coverage``), so a refusal seen here holds the heading searches and
supplier searches of the same site, and theirs hold these reads. When that
wait is due, a read here takes part in the site's shared probe like theirs.

* Meta search for a rising query: the heading searches' own request, reader
  and response bridge (``capture_meta_search``, ``record_meta_response``),
  kept apart from theirs by ``capture_options`` (their saved captures of the
  same address are never touched).
* Advertiser page: its own reader and bridge (``meta_advertisers``).
* IndiaMART / TradeIndia results page: the supplier lane's own request,
  reader, saved page (in the supplier lane's own folder,
  ``supplier_routes/shared``) and response bridge (``supplier_search``), so a
  page read here and a type's supplier search of the same words are one read
  that both lanes replay. TradeIndia
  is read in the browser only after its plain page proved unreadable
  (``supplier_request_for_state``).
"""

from pathlib import Path

from .models import digest, timestamp, utc_now
from .storage import atomic_json, safe_path

META_PURPOSE = {"purpose": "wider-discovery"}
REFRESH_REASON = "Wider discovery reads this page again after its recheck days."


def read_root(memory, lane):
    """Where a lane's raw reads are saved: supplier pages where the supplier
    lane saves (and replays) its own, the Meta pages under ``wider_discovery``."""
    if lane == "supplier-listings":
        return safe_path(memory.root, "supplier_routes", "shared")
    return safe_path(memory.root, "wider_discovery")


def physical_request(state, target, *, retry=0):
    """(route, request, capture_options) of a target's next read; ``retry`` names
    a new generation after a refusal another lane's read has since cleared."""
    lane, route = target["lane"], target["route"]
    extra = {"retry": retry} if retry else {}
    if lane == "meta-advertisers":
        from .meta_advertisers import prepare_advertiser_request
        request = prepare_advertiser_request(target["request"]["advertiser_id"])
        return route, {name: request[name] for name in ("url", "country", "limit")}, dict(META_PURPOSE, **extra)
    if lane == "trends-rising":
        from .meta_browser import prepare_meta_query
        return route, prepare_meta_query(target["request"]["query"]), dict(META_PURPOSE, **extra)
    from .supplier_search import supplier_request_for_state
    return route, supplier_request_for_state(state, route, target["request"]["term"]), extra


def access_scope(route, request):
    """The shared access identity the other lanes use for this request."""
    from .source_coverage import SIGNED_IN_TRANSPORT, signed_in_access_scope, source_access_scope
    if request.get("transport") == SIGNED_IN_TRANSPORT:
        return signed_in_access_scope(request["url"])
    if route in ("indiamart", "tradeindia"):
        return source_access_scope(request["url"], transport="supplier-public", access_identity="owned-supplier-public")
    return source_access_scope(request["url"])


def open_read(memory, route, request, capture_options, *, owner_id, as_of, root):
    """Claim the read: ``{"action": "read", "acquisition_id"}``, or a skip with its reason.

    As in the other lanes, a refused original whose site wait is due is read
    again as a new generation of the same request (``access_recovery``); this
    read is then the site's probe (``probe_changes``). A refused original whose
    wait another read has cleared is ``SOURCE_REFUSAL_CLEARED`` (the caller may
    start a limited new generation); an unfinished or failed original is never
    asked again (``SOURCE_ORIGINAL_INCOMPLETE``)."""
    from .signed_in_attempts import NOT_STARTED
    from .source_acquisitions import _record, _response, claim_source_acquisition
    from .source_coverage import source_access_wait, source_response_closed
    options = {"owner_id": owner_id, "as_of": as_of, "capture_options": capture_options,
               "artifact_roots": [root], "collection_channel": "direct_local",
               "incremental_collection_fee": "unknown" if route == "meta-ads" else "known_zero"}
    claim = claim_source_acquisition(memory, route, request, **options)
    if claim["action"] == "refresh_due":
        claim = claim_source_acquisition(memory, route, request, refresh_due_at=as_of, refresh_reason=REFRESH_REASON,
                                         **options)
    aid = claim["acquisition_id"]
    if claim["action"] == "reuse":
        return {"action": "skip", "acquisition_id": aid, "reason": "SOURCE_RECENTLY_READ"}
    if claim["action"] == "join":
        folder = safe_path(Path(root), aid)
        if claim["owner_id"] == owner_id and claim["state"] == "dispatching" and (folder / NOT_STARTED).is_file():
            # A signed-in start that never navigated, whose reopening was interrupted.
            from .supplier_search import _reopen_not_started
            _reopen_not_started(memory, aid, folder, as_of)
            claim = dict(claim, state="reserved")
        if not (claim["owner_id"] == owner_id and claim["state"] == "reserved"):
            state = memory.snapshot()
            wait = source_access_wait(state, access_scope(route, request), as_of=as_of)
            if wait and wait["phase"] == "due" and wait["acquisition_id"] == aid:
                recovery = {name: wait[name] for name in ("failure_id", "failure_hash", "response_id", "response_hash",
                                                          "next_retry_at")}
                claim = claim_source_acquisition(memory, route, request, access_recovery=recovery, **options)
                return {"action": "read", "acquisition_id": claim["acquisition_id"]}
            owner = _record(state, aid)
            response = _response(state, owner)
            cleared = (owner["state"] == "failed_access" and response is not None and source_response_closed(response)
                       and not (wait and wait["acquisition_id"] == aid))
            return {"action": "skip", "acquisition_id": aid,
                    "reason": "SOURCE_REFUSAL_CLEARED" if cleared else "SOURCE_ORIGINAL_INCOMPLETE"}
    return {"action": "read", "acquisition_id": claim["acquisition_id"]}


def probe_changes(memory, route, request, acquisition_id, *, caller_id, as_of):
    """When the site's shared wait is due, this read is its probe: the changes
    to save with the read's own reservation (``marketplace_access_admission``)."""
    from .source_coverage import marketplace_access_admission
    return marketplace_access_admission(memory, {"access_scope": access_scope(route, request), "acquisition_id": acquisition_id},
                                        caller_id=caller_id, as_of=as_of)["changes"]


def _supplier_page(memory, record, request, readers, signed_in_root):
    """One supplier results page, saved as the supplier lane saves its pages."""
    from .supplier_search import CAPTURE_CONTRACT, _item, _note_not_started, _reopen_not_started
    from .source_coverage import SIGNED_IN_TRANSPORT
    folder, aid, route = Path(record["folder"]), record["acquisition_id"], record["route"]
    item = _item(aid, request)
    intent = {"contract": CAPTURE_CONTRACT, "acquisition_id": aid, "request": request}
    atomic_json(folder / "intent.json", intent)
    if request["transport"] == SIGNED_IN_TRANSPORT:
        from .signed_in_attempts import SignedInWait, count_signed_in_read, release_unsent_read
        counted = None
        try:
            counted = count_signed_in_read(signed_in_root, route)  # pacing may still refuse it here
            receipt = readers["indiamart"](item, folder=folder / "native", root=signed_in_root)
        except SignedInWait as error:
            # Never on the clock before this read's own dispatch mark.
            now = max(record["started_at"], utc_now(timespec="microseconds"), key=timestamp)
            if error.started_at is None:  # refused before the browser was asked (pacing)
                error.started_at = error.finished_at = now
            # Never sent: the acquisition goes back to "reserved" for the next read
            # (a reopening that fails is retried by the next claim, see open_read).
            release_unsent_read(signed_in_root, route, counted)
            _note_not_started(folder, error)
            _reopen_not_started(memory, aid, folder, now)
            raise
    elif request["transport"] == "public-http":
        receipt = readers["tradeindia"](item)
    else:
        receipt = readers["tradeindia-browser"](item, folder=folder / "native")
    atomic_json(folder / "page.json", {**intent, "receipt": receipt, "hash": digest(receipt)})


def read(memory, record, readers, signed_in_root):
    """The physical read (the acquisition is already marked dispatching)."""
    folder, aid = Path(record["folder"]), record["acquisition_id"]
    folder.mkdir(parents=True, exist_ok=True)
    if record["lane"] == "meta-advertisers":
        from .meta_advertisers import capture_advertiser_ads
        capture_advertiser_ads(folder.parent, aid, record["request"]["advertiser_id"], browser=readers["meta"])
    elif record["lane"] == "trends-rising":
        from .meta_browser import capture_meta_search
        capture_meta_search(folder.parent, aid, record["request"]["query"], browser=readers["meta"], retain_transport=True)
    else:
        _supplier_page(memory, record, record["physical_request"], readers, signed_in_root)


def close(memory, record, *, as_of):
    """Record the saved response and its access answer; returns the lane outcome.

    Saved files only: a read whose result was not saved raises
    ``SOURCE_ORIGINAL_RECEIPT_PENDING`` and is never read again here.
    """
    from .source_coverage import _record_marketplace_access
    folder, aid = safe_path(Path(record["folder"])), record["acquisition_id"]
    at = max(as_of, utc_now(timespec="microseconds"), key=timestamp)
    if record["lane"] == "supplier-listings":
        from .supplier_search import _record_response
        result, original, parsed = _record_response(memory, aid, folder=folder, as_of=at)
        response = memory.snapshot()["source_checks"][result["stage_id"]]
        outcome = {"observed_at": original["receipt"]["checked_at"], "results": parsed["results"],
                   "reason": parsed["reason"]}
    else:
        if record["lane"] == "meta-advertisers":
            from .meta_advertisers import capture_advertiser_ads, record_advertiser_response
            result = record_advertiser_response(memory, aid, folder=folder, as_of=at)
            receipt, _ = capture_advertiser_ads(folder.parent, aid, record["request"]["advertiser_id"], reconcile=True)
        else:
            from .meta_browser import capture_meta_search
            from .source_acquisitions import record_meta_response
            result = record_meta_response(memory, aid, folder=folder, as_of=at)
            receipt, _ = capture_meta_search(folder.parent, aid, record["request"]["query"], reconcile=True)
        response = memory.snapshot()["source_checks"][result["stage_id"]]
        outcome = {"observed_at": receipt["observed_at"], "records": receipt["records"], "raw_hash": receipt["raw_hash"],
                   "reason": receipt["reason"]}
    _record_marketplace_access(memory, aid, result["stage_id"], as_of=at)
    state, reason = response["source_state"], response.get("parse_reason")
    if state in ("usable", "partial"):
        outcome["state"] = "read"
    elif state == "empty":
        outcome["state"] = "empty"
    elif state == "blocked" and reason != "OWNER_SIGN_IN_REQUIRED":
        outcome["state"] = "refused"  # an expired sign-in waits on the owner's hold, not a site refusal
    elif (record["route"] == "tradeindia" and reason == "SUPPLIER_ROUTE_LAYOUT_UNRECOGNISED"
          and record["physical_request"]["transport"] == "public-http"):
        # The plain page proved unreadable: the browser read follows next round.
        outcome.update(state="not_started", reason="WIDER_DISCOVERY_BROWSER_READ_NEXT")
    else:
        outcome["state"] = "failed"
    return outcome


def abandon(memory, record, reason, *, as_of):
    """A read with no saved result: its acquisition's execution is unknown,
    unless it proved it never started (its reopening is finished by the next
    claim, ``open_read``). Returns the acquisition's state before, or
    ``"never_sent"``."""
    from .signed_in_attempts import NOT_STARTED
    from .source_acquisitions import _record, mark_source_execution_unknown
    owner = _record(memory.snapshot(), record["acquisition_id"])
    if (Path(record["folder"]) / NOT_STARTED).is_file():
        return "never_sent"
    if owner["state"] == "dispatching":
        mark_source_execution_unknown(memory, record["acquisition_id"], reason=reason,
                                      as_of=max(as_of, utc_now(timespec="microseconds"), key=timestamp))
    return owner["state"]
