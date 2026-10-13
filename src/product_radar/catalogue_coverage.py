"""Exact family/phrase choices and conditional workload, not fresh-stock proof."""

from collections import Counter
from decimal import Decimal, ROUND_CEILING

from .catalogue import alias_groups, validate_catalogue
from .discovery import normalise
from .models import SafetyError, digest
from .research_records import is_paid_query_task
from .service import CATEGORIES
from .storage import atomic_json, safe_path

VERSION = "catalogue-choice-coverage-v1"


def _choice(family_id, phrase):
    return (family_id, normalise(phrase["text"]), phrase["source"], phrase["purpose"], phrase["language"], phrase["country"])


def _request_choice(family_id, request):
    request = request if isinstance(request, dict) else {}
    term = request.get("term")
    return (family_id, normalise(term) if isinstance(term, str) else "", request.get("source"),
            request.get("purpose"), request.get("language"), request.get("country"))


def catalogue_matrix(catalogue, state=None, *, financial_attempts=None):
    """Count real per-family choices; no arbitrary areas-times-phrases math.

    A saved dispatch proves an attempted question even when no usable capture
    follows. Capture binding is needed for evidence, not to count the attempt.
    Prepared tasks alone are not attempted searches. Uncertain submission is shown separately and
    is not safe unused runway. Unmapped product/list checks remain unmapped,
    rather than silently declaring every validation option tried or untouched.
    """
    validate_catalogue(catalogue)
    state = state or {}
    financial_attempts = financial_attempts or {}
    families = {row["id"]: row for row in catalogue["families"] if row["disposition"] == "usable"}
    choices = {}
    for family_id, family in families.items():
        for phrase in family["phrases"]:
            choice = _choice(family_id, phrase)
            choices.setdefault(choice, {"id": "choice-" + digest(choice)[:32], "family_id": family_id,
                "category": family["category"], "parent_id": family["parent_id"], "family": family["label"],
                "phrase": phrase["text"], "source": phrase["source"], "purpose": phrase["purpose"],
                "language": phrase["language"], "country": phrase["country"],
                "state": "not_tried" if phrase["source"] == "meta" and phrase["purpose"] == "discovery" else "coverage_not_mapped",
                "prepared_task_ids": [], "acquisition_ids": [], "uncertain_attempt_ids": []})
    unmapped = []
    from .research_cohort import discovery_counts, discovery_questions
    discovery = discovery_questions(state)
    covered_questions = set()
    for actual in discovery:
        cycle_id, question_id, action = actual["cycle_id"], actual["question_id"], actual["action"]
        covered_questions.add(question_id)
        choice = _request_choice(actual["family_id"], {"source": actual["source"],
            "purpose": actual["purpose"], "term": actual["term"],
            "language": actual["language"], "country": actual["country"]})
        if choice not in choices:
            if action:
                unmapped.append({"kind": "public_discovery_question", "id": question_id, "cycle_id": cycle_id,
                    "actually_attempted": action.get("dispatch_started"),
                    "reason": "Department list or alternate-source retrieval, not the original catalogue source/purpose choice."})
            continue
        row = choices[choice]
        if question_id not in row["prepared_task_ids"]: row["prepared_task_ids"].append(question_id)
        if action.get("dispatch_started") is True:
            row["state"] = "tried"
            if action.get("acquisition_id") and action["acquisition_id"] not in row["acquisition_ids"]:
                row["acquisition_ids"].append(action["acquisition_id"])
        elif action.get("state") in {"intent_saved", "execution_uncertain"}:
            if question_id not in row["uncertain_attempt_ids"]: row["uncertain_attempt_ids"].append(question_id)
            if row["state"] != "tried": row["state"] = "execution_uncertain"
        elif row["state"] in {"not_tried", "coverage_not_mapped"}:
            row["state"] = "prepared_not_dispatched"
    for outcome_id, outcome in state.get("query_outcomes", {}).items():
        request = outcome.get("request", {})
        choice = _request_choice(outcome.get("family_id"), request)
        capture = next((row for row in state.get("captures", {}).values()
                        if row.get("acquisition_id") == outcome.get("acquisition_id") and
                        row.get("observed_at") == outcome.get("observed_at")), None)
        if choice not in choices or capture is None:
            unmapped.append({"kind": "query_outcome", "id": outcome_id,
                            "reason": "No exact catalogue choice and retained capture binding."})
            continue
        row = choices[choice]
        row["state"] = "tried"
        if outcome["acquisition_id"] not in row["acquisition_ids"]:
            row["acquisition_ids"].append(outcome["acquisition_id"])
    for task_id, task in state.get("query_tasks", {}).items():
        if task_id in covered_questions:
            continue  # This real v2 direction was already handled through its wave binding.
        if not is_paid_query_task(task):
            unmapped.append({"kind": "query_task", "id": task_id,
                            "reason": "Not a recognised prepared paid task; its research contract remains separate."})
            continue
        request = task.get("request", {})
        choice = _request_choice(task.get("family_id"), request)
        if choice not in choices:
            unmapped.append({"kind": "query_task", "id": task_id,
                            "reason": "Not an exact family/phrase/source/purpose choice; kept separate."})
            continue
        row = choices[choice]
        row["prepared_task_ids"].append(task_id)
        attempt = financial_attempts.get(task.get("attempt_id"))
        if attempt is not None and attempt.get("submission") == "submitted" and attempt.get("run_id"):
            row["state"] = "tried"
            if attempt.get("dataset_id") and attempt["dataset_id"] not in row["acquisition_ids"]:
                row["acquisition_ids"].append(attempt["dataset_id"])
        elif attempt is not None and attempt.get("submission") == "uncertain":
            row["uncertain_attempt_ids"].append(task["attempt_id"])
            if row["state"] != "tried":
                row["state"] = "execution_uncertain"
        elif row["state"] in ("not_tried", "coverage_not_mapped"):
            row["state"] = "prepared_not_dispatched"
    rows = sorted(choices.values(), key=lambda row: (row["category"], row["parent_id"], row["family_id"], row["source"], row["purpose"], normalise(row["phrase"])))
    categories = {}
    groups = alias_groups(catalogue)
    for category in CATEGORIES:
        selected = [row for row in rows if row["category"] == category]
        meta = [row for row in selected if row["source"] == "meta" and row["purpose"] == "discovery"]
        names = [family for family in families.values() if family["category"] == category]
        categories[category] = {"families": len(names), "distinct_alias_groups": len({groups[row["id"]] for row in names}),
            "family_phrase_pairs": len({(row["family_id"], normalise(row["phrase"])) for row in selected}),
            "source_purpose_options": len(selected), "meta_discovery_options": len(meta),
            "meta_discovery_tried": sum(row["state"] == "tried" for row in meta),
            "meta_discovery_execution_uncertain": sum(row["state"] == "execution_uncertain" for row in meta),
            "meta_discovery_not_dispatched": sum(row["state"] in ("not_tried", "prepared_not_dispatched") for row in meta),
            "source_states": {source: dict(Counter(row["state"] for row in selected if row["source"] == source))
                for source in sorted({row["source"] for row in selected})}}
    totals = {name: sum(row[name] for row in categories.values()) for name in (
        "families", "family_phrase_pairs", "source_purpose_options", "meta_discovery_options", "meta_discovery_tried",
        "meta_discovery_execution_uncertain", "meta_discovery_not_dispatched")}
    totals["distinct_alias_groups"] = len({groups[family_id] for family_id in families})
    discovery_summary = discovery_counts(discovery)
    totals["actual_public_discovery_queries"] = discovery_summary["physical_discovery_reads"]
    totals["prepared_independent_discovery_questions"] = discovery_summary["prepared_questions"]
    totals["answered_independent_discovery_questions"] = discovery_summary["answered_questions"]
    return {"schema_version": 1, "version": VERSION, "revision": catalogue["revision"], "categories": categories,
        "totals": totals, "rows": rows, "unmapped": unmapped, "direct_discovery": discovery_summary,
        "note": "An attempted phrase does not exhaust a family. Synonyms and source variants are not new products. Product checks without exact catalogue bindings are listed separately."}


