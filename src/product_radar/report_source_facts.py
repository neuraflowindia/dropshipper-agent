"""Small, saved-only source outcomes for the existing report fact contract.

Coverage is work status, not a demand vote. Measurements describe the retained
relevant sample, never sales, market size, or an inferred sourcing cost. Original
receipt/task records are separately bound by the selection's source revisions.
"""

from copy import deepcopy
from decimal import Decimal
import math
import re

from .models import SafetyError, canonical, digest, identifier, timestamp
from .source_coverage import (ROUTES, STATES, TASK_KIND, RUN_KIND, CHECK_KIND,
                              RELEVANCE_KIND, DISPATCH_KIND, source_coverage)
from .supplier_effort import RETIRED_SUPPLIER_ROUTES


CONTRACT = "saved-source-routes-v1"
FUNCTIONAL_CONTRACT = "saved-source-routes-v2"
FUNCTIONAL_ROUTE_PROOF_CONTRACT = "functional-source-route-proof-v1"
CONTRACTS = (CONTRACT, FUNCTIONAL_CONTRACT)
FIELDS = ("source_route_contract", "source_route_checks")
# An internal follow-up read the client sees inside another route's row: the
# deeper Meta read counts in the single Facebook Ads Library row (W11).
FOLDED_ROUTES = {"meta-ads-depth": "meta-ads"}
DEEPER_META_NOTE = " Includes ads from a deeper read of the Ads Library."
# Client-facing route rows. A retired route is no longer part of the service,
# so new packets omit it; internal records keep its history, and a packet with
# a row for every client-facing route (retired ones included) stays readable.
PACKET_ROUTES = tuple(route for route in ROUTES if route not in FOLDED_ROUTES)
CLIENT_ROUTES = tuple(route for route in PACKET_ROUTES if route not in RETIRED_SUPPLIER_ROUTES)
MAX_BLOCK_BYTES = 64 * 1024
MAX_DECIMAL_LENGTH = 32
DECIMAL_TEXT = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z")
ROUTE_LABELS = {
    "meta-ads": "Facebook Ads Library", "amazon-bestsellers": "Amazon India Bestsellers",
    "amazon-search": "Amazon India product search",
    "flipkart-search": "Flipkart product search", "google-trends": "Google Trends India",
    "blinkit": "Blinkit", "zepto": "Zepto", "instamart": "Instamart",
    "google-suppliers": "Google supplier search", "google-maps": "Google Maps supplier search",
    "indiamart": "IndiaMART", "tradeindia": "TradeIndia", "company-pages": "Supplier company pages",
    "deodap": "DeoDap", "baapstore": "BaapStore", "dropship-india": "Dropship India", "dropdash": "Dropdash",
    "meta-ads-depth": "Facebook Ads Library (deeper read)",
}
METRICS = {
    "relevant_ads": ("Relevant ads in saved sample", "ads"),
    "advertisers": ("Identified advertisers", "advertisers"),
    "active_ads": ("Ads marked active", "ads"),
    "unknown_active_status": ("Ads without an active-status answer", "ads"),
    "missing_advertiser_ids": ("Ads without an advertiser identity", "ads"),
    "relevant_listings": ("Relevant listing examples", "listings"),
    "list_rank_bounds": ("Observed list positions", "list rank, not units sold"),
    "displayed_prices": ("Listings with a displayed price", "listings"),
    "available_listings": ("Listings marked available", "listings across the checked locations"),
    "unavailable_listings": ("Listings marked unavailable", "listings across the checked locations"),
    "unknown_availability": ("Listings without an availability answer", "listings"),
    "chart_intervals": ("Saved chart intervals", "intervals"),
    "nonzero_points": ("Chart intervals with nonzero interest", "intervals"),
    "previous_mean_bounds": ("Previous four-period interest", "relative interest index, not sales"),
    "recent_mean_bounds": ("Recent four-period interest", "relative interest index, not sales"),
}
_TABLES = ("query_tasks", "source_checks", "observations", "captures", "trends_evidence")
_ROUTE_PROOF_TABLES = ("concepts", "descriptions", "associations", "query_tasks", "source_checks",
                       "observations", "captures", "trends_evidence", "jobs", "brand_reviews")


