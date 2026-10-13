"""Bounded definition judgment from actual accepted A/B product examples.

This attaches meaning to an existing concept. It does not create identities,
change associations/checklists, collect sources or publish a report. Only the
initial model acceptance reads original Jobs files; the saved nested proof can
be checked later without relying on a temporary model-output file.
"""

from copy import deepcopy
import re

from .functional_types import (accept_functional_definition, effective_functional_definition,
    validate_definition)
from .models import SafetyError, canonical, digest, identifier, timestamp
from .operations import record_saved_decision
from .research_memory import change, key, load_event
from .service import CATEGORIES
from .storage import safe_path

CONTRACT = "functional-definition-review-v1"
MODE = "functional_definition_review"
PROMPT_VERSION = "research-b-functional-definition-v1"
PROOF = "functional-definition-original-acceptance-v1"
PROOF_KEY = "definition_acceptance"
MAX_ITEMS = 5
MAX_EXAMPLES = 20
MAX_INPUT_BYTES = 48 * 1024
MAX_RESULT_BYTES = 48 * 1024
MAX_PROOF_BYTES = 480 * 1024
_DESCRIPTION_FIELDS = ("label", "category", "function", "mechanism", "audience", "features", "basis")
_HASH = re.compile(r"[0-9a-f]{64}")


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise SafetyError("AI_FUNCTIONAL_DEFINITION_TEXT_INVALID")
    return value


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_BINDING_INVALID")
    return value


def validate_definition_item(item):
    if (not isinstance(item, dict) or set(item) != {"id", "category", "examples"}
            or item.get("category") not in CATEGORIES or not isinstance(item["examples"], list)
            or not 1 <= len(item["examples"]) <= MAX_EXAMPLES):
        raise SafetyError("AI_FUNCTIONAL_DEFINITION_INPUT_INVALID")
    identifier(item["id"])
    children = []
    for example in item["examples"]:
        if not isinstance(example, dict) or set(example) != {"child_id", "semantic_id", "description"}:
            raise SafetyError("AI_FUNCTIONAL_DEFINITION_INPUT_INVALID")
        identifier(example["child_id"]); identifier(example["semantic_id"])
        description = example["description"]
        if (not isinstance(description, dict) or set(description) != set(_DESCRIPTION_FIELDS)
                or description.get("category") not in (*CATEGORIES, "Out of scope")
                or description.get("basis") not in {"observed", "inferred"}
                or not isinstance(description.get("features"), list) or len(description["features"]) > 40):
            raise SafetyError("AI_FUNCTIONAL_DEFINITION_INPUT_INVALID")
        for field in ("label", "function", "mechanism", "audience"):
            _text(description[field])
        for value in description["features"]: _text(value)
        children.append(example["child_id"])
    if children != sorted(set(children)):
        raise SafetyError("AI_FUNCTIONAL_DEFINITION_INPUT_INVALID")
    return item


def validate_definition_payload(payload):
    if (not isinstance(payload, dict) or set(payload) != {"mode", "definition_contract", "items"}
            or payload.get("mode") != MODE or payload.get("definition_contract") != CONTRACT
            or not isinstance(payload.get("items"), list) or not 1 <= len(payload["items"]) <= MAX_ITEMS):
        raise SafetyError("AI_FUNCTIONAL_DEFINITION_INPUT_INVALID")
    for item in payload["items"]: validate_definition_item(item)
    if len({item["id"] for item in payload["items"]}) != len(payload["items"]):
        raise SafetyError("AI_FUNCTIONAL_DEFINITION_INPUT_INVALID")
    if len(canonical(payload)) > MAX_INPUT_BYTES:
        raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_SIZE_LIMIT")
    return payload


