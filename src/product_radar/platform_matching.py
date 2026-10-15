"""Per-type check of a dropshipping platform: a local match, no platform read.

Shopify feeds cannot be searched, and the signed-in platforms are read as
whole catalogue and list pages, so each narrow type is checked against the
platform's saved catalogue (``dropship_platforms``): the words of the type's
reviewed phrases and core label must appear in an item's title, tags or
product type. The few best items become one capture of research events, and
the existing source review judges their relevance in its usual batches. A
match is never a relevance judgment, a market vote or a supplier promise.

The per-type question is local work: it is answered from the saved reads
before any source callback is admitted (``source_access_preflight`` returns
the saved answer), so it never takes a work-turn source slot or sends
anything. ``dispatch_started`` is true because those reads actually happened.
Questions are planned only once a platform has a finished catalogue, and the
request carries that catalogue's date, so each weekly refresh is matched
again (``dropship_platforms.catalogue_date``); should the saved catalogue
still be unusable, the question waits calmly with the platform's own reason.
Coverage is judged on the item's title; tags and product type only break ties.
"""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from .discovery import meaningful_tokens, normalise
from .dropship_platforms import (LABELS, LIST_ROLES, ROUTES, catalogue_wait, load_catalogue, load_refresh,
                                 platform_folder, settings)
from .signed_in_attempts import SignedInWait
from .models import SafetyError, digest, identifier, timestamp, utc_now
from .research_memory import change, key
from .storage import atomic_json, load_json, safe_path

MATCH_VERSION = "dropship-local-match-v2"
DISCOVERY_MAPPER = "dropship-discovery-v1"
CAPTURE_LIMIT = 4 * 1024 * 1024
_WORD_CACHE = {}


def _stem(word):
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def words(text):
    return {_stem(word) for word in meaningful_tokens(text or "")}


def match_terms(profile, *, limit):
    """The type's reviewed phrases (subtype first) and core label, as match terms."""
    rows = sorted((row for row in profile.get("queries", []) if row.get("scope") in ("subtype", "family")),
                  key=lambda row: {"subtype": 0, "family": 1}[row["scope"]])
    texts = [row["text"] for row in rows] + [profile.get("core_type")] + list(profile.get("aliases") or [])
    terms, seen = [], set()
    for text in texts:
        if isinstance(text, str) and words(text) and normalise(text) not in seen:
            seen.add(normalise(text))
            terms.append(" ".join(text.split())[:200])
        if len(terms) == limit:
            break
    return terms


def platform_request(route, profile, catalogue_at):
    """The frozen per-type question for one platform and one finished catalogue
    (no query is sent anywhere)."""
    if route not in ROUTES:
        raise SafetyError("DROPSHIP_ROUTE_INVALID")
    timestamp(catalogue_at)
    terms = match_terms(profile, limit=settings()["matching"]["max_terms"])
    if not terms:
        raise SafetyError("PRODUCT_QUERY_PROFILE_REQUIRED")
    return {"catalogue": route, "catalogue_at": catalogue_at, "terms": terms, "match_version": MATCH_VERSION, "country": "IN"}


def _item_words(root, route, catalogue):
    path = platform_folder(root, route) / "catalogue.json"
    try:
        info = path.stat()
        stamp = (str(path), info.st_mtime_ns, info.st_size)
    except FileNotFoundError:
        stamp = None
    cached = _WORD_CACHE.get(route)
    if stamp is None or cached is None or cached[0] != stamp:
        table = {item_id: (words(item["title"]), words(" ".join(item["tags"] + [item.get("product_type") or ""])))
                 for item_id, item in catalogue["items"].items()}
        if stamp is None:
            return table
        _WORD_CACHE[route] = cached = (stamp, table)
    return cached[1]


def local_matches(catalogue, terms, *, limit, min_coverage, item_words=None):
    """Best catalogue items for the terms, strongest first.

    An item qualifies when its title covers at least ``min_coverage`` of a
    term's words (and at least two words, or the term's only word). Tags and
    product type never qualify an item; they only order equally covered ones.
    """
    threshold = Decimal(str(min_coverage))
    wanted = [(term, words(term)) for term in terms]
    item_words = item_words or {item_id: (words(item["title"]), words(" ".join(item["tags"] + [item.get("product_type") or ""])))
                                for item_id, item in catalogue["items"].items()}
    ranked = []
    for item_id, item in catalogue["items"].items():
        title, extra = item_words.get(item_id, (set(), set()))
        best = None
        for term, needed in wanted:
            shared = needed & title
            if not needed or len(shared) < min(2, len(needed)):
                continue
            coverage = Decimal(len(shared)) / Decimal(len(needed))
            if coverage < threshold:
                continue
            order = (coverage, len(shared), len((needed - title) & extra))
            if best is None or order > best[0]:
                best = (order, term, shared, coverage)
        if best is not None:
            on_list = any(role in LIST_ROLES for role in item["roles"])
            ranked.append(((*best[0], on_list, item["last_seen_at"]), item_id, {
                "item": item, "term": best[1], "coverage": str(best[3].quantize(Decimal("0.01"))), "shared": sorted(best[2])}))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in ranked[:limit]]


