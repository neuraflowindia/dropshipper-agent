"""Pure owner measurements from actual discovery originals, never execution.

The current programme's discovery cohort is separate from the old paid sample,
from existing stock, and from project-wide model effort. Missing receipts or
unfinished interpretation stay explicit; a prepared question is not a search.
"""

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .models import SafetyError, digest, identifier, timestamp, usd
from .research_costs import collection_costs


VERSION = "current-research-cohort-v1"
FORECAST_VERSION = "current-cohort-forecast-v1"
_SOURCES = {"meta-ads": "meta", "amazon-search": "marketplace", "flipkart-search": "marketplace",
            "amazon-bestsellers": "marketplace", "amazon-movers-and-shakers": "marketplace", "google-trends": "trends",
            "blinkit": "quickcommerce", "zepto": "quickcommerce", "instamart": "quickcommerce"}


def discovery_questions(state, *, programme_id=None, as_of=None):
    """Retain one row per real wave/cycle question, with original bindings."""
    from .catalogue_discovery import VERSION as OLD, WAVE_VERSION, validate_discovery_wave
    from .research_questions import validate_question_record
    cutoff = timestamp(as_of) if as_of is not None else None
    output = []
    for rid, cycle in sorted(state.get("batch_runs", {}).items()):
        if cycle.get("kind") not in {OLD, WAVE_VERSION}: continue
        if programme_id is not None and cycle.get("programme_id") != programme_id: continue
        if cutoff is not None and cycle.get("prepared_at") and timestamp(cycle["prepared_at"]) > cutoff: continue
        modern = cycle["kind"] == WAVE_VERSION
        if modern: validate_discovery_wave(state, cycle)
        for planned in cycle.get("planned_actions", []):
            for question in planned.get("questions", []):
                saved = validate_question_record(state["query_tasks"].get(question["id"])) if modern else None
                action = cycle.get("actions", {}).get(question["id"], {})
                # Future saved progress cannot be reported at an earlier cutoff.
                if cutoff is not None and action.get("observed_at") and timestamp(action["observed_at"]) > cutoff:
                    action = {}
                request = question["request"]
                output.append({"cycle_id": rid, "cycle_kind": cycle["kind"], "programme_id": cycle.get("programme_id"),
                    "question_id": question["id"], "family_id": planned.get("family_id"), "route": question["route"],
                    "source": _SOURCES.get(question["route"]),
                    "purpose": saved["purpose"] if saved else question.get("choice_purpose"),
                    "language": (saved["subject"].get("language") or request.get("language", "en")) if saved else question.get("language"),
                    "country": request.get("country"), "term": request.get("query") or request.get("term"),
                    "action": action, "question": saved, "cycle": cycle})
    return output


def discovery_counts(rows):
    """Count answered questions separately from deduplicated physical reads."""
    prepared, attempted, answered, reused, uncertain, physical, uncertain_originals = (set() for _ in range(7))
    routes = {}
    for row in rows:
        qid, action = row["question_id"], row["action"]
        prepared.add(qid)
        route = routes.setdefault(row["route"], {"prepared": set(), "attempted": set(), "answered": set(), "physical": set()})
        route["prepared"].add(qid)
        if action.get("dispatch_started") is True:
            attempted.add(qid); route["attempted"].add(qid)
            if action.get("reused_acquisition") is True: reused.add(qid)
            else:
                origin = action.get("acquisition_id") or [row["cycle_id"], qid]
                origin = digest(origin)
                physical.add(origin); route["physical"].add(origin)
        # Earlier successful work for this same durable question must not
        # hide a newer uncertain original. A proved start and lost reply may
        # also legitimately be both attempted and uncertain.
        if action.get("state") in {"intent_saved", "execution_uncertain"}:
            uncertain.add(qid)
            uncertain_originals.add(digest(action.get("acquisition_id") or [row["cycle_id"], qid]))
        if action.get("state") in {"acquired", "interpretation_pending", "empty"}:
            answered.add(qid); route["answered"].add(qid)
    return {"prepared_questions": len(prepared), "attempted_questions": len(attempted), "answered_questions": len(answered),
        "questions_reusing_original_reads": len(reused), "uncertain_questions": len(uncertain),
        "uncertain_originals": len(uncertain_originals),
        "physical_discovery_reads": len(physical),
        "routes": {route: {key: len(value) for key, value in counts.items()} for route, counts in sorted(routes.items())}}


