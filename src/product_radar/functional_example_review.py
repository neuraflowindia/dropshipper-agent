"""Grounded, original-result-bound judgments of new examples of accepted types.

The type definition and identity association are immutable inputs. This module
records only same/different/unclear judgments; the existing membership writer
continues to attach observations and all actual offer facts stay in originals.
"""

from copy import deepcopy
import re

from .functional_definition_review import (_description, _dependency_signal, _hash, _text,
    validate_definition_item, MAX_EXAMPLES, MAX_INPUT_BYTES, MAX_RESULT_BYTES, MAX_PROOF_BYTES)
from .functional_types import MEANING_FIELDS, definition_meaning_hash, effective_functional_definition, validate_definition
from .models import SafetyError, canonical, digest, identifier, timestamp
from .operations import record_saved_decision
from .research_memory import change, key, load_event
from .storage import safe_path

CONTRACT = "functional-example-review-v1"
MODE = "functional_example_review"
PROMPT_VERSION = "research-b-functional-example-v1"
PROOF = "functional-example-original-acceptance-v1"
MAX_ITEMS = 5


def validate_example_item(item):
    fields = {"id", "concept_id", "meaning_hash", "definition", "category", "examples"}
    if not isinstance(item, dict) or set(item) != fields:
        raise SafetyError("AI_FUNCTIONAL_EXAMPLE_INPUT_INVALID")
    identifier(item["concept_id"])
    definition = item["definition"]
    if not isinstance(definition, dict) or set(definition) != set(MEANING_FIELDS):
        raise SafetyError("AI_FUNCTIONAL_EXAMPLE_INPUT_INVALID")
    checked = validate_definition(dict(definition, report_title="Accepted functional type"))
    if (canonical(definition) != canonical({field: checked[field] for field in MEANING_FIELDS})
            or item["meaning_hash"] != definition_meaning_hash(checked)):
        raise SafetyError("AI_FUNCTIONAL_EXAMPLE_MEANING_INVALID")
    validate_definition_item({"id": item["id"], "category": item["category"], "examples": item["examples"]})
    if item["id"] != key("functional-example-question", {k: v for k, v in item.items() if k != "id"}):
        raise SafetyError("AI_FUNCTIONAL_EXAMPLE_INPUT_INVALID")
    return item


def validate_example_payload(payload):
    if (not isinstance(payload, dict) or set(payload) != {"mode", "example_contract", "items"}
            or payload.get("mode") != MODE or payload.get("example_contract") != CONTRACT
            or not isinstance(payload.get("items"), list) or not 1 <= len(payload["items"]) <= MAX_ITEMS):
        raise SafetyError("AI_FUNCTIONAL_EXAMPLE_INPUT_INVALID")
    for item in payload["items"]: validate_example_item(item)
    if len({item["concept_id"] for item in payload["items"]}) != len(payload["items"]):
        raise SafetyError("AI_FUNCTIONAL_EXAMPLE_INPUT_INVALID")
    if len(canonical(payload)) > MAX_INPUT_BYTES:
        raise SafetyError("FUNCTIONAL_EXAMPLE_REVIEW_SIZE_LIMIT")
    return payload


def validate_example_result(item, result):
    validate_example_item(item)
    if (not isinstance(result, dict) or set(result) != {"id", "decisions", "reason"}
            or result.get("id") != item["id"] or not isinstance(result.get("decisions"), list)):
        raise SafetyError("AI_FUNCTIONAL_EXAMPLE_RESULT_INVALID")
    _text(result["reason"])
    known, decisions = {row["child_id"] for row in item["examples"]}, {}
    for row in result["decisions"]:
        if (not isinstance(row, dict) or set(row) != {"child_id", "decision", "reason"}
                or row.get("child_id") not in known or row["child_id"] in decisions
                or row.get("decision") not in {"same", "different", "unclear"}):
            raise SafetyError("AI_FUNCTIONAL_EXAMPLE_RESULT_INVALID")
        _text(row["reason"]); decisions[row["child_id"]] = row
    if set(decisions) != known:
        raise SafetyError("AI_FUNCTIONAL_EXAMPLE_RESULT_INVALID")
    normalized = {"id": result["id"], "decisions": [deepcopy(decisions[child]) for child in sorted(decisions)],
        "reason": result["reason"]}
    if len(canonical(normalized)) > MAX_RESULT_BYTES:
        raise SafetyError("FUNCTIONAL_EXAMPLE_REVIEW_SIZE_LIMIT")
    return normalized


def _payload(items): return {"mode": MODE, "example_contract": CONTRACT, "items": items}


