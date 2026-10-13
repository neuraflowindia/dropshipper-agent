"""Finite, explainable discovery choices. No network, model, ledger or file writes.

Outcomes have family_id, source, status, observed_at and request. A completed
capture additionally declares complete=True; reviewed outcomes can declare
resolved_count, known_count, unseen_promising and useful_progress. Missing or
pending interpretation never becomes a zero-yield sample. The caller freezes
and persists this plan before connecting it to the existing launch mechanism.
"""

from copy import deepcopy
from datetime import timedelta, timezone
from decimal import Decimal

from .catalogue import CATEGORIES, alias_groups, validate_catalogue
from .discovery import normalise, tokens
from .models import SafetyError, digest, identifier, timestamp, usd
from .research_records import is_paid_query_task

REQUEST_FIELDS = ("source", "route", "build", "schema_version", "purpose", "term", "language", "country", "date_scope", "coverage", "options")
DEFAULT_ROUTES = {
    "meta": {"available": True, "route": "apify", "build": "2.7.26", "schema_version": 1,
             "estimate_basis": "provisional trial bound", "reason": "Existing captured Meta route; launch still requires normal preparation."},
    "marketplace": {"available": False, "route": "apify", "build": "pending-product-search", "schema_version": 1,
                    "reason": "A product-specific marketplace search route has not been demonstrated."},
    "trends": {"available": False, "route": "browser", "build": "pending-browser-trends-v1", "schema_version": 1,
               "reason": "Browser export route is pending verification."},
}


def semantic_request(request):
    """Never use acquisition IDs as cache scope; preserve exact phrase and bounds."""
    if not isinstance(request, dict) or any(field not in request for field in REQUEST_FIELDS):
        raise SafetyError("PLANNER_REQUEST_SCOPE_INVALID")
    if (request["source"] not in ("meta", "marketplace", "trends") or request["purpose"] not in ("discovery", "validation")
            or request["language"] not in ("en", "hi-Latn") or request["country"] != "IN"
            or type(request["schema_version"]) is not int or request["schema_version"] < 1
            or any(not isinstance(request[field], str) or not request[field].strip() for field in ("route", "build", "term"))
            or any(not isinstance(request[field], dict) for field in ("date_scope", "coverage", "options"))):
        raise SafetyError("PLANNER_REQUEST_SCOPE_INVALID")
    return {field: deepcopy(request[field]) for field in REQUEST_FIELDS}


def semantic_key(request):
    return digest(semantic_request(request))


def _money(value):
    return None if value is None else usd(value)


def _compatible(request, captured):
    """A capped capture can cover an equal/smaller task, never a larger one."""
    left, right = semantic_request(request), semantic_request(captured)
    required, available = left.pop("coverage"), right.pop("coverage")
    if left != right or not isinstance(required, dict) or not isinstance(available, dict):
        return False
    if set(required) != set(available):
        return False
    if "limit" not in required:
        return required == available
    if type(required["limit"]) is not int or type(available["limit"]) is not int:
        return False
    return (required["limit"] > 0 and available["limit"] >= required["limit"]
            and {k: v for k, v in required.items() if k != "limit"} == {k: v for k, v in available.items() if k != "limit"})


def _reviewed_sample(outcome):
    if outcome.get("status") != "completed" or outcome.get("reviewed") is not True or outcome.get("complete") is not True:
        return False
    resolved, known = outcome.get("resolved_count"), outcome.get("known_count")
    return (type(resolved) is int and type(known) is int and resolved >= 10 and 0 <= known <= resolved
            and Decimal(known) / resolved >= Decimal("0.80")
            and outcome.get("unseen_promising") is False and outcome.get("useful_progress") is False
            and type(outcome.get("pending_count")) is int and outcome["pending_count"] == 0)