def validate_definition_result(item, result):
    validate_definition_item(item)
    if (not isinstance(result, dict) or set(result) != {"id", "decision", "definition", "references", "example_decisions", "reason"}
            or result.get("id") != item["id"] or result.get("decision") not in {"prepared", "unclear"}
            or not isinstance(result.get("references"), list) or not isinstance(result.get("example_decisions"), list)):
        raise SafetyError("AI_FUNCTIONAL_DEFINITION_RESULT_INVALID")
    _text(result["reason"])
    known = {example["child_id"] for example in item["examples"]}
    decisions = {}
    for row in result["example_decisions"]:
        if (not isinstance(row, dict) or set(row) != {"child_id", "decision", "reason"}
                or row.get("child_id") not in known or row["child_id"] in decisions
                or row.get("decision") not in {"same", "different", "unclear"}):
            raise SafetyError("AI_FUNCTIONAL_DEFINITION_EXAMPLE_INVALID")
        _text(row["reason"]); decisions[row["child_id"]] = row["decision"]
    refs = result["references"]
    if (set(decisions) != known or any(not isinstance(ref, str) for ref in refs)
            or len(set(refs)) != len(refs) or not set(refs) <= known):
        raise SafetyError("AI_FUNCTIONAL_DEFINITION_EXAMPLE_INVALID")
    if result["decision"] == "prepared":
        definition = validate_definition(result["definition"])
        if len(definition["report_title"]) > 160 or not refs or any(decisions[ref] != "same" for ref in refs):
            raise SafetyError("AI_FUNCTIONAL_DEFINITION_REFERENCE_INVALID")
    else:
        if result["definition"] is not None or refs or any(value == "same" for value in decisions.values()):
            raise SafetyError("AI_FUNCTIONAL_DEFINITION_UNCLEAR_INVALID")
        definition = None
    normalized = dict(deepcopy(result), definition=definition,
        references=sorted(refs), example_decisions=sorted(deepcopy(result["example_decisions"]), key=lambda row: row["child_id"]))
    if len(canonical(normalized)) > MAX_RESULT_BYTES:
        raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_SIZE_LIMIT")
    return normalized


def _description(state, child_id, semantic_id):
    saved = state.get("descriptions", {}).get(semantic_id)
    if not isinstance(saved, dict) or not isinstance(saved.get("result"), dict):
        raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTED_DESCRIPTION_REQUIRED")
    result = saved["result"]
    if result.get("id") != semantic_id or result.get("kind") not in {"product", "mixed"} or not isinstance(result.get("products"), list):
        raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTED_DESCRIPTION_REQUIRED")
    matches = [product for index, product in enumerate(result["products"])
        if key("child", [semantic_id, index, digest(result)]) == child_id]
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTED_DESCRIPTION_REQUIRED")
    product = matches[0]
    try: projected = {field: deepcopy(product[field]) for field in _DESCRIPTION_FIELDS}
    except KeyError: raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTED_DESCRIPTION_REQUIRED") from None
    return product, projected, saved


def definition_review_input(state, concept_id, *, as_of, child_ids=None):
    """Prepare actual examples locally; dates/prices are not model cache keys."""
    timestamp(as_of)
    meaning = effective_functional_definition(state, concept_id)
    concept = state["concepts"][concept_id]
    category = concept["description"].get("category")
    if category not in CATEGORIES:
        raise SafetyError("FUNCTIONAL_DEFINITION_CATEGORY_UNSUPPORTED")
    if meaning["accepted"]:
        return {"state": "current", "concept_id": concept_id, "definition_id": meaning["definition_id"],
                "meaning_hash": meaning["meaning_hash"]}
    from .source_coverage import _product_context
    _, excluded = _product_context(state, concept_id)
    if excluded:
        raise SafetyError("FUNCTIONAL_DEFINITION_NOT_APPLICABLE")
    associations = {cid: row for cid, row in state.get("associations", {}).items()
        if row.get("concept_id") == concept_id and row.get("status") == "resolved"}
    chosen = sorted(associations)[:MAX_EXAMPLES] if child_ids is None else child_ids
    if (not isinstance(chosen, list) or not chosen or len(chosen) > MAX_EXAMPLES
            or chosen != sorted(set(chosen)) or not set(chosen) <= set(associations)):
        raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTED_EXAMPLES_REQUIRED")
    examples, bindings = [], {}
    for child_id in chosen:
        association = associations[child_id]; semantic_id = association.get("semantic_id")
        product, description, saved = _description(state, child_id, semantic_id)
        originals = [(oid, observation) for oid, observation in state.get("observations", {}).items()
            if observation.get("semantic_id") == semantic_id and timestamp(observation["observed_at"]) <= timestamp(as_of)]
        if not originals:
            raise SafetyError("FUNCTIONAL_DEFINITION_ORIGINAL_OBSERVATION_REQUIRED")
        # One actual original per semantic example is sufficient grounding.
        # Extra recaptures/price dates do not multiply model inputs or examples.
        oid, observation = min(originals, key=lambda row: (timestamp(row[1]["observed_at"]), row[0]))
        examples.append({"child_id": child_id, "semantic_id": semantic_id, "description": description})
        bindings[child_id] = {"semantic_id": semantic_id, "association_hash": digest(association),
            "description_hash": digest(product), "description_record_hash": digest(saved),
            "observation_id": oid, "observation_hash": digest(observation)}
    item = validate_definition_item({"id": concept_id, "category": category, "examples": examples})
    binding = {"concept_id": concept_id, "expected_meaning_hash": meaning["meaning_hash"],
        "item_hash": digest(item), "children": bindings, "prepared_at": as_of}
    return {"state": "prepared", "concept_id": concept_id, "item": item, "binding": binding,
        "omitted_example_count": max(0, len(associations) - len(chosen))}