def example_review_input(state, concept_id, child_ids, *, as_of):
    timestamp(as_of)
    meaning = effective_functional_definition(state, concept_id)
    if not meaning["accepted"]:
        raise SafetyError("FUNCTIONAL_EXAMPLE_ACCEPTED_MEANING_REQUIRED")
    from .source_coverage import _product_context
    _, excluded = _product_context(state, concept_id)
    if excluded: raise SafetyError("FUNCTIONAL_EXAMPLE_NOT_APPLICABLE")
    if (not isinstance(child_ids, list) or not 1 <= len(child_ids) <= MAX_EXAMPLES
            or child_ids != sorted(set(child_ids))):
        raise SafetyError("FUNCTIONAL_EXAMPLE_SCOPE_INVALID")
    examples, children = [], {}
    for child in child_ids:
        association = state.get("associations", {}).get(child)
        if (not isinstance(association, dict) or association.get("status") != "resolved"
                or association.get("concept_id") != concept_id):
            raise SafetyError("FUNCTIONAL_EXAMPLE_ASSOCIATION_CHANGED")
        semantic_id = association["semantic_id"]
        product, description, saved = _description(state, child, semantic_id)
        originals = [(oid, row) for oid, row in state.get("observations", {}).items()
            if row.get("semantic_id") == semantic_id and timestamp(row["observed_at"]) <= timestamp(as_of)]
        if not originals: raise SafetyError("FUNCTIONAL_EXAMPLE_ORIGINAL_OBSERVATION_REQUIRED")
        oid, observation = min(originals, key=lambda pair: (timestamp(pair[1]["observed_at"]), pair[0]))
        examples.append({"child_id": child, "semantic_id": semantic_id, "description": description})
        children[child] = {"semantic_id": semantic_id, "association_hash": digest(association),
            "description_hash": digest(product), "description_record_hash": digest(saved),
            "observation_id": oid, "observation_hash": digest(observation)}
    item = {"concept_id": concept_id, "meaning_hash": meaning["meaning_hash"],
        "definition": {field: deepcopy(meaning[field]) for field in MEANING_FIELDS},
        "category": state["concepts"][concept_id]["description"]["category"], "examples": examples}
    item["id"] = key("functional-example-question", item); validate_example_item(item)
    definition = state["source_checks"][meaning["definition_id"]]
    if timestamp(definition["reviewed_at"]) > timestamp(as_of):
        raise SafetyError("FUNCTIONAL_EXAMPLE_MEANING_AFTER_CUTOFF")
    binding = {"concept_id": concept_id, "meaning_hash": meaning["meaning_hash"], "item_hash": digest(item),
        "definition_id": meaning["definition_id"], "definition_hash": digest(definition), "children": children,
        "prepared_at": as_of}
    return {"concept_id": concept_id, "item": item, "binding": binding}


def _validate_binding(state, item, binding, *, current=True):
    fields = {"concept_id", "meaning_hash", "item_hash", "definition_id", "definition_hash", "children", "prepared_at"}
    if (not isinstance(binding, dict) or set(binding) != fields
            or binding["concept_id"] != item["concept_id"] or binding["meaning_hash"] != item["meaning_hash"]
            or binding["item_hash"] != digest(item) or not isinstance(binding["children"], dict)
            or not binding["children"] or not set(binding["children"]) <= {row["child_id"] for row in item["examples"]}):
        raise SafetyError("FUNCTIONAL_EXAMPLE_BINDING_INVALID")
    _hash(binding["definition_hash"]); identifier(binding["definition_id"]); timestamp(binding["prepared_at"])
    examples = {row["child_id"]: row for row in item["examples"]}
    for child, row in binding["children"].items():
        if (not isinstance(row, dict) or set(row) != {"semantic_id", "association_hash", "description_hash",
                "description_record_hash", "observation_id", "observation_hash"}
                or row["semantic_id"] != examples[child]["semantic_id"]):
            raise SafetyError("FUNCTIONAL_EXAMPLE_BINDING_INVALID")
        identifier(row["observation_id"])
        for field in ("association_hash", "description_hash", "description_record_hash", "observation_hash"): _hash(row[field])
    if current:
        actual = example_review_input(state, item["concept_id"], sorted(binding["children"]), as_of=binding["prepared_at"])
        expected_item = dict(item, examples=[row for row in item["examples"] if row["child_id"] in binding["children"]])
        expected_item["id"] = key("functional-example-question", {k: v for k, v in expected_item.items() if k != "id"})
        actual_binding = dict(actual["binding"], item_hash=digest(item))
        if actual["item"] != expected_item or actual_binding != binding:
            raise SafetyError("FUNCTIONAL_EXAMPLE_INPUT_CHANGED")
        explicit = state["source_checks"][binding["definition_id"]]["example_decisions"]
        for child, saved in binding["children"].items():
            judgment = explicit.get(child)
            if (judgment and judgment.get("association_hash") == saved["association_hash"]
                    and judgment.get("description_hash") == saved["description_hash"]):
                raise SafetyError("FUNCTIONAL_EXAMPLE_JUDGMENT_ALREADY_REVIEWED")
    return binding


