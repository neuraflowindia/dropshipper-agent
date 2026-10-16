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
   with the fewest headings that has waiting material. The target
   ``headings_per_sub_area`` is a soft cap: sub-areas below it always go
   first, and a sub-area at or above it is built only when no sub-area below
   it has waiting material ready (one on a retry wait holds nobody up).
   Within that, sub-areas with ``min_batch`` waiting examples first, the
   categories in turn on ties.
3. Renewal: when new types have arrived that the map has not considered yet
   (found through new arrivals, rising queries, new advertisers and the other
   discovery lanes), one ``catalogue_expansion.run_expansion`` call with at
   most ``concepts_per_call`` (five at most) of them.
4. Top-up (``map_topup``): when the build call had nothing to build and fewer
   than ``topup.min_unsearched_headings`` usable headings were never searched,
   one C call asks for at most ``headings_per_call`` new headings for the
   sub-area with the fewest never-searched headings; at most
   ``max_calls_per_day`` a day. A saved answer that was never admitted is
   admitted first (never paid for twice).

At the end of each round the lane saves a small runway summary
(``product_map/runway.json``): how many headings, how many never searched and
the recent first-search pace; ``map_line`` turns it into the owner's line.

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
from datetime import timedelta, timezone

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


_FIELDS = {"kind", "id", "programme_id", "last_category", "branch_waits", "set_aside", "renewal_wait"}
# Added with the AI top-up; a lane record saved before it reads them as these.
_TOPUP_FIELDS = {"topup_wait": None, "topup_day": None, "topup_last_category": None, "topup_branch_waits": {}}


def _wait_shape(value):
    return value is None or (isinstance(value, dict) and set(value) == {"reason", "next_retry_at"}
                             and isinstance(value["reason"], str) and timestamp(value["next_retry_at"]) is not None)


def lane_record(state, programme_id):
    """The lane's saved state (a new one before its first round)."""
    lane_id = _lane_id(programme_id)
    saved = state["batch_runs"].get(lane_id)
    if saved is None:
        return {"kind": LANE_KIND, "id": lane_id, "programme_id": programme_id, "last_category": None,
                "branch_waits": {}, "set_aside": {}, "renewal_wait": None, **deepcopy(_TOPUP_FIELDS)}
    if (not isinstance(saved, dict) or saved.get("kind") != LANE_KIND or saved.get("id") != lane_id
            or set(saved) not in (_FIELDS, _FIELDS | set(_TOPUP_FIELDS))
            or not isinstance(saved["branch_waits"], dict) or not isinstance(saved["set_aside"], dict)):
        raise SafetyError("PRODUCT_MAP_LANE_INVALID")
    lane = dict(deepcopy(_TOPUP_FIELDS), **deepcopy(saved))
    day, waits = lane["topup_day"], lane["topup_branch_waits"]
    try:
        if (not _wait_shape(lane["topup_wait"]) or not isinstance(waits, dict)
                or any(not isinstance(branch_id, str) or wait is None or not _wait_shape(wait)
                       for branch_id, wait in waits.items())
                or lane["topup_last_category"] is not None
                and not isinstance(lane["topup_last_category"], str)
                or day is not None and (not isinstance(day, dict) or set(day) != {"date", "calls"}
                                        or not isinstance(day["date"], str) or type(day["calls"]) is not int)):
            raise ValueError
    except (SafetyError, ValueError):
        raise SafetyError("PRODUCT_MAP_LANE_INVALID") from None
    return lane


def _save_lane(memory, state, new):
    """Save the lane's record when it changed (only this lane writes it)."""
    previous = state["batch_runs"].get(new["id"])
    if previous != new:
        memory.put("batch_runs", new["id"], new, previous=previous,
                   event_id=key("product-map-lane-state", [new["id"], digest(previous), digest(new)]))


def config_hash(config, catalogue):
    """What a looked-at candidate's outcome depends on (staged material is always kept)."""
    from .wider_candidates import category_words
    # The target is not part of it: it only orders the builds (a soft cap), never what is staged.
    return digest([config["branch_words"], config["build"]["reexamine_after_headings"],
                   catalogue["revision"], [row["id"] for row in catalogue["branches"]], category_words()])


def _due(wait, at):
    return wait is None or timestamp(wait["next_retry_at"]) <= timestamp(at)


def _later(at, minutes):
    return (timestamp(at) + timedelta(minutes=minutes)).isoformat()


