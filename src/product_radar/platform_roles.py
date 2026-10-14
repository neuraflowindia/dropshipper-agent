"""What the dropshipping platforms contribute, once their items are reviewed.

* Presence fact (for the coming weighted ranking score): per narrow type and
  platform, the relevant listed items (count, links), whether any is on a
  bestseller / trending / high-demand list or a new-arrivals list, the
  platform's dropship/wholesale price and MRP, the stock flag, the dates seen
  and how much of the platform was read (a whole feed or a sample). Stored as
  a versioned ``source_checks`` record; read it with
  ``current_platform_presence`` (saved) or ``platform_presence`` (computed now).
  It is not a market vote: that waits for the ranking redesign (W7).
* Supplier lead: a relevant, still-listed item is recorded as one stable lead
  per type, platform and item (price, MRP, stock, the product's own stated
  terms or "Not stated", the platform's stated terms) and as supplier effort
  (``supplier_effort.record_supplier_effort``), backed by a small saved copy of
  the read that showed the item.
* Discovery: a few in-stock, in-category list items per round (bestseller /
  high-demand / winning or on two state lists first, then trending, then new
  arrivals; newest first; platforms in turn) become one capture per platform
  and go through the existing candidate admission (``research_flow.run_flow``).
  Items already captured, items that already match an accepted type, near
  duplicates and items outside the four client categories never reach an AI
  call; nominations stop while captures are still being admitted, and a daily
  cap bounds them.

``platform_step`` is the programme's own platform lane: once every round,
whatever the discovery lane does, ``advance_dropship_platforms`` runs the
platforms' paced refresh within a time budget, then presence and leads (only
for types whose inputs changed), then discovery admission.
"""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import re
import time

from .dropship_platforms import (FEED_ROUTES, LABELS, LIST_ROLES, ROUTES, SIGNED_IN_ROUTES, advance_platform, amount,
                                 item_receipt, load_catalogue, platform_lines, platform_settings, record_catalogue_states,
                                 settings)
from .models import SafetyError, digest, timestamp, utc_now
from .platform_matching import DISCOVERY_MAPPER, _semantic, _stem, import_platform_capture, item_facts, words
from .research_memory import key
from .storage import atomic_json, load_json, safe_path

PRESENCE_KIND = "dropship-platform-presence"
PRESENCE_VERSION = "dropship-presence-v2"
LEAD_KIND = "dropship-platform-lead-v2"
PRESENCE_RENEW_DAYS = 7
MAX_ITEMS = 20
NOT_STATED = "Not stated"
_STATES = ("relevant", "relevant_delisted", "no_relevant_items", "no_relevant_items_in_sample", "review_pending",
           "not_checked")
# Discovery order: a strong list first, then trending, then new arrivals.
_TIERS = (("bestseller", "high_demand", "winning"), ("trending",), ("new_arrival",))


def _reviewed_items(state, concept_id, *, as_of):
    """Latest accepted review per platform item: {route: {item_id: row}}."""
    from .market_validation import FUNCTIONAL_MATCH_METHOD, _functional_match
    from .research_scope import validated_scoped_evidence
    result = {}
    for row in validated_scoped_evidence(state, concept_id, as_of=as_of):
        if row["route"] not in ROUTES or row["method"] not in (FUNCTIONAL_MATCH_METHOD, "operator_source_review"):
            continue
        relevant = (row["scope"] == "narrow_type" and row["admissibility"] == "accepted"
                    and row["relationship"] in ("exact_product", "relevant_family"))
        for ref in row["references"]:
            observation = state["observations"].get(ref["id"]) if ref["table"] == "observations" else None
            if observation is None or observation["source"] != row["route"] or not _functional_match(row, ref["id"]):
                continue
            item_id, order = observation["facts"]["item_id"], (timestamp(row["reviewed_at"]), row["id"])
            current = result.setdefault(row["route"], {}).get(item_id)
            if current is None or order > current["order"]:
                result[row["route"]][item_id] = {"order": order, "relevant": relevant, "observation_id": ref["id"],
                                                 "relationship": row["relationship"], "review_id": row["id"],
                                                 "reviewed_at": row["reviewed_at"], "facts": observation["facts"],
                                                 "title": observation["semantic"]["title"]}
    return result