def _original_job(state, jid, item):
    from .research_ai import MODEL, _item_bindings
    try:
        job = state["jobs"][jid]; request = job["request"]; payload = validate_example_payload(request["payload"])
        if (set(request) != {"role", "payload", "model", "prompt_version", "runner"}
                or request["role"] != "B" or request["model"] != MODEL or request["prompt_version"] != PROMPT_VERSION
                or not isinstance(request["runner"], str) or not request["runner"] or key("job", request) != jid
                or job["binding_keys"] != _item_bindings("B", payload, MODEL, PROMPT_VERSION, request["runner"])
                or [old for old in payload["items"] if old["id"] == item["id"]] != [item]):
            raise SafetyError("FUNCTIONAL_EXAMPLE_ORIGINAL_JOB_INVALID")
    except (KeyError, TypeError, AttributeError):
        raise SafetyError("FUNCTIONAL_EXAMPLE_ORIGINAL_JOB_INVALID") from None
    return job


def _owners(state, concept_id, meaning_hash):
    result = []
    for jid, job in state.get("jobs", {}).items():
        request = job.get("request", {})
        if request.get("role") != "B" or request.get("prompt_version") != PROMPT_VERSION: continue
        for item in request.get("payload", {}).get("items", []):
            if item.get("concept_id") == concept_id and item.get("meaning_hash") == meaning_hash:
                _original_job(state, jid, item); result.append((jid, item))
    return sorted(result, key=lambda pair: (pair[0], pair[1]["id"]))


def validate_example_acceptance_proof(proof):
    from .research_ai import validate_results
    fields = {"contract", "job_id", "original_job", "attempt", "result_fingerprint", "item", "binding", "request_hash",
        "binding_key", "result_receipt", "result_hash", "accepted_item", "accepted_item_hash"}
    if not isinstance(proof, dict) or set(proof) != fields or proof.get("contract") != PROOF:
        raise SafetyError("FUNCTIONAL_EXAMPLE_ACCEPTANCE_PROOF_INVALID")
    if len(canonical(proof)) > MAX_PROOF_BYTES: raise SafetyError("FUNCTIONAL_EXAMPLE_REVIEW_SIZE_LIMIT")
    item = validate_example_item(proof["item"]); _validate_binding({}, item, proof["binding"], current=False)
    job = _original_job({"jobs": {proof["job_id"]: proof["original_job"]}}, proof["job_id"], item)
    request, receipt = job["request"], proof["result_receipt"]
    if (not isinstance(proof["attempt"], str) or not re.fullmatch(r"initial|retry-(?:[1-9]|1[0-9]|20)", proof["attempt"])
            or not isinstance(proof["result_fingerprint"], list) or len(proof["result_fingerprint"]) != 5
            or any(type(value) is not int or value < 0 for value in proof["result_fingerprint"])
            or proof["request_hash"] != digest(request) or proof["binding_key"] != job["binding_keys"][item["id"]]
            or not isinstance(receipt, dict) or set(receipt) != {"request_hash", "response", "usage", "completed_at"}
            or receipt["request_hash"] != digest(request) or proof["result_hash"] != digest(receipt)):
        raise SafetyError("FUNCTIONAL_EXAMPLE_ACCEPTANCE_PROOF_INVALID")
    timestamp(receipt["completed_at"])
    accepted = validate_results("B", request["payload"], receipt["response"], declared_version=PROMPT_VERSION)["accepted"].get(item["id"])
    if accepted is None or accepted != proof["accepted_item"] or digest(accepted) != proof["accepted_item_hash"]:
        raise SafetyError("FUNCTIONAL_EXAMPLE_ACCEPTED_RESULT_MISMATCH")
    return deepcopy(accepted)


def _record_id(record):
    proof = record["job_proof"]
    return key("functional-example-review", {"concept_id": record["concept_id"], "meaning_hash": record["meaning_hash"],
        "binding": {k: v for k, v in record["binding"].items() if k != "prepared_at"},
        "job_id": record["job_id"], "request_hash": proof["request_hash"],
        "result_hash": proof["result_hash"], "accepted_item_hash": proof["accepted_item_hash"]})


def validate_example_review(record_id, record):
    fields = {"kind", "concept_id", "meaning_hash", "binding", "job_id", "job_proof", "result", "reviewed_at", "record_hash"}
    if (not isinstance(record, dict) or set(record) != fields or record.get("kind") != CONTRACT
            or record.get("record_hash") != digest({k: v for k, v in record.items() if k != "record_hash"})):
        raise SafetyError("FUNCTIONAL_EXAMPLE_REVIEW_RECORD_INVALID")
    accepted = validate_example_acceptance_proof(record["job_proof"]); proof = record["job_proof"]
    if (record_id != _record_id(record) or record["result"] != accepted or record["job_id"] != proof["job_id"]
            or record["binding"] != proof["binding"] or record["concept_id"] != proof["item"]["concept_id"]
            or record["meaning_hash"] != proof["item"]["meaning_hash"]
            or timestamp(record["reviewed_at"]) < timestamp(proof["result_receipt"]["completed_at"])
            or timestamp(record["reviewed_at"]) < timestamp(record["binding"]["prepared_at"])):
        raise SafetyError("FUNCTIONAL_EXAMPLE_REVIEW_RECORD_INVALID")
    return record


