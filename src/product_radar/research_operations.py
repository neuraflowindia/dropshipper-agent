"""Connect the researched catalogue, saved history and finite working plans.

Planning never launches a collector. Events retain plan/outcome provenance;
HTML files are disposable working views, not another editable source of truth.
"""

from html import escape
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import re
from types import SimpleNamespace

from .brand_reviews import brand_binding, current_brand
from .catalogue import catalogue_summary, validate_catalogue
from .models import SafetyError, digest, identifier, timestamp, utc_now
from .research_memory import key
from .research_planner import is_empty_refinement, plan_searches, semantic_key, validate_outcome, validate_refinement
from .research_records import is_paid_query_task
from .storage import atomic_bytes, atomic_json


def supplier_capture_inputs(memory, capture):
    """Shortlist saved leads by wording; F must decide actual relevance.

    This creates interpretation inputs, not supplier recommendations. The
    original receipt, date and surrounding limitations remain in the capture.
    """
    from .discovery import tokens
    from .suppliers import validate_supplier_input
    if not isinstance(capture, dict) or not isinstance(capture.get("pages"), list) or not isinstance(capture.get("supplier_leads"), list):
        raise SafetyError("SUPPLIER_CAPTURE_INVALID")
    timestamp(capture["checked_at"])
    pages = {p["id"]: p for p in capture["pages"]}
    if len(pages) != len(capture["pages"]):
        raise SafetyError("SUPPLIER_PAGE_ID_DUPLICATED")
    concepts = memory.snapshot()["concepts"]
    items = []
    def terms(value):
        return {word[:-1] if len(word) > 3 and word.endswith("s") else word for word in tokens(value)}
    for lead in capture["supplier_leads"]:
        wording = terms(" ".join(lead["related_product_labels"]))
        ranked = sorted(((-len(wording & terms(c["description"]["label"] + " " + c["description"]["function"])), i, c)
                         for i, c in concepts.items()), key=lambda row: (row[0], row[1]))
        for score, concept_id, concept in ranked[:2]:
            if score >= 0:
                continue
            item = {"id": key("lead-check", [lead["id"], concept_id]),
                    "product": dict(concept["description"], concept_id=concept_id),
                    "pages": [{field: pages[page_id][field] for field in ("id", "url", "title", "text")}
                              for page_id in lead["page_ids"]]}
            validate_supplier_input(item)
            items.append(item)
    if not items:
        raise SafetyError("NO_RELATED_SAVED_SUPPLIER_INPUTS")
    return items


def save_source_check(memory, *, source, route, at, state, reason, reference):
    """Record a real route observation; a source failure is not poor demand."""
    if source not in ("meta", "marketplace", "trends", "supplier") or state not in ("usable", "insufficient", "blocked", "failed"):
        raise SafetyError("SOURCE_CHECK_INVALID")
    timestamp(at)
    for value in (route, reason, reference):
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            raise SafetyError("SOURCE_CHECK_INVALID")
    check_id = key("source-check", [source, route, at, reference])
    memory.put("source_checks", check_id, {"source": source, "route": route, "at": at,
               "state": state, "reason": reason, "reference": reference})
    return check_id


def record_query_outcome(memory, outcome):
    """Import measured feedback only for a real acquisition in this namespace."""
    validate_outcome(outcome)
    identifier(outcome["acquisition_id"])
    state = memory.snapshot()
    source = {"marketplace": "amazon"}.get(outcome["source"], outcome["source"])
    capture = next((c for c in state["captures"].values() if c["acquisition_id"] == outcome["acquisition_id"] and c["source"] == source), None)
    if capture is None or capture["observed_at"] != outcome["observed_at"]:
        raise SafetyError("QUERY_OUTCOME_CAPTURE_REQUIRED")
    if "empty_result" in outcome:
        from .storage import load_json, safe_path
        task = next((row for row in state["query_tasks"].values()
                     if is_paid_query_task(row) and row["family_id"] == outcome["family_id"] and semantic_key(row["request"]) == semantic_key(outcome["request"])
                     and row["attempt_id"] == capture.get("reference", {}).get("attempt_id")), None)
        with memory.journal.locked():
            attempt = memory.journal.state()[1].get(task["attempt_id"]) if task else None
        manifest = load_json(safe_path(memory.journal.root, task["attempt_id"]) / "capture_manifest.json") if task else None
        if task is None or empty_meta_result(memory, task, capture, attempt, manifest) != outcome["empty_result"]:
            raise SafetyError("QUERY_EMPTY_RESULT_PROOF_REQUIRED")
    if is_empty_refinement(outcome.get("refinement")):
        task = next((row for row in state["query_tasks"].values()
                     if is_paid_query_task(row) and row["family_id"] == outcome["family_id"] and semantic_key(row["request"]) == semantic_key(outcome["request"])
                     and row["attempt_id"] == capture.get("reference", {}).get("attempt_id")), None)
        if task is None or task.get("refinement") != outcome["refinement"]:
            raise SafetyError("QUERY_REFINEMENT_TASK_REQUIRED")
    outcome_id = key("outcome", [outcome["family_id"], outcome["acquisition_id"], semantic_key(outcome["request"])])
    value = dict(outcome, id=outcome_id)
    old = state["query_outcomes"].get(outcome_id)
    if old == value:
        return outcome_id
    memory.put("query_outcomes", outcome_id, value, previous=old,
               event_id=key("outcome-revision", [outcome_id, digest(old), digest(value)]))
    return outcome_id


