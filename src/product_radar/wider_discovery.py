"""Wider discovery (Plan R10 W8): more ways for new product candidates to arrive.

It runs inside the discovery step, after the round's heading searches
(``catalogue_discovery``), so those keep the first use of the round's source
reads and of the AI share held for discovery. Three lanes (choosing:
``wider_candidates``) nominate candidates for the existing admission
(``research_flow.run_flow``):

* ``meta-advertisers``: an advertiser behind a relevant reviewed ad has its
  other active India ads read from its Ad Library page (``meta_advertisers``),
  the same free direct read as the searches.
* ``trends-rising``: a rising related query saved with a Trends read becomes
  one Meta search (``meta_browser.capture_meta_search``), the discovery route.
* ``supplier-listings``: IndiaMART's and TradeIndia's results pages for mapped
  category terms, through each route's own reader and parser: IndiaMART in the
  owner's signed-in profile with its pacing and the round's signed-in share
  (``signed_in_round``), TradeIndia read plainly first and in the browser only
  when the plain page's layout is not recognised (the next round). IndiaMART
  yields to the type supplier searches: it is read only after the round's
  stages, and not while a type close to a report waits for a supplier
  search. Listings not seen before become candidates (the sites offer no
  "newest first" order, so "newest" means not seen before). Reading only:
  nothing on any page is clicked.

Each read is a shared source acquisition (``wider_access``): its response
feeds the site's shared access wait, so a refusal met here holds the other
lanes' reads of that site and theirs hold these. Before the read, one of the
round's source reads is reserved (``work_turns.reserve_source_callback``)
together with its saved intent (``batch_runs`` kind
``wider-discovery-read-v1``), so a read is never repeated silently: one found
unfinished at the next round is completed from its saved page or recorded as
uncertain. One read's local failure is saved as that read's failure and the
lane moves on. Ads already seen, listings already captured or covering a
known type, and anything outside the four client categories never reach an
AI call; without the AI, while earlier candidates are being admitted, or once
the day's reads are used, no new read starts. Raw pages stay as files; only
nominated candidates become research events.
"""

from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .models import SafetyError, digest, timestamp, utc_now
from .research_memory import change, key
from .storage import atomic_json, load_json, safe_path
from .wider_access import abandon, access_scope, close, open_read, physical_request, probe_changes, read, read_root
from .wider_candidates import (LANES, READ_KIND, SUPPLIER_ROUTES, advertiser_targets, gate, known_words, latest_reads,
                               reads, rising_targets, settings, supplier_targets)

NOMINATION_VERSION = "wider-discovery-nomination-v1"
SUPPLIER_MAPPER = "wider-discovery-supplier-v1"
# A round's reads stop for these; the next round tries again.
STOP_CODES = ("WORK_TURN_SOURCE_LIMIT", "WORK_TURN_CLOSED", "PROGRAMME_OWNER_PAUSED")
# One read's or one record's local failure is that read's own (saved as failed);
# a damaged shared store is not (``catalogue_discovery._raise_shared_integrity``).
_CONTAINED = (SafetyError, OSError, KeyError, TypeError, ValueError)
MODEL_RECHECK_SECONDS = 300
# New generations of one key's request after refusals another read has since
# cleared, per recheck window (a rising query: ever).
MAX_NEW_GENERATIONS = 2
_LISTING_PATHS = {"indiamart": "/proddetail/", "tradeindia": "/products/"}
_LABELS = {"indiamart": "IndiaMART", "tradeindia": "TradeIndia"}
LISTING_LIMITATION = ("A supplier directory listing: it nominates a product to research; it is not demand, sales "
                      "or a market vote.")


def _readers():
    from functools import partial
    from .indiamart_signed_in import fetch_indiamart_signed_in_page
    from .native_public_browser import native_snapshot
    from .public_pages import fetch_public_page
    from .supplier_route_acquisition import fetch_supplier_native_page
    return {"meta": native_snapshot, "indiamart": fetch_indiamart_signed_in_page,
            "tradeindia": partial(fetch_public_page, retain_transport=True),
            "tradeindia-browser": partial(fetch_supplier_native_page, route="tradeindia")}


def _save(memory, old, updated):
    memory.put("batch_runs", updated["id"], updated, previous=old,
               event_id=key("wider-discovery-read-state", [updated["id"], digest(old), digest(updated)]))