def _checked(state, concept_id, *, as_of):
    """Per route: latest completed per-type check date and its candidate item ids."""
    from .source_coverage import TASK_KIND
    checked = {}
    for task in state["query_tasks"].values():
        if task.get("kind") != TASK_KIND or task.get("route") not in ROUTES or concept_id not in task.get("targets", {}):
            continue
        for receipt_id in task.get("receipt_refs", []):
            envelope = state["source_checks"].get(receipt_id, {}).get("envelope", {})
            if envelope.get("dispatch_started") is not True or timestamp(envelope["observed_at"]) > timestamp(as_of):
                continue
            row = checked.setdefault(task["route"], {"at": envelope["observed_at"], "items": set()})
            row["at"] = max(row["at"], envelope["observed_at"], key=timestamp)
            row["items"].update(state["observations"][oid]["facts"]["item_id"] for oid in envelope.get("observation_ids", [])
                                if oid in state["observations"])
    return checked


def _item_view(item_id, review, catalogue):
    item = catalogue["items"].get(item_id)
    facts = review["facts"]
    source = item or {"url": facts["url"], "price": (facts["price"] or {}).get("amount"), "mrp": facts["mrp"],
                      "price_basis": (facts["price"] or {}).get("basis"), "in_stock": facts["in_stock"],
                      "roles": facts["role_dates"], "first_seen_at": facts["first_seen_at"],
                      "last_seen_at": facts["last_seen_at"], "terms": facts.get("terms"), "title": review["title"]}
    roles = {role: {"first_seen_at": dates["first_seen_at"], "last_seen_at": dates["last_seen_at"]}
             for role, dates in sorted(source["roles"].items())}
    return {"item_id": item_id, "title": source["title"], "url": source["url"], "listed": item is not None,
            "observation_id": review["observation_id"], "relationship": review["relationship"],
            "review_id": review["review_id"], "roles": roles,
            "on_list": any(role in LIST_ROLES for role in roles), "new_arrival": "new_arrival" in roles,
            "price": source["price"], "mrp": source["mrp"], "price_basis": source.get("price_basis"),
            "in_stock": source["in_stock"], "terms": source.get("terms") or NOT_STATED,
            "first_seen_at": source["first_seen_at"], "last_seen_at": source["last_seen_at"]}