def current_example_judgments(state, concept_id, meaning_hash, *, as_of=None):
    """Current scoped judgments only; unknown rows never become positive votes."""
    output = {}
    if as_of is not None: timestamp(as_of)
    meaning = effective_functional_definition(state, concept_id)
    explicit = state["source_checks"][meaning["definition_id"]]["example_decisions"] if meaning["accepted"] else {}
    for rid, record in state.get("source_checks", {}).items():
        if record.get("kind") != CONTRACT: continue
        validate_example_review(rid, record)
        if record["concept_id"] != concept_id or record["meaning_hash"] != meaning_hash: continue
        if as_of is not None and timestamp(record["reviewed_at"]) > timestamp(as_of): continue
        for answer in record["result"]["decisions"]:
            child = answer["child_id"]
            if child not in record["binding"]["children"]: continue
            bound = record["binding"]["children"][child]
            association = state.get("associations", {}).get(child)
            try: product, _, saved_description = _description(state, child, bound["semantic_id"])
            except SafetyError: continue
            original_observation = state.get("observations", {}).get(bound["observation_id"])
            if (not association or association.get("concept_id") != concept_id or association.get("status") != "resolved"
                    or digest(association) != bound["association_hash"] or digest(product) != bound["description_hash"]
                    or digest(saved_description) != bound["description_record_hash"] or not original_observation
                    or original_observation.get("semantic_id") != bound["semantic_id"]
                    or digest(original_observation) != bound["observation_hash"]): continue
            original = explicit.get(child)
            if (original and original.get("association_hash") == bound["association_hash"]
                    and original.get("description_hash") == bound["description_hash"]):
                # This automatic lane does not revise an explicit definition
                # judgment, including a deliberately different/unclear one.
                continue
            value = {"decision": answer["decision"], "reason": " ".join(answer["reason"].split()),
                "association_hash": bound["association_hash"], "description_hash": bound["description_hash"],
                "review_id": rid, "review_hash": record["record_hash"]}
            old = output.get(child)
            if old and any(old[field] != value[field] for field in ("decision", "reason", "association_hash", "description_hash")):
                raise SafetyError("FUNCTIONAL_EXAMPLE_REVIEW_CONFLICT")
            if old is None or rid < old["review_id"]: output[child] = value
    return output


def validate_membership_example_review(member, review, *, as_of=None):
    """Portable proof for a membership, independent of mutable Jobs files."""
    ref = member.get("example_review")
    if not isinstance(ref, dict) or set(ref) != {"id", "record_hash"}:
        raise SafetyError("FUNCTIONAL_EXAMPLE_MEMBERSHIP_PROOF_INVALID")
    validate_example_review(ref["id"], review)
    proof = review["job_proof"]; child = member.get("child_id")
    bound = review["binding"]["children"].get(child)
    answers = [row for row in review["result"]["decisions"] if row["child_id"] == child]
    if (ref["record_hash"] != review["record_hash"] or review["concept_id"] != member.get("concept_id")
            or review["meaning_hash"] != member.get("meaning_hash") or not bound or len(answers) != 1
            or answers[0]["decision"] != "same" or member.get("method") != "bounded_B_example_review"
            or member.get("semantic_id") != bound["semantic_id"] or member.get("association_hash") != bound["association_hash"]
            or member.get("reason") != " ".join(answers[0]["reason"].split())
            or member.get("observation_hashes", {}).get(bound["observation_id"]) != bound["observation_hash"]
            or timestamp(member.get("prepared_at")) < timestamp(review["reviewed_at"])
            or as_of is not None and timestamp(review["reviewed_at"]) > timestamp(as_of)):
        raise SafetyError("FUNCTIONAL_EXAMPLE_MEMBERSHIP_PROOF_INVALID")
    return review


def _application(state, item, children, *, as_of):
    row = example_review_input(state, item["concept_id"], sorted(children), as_of=as_of)
    expected = dict(item, examples=[example for example in item["examples"] if example["child_id"] in children])
    expected["id"] = key("functional-example-question", {k: v for k, v in expected.items() if k != "id"})
    if row["item"] != expected: raise SafetyError("FUNCTIONAL_EXAMPLE_INPUT_CHANGED")
    row["item"] = deepcopy(item); row["binding"]["item_hash"] = digest(item)
    return row