def _reserve(memory, record, at, probe=()):
    """Reserve one of the round's source reads, joined with the read's saved
    intent (and, for a site wait that is due, its shared probe)."""
    from .programme import programme_service_state
    from .work_turns import active_work_turn, reserve_source_callback
    if not programme_service_state(memory)["enabled"]:
        raise SafetyError("PROGRAMME_OWNER_PAUSED")
    joined = [change("batch_runs", record["id"], record), *probe]
    if active_work_turn(memory) is not None:
        reserve_source_callback(memory, record["id"], at=at, lane="exploration", changes=joined)
    else:
        memory.commit(key("wider-discovery-intent", record["id"]), joined)


def _target_wait(state, target, at, signed_in_root, context):
    """Why this read may not start now, or None: IndiaMART yields to the type
    supplier searches and waits for the owner's signed-in hold; any site's
    shared access wait (a refusal seen by any lane) holds it."""
    from .catalogue_discovery import _raise_shared_integrity
    from .source_coverage import source_access_wait
    def wait(reason, next_retry_at=None):
        return {"lane": target["lane"], "route": target["route"], "reason": reason, "next_retry_at": next_retry_at}
    if target["route"] == "indiamart":
        # Its four reads a round belong to the supplier searches of types close to a
        # report: the wider read runs only after the round's stages and only when
        # none of them waits for a supplier search.
        if context.get("discovery_position") != "after" or (context.get("priority_concept_ids") or {}).get("suppliers"):
            return wait("WIDER_DISCOVERY_SUPPLIERS_FIRST")
        from .indiamart_signed_in import signed_in_hold
        hold = signed_in_hold(signed_in_root, as_of=at)
        if hold:
            return wait(hold["reason"], hold["next_retry_at"])
    try:
        route, request, _ = physical_request(state, target)
        found = source_access_wait(state, access_scope(route, request), as_of=at)
    except SafetyError as error:
        _raise_shared_integrity(error)
        return wait(str(error))
    # A due wait is no wait: this read becomes the site's probe (``_read``).
    return wait(found["reason"], found["next_retry_at"]) if found and found["phase"] != "due" else None


def _round_facts(state, catalogue):
    """What is already known, once per round: ads seen, listings captured and
    their titles, and the word sets of known phrases and types."""
    from .platform_matching import words
    facts = {"ads": set(), "listings": set(), "titles": set(), "known": known_words(state, catalogue)}
    for row in state["observations"].values():
        if row["source"] == "meta":
            facts["ads"].add(row["facts"].get("ad_id"))
        elif row["source"] in SUPPLIER_ROUTES:
            facts["listings"].add((row["source"], row["facts"].get("url")))
            facts["titles"].add(frozenset(words(row["semantic"].get("title") or "")))
    return facts


def _meta_choice(record, outcome, facts, config):
    """New active ads; an advertiser's ads also pass the category gate one by one."""
    lane = record["lane"]
    limit = config["meta_advertisers"]["ads_per_read"] if lane == "meta-advertisers" else config["trends_rising"]["cards_per_read"]
    chosen = []
    for ad in outcome["records"]:
        if len(chosen) >= limit:
            break
        if ad["ad_id"] in facts["ads"] or ad.get("active") is False:
            continue
        if lane == "meta-advertisers" and gate(ad["presentation"].get("body") or "") is None:
            continue
        chosen.append(ad)
        facts["ads"].add(ad["ad_id"])
    return chosen


def _listing_choice(record, outcome, facts, config):
    """Product listings not captured before, not covering a known type, in a client category."""
    from .platform_matching import words
    route, rows = record["route"], []
    for result in outcome["results"]:
        if len(rows) >= config["supplier_listings"]["listings_per_read"]:
            break
        url, title = result.get("url"), " ".join(str(result.get("title") or "").split())
        found = frozenset(words(title))
        if (not isinstance(url, str) or not urlsplit(url).path.casefold().startswith(_LISTING_PATHS[route])
                or (route, url) in facts["listings"] or not found or found in facts["titles"]
                or any(known <= found for known in facts["known"]) or gate(title) is None):
            continue
        facts["listings"].add((route, url))
        facts["titles"].add(found)
        rows.append({"semantic": {"title": title, "product_url": url, "platform": _LABELS[route]},
                     "facts": {"source_route": route, "item_id": url, "url": url, "list_type": "supplier_listing_discovery",
                               "query": record["request"]["term"], "category_term": record["category"],
                               **{name: result.get(name) for name in ("supplier_name", "location", "price_text", "moq_text")},
                               "limitations": [LISTING_LIMITATION]}})
    return rows


def _nomination_path(record):
    return Path(record["nomination_folder"]) / ("receipt.json" if record["lane"] == "supplier-listings" else "nomination.json")