def heading_counts(base, state):
    """Usable headings per sub-area: the base headings and the admitted ones
    (the effective catalogue's families, without building it)."""
    from .map_topup import usable_headings
    return Counter(row["parent_id"] for row in usable_headings(base, state).values())


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

    The target (``headings_per_sub_area``) is a soft cap: only when no
    sub-area below it has waiting material ready (a sub-area on a retry wait
    holds nobody up) may one at or above it be built.
    Among those, sub-areas with at least ``min_batch`` waiting examples go
    first (a call for one or two examples only when no fuller sub-area is
    ready); then the fewest headings, the categories in turn."""
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
    due = [branch_id for branch_id in waiting if _due(lane["branch_waits"].get(branch_id), at)]
    ready = [branch_id for branch_id in due if counts[branch_id] < config["target"]["headings_per_sub_area"]] or due
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


def _build_used_call(build):
    """Whether the round's build step used (or tried to use) an AI call: then
    the top-up leaves the round's allowance to the other work."""
    if build["state"] == "nothing_waiting" or "materials" not in build:
        return False
    return bool(build["job_ids"]) or any(not str(row["reason"]).startswith("CATALOGUE_SEED_")
                                         for row in build["failures"])


def _utc_day(at):
    return timestamp(at).astimezone(timezone.utc).date().isoformat()


def _calls_today(lane, at):
    day = lane["topup_day"]
    return day["calls"] if day is not None and day["date"] == _utc_day(at) else 0


def _count_call(lane, at):
    lane["topup_day"] = {"date": _utc_day(at), "calls": _calls_today(lane, at) + 1}


# A replay that may work later (a read that failed now); any other replay or
# admission failure is final for that saved answer: it is closed with one record.
_SELF_HEALING = ("SAVED_FILE_UNAVAILABLE",)
# A saved original whose outcome is not known yet holds only its own sub-area, for a day.
_PENDING = "AI_JOB_PENDING_RECONCILIATION"
PENDING_WAIT_HOURS = 24


def _topup_wait(lane, at, config, problem):
    lane["topup_wait"] = {"reason": problem, "next_retry_at": _later(at, config["topup"]["retry_minutes"])}
    return {"state": "waiting", "reason": problem}


def _close_job(memory, lane, item, job_id, at, config, problem):
    """Finish a saved answer that can never be admitted with one rejected
    record, so it is not replayed again; a failed save waits."""
    from .catalogue_discovery import _raise_shared_integrity
    from .map_topup import closing_record, save_records
    try:
        save_records(memory, [closing_record(item, job_id, at, problem)])
    except _CONTAINED as error:
        _raise_shared_integrity(error)
        return dict(_topup_wait(lane, at, config, str(error) if isinstance(error, SafetyError)
                                else "PRODUCT_MAP_TOPUP_LOCAL_FAILURE"), job_id=job_id)
    return {"state": "closed", "reason": problem, "job_id": job_id}


def _admit_answer(memory, state, base, lane, item, result, job_id, at, config):
    """Save one answered job's admissions. A failed save waits and the saved
    job is admitted again later (``map_topup.saved_unadmitted_job``); an
    admission that fails in itself closes the job."""
    from .catalogue_discovery import _raise_shared_integrity
    from .map_topup import admission_records, save_records
    try:
        records = admission_records(base, state, item, result, job_id, config, at)
    except _CONTAINED as error:
        _raise_shared_integrity(error)
        if isinstance(error, OSError) or str(error) in _SELF_HEALING:
            return dict(_topup_wait(lane, at, config, "PRODUCT_MAP_TOPUP_LOCAL_FAILURE" if isinstance(error, OSError)
                                    else str(error)), branch_id=item["branch"]["id"], job_id=job_id)
        return _close_job(memory, lane, item, job_id, at, config, str(error) if isinstance(error, SafetyError)
                          else "PRODUCT_MAP_TOPUP_ADMISSION_FAILED")
    try:
        save_records(memory, records)
    except _CONTAINED as error:
        _raise_shared_integrity(error)
        problem = str(error) if isinstance(error, SafetyError) else "PRODUCT_MAP_TOPUP_LOCAL_FAILURE"
        return dict(_topup_wait(lane, at, config, problem), branch_id=item["branch"]["id"], job_id=job_id)
    lane["topup_wait"] = None
    lane["topup_branch_waits"].pop(item["branch"]["id"], None)
    return {"state": "added", "branch_id": item["branch"]["id"], "job_id": job_id,
            "outcomes": dict(Counter(row["outcome"] for row in records))}


