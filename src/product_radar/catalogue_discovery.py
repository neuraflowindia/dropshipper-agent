"""Finite public discovery slices, separate from a frozen product-review pool.

The cycle freezes actual accepted query choices and list scopes once. It uses
the existing free collectors and A/B admission flow, never a paid Actor, and
does not mistake downloaded cards for new or report-ready products. A failed
route cannot consume the sibling routes' turn. Original attempts are replayed
before another external request; a crash with uncertain navigation stays
uncertain instead of silently launching a replacement.

The explicit v2 path renews those finite slices and retains independent
source questions. Unsupported routes are parked, never treated as completed.
"""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path

from .catalogue import alias_groups, load_catalogue, validate_catalogue
from .catalogue_expansion import effective_catalogue
from .discovery import normalise
from .models import SafetyError, digest, identifier, timestamp, utc_now
from .operations import preparation_step
from .research_memory import change, key
from .research_questions import physical_request_fingerprint
from .service import CATEGORIES
from .storage import Journal, atomic_json, load_json, safe_path
from .work_turns import active_work_turn, reserve_source_callback

VERSION = "catalogue-discovery-cycle-v1"
WAVE_VERSION = "discovery-wave-v2"
WAVE_CONTROL_VERSION = "discovery-wave-control-v2"
SEED_CYCLE_VERSION = "catalogue-discovery-seed-cycle-v1"
SEED_RETRY_SECONDS = 300
# Amazon India and Flipkart no longer produce candidates (owner decision D4):
# they keep running only as each type's crowding check.
FREE_ROUTES = ("meta-ads",)
# Saved discovery questions of these routes are closed, never read or interpreted.
RETIRED_ROUTES = frozenset(("amazon-search", "flipkart-search", "amazon-bestsellers", "amazon-movers-and-shakers"))
_CLOSED_ACTIONS = frozenset(("acquired", "empty", "retired"))
KNOWN_LOCAL_FAILURES = {"RUNNER_ALREADY_ACTIVE", "MARKETPLACE_BROWSER_NOT_INSTALLED", "NATIVE_PUBLIC_BROWSER_UNAVAILABLE"}


def _raise_shared_integrity(error):
    # The programme's actual shared memory/path failures are not a source
    # outage or permission to continue issuing reads against corrupt state.
    from .programme import INTEGRITY_FAILURES
    if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
        raise error


def _cycle_id(programme_id):
    return key("discovery-cycle", [VERSION, identifier(programme_id)])


def _binding(record):
    fields = ("kind", "id", "programme_id", "baseline_concepts", "catalogue_hash", "prepared_at", "planned_actions")
    if record.get("kind") == WAVE_VERSION:
        fields += ("wave_sequence", "previous_wave_id", "policy")
    return {field: record[field] for field in fields}


def _save(memory, record):
    old = memory.snapshot()["batch_runs"].get(record["id"])
    if old is not None and _binding(old) != _binding(record):
        raise SafetyError("DISCOVERY_CYCLE_BINDING_CHANGED")
    if old != record:
        memory.put("batch_runs", record["id"], record, previous=old,
            event_id=key("discovery-cycle-state", [record["id"], digest(old), digest(record)]))


def _callable(value, *, reconciliation=False):
    callback = value.get("reconcile" if reconciliation else "handler") if isinstance(value, dict) else None if reconciliation else value
    return callback if callable(callback) else None


def _retire_closed_routes(record, at):
    """D4: an unfinished saved Amazon/Flipkart discovery question (even one
    with saved cards awaiting interpretation, or a failed one awaiting a
    retry) closes as retired. Its cards are never turned into candidates and
    it is not unfinished work. Finished questions keep their history."""
    retired = []
    for planned in record["planned_actions"]:
        for question in planned["questions"]:
            old = record["actions"].get(question["id"], {})
            if question["route"] in RETIRED_ROUTES and old.get("state") not in _CLOSED_ACTIONS:
                record["actions"][question["id"]] = dict(old, state="retired", reason="DISCOVERY_ROUTE_RETIRED",
                    next_trigger="none", updated_at=at)
                retired.append(question["id"])
    return retired


def _source_request(route, *, phrase):
    """Freeze the installed Meta collector contract before any dispatch.

    Already saved cycles keep their exact older request through replay. Meta
    is the only discovery route (D4); any other route is refused.
    """
    if route not in FREE_ROUTES:
        raise SafetyError("DISCOVERY_ROUTE_RETIRED")
    return {"query": phrase, "country": "IN", "limit": 30}


def _recent_capture(state, route, request, at, cache_days):
    """Retain original dates; actual scoped receipts, not a global healthy flag."""
    wanted = physical_request_fingerprint(route, request)
    latest = None
    for capture in reversed(list(state["captures"].values())):
        ref = capture.get("reference")
        captured = ref.get("request") if isinstance(ref, dict) else None
        actual_route = ref.get("route") if isinstance(ref, dict) else None
        if (actual_route != route or not isinstance(captured, dict)
                or capture.get("mapping_status") not in {"usable", "partial", "empty"}):
            continue
        try:
            same_request = physical_request_fingerprint(route, captured) == wanted
        except SafetyError:
            # An unsupported old envelope proves neither the exact current
            # bounds nor a failed website. Retain it; do not invent equivalence.
            continue
        if not same_request:
            continue
        age = timestamp(at) - timestamp(capture["observed_at"])
        if timedelta(0) <= age and (cache_days is None or age <= timedelta(days=cache_days)):
            if cache_days is not None:
                return capture  # Preserve the legacy finite-cache reader.
            if latest is None or timestamp(capture["observed_at"]) > timestamp(latest["observed_at"]):
                latest = capture
    return latest


def _query_candidates(catalogue, state, at, handlers, cache_days):
    groups = alias_groups(catalogue)
    visits, busy, recent_choices = {}, set(), set()
    for cycle in state["batch_runs"].values():
        if cycle.get("kind") != VERSION:
            continue
        for planned in cycle["planned_actions"]:
            for question in planned.get("questions", []):
                action = cycle["actions"].get(question["id"], {})
                family_id = planned.get("family_id")
                if family_id not in groups:
                    continue
                group = groups[family_id]
                if action.get("state") in {"intent_saved", "execution_uncertain", "interpretation_pending"}:
                    busy.add(group)
                if action.get("dispatch_started") is True:
                    when = action.get("observed_at", cycle["prepared_at"])
                    visits[group] = max(visits.get(group, when), when)
                    if timedelta(0) <= timestamp(at) - timestamp(when) <= timedelta(days=cache_days):
                        request = question["request"]
                        recent_choices.add((group, normalise(request.get("query") or request.get("term") or "")))
    for outcome in state["query_outcomes"].values():
        family_id = outcome.get("family_id")
        if family_id in groups:
            when = outcome["observed_at"]
            group = groups[family_id]
            if outcome.get("status") == "pending":
                busy.add(group)
            else:
                visits[group] = max(visits.get(group, when), when)
                if timedelta(0) <= timestamp(at) - timestamp(when) <= timedelta(days=cache_days):
                    recent_choices.add((group, normalise(outcome.get("request", {}).get("term", ""))))
    selected_groups, candidates = set(), []
    for family in sorted(catalogue["families"], key=lambda row: (visits.get(groups[row["id"]], ""), row["id"])):
        group = groups[family["id"]]
        if family["disposition"] != "usable" or group in busy or group in selected_groups:
            continue
        phrase = next((row for row in family["phrases"] if row["purpose"] == "discovery"
                       and row["source"] in {"meta", "marketplace"}
                       and (group, normalise(row["text"])) not in recent_choices), None)
        if phrase is None:
            continue
        routes = [route for route in FREE_ROUTES if _callable(handlers.get(route)) is not None]
        questions = []
        for route in routes:
            request = _source_request(route, phrase=phrase["text"])
            if _recent_capture(state, route, request, at, cache_days) is not None:
                # An already acquired exact type is not an unexplored search.
                continue
            questions.append({"id": key("discovery-question", [route, request]), "route": route,
                              "request": request, "choice_source": phrase["source"],
                              "choice_purpose": phrase["purpose"], "language": phrase["language"]})
        if questions:
            candidates.append({"id": key("discovery-action", [family["id"], phrase["text"]]),
                "kind": "family_query", "family_id": family["id"], "category": family["category"],
                "parent_id": family["parent_id"], "family": family["label"], "phrase": phrase["text"],
                "questions": questions,
                "reason": "Use the accepted compact type on Meta, the one discovery route; Amazon and Flipkart only check each type's crowding."})
            selected_groups.add(group)
    return candidates


def prepare_discovery_cycle(memory, programme_id, catalogue, *, handlers, action_limit=4,
                            cache_days=7, as_of=None, baseline_concepts=None):
    """Freeze a finite public research slice; preparation performs no calls."""
    if (type(action_limit) is not int or not 1 <= action_limit <= 20
            or type(cache_days) is not int or not 0 <= cache_days <= 90
            or not isinstance(handlers, dict)):
        raise SafetyError("DISCOVERY_SLICE_INVALID")
    cycle_id = _cycle_id(programme_id)
    state = memory.snapshot()
    existing = state["batch_runs"].get(cycle_id)
    if existing is not None:
        return deepcopy(existing)
    if baseline_concepts is not None and (not isinstance(baseline_concepts, list)
            or len(set(baseline_concepts)) != len(baseline_concepts)
            or any(not isinstance(value, str) for value in baseline_concepts)):
        raise SafetyError("DISCOVERY_BASELINE_INVALID")
    for value in baseline_concepts or []:
        identifier(value)
    at = as_of or utc_now(); timestamp(at)
    catalogue = effective_catalogue(validate_catalogue(catalogue), state)
    candidates = _query_candidates(catalogue, state, at, handlers, cache_days)
    # Amazon department lists are crowding reads for types, not discovery (D4).
    lists = []
    # One list and one family lane each get an early turn, then rotate categories.
    selected, categories = [], set()
    pools = [lists, candidates]
    while any(pools) and len(selected) < action_limit:
        for pool in pools:
            if not pool or len(selected) >= action_limit:
                continue
            choice = next((row for row in pool if row["category"] not in categories), pool[0])
            pool.remove(choice); selected.append(choice); categories.add(choice["category"])
    record = {"kind": VERSION, "id": cycle_id, "programme_id": programme_id,
        "baseline_concepts": sorted(state["concepts"] if baseline_concepts is None else baseline_concepts), "catalogue_hash": digest(catalogue), "prepared_at": at,
        "planned_actions": selected, "actions": {}, "admission": {"state": "not_started"},
        "new_concept_ids": [], "status": "prepared", "updated_at": at,
        "paid_actor_calls": 0, "client_publication_attempted": False}
    _save(memory, record)
    return deepcopy(record)


