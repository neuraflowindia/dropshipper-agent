"""The product-map lane (Plan R10 W10, owner decision D8): grow the map of
product areas and headings, and keep renewing it, inside the programme's
ordinary AI allowance. No separate bulk AI run exists.

It runs in the discovery step (``catalogue_discovery``): first in one round of
four (the programme rotates the first use of the AI calls held for discovery
among the heading searches, the platforms, wider discovery and this lane),
otherwise after the heading searches and wider discovery. Each round:

1. Staging, local only: new material is looked at and the useful part staged
   as seed material (``map_materials``); the fresh-signal list the heading
   order reads is refreshed.
2. Build: one seed C call (``catalogue_seed.build_catalogue_branch``, one
   sub-area, at most ``materials_per_call`` staged examples) for the sub-area
   with the fewest headings that has waiting material and is not full
   (``headings_per_sub_area``); sub-areas with ``min_batch`` waiting examples
   first, the categories in turn on ties.
3. Renewal: when new types have arrived that the map has not considered yet
   (found through new arrivals, rising queries, new advertisers and the other
   discovery lanes), one ``catalogue_expansion.run_expansion`` call with at
   most ``concepts_per_call`` (five at most) of them.

Each call is an ordinary work-turn model execution and starts only while the
round still has an allowance for it (``work_turns.new_model_work_wait``);
otherwise the lane simply waits for the next round. A failed call waits
``retry_minutes`` (its saved original job is reused, never repeated blind); a
material that cannot be read is set aside for a day so its sub-area moves on.
An admitted heading is part of the effective catalogue at once, so the next
discovery wave can serve it (``heading_order`` serves never-served headings
first). The lane's small state is one ``batch_runs`` record per programme.
"""

from collections import Counter
from copy import deepcopy
from datetime import timedelta

from .map_materials import (advance_staging, built_material_ids, collect_rows, load_examined, settings,
                            staged_materials, write_signals)
from .models import SafetyError, digest, identifier, timestamp, utc_now
from .research_memory import key

LANE_KIND = "product-map-lane-v1"
SET_ASIDE_HOURS = 24
# Allowance and lock waits end with the round; they never hold a sub-area.
_ROUND_WAITS = ("WORK_TURN_MODEL_LIMIT", "WORK_TURN_PURPOSE_LIMIT", "WORK_TURN_CLOSED", "RUNNER_ALREADY_ACTIVE")
_CONTAINED = (SafetyError, OSError, KeyError, TypeError, ValueError)


def _lane_id(programme_id):
    return key("product-map-lane", identifier(programme_id))


def lane_record(state, programme_id):
    """The lane's saved state (a new one before its first round)."""
    lane_id = _lane_id(programme_id)
    saved = state["batch_runs"].get(lane_id)
    if saved is None:
        return {"kind": LANE_KIND, "id": lane_id, "programme_id": programme_id, "last_category": None,
                "branch_waits": {}, "set_aside": {}, "renewal_wait": None}
    if (not isinstance(saved, dict) or saved.get("kind") != LANE_KIND or saved.get("id") != lane_id
            or set(saved) != {"kind", "id", "programme_id", "last_category", "branch_waits", "set_aside", "renewal_wait"}
            or not isinstance(saved["branch_waits"], dict) or not isinstance(saved["set_aside"], dict)):
        raise SafetyError("PRODUCT_MAP_LANE_INVALID")
    return deepcopy(saved)


def _save_lane(memory, state, new):
    """Save the lane's record when it changed (only this lane writes it)."""
    previous = state["batch_runs"].get(new["id"])
    if previous != new:
        memory.put("batch_runs", new["id"], new, previous=previous,
                   event_id=key("product-map-lane-state", [new["id"], digest(previous), digest(new)]))


def config_hash(config, catalogue):
    """What a looked-at candidate's outcome depends on (staged material is always kept)."""
    from .wider_candidates import category_words
    return digest([config["branch_words"], config["target"], config["build"]["reexamine_after_headings"],
                   catalogue["revision"], [row["id"] for row in catalogue["branches"]], category_words()])


def _due(wait, at):
    return wait is None or timestamp(wait["next_retry_at"]) <= timestamp(at)


def _later(at, minutes):
    return (timestamp(at) + timedelta(minutes=minutes)).isoformat()


def heading_counts(base, state):
    """Usable headings per sub-area: the base headings and the admitted ones
    (the effective catalogue's families, without building it)."""
    families = {row["id"]: row for row in base["families"]}
    families.update((row["family"]["id"], row["family"]) for row in state["catalogue_additions"].values()
                    if row.get("outcome") == "accepted")
    return Counter(row["parent_id"] for row in families.values() if row["disposition"] == "usable")