def empty_meta_result(memory, task, capture, attempt, manifest):
    """Prove one exact saved no-ads diagnostic; never infer from rejected rows.

    Uses the existing read-only controller verifier for page/body hashes and
    pagination. A provider may count its diagnostic as one dataset item: retain
    that count separately from the proven zero ad rows. No network or writes.
    """
    if (task["request"]["source"] != "meta" or not attempt or not isinstance(manifest, dict)
            or attempt.get("remote_status") != "SUCCEEDED" or attempt.get("controls_ok") is not True
            or manifest.get("bounded_capture_complete") is not True or manifest.get("truncated") is not False
            or type(manifest.get("captured_count")) is not int or manifest["captured_count"] != 1
            or type(manifest.get("returned_total")) is not int or manifest["returned_total"] not in (0, 1)):
        return None
    from .benchmark import MetaTrial
    from .storage import load_json, safe_path
    from urllib.parse import parse_qs, urlsplit
    from .models import prepared_request
    folder = safe_path(memory.journal.root, task["attempt_id"])
    rows = load_json(folder / "captured_rows.json", 5 * 1024 * 1024)
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or set(rows[0]) != {"error", "errorCode", "url"}
            or rows[0]["error"] != "Ads not found" or rows[0]["errorCode"] != "ADS_NOT_FOUND"):
        return None
    prepared = prepared_request(load_json(folder / "prepared.json"))
    query = parse_qs(urlsplit(prepared.input["urls"][0]["url"]).query)
    expected_request = {"source": "meta", "route": "apify", "build": prepared.build, "schema_version": 1,
        "purpose": prepared.purpose, "term": query["q"][0], "language": "en", "country": "IN",
        "date_scope": {"kind": "active_ads_at_capture"}, "coverage": {"limit": prepared.input["limitPerSource"]},
        "options": {"actor": prepared.actor, "expected_build_id": prepared.expected_build_id,
                    "scrapeAdDetails": prepared.input["scrapeAdDetails"], "active_status": query["active_status"][0],
                    "ad_type": query["ad_type"][0], "media_type": query["media_type"][0], "search_type": query["search_type"][0]}}
    if (prepared.attempt_id != task["attempt_id"] or prepared.config_hash != task["config_hash"]
            or attempt.get("config_hash") != prepared.config_hash or prepared.build != task["request"]["build"]
            or semantic_key(task["request"]) != semantic_key(expected_request)
            or prepared.purpose != task["request"]["purpose"] or prepared.input["limitPerSource"] != task["request"]["coverage"]["limit"]
            or query.get("q") != [task["request"]["term"]] or query.get("country") != [task["request"]["country"]]
            or rows[0]["url"] != prepared.input["urls"][0]["url"]
            or digest(rows) != capture["capture_hash"] or capture["capture_hash"] != attempt.get("capture")
            or manifest.get("captured_hash") != capture["capture_hash"]
            or manifest.get("observed_at") != capture["observed_at"] or manifest.get("dataset_id") != capture["acquisition_id"]):
        raise SafetyError("QUERY_EMPTY_RESULT_PROOF_REQUIRED")
    verifier = MetaTrial(memory.journal.root, memory.journal)
    verifier.prepared, verifier.folder = prepared, folder
    with memory.journal.locked():
        verifier.verify_saved_capture(manifest, rows)
    return {"kind": "meta_ads_not_found", "capture_hash": capture["capture_hash"],
            "request_key": semantic_key(task["request"]), "provider_items": manifest["returned_total"], "ad_rows": 0}


