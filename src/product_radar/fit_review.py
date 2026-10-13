"""Finite saved-input migration and named fit review with a frozen base.

The existing memory stores bases and application receipts. A later answer can
only complete its own base; it cannot rebase itself or improve a target score.
"""

from copy import deepcopy

from .brand_reviews import brand_binding, current_brand
from .fit_policy import FIT_POLICY_ID, LEGACY_POLICY_ID, evaluate_fit, policy_for
from .fit_description import (DESCRIPTION_CONTRACT, DESCRIPTION_INFERENCE_KIND, DESCRIPTION_STYLE,
                              description_inference_context, validate_description_inference,
                              FUNCTIONAL_CONTRACT, FUNCTIONAL_INFERENCE_KIND, FUNCTIONAL_STYLE,
                              functional_inference_context, functional_changed_criteria)
from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .research_memory import change, key

VERSION = "saved-fit-review-v1"
CONTRACT = "research-e-fit10-v1"
PRACTICAL_CONTRACT = "research-e-fit10-v2"
CONTRACTS = (CONTRACT, PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT)


def _description_kwargs(contract_version, description):
    if contract_version == FUNCTIONAL_CONTRACT:
        return {"description": description, "description_contract": FUNCTIONAL_INFERENCE_KIND}
    return ({"description": description, "description_contract": DESCRIPTION_INFERENCE_KIND}
            if contract_version == DESCRIPTION_CONTRACT else {})


def _repairable_output_failures(contract_version):
    """Only a finite correction of the same answer, never an input repair."""
    from .research_flow import QUALITY_FAILURES
    failures = QUALITY_FAILURES | {"AI_FIT_REFERENCE_ALIASES_INVALID", "AI_FIT_CRITERION_MISSING"}
    if contract_version in (DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT):
        failures |= {"FIT_DESCRIPTION_BINDING_INVALID", "FIT_DESCRIPTION_FIELDS_INVALID",
                     "FIT_DESCRIPTION_INFERENCE_INVALID"}
    if contract_version == FUNCTIONAL_CONTRACT:
        failures |= {"AI_FUNCTIONAL_FIT_MARKET_CLAIM_REFUSED"}
    return failures


class _SavedReviewRunner:
    """Read one recorded runner's job; never invent a second runner variant."""
    def __init__(self, identity):
        self.identity = identity

    def __call__(self, role, payload, folder):
        raise SafetyError("AI_JOB_PENDING_RECONCILIATION")


def _facts(state, concept_id, *, contract_version=None):
    from .research_flow import assessment_item
    concept = state["concepts"].get(concept_id)
    if (not concept or concept.get("status") in ("excluded", "merged", "retired")
            or concept.get("identity_conflicted")):
        raise SafetyError("FIT_REVIEW_ACTIVE_CONCEPT_REQUIRED")
    if current_brand(state, "concept", concept_id, concept["description"]) == "established":
        raise SafetyError("FIT_REVIEW_ACTIVE_CONCEPT_REQUIRED")
    item = assessment_item(state, concept_id, contract_version=contract_version)
    if not item["observations"] and contract_version != FUNCTIONAL_CONTRACT:
        raise SafetyError("FIT_REVIEW_SAVED_FACTS_REQUIRED")
    return item


def _preparation_contract(state, concept_id, contract_version):
    """A new question cannot downgrade accepted meaning; old owners still replay."""
    if contract_version != FUNCTIONAL_CONTRACT:
        from .functional_types import functional_role_context
        if functional_role_context(state, concept_id, role="C") is not None:
            raise SafetyError("FIT_FUNCTIONAL_CONTRACT_REQUIRED")


def _unknown(number, reason):
    return {"id": number, "state": "Unclear", "basis": "not_stated", "references": [], "reason": reason}


def _retained_rows(parent, refs, *, review_ids, description=None):
    old = {row["id"]: row for row in (parent or {}).get("result", {}).get("criteria", [])}
    rows, diagnostics = [], []
    for number in policy_for(FIT_POLICY_ID)["active_ids"]:
        row = old.get(number)
        if number in review_ids:
            rows.append(_unknown(number, "This criterion has a new practical meaning and awaits its named review."))
        elif row is None:
            rows.append(_unknown(number, "No applicable saved judgment was available."))
        elif (not set(row.get("references", [])) <= refs or
              row.get("state") in ("Meets", "Does not meet") and
              (row.get("basis") not in ("observed", "inferred") or not row.get("references"))
              and not validate_description_inference(row, description, **{
                  "description_contract": FUNCTIONAL_INFERENCE_KIND
                  if (parent or {}).get("contract_version") == FUNCTIONAL_CONTRACT else DESCRIPTION_INFERENCE_KIND
                  if (parent or {}).get("contract_version") == DESCRIPTION_CONTRACT else None})):
            diagnostics.append({"criterion_id": number, "code": "SAVED_FIT_UNSUPPORTED_JUDGMENT",
                                "original_row": deepcopy(row)})
            rows.append(_unknown(number, "The earlier judgment lacked a stated supporting basis; this is unknown, not a failure."))
        else:
            retained = {field: deepcopy(row[field]) for field in ("id", "state", "basis", "references", "reason")}
            if "description_inference" in row:
                retained["description_inference"] = deepcopy(row["description_inference"])
            rows.append(retained)
    evaluate_fit(rows, FIT_POLICY_ID, **_description_kwargs((parent or {}).get("contract_version"), description))
    return rows, diagnostics