def _semantic(route, item):
    return {"title": item["title"], "product_url": item["url"], "platform": LABELS[route],
            "product_type": item.get("product_type"), "tags": item["tags"][:8]}


def item_facts(route, item, *, list_type, query=None, match=None):
    """Observation facts for one platform item (supplier/presence context only)."""
    return {"source_route": route, "platform": LABELS[route], "item_id": item["item_id"], "url": item["url"],
            "list_type": list_type, "query": query,
            "price": {"amount": item["price"], "currency": "INR", "basis": item["price_basis"]} if item["price"] else None,
            "mrp": item["mrp"], "in_stock": item["in_stock"], "roles": sorted(item["roles"]),
            "role_dates": {role: {"first_seen_at": dates["first_seen_at"], "last_seen_at": dates["last_seen_at"]}
                           for role, dates in sorted(item["roles"].items())},
            "collections": sorted(item["collections"])[:20], "vendor": item.get("vendor"), "created_at": item.get("created_at"),
            "first_seen_at": item["first_seen_at"], "last_seen_at": item["last_seen_at"], "terms": item.get("terms"),
            "source_ref": item["source_ref"], "images": item["images"][:2], "match": deepcopy(match),
            "limitations": ["A supplier catalogue listing: price, stock and list membership are the platform's own display, "
                            "not sales, demand or a supply promise."]}


def import_platform_capture(memory, route, acquisition_id, rows, *, observed_at, reference, request, mapper):
    """Save one capture of platform items as research events (idempotent)."""
    identifier(acquisition_id)
    timestamp(observed_at)
    mapped = {"status": "usable" if rows else "empty", "rejected": [], "rows": rows}
    capture_id = key("capture", [route, acquisition_id])
    old = memory.snapshot()["captures"].get(capture_id)
    if old is not None:
        if old["mapping_hash"] != digest(mapped):
            raise SafetyError("RESEARCH_CAPTURE_CONFLICT")
        state = memory.snapshot()
        return capture_id, sorted(oid for oid, row in state["observations"].items() if row["capture_id"] == capture_id)
    capture = {"source": route, "acquisition_id": acquisition_id, "observed_at": observed_at,
               "capture_hash": digest([route, acquisition_id, request, rows]), "mapping_hash": digest(mapped),
               "reference": {"route": route, "request": deepcopy(request), "folder": str(reference.parent),
                             "file": reference.name},
               "imported_at": utc_now(), "mapping_status": mapped["status"], "rejected": [], "mapper_version": mapper}
    changes, ids = [change("captures", capture_id, capture)], []
    for row in rows:
        observation_id = key("obs", [route, acquisition_id, row["facts"]["item_id"]])
        ids.append(observation_id)
        changes.append(change("observations", observation_id, {
            "source": route, "acquisition_id": acquisition_id, "source_item": row["facts"]["item_id"],
            "capture_id": capture_id, "observed_at": observed_at, "semantic_id": key("item", row["semantic"]),
            "semantic": row["semantic"], "facts": row["facts"]}))
    memory.commit(capture_id, changes)
    return capture_id, sorted(ids)


def _wait_until(root, route, reason, signed_in_root, as_of=None):
    """When a waiting per-type question is worth looking at again (a local check only).

    Judged at the caller's time, so a readiness hint is the same answer for the
    same saved state and time rather than moving with the wall clock.
    """
    if reason.startswith("OWNER_") or reason.startswith("SIGNED_IN_"):
        from .signed_in_browser import PROJECT_ROOT, signed_in_recheck_at
        return signed_in_recheck_at(PROJECT_ROOT if signed_in_root is None else signed_in_root, reason, as_of=as_of)
    return load_refresh(root, route)["blocked_until"] or (timestamp(as_of or utc_now()) + timedelta(minutes=10)).isoformat()


def _validated_request(route, request):
    if (not isinstance(request, dict) or set(request) != {"catalogue", "catalogue_at", "terms", "match_version", "country"}
            or request["catalogue"] != route or request["match_version"] != MATCH_VERSION
            or not isinstance(request["catalogue_at"], str)
            or not isinstance(request["terms"], list) or not 1 <= len(request["terms"]) <= 12
            or any(not isinstance(term, str) or not words(term) for term in request["terms"])):
        raise SafetyError("DROPSHIP_REQUEST_INVALID")
    return request