def sync_query_feedback(memory, *, as_of=None):
    """Measure bound saved captures after interpretation; no guessed zero yield.

    Unreviewed rows remain pending. New concepts are compared with the frozen
    pre-search baseline, not with whichever report happens to be on screen.
    """
    from .storage import load_json, safe_path
    from .research_flow import current_assessment_fit, evidence_indexes
    from .fit_policy import LEGACY_POLICY_ID
    from .service import load_policy
    policy_id = load_policy().get("fit_policy_id", LEGACY_POLICY_ID)
    state = memory.snapshot()
    indexes = evidence_indexes(state)
    # A settled exclusion is an interpretation result, not a checklist job
    # waiting forever: ordinary flow deliberately never reassesses it.
    terminal_exclusions, bound_concepts = set(), set()
    for concept_id, concept in state["concepts"].items():
        try:
            verdict = current_brand(state, "concept", concept_id, concept["description"])
        except SafetyError:
            continue  # A broken brand binding is pending, not poor query yield.
        bound_concepts.add(concept_id)
        if verdict == "established" or concept.get("status") in ("excluded", "merged", "retired"):
            terminal_exclusions.add(concept_id)
    current_assessments = {i for i, record in state["assessments"].items()
                           if i in bound_concepts and i not in terminal_exclusions
                           and current_assessment_fit(state, i, expected_policy_id=policy_id, indexes=indexes) is not None}
    fit_pending = {i for i in current_assessments if policy_id != LEGACY_POLICY_ID
                   and current_assessment_fit(state, i, expected_policy_id=policy_id, indexes=indexes)["reason"] == "fit_information_pending"}
    with memory.journal.locked(timeout=5):
        attempts = memory.journal.state()[1]
    saved = []
    target_summary = None
    if any(is_paid_query_task(task) and task.get("target") for task in state["query_tasks"].values()):
        from .research_flow import candidate_summary
        target_summary = candidate_summary(memory, as_of=as_of or utc_now())
    for task_id, task in state["query_tasks"].items():
        if not is_paid_query_task(task):
            continue
        capture = next((c for c in state["captures"].values()
                        if isinstance(c.get("reference"), dict)
                        and c["reference"].get("attempt_id") == task["attempt_id"]), None)
        attempt = attempts.get(task["attempt_id"])
        if capture is None or attempt is None:
            continue
        manifest = load_json(safe_path(memory.journal.root, task["attempt_id"]) / "capture_manifest.json")
        if (manifest["captured_hash"] != capture["capture_hash"] or attempt["capture"] != capture["capture_hash"]
                or manifest.get("dataset_id") != attempt["dataset_id"]
                or capture["acquisition_id"] != attempt["dataset_id"]
                or manifest.get("observed_at") != capture["observed_at"]):
            raise SafetyError("QUERY_FEEDBACK_CAPTURE_MISMATCH")
        semantic_ids = {o["semantic_id"] for o in state["observations"].values()
                        if o["capture_id"] == key("capture", [capture["source"], capture["acquisition_id"]])}
        linked, pending = set(), 0
        for semantic_id in semantic_ids:
            description = state["descriptions"].get(semantic_id)
            if description is None:
                pending += 1
                continue
            expected = len(description["result"]["products"])
            current_children = {key("child", [semantic_id, index, digest(description["result"])]) for index in range(expected)}
            links = [a for child_id, a in state["associations"].items()
                     if child_id in current_children and a["semantic_id"] == semantic_id]
            if len(links) != expected or any(a["status"] not in ("resolved", "excluded") for a in links):
                pending += 1
            linked.update(a["concept_id"] for a in links if a["status"] == "resolved")
        new = linked - set(task["baseline_concepts"])
        assessments_complete = linked <= (current_assessments | terminal_exclusions)
        promising = {i for i in new if i in current_assessments
                     and current_assessment_fit(state, i, expected_policy_id=policy_id, indexes=indexes)["eligible"]}
        reviewed = pending == 0 and assessments_complete and not (new & fit_pending)
        complete = manifest.get("bounded_capture_complete") is True and manifest.get("truncated") is False
        failed = capture["mapping_status"] == "source_failure" or attempt["remote_status"] != "SUCCEEDED"
        status = "failed" if failed else "completed" if complete else "partial"
        actual = None
        if attempt["charge"] is not None and attempt["read_charge"] is not None:
            from .models import usd
            actual = str(usd(attempt["charge"]) + usd(attempt["read_charge"]))
        outcome = {"id": key("feedback", [task_id, capture["acquisition_id"]]),
                   "family_id": task["family_id"], "source": task["request"]["source"],
                   "acquisition_id": capture["acquisition_id"], "observed_at": capture["observed_at"],
                   "status": status, "complete": complete and not failed, "reviewed": reviewed and not failed,
                   "request": task["request"], "resolved_count": len(linked),
                   "known_count": len(linked & set(task["baseline_concepts"])), "pending_count": pending,
                   "unseen_promising": bool(promising) if reviewed and not failed else None,
                   "useful_progress": (True if promising else False if reviewed and not (linked & set(task["baseline_concepts"])) else None) if not failed else None,
                   "cost_usd": actual}
        outcome["fit_policy_id"] = policy_id
        outcome["fit_pending_count"] = len(new & fit_pending)
        marker = empty_meta_result(memory, task, capture, attempt, manifest)
        if marker is not None:
            # The successful, complete exact diagnostic itself proves an empty
            # request. A mapper's error classification does not turn this into
            # unreviewed data; arbitrary errors never reach this branch.
            failed, complete, reviewed, pending, status = False, True, True, 0, "completed"
            outcome.update(status=status, complete=complete, reviewed=reviewed,
                           resolved_count=0, known_count=0, pending_count=0,
                           unseen_promising=False, useful_progress=False)
            outcome["empty_result"] = marker
        if is_empty_refinement(task.get("refinement")):
            outcome["refinement"] = deepcopy(task["refinement"])
        if task.get("target"):
            measured = validation_target_result(task, target_summary, state=state)
            if pending or not reviewed or failed:
                measured.update(status="source_unavailable" if failed else "interpretation_pending", useful_progress=None)
            outcome["target_result"] = measured
            outcome["useful_progress"] = measured["useful_progress"]
        saved.append(record_query_outcome(memory, outcome))
        value = dict(task, status=outcome["status"], collector_dispatched=True, acquisition_id=capture["acquisition_id"])
        if value != task:
            memory.put("query_tasks", task_id, value, previous=task,
                       event_id=key("task-feedback", [task_id, digest(task), digest(value)]))
    return saved


def _is_route_observation(check):
    """Only the legacy route-observation contract describes global availability.

    Typed per-subject overlays/receipts keep their own readers. A matching
    source or timestamp alone cannot turn them into a provider-health vote.
    """
    return (isinstance(check, dict) and "kind" not in check
            and check.get("source") in ("meta", "marketplace", "trends", "supplier")
            and check.get("state") in ("usable", "insufficient", "blocked", "failed")
            and isinstance(check.get("at"), str) and isinstance(check.get("reason"), str))