def platform_presence(state, concept_id, *, root, as_of, catalogues=None):
    """The platform presence fact for one narrow type, computed from saved records."""
    from .functional_types import effective_functional_definition
    meaning = effective_functional_definition(state, concept_id)
    reviewed, checked = _reviewed_items(state, concept_id, as_of=as_of), _checked(state, concept_id, as_of=as_of)
    catalogues = catalogues if catalogues is not None else {route: load_catalogue(root, route) for route in ROUTES}
    platforms = {}
    for route in ROUTES:
        config, catalogue = platform_settings(route), catalogues[route]
        rows = reviewed.get(route, {})
        items = sorted((_item_view(item_id, review, catalogue) for item_id, review in rows.items()
                        if review["relevant"]), key=lambda row: (not row["listed"], not row["on_list"], row["item_id"]))
        listed = [row for row in items if row["listed"]]
        pending = len(checked.get(route, {}).get("items", set()) - set(rows))
        state_name = ("relevant" if listed else "relevant_delisted" if items else "review_pending" if pending else
                      ("no_relevant_items" if catalogue["scope"] == "full" else "no_relevant_items_in_sample")
                      if route in checked else "not_checked")
        prices = [Decimal(row["price"]) for row in listed if row["price"]]
        mrps = [Decimal(row["mrp"]) for row in listed if row["mrp"]]
        platforms[route] = {
            "label": LABELS[route], "state": state_name, "checked_at": checked.get(route, {}).get("at"),
            "catalogue_scope": catalogue["scope"], "catalogue_items": len(catalogue["items"]),
            "catalogue_at": catalogue["full_completed_at"],
            "relevant_count": len(items), "listed_count": len(listed), "pending_review": pending,
            "links": [row["url"] for row in listed][:MAX_ITEMS], "items": items[:MAX_ITEMS],
            "on_bestseller_or_trending": any(row["on_list"] for row in listed),
            "list_roles": sorted({role for row in listed for role in row["roles"] if role in LIST_ROLES}),
            "on_new_arrivals": any(row["new_arrival"] for row in listed),
            "lowest_price": amount(str(min(prices))) if prices else None,
            "highest_mrp": amount(str(max(mrps))) if mrps else None,
            "price_basis": config["price_basis"], "stated_terms": config["stated_terms"] or NOT_STATED,
            "in_stock_items": sum(row["in_stock"] is True for row in listed),
            "first_seen_at": min((row["first_seen_at"] for row in items), key=timestamp, default=None),
            "last_seen_at": max((row["last_seen_at"] for row in items), key=timestamp, default=None)}
    carrying = [route for route, row in platforms.items() if row["listed_count"]]
    return {"kind": PRESENCE_KIND, "version": PRESENCE_VERSION, "concept_id": concept_id,
            "meaning_hash": meaning["meaning_hash"], "as_of": as_of, "platform_count": len(carrying),
            "platforms_carrying": carrying,
            "on_list_platforms": [route for route in carrying if platforms[route]["on_bestseller_or_trending"]],
            "new_arrival_platforms": [route for route in carrying if platforms[route]["on_new_arrivals"]],
            "platforms": platforms}


def _change_hash(value):
    """What makes a new presence record: everything except the moving 'last seen' dates."""
    def strip(node):
        if isinstance(node, dict):
            return {name: strip(item) for name, item in node.items() if name not in ("last_seen_at", "as_of", "at")}
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node
    return digest(strip(value))


def _valid_presence(record):
    platforms = record.get("platforms") if isinstance(record, dict) else None
    if (not isinstance(platforms, dict) or record.get("kind") != PRESENCE_KIND or record.get("version") != PRESENCE_VERSION
            or set(platforms) != set(ROUTES) or any(row.get("state") not in _STATES for row in platforms.values())
            or record.get("change_hash") != _change_hash({name: item for name, item in record.items()
                                                          if name not in ("change_hash", "at")})):
        raise SafetyError("DROPSHIP_PRESENCE_RECORD_INVALID")
    return record


def current_platform_presence(state, concept_id):
    """Read API: the latest saved presence fact for the type's current meaning, or None.

    Returns ``{kind, version, concept_id, meaning_hash, as_of, at, change_hash,
    platform_count, platforms_carrying, on_list_platforms, new_arrival_platforms,
    platforms: {route: {label, state, checked_at, catalogue_scope,
    catalogue_items, catalogue_at, relevant_count, listed_count, pending_review,
    links, items, on_bestseller_or_trending, list_roles, on_new_arrivals,
    lowest_price, highest_mrp, price_basis, stated_terms, in_stock_items,
    first_seen_at, last_seen_at}}}``; each item has ``item_id, title, url,
    listed, observation_id, relationship, review_id, roles {role:
    {first_seen_at, last_seen_at}}, on_list, new_arrival, price, mrp,
    price_basis, in_stock, terms, first_seen_at, last_seen_at``. A platform's
    ``state`` is relevant / relevant_delisted / review_pending /
    no_relevant_items (whole feed) / no_relevant_items_in_sample (a sampled
    signed-in catalogue) / not_checked.
    """
    from .functional_types import effective_functional_definition
    meaning = effective_functional_definition(state, concept_id)["meaning_hash"]
    rows = [row for row in state["source_checks"].values()
            if row.get("kind") == PRESENCE_KIND and row.get("version") == PRESENCE_VERSION
            and row.get("concept_id") == concept_id and row.get("meaning_hash") == meaning]
    if not rows:
        return None
    return deepcopy(_valid_presence(max(rows, key=lambda row: (timestamp(row["at"]), row["change_hash"]))))