def _replay(memory, state, base, lane, saved, at, config):
    """Admit a saved answer that was never admitted (no AI call)."""
    from .catalogue_discovery import _raise_shared_integrity
    from .map_topup import replay_saved_job
    job_id, request = saved
    item = request["payload"]["items"][0]
    try:
        _, result = replay_saved_job(memory, request)
    except _CONTAINED as error:
        _raise_shared_integrity(error)
        if isinstance(error, OSError) or str(error) in _SELF_HEALING:
            return dict(_topup_wait(lane, at, config, "PRODUCT_MAP_TOPUP_LOCAL_FAILURE" if isinstance(error, OSError)
                                    else str(error)), job_id=job_id)
        return dict(_close_job(memory, lane, item, job_id, at, config, str(error) if isinstance(error, SafetyError)
                               else "PRODUCT_MAP_TOPUP_REPLAY_FAILED"), replayed=True)
    return dict(_admit_answer(memory, state, base, lane, item, result, job_id, at, config), replayed=True)


def topup_need(base, state, config, served=None):
    """(never-searched usable headings, the threshold below which the AI tops up)."""
    from .heading_order import first_served_times
    from .map_topup import unsearched_counts
    served = first_served_times(state) if served is None else served
    return sum(unsearched_counts(base, state, served)[0].values()), config["topup"]["min_unsearched_headings"]


def _topup(memory, state, base, runner, lane, at, config, *, build_called):
    """The AI top-up (``map_topup``): a saved unadmitted answer first, then at
    most one new call when the map runs low and the build did not use the call."""
    from .catalogue_discovery import _raise_shared_integrity
    from .heading_order import first_served_times
    from .map_topup import PROMPT_VERSION, payload_for, saved_unadmitted_job, topup_choice, topup_item
    from .model_recovery import run_current_model_job
    lane["topup_branch_waits"] = {branch_id: wait for branch_id, wait in lane["topup_branch_waits"].items()
                                  if not _due(wait, at)}
    if not _due(lane["topup_wait"], at):
        return dict(lane["topup_wait"], state="waiting")
    try:
        saved = saved_unadmitted_job(memory, state)
    except _CONTAINED as error:
        _raise_shared_integrity(error)
        return _topup_wait(lane, at, config, str(error) if isinstance(error, SafetyError)
                           else "PRODUCT_MAP_TOPUP_LOCAL_FAILURE")
    if saved is not None:
        return _replay(memory, state, base, lane, saved, at, config)
    if build_called:
        return {"state": "build_used_the_call"}
    served = first_served_times(state)
    unsearched, threshold = topup_need(base, state, config, served)
    if unsearched >= threshold:
        return {"state": "not_needed", "unsearched": unsearched}
    if _calls_today(lane, at) >= config["topup"]["max_calls_per_day"]:
        return {"state": "day_limit", "unsearched": unsearched, "calls_today": _calls_today(lane, at)}
    reason = _model_wait(memory, runner, 1)
    if reason:
        return {"state": "waiting", "reason": reason, "unsearched": unsearched}
    branch = topup_choice(base, state, served, lane["topup_last_category"], at,
                          target=config["target"]["headings_per_sub_area"], waiting=lane["topup_branch_waits"])
    if branch is None:
        return {"state": "nothing_to_top_up", "unsearched": unsearched}
    item = topup_item(base, state, branch, config["topup"]["headings_per_call"])
    lane["topup_last_category"] = branch["category"]
    try:
        job_id, result = run_current_model_job(memory, runner, "C", payload_for(item), declared_version=PROMPT_VERSION,
                                               current_items=[item])
    except _CONTAINED as error:
        _raise_shared_integrity(error)
        problem = str(error) if isinstance(error, SafetyError) else "PRODUCT_MAP_TOPUP_LOCAL_FAILURE"
        if problem == _PENDING:
            # Its saved original is still being reconciled: only this sub-area waits; nothing was sent.
            lane["topup_branch_waits"][branch["id"]] = {"reason": problem,
                                                        "next_retry_at": _later(at, PENDING_WAIT_HOURS * 60)}
        elif problem not in _ROUND_WAITS:
            _count_call(lane, at)  # it may have reached the model: it counts against the day
            _topup_wait(lane, at, config, problem)
            # The sub-area waits too, so the rotation moves on rather than pinning it.
            lane["topup_branch_waits"][branch["id"]] = dict(lane["topup_wait"])
        return {"state": "waiting", "reason": problem, "branch_id": branch["id"]}
    _count_call(lane, at)
    return _admit_answer(memory, state, base, lane, item, result, job_id, at, config)