def _functional_route_state(state, concept_id):
    """Select actual route dependencies, not the entire research history.

    Global recaptures of a nominated source item/location remain dependencies,
    even if their newer description belongs elsewhere. Jobs are exact saved
    owners, but their other batch items do not nominate more route originals.
    """
    from .research_memory import TABLES
    from .research_records import FUNCTIONAL_DEFINITION_KIND, SCOPED_EVIDENCE_KIND, is_paid_query_task
    from .source_interpretation import _source_identity
    from .supplier_effort import VERSION as SUPPLIER_EFFORT_KIND
    selected = {table: {} for table in TABLES}
    def add(table, record_id):
        # A record already selected is an equal copy of the same unchanged
        # original; copying it again on every pass changes nothing.
        if record_id in state.get(table, {}) and record_id not in selected[table]:
            selected[table][record_id] = deepcopy(state[table][record_id])
    add("concepts", concept_id)
    dependencies, runs = route_dependencies(state, concept_id, functional=True)
    for table, rows in dependencies.items():
        for rid in rows:
            add(table, rid)
    for rid, row in state["source_checks"].items():
        if row.get("concept_id") == concept_id and row.get("kind") in (
                FUNCTIONAL_DEFINITION_KIND, SCOPED_EVIDENCE_KIND, SUPPLIER_EFFORT_KIND):
            add("source_checks", rid)
    for rid, row in state["associations"].items():
        if row.get("concept_id") == concept_id:
            add("associations", rid)
    for rid, row in state.get("brand_reviews", {}).items():
        if row.get("target_id") == concept_id or rid == state["concepts"][concept_id].get("origin_brand_review_id"):
            add("brand_reviews", rid)
    while True:
        before = sum(len(rows) for rows in selected.values())
        for task in list(selected["query_tasks"].values()):
            for rid in task.get("receipt_refs", []):
                add("source_checks", rid)
            # The raw shared task still preserves every original marker, but
            # only this target can nominate a replacement dependency here.
            marker = task.get("superseded_targets", {}).get(concept_id)
            if isinstance(marker, dict):
                add("query_tasks", marker.get("task_id"))
            for dependency in task.get("binding", {}).get("dependencies", []):
                add(dependency.get("table"), dependency.get("id"))
        for row in list(selected["source_checks"].values()):
            for ref in row.get("references", []):
                add(ref.get("table"), ref.get("id"))
            for rid in row.get("basis_refs", {}):
                add("observations", rid)
            for rid in row.get("association_hashes", {}):
                add("associations", rid)
            for rid in row.get("request_dependencies", {}):
                add("query_tasks", rid)
            if row.get("question"):
                add("query_tasks", row["question"]["id"])
            proof = row.get("job_proof") or {}
            add("jobs", proof.get("job_id"))
            for capsule in proof.get("capture_contexts", {}).values():
                for name in ("capture", "original_task", "original_receipt"):
                    ref = capsule["context"].get(name)
                    if ref is not None:
                        add(ref["table"], ref["id"])
            envelope = row.get("envelope", {})
            for field, table in (("observation_ids", "observations"), ("capture_ids", "captures")):
                for rid in envelope.get(field, []):
                    add(table, rid)
            for rid in envelope.get("evidence_refs", []):
                for table in _TABLES:
                    add(table, rid)
        semantics = {row.get("semantic_id") for row in selected["associations"].values()
                     if row.get("concept_id") == concept_id}
        for rid, row in state["observations"].items():
            if row.get("semantic_id") in semantics:
                add("observations", rid)
        identities = {_source_identity(row) for row in selected["observations"].values()}
        for rid, row in state["observations"].items():
            if _source_identity(row) in identities:
                add("observations", rid)
        observed_semantics = {row.get("semantic_id") for row in selected["observations"].values()}
        for rid, row in state["associations"].items():
            if row.get("semantic_id") in observed_semantics:
                add("associations", rid)
        for semantic_id in semantics:
            add("descriptions", semantic_id)
        for row in selected["observations"].values():
            add("captures", row.get("capture_id"))
        attempts = {row["reference"].get("attempt_id") for row in selected["captures"].values()
                    if isinstance(row.get("reference"), dict)} - {None}
        for rid, row in state["query_tasks"].items():
            if is_paid_query_task(row) and row["attempt_id"] in attempts:
                add("query_tasks", rid)
        chart_fields = ("term", "geo", "time_range", "property", "category")
        def chart_identity(row):
            metadata = row.get("parsed", {}).get("metadata", {})
            return digest({field: metadata.get(field) for field in chart_fields})
        chart_identities = {chart_identity(row) for row in selected["trends_evidence"].values()}
        for rid, row in state.get("trends_evidence", {}).items():
            if row.get("concept_id") == concept_id or chart_identity(row) in chart_identities:
                add("trends_evidence", rid)
        if sum(len(rows) for rows in selected.values()) == before:
            break
    return selected, {rid: {"prepared_at": state["batch_runs"][rid]["prepared_at"],
        "task_ids": [tid for tid in state["batch_runs"][rid].get("task_ids", []) if tid in selected["query_tasks"]],
        "scope": deepcopy(scope)} for rid, scope in runs.items()}