def record_platform_presence(memory, concept_id, *, root, as_of, catalogues=None):
    """Save the presence fact when it changed (or the saved one is a week old)."""
    state = memory.snapshot()
    value = platform_presence(state, concept_id, root=root, as_of=as_of, catalogues=catalogues)
    value["change_hash"] = _change_hash(value)
    saved = current_platform_presence(state, concept_id)
    if (saved is not None and saved["change_hash"] == value["change_hash"]
            and timestamp(as_of) - timestamp(saved["at"]) < timedelta(days=PRESENCE_RENEW_DAYS)):
        return saved
    record = dict(value, at=as_of)
    record_id = key("dropship-presence", [concept_id, value["meaning_hash"], value["change_hash"], as_of])
    memory.put("source_checks", record_id, record, event_id=key("dropship-presence-record", record_id))
    return record


def lead_id(concept_id, route, item_id):
    """One stable lead per type, platform and item."""
    return key("dropship-lead", [concept_id, route, item_id])


def record_platform_leads(memory, concept_id, presence, *, root, catalogues):
    """Each still-listed relevant item: a supplier lead and the platform's supplier effort.

    A lead changes only when its price, MRP, stock flag or stated terms change;
    only then is supplier effort recorded again, citing a small saved copy of
    the read that showed the item (kept with the lead, so later pruning of the
    platform's raw reads never breaks the proof).
    """
    from .supplier_effort import record_supplier_effort
    state = memory.snapshot()
    description = state["concepts"][concept_id]["description"]
    recorded = []
    for route, row in presence["platforms"].items():
        catalogue = catalogues[route]
        listed = [item for item in row["items"] if item["listed"] and item["item_id"] in catalogue["items"]][:10]
        changed, receipts, leads, reference = [], {}, [], None
        for item in listed:
            saved = catalogue["items"][item["item_id"]]
            identity = lead_id(concept_id, route, item["item_id"])
            lead = {"kind": LEAD_KIND, "source": "dropship-platforms", "route": route, "platform": LABELS[route],
                    "concept_id": concept_id, "meaning_hash": presence["meaning_hash"], "description_hash": digest(description),
                    "item_id": item["item_id"], "title": saved["title"], "url": saved["url"],
                    "price": {"amount": saved["price"], "currency": "INR", "basis": saved["price_basis"]} if saved["price"] else None,
                    "mrp": saved["mrp"], "in_stock": saved["in_stock"], "item_terms": saved.get("terms") or NOT_STATED,
                    "stated_terms": row["stated_terms"], "terms_url": platform_settings(route)["terms_url"],
                    "observation_id": item["observation_id"], "review_id": item["review_id"],
                    "first_seen_at": saved["first_seen_at"]}
            old = state["source_checks"].get(identity)
            leads.append(identity)
            if old is not None and {name: value for name, value in old.items() if name not in ("evidence", "seen_at")} == lead:
                continue
            receipt = item_receipt(root, route, saved)
            folder = safe_path(root, "dropship_platforms", "leads", identity)
            folder.mkdir(parents=True, exist_ok=True)
            evidence = folder / "receipt.json"
            atomic_json(evidence, {"version": "dropship-lead-evidence-v1", "route": route, "receipt": receipt,
                                   "item": {name: saved[name] for name in ("item_id", "title", "url", "price", "mrp", "in_stock",
                                                                           "terms", "first_seen_at", "last_seen_at")}})
            receipts[receipt["id"]], reference = receipt, str(evidence)
            memory.put("source_checks", identity, dict(lead, evidence=str(evidence), seen_at=saved["last_seen_at"]),
                       previous=old, event_id=key("dropship-lead-record", [identity, digest(lead)]))
            changed.append(identity)
        if not changed:
            continue
        effort = record_supplier_effort(memory, concept_id=concept_id, description=description,
                                        pass_id=key("dropship-pass", [route, sorted(changed), sorted(receipts)]),
                                        receipts=list(receipts.values())[:10], receipt_reference=reference,
                                        outcome="usable_leads", accepted_lead_refs=leads, route=route)
        recorded.append({"route": route, "lead_ids": changed, "effort_id": effort["check_id"]})
    return recorded