def evidence_expired(value, at):
    """Keep old date-only views; current timestamps expire at the exact instant."""
    if isinstance(value, str) and len(value) == 10:
        return date.fromisoformat(value) < at.date()
    return timestamp(value) <= at


def _identities(state, record):
    from .functional_aliases import alias_binding, alias_projection
    from .issue_allocation import _held, _published
    aliases = alias_projection(state) if alias_binding(state) is not None else {}
    published = _published(state.get("published_history", {}), record["client_id"], aliases=aliases)
    held = _held(state.get("issue_reservations", {}), client_id=record["client_id"], aliases=aliases,
                 history=state.get("published_history", {}))
    return aliases, published, held


def _stock(state, record, pool, at):
    aliases, published, held = _identities(state, record)
    scope = {identifier(cid) for cid in record["concept_ids"]}
    for child in record.get("follow_on_reviews", {}).values():
        scope.update(identifier(cid) for cid in child["concept_ids"])
    scope.update(identifier(cid) for cid in record.get("reporting_inventory_concept_ids", []))
    if record.get("reporting_intake_ids"):
        from .inventory_intake import reporting_intake_concept_ids
        scope.update(reporting_intake_concept_ids(state, record))
    new, updates = set(), set()
    for product in pool["eligible"]:
        cid = identifier(product["concept_id"])
        if cid not in scope: continue
        if product.get("usable_until") and evidence_expired(product["usable_until"], at): continue
        identity = aliases.get(cid, {}).get("canonical_id", cid)
        if identity is None: raise SafetyError("COHORT_ALIAS_IDENTITY_INVALID")
        if cid in held: continue
        if cid not in published and product.get("status") == "New": new.add(identity)
        elif cid in published and product.get("status") == "Updated": updates.add(identity)
    return {"available_new_types": len(new), "accepted_available_updates": len(updates),
            "current_only_not_guaranteed_fresh_for_the_whole_month": True}


def _dated_original(state, acquisition_id, as_of):
    from .source_acquisitions import _record, _stage
    owner = _record(state, acquisition_id)
    stages = [_stage(state, owner, sid) for sid in owner["stage_ids"]]
    stages = [row for row in stages if timestamp(row.get("observed_at", row["recorded_at"])) <= as_of]
    responses = [row for row in stages if row.get("stage_type") == "response"]
    dates = [timestamp(row["observed_at"]) for row in responses if row.get("dispatch_started") is True]
    return owner, stages, responses, min(dates) if dates else None


def _original_costs(state, acquisition_ids, financial_state, *, started_at, as_of):
    zero, paid, unknown, pending, reused = set(), set(), set(), set(), set()
    routes, channels, originals = Counter(), Counter(), []
    for aid in sorted(set(acquisition_ids)):
        if aid not in state.get("batch_runs", {}) or state["batch_runs"][aid].get("kind") != "source-acquisition-owner-v1":
            unknown.add(aid); continue
        owner, stages, responses, collected_at = _dated_original(state, aid, as_of)
        binding = owner["binding"]
        if timestamp(owner["created_at"]) > as_of: continue
        # A reservation may predate its first actual read. Only proved older
        # collected bytes are saved reuse; an old uncertain intent is not free.
        if collected_at is not None and collected_at < started_at:
            reused.add(aid); continue
        # Dispatch intent is uncertainty, not a proved started request or free bill.
        attempted = any(row.get("dispatch_started") is True for row in responses)
        if not attempted and any(row.get("stage_type") in {"dispatch_intent", "execution_unknown"} for row in stages): pending.add(aid)
        routes[binding["route"]] += 1; channels[binding["collection_channel"]] += 1
        originals.append({"acquisition_id": aid, "binding_hash": owner["binding_hash"],
                          "stage_hashes": sorted(row["record_hash"] for row in stages)})
        proof = next((row.get("expense_proof") for row in reversed(responses) if row.get("expense_proof") is not None), None)
        if (binding["collection_channel"] == "direct_local" and binding["incremental_collection_fee"] == "known_zero"
                and proof is not None and proof.get("collection_channel") == "direct_local"
                and proof.get("provider_charge_usd") is not None and attempted):
            # Require the actual recorded response's expense proof too; a
            # proposed known-zero question/owner is not a completed free bill.
            if usd(proof["provider_charge_usd"]) != 0:
                raise SafetyError("COHORT_ZERO_FEE_PROOF_CONFLICT")
            zero.add(aid)
        elif binding["original_attempt_id"] in (financial_state[1] if financial_state is not None else {}):
            paid.add(binding["original_attempt_id"])
        else: unknown.add(aid)
    costs = collection_costs(SimpleNamespace(state=lambda: financial_state), sorted(paid)) if financial_state is not None else None
    known = costs["known_actual_usd"] if costs else "0"
    complete = not unknown and not pending and (costs is None or costs["accounting_complete"])
    return {"state": "known" if complete else "partial", "known_provider_collection_usd": known,
        "provider_collection_usd": known if complete else None,
        "unsettled_provider_holds_usd": costs["unsettled_hold_usd"] if costs else "0",
        "known_zero_originals": len(zero), "paid_attempts": len(paid), "unknown_fee_originals": len(unknown),
        "pending_originals": len(pending), "saved_older_originals_reused": len(reused),
        "current_originals": len(originals), "routes": dict(sorted(routes.items())), "channels": dict(sorted(channels.items())),
        "original_binding_hash": digest(originals), "paid_attempt_ids": sorted(paid),
        "direct_collection": {"proved_known_zero_originals": len(zero), "known_provider_charge_usd": "0"},
        "funded_collection": {"linked_paid_attempts": len(paid), "known_provider_charge_usd": known,
                              "unsettled_holds_usd": costs["unsettled_hold_usd"] if costs else "0"},
        "unpriced_collection": {"originals": len(unknown), "provider_charge_usd": None if unknown else "0"},
        "note": "Shared original collection charges count once, including failed reads. Older saved originals are reuse, not new collection. Missing or unsettled charges are not zero; AI and hosting remain separate."}


