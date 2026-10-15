"""Amazon India and Flipkart crowding level per narrow type (Plan R10 W6, D4).

Amazon and Flipkart are no longer discovery sources; they answer "how crowded
is this product already?". Code decides the level from AI-reviewed relevant
listings only (the existing type-scoped relevance reviews; no extra model
call), joined to parser-v3 card facts read from the saved first pages:

* High: a relevant item on an Amazon bestseller list, or enough relevant
  listings with many ratings, or many relevant listings that show a brand from
  many different brands/sellers (rule ``many_brands_shown``: only cards that
  show a brand count; a card that shows none is "brand not shown", never a
  brand of its own), or enough relevant listings showing "1K+ bought in past
  month" (thresholds in config/research/crowding.json).
* Low: at most two relevant listings across both sites, none with 200+ ratings.
* Medium: everything else.
* Not checked: the type's Amazon and Flipkart reads (or their relevance
  reviews) are not finished, or a saved page cannot be read. Never Low. The
  check is free, so it is simply worked out again on the next round.

High can be known from part of the reads (more reads only add listings);
Low and Medium need both sites read and reviewed. One versioned fact is saved
per type; ``crowding_for`` is the single read API for eligibility, the coming
ranking score and the reports. Selection order is deliberately not changed.
"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from .marketplace_cards import CARD_PARSER_VERSION, merge_card_facts, read_capture_cards
from .models import SafetyError, digest, identifier, timestamp, utc_now
from .research_memory import change, key
from .storage import load_json

VERSION = "crowding-v2"  # v2: the brand rule counts only listings that show a brand
FACT_KIND = "crowding-fact-v1"
CALIBRATION_KIND = "crowding-calibration-v1"
SETTINGS = Path(__file__).resolve().parents[2] / "config/research/crowding.json"
LEVELS = ("low", "medium", "high")
NOT_CHECKED = "not_checked"
SEARCH_ROUTES = ("amazon-search", "flipkart-search")
SITES = {"amazon-search": "amazon", "amazon-bestsellers": "amazon", "flipkart-search": "flipkart"}
REFRESH_LIMIT = 20
_FINAL = frozenset(("acquired", "completed_no_results", "insufficient", "finished"))
_RELEVANT = ("exact_product", "relevant_family")
_HIGH_FIELDS = {"bestseller_hits", "rated_listings", "rated_min_ratings", "listings", "brands", "bought_listings",
                "bought_min_per_month"}


def _share(value):
    try:
        number = Decimal(value) if isinstance(value, str) else None
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite() or not 0 <= number <= 1:
        raise SafetyError("CROWDING_SETTINGS_INVALID")
    return number


def validate_thresholds(value):
    """Numbers only; the agent may change the rating thresholds once (calibration)."""
    try:
        high, low, calibration = value["high"], value["low"], value["calibration"]
        numbers = [high[field] for field in sorted(_HIGH_FIELDS)] + [low["max_listings"], low["ratings_below"],
                                                                     calibration["after_checked_types"]]
        if (set(value) != {"schema_version", "kind", "version", "high", "low", "calibration"}
                or value["schema_version"] != 1 or value["kind"] != "crowding-thresholds"
                or not isinstance(value["version"], str) or set(high) != _HIGH_FIELDS
                or set(low) != {"max_listings", "ratings_below"}
                or set(calibration) != {"after_checked_types", "high_share_above", "high_share_below"}
                or any(type(number) is not int or not 1 <= number <= 10 ** 9 for number in numbers)):
            raise SafetyError("CROWDING_SETTINGS_INVALID")
        identifier(value["version"])
        if _share(calibration["high_share_below"]) >= _share(calibration["high_share_above"]):
            raise SafetyError("CROWDING_SETTINGS_INVALID")
    except (KeyError, TypeError):
        raise SafetyError("CROWDING_SETTINGS_INVALID") from None
    return value


def load_thresholds(path=SETTINGS):
    return validate_thresholds(load_json(path, 16384))


def crowding_level(listings, thresholds):
    """Pure rule over relevant listings: (level or None, rule, counts).

    Each listing is {"site", "bestseller", "card"}; card is the parser-v2 card
    facts, or None when its saved page could not be read. None means High is
    not shown and the rest needs complete reads to decide. Amazon's "Best
    seller" badge on a search card counts as a bestseller hit.
    """
    high, low = thresholds["high"], thresholds["low"]
    cards = [row["card"] for row in listings if row["card"] is not None]
    ratings = [card["ratings_count"] for card in cards if card.get("ratings_count") is not None]
    branded = [card for card in cards if card.get("brand")]
    brands = {" ".join(card["brand"].casefold().split()) for card in branded}
    counts = {"relevant_listings": {site: sum(row["site"] == site for row in listings) for site in ("amazon", "flipkart")},
              # Only cards that show a brand count; most pages show none ("brand not shown").
              "brands_or_sellers": len(brands), "listings_with_brand": len(branded),
              "brand_not_shown": len(cards) - len(branded), "max_ratings": max(ratings) if ratings else None,
              "listings_rated_high": sum(value >= high["rated_min_ratings"] for value in ratings),
              "listings_rated_200_plus": sum(value >= low["ratings_below"] for value in ratings),
              "bought_past_month_hits": sum((card.get("bought_past_month") or 0) >= high["bought_min_per_month"]
                                            for card in cards),
              "bestseller_hits": sum(row["bestseller"] or bool(row["card"] and row["card"].get("bestseller_badge"))
                                     for row in listings),
              "bestseller_badges": sum(bool(row["card"] and row["card"].get("bestseller_badge")) for row in listings),
              "sponsored_listings": sum(card.get("sponsored") is True for card in cards),
              "ratings_not_shown": sum(card.get("ratings_count") is None for card in cards),
              "card_facts_missing": len(listings) - len(cards),
              "variants_collapsed": sum(row.get("variants", 1) - 1 for row in listings)}
    return (*threshold_ladder(counts, thresholds), counts)


_LADDER_COUNTS = ("bestseller_hits", "listings_rated_high", "listings_with_brand", "brands_or_sellers",
                  "bought_past_month_hits", "card_facts_missing", "listings_rated_200_plus", "ratings_not_shown")


def threshold_ladder(counts, thresholds):
    """(level or None, rule) the thresholds give for a fact's counts; pure, so a
    saved or frozen fact can be re-checked from its own counts."""
    high, low = thresholds["high"], thresholds["low"]
    if (not isinstance(counts, dict) or any(type(counts.get(name)) is not int or counts[name] < 0 for name in _LADDER_COUNTS)
            or not isinstance(counts.get("relevant_listings"), dict) or set(counts["relevant_listings"]) != {"amazon", "flipkart"}
            or any(type(value) is not int or value < 0 for value in counts["relevant_listings"].values())):
        raise SafetyError("CROWDING_FACT_INVALID")
    total = sum(counts["relevant_listings"].values())
    for rule, met in (("bestseller_list", counts["bestseller_hits"] >= high["bestseller_hits"]),
                      ("well_rated_listings", counts["listings_rated_high"] >= high["rated_listings"]),
                      ("many_brands_shown", counts["listings_with_brand"] >= high["listings"]
                       and counts["brands_or_sellers"] >= high["brands"]),
                      ("bought_past_month", counts["bought_past_month_hits"] >= high["bought_listings"])):
        if met:
            return "high", rule
    if counts["card_facts_missing"]:
        return None, "saved_page_unreadable"
    # An unknown rating count is not a zero: such a listing never supports Low.
    if total <= low["max_listings"] and counts["listings_rated_200_plus"] == 0 and counts["ratings_not_shown"] == 0:
        return "low", "few_listings"
    return "medium", "default"


def _relevant_rows(state, concept_id, *, as_of):
    """Latest accepted narrow-type relevance per current Amazon/Flipkart row."""
    from .discovery import latest_observations
    from .market_validation import _functional_match
    from .read_memo import remember
    from .research_scope import validated_scoped_evidence
    from .source_interpretation import FUNCTIONAL_MATCH_METHOD
    # Once per refresh or stage plan (one read scope), not once per type.
    current = remember("crowding_latest_observations", state, (as_of,),
                       lambda: latest_observations(state["observations"], as_of, max_age_days=None),
                       tables=("observations",), copy=False)
    latest = {}
    for row in validated_scoped_evidence(state, concept_id, as_of=as_of):
        if row["method"] not in (FUNCTIONAL_MATCH_METHOD, "operator_source_review"):
            continue
        for ref in row["references"]:
            observation = current.get(ref["id"]) if ref["table"] == "observations" else None
            if observation is None or observation["source"] not in ("amazon", "flipkart") or not _functional_match(row, ref["id"]):
                continue
            order = timestamp(row["reviewed_at"]), row["revision"], row["id"]
            if ref["id"] not in latest or order > latest[ref["id"]][0]:
                latest[ref["id"]] = order, row
    return sorted(ref for ref, (_, row) in latest.items() if row["scope"] == "narrow_type"
                  and row["admissibility"] == "accepted" and row["relationship"] in _RELEVANT)


def _route(state, observation):
    """The actual route; older saved rows without one follow their list type."""
    facts = observation.get("facts", {})
    reference = state["captures"].get(observation.get("capture_id"), {}).get("reference")
    route = facts.get("source_route") or (reference.get("route") if isinstance(reference, dict) else None)
    if route is None:
        kind = facts.get("list_type")
        route = ({"bestsellers": "amazon-bestsellers", "product_search": "amazon-search"}.get(kind)
                 if observation["source"] == "amazon" else "flipkart-search" if kind == "product_search" else None)
    return route if route in SITES else None


def _reads(state, concept_id, *, as_of):
    """Each site's own search for this type: read, or why it is not yet."""
    from .source_coverage import TASK_KIND, _scope, _target_superseded
    from .source_interpretation import functional_source_questions
    scope, reads, captures = _scope(state, concept_id), {}, {}
    for route in SEARCH_ROUTES:
        tasks = sorted((task_id, task) for task_id, task in state["query_tasks"].items()
                       if task.get("kind") == TASK_KIND and task.get("route") == route
                       and task.get("targets", {}).get(concept_id) == scope
                       and not _target_superseded(state, task, concept_id))
        done = [task_id for task_id, task in tasks if task.get("status") in _FINAL and task.get("receipt_refs")]
        if not tasks:
            reads[route] = "not_started"
        elif not done:
            reads[route] = ("failed" if all(task.get("status") in ("failed", "blocked") for _, task in tasks)
                            else "pending")
        elif any(row["unknown_ids"] for row in functional_source_questions(
                state, concept_ids=[concept_id], task_ids=done, as_of=as_of)):
            reads[route] = "review_pending"
        else:
            reads[route] = "read"
        captures[route] = sorted({ref for task_id in done for ref in
                                  state["source_checks"][state["query_tasks"][task_id]["receipt_refs"][-1]]
                                  ["envelope"].get("capture_ids", [])})
    return reads, captures