def _review_base(state, concept_id, item, parent, requested, reason, at, *, contract_version=CONTRACT, attribution=None):
    refs = {row["id"] for row in item["observations"]}
    rows, diagnostics = _retained_rows(parent, refs, review_ids=requested, description=item["description"])
    retained = [row for row in rows if row["id"] not in requested]
    if contract_version in (DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT):
        # The NEW v3 base has an explicit nullable field on every row. Older
        # observation-backed judgments keep their meaning, not a manufactured
        # description basis, and the old accepted answer remains unchanged.
        for row in retained:
            row.setdefault("description_inference", None)
    binding = {"version": VERSION, "concept_id": concept_id, "fit_policy_id": FIT_POLICY_ID,
               "contract_version": contract_version,
               "mode": "fit_assessment" if requested == list(policy_for(FIT_POLICY_ID)["active_ids"]) else "fit_partial_review",
               "input": deepcopy(item), "input_hash": digest(item),
               "parent_assessment_hash": digest(parent) if parent else None,
               "parent_assessment": deepcopy(parent), "retained_rows": retained,
               "retained_rows_hash": digest(retained), "requested_criterion_ids": requested,
               "evidence_ids": sorted(refs), "review_reason": reason.strip(), "diagnostics": diagnostics,
               "brand_binding": brand_binding(state, "concept", concept_id, state["concepts"][concept_id]["description"])}
    if contract_version == FUNCTIONAL_CONTRACT:
        from .functional_types import functional_role_context
        binding["functional_proof"] = deepcopy(functional_role_context(state, concept_id, role="E")["proof"])
        binding["functional_proof_hash"] = digest(binding["functional_proof"])
    if attribution is not None:
        if contract_version != DESCRIPTION_CONTRACT:
            raise SafetyError("FIT_DESCRIPTION_ATTRIBUTION_INVALID")
        binding["description_review_attribution"] = deepcopy(attribution)
    return {"kind": "fit_review_base", "source": "fit", "route": "saved_public_interpretation",
            "reference": key("fit-review-base", binding), "at": at, "state": "prepared", "binding": binding}


def prepare_fit_review(memory, concept_id, *, requested_criterion_ids, reason, as_of=None,
                       contract_version=CONTRACT, attribution=None):
    """Persist exact parent/retained rows before any model call."""
    identifier(concept_id)
    at = as_of or utc_now(); timestamp(at)
    active = list(policy_for(FIT_POLICY_ID)["active_ids"])
    if (contract_version not in CONTRACTS
            or not isinstance(requested_criterion_ids, (list, tuple)) or not requested_criterion_ids
            or any(type(value) is not int for value in requested_criterion_ids)
            or len(set(requested_criterion_ids)) != len(requested_criterion_ids)
            or not set(requested_criterion_ids) <= set(active)
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 4096):
        raise SafetyError("FIT_REVIEW_REQUEST_INVALID")
    requested = [number for number in active if number in requested_criterion_ids]
    state = memory.snapshot()
    _preparation_contract(state, concept_id, contract_version)
    item = _facts(state, concept_id, contract_version=contract_version)
    parent = state["assessments"].get(concept_id)
    if contract_version == FUNCTIONAL_CONTRACT:
        affected = functional_changed_criteria(parent, item)
        if not set(affected) <= set(requested):
            raise SafetyError("FIT_REVIEW_AFFECTED_CRITERIA_REQUIRED")
    if (parent and parent.get("input_hash") != digest(item)
            and not (contract_version in (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT) and requested == active)
            and contract_version != FUNCTIONAL_CONTRACT):
        raise SafetyError("FIT_REVIEW_PARENT_FACTS_CHANGED")
    base = _review_base(state, concept_id, item, parent, requested, reason, at,
                        contract_version=contract_version, attribution=attribution)
    review_id, binding = base["reference"], base["binding"]
    prior = state["source_checks"].get(review_id)
    if prior:
        if prior.get("binding") != binding:
            raise SafetyError("FIT_REVIEW_BASE_CONFLICT")
        return deepcopy(prior)
    memory.commit(review_id, [change("source_checks", review_id, base)],
                  validate=lambda current: _current_base(current, base))
    return deepcopy(base)


def _current_description_base(state, concept_id, item, parent):
    """One automatic full v3 review per exact current frozen input owner."""
    expected_brand = brand_binding(state, "concept", concept_id, item["description"])
    found = [row for row in state["source_checks"].values()
             if row.get("kind") == "fit_review_base"
             and row.get("binding", {}).get("contract_version") == DESCRIPTION_CONTRACT
             and row["binding"].get("concept_id") == concept_id
             and row["binding"].get("requested_criterion_ids") == list(policy_for(FIT_POLICY_ID)["active_ids"])
             and row["binding"].get("input_hash") == digest(item)
             and row["binding"].get("parent_assessment_hash") == (digest(parent) if parent else None)
             and row["binding"].get("brand_binding") == expected_brand]
    if len(found) > 1:
        raise SafetyError("FIT_DESCRIPTION_REVIEW_CONFLICT")
    if found:
        _current_base(state, found[0])
        return deepcopy(found[0])
    return None


def saved_description_review_attribution(request, saved_result):
    """Name an exhausted v2 repair without inventing model-selected fields.

    This pure result preserves the raw answer through hashes and produces an
    explicitly unknown projection, not awarded description-based points. A
    separate v3 review can then ask the model to choose its actual fields once.
    It does not read files, call a runner or update research memory.
    """
    from .research_ai import validate_results
    if (not isinstance(request, dict) or request.get("role") != "E"
            or request.get("prompt_version") != PRACTICAL_CONTRACT
            or not isinstance(saved_result, dict) or saved_result.get("request_hash") != digest(request)):
        raise SafetyError("FIT_DESCRIPTION_ATTRIBUTION_INVALID")
    payload = request.get("payload", {})
    if not isinstance(payload, dict) or not isinstance(saved_result.get("response"), dict):
        raise SafetyError("FIT_DESCRIPTION_ATTRIBUTION_INVALID")
    repair = payload.get("checklist_repair", {})
    items = payload.get("items", [])
    if (not isinstance(repair, dict) or repair.get("scope") != "one-practical-fit-contract-repair-v1"
            or repair.get("failure_code") != "AI_UNSUPPORTED_MEETS"
            or not isinstance(items, list) or len(items) != 1):
        raise SafetyError("FIT_DESCRIPTION_ATTRIBUTION_INVALID")
    item = items[0]
    if not isinstance(item, dict) or not {"id", "description", "observations"} <= set(item):
        raise SafetyError("FIT_DESCRIPTION_ATTRIBUTION_INVALID")
    input_item = {field: deepcopy(item[field]) for field in ("id", "description", "observations")}
    if repair.get("input_hash") != digest(input_item):
        raise SafetyError("FIT_DESCRIPTION_ATTRIBUTION_INVALID")
    checked = validate_results("E", payload, saved_result["response"], declared_version=PRACTICAL_CONTRACT)
    if checked["failures"] != {item["id"]: "AI_UNSUPPORTED_MEETS"}:
        raise SafetyError("FIT_DESCRIPTION_ATTRIBUTION_NOT_APPLICABLE")
    response = deepcopy(saved_result["response"])
    changed = []
    for row in response["results"][0]["criteria"]:
        if row["state"] == "Meets" and (row["basis"] not in ("observed", "inferred") or not row["references"]):
            changed.append(row["id"])
            row.update(state="Unclear", basis="not_stated", references=[],
                       reason="The proposed benefit has no supporting observation reference. A separate description-grounded review is required; this is unknown, not a product failure.")
    normalised = validate_results("E", payload, response, declared_version=PRACTICAL_CONTRACT)
    if normalised["failures"] or item["id"] not in normalised["accepted"]:
        raise SafetyError("FIT_DESCRIPTION_ATTRIBUTION_NOT_APPLICABLE")
    result = normalised["accepted"][item["id"]]
    return {"kind": "saved_fit_description_review_attribution", "version": "saved-fit-description-attribution-v1",
            "state": "new_contract_review_required", "source_job_id": key("job", request),
            "source_request_hash": digest(request), "source_response_hash": digest(saved_result["response"]),
            "source_contract": PRACTICAL_CONTRACT, "target_contract": DESCRIPTION_CONTRACT,
            "concept_id": item["id"], "input_hash": digest(input_item),
            "description_hash": digest(item["description"]), "criterion_ids": changed,
            "unknown_result": result, "unknown_result_hash": digest(result),
            "diagnostics": ["FIT_DESCRIPTION_REVIEW_REQUIRED:" + str(number) for number in changed] + normalised["diagnostics"]}