def validate_discovery_wave(state, record):
    """Validate exact frozen question references, not permission to collect."""
    from .research_questions import validate_question_record
    if (not isinstance(record, dict) or record.get("kind") != WAVE_VERSION
            or type(record.get("wave_sequence")) is not int or record["wave_sequence"] < 1
            or record.get("id") != key("discovery-wave", [record.get("programme_id"), record["wave_sequence"]])
            or not isinstance(record.get("planned_actions"), list) or not record["planned_actions"]
            or len(record["planned_actions"]) > 20):
        raise SafetyError("DISCOVERY_WAVE_INVALID")
    # Waves saved before D4 also carry the old "include_lists" flag; it plans nothing now.
    if (not isinstance(record.get("policy"), dict)
            or set(record["policy"]) - {"include_lists"} != {"action_limit", "cache_days"}
            or type(record["policy"]["action_limit"]) is not int or not 1 <= record["policy"]["action_limit"] <= 20
            or len(record["planned_actions"]) > record["policy"]["action_limit"]
            or type(record["policy"].get("include_lists", False)) is not bool
            or type(record["policy"]["cache_days"]) is not int or not 1 <= record["policy"]["cache_days"] <= 90
            or not isinstance(record.get("catalogue_hash"), str)
            or not isinstance(record.get("baseline_concepts"), list)
            or any(not isinstance(cid, str) for cid in record["baseline_concepts"])
            or len(set(record["baseline_concepts"])) != len(record["baseline_concepts"])
            or not isinstance(record.get("actions"), dict)):
        raise SafetyError("DISCOVERY_WAVE_INVALID")
    identifier(record["programme_id"]); timestamp(record.get("prepared_at"))
    for cid in record["baseline_concepts"]:
        identifier(cid)
    expected_previous = None if record["wave_sequence"] == 1 else key(
        "discovery-wave", [record["programme_id"], record["wave_sequence"] - 1])
    if record.get("previous_wave_id") != expected_previous:
        raise SafetyError("DISCOVERY_WAVE_INVALID")
    try:
        binding = _binding(record)
    except KeyError:
        raise SafetyError("DISCOVERY_WAVE_INVALID") from None
    if record.get("binding_hash") != digest(binding):
        raise SafetyError("DISCOVERY_WAVE_BINDING_CHANGED")
    seen = set()
    for planned in record["planned_actions"]:
        if not isinstance(planned, dict):
            raise SafetyError("DISCOVERY_WAVE_INVALID")
        questions = planned.get("questions")
        if not isinstance(questions, list) or len(questions) != 1:
            raise SafetyError("DISCOVERY_WAVE_INVALID")
        question = questions[0]
        if not isinstance(question, dict):
            raise SafetyError("DISCOVERY_WAVE_INVALID")
        saved = validate_question_record(state["query_tasks"].get(question.get("id")))
        if (saved["id"] in seen or saved["binding_hash"] != question.get("question_binding_hash")
                or planned.get("id") != saved["id"] or planned.get("kind") != "independent_question"
                or saved["route"] != question.get("route") or saved["request"] != question.get("request")
                or saved["subject"].get("kind") != "catalogue_direction"
                or saved["subject"].get("family_id") != planned.get("family_id")):
            raise SafetyError("DISCOVERY_WAVE_BINDING_CHANGED")
        seen.add(saved["id"])
    if not set(record["actions"]) <= seen or any(not isinstance(action, dict) for action in record["actions"].values()):
        raise SafetyError("DISCOVERY_WAVE_INVALID")
    return record


def _wave_control(state, programme_id):
    control_id = key("discovery-wave-control", identifier(programme_id))
    saved = state["batch_runs"].get(control_id)
    if saved is None:
        return control_id, {"kind": WAVE_CONTROL_VERSION, "id": control_id, "programme_id": programme_id,
            "next_sequence": 1, "current_wave_id": None, "last_category": None}
    if (saved.get("kind") != WAVE_CONTROL_VERSION or saved.get("id") != control_id
            or saved.get("programme_id") != programme_id or type(saved.get("next_sequence")) is not int
            or saved["next_sequence"] < 2):
        raise SafetyError("DISCOVERY_WAVE_CONTROL_INVALID")
    current = validate_discovery_wave(state, state["batch_runs"].get(saved.get("current_wave_id")))
    if current["wave_sequence"] + 1 != saved["next_sequence"] or current["programme_id"] != programme_id:
        raise SafetyError("DISCOVERY_WAVE_CONTROL_INVALID")
    return control_id, deepcopy(saved)


def _wave_candidates(state, programme_id, catalogue, handlers, at, *, cache_days, location=None):
    """Pure source-specific choices; no family-wide busy/cooldown exclusion."""
    from .research_questions import _catalogue_direction_scope
    from .source_acquisitions import source_readiness_owner_scope
    current = effective_catalogue(validate_catalogue(catalogue), state)
    with source_readiness_owner_scope(state), _catalogue_direction_scope(state, catalogue, current):
        return _wave_candidates_in_scope(state, programme_id, catalogue, current, handlers, at,
            cache_days=cache_days, location=location)


def _wave_candidates_in_scope(state, programme_id, catalogue, current, handlers, at, *, cache_days, location=None):
    from .research_questions import _prepare, catalogue_direction_subject
    from .source_coverage import shared_source_hooks, source_readiness_hint
    candidates, waits, seen, gaps = [], [], set(), []
    families = [row for row in current["families"] if row["disposition"] == "usable"]
    specs = []  # Amazon department lists are crowding reads for types, not discovery (D4).
    for family in sorted(families, key=lambda row: row["id"]):
        for phrase in family["phrases"]:
            if phrase["source"] == "supplier":
                from .supplier_route_acquisition import ACTIVE_SEARCH_ROUTES
                from .supplier_search import supplier_request_for_state
                # A retired supplier route is never planned; its saved
                # questions/answers remain validatable history below.
                for route in ACTIVE_SEARCH_ROUTES:
                    if not shared_source_hooks(handlers.get(route))["prepare"]:
                        continue
                    specs.append((family, route, supplier_request_for_state(state, route, phrase["text"]),
                                  {"search_scope": "accepted_direction_supplier_leads"}, "sourcing"))
                continue
            if phrase["source"] == "trends":
                # A chart answers a type-demand question; it is not a set of
                # product cards and must not create a fabricated product.
                specs.append((family, "google-trends", {"query": phrase["text"], "country": phrase["country"]},
                              {"search_scope": "accepted_direction_demand_context"}, "validation"))
                continue
            if phrase["source"] not in {"meta", "marketplace", "quickcommerce"}:
                continue
            if phrase["source"] in {"meta", "marketplace"}:
                # Amazon and Flipkart searches are the types' crowding check (D4).
                routes = FREE_ROUTES if phrase["purpose"] == "discovery" or phrase["source"] == "meta" else ()
                for route in routes:
                    specs.append((family, route, _source_request(route, phrase=phrase["text"]),
                                  {"search_scope": "accepted_direction"}, phrase["purpose"]))
            for route in ("blinkit", "zepto", "instamart"):
                if location is None:
                    gaps.append({"family_id": family["id"], "route": route, "query": phrase["text"],
                                 "reason": "QUICKCOMMERCE_LOCATION_REQUIRED"})
                else:
                    specs.append((family, route, {"query": phrase["text"], "location": deepcopy(location)},
                                  {"search_scope": "accepted_direction_location_catalogue"}, phrase["purpose"]))
    for family, route, request, options, purpose in specs:
        hooks = shared_source_hooks(handlers.get(route))
        if hooks["prepare"]:
            try: request = hooks["prepare"]({"route": route, "request": request, "as_of": at})
            except SafetyError as error:
                _raise_shared_integrity(error)
                gaps.append({"family_id": family["id"], "route": route, "query": request.get("query"), "reason": str(error)})
                continue
        funded = request.get("transport") == "funded-http"
        # Subject/question validators apply saved admissions themselves. Pass
        # their original base, not a view whose admitted families already have
        # saved aliases merged into them: re-expanding that view falsely looks
        # like a changed accepted-family definition.
        spec = {"subject": catalogue_direction_subject(state, family["id"], catalogue=catalogue),
                "route": route, "purpose": purpose, "request": request, "question_options": options,
                "programme_id": programme_id, "collection_channel": "funded_provider" if funded else "direct_local",
                "incremental_collection_fee": "unknown" if funded or route == "meta-ads" else "known_zero"}
        question = _prepare(state, spec, at, catalogue)
        if question["id"] in seen:
            continue
        seen.add(question["id"])
        previous = state["query_tasks"].get(question["id"])
        if previous is not None:
            from .research_questions import validate_question_record
            validate_question_record(previous)
            progress = previous.get("progress", {})
            if progress.get("owner_wave_id") and progress.get("scheduling") != "settled":
                continue  # Its original wave owns unfinished interpretation/recovery.
        installed = handlers.get(route)
        shared = callable(hooks["resolve"])
        due_hint = None
        if shared and callable(hooks["due_hint"]):
            try:
                due_hint = source_readiness_hint(installed, "due_hint", {
                    "task_id": question["id"], "route": route,
                    "request": deepcopy(request), "as_of": at}, state=state, include_access_hint=False)
            except (SafetyError, OSError) as error:
                _raise_shared_integrity(error)
                # Keep this exact question for one bounded local inspection;
                # its resolver records the named per-request repair wait.
                # A broken owner/read hint must not erase healthy questions
                # or authorize a replacement physical request.
                due_hint = {"ready": False, "signal": {}}
            actual_wait = due_hint.get("signal", {}).get("wait")
            if actual_wait and not due_hint["ready"]:
                if due_hint.get("next_retry_at"):
                    waits.append(due_hint["next_retry_at"])
                continue  # Do not fill new waves with known waiting requests.
        # Metadata alone cannot suppress a newly useful installed question.
        latest = None
        if isinstance(installed, dict) and callable(installed.get("resolve_source")) and due_hint and due_hint.get("signal", {}).get("complete"):
            latest = {"observed_at": due_hint["signal"]["observed_at"]}
        refresh = None
        if latest is not None:
            due = timestamp(latest["observed_at"]) + timedelta(days=cache_days)
            if timestamp(at) < due:
                if previous is not None and previous.get("progress", {}).get("scheduling") == "settled":
                    waits.append(due.isoformat()); continue
                # Fresh bytes can answer a NEW semantic obligation locally;
                # their mere existence does not prove this direction checked.
            else:
                refresh = {"refresh_due_at": due.isoformat(),
                           "refresh_reason": "Useful due refresh of this accepted research direction or inspected list."}
        elif previous is not None and previous.get("progress", {}).get("owner_wave_id"):
            continue  # No proved complete original: do not manufacture a replacement.
        candidates.append({"spec": spec, "question": question, "family": family,
            "refresh": refresh, "shared": shared, "last_served": (previous or {}).get("progress", {}).get("last_served_turn", 0)})
    return current, candidates, waits, gaps