def _nominate(memory, record, outcome, facts, config):
    """Choose once (saved before import, so a crash re-imports the same rows),
    then import; returns (capture id or None, candidate count)."""
    from .platform_matching import import_platform_capture
    from .research_memory import import_capture
    path = _nomination_path(record)
    if path.is_file():
        saved = load_json(path, 8 * 1024 * 1024)
        if not isinstance(saved, dict) or saved.get("version") != NOMINATION_VERSION or saved.get("acquisition_id") != record["acquisition_id"]:
            raise SafetyError("WIDER_DISCOVERY_NOMINATION_CONFLICT")
    else:
        rows = (_listing_choice if record["lane"] == "supplier-listings" else _meta_choice)(record, outcome, facts, config)
        saved = {"version": NOMINATION_VERSION, "acquisition_id": record["acquisition_id"], "lane": record["lane"],
                 "route": record["route"], "observed_at": outcome["observed_at"],
                 "baseline_concepts": sorted(memory.snapshot()["concepts"]), "rows": rows}
        atomic_json(path, saved)
    if not saved["rows"]:
        return None, 0
    if record["lane"] == "supplier-listings":
        capture_id, _ = import_platform_capture(memory, record["route"], record["acquisition_id"], saved["rows"],
            observed_at=saved["observed_at"], reference=path, mapper=SUPPLIER_MAPPER,
            request={"lane": record["lane"], "route": record["route"], "term": record["request"]["term"]})
        return capture_id, len(saved["rows"])
    mapped = {"status": "usable", "records": saved["rows"], "rejected": []}
    imported = import_capture(memory, source="meta", acquisition_id=record["acquisition_id"],
        observed_at=saved["observed_at"], mapped=mapped, reference=str(path),
        capture_hash=digest([NOMINATION_VERSION, outcome["raw_hash"], [row["ad_id"] for row in saved["rows"]]]))
    return imported["capture_id"], len(saved["rows"])


def _failure(error):
    return str(error) if isinstance(error, SafetyError) else "WIDER_DISCOVERY_LOCAL_FAILURE"


def _finish(memory, record, outcome, facts, config, at):
    """Save the read's outcome; a nomination that cannot be saved fails this read only."""
    from .catalogue_discovery import _raise_shared_integrity
    updated = dict(record, state=outcome["state"], reason=outcome.get("reason"), updated_at=at,
                   next_retry_at=outcome.get("next_retry_at"))
    if outcome["state"] == "read":
        try:
            updated["capture_id"], updated["candidates"] = _nominate(memory, record, outcome, facts, config)
        except _CONTAINED as error:
            _raise_shared_integrity(error)
            updated.update(state="failed", reason=_failure(error))
    _save(memory, record, updated)
    return {name: updated[name] for name in ("lane", "route", "key", "state", "reason", "candidates")}


def _outcome(memory, record, at):
    """Close the read from its saved result, or say why it has none."""
    from .catalogue_discovery import _raise_shared_integrity
    try:
        return close(memory, record, as_of=at)
    except _CONTAINED as error:
        _raise_shared_integrity(error)
        if _failure(error) != "SOURCE_ORIGINAL_RECEIPT_PENDING":
            return {"state": "failed", "reason": _failure(error)}
    # Nothing saved: never sent (the claim is still reserved), else uncertain and not sent again.
    prior = abandon(memory, record, "WIDER_DISCOVERY_RESULT_NOT_SAVED", as_of=at)
    return {"state": "not_started" if prior in ("reserved", "never_sent") else "uncertain",
            "reason": "SOURCE_ORIGINAL_RECEIPT_PENDING"}


def _generation(memory, target, at, config):
    """The latest generation of this key's request, and whether another new one
    may start now (at most two per recheck window after cleared refusals)."""
    rows = [row for row in reads(memory.snapshot()) if (row["lane"], row["key"]) == (target["lane"], target["key"])]
    retry = max((row.get("retry", 0) for row in rows), default=0)
    days = {"meta-advertisers": config["meta_advertisers"]["recheck_days"], "trends-rising": None,
            "supplier-listings": config["supplier_listings"]["recheck_days"]}[target["lane"]]
    recent = [row for row in rows if row.get("retry_of")
              and (days is None or timestamp(at) - timestamp(row["started_at"]) < timedelta(days=days))]
    return retry, len(recent) < MAX_NEW_GENERATIONS