def _validate_binding(state, item, binding, *, current=True):
    if (not isinstance(binding, dict) or set(binding) != {"concept_id", "expected_meaning_hash", "item_hash", "children", "prepared_at"}
            or binding["concept_id"] != item["id"] or binding["item_hash"] != digest(item)
            or not isinstance(binding["children"], dict)
            or set(binding["children"]) != {row["child_id"] for row in item["examples"]}):
        raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_BINDING_INVALID")
    _hash(binding["expected_meaning_hash"]); timestamp(binding["prepared_at"])
    for child_id, child in binding["children"].items():
        if not isinstance(child, dict) or set(child) != {"semantic_id", "association_hash", "description_hash", "description_record_hash", "observation_id", "observation_hash"}:
            raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_BINDING_INVALID")
        identifier(child_id); identifier(child["semantic_id"]); identifier(child["observation_id"])
        if child["semantic_id"] != next(row["semantic_id"] for row in item["examples"] if row["child_id"] == child_id):
            raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_BINDING_INVALID")
        for field in ("association_hash", "description_hash", "description_record_hash", "observation_hash"): _hash(child[field])
    if current:
        prepared = definition_review_input(state, item["id"], as_of=binding["prepared_at"], child_ids=sorted(binding["children"]))
        if prepared["state"] != "prepared" or canonical(prepared["item"]) != canonical(item) or canonical(prepared["binding"]) != canonical(binding):
            raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_INPUT_CHANGED")
    return binding


def _payload(items):
    return {"mode": MODE, "definition_contract": CONTRACT, "items": items}


def _original_job(state, job_id, item):
    from .research_ai import MODEL, _item_bindings
    job = state.get("jobs", {}).get(job_id)
    try:
        request = job["request"]; payload = validate_definition_payload(request["payload"])
        if (set(request) != {"role", "payload", "model", "prompt_version", "runner"}
                or request["role"] != "B" or request["model"] != MODEL or request["prompt_version"] != PROMPT_VERSION
                or not isinstance(request["runner"], str) or not request["runner"] or key("job", request) != job_id
                or canonical(job["binding_keys"]) != canonical(_item_bindings("B", payload, MODEL, PROMPT_VERSION, request["runner"]))
                or [row for row in payload["items"] if row["id"] == item["id"]] != [item]):
            raise SafetyError("FUNCTIONAL_DEFINITION_ORIGINAL_JOB_INVALID")
    except (KeyError, TypeError, AttributeError):
        raise SafetyError("FUNCTIONAL_DEFINITION_ORIGINAL_JOB_INVALID") from None
    return job


def _owner(state, item):
    owners = []
    for jid, job in state.get("jobs", {}).items():
        request = job.get("request", {})
        if request.get("role") != "B" or request.get("prompt_version") != PROMPT_VERSION: continue
        if not any(row.get("id") == item["id"] and canonical(row) == canonical(item)
            for row in request.get("payload", {}).get("items", [])): continue
        _original_job(state, jid, item); owners.append(jid)
    if len(owners) > 1:
        raise SafetyError("FUNCTIONAL_DEFINITION_ORIGINAL_OWNER_CONFLICT")
    return owners[0] if owners else None