def _file_signal(memory, jid, *, as_of, retry_seconds=300, retry_not_before=None, state=None):
    from .research_ai import current_attempt, closed_model_retry_hint, prepared_closed_model_retry_hint
    base = safe_path(memory.root, "ai_jobs", jid); folder = current_attempt(base)
    files = []
    for name in ("intent.json", "completion.json", "result.json", "runner_execution.json", "execution_failure.json", "failure.json"):
        try:
            status = (folder / name).lstat()
            info = [status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns]
        except FileNotFoundError: info = None
        files.append([name, info])
    present = {name for name, info in files if info is not None}
    local = bool(present & {"completion.json", "result.json"})
    ready = local or not bool(present & {"intent.json", "runner_execution.json", "execution_failure.json", "failure.json"})
    retry = None
    if not local:
        try:
            checked_empty = folder != base and not present
            reader = prepared_closed_model_retry_hint if checked_empty else closed_model_retry_hint
            retry = reader(memory, jid, as_of=as_of,
                retry_seconds=retry_seconds, not_before=retry_not_before, state=state)
            if checked_empty:
                # An empty retry folder does not erase the actual checked
                # pointer's delay, or turn a manual pointer into automatic
                # current-input recovery authority.
                ready = retry["ready"]
        except (SafetyError, OSError) as error:
            # This original owns only its actual children. A damaged/missing
            # terminal proof cannot authorise its retry, or stop a disjoint
            # new example of the same type from continuing.
            retry = {"ready": False, "reason": str(error) if isinstance(error, SafetyError)
                else "FUNCTIONAL_EXAMPLE_ORIGINAL_FILE_WAIT", "next_retry_at": None,
                "signal": digest({"job_id": jid, "files": files, "reason": str(error)})}
            ready = False
    return {"job_id": jid, "attempt": "initial" if folder == base else folder.name, "files": files,
        "local": local, "ready": ready or retry is not None and retry["ready"],
        **({"closed_retry": retry, "reason": retry["reason"]} if retry is not None else {})}


def _candidates(memory, state, cid, *, as_of, retry_seconds=300, retry_not_before=None):
    from .functional_types import example_membership_preparation
    meaning = effective_functional_definition(state, cid)
    if not meaning["accepted"]: raise SafetyError("FUNCTIONAL_EXAMPLE_ACCEPTED_MEANING_REQUIRED")
    preparation = example_membership_preparation(state, cid)
    reviewed = current_example_judgments(state, cid, meaning["meaning_hash"], as_of=as_of)
    all_reviewed = current_example_judgments(state, cid, meaning["meaning_hash"])
    future = {child: {"review_id": row["review_id"], "due_at": state["source_checks"][row["review_id"]]["reviewed_at"]}
        for child, row in all_reviewed.items()
        if timestamp(state["source_checks"][row["review_id"]]["reviewed_at"]) > timestamp(as_of)}
    if future: preparation = dict(preparation, next_retry_at=min((row["due_at"] for row in future.values()), key=timestamp))
    known = dict(state["source_checks"][meaning["definition_id"]]["example_decisions"]); known.update(reviewed)
    pending = []
    for child in preparation["pending_child_ids"]:
        if child in future: continue
        association = state["associations"][child]
        product, _, _ = _description(state, child, association["semantic_id"])
        judgment = known.get(child)
        if (judgment and judgment.get("association_hash") == digest(association)
                and judgment.get("description_hash") == digest(product)): continue
        pending.append(child)
    if not pending:
        return [], {"meaning_hash": meaning["meaning_hash"], "pending_children": preparation["pending_child_ids"],
                    "current_reviews": {child: row["review_id"] for child, row in reviewed.items()}, "future_reviews": future}, preparation
    owned, groups, originals = set(), [], []
    for jid, item in _owners(state, cid, meaning["meaning_hash"]):
        children = []
        for example in item["examples"]:
            child = example["child_id"]
            if child not in pending: continue
            try: actual = example_review_input(state, cid, [child], as_of=as_of)
            except SafetyError: continue
            if actual["item"]["examples"] == [example]: children.append(child)
        if not children: continue
        if owned & set(children): raise SafetyError("FUNCTIONAL_EXAMPLE_ORIGINAL_OWNER_CONFLICT")
        owned.update(children)
        row = _application(state, item, children, as_of=as_of)
        hint = _file_signal(memory, jid, as_of=as_of, retry_seconds=retry_seconds,
                            retry_not_before=retry_not_before, state=state)
        if hint["ready"] and not hint["local"]:
            for old in state["jobs"][jid]["request"]["payload"]["items"]:
                try:
                    current = example_review_input(state, old["concept_id"],
                        [example["child_id"] for example in old["examples"]], as_of=as_of)
                    valid = current["item"] == old
                except SafetyError: valid = False
                if not valid:
                    hint.update(ready=False, reason="FUNCTIONAL_EXAMPLE_ORIGINAL_BATCH_INPUT_CHANGED")
                    break
        signal = {**hint, "item_hash": digest(item),
            "binding": {k: v for k, v in row["binding"].items() if k != "prepared_at"},
            "original_batch": {old["concept_id"]: _dependency_signal(state, old["concept_id"])
                for old in state["jobs"][jid]["request"]["payload"]["items"]}}
        originals.append(signal); groups.append({**row, "owner": jid, "local": hint["local"], "ready": hint["ready"],
            **({"closed_retry": hint["closed_retry"]} if hint.get("closed_retry") is not None else {}),
            **({"reason": hint["reason"]} if hint.get("reason") else {})})
    unowned = sorted(set(pending) - owned)
    unprepared = {}
    if unowned:
        chosen, packed = [], None
        for child in unowned:
            if len(chosen) >= MAX_EXAMPLES: break
            try:
                trial = example_review_input(state, cid, chosen + [child], as_of=as_of)
                validate_example_payload(_payload([trial["item"]]))
            except SafetyError as error:
                unprepared[child] = str(error); continue
            chosen.append(child); packed = trial
        if packed is not None:
            groups.append({**packed, "owner": None, "local": False, "ready": True})
        elif not groups:
            preparation = dict(preparation, unprepared_reason=next(iter(unprepared.values())))
    signal = {"meaning_hash": meaning["meaning_hash"], "pending": pending, "future_reviews": future,
        "unprepared_children": unprepared,
        "groups": [{"item": row["item"], "binding": {k: v for k, v in row["binding"].items() if k != "prepared_at"}}
            for row in groups], "originals": originals}
    return sorted(groups, key=lambda row: (not row["local"], not row["ready"], row["item"]["id"])), signal, preparation