def prepare_description_fit_from_saved_repair(memory, request, saved_result, *, as_of=None):
    """Prepare a separately named v3 question; do not execute or promote v2."""
    attribution = saved_description_review_attribution(request, saved_result)
    state = memory.snapshot()
    concept_id = attribution["concept_id"]
    _preparation_contract(state, concept_id, DESCRIPTION_CONTRACT)
    item = _facts(state, concept_id)
    if digest(item) != attribution["input_hash"]:
        raise SafetyError("FIT_REVIEW_BASE_CHANGED")
    from .research_flow import current_assessment_fit
    parent = state["assessments"].get(concept_id)
    if (parent and parent.get("contract_version") in (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT)
            and current_assessment_fit(state, concept_id, expected_policy_id=FIT_POLICY_ID)):
        return {"status": "accepted", "reused": True, "concept_id": concept_id, "review_id": None}
    existing = _current_description_base(state, concept_id, item, parent)
    if existing:
        return {"status": "prepared", "reused": True, "concept_id": concept_id,
                "review_id": existing["reference"], "attribution": attribution}
    base = prepare_fit_review(memory, concept_id,
        requested_criterion_ids=list(policy_for(FIT_POLICY_ID)["active_ids"]),
        reason="description_grounded_review_after_exhausted_v2_repair", as_of=as_of,
        contract_version=DESCRIPTION_CONTRACT, attribution=attribution)
    return {"status": "prepared", "reused": False, "concept_id": concept_id, "review_id": base["reference"],
            "attribution": attribution}


def prepare_practical_fit_reviews(memory, *, concept_ids=None, as_of=None, contract_version=PRACTICAL_CONTRACT):
    """Prepare every current product's full practical review, without calls.

    A completed v2 review is reused only while its facts are unchanged. A
    missing or stale earlier assessment is not a reason to leave a product
    unfinished: a full review binds both the new facts and the old parent.
    """
    from .research_flow import current_assessment_fit
    if contract_version not in (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT):
        raise SafetyError("FIT_REVIEW_REQUEST_INVALID")
    if isinstance(concept_ids, list) and not concept_ids:
        # The caller already found no reviews to prepare. Preserve request
        # validation without rereading the entire saved research for no work.
        at = as_of or utc_now(); timestamp(at)
        return []
    state = memory.snapshot()
    targets = sorted(state["concepts"]) if concept_ids is None else concept_ids
    if (not isinstance(targets, list) or len(set(targets)) != len(targets)
            or any(not isinstance(target, str) or target not in state["concepts"] for target in targets)):
        raise SafetyError("FIT_REVIEW_REQUEST_INVALID")
    at = as_of or utc_now(); timestamp(at)
    outcomes, new_bases = [], []
    for concept_id in targets:
        try:
            _preparation_contract(state, concept_id, contract_version)
            item = _facts(state, concept_id, contract_version=contract_version)
            parent = state["assessments"].get(concept_id)
            if (parent and parent.get("contract_version") in ((FUNCTIONAL_CONTRACT,) if contract_version == FUNCTIONAL_CONTRACT
                    else (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT))
                    and current_assessment_fit(state, concept_id, expected_policy_id=FIT_POLICY_ID)):
                outcomes.append({"concept_id": concept_id, "status": "accepted", "review_id": None,
                                 "input_hash": digest(item), "reused": True})
                continue
            requested = (functional_changed_criteria(parent, item) if contract_version == FUNCTIONAL_CONTRACT
                         else list(policy_for(FIT_POLICY_ID)["active_ids"]))
            # A corrupt saved current answer is not repaired by retaining it.
            if not requested:
                requested = list(policy_for(FIT_POLICY_ID)["active_ids"])
            base = (_current_description_base(state, concept_id, item, parent)
                    if contract_version == DESCRIPTION_CONTRACT else None)
            if base is None:
                base = _review_base(state, concept_id, item, parent,
                    requested,
                    "functional_type_estimates_v4_review" if contract_version == FUNCTIONAL_CONTRACT
                    else "full_practical_description_estimates_v3_review" if contract_version == DESCRIPTION_CONTRACT
                    else "full_practical_estimates_v2_review", at, contract_version=contract_version)
            previous = state["source_checks"].get(base["reference"])
            if previous:
                if previous.get("binding") != base["binding"]:
                    raise SafetyError("FIT_REVIEW_BASE_CONFLICT")
            else:
                new_bases.append(base)
            outcomes.append({"concept_id": concept_id, "status": "prepared", "review_id": base["reference"],
                             "input_hash": digest(item), "reused": False})
        except SafetyError as error:
            outcomes.append({"concept_id": concept_id, "status": "not_reviewable", "review_id": None,
                             "reason": str(error), "reused": False})
    # Freeze small groups with one short commit, not several whole-memory
    # snapshots per product. No financial lock spans a model or source call.
    for offset in range(0, len(new_bases), 10):
        chunk = new_bases[offset:offset + 10]
        def unchanged(current, bases=chunk):
            for base in bases:
                _current_base(current, base)
        try:
            memory.commit(key("practical-fit-bases", [base["reference"] for base in chunk]),
                          [change("source_checks", base["reference"], base) for base in chunk], validate=unchanged)
        except SafetyError as error:
            if str(error) not in ("FIT_REVIEW_BASE_CHANGED", "RESEARCH_DEPENDENCY_CHANGED"):
                raise
            # A concurrent fact change affects that product's preparation,
            # not all the healthy members of this small group.
            for base in chunk:
                concept_id = base["binding"]["concept_id"]
                outcome = next(row for row in outcomes if row["concept_id"] == concept_id)
                try:
                    requested = list(policy_for(FIT_POLICY_ID)["active_ids"])
                    if contract_version == FUNCTIONAL_CONTRACT:
                        latest = memory.snapshot()
                        fresh_item = _facts(latest, concept_id, contract_version=contract_version)
                        requested = functional_changed_criteria(latest["assessments"].get(concept_id), fresh_item) or requested
                    fresh = prepare_fit_review(memory, concept_id,
                        requested_criterion_ids=requested,
                        reason="functional_type_estimates_v4_review" if contract_version == FUNCTIONAL_CONTRACT
                        else "full_practical_description_estimates_v3_review" if contract_version == DESCRIPTION_CONTRACT
                        else "full_practical_estimates_v2_review", as_of=at,
                        contract_version=contract_version)
                    outcome.update(review_id=fresh["reference"], input_hash=fresh["binding"]["input_hash"])
                except SafetyError as failure:
                    outcome.update(status="not_reviewable", review_id=None, reason=str(failure))
    return outcomes