def freeze_functional_route_proof(state, concept_id, *, as_of):
    """Freeze selected originals for deterministic, saved-only route replay."""
    from .functional_types import effective_functional_definition
    from .frozen_records import freeze_original_json
    timestamp(as_of)
    meaning = effective_functional_definition(state, concept_id)
    if not meaning["accepted"]:
        raise SafetyError("FUNCTIONAL_ROUTE_MEANING_REQUIRED")
    selected, runs = _functional_route_state(state, concept_id)
    proof = {"contract": FUNCTIONAL_ROUTE_PROOF_CONTRACT, "concept_id": concept_id,
        "meaning_hash": meaning["meaning_hash"], "as_of": as_of,
        "records": {table: {rid: freeze_original_json(row) for rid, row in sorted(rows.items())}
                    for table, rows in selected.items() if table in _ROUTE_PROOF_TABLES or rows},
        # Canonical JSON sorts object keys. Equal-date replacements and the
        # existing route task list retain original insertion order, so carry
        # that selected order explicitly instead of accidentally changing it
        # when this packet is written to disk or read in another language.
        "record_order": {table: [rid for rid in state[table] if rid in rows]
                         for table, rows in selected.items() if table in _ROUTE_PROOF_TABLES or rows},
        "run_scopes": {rid: dict(row, scope=freeze_original_json(row["scope"])) for rid, row in sorted(runs.items())}}
    proof["proof_hash"] = digest(proof)
    return proof


def functional_route_proof_state(proof):
    """Read the explicit capsule only; never fetch a missing original file."""
    from .frozen_records import read_original_json
    from .research_memory import TABLES
    try:
        if (set(proof) != {"contract", "concept_id", "meaning_hash", "as_of", "records", "record_order", "run_scopes", "proof_hash"}
                or proof["contract"] != FUNCTIONAL_ROUTE_PROOF_CONTRACT
                or proof["proof_hash"] != digest({name: value for name, value in proof.items() if name != "proof_hash"})
                or not set(_ROUTE_PROOF_TABLES) <= set(proof["records"])
                or not set(proof["records"]) <= set(TABLES)
                or set(proof["record_order"]) != set(proof["records"])):
            raise SafetyError("FUNCTIONAL_ROUTE_PROOF_INVALID")
        timestamp(proof["as_of"])
        selected = {table: {} for table in TABLES}
        for table, rows in proof["records"].items():
            order = proof["record_order"][table]
            if (not isinstance(order, list) or any(not isinstance(rid, str) for rid in order)
                    or len(set(order)) != len(order) or set(order) != set(rows)):
                raise SafetyError("FUNCTIONAL_ROUTE_PROOF_INVALID")
            selected[table] = {rid: read_original_json(rows[rid]) for rid in order}
        for rid, saved in proof["run_scopes"].items():
            scope = read_original_json(saved["scope"])
            if (set(saved) != {"prepared_at", "task_ids", "scope"}
                    or set(scope) != {"route_pauses", "pending_preparation"}
                    or len(set(saved["task_ids"])) != len(saved["task_ids"])
                    or any(tid not in selected["query_tasks"] for tid in saved["task_ids"])):
                raise SafetyError("FUNCTIONAL_ROUTE_PROOF_INVALID")
            timestamp(saved["prepared_at"])
            if rid not in selected["batch_runs"]:
                selected["batch_runs"][rid] = {"kind": RUN_KIND, "prepared_at": saved["prepared_at"],
                    "task_ids": deepcopy(saved["task_ids"]), **scope}
        return selected
    except (KeyError, TypeError, AttributeError, ValueError):
        raise SafetyError("FUNCTIONAL_ROUTE_PROOF_INVALID") from None


def validate_functional_route_proof(product):
    """Rederive full outcomes, details and measurements; a hash is not enough."""
    from .functional_types import effective_functional_definition
    from .market_validation import functional_market_context
    from .report_selection import _url
    try:
        proof = product["functional_route_proof"]
        state = functional_route_proof_state(proof)
        cid, as_of = product["concept_id"], product["example_as_of"]
        meaning = effective_functional_definition(state, cid)
        if (proof["concept_id"] != cid or proof["as_of"] != as_of
                or proof["meaning_hash"] != product["functional_definition"]["meaning_hash"]
                or meaning["meaning_hash"] != proof["meaning_hash"]
                or state["concepts"][cid]["description"] != product["description"]):
            raise SafetyError("FUNCTIONAL_ROUTE_PROOF_INVALID")
        context = functional_market_context(state, cid, as_of=as_of)
        relevant = {ref: state["observations"][ref] for ref in context["matches"] if ref in state["observations"]}
        route_product = {"market": context["market"], "functional_market_contract": context["contract"],
                         "functional_market_context": context}
        coverage = coverage_rows(None, state, [cid], as_of)[cid]
        legacy = any(isinstance(row, dict) and row.get("route") in RETIRED_SUPPLIER_ROUTES
                     for row in product.get("source_route_checks") or [])
        expected = source_route_facts(state, cid, coverage, route_product, relevant, as_of, include_retired=legacy)
        for row in expected:
            row["url"] = _url(row["url"])
        if product["source_route_contract"] != FUNCTIONAL_CONTRACT or product["source_route_checks"] != expected:
            raise SafetyError("FUNCTIONAL_ROUTE_DISPLAY_CHANGED")
        return expected
    except (KeyError, TypeError, AttributeError, ValueError):
        raise SafetyError("FUNCTIONAL_ROUTE_PROOF_INVALID") from None


