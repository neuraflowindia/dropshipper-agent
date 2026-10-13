"""One explicit repair of a saved per-product checklist contract rejection.

This is not a score-improvement loop or an execution retry. The completed
original response, accepted siblings and frozen reports remain untouched.
"""

from copy import deepcopy

from .brand_reviews import brand_binding, current_brand
from .models import SafetyError, digest, identifier
from .research_ai import Jobs, MAX_JOB_BYTES, current_attempt, validate_results
from .model_recovery import run_current_model_job
from .research_flow import CHECKLIST_RULES, CHECKLIST_SCOPE, QUALITY_FAILURES, evidence_indexes, problem
from .research_memory import change, key
from .storage import Journal, load_json, safe_path


REPAIR_SCOPE = "one-item-checklist-contract-repair-v1"
REPAIR_RULES = (
    "Repair only the named item's rejected output contract using its current supplied facts. "
    "Return exactly one result with the supplied concept id. Criterion ids are integers 1 through 13. "
    "Every criterion reference and every relevance id must be copied exactly from observations[].id; "
    "semantic_id, concept id, ad id, seller id and invented ids are not observation references. "
    "Classify all supplied observations. Correct the named validation error; do not target a higher score, "
    "seven matches or a recommendation. Ordinary evidence and checklist rules still apply."
)
LEGACY_CHECKLIST_RULES = {CHECKLIST_SCOPE: CHECKLIST_RULES}


class _SavedRepair:
    """Keep the original runner binding while replaying, never execute again."""
    def __init__(self, identity):
        self.identity = identity

    def __call__(self, role, payload, folder):
        raise SafetyError("AI_JOB_PENDING_RECONCILIATION")


def _run_repair(memory, runner, payload, state, declared_version):
    existing = [job for job in state["jobs"].values()
                if job["request"].get("role") == "E"
                and job["request"]["payload"].get("checklist_repair") == payload["checklist_repair"]]
    if len(existing) > 1 or (existing and existing[0]["request"]["payload"] != payload):
        raise SafetyError("CHECKLIST_REPAIR_JOB_CONFLICT")
    if existing:
        # Changing/omitting the runner cannot start a second repair variant.
        from .work_turns import active_work_turn
        same_live_owner = (runner is not None and getattr(runner, "closed_retry_policy", None) is not None
            and active_work_turn(memory) is not None
            and getattr(runner, "identity", "codex-chatgpt-high-v1") == existing[0]["request"]["runner"])
        if not same_live_owner:
            runner = _SavedRepair(existing[0]["request"]["runner"])
        if existing[0]["request"].get("prompt_version") != declared_version:
            raise SafetyError("CHECKLIST_REPAIR_JOB_CONFLICT")
    elif runner is None:
        raise SafetyError("AI_RUNNER_NOT_ENABLED")
    return run_current_model_job(memory, runner, "E", payload, declared_version=declared_version)


def _item(state, concept_id):
    concept = state["concepts"].get(concept_id)
    if not concept:
        raise SafetyError("CHECKLIST_REPAIR_CONCEPT_REQUIRED")
    if (concept.get("status") in ("excluded", "merged", "retired")
            or current_brand(state, "concept", concept_id, concept["description"]) == "established"):
        raise SafetyError("CHECKLIST_REPAIR_CONCEPT_EXCLUDED")
    # A correction of an already released product is a separate review, not
    # permission to quietly replace a report's accepted facts.
    if any(row.get("status") == "published" and row.get("concept_id") == concept_id
           for row in state["published_history"].values()):
        raise SafetyError("CHECKLIST_REPAIR_RELEASE_REVIEW_REQUIRED")
    if any(row.get("state") in ("uploaded", "published")
           and any(p.get("concept_id") == concept_id for p in row.get("products", []))
           for row in state["issue_reservations"].values()):
        raise SafetyError("CHECKLIST_REPAIR_RELEASE_REVIEW_REQUIRED")
    by_concept, by_semantic = evidence_indexes(state)
    evidence = []
    for semantic_id in sorted(by_concept.get(concept_id, set())):
        observations = by_semantic.get(semantic_id, {})
        if not observations:
            raise SafetyError("CHECKLIST_REPAIR_EVIDENCE_REQUIRED")
        # Match run_flow's stable original reference, not the newest dated copy.
        obs_id, observation = next(iter(observations.items()))
        evidence.append({"id": obs_id, "semantic_id": semantic_id,
                         "source_text": observation["semantic"]})
    if not evidence:
        raise SafetyError("CHECKLIST_REPAIR_EVIDENCE_REQUIRED")
    return {"id": concept_id, "description": deepcopy(concept["description"]),
            "observations": sorted(evidence, key=lambda row: row["id"])}