def project_saved_fit(memory, concept_id, *, reason="legacy_fit_policy_migration", as_of=None):
    """Append ten-row interpretation; original accepted answers remain history.

    Retired rows disappear from the current score. IDs7/13 await their changed
    practical review. Unknown negatives receive no point and are not failures.
    """
    identifier(concept_id)
    at = as_of or utc_now(); timestamp(at)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4096:
        raise SafetyError("FIT_REVIEW_REQUEST_INVALID")
    state = memory.snapshot()
    _preparation_contract(state, concept_id, CONTRACT)
    item = _facts(state, concept_id)
    parent = state["assessments"].get(concept_id)
    if not parent:
        base = prepare_fit_review(memory, concept_id,
            requested_criterion_ids=list(policy_for(FIT_POLICY_ID)["active_ids"]), reason=reason, as_of=at)
        return {"status": "assessment_pending", "applied": False, "review_id": base["reference"]}
    if parent.get("input_hash") != digest(item):
        return {"status": "saved_facts_changed", "applied": False, "review_id": None}
    if parent.get("fit_policy_id", LEGACY_POLICY_ID) == FIT_POLICY_ID:
        if parent.get("contract_version") not in CONTRACTS:
            base = prepare_fit_review(memory, concept_id,
                requested_criterion_ids=list(policy_for(FIT_POLICY_ID)["active_ids"]),
                reason="current_fit_contract_review_required", as_of=at)
            return {"status": "assessment_pending", "applied": False, "review_id": base["reference"]}
        evaluate_fit(parent["result"]["criteria"], FIT_POLICY_ID,
                     **_description_kwargs(parent.get("contract_version"), item["description"]))
        pending = [record_id for record_id, row in state["source_checks"].items()
                   if row.get("kind") == "fit_review_base" and row["binding"]["concept_id"] == concept_id
                   and row["binding"]["parent_assessment_hash"] == digest(parent)]
        projection = parent.get("projection", {})
        receipt = state["source_checks"].get(projection.get("id"), {})
        if (not pending and projection.get("pending_criterion_ids") == [7, 13]
                and receipt.get("kind") == "fit_projection_applied"
                and receipt.get("assessment_hash") == digest(parent)):
            # Recover the pre-atomic implementation's possible gap without
            # changing the projection, restoring an old answer or dispatching.
            base = prepare_fit_review(memory, concept_id, requested_criterion_ids=[7, 13],
                                      reason=projection["reason"], as_of=at)
            return {"status": "review_base_recovered", "applied": False, "review_id": base["reference"]}
        return {"status": "already_current", "applied": False, "review_id": pending[0] if pending else None}
    if parent.get("fit_policy_id", LEGACY_POLICY_ID) != LEGACY_POLICY_ID:
        raise SafetyError("FIT_REVIEW_POLICY_INVALID")
    rows, diagnostics = _retained_rows(parent, {row["id"] for row in item["observations"]},
                                       review_ids=(7, 13), description=item["description"])
    projection_id = key("fit-projection", [concept_id, digest(parent), digest(item), FIT_POLICY_ID])
    if projection_id in state["source_checks"]:
        return {"status": "already_applied", "applied": False, "projection_id": projection_id, "review_id": None}
    result = dict(deepcopy(parent["result"]), criteria=rows,
                  audience_positioning={"positioning": "unclear", "reason": "The changed audience meaning awaits its named practical review."})
    value = {"result": result, "input_hash": digest(item), "job_id": parent.get("job_id"),
             "fit_policy_id": FIT_POLICY_ID, "contract_version": CONTRACT,
             "used_record_ids": [row["id"] for row in item["observations"]],
             "diagnostics": diagnostics, "projection": {"id": projection_id,
             "parent_assessment_hash": digest(parent), "previous_policy_id": LEGACY_POLICY_ID,
             "reason": reason, "pending_criterion_ids": [7, 13]}}
    base = _review_base(state, concept_id, item, value, [7, 13], reason, at)
    receipt = {"kind": "fit_projection_applied", "source": "fit", "route": "saved_row_projection",
               "reference": projection_id, "at": at, "state": "applied", "concept_id": concept_id,
               "parent_hash": digest(parent), "assessment_hash": digest(value), "review_id": base["reference"],
               "fit": evaluate_fit(rows, FIT_POLICY_ID)}
    def unchanged(current):
        if (_facts(current, concept_id) != item or current["assessments"].get(concept_id) != parent
                or brand_binding(current, "concept", concept_id, item["description"]) != brand_binding(state, "concept", concept_id, item["description"])):
            raise SafetyError("FIT_REVIEW_BASE_CHANGED")
    applied = memory.commit(projection_id, [change("assessments", concept_id, value, parent),
                                            change("source_checks", projection_id, receipt),
                                            change("source_checks", base["reference"], base)], validate=unchanged)
    return {"status": "projected", "applied": applied, "projection_id": projection_id,
            "review_id": base["reference"], "fit": receipt["fit"]}