def missing_market_checks(summary, *, as_of, source_checks=()):
    """Identify concrete checks without pretending an unproved route is ready."""
    from .research_flow import market_follow_up
    from .opportunity_score import opportunity_fields, work_rank
    now = timestamp(as_of)
    route_status = {}
    for check in source_checks:
        if not _is_route_observation(check):
            continue
        at = timestamp(check["at"])
        if at <= now and (check["source"] not in route_status or at > timestamp(route_status[check["source"]]["at"])):
            route_status[check["source"]] = check
    tasks = []
    for product in summary["products"]:
        from .fit_policy import FIT_POLICY_ID
        if (product.get("status") in ("excluded_established_brand", "decision_binding_pending")
                or product["checklist_meets"] is None):
            continue
        if product.get("fit_policy_id") == FIT_POLICY_ID:
            if not product.get("fit", {}).get("eligible"):
                continue
        elif product["checklist_meets"] < 7:
            continue
        market = product["market"]
        current_fit = product.get("fit") if product.get("fit_policy_id") == FIT_POLICY_ID else None
        opportunity = product.get("opportunity") if current_fit else None
        if current_fit and opportunity is None:
            raise SafetyError("OPPORTUNITY_SCORE_REQUIRED")
        if opportunity is None and market["available_families"] >= 2:
            continue  # Weak products are set aside; usable coverage is not a signal.
        scored = opportunity is not None
        actual_sources = market["sources"] if scored else market["families"]
        # The sites that can add score points. Amazon and Flipkart are the
        # crowding check (its own free stage), not a market question.
        questions = (("meta", "Check relevant product-specific advertisers and active ads."),
                     ("google-trends", "Check comparable India search interest for this product type."),
                     ("blinkit", "Check relevant Blinkit listings at the recorded location."),
                     ("zepto", "Check relevant Zepto listings at the recorded location."),
                     ("instamart", "Check relevant Instamart listings at the recorded location.")) if scored else (
                     ("marketplace", "Look for this product on a relevant India bestseller list."),
                     ("meta", "Check relevant product-specific advertisers and active ads."),
                     ("trends", "Check comparable India search interest for this product."))
        choices = []
        for family, question in questions:
            if actual_sources[family]["available"]:
                continue
            check = route_status.get(family) or route_status.get({"amazon": "marketplace", "google-trends": "trends"}.get(family, family))
            choices.append({"source": family, "question": question,
                            "route_state": check["state"] if check else "not_tested_for_this_question",
                            "limitation": check["reason"] if check else "A suitable product-specific route still needs integration."})
        if not choices:
            continue
        tasks.append({"id": key("market-task", [product["id"], market, product.get("fit_policy_id"), product.get("brand_binding")]), "concept_id": product["id"],
                      "label": product["label"], "kind": "missing_market_check", "choices": choices,
                      "needed_sources": max(0, 2 - market["available_families"]),
                      "positive_families": market["positive_families"],
                      "positive_signal_needed": market["positive_families"] == 0 if opportunity is None else False,
                      "reason": "Checklist fit passes. Reuse relevant saved evidence first. " + market_follow_up(market, fit=current_fit, opportunity=opportunity),
                      "collector_dispatched": False})
        tasks[-1].update(fit_policy_id=product.get("fit_policy_id", "fit-13-v1"),
                         fit_tier=product.get("fit_tier"), kpi_matches=product["checklist_meets"],
                         brand_binding=product.get("brand_binding"))
        if opportunity is not None:
            # More market facts can only add want/early points; the crowding
            # check is separate free work and never needs another market site.
            waiting = not opportunity["eligible"] and opportunity["reason"] == "demand_too_quiet"
            tasks[-1].update(opportunity_fields(opportunity), blocking=not opportunity["eligible"],
                             available_sources=market["available_sources"], positive_sources=market["positive_sources"],
                             needed_sources=0, positive_signal_needed=False,
                             minimum_additional_sources=1 if waiting else 0)
    tasks.sort(key=lambda task: (*work_rank(task), 0 if task.get("fit_tier") == "good" else 1,
                                -task.get("positive_sources", task["positive_families"]), task["concept_id"]))
    return tasks


def _meta_points(record):
    return record["opportunity_basis"]["parts"]["want"]["items"]["facebook_ads"]["points"]


def validation_targets(memory, *, as_of, client_id="default"):
    """Current useful Meta questions from one saved snapshot, never collection."""
    from .report_selection import build_report_pool
    from .service import load_policy
    from .fit_policy import LEGACY_POLICY_ID
    from .opportunity_score import POLICY_ID
    state = memory.snapshot()
    frozen = SimpleNamespace(snapshot=lambda: deepcopy(state), evidence_type=memory.evidence_type)
    pool = build_report_pool(frozen, as_of=as_of, client_id=client_id,
                             fit_policy_id=load_policy().get("fit_policy_id", LEGACY_POLICY_ID))
    decisions = {row["concept_id"]: row for row in pool["decisions"]}
    targets = {}
    for work in pool["needed_market_work"]:
        concept_id = work["concept_id"]
        fact = pool["facts_by_concept"].get(concept_id)
        if not fact or decisions[concept_id]["decision"] == "excluded":
            continue
        codes = decisions[concept_id]["codes"]
        # The free crowding check (W6) is separate work; it never holds a market question back.
        if any(code not in ("market_coverage_required", "demand_too_quiet", "supplier_search_required",
                            "crowding_pending") for code in codes):
            continue
        families = {row["source"]: row for row in fact["market_checks"]}
        if fact.get("opportunity_policy_id") == POLICY_ID:
            if _meta_points(fact):
                continue  # Facebook ads already count in the score.
        elif families["marketplace"]["state"] != "Positive" or families["meta"]["state"] != "Insufficient":
            continue
        assessment = state["assessments"][concept_id]
        evidence_ids = sorted({i for signal in families.values() for i in signal["evidence_ids"]
                               if i in state["observations"]})
        review_facts = [{"id": i, "description_hash": state["market_reviews"][i]["description_hash"],
                         "relevance": state["market_reviews"][i]["relevance"]}
                        for i in fact["market_review_ids"]]
        before = {"available_families": work["available_families"],
                  "positive_families": sum(row["state"] == "Positive" for row in families.values()),
                  "meta_state": families["meta"]["state"], "meta_used_ids": list(families["meta"]["evidence_ids"]),
                  "marketplace_state": families["marketplace"]["state"]}
        if fact.get("opportunity_policy_id") == POLICY_ID:
            before.update(opportunity_score=fact["opportunity_score"], meta_points=_meta_points(fact))
        question = {"description": fact["description"], "assessment_input_hash": assessment["input_hash"],
                    "criteria": assessment["result"]["criteria"], "relevance": assessment["result"]["relevance"],
                    "market": fact["market_checks"], "reviews": review_facts,
                    "observations": [{"id": i, "hash": digest(state["observations"][i])} for i in evidence_ids]}
        question.update(fit_policy_id=assessment.get("fit_policy_id", LEGACY_POLICY_ID),
                        brand_binding=brand_binding(state, "concept", concept_id, fact["description"]))
        targets[concept_id] = {"concept_id": concept_id, "description_hash": digest(fact["description"]),
                              "assessment_input_hash": assessment["input_hash"], "question_fingerprint": digest(question),
                              "before_market": before, "label": fact["description"]["label"],
                              "category": fact["description"]["category"],
                              "supplier_search_complete": fact["supplier_search_status"] == "complete"}
        if assessment.get("fit_policy_id", LEGACY_POLICY_ID) != LEGACY_POLICY_ID:
            targets[concept_id].update(fit_tier=fact["fit_tier"], kpi_matches=fact["kpi_matches"])
        if "opportunity_policy_id" in fact:
            from .opportunity_score import opportunity_fields
            targets[concept_id].update(opportunity_fields(fact))
    return targets, pool