def _programme_model_effort(state, programme_id, measurement, at):
    """Join already reduced actual receipts, not new file or model probes."""
    from .work_turns import _record
    from .operational_effort_cache import _row
    admitted = set()
    for tid, turn in state.get("batch_runs", {}).items():
        if turn.get("kind") != "work-turn-v1" or turn.get("programme_id") != programme_id: continue
        _record(state, tid)
        for reservation in turn["reservations"]["model"].values():
            if timestamp(reservation["at"]) > at: continue
            job = state.get("jobs", {}).get(reservation["job_id"])
            if not job or digest(job["request"]) != reservation["request_hash"]:
                raise SafetyError("COHORT_MODEL_OWNER_CHANGED")
            # Work-turn ownership names the first folder "original"; the
            # existing receipt reducer names that same folder "initial".
            attempt = "initial" if reservation["attempt"] == "original" else reservation["attempt"]
            admitted.add((reservation["job_id"], attempt))
    receipts = {}
    for row in (measurement or {}).get("rows", []):
        pair = (row["job_id"], row["attempt"])
        if pair not in admitted: continue
        _row(row, row["job_id"])
        if pair in receipts and receipts[pair] != row: raise SafetyError("COHORT_MODEL_RECEIPT_CONFLICT")
        receipts[pair] = row
    usage = [row["usage"] for row in receipts.values() if row["usage"] is not None]
    return {"scope": "shared_current_programme_not_exclusive_discovery_or_report_cost",
        "admitted_job_attempts": len(admitted), "measured_job_attempts": len(receipts),
        "actual_execution_receipts": sum(row["execution_receipt_saved"] for row in receipts.values()),
        "saved_response_receipts": sum(row["response_saved"] for row in receipts.values()),
        "usage_receipts": len(usage), "attempts_without_current_measurement": len(admitted - receipts.keys()),
        "recorded_tokens": {field: sum(row[field] for row in usage) for field in ("input_tokens", "cached_input_tokens", "output_tokens")},
        "incremental_invoice_usd": None, "reason": "COHORT_MODEL_INVOICE_NOT_RECORDED",
        "note": "Reservations are not executed calls. Only supplied original receipt measurements count as execution/token effort. Shared programme work is not allocated exclusively to a report or discovery candidate; subscription usage is not an inferred cash invoice."}