def validate_definition_acceptance_proof(proof, *, concept_id=None, definition=None, example_decisions=None, basis_refs=None):
    """Pure exact original request/result validation for later portable reads."""
    from .research_ai import validate_results
    fields = {"contract", "job_id", "original_job", "attempt", "result_fingerprint", "item", "binding", "request_hash",
        "binding_key", "result_receipt", "result_hash", "accepted_item", "accepted_item_hash"}
    if not isinstance(proof, dict) or set(proof) != fields or proof.get("contract") != PROOF:
        raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTANCE_PROOF_INVALID")
    if len(canonical(proof)) > MAX_PROOF_BYTES:
        raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_SIZE_LIMIT")
    item = validate_definition_item(proof["item"])
    _validate_binding({}, item, proof["binding"], current=False)
    job = _original_job({"jobs": {proof["job_id"]: proof["original_job"]}}, proof["job_id"], item)
    request, receipt = job["request"], proof["result_receipt"]
    if (not isinstance(proof["attempt"], str) or not re.fullmatch(r"initial|retry-(?:[1-9]|1[0-9]|20)", proof["attempt"])
            or not isinstance(proof["result_fingerprint"], list) or len(proof["result_fingerprint"]) != 5
            or any(type(value) is not int or value < 0 for value in proof["result_fingerprint"])
            or proof["binding_key"] != job["binding_keys"][item["id"]]
            or proof["request_hash"] != digest(request) or proof["result_hash"] != digest(receipt)
            or not isinstance(receipt, dict) or set(receipt) != {"request_hash", "response", "usage", "completed_at"}
            or receipt["request_hash"] != digest(request)):
        raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTANCE_PROOF_INVALID")
    timestamp(receipt["completed_at"])
    checked = validate_results("B", request["payload"], receipt["response"], declared_version=PROMPT_VERSION)
    accepted = checked["accepted"].get(item["id"])
    if (accepted is None or canonical(accepted) != canonical(proof["accepted_item"])
            or digest(accepted) != proof["accepted_item_hash"] or concept_id is not None and concept_id != item["id"]):
        raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTED_RESULT_MISMATCH")
    if definition is not None:
        if accepted["decision"] != "prepared" or canonical(validate_definition(definition)) != canonical(accepted["definition"]):
            raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTED_RESULT_MISMATCH")
    children = proof["binding"]["children"]
    expected_decisions = {row["child_id"]: {"decision": row["decision"], "reason": " ".join(row["reason"].split()),
        "association_hash": children[row["child_id"]]["association_hash"],
        "description_hash": children[row["child_id"]]["description_hash"]} for row in accepted["example_decisions"]}
    expected_basis = {children[child]["observation_id"]: children[child]["observation_hash"] for child in accepted["references"]}
    if (example_decisions is not None and canonical(example_decisions) != canonical(expected_decisions)
            or basis_refs is not None and canonical(basis_refs) != canonical(expected_basis)):
        raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTED_RESULT_MISMATCH")
    return deepcopy(accepted)


def verify_definition_acceptance(memory, state, prepared, job_id):
    from .research_ai import MAX_JOB_BYTES, current_attempt
    item, binding = prepared["item"], prepared["binding"]
    _validate_binding(state, item, binding)
    job = _original_job(state, job_id, item)
    base = safe_path(memory.root, "ai_jobs", job_id); folder = current_attempt(base); path = folder / "result.json"
    try:
        before = path.lstat()
        receipt = load_event(path, MAX_JOB_BYTES)
        after = path.lstat()
    except OSError:
        raise SafetyError("FUNCTIONAL_DEFINITION_ORIGINAL_RESULT_UNAVAILABLE") from None
    fingerprint = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if fingerprint(before) != fingerprint(after) or current_attempt(base) != folder:
        raise SafetyError("FUNCTIONAL_DEFINITION_ORIGINAL_RESULT_CHANGED")
    from .research_ai import validate_results
    checked = validate_results("B", job["request"]["payload"], receipt.get("response"), declared_version=PROMPT_VERSION)
    accepted = checked["accepted"].get(item["id"])
    if accepted is None: raise SafetyError("FUNCTIONAL_DEFINITION_ORIGINAL_RESULT_NOT_ACCEPTED")
    proof = {"contract": PROOF, "job_id": job_id, "original_job": deepcopy(job),
        "attempt": "initial" if folder == base else folder.name, "item": deepcopy(item), "binding": deepcopy(binding),
        "result_fingerprint": list(fingerprint(after)),
        "request_hash": digest(job["request"]), "binding_key": job["binding_keys"][item["id"]],
        "result_receipt": receipt, "result_hash": digest(receipt), "accepted_item": accepted,
        "accepted_item_hash": digest(accepted)}
    validate_definition_acceptance_proof(proof)
    return {"proof": proof, "path": path, "fingerprint": fingerprint(after), "base": base, "folder": folder}