def validation_target_result(action, summary, *, state=None):
    """Measure accepted target evidence; query wording is never association."""
    target = action.get("target")
    if not target:
        return None
    product = next((p for p in summary["products"] if p["id"] == target["concept_id"]), None)
    before = deepcopy(target["before_market"])
    if product is None:
        return {"concept_id": target["concept_id"], "before_market": before, "after_market": None,
                "status": "target_unavailable", "useful_progress": None}
    if product.get("status") in ("excluded_established_brand", "decision_binding_pending"):
        return {"concept_id": target["concept_id"], "before_market": before, "after_market": None,
                "status": "target_unavailable", "useful_progress": None}
    market = product["market"]
    meta = market["families"]["meta"]
    after = {"available_families": market["available_families"], "positive_families": market["positive_families"],
             "meta_state": meta["state"], "meta_used_ids": list(meta["used_ids"]),
             "marketplace_state": market["families"]["marketplace"]["state"]}
    opportunity = product.get("opportunity") if product.get("fit_policy_id") == "fit-10-v1" and product.get("fit") else None
    if opportunity is not None:
        after.update(opportunity_score=opportunity["opportunity_score"], meta_points=_meta_points(opportunity))
    if state is not None and digest(state["concepts"][product["id"]]["description"]) != target["description_hash"]:
        return {"concept_id": product["id"], "before_market": before, "after_market": after,
                "status": "target_changed", "useful_progress": None}
    pending = product["checklist_meets"] is None or product["id"] in {
        p["affected_id"] for p in summary.get("problems", [])
        if p.get("stage") == "assessment" and p.get("current_status") == "unresolved"}
    progress = ((after["meta_points"] > before.get("meta_points", 0)) if opportunity is not None
                else (meta["available"] and before["meta_state"] == "Insufficient")) or (
                meta["positive"] is True and before["meta_state"] != "Positive")
    covered = opportunity["eligible"] if opportunity is not None else market["available_families"] >= 2
    status = ("interpretation_pending" if pending else "coverage_satisfied" if covered
              else "coverage_still_insufficient")
    return {"concept_id": product["id"], "before_market": before, "after_market": after,
            "status": status, "useful_progress": None if pending else bool(progress)}


