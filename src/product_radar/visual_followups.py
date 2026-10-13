"""One bounded saved-photo follow-up for genuinely text-unclear identities.

No image collection, new source search, publication or general merge/split is
performed. Supplied photographs are checked against saved source ownership;
caller-assigned owner labels alone never establish that relationship.
"""

from copy import deepcopy

from . import research_ai
from .brand_reviews import brand_binding, current_brand
from .discovery import find_possible_matches
from .models import SafetyError, digest, identifier, timestamp, utc_now
from .research_memory import change, key
from .service import CATEGORIES
from .storage import Journal, load_json, safe_path
from .visual_identity import prepare_visual_comparison, validate_visual_payload

VERSION = "saved-visual-followup-v1"


def _association_input(value):
    return {k: v for k, v in value.items() if k != "visual_followup"}


def _child(state, child_id, semantic_id):
    saved = state["descriptions"].get(semantic_id)
    result = saved.get("result") if isinstance(saved, dict) else None
    if not isinstance(result, dict) or result.get("id") != semantic_id:
        raise SafetyError("VISUAL_CURRENT_DESCRIPTION_REQUIRED")
    products = result.get("products", [])
    for index, product in enumerate(products):
        if key("child", [semantic_id, index, digest(result)]) == child_id:
            return product, digest(result), len(products) == 1 and result.get("kind") == "product"
    raise SafetyError("VISUAL_CURRENT_CHILD_REQUIRED")


def _active(state, concept_id, concept):
    return (concept.get("status") not in ("excluded", "retired", "merged")
            and not concept.get("identity_conflicted")
            and concept.get("description", {}).get("category") in CATEGORIES
            and current_brand(state, "concept", concept_id, concept["description"]) != "established")


def _saved_b_answer(memory, state, child_id, association, product):
    job_id = association.get("job_id")
    job = state["jobs"].get(job_id)
    request = job.get("request") if isinstance(job, dict) else None
    if not request or request.get("role") != "B":
        raise SafetyError("VISUAL_TEXT_UNCLEAR_JOB_REQUIRED")
    item = next((row for row in request["payload"]["items"] if row.get("id") == child_id), None)
    if item is None or item.get("description") != product:
        raise SafetyError("VISUAL_TEXT_DESCRIPTION_CHANGED")
    folder = research_ai.current_attempt(safe_path(memory.root, "ai_jobs", identifier(job_id)))
    if not (folder / "result.json").is_file():
        raise SafetyError("VISUAL_TEXT_RESULT_UNAVAILABLE")
    saved = load_json(folder / "result.json", research_ai.MAX_JOB_BYTES)
    if saved.get("request_hash") != digest(request):
        raise SafetyError("VISUAL_TEXT_RESULT_CONFLICT")
    results = research_ai.validate_results("B", request["payload"], saved["response"])
    answer = results["accepted"].get(child_id)
    if not answer or answer["decision"] != "unclear":
        raise SafetyError("VISUAL_GENUINE_TEXT_UNCLEAR_REQUIRED")
    return job_id, digest(request), digest(answer), item["candidates"]


def _visual_candidates(product, active, original, images, child_id):
    """Choose a bounded comparison without losing the original B scope.

    B saved descriptions, not source-association hashes. A fallback therefore
    requires an unchanged saved candidate description; _photo_ownership below
    proves and freezes the actual current source association separately.
    """
    retrieved = find_possible_matches(product, active, limit=max(3, len(active)))
    current = {row["id"]: row for row in retrieved["candidates"]}
    original = {row["id"]: row for row in original}
    retained = {i: {"id": i, "description": active[i]["description"],
                    "retrieval_score": 0, "retrieval_reasons": ["original_b_photo"]}
                for i, row in original.items()
                if i in active and row["description"] == active[i]["description"]}
    # A changed, still-active original candidate is not safe to photograph via
    # the old B description, but cannot silently disappear from its unexamined
    # comparison scope and permit a new-product decision.
    scope = set(current) | (set(original) & set(active))
    photographed = sorted({row["owner_id"] for row in images if row["owner_id"] != child_id})
    selected = []
    for candidate_id in photographed:
        if candidate_id in original and candidate_id in active and candidate_id not in retained:
            raise SafetyError("VISUAL_ORIGINAL_CANDIDATE_CHANGED")
        if candidate_id not in current and candidate_id not in retained:
            raise SafetyError("VISUAL_PHOTO_OWNER_INVALID")
        selected.append(current.get(candidate_id, retained.get(candidate_id)))
    if len(selected) > 3:
        raise SafetyError("VISUAL_CANDIDATE_LIMIT")
    for candidate in retrieved["candidates"]:
        if len(selected) == 3:
            break
        if candidate["id"] not in photographed:
            selected.append(candidate)
    return {"candidates": selected, "omitted": len(scope) - len(selected),
            "comparison_scope": sorted(scope)}