def validate_current_definition_acceptance(memory, state, proof):
    """Pin exact original attempt and accepted files during the definition commit."""
    from .research_ai import MAX_JOB_BYTES, current_attempt
    validate_definition_acceptance_proof(proof)
    _validate_binding(state, proof["item"], proof["binding"])
    job = _original_job(state, proof["job_id"], proof["item"])
    if any(canonical(job.get(field)) != canonical(proof["original_job"].get(field)) for field in ("request", "binding_keys")):
        raise SafetyError("FUNCTIONAL_DEFINITION_ORIGINAL_RESULT_CHANGED")
    base = safe_path(memory.root, "ai_jobs", proof["job_id"]); folder = current_attempt(base)
    if ("initial" if folder == base else folder.name) != proof["attempt"]:
        raise SafetyError("FUNCTIONAL_DEFINITION_ORIGINAL_RESULT_CHANGED")
    path = folder / "result.json"
    try:
        status = path.lstat()
        fingerprint = [status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns]
    except OSError:
        raise SafetyError("FUNCTIONAL_DEFINITION_ORIGINAL_RESULT_UNAVAILABLE") from None
    if fingerprint != proof["result_fingerprint"]:
        raise SafetyError("FUNCTIONAL_DEFINITION_ORIGINAL_RESULT_CHANGED")
    receipt = load_event(path, MAX_JOB_BYTES)
    if canonical(receipt) != canonical(proof["result_receipt"]) or current_attempt(base) != folder:
        raise SafetyError("FUNCTIONAL_DEFINITION_ORIGINAL_RESULT_CHANGED")


def _current_unclear(state, item):
    for rid, row in state.get("source_checks", {}).items():
        if row.get("kind") != CONTRACT: continue
        if (set(row) != {"kind", "job_proof", "result", "record_hash"}
                or row["record_hash"] != digest({k: v for k, v in row.items() if k != "record_hash"})
                or rid != key("functional-definition-review", {k: v for k, v in row.items() if k != "record_hash"})):
            raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_RECORD_INVALID")
        accepted = validate_definition_acceptance_proof(row["job_proof"])
        if canonical(accepted) != canonical(row["result"]) or accepted["decision"] != "unclear":
            raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_RECORD_INVALID")
        if canonical(row["job_proof"]["item"]) == canonical(item): return rid
    return None


def _validate_review_scope(concept_ids, as_of, limit):
    if (not isinstance(concept_ids, (list, tuple))
            or any(not isinstance(cid, str) for cid in concept_ids) or len(set(concept_ids)) != len(concept_ids)
            or type(limit) is not int or not 1 <= limit <= MAX_ITEMS):
        raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_SCOPE_INVALID")
    timestamp(as_of)


def prepare_functional_definition_reviews(state, concept_ids, *, as_of, limit=MAX_ITEMS):
    _validate_review_scope(concept_ids, as_of, limit)
    ready, current, waiting = [], {}, {}
    for cid in concept_ids:
        try:
            prepared = definition_review_input(state, cid, as_of=as_of)
            if prepared["state"] == "current": current[cid] = prepared; continue
            unclear = _current_unclear(state, prepared["item"])
            if unclear is not None:
                waiting[cid] = {"reason": "FUNCTIONAL_DEFINITION_UNCLEAR", "review_id": unclear}; continue
            if len(ready) < limit: ready.append(prepared)
        except SafetyError as error: waiting[cid] = {"reason": str(error)}
    return {"prepared": ready, "current": current, "waiting": waiting}


def _dependency_signal(state, cid):
    """Actual saved dependencies, including invalid/unprepared subject inputs."""
    associations = {child: row for child, row in state.get("associations", {}).items() if row.get("concept_id") == cid}
    semantic_ids = {row.get("semantic_id") for row in associations.values() if isinstance(row.get("semantic_id"), str)}
    return {"concept": state.get("concepts", {}).get(cid), "associations": associations,
        "descriptions": {sid: state.get("descriptions", {}).get(sid) for sid in sorted(semantic_ids)},
        "observations": {oid: row for oid, row in state.get("observations", {}).items() if row.get("semantic_id") in semantic_ids}}


