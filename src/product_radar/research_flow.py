"""Small connected saved-evidence flow; never collects or publishes products."""

from html import escape
import re
from urllib.parse import urlsplit

from .discovery import compute_market_signals, explicit_conflict, find_possible_matches, latest_observations
from .brand_reviews import brand_binding, current_brand
from .models import SafetyError, digest, timestamp, utc_now
from .research_ai import CRITERIA, Jobs
from .research_memory import change, key
from .service import CATEGORIES
from .storage import Journal, atomic_bytes, atomic_json

QUALITY_FAILURES = {"AI_TEXT_INVALID", "AI_FIELDS_INVALID", "AI_LIST_INVALID", "AI_NUMBER_INVALID",
                    "AI_ENUM_INVALID", "AI_RESULT_MISSING_OR_DUPLICATED", "AI_PRODUCT_ENVELOPE_INVALID",
                    "AI_MATCH_REFERENCE_INVALID", "AI_CRITERION_ID_INVALID", "AI_EVIDENCE_REFERENCE_INVALID",
                    "AI_UNSUPPORTED_MEETS", "AI_RELEVANCE_REFERENCE_INVALID", "AI_FIT_CRITERIA_INVALID",
                    "AI_FIT_AUDIENCE_CONTRADICTION", "FIT_CRITERION_COVERAGE_INVALID", "FIT_UNSUPPORTED_NEGATIVE",
                    "AI_FIT_REFERENCE_ALIASES_INVALID", "AI_FIT_CRITERION_MISSING", "AI_RELEVANCE_RESULT_MISSING"}
DESCRIPTION_SCOPE = "recommendation-units-v2"
DESCRIPTION_RULES = ("Identify recommendation-level purchase units. A newborn kit, tool set or other bundled offer is one product; "
    "do not invent separate standalone listings for all its components. A creative card's specific title/destination takes "
    "priority over a parent catalogue's general range; parent text supplies context, not extra products for every card. "
    "Return kind=product for exactly one product, mixed for two to eight genuinely distinct standalone products, "
    "or uninterpretable with a concrete reason if the actual purchase units cannot be resolved within that bound.")

# Checklist-only guidance stays out of A/B and marketplace-relevance jobs.
# Adding it does not invalidate accepted unchanged assessments or source facts.
CHECKLIST_SCOPE = "practical-checklist-v2"
CHECKLIST_RULES = (
    "Use a practical product-specific judgment, not a demand for measured proof of every benefit. "
    "Small size and easy shipping may be inferred from the described product form/material/handling; "
    "do not invent dimensions, weight, delivery prices or assured shipping suitability. "
    "Confidence, convenience, time savings, problem solving and quality of life may use a specific plausible "
    "use-based explanation. Saving money may be an explained avoided expense, without an invented amount. "
    "Differentiation and perceived value may use supplied design/use context, labelled as inference; "
    "neither a global uniqueness search nor consumer survey is required. Audience judgment may use relevant "
    "product-use context, but not a blanket gender stereotype or invented demographic percentage. "
    "Ordinary advertisements do not establish past profitable winners; margin remains Not assessed. "
    "Unclear is appropriate when there is no useful basis, not merely because a benefit is unstated in an advert. "
    "Earlier judgments are review context, not source evidence or a required score. Reconsider them against "
    "the current supplied facts. If a state changes, explain the new information or error being corrected in "
    "that criterion's reason; a seller, colour or pack change alone is not a reason to discard a supported benefit. "
    "Do not preserve an unsupported past judgment to reach seven points. Cite only current supplied observation "
    "IDs for facts and inference inputs."
)
FIT_CHECKLIST_SCOPE = "practical-fit-10-v1"
FIT_CHECKLIST_RULES = (
    "Judge the ten supplied original criterion IDs from current described product features and supplied observations. "
    "Practical labelled inference is allowed; missing measured proof is not a negative verdict. "
    "For time savings identify the ordinary task and the mechanism that plausibly reduces steps, handling or repeated effort; "
    "never invent minutes or percentages. Judge likely Indian buyer/use positioning moderately as women-centric, "
    "men-centric, unisex or unclear; distinguish a baby's use from its purchaser and do not label whole categories automatically. "
    "A specific household kitchen aid can reasonably be women-centric; a plain suitcase may be unisex. "
    "Unknown is Unclear, not Does not meet. Explain each benefit separately rather than copying generic praise. "
    "High margin, past winners and extreme uniqueness are retired, not assessed or counted. "
    "Do not invent costs, dimensions, demographic shares, sales, profit or RTO. Cite only supplied observation IDs. "
    "Earlier judgments are context, not a required score. Return the requested rows without totals or selection decisions."
)

STATUS_LABELS = {
    "excluded_established_brand": "Excluded: established brand",
    "assessment_pending": "Awaiting checklist review",
    "below_checklist_threshold": "Fewer than seven checklist matches",
    "fit_information_pending": "More product information would help",
    "below_fit_threshold": "Fewer than five supported fit matches",
    "decision_binding_pending": "Saved decision needs a targeted review",
    "needs_market_evidence": "Needs another market check",
    "weak_market_signal": "Market evidence is currently weak",
    "candidate_for_supplier_review": "Ready for supplier search",
}