def crowding_inputs(state, concept_id, *, as_of, folders=None):
    """Inputs of the check (records plus the saved pages' file state, no file
    contents); their hash tells when to look again. ``folders`` maps a saved
    page folder to where it is read (a store copy); records are never changed."""
    from .functional_types import effective_functional_definition
    meaning = effective_functional_definition(state, concept_id)
    if not meaning["accepted"]:
        return None
    listings = []
    for ref in _relevant_rows(state, concept_id, as_of=as_of):
        observation = state["observations"][ref]
        route = _route(state, observation)
        if route is None:
            continue
        facts = observation["facts"]
        listings.append({"evidence_id": ref, "route": route, "site": SITES[route],
                         "identity": facts.get("asin") or facts.get("product_id") or observation["source_item"],
                         "title": str(observation.get("semantic", {}).get("title") or ""),
                         "capture_id": (facts.get("direct_capture") or {}).get("capture_id")})
    reads, captures = _reads(state, concept_id, as_of=as_of)
    # The saved pages' own file state: a page that appears or changes is looked at again.
    pages = {capture_id: _page_state(state, capture_id, folders) for capture_id in sorted(
        {row["capture_id"] for row in listings if row["capture_id"]} | {ref for refs in captures.values() for ref in refs})}
    return {"meaning_hash": meaning["meaning_hash"], "listings": listings, "reads": reads, "read_captures": captures,
            "pages": pages}