def functional_definition_readiness(memory, concept_ids, *, as_of, limit=MAX_ITEMS, skip_concept_bindings=None,
                                  retry_seconds=300, retry_not_before=None, state=None):
    """Pure state/file hints; an enclosing inspection may share verified state.

    Standalone reads and actual review/admission stay fresh. Original files are
    inspected on every call, even when the enclosing inspection supplies state.
    """
    from .research_ai import current_attempt, closed_model_retry_hint, prepared_closed_model_retry_hint
    from .work_turns import new_model_work_wait
    # Validate the full bounded scope once. Unknown originals must not occupy
    # every ready slot and starve an independent concept behind them.
    state = memory.snapshot() if state is None else state
    _validate_review_scope(concept_ids, as_of, limit)
    fresh_wait = new_model_work_wait(memory, "B", state=state, review=True)
    if (skip_concept_bindings is not None and (not isinstance(skip_concept_bindings, dict)
            or any(not isinstance(cid, str) or not isinstance(value, str) or not _HASH.fullmatch(value)
                   for cid, value in skip_concept_bindings.items()))):
        raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_SCOPE_INVALID")
    skipped = skip_concept_bindings or {}
    local_ids, model_ids, waiting, current, bindings, skipped_ids = [], [], {}, {}, {}, []
    retry_hints, waits = {}, []
    for cid in concept_ids:
        prepared = prepare_functional_definition_reviews(state, [cid], as_of=as_of, limit=1)
        current.update(prepared["current"]); waiting.update(prepared["waiting"])
        if not prepared["prepared"]:
            bindings[cid] = digest({"concept_id": cid, "dependencies": _dependency_signal(state, cid),
                "current": prepared["current"].get(cid), "waiting": prepared["waiting"].get(cid)})
            if skipped.get(cid) == bindings[cid]: skipped_ids.append(cid)
            continue
        row = prepared["prepared"][0]; item = row["item"]
        signal = {"concept_id": cid, "item_hash": digest(item),
            "binding": {k: v for k, v in row["binding"].items() if k != "prepared_at"}}
        try:
            validate_definition_payload(_payload([item]))
            owner = _owner(state, item); files = []
            ready = True
            local = False
            if owner is not None:
                base = safe_path(memory.root, "ai_jobs", owner); folder = current_attempt(base)
                signal["attempt"] = "initial" if folder == base else folder.name
                for name in ("intent.json", "completion.json", "result.json", "runner_execution.json", "failure.json"):
                    try:
                        status = (folder / name).lstat()
                        info = [status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns]
                    except FileNotFoundError: info = None
                    files.append([name, info])
                present = {name for name, info in files if info is not None}
                local = bool(present & {"completion.json", "result.json"})
                ready = bool(present & {"completion.json", "result.json"}) or not bool(present & {"intent.json", "runner_execution.json", "failure.json"})
                if not local:
                    retry = closed_model_retry_hint(memory, owner, as_of=as_of, retry_seconds=retry_seconds,
                                                    not_before=retry_not_before, state=state)
                    retry = dict(retry, requires_prepare=True)
                    if folder.name.startswith("retry-") and not present:
                        prepared_retry = prepared_closed_model_retry_hint(memory, owner, as_of=as_of,
                            retry_seconds=retry_seconds, not_before=retry_not_before, state=state)
                        if prepared_retry["previous_attempt"] is not None:
                            # A checked pointer is not an unstarted original:
                            # changed configured/global deadlines still apply.
                            retry = dict(prepared_retry, requires_prepare=False)
                            ready = prepared_retry["ready"]
                    retry_hints[owner] = retry
                    signal["closed_retry"] = retry
                    ready = ready or retry["ready"]
                    if retry["next_retry_at"] is not None and timestamp(retry["next_retry_at"]) > timestamp(as_of):
                        waits.append(retry["next_retry_at"])
                # A never-started original batch must still have all its original
                # inputs. A changed sibling wakes its exact owner, not a new job.
                signal["original_batch"] = {old["id"]: _dependency_signal(state, old["id"])
                    for old in state["jobs"][owner]["request"]["payload"]["items"]}
            signal.update(owner=owner, files=files)
            if owner is None and fresh_wait is not None:
                ready = False
                signal["fresh_work_wait"] = fresh_wait
            bindings[cid] = digest(signal)
            if skipped.get(cid) == bindings[cid]:
                skipped_ids.append(cid)
                if not ready: waiting[cid] = {"reason": fresh_wait if owner is None and fresh_wait is not None
                    else retry_hints.get(owner, {}).get("reason", "AI_JOB_PENDING_RECONCILIATION")}
                continue
            if not ready:
                waiting[cid] = {"reason": fresh_wait if owner is None and fresh_wait is not None
                    else retry_hints.get(owner, {}).get("reason", "AI_JOB_PENDING_RECONCILIATION")}
            elif local: local_ids.append(cid)
            else: model_ids.append(cid)
        except (SafetyError, OSError) as error:
            waiting[cid] = {"reason": str(error) if isinstance(error, SafetyError) else "FUNCTIONAL_DEFINITION_ORIGINAL_FILE_WAIT"}
            bindings[cid] = digest({"input": signal, "waiting": waiting[cid]})
            if skipped.get(cid) == bindings[cid]: skipped_ids.append(cid)
    selected = (local_ids + model_ids)[:limit]
    return {"ready": bool(selected), "local_ready": bool(local_ids), "model_ready": bool(model_ids),
        "local_ready_concept_ids": local_ids[:limit], "model_ready_concept_ids": model_ids[:limit],
        "ready_concept_ids": selected, "current": current,
        "waiting": waiting, "bindings": bindings, "skipped_concept_ids": skipped_ids,
        "retry_hints": retry_hints, "waits": sorted(set(waits), key=timestamp),
        "signal": digest({"bindings": bindings, "current": current, "waiting": waiting})}