def platform_leads(state, concept_id):
    """Saved platform leads for a type (one per platform item)."""
    return [dict(row, lead_id=identity) for identity, row in sorted(state["source_checks"].items())
            if row.get("kind") == LEAD_KIND and row.get("concept_id") == concept_id]


def _phrase_words(text):
    return [_stem(word) for word in re.findall(r"[a-z0-9]+", text.casefold())]


def item_category(item, config):
    """The client category an item fits (from its product type, tags and title), or
    None when it fits none or is in an excluded kind (jewellery, cosmetics, ...)."""
    text = " ".join([item.get("product_type") or "", " ".join(item.get("tags") or []), item["title"]])
    found = _phrase_words(text)
    joined = " " + " ".join(found) + " "

    def has(phrase):
        return " " + " ".join(_phrase_words(phrase)) + " " in joined
    if any(has(phrase) for phrase in config["exclude"]):
        return None
    return next((category for category, phrases in config["categories"].items() if any(has(phrase) for phrase in phrases)), None)


def _tier(item):
    roles = set(item["roles"])
    if roles & set(_TIERS[0]) or len(item["roles"].get("state_bestseller", {}).get("via", [])) >= 2:
        return 0
    return 1 if "trending" in roles else 2 if "new_arrival" in roles else None


def _discovery_candidates(state, catalogues, config):
    """Admission-ready list items, in nomination order; nothing here calls a model."""
    from .source_coverage import TASK_KIND
    captured, titles = set(), set()
    for row in state["observations"].values():
        if row["source"] in ROUTES:
            captured.add((row["source"], row["facts"].get("item_id")))
            titles.add(frozenset(words(row["semantic"].get("title") or "")))
    # Every accepted type's platform words: an item whose title fully covers them is presence, not a new candidate.
    known = {frozenset(words(term)) for task in state["query_tasks"].values()
             if task.get("kind") == TASK_KIND and task.get("route") in ROUTES for term in task["request"].get("terms", [])}
    known.discard(frozenset())
    queues = []
    for route in ROUTES:
        ranked = []
        for item in catalogues[route]["items"].values():
            tier = _tier(item)
            title = frozenset(words(item["title"]))
            if (tier is None or item["in_stock"] is not True or (route, item["item_id"]) in captured or not title
                    or title in titles or any(term <= title for term in known) or item_category(item, config) is None):
                continue
            held = [role for role in _TIERS[tier] + (("state_bestseller",) if tier == 0 else ()) if role in item["roles"]]
            newest = max(timestamp(item["roles"][role]["first_seen_at"]) for role in held)
            ranked.append((tier, -newest.timestamp(), item["item_id"], route, item, title))
        queues.append(sorted(ranked, key=lambda row: row[:3]))
    # Strongest tier first; within a tier newest first, platforms in turn (the
    # starting platform rotates with each discovery capture made).
    start = sum(row.get("mapper_version") == DISCOVERY_MAPPER for row in state["captures"].values()) % len(ROUTES)
    order, seen = [], set()
    for tier in range(len(_TIERS)):
        lanes = [[row for row in queue if row[0] == tier] for queue in queues[start:] + queues[:start]]
        while any(lanes):
            for lane in lanes:
                if lane:
                    row = lane.pop(0)
                    if row[5] not in seen:  # a near duplicate (same title words) on another platform
                        seen.add(row[5])
                        order.append((row[3], row[4]))
    return order