def functional_example_readiness(memory, concept_ids, *, as_of, limit=MAX_ITEMS, skip_concept_bindings=None,
                                retry_seconds=300, retry_not_before=None, state=None):
    """Pure hints from fresh or enclosing verified state; native files stay live."""
    if (not isinstance(concept_ids, (list, tuple)) or any(not isinstance(cid, str) for cid in concept_ids)
            or len(set(concept_ids)) != len(concept_ids) or type(limit) is not int or not 1 <= limit <= MAX_ITEMS
            or skip_concept_bindings is not None and (not isinstance(skip_concept_bindings, dict)
                or any(not isinstance(cid, str) or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                    for cid, value in skip_concept_bindings.items()))):
        raise SafetyError("FUNCTIONAL_EXAMPLE_SCOPE_INVALID")
    timestamp(as_of)
    state = memory.snapshot() if state is None else state
    skipped = skip_concept_bindings or {}
    bindings, waiting, current, selected, skipped_ids, waits, retry_hints = {}, {}, {}, [], [], [], {}
    for cid in concept_ids:
        try:
            groups, signal, preparation = _candidates(memory, state, cid, as_of=as_of,
                retry_seconds=retry_seconds, retry_not_before=retry_not_before)
            bindings[cid] = digest(signal)
            for group in groups:
                retry = group.get("closed_retry")
                if retry is not None:
                    retry_hints[group["owner"]] = retry
                    if retry["next_retry_at"] is not None and timestamp(retry["next_retry_at"]) > timestamp(as_of):
                        waits.append(retry["next_retry_at"])
            if preparation.get("next_retry_at"): waits.append(preparation["next_retry_at"])
            if not groups:
                if preparation.get("unprepared_reason"):
                    waiting[cid] = {"reason": preparation["unprepared_reason"]}
                    if skipped.get(cid) == bindings[cid]: skipped_ids.append(cid)
                    continue
                if preparation.get("next_retry_at"):
                    waiting[cid] = {"reason": "FUNCTIONAL_EXAMPLE_REVIEW_AFTER_CUTOFF", "next_retry_at": preparation["next_retry_at"]}
                    if skipped.get(cid) == bindings[cid]: skipped_ids.append(cid)
                    continue
                current[cid] = {"state": "current", "concept_id": cid, "meaning_hash": preparation["meaning_hash"],
                    "pending_child_ids": preparation["pending_child_ids"]}
                continue
            if skipped.get(cid) == bindings[cid]: skipped_ids.append(cid); continue
            ready = next((row for row in groups if row["ready"]), None)
            if ready is None: waiting[cid] = {"reason": groups[0].get("reason", "AI_JOB_PENDING_RECONCILIATION")}
            else: selected.append(ready)
        except (SafetyError, OSError) as error:
            waiting[cid] = {"reason": str(error) if isinstance(error, SafetyError) else "FUNCTIONAL_EXAMPLE_ORIGINAL_FILE_WAIT"}
            bindings[cid] = digest({"dependencies": _dependency_signal(state, cid), "waiting": waiting[cid]})
            if skipped.get(cid) == bindings[cid]: skipped_ids.append(cid)
    selected.sort(key=lambda row: (not row["local"], concept_ids.index(row["concept_id"])))
    selected = selected[:limit]
    local = [row["concept_id"] for row in selected if row["local"]]
    model = [row["concept_id"] for row in selected if not row["local"]]
    return {"ready": bool(selected), "local_ready": bool(local), "model_ready": bool(model),
        "local_ready_concept_ids": local, "model_ready_concept_ids": model,
        "ready_concept_ids": [row["concept_id"] for row in selected], "prepared": selected,
        "current": current, "waiting": waiting, "bindings": bindings, "skipped_concept_ids": skipped_ids,
        "retry_hints": retry_hints,
        "waits": sorted(set(waits), key=timestamp), "next_retry_at": min(waits, key=timestamp) if waits else None,
        "signal": digest({"bindings": bindings, "waiting": waiting, "current": current})}