def validate_outcome(outcome):
    """Validate declared facts, not whether source/model judgments are true."""
    if not isinstance(outcome, dict):
        raise SafetyError("PLANNER_OUTCOMES_INVALID")
    try:
        identifier(outcome["id"])
        identifier(outcome["family_id"])
        timestamp(outcome["observed_at"])
        if outcome["status"] not in ("completed", "partial", "failed", "pending"):
            raise SafetyError("PLANNER_OUTCOME_STATUS_INVALID")
        if outcome.get("source") not in ("meta", "marketplace", "trends"):
            raise SafetyError("PLANNER_OUTCOME_SOURCE_INVALID")
        if outcome.get("request"):
            if semantic_request(outcome["request"])["source"] != outcome["source"]:
                raise SafetyError("PLANNER_OUTCOME_SOURCE_INVALID")
        for field in ("resolved_count", "known_count", "pending_count", "new_eligible_count"):
            if field in outcome and (type(outcome[field]) is not int or outcome[field] < 0):
                raise SafetyError("PLANNER_OUTCOME_COUNT_INVALID")
        if outcome.get("known_count", 0) > outcome.get("resolved_count", outcome.get("known_count", 0)):
            raise SafetyError("PLANNER_OUTCOME_COUNT_INVALID")
        for field in ("reviewed", "complete", "unseen_promising", "useful_progress"):
            if field in outcome and outcome[field] is not None and type(outcome[field]) is not bool:
                raise SafetyError("PLANNER_OUTCOME_REVIEW_INVALID")
        if outcome.get("cost_usd") is not None:
            usd(outcome["cost_usd"])
        if "empty_result" in outcome:
            marker = outcome["empty_result"]
            if (not isinstance(marker, dict) or set(marker) != {"kind", "capture_hash", "request_key", "provider_items", "ad_rows"}
                    or marker["kind"] != "meta_ads_not_found" or outcome["source"] != "meta"
                    or outcome["status"] != "completed" or outcome.get("complete") is not True
                    or outcome.get("reviewed") is not True or outcome.get("resolved_count") != 0
                    or outcome.get("pending_count") != 0 or marker["request_key"] != semantic_key(outcome["request"])
                    or not _hash(marker["capture_hash"]) or type(marker["provider_items"]) is not int
                    or marker["provider_items"] not in (0, 1) or type(marker["ad_rows"]) is not int or marker["ad_rows"] != 0):
                raise SafetyError("PLANNER_EMPTY_RESULT_INVALID")
            identifier(outcome["acquisition_id"])
        if is_empty_refinement(outcome.get("refinement")):
            validate_refinement(outcome["refinement"])
        return outcome
    except (KeyError, TypeError):
        raise SafetyError("PLANNER_OUTCOMES_INVALID") from None


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def is_empty_refinement(value):
    return isinstance(value, dict) and value.get("kind") == "empty_result_alternative"


def validate_refinement(value):
    fields = {"kind", "base_outcome_id", "base_acquisition_id", "base_request_key", "base_capture_hash", "reason"}
    if (not isinstance(value, dict) or set(value) != fields or value["kind"] != "empty_result_alternative"
            or not _hash(value["base_request_key"]) or not _hash(value["base_capture_hash"])
            or not isinstance(value["reason"], str) or not 1 <= len(value["reason"].strip()) <= 2000):
        raise SafetyError("PLANNER_REFINEMENT_INVALID")
    identifier(value["base_outcome_id"]); identifier(value["base_acquisition_id"])
    return value


def _same_refinement_base(value, base):
    return (is_empty_refinement(value) and value["base_acquisition_id"] == base["acquisition_id"]
            and value["base_request_key"] == semantic_key(base["request"])
            and value["base_capture_hash"] == base["empty_result"]["capture_hash"])


def cooldown_until(outcomes, *, as_of, cooldown_days=14):
    """Only two comparable, actually reviewed repeat-heavy samples qualify."""
    now = timestamp(as_of)
    groups = {}
    for outcome in outcomes:
        if outcome.get("status") != "completed" or outcome.get("reviewed") is not True or not outcome.get("request"):
            continue
        at = timestamp(outcome["observed_at"])
        if at <= now:
            groups.setdefault(semantic_key(outcome["request"]), []).append(outcome)
    until = None
    for comparable in groups.values():
        comparable.sort(key=lambda item: (timestamp(item["observed_at"]), item.get("id", "")))
        # Separate acquisitions, not a repeated review/import of one capture.
        unique, seen = [], set()
        for item in reversed(comparable):
            acquisition = item.get("acquisition_id") or item.get("id")
            if not acquisition or acquisition in seen:
                continue
            seen.add(acquisition)
            unique.append(item)
        if len(unique) < 2 or not all(_reviewed_sample(item) for item in unique[:2]):
            continue
        latest = timestamp(unique[0]["observed_at"])
        # A newer useful lead in another formulation reopens the family.
        if any(timestamp(item["observed_at"]) > latest and
               (item.get("unseen_promising") is True or item.get("useful_progress") is True) for item in outcomes):
            continue
        candidate = latest + timedelta(days=cooldown_days)
        if candidate > now and (until is None or candidate > until):
            until = candidate
    return until.isoformat() if until else None