class _SavedDefinitionRunner:
    def __init__(self, identity): self.identity = identity
    def __call__(self, role, payload, folder): raise SafetyError("AI_JOB_PENDING_RECONCILIATION")


def review_functional_definitions(memory, concept_ids, runner=None, *, as_of, limit=MAX_ITEMS, skip_concept_bindings=None,
                                retry_seconds=300, retry_not_before=None):
    """One finite batch, exact original recovery and atomic existing-definition acceptance."""
    from .research_ai import Jobs, current_attempt, prepare_closed_model_retry, prepared_closed_model_retry_hint
    from .work_turns import active_work_turn
    def jobs(selected_runner):
        guard = getattr(selected_runner, "check_new_execution", None)
        def before_execution(request, folder):
            if folder.name.startswith("retry-"):
                prepared = prepared_closed_model_retry_hint(memory, key("job", request), as_of=as_of,
                    retry_seconds=retry_seconds, not_before=retry_not_before)
                if not prepared["ready"]: raise SafetyError(prepared["reason"])
            guard()
        callback = before_execution if callable(guard) and active_work_turn(memory) is not None else None
        return Jobs(memory, selected_runner, before_new_execution=callback)
    readiness = functional_definition_readiness(memory, concept_ids, as_of=as_of, limit=limit,
        skip_concept_bindings=skip_concept_bindings, retry_seconds=retry_seconds, retry_not_before=retry_not_before)
    if not readiness["ready_concept_ids"]:
        return {"accepted": dict(readiness["current"]), "failures": dict(readiness["waiting"]), "prepared_count": 0,
            "bindings": readiness["bindings"], "skipped_concept_ids": readiness["skipped_concept_ids"],
            "client_publication_attempted": False}
    prepared = prepare_functional_definition_reviews(memory.snapshot(), readiness["ready_concept_ids"], as_of=as_of, limit=limit)
    accepted = dict(readiness["current"]); failures = dict(readiness["waiting"])
    pending = {row["concept_id"]: row for row in prepared["prepared"]}
    state = memory.snapshot(); originals, novel = {}, []
    for cid, row in pending.items():
        try: owner = _owner(state, row["item"])
        except SafetyError as error: failures[cid] = {"reason": str(error)}; continue
        if owner is None: novel.append(row["item"])
        else: originals.setdefault(owner, []).append(cid)
    outcomes = {}
    for owner, ids in originals.items():
        request = state["jobs"][owner]["request"]
        folder = current_attempt(safe_path(memory.root, "ai_jobs", owner))
        saved = any((folder / name).exists() for name in ("intent.json", "completion.json", "result.json"))
        retry = readiness["retry_hints"].get(owner)
        selected_runner = _SavedDefinitionRunner(request["runner"])
        if not saved or (retry is not None and retry["ready"]):
            if runner is None or getattr(runner, "identity", "codex-chatgpt-high-v1") != request["runner"]:
                failures.update({cid: {"reason": "AI_RUNNER_NOT_ENABLED"} for cid in ids}); continue
            still_current = True
            for old in request["payload"]["items"]:
                try: actual = definition_review_input(memory.snapshot(), old["id"], as_of=as_of,
                    child_ids=[row["child_id"] for row in old["examples"]])
                except SafetyError: still_current = False; break
                if actual["state"] != "prepared" or canonical(actual["item"]) != canonical(old):
                    still_current = False; break
            if not still_current:
                failures.update({cid: {"reason": "FUNCTIONAL_DEFINITION_ORIGINAL_BATCH_INPUT_CHANGED"} for cid in ids}); continue
            if retry is not None and retry["ready"] and retry.get("requires_prepare", True):
                try:
                    guard = getattr(runner, "check_new_execution", None)
                    if callable(guard) and active_work_turn(memory) is not None:
                        guard()
                    prepare_closed_model_retry(memory, owner, as_of=as_of, expected_signal=retry["signal"],
                        retry_seconds=retry_seconds, not_before=retry_not_before)
                except (SafetyError, OSError) as error:
                    failures.update({cid: {"reason": str(error) if isinstance(error, SafetyError)
                        else "FUNCTIONAL_DEFINITION_ORIGINAL_FILE_WAIT"} for cid in ids}); continue
            selected_runner = runner
        try:
            _, result = jobs(selected_runner).run("B", request["payload"], declared_version=PROMPT_VERSION)
            outcomes.update({cid: result["accepted"][cid] for cid in ids if cid in result["accepted"]})
            failures.update({cid: {"reason": result["failures"][cid]} for cid in ids if cid in result["failures"]})
        except SafetyError as error: failures.update({cid: {"reason": str(error)} for cid in ids})
    if novel:
        selected = []
        for item in novel:
            if len(canonical(_payload(selected + [item]))) > MAX_INPUT_BYTES:
                failures[item["id"]] = {"reason": "FUNCTIONAL_DEFINITION_REVIEW_SIZE_LIMIT" if not selected
                    else "FUNCTIONAL_DEFINITION_REVIEW_BATCH_LIMIT"}
            else: selected.append(item)
        novel = selected
    if novel and runner is None:
        failures.update({item["id"]: {"reason": "AI_RUNNER_NOT_ENABLED"} for item in novel})
        novel = []
    if novel:
        try:
            _, result = jobs(runner).run("B", _payload(novel), declared_version=PROMPT_VERSION)
            outcomes.update(result["accepted"])
            failures.update({cid: {"reason": reason} for cid, reason in result["failures"].items()})
        except SafetyError as error: failures.update({item["id"]: {"reason": str(error)} for item in novel})
    for cid, result in outcomes.items():
        row = pending[cid]
        try:
            state = memory.snapshot(); owner = _owner(state, row["item"])
            verified = verify_definition_acceptance(memory, state, row, owner)
            proof = verified["proof"]
            if canonical(result) != canonical(proof["accepted_item"]):
                raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTED_RESULT_MISMATCH")
            if result["decision"] == "unclear":
                review = {"kind": CONTRACT, "job_proof": proof, "result": result}
                rid = key("functional-definition-review", review); review["record_hash"] = digest(review)
                if rid not in state["source_checks"]:
                    committed = memory.commit(key("save-functional-definition-review", rid), [change("source_checks", rid, review)],
                        validate=lambda latest, actual=proof: validate_current_definition_acceptance(memory, latest, actual))
                    record_saved_decision(committed, record_kind=CONTRACT, record_id=rid, concept_id=cid)
                failures[cid] = {"reason": "FUNCTIONAL_DEFINITION_UNCLEAR", "review_id": rid}
                continue
            refs = sorted({proof["binding"]["children"][child]["observation_id"] for child in result["references"]})
            decisions = {child["child_id"]: {"decision": child["decision"], "reason": child["reason"]}
                for child in result["example_decisions"]}
            saved = accept_functional_definition(memory, cid, result["definition"], basis_refs=refs,
                reviewer="bounded_B_definition_review", reviewed_at=max((as_of, proof["result_receipt"]["completed_at"]), key=timestamp),
                expected_meaning_hash=row["binding"]["expected_meaning_hash"], job_id=owner,
                example_decisions=decisions, model_acceptance=proof)
            accepted[cid] = {"state": "current", "concept_id": cid, **saved, "job_id": owner}
            failures.pop(cid, None)
        except SafetyError as error: failures[cid] = {"reason": str(error)}
        except OSError: failures[cid] = {"reason": "FUNCTIONAL_DEFINITION_REVIEW_SAVE_FAILED"}
    return {"accepted": accepted, "failures": failures, "prepared_count": len(prepared["prepared"]),
        "bindings": readiness["bindings"], "skipped_concept_ids": readiness["skipped_concept_ids"],
        "client_publication_attempted": False}