def exclude_established_brand(memory, review):
    """Apply one source-backed operator correction, not a brand-name blacklist.

    Preserve original model descriptions/jobs and the old canonical concept in
    the event chain. Code records the review; it does not verify website meaning
    or fetch the supplied URL. Existing selection dependency checks invalidate
    affected drafts, without editing frozen reports or publication history.
    """
    from .research_ai import text
    from .models import identifier
    fields = {"concept_id", "expected_description_hash", "reason", "source_url", "source_excerpt", "reviewer", "reviewed_at"}
    if not isinstance(review, dict) or set(review) != fields:
        raise SafetyError("BRAND_REVIEW_INVALID")
    if not isinstance(review["expected_description_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", review["expected_description_hash"]):
        raise SafetyError("BRAND_REVIEW_INVALID")
    identifier(review["concept_id"])
    identifier(review["reviewer"])
    timestamp(review["reviewed_at"])
    for field in ("reason", "source_url", "source_excerpt"):
        text(review[field])
    try:
        url = urlsplit(review["source_url"])
        valid_source = url.scheme == "https" and bool(url.hostname) and not url.username and not url.password
    except ValueError:
        valid_source = False
    if not valid_source:
        raise SafetyError("BRAND_REVIEW_SOURCE_INVALID")
    record = dict(review, decision="established", method="operator_source_review", version="brand-review-v1")
    with Journal(memory.root).locked():
        state = memory.snapshot()
        concept = state["concepts"].get(review["concept_id"])
        if not concept:
            raise SafetyError("KNOWN_CONCEPT_REQUIRED")
        if concept.get("brand_review") == record and concept["description"].get("brand") == "established":
            return {"applied": False, "concept_id": review["concept_id"], "decision": "established"}
        if digest(concept["description"]) != review["expected_description_hash"]:
            raise SafetyError("BRAND_REVIEW_DESCRIPTION_CHANGED")
        updated = dict(concept, description=dict(concept["description"], brand="established"), brand_review=record)
        memory.put("concepts", review["concept_id"], updated, previous=concept,
                   event_id=key("brand-review", record))
        return {"applied": True, "concept_id": review["concept_id"], "decision": "established"}


def research_status(meets, market, *, fit=None):
    if meets is None:
        return "assessment_pending"
    if fit is not None and not fit["eligible"]:
        return fit["reason"]
    if fit is not None and fit.get("fit_policy_id") == "fit-10-v1":
        from .opportunity_policy import evaluate_opportunity
        opportunity = evaluate_opportunity(fit, market)
        return "candidate_for_supplier_review" if opportunity["eligible"] else "needs_market_evidence"
    if fit is None and meets < 7:
        return "below_checklist_threshold"
    if market["available_families"] < 2:
        return "needs_market_evidence"
    if market["positive_families"] == 0:
        return "weak_market_signal"
    return "candidate_for_supplier_review"


def market_follow_up(market, *, fit=None):
    """Explain actual coverage gaps; all three sources are not mandatory."""
    if fit is not None and fit.get("fit_policy_id") == "fit-10-v1":
        from .opportunity_policy import evaluate_opportunity, opportunity_counts
        opportunity = evaluate_opportunity(fit, market)
        available, _ = opportunity_counts(opportunity)
        if opportunity["eligible"]:
            if available >= 2:
                return ("Two usable independent market sources support this priority. Growth is not required. "
                        "Complete other useful planned checks without holding up otherwise suitable products.")
            return ("One usable source gives supported positive context. This is a lower-priority option, not a rejection. "
                    "Complete other useful planned checks without requiring a second source before inclusion.")
        if available:
            return ("One usable source does not yet give enough positive context for a single-source recommendation. "
                    "Check another independent source or complete the existing source interpretation. This is pending, not a rejection.")
        return ("No usable relevant market source is connected yet. Complete the planned checks; "
                "one reasonably positive source or two usable independent sources can support inclusion.")
    families = market["families"]
    checked = [name for name, value in families.items() if value["available"]]
    if len(checked) >= 2:
        return ("The available market checks are weak; set this product aside unless useful new evidence appears."
                if market["positive_families"] == 0 else "Market coverage is sufficient; remaining checklist and supplier checks still apply.")
    if not checked:
        return ("Check product-specific ads and one independent marketplace or India search-interest source. "
                "Two usable independent sources are needed, and at least one must show a positive signal.")
    options = {"meta": "Check a relevant Amazon list or an India Google Trends series.",
               "marketplace": "Check product-specific ad activity or an India Google Trends series.",
               "trends": "Check product-specific ad activity or a relevant Amazon list."}
    if market["positive_families"] == 0:
        return (options[checked[0]] + " No positive market signal is established yet. "
                "Another usable check alone does not qualify this product; at least one source must also show a positive signal.")
    return (options[checked[0]] + " An existing source already supplies a positive signal. "
            "One additional usable independent source is needed; it does not have to be positive.")


def evidence_indexes(state):
    by_concept, by_semantic = {}, {}
    for association in state["associations"].values():
        if association["concept_id"] is not None:
            by_concept.setdefault(association["concept_id"], set()).add(association["semantic_id"])
    for obs_id, observation in state["observations"].items():
        by_semantic.setdefault(observation["semantic_id"], {})[obs_id] = observation
    return by_concept, by_semantic


def assessment_item(state, concept_id, *, indexes=None, contract_version=None):
    """The canonical current checklist facts, shared by execution and feedback."""
    from .fit_description import FUNCTIONAL_CONTRACT
    if contract_version == FUNCTIONAL_CONTRACT:
        from .functional_types import functional_role_context
        context = functional_role_context(state, concept_id, role="E")
        if context is None:
            raise SafetyError("FIT_FUNCTIONAL_DEFINITION_REQUIRED")
        return {"id": concept_id, "functional_contract": FUNCTIONAL_CONTRACT,
                "meaning_hash": context["meaning_hash"], "description": context["description"],
                "observations": context["observations"]}
    by_concept, by_semantic = indexes if indexes is not None else evidence_indexes(state)
    evidence = []
    for semantic_id in sorted(by_concept.get(concept_id, set())):
        obs_id, observation = next(iter(by_semantic[semantic_id].items()))
        evidence.append({"id": obs_id, "semantic_id": semantic_id,
                         "source_text": observation["semantic"]})
    return {"id": concept_id, "description": state["concepts"][concept_id]["description"],
            "observations": sorted(evidence, key=lambda row: row["id"])}


def fit_description_context(record, description):
    """Allow description estimates only for their explicitly declared contract.

    These are product-use judgments, never observation IDs or market votes.
    Historical contracts cannot gain this meaning by copying a marker.
    """
    from .fit_description import (DESCRIPTION_CONTRACT, DESCRIPTION_INFERENCE_KIND,
                                  FUNCTIONAL_CONTRACT, FUNCTIONAL_INFERENCE_KIND)
    if record.get("contract_version") == FUNCTIONAL_CONTRACT:
        if record.get("fit_description_contract") != FUNCTIONAL_INFERENCE_KIND:
            raise SafetyError("FIT_DESCRIPTION_CONTRACT_REQUIRED")
        return {"description": description, "description_contract": FUNCTIONAL_INFERENCE_KIND}
    if record.get("contract_version") == DESCRIPTION_CONTRACT:
        if record.get("fit_description_contract") != DESCRIPTION_INFERENCE_KIND:
            raise SafetyError("FIT_DESCRIPTION_CONTRACT_REQUIRED")
        return {"description": description, "description_contract": DESCRIPTION_INFERENCE_KIND}
    if "fit_description_contract" in record:
        raise SafetyError("FIT_DESCRIPTION_CONTRACT_INVALID")
    return {}


def current_assessment_fit(state, concept_id, *, expected_policy_id=None, indexes=None):
    """Read the saved contract explicitly; old totals never acquire new meaning."""
    from .fit_policy import FIT_POLICY_ID, LEGACY_POLICY_ID, evaluate_fit
    from .research_ai import FIT_PROMPT_VERSIONS
    record = state["assessments"].get(concept_id)
    if not record or concept_id not in state["concepts"]:
        return None
    try:
        item = assessment_item(state, concept_id, indexes=indexes, contract_version=record.get("contract_version"))
        if record.get("input_hash") != digest(item):
            return None
        from .fit_description import FUNCTIONAL_CONTRACT
        if record.get("contract_version") == FUNCTIONAL_CONTRACT:
            if record.get("input") != item or record.get("meaning_hash") != item["meaning_hash"]:
                return None
            if (record.get("record_hash") != digest({field: value for field, value in record.items() if field != "record_hash"})
                    or digest(record.get("functional_proof")) != record.get("functional_proof_hash")):
                return None
            if record.get("result", {}).get("relevance") != []:
                return None
            # The semantic reader does not turn a projection into a raw source
            # or rebind an earlier established-brand decision to cleaned text.
            if current_brand(state, "concept", concept_id, state["concepts"][concept_id]["description"]) == "established":
                return None
    except (SafetyError, KeyError, TypeError):
        return None
    policy_id = record.get("fit_policy_id", LEGACY_POLICY_ID)
    if expected_policy_id is not None and policy_id != expected_policy_id:
        return None
    if policy_id == FIT_POLICY_ID and record.get("contract_version") not in FIT_PROMPT_VERSIONS:
        return None
    try:
        fit = evaluate_fit(record["result"]["criteria"], policy_id,
                           **fit_description_context(record, item["description"]))
        known = {row["id"] for row in item["observations"]}
        if any(not set(row["references"]) <= known for row in record["result"]["criteria"]):
            return None
        return fit
    except (SafetyError, KeyError, TypeError):
        return None


class _RecordedAssessmentRunner:
    def __init__(self, identity):
        self.identity = identity

    def __call__(self, role, payload, folder):
        raise SafetyError("AI_JOB_PENDING_RECONCILIATION")


def _description_source_job(state, item, payload, runner):
    """Resolve a new A answer's actual owner, not Jobs' derived batch ID."""
    from .research_ai import MODEL, _item_bindings, prompt_version
    context = {field: value for field, value in payload.items() if field != "items"}
    version = prompt_version("A", payload)
    identity = getattr(runner, "identity", "codex-chatgpt-high-v1")
    found = []
    for job_id, job in state["jobs"].items():
        request = job.get("request", {})
        if (not isinstance(request, dict) or request.get("role") != "A"
                or request.get("prompt_version") != version or request.get("runner") != identity
                or not isinstance(request.get("payload"), dict)):
            continue
        original = request["payload"]
        if {field: value for field, value in original.items() if field != "items"} != context:
            continue
        rows = original.get("items")
        if not isinstance(rows, list):
            continue
        matching = [row for row in rows if isinstance(row, dict) and row.get("id") == item["id"]]
        if not matching or matching[0] != item:
            continue
        if (len(matching) != 1 or set(request) != {"role", "payload", "model", "prompt_version", "runner"}
                or request.get("model") != MODEL or key("job", request) != job_id
                or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in rows)
                or len({row["id"] for row in rows}) != len(rows)
                or job.get("binding_keys") != _item_bindings("A", original, MODEL, version, identity)):
            raise SafetyError("DESCRIPTION_SOURCE_JOB_BINDING_CHANGED")
        found.append(job_id)
    if not found:
        raise SafetyError("DESCRIPTION_SOURCE_JOB_REQUIRED")
    if len(found) != 1:
        raise SafetyError("DESCRIPTION_SOURCE_JOB_BINDING_CONFLICT")
    return found[0]