def current_research_cohort(record, state, financial_state, pool, *, as_of, model_effort=None):
    """A current programme sample, not a charge assigned to one report."""
    from .catalogue_discovery import _acquisition_progress
    at = timestamp(as_of)
    if not record or not state or pool is None:
        return {"state": "insufficient_sample", "version": VERSION, "reason": "COHORT_CURRENT_VERIFIED_POOL_NOT_SUPPLIED"}
    rows = discovery_questions(state, programme_id=record["programme_id"], as_of=as_of)
    modern = [row for row in rows if row["cycle_kind"] == "discovery-wave-v2"]
    if not modern:
        return {"state": "insufficient_sample", "version": VERSION, "reason": "COHORT_NO_CURRENT_DISCOVERY_ORIGINALS",
                "counts": discovery_counts(rows), "stock": _stock(state, record, pool, at)}
    started = min(timestamp(row["cycle"]["prepared_at"]) for row in modern)
    discovery_ids = {row["action"]["acquisition_id"] for row in modern if row["action"].get("acquisition_id")}
    observed = {rid: row for rid, row in state.get("observations", {}).items()
                if row.get("acquisition_id") in discovery_ids and timestamp(row["observed_at"]) <= at}
    current_ids = set()
    for aid in discovery_ids:
        if state.get("batch_runs", {}).get(aid, {}).get("kind") != "source-acquisition-owner-v1": continue
        owner, _, _, collected_at = _dated_original(state, aid, at)
        if timestamp(owner["created_at"]) <= at and collected_at is not None and started <= collected_at <= at:
            current_ids.add(aid)
    current_observed = {rid: row for rid, row in observed.items() if row["acquisition_id"] in current_ids}
    fresh_semantics = {row["semantic_id"] for row in current_observed.values()}
    fresh = {}
    for row in modern:
        cycle = row["cycle"]
        progress = _acquisition_progress(state, [row["action"]["acquisition_id"]] if row["action"].get("acquisition_id") else [], cycle["baseline_concepts"])
        for cid in progress["new_concept_ids"]: fresh[cid] = state["concepts"][cid]
    fresh_concepts = {row["concept_id"] for row in state.get("associations", {}).values()
                      if row.get("status") == "resolved" and row.get("semantic_id") in fresh_semantics}
    new = set(fresh) & fresh_concepts
    aliases, published, held = _identities(state, record)
    first = min((row["cycle"] for row in modern), key=lambda row: row["wave_sequence"])
    earlier_identities = {aliases.get(cid, {}).get("canonical_id", cid) for cid in first["baseline_concepts"]}
    new = {cid for cid in new if aliases.get(cid, {}).get("canonical_id", cid) not in earlier_identities}
    follow_runs = {child["source_run_id"] for child in record.get("follow_on_reviews", {}).values()
                   if new.intersection(child.get("concept_ids", []))}
    associated_ids = {task["acquisition_id"] for task in state.get("query_tasks", {}).values()
                      if task.get("kind") == "all_source_task_v1" and task.get("run_id") in follow_runs
                      and new.intersection(task.get("targets", {})) and task.get("acquisition_id")}
    costs = _original_costs(state, discovery_ids | associated_ids, financial_state, started_at=started, as_of=at)
    progress = _acquisition_progress(state, discovery_ids, [])
    decisions = {row["concept_id"]: row["decision"] for row in pool.get("decisions", [])}
    # Genuine publication already proves this cohort type qualified. A later
    # pending Updated review is separate work, not an unfinished first outcome.
    completed = {cid for cid in new if decisions.get(cid) in {"included", "excluded"}} | (new & published)
    eligible = {row["concept_id"] for row in pool["eligible"]}
    unique = len(fresh_semantics)
    complete = progress["all_items_interpreted"] and completed == new
    reasons = []
    if not unique: reasons.append("COHORT_NO_CAPTURED_CANDIDATES")
    if not complete: reasons.append("COHORT_ASSESSMENT_OR_IDENTITY_UNFINISHED")
    if not costs["current_originals"]: reasons.append("COHORT_ONLY_SAVED_OLDER_REUSE")
    if costs["state"] != "known": reasons.append("COHORT_COLLECTION_FEES_NOT_SETTLED")
    # Yield measures useful discoveries, not remaining inventory. Reserving or
    # genuinely publishing a useful discovery must not erase its earlier
    # usefulness. The separate stock view still removes held/published New.
    productive = {aliases.get(cid, {}).get("canonical_id", cid) for cid in new & (eligible | published)}
    available_new = {aliases.get(cid, {}).get("canonical_id", cid) for cid in new & eligible
                     if cid not in published and cid not in held}
    if None in productive | available_new: raise SafetyError("COHORT_ALIAS_IDENTITY_INVALID")
    numerator = len(productive)
    value = {"state": "known" if not reasons else "insufficient_sample", "version": VERSION,
        "programme_id": record["programme_id"], "started_at": started.isoformat(), "as_of": as_of,
        "counts": discovery_counts(rows), "raw_capture_rows": len(current_observed), "unique_source_candidates": unique,
        "saved_older_capture_rows_reused": len(observed) - len(current_observed),
        "newly_resolved_types": len(new), "completed_report_decisions": len(completed), "eligible_current_types": len(new & eligible),
        "useful_new_types_observed": numerator, "never_shown_unreserved_new_types": len(available_new),
        "measured_current_useful_new_yield": str(Decimal(numerator) / unique) if complete and unique else None,
        "costs": costs, "stock": _stock(state, record, pool, at), "reasons": reasons,
        "model": _programme_model_effort(state, record["programme_id"], model_effort, at),
        "local_runtime_seconds": None, "hosting_storage_other_usd": None,
        "note": "This is the current programme discovery cohort and its linked follow-on checks, not the historical 156 products or existing backlog. A small complete sample is measured, not a promised future yield. Unknown costs stay unknown. Provider collection costs include both discovery and linked validation; the source mix is not assumed constant forever."}
    value["cohort_hash"] = digest(value)
    return value