def _page_state(state, capture_id, folders=None):
    reference = state["captures"].get(capture_id, {}).get("reference")
    folder = reference.get("folder") if isinstance(reference, dict) else None
    if not isinstance(folder, str):
        return None
    if folders is not None:
        try:
            folder = folders(folder)
        except SafetyError:
            return [[name, None, None] for name in ("rendered.html", "snapshot.json")]
    files = []
    for name in ("rendered.html", "snapshot.json"):
        try:
            info = (Path(folder) / name).stat()
            files.append([name, info.st_size, info.st_mtime_ns])
        except OSError:
            files.append([name, None, None])
    return files


def _cards(state, capture_id, route, reader, memo):
    capture = state["captures"].get(capture_id) if capture_id else None
    reference = capture.get("reference") if isinstance(capture, dict) else None
    if not isinstance(reference, dict) or not reference.get("folder") or reference.get("route") != route:
        return None
    if capture_id not in memo:
        try:
            memo[capture_id] = reader(reference["folder"], route)
        except (SafetyError, OSError):
            memo[capture_id] = None
    return memo[capture_id]


_COMPUTE = object()


def assess_crowding(state, concept_id, *, as_of, thresholds, card_reader=read_capture_cards, memo=None, inputs=_COMPUTE,
                    folders=None):
    """One type's crowding fact (not saved); reads only saved pages."""
    from .marketplace_browser import verified_list_observation
    memo = {} if memo is None else memo
    inputs = crowding_inputs(state, concept_id, as_of=as_of, folders=folders) if inputs is _COMPUTE else inputs
    fact = {"kind": FACT_KIND, "version": VERSION, "concept_id": concept_id, "parser_version": CARD_PARSER_VERSION,
            "thresholds_version": thresholds["version"], "meaning_hash": None, "level": NOT_CHECKED,
            "reason": "CROWDING_MEANING_PENDING", "rule": None, "counts": None, "reads": None,
            "search_results": None, "evidence_ids": [], "unreadable_captures": [], "basis_hash": digest(inputs)}
    if inputs is None:
        return fact
    merged = {}
    for row in inputs["listings"]:
        pages = _cards(state, row["capture_id"], row["route"], card_reader, memo)
        card = pages["cards"].get(row["identity"]) if pages and pages.get("state") == "read" else None
        bestseller = (row["route"] == "amazon-bestsellers"
                      and verified_list_observation(state["observations"][row["evidence_id"]]))
        saved = merged.setdefault((row["site"], row["identity"]), {"site": row["site"], "bestseller": False,
                                                                   "card": None, "evidence_ids": [], "title": row["title"]})
        saved["bestseller"] = saved["bestseller"] or bestseller
        saved["evidence_ids"].append(row["evidence_id"])
        if card is not None:
            saved["card"] = card if saved["card"] is None else merge_card_facts(saved["card"], card)
    listings = _collapse_variants(list(merged.values()))
    level, rule, counts = crowding_level(listings, thresholds)
    results = {}
    for route, capture_ids in inputs["read_captures"].items():
        totals = [pages for capture_id in capture_ids for pages in [_cards(state, capture_id, route, card_reader, memo)]
                  if pages and pages.get("total_results") is not None]
        best = max(totals, key=lambda pages: pages["total_results"]) if totals else None
        results[SITES[route]] = ({"total": best["total_results"], "lower_bound": best["total_results_lower_bound"]}
                                 if best else None)
    complete = all(value == "read" for value in inputs["reads"].values())
    if level is None or level != "high" and not complete:
        level = NOT_CHECKED
        reason = "CROWDING_READS_PENDING" if not complete else "CROWDING_SAVED_PAGE_UNREADABLE"
    else:
        reason = "CROWDING_CHECKED"
    fact.update(meaning_hash=inputs["meaning_hash"], level=level, reason=reason, rule=rule if level != NOT_CHECKED else None,
                counts=counts, reads=inputs["reads"], search_results=results,
                evidence_ids=sorted(ref for row in listings for ref in row["evidence_ids"]),
                # A saved page that cannot be read is refreshed by the type's next
                # due read of that search (the 7-day source freshness rule).
                unreadable_captures=sorted({row["capture_id"] for row in inputs["listings"] if row["capture_id"]
                                            and not (_cards(state, row["capture_id"], row["route"], card_reader, memo)
                                                     or {}).get("state") == "read"}))
    return fact