def _current_base(state, base):
    binding = base["binding"]
    if (binding["fit_policy_id"] != FIT_POLICY_ID or binding["contract_version"] not in CONTRACTS
            or digest(binding["retained_rows"]) != binding["retained_rows_hash"]
            or key("fit-review-base", binding) != base["reference"]):
        raise SafetyError("FIT_REVIEW_BASE_CONFLICT")
    item = _facts(state, binding["concept_id"], contract_version=binding["contract_version"])
    parent = state["assessments"].get(binding["concept_id"])
    if (digest(item) != binding["input_hash"] or item != binding["input"]
            or (digest(parent) if parent else None) != binding["parent_assessment_hash"]
            or brand_binding(state, "concept", binding["concept_id"], state["concepts"][binding["concept_id"]]["description"]) != binding["brand_binding"]):
        raise SafetyError("FIT_REVIEW_BASE_CHANGED")
    if binding["contract_version"] == FUNCTIONAL_CONTRACT:
        if digest(binding.get("functional_proof")) != binding.get("functional_proof_hash"):
            raise SafetyError("FIT_REVIEW_BASE_CONFLICT")
        if not set(functional_changed_criteria(parent, item)) <= set(binding["requested_criterion_ids"]):
            raise SafetyError("FIT_REVIEW_AFFECTED_CRITERIA_REQUIRED")
    attribution = binding.get("description_review_attribution")
    if attribution is not None:
        if not isinstance(attribution, dict):
            raise SafetyError("FIT_DESCRIPTION_ATTRIBUTION_INVALID")
        source = state["jobs"].get(attribution.get("source_job_id"), {})
        if (binding["contract_version"] != DESCRIPTION_CONTRACT
                or attribution.get("kind") != "saved_fit_description_review_attribution"
                or attribution.get("version") != "saved-fit-description-attribution-v1"
                or attribution.get("state") != "new_contract_review_required"
                or attribution.get("source_contract") != PRACTICAL_CONTRACT
                or attribution.get("target_contract") != DESCRIPTION_CONTRACT
                or attribution.get("concept_id") != binding["concept_id"]
                or attribution.get("input_hash") != binding["input_hash"]
                or attribution.get("description_hash") != digest(item["description"])
                or digest(source.get("request")) != attribution.get("source_request_hash")
                or attribution.get("unknown_result_hash") != digest(attribution.get("unknown_result"))):
            raise SafetyError("FIT_DESCRIPTION_ATTRIBUTION_INVALID")


def _payload(base):
    from .research_ai import CRITERIA
    from .research_flow import FIT_CHECKLIST_RULES, FIT_CHECKLIST_SCOPE
    binding = base["binding"]
    payload = {"mode": binding["mode"], "fit_policy_id": FIT_POLICY_ID,
            "requested_criterion_ids": binding["requested_criterion_ids"],
            "criteria": [{"id": number, "name": CRITERIA[number - 1]} for number in binding["requested_criterion_ids"]],
            "checklist_scope": FIT_CHECKLIST_SCOPE, "checklist_rules": FIT_CHECKLIST_RULES,
            "items": [dict(deepcopy(binding["input"]), fit_review_base_id=base["reference"],
                       named_review_reason=binding["review_reason"],
                       earlier_judgments=deepcopy((binding["parent_assessment"] or {}).get("result", {}).get("criteria", [])))]}
    if binding["contract_version"] in (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT):
        payload["judgment_style"] = (FUNCTIONAL_STYLE if binding["contract_version"] == FUNCTIONAL_CONTRACT
            else DESCRIPTION_STYLE if binding["contract_version"] == DESCRIPTION_CONTRACT else "practical-estimates-v2")
        # Prior judgments stay in the immutable base for audit and concurrency,
        # not in the fresh practical question where they could anchor a score.
        model_item = payload["items"][0]
        model_item.pop("earlier_judgments")
        model_item["reference_aliases"] = {"R" + str(index + 1): row["id"]
                                           for index, row in enumerate(model_item["observations"])}
        if binding["contract_version"] == DESCRIPTION_CONTRACT:
            model_item["description_inference_context"] = description_inference_context(model_item["description"])
        elif binding["contract_version"] == FUNCTIONAL_CONTRACT:
            model_item.pop("fit_review_base_id")
            model_item.pop("named_review_reason")
            model_item["description_inference_context"] = functional_inference_context(model_item["description"])
            if binding["mode"] == "fit_partial_review":
                model_item["earlier_requested_reasons"] = [
                    {"id": row["id"], "reason": row["reason"]}
                    for row in (binding["parent_assessment"] or {}).get("result", {}).get("criteria", [])
                    if row["id"] in binding["requested_criterion_ids"]]
    return payload