def current_cohort_forecast(cohort, policy, *, as_of):
    """Eight explicit demand cases; never substitute an old paid baseline."""
    from .service import _validate_policy
    _validate_policy(policy); at = timestamp(as_of)
    today = at.astimezone(ZoneInfo(policy["timezone"])).date(); start = today.replace(day=1)
    end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    issues = sum((start + timedelta(days=n)).weekday() in policy["publication_weekdays"] for n in range((end-start).days))
    stock = cohort.get("stock", {}); backlog = stock.get("available_new_types")
    if backlog is not None and (type(backlog) is not int or backlog < 0):
        raise SafetyError("CURRENT_COHORT_STOCK_INVALID")
    measured = cohort.get("state") == "known"
    if measured and (cohort.get("version") != VERSION
            or cohort.get("cohort_hash") != digest({k: v for k, v in cohort.items() if k != "cohort_hash"})):
        raise SafetyError("CURRENT_COHORT_BINDING_INVALID")
    candidates = cohort.get("unique_source_candidates", 0)
    if type(candidates) is not int or candidates < 0 or measured and candidates == 0:
        raise SafetyError("CURRENT_COHORT_SAMPLE_INVALID")
    unit = Decimal(cohort["costs"]["provider_collection_usd"]) / candidates if measured and candidates else None
    rows = []
    for entries in (10, 15):
        maximum_updates = int((Decimal(entries) * Decimal(policy["maximum_update_fraction"])).to_integral_value(rounding=ROUND_FLOOR))
        for updates in dict.fromkeys((0, maximum_updates)):
            required = entries - updates
            for yield_rate in ("0.05", "0.03"):
                remaining_backlog = backlog or 0; needs = 0
                for _ in range(issues):
                    used = min(remaining_backlog, required); remaining_backlog -= used
                    needs += int((Decimal(required-used) / Decimal(yield_rate)).to_integral_value(rounding=ROUND_CEILING))
                total = None if unit is None else format((unit * needs).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING), "f")
                rows.append({"entries_per_report": entries, "updates_per_report_if_genuine_updates_exist": updates,
                    "new_types_per_report": required, "logical_reports": issues, "assumed_useful_new_yield": yield_rate,
                    "yield_denominator": "unique_source_candidates_from_current_originals",
                    "new_types_needed_before_backlog": required * issues, "available_new_backlog_now": backlog,
                    "new_types_needed_after_backlog": max(0, required * issues - (backlog or 0)),
                    "new_candidates_needed_after_backlog": needs if backlog is not None else None,
                    "provider_collection_estimate_usd": total, "incremental_ai_estimate_usd": None,
                    "hosting_storage_other_estimate_usd": None, "all_in_estimate_usd": None,
                    "local_runtime_cash_limit_applied": False})
    return {"state": "known" if measured else "insufficient_sample", "version": FORECAST_VERSION,
        "reason": None if measured else "CURRENT_COHORT_NOT_COMPLETE_AND_PRICED",
        "month": start.strftime("%Y-%m"), "calendar_issue_count": issues, "calendar_timezone": policy["timezone"],
        "calendar_start": start.isoformat(), "calendar_end_exclusive": end.isoformat(), "scenarios": rows,
        "cohort_hash": cohort.get("cohort_hash"), "measured_current_yield": cohort.get("measured_current_useful_new_yield"),
        "current_stock": stock, "provider_cost_per_source_candidate_usd": str(unit) if unit is not None else None,
        "note": "Whole-month workload scenarios, not bills or a remaining-month release plan. Five/three percent are planning assumptions, not measured yield. Backlog is used once and may need later freshness repair. The update case is conditional on enough genuine accepted updates. Known-zero local collection does not make AI, hosting or the complete report free. Similar local searches have no imposed runtime cash cap."}