def _title_key(title):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", title.casefold()).split())[:25]


def _collapse_variants(listings):
    """Colour/size variants of one product are one listing: on one site, cards
    with the same ratings count, stars and title start share their reviews."""
    groups, result = {}, []
    for row in listings:
        card = row["card"] or {}
        if card.get("ratings_count") is None or not _title_key(row["title"]):
            result.append(dict(row, variants=1)); continue
        group = (row["site"], card["ratings_count"], card.get("star_rating"), _title_key(row["title"]))
        if group not in groups:
            groups[group] = dict(row, variants=1, evidence_ids=list(row["evidence_ids"])); result.append(groups[group])
            continue
        saved = groups[group]
        saved.update(variants=saved["variants"] + 1, bestseller=saved["bestseller"] or row["bestseller"],
                     card=merge_card_facts(saved["card"], card), evidence_ids=saved["evidence_ids"] + row["evidence_ids"])
    return result


def fact_id(concept_id):
    return key("crowding-fact", identifier(concept_id))


def _current(fact, meaning_hash, thresholds):
    return (isinstance(fact, dict) and fact.get("kind") == FACT_KIND and fact.get("version") == VERSION
            and fact.get("parser_version") == CARD_PARSER_VERSION and fact.get("meaning_hash") == meaning_hash
            and meaning_hash is not None and fact.get("thresholds_version") == thresholds["version"]
            and fact.get("level") in LEVELS + (NOT_CHECKED,))