def _first_pass_settled(wave):
    return all(wave.get("actions", {}).get(planned["questions"][0]["id"], {}).get("state")
               for planned in wave["planned_actions"])


def prepare_next_discovery_wave(memory, programme_id, catalogue, *, handlers, action_limit=4,
                                cache_days=7, as_of=None, location=None):
    """Prepare a real nonempty finite wave, or retain a named saved wait."""
    if (type(action_limit) is not int or not 1 <= action_limit <= 20
            or type(cache_days) is not int or not 1 <= cache_days <= 90 or not isinstance(handlers, dict)):
        raise SafetyError("DISCOVERY_SLICE_INVALID")
    from .programme import programme_service_state
    from .research_questions import prepare_research_questions
    if not programme_service_state(memory)["enabled"]:
        raise SafetyError("PROGRAMME_OWNER_PAUSED")
    at = as_of or utc_now(); timestamp(at); identifier(programme_id)
    state = memory.snapshot(); control_id, control = _wave_control(state, programme_id)
    if control["current_wave_id"]:
        existing = validate_discovery_wave(state, state["batch_runs"][control["current_wave_id"]])
        if not _first_pass_settled(existing):
            return deepcopy(existing)
    current, candidates, waits, gaps = _wave_candidates(state, programme_id, catalogue, handlers, at,
        cache_days=cache_days, location=location)
    if not candidates:
        return {"kind": WAVE_VERSION, "state": "waiting", "wave_id": None,
                "next_retry_at": min(waits, key=timestamp) if waits else None,
                "next_trigger": "saved_retry_time" if waits else "saved_input_reply_or_connection_change", "preparation_gaps": gaps}
    categories = list(CATEGORIES)
    offset = (categories.index(control["last_category"]) + 1) % len(categories) if control["last_category"] in categories else 0
    categories = categories[offset:] + categories[:offset]
    candidates.sort(key=lambda row: (row["last_served"], categories.index(row["family"]["category"]),
                                    row["family"]["id"], row["question"]["route"], row["question"]["id"]))
    selected, used = [], set()
    while candidates and len(selected) < action_limit:
        choice = next((row for row in candidates if row["family"]["category"] not in used), candidates[0])
        candidates.remove(choice); selected.append(choice); used.add(choice["family"]["category"])
    prepared = prepare_research_questions(memory, [row["spec"] for row in selected], as_of=at, catalogue=catalogue)
    questions = {row["id"]: row for row in prepared["records"]}
    state = memory.snapshot(); old_control = state["batch_runs"].get(control_id)
    if _wave_control(state, programme_id)[1] != control:
        raise SafetyError("DISCOVERY_WAVE_CONTROL_CHANGED")
    sequence = control["next_sequence"]; wave_id = key("discovery-wave", [programme_id, sequence])
    planned = []
    for row in selected:
        question = questions[row["question"]["id"]]; family = row["family"]
        planned.append({"id": question["id"], "kind": "independent_question", "family_id": family["id"],
            "category": family["category"], "questions": [{"id": question["id"], "route": question["route"],
                "request": deepcopy(question["request"]), "question_binding_hash": question["binding_hash"],
                **(row["refresh"] or {})}],
            "reason": "An independent source-specific question about an accepted direction; not a failed-source fallback."})
    wave = {"kind": WAVE_VERSION, "id": wave_id, "programme_id": programme_id, "wave_sequence": sequence,
        "previous_wave_id": control["current_wave_id"], "catalogue_hash": digest(current), "prepared_at": at,
        "baseline_concepts": sorted(state["concepts"]), "planned_actions": planned, "actions": {},
        "policy": {"action_limit": action_limit, "cache_days": cache_days},
        "admission": {"state": "not_started"}, "new_concept_ids": [], "status": "prepared", "updated_at": at,
        "paid_actor_calls": 0, "client_publication_attempted": False}
    if gaps: wave["preparation_gaps"] = gaps
    wave["binding_hash"] = digest(_binding(wave))
    updated_control = dict(control, current_wave_id=wave_id, next_sequence=sequence + 1,
                           last_category=planned[-1]["category"])
    changes = [change("batch_runs", wave_id, wave), change("batch_runs", control_id, updated_control, old_control)]
    for question in questions.values():
        old = state["query_tasks"][question["id"]]
        updated = dict(old, progress={**old["progress"], "owner_wave_id": wave_id,
            "owner_programme_id": programme_id, "acquisition": "prepared", "interpretation": "not_started", "scheduling": "ready"})
        changes.append(change("query_tasks", question["id"], updated, old))
    def validate(current_state):
        if not programme_service_state(memory)["enabled"]:
            raise SafetyError("PROGRAMME_OWNER_PAUSED")
        if _wave_control(current_state, programme_id)[1] != control:
            raise SafetyError("DISCOVERY_WAVE_CONTROL_CHANGED")
        validate_discovery_wave(current_state, wave)
    memory.commit(key("prepare-discovery-wave", [wave_id, wave["binding_hash"]]), changes, validate=validate)
    return deepcopy(wave)


def _receipt_path(memory, record, question, *, acquisition_id=None):
    parts = ["discovery_cycles", record["id"], question["id"]]
    if record.get("kind") == WAVE_VERSION:
        original = acquisition_id or record.get("actions", {}).get(question["id"], {}).get("acquisition_id")
        if original is not None:
            parts.append(identifier(original))
    return safe_path(memory.root, *parts) / "receipt.json"


def _validate_receipt(memory, question, acquisition_id, receipt):
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
            or receipt.get("route") != question["route"] or receipt.get("acquisition_id") != acquisition_id
            or receipt.get("dispatch_started") is not None and type(receipt["dispatch_started"]) is not bool
            or receipt.get("state") not in {"usable", "partial", "empty", "blocked", "failed", "unavailable", "insufficient"}
            or not isinstance(receipt.get("capture_ids"), list) or not isinstance(receipt.get("observation_ids"), list)
            or not isinstance(receipt.get("reference"), str)):
        raise SafetyError("DISCOVERY_RECEIPT_INVALID")
    timestamp(receipt["observed_at"])
    reference = safe_path(Path(receipt["reference"]))
    if not reference.is_file():
        raise SafetyError("DISCOVERY_CAPTURE_REFERENCE_MISSING")
    state = memory.snapshot()
    for cid in receipt["capture_ids"]:
        capture = state["captures"].get(cid)
        if (capture is None or capture["acquisition_id"] != acquisition_id
                or capture["observed_at"] != receipt["observed_at"]):
            raise SafetyError("DISCOVERY_CAPTURE_BINDING_INVALID")
    if any(oid not in state["observations"] or state["observations"][oid]["capture_id"] not in receipt["capture_ids"]
           for oid in receipt["observation_ids"]):
        raise SafetyError("DISCOVERY_CAPTURE_BINDING_INVALID")
    term_capture = receipt.get("term_capture_ref")
    supplier_capture = receipt.get("supplier_lead_capture_ref")
    if question["route"] == "google-trends":
        from .trends_shared import validate_trends_term_receipt
        validate_trends_term_receipt(memory, question["request"], acquisition_id, receipt)
    elif term_capture is not None:
        raise SafetyError("DISCOVERY_CAPTURE_BINDING_INVALID")
    if supplier_capture is not None:
        from .supplier_search import validate_supplier_lead_receipt
        from .supplier_route_acquisition import SEARCH_ROUTES
        from .research_questions import validate_question_record
        original_question = validate_question_record(state["query_tasks"].get(question["id"]))
        if (question["route"] not in SEARCH_ROUTES or original_question["purpose"] != "sourcing"
                or original_question["binding_hash"] != question.get("question_binding_hash")
                or original_question["request"] != question["request"]):
            raise SafetyError("DISCOVERY_SUPPLIER_PURPOSE_INVALID")
        validate_supplier_lead_receipt(memory, question["request"], acquisition_id, receipt)
    if receipt["state"] in {"usable", "partial"} and not receipt["observation_ids"] and term_capture is None and supplier_capture is None:
        raise SafetyError("DISCOVERY_CAPTURE_BINDING_INVALID")
    return receipt


def _acquisition_progress(state, acquisition_ids, baseline):
    observations = {oid: row for oid, row in state["observations"].items() if row["acquisition_id"] in acquisition_ids}
    semantics = {row["semantic_id"] for row in observations.values()}
    described = {sid for sid in semantics if sid in state["descriptions"]}
    children = {key("child", [sid, index, digest(state["descriptions"][sid]["result"])])
                for sid in described for index in range(len(state["descriptions"][sid]["result"]["products"]))}
    associations = {cid: state["associations"][cid] for cid in children if cid in state["associations"]}
    resolved = {row["concept_id"] for row in associations.values() if row.get("status") == "resolved"}
    pending_children = children - {cid for cid, row in associations.items() if row.get("status") in {"resolved", "excluded", "deferred"}}
    deferred = sum(row.get("status") == "deferred" for row in associations.values())
    return {"captured_cards": len(observations), "unique_source_items": len(semantics),
        "description_pending": len(semantics - described), "identity_pending": len(pending_children),
        "identity_deferred": deferred, "resolved_concept_ids": sorted(resolved),
        "new_concept_ids": sorted(resolved - set(baseline)), "known_concept_ids": sorted(resolved & set(baseline)),
        "all_items_interpreted": semantics == described and not pending_children and not deferred}


