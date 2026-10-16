"""Pure owner-only counts from supplied, already verified programme inputs.

This module opens no file, reads no key, acquires no lock and calls no source
or model. The caller supplies one compatible verified research snapshot, the
Journal.state() pair, existing account_pool_status output, an accepted effective
catalogue, build_report_pool output and the actual allocate_issues result.
Neither a prepared question nor an old checkpoint becomes a new request.

Optional effort_rows contains already reduced ``model``/``direct`` audit output
and explicit ``paid_attempt_ids`` for shared supporting acquisitions. Controller
envelopes are not passed to the incompatible direct_capture_effort reader here.
Optional forecast retains the original explicitly supplied yield_forecast API.
The owner cost_snapshot provides a separately selected historical reference and
safe completed-draft counts; neither checklist acceptance nor a shared average
becomes measured current yield or a forecasting cost unit. Allocation counts
describe proposals/plans, never files already generated.
"""

from collections import Counter
from datetime import date
from decimal import Decimal, ROUND_CEILING
from math import isfinite
from types import SimpleNamespace

from .brand_reviews import current_brand
from .catalogue_coverage import catalogue_matrix
from .fit_policy import FIT_POLICY_ID
from .fit_review import PRACTICAL_CONTRACT
from .fit_description import DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT
from .issue_allocation import ACTIVE_STATES
from .models import SafetyError, identifier, timestamp, usd
from .research_costs import collection_costs, collection_cost_totals, yield_forecast
from .programme_cost_snapshot import VERSION as COST_SNAPSHOT_VERSION, generated_draft_summary
from .research_flow import current_assessment_fit, evidence_indexes
from .research_records import is_paid_query_task
from .storage import Journal

VERSION = "programme-cost-capacity-summary-v1"


def _unknown(reason):
    return {"state": "unknown", "reason": reason}