def crowding_for(state, concept_id, *, thresholds=None):
    """The read API: a type's saved crowding fact for its current meaning.

    Returns the saved fact (level "low"/"medium"/"high"/"not_checked", the
    rule that decided it, counts per site, brands/sellers, max ratings,
    bought-in-past-month and bestseller hits, read states and evidence ids).
    Anything missing, for an older meaning, parser or thresholds is returned
    as Not checked with reason CROWDING_NOT_ASSESSED, never as Low.
    """
    from .functional_types import effective_functional_definition
    thresholds = thresholds or load_thresholds()
    meaning = effective_functional_definition(state, concept_id)
    saved = state["batch_runs"].get(fact_id(concept_id))
    if _current(saved, meaning["meaning_hash"] if meaning["accepted"] else None, thresholds):
        return deepcopy(saved)
    return {"kind": FACT_KIND, "version": VERSION, "concept_id": concept_id, "level": NOT_CHECKED,
            "reason": "CROWDING_NOT_ASSESSED", "rule": None, "counts": None, "reads": None, "search_results": None, "unreadable_captures": [],
            "evidence_ids": [], "thresholds_version": thresholds["version"], "parser_version": CARD_PARSER_VERSION,
            "meaning_hash": meaning["meaning_hash"] if meaning["accepted"] else None}


def validate_crowding_fact(fact, concept_id, meaning_hash, thresholds=None):
    """A crowding fact as ``crowding_for`` returns it, for this type and meaning
    and the current thresholds (a frozen report copy is checked without the
    research store): its level and rule must be what the threshold ladder gives
    for its own stored counts; a checked level never stands on its word."""
    thresholds = thresholds or load_thresholds()
    try:
        known = fact["level"] in LEVELS
        if (fact["kind"] != FACT_KIND or fact["version"] != VERSION or fact["concept_id"] != concept_id
                or fact["meaning_hash"] != meaning_hash or meaning_hash is None
                or fact["parser_version"] != CARD_PARSER_VERSION or fact["thresholds_version"] != thresholds["version"]
                or fact["level"] not in LEVELS + (NOT_CHECKED,) or not isinstance(fact["evidence_ids"], list)
                or fact["evidence_ids"] != sorted(set(fact["evidence_ids"]))
                or any(not isinstance(ref, str) for ref in fact["evidence_ids"])):
            raise SafetyError("CROWDING_FACT_INVALID")
        if known and (fact["reason"] != "CROWDING_CHECKED"
                      or threshold_ladder(fact["counts"], thresholds) != (fact["level"], fact["rule"])
                      or sum(fact["counts"]["relevant_listings"].values()) > len(fact["evidence_ids"])):
            raise SafetyError("CROWDING_FACT_INVALID")
        # Not checked: never with a rule, and never when its own counts already show High.
        if not known and (fact["rule"] is not None or fact["reason"] == "CROWDING_CHECKED"
                          or fact["counts"] is not None and threshold_ladder(fact["counts"], thresholds)[0] == "high"):
            raise SafetyError("CROWDING_FACT_INVALID")
        for ref in fact["evidence_ids"]:
            identifier(ref)
    except (SafetyError, KeyError, TypeError, AttributeError):  # an evidence id that is not an identifier too
        raise SafetyError("CROWDING_FACT_INVALID") from None
    return fact