def _photo_ownership(state, record, child_id, semantic_id, candidates, at):
    try:
        observation_id, owner = record["source_ref"], record["owner_id"]
        observation = state["observations"].get(observation_id)
        if (not observation or record["observed_at"] != observation["observed_at"]
                or record["source_url"] != observation.get("semantic", {}).get("original_image_url")
                or timestamp(observation["observed_at"]) > timestamp(at)):
            raise SafetyError("VISUAL_PHOTO_SOURCE_BINDING_INVALID")
        if owner == child_id:
            if observation["semantic_id"] != semantic_id:
                raise SafetyError("VISUAL_PHOTO_OWNER_INVALID")
            _, parent_hash, unambiguous = _child(state, child_id, semantic_id)
            binding = {"owner": owner, "parent_hash": parent_hash}
        else:
            if owner not in {candidate["id"] for candidate in candidates}:
                raise SafetyError("VISUAL_PHOTO_OWNER_INVALID")
            associations = [(i, a) for i, a in state["associations"].items()
                            if a.get("status") == "resolved" and a.get("concept_id") == owner
                            and a.get("semantic_id") == observation["semantic_id"]]
            if not associations:
                raise SafetyError("VISUAL_PHOTO_OWNER_INVALID")
            source_child, source_association = min(associations)
            _, parent_hash, unambiguous = _child(state, source_child, observation["semantic_id"])
            binding = {"owner": owner, "source_child": source_child,
                       "association_hash": digest(_association_input(source_association)), "parent_hash": parent_hash}
        if not unambiguous:
            raise SafetyError("VISUAL_MIXED_SOURCE_IMAGE_UNATTRIBUTED")
        # An old photograph can remain useful for identity, but not after its
        # source listing was replaced with different meaning or a different URL.
        source_key = (observation["source"], observation["source_item"])
        latest = [(i, o) for i, o in state["observations"].items()
                  if (o["source"], o["source_item"]) == source_key]
        latest_id, current = max(latest, key=lambda row: (timestamp(row[1]["observed_at"]), row[0]))
        if timestamp(current["observed_at"]) > timestamp(at):
            raise SafetyError("VISUAL_CURRENT_SOURCE_POSTDATES_CHECK")
        if (current["semantic_id"] != observation["semantic_id"]
                or current.get("semantic", {}).get("original_image_url") != record["source_url"]):
            raise SafetyError("VISUAL_PHOTO_SOURCE_REPLACED")
        return {**binding, "observation_id": observation_id, "observation_hash": digest(observation),
                "current_observation_id": latest_id, "current_observation_hash": digest(current)}
    except (KeyError, TypeError, ValueError):
        raise SafetyError("VISUAL_PHOTO_SOURCE_BINDING_INVALID") from None


def _protected(state, candidates):
    ids = {row["id"] for row in candidates}
    references = {}
    for record_id, row in state["published_history"].items():
        if row.get("status") == "published" and row.get("concept_id") in ids:
            references["published:" + record_id] = digest(row)
    for record_id, row in state["issue_reservations"].items():
        if (row.get("state") in ("reserved", "uploaded", "published")
                and any(p.get("concept_id") in ids for p in row.get("products", []))):
            references["issue:" + record_id] = digest(row)
    return references