def workload_scenarios(matrix, *, horizon_weeks=26, issues_per_week=3, entries_per_issue=10,
                       queries_per_issue=(10, 20, 30), new_candidates_per_query=None, useful_yields=("0.05", "0.03")):
    """Show explicit consumption/yield assumptions without a coverage gate."""
    if (any(type(value) is not int or not 1 <= value <= 10000 for value in (horizon_weeks, issues_per_week, entries_per_issue))
            or not isinstance(queries_per_issue, (list, tuple)) or not 1 <= len(queries_per_issue) <= 10
            or any(type(value) is not int or not 1 <= value <= 10000 for value in queries_per_issue)):
        raise SafetyError("CATALOGUE_WORKLOAD_COUNTS_INVALID")
    issues = horizon_weeks * issues_per_week
    updates_per_issue = entries_per_issue // 5
    new_per_issue = entries_per_issue - updates_per_issue
    available = matrix["totals"]["meta_discovery_not_dispatched"]
    query_cases = [{"new_queries_per_issue": rate, "horizon_queries": rate * issues,
        "unused_meta_options": available, "workloads_at_assumed_rate": str(Decimal(available) / rate),
        "additional_choices_at_same_rate": max(0, rate * issues - available)} for rate in queries_per_issue]
    yields = []
    if new_candidates_per_query is not None:
        if type(new_candidates_per_query) is not int or not 1 <= new_candidates_per_query <= 100000:
            raise SafetyError("CATALOGUE_WORKLOAD_CANDIDATES_INVALID")
        if not isinstance(useful_yields, (list, tuple)) or not 1 <= len(useful_yields) <= 10:
            raise SafetyError("CATALOGUE_WORKLOAD_YIELDS_INVALID")
        for raw in useful_yields:
            try:
                rate = Decimal(raw)
            except (ValueError, TypeError, ArithmeticError):
                raise SafetyError("CATALOGUE_WORKLOAD_YIELDS_INVALID") from None
            if not rate.is_finite() or not 0 < rate <= 1:
                raise SafetyError("CATALOGUE_WORKLOAD_YIELDS_INVALID")
            for fresh in (new_per_issue, entries_per_issue):
                candidates = int((Decimal(fresh) / rate).to_integral_value(rounding=ROUND_CEILING))
                queries = (candidates + new_candidates_per_query - 1) // new_candidates_per_query
                yields.append({"useful_yield": str(rate), "new_products_per_issue": fresh,
                    "assumed_new_candidates_per_query": new_candidates_per_query,
                    "candidates_per_issue": candidates, "queries_per_issue": queries,
                    "horizon_candidates": candidates * issues, "horizon_queries": queries * issues,
                    "additional_choices_at_same_rate": max(0, queries * issues - available)})
    return {"horizon_weeks": horizon_weeks, "issues": issues, "entries": issues * entries_per_issue,
        "new_entries_if_maximum_useful_updates_exist": issues * new_per_issue,
        "new_entries_if_no_useful_updates": issues * entries_per_issue, "query_cases": query_cases, "yield_cases": yields,
        "note": "Approximate six-month planning only. Yield and new candidates per query are assumptions unless explicitly measured. Direct lists, genuine new functions, useful revisits and changed source mix can improve supply; repeats and failures can reduce it. No guarantee, padded-term quota or release blocker is created."}


def write_catalogue_coverage(memory, catalogue, *, financial_attempts=None, **scenario_options):
    state = memory.snapshot()
    matrix = catalogue_matrix(catalogue, state, financial_attempts=financial_attempts)
    result = {"matrix": matrix, "workload": workload_scenarios(matrix, **scenario_options)}
    path = safe_path(memory.root, "catalogue_choice_coverage.json")
    atomic_json(path, result)
    return {"reference": str(path), **result}