def crowding_unchanged(state, concept_id, facts, meaning_hash):
    """Reserve/release check: the type's current crowding still has the frozen
    level, rule, thresholds version and meaning. Only these four are compared,
    so a re-check that finds the same answer never disturbs a reservation."""
    current = crowding_for(state, concept_id)
    return ((current["level"], current["rule"], current["thresholds_version"], current["meaning_hash"])
            == (facts["level"], facts["rule"], facts["thresholds_version"], meaning_hash))


def crowding_known(fact):
    return isinstance(fact, dict) and fact.get("level") in LEVELS


def _due_inputs(state, concept_id, *, as_of, thresholds, folders=None):
    """The check's inputs when its saved answer is missing or out of date, else None."""
    inputs = crowding_inputs(state, concept_id, as_of=as_of, folders=folders)
    if inputs is None:
        return None  # No accepted meaning: nothing can be checked or saved.
    saved = state["batch_runs"].get(fact_id(concept_id))
    current = _current(saved, inputs["meaning_hash"], thresholds) and saved.get("basis_hash") == digest(inputs)
    return None if current else inputs


def crowding_due(state, concept_id, *, as_of, thresholds=None):
    """Read-only: would the crowding stage change this type's saved answer?"""
    return _due_inputs(state, concept_id, as_of=as_of, thresholds=thresholds or load_thresholds()) is not None


def calibration_record(state, thresholds=None):
    thresholds = thresholds or load_thresholds()
    saved = state["batch_runs"].get(key("crowding-calibration", thresholds["version"]))
    return deepcopy(saved) if isinstance(saved, dict) and saved.get("kind") == CALIBRATION_KIND else None


def crowding_summary(state, thresholds=None):
    """Owner meter and agent view from saved facts only: checked types per
    level and the one-time calibration state, with an alert line when the
    rating thresholds need their agreed one-time adjustment."""
    thresholds = thresholds or load_thresholds()
    facts = [row for row in state["batch_runs"].values() if isinstance(row, dict) and row.get("kind") == FACT_KIND
             and row.get("version") == VERSION and row.get("thresholds_version") == thresholds["version"]]
    levels = {level: sum(row.get("level") == level for row in facts) for level in LEVELS + (NOT_CHECKED,)}
    record = calibration_record(state, thresholds)
    checked, sample = sum(levels[level] for level in LEVELS), thresholds["calibration"]["after_checked_types"]
    calibration = record["state"] if record else "waiting_for_sample"
    alert = None
    if calibration == "calibration_needed":
        alert = ("CROWDING CALIBRATION NEEDED: " + record["high_share"] + " of the first " + str(record["checked_types"])
                 + " checked types are High (" + thresholds["version"] + "). Adjust the rating thresholds once in "
                 "config/research/crowding.json with a new version and record the before/after counts.")
    text = ("Amazon/Flipkart crowding: " + str(checked) + " types checked (" + str(levels["high"]) + " high, "
            + str(levels["medium"]) + " medium, " + str(levels["low"]) + " low), " + str(levels[NOT_CHECKED])
            + " not checked yet; calibration " + calibration.replace("_", " ")
            + ("" if record else " (" + str(min(checked, sample)) + " of " + str(sample) + ")") + ".")
    return {"thresholds_version": thresholds["version"], "levels": levels, "calibration": calibration,
            "alert": alert, "text": text}