def plan_meta_validation(memory, *, as_of, client_id="default", max_actions=3, budget_usd=None,
                         concept_id=None, term=None, reason=None):
    """Save finite follow-up, optionally naming one deliberate query refinement.

    Refinement changes only the provider query, never the accepted product or
    evidence question. Exact work remains reusable; explaining the same query
    differently does not authorise another collection.
    """
    timestamp(as_of); identifier(client_id)
    if type(max_actions) is not int or not 1 <= max_actions <= 5:
        raise SafetyError("VALIDATION_PLAN_LIMIT_INVALID")
    refinement = None
    if any(value is not None for value in (concept_id, term, reason)):
        if (any(value is None for value in (concept_id, term, reason))
                or not isinstance(term, str) or not 1 <= len(term.strip()) <= 120
                or not any(c.isalnum() for c in term) or any(ord(c) < 32 or ord(c) == 127 for c in term)
                or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000
                or any(ord(c) < 32 or ord(c) == 127 for c in reason)):
            raise SafetyError("VALIDATION_REFINEMENT_INVALID")
        identifier(concept_id)
        refinement = {"concept_id": concept_id, "term": " ".join(term.split()),
                      "reason": " ".join(reason.split())}
    from .models import usd
    from .query_dispatch import find_existing_task
    budget = None if budget_usd is None else usd(budget_usd)
    targets, _ = validation_targets(memory, as_of=as_of, client_id=client_id)
    if refinement is not None and concept_id not in targets:
        raise SafetyError("VALIDATION_REFINEMENT_TARGET_UNAVAILABLE")
    state = memory.snapshot()
    with memory.journal.locked():
        funded_attempts = set(memory.journal.state()[1])
    actions, decisions, seen_requests = [], [], set()
    spent = Decimal(0)
    prior_entries = [entry for run in state["batch_runs"].values() for entry in run.get("actions", {}).values()]
    # Batch entries carry the evidence target, while their immutable saved plan
    # carries the actual request. Join them rather than infer query identity
    # from the label or add provider facts to the product fingerprint.
    prior_actions = {action["action_id"]: action for plan in state["batch_plans"].values()
                     for action in plan.get("actions", [])}
    from .opportunity_score import work_rank
    ordered = sorted(targets.values(), key=lambda t: (
                     *work_rank(t),
                     0 if t.get("fit_tier") == "good" else 1,
                     -t.get("kpi_matches", 0) if t.get("fit_tier") in ("good", "decent") else 0,
                     -t["before_market"].get("positive_sources", t["before_market"]["positive_families"]),
                     -int(t["supplier_search_complete"]), t["concept_id"]))
    if refinement is not None:
        ordered = [targets[concept_id]]
    for target in ordered:
        family_id = key("validation-family", target["concept_id"])
        decision = {"family_id": family_id, "concept_id": target["concept_id"]}
        if refinement is not None:
            phrase = refinement["term"]
        else:
            from .product_queries import product_query_terms
            try:
                phrase = product_query_terms(memory, target["concept_id"], source="meta", limit=1)[0]["text"]
            except SafetyError as error:
                decisions.append(dict(decision, decision="defer",
                    reason="The accepted label needs a reviewed product-type query within 120 characters."
                           if str(error) == "PRODUCT_QUERY_PROFILE_REQUIRED" else str(error)))
                continue
        if not phrase or len(phrase) > 120 or (refinement is None and any(ord(c) < 32 for c in target["label"])):
            decisions.append(dict(decision, decision="defer", reason="The accepted label needs a specific, usable query within 120 characters."))
            continue
        request = {"source": "meta", "route": "apify", "build": "2.7.26", "schema_version": 1,
                   "purpose": "validation", "term": phrase, "language": "en", "country": "IN",
                   "date_scope": {"kind": "active_ads_at_capture"}, "coverage": {"limit": 10},
                   "options": {"actor": "curious_coder~facebook-ads-library-scraper", "expected_build_id": "wYDFs3xTPOVCo9Wag",
                               "scrapeAdDetails": False, "active_status": "active", "ad_type": "all", "media_type": "all",
                               "search_type": "keyword_unordered"}}
        request_key = semantic_key(request)
        action = {"action_id": key("validation", [target["concept_id"], target["question_fingerprint"], request_key]),
                  "family_id": family_id, "category": target["category"], "source": "meta", "phrase": phrase,
                  "semantic_key": request_key, "request": request, "kind": "collect", "target": deepcopy(target),
                  "baseline_concepts": sorted(state["concepts"]), "cost_estimate_usd": "0.05",
                  "estimate_basis": "Existing US$0.03 Actor bound plus US$0.02 retained read allowance.",
                  "yield_estimate_tentative": True,
                  "reason": "Accepted checklist and relevant marketplace evidence exist; check product-specific Meta coverage."}
        if refinement is not None:
            action["refinement"] = deepcopy(refinement)
            action["refinement_hash"] = digest(refinement)
            action["action_id"] = key("validation", [target["concept_id"], target["question_fingerprint"],
                                                      request_key, action["refinement_hash"]])
            action["reason"] = "Named query refinement: " + refinement["reason"]
        existing = find_existing_task(memory, action, as_of=as_of)
        checked = any(target["question_fingerprint"] in (
                          entry.get("target", {}).get("question_fingerprint"),
                          entry.get("target_result", {}).get("question_fingerprint"))
                      and entry.get("target", {}).get("concept_id") == target["concept_id"]
                      and entry.get("status") in ("captured", "reused", "blocked")
                      and (refinement is None or prior_actions.get(entry.get("action_id"), {}).get("semantic_key") == request_key)
                      for entry in prior_entries)
        if existing is None and checked:
            decisions.append(dict(decision, decision="defer", reason="This unchanged question already has a recorded result; use new facts or a named refined question, not automatic recollection."))
            continue
        if request_key in seen_requests:
            decisions.append(dict(decision, decision="omit", reason="This finite plan already covers the exact query; interpretation must decide which products its ads actually support."))
            continue
        no_new_commitment = existing and (existing["reuse_kind"] == "fresh_capture"
                                          or existing["attempt_id"] in funded_attempts)
        cost = Decimal(0) if no_new_commitment else Decimal("0.05")
        if len(actions) >= max_actions or budget is not None and spent + cost > budget:
            decisions.append(dict(decision, decision="defer", reason="The finite action or existing planning-money envelope is filled; retain this useful question."))
            continue
        if existing:
            if no_new_commitment:
                action["cost_estimate_usd"] = "0.00"
                action["estimate_basis"] = "Reuse or recover existing exact work; existing financial holds remain authoritative."
            if existing["reuse_kind"] == "fresh_capture":
                action["kind"] = "reuse"
        actions.append(action); seen_requests.add(request_key); spent += cost
        decisions.append(dict(decision, decision="chosen", action_id=action["action_id"], reason=action["reason"]))
    plan = {"schema_version": 1, "plan_kind": "meta_validation", "as_of": as_of, "client_id": client_id,
            "snapshot_hash": digest({"targets": targets, "as_of": as_of, "max_actions": max_actions,
                                     "budget_usd": str(budget) if budget is not None else None}),
            "actions": actions, "decisions": decisions, "evidence_type": memory.evidence_type,
            "market_checks": [], "source_checks": [], "collector_dispatched": False,
            "budget": {"available_usd": str(budget) if budget is not None else None,
                       "reserved_for_validation_and_suppliers_usd": "0", "discovery_estimate_usd": str(spent),
                       "has_unknown_cost": False, "note": "Planning commitment estimate only; existing production authority and actual holds control launch."},
            "launch_note": "Finite saved Meta validation questions. Preparation and existing production controller checks precede any launch.",
            "note": "Query names do not prove returned-product identity; ordinary A/B/E checks remain required."}
    if refinement is not None:
        plan["refinement"] = deepcopy(refinement)
        plan["refinement_hash"] = digest(refinement)
    plan["id"] = key("plan", plan)
    memory.put("batch_plans", plan["id"], plan)
    atomic_json(memory.root / "next_searches.json", plan)
    write_plan_view(memory.root / "next_searches.html", plan)
    return plan