def _verify_current_proof(memory, state, proof):
    from .research_ai import current_attempt, MAX_JOB_BYTES
    validate_example_acceptance_proof(proof); _validate_binding(state, proof["item"], proof["binding"])
    job = _original_job(state, proof["job_id"], proof["item"])
    if any(job.get(field) != proof["original_job"].get(field) for field in ("request", "binding_keys")):
        raise SafetyError("FUNCTIONAL_EXAMPLE_ORIGINAL_RESULT_CHANGED")
    base = safe_path(memory.root, "ai_jobs", proof["job_id"]); folder = current_attempt(base)
    if ("initial" if folder == base else folder.name) != proof["attempt"]:
        raise SafetyError("FUNCTIONAL_EXAMPLE_ORIGINAL_RESULT_CHANGED")
    path = folder / "result.json"
    try:
        before = path.lstat(); receipt = load_event(path, MAX_JOB_BYTES); after = path.lstat()
    except OSError: raise SafetyError("FUNCTIONAL_EXAMPLE_ORIGINAL_RESULT_UNAVAILABLE") from None
    fingerprint = lambda stat: [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    if (fingerprint(before) != proof["result_fingerprint"] or fingerprint(after) != fingerprint(before)
            or receipt != proof["result_receipt"] or current_attempt(base) != folder):
        raise SafetyError("FUNCTIONAL_EXAMPLE_ORIGINAL_RESULT_CHANGED")


def _acceptance_proof(memory, state, row, jid):
    from .research_ai import current_attempt, MAX_JOB_BYTES, validate_results
    job = _original_job(state, jid, row["item"])
    base = safe_path(memory.root, "ai_jobs", jid); folder = current_attempt(base); path = folder / "result.json"
    try:
        before = path.lstat(); receipt = load_event(path, MAX_JOB_BYTES); after = path.lstat()
    except OSError: raise SafetyError("FUNCTIONAL_EXAMPLE_ORIGINAL_RESULT_UNAVAILABLE") from None
    fingerprint = lambda stat: [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    if fingerprint(before) != fingerprint(after): raise SafetyError("FUNCTIONAL_EXAMPLE_ORIGINAL_RESULT_CHANGED")
    accepted = validate_results("B", job["request"]["payload"], receipt["response"], declared_version=PROMPT_VERSION)["accepted"].get(row["item"]["id"])
    if accepted is None: raise SafetyError("FUNCTIONAL_EXAMPLE_ORIGINAL_RESULT_NOT_ACCEPTED")
    proof = {"contract": PROOF, "job_id": jid, "original_job": deepcopy(job),
        "attempt": "initial" if folder == base else folder.name, "result_fingerprint": fingerprint(after),
        "item": deepcopy(row["item"]), "binding": deepcopy(row["binding"]), "request_hash": digest(job["request"]),
        "binding_key": job["binding_keys"][row["item"]["id"]], "result_receipt": receipt,
        "result_hash": digest(receipt), "accepted_item": accepted, "accepted_item_hash": digest(accepted)}
    _verify_current_proof(memory, state, proof)
    return proof


class _SavedRunner:
    def __init__(self, identity): self.identity = identity
    def __call__(self, role, payload, folder): raise SafetyError("AI_JOB_PENDING_RECONCILIATION")


def review_functional_examples(memory, concept_ids, runner=None, *, as_of, limit=MAX_ITEMS, skip_concept_bindings=None,
                               retry_seconds=300, retry_not_before=None, read_state=None):
    """``read_state`` (optional) returns the current research state; a caller
    may pass one that keeps its copy until something is saved.
    """
    from .model_recovery import run_current_model_job
    read = read_state or memory.snapshot
    ready = functional_example_readiness(memory, concept_ids, as_of=as_of, limit=limit,
        skip_concept_bindings=skip_concept_bindings, retry_seconds=retry_seconds, retry_not_before=retry_not_before,
        state=read())
    accepted, failures, outcomes = dict(ready["current"]), dict(ready["waiting"]), {}
    pending = {row["item"]["id"]: row for row in ready["prepared"]}
    state = read(); originals, novel = {}, []
    for row in ready["prepared"]:
        if row["owner"]: originals.setdefault(row["owner"], []).append(row)
        else: novel.append(row)
    for jid, rows in originals.items():
        request = deepcopy(state["jobs"][jid]["request"]); chosen_runner = _SavedRunner(request["runner"])
        if not rows[0]["local"]:
            if runner is None or getattr(runner, "identity", "codex-chatgpt-high-v1") != request["runner"]:
                failures.update({row["concept_id"]: {"reason": "AI_RUNNER_NOT_ENABLED"} for row in rows}); continue
            unchanged = True
            for item in request["payload"]["items"]:
                try:
                    current = example_review_input(memory.snapshot(), item["concept_id"],
                        [example["child_id"] for example in item["examples"]], as_of=as_of)
                    if current["item"] != item: unchanged = False; break
                except SafetyError: unchanged = False; break
            if not unchanged:
                failures.update({row["concept_id"]: {"reason": "FUNCTIONAL_EXAMPLE_ORIGINAL_BATCH_INPUT_CHANGED"} for row in rows}); continue
            chosen_runner = runner
        try:
            _, result = run_current_model_job(memory, chosen_runner, "B", request["payload"],
                declared_version=PROMPT_VERSION, as_of=as_of, retry_seconds=retry_seconds,
                retry_not_before=retry_not_before)
            for row in rows:
                item_id = row["item"]["id"]
                if item_id in result["accepted"]: outcomes[item_id] = (jid, result["accepted"][item_id])
                elif item_id in result["failures"]: failures[row["concept_id"]] = {"reason": result["failures"][item_id]}
        except SafetyError as error: failures.update({row["concept_id"]: {"reason": str(error)} for row in rows})
    packed = []
    for row in novel:
        if len(canonical(_payload([old["item"] for old in packed] + [row["item"]]))) > MAX_INPUT_BYTES:
            failures[row["concept_id"]] = {"reason": "FUNCTIONAL_EXAMPLE_REVIEW_BATCH_LIMIT"}; continue
        packed.append(row)
    if packed and runner is None:
        failures.update({row["concept_id"]: {"reason": "AI_RUNNER_NOT_ENABLED"} for row in packed}); packed = []
    if packed:
        try:
            jid, result = run_current_model_job(memory, runner, "B", _payload([row["item"] for row in packed]),
                declared_version=PROMPT_VERSION, as_of=as_of, retry_seconds=retry_seconds,
                retry_not_before=retry_not_before)
            for row in packed:
                item_id = row["item"]["id"]
                if item_id in result["accepted"]: outcomes[item_id] = (jid, result["accepted"][item_id])
                elif item_id in result["failures"]: failures[row["concept_id"]] = {"reason": result["failures"][item_id]}
        except SafetyError as error: failures.update({row["concept_id"]: {"reason": str(error)} for row in packed})
    for item_id, (jid, answer) in outcomes.items():
        row = pending[item_id]; cid = row["concept_id"]
        try:
            state = memory.snapshot(); proof = _acceptance_proof(memory, state, row, jid)
            if proof["accepted_item"] != answer: raise SafetyError("FUNCTIONAL_EXAMPLE_ACCEPTED_RESULT_MISMATCH")
            record = {"kind": CONTRACT, "concept_id": cid, "meaning_hash": row["item"]["meaning_hash"],
                "binding": row["binding"], "job_id": jid, "job_proof": proof, "result": answer,
                "reviewed_at": max((as_of, proof["result_receipt"]["completed_at"]), key=timestamp)}
            rid = _record_id(record); record["record_hash"] = digest(record); validate_example_review(rid, record)
            if rid not in state["source_checks"]:
                committed = memory.commit(key("save-functional-example-review", rid), [change("source_checks", rid, record)],
                    validate=lambda latest, actual=proof: _verify_current_proof(memory, latest, actual))
                record_saved_decision(committed, record_kind=CONTRACT, record_id=rid, concept_id=cid)
            else: validate_example_review(rid, state["source_checks"][rid])
            accepted[cid] = {"state": "reviewed", "concept_id": cid, "review_id": rid,
                "judged_child_ids": sorted(row["binding"]["children"]), "job_id": jid}
            failures.pop(cid, None)
        except SafetyError as error: failures[cid] = {"reason": str(error)}
        except OSError: failures[cid] = {"reason": "FUNCTIONAL_EXAMPLE_REVIEW_SAVE_FAILED"}
    return {"accepted": accepted, "failures": failures, "prepared_count": len(ready["prepared"]),
        "bindings": ready["bindings"], "skipped_concept_ids": ready["skipped_concept_ids"],
        "client_publication_attempted": False}
