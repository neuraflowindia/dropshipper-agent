"""Which quick-commerce checks are worth a credit (Plan R10 W5, D7).

One search on one app in one place costs one prepaid credit, so paid checks
are kept for product types that are close to a report:

* Admission: the type passes the ten-criterion checklist (at least five
  Meets under the current functional contract) and its Amazon/Flipkart
  crowding is Low or Medium. A checklist not yet passed, High crowding or a
  crowding check not yet done keeps the type's checks reserved for later.
  Admission is checked when a question is planned and again just before it
  is sent (``paid_question_hold``), so a type that changed while its
  question waited is not paid for.
* One phrase: the type's main reviewed quick-commerce phrase; never the
  broader or alternate wording tried after sparse results on other routes.
* Places: the reference points in ``config/research/quickcommerce_locations.json``.
  The first (Mumbai) is asked on all three apps; the others only once a
  first-place result was reviewed as a relevant match for the type.
* Repeats: a place answered for the type within ``recheck_days`` (30) is not
  asked again, whatever the week's run or a newly reviewed phrase. The one
  exception is a type allocated to an upcoming issue: its supporting
  quick-commerce rows keep the contract's seven-day validity and are checked
  again once older than that (``issue_refresh_held``), still only while the
  type is admitted and, for a later place, after a relevant first-place result.

A held-back check is a calm planning gap or wait whose reason starts with
``QUICKCOMMERCE_RESERVED_`` ("reserved for later"), never a failed check.
Nothing here reads a network or spends a credit; the credit balance itself is
checked before each batch (``quickcommerce_credits``).
"""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path

from .models import SafetyError, digest, timestamp
from .quickcommerce import PLATFORMS, prepare_query
from .storage import load_json

LOCATIONS = Path(__file__).resolve().parents[2] / "config/research/quickcommerce_locations.json"
KIND = "quickcommerce-locations-v1"
MAX_LOCATIONS = 5
# Task states after which the next place may be asked: an answer, or a
# failure with no retry left. Uncertain (reconciling) work is never settled.
_ANSWERED = frozenset(("acquired", "completed_no_results", "insufficient", "finished", "waiting_input"))
# Saved answers that stop a repeat of the same place within the recheck period.
RESULT_STATES = frozenset(("acquired", "completed_no_results", "insufficient", "finished"))


def validate_settings(value):
    """The checked places (name and exact coordinate text, first place first) and recheck period."""
    try:
        rows = value["locations"]
        if (set(value) - {"note"} != {"schema_version", "kind", "locations", "recheck_days"}
                or value["schema_version"] != 1 or value["kind"] != KIND
                or "note" in value and not isinstance(value["note"], str)
                or type(value["recheck_days"]) is not int or not 7 <= value["recheck_days"] <= 90
                or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_LOCATIONS
                or any(not isinstance(row, dict) or not isinstance(row.get("lat"), str)
                       or not isinstance(row.get("lon"), str) for row in rows)):
            raise SafetyError("QUICKCOMMERCE_LOCATIONS_INVALID")
        places = [prepare_query("blinkit", "location check", row)["location"] for row in rows]
    except (KeyError, TypeError, SafetyError):
        raise SafetyError("QUICKCOMMERCE_LOCATIONS_INVALID") from None
    if (len({place["name"].casefold() for place in places}) != len(places)
            or len({(place["lat"], place["lon"]) for place in places}) != len(places)):
        raise SafetyError("QUICKCOMMERCE_LOCATIONS_INVALID")
    return {"places": places, "recheck_days": value["recheck_days"]}


def load_settings(path=LOCATIONS):
    try:
        value = load_json(path, 16384)
    except (OSError, SafetyError):
        raise SafetyError("QUICKCOMMERCE_LOCATIONS_INVALID") from None
    return validate_settings(value)


def admission_hold(state, concept_id, *, indexes=None, thresholds=None):
    """None when paid checks may be planned for the type, else its reserved reason."""
    from .crowding import LEVELS, crowding_for
    from .fit_policy import FIT_POLICY_ID
    from .nearly_ready import passing_checklist
    from .research_flow import current_assessment_fit
    fit = current_assessment_fit(state, concept_id, expected_policy_id=FIT_POLICY_ID, indexes=indexes)
    if not passing_checklist(state, concept_id, fit):
        return "QUICKCOMMERCE_RESERVED_CHECKLIST"
    level = crowding_for(state, concept_id, thresholds=thresholds)["level"]
    if level == "high":
        return "QUICKCOMMERCE_RESERVED_CROWDING_HIGH"
    return None if level in LEVELS else "QUICKCOMMERCE_RESERVED_CROWDING_NOT_CHECKED"


def first_place_relevant(state, concept_id, first, *, as_of):
    """A first-place result on any of the three apps was reviewed as a relevant narrow-type match."""
    from .market_validation import _functional_match
    from .research_scope import validated_scoped_evidence
    from .source_interpretation import FUNCTIONAL_MATCH_METHOD
    # Only first-place rows this type's own quick-commerce questions returned
    # can show it; without any, the (costly) scoped-review read is skipped.
    rows = {ref for task in state["query_tasks"].values()
            if task.get("route") in PLATFORMS and concept_id in task.get("targets", {})
            for receipt in task.get("receipt_refs", [])
            for ref in state["source_checks"].get(receipt, {}).get("envelope", {}).get("observation_ids", [])
            if state["observations"].get(ref, {}).get("source") in PLATFORMS
            and state["observations"][ref].get("facts", {}).get("location") == first}
    if not rows:
        return False
    return any(row["method"] == FUNCTIONAL_MATCH_METHOD and row["route"] in PLATFORMS
               and row["scope"] == "narrow_type" and row["admissibility"] == "accepted"
               and any(ref["table"] == "observations" and ref["id"] in rows and _functional_match(row, ref["id"])
                       for ref in row["references"])
               for row in validated_scoped_evidence(state, concept_id, as_of=as_of))