def _prepare(memory, state, child_id, images, at):
    identifier(child_id)
    association = state["associations"].get(child_id)
    if not association or association.get("status") != "deferred" or association.get("concept_id") is not None:
        raise SafetyError("VISUAL_DEFERRED_CHILD_REQUIRED")
    semantic_id = association["semantic_id"]
    product, parent_hash, single = _child(state, child_id, semantic_id)
    if not single:
        raise SafetyError("VISUAL_MIXED_SOURCE_IMAGE_UNATTRIBUTED")
    if product.get("category") not in CATEGORIES or current_brand(state, "child", child_id, product) == "established":
        raise SafetyError("VISUAL_ACTIVE_TARGET_REQUIRED")
    b_job, b_request_hash, b_answer_hash, original = _saved_b_answer(memory, state, child_id, association, product)
    active = {}
    for concept_id, concept in state["concepts"].items():
        try:
            if _active(state, concept_id, concept):
                active[concept_id] = concept
        except SafetyError:
            continue  # A broken correction binding cannot hold up other candidates.
    if not isinstance(images, list) or len(images) > 4:
        raise SafetyError("VISUAL_IMAGE_LIMIT")
    shortlist = _visual_candidates(product, active, original, images, child_id)
    if not shortlist["candidates"]:
        raise SafetyError("VISUAL_CURRENT_CANDIDATES_REQUIRED")
    ownership = [_photo_ownership(state, record, child_id, semantic_id, shortlist["candidates"], at) for record in images]
    prepared = prepare_visual_comparison(child_id, semantic_id, product, shortlist["candidates"], images,
                                         media_root=research_ai.PUBLIC_PRODUCT_IMAGES)
    inputs = {"version": VERSION, "child_id": child_id, "semantic_id": semantic_id,
              "parent_description_hash": parent_hash, "association_hash": digest(_association_input(association)),
              "b_job_id": b_job, "b_request_hash": b_request_hash, "b_answer_hash": b_answer_hash,
              "request_hash": prepared["request_hash"], "omitted_candidates": shortlist["omitted"],
              "comparison_scope": shortlist["comparison_scope"],
              "candidate_bindings": {row["id"]: digest(active[row["id"]]) for row in shortlist["candidates"]},
              "photo_ownership": ownership, "protected_references": _protected(state, shortlist["candidates"])}
    target_brand = brand_binding(state, "child", child_id, product)
    if target_brand["review_id"]:
        inputs["target_brand_binding"] = target_brand
    corrected = {row["id"]: brand_binding(state, "concept", row["id"], active[row["id"]]["description"])
                 for row in shortlist["candidates"]}
    if any(value["review_id"] for value in corrected.values()):
        inputs["candidate_brand_bindings"] = corrected
    return prepared, inputs


def _save_progress(memory, child_id, expected_association, record):
    current = memory.snapshot()["associations"].get(child_id)
    if not current or digest(_association_input(current)) != digest(_association_input(expected_association)):
        raise SafetyError("VISUAL_ASSOCIATION_CHANGED")
    previous = current.get("visual_followup")
    comparable = {k: v for k, v in (previous or {}).items() if k != "recorded_at"}
    if comparable == record:
        return deepcopy(previous)
    saved = dict(record, recorded_at=utc_now())
    memory.put("associations", child_id, dict(current, visual_followup=saved), previous=current)
    return saved


def _recover_failed_job(memory, state, prior, request, child_id, *, allow_launch):
    """Reopen only the original saved job, never manufacture an automatic retry."""
    job_id = key("job", request)
    if (prior.get("job_id") != job_id
            or state["jobs"].get(job_id, {}).get("request") != request):
        return False
    base = safe_path(memory.root, "ai_jobs", job_id)
    folder = research_ai.current_attempt(base)
    if (folder / "result.json").is_file():
        saved = load_json(folder / "result.json", research_ai.MAX_JOB_BYTES)
        if not isinstance(saved, dict) or saved.get("request_hash") != digest(request):
            raise SafetyError("AI_JOB_RESULT_CONFLICT")
        response = saved["response"]
    elif (folder / "completion.json").is_file():
        saved = load_json(folder / "completion.json", research_ai.MAX_JOB_BYTES)
        intent = load_json(folder / "intent.json", research_ai.MAX_JOB_BYTES)
        if not isinstance(saved, dict) or not isinstance(intent, dict) or intent.get("request") != request:
            raise SafetyError("AI_JOB_RESULT_CONFLICT")
        response = saved["response"]
    else:
        # prepare_retry retains the failed attempt and opens an empty new one.
        # A running/failed attempt is not permission to start another call.
        if folder == base or any((folder / name).exists() for name in
                                 ("intent.json", "failure.json")):
            return False
        retries = load_json(base / "retries.json")
        entry = retries[-1]
        previous_label = "initial" if len(retries) == 1 else "retry-" + str(len(retries) - 1)
        # Older progress records did not name an attempt. Their exact preceding
        # failure/intent below still proves a deliberately prepared retry.
        if prior.get("job_attempt") not in (None, previous_label):
            return False
        previous = base if previous_label == "initial" else safe_path(base, previous_label)
        failure = load_json(previous / "failure.json")
        intent = load_json(previous / "intent.json", research_ai.MAX_JOB_BYTES)
        if (not isinstance(failure, dict) or not isinstance(intent, dict)
                or not isinstance(entry, dict) or set(entry) != {"reason", "previous_failure", "at"}
                or not isinstance(entry["reason"], str) or not entry["reason"].strip()
                or entry["previous_failure"] != failure.get("code")
                or failure.get("code") != prior.get("reason") or intent.get("request") != request
                or any((previous / name).exists() for name in ("result.json", "completion.json"))):
            return False
        timestamp(entry["at"])
        # A no-model inspection must not consume the deliberate retry or replace
        # its original failure with AI_RUNNER_NOT_ENABLED.
        return allow_launch
    # Let Jobs reconcile the receipt/application only after validating the same
    # child answer. Invalid saved answers cannot cause a second model call.
    return child_id in research_ai.validate_results("D", request["payload"], response)["accepted"]