def advance_discovery_cycle(memory, cycle_id, *, handlers, runner=None, max_actions=2,
                            flow_limit=10, admission=None, as_of=None, shared_marketplace=False,
                            question_ids=None):
    """Advance sibling questions, then reuse existing scoped product admission."""
    local_only = shared_marketplace is True and question_ids is not None and max_actions == 0
    if (type(max_actions) is not int or not (1 <= max_actions <= 20 or local_only)
            or type(flow_limit) is not int or not 1 <= flow_limit <= 50):
        raise SafetyError("DISCOVERY_SLICE_INVALID")
    identifier(cycle_id); at = as_of or utc_now(); timestamp(at)
    if type(shared_marketplace) is not bool:
        raise SafetyError("SOURCE_MARKETPLACE_SHARED_OPTION_INVALID")
    from .fit_policy import FIT_POLICY_ID
    from .research_flow import run_flow
    admission = admission or run_flow
    with Journal(safe_path(memory.root, "discovery_cycles", cycle_id, "writer")).locked(timeout=5):
        initial_state = memory.snapshot()
        record = deepcopy(initial_state["batch_runs"].get(cycle_id))
        if record is None or record.get("kind") not in {VERSION, WAVE_VERSION}:
            raise SafetyError("DISCOVERY_CYCLE_REQUIRED")
        wave = record["kind"] == WAVE_VERSION
        if local_only and not wave:
            raise SafetyError("DISCOVERY_SLICE_INVALID")
        if wave:
            validate_discovery_wave(initial_state, record)
            if active_work_turn(memory, state=initial_state) is None or not shared_marketplace:
                raise SafetyError("DISCOVERY_WAVE_TURN_REQUIRED")
        elif question_ids is not None:
            raise SafetyError("DISCOVERY_SLICE_INVALID")
        if question_ids is not None and (not isinstance(question_ids, list)
                or any(not isinstance(value, str) for value in question_ids)
                or not set(question_ids) <= {q["id"] for row in record["planned_actions"] for q in row["questions"]}):
            raise SafetyError("DISCOVERY_SLICE_INVALID")
        retired = _retire_closed_routes(record, at)
        calls, failures, visited_routes = 0, [], set()
        for planned in record["planned_actions"]:
            if calls >= max_actions and not shared_marketplace:
                break
            for question in planned["questions"]:
                if question_ids is not None and question["id"] not in question_ids:
                    continue
                old = record["actions"].get(question["id"], {})
                if old.get("state") == "retired":
                    continue
                if old.get("state") in {"acquired", "interpretation_pending", "empty"} and not old.get("supplier_original_pending"):
                    break  # Only a failed route merits its saved fallback.
                if old.get("state") in {"failed", "blocked", "insufficient"} and not wave:
                    continue
                if calls >= max_actions and not shared_marketplace:
                    break
                route = question["route"]
                installed = handlers.get(route)
                from .source_coverage import shared_source_hooks, shared_source_caller_fields
                hooks = shared_source_hooks(installed)
                if wave and not (callable(hooks["resolve"]) and callable(hooks["admission"])):
                    record["actions"][question["id"]] = dict(old, state="not_connected", dispatch_started=False,
                        reason="Shared direct collection is not connected for this source.",
                        next_trigger="source_connection_change", updated_at=at)
                    _save(memory, record)
                    continue
                if route in visited_routes and old.get("state") not in {"intent_saved", "execution_uncertain"}:
                    continue
                saved = _receipt_path(memory, record, question)
                uncertain = old.get("state") in {"intent_saved", "execution_uncertain"}
                callback = _callable(handlers.get(route), reconciliation=uncertain)
                if callback is None and not saved.exists():
                    if uncertain:
                        # Missing recovery support cannot undo the original
                        # dispatch or turn an unknown outcome into a new launch.
                        record["actions"][question["id"]] = dict(old, updated_at=at,
                            reason="The original search is awaiting its saved result or recovery connection.")
                        failures.append({"question_id": question["id"], "route": route,
                                         "reason": "DISCOVERY_ORIGINAL_RECOVERY_REQUIRED"})
                        break  # An uncertain original is not a failed fallback route.
                    record["actions"][question["id"]] = dict(old, state="not_connected", dispatch_started=False,
                        reason="This original free route is not connected.")
                    continue
                acquisition_id = key("discovery-acquisition", [record["id"], question["id"]])
                resolution = None
                resolver = hooks["resolve"] if shared_marketplace else None
                if resolver:
                    try:
                        resolution = resolver({"task_id": question["id"], "route": route,
                            "acquisition_id": old.get("acquisition_id", acquisition_id),
                            "request": deepcopy(question["request"]), "concept_ids": [], "description_hashes": {},
                            "reconciling": uncertain, "as_of": at,
                            **{name: question[name] for name in ("refresh_due_at", "refresh_reason") if name in question}})
                    except (SafetyError, OSError) as error:
                        if wave:
                            _raise_shared_integrity(error)
                        reason = str(error) if isinstance(error, SafetyError) else "SOURCE_MARKETPLACE_LOCAL_PROOF_PENDING"
                        failures.append({"question_id": question["id"], "route": route, "reason": reason})
                        record["actions"][question["id"]] = dict(old, updated_at=at, reason=reason)
                        if wave:
                            record["actions"][question["id"]].update(state="waiting", next_trigger="saved_input_reply_or_connection_change")
                        break
                    if resolution["action"] in {"wait", "not_connected"}:
                        # A closed supplier question (replaced transport or retired
                        # route) sends nothing; its direction waits for a new one.
                        record["actions"][question["id"]] = dict(old, updated_at=at,
                            reason=resolution["reason"], acquisition_owner_ref=resolution.get("acquisition_id"))
                        if wave:
                            record["actions"][question["id"]].update(state="waiting",
                                next_retry_at=resolution.get("next_retry_at"), next_trigger="saved_input_reply_or_connection_change")
                        break
                    acquisition_id = resolution["acquisition_id"]
                    if wave:
                        # An eligible due access probe may be a new physical
                        # generation under the SAME semantic question. Its
                        # result must not read/overwrite the old generation's
                        # caller receipt merely because the question ID fits.
                        saved = _receipt_path(memory, record, question, acquisition_id=acquisition_id)
                    if resolution["action"] == "collect":
                        callback = _callable(handlers.get(route))
                        uncertain = False
                local_saved = bool(resolution and resolution["action"] == "saved")
                if not local_saved and not saved.exists() and calls >= max_actions:
                    continue
                same_origin = not wave or old.get("acquisition_id") == acquisition_id
                action = dict(old, state="intent_saved", dispatch_started=old.get("dispatch_started") if same_origin else None,
                    acquisition_id=acquisition_id, updated_at=at)
                if not same_origin:
                    for field in ("reused_acquisition", "observed_at", "capture_ids", "observation_ids", "reference", "original_reference", "term_capture_ref", "supplier_lead_capture_ref", "supplier_results_hash", "supplier_original_pending"):
                        action.pop(field, None)
                probe = {"changes": [], "caller_fields": {}, "context_fields": {}}
                if resolution and resolution["action"] == "collect" and hooks["admission"]:
                    try:
                        probe = hooks["admission"]({"task_id": question["id"], "as_of": at}, resolution)
                    except SafetyError as error:
                        if not wave:
                            raise
                        _raise_shared_integrity(error)
                        record["actions"][question["id"]] = dict(old, state="waiting", reason=str(error), updated_at=at,
                            next_trigger="saved_input_reply_or_connection_change")
                        _save(memory, record); continue
                if resolution:
                    action.update(acquisition_owner_ref=acquisition_id,
                                  **shared_source_caller_fields(installed, resolution["request"]))
                    if local_saved:
                        action["reused_acquisition"] = True
                    action.update(probe["caller_fields"])
                previous = deepcopy(record)
                record["actions"][question["id"]] = action
                turn = active_work_turn(memory)
                if turn is not None and not saved.exists() and not local_saved:
                    original = memory.snapshot()["batch_runs"][record["id"]]
                    owner = key("discovery-service", [turn["id"], turn["counters"]["source_callbacks"],
                                                      record["id"], question["id"], acquisition_id])
                    try:
                        reserve_source_callback(memory, owner, at=at, lane="backlog" if wave and old else "exploration",
                                                changes=[change("batch_runs", record["id"], record, original), *probe["changes"]])
                    except SafetyError as error:
                        record = previous
                        if str(error) not in {"WORK_TURN_SOURCE_LIMIT", "WORK_TURN_CLOSED"}:
                            raise
                        failures.append({"question_id": question["id"], "stage": "turn_admission", "reason": str(error)})
                        continue
                else:
                    if probe["changes"]:
                        original = memory.snapshot()["batch_runs"][record["id"]]
                        joined = [change("batch_runs", record["id"], record, original), *probe["changes"]]
                        memory.commit(key("discovery-access-admission", joined), joined)
                    else:
                        _save(memory, record)
                def dispatched():
                    action.update(dispatch_started=True, observed_at=at)
                    _save(memory, record)
                context = {"task_id": question["id"], "route": route, "acquisition_id": acquisition_id,
                    "request": deepcopy(resolution["request"] if resolution else question["request"]), "concept_ids": [], "description_hashes": {},
                    "timeout_seconds": 45, "record_dispatch": dispatched, "reconciling": uncertain, "as_of": at}
                if resolution:
                    context.update(marketplace_admitted=not local_saved, shared_marketplace=True,
                                   marketplace_caller={"table": "batch_runs", "id": record["id"]})
                    if isinstance(installed, dict) and callable(installed.get("resolve_source")):
                        context.update(source_admitted=not local_saved,
                                       source_caller={"table": "batch_runs", "id": record["id"]})
                    context.update(probe["context_fields"])
                try:
                    if local_saved:
                        receipt = _validate_receipt(memory, question, acquisition_id, resolution["receipt"])
                        atomic_json(saved, receipt)
                    elif saved.exists():
                        receipt = _validate_receipt(memory, question, acquisition_id, load_json(saved))
                    else:
                        visited_routes.add(route); calls += 1
                        receipt = _validate_receipt(memory, question, acquisition_id, callback(context))
                        atomic_json(saved, receipt)
                    actual = receipt["dispatch_started"]
                    if action.get("dispatch_started") is True and actual is not True:
                        raise SafetyError("DISCOVERY_DISPATCH_BINDING_CHANGED")
                    action.update(state="acquired" if receipt["state"] in {"usable", "partial"} and receipt.get("supplier_lead_capture_ref")
                        else "acquired" if receipt["state"] == "usable" and receipt.get("term_capture_ref")
                        else "interpretation_pending" if receipt["state"] in {"usable", "partial"}
                        else "empty" if receipt["state"] == "empty" else "execution_uncertain" if actual is None
                        else "failed" if receipt["state"] == "unavailable" else receipt["state"],
                        dispatch_started=actual, observed_at=receipt["observed_at"],
                        capture_ids=receipt["capture_ids"], observation_ids=receipt["observation_ids"],
                        reference=str(saved), original_reference=receipt["reference"], reason=receipt["reason"])
                    if receipt.get("term_capture_ref") is not None:
                        action["term_capture_ref"] = deepcopy(receipt["term_capture_ref"])
                    if receipt.get("supplier_lead_capture_ref") is not None:
                        action["supplier_lead_capture_ref"] = deepcopy(receipt["supplier_lead_capture_ref"])
                        action["supplier_results_hash"] = receipt["supplier_results_hash"]
                        action["supplier_original_pending"] = receipt["supplier_original_pending"]
                except Exception as error:
                    if getattr(error, "calm_wait", False) is True and action.get("dispatch_started") is not True:
                        # A signed-in read that never started (the owner's sign-in, the
                        # desktop, the one browser or pacing): the question waits, as for
                        # a resolver's calm wait, never an uncertain dispatch.
                        record["actions"][question["id"]] = dict(old, updated_at=at, reason=str(error),
                            acquisition_owner_ref=action.get("acquisition_owner_ref"))
                        if wave:
                            record["actions"][question["id"]].update(state="waiting",
                                next_retry_at=getattr(error, "next_retry_at", None),
                                next_trigger="saved_input_reply_or_connection_change")
                        _save(memory, record)
                        break
                    if wave:
                        _raise_shared_integrity(error)
                    reason = str(error) if isinstance(error, SafetyError) else "DISCOVERY_ORIGINAL_CAPTURE_PENDING" if isinstance(error, FileNotFoundError) else "DISCOVERY_ACQUISITION_FAILED"
                    actual = False if reason in KNOWN_LOCAL_FAILURES and action.get("dispatch_started") is not True else action.get("dispatch_started")
                    action.update(state="local_retry_pending" if actual is False else "execution_uncertain",
                                  dispatch_started=actual, reason=reason)
                    failures.append({"question_id": question["id"], "route": route, "reason": reason})
                _save(memory, record)
                if action["state"] in {"interpretation_pending", "empty", "execution_uncertain", "local_retry_pending"}:
                    break
                # Give healthy sibling families a turn before another route.
                if calls >= max_actions and not shared_marketplace:
                    break
        acquisitions = sorted({row["acquisition_id"] for row in record["actions"].values()
                               if row.get("state") in {"acquired", "interpretation_pending"}})
        admission_state = memory.snapshot()
        before_admission = _acquisition_progress(admission_state, acquisitions, record["baseline_concepts"])
        if acquisitions and not before_admission["all_items_interpreted"]:
            try:
                # A larger explicitly configured wave must not repeatedly
                # spend its interpretation slice on its already finished
                # first ten acquisitions, leaving the remainder unreachable.
                pending = ([aid for aid in acquisitions
                            if not _acquisition_progress(admission_state, [aid], record["baseline_concepts"])["all_items_interpreted"]]
                           if wave else acquisitions)
                with preparation_step("discovery_saved_answers", counts={"saved_searches": len(pending[:10])}):
                    admitted = admission(memory, runner, acquisition_ids=pending[:10], limit=flow_limit, as_of=at, fit_policy_id=FIT_POLICY_ID)
                record["admission"] = {"state": "advanced", "updated_at": at}
                waits = admitted.get("preparation_waits", []) if isinstance(admitted, dict) else []
                if waits:
                    if (not isinstance(waits, list) or len(waits) > 9 or any(
                            not isinstance(row, dict) or set(row) != {"stage", "reason", "item_count"}
                            or row["stage"] not in ("description", "identity", "assessment")
                            or row["reason"] not in ("WORK_TURN_CLOSED", "WORK_TURN_MODEL_LIMIT", "WORK_TURN_PURPOSE_LIMIT")
                            or type(row["item_count"]) is not int or row["item_count"] < 1 for row in waits)):
                        raise SafetyError("DISCOVERY_ADMISSION_RESULT_INVALID")
                    record["admission"].update(state="advanced_with_waits", preparation_waits=deepcopy(waits))
                    from .operations import record_event
                    record_event("preparation_deferred", state="waiting", reason=waits[0]["reason"],
                                 counts={"waiting_inputs": sum(row["item_count"] for row in waits)})
            except Exception as error:
                if wave:
                    _raise_shared_integrity(error)
                reason = str(error) if isinstance(error, SafetyError) else "DISCOVERY_PRODUCT_ADMISSION_FAILED"
                record["admission"] = {"state": "waiting", "reason": reason, "updated_at": at}
                failures.append({"stage": "admission", "reason": reason})
        progress = _acquisition_progress(memory.snapshot(), acquisitions, record["baseline_concepts"])
        record["new_concept_ids"] = progress["new_concept_ids"]
        record["updated_at"] = at
        if progress["all_items_interpreted"]:
            for action in record["actions"].values():
                if action.get("state") == "interpretation_pending":
                    action["state"] = "acquired"
        unfinished = sum(not any(record["actions"].get(q["id"], {}).get("state") in {"acquired", "empty"} for q in row["questions"])
                         and not all(record["actions"].get(q["id"], {}).get("state") in {"failed", "blocked", "insufficient", "retired"}
                                     for q in row["questions"])
                         for row in record["planned_actions"])
        record["status"] = "checked" if not unfinished and progress["all_items_interpreted"] else "work_remaining"
        record["progress"] = progress; _save(memory, record)
        if wave:
            _save_wave_question_progress(memory, record, handlers, at,
                                         served=None if question_ids is None else question_ids + retired)
        return {"state": record["status"],
            **({"wave_id": cycle_id, "wave_sequence": record["wave_sequence"]} if wave else {"cycle_id": cycle_id}),
            "planned_actions": len(record["planned_actions"]),
            "actual_dispatches": sum(row.get("dispatch_started") is True and not row.get("reused_acquisition")
                                     for row in record["actions"].values()),
            "external_handler_calls_this_round": calls, "pending_actions": unfinished,
            "failure_count_this_round": len(failures), "failures": failures, **progress,
            "paid_actor_calls": 0, "original_product_pool_changed": False,
            "new_products_need_full_source_review": progress["new_concept_ids"],
            "note": "Newly admitted product identities are separate from the frozen review run. Cards, identities and report-ready products are different counts."}