def _calibrate(memory, thresholds, at):
    """Once per threshold version, after the first checked types: flag a skew.

    More than the upper share or fewer than the lower share of checked types
    in High records "calibration_needed" for the agent to adjust the rating
    thresholds (only those) once; otherwise "in_range". Never changes them.
    """
    state = memory.snapshot()
    saved = calibration_record(state, thresholds)
    if saved is not None:
        return saved
    rule = thresholds["calibration"]
    checked = [row for row in state["batch_runs"].values() if isinstance(row, dict) and row.get("kind") == FACT_KIND
               and row.get("version") == VERSION and row.get("thresholds_version") == thresholds["version"]
               and row.get("level") in LEVELS]
    if len(checked) < rule["after_checked_types"]:
        return None
    high = sum(row["level"] == "high" for row in checked)
    share = Decimal(high) / Decimal(len(checked))
    needed = share > _share(rule["high_share_above"]) or share < _share(rule["high_share_below"])
    record = {"kind": CALIBRATION_KIND, "version": VERSION, "thresholds_version": thresholds["version"], "at": at,
              "checked_types": len(checked), "high_types": high, "high_share": str(share.quantize(Decimal("0.0001"))),
              "levels": {level: sum(row["level"] == level for row in checked) for level in LEVELS},
              "state": "calibration_needed" if needed else "in_range",
              "message": ("High crowding is outside the expected share of checked types; adjust the rating thresholds once "
                          "and record the before/after counts." if needed else
                          "High crowding share of the first checked types is within the expected range.")}
    record_id = key("crowding-calibration", thresholds["version"])
    memory.put("batch_runs", record_id, record, event_id=key("crowding-calibration-saved", [record_id, digest(record)]))
    from .operations import record_event
    record_event("crowding_calibration", state=record["state"],
                 reason="CROWDING_CALIBRATION_NEEDED" if needed else "CROWDING_CALIBRATION_IN_RANGE")
    return record


def refresh_crowding(memory, concept_ids, *, as_of=None, priority=(), limit=REFRESH_LIMIT, thresholds=None,
                     card_reader=read_capture_cards, folders=None):
    """Work out and save the crowding fact of up to ``limit`` due types (free, no model).

    Only types whose saved answer is missing or out of date (a new meaning,
    parser, thresholds or changed reads, reviews or saved pages) are worked
    out, priority types first, so a large group never re-checks the same types
    while others wait. Finishes with the one-time calibration check.
    ``card_reader`` and ``folders`` let a reader work on a copy of a store:
    saved page folders are mapped at read time, never rewritten in records.
    """
    from .read_memo import pure_read_scope
    at = as_of or utc_now(); timestamp(at)
    if (not isinstance(concept_ids, (list, tuple)) or any(not isinstance(cid, str) for cid in concept_ids)
            or not isinstance(priority, (list, tuple, set, frozenset)) or type(limit) is not int or not 1 <= limit <= 500):
        raise SafetyError("CROWDING_REFRESH_INVALID")
    thresholds = validate_thresholds(thresholds) if thresholds is not None else load_thresholds()
    state = memory.snapshot()
    ids = [cid for cid in dict.fromkeys(concept_ids) if cid in state["concepts"]
           and state["concepts"][cid].get("status") not in ("excluded", "merged", "retired")]
    results, memo, levels, more_due = {}, {}, {}, False
    first = [cid for cid in dict.fromkeys(priority) if cid in ids]
    with pure_read_scope():
        for cid in first + [cid for cid in ids if cid not in set(first)]:
            inputs = _due_inputs(state, cid, as_of=at, thresholds=thresholds, folders=folders)
            if inputs is None:
                continue
            if len(results) == limit:
                more_due = True; break
            results[cid] = assess_crowding(state, cid, as_of=at, thresholds=thresholds, card_reader=card_reader,
                                           memo=memo, inputs=inputs)
    changes = []
    for cid, fact in results.items():
        levels[fact["level"]] = levels.get(fact["level"], 0) + 1
        record_id = fact_id(cid)
        old = state["batch_runs"].get(record_id)
        if isinstance(old, dict) and {field: value for field, value in old.items() if field != "assessed_at"} == fact:
            continue
        changes.append(change("batch_runs", record_id, dict(fact, assessed_at=at), old))
    if changes:
        # One save for the refresh; each answer is still checked against the
        # saved one it replaces (a change in between refuses the save).
        memory.commit(key("crowding-facts-saved", [[row["id"], row["previous_hash"], digest(row["value"])]
                                                    for row in changes]), changes)
    calibration = _calibrate(memory, thresholds, at)
    return {"state": "checked" if results else "nothing_due", "assessed": len(results), "saved": len(changes),
            "more_due": more_due, "levels": levels,
            "calibration": calibration["state"] if calibration else None}