def _resolve(memory, child_id, images, runner, at):
    state = memory.snapshot()
    association = state["associations"].get(child_id)
    prior = association.get("visual_followup") if association else None
    if association and association.get("status") == "resolved":
        if prior and prior.get("status") == "applied" and prior.get("image_input_hash") == digest(images):
            validate_visual_payload(prior["payload"], media_root=research_ai.PUBLIC_PRODUCT_IMAGES)
            return {"child_id": child_id, "status": "already_applied", "applied": False,
                    "job_id": prior["job_id"], "concept_id": association["concept_id"], "reason": prior["reason"]}
        raise SafetyError("VISUAL_DEFERRED_CHILD_REQUIRED")
    prepared, inputs = _prepare(memory, state, child_id, images, at)
    question_hash = digest(inputs)
    request = {"role": "D", "payload": prepared["request"]["payload"],
               "model": research_ai.MODEL, "prompt_version": research_ai.prompt_version("D"),
               "runner": getattr(runner, "identity", "codex-chatgpt-high-v1")}
    if prior and prior.get("question_hash") == question_hash and prior.get("status") in (
            "unavailable", "unclear", "incomplete_comparison", "needs_review", "failed"):
        if prior["status"] != "failed" or not _recover_failed_job(
                memory, state, prior, request, child_id, allow_launch=runner is not None):
            return {"child_id": child_id, "status": prior["status"], "applied": False,
                    "job_id": prior.get("job_id"), "reason": prior["reason"]}
    record = {"version": VERSION, "question_hash": question_hash, "inputs": inputs,
              "image_input_hash": digest(images), "payload": prepared["request"]["payload"],
              "b_job_id": association["job_id"], "job_id": None, "status": "unavailable",
              "reason": "A target photograph and at least one attributable candidate photograph are required."}
    if prepared["status"] != "prepared":
        _save_progress(memory, child_id, association, record)
        return {"child_id": child_id, "status": "unavailable", "applied": False, "job_id": None}
    # Retain the intended durable job identity even when execution is blocked.
    # This is the existing Jobs key, not a second cache or an execution receipt.
    record["job_id"] = key("job", request)
    folder = research_ai.current_attempt(safe_path(memory.root, "ai_jobs", record["job_id"]))
    record["job_attempt"] = "initial" if folder.name == record["job_id"] else folder.name
    try:
        job_id, result = research_ai.Jobs(memory, runner).run("D", prepared["request"]["payload"])
        record["job_id"] = job_id
        answer = result["accepted"].get(child_id)
        if answer is None:
            raise SafetyError(result["failures"].get(child_id, "VISUAL_RESULT_UNAVAILABLE"))
    except SafetyError as error:
        record.update(status="failed", reason=str(error))
        _save_progress(memory, child_id, association, record)
        return {"child_id": child_id, "status": "failed", "applied": False, "reason": str(error), "job_id": record["job_id"]}
    record["answer"] = answer
    latest = memory.snapshot()
    try:
        _, current_inputs = _prepare(memory, latest, child_id, images, at)
        unchanged = digest(current_inputs) == question_hash
    except SafetyError:
        unchanged = False
    if not unchanged:
        record.update(status="inputs_changed", reason="The bound source, child, candidate or protected report inputs changed during the check.")
        _save_progress(memory, child_id, association, record)
        return {"child_id": child_id, "status": "inputs_changed", "applied": False, "job_id": job_id}
    decision = answer["decision"]
    if decision == "unclear":
        record.update(status="unclear", reason=answer["reason"])
    elif decision == "different" and inputs["omitted_candidates"]:
        record.update(status="incomplete_comparison", reason="The photographs do not cover all plausible existing candidates; no new concept was created.")
    elif decision == "same" and _protected(latest, [{"id": answer["match_id"]}]):
        record.update(status="needs_review", reason="Attaching to this published or reserved concept would change its source-dependent facts; operator review is required.")
    else:
        concept_id = answer["match_id"] if decision == "same" else key("concept", child_id)
        if decision == "different" and concept_id in latest["concepts"]:
            record.update(status="needs_review", reason="The provisional concept key already exists; no concept was overwritten.")
        else:
            record.update(status="applied", reason=answer["reason"], recorded_at=utc_now())
            changes = []
            if decision == "different":
                description = prepared["request"]["payload"]["items"][0]["description"]
                effective_brand = brand_binding(latest, "child", child_id, description)
                origin = ({"origin_child_id": child_id, "origin_brand_review_id": effective_brand["review_id"],
                           "origin_description_hash": digest(description)} if effective_brand["review_id"] else {})
                changes.append(change("concepts", concept_id,
                    {"description": description, "status": "provisional", **origin}))
            else:
                origin = {}
            current_association = latest["associations"][child_id]
            updated = dict(current_association, concept_id=concept_id, status="resolved", reason=answer["reason"],
                           b_reason=current_association["reason"], method="llm_visual_accepted", visual_followup=record)
            updated.update(origin)
            changes.append(change("associations", child_id, updated, current_association))
            def unchanged(current):
                _, bound_inputs = _prepare(memory, current, child_id, images, at)
                if digest(bound_inputs) != question_hash:
                    raise SafetyError("VISUAL_APPLICATION_BINDING_CHANGED")
            try:
                memory.commit(key("visual-apply", [child_id, question_hash, job_id]), changes, validate=unchanged)
            except SafetyError as error:
                record.update(status="inputs_changed", reason=str(error))
                _save_progress(memory, child_id, association, record)
                return {"child_id": child_id, "status": "inputs_changed", "applied": False, "job_id": job_id}
            return {"child_id": child_id, "status": "applied", "applied": True, "job_id": job_id, "concept_id": concept_id}
    _save_progress(memory, child_id, association, record)
    return {"child_id": child_id, "status": record["status"], "applied": False, "job_id": job_id}