def _wave_question_hint(handlers, question, at, *, state=None):
    from .source_coverage import shared_source_hooks, source_readiness_hint
    installed = handlers.get(question["route"])
    hooks = shared_source_hooks(installed)
    result = {"connected": callable(hooks["resolve"]) and callable(hooks["admission"])}
    context = {"task_id": question["id"], "route": question["route"], "request": deepcopy(question["request"]),
               "as_of": at, **{name: question[name] for name in ("refresh_due_at", "refresh_reason") if name in question}}
    if isinstance(installed, dict):
        for hook, name in (("saved_hint", "original"), ("due_hint", "access")):
            if callable(hooks[hook]):
                try:
                    result[name] = source_readiness_hint(installed, hook, context, state=state,
                                                        include_access_hint=False)
                except (SafetyError, OSError) as error:
                    _raise_shared_integrity(error)
                    result[name] = {"ready": False, "reason": str(error) if isinstance(error, SafetyError)
                                    else "SOURCE_SAVED_HINT_READ_FAILED"}
    return result


def _save_wave_question_progress(memory, wave, handlers, at, *, served=None):
    """One bounded commit updates actual obligations, never invented attempts."""
    state = memory.snapshot(); validate_discovery_wave(state, wave)
    turn = active_work_turn(memory); changes = []
    served = set(served) if served is not None else {row["questions"][0]["id"] for row in wave["planned_actions"]}
    mapping = {"acquired": ("parsed", "accepted", "settled"), "empty": ("completed_empty", "not_started", "settled"),
        "interpretation_pending": ("parsed", "pending", "parked"),
        "retired": ("retired", "not_started", "settled"),
        "execution_uncertain": ("execution_unknown", "not_started", "parked"),
        "intent_saved": ("execution_unknown", "not_started", "parked"),
        "failed": ("failed_access", "not_started", "parked"), "blocked": ("failed_access", "not_started", "parked")}
    for planned in wave["planned_actions"]:
        question = planned["questions"][0]; qid = question["id"]
        if qid not in served:
            continue
        action = wave["actions"].get(qid, {})
        if not action.get("state"):
            continue
        old = state["query_tasks"][qid]
        if old.get("progress", {}).get("owner_wave_id") != wave["id"]:
            continue  # A newer due generation owns this logical question now.
        acquisition, interpretation, scheduling = mapping.get(action["state"], ("prepared", "not_started", "parked"))
        if action.get("supplier_original_pending"):
            acquisition, scheduling = "response_saved", "parked"
        hint = _wave_question_hint(handlers, question, at, state=state)
        progress = {**old["progress"], "acquisition": acquisition, "interpretation": interpretation,
            "scheduling": scheduling, "acquisition_owner_ref": action.get("acquisition_owner_ref", action.get("acquisition_id")),
            "reason": action.get("reason"), "next_retry_at": action.get("next_retry_at"),
            "next_trigger": action.get("next_trigger", "saved_input_reply_or_connection_change"),
            "saved_wake_hint": hint, "last_served_turn": turn["turn_sequence"] if turn else 0}
        if "supplier_original_pending" in action:
            progress["supplier_original_pending"] = action["supplier_original_pending"]
        updated = dict(old, progress=progress)
        if updated != old:
            changes.append(change("query_tasks", qid, updated, old))
    if changes:
        memory.commit(key("discovery-wave-question-progress", [wave["id"], changes]), changes)


def discovery_wave_readiness(memory, programme_id, catalogue, *, handlers, as_of,
                             cache_days=7, runner_available=False, state=None, location=None):
    """Pure bounded work selection; unchanged waits create no empty waves."""
    state = memory.snapshot() if state is None else state
    from .source_acquisitions import source_readiness_owner_scope
    with source_readiness_owner_scope(state):
        return _discovery_wave_readiness(memory, programme_id, catalogue, handlers=handlers, as_of=as_of,
            cache_days=cache_days, runner_available=runner_available,
            state=state, location=location)