def renewal_candidates(state, limit):
    """Types the map has not considered yet (a resumable failure is not considered)."""
    from .catalogue_expansion import exploration_description
    considered = {row["concept_id"] for row in state["catalogue_additions"].values()
                  if row.get("concept_id") and not row.get("resumable", False)}
    found = []
    for concept_id in sorted(state["concepts"]):
        if len(found) >= limit:
            break
        if concept_id not in considered and exploration_description(state, concept_id) is not None:
            found.append(concept_id)
    return found


def build_choice(state, base, examined, lane, at, config):
    """The sub-area to build next and its waiting material, or None.

    Sub-areas with at least ``min_batch`` waiting examples go first (a call
    for one or two examples only when no fuller sub-area is ready); then the
    fewest headings, the categories in turn."""
    from .service import CATEGORIES
    if examined is None:
        return None
    built, counts = built_material_ids(state), heading_counts(base, state)
    branches = {row["id"]: row for row in base["branches"]}
    waiting = {}
    for material_id, branch_id in staged_materials(examined).items():
        aside = lane["set_aside"].get(material_id)
        if material_id not in built and branch_id in branches and (aside is None or timestamp(aside) <= timestamp(at)):
            waiting.setdefault(branch_id, []).append(material_id)
    ready = [branch_id for branch_id in waiting if counts[branch_id] < config["target"]["headings_per_sub_area"]
             and _due(lane["branch_waits"].get(branch_id), at)]
    if not ready:
        return None
    categories = list(CATEGORIES)
    if lane["last_category"] in categories:
        offset = categories.index(lane["last_category"]) + 1
        categories = categories[offset:] + categories[:offset]
    full = [value for value in ready if len(waiting[value]) >= config["build"]["min_batch"]]
    branch_id = min(full or ready, key=lambda value: (counts[value], categories.index(branches[value]["category"]), digest(value)))
    return branch_id, waiting[branch_id][:config["build"]["materials_per_call"]]


def _model_wait(memory, runner, count):
    from .work_turns import new_model_work_wait
    if runner is None:
        return "PRODUCT_MAP_MODEL_WAITING"
    return new_model_work_wait(memory, "C", item_count=count)


def _renew(memory, state, base, runner, lane, at, config):
    from .catalogue_discovery import _raise_shared_integrity
    from .catalogue_expansion import run_expansion
    if not _due(lane["renewal_wait"], at):
        return dict(lane["renewal_wait"], state="waiting")
    ids = renewal_candidates(state, config["renewal"]["concepts_per_call"])
    if not ids:
        return {"state": "nothing_new"}
    reason = _model_wait(memory, runner, len(ids))
    if reason:
        return {"state": "waiting", "reason": reason, "concept_ids": ids}
    try:
        result = run_expansion(memory, base, runner, concept_ids=ids, limit=len(ids), as_of=at)
        problem = result["problem"]
    except _CONTAINED as error:
        _raise_shared_integrity(error)
        result, problem = {"records": []}, str(error) if isinstance(error, SafetyError) else "PRODUCT_MAP_RENEWAL_LOCAL_FAILURE"
    lane["renewal_wait"] = ({"reason": problem, "next_retry_at": _later(at, config["renewal"]["retry_minutes"])}
                            if problem and problem not in _ROUND_WAITS else None)
    return {"state": "waiting" if problem else "renewed", "reason": problem, "concept_ids": ids,
            "outcomes": dict(Counter(row["outcome"] for row in result["records"]))}


def _build(memory, state, base, runner, lane, examined, at, config):
    from .catalogue_discovery import _raise_shared_integrity
    from .catalogue_seed import build_catalogue_branch
    choice = build_choice(state, base, examined, lane, at, config)
    if choice is None:
        return {"state": "nothing_waiting"}
    branch_id, material_ids = choice
    reason = _model_wait(memory, runner, len(material_ids))
    if reason:
        return {"state": "waiting", "reason": reason, "branch_id": branch_id}
    try:
        result = build_catalogue_branch(memory, base, runner, branch_id=branch_id, material_ids=material_ids,
                                        limit=len(material_ids), as_of=at)
    except _CONTAINED as error:
        _raise_shared_integrity(error)
        result = {"records": [], "reused": [], "job_ids": [], "failures": [{"material_id": None,
                  "reason": str(error) if isinstance(error, SafetyError) else "PRODUCT_MAP_BUILD_LOCAL_FAILURE"}]}
    lane["last_category"] = next(row["category"] for row in base["branches"] if row["id"] == branch_id)
    call = [row for row in result["failures"] if not str(row["reason"]).startswith("CATALOGUE_SEED_")]
    for row in result["failures"]:
        if row["material_id"] and str(row["reason"]).startswith("CATALOGUE_SEED_"):
            lane["set_aside"][row["material_id"]] = _later(at, SET_ASIDE_HOURS * 60)
    built = built_material_ids(state) | {row["material_id"] for row in result["records"] + result["reused"]}
    lane["set_aside"] = {mid: when for mid, when in lane["set_aside"].items() if mid not in built}
    held = next((row["reason"] for row in call if row["reason"] not in _ROUND_WAITS), None)
    if held and not result["records"]:
        lane["branch_waits"][branch_id] = {"reason": held, "next_retry_at": _later(at, config["build"]["retry_minutes"])}
    else:
        lane["branch_waits"].pop(branch_id, None)
    return {"state": "built" if result["records"] else "waiting", "branch_id": branch_id,
            "materials": len(material_ids), "outcomes": dict(Counter(row["outcome"] for row in result["records"])),
            "job_ids": result["job_ids"], "failures": result["failures"][:5]}