def _original(memory, state, concept_id, failure_id, job_id, item):
    failure = state["problems"].get(failure_id)
    job = state["jobs"].get(job_id)
    if (not failure or failure.get("stage") != "assessment"
            or failure.get("affected_id") != concept_id or failure.get("job_id") != job_id
            or failure.get("reason") not in QUALITY_FAILURES or not job):
        raise SafetyError("CHECKLIST_REPAIR_FAILURE_BINDING_INVALID")
    if failure.get("revision") != digest(item):
        raise SafetyError("CHECKLIST_REPAIR_INPUT_CHANGED")
    request = job["request"]
    payload = request["payload"]
    matches = [row for row in payload.get("items", []) if row.get("id") == concept_id]
    if (request.get("role") != "E" or key("job", request) != job_id
            or len(matches) != 1 or "checklist_repair" in payload
            or request.get("prompt_version") != "research-abe-v1"
            or payload.get("mode") is not None
            or payload.get("checklist_scope") not in LEGACY_CHECKLIST_RULES
            or payload.get("checklist_rules") != LEGACY_CHECKLIST_RULES[payload["checklist_scope"]]):
        raise SafetyError("CHECKLIST_REPAIR_FAILURE_BINDING_INVALID")
    original_item = matches[0]
    if {k: v for k, v in original_item.items() if k != "earlier_judgments"} != item:
        raise SafetyError("CHECKLIST_REPAIR_INPUT_CHANGED")
    context = {k: v for k, v in payload.items() if k != "items"}
    binding = digest(["E", original_item, context, request["model"],
                      request["prompt_version"], request["runner"]])
    if job["binding_keys"].get(concept_id) != binding:
        raise SafetyError("CHECKLIST_REPAIR_FAILURE_BINDING_INVALID")
    # Read the completed response, never invoke/retry the original model job.
    folder = current_attempt(safe_path(memory.root, "ai_jobs", job_id))
    saved = load_json(folder / "result.json", MAX_JOB_BYTES)
    if saved.get("request_hash") != digest(request):
        raise SafetyError("AI_JOB_RESULT_CONFLICT")
    checked = validate_results("E", payload, saved["response"], declared_version=request["prompt_version"])
    if checked["failures"].get(concept_id) != failure["reason"]:
        raise SafetyError("CHECKLIST_REPAIR_FAILURE_BINDING_INVALID")
    return failure, payload, original_item


def repair_checklist_item(memory, runner=None, *, concept_id, failure_id, job_id):
    """Run/reuse one named E repair; commit only if its exact inputs stay current.

    The same product/failure/job has one repair scope. A rejected repair replays
    its saved rejection; this function never prepares further model attempts.
    """
    for value in (concept_id, failure_id, job_id):
        identifier(value)
    with Journal(memory.root).locked():
        state = memory.snapshot()
        item = _item(state, concept_id)
        failure, original_payload, original_item = _original(
            memory, state, concept_id, failure_id, job_id, item)
        previous = state["assessments"].get(concept_id)
        original_brand = brand_binding(state, "concept", concept_id, item["description"])
        repair = {"scope": REPAIR_SCOPE, "original_job_id": job_id,
                  "failure_id": failure_id, "failure_code": failure["reason"],
                  "input_hash": digest(item), "rules": REPAIR_RULES}
        existing_repair = previous and previous.get("repair") == repair
        if previous and previous.get("input_hash") == digest(item) and not existing_repair:
            raise SafetyError("CHECKLIST_REPAIR_ACCEPTED_RESULT_REUSE_REQUIRED")
        payload = {k: deepcopy(v) for k, v in original_payload.items() if k != "items"}
        payload.update(items=[dict(item, earlier_judgments=deepcopy(original_item.get("earlier_judgments", [])))],
                       checklist_repair=repair)
        repair_job_id, result = _run_repair(memory, runner, payload, state, state["jobs"][job_id]["request"]["prompt_version"])
        def unchanged(current):
            if (_item(current, concept_id) != item or current["concepts"][concept_id] != state["concepts"][concept_id]
                    or current["problems"].get(failure_id) != failure
                    or current["jobs"].get(job_id) != state["jobs"][job_id]
                    or brand_binding(current, "concept", concept_id, item["description"]) != original_brand
                    or current["assessments"].get(concept_id) != previous):
                raise SafetyError("CHECKLIST_REPAIR_DEPENDENCY_CHANGED")
        unchanged(memory.snapshot())
        if concept_id not in result["accepted"]:
            reason = result["failures"][concept_id]
            problem(memory, "assessment", concept_id, reason,
                    job_id=repair_job_id, revision=digest(item))
            return {"status": "rejected", "applied": False, "job_id": repair_job_id,
                    "original_job_id": job_id, "reason": reason}
        value = {"result": result["accepted"][concept_id], "input_hash": digest(item),
                 "job_id": repair_job_id, "used_record_ids": [row["id"] for row in item["observations"]],
                 "diagnostics": result["diagnostics"], "repair": repair}
        applied = False
        if previous != value:
            applied = memory.commit(key("assess-repair", [repair_job_id, concept_id]),
                                    [change("assessments", concept_id, value, previous)], validate=unchanged)
        return {"status": "accepted", "applied": applied, "job_id": repair_job_id,
                "original_job_id": job_id}