def _discovery_wave_readiness(memory, programme_id, catalogue, *, handlers, as_of,
                              cache_days, runner_available, state, location):
    _, control = _wave_control(state, programme_id)
    work, waits, projection, signals = [], [], {}, []
    current = state["batch_runs"].get(control["current_wave_id"]) if control["current_wave_id"] else None
    owned = [row for row in state["query_tasks"].values() if row.get("kind") == "research-question-v2"
             and row.get("progress", {}).get("owner_programme_id") == programme_id]
    for question in owned:
        progress = question["progress"]; wave_id = progress["owner_wave_id"]
        wave = validate_discovery_wave(state, state["batch_runs"].get(wave_id))
        ref = next(row["questions"][0] for row in wave["planned_actions"] if row["questions"][0]["id"] == question["id"])
        action = wave.get("actions", {}).get(question["id"], {})
        projection.setdefault(wave_id, {"questions": {}, "progress": wave.get("progress", {})})["questions"][question["id"]] = {
            name: action.get(name) for name in ("state", "dispatch_started", "acquisition_id", "capture_ids", "observation_ids")}
        if progress.get("scheduling") == "settled":
            continue
        hint = _wave_question_hint(handlers, ref, as_of, state=state)
        signals.append([question["id"], hint])
        retry = progress.get("next_retry_at") or hint.get("access", {}).get("next_retry_at")
        if retry and timestamp(retry) > timestamp(as_of):
            waits.append(retry)
        # Closed locally on the next advance (D4): no read, interpretation or wait.
        retiring = ref["route"] in RETIRED_ROUTES and action.get("state") not in _CLOSED_ACTIONS
        never_served = not action.get("state") and not retiring
        interpreted = action.get("state") == "interpretation_pending"
        changed = hint != progress.get("saved_wake_hint")
        due = bool(retry and timestamp(retry) <= timestamp(as_of))
        # A changed arrival is merely permission to inspect the original;
        # resolver/leaf still own evidence, currentness and launch admission.
        ready = never_served or interpreted and runner_available or changed or due or retiring
        if ready:
            work.append({"wave_id": wave_id, "question_id": question["id"],
                         "lane": "exploration" if never_served else "backlog",
                         "callback": bool(hint["connected"] and not interpreted and not retiring
                             and not (hint.get("access", {}).get("local_saved") and not ref.get("refresh_due_at"))),
                         "last_served": progress.get("last_served_turn", 0)})
    work.sort(key=lambda row: (row["last_served"], row["question_id"]))
    current_catalogue, candidates, new_waits, gaps = _wave_candidates(state, programme_id, catalogue, handlers, as_of,
        cache_days=cache_days, location=location)
    waits.extend(new_waits)
    new_wave = bool((current is None or _first_pass_settled(current)) and candidates)
    # A different accepted catalogue or newly due question is a real wake
    # trigger even if another lane was already ready at the last checkpoint.
    # No clock timestamp or retry-only diagnostic is included here.
    signals.append(["independent_candidates", digest(current_catalogue),
                    [row["question"]["id"] for row in candidates] if new_wave else []])
    if gaps: signals.append(["source_preparation_gaps", gaps])
    return {"work": work, "new_wave_ready": new_wave,
        "callback_ready": any(row["callback"] and row["lane"] == "exploration" for row in work)
            or new_wave and any(row["shared"] for row in candidates),
        "backlog_ready": any(row["callback"] and row["lane"] == "backlog" for row in work),
        "local_ready": any(not row["callback"] for row in work) or new_wave and not any(row["shared"] for row in candidates),
        "projection": projection, "signal": signals, "preparation_gaps": gaps,
        "next_retry_at": min(waits, key=timestamp) if waits else None}


def advance_discovery_waves(memory, programme_id, catalogue, *, handlers, runner=None, max_actions=2,
                            flow_limit=10, admission=None, action_limit=4, cache_days=7, as_of=None, location=None):
    """Serve finite old work and independent exploration under the same turn."""
    turn = active_work_turn(memory)
    if turn is None:
        raise SafetyError("DISCOVERY_WAVE_TURN_REQUIRED")
    if type(max_actions) is not int or not 1 <= max_actions <= 20:
        raise SafetyError("DISCOVERY_SLICE_INVALID")
    at = as_of or utc_now(); timestamp(at)
    ready = discovery_wave_readiness(memory, programme_id, catalogue, handlers=handlers, as_of=at,
        # This callback already owns a real ready turn. Even without a live
        # runner, Jobs may apply exact saved replies; readiness outside the
        # callback separately avoids advertising new AI during a model wait.
        cache_days=cache_days, runner_available=True, location=location)
    available = ready["work"]
    if ready["new_wave_ready"]:
        wave = prepare_next_discovery_wave(memory, programme_id, catalogue, handlers=handlers,
            action_limit=action_limit, cache_days=cache_days, as_of=at, location=location)
        if wave.get("id"):
            fresh = []
            # Preparation committed real question records. Share this new
            # verified boundary, not the pre-commit readiness snapshot.
            wave_state = memory.snapshot()
            for row in wave["planned_actions"]:
                question = row["questions"][0]; hint = _wave_question_hint(handlers, question, at, state=wave_state)
                fresh.append({"wave_id": wave["id"], "question_id": question["id"], "lane": "exploration",
                    "callback": bool(hint["connected"] and not (hint.get("access", {}).get("local_saved")
                                                                and not question.get("refresh_due_at")))})
            available = [row for row in available if row["lane"] == "backlog"] + fresh
    # max_actions remains the PHYSICAL callback allowance. At most two local
    # rows (one old interpretation and one saved/unconnected exploration row
    # when both exist) accompany it without spending a protected source slot.
    local = [row for row in available if not row["callback"]]
    old_local = next((row for row in local if row["lane"] == "backlog"), None)
    new_local = next((row for row in local if row["lane"] == "exploration"), None)
    selected_local = [row for row in (old_local, new_local) if row is not None]
    selected_local += [row for row in local if row not in selected_local][:2 - len(selected_local)]
    callbacks = [row for row in available if row["callback"]]
    lanes = {lane: [row for row in callbacks if row["lane"] == lane] for lane in ("backlog", "exploration")}
    if all(lanes.values()):
        if max_actions == 1:
            preferred = "exploration" if turn["programme_turn_sequence"] % 2 else "backlog"
            selected = lanes[preferred][:1]
        else:
            first = [lanes["backlog"][0], lanes["exploration"][0]]
            used = {(row["wave_id"], row["question_id"]) for row in first}
            remainder = [row for row in callbacks if (row["wave_id"], row["question_id"]) not in used]
            # Fresh independent source work uses its reserved allowance before
            # a second pending interpretation row. No original is replaced.
            remainder.sort(key=lambda row: row["lane"] != "exploration")
            selected = first + remainder[:max_actions - 2]
    else:
        selected = callbacks[:max_actions]
    selected = selected_local + selected
    if not selected:
        return {"state": "waiting", "wave_ids": [], "new_concept_ids": [], "new_products_need_full_source_review": [],
            "next_retry_at": ready["next_retry_at"], "paid_actor_calls": 0, "external_handler_calls_this_round": 0,
            "original_product_pool_changed": False}
    groups = {}
    for row in selected:
        groups.setdefault(row["wave_id"], []).append(row["question_id"])
    results = []
    remaining = max_actions
    for wave_id, question_ids in groups.items():
        result = advance_discovery_cycle(memory, wave_id, handlers=handlers, runner=runner,
            max_actions=remaining, flow_limit=flow_limit, admission=admission, as_of=at,
            shared_marketplace=True, question_ids=question_ids)
        results.append(result); remaining -= result["external_handler_calls_this_round"]
    new = sorted({cid for result in results for cid in result["new_concept_ids"]})
    return {"state": "work_remaining", "wave_ids": list(groups), "waves": results,
        "new_concept_ids": new, "new_products_need_full_source_review": new,
        "external_handler_calls_this_round": sum(row["external_handler_calls_this_round"] for row in results),
        "paid_actor_calls": 0, "original_product_pool_changed": False}


def _seed_cycle_id(programme_id):
    return key("discovery-seed-cycle", [SEED_CYCLE_VERSION, identifier(programme_id)])


def catalogue_seed_wake_hint(memory, state, programme_id, manifest, *, as_of):
    """Pure exact-file/deadline hints for the ORIGINAL frozen seed work.

    This never reads public sites, changes the manifest, admits source facts
    or starts a model. A changed hint permits one normal bounded inspection;
    the existing loader/Jobs path still validates and owns the actual work.
    """
    from .catalogue_seed import MAX_SAVED_BYTES
    from .research_ai import current_attempt
    at = timestamp(as_of)
    if manifest is None:
        return {"ready": False, "next_retry_at": None, "signal": None, "branch_hints": {}}
    cycle = state["batch_runs"].get(_seed_cycle_id(programme_id))
    preparations = safe_path(memory.root, "catalogue_materials", "preparations")
    files, deadlines, waits, ready, branch_hints = {}, [], [], cycle is None, {}
    branch_paths = None

    def owned(reference, root, code):
        path = safe_path(Path(reference))
        path = path.resolve()
        if not path.is_relative_to(root.resolve()):
            raise SafetyError(code)
        return path

    def watch(path):
        safe_path(path)
        if branch_paths is not None:
            branch_paths.add(str(path))
        try:
            info = path.stat()
            files[str(path)] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        except FileNotFoundError:
            files[str(path)] = ["missing"]
        except OSError as error:
            files[str(path)] = ["local_io", error.errno]

    original_manifest = cycle.get("manifest_reference") if cycle else manifest
    if isinstance(original_manifest, (str, Path)) and original_manifest != "supplied-manifest-mapping":
        manifest_path = owned(original_manifest, preparations, "DISCOVERY_SEED_MANIFEST_OUTSIDE_RESEARCH")
        watch(manifest_path)
        # A missing/unreadable whole manifest cannot start a cycle. Its file
        # state is still in the signal, so the file appearing wakes the lane.
        ready = ready and files[str(manifest_path)][0] not in ("missing", "local_io")
    if cycle is None:
        return {"ready": ready, "next_retry_at": None,
                "signal": {"manifest": digest(manifest) if isinstance(manifest, dict) else str(manifest), "files": files},
                "branch_hints": {}}
    if (cycle.get("kind") != SEED_CYCLE_VERSION or cycle.get("programme_id") != programme_id
            or not isinstance(cycle.get("planned_branches"), list) or len(cycle["planned_branches"]) > 200
            or not isinstance(cycle.get("branches"), dict)):
        raise SafetyError("DISCOVERY_SEED_BINDING_CHANGED")
    pages = safe_path(memory.root, "catalogue_materials", "pages")
    for planned in cycle["planned_branches"]:
        branch_id = identifier(planned["branch_id"])
        progress = cycle["branches"].get(branch_id)
        if progress and progress.get("state") == "completed":
            continue
        branch_paths = set()
        due_at = progress.get("next_retry_at") if progress else None
        due = due_at is None or timestamp(due_at) <= at
        ready |= due
        deadlines.append([branch_id, due])
        if due_at and not due:
            waits.append(due_at)
        reference = planned.get("prepared_reference")
        if isinstance(reference, str):
            watch(owned(reference, preparations, "CATALOGUE_SEED_PREPARATION_OUTSIDE_RESEARCH"))
        groups = [planned.get(name, []) for name in ("prepared_material_ids", "reused_material_ids")]
        ids = list(dict.fromkeys(value for group in groups if isinstance(group, list) for value in group if isinstance(value, str)))
        if len(ids) > 1000:
            raise SafetyError("CATALOGUE_SEED_MATERIAL_IDS_INVALID")
        for material_id in ids:
            path = safe_path(memory.root, "catalogue_materials", "items", identifier(material_id)) / "material.json"
            watch(path)
            try:
                material = load_json(path, MAX_SAVED_BYTES)
                if isinstance(material, dict) and isinstance(material.get("page_reference"), str):
                    watch(owned(material["page_reference"], pages, "CATALOGUE_SEED_PREPARATION_OUTSIDE_RESEARCH"))
            except (SafetyError, OSError) as error:
                _raise_shared_integrity(error)
                # Invalid/missing material remains a stable local hint; the
                # actual seed loader records the exact repair reason.
        for job_id in progress.get("job_ids", []) if progress else []:
            identifier(job_id)
            job = state["jobs"].get(job_id)
            if not job or job.get("request", {}).get("role") != "C":
                continue
            folder = current_attempt(safe_path(memory.root, "ai_jobs", job_id))
            for name in ("result.json", "completion.json"):
                watch(folder / name)
        branch_hints[branch_id] = digest({path: files[path] for path in sorted(branch_paths)})
        ready |= progress is not None and progress.get("saved_wake_hash") != branch_hints[branch_id]
        branch_paths = None
    matrix = cycle.get("accepted_matrix")
    if isinstance(matrix, dict) and isinstance(matrix.get("reference"), str):
        watch(owned(matrix["reference"], safe_path(memory.root, "discovery_cycles", cycle["id"]),
                    "DISCOVERY_SEED_MATRIX_CHANGED"))
    return {"ready": bool(ready), "next_retry_at": min(waits, key=timestamp) if waits else None,
            "signal": {"manifest_hash": cycle["manifest_hash"], "deadlines": deadlines, "files": files},
            "branch_hints": branch_hints}