def map_summary(base, examined, state, config):
    """How big the map is and how much staged material still waits."""
    from .service import CATEGORIES
    counts = heading_counts(base, state)
    branches = [row for row in base["branches"] if row["parent_id"] is not None]
    built = built_material_ids(state)
    return {"headings": sum(counts.values()), "sub_areas": len(branches),
            "categories": {category: sum(counts[row["id"]] for row in branches if row["category"] == category)
                           for category in CATEGORIES},
            "full_sub_areas": sum(counts[row["id"]] >= config["target"]["headings_per_sub_area"] for row in branches),
            "waiting_material": sum(material_id not in built for material_id in staged_materials(examined or {}))}


def advance_product_map(memory, context, *, programme_id, catalogue=None):
    """One bounded product-map turn: stage, build, renew. The research lookup
    copy is written once at the end of the turn, not after every saved record."""
    with memory.defer_lookup_projection():
        return _advance(memory, context, programme_id=programme_id, catalogue=catalogue)


def _advance(memory, context, *, programme_id, catalogue):
    from .catalogue import load_catalogue, validate_catalogue
    from .catalogue_expansion import effective_catalogue
    at = context.get("at") or utc_now()
    timestamp(at)
    config = settings()
    base = validate_catalogue(catalogue or load_catalogue())
    state = memory.snapshot()
    view = effective_catalogue(base, state)
    rows = collect_rows(memory, state, at, config)
    signals = write_signals(memory, rows, at, config)
    staging, examined = advance_staging(memory, state, view, rows, config, config_hash(config, base))
    lane = lane_record(state, programme_id)
    runner = context.get("runner")
    # The build call goes first: it is the lane's own work in the rounds when it
    # has the first use of the AI calls held for discovery. A full copy of the
    # research state is costly: read it again only after a call wrote to it.
    build = (_build(memory, state, base, runner, lane, examined, at, config) if examined is not None
             else {"state": "waiting", "reason": staging["waiting"]})
    current = memory.snapshot() if build.get("outcomes") else state
    renewal = _renew(memory, current, base, runner, lane, at, config)
    _save_lane(memory, state, lane)
    current = memory.snapshot() if renewal.get("outcomes") else current
    return {"state": "checked" if "waiting" not in (renewal["state"], build["state"]) else "waiting",
            "staging": staging, "signals": signals, "renewal": renewal, "build": build,
            "map": map_summary(base, examined, current, config), "new_concept_ids": []}


def map_step(memory, programme_record, context):
    """The discovery step's product-map lane (``catalogue_discovery``)."""
    from .operations import preparation_step
    with preparation_step("product_map"):
        return advance_product_map(memory, context, programme_id=programme_record["programme_id"],
                                   catalogue=context.get("discovery_catalogue"))


def map_lane_hint(memory, state, programme_id, catalogue, *, as_of, runner_available):
    """Wake hint: AI work waits (material to build, types to consider). Local
    files and saved records only; staging alone is never a reason to wake."""
    from .catalogue import validate_catalogue
    config = settings()
    base = validate_catalogue(catalogue)
    lane = lane_record(state, programme_id)
    examined = load_examined(memory, config_hash(config, base))
    choice = build_choice(state, base, examined, lane, as_of, config)
    renewal = renewal_candidates(state, config["renewal"]["concepts_per_call"]) if _due(lane["renewal_wait"], as_of) else []
    waits = [row["next_retry_at"] for row in lane["branch_waits"].values() if not _due(row, as_of)]
    waits += [lane["renewal_wait"]["next_retry_at"]] if not _due(lane["renewal_wait"], as_of) else []
    waits += [when for when in lane["set_aside"].values() if timestamp(when) > timestamp(as_of)]
    return {"ready": bool(runner_available and (choice or renewal)),
            "next_retry_at": min(waits, key=timestamp) if waits else None,
            "signal": {"build": list(choice) if choice else None, "renewal": renewal, "lane": digest(lane)}}