# --- The runway line -----------------------------------------------------------

RUNWAY = "runway.json"
RUNWAY_VERSION = 1


def runway_summary(base, state, at, config):
    """How long the never-searched headings last at the recent pace.

    Pace: headings first searched in the last seven days, per day (over the
    days since the first search ever, when that is under a week). Saved
    records only."""
    from .heading_order import first_served_times
    from .map_topup import usable_headings
    first = first_served_times(state)
    headings = usable_headings(base, state)
    times = [timestamp(first[family_id]) for family_id in headings if family_id in first]
    now = timestamp(at)
    recent = sum(now - timedelta(days=7) < when <= now for when in times)
    earliest = min(times) if times else None
    days = None if earliest is None else min(7.0, max(1.0, (now - earliest).total_seconds() / 86400))
    return {"version": RUNWAY_VERSION, "at": at, "headings": len(headings),
            "unsearched": len(headings) - len(times), "first_searched_last_7_days": recent,
            "pace_per_day": None if days is None else round(recent / days, 2),
            "topup_threshold": config["topup"]["min_unsearched_headings"]}


def _horizon(days):
    if days < 1.5:
        return "about a day"
    if days < 14:
        return f"about {round(days)} days"
    if days < 60:
        return f"roughly {round(days / 7)} weeks"
    return f"roughly {round(days / 30)} months"


def runway_text(summary):
    """The owner's plain-English product-map line."""
    if summary is None:
        return "Product map: not measured yet (the product-map lane has not run)."
    pace, left, threshold = summary["pace_per_day"], summary["unsearched"], summary["topup_threshold"]
    later = (f"headings are searched again for new ads, and the AI adds new headings whenever fewer than "
             f"{threshold:,} are left unsearched.")
    if not left:
        return f"Product map: {summary['headings']:,} headings; every heading has had its first search. Now " + later
    line = f"Product map: {summary['headings']:,} headings, {left:,} not searched yet"
    if pace is None:
        return line + "; the search pace is not measured yet (no heading has been searched). After that, " + later
    if pace == 0:
        return line + "; no heading had its first search in the last 7 days. After every heading's first search, " + later
    rate = "fewer than one" if pace < 1 else f"about {round(pace):,}"
    return (line + f"; {rate} searched a day lately, so {_horizon(left / pace)} until every heading has had its "
            "first search. After that, " + later)


def runway_line(base, state, at, config=None):
    """The runway line from the saved records (the owner meter's own read)."""
    return runway_text(runway_summary(base, state, at, config or settings()))


def _save_runway(memory, base, state, at, config):
    from .storage import atomic_json, safe_path
    summary = runway_summary(base, state, at, config)
    try:
        atomic_json(safe_path(memory.root, "product_map") / RUNWAY, summary)
    except (SafetyError, OSError):
        pass  # the line then shows the previous round's numbers
    return summary


def map_line(root):
    """The desktop line from the lane's saved summary (cheap: one small file)."""
    from .storage import load_json, safe_path
    path = safe_path(root, "product_map") / RUNWAY
    if not path.exists() and not path.is_symlink():
        return runway_text(None)
    value = load_json(path, 16384)
    fields = {"version", "at", "headings", "unsearched", "first_searched_last_7_days", "pace_per_day", "topup_threshold"}
    if (not isinstance(value, dict) or set(value) != fields or value["version"] != RUNWAY_VERSION
            or any(type(value[name]) is not int or value[name] < 0
                   for name in ("headings", "unsearched", "first_searched_last_7_days", "topup_threshold"))
            or value["pace_per_day"] is not None and (type(value["pace_per_day"]) not in (int, float)
                                                      or value["pace_per_day"] < 0)):
        raise SafetyError("PRODUCT_MAP_RUNWAY_INVALID")
    return runway_text(value)


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
    # Saved now, so the build's and renewal's lane state is never lost to a top-up failure.
    _save_lane(memory, state, lane)
    saved_lane = deepcopy(lane)
    current = memory.snapshot() if renewal.get("outcomes") else current
    try:
        topup = _topup(memory, current, base, runner, lane, at, config, build_called=_build_used_call(build))
    except _CONTAINED as error:
        from .catalogue_discovery import _raise_shared_integrity
        _raise_shared_integrity(error)
        topup = {"state": "waiting", "reason": str(error) if isinstance(error, SafetyError)
                 else "PRODUCT_MAP_TOPUP_LOCAL_FAILURE"}
    _save_lane(memory, {"batch_runs": {lane["id"]: saved_lane}}, lane)  # saved_lane is what is stored now
    current = memory.snapshot() if topup.get("outcomes") else current
    runway = _save_runway(memory, base, current, at, config)
    return {"state": "checked" if "waiting" not in (renewal["state"], build["state"], topup["state"]) else "waiting",
            "staging": staging, "signals": signals, "renewal": renewal, "build": build, "topup": topup,
            "map": dict(map_summary(base, examined, current, config), unsearched=runway["unsearched"]),
            "new_concept_ids": []}