def plan_research(memory, catalogue, *, as_of, slots=8, budget_usd=None, summary=None, urgent_family_ids=()):
    validate_catalogue(catalogue)
    # Priorities choose among the existing planner's safe actions; they do not
    # force a new collection when fresh coverage or pending work already exists.
    if not isinstance(urgent_family_ids, (list, tuple)):
        raise SafetyError("PLANNER_PRIORITY_FAMILIES_INVALID")
    family_ids = {family["id"] for family in catalogue["families"]}
    for family_id in urgent_family_ids:
        identifier(family_id)
        if family_id not in family_ids:
            raise SafetyError("PLANNER_PRIORITY_FAMILY_UNKNOWN")
    state = memory.snapshot()
    from .fit_policy import LEGACY_POLICY_ID
    from .service import load_policy
    policy_id = load_policy().get("fit_policy_id", LEGACY_POLICY_ID)
    outcomes = []
    for row in state["query_outcomes"].values():
        current = deepcopy(row)
        if row.get("fit_policy_id", LEGACY_POLICY_ID) != policy_id and "empty_result" not in row:
            # Keep capture freshness/repeat identity, but do not let an old
            # scoring policy suppress this family's present useful work.
            current.update(reviewed=False, unseen_promising=None, useful_progress=None)
        outcomes.append(current)
    # Only proven Meta keyword inputs are represented here. No assumed date,
    # pagination or exclusion parameters are passed to the current adapter.
    routes = {"meta": {"available": True, "route": "apify", "build": "2.7.26", "schema_version": 1,
                       "date_scope": {"kind": "active_ads_at_capture"},
                       "options": {"actor": "curious_coder~facebook-ads-library-scraper",
                                   "expected_build_id": "wYDFs3xTPOVCo9Wag", "scrapeAdDetails": False,
                                   "active_status": "active", "ad_type": "all", "media_type": "all",
                                   "search_type": "keyword_unordered"},
                       "estimate_basis": "Provisional US$0.03 Actor bound plus US$0.02 existing read allowance."}}
    plan = plan_searches(catalogue, outcomes, as_of=as_of, slots=slots,
                         routes=routes, budget_usd=budget_usd, coverage={"limit": 5},
                         per_search_usd="0.05", validation_reserve_usd="0.06", urgent_family_ids=urgent_family_ids,
                         refinement_tasks=[dict(task, id=task_id) for task_id, task in state["query_tasks"].items()
                                           if is_paid_query_task(task) and is_empty_refinement(task.get("refinement"))])
    plan["evidence_type"] = memory.evidence_type
    plan["market_checks"] = missing_market_checks(summary, as_of=as_of, source_checks=state["source_checks"].values()) if summary else []
    plan["source_checks"] = sorted((check for check in state["source_checks"].values() if _is_route_observation(check)),
                                   key=lambda c: (c["source"], c["at"], c.get("reference", "")))
    plan["catalogue_summary"] = catalogue_summary(catalogue)
    plan["collector_dispatched"] = False
    plan["launch_note"] = "This plan does not launch searches. Dispatch must bind each task to a prepared existing-controller attempt and recheck the financial authority."
    # Persist by full frozen input/result identity. Rebuilding this view is not
    # another search and does not mutate earlier plans or observation dates.
    plan_id = key("plan", plan)
    plan["id"] = plan_id
    memory.put("batch_plans", plan_id, plan)
    atomic_json(memory.root / "next_searches.json", plan)
    write_plan_view(memory.root / "next_searches.html", plan)
    return plan


def bind_prepared_action(memory, plan_id, action_id, prepared):
    """Bind one logical search to its saved preparation, never a new launch."""
    identifier(plan_id)
    identifier(action_id)
    state = memory.snapshot()
    plan = state["batch_plans"].get(plan_id)
    action = next((a for a in plan["actions"] if a["action_id"] == action_id), None) if plan else None
    if action is None or action["kind"] != "collect" or action["source"] != "meta":
        raise SafetyError("PLANNED_META_ACTION_REQUIRED")
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(prepared.input["urls"][0]["url"]).query)
    if (prepared.build != action["request"]["build"] or prepared.purpose != action["request"]["purpose"]
            or query.get("q") != [action["phrase"]] or query.get("country") != ["IN"]
            or prepared.input["limitPerSource"] != action["request"]["coverage"]["limit"]):
        raise SafetyError("PLANNED_REQUEST_MISMATCH")
    task_id = key("task", [plan_id, action_id])
    old = state["query_tasks"].get(task_id)
    if old is not None:
        if (not is_paid_query_task(old) or old["attempt_id"] != prepared.attempt_id
                or old["config_hash"] != prepared.config_hash):
            raise SafetyError("RESEARCH_TASK_ALREADY_BOUND")
        return dict(old, id=task_id)
    value = {"plan_id": plan_id, "action_id": action_id, "family_id": action["family_id"],
             "attempt_id": prepared.attempt_id, "config_hash": prepared.config_hash,
             "request": action["request"], "status": "prepared",
             "baseline_concepts": sorted(state["concepts"]), "collector_dispatched": False}
    if "target" in action:
        value["target"] = deepcopy(action["target"])
        value["baseline_concepts"] = deepcopy(action["baseline_concepts"])
    if is_empty_refinement(action.get("refinement")):
        validate_refinement(action["refinement"])
        base = state["query_outcomes"].get(action["refinement"]["base_outcome_id"])
        # The planner owns family interpretation. Binding proves the immutable
        # original acquisition/request, without inventing another product ID.
        if (base is None or not base.get("empty_result") or is_empty_refinement(base.get("refinement"))
                or base["acquisition_id"] != action["refinement"]["base_acquisition_id"]
                or semantic_key(base["request"]) != action["refinement"]["base_request_key"]
                or base["empty_result"]["capture_hash"] != action["refinement"]["base_capture_hash"]):
            raise SafetyError("QUERY_REFINEMENT_BASE_REQUIRED")
        prior = [task for task in state["query_tasks"].values() if is_paid_query_task(task) and is_empty_refinement(task.get("refinement"))
                 and task["refinement"]["base_acquisition_id"] == action["refinement"]["base_acquisition_id"]
                 and task["refinement"]["base_request_key"] == action["refinement"]["base_request_key"]
                 and task["refinement"]["base_capture_hash"] == action["refinement"]["base_capture_hash"]]
        if prior:
            raise SafetyError("QUERY_REFINEMENT_ALREADY_BOUND")
        value["refinement"] = deepcopy(action["refinement"])
    memory.put("query_tasks", task_id, value)
    return dict(value, id=task_id)