def _assessment_source_job(state, item, context, version):
    """Resolve one exact recorded full-fit request, never a derived batch ID."""
    from .research_ai import MODEL
    found = []
    for job_id, job in state["jobs"].items():
        request = job.get("request", {})
        if (not isinstance(request, dict) or request.get("role") != "E"
                or request.get("prompt_version") != version or not isinstance(request.get("payload"), dict)):
            continue
        payload = request["payload"]
        if {field: value for field, value in payload.items() if field != "items"} != context:
            continue
        saved_items = payload.get("items", [])
        if not isinstance(saved_items, list):
            continue
        matches = [row for row in saved_items if isinstance(row, dict) and row.get("id") == item["id"]]
        if not matches or matches[0] != item:
            continue
        if (len(matches) != 1 or request.get("model") != MODEL or not isinstance(request.get("runner"), str)
                or key("job", request) != job_id
                or job.get("binding_keys", {}).get(item["id"]) != digest(["E", item, context, MODEL, version, request["runner"]])):
            raise SafetyError("FIT_ASSESSMENT_JOB_BINDING_CHANGED")
        found.append((job_id, request["runner"]))
    if len(found) > 1:
        raise SafetyError("FIT_ASSESSMENT_JOB_BINDING_CONFLICT")
    return found[0] if found else None


def problem(memory, stage, affected_id, reason, *, job_id=None, revision=None):
    problem_id = key("problem", [stage, affected_id, reason, job_id, revision])
    state = memory.snapshot()
    if problem_id not in state["problems"]:
        certainty, retries = "not_recorded", 0
        if job_id and job_id in state["jobs"]:
            from .research_ai import current_attempt
            from .storage import load_json, safe_path
            base = safe_path(memory.root, "ai_jobs", job_id)
            try:
                folder = current_attempt(base)
                if (folder / "result.json").exists() or (folder / "completion.json").exists():
                    certainty = "completed_saved_response"
                elif (folder / "intent.json").exists():
                    certainty = "may_have_started"
                else:
                    certainty = "not_dispatched"
                if (base / "retries.json").exists():
                    retries = len(load_json(base / "retries.json"))
            except SafetyError:
                certainty = "saved_attempt_needs_reconciliation"
        trigger = ("reconcile_saved_attempt" if reason == "AI_JOB_PENDING_RECONCILIATION" else
                   "restore_saved_bindings_or_named_review" if "BINDING" in reason or "BASE_CHANGED" in reason else
                   "model_allowance_or_explicit_resume" if "QUOTA" in reason or "MODEL_LIMIT" in reason else
                   "new_evidence_or_targeted_review")
        memory.put("problems", problem_id, {"stage": stage, "affected_id": affected_id,
            "reason": reason, "job_id": job_id, "revision": revision, "cause_status": "observed", "at": utc_now(),
            "expected": "A usable saved interpretation or identity decision.",
            "observed": reason, "next_trigger": trigger, "execution_certainty": certainty,
            "independent_work": "Other items and saved analysis can continue.", "retry_count": retries})


def run_flow(memory, runner=None, *, limit=10, as_of=None, acquisition_ids=None, fit_policy_id=None):
    # Single research dispatcher, separate from short financial commit locks.
    # A second flow cannot race identity decisions during model calls.
    from .research_ai import bind_model_retry_leader
    with Journal(memory.root).locked() as leader, bind_model_retry_leader(memory, leader):
        return _run_flow(memory, runner, limit=limit, as_of=as_of, acquisition_ids=acquisition_ids, fit_policy_id=fit_policy_id)


def _pending_descriptions(state, unique, deferred, limit, acquisition_ids):
    """Share a selected multi-capture slice without changing saved item meaning.

    Caller acquisition order and captured item order are stable. Every selected
    capture gets a turn; a shared semantic item consumes only one slot. No
    relevance judgment or permanent exclusion is made by this ordering.
    """
    pending = [item_id for item_id in unique
               if item_id not in state["descriptions"] and item_id not in deferred]
    if acquisition_ids is None or len(set(acquisition_ids)) <= 1:
        return pending[:limit]
    waiting = set(pending)
    grouped = {acquisition_id: [] for acquisition_id in acquisition_ids}
    for observation in state["observations"].values():
        acquisition_id, item_id = observation["acquisition_id"], observation["semantic_id"]
        if acquisition_id in grouped and item_id in waiting:
            grouped[acquisition_id].append(item_id)
    queues = {acquisition_id: iter(dict.fromkeys(items))
              for acquisition_id, items in grouped.items()}
    selected, seen = [], set()
    while queues and len(selected) < limit:
        for acquisition_id, queue in tuple(queues.items()):
            item_id = next((item for item in queue if item not in seen), None)
            if item_id is None:
                del queues[acquisition_id]
                continue
            selected.append(item_id)
            seen.add(item_id)
            if len(selected) == limit:
                break
    return selected