def catalogue_seed_lane_hint(memory, state, programme_id, discovery_step, *, as_of):
    """Wake hint for the installed seed lane; local stats only, never a read.

    With the repository inventory, a store without its own manifest is ready
    only when every declared source page exists and no unchanged failed
    preparation is waiting. After that, the ordinary seed-cycle hint applies.
    """
    inventory = getattr(discovery_step, "discovery_seed_inventory", None)
    if inventory is None:
        return catalogue_seed_wake_hint(memory, state, programme_id,
            getattr(discovery_step, "discovery_seed_manifest", None), as_of=as_of)
    from .catalogue_seed import seed_inventory_gate, seed_inventory_manifest_path
    path = seed_inventory_manifest_path(memory, inventory["plan"])
    if _seed_cycle_id(programme_id) in state["batch_runs"] or path.is_file():
        return catalogue_seed_wake_hint(memory, state, programme_id, path, as_of=as_of)
    gate = seed_inventory_gate(memory, inventory["plan"], source_root=inventory["source_root"], as_of=as_of)
    return {"ready": gate["ready"], "next_retry_at": gate["next_retry_at"],
            "signal": {"inventory_hash": digest(inventory["plan"]), "manifest": "missing",
                       "sources": gate["files"], "attempt": gate["attempt"]},
            "branch_hints": {}}


def _seed_inventory_manifest(memory, programme_id, catalogue, inventory, at):
    """Store-local manifest path, or a named seed wait; prepared at most once."""
    from .catalogue_seed import prepare_seed_inventory_manifest, seed_inventory_gate, seed_inventory_manifest_path
    path = seed_inventory_manifest_path(memory, inventory["plan"])
    if _seed_cycle_id(programme_id) in memory.snapshot()["batch_runs"]:
        return path, None  # An existing cycle keeps its own original bindings.
    wait = {"first_pass_done": True, "new_concept_ids": [], "paid_actor_calls": 0, "original_product_pool_changed": False}
    if not path.is_file():
        gate = seed_inventory_gate(memory, inventory["plan"], source_root=inventory["source_root"], as_of=at)
        if not gate["ready"]:
            return None, dict(wait, state="waiting", reason=gate["reason"], failures=[{"reason": gate["reason"]}],
                missing_sources=gate["missing_sources"], next_retry_at=gate["next_retry_at"])
    try:
        # Prepares once, or verifies that a saved manifest binds this plan.
        with preparation_step("catalogue_seed"):
            return prepare_seed_inventory_manifest(memory, catalogue, inventory["plan"],
                source_root=inventory["source_root"], as_of=at), None
    except (SafetyError, OSError) as error:
        _raise_shared_integrity(error)
        reason = str(error) if isinstance(error, SafetyError) else "CATALOGUE_SEED_LOCAL_IO_FAILED"
        return None, dict(wait, state="input_failed", reason=reason, failures=[{"reason": reason}])


def _save_seed_cycle(memory, record):
    old = memory.snapshot()["batch_runs"].get(record["id"])
    fields = ("kind", "id", "programme_id", "baseline_concepts", "manifest_hash", "manifest_reference", "prepared_at", "planned_branches")
    if old is not None and any(old[field] != record[field] for field in fields):
        raise SafetyError("DISCOVERY_SEED_BINDING_CHANGED")
    if old != record:
        memory.put("batch_runs", record["id"], record, previous=old,
            event_id=key("discovery-seed-state", [record["id"], digest(old), digest(record)]))


def prepare_catalogue_seed_cycle(memory, programme_id, manifest, *, as_of=None):
    """Freeze prepared-file bindings separately from product/source run IDs."""
    from .catalogue_seed import MAX_SAVED_BYTES
    cycle_id = _seed_cycle_id(programme_id)
    state = memory.snapshot()
    existing = state["batch_runs"].get(cycle_id)
    if existing is not None:
        return deepcopy(existing)  # Original branch bindings, not a later manifest replacement.
    at = as_of or utc_now(); timestamp(at)
    reference = "supplied-manifest-mapping"
    if isinstance(manifest, (str, Path)):
        path = safe_path(Path(manifest))
        if not path.resolve().is_relative_to(safe_path(memory.root, "catalogue_materials", "preparations").resolve()):
            raise SafetyError("DISCOVERY_SEED_MANIFEST_OUTSIDE_RESEARCH")
        reference = str(path); manifest = load_json(path, MAX_SAVED_BYTES)
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or not isinstance(manifest.get("branches"), list) or len(manifest["branches"]) > 200):
        raise SafetyError("DISCOVERY_SEED_MANIFEST_INVALID")
    try:
        branches = deepcopy(manifest["branches"])
        ids = [identifier(row["branch_id"]) for row in branches]
        if len(set(ids)) != len(ids):
            raise ValueError
    except (TypeError, KeyError, ValueError):
        raise SafetyError("DISCOVERY_SEED_MANIFEST_INVALID") from None
    old_discovery = state["batch_runs"].get(_cycle_id(programme_id))
    record = {"kind": SEED_CYCLE_VERSION, "id": cycle_id, "programme_id": programme_id,
        "baseline_concepts": deepcopy(old_discovery["baseline_concepts"]) if old_discovery else sorted(state["concepts"]),
        "manifest_hash": digest(manifest), "manifest_reference": reference, "prepared_at": at,
        "planned_branches": branches, "branches": {}, "accepted_matrix": None,
        "paid_actor_calls": 0, "new_concept_ids": [], "original_product_pool_changed": False}
    _save_seed_cycle(memory, record)
    return deepcopy(record)


def _seed_matrix(memory, catalogue, record, at):
    """Retain the admitted choice matrix; staged examples never add capacity."""
    from .catalogue_coverage import catalogue_matrix
    state = memory.snapshot(); view = effective_catalogue(catalogue, state); catalogue_hash = digest(view)
    previous = record.get("accepted_matrix")
    if previous is not None and previous["catalogue_hash"] == catalogue_hash:
        path = safe_path(Path(previous["reference"]))
        if digest(load_json(path, 8 * 1024 * 1024)) != previous["hash"]:
            raise SafetyError("DISCOVERY_SEED_MATRIX_CHANGED")
        return previous
    matrix = catalogue_matrix(view, state)
    saved = {"schema_version": 1, "seed_cycle_id": record["id"], "manifest_hash": record["manifest_hash"],
        "catalogue_hash": catalogue_hash, "checked_at": at, "matrix": matrix,
        "note": "Only actual admitted catalogue families and phrases are counted. Prepared examples and C judgments are not fresh products or six-month capacity."}
    path = safe_path(memory.root, "discovery_cycles", record["id"], "accepted_catalogue") / (catalogue_hash + ".json")
    if path.exists():
        saved = load_json(path, 8 * 1024 * 1024)
        if saved.get("catalogue_hash") != catalogue_hash or saved.get("seed_cycle_id") != record["id"]:
            raise SafetyError("DISCOVERY_SEED_MATRIX_CHANGED")
    else:
        atomic_json(path, saved)
    return {"reference": str(path), "hash": digest(saved), "catalogue_hash": catalogue_hash,
        "totals": saved["matrix"]["totals"], "categories": saved["matrix"]["categories"]}


def _seed_summary(memory, record):
    state = memory.snapshot()
    outcomes, eligible, judged, reused, completed = {}, set(), set(), set(), set()
    material_ids, prepared_ids, failures = set(), set(), []
    for planned in record["planned_branches"]:
        branch = record["branches"].get(planned["branch_id"], {})
        supplied = planned.get("material_ids", [])
        branch_ids = {value for value in supplied if isinstance(value, str)} if isinstance(supplied, list) else set()
        material_ids.update(branch_ids)
        supplied_prepared = planned.get("prepared_material_ids", [])
        if isinstance(supplied_prepared, list):
            prepared_ids.update(value for value in supplied_prepared if isinstance(value, str))
        eligible.update(branch.get("eligible_material_ids", [])); reused.update(branch.get("reused_material_ids", []))
        failures.extend(branch.get("failures", []))
        for addition_id in branch.get("addition_ids", []):
            addition = state["catalogue_additions"].get(addition_id)
            if addition is None or addition.get("material_id") not in branch_ids:
                continue
            mid = addition["material_id"]; completed.add(mid); outcomes[mid] = addition["outcome"]
            if addition.get("job_id") is not None and addition.get("proposal") is not None:
                judged.add(mid)
    first_pass_done = all(row["branch_id"] in record["branches"] for row in record["planned_branches"])
    pending = material_ids - completed
    counts = {"branches": len(record["planned_branches"]), "branches_considered": len(record["branches"]),
        "branches_complete": sum(row.get("state") == "completed" for row in record["branches"].values()),
        "prepared_examples": len(prepared_ids), "eligible_examples": len(eligible),
        "completed_examples": len(completed), "actual_judged_examples": len(judged),
        "exact_local_merges": sum(state["catalogue_additions"][aid].get("job_id") is None
            for row in record["branches"].values() for aid in row.get("addition_ids", []) if aid in state["catalogue_additions"]),
        "cache_reused_examples": len(reused), "accepted_examples": sum(value == "accepted" for value in outcomes.values()),
        "merged_examples": sum(value == "merged" for value in outcomes.values()),
        "no_useful_addition_examples": sum(value == "no_useful_addition" for value in outcomes.values()),
        "unclear_examples": sum(value == "deferred" for value in outcomes.values()),
        "contract_rejected_examples": sum(value == "rejected" for value in outcomes.values()), "pending_examples": len(pending)}
    return {"state": "checked" if first_pass_done and not pending and all(row.get("state") == "completed" for row in record["branches"].values())
        else "work_remaining", "seed_cycle_id": record["id"], "first_pass_done": first_pass_done,
        "counts": counts, "pending_material_ids": sorted(pending), "failures": failures,
        "accepted_matrix": record.get("accepted_matrix"), "baseline_concepts": record["baseline_concepts"],
        "new_concept_ids": [], "paid_actor_calls": 0, "original_product_pool_changed": False}