def apply_fit_review(memory, review_id, answer, *, job_id, diagnostics=()):
    """Apply a validated answer once, or retain a precise stale-base receipt."""
    application_id = key("fit-review-apply", review_id)
    state = memory.snapshot()
    base = state["source_checks"].get(review_id)
    if not base or base.get("kind") != "fit_review_base":
        raise SafetyError("FIT_REVIEW_BASE_REQUIRED")
    binding = base["binding"]
    from .research_ai import validate_results
    checked = validate_results("E", _payload(base), {"results": [answer]},
                               declared_version=binding["contract_version"])
    if checked["failures"] or binding["concept_id"] not in checked["accepted"]:
        raise SafetyError(checked["failures"].get(binding["concept_id"], "FIT_REVIEW_ANSWER_BINDING_INVALID"))
    answer = checked["accepted"][binding["concept_id"]]
    prior = state["source_checks"].get(application_id)
    if prior:
        if prior.get("answer_hash") != digest(answer) or prior.get("job_id") != job_id:
            raise SafetyError("FIT_REVIEW_APPLICATION_CONFLICT")
        return {"review_id": review_id, "status": "accepted", "applied": False, "job_id": job_id}
    _current_base(state, base)
    expected = binding["requested_criterion_ids"]
    if answer.get("id") != binding["concept_id"] or [row["id"] for row in answer.get("criteria", [])] != expected:
        raise SafetyError("FIT_REVIEW_ANSWER_BINDING_INVALID")
    all_rows = {row["id"]: deepcopy(row) for row in binding["retained_rows"] + answer["criteria"]}
    criteria = [all_rows[number] for number in policy_for(FIT_POLICY_ID)["active_ids"]]
    fit = evaluate_fit(criteria, FIT_POLICY_ID,
                       **_description_kwargs(binding["contract_version"], binding["input"]["description"]))
    parent = binding["parent_assessment"]
    if parent and binding["mode"] == "fit_partial_review":
        # A changed criterion is not another marketplace/compliance review.
        # The original valid meaning of these unchanged facts remains intact.
        result = dict(deepcopy(parent["result"]), criteria=criteria)
        result["audience_positioning"] = deepcopy(answer["audience_positioning"] if 13 in expected else
                                                 parent["result"].get("audience_positioning", answer["audience_positioning"]))
    else:
        result = dict(deepcopy(answer), criteria=criteria)
    value = {"result": result, "input_hash": binding["input_hash"], "job_id": job_id,
             "used_record_ids": binding["evidence_ids"], "fit_policy_id": FIT_POLICY_ID,
             "contract_version": binding["contract_version"], "diagnostics": list(diagnostics),
             "fit_review": {"review_id": review_id, "parent_assessment_hash": binding["parent_assessment_hash"],
                            "retained_rows_hash": binding["retained_rows_hash"], "requested_criterion_ids": expected}}
    receipt = {"kind": "fit_review_applied", "source": "fit", "route": "saved_public_interpretation",
               "reference": application_id, "at": utc_now(), "state": "applied", "review_id": review_id,
               "concept_id": binding["concept_id"], "answer_hash": digest(answer), "job_id": job_id,
               "assessment_hash": digest(value), "fit": fit}
    if binding["contract_version"] in (DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT):
        from .fit_description import FIT_ESTIMATE_VALIDATION_VERSION
        receipt["output_validation_version"] = FIT_ESTIMATE_VALIDATION_VERSION
    if binding["contract_version"] == DESCRIPTION_CONTRACT:
        value["fit_description_contract"] = DESCRIPTION_INFERENCE_KIND
        receipt["fit_description_contract"] = DESCRIPTION_INFERENCE_KIND
        receipt["assessment_hash"] = digest(value)
    elif binding["contract_version"] == FUNCTIONAL_CONTRACT:
        value.update(fit_description_contract=FUNCTIONAL_INFERENCE_KIND,
                     meaning_hash=binding["input"]["meaning_hash"], input=deepcopy(binding["input"]),
                     functional_proof=deepcopy(binding["functional_proof"]),
                     functional_proof_hash=binding["functional_proof_hash"])
        value["record_hash"] = digest(value)
        receipt["fit_description_contract"] = FUNCTIONAL_INFERENCE_KIND
        receipt["assessment_hash"] = digest(value)
    if binding.get("description_review_attribution") is not None:
        value["fit_review"]["description_review_attribution"] = deepcopy(binding["description_review_attribution"])
        receipt["description_review_attribution_hash"] = digest(binding["description_review_attribution"])
        if binding["contract_version"] == FUNCTIONAL_CONTRACT:
            value["record_hash"] = digest({field: entry for field, entry in value.items() if field != "record_hash"})
        receipt["assessment_hash"] = digest(value)
    applied = memory.commit(application_id, [change("assessments", binding["concept_id"], value, parent),
                                            change("source_checks", application_id, receipt)],
                            validate=lambda current: _current_base(current, base))
    return {"review_id": review_id, "status": "accepted", "applied": applied, "job_id": job_id}


def _bound_jobs(state, base, payload):
    """Keep one exact saved request variant for this frozen review item."""
    from .research_ai import MODEL
    from .fit_description import FUNCTIONAL_RESPONSE_VERSION
    context = {field: value for field, value in payload.items() if field != "items"}
    contract_version = base["binding"]["contract_version"]
    found = []
    for job_id, job in state["jobs"].items():
        request = job.get("request", {})
        if not isinstance(request, dict) or request.get("role") != "E" or not isinstance(request.get("payload"), dict):
            continue
        if contract_version == FUNCTIONAL_CONTRACT and "checklist_repair" in request["payload"]:
            continue
        items = request["payload"].get("items", [])
        if not isinstance(items, list):
            continue
        matches = [item for item in items if isinstance(item, dict) and (
            item == payload["items"][0] if contract_version == FUNCTIONAL_CONTRACT
            else item.get("fit_review_base_id") == base["reference"])]
        if not matches:
            continue
        actual_context = {field: value for field, value in request["payload"].items() if field != "items"}
        comparable_context = dict(actual_context)
        comparable_expected = dict(context)
        if contract_version == FUNCTIONAL_CONTRACT:
            attribution = comparable_context.pop("response_attribution", None)
            comparable_expected.pop("response_attribution", None)
            if attribution not in (None, FUNCTIONAL_RESPONSE_VERSION):
                raise SafetyError("FIT_REVIEW_JOB_CONFLICT")
        if (len(matches) != 1 or matches[0] != payload["items"][0]
                or comparable_context != comparable_expected
                or request.get("model") != MODEL or request.get("prompt_version") != contract_version
                or not isinstance(request.get("runner"), str) or key("job", request) != job_id
                or job.get("binding_keys", {}).get(matches[0]["id"]) != digest(["E", matches[0], actual_context, MODEL, contract_version, request["runner"]])):
            raise SafetyError("FIT_REVIEW_JOB_CONFLICT")
        found.append((job_id, request["runner"]))
    if len(found) > 1:
        raise SafetyError("FIT_REVIEW_JOB_CONFLICT")
    return found