def map_step(memory, programme_record, context):
    """The discovery step's product-map lane (``catalogue_discovery``)."""
    from .operations import preparation_step
    with preparation_step("product_map"):
        return advance_product_map(memory, context, programme_id=programme_record["programme_id"],
                                   catalogue=context.get("discovery_catalogue"))


def map_lane_hint(memory, state, programme_id, catalogue, *, as_of, runner_available):
    """Wake hint: AI work waits (material to build, types to consider, a due
    top-up or a saved top-up answer to admit). Local files and saved records
    only; staging alone is never a reason to wake."""
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
    try:
        topup = topup_hint(memory, state, base, lane, as_of, config, runner_available=runner_available)
    except _CONTAINED as error:
        from .catalogue_discovery import _raise_shared_integrity
        _raise_shared_integrity(error)  # the top-up's own defect never hides the build and renewal hints
        topup = {"ready": False, "next_retry_at": None,
                 "signal": str(error) if isinstance(error, SafetyError) else "PRODUCT_MAP_TOPUP_LOCAL_FAILURE"}
    waits += [topup["next_retry_at"]] if topup["next_retry_at"] else []
    return {"ready": bool(runner_available and (choice or renewal)) or topup["ready"],
            "next_retry_at": min(waits, key=timestamp) if waits else None,
            "signal": {"build": list(choice) if choice else None, "renewal": renewal, "topup": topup["signal"],
                       "lane": digest(lane)}}


def topup_hint(memory, state, base, lane, as_of, config, *, runner_available):
    """Whether top-up work is due: a saved answer to admit (local work), or a
    new call while the map is low, the day's calls remain and the AI is there.
    The never-searched count is read only when a call could start."""
    from datetime import datetime, time
    from .map_topup import saved_unadmitted_job
    if not _due(lane["topup_wait"], as_of):
        return {"ready": False, "next_retry_at": lane["topup_wait"]["next_retry_at"], "signal": "waiting"}
    saved = saved_unadmitted_job(memory, state)
    if saved is not None:
        return {"ready": True, "next_retry_at": None, "signal": ["admit", saved[0]]}
    if not runner_available or not config["topup"]["max_calls_per_day"]:
        return {"ready": False, "next_retry_at": None, "signal": None}
    from .heading_order import first_served_times
    from .map_topup import topup_choice
    served = first_served_times(state)
    unsearched, threshold = topup_need(base, state, config, served)
    if unsearched >= threshold:
        return {"ready": False, "next_retry_at": None, "signal": None}
    if _calls_today(lane, as_of) >= config["topup"]["max_calls_per_day"]:
        tomorrow = datetime.combine(timestamp(as_of).astimezone(timezone.utc).date() + timedelta(days=1), time(),
                                    tzinfo=timezone.utc)
        return {"ready": False, "next_retry_at": tomorrow.isoformat(), "signal": "day_limit"}
    waiting = {branch_id: wait for branch_id, wait in lane["topup_branch_waits"].items() if not _due(wait, as_of)}
    if topup_choice(base, state, served, lane["topup_last_category"], as_of,
                    target=config["target"]["headings_per_sub_area"], waiting=waiting) is None:
        # Every sub-area is at its ceiling, resting or waiting: wake when a wait ends.
        retry = min((wait["next_retry_at"] for wait in waiting.values()), key=timestamp, default=None)
        return {"ready": False, "next_retry_at": retry, "signal": "no_sub_area"}
    return {"ready": True, "next_retry_at": None, "signal": ["call", _utc_day(as_of), _calls_today(lane, as_of)]}