def advance_catalogue_seed_cycle(memory, cycle_id, catalogue, *, runner=None, max_branches=1, as_of=None, wake_hints=None):
    """One finite original C branch turn; errors yield to healthy branches."""
    from .catalogue_seed import build_catalogue_branch, load_prepared_catalogue_branch
    if type(max_branches) is not int or not 1 <= max_branches <= 10:
        raise SafetyError("DISCOVERY_SEED_SLICE_INVALID")
    if wake_hints is not None and (not isinstance(wake_hints, dict) or len(wake_hints) > 200
            or any(not isinstance(value, str) or len(value) != 64 for value in wake_hints.values())):
        raise SafetyError("DISCOVERY_SEED_WAKE_HINT_INVALID")
    identifier(cycle_id); at = as_of or utc_now(); timestamp(at)
    with Journal(safe_path(memory.root, "discovery_cycles", cycle_id, "writer")).locked(timeout=5):
        record = deepcopy(memory.snapshot()["batch_runs"].get(cycle_id))
        if record is None or record.get("kind") != SEED_CYCLE_VERSION:
            raise SafetyError("DISCOVERY_SEED_CYCLE_REQUIRED")
        def due(planned):
            prior = record["branches"].get(planned["branch_id"])
            return prior is None or prior.get("state") != "completed" and (
                prior.get("next_retry_at") is None or timestamp(at) >= timestamp(prior["next_retry_at"])
                or wake_hints is not None and planned["branch_id"] in wake_hints
                   and prior.get("saved_wake_hash") != wake_hints[planned["branch_id"]])
        # An unvisited branch gets its first bounded turn before retrying a
        # failed sibling. Saved original jobs still own every pending item.
        selected = sorted((row for row in record["planned_branches"] if due(row)),
            key=lambda row: row["branch_id"] in record["branches"])[:max_branches]
        for planned in selected:
            branch_id = planned["branch_id"]
            old = deepcopy(record["branches"].get(branch_id, {}))
            row = dict(old, state="intent_saved", considered_at=old.get("considered_at", at))
            record["branches"][branch_id] = row; _save_seed_cycle(memory, record)
            try:
                prepared = load_prepared_catalogue_branch(memory, catalogue, planned)
                result = build_catalogue_branch(memory, catalogue, runner, branch_id=branch_id,
                    material_ids=prepared["material_ids"], limit=10, as_of=at, prepared_payload=prepared["payload"])
                additions = result["records"] + result["reused"]
                addition_ids = list(dict.fromkeys(old.get("addition_ids", []) + [item["id"] for item in additions]))
                completed = {item["material_id"] for item in additions}
                missing = sorted(set(planned["material_ids"]) - completed)
                failures = result["failures"] + prepared["pending"] + [{"material_id": mid,
                    "reason": "CATALOGUE_SEED_PREPARATION_NOT_SUPPLIED"} for mid in prepared["unprepared_material_ids"]]
                row.update(state="completed" if not missing else "pending", eligibility="eligible" if not failures
                    else "partly_eligible" if result["eligible_material_ids"] else "not_confirmed",
                    eligible_material_ids=sorted(set(old.get("eligible_material_ids", [])) | set(result["eligible_material_ids"])),
                    addition_ids=addition_ids, reused_material_ids=sorted(set(old.get("reused_material_ids", []))
                        | {item["material_id"] for item in result["reused"]}),
                    pending_material_ids=missing, failures=failures, job_ids=list(dict.fromkeys(old.get("job_ids", []) + result["job_ids"])),
                    prepared_reference=prepared["reference"], payload_hash=prepared["payload_hash"])
            except (SafetyError, OSError, KeyError, TypeError, ValueError) as error:
                reason = str(error) if isinstance(error, SafetyError) else "CATALOGUE_SEED_LOCAL_IO_FAILED"
                row.update(state="input_failed", eligibility="not_confirmed", failures=[{"branch_id": branch_id, "reason": reason}])
            row["next_retry_at"] = None if row["state"] == "completed" else (timestamp(at) + timedelta(seconds=SEED_RETRY_SECONDS)).isoformat()
            if wake_hints is not None and branch_id in wake_hints:
                row["saved_wake_hash"] = wake_hints[branch_id]
            _save_seed_cycle(memory, record)
        # A matrix/file error is local, never permission to repeat a C request.
        try:
            record["accepted_matrix"] = _seed_matrix(memory, catalogue, record, at)
            record.pop("matrix_failure", None)
        except (SafetyError, OSError, KeyError, TypeError, ValueError) as error:
            record["matrix_failure"] = str(error) if isinstance(error, SafetyError) else "CATALOGUE_SEED_MATRIX_SAVE_FAILED"
        _save_seed_cycle(memory, record)
        summary = _seed_summary(memory, record)
        summary.update(branches_advanced_this_round=len(selected), matrix_failure=record.get("matrix_failure"))
        if record.get("matrix_failure"):
            summary["state"] = "work_remaining"
        return summary


def create_catalogue_discovery_step(catalogue=None, *, action_limit=4, actions_per_round=2,
                                    admission=None, seed_manifest=None, seed_branches_per_round=1,
                                    shared_marketplace=False, renewable_waves=False, seed_inventory=None,
                                    seed_source_root=None):
    """N22 callback. Construction is read-only and performs no external work.

    ``seed_inventory`` (the repository plan) with ``seed_source_root`` lets
    each store prepare its own seed manifest through the ordinary staging
    route the first time the lane runs; ``seed_manifest`` supplies one directly.
    """
    base = validate_catalogue(catalogue or load_catalogue())
    if type(seed_branches_per_round) is not int or not 1 <= seed_branches_per_round <= 10:
        raise SafetyError("DISCOVERY_SEED_SLICE_INVALID")
    if type(renewable_waves) is not bool or renewable_waves and not shared_marketplace:
        raise SafetyError("DISCOVERY_WAVE_OPTION_INVALID")
    inventory = None
    if seed_inventory is not None:
        from .catalogue_seed import validate_inventory_plan
        if seed_manifest is not None or not isinstance(seed_source_root, (str, Path)) or not str(seed_source_root):
            raise SafetyError("DISCOVERY_SEED_OPTION_INVALID")
        inventory = {"plan": deepcopy(validate_inventory_plan(seed_inventory)), "source_root": Path(seed_source_root)}
    supplied_manifest = deepcopy(seed_manifest) if isinstance(seed_manifest, dict) else seed_manifest
    def step(memory, programme_record, context):
        seed, manifest = None, supplied_manifest
        if inventory is not None:
            manifest, seed = _seed_inventory_manifest(memory, programme_record["programme_id"], base, inventory, context["at"])
        if manifest is not None:
            try:
                with preparation_step("catalogue_seed"):
                    prepared = prepare_catalogue_seed_cycle(memory, programme_record["programme_id"], manifest, as_of=context["at"])
                    seed_options = ({"wake_hints": catalogue_seed_wake_hint(memory, memory.snapshot(),
                        programme_record["programme_id"], manifest, as_of=context["at"])["branch_hints"]}
                        if renewable_waves else {})
                    seed = advance_catalogue_seed_cycle(memory, prepared["id"], base, runner=context.get("runner"),
                        max_branches=seed_branches_per_round, as_of=context["at"], **seed_options)
            except (SafetyError, OSError, KeyError, TypeError, ValueError) as error:
                seed = {"state": "input_failed", "first_pass_done": True, "failures": [{"reason": str(error)
                    if isinstance(error, SafetyError) else "CATALOGUE_SEED_LOCAL_IO_FAILED"}], "new_concept_ids": [], "paid_actor_calls": 0}
            # Original product lanes still run after this callback. A genuinely
            # new discovery cycle waits only for one finite initial branch pass,
            # not for unavailable evidence or endless successful new ideas.
            if not renewable_waves and not seed["first_pass_done"] and _cycle_id(programme_record["programme_id"]) not in memory.snapshot()["batch_runs"]:
                return {"state": "work_remaining", "seed": seed, "new_concept_ids": [],
                    "new_products_need_full_source_review": [], "paid_actor_calls": 0,
                    "original_product_pool_changed": False, "note": "Prepared branch decisions are advancing before new search choices are frozen. Original product-review lanes continue independently."}
        if renewable_waves:
            with preparation_step("discovery_questions"):
                result = advance_discovery_waves(memory, programme_record["programme_id"], base,
                    handlers=context["handlers"], runner=context.get("runner"), max_actions=actions_per_round,
                    flow_limit=min(50, context.get("fit_limit", 10)), admission=admission, action_limit=action_limit,
                    as_of=context["at"], location=programme_record.get("location"))
            if seed is not None:
                result["seed"] = seed
            return result
        record = prepare_discovery_cycle(memory, programme_record["programme_id"], base,
            handlers=context["handlers"], action_limit=action_limit, as_of=context["at"],
            baseline_concepts=seed.get("baseline_concepts") if seed else None)
        result = advance_discovery_cycle(memory, record["id"], handlers=context["handlers"], runner=context.get("runner"),
            max_actions=actions_per_round, flow_limit=min(50, context.get("fit_limit", 10)), admission=admission, as_of=context["at"],
            shared_marketplace=shared_marketplace)
        if seed is not None:
            result["seed"] = seed
            result["seed_catalogue_available_to_cycle"] = record["catalogue_hash"] == (seed.get("accepted_matrix") or {}).get("catalogue_hash")
            if seed["state"] != "checked":
                result["state"] = "work_remaining"
        return result
    if renewable_waves:
        step.discovery_wave_contract = WAVE_VERSION
        step.discovery_catalogue = deepcopy(base)
        step.discovery_wave_options = {"cache_days": 7}
        step.discovery_seed_manifest = deepcopy(supplied_manifest)
        step.discovery_seed_inventory = deepcopy(inventory)
    return step