def fit_review_repair_jobs(state, base, *, original_job_ids=None):
    """Find actual original-linked repair owners, never presentation-only IDs."""
    if original_job_ids is None:
        original_job_ids = [job_id for job_id, _ in _bound_jobs(state, base, _payload(base))]
    original_job_ids = set(original_job_ids)
    functional = base["binding"]["contract_version"] == FUNCTIONAL_CONTRACT
    found = []
    for job_id, job in state["jobs"].items():
        request = job.get("request", {})
        repair = request.get("payload", {}).get("checklist_repair", {})
        if request.get("role") != "E" or not isinstance(repair, dict):
            continue
        if functional:
            matches = (repair.get("scope") == "one-functional-fit-contract-repair-v1"
                and repair.get("original_job_id") in original_job_ids
                and repair.get("input_hash") == base["binding"]["input_hash"])
        else:
            matches = repair.get("review_id") == base["reference"]
        if matches:
            if key("job", request) != job_id or request.get("prompt_version") != base["binding"]["contract_version"]:
                raise SafetyError("FIT_REVIEW_JOB_CONFLICT")
            found.append(job_id)
    if len(found) > 1:
        raise SafetyError("FIT_REVIEW_JOB_CONFLICT")
    return found


def run_fit_reviews(memory, runner, review_ids, *, limit=10, repair_rejected=True):
    """Advance compatible bounded questions; failures never stop siblings."""
    from .research_ai import Jobs, MAX_JOB_BYTES
    from .model_recovery import run_current_model_job
    from .research_flow import problem
    def record_problem(*args, **kwargs):
        try:
            problem(memory, *args, **kwargs)
        except SafetyError as error:
            # The immutable input/model response and returned per-item outcome
            # still describe this failure. A busy diagnostic writer must not
            # abort healthy siblings; the next pass records it again. Integrity
            # and binding failures are not softened or silently discarded.
            if str(error) not in {"RUNNER_ALREADY_ACTIVE", "SAVED_FILE_UNAVAILABLE"}:
                raise
    if (not isinstance(review_ids, list) or any(not isinstance(value, str) for value in review_ids)
            or len(review_ids) != len(set(review_ids))
            or type(limit) is not int or not 1 <= limit <= 50):
        raise SafetyError("FIT_REVIEW_BATCH_INVALID")
    state = memory.snapshot()
    pending, outcomes, seen_concepts = {}, [], set()
    for review_id in review_ids[:limit]:
        identifier(review_id)
        applied = state["source_checks"].get(key("fit-review-apply", review_id))
        if applied:
            outcomes.append({"review_id": review_id, "status": "accepted", "applied": False, "job_id": applied["job_id"]})
            continue
        base = state["source_checks"].get(review_id)
        try:
            if not base or base.get("kind") != "fit_review_base":
                raise SafetyError("FIT_REVIEW_BASE_REQUIRED")
            _current_base(state, base)
            if base["binding"]["concept_id"] in seen_concepts:
                outcomes.append({"review_id": review_id, "status": "deferred", "applied": False,
                                 "reason": "Another named review of this same current base is already selected."})
                continue
            seen_concepts.add(base["binding"]["concept_id"])
            payload = _payload(base)
            if base["binding"]["contract_version"] == FUNCTIONAL_CONTRACT and len(canonical(payload)) > MAX_JOB_BYTES:
                raise SafetyError("FIT_FUNCTIONAL_INPUT_SIZE_LIMIT")
            saved_jobs = _bound_jobs(state, base, payload)
            if saved_jobs:
                # Preserve the exact original response format, including old
                # hash-bearing questions. Never relabel their returned links.
                original = state["jobs"][saved_jobs[0][0]]["request"]["payload"]
                payload = dict(deepcopy(original), items=deepcopy(payload["items"]))
            elif base["binding"]["contract_version"] == FUNCTIONAL_CONTRACT:
                from .fit_description import FUNCTIONAL_RESPONSE_VERSION
                payload["response_attribution"] = FUNCTIONAL_RESPONSE_VERSION
            if runner is None and not saved_jobs:
                outcomes.append({"review_id": review_id, "status": "pending", "applied": False,
                                 "reason": "AI_RUNNER_NOT_ENABLED"})
                record_problem("fit_review", base["binding"]["concept_id"], "AI_RUNNER_NOT_ENABLED", revision=review_id)
                continue
            runner_id = saved_jobs[0][1] if saved_jobs else getattr(runner, "identity", "codex-chatgpt-high-v1")
            group = digest([{k: v for k, v in payload.items() if k != "items"}, runner_id])
            pending.setdefault(group, []).append((base, payload, runner_id))
        except SafetyError as error:
            outcomes.append({"review_id": review_id, "status": "needs_reconciliation", "applied": False, "reason": str(error)})
            record_problem("fit_review", base["binding"]["concept_id"] if base else review_id,
                    str(error), revision=review_id)
    for grouped in pending.values():
        chunks = []
        for member in grouped:
            if not chunks or len(chunks[-1]) == 5:
                chunks.append([])
            chunk = chunks[-1]
            if chunk and member[0]["binding"]["contract_version"] == FUNCTIONAL_CONTRACT:
                combined = dict(member[1], items=[item for _, data, _ in chunk + [member] for item in data["items"]])
                if len(canonical(combined)) > MAX_JOB_BYTES:
                    chunk = []; chunks.append(chunk)
            chunk.append(member)
        for chunk in chunks:
            payload = dict(chunk[0][1], items=[item for _, data, _ in chunk for item in data["items"]])
            selected_runner = runner
            if chunk[0][2] != getattr(runner, "identity", "codex-chatgpt-high-v1"):
                selected_runner = _SavedReviewRunner(chunk[0][2])
            try:
                job_id, checked = run_current_model_job(memory, selected_runner, "E", payload,
                    declared_version=chunk[0][0]["binding"]["contract_version"],
                    current_items=[item for _, data, _ in grouped for item in data["items"]])
            except SafetyError as error:
                for base, _, _ in chunk:
                    outcomes.append({"review_id": base["reference"], "status": "pending", "applied": False, "reason": str(error)})
                    record_problem("fit_review", base["binding"]["concept_id"], str(error), revision=base["reference"])
                continue
            for base, data, _ in chunk:
                concept_id = base["binding"]["concept_id"]
                source_job_id = job_id
                try:
                    # Batch reshaping may return a derived result. The applied
                    # receipt belongs to the actual original saved model job.
                    saved_jobs = _bound_jobs(memory.snapshot(), base, data)
                    if not saved_jobs:
                        # A reshaped batch may recover saved siblings while a
                        # new item never gains admission. Keep that original
                        # wait, not a fabricated missing-answer/job diagnosis.
                        reason = checked["failures"].get(concept_id)
                        if reason is not None:
                            outcomes.append({"review_id": base["reference"], "status": "pending",
                                             "applied": False, "reason": reason})
                            record_problem("fit_review", concept_id, reason, revision=base["reference"])
                            continue
                        raise SafetyError("FIT_REVIEW_JOB_REQUIRED")
                    source_job_id = saved_jobs[0][0]
                    answer = checked["accepted"].get(concept_id)
                    if answer is None:
                        code = checked["failures"][concept_id]
                        record_problem("fit_review", concept_id, code, job_id=source_job_id, revision=base["reference"])
                        if repair_rejected and base["binding"]["contract_version"] in (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT):
                            if code in _repairable_output_failures(base["binding"]["contract_version"]):
                                outcomes.append(repair_practical_fit_review(memory, runner, base["reference"]))
                                continue
                        outcomes.append({"review_id": base["reference"], "status": "rejected", "applied": False, "job_id": source_job_id, "reason": code})
                        continue
                    outcomes.append(apply_fit_review(memory, base["reference"], answer,
                                                     job_id=source_job_id, diagnostics=checked["diagnostics"]))
                except SafetyError as error:
                    outcomes.append({"review_id": base["reference"], "status": "needs_reconciliation", "applied": False, "job_id": source_job_id, "reason": str(error)})
                    record_problem("fit_review", concept_id, str(error), job_id=source_job_id, revision=base["reference"])
    return outcomes