def _read(memory, target, *, programme_id, at, readers, signed_in_root, facts, config):
    from .catalogue_discovery import _raise_shared_integrity
    from .signed_in_attempts import SignedInWait
    from .source_acquisitions import mark_source_dispatching
    owner = key("wider-discovery-owner", [target["lane"], target["key"]])
    root = read_root(memory, target["lane"])
    retry, may_renew = _generation(memory, target, at, config)
    route, request, options = physical_request(memory.snapshot(), target, retry=retry)
    claim = open_read(memory, route, request, options, owner_id=owner, as_of=at, root=root)
    retry_of = None
    if claim["action"] == "skip" and claim["reason"] == "SOURCE_REFUSAL_CLEARED" and may_renew:
        # The site answers others again: one more generation of this request.
        retry_of, retry = claim["acquisition_id"], retry + 1
        route, request, options = physical_request(memory.snapshot(), target, retry=retry)
        claim = open_read(memory, route, request, options, owner_id=owner, as_of=at, root=root)
    aid = claim["acquisition_id"]
    record = {"kind": READ_KIND, "id": key("wider-discovery-read", [aid, at]),
              **{name: target[name] for name in ("lane", "route", "key", "category", "request", "basis")},
              "physical_request": request, "capture_options": options, "retry": retry, "retry_of": retry_of,
              "programme_id": programme_id, "acquisition_id": aid, "folder": str(root / aid),
              "nomination_folder": str(safe_path(memory.root, "wider_discovery", aid)), "started_at": at, "updated_at": at,
              "state": "intent_saved", "reason": None, "next_retry_at": None, "capture_id": None, "candidates": 0}
    if claim["action"] == "skip":
        # No request is sent: a fresh saved read counts as read; an unfinished or
        # failed original is never asked again (the key waits its recheck days).
        record.update(state="empty" if claim["reason"] == "SOURCE_RECENTLY_READ" else "failed", reason=claim["reason"])
        memory.put("batch_runs", record["id"], record, event_id=key("wider-discovery-skip", record["id"]))
        return {name: record[name] for name in ("lane", "route", "key", "state", "reason", "candidates")}
    from .source_coverage import source_access_wait
    probe = []
    if source_access_wait(memory.snapshot(), access_scope(route, request), as_of=at):
        probe = probe_changes(memory, route, request, aid, caller_id=record["id"], as_of=at)
    _reserve(memory, record, at, probe)
    try:
        mark_source_dispatching(memory, aid, as_of=max(at, utc_now(timespec="microseconds"), key=timestamp))
        read(memory, record, readers, signed_in_root)
    except SignedInWait as error:
        return _finish(memory, record, {"state": "not_started", "reason": str(error), "next_retry_at": error.next_retry_at},
                       facts, config, at)
    except _CONTAINED as error:
        _raise_shared_integrity(error)
    return _finish(memory, record, _outcome(memory, record, at), facts, config, at)


def _pending(state, at, config):
    """Reads whose candidates are still being admitted (oldest first)."""
    from .discovery_admission import unsettled
    rows = [dict(row, imported_at=state["captures"][row["capture_id"]]["imported_at"]) for row in reads(state)
            if row.get("capture_id") in state["captures"]]
    return unsettled(state, rows, at, config["admission"]["settle_after_days"])


def _round_hold(state, rows, at, config, runner):
    """No new reads without the AI, while earlier candidates are being admitted,
    or once the day's reads are used."""
    if runner is None:
        # Candidates read now would only age unjudged until they count as settled.
        return {"reason": "WIDER_DISCOVERY_MODEL_WAITING",
                "next_retry_at": (timestamp(at) + timedelta(seconds=MODEL_RECHECK_SECONDS)).isoformat()}
    if len(_pending(state, at, config)) >= config["admission"]["max_pending_reads"]:
        return {"reason": "WIDER_DISCOVERY_ADMISSION_PENDING", "next_retry_at": None}
    day = [row for row in rows if row["state"] != "not_started"
           and timedelta(0) <= timestamp(at) - timestamp(row["started_at"]) < timedelta(hours=24)]
    if len(day) >= config["admission"]["daily_reads"]:
        oldest = day[len(day) - config["admission"]["daily_reads"]]
        return {"reason": "WIDER_DISCOVERY_DAILY_READS_USED",
                "next_retry_at": (timestamp(oldest["started_at"]) + timedelta(hours=24)).isoformat()}
    return None