def collect_type_matches(memory, route, context, *, signed_in_root=None):
    """Answer one per-type platform question from the saved catalogue."""
    request = _validated_request(route, context["request"])
    aid = identifier(context["acquisition_id"])
    folder = safe_path(memory.root, "dropship_platforms", "matches", aid)
    path = folder / "receipt.json"
    if path.is_file():
        receipt = load_json(path, CAPTURE_LIMIT)
        if receipt.get("route") != route or receipt.get("acquisition_id") != aid or receipt.get("request") != request:
            raise SafetyError("DROPSHIP_MATCH_RECEIPT_CONFLICT")
    else:
        hold = platform_hold(memory.root, route, signed_in_root=signed_in_root, as_of=context.get("as_of"))
        catalogue = load_catalogue(memory.root, route) if hold is None else None
        if hold is None and not (catalogue["full_completed_at"] and catalogue["items"]):
            hold = {"reason": "DROPSHIP_CATALOGUE_PENDING", "next_retry_at": _wait_until(
                memory.root, route, "DROPSHIP_CATALOGUE_PENDING", signed_in_root, context.get("as_of"))}
        if hold is not None:
            raise SignedInWait(hold["reason"], next_retry_at=hold["next_retry_at"])
        config = settings()["matching"]
        matches = local_matches(catalogue, request["terms"], limit=config["max_matches_per_type"],
                                min_coverage=config["min_coverage"], item_words=_item_words(memory.root, route, catalogue))
        rows = [{"semantic": _semantic(route, row["item"]),
                 "facts": item_facts(route, row["item"], list_type="dropship_catalogue_match", query=row["term"],
                                     match={"term": row["term"], "coverage": row["coverage"], "shared_words": row["shared"]})}
                for row in matches]
        receipt = {"version": MATCH_VERSION, "route": route, "acquisition_id": aid, "request": request,
                   "observed_at": catalogue["updated_at"], "full_completed_at": catalogue["full_completed_at"],
                   "lists_completed_at": catalogue["lists_completed_at"], "catalogue_items": len(catalogue["items"]),
                   "state": "usable" if rows else "empty", "rows": rows}
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(path, receipt)
    capture_id, observation_ids = import_platform_capture(memory, route, aid, receipt["rows"], observed_at=receipt["observed_at"],
                                                          reference=path, request=request, mapper=MATCH_VERSION)
    if callable(context.get("record_dispatch")):
        context["record_dispatch"]()
    return {"schema_version": 1, "route": route, "acquisition_id": aid, "observed_at": receipt["observed_at"],
            "state": receipt["state"], "dispatch_started": True, "reference": str(path),
            "capture_ids": [capture_id], "observation_ids": observation_ids, "evidence_refs": [],
            "reason": ("Saved platform catalogue items matching this type's words; relevance review follows."
                       if receipt["rows"] else "No item in the saved platform catalogue matched this type's words.")}


def platform_hold(root, route, *, signed_in_root=None, as_of=None):
    """None when the platform's catalogue can answer a question, else its calm wait."""
    wait = catalogue_wait(root, route)
    return None if wait is None else {"reason": wait, "next_retry_at": _wait_until(root, route, wait, signed_in_root, as_of)}


def create_dropship_handlers(memory, *, signed_in_root=None):
    """Installed per-type handlers for the three platform routes (local work only).

    The question is answered before callback admission
    (``source_access_preflight`` returns the saved local answer), so it never
    uses the round's source allowance or a work-turn slot; while a platform's
    catalogue is unusable it waits there instead, and the programme's readiness
    sees the same wait (``source_access_hint``). ``signed_in_root`` holds the
    signed-in browser's records (the project by default).
    """
    handlers = {}
    for route in ROUTES:
        def collect(context, route=route):
            return collect_type_matches(memory, route, context, signed_in_root=signed_in_root)

        def preflight(context, route=route):
            try:
                receipt = collect_type_matches(memory, route, dict(context, record_dispatch=None), signed_in_root=signed_in_root)
            except SignedInWait as error:
                return {"action": "wait", "calm_wait": True, "reason": str(error), "next_retry_at": error.next_retry_at}
            return {"action": "saved", "acquisition_id": context["acquisition_id"], "receipt": receipt}

        def access_hint(context, route=route):
            hold = platform_hold(memory.root, route, signed_in_root=signed_in_root, as_of=context.get("as_of"))
            return {"ready": hold is None, "local_saved": False, "bypass_public_pause": False,
                    "reason": hold and hold["reason"], "next_retry_at": hold and hold["next_retry_at"], "signal": {}}
        handlers[route] = {"handler": collect, "reconcile": collect, "source_access_preflight": preflight,
                           "source_access_hint": access_hint}
    return handlers