def import_trends_evidence(memory, *, concept_id, acquisition_id, csv_bytes, metadata, expected_description_hash=None):
    """Attach a controlled CSV to a known concept, not another product listing."""
    from .providers.trends_browser import parse_trends_csv
    identifier(concept_id)
    identifier(acquisition_id)
    state = memory.snapshot()
    if concept_id not in state["concepts"]:
        raise SafetyError("KNOWN_CONCEPT_REQUIRED")
    if expected_description_hash is not None and (
            not isinstance(expected_description_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_description_hash)
            or digest(state["concepts"][concept_id]["description"]) != expected_description_hash):
        raise SafetyError("TRENDS_PRODUCT_CHANGED")
    parsed = parse_trends_csv(csv_bytes, metadata)
    evidence_id = key("trends", [concept_id, acquisition_id])
    old = state["trends_evidence"].get(evidence_id)
    if old:
        if expected_description_hash is not None and old.get("description_hash") != expected_description_hash:
            raise SafetyError("TRENDS_PRODUCT_CHANGED")
        if old["concept_id"] != concept_id or old["acquisition_id"] != acquisition_id or old["parsed"] != parsed:
            raise SafetyError("TRENDS_CAPTURE_CONFLICT")
        return dict(id=evidence_id, **old)
    value = {"concept_id": concept_id, "acquisition_id": acquisition_id, "parsed": parsed,
             "description_hash": digest(state["concepts"][concept_id]["description"])}
    if expected_description_hash is None:
        memory.put("trends_evidence", evidence_id, value)
    else:
        from .research_memory import change
        def unchanged(current):
            concept = current["concepts"].get(concept_id)
            if not concept or digest(concept["description"]) != expected_description_hash:
                raise SafetyError("TRENDS_PRODUCT_CHANGED")
        memory.commit(key("put", ["trends_evidence", evidence_id, value]),
                      [change("trends_evidence", evidence_id, value)], validate=unchanged)
    return dict(id=evidence_id, **value)


def _page(title, intro, content):
    return ("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>" + escape(title) + "</title><style>body{font:16px system-ui;margin:24px auto;padding:0 20px;max-width:1100px;background:#faf8f3;color:#283342}"
            "h1{font-size:28px}h2{font-size:21px}p{line-height:1.5;max-width:78ch}table{border-collapse:collapse;width:100%}"
            "th,td{text-align:left;padding:12px;border-bottom:1px solid #dedbd4;vertical-align:top}.scroll{overflow:auto}"
            "details{padding:12px 0;border-bottom:1px solid #dedbd4}summary{cursor:pointer}a{color:#283342}input{padding:10px;width:min(90%,450px);font:inherit}</style>"
            "<h1>" + escape(title) + "</h1><p>" + escape(intro) + "</p>" + content + "</html>")


def write_plan_view(path, plan):
    rows = []
    for action in plan["actions"]:
        cells = (action["category"], action["phrase"], action["kind"], action["reason"], action["cost_estimate_usd"] or "Not known")
        rows.append("<tr>" + "".join("<td>" + escape(str(c)) + "</td>" for c in cells) + "</tr>")
    heading = "Product-specific Meta checks" if plan.get("plan_kind") == "meta_validation" else "Discovery choices"
    content = "<p>" + escape(plan["launch_note"]) + "</p><h2>" + heading + "</h2><div class='scroll'><table><tr><th>Category</th><th>Search</th><th>Action</th><th>Why</th><th>Estimated US$</th></tr>" + "".join(rows) + "</table></div>"
    content += "<h2>Checklist-passing products needing market checks</h2>"
    if not plan["market_checks"]:
        content += "<p>No additional market check is queued from the assessed candidates in this snapshot.</p>"
    for task in plan["market_checks"]:
        content += "<details open><summary>" + escape(task["label"]) + "</summary><p>" + escape(task["reason"]) + "</p>"
        for choice in task["choices"]:
            content += "<p>" + escape(choice["question"] + " " + choice["route_state"] + ": " + choice["limitation"]) + "</p>"
        content += "</details>"
    content += "<h2>Other areas</h2><p>Unselected areas remain available for later batches. The reasons below describe this plan, not permanent product exclusions.</p>"
    for decision in plan["decisions"]:
        content += "<details><summary>" + escape(decision["family_id"] + " — " + decision["decision"]) + "</summary><p>" + escape(decision["reason"]) + "</p></details>"
    atomic_bytes(Path(path), _page("Next research searches", "Internal working plan. No client report or provider launch occurs when this view is rebuilt.", content).encode())


def write_catalogue_view(root, catalogue):
    validate_catalogue(catalogue)
    summary = catalogue_summary(catalogue)
    atomic_json(Path(root) / "catalogue_summary.json", summary)
    sources = {s["id"]: s for s in catalogue["sources"]}
    content = "<p>" + escape(str(summary["distinct_usable_families"]) + " distinct usable areas; " + str(summary["phrase_variants"]) + " search options. " + summary["coverage_note"]) + "</p>"
    content += "<label>Find an area <input id='filter' placeholder='For example: storage or travel'></label>"
    for family in sorted(catalogue["families"], key=lambda f: (f["category"], f["parent_id"], f["label"])):
        content += "<details class='family'><summary>" + escape(family["category"] + " · " + family["label"]) + "</summary>"
        content += "<p>" + escape(family["definition"] + " " + family["problem"]) + "</p><p>Basis: " + escape(family["basis"]) + ". " + escape(family["rationale"]) + "</p>"
        content += "<p>Search options: " + escape("; ".join(p["text"] for p in family["phrases"])) + "</p>"
        content += "<p>Distinctions: " + escape("; ".join(family["included"])) + ". Not the same as: " + escape("; ".join(family["excluded"])) + "</p>"
        for source_id in family["sources"]:
            source = sources[source_id]
            content += "<p><a href='" + escape(source["url"], quote=True) + "' rel='noreferrer'>" + escape(source["title"]) + "</a></p>"
        content += "</details>"
    content += "<script>document.getElementById('filter').addEventListener('input',function(){let q=this.value.toLowerCase();document.querySelectorAll('.family').forEach(el=>{el.hidden=!el.textContent.toLowerCase().includes(q)})});</script>"
    atomic_bytes(Path(root) / "catalogue.html", _page("Research areas", "These are places to look for products, not recommendations or proof of demand. Observed examples and sensible related ideas are labelled separately.", content).encode())
    return summary