def _settled(state, acquisition_id, imported_at, as_of, settle_days):
    """Admission of a discovery capture is over: every item described and its
    children decided, or recorded as failed/deferred, or the capture is older
    than ``settle_days`` (it never holds new nominations for ever)."""
    if timestamp(as_of) - timestamp(imported_at) >= timedelta(days=settle_days):
        return True
    problems = {row.get("affected_id") for row in state["problems"].values()}
    for row in state["observations"].values():
        if row["acquisition_id"] != acquisition_id:
            continue
        semantic = row["semantic_id"]
        description = state["descriptions"].get(semantic)
        if description is None:
            if semantic not in problems:
                return False
            continue
        for index in range(len(description["result"]["products"])):
            child = key("child", [semantic, index, digest(description["result"])])
            status = state["associations"].get(child, {}).get("status")
            if status not in ("resolved", "excluded", "deferred") and child not in problems:
                return False
    return True


def advance_platform_discovery(memory, *, root, runner, as_of, flow_limit=10, catalogues=None):
    """Nominate a few new list items and continue their admission; returns new concept ids."""
    from .catalogue_discovery import _acquisition_progress
    from .fit_policy import FIT_POLICY_ID
    from .research_flow import run_flow
    config = settings()["discovery"]
    catalogues = catalogues or {route: load_catalogue(root, route) for route in ROUTES}
    state = memory.snapshot()
    discovery = sorted((row for row in state["captures"].values() if row.get("mapper_version") == DISCOVERY_MAPPER),
                       key=lambda row: row["imported_at"])
    pending = [row for row in discovery if not _settled(state, row["acquisition_id"], row["imported_at"], as_of,
                                                         config["settle_after_days"])]
    today = sum(1 for row in state["observations"].values() if row["source"] in ROUTES
                and row["facts"].get("list_type") == "dropship_discovery_list"
                and timestamp(as_of) - timestamp(state["captures"][row["capture_id"]]["imported_at"]) < timedelta(hours=24))
    room = 0 if len(pending) >= config["max_pending_captures"] else min(config["items_per_round"], config["daily_cap"] - today)
    picked = _discovery_candidates(state, catalogues, config)[:max(0, room)]
    by_route = {}
    for route, item in picked:
        by_route.setdefault(route, []).append(item)
    for route, items in by_route.items():
        aid = key("dropship-discovery", [route, as_of, sorted(item["item_id"] for item in items)])
        folder = safe_path(root, "dropship_platforms", "discovery", aid)
        rows = [{"semantic": _semantic(route, item), "facts": item_facts(route, item, list_type="dropship_discovery_list")}
                for item in items]
        request = {"discovery": route, "roles": sorted({role for item in items for role in item["roles"]}), "items": len(rows)}
        folder.mkdir(parents=True, exist_ok=True)
        observed = max((item["last_seen_at"] for item in items), key=timestamp)
        atomic_json(folder / "receipt.json", {"version": DISCOVERY_MAPPER, "route": route, "acquisition_id": aid,
                                              "observed_at": observed, "baseline_concepts": sorted(state["concepts"]),
                                              "rows": rows})
        import_platform_capture(memory, route, aid, rows, observed_at=observed, reference=folder / "receipt.json",
                                request=request, mapper=DISCOVERY_MAPPER)
    state = memory.snapshot()
    pending = [row for row in sorted((row for row in state["captures"].values() if row.get("mapper_version") == DISCOVERY_MAPPER),
                                     key=lambda row: row["imported_at"])
               if not _settled(state, row["acquisition_id"], row["imported_at"], as_of, config["settle_after_days"])][:10]
    result = {"nominated": len(picked), "pending_acquisitions": len(pending), "new_concept_ids": []}
    if not pending:
        return result
    run_flow(memory, runner, acquisition_ids=[row["acquisition_id"] for row in pending], limit=flow_limit, as_of=as_of,
             fit_policy_id=FIT_POLICY_ID)
    state = memory.snapshot()
    new = set()
    for row in pending:  # new = admitted types that did not exist when the items were nominated
        baseline = load_json(Path(row["reference"]["folder"]) / row["reference"]["file"], 8 * 1024 * 1024)["baseline_concepts"]
        new.update(_acquisition_progress(state, [row["acquisition_id"]], baseline)["new_concept_ids"])
    result["new_concept_ids"] = sorted(new)
    return result