def _fresh_model_wait(memory, state, role, payload):
    """Skip only unowned inputs when fresh work has no turn allowance.

    A matching saved request, including one owned by a different original
    runner, stays on its normal Jobs/recovery path. This is only a selection
    hint; actual execution admission and saved-file checks remain fresh.
    """
    from .research_ai import prompt_version
    from .work_turns import model_purpose, new_model_work_wait, review_model_request
    request = {"role": role, "payload": payload}
    reason = new_model_work_wait(memory, model_purpose(request), state=state, review=review_model_request(request))
    if reason is None:
        return None, set()
    version = prompt_version(role, payload)
    context = {name: value for name, value in payload.items() if name != "items"}
    current = {item["id"]: item for item in payload["items"]}
    owned = set()
    for job in state["jobs"].values():
        request = job.get("request", {})
        original = request.get("payload", {})
        if (request.get("role") != role or request.get("prompt_version") != version
                or not isinstance(original, dict)
                or not isinstance(original.get("items"), list)
                or {name: value for name, value in original.items() if name != "items"} != context):
            continue
        for item in original.get("items", []):
            if isinstance(item, dict) and item.get("id") in current and current[item["id"]] == item:
                owned.add(item["id"])
    return reason, set(current) - owned


def _run_flow(memory, runner=None, *, limit=10, as_of=None, acquisition_ids=None, fit_policy_id=None):
    """Advance a finite batch, reusing completed jobs for unchanged inputs."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise SafetyError("RESEARCH_BATCH_LIMIT_INVALID")
    as_of = as_of or utc_now()
    timestamp(as_of)
    from .fit_policy import FIT_POLICY_ID, LEGACY_POLICY_ID, policy_for
    policy_id = fit_policy_id or LEGACY_POLICY_ID
    policy = policy_for(policy_id)
    from .model_recovery import current_model_retry_bindings, run_current_model_job
    jobs = Jobs(memory, runner)
    state = memory.snapshot()
    preparation_waits = []
    allowed_semantics = None
    if acquisition_ids is not None:
        if (not isinstance(acquisition_ids, list) or not 1 <= len(acquisition_ids) <= 10
                or any(not isinstance(value, str) for value in acquisition_ids)
                or not set(acquisition_ids) <= {c["acquisition_id"] for c in state["captures"].values()}):
            raise SafetyError("RESEARCH_ACQUISITION_SCOPE_INVALID")
        allowed_semantics = {o["semantic_id"] for o in state["observations"].values() if o["acquisition_id"] in acquisition_ids}
    unique = {}
    for observation in state["observations"].values():
        if allowed_semantics is not None and observation["semantic_id"] not in allowed_semantics:
            continue
        unique.setdefault(observation["semantic_id"], observation["semantic"])
    # Resume known descriptions first; new work is bounded explicitly.
    deferred_descriptions = {p["affected_id"] for p in state["problems"].values()
                             if p["stage"] == "description" and p["reason"] in QUALITY_FAILURES
                             and state["jobs"].get(p.get("job_id"), {}).get("request", {}).get("payload", {}).get("description_scope") == DESCRIPTION_SCOPE}
    description_payload = {"items": [{"id": i, "source_text": text} for i, text in unique.items()],
                           "description_scope": DESCRIPTION_SCOPE, "description_rules": DESCRIPTION_RULES}
    candidates = [item for item in description_payload["items"]
                  if item["id"] not in state["descriptions"] and item["id"] not in deferred_descriptions]
    wait, fresh = _fresh_model_wait(memory, state, "A", dict(description_payload, items=candidates))
    if fresh:
        deferred_descriptions |= fresh
        preparation_waits.append({"stage": "description", "reason": wait, "item_count": len(fresh)})
    # The pure selector removes only proved-ended, due originals whose entire
    # saved batch still appears in this current scope. Legacy callers and
    # uncertain/stale originals retain exactly the existing deferred behavior.
    pending = []
    if any(item["id"] not in deferred_descriptions for item in candidates):
        pending_bindings = jobs.pending_bindings(state)
        pending_bindings -= current_model_retry_bindings(memory, runner, "A", description_payload)
        deferred_descriptions |= jobs.blocked_items("A", description_payload, pending_bindings)
        pending = _pending_descriptions(state, unique, deferred_descriptions, limit, acquisition_ids)
    for offset in range(0, len(pending), 10):
        item_ids = pending[offset:offset + 10]
        payload = {"items": [{"id": i, "source_text": unique[i]} for i in item_ids],
                   "description_scope": DESCRIPTION_SCOPE, "description_rules": DESCRIPTION_RULES}
        try:
            job_id, result = run_current_model_job(memory, runner, "A", payload,
                current_items=description_payload["items"])
        except SafetyError as error:
            for item_id in item_ids:
                problem(memory, "description", item_id, str(error))
            continue
        for item_id, value in result["accepted"].items():
            try:
                source_job_id = _description_source_job(memory.snapshot(),
                    {"id": item_id, "source_text": unique[item_id]}, payload, runner)
            except SafetyError as error:
                problem(memory, "description", item_id, str(error), job_id=job_id)
                continue
            memory.put("descriptions", item_id, {"result": value, "job_id": source_job_id},
                       event_id=key("describe", [source_job_id, item_id]))
        for item_id, reason in result["failures"].items():
            problem(memory, "description", item_id, reason, job_id=job_id)
    state = memory.snapshot()
    # Accepted child order is durable; it is never recomputed on replay.
    remaining = []
    for item_id, description in state["descriptions"].items():
        if allowed_semantics is not None and item_id not in allowed_semantics:
            continue
        for index, product in enumerate(description["result"]["products"]):
            child_id = key("child", [item_id, index, digest(description["result"])])
            association = state["associations"].get(child_id)
            if association is None or association.get("status") == "brand_review_pending":
                remaining.append((child_id, item_id, product))
    identity_slots = 0
    pending_bindings = jobs.pending_bindings(state) if remaining else set()
    for child_id, item_id, product in remaining:
        if identity_slots == limit:
            break
        state = memory.snapshot()  # Includes concepts accepted earlier in this batch.
        previous_association = state["associations"].get(child_id)
        try:
            brand = brand_binding(state, "child", child_id, product)
            if previous_association and (brand["review_id"] != previous_association.get("brand_review_id")
                    or brand["verdict"] == "established"):
                raise SafetyError("BRAND_PENDING_REVIEW_CHANGED")
        except SafetyError as error:
            problem(memory, "brand_binding", child_id, str(error))
            continue
        if product["category"] not in CATEGORIES or brand["verdict"] == "established":
            identity_slots += 1
            memory.put("associations", child_id, {"semantic_id": item_id, "concept_id": None,
                       "status": "excluded", "reason": "out_of_scope" if product["category"] not in CATEGORIES else "established_brand"},
                       previous=previous_association)
            continue
        shortlist = find_possible_matches(product, state["concepts"])
        decision, match_id, reason, job_id = "different", None, "no_known_match", None
        execution_failed = False
        blocked = False
        if shortlist["candidates"]:
            for width in (10, 20):
                shortlist = find_possible_matches(product, state["concepts"], limit=width)
                payload = {"items": [{"id": child_id, "description": product, "candidates": shortlist["candidates"]}]}
                wait, fresh = _fresh_model_wait(memory, state, "B", payload)
                if fresh:
                    preparation_waits.append({"stage": "identity", "reason": wait, "item_count": 1})
                    identity_slots += 1
                    blocked = True
                    break
                # Ordinary identity questions contain one actual child. A
                # historical larger batch is not silently declared current
                # from that one child; the shared selector keeps it pending.
                current_pending = pending_bindings - current_model_retry_bindings(memory, runner, "B", payload)
                if jobs.blocked_items("B", payload, current_pending):
                    blocked = True
                    break
                try:
                    job_id, result = run_current_model_job(memory, runner, "B", payload)
                    if child_id not in result["accepted"]:
                        raise SafetyError(result["failures"][child_id])
                    answer = result["accepted"][child_id]
                    decision, match_id, reason = answer["decision"], answer["match_id"], answer["reason"]
                    if decision == "same" and explicit_conflict(product, state["concepts"][match_id]["description"]):
                        decision, reason = "unclear", "conflicting_observed_function_or_audience"
                except SafetyError as error:
                    decision, reason = "unclear", str(error)
                    execution_failed = reason not in QUALITY_FAILURES
                    break
                if decision != "unclear" or not shortlist["omitted"]:
                    break
        if blocked:
            continue
        identity_slots += 1
        if decision == "unclear":
            problem(memory, "identity", child_id, reason, job_id=job_id)
            if execution_failed:
                continue
            # Save this evidence-dependent deferral, rather than cycling the same
            # question on each invocation. New interpretation creates a new child.
            memory.put("associations", child_id, {"semantic_id": item_id, "concept_id": None,
                       "status": "deferred", "reason": reason, "job_id": job_id}, previous=previous_association)
            continue
        concept_id = match_id if decision == "same" else key("concept", child_id)
        changes = []
        origin = ({"origin_child_id": child_id, "origin_brand_review_id": brand["review_id"],
                   "origin_description_hash": digest(product)} if brand["review_id"] else {})
        if decision != "same":
            changes.append(change("concepts", concept_id, {"description": product, "status": "provisional", **origin}))
        changes.append(change("associations", child_id, {"semantic_id": item_id, "concept_id": concept_id,
                       "status": "resolved", "reason": reason, "job_id": job_id,
                       "method": "llm_accepted" if job_id else "no_known_match", **origin}, previous_association))
        def unchanged_brand(current, target=child_id, description=product, expected_brand=brand):
            if brand_binding(current, "child", target, description) != expected_brand:
                raise SafetyError("BRAND_RESOLUTION_BINDING_CHANGED")
        try:
            memory.commit(key("resolve-brand", [child_id, brand["review_id"]]) if brand["review_id"] else key("resolve", child_id),
                          changes, validate=unchanged_brand)
        except SafetyError as error:
            problem(memory, "identity", child_id, str(error), job_id=job_id)
            continue
    state = memory.snapshot()
    assessment_items = []
    named_fit_reviews = []
    by_concept, by_semantic = evidence_indexes(state)
    for concept_id, concept in state["concepts"].items():
        try:
            effective_brand = current_brand(state, "concept", concept_id, concept["description"])
        except SafetyError as error:
            problem(memory, "brand_binding", concept_id, str(error))
            continue
        if effective_brand == "established" or concept.get("status") in ("excluded", "merged", "retired"):
            continue
        semantic_ids = by_concept.get(concept_id, set())
        if allowed_semantics is not None and not semantic_ids & allowed_semantics:
            continue
        from .functional_types import functional_role_context
        try:
            functional_context = functional_role_context(state, concept_id, role="C")
        except SafetyError as error:
            problem(memory, "fit_contract", concept_id, str(error))
            continue
        if functional_context is not None:
            # Accepted type meanings belong to the explicitly versioned named
            # fit workflow. The legacy batch must never overwrite them using
            # a retail title or its superseded broad examples.
            from .fit_description import FUNCTIONAL_CONTRACT
            from .fit_review import prepare_practical_fit_reviews
            if policy_id == FIT_POLICY_ID and len(named_fit_reviews) < limit:
                prepared = prepare_practical_fit_reviews(memory, concept_ids=[concept_id],
                    as_of=as_of, contract_version=FUNCTIONAL_CONTRACT)[0]
                if prepared.get("review_id"):
                    named_fit_reviews.append(prepared["review_id"])
            continue
        # Stable original observation references let repeated dated collection
        # reuse checklist meaning while market arithmetic uses fresh observations.
        item = assessment_item(state, concept_id, indexes=(by_concept, by_semantic))
        input_hash = digest(item)
        previous = state["assessments"].get(concept_id)
        if policy_id == FIT_POLICY_ID:
            prepared_bases = [(review_id, base) for review_id, base in state["source_checks"].items()
                              if base.get("kind") == "fit_review_base"
                              and base["binding"]["concept_id"] == concept_id
                              and base["binding"]["input_hash"] == input_hash
                              and base["binding"]["parent_assessment_hash"] == (digest(previous) if previous else None)
                              and key("fit-review-apply", review_id) not in state["source_checks"]]
            if prepared_bases:
                # A deliberately prepared full/partial question already owns
                # these exact facts. Ordinary flow must not bypass it with a
                # second question lacking its named/frozen base.
                for review_id, base in prepared_bases:
                    if (len(named_fit_reviews) < limit and not any(
                            p["stage"] == "fit_review" and p.get("revision") == review_id
                            and p["reason"] in QUALITY_FAILURES for p in state["problems"].values())):
                        named_fit_reviews.append(review_id)
                continue
        if (policy_id == FIT_POLICY_ID and previous and previous.get("input_hash") == input_hash
                and previous.get("fit_policy_id") == FIT_POLICY_ID):
            if current_assessment_fit(state, concept_id, expected_policy_id=FIT_POLICY_ID, indexes=(by_concept, by_semantic)) is None:
                problem(memory, "fit_contract", concept_id, "FIT_CURRENT_CONTRACT_REVIEW_REQUIRED", revision=input_hash)
                continue
            bases = [(review_id, base) for review_id, base in state["source_checks"].items()
                     if base.get("kind") == "fit_review_base"
                     and base["binding"]["concept_id"] == concept_id
                     and base["binding"]["parent_assessment_hash"] == digest(previous)]
            if not bases and previous.get("projection") and len(named_fit_reviews) < limit:
                from .fit_review import project_saved_fit
                try:
                    recovered = project_saved_fit(memory, concept_id, as_of=as_of)
                except SafetyError as error:
                    problem(memory, "fit_review", concept_id, str(error), revision=input_hash)
                    continue
                if recovered.get("review_id"):
                    named_fit_reviews.append(recovered["review_id"])
            for review_id, base in bases:
                if (len(named_fit_reviews) < limit
                        and key("fit-review-apply", review_id) not in state["source_checks"]
                        and not any(p["stage"] == "fit_review" and p.get("revision") == review_id
                                    and p["reason"] in QUALITY_FAILURES for p in state["problems"].values())):
                    named_fit_reviews.append(review_id)
        if (policy_id == FIT_POLICY_ID and previous and previous.get("input_hash") == input_hash
                and previous.get("fit_policy_id", LEGACY_POLICY_ID) == LEGACY_POLICY_ID):
            if len(named_fit_reviews) < limit:
                from .fit_review import project_saved_fit
                try:
                    projected = project_saved_fit(memory, concept_id, as_of=as_of)
                except SafetyError as error:
                    problem(memory, "fit_review", concept_id, str(error), revision=input_hash)
                    continue
                if projected.get("review_id"):
                    named_fit_reviews.append(projected["review_id"])
            continue
        failed_same_input = any(p["stage"] == "assessment" and p["affected_id"] == concept_id
                                and p["revision"] == input_hash and p["reason"] in QUALITY_FAILURES
                                and state["jobs"].get(p.get("job_id"), {}).get("request", {}).get("payload", {}).get("fit_policy_id", LEGACY_POLICY_ID) == policy_id
                                for p in state["problems"].values())
        if (not previous or previous["input_hash"] != input_hash
                or previous.get("fit_policy_id", LEGACY_POLICY_ID) != policy_id) and not failed_same_input:
            assessment_items.append((item, input_hash, previous))
    context = {"criteria": [{"id": n, "name": label} for n, label in enumerate(CRITERIA, 1)],
               "checklist_scope": CHECKLIST_SCOPE, "checklist_rules": CHECKLIST_RULES}
    if policy_id == FIT_POLICY_ID:
        context = {"mode": "fit_assessment", "fit_policy_id": policy_id,
                   "requested_criterion_ids": list(policy["active_ids"]),
                   "criteria": [{"id": n, "name": CRITERIA[n - 1]} for n in policy["active_ids"]],
                   "checklist_scope": FIT_CHECKLIST_SCOPE, "checklist_rules": FIT_CHECKLIST_RULES}
    current_assessment_items = [dict(item, earlier_judgments=previous["result"]["criteria"] if previous else [])
        for item, _, previous in assessment_items]
    wait, fresh = _fresh_model_wait(memory, state, "E", dict(context, items=current_assessment_items))
    if fresh:
        preparation_waits.append({"stage": "assessment", "reason": wait, "item_count": len(fresh)})
        assessment_items = [row for row in assessment_items if row[0]["id"] not in fresh]
    pending_bindings = set()
    if assessment_items:
        pending_bindings = jobs.pending_bindings(state)
        pending_bindings -= current_model_retry_bindings(memory, runner, "E",
            dict(context, items=current_assessment_items))
        assessment_items = [(item, input_hash, previous) for item, input_hash, previous in assessment_items
                            if not jobs.blocked_items("E", dict(context, items=[dict(item,
                                earlier_judgments=previous["result"]["criteria"] if previous else [])]), pending_bindings)]
    grouped = {}
    from .research_ai import prompt_version
    version = prompt_version("E", context)
    ready = 0
    for item, input_hash, previous in assessment_items:
        if ready == limit:
            break
        owner = getattr(runner, "identity", "codex-chatgpt-high-v1")
        if policy_id == FIT_POLICY_ID:
            model_item = dict(item, earlier_judgments=previous["result"]["criteria"] if previous else [])
            try:
                original = _assessment_source_job(state, model_item, context, version)
            except SafetyError as error:
                problem(memory, "assessment", item["id"], str(error), revision=input_hash)
                continue
            if original:
                if state["jobs"][original[0]]["binding_keys"][item["id"]] in pending_bindings:
                    continue  # The original owner, not the incoming runner, owns this unfinished slot.
                owner = original[1]
            elif runner is None:
                problem(memory, "assessment", item["id"], "AI_RUNNER_NOT_ENABLED", revision=input_hash)
                continue  # No model is enabled; do not claim a placeholder runner variant.
        grouped.setdefault(owner, []).append((item, input_hash, previous))
        ready += 1
    batches = [(owner, items[offset:offset + 5]) for owner, items in grouped.items()
               for offset in range(0, len(items), 5)]
    for owner, batch in batches:
        # Earlier verdicts belong to this model item, not the whole batch:
        # per-item job reuse must survive completion/reordering of siblings.
        # The canonical fact item/input_hash above stays free of prior verdicts.
        payload = dict(context, items=[dict(item, earlier_judgments=previous["result"]["criteria"] if previous else [])
                                      for item, _, previous in batch])
        selected_runner = runner
        if policy_id == FIT_POLICY_ID and owner != getattr(runner, "identity", "codex-chatgpt-high-v1"):
            selected_runner = _RecordedAssessmentRunner(owner)
        try:
            job_id, result = run_current_model_job(memory, selected_runner, "E", payload,
                current_items=current_assessment_items)
        except SafetyError as error:
            for item, input_hash, _ in batch:
                problem(memory, "assessment", item["id"], str(error), revision=input_hash)
            continue
        for item, input_hash, previous in batch:
            concept_id = item["id"]
            source_job_id = job_id
            if policy_id == FIT_POLICY_ID:
                model_item = dict(item, earlier_judgments=previous["result"]["criteria"] if previous else [])
                try:
                    original = _assessment_source_job(memory.snapshot(), model_item, context, version)
                    if not original:
                        raise SafetyError("FIT_ASSESSMENT_SOURCE_JOB_REQUIRED")
                    source_job_id = original[0]
                except SafetyError as error:
                    problem(memory, "assessment", concept_id, str(error), job_id=job_id, revision=input_hash)
                    continue
            if concept_id not in result["accepted"]:
                problem(memory, "assessment", concept_id, result["failures"][concept_id], job_id=source_job_id, revision=input_hash)
                continue
            value = {"result": result["accepted"][concept_id], "input_hash": input_hash,
                     "job_id": source_job_id, "used_record_ids": [o["id"] for o in item["observations"]],
                     "diagnostics": result["diagnostics"]}
            if policy_id == FIT_POLICY_ID:
                value.update(fit_policy_id=policy_id, contract_version=prompt_version("E", payload))
            if policy_id == FIT_POLICY_ID:
                def unchanged(current, target=concept_id, prior=previous, facts=item):
                    active = current["concepts"].get(target, {})
                    if (current["assessments"].get(target) != prior or assessment_item(current, target) != facts
                            or active.get("status") in ("excluded", "merged", "retired")
                            or current_brand(current, "concept", target, active["description"]) == "established"):
                        raise SafetyError("FIT_ASSESSMENT_BASE_CHANGED")
                try:
                    memory.commit(key("assess", [source_job_id, concept_id]),
                                  [change("assessments", concept_id, value, previous)], validate=unchanged)
                except SafetyError as error:
                    problem(memory, "assessment", concept_id, str(error), job_id=source_job_id, revision=input_hash)
                    continue
            else:
                memory.put("assessments", concept_id, value, previous=previous,
                           event_id=key("assess", [job_id, concept_id]))
    if named_fit_reviews:
        from .fit_review import run_fit_reviews
        run_fit_reviews(memory, runner, named_fit_reviews, limit=limit)
    summary = candidate_summary(memory, as_of=as_of)
    if preparation_waits:
        grouped_waits = {}
        for wait in preparation_waits:
            pair = (wait["stage"], wait["reason"])
            grouped_waits[pair] = grouped_waits.get(pair, 0) + wait["item_count"]
        summary["preparation_waits"] = [{"stage": stage, "reason": reason, "item_count": count}
            for (stage, reason), count in grouped_waits.items()]
    return summary


def trends_description_current(state, concept_id, evidence, *, as_of=None):
    """Bind a chart to product meaning; retain unchanged legacy source origins.

    Legacy captures predate an explicit description_hash. Their immutable
    original source description and origin child can establish the same bound
    meaning without changing events or migrating live records.
    """
    concept = state["concepts"].get(concept_id)
    if not concept or evidence.get("concept_id") != concept_id:
        return False
    current_hash = digest(concept["description"])
    evidence_id = evidence.get("id") or key("trends", [concept_id, evidence.get("acquisition_id")])
    actual = {field: value for field, value in evidence.items() if field != "id"}
    scope_reviews = [row for row in state.get("source_checks", {}).values()
                     if row.get("kind") == "trends_scope_review"
                     and row.get("review", {}).get("evidence_id") == evidence_id
                     and row["review"].get("expected_evidence_hash") == digest(actual)
                     and row["review"].get("expected_description_hash") == current_hash
                     and (as_of is None or timestamp(row["at"]) <= timestamp(as_of))]
    if scope_reviews:
        latest = max(scope_reviews, key=lambda row: (timestamp(row["at"]), row["reference"]))
        if latest["review"]["relationship"] not in ("exact_product", "relevant_family"):
            return False
    if "description_hash" in evidence:
        return evidence["description_hash"] == current_hash
    origins = [(child_id, a) for child_id, a in state["associations"].items()
               if a.get("concept_id") == concept_id and a.get("status") == "resolved"
               and (key("concept", child_id) == concept_id or a.get("method") == "no_known_match")]
    for child_id, association in origins:
        description = state["descriptions"].get(association.get("semantic_id"), {}).get("result")
        if not isinstance(description, dict):
            continue
        for index, product in enumerate(description.get("products", [])):
            if key("child", [association["semantic_id"], index, digest(description)]) == child_id:
                return digest(product) == current_hash
    return False


def candidate_summary(memory, *, as_of=None):
    from .fit_policy import FIT_POLICY_ID
    from .opportunity_policy import evaluate_opportunity
    as_of = as_of or utc_now()
    state = memory.snapshot()
    products = []
    recovered_market_revisions = set()
    by_concept, by_semantic = evidence_indexes(state)
    from .research_records import iter_functional_definitions
    functional_ids = {row.get("concept_id") for _, row in iter_functional_definitions(state)}
    _, latest_by_semantic = evidence_indexes(dict(state, observations=latest_observations(state["observations"], as_of)))
    for concept_id, concept in state["concepts"].items():
        # Redirected working duplicates are history, not another product row.
        # Their original source associations now belong to the survivor.
        if concept.get("status") in ("merged", "retired"):
            continue
        semantic_ids = by_concept.get(concept_id, set())
        observations = {i: o for semantic_id in sorted(semantic_ids) for i, o in by_semantic[semantic_id].items()}
        fresh = {i: o for semantic_id in sorted(semantic_ids) for i, o in latest_by_semantic.get(semantic_id, {}).items()}
        assessment = state["assessments"].get(concept_id)
        from .fit_description import FUNCTIONAL_CONTRACT
        functional_fit = concept_id in functional_ids or bool(assessment and assessment.get("contract_version") == FUNCTIONAL_CONTRACT)
        # The common reader dispatches the saved contract and current semantic
        # input. A legacy raw-title comparison cannot decide a functional fit.
        fit = current_assessment_fit(state, concept_id, indexes=(by_concept, by_semantic)) if assessment else None
        if assessment and fit is None:
            assessment = None
        if functional_fit and assessment and assessment.get("contract_version") != FUNCTIONAL_CONTRACT:
            assessment, fit = None, None
        relevance = {}
        if assessment:
            for row in assessment["result"]["relevance"]:
                relevance[state["observations"][row["id"]]["semantic_id"]] = row["relationship"]
        # Independent marketplace matching must not overwrite the checklist's
        # model job or pretend its old input contained newly attached evidence.
        from .market_validation import REVIEW_VERSION
        reviews = sorted((r for r in state.get("market_reviews", {}).values()
                          if r["concept_id"] == concept_id
                          and not functional_fit
                          and r.get("review_version") == REVIEW_VERSION
                          and r["description_hash"] == digest(concept["description"])
                          and timestamp(r["reviewed_at"]) <= timestamp(as_of)),
                         key=lambda r: (timestamp(r["reviewed_at"]), r["input_hash"]))
        for review in reviews:
            recovered_market_revisions.add((concept_id, review["input_hash"]))
            for row in review["relevance"]:
                observation = state["observations"].get(row["id"])
                if observation is None or timestamp(observation["observed_at"]) > timestamp(as_of):
                    continue
                semantic_id = observation["semantic_id"]
                relevance[semantic_id] = row["relationship"]
                if row["relationship"] in ("exact_product", "relevant_family"):
                    observations[row["id"]] = dict(observation, id=row["id"])
                fresh.update(latest_by_semantic.get(semantic_id, {}))
        # Resolve latest source content globally first: if an ad now describes
        # something else, its older version cannot keep supporting this concept.
        trends_evidence = [dict(id=i, **e) for i, e in state["trends_evidence"].items()
                           if not functional_fit
                           if trends_description_current(state, concept_id, e, as_of=as_of)]
        from .market_validation import current_source_support
        market = compute_market_signals(fresh, relevance, as_of, trends_evidence,
                                       source_support={} if functional_fit else current_source_support(state, concept_id, as_of=as_of))
        functional_market, market_scope_problem = None, None
        if functional_fit:
            from .market_validation import functional_market_context
            try:
                functional_market = functional_market_context(state, concept_id, as_of=as_of)
                if functional_market is not None:
                    market = functional_market["market"]
                    for observation_id, match in functional_market["matches"].items():
                        observation = state["observations"].get(observation_id)
                        if observation is not None:
                            observations[observation_id] = dict(observation, id=observation_id)
                            relevance[observation["semantic_id"]] = match["relationship"]
            except (SafetyError, KeyError, TypeError, ValueError) as error:
                market_scope_problem = str(error)
        meets = fit["kpi_matches"] if fit else None
        current_fit = fit if fit and fit["fit_policy_id"] == FIT_POLICY_ID else None
        try:
            effective_brand = brand_binding(state, "concept", concept_id, concept["description"])
            brand_problem = None
        except SafetyError as error:
            effective_brand, brand_problem = None, str(error)
        established = effective_brand and effective_brand["verdict"] == "established"
        dates = [o["observed_at"] for o in observations.values()]
        products.append({"id": concept_id, "label": concept["description"]["label"], "category": concept["description"]["category"],
            "identity_status": "provisional", "first_seen": min(dates, key=timestamp), "last_observed": max(dates, key=timestamp),
            "observations": len(observations), "checklist_meets": meets,
            "fit_policy_id": fit["fit_policy_id"] if fit else assessment.get("fit_policy_id") if assessment else None,
            "checklist_total": fit["kpi_total"] if fit else None,
            "fit_tier": fit["fit_tier"] if fit else None,
            "fit": fit, "brand_binding": effective_brand, "brand_binding_problem": brand_problem,
            **({"opportunity": evaluate_opportunity(current_fit, market)}
               if current_fit else {}),
            "criteria": assessment["result"]["criteria"] if assessment else [], "market": market,
            **({"market_scope_status": "reviewed" if functional_market and functional_market["scoped_evidence"] else
                "reviewed_no_usable_match" if functional_market and functional_market["context_reviews"] else "needs_review",
                "functional_market_contract": (functional_market or {}).get("contract"),
                "market_scope_problem": market_scope_problem} if functional_fit else {}),
            "status": "decision_binding_pending" if brand_problem else "excluded_established_brand" if established else research_status(meets, market, fit=current_fit),
            "market_follow_up": ("Established branded product; do not pursue it as an unbranded recommendation."
                                 if established else "The saved brand correction needs a targeted binding review."
                                 if brand_problem else "Some fit criteria are unknown; a named product review can resolve them without treating unknowns as failures."
                                 if current_fit and current_fit["reason"] == "fit_information_pending" else
                                 "Fewer than five supported fit matches; retain the exact reasons and reconsider only with useful new facts."
                                 if current_fit and not current_fit["eligible"] else
                                 "Below seven checklist matches; review missing product facts before further market research."
                                 if current_fit is None and meets is not None and meets < 7 else
                                 "Saved market results still need checking against this product type; old broader matches are not counted."
                                 if functional_fit and (market_scope_problem or not functional_market or
                                    not functional_market["scoped_evidence"] and not functional_market["context_reviews"])
                                 else "The saved source results were checked, but did not show usable information for this product type."
                                 if functional_fit and functional_market and not functional_market["scoped_evidence"]
                                 else market_follow_up(market, fit=current_fit)),
            "prices": [{"observation_id": i, "observed_at": o["observed_at"], **o["facts"]["price"]} for i, o in observations.items()
                       if "price" in o["facts"] and relevance.get(o["semantic_id"]) in ("exact_product", "relevant_family")],
            # This view owns product/market research, not the supplier join.
            # Actual completed searches are joined by report_selection; do not
            # falsely label them unsearched merely because this view omits them.
            "client_ready": False, "supplier_status": "not_evaluated_in_this_view",
            "compliance_note": assessment["result"]["compliance_note"] if assessment else "Not assessed",
            "missing_questions": assessment["result"]["missing_questions"] if assessment else []})
    # The writing helper validates each relevant completed G job once, using
    # this same snapshot. It does not run models or rewrite historical notes.
    from .report_narration import recovered_narration_problems
    # Some report-pool projections deliberately supply only a frozen snapshot,
    # not access to saved AI artifacts. They cannot certify this recovery.
    recovered_narration = (recovered_narration_problems(state, memory.root, as_of=as_of)
                          if getattr(memory, "root", None) is not None else set())
    problems = []
    for problem_id, saved in state["problems"].items():
        stage, affected = saved["stage"], saved["affected_id"]
        recovered = ((stage == "description" and affected in state["descriptions"])
                     or (stage == "identity" and state["associations"].get(affected, {}).get("status") in ("resolved", "excluded"))
                     or (stage == "assessment" and affected in state["assessments"]
                         and state["assessments"][affected]["input_hash"] == saved["revision"])
                     or (stage == "market_relevance" and (affected, saved["revision"]) in recovered_market_revisions)
                     or (stage == "fit_review" and key("fit-review-apply", saved.get("revision")) in state["source_checks"])
                     or (stage == "fit_contract" and current_assessment_fit(state, affected, expected_policy_id=FIT_POLICY_ID) is not None)
                     or (stage == "report_narration" and problem_id in recovered_narration))
        if stage == "batch_dispatch":
            recovered = any(
                run.get("actions", {}).get(affected, {}).get("status") in ("captured", "reused")
                and any(action["action_id"] == affected and digest(action) == saved["revision"]
                        for action in state["batch_plans"].get(run["plan_id"], {}).get("actions", []))
                for run in state["batch_runs"].values())
        problems.append(dict(saved, current_status="recovered" if recovered else "unresolved"))
    return {"schema_version": 1, "evidence_type": memory.evidence_type, "as_of": as_of,
            "client_report": False, "products": products, "captures": len(state["captures"]),
            "observations": len(state["observations"]), "described_items": len(state["descriptions"]),
            "deferred_items": [dict(id=i, **a) for i, a in state["associations"].items() if a["status"] != "resolved"],
            "uninterpreted_items": [{"id": i, "kind": d["result"]["kind"], "reason": d["result"]["reason"]}
                                    for i, d in state["descriptions"].items() if not d["result"]["products"]],
            "problems": problems,
            "unresolved_issues": sum(p["current_status"] == "unresolved" for p in problems),
            "recovered_issues": sum(p["current_status"] == "recovered" for p in problems),
            "catalogue_seeds": [{"concept_id": i, "category": c["description"]["category"], "label": c["description"]["label"],
                                 "function": c["description"]["function"], "origin": "interpreted_capture", "admitted_family": False}
                                for i, c in state["concepts"].items() if c.get("status") not in ("merged", "retired")]}


def write_summary(memory, summary):
    atomic_json(memory.root / "candidate_summary.json", summary)
    rows = []
    for product in summary["products"]:
        market_label = (product["market"].get("source_label", product["market"]["label"])
                        if product.get("fit_policy_id") == "fit-10-v1" else product["market"]["label"])
        cells = (product["label"], product["category"], str(product["checklist_meets"]) + "/" + str(product.get("checklist_total", 13)) if product["checklist_meets"] is not None else "Not assessed",
                 market_label, STATUS_LABELS[product["status"]], product["market_follow_up"], product["last_observed"])
        rows.append("<tr>" + "".join("<td>" + escape(c) + "</td>" for c in cells) + "</tr>")
    document = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Research working summary</title>
<style>body{font:16px system-ui;margin:32px;background:#faf8f3;color:#283342}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:12px;border-bottom:1px solid #dedbd4;vertical-align:top}p{max-width:75ch}</style>
<h1>Research working summary</h1><p>Internal research, not a client report. Checklist judgments are separate from market evidence. Supplier search results and full report readiness are shown separately; this view does not say whether a supplier search is complete. Publication checks are still required.</p>"""
    if summary["evidence_type"] == "synthetic":
        document += "<p>This small manufactured example checks product grouping and reuse of saved work. Its ads and checklist answers are fictional. Marketplace and Google Trends evidence were not supplied, so the products remain examples awaiting market checks.</p>"
    document += "<p>Current recommendations are prioritised, not filtered by a mandatory two-out-of-three source-type rule. Six or more checklist matches with two usable independent sources come first; five matches with two sources come next. Six or more matches with one reasonably positive source come third; five matches with that support come fourth. Facebook ads, Amazon India, Flipkart, Google Trends, Blinkit, Zepto and Instamart are separate sources. Multiple searches on one site count once. Prices and catalogue presence do not prove sales. Historical saved reports keep their original rules.</p>"
    document += "<p>Evidence: " + escape(summary["evidence_type"]) + ". Products: " + str(len(rows)) + ". Unresolved issues: " + str(summary["unresolved_issues"]) + ". Recovered earlier issues: " + str(summary["recovered_issues"]) + ".</p>"
    document += "<p>Saved source items: " + str(summary["observations"]) + ". Items interpreted: " + str(summary["described_items"]) + ". These are source records, not necessarily different products. The working flow reviews a limited batch at a time.</p>"
    document += "<table><thead><tr>" + "".join("<th>" + h + "</th>" for h in ("Product", "Category", "Checklist", "Market evidence", "Next status", "Next market check", "Last observed")) + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></html>"
    atomic_bytes(memory.root / "candidate_summary.html", document.encode())