def _part(function, *args):
    try:
        return function(*args)
    except (SafetyError, KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return _unknown("SUMMARY_INPUTS_MISSING_OR_INCONSISTENT")


def _ids(values):
    if not isinstance(values, list):
        raise SafetyError("SUMMARY_IDS_REQUIRED")
    return {identifier(value) for value in values}


def _count(value):
    if type(value) is not int or value < 0:
        raise SafetyError("SUMMARY_COUNT_INVALID")
    return value


def _fraction(numerator, denominator):
    if not denominator:
        return None
    return format((Decimal(numerator) / denominator).quantize(Decimal("0.00000001")), "f")


def _scopes(record, state=None):
    original = _ids(record["concept_ids"])
    children = record.get("follow_on_reviews", {})
    if not isinstance(children, dict):
        raise SafetyError("SUMMARY_SCOPES_INVALID")
    fresh, runs = set(), {identifier(record["source_run_id"])}
    for child in children.values():
        child_ids = _ids(child["concept_ids"])
        if child_ids & (original | fresh):
            raise SafetyError("SUMMARY_SCOPES_OVERLAP")
        fresh.update(child_ids); runs.add(identifier(child["source_run_id"]))
    inventory = _ids(record.get("reporting_inventory_concept_ids", []))
    if record.get("reporting_intake_ids"):
        if state is None:
            raise SafetyError("INVENTORY_VERIFIED_STATE_REQUIRED")
        from .inventory_intake import reporting_intake_concept_ids
        inventory |= reporting_intake_concept_ids(state, record)
    return original, fresh, original | fresh | inventory, runs


def _money(financial_state, effort):
    if financial_state is None:
        return _unknown("SUMMARY_LEDGER_NOT_SUPPLIED")
    if (not isinstance(financial_state, (list, tuple)) or len(financial_state) != 2
            or any(not isinstance(value, dict) for value in financial_state)):
        raise SafetyError("SUMMARY_LEDGER_INVALID")
    budgets, _ = financial_state
    # Existing functions only need state(); this adapter cannot read a journal.
    view = SimpleNamespace(state=lambda: financial_state)
    historical = collection_cost_totals(financial_state)
    future = sum((usd(row["policy"]["future_allocation"]) for row in budgets.values()), Decimal(0))
    headrooms = [Journal.spendable(view, namespace) for namespace in budgets]
    selected_ids = effort.get("paid_attempt_ids") if isinstance(effort, dict) else None
    selected = _part(lambda: collection_costs(view, list(_ids(selected_ids)))) if selected_ids is not None else None
    selected_known = selected is not None and selected.get("state") != "unknown"
    return {"state": "known", "ledger_physical_acquisitions": historical["physical_acquisitions"],
        "ledger_known_incurred_usd": historical["known_actual_usd"],
        "ledger_unsettled_hold_usd": historical["unsettled_hold_usd"],
        "ledger_accounting_complete": historical["accounting_complete"],
        "saved_future_source_allocations_usd": str(future),
        "saved_unallocated_headroom_all_periods_usd": str(sum((max(Decimal(0), value) for value in headrooms), Decimal(0))),
        "selected_supporting_cost_state": "known" if selected_known else "unknown",
        "selected_supporting_acquisitions": selected["physical_acquisitions"] if selected_known else None,
        "selected_known_incurred_usd": selected["known_actual_usd"] if selected_known else None,
        "selected_unsettled_hold_usd": selected["unsettled_hold_usd"] if selected_known else None,
        "shared_cost_attribution": "unallocated", "per_issue_cost_usd": None,
        "incremental_ai_invoice_usd": None, "hosting_and_computer_cost_usd": None,
        "note": "Ledger totals retain historical periods. Saved headroom is not a fresh provider balance or all funds earmarked for sources. Supporting-acquisition charges are shared historical expense, not this programme's exclusive incremental bill. No recorded shared-cost allocation rule has been applied."}


def _accounts(status, approved):
    _count(approved)
    if status is None:
        return {**_unknown("SUMMARY_ACCOUNT_STATUS_NOT_SUPPLIED"), "approved_accounts": approved,
                "configured_accounts": None, "saved_funded_accounts": None, "currently_verified_funded_accounts": None}
    rows = status["accounts"]
    if not isinstance(rows, list):
        raise SafetyError("SUMMARY_ACCOUNTS_INVALID")
    configured = _count(status["configured_financial_accounts"])
    saved = [row for row in rows if row.get("enabled") is True and row.get("state") == "saved_account"]
    return {"state": "known", "approved_accounts": approved, "registry_profiles": len(rows),
        "enabled_profiles": sum(row.get("enabled") is True for row in rows), "configured_accounts": configured,
        "saved_funded_accounts": sum(usd(row["opening_usd"]) > 0 for row in saved),
        "currently_verified_funded_accounts": None,
        "saved_enabled_unallocated_headroom_usd": str(sum((usd(row["spendable_usd"]) for row in saved), Decimal(0))),
        "pool_enabled": status["pool_enabled"],
        "note": "Configured grants and enabled saved headroom are dated offline facts. Existing account status supplies no current period/access proof, so currently usable provider funding remains unknown. Disabled/missing profiles and same-account aliases add no funding."}


def _catalogue(catalogue, state, financial_state):
    if catalogue is None:
        return _unknown("SUMMARY_ACCEPTED_CATALOGUE_NOT_SUPPLIED")
    attempts = financial_state[1] if financial_state is not None else None
    matrix = catalogue_matrix(catalogue, state, financial_attempts=attempts)
    # An incomplete *untyped* legacy row with an explicit attempt reference
    # cannot authorise paid execution, but its missing financial history also
    # cannot be described as proved-unused capacity. Typed research questions
    # and unrelated new record kinds do not acquire this legacy meaning.
    coverage_known = state is not None and (attempts is not None or not any(
        row.get("attempt_id") for row in state.get("query_tasks", {}).values()
        if is_paid_query_task(row) or "kind" not in row))
    inventory_fields = ("families", "distinct_alias_groups", "family_phrase_pairs", "source_purpose_options", "meta_discovery_options")
    attempt_fields = ("meta_discovery_tried", "meta_discovery_execution_uncertain", "meta_discovery_not_dispatched", "actual_public_discovery_queries")
    categories = {}
    for category, counts in matrix["categories"].items():
        source_purpose = Counter((row["source"], row["purpose"]) for row in matrix["rows"] if row["category"] == category)
        categories[category] = {**{name: counts[name] for name in inventory_fields},
            **{name: counts.get(name) if coverage_known else None for name in attempt_fields if name in counts},
            "source_purpose_counts": [{"source": source, "purpose": purpose, "choices": count}
                for (source, purpose), count in sorted(source_purpose.items())]}
    return {"state": "known" if coverage_known else "partial", "revision": matrix["revision"],
        "totals": {**{name: matrix["totals"][name] for name in inventory_fields},
                   **{name: matrix["totals"][name] if coverage_known else None for name in attempt_fields}},
        "categories": categories, "unmapped_saved_questions": len(matrix["unmapped"]) if state is not None else None,
        "note": "Only admitted families and exact normalized family/phrase/source/purpose/language/country choices count. An attempted choice is not an exhausted family. Unmapped validation checks are neither unused inventory nor fictitious tried choices. Prepared seed examples are not added capacity."}


def _inventory(record, state, pool, allocations, at, allocation_basis):
    if record is None or state is None or pool is None:
        return _unknown("SUMMARY_VERIFIED_REPORT_INPUTS_NOT_SUPPLIED")
    _, _, scope, _ = _scopes(record, state)
    client = record["client_id"]; today = timestamp(at).date()
    from .functional_aliases import alias_binding, alias_projection
    from .issue_allocation import _held, _published
    aliases = alias_projection(state) if alias_binding(state) is not None else {}
    published = _published(state["published_history"], client, aliases=aliases)
    held = _held(state["issue_reservations"], client_id=client, aliases=aliases, history=state["published_history"])
    eligible = {}
    for row in pool["eligible"]:
        cid = identifier(row["concept_id"])
        if cid in scope:
            if cid in eligible and eligible[cid] != row:
                raise SafetyError("SUMMARY_ELIGIBLE_DUPLICATE_CONFLICT")
            eligible[cid] = row
    from .research_cohort import evidence_expired
    stale = {cid for cid, row in eligible.items() if row.get("usable_until")
             and evidence_expired(row["usable_until"], timestamp(at))}
    usable = set(eligible) - stale
    new = usable - published
    if allocation_basis not in ("supplied_plan", "current_research_proposal"):
        raise SafetyError("SUMMARY_ALLOCATION_BASIS_INVALID")
    value = {"state": "known", "pool_as_of": pool.get("as_of"), "eligible_scoped_products": len(eligible),
        "stale_declared_products": len(stale), "eligible_unpublished_products": len(new),
        "eligible_reserved_products": len(usable & held), "reserved_unpublished_products": len(new & held),
        "available_unpublished_unreserved_products": len(new - held),
        "eligible_previously_published_products": len(usable & published),
        "logical_release_slots": len({row["issue_id"] for row in record["release_slots"]}),
        "allocation_basis": allocation_basis,
        "allocation_state": "unknown",
        "complete_allocated_issues": None, "incomplete_allocated_issues": None, "allocated_distinct_products": None,
        "allocated_new_products": None, "allocated_updates": None, "issue_shortfalls": None,
        "note": "Usable unreserved New stock is not fresh discovery yield. Allocated issue counts describe the supplied plan or current research proposal, not generated files. Saved completed logical drafts are reported separately; languages are not separate issues or research bills. This is not owner approval or client publication."}
    from .inventory_intake import current_intake_records, inventory_work_records
    intakes = current_intake_records(state, record, pool=pool)
    work = inventory_work_records(state, record)
    value.update(reporting_intake_new_types=len({row["binding"]["concept_id"] for row in intakes if row["binding"]["status"] == "New"}),
        reporting_intake_updated_types=len({row["binding"]["concept_id"] for row in intakes if row["binding"]["status"] == "Updated"}),
        inventory_work_states=dict(Counter(row["progress"]["state"] for row in work)),
        inventory_types_needing_specific_work=len({row["binding"]["subject"]["concept_id"] for row in work
            if row["progress"]["state"] not in {"complete", "superseded"}}))
    if allocations is None:
        return value
    allocation = _part(_allocated, record, allocations, scope)
    value["allocation_state"] = allocation["state"]
    if allocation["state"] == "known":
        value.update({name: count for name, count in allocation.items() if name != "state"})
    return value


def _allocated(record, allocations, scope):
    issue_rows, assigned, bindings = {}, set(), {}
    slots = {row["issue_id"]: row for row in record["release_slots"]}
    for issue in allocations["issues"]:
        iid = identifier(issue["issue_id"]); products = issue["products"]
        ids = _ids([row["concept_id"] for row in products])
        counts = {"products": len(products), "new": sum(row["status"] == "New" for row in products),
                  "updates": sum(row["status"] == "Updated" for row in products)}
        if (iid not in slots or issue.get("client_id") != slots[iid].get("client_id", record["client_id"])
                or issue.get("date") != slots[iid]["date"]
                or len(ids) != len(products) or not ids <= scope or counts != issue["counts"]
                or counts["new"] + counts["updates"] != counts["products"]
                or _count(issue["shortfall"]) != max(0, 10 - len(products))
                or type(issue["ready_for_review"]) is not bool
                or issue["ready_for_review"] != (10 <= len(products) <= 20 and counts["updates"] <= len(products) // 5)):
            raise SafetyError("SUMMARY_ALLOCATION_INVALID")
        compact = {"issue_id": iid, "products": counts["products"], "new": counts["new"],
                   "updates": counts["updates"], "shortfall": issue["shortfall"], "complete": issue["ready_for_review"]}
        product_binding = sorted((row["concept_id"], row["status"]) for row in products)
        if iid in issue_rows:
            if issue_rows[iid] != compact or bindings[iid] != product_binding:
                raise SafetyError("SUMMARY_ISSUE_DUPLICATE_CONFLICT")
            continue
        if assigned & ids:
            raise SafetyError("SUMMARY_ALLOCATED_PRODUCT_REPEATED")
        assigned.update(ids); issue_rows[iid] = compact; bindings[iid] = product_binding
    if set(issue_rows) != set(slots):
        raise SafetyError("SUMMARY_ALLOCATION_SLOTS_INCOMPLETE")
    rows = list(issue_rows.values())
    return {"state": "known", "complete_allocated_issues": sum(row["complete"] for row in rows),
        "incomplete_allocated_issues": sum(not row["complete"] for row in rows), "allocated_distinct_products": len(assigned),
        "allocated_new_products": sum(row["new"] for row in rows), "allocated_updates": sum(row["updates"] for row in rows),
        "issue_shortfalls": rows}


def _research(record, state, pool):
    if record is None or state is None:
        return _unknown("SUMMARY_VERIFIED_RESEARCH_INPUTS_NOT_SUPPLIED")
    original, follow_on, reporting, runs = _scopes(record, state)
    indexes = evidence_indexes(state)
    def fits(ids):
        complete = 0; eligible = 0; excluded = 0; missing = 0; uncertain = 0
        for cid in ids:
            concept = state["concepts"].get(cid)
            if concept is None:
                missing += 1
                continue
            try:
                if concept.get("status") in {"excluded", "merged", "retired"} or current_brand(
                        state, "concept", cid, concept["description"]) == "established":
                    excluded += 1
                    continue
            except (SafetyError, KeyError, TypeError, ValueError):
                uncertain += 1
                continue
            fit = current_assessment_fit(state, cid, expected_policy_id=FIT_POLICY_ID, indexes=indexes)
            from .functional_types import functional_role_context
            contracts = ((FUNCTIONAL_CONTRACT,) if functional_role_context(state, cid) is not None
                         else (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT))
            if fit and state["assessments"][cid].get("contract_version") in contracts:
                complete += 1; eligible += fit["eligible"]
        return {"products": len(ids), "deliberately_excluded_products": excluded, "missing_concept_records": missing,
                "unresolved_identity_bindings": uncertain, "review_targets": len(ids) - excluded - missing - uncertain,
                "current_practical_reviews": complete, "fit_passing_products": eligible,
                "not_currently_reviewed": len(ids) - excluded - missing - uncertain - complete}
    from .research_cohort import discovery_counts, discovery_questions
    discovered = discovery_questions(state, programme_id=record["programme_id"])
    cycles = [row for row in state["batch_runs"].values()
              if row.get("kind") in {"catalogue-discovery-cycle-v1", "discovery-wave-v2"}
              and row.get("programme_id") == record["programme_id"]]
    new = set().union(*(_ids(row.get("new_concept_ids", [])) for row in cycles)) if cycles else set()
    new -= original
    acquisitions = {action["acquisition_id"] for row in cycles for action in row.get("actions", {}).values()
                    if action.get("state") in {"acquired", "interpretation_pending"} and action.get("acquisition_id")}
    observations = [row for row in state["observations"].values() if row.get("acquisition_id") in acquisitions]
    decisions = {row["concept_id"]: row["decision"] for row in pool["decisions"]} if pool is not None else None
    eligible = {row["concept_id"] for row in pool["eligible"]} if pool is not None else None
    completed = {cid for cid in new if decisions.get(cid) in {"included", "excluded"}} if decisions is not None else None
    ready = len(new & eligible) if eligible is not None else None
    completed_ready = len(completed & eligible) if completed is not None and eligible is not None else None
    tasks = [row for row in state.get("query_tasks", {}).values()
             if row.get("kind") == "all_source_task_v1" and row.get("run_id") in runs]
    attempts = {ref for row in tasks for ref in row.get("attempt_refs", [])}
    return {"state": "known", "original_pool": fits(original), "follow_on_pool": fits(follow_on),
        "reporting_only_pool": fits(reporting - original - follow_on),
        "discovery_cycles": len(cycles), "discovery_waves": sum(row["kind"] == "discovery-wave-v2" for row in cycles),
        "direct_discovery": discovery_counts(discovered), "discovery_capture_rows": len(observations),
        "discovery_unique_source_items": len({row["semantic_id"] for row in observations}),
        "discovery_new_distinct_concepts": len(new), "currently_eligible_fresh_concepts": ready,
        "fresh_completed_report_decisions": len(completed) if completed is not None else None,
        "fresh_decisions_pending": len(new - completed) if completed is not None else None,
        "fresh_completed_decision_eligibility_fraction": _fraction(completed_ready, len(completed)) if completed_ready is not None else None,
        "fresh_pool_ready_fraction": _fraction(ready, len(new)) if completed == new and ready is not None else None,
        "prepared_source_queries": len(tasks), "shared_controller_acquisition_attempts": len(attempts),
        "prepared_product_route_questions": len({(cid, row["route"]) for row in tasks for cid in row.get("targets", {})}),
        "task_states": dict(Counter(row["status"] for row in tasks)),
        "route_queries": dict(Counter(row["route"] for row in tasks)),
        "note": "Original-pool checklist acceptance, fresh identity admission and current report readiness are separate. An incomplete fresh pool has no final yield. Completed report decisions do not assert all optional source checks are complete. Shared controller acquisitions are not underlying browser/API request-stage counts."}


def _effort(effort):
    if effort is None:
        return _unknown("SUMMARY_REDUCED_EFFORT_NOT_SUPPLIED")
    value = {"state": "unknown", "model": None, "direct": None, "incremental_ai_invoice_usd": None}
    model = effort.get("model")
    if model is not None:
        fields = ("saved_job_attempts", "usage_receipts", "attempts_without_usable_token_counts", "duration_receipts")
        value["model"] = {name: _count(model[name]) for name in fields}
        if (model["usage_receipts"] + model["attempts_without_usable_token_counts"] != model["saved_job_attempts"]
                or model["duration_receipts"] > model["saved_job_attempts"]):
            raise SafetyError("SUMMARY_MODEL_COUNTS_INVALID")
        tokens = model["recorded_tokens"]
        value["model"]["recorded_tokens"] = {name: _count(tokens[name]) for name in ("input_tokens", "cached_input_tokens", "output_tokens")}
        if tokens["cached_input_tokens"] > tokens["input_tokens"]:
            raise SafetyError("SUMMARY_TOKENS_INVALID")
        duration = model["recorded_duration_sum_seconds"]
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not isfinite(duration) or duration < 0:
            raise SafetyError("SUMMARY_DURATION_INVALID")
        value["model"]["recorded_duration_sum_seconds"] = duration
    direct = effort.get("direct")
    if direct is not None:
        fields = ("recognised_acquisitions", "actual_dispatches", "proved_pre_dispatch_failures", "uncertain_dispatches")
        value["direct"] = {name: _count(direct[name]) for name in fields}
        value["direct"]["states"] = {name: _count(count) for name, count in direct["states"].items()}
        if (direct["actual_dispatches"] + direct["proved_pre_dispatch_failures"] + direct["uncertain_dispatches"]
                != direct["recognised_acquisitions"] or sum(value["direct"]["states"].values()) != direct["recognised_acquisitions"]):
            raise SafetyError("SUMMARY_DIRECT_COUNTS_INVALID")
    present = sum(value[name] is not None for name in ("model", "direct"))
    value["state"] = "known" if present == 2 else "partial" if present == 1 else "unknown"
    if present == 0:
        value["reason"] = "SUMMARY_REDUCED_MODEL_AND_DIRECT_EFFORT_NOT_SUPPLIED"
    value["note"] = "Only supplied reduced receipt facts are measured. Tokens are not a cash invoice; duration sums are not elapsed programme time. Direct acquisitions are not necessarily individual browser/API stages. Missing measures stay unknown."
    return value


def _forecast(forecast):
    if forecast is None:
        return {**_unknown("SUMMARY_MEASURED_FORECAST_BASELINE_NOT_SUPPLIED"), "assumed_yields": ["0.05", "0.03"]}
    allowed = ("baseline_collection_usd", "baseline_candidates", "new_products_per_report", "report_count", "separately_estimated_usd")
    result = yield_forecast(**{name: forecast[name] for name in allowed if name in forecast}, yields=("0.05", "0.03"))
    return dict(result, state="known", assumed_yields=["0.05", "0.03"], allowance_fit_not_inferred=True)


def _cost_context(snapshot, money, record, at):
    """Consume the pure builder's versioned snapshot, not unchecked raw receipts."""
    if snapshot is None:
        return _unknown("SUMMARY_OWNER_COST_SNAPSHOT_NOT_SUPPLIED")
    if (not isinstance(snapshot, dict) or type(snapshot.get("schema_version")) is not int
            or snapshot["schema_version"] != 1 or snapshot.get("version") != COST_SNAPSHOT_VERSION
            or snapshot.get("owner_only") is not True or snapshot.get("per_issue_cash_cost_usd") is not None
            or snapshot.get("client_publication_attempted") is not False or snapshot.get("as_of") != at):
        raise SafetyError("SUMMARY_COST_SNAPSHOT_INVALID")
    average = snapshot["shared_project_cost_average"]
    generated = snapshot["generated_reports"]
    forecast = snapshot["conditional_forecast"]
    lineage = snapshot["supporting_paid_capture_lineage"]
    for section in (average, generated, forecast, lineage):
        if not isinstance(section, dict) or section.get("state") not in ("known", "partial", "unknown"):
            raise SafetyError("SUMMARY_COST_SNAPSHOT_INVALID")
    if average.get("exclusive_per_issue_cash_cost_usd") is not None or average.get("all_in_per_issue_cost_usd") is not None:
        raise SafetyError("SUMMARY_COST_SNAPSHOT_EXCLUSIVE_BILL_INVALID")
    if average["state"] == "known":
        count = _count(generated["complete_generated_logical_issues"])
        if (count == 0 or generated["state"] != "known" or average["completed_generated_logical_issues"] != count
                or money.get("state") != "known"
                or Decimal(average["known_project_collection_usd"]) != Decimal(money["ledger_known_incurred_usd"])
                or Decimal(average["known_shared_average_usd"]) != (Decimal(money["ledger_known_incurred_usd"]) / count
                    ).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)):
            raise SafetyError("SUMMARY_COST_SNAPSHOT_AVERAGE_BINDING_INVALID")
    if record is not None and generated["state"] in ("known", "partial"):
        expected = {row["issue_id"] for row in record["release_slots"]}
        if any(row["issue_id"] not in expected for row in generated["issues"]):
            raise SafetyError("SUMMARY_COST_SNAPSHOT_GENERATED_SCOPE_INVALID")
    return {"state": "known", "shared_project_cost_average": average, "generated_reports": generated,
            "conditional_forecast": forecast, "supporting_paid_capture_lineage": lineage,
            "current_research_cohort": snapshot.get("current_research_cohort", _unknown("SUMMARY_CURRENT_COHORT_NOT_SUPPLIED")),
            "current_forecast": snapshot.get("current_forecast", _unknown("SUMMARY_CURRENT_FORECAST_NOT_SUPPLIED")),
            "warnings": snapshot["warnings"]}


def programme_cost_capacity_summary(record=None, *, state=None, financial_state=None, account_status=None,
                                    catalogue=None, report_pool=None, allocations=None, effort_rows=None,
                                    forecast=None, approved_accounts=3, as_of=None, cost_snapshot=None,
                                    allocation_basis="supplied_plan", project_root=None):
    """Return plain saved counts, never a provider bill, queue or new action.

    Invalid/missing sections become unknown without suppressing healthy facts.
    Existing account status cannot prove current access or period availability.
    The supplied catalogue must already be the accepted effective view; applying
    admissions again could double-count or conflict with a caller's merged view.
    """
    saved_record = record if isinstance(record, dict) else {}
    at = as_of or saved_record.get("updated_at") or saved_record.get("created_at")
    try:
        timestamp(at)
    except SafetyError:
        at = None
    money = _part(_money, financial_state, effort_rows)
    context = _part(_cost_context, cost_snapshot, money, record, at)
    if context["state"] == "known":
        generated = context["generated_reports"]
        average = context["shared_project_cost_average"]
        derived_forecast = context["conditional_forecast"]
        lineage = context["supporting_paid_capture_lineage"]
        warnings = context["warnings"]
    else:
        generated = generated_draft_summary(record, project_root=project_root)
        average = _unknown("SUMMARY_SHARED_PROJECT_DRAFT_AVERAGE_NOT_SUPPLIED")
        lineage = _unknown("SUMMARY_SUPPORTING_CAPTURE_LINEAGE_NOT_SUPPLIED")
        derived_forecast = _part(_forecast, None)
        warnings = [{"code": "OWNER_COST_SNAPSHOT_UNKNOWN"}]
    return {"schema_version": 1, "version": VERSION, "as_of": at, "owner_only": True,
        "money": money,
        "accounts": _part(_accounts, account_status, approved_accounts),
        "catalogue": _part(_catalogue, catalogue, state, financial_state),
        "research": _part(_research, record, state, report_pool),
        "report_supply": _part(_inventory, record, state, report_pool, allocations, at, allocation_basis),
        "generated_reports": generated, "shared_project_cost_average": average,
        "supporting_paid_capture_lineage": lineage,
        "effort": _part(_effort, effort_rows),
        "forecast": _part(_forecast, forecast) if forecast is not None else derived_forecast,
        "current_research_cohort": context.get("current_research_cohort", _unknown("SUMMARY_CURRENT_COHORT_NOT_SUPPLIED")),
        "current_forecast": context.get("current_forecast", _unknown("SUMMARY_CURRENT_FORECAST_NOT_SUPPLIED")),
        "historical_forecast_is_not_current_source_cost": True,
        "cost_warnings": warnings,
        "per_issue_cash_cost_usd": None, "client_publication_attempted": False}


METER_VERSION = "owner-meter-v1"
METER_BASELINE_VERSION = "owner-meter-baseline-v1"
METER_COUNTS = ("candidates", "accepted_narrow_types", "assessed_types", "assessment_passing_types",
                "report_eligible_types", "report_sets_produced", "ai_jobs")


def _meter_part(function, *args):
    try:
        return function(*args)
    except (SafetyError, KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as error:
        return _unknown(str(error) if isinstance(error, SafetyError) else "METER_INPUTS_INCONSISTENT")


def meter_counts(state, report_pool, programmes, project_root=None, recommendation_contract=None):
    """Cumulative saved counts from one verified snapshot; no request is implied.

    Candidates are active saved product concepts. Accepted narrow types have an
    accepted manufacturer-independent definition; assessed types also have a
    current ten-point assessment. Report-eligible types are current-format pool
    rows. Report sets are completed logical drafts (all languages count once).
    """
    from .functional_types import functional_type_inventory
    inventory = functional_type_inventory(state)
    accepted = [row["concept_id"] for row in inventory["concepts"] if row["accepted_definition"]]
    indexes = evidence_indexes(state)
    assessed = passing = 0
    for concept_id in accepted:
        fit = current_assessment_fit(state, concept_id, expected_policy_id=FIT_POLICY_ID, indexes=indexes)
        if fit and state["assessments"][concept_id].get("contract_version") == FUNCTIONAL_CONTRACT:
            assessed += 1
            passing += bool(fit["eligible"])
    eligible = None
    if report_pool is not None:
        eligible = len({identifier(row["concept_id"]) for row in report_pool["eligible"]
                        if recommendation_contract is None or row.get("recommendation_contract") == recommendation_contract})
    produced = 0
    for record in programmes:
        generated = generated_draft_summary(record, project_root=project_root)
        if generated.get("state") == "unknown":
            raise SafetyError("METER_PROGRAMME_DRAFTS_UNREADABLE")
        produced += _count(generated["known_generated_logical_issues"])
    return {"candidates": _count(inventory["active_concepts"]), "accepted_narrow_types": len(accepted),
            "assessed_types": assessed, "assessment_passing_types": passing,
            "report_eligible_types": eligible, "report_sets_produced": produced,
            "ai_jobs": len(state.get("jobs", {})), "unreadable_type_records": len(inventory["unresolved"])}


def validate_meter_baseline(value):
    fields = {"schema_version", "kind", "started_at", "reason", "carried_attempt_ids", "carried_known_usd",
              "carried_unsettled_hold_usd", "counts"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["kind"] != METER_BASELINE_VERSION
            or not isinstance(value["reason"], str) or not 1 <= len(value["reason"].strip()) <= 500
            or not isinstance(value["carried_attempt_ids"], list)
            or len(set(value["carried_attempt_ids"])) != len(value["carried_attempt_ids"])
            or not isinstance(value["counts"], dict) or set(value["counts"]) != set(METER_COUNTS)
            or any(type(count) is not int or count < 0 for count in value["counts"].values())):
        raise SafetyError("METER_BASELINE_INVALID")
    timestamp(value["started_at"])
    _ids(value["carried_attempt_ids"])
    usd(value["carried_known_usd"]); usd(value["carried_unsettled_hold_usd"])
    return value


def _usd_text(value):
    return format(value.normalize(), "f") if value else "0"


def _meter_costs(financial_state, baseline):
    budgets, attempts = financial_state
    carried = baseline["carried_attempt_ids"]
    if any(attempt_id not in attempts for attempt_id in carried):
        raise SafetyError("METER_CARRIED_ATTEMPT_MISSING")
    kept = set(carried)
    fresh = [attempt_id for attempt_id in attempts if attempt_id not in kept]
    totals = collection_cost_totals(financial_state, fresh)
    unknown = sum((attempts[a]["charge"] is None) + (attempts[a]["read_charge"] is None)
                  + sum(read["charge"] is None for read in attempts[a].get("renewed_reads", {}).values())
                  for a in fresh)
    before = collection_cost_totals(financial_state, carried)
    view = SimpleNamespace(state=lambda: financial_state)
    accounts = {namespace: {"opening_usd": row["allowance"],
                            "earmarked_for_sources_usd": row.get("policy", {}).get("future_allocation"),
                            "unallocated_headroom_usd": str(Journal.spendable(view, namespace))}
                for namespace, row in sorted(budgets.items())}
    return {"state": "known", "provider_collections": totals["physical_acquisitions"],
        "collections_by_source": totals["acquisitions_by_source"],
        "known_usd": totals["known_actual_usd"], "reserved_not_billed_usd": totals["unsettled_hold_usd"],
        "unknown_charge_items": unknown, "accounting_complete": totals["accounting_complete"],
        "carried_over_known_change_usd": _usd_text(usd(before["known_actual_usd"]) - usd(baseline["carried_known_usd"])),
        "carried_over_reserved_change_usd": _usd_text(usd(before["unsettled_hold_usd"]) - usd(baseline["carried_unsettled_hold_usd"])),
        "account_periods": accounts,
        "note": "Provider collection money only (the saved spending ledger). Reserved amounts are holds, not bills; unknown items are charges not yet confirmed. Spending before the fresh start stays in the guard but is excluded here. AI subscription and hosting are separate."}


def _meter_elapsed(started_at, at):
    seconds = max(0, int((timestamp(at) - timestamp(started_at)).total_seconds()))
    return {"seconds": seconds, "text": f"{seconds // 86400}d {seconds % 86400 // 3600}h {seconds % 3600 // 60}m"}


def owner_meter(*, baseline, state, financial_state, report_pool, programmes, activity, run_log, as_of,
                project_root=None, recommendation_contract=None):
    """One owner meter since the saved fresh start: plain saved counts only.

    No file, lock, provider or model. Missing/inconsistent optional inputs make
    only their own section unknown. "since_start" subtracts the baseline counts.
    """
    timestamp(as_of)
    baseline = _meter_part(validate_meter_baseline, baseline) if baseline is not None else _unknown("METER_NOT_STARTED")
    started = baseline.get("state") != "unknown"
    missing = None if started else _unknown(baseline["reason"])
    counts = _meter_part(meter_counts, state, report_pool, programmes, project_root, recommendation_contract)
    since = missing or (_unknown(counts["reason"]) if counts.get("state") == "unknown" else {
        name: None if counts[name] is None else counts[name] - baseline["counts"][name] for name in METER_COUNTS})
    costs = missing or (_unknown("METER_LEDGER_NOT_SUPPLIED") if financial_state is None
                        else _meter_part(_meter_costs, financial_state, baseline))
    keep = ("state", "message", "attention", "run_id", "step", "phase", "last_reason", "started_at", "observed_at",
            "last_useful_result_at", "seconds_since_activity", "seconds_since_useful_result", "counts")
    from .source_coverage import source_access_backoffs
    access_waits = (_meter_part(lambda: source_access_backoffs(state, as_of=as_of))
                    if isinstance(state, dict) else _unknown("METER_STATE_NOT_SUPPLIED"))
    return {"schema_version": 1, "kind": METER_VERSION, "checked_at": as_of,
        "started_at": baseline["started_at"] if started else None,
        "elapsed": _meter_part(_meter_elapsed, baseline["started_at"], as_of) if started else missing,
        "elapsed_since_first_research_activity": (_meter_part(_meter_elapsed, run_log["first_event_at"], as_of)
            if isinstance(run_log, dict) and run_log.get("first_event_at") else _unknown("METER_NO_RESEARCH_ACTIVITY_YET")),
        "cost_since_start": costs, "research_now": counts, "research_since_start": since,
        "activity_now": {name: activity[name] for name in keep if isinstance(activity, dict) and name in activity},
        "runs_since_start": run_log if run_log is not None else _unknown("METER_RUN_LOG_NOT_SUPPLIED"),
        # Websites currently refusing us, with how many refusals in a row and
        # the page's own reason ("challenge" is true for any page saved blocked).
        "access_waits_now": access_waits,
        "provider_calls": 0, "model_calls": 0, "research_writes": 0, "financial_writes": 0}