def outcome_display(outcome, actual):
    if outcome in ("not_attempted", "not_connected"):
        return "Not tried", "No product check is saved for this route."
    if outcome == "not_applicable":
        return "Not applicable", "This check does not apply to the saved product scope."
    if outcome == "completed_relevant":
        return "Relevant result found", "Saved results were matched to this product type."
    if outcome == "completed_no_results":
        return "No usable result found", "The completed check found no usable result for this product type."
    if outcome == "interpretation_pending":
        return "Results need review", "Source results are saved; their match to this product still needs review."
    if outcome == "insufficient":
        return "Result unclear", "The check did not provide enough usable information; this is not a product failure."
    if outcome == "in_progress":
        return ("Check in progress", "An attempted check still needs its saved result.") if actual else (
            "Not tried", "The check is prepared, but no actual request is confirmed.")
    if outcome in ("failed", "blocked"):
        if not actual:
            return "Not tried", "No request for this product was sent; the route could not start or is waiting."
        return ("Check failed" if outcome == "failed" else "Check blocked",
                "The attempted check did not produce a usable answer; this is not a product failure.")
    raise SafetyError("LIVE_SOURCE_ROUTE_OUTCOME_INVALID")


def route_for_observation(observation, captures):
    facts = observation.get("facts", {})
    capture = captures.get(observation.get("capture_id"), {})
    reference = capture.get("reference")
    explicit = facts.get("source_route") or (reference.get("route") if isinstance(reference, dict) else None)
    if explicit in ROUTES:
        return explicit
    source, kind = observation.get("source"), facts.get("list_type")
    if source == "meta":
        return "meta-ads"
    if source == "amazon":
        return {"bestsellers": "amazon-bestsellers",
                "product_search": "amazon-search", "product-search": "amazon-search"}.get(kind)
    if source == "flipkart" and kind in ("product_search", "product-search"):
        return "flipkart-search"
    return source if source in ("blinkit", "zepto", "instamart") else None


def functional_task_projection(task, concept_id):
    """Currentness of one target without changing the original task proof.

    A physical question can acquire more targets and their interpretations.
    Those additions do not change an existing target's facts. Keep transport,
    ownership, receipts, access/retry state and unknown fields exact; only the
    named per-target maps and the generic bookkeeping revision are narrowed.
    """
    identifier(concept_id)
    if (not isinstance(task, dict) or task.get("kind") != TASK_KIND
            or not isinstance(task.get("targets"), dict)
            or "revision" in task and (type(task["revision"]) is not int or task["revision"] < 1)):
        raise SafetyError("REPORT_SOURCE_TASK_PROJECTION_INVALID")
    projected = deepcopy(task)
    projected.pop("revision", None)
    for field in ("targets", "target_results", "query_plans", "superseded_targets"):
        if field not in projected:
            continue
        values = projected.pop(field)
        if not isinstance(values, dict):
            raise SafetyError("REPORT_SOURCE_TASK_PROJECTION_INVALID")
        if concept_id in values:
            projected[field] = {concept_id: values[concept_id]}
    return projected


def source_revisions_current(state, product):
    """Compare current records while retaining every frozen raw audit hash.

    Only functional shared-task target bookkeeping has a scoped comparison.
    The exact original task must still be present in this product's checked
    frozen route capsule and match its stored raw revision hash. All other
    records retain the original full-record comparison, including receipts.
    """
    revisions = product.get("source_revisions", {})
    if (not isinstance(revisions, dict) or any(table not in state or not isinstance(rows, dict)
            for table, rows in revisions.items())):
        return False
    changed = [(table, rid, expected) for table, rows in revisions.items()
        for rid, expected in rows.items() if digest(state[table].get(rid)) != expected]
    if not changed:
        return True
    if (product.get("recommendation_contract") != "functional-type-examples-v1"
            or product.get("source_route_contract") != FUNCTIONAL_CONTRACT
            or any(table != "query_tasks" or not isinstance(state[table].get(rid), dict)
                   or state[table][rid].get("kind") != TASK_KIND
                   for table, rid, _ in changed)):
        return False
    try:
        cid, proof = product["concept_id"], product["functional_route_proof"]
        if (proof["concept_id"] != cid or proof["as_of"] != product["example_as_of"]
                or proof["meaning_hash"] != product["functional_definition"]["meaning_hash"]):
            return False
        original = functional_route_proof_state(proof)
        for _, rid, expected in changed:
            task = original["query_tasks"].get(rid)
            if (digest(task) != expected or functional_task_projection(task, cid)
                    != functional_task_projection(state["query_tasks"][rid], cid)):
                return False
    except (SafetyError, KeyError, TypeError, ValueError, AttributeError):
        return False
    return True