def _platform_concepts(state):
    from .research_records import SCOPED_EVIDENCE_KIND
    return sorted({row["concept_id"] for row in state["source_checks"].values()
                   if row.get("kind") == SCOPED_EVIDENCE_KIND and row.get("route") in ROUTES
                   and row.get("concept_id") in state["concepts"]})


PRESENCE_INPUTS = "presence-inputs.json"


def _presence_input_index(state):
    """Per type: the platform reviews and per-type checks its presence is worked out from."""
    from .research_records import SCOPED_EVIDENCE_KIND
    from .source_coverage import TASK_KIND
    index = {}
    for record_id, row in state["source_checks"].items():
        if row.get("kind") == SCOPED_EVIDENCE_KIND and row.get("route") in ROUTES:
            index.setdefault(row.get("concept_id"), []).append(["review", record_id, digest(row)])
    for task_id, task in state["query_tasks"].items():
        if task.get("kind") == TASK_KIND and task.get("route") in ROUTES:
            for concept_id in task.get("targets", {}):
                index.setdefault(concept_id, []).append(["check", task_id, task.get("receipt_refs", [])])
    return index


def catalogue_inputs(catalogues):
    """Each platform's finished catalogue cycles and settings (what presence reads of it)."""
    return {route: [catalogue["full_completed_at"], catalogue["lists_completed_at"], catalogue["scope"],
                    catalogue["parser_version"], digest(platform_settings(route))]
            for route, catalogue in sorted(catalogues.items())}


def presence_input_hash(state, concept_id, meaning, platforms, index):
    """Everything a type's presence and leads are worked out from: its meaning
    and description, its platform reviews and checks, the platforms'
    ``catalogue_inputs``, and the presence and lead versions."""
    return digest([PRESENCE_VERSION, LEAD_KIND, meaning["meaning_hash"], digest(state["concepts"][concept_id]["description"]),
                   sorted(index.get(concept_id, [])), platforms])


def _presence_inputs_path(root):
    return safe_path(root, "dropship_platforms") / PRESENCE_INPUTS


def _load_presence_inputs(root):
    """The input hash each type's presence was last worked out from (a local
    speed aid only: a missing or unreadable file means work everything out)."""
    path = _presence_inputs_path(root)
    try:
        value = load_json(path, 4 * 1024 * 1024) if path.exists() else {}
    except (SafetyError, OSError):
        return {}
    if (not isinstance(value, dict) or value.get("version") != PRESENCE_VERSION or not isinstance(value.get("types"), dict)
            or any(not isinstance(row, str) for row in value["types"].values())):
        return {}
    return dict(value["types"])


def _save_presence_inputs(root, inputs):
    atomic_json(_presence_inputs_path(root), {"version": PRESENCE_VERSION, "types": dict(sorted(inputs.items()))})


def _readers(pacing_root, browser):
    from .platform_feeds import FeedReader
    from .platform_signed_in import SignedInReader
    return {route: (SignedInReader(route, pacing_root=pacing_root, browser=browser) if route in SIGNED_IN_ROUTES
                    else FeedReader(route, pacing_root=pacing_root)) for route in ROUTES}


def _failure(error):
    return str(error) if isinstance(error, SafetyError) else "DROPSHIP_LOCAL_IO_FAILED"