def _request(phrase, route, coverage):
    return {"source": phrase["source"], "route": route["route"], "build": route["build"],
            "schema_version": route["schema_version"], "purpose": phrase["purpose"], "term": phrase["text"],
            "language": phrase["language"], "country": phrase["country"],
            "date_scope": deepcopy(route.get("date_scope", {"kind": "unspecified"})),
            "coverage": deepcopy(coverage), "options": deepcopy(route.get("options", {}))}


def _finite_count(value, code):
    if type(value) is not int or not 0 <= value <= 1000:
        raise SafetyError(code)


def plan_searches(catalogue, outcomes, *, as_of, slots=8, routes=None, budget_usd=None,
                  per_search_usd="0.03", validation_reserve_usd="0.06", coverage=None,
                  cache_days=7, cooldown_days=14, urgent_family_ids=(), refinement_tasks=()):
    """Rank a frozen snapshot, with one discovery action per accepted family.

    routes is keyed by meta/marketplace/trends. available=True means a usable
    route, not authorization to launch it. Explicit cost_estimate_usd=None is
    unknown; an explicitly measured zero is free. An omitted price uses the
    supplied conservative trial estimate and is marked provisional.
    """
    validate_catalogue(catalogue)
    now = timestamp(as_of)
    _finite_count(slots, "PLANNER_SLOTS_INVALID")
    _finite_count(cache_days, "PLANNER_CACHE_INVALID")
    _finite_count(cooldown_days, "PLANNER_COOLDOWN_INVALID")
    if not isinstance(outcomes, list) or any(not isinstance(outcome, dict) for outcome in outcomes):
        raise SafetyError("PLANNER_OUTCOMES_INVALID")
    for outcome in outcomes:
        validate_outcome(outcome)
    if not isinstance(refinement_tasks, (list, tuple)):
        raise SafetyError("PLANNER_REFINEMENT_TASK_INVALID")
    for task in refinement_tasks:
        if not is_paid_query_task(task):
            raise SafetyError("PLANNER_REFINEMENT_TASK_INVALID")
        identifier(task.get("id")); identifier(task.get("family_id")); identifier(task.get("attempt_id"))
        semantic_request(task.get("request")); validate_refinement(task.get("refinement"))
    coverage = deepcopy(coverage if coverage is not None else {"limit": 20})
    if not isinstance(coverage, dict) or ("limit" in coverage and (type(coverage["limit"]) is not int or coverage["limit"] <= 0)):
        raise SafetyError("PLANNER_COVERAGE_INVALID")
    route_config = deepcopy(DEFAULT_ROUTES if routes is None else routes)
    if not isinstance(route_config, dict):
        raise SafetyError("PLANNER_ROUTE_INVALID")
    fallback, reserve, budget = _money(per_search_usd), usd(validation_reserve_usd), _money(budget_usd)
    available_budget = max(Decimal("0"), budget - reserve) if budget is not None else None
    families = {family["id"]: family for family in catalogue["families"]}
    groups = alias_groups(catalogue)
    history = {group: [] for group in groups.values()}
    category_visit = {category: None for category in CATEGORIES}
    parent_visit = {}
    for outcome in outcomes:
        family_id = outcome.get("family_id")
        if family_id not in families:
            continue  # Old retired catalogue IDs remain in the frozen snapshot.
        at = timestamp(outcome["observed_at"])
        if at > now:
            continue
        if outcome.get("status") not in ("completed", "partial", "failed", "pending"):
            raise SafetyError("PLANNER_OUTCOME_STATUS_INVALID")
        if outcome.get("request"):
            semantic_request(outcome["request"])
        history[groups[family_id]].append(outcome)
        family = families[family_id]
        if outcome["status"] != "pending":
            category_visit[family["category"]] = max(at, category_visit[family["category"]] or at)
            parent_visit[family["parent_id"]] = max(at, parent_visit.get(family["parent_id"]) or at)
    for values in history.values():
        values.sort(key=lambda item: (timestamp(item["observed_at"]), item.get("id", "")))
    decisions, candidates, represented = [], [], set()
    for family in sorted(families.values(), key=lambda item: item["id"]):
        family_id, group = family["id"], groups[family["id"]]
        decision = {"family_id": family_id, "category": family["category"], "decision": "defer", "reason": ""}
        decisions.append(decision)
        if family["disposition"] != "usable":
            decision["reason"] = "Family is " + family["disposition"] + "; it is not ready for discovery."
            continue
        if group in represented:
            decision["reason"] = "Accepted exact alias shares a family already considered; spelling does not create a new area."
            continue
        past = history[group]
        if any(item["status"] == "pending" and not item.get("resolved_at") for item in past):
            decision["reason"] = "A saved acquisition is pending; resume or reconcile it rather than starting a replacement."
            continue
        if any(task["family_id"] in groups and groups[task["family_id"]] == group
               and task.get("status") in ("prepared", "pending", "running") for task in refinement_tasks):
            decision["reason"] = "The saved alternative task is unfinished; resume or reconcile it rather than replace it, even when its original capture is old."
            continue
        until = cooldown_until(past, as_of=as_of, cooldown_days=cooldown_days)
        urgent = family_id in urgent_family_ids
        if until and not urgent:
            decision.update(reason="Two comparable reviewed repeat-heavy samples; try another area until the temporary revisit date.", next_eligible_at=until)
            continue
        choices, unavailable, empty_bases = [], [], []
        for phrase_index, phrase in enumerate(family["phrases"]):
            if phrase["purpose"] != "discovery":
                continue
            route = route_config.get(phrase["source"])
            if not isinstance(route, dict):
                unavailable.append(phrase["source"] + ": no route configured")
                continue
            for required in ("route", "build", "schema_version"):
                if required not in route:
                    raise SafetyError("PLANNER_ROUTE_INVALID")
            request = _request(phrase, route, coverage)
            # Accepted aliases and changed seed wording retain the same empty
            # episode; a spelling change cannot reset its one-refinement bound.
            empty_bases.extend(item for item in past if item.get("empty_result") and not is_empty_refinement(item.get("refinement"))
                               and timedelta(0) <= now - timestamp(item["observed_at"]) <= timedelta(days=cache_days)
                               and _compatible(dict(request, term=item["request"]["term"]), item["request"]))
            cached = [item for item in past if item.get("status") == "completed" and item.get("complete") is True
                      and item.get("request") and _compatible(request, item["request"])
                      and timedelta(0) <= now - timestamp(item["observed_at"]) <= timedelta(days=cache_days)]
            if cached:
                latest = max(cached, key=lambda item: (timestamp(item["observed_at"]), item.get("id", "")))
                if latest.get("empty_result"):
                    unavailable.append(phrase["source"] + ": exact saved query returned only an empty-ad diagnostic")
                    continue
                choices.append({"request": request, "source": phrase["source"], "phrase": phrase["text"], "kind": "reuse",
                                "cost": Decimal("0"), "estimate_basis": "No new collector request", "capture_id": latest.get("id"),
                                "captured_observed_at": latest["observed_at"],
                                "reason": f"Reuse equivalent complete captured coverage within {cache_days} days; retain its original observation date.",
                                "phrase_index": phrase_index})
                if is_empty_refinement(latest.get("refinement")):
                    choices[-1]["refinement"] = deepcopy(latest["refinement"])
                continue
            if route.get("available") is not True:
                unavailable.append(phrase["source"] + ": " + route.get("reason", "route availability has not been established"))
                continue
            cost = _money(route["cost_estimate_usd"]) if "cost_estimate_usd" in route else fallback
            choices.append({"request": request, "source": phrase["source"], "phrase": phrase["text"], "kind": "collect",
                            "cost": cost, "estimate_basis": route.get("estimate_basis", "provisional trial bound"),
                            "reason": "Use the accepted source phrase; source exclusions are not assumed.", "phrase_index": phrase_index})
        if empty_bases:
            base = max(empty_bases, key=lambda item: (timestamp(item["observed_at"]), item["id"]))
            prior = [item for item in past if is_empty_refinement(item.get("refinement")) and _same_refinement_base(item["refinement"], base)]
            prepared = [task for task in refinement_tasks if task["family_id"] in groups
                        and groups[task["family_id"]] == group and _same_refinement_base(task["refinement"], base)]
            # Only a saved nonempty result can be useful after the one alternate
            # attempt. Pending/failed work is reconciled, not silently replaced.
            if prior or prepared:
                choices = [item for item in choices if item["kind"] == "reuse"]
                unavailable.append("The one refinement is saved or exhausted; resume its existing work without another collection.")
            else:
                # Reviewed catalogue alternatives, never algorithmic adjective
                # deletion, source-purpose conversion or another new family.
                choices = [item for item in choices if item["kind"] == "reuse" or (
                    item["source"] == base["source"] and normalise(item["phrase"]) != normalise(base["request"]["term"])
                    and tokens(item["phrase"]) != tokens(base["request"]["term"]))]
                for item in choices:
                    if item["kind"] == "collect":
                        item["refinement"] = {"kind": "empty_result_alternative", "base_outcome_id": base["id"],
                            "base_acquisition_id": base["acquisition_id"], "base_request_key": semantic_key(base["request"]),
                            "base_capture_hash": base["empty_result"]["capture_hash"],
                            "reason": "One reviewed same-family alternative after the exact original query returned no ad rows."}
                        item["reason"] = item["refinement"]["reason"]
        if not choices:
            decision["reason"] = "No demonstrated available discovery route or equivalent fresh capture for this family."
            decision["route_details"] = sorted(set(unavailable))
            continue
        # Fresh evidence first, then an affordable known estimate, then primary phrase.
        choices.sort(key=lambda item: (item["kind"] != "reuse", item["cost"] is None,
                                       item["cost"] or Decimal("0"), item["phrase_index"], item["source"]))
        choice = choices[0]
        if choice["cost"] is None and budget is not None:
            decision["reason"] = "Available route has unknown cost; inspect an estimate rather than count it as free."
            continue
        represented.add(group)
        decision.update(decision="eligible", reason="Unexplored accepted family." if not past else "Due revisit or saved evidence reuse.")
        latest_visit = max((timestamp(item["observed_at"]) for item in past), default=None)
        reviewed = [item for item in past if item.get("reviewed") is True and item["status"] == "completed"]
        progress = any(item.get("unseen_promising") is True or item.get("useful_progress") is True for item in reviewed)
        # Measured marginal yield is never inferred from unreviewed downloaded rows.
        measured = [item for item in reviewed if type(item.get("new_eligible_count")) is int and item.get("cost_usd") is not None
                    and item.get("request") and semantic_key(item["request"]) == semantic_key(choice["request"])]
        numerator = sum(item["new_eligible_count"] for item in measured)
        denominator = sum((usd(item["cost_usd"]) for item in measured), Decimal("0"))
        yield_per_cost = Decimal(numerator) / denominator if measured and denominator > 0 else None
        candidates.append({"family": family, "group": group, "choice": choice, "decision": decision,
                           "unexplored": not past, "latest_visit": latest_visit, "progress": progress,
                           "yield_per_cost": yield_per_cost, "yield_tentative": len(measured) < 2, "urgent": urgent})
    actions, spent, used_requests = [], Decimal("0"), set()

    def instant(value):
        return value.astimezone(timezone.utc).isoformat() if value else ""

    while candidates and len(actions) < slots:
        # Three new areas followed by a useful revisit is a preference, not a quota.
        # Cached coverage avoids another collection *within* a chosen family;
        # it must not monopolise slots ahead of distinct unexplored families.
        # A reuse also consumes its slot, so it cannot keep the revisit turn open.
        revisits = [item for item in candidates if not item["unexplored"] and (item["progress"] or item["urgent"] or item["choice"]["kind"] == "reuse" or item["choice"].get("refinement"))]
        new = [item for item in candidates if item["unexplored"]]
        desired_revisit = len(actions) % 4 == 3
        pool = [item for item in candidates if item["urgent"]] or (revisits if desired_revisit and revisits else new or revisits or candidates)

        def rank(item):
            family = item["family"]
            season = family["season"]
            seasonal_rank = 2 if season["kind"] == "seasonal" and now.month not in season["months"] else 1 if season["kind"] == "unknown" else 0
            # An urgent pool can contain both new families and revisits. Keep
            # their different ranking fields behind a common numeric prefix;
            # otherwise Python compares a new-family date string with a
            # revisit boolean. Preserve the new/new/new/revisit preference.
            preference = 0 if item["unexplored"] != desired_revisit else 1
            if item["unexplored"]:
                return (preference, instant(category_visit[family["category"]]), seasonal_rank, instant(parent_visit.get(family["parent_id"])), family["id"])
            ratio = item["yield_per_cost"]
            return (preference, not item["progress"], ratio is None, -(ratio or Decimal("0")), instant(item["latest_visit"]), family["id"])

        chosen = min(pool, key=rank)
        candidates.remove(chosen)
        choice, decision, family = chosen["choice"], chosen["decision"], chosen["family"]
        request_key = semantic_key(choice["request"])
        if request_key in used_requests:
            decision.update(decision="omit", reason="An equivalent request is already selected; overlapping families do not justify a duplicate task.")
            continue
        cost = choice["cost"]
        if cost is not None and available_budget is not None and spent + cost > available_budget:
            decision.update(decision="defer", reason="Keep the remaining allowance for validation and supplier work; this discovery estimate does not fit.")
            continue
        used_requests.add(request_key)
        spent += cost or Decimal("0")
        reason = choice["reason"] + (" Explore a distinct previously unvisited family." if chosen["unexplored"] else
                                     " This revisit has demonstrated useful progress." if chosen["progress"] else
                                     " Reuse saved work before another collection." if choice["kind"] == "reuse" else
                                     " Refine an existing family; this is not an unexplored research area." if choice.get("refinement") else
                                     " A bounded due probe is chosen because unexplored alternatives are unavailable.")
        if chosen["urgent"]:
            reason += " A supplied relevant-lead priority brings this family forward."
        action = {"action_id": "search-" + request_key[:40], "family_id": family["id"], "category": family["category"],
                  "source": choice["source"], "phrase": choice["phrase"], "semantic_key": request_key,
                  "request": choice["request"], "kind": choice["kind"], "reason": reason,
                  "cost_estimate_usd": str(cost) if cost is not None else None,
                  "estimate_basis": choice["estimate_basis"], "yield_estimate_tentative": chosen["yield_tentative"]}
        if choice.get("capture_id"):
            action["capture_id"] = choice["capture_id"]
            action["captured_observed_at"] = choice["captured_observed_at"]
        if choice.get("refinement"):
            action["refinement"] = deepcopy(choice["refinement"])
        actions.append(action)
        decision.update(decision="reuse" if choice["kind"] == "reuse" else "chosen", reason=reason, action_id=action["action_id"])
        category_visit[family["category"]] = now + timedelta(microseconds=len(actions))
        parent_visit[family["parent_id"]] = now + timedelta(microseconds=len(actions))
    for item in candidates:
        item["decision"].update(decision="omit", reason="A higher-priority useful area fills this finite batch; retain this family for a later batch.")
    frozen = {"catalogue": catalogue, "outcomes": sorted(deepcopy(outcomes), key=digest), "as_of": as_of,
              "slots": slots, "routes": route_config, "budget_usd": str(budget) if budget is not None else None,
              "per_search_usd": str(fallback) if fallback is not None else None, "validation_reserve_usd": str(reserve),
              "coverage": coverage, "cache_days": cache_days, "cooldown_days": cooldown_days,
              "urgent_family_ids": sorted(urgent_family_ids)}
    if refinement_tasks:
        frozen["refinement_tasks"] = sorted(deepcopy(refinement_tasks), key=digest)
    return {"schema_version": 1, "catalogue_revision": catalogue["revision"], "snapshot_hash": digest(frozen),
            "as_of": as_of, "actions": actions, "decisions": decisions,
            "budget": {"available_usd": str(budget) if budget is not None else None,
                       "reserved_for_validation_and_suppliers_usd": str(reserve), "discovery_estimate_usd": str(spent),
                       "has_unknown_cost": any(action["cost_estimate_usd"] is None for action in actions),
                       "note": "Planning estimate only; the existing ledger still controls each actual launch."},
            "note": "A finite next batch, not hundreds of launches or a guarantee of new products. Replan after reviewed outcomes."}