def route_dependencies(state, concept_id, *, functional=False):
    """Concept-scoped exact records; never hash a whole run or unrelated task."""
    if type(functional) is not bool:
        raise SafetyError("LIVE_SOURCE_ROUTE_CONTRACT_INVALID")
    scope = digest(state["concepts"][concept_id]["description"])
    tasks = {task_id: task for task_id, task in state.get("query_tasks", {}).items()
             if task.get("kind") == TASK_KIND and task.get("targets", {}).get(concept_id) == scope}
    checks = {check_id: check for check_id, check in state.get("source_checks", {}).items()
              if (check.get("task_id") in tasks or check.get("kind") == "all_source_applicability_v1"
                  and check.get("concept_id") == concept_id and check.get("description_hash") == scope)}
    if functional:
        for check_id, check in list(checks.items()):
            if check.get("kind") != RELEVANCE_KIND:
                continue
            results = check.get("results")
            if (not isinstance(results, list) or not results
                    or any(not isinstance(row, dict) or not isinstance(row.get("concept_id"), str) for row in results)):
                raise SafetyError("SOURCE_RELEVANCE_INVALID")
            if not any(row["concept_id"] == concept_id for row in results):
                del checks[check_id]
    for task in tasks.values():
        for check_id in task.get("receipt_refs", []):
            if check_id in state.get("source_checks", {}):
                checks[check_id] = state["source_checks"][check_id]
    # A shared route pause can affect an untried target without giving that
    # target an attempt. Bind its actual receipt, not unrelated run progress.
    run_scope = {}
    for run_id, run in state.get("batch_runs", {}).items():
        if run.get("kind") != RUN_KIND:
            continue
        own = {task_id for task_id in run.get("task_ids", []) if task_id in tasks}
        if not own:
            continue
        routes = {tasks[task_id]["route"] for task_id in own}
        pauses = {route: pause for route, pause in run.get("route_pauses", {}).items() if route in routes}
        pending = [row for row in run.get("pending_preparation", [])
                   if row.get("concept_id") == concept_id and row.get("description_hash") == scope]
        run_scope[run_id] = {"route_pauses": pauses, "pending_preparation": pending}
        for pause in pauses.values():
            check_id = pause.get("receipt_id")
            if check_id in state.get("source_checks", {}):
                checks[check_id] = state["source_checks"][check_id]
    return {"query_tasks": tasks, "source_checks": checks}, run_scope


def coverage_rows(memory, state, concept_ids, as_of):
    """Reuse the existing coverage logic without assuming collector access.

    A report view does not know this process's installed handler registry. Its
    untried rows therefore say 'not tried', not 'connector is unavailable'.
    Future task receipts/reviews cannot supply an earlier report's answer.
    """
    at = timestamp(as_of)
    tasks = {}
    for task_id, original in state.get("query_tasks", {}).items():
        if original.get("kind") != TASK_KIND or timestamp(original["prepared_at"]) > at:
            continue
        task = deepcopy(original)
        eligible = [ref for ref in task["receipt_refs"] if timestamp(state["source_checks"][ref]["at"]) <= at]
        future_review = any(row.get("kind") == RELEVANCE_KIND and row.get("task_id") == task_id
                            and timestamp(row["at"]) > at for row in state.get("source_checks", {}).values())
        if eligible != task["receipt_refs"] or future_review:
            task["receipt_refs"], task["target_results"], task["attempt_refs"] = eligible, {}, []
            task["status"], task["reconcile_required"] = "prepared", False
            for ref in eligible:
                receipt = state["source_checks"][ref]["envelope"]
                task.update(status=receipt["status"], reason=receipt["reason"], target_results=deepcopy(receipt["target_results"]))
                if receipt.get("dispatch_started") is True:
                    task["attempt_refs"].append(receipt["acquisition_id"])
            for row in state.get("source_checks", {}).values():
                if row.get("kind") == RELEVANCE_KIND and row.get("task_id") == task_id and timestamp(row["at"]) <= at:
                    task["target_results"].update({result["concept_id"]: deepcopy(result) for result in row["results"]})
            # A genuine dispatch before the cut-off remains an actual pending
            # attempt even when its eventual receipt arrived after that time.
            dispatches = [row for row in state.get("source_checks", {}).values()
                          if row.get("kind") == DISPATCH_KIND and row.get("task_id") == task_id and timestamp(row["at"]) <= at]
            completed_acquisitions = {state["source_checks"][ref]["envelope"]["acquisition_id"] for ref in eligible}
            task["attempt_refs"] = sorted(set(task["attempt_refs"]) | {row["acquisition_id"] for row in dispatches})
            if any(row["acquisition_id"] not in completed_acquisitions for row in dispatches):
                task.update(status="dispatching", reconcile_required=True, reason="An actual check still needs its saved result.")
        if task.get("dispatched_at") and timestamp(task["dispatched_at"]) > at:
            task.pop("dispatched_at")
        tasks[task_id] = task
    view = dict(state, query_tasks=tasks,
        source_checks={ref: row for ref, row in state.get("source_checks", {}).items()
                       if not row.get("at") or timestamp(row["at"]) <= at},
        batch_runs={run_id: dict(run, task_ids=[ref for ref in run.get("task_ids", []) if ref in tasks],
                         route_pauses={route: pause for route, pause in run.get("route_pauses", {}).items()
                             if pause.get("receipt_id") in state["source_checks"]
                             and timestamp(state["source_checks"][pause["receipt_id"]]["at"]) <= at})
                    for run_id, run in state.get("batch_runs", {}).items()
                    if run.get("kind") != RUN_KIND or timestamp(run["prepared_at"]) <= at})
    result = source_coverage(memory, concept_ids=concept_ids, as_of=as_of, state=view, include_staged_progress=False)
    rows = {}
    for row in result["rows"]:
        row = deepcopy(row)
        if row["state"] == "not_connected":
            row["state"] = "not_attempted"
        rows.setdefault(row["concept_id"], []).append(row)
    return rows