def _before_read(memory):
    """Refuse a read while the owner has paused research or the work turn is closed."""
    from .programme import programme_service_state
    from .work_turns import check_turn_launch

    def check():
        if not programme_service_state(memory)["enabled"]:
            raise SafetyError("PROGRAMME_OWNER_PAUSED")
        check_turn_launch(memory, "source")
    return check


def advance_dropship_platforms(memory, context, *, pacing_root=None, readers=None, browser=None, monotonic=time.monotonic):
    """One bounded platform turn: paced refresh within the round's time budget,
    presence and leads, then discovery admission."""
    from .catalogue_discovery import _raise_shared_integrity
    from .functional_types import effective_functional_definition
    from .signed_in_browser import PROJECT_ROOT
    at = context.get("at") or utc_now()
    timestamp(at)
    root = memory.root
    readers = readers or _readers(PROJECT_ROOT if pacing_root is None else pacing_root, browser)
    deadline = monotonic() + settings()["refresh"]["seconds_per_round"]
    summary = {"refresh": {}, "presence": {"checked": 0, "unchanged": 0, "failures": []}, "leads": 0, "discovery": None}
    # Signed-in pages first: one only starts while the round's platform time
    # still covers its worst case; the quick feed reads use what is left.
    for route in SIGNED_IN_ROUTES + FEED_ROUTES:
        try:
            summary["refresh"][route] = advance_platform(root, route, readers[route], deadline=deadline,
                                                         before_read=_before_read(memory), monotonic=monotonic)
        except (SafetyError, OSError) as error:
            _raise_shared_integrity(error)
            summary["refresh"][route] = {"state": "waiting", "reason": _failure(error)}
    try:
        summary["catalogues_noted"] = record_catalogue_states(memory, root)
    except (SafetyError, OSError) as error:
        _raise_shared_integrity(error)
        summary["catalogues_noted"] = {"state": "waiting", "reason": _failure(error)}
    state = memory.snapshot()
    catalogues = {route: load_catalogue(root, route) for route in ROUTES}
    index, inputs, platforms = _presence_input_index(state), _load_presence_inputs(root), catalogue_inputs(catalogues)
    for concept_id in _platform_concepts(state):
        try:
            meaning = effective_functional_definition(state, concept_id)
            if not meaning["accepted"]:
                continue
            input_hash = presence_input_hash(state, concept_id, meaning, platforms, index)
            saved = current_platform_presence(state, concept_id)
            if (inputs.get(concept_id) == input_hash and saved is not None
                    and timestamp(at) - timestamp(saved["at"]) < timedelta(days=PRESENCE_RENEW_DAYS)):
                summary["presence"]["unchanged"] += 1  # nothing it is worked out from has changed
                continue
            presence = record_platform_presence(memory, concept_id, root=root, as_of=at, catalogues=catalogues)
            summary["presence"]["checked"] += 1
            summary["leads"] += sum(len(row["lead_ids"]) for row in record_platform_leads(
                memory, concept_id, presence, root=root, catalogues=catalogues))
            inputs[concept_id] = input_hash
        except (SafetyError, OSError) as error:
            _raise_shared_integrity(error)
            summary["presence"]["failures"].append({"concept_id": concept_id, "reason": _failure(error)})
    try:
        _save_presence_inputs(root, inputs)
    except OSError as error:
        summary["presence"]["failures"].append({"concept_id": None, "reason": _failure(error)})
    try:
        summary["discovery"] = advance_platform_discovery(memory, root=root, runner=context.get("runner"), as_of=at,
                                                          flow_limit=min(50, context.get("fit_limit", 10)), catalogues=catalogues)
    except (SafetyError, OSError) as error:
        _raise_shared_integrity(error)
        summary["discovery"] = {"state": "waiting", "reason": _failure(error), "new_concept_ids": []}
    summary["owner_lines"] = platform_lines(root)
    return summary


def platform_step(memory, programme_record, context):
    """The programme's platform lane (``programme`` ``platform_step``): one bounded turn."""
    return advance_dropship_platforms(memory, context)