def resolve_saved_visual_followups(memory, requests, runner=None, *, as_of=None):
    """Process at most five explicit saved-photo questions; siblings continue.

    Requests contain only child_id and images (the existing provenance records).
    This is an operator-connected entry point, not automatic B dispatch or image
    fetching. It does not correct resolved associations or edit protected reports.
    """
    if (not isinstance(requests, list) or not 1 <= len(requests) <= 5
            or any(not isinstance(row, dict) or set(row) != {"child_id", "images"} for row in requests)):
        raise SafetyError("VISUAL_FOLLOWUP_REQUESTS_INVALID")
    ids = [identifier(row["child_id"]) for row in requests]
    if len(set(ids)) != len(ids):
        raise SafetyError("VISUAL_FOLLOWUP_REQUESTS_INVALID")
    at = as_of or utc_now()
    timestamp(at)
    results = []
    with Journal(memory.root).locked():
        for row in requests:
            try:
                results.append(_resolve(memory, row["child_id"], row["images"], runner, at))
            except SafetyError as error:
                results.append({"child_id": row["child_id"], "status": "unavailable", "applied": False, "reason": str(error)})
            except (KeyError, TypeError, ValueError, OSError):
                results.append({"child_id": row["child_id"], "status": "unavailable", "applied": False,
                                "reason": "VISUAL_SAVED_INPUT_INVALID"})
    return {"results": results, "applied": sum(row["applied"] for row in results),
            "collector_requests": 0, "client_ready": False}