def _metric(name, value, refs):
    # The private portal deliberately hashes integer-only JSON. Preserve the
    # exact ordinary decimal representation of computed floats as text; never
    # round a chart index or relax the cross-language canonical hash contract.
    def decimal_text(number):
        if type(number) is not float:
            return number
        if not math.isfinite(number):
            raise SafetyError("LIVE_SOURCE_MEASUREMENT_INVALID")
        text = format(Decimal(str(number)), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    value = [decimal_text(number) for number in value] if isinstance(value, list) else decimal_text(value)
    return {"name": name, "value": value, "evidence_ids": sorted(set(refs))}


def _metric_number(value):
    """Bound an integer or canonical decimal string without accepting bools."""
    if value is None:
        return None
    if type(value) is int:
        number = Decimal(value)
    elif isinstance(value, str) and len(value) <= MAX_DECIMAL_LENGTH and DECIMAL_TEXT.fullmatch(value):
        number = Decimal(value)
    else:
        raise SafetyError("LIVE_SOURCE_MEASUREMENT_INVALID")
    if not 0 <= number <= Decimal(10 ** 9):
        raise SafetyError("LIVE_SOURCE_MEASUREMENT_INVALID")
    return number


def _measurement_text(metric):
    label, unit = METRICS[metric["name"]]
    value = metric["value"]
    if value is None:
        return label + ": not stated"
    if isinstance(value, list):
        text = str(value[0]) if value[0] == value[1] else str(value[0]) + "–" + str(value[1])
    else:
        text = str(value)
    return label + ": " + text + " (" + unit + ")"


def measurement_text(metric):
    """Shared factual display; numeric labels/units never come from G prose."""
    return _measurement_text(metric)


def source_route_facts(state, concept_id, coverage, product, relevant, as_of, *, include_retired=False):
    scope, at = digest(state["concepts"][concept_id]["description"]), timestamp(as_of)
    routed = {}
    for observation_id, observation in relevant.items():
        route = route_for_observation(observation, state["captures"])
        if route:
            routed.setdefault(route, []).append((observation_id, observation))
    output = []
    for row in coverage:
        if row["route"] in RETIRED_SUPPLIER_ROUTES and not include_retired or row["route"] in FOLDED_ROUTES:
            continue  # Not part of the service, or shown inside another row: no client row.
        route, outcome, actual = row["route"], row["state"], row["actual_attempt"]
        state_label, detail = outcome_display(outcome, actual)
        refs = {ref for ref in row["evidence_refs"] if any(ref in state.get(table, {}) for table in _TABLES)}
        task_ids = row["task_ids"]
        check_ids = {ref for task_id in task_ids for ref in state["query_tasks"][task_id].get("receipt_refs", [])
                     if timestamp(state["source_checks"][ref]["at"]) <= at}
        if row.get("blocked_by"):
            check_ids.add(row["blocked_by"])
        checks = [state["source_checks"][ref] for ref in check_ids]
        measurements = []
        examples = routed.get(route, [])
        if route == "meta-ads":
            signal = product["market"]["families"]["meta"]
            used = signal["used_ids"]
            if used:
                measurements.append(_metric("relevant_ads", len(used), used))
                measurements.extend(_metric(name, signal.get(name), used) for name in
                    ("advertisers", "active_ads", "unknown_active_status", "missing_advertiser_ids"))
                if any(isinstance(row.get("facts"), dict) and row["facts"].get("source_route") == "meta-ads-depth"
                       for row in relevant.values()):
                    detail += DEEPER_META_NOTE  # its ads count here once, never in a row of their own
        elif examples:
            unique = {}
            for observation_id, observation in examples:
                identity = observation.get("source_item", observation_id)
                if route in ("blinkit", "zepto", "instamart"):
                    # One item checked in two places is two local listing
                    # observations, not proof it is available in either place
                    # twice. Preserve both actual locations in the sample.
                    identity = (identity, digest(observation["facts"].get("location")))
                unique[identity] = (observation_id, observation)
            used = [observation_id for observation_id, _ in unique.values()]
            measurements.append(_metric("relevant_listings", len(unique), used))
            displayed = [observation_id for observation_id, observation in unique.values()
                         if observation["facts"].get("price", {}).get("displayed") is not None
                         and str(observation["facts"]["price"]["displayed"]).strip()]
            measurements.append(_metric("displayed_prices", len(displayed), used))
            if route == "amazon-bestsellers":
                from .marketplace_browser import verified_list_observation
                ranks = [(i, o["facts"]["position"]) for i, o in unique.values()
                         if verified_list_observation(o) and type(o["facts"].get("position")) is int]
                if ranks:
                    measurements.append(_metric("list_rank_bounds", [min(v for _, v in ranks), max(v for _, v in ranks)],
                                                [i for i, _ in ranks]))
            if route in ("blinkit", "zepto", "instamart"):
                values = [o["facts"].get("available") for _, o in unique.values()]
                known = any(type(value) is bool for value in values)
                measurements.extend([_metric("available_listings", sum(value is True for value in values) if known else None, used),
                    _metric("unavailable_listings", sum(value is False for value in values) if known else None, used),
                    _metric("unknown_availability", sum(type(value) is not bool for value in values), used)])
                locations = sorted({str(o["facts"]["location"]["name"]) for _, o in unique.values()
                                    if isinstance(o["facts"].get("location"), dict) and o["facts"]["location"].get("name")})
                unnamed = any(not isinstance(o["facts"].get("location"), dict)
                              or not o["facts"]["location"].get("name") for _, o in unique.values())
                if len(locations) == 1 and not unnamed:
                    detail += " Checked location: " + locations[0] + "."
                elif locations:
                    detail += " Combined saved sample from: " + "; ".join(locations + (["location not stated"] if unnamed else [])) + "."
                else:
                    detail += " Checked locations were not stated."
        if route == "google-trends":
            from .trends import compute_trends_signal
            source = product["market"].get("sources", {}).get("google-trends", {})
            for evidence_id in source.get("used_ids", []):
                evidence = state["trends_evidence"].get(evidence_id)
                if not evidence:
                    continue
                parsed = evidence["parsed"]
                measurements.append(_metric("chart_intervals", len(parsed["series"]), [evidence_id]))
                signal = compute_trends_signal(parsed, now=as_of)
                measurements.extend(_metric(name, signal[name], [evidence_id]) for name in
                                    ("nonzero_points", "previous_mean_bounds", "recent_mean_bounds") if name in signal)
                break
        refs.update(ref for metric in measurements for ref in metric["evidence_ids"])
        dates = [state["observations"][ref]["observed_at"] for ref in refs if ref in state["observations"]]
        if row.get("at"):
            dates.append(row["at"])
        urls = [state["observations"][ref]["facts"].get("url") for ref in sorted(refs) if ref in state["observations"]]
        detail += (" " + "; ".join(_measurement_text(metric) for metric in measurements) + ".") if measurements else ""
        functional = product.get("functional_market_contract") == "functional-market-scope-v1"
        # Legacy packets keep their existing reducer. The additive functional
        # contract has an explicit cross-language tie rule, not Python set order.
        latest_id = (max(check_ids, key=lambda ref: (timestamp(state["source_checks"][ref]["at"]), ref))
                     if functional and check_ids else None)
        latest = (state["source_checks"][latest_id] if latest_id is not None else
                  max(checks, key=lambda check: timestamp(check["at"])) if checks else None)
        last_attempt = None
        if latest and latest.get("kind") == CHECK_KIND:
            envelope = latest["envelope"]
            attempted = (envelope.get("dispatch_started") is True or any(
                envelope["acquisition_id"] in state["query_tasks"][task_id].get("attempt_refs", []) for task_id in task_ids))
            last_attempt = {"outcome": envelope["state"], "actual_attempt": attempted,
                            "at": latest["at"], "receipt_id": latest_id if latest_id is not None else
                            next(ref for ref in check_ids if state["source_checks"][ref] == latest)}
            if outcome == "completed_relevant" and envelope["state"] in ("blocked", "failed", "unavailable"):
                detail += ((" The most recent attempt " + ("was blocked" if envelope["state"] == "blocked" else "failed")) if attempted else
                           " The latest check could not start") + "; earlier saved relevant results are retained."
        output.append({"route": route, "source": ROUTE_LABELS[route], "source_role": ROUTES[route]["source"],
            "description_hash": scope, "outcome": outcome, "state": state_label, "actual_attempt": actual,
            "detail": detail, "measurements": measurements, "evidence_ids": sorted(refs), "task_ids": task_ids,
            "receipt_ids": sorted(check_ids), "observed_at": max(dates, key=timestamp) if dates else None,
            "url": next((url for url in urls if isinstance(url, str)), None), "last_attempt": last_attempt})
        if product.get("functional_market_contract") == "functional-market-scope-v1":
            proof = product["functional_market_context"]
            output[-1].update(meaning_hash=proof["meaning_hash"], scoped_evidence_ids=sorted(
                row["id"] for row in proof["scoped_evidence"] if row["route"] == route))
    return output


def validate_route_facts(product):
    if product.get("source_route_contract") not in CONTRACTS:
        raise SafetyError("LIVE_SOURCE_ROUTE_CONTRACT_INVALID")
    rows, revisions = product.get("source_route_checks"), product.get("source_revisions", {})
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or [row.get("route") for row in rows] not in (list(CLIENT_ROUTES), list(PACKET_ROUTES))):
        raise SafetyError("LIVE_SOURCE_ROUTE_FACTS_INVALID")
    try:
        if len(canonical(rows)) > MAX_BLOCK_BYTES:
            raise SafetyError("LIVE_SOURCE_ROUTE_FACTS_INVALID")
    except (ValueError, TypeError):
        raise SafetyError("LIVE_SOURCE_ROUTE_FACTS_INVALID") from None
    known = {ref for table in _TABLES for ref in revisions.get(table, {})}
    for row in rows:
        if product["source_route_contract"] == FUNCTIONAL_CONTRACT:
            if (product.get("recommendation_contract") != "functional-type-examples-v1"
                    or row.get("meaning_hash") != product["functional_definition"]["meaning_hash"]
                    or not isinstance(row.get("scoped_evidence_ids"), list)
                    or row["scoped_evidence_ids"] != sorted(set(row["scoped_evidence_ids"]))
                    or not set(row["scoped_evidence_ids"]) <= set(revisions.get("source_checks", {}))):
                raise SafetyError("LIVE_SOURCE_ROUTE_FUNCTIONAL_SCOPE_INVALID")
        if (row.get("source") != ROUTE_LABELS[row["route"]] or row.get("source_role") != ROUTES[row["route"]]["source"]
                or row.get("description_hash") != digest(product.get("description"))
                or not isinstance(row.get("outcome"), str) or row["outcome"] not in STATES
                or type(row.get("actual_attempt")) is not bool
                or row.get("state") != outcome_display(row["outcome"], row["actual_attempt"])[0]
                or not isinstance(row.get("detail"), str) or not 1 <= len(row["detail"]) <= 8000):
            raise SafetyError("LIVE_SOURCE_ROUTE_FACTS_INVALID")
        for field in ("evidence_ids", "task_ids", "receipt_ids"):
            values = row.get(field)
            if (not isinstance(values, list) or any(not isinstance(value, str) for value in values)
                    or len(values) != len(set(values)) or not set(values) <= known):
                raise SafetyError("LIVE_SOURCE_ROUTE_REFERENCE_INVALID")
        if (not set(row["task_ids"]) <= set(revisions.get("query_tasks", {}))
                or not set(row["receipt_ids"]) <= set(revisions.get("source_checks", {}))):
            raise SafetyError("LIVE_SOURCE_ROUTE_REFERENCE_INVALID")
        if row.get("observed_at"):
            timestamp(row["observed_at"])
        metrics = row.get("measurements")
        if (not isinstance(metrics, list) or len(metrics) > len(METRICS)
                or any(not isinstance(metric, dict) or not isinstance(metric.get("name"), str) for metric in metrics)
                or len({metric["name"] for metric in metrics}) != len(metrics)):
            raise SafetyError("LIVE_SOURCE_MEASUREMENT_INVALID")
        for metric in metrics:
            if not isinstance(metric, dict) or set(metric) != {"name", "value", "evidence_ids"} or metric["name"] not in METRICS:
                raise SafetyError("LIVE_SOURCE_MEASUREMENT_INVALID")
            value = metric["value"]
            values = value if isinstance(value, list) else [value]
            numbers = [_metric_number(v) for v in values]
            if ((isinstance(value, list) and (len(numbers) != 2 or any(v is None for v in numbers) or numbers[0] > numbers[1]))
                    or not isinstance(metric["evidence_ids"], list) or not metric["evidence_ids"]
                    or any(not isinstance(ref, str) for ref in metric["evidence_ids"])
                    or not set(metric["evidence_ids"]) <= set(row["evidence_ids"])):
                raise SafetyError("LIVE_SOURCE_MEASUREMENT_INVALID")
        last = row.get("last_attempt")
        if last is not None:
            if (not isinstance(last, dict) or set(last) != {"outcome", "actual_attempt", "at", "receipt_id"}
                    or not isinstance(last.get("outcome"), str)
                    or last["outcome"] not in {"usable", "partial", "interpretation_pending", "empty", "completed_no_results",
                                                  "insufficient", "blocked", "failed", "unavailable"}
                    or type(last["actual_attempt"]) is not bool
                    or last["receipt_id"] not in row["receipt_ids"]):
                raise SafetyError("LIVE_SOURCE_ROUTE_FACTS_INVALID")
            timestamp(last["at"])