def paid_question_hold(state, concept_ids, location, *, as_of, settings=None, cache=None):
    """Just before a paid call: None if any target type is still admitted for
    this place, else the first target's reserved reason (nothing is sent).
    ``cache`` keeps each type's answers for one source pass over one state."""
    settings = settings or load_settings()
    cache = {} if cache is None else cache
    first = settings["places"][0]
    if location not in settings["places"]:
        return "QUICKCOMMERCE_RESERVED_PLACE_NOT_CONFIGURED"
    reasons = []
    for concept_id in concept_ids:
        if ("admission", concept_id) not in cache:
            cache[("admission", concept_id)] = admission_hold(state, concept_id)
        reason = cache[("admission", concept_id)]
        if reason is None and location != first:
            if ("relevant", concept_id) not in cache:
                cache[("relevant", concept_id)] = first_place_relevant(state, concept_id, first, as_of=as_of)
            reason = None if cache[("relevant", concept_id)] else "QUICKCOMMERCE_RESERVED_FIRST_PLACE"
        if reason is None:
            return None
        reasons.append(reason)
    return reasons[0] if reasons else "QUICKCOMMERCE_RESERVED_CHECKLIST"


def issue_refresh_held(state, concept_id, route, *, cache):
    """True when an issue's support refresh of this row would be a paid check
    for a type no longer admitted (no refresh is requested); other routes never are."""
    if route not in PLATFORMS:
        return False
    if concept_id not in cache:
        cache[concept_id] = admission_hold(state, concept_id)
    return cache[concept_id] is not None


def main_phrase(profile):
    """The type's main reviewed quick-commerce phrase (subtype before family wording)."""
    phrases = [row for row in profile["queries"] if row["source"] == "quickcommerce" and row["scope"] != "context"]
    phrases.sort(key=lambda row: {"subtype": 0, "family": 1}[row["scope"]])
    if not phrases:
        raise SafetyError("PRODUCT_QUERY_PROFILE_REQUIRED")
    return phrases[0]


def answered_at(state, task):
    """When a saved question was answered (its last receipt), or None."""
    if task.get("status") not in RESULT_STATES or not task.get("receipt_refs"):
        return None
    return state["source_checks"].get(task["receipt_refs"][-1], {}).get("at")


def _settled(task, max_attempts):
    if task.get("reconcile_required") or task.get("status") == "dispatching":
        return False
    if task.get("status") in _ANSWERED:
        return True
    return task.get("status") in {"failed", "blocked"} and (
        not task.get("retryable") or task.get("tries", 0) >= max_attempts
        or task.get("prelaunch_failures", 0) >= max_attempts)


def next_question(context, profile, tasks, settings, *, run_id, as_of, relevant, max_attempts=2):
    """The type's next paid question on this app, or a reserved reason (raised).

    ``tasks`` holds every saved question for the type on this app, from any
    run or phrase, each with its answer time. A place is answered when this
    run's question for it is settled, or any question for it was answered
    within the recheck period. ``relevant`` answers (lazily) whether the
    first place showed a relevant result.
    """
    phrase = main_phrase(profile)
    now, window = timestamp(as_of), timedelta(days=settings["recheck_days"])
    rejoin = None
    for index, place in enumerate(settings["places"]):
        prepared = prepare_query(context["route"], phrase["text"], place)
        request = {"query": prepared["query"], "location": prepared["location"]}
        current = [task for task, _ in tasks if task.get("run_id") == run_id and task.get("request") == request]
        if current and not any(_settled(task, max_attempts) for task in current):
            return _planned(context, profile, phrase, request, index)  # still being asked: re-join it
        recent = any(task.get("request", {}).get("location") == place and at and now - timestamp(at) < window
                     for task, at in tasks)
        if current or recent:
            rejoin = (request, index) if current else rejoin
            continue
        if index and not relevant():
            raise SafetyError("QUICKCOMMERCE_RESERVED_FIRST_PLACE")
        return _planned(context, profile, phrase, request, index)
    if rejoin is None:
        # Every place was answered recently (perhaps in an earlier week or
        # under an earlier phrase): nothing new is asked before the recheck.
        raise SafetyError("QUICKCOMMERCE_RESERVED_RECENT_CHECK")
    return _planned(context, profile, phrase, *rejoin)


def _planned(context, profile, phrase, request, index):
    context["query_plan"] = {"profile_hash": digest(profile), "term_index": 1, "term_count": 1,
        "scope": phrase["scope"], "previous_task_ids": [], "previous_receipt_refs": [],
        "reason": ("Use the primary reviewed product-type phrase in the first reference place." if index == 0 else
                   "The first place showed a relevant reviewed result; check the next reference place.")}
    return deepcopy(request)