def repair_practical_fit_review(memory, runner, review_id):
    """Repair one completed practical shape/reference rejection, never seek a score.

    Reuses the existing checklist-repair job owner. Accepted siblings, original
    responses and old packets stay unchanged. An unchanged failed repair is
    reused; there is no repeat-until-passing cycle or uncertain-job relaunch.
    """
    from .checklist_repair import _run_repair
    from .research_ai import MAX_JOB_BYTES, current_attempt, validate_results
    from .research_flow import problem
    from .storage import load_json, safe_path
    identifier(review_id)
    state = memory.snapshot()
    base = state["source_checks"].get(review_id)
    if not base or base.get("kind") != "fit_review_base" or base["binding"]["contract_version"] not in (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT):
        raise SafetyError("PRACTICAL_FIT_REPAIR_BASE_REQUIRED")
    application = state["source_checks"].get(key("fit-review-apply", review_id))
    if application:
        return {"review_id": review_id, "status": "accepted", "applied": False,
                "job_id": application["job_id"], "repair_reused": True}
    _current_base(state, base)
    original_payload = _payload(base)
    original_jobs = _bound_jobs(state, base, original_payload)
    if len(original_jobs) != 1:
        raise SafetyError("PRACTICAL_FIT_REPAIR_ORIGINAL_REQUIRED")
    original_job_id = original_jobs[0][0]
    request = state["jobs"][original_job_id]["request"]
    folder = current_attempt(safe_path(memory.root, "ai_jobs", original_job_id))
    saved = load_json(folder / "result.json", MAX_JOB_BYTES)
    if saved.get("request_hash") != digest(request):
        raise SafetyError("AI_JOB_RESULT_CONFLICT")
    contract_version = base["binding"]["contract_version"]
    original_checked = validate_results("E", request["payload"], saved["response"], declared_version=contract_version)
    concept_id = base["binding"]["concept_id"]
    failure = original_checked["failures"].get(concept_id)
    if failure not in _repairable_output_failures(contract_version):
        raise SafetyError("PRACTICAL_FIT_REPAIR_QUALITY_FAILURE_REQUIRED")
    repair = {"scope": "one-practical-fit-contract-repair-v1", "review_id": review_id,
              "original_job_id": original_job_id, "failure_code": failure,
              "input_hash": base["binding"]["input_hash"],
              "rules": "Correct only the rejected output shape or supplied-reference use. Assess these same facts once; do not target a higher score or a recommendation."}
    payload = dict(deepcopy(request["payload"]), items=deepcopy(original_payload["items"]))
    item = payload["items"][0]
    # Original fit-review bindings keep their own one saved job. This named
    # repair has an explicit separate request identity and original-job link.
    item.pop("fit_review_base_id", None)
    if contract_version == FUNCTIONAL_CONTRACT:
        repair.pop("review_id")
        repair["scope"] = "one-functional-fit-contract-repair-v1"
    else:
        item["fit_review_repair_id"] = review_id
        item["named_review_reason"] = (
            "Correct the earlier output validation error " + failure +
            ". Use only the supplied R aliases and exactly the requested original criterion IDs. "
            "Make the same practical judgments from these facts; do not try to increase the score.")
    payload["checklist_repair"] = repair
    job_id, result = _run_repair(memory, runner, payload, state, contract_version)
    _current_base(memory.snapshot(), base)
    if concept_id not in result["accepted"]:
        reason = result["failures"][concept_id]
        problem(memory, "fit_review_repair", concept_id, reason, job_id=job_id, revision=review_id)
        return {"review_id": review_id, "status": "rejected", "applied": False,
                "job_id": job_id, "original_job_id": original_job_id, "reason": reason}
    outcome = apply_fit_review(memory, review_id, result["accepted"][concept_id],
                               job_id=job_id, diagnostics=result["diagnostics"])
    return dict(outcome, original_job_id=original_job_id, repair_reused=False)