def _admit(memory, runner, at, config, flow_limit):
    """Continue the existing admission of pending candidates; returns new type ids."""
    from .discovery_admission import admit
    pending = _pending(memory.snapshot(), at, config)[:10]
    if not pending:
        return {"pending_reads": 0, "new_concept_ids": []}
    if runner is None:
        # Without the AI (paused or not connected) candidates wait unjudged; a
        # run without it would record them as failed descriptions.
        return {"state": "waiting", "reason": "WIDER_DISCOVERY_MODEL_WAITING", "pending_reads": len(pending),
                "new_concept_ids": []}
    paths = {row["acquisition_id"]: _nomination_path(row) for row in pending}
    new = admit(memory, runner, list(paths), as_of=at, flow_limit=flow_limit,
                baseline=lambda aid: load_json(paths[aid], 8 * 1024 * 1024)["baseline_concepts"])
    return {"pending_reads": len(pending), "new_concept_ids": new}


def advance_wider_discovery(memory, context, *, programme_id, catalogue=None, readers=None, signed_in_root=None):
    """One bounded wider-discovery turn: finish unfinished reads, read within the
    lanes' limits, then continue admission of the candidates."""
    from .catalogue import load_catalogue, validate_catalogue
    from .catalogue_discovery import _raise_shared_integrity
    from .catalogue_expansion import effective_catalogue
    from .signed_in_browser import PROJECT_ROOT
    from .work_turns import active_work_turn
    at = context.get("at") or utc_now()
    timestamp(at)
    config = settings()
    readers = readers or _readers()
    signed_in_root = PROJECT_ROOT if signed_in_root is None else signed_in_root
    state = memory.snapshot()
    view = effective_catalogue(validate_catalogue(catalogue or load_catalogue()), state)
    facts = _round_facts(state, view)
    summary = {"state": "checked", "recovered": [], "reads": [], "waits": [], "admission": None}
    for record in [row for row in reads(state) if row["state"] == "intent_saved"]:
        try:
            summary["recovered"].append(_finish(memory, record, _outcome(memory, record, at), facts, config, at))
        except _CONTAINED as error:
            _raise_shared_integrity(error)
            summary["waits"].append({"lane": record["lane"], "route": record["route"], "reason": _failure(error),
                                     "next_retry_at": None})
    state = memory.snapshot()
    rows = reads(state)
    hold = _round_hold(state, rows, at, config, context.get("runner"))
    if hold:
        summary["waits"].append(dict(hold, lane=None, route=None))
    else:
        latest = latest_reads(rows)
        plans = {"meta-advertisers": lambda: advertiser_targets(state, at, config, latest),
                 "trends-rising": lambda: rising_targets(memory.root, state, at, config, latest, view),
                 "supplier-listings": lambda: supplier_targets(state, at, config, latest)}
        turn = active_work_turn(memory)
        # The lanes take turns going first, one turn after another.
        start = (turn["programme_turn_sequence"] if turn else len(rows)) % len(LANES)
        stopped = None
        for lane in LANES[start:] + LANES[:start]:
            try:
                targets = plans[lane]()
            except _CONTAINED as error:
                _raise_shared_integrity(error)
                summary["waits"].append({"lane": lane, "route": None, "reason": _failure(error), "next_retry_at": None})
                continue
            for target in targets:
                # Fresh state each time: a refusal met by the previous read holds this one.
                wait = _target_wait(memory.snapshot(), target, at, signed_in_root, context)
                if wait:
                    summary["waits"].append(wait)
                    continue
                try:
                    summary["reads"].append(_read(memory, target, programme_id=programme_id, at=at, readers=readers,
                                                  signed_in_root=signed_in_root, facts=facts, config=config))
                except _CONTAINED as error:
                    _raise_shared_integrity(error)
                    if _failure(error) in STOP_CODES:
                        stopped = _failure(error)
                        break
                    summary["waits"].append({"lane": target["lane"], "route": target["route"], "reason": _failure(error),
                                             "next_retry_at": None})
            if stopped:
                summary["waits"].append({"lane": lane, "route": None, "reason": stopped, "next_retry_at": None})
                break
    try:
        summary["admission"] = _admit(memory, context.get("runner"), at, config, min(50, context.get("fit_limit", 10)))
    except _CONTAINED as error:
        _raise_shared_integrity(error)
        summary["admission"] = {"state": "waiting", "reason": _failure(error), "new_concept_ids": []}
    summary["new_concept_ids"] = summary["admission"]["new_concept_ids"]
    if summary["waits"] and not summary["reads"]:
        summary["state"] = "waiting"
    return summary


def wider_step(memory, programme_record, context):
    """The discovery step's wider lane (``catalogue_discovery``), after the heading searches."""
    return advance_wider_discovery(memory, context, programme_id=programme_record["programme_id"],
                                   catalogue=context.get("discovery_catalogue"))
