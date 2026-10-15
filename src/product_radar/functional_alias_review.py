"""Bounded semantic review of real accepted narrow types.

Reviewing does not merge concepts or transfer their evidence. The separate
functional_aliases acceptance path owns application and release-conflict checks.
Initial model proof is read from the original saved Jobs result; a self-hashed
caller capsule is not permission to manufacture an accepted model decision.
"""

from copy import deepcopy
import re

from .functional_aliases import MAX_MEMBERS, _validate_input, prepare_alias_input
from .functional_types import MEANING_FIELDS, definition_meaning_hash, validate_definition
from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .operations import record_saved_decision
from .research_memory import change, key
from .service import CATEGORIES
from .storage import load_json, safe_path

CONTRACT = "functional-alias-review-v1"
MODE = "functional_alias_review"
PROMPT_VERSION = "research-b-functional-alias-v1"
PROOF = "functional-alias-job-proof-v1"
REVIEW_KIND = "functional-alias-review-v1"
MAX_GROUPS = 10


def _fingerprint(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def validate_alias_item(item):
    if (not isinstance(item, dict) or set(item) != {"id", "category", "members"}
            or item.get("category") not in CATEGORIES or not isinstance(item["members"], list)
            or not 2 <= len(item["members"]) <= MAX_MEMBERS):
        raise SafetyError("AI_FUNCTIONAL_ALIAS_INPUT_INVALID")
    ids = []
    for member in item["members"]:
        if not isinstance(member, dict) or set(member) != {"concept_id", "meaning_hash", "definition"}:
            raise SafetyError("AI_FUNCTIONAL_ALIAS_INPUT_INVALID")
        identifier(member["concept_id"])
        if not isinstance(member["definition"], dict) or set(member["definition"]) != set(MEANING_FIELDS):
            raise SafetyError("AI_FUNCTIONAL_ALIAS_INPUT_INVALID")
        checked = validate_definition(dict(member["definition"], report_title="Accepted functional type"))
        if (canonical({field: checked[field] for field in MEANING_FIELDS}) != canonical(member["definition"])
                or member["meaning_hash"] != definition_meaning_hash(checked)):
            raise SafetyError("AI_FUNCTIONAL_ALIAS_MEANING_INVALID")
        ids.append(member["concept_id"])
    if ids != sorted(set(ids)) or item["id"] != key("functional-alias-review", {k: v for k, v in item.items() if k != "id"}):
        raise SafetyError("AI_FUNCTIONAL_ALIAS_INPUT_INVALID")
    return item


def validate_alias_payload(payload):
    if (not isinstance(payload, dict) or set(payload) != {"mode", "alias_contract", "items"}
            or payload["mode"] != MODE or payload["alias_contract"] != CONTRACT
            or not isinstance(payload["items"], list) or not 1 <= len(payload["items"]) <= MAX_GROUPS):
        raise SafetyError("AI_FUNCTIONAL_ALIAS_INPUT_INVALID")
    for item in payload["items"]:
        validate_alias_item(item)
    if len({item["id"] for item in payload["items"]}) != len(payload["items"]):
        raise SafetyError("AI_FUNCTIONAL_ALIAS_INPUT_INVALID")
    return payload


def semantic_alias_item(state, prepared):
    """Literal meaning only; display corrections/review clocks cost no new call."""
    _validate_input(state, prepared)
    members = []
    for cid in prepared["member_ids"]:
        binding = prepared["definition_bindings"][cid]
        record = state["source_checks"][binding["definition_id"]]
        members.append({"concept_id": cid, "meaning_hash": binding["meaning_hash"],
                        "definition": {field: deepcopy(record["definition"][field]) for field in MEANING_FIELDS}})
    item = {"category": prepared["category"], "members": members}
    item["id"] = key("functional-alias-review", item)
    return validate_alias_item(item)


def prepare_functional_alias_review(memory, member_ids, *, reviewed_at):
    state = memory.snapshot()
    prepared = prepare_alias_input(state, member_ids, reviewed_at=reviewed_at)
    item = semantic_alias_item(state, prepared)
    return {"input": prepared, "item": item,
            "exact_meaning": len({m["meaning_hash"] for m in item["members"]}) == 1,
            "payload": {"mode": MODE, "alias_contract": CONTRACT, "items": [item]}}


def _original_job(state, job_id, item):
    """A real original job, not an aggregate derived_result or status shortcut."""
    from .research_ai import _item_bindings
    identifier(job_id)
    job = state.get("jobs", {}).get(job_id)
    try:
        request, bindings = job["request"], job["binding_keys"]
        if (set(request) != {"role", "payload", "model", "prompt_version", "runner"}
                or request["role"] != "B" or request["prompt_version"] != PROMPT_VERSION
                or not isinstance(request["model"], str) or not request["model"]
                or not isinstance(request["runner"], str) or not request["runner"]
                or key("job", request) != job_id):
            raise SafetyError("FUNCTIONAL_ALIAS_JOB_INVALID")
        payload = validate_alias_payload(request["payload"])
        expected = _item_bindings("B", payload, request["model"], PROMPT_VERSION, request["runner"])
        if canonical(bindings) != canonical(expected):
            raise SafetyError("FUNCTIONAL_ALIAS_JOB_INVALID")
        matching = [row for row in payload["items"] if row["id"] == item["id"]]
        if len(matching) != 1 or canonical(matching[0]) != canonical(item):
            raise SafetyError("FUNCTIONAL_ALIAS_JOB_INPUT_MISMATCH")
    except (KeyError, TypeError, AttributeError):
        raise SafetyError("FUNCTIONAL_ALIAS_JOB_INVALID") from None
    return job, request, bindings[item["id"]]


def validate_alias_job_proof(state, prepared, proof):
    """Pure historical projection, after initial file-backed acceptance.

    The original job request/bindings remain exact; later mutable admission
    bookkeeping is not a change to the accepted semantic result.
    """
    from .research_ai import validate_results
    fields = {"contract", "job_id", "item_id", "job_hash", "original_job", "request_hash", "binding_key",
              "result_receipt", "result_hash", "accepted_item", "accepted_item_hash"}
    if not isinstance(proof, dict) or set(proof) != fields or proof["contract"] != PROOF:
        raise SafetyError("FUNCTIONAL_ALIAS_JOB_PROOF_INVALID")
    item = semantic_alias_item(state, prepared)
    job, request, binding = _original_job(state, proof["job_id"], item)
    original = proof["original_job"]
    if (not isinstance(original, dict) or digest(original) != proof["job_hash"]
            or canonical(original.get("request")) != canonical(request)
            or canonical(original.get("binding_keys")) != canonical(job["binding_keys"])
            or proof["item_id"] != item["id"] or proof["binding_key"] != binding
            or proof["request_hash"] != digest(request)
            or digest(proof["result_receipt"]) != proof["result_hash"]):
        raise SafetyError("FUNCTIONAL_ALIAS_JOB_PROOF_INVALID")
    receipt = proof["result_receipt"]
    if (not isinstance(receipt, dict) or set(receipt) != {"request_hash", "response", "usage", "completed_at"}
            or receipt["request_hash"] != digest(request)):
        raise SafetyError("FUNCTIONAL_ALIAS_JOB_PROOF_INVALID")
    timestamp(receipt["completed_at"])
    checked = validate_results("B", request["payload"], receipt["response"], declared_version=PROMPT_VERSION)
    accepted = checked["accepted"].get(item["id"])
    if (accepted is None or canonical(accepted) != canonical(proof["accepted_item"])
            or digest(accepted) != proof["accepted_item_hash"]):
        raise SafetyError("FUNCTIONAL_ALIAS_JOB_NOT_ACCEPTED")
    return deepcopy(accepted)


def verify_alias_job(memory, state, prepared, job_id, item_id):
    """Verify exact original saved result bytes before any new alias acceptance."""
    from .research_ai import MAX_JOB_BYTES, current_attempt, validate_results
    item = semantic_alias_item(state, prepared)
    if item_id != item["id"]:
        raise SafetyError("FUNCTIONAL_ALIAS_JOB_INPUT_MISMATCH")
    job, request, binding = _original_job(state, job_id, item)
    path = current_attempt(safe_path(memory.root, "ai_jobs", job_id)) / "result.json"
    try:
        before = _fingerprint(path.stat())
        receipt = load_json(path, MAX_JOB_BYTES)
        after = _fingerprint(path.stat())
    except OSError:
        raise SafetyError("FUNCTIONAL_ALIAS_JOB_RESULT_UNAVAILABLE") from None
    if before != after or not isinstance(receipt, dict) or receipt.get("request_hash") != digest(request):
        raise SafetyError("FUNCTIONAL_ALIAS_JOB_RESULT_CHANGED")
    checked = validate_results("B", request["payload"], receipt.get("response"), declared_version=PROMPT_VERSION)
    accepted = checked["accepted"].get(item_id)
    if accepted is None:
        raise SafetyError("FUNCTIONAL_ALIAS_JOB_NOT_ACCEPTED")
    proof = {"contract": PROOF, "job_id": job_id, "item_id": item_id, "job_hash": digest(job),
             "original_job": deepcopy(job), "request_hash": digest(request), "binding_key": binding,
             "result_receipt": deepcopy(receipt), "result_hash": digest(receipt),
             "accepted_item": deepcopy(accepted), "accepted_item_hash": digest(accepted)}
    validate_alias_job_proof(state, prepared, proof)
    return {"proof": proof, "result_path": path, "result_fingerprint": after,
            "decision": accepted["decision"], "reason": accepted["reason"]}


def _review_record(state, rid, row):
    """Validate one saved alias review; reused inside one pure read scope.

    Only the state's own saved row is reused (keyed by its ID and the exact
    state objects); any other row is validated afresh, exactly as before.
    """
    if isinstance(state.get("source_checks"), dict) and state["source_checks"].get(rid) is row:
        from .read_memo import remember
        return remember("functional_alias_review_record", state, (rid,),
                        lambda: _review_record_uncached(state, rid, row), copy=False)
    return _review_record_uncached(state, rid, row)


def _review_record_uncached(state, rid, row):
    fields = {"kind", "input", "item_id", "method", "result", "job_proof", "record_hash"}
    if (not isinstance(row, dict) or set(row) != fields or row["kind"] != REVIEW_KIND
            or row["method"] != "model" or row["record_hash"] != digest({k: v for k, v in row.items() if k != "record_hash"})
            or rid != key("functional-alias-semantic-review", {k: v for k, v in row.items() if k != "record_hash"})):
        raise SafetyError("FUNCTIONAL_ALIAS_REVIEW_RECORD_INVALID")
    accepted = validate_alias_job_proof(state, row["input"], row["job_proof"])
    if row["item_id"] != accepted["id"] or canonical(row["result"]) != canonical(accepted):
        raise SafetyError("FUNCTIONAL_ALIAS_REVIEW_RECORD_INVALID")
    return row


def current_alias_review(state, prepared, *, as_of=None):
    _validate_input(state, prepared, current=True)
    item = semantic_alias_item(state, prepared)
    if as_of is not None:
        timestamp(as_of)
        if timestamp(prepared["reviewed_at"]) > timestamp(as_of):
            raise SafetyError("FUNCTIONAL_ALIAS_REVIEW_DATE_INVALID")
    if len({m["meaning_hash"] for m in item["members"]}) == 1:
        return {"method": "exact_meaning", "decision": "same", "reason": "The accepted functional meaning fields match exactly.",
                "item_id": item["id"], "record_id": None, "job_proof": None}
    current = None
    for rid, row in state.get("source_checks", {}).items():
        if not isinstance(row, dict) or row.get("kind") != REVIEW_KIND:
            continue
        # Validate positive typed envelopes before trusting their routing field.
        _review_record(state, rid, row)
        if row["item_id"] != item["id"]:
            continue
        if as_of is not None and timestamp(row["job_proof"]["result_receipt"]["completed_at"]) > timestamp(as_of):
            continue
        current = {"method": "model", "decision": row["result"]["decision"], "reason": row["result"]["reason"],
                   "item_id": item["id"], "record_id": rid, "job_proof": deepcopy(row["job_proof"])}
    return current


def _tokens(value):
    # Retrieval only. Sharing a token is never a same-type judgment.
    stop = {"a", "an", "the", "of", "for", "and", "or", "to", "in", "on", "with", "by", "use", "using", "product", "people"}
    return {token for token in re.findall(r"[^\W_]+", value.casefold()) if token not in stop and len(token) > 1}


def _original_signal(memory, state, item, *, as_of, retry_seconds=300, retry_not_before=None):
    from .research_ai import current_attempt, closed_model_retry_hint, prepared_closed_model_retry_hint
    try: job_id = _owner(state, item)
    except SafetyError as error:
        if str(error) != "FUNCTIONAL_ALIAS_JOB_OWNER_MISSING": raise
        return None
    base = safe_path(memory.root, "ai_jobs", job_id); folder = current_attempt(base)
    files = []
    for name in ("intent.json", "completion.json", "result.json", "runner_execution.json", "execution_failure.json", "failure.json"):
        path = folder / name
        try: value = list(_fingerprint(path.stat()))
        except FileNotFoundError: value = None
        files.append([name, value])
    retry = None
    if not any(name in {"completion.json", "result.json"} and value is not None for name, value in files):
        try:
            reader = prepared_closed_model_retry_hint if folder != base and not any(value is not None for _, value in files) else closed_model_retry_hint
            retry = reader(memory, job_id, as_of=as_of, retry_seconds=retry_seconds,
                not_before=retry_not_before, state=state)
        except (SafetyError, OSError) as error:
            # A damaged/unknown original is a named wait for this group, not
            # permission to replace its owner or stop independent groups.
            retry = {"ready": False, "reason": str(error) if isinstance(error, SafetyError)
                else "FUNCTIONAL_ALIAS_JOB_LOCAL_FAILED", "next_retry_at": None,
                "signal": digest({"job_id": job_id, "files": files, "reason": str(error)})}
    return [job_id, folder.name, files, retry]


def _application_hash(state, prepared, item, aliases, *, original_signal=None):
    from .functional_aliases import _contradictory_reviews
    members = set(prepared["member_ids"])
    expanded = set(members)
    for cid in members: expanded.update(aliases[cid]["member_ids"])
    return digest({"item": item, "definitions": prepared["definition_bindings"], "original_job": original_signal,
        "aliases": {cid: aliases[cid] for cid in sorted(expanded)},
        "different_reviews": {rid: digest(state["source_checks"][rid])
            for rid in _contradictory_reviews(state, prepared["member_ids"], aliases)},
        "exposures": {rid: digest(row) for rid, row in state.get("published_history", {}).items()
            if row.get("concept_id") in expanded and row.get("status") == "published"},
        "holds": {rid: digest(row) for rid, row in state.get("issue_reservations", {}).items()
            if row.get("state") in {"reserved", "uploaded"}
            and any(p.get("concept_id") in expanded for p in row.get("products", []))}})


def _definition_groups(state):
    """Per-read routing index; no new registry or quadratic global hashing."""
    from .functional_types import _DEFINITION_INTENT_FIELDS
    from .research_records import iter_functional_definitions
    groups = {}
    for rid, row in iter_functional_definitions(state):
        # Authenticate before trusting concept_id to exclude a damaged row.
        try:
            expected = key("functional-definition", {field: row[field] for field in _DEFINITION_INTENT_FIELDS})
        except (KeyError, TypeError):
            raise SafetyError("FUNCTIONAL_DEFINITION_RECORD_INVALID") from None
        if rid != expected or row.get("record_hash") != digest({k: v for k, v in row.items() if k != "record_hash"}):
            raise SafetyError("FUNCTIONAL_DEFINITION_BINDING_MISMATCH")
        identifier(row["concept_id"])
        groups.setdefault(row["concept_id"], {})[rid] = row
    return groups


def _matching_groups(memory, state, concept_ids, *, reviewed_at, limit, skip_group_ids=(), skip_group_bindings=None,
                     retry_seconds=300, retry_not_before=None):
    """Exact groups, then a small local retrieval shortlist, not all-pairs AI."""
    from .functional_aliases import alias_projection
    from .functional_types import effective_functional_definition
    if (not isinstance(concept_ids, (list, tuple, set)) or any(not isinstance(cid, str) for cid in concept_ids)
            or type(limit) is not int or not 1 <= limit <= MAX_GROUPS):
        raise SafetyError("FUNCTIONAL_ALIAS_GROUPS_INVALID")
    targets, skipped = set(concept_ids), set(skip_group_ids)
    skip_group_bindings = {} if skip_group_bindings is None else skip_group_bindings
    if not isinstance(skip_group_bindings, dict) or any(not isinstance(k, str) or not isinstance(v, str)
            for k, v in skip_group_bindings.items()):
        raise SafetyError("FUNCTIONAL_ALIAS_GROUPS_INVALID")
    for cid in targets: identifier(cid)
    definitions, use_index, mechanism_index = {}, {}, {}
    definition_groups = _definition_groups(state)
    errors = {}
    for cid in sorted(definition_groups):
        concept = state.get("concepts", {}).get(cid)
        if not isinstance(concept, dict):
            errors[cid] = "FUNCTIONAL_CONCEPT_NOT_FOUND"; continue
        if concept.get("status") in {"merged", "retired", "excluded"}: continue
        try:
            definition = effective_functional_definition({**state, "source_checks": definition_groups[cid]}, cid)
            if not definition["accepted"]: continue
            if timestamp(state["source_checks"][definition["definition_id"]]["reviewed_at"]) > timestamp(reviewed_at):
                continue
            definition = dict(definition, definition=state["source_checks"][definition["definition_id"]]["definition"])
            definitions[cid] = definition
            category = concept["description"]["category"]
            for token in _tokens(definition["definition"]["primary_use"]):
                use_index.setdefault((category, token), set()).add(cid)
            for token in _tokens(definition["definition"]["mechanism"]):
                mechanism_index.setdefault((category, token), set()).add(cid)
        except SafetyError as error:
            errors[cid] = str(error)
    aliases = alias_projection(state)
    exact_by_meaning = {}
    for cid, definition in definitions.items():
        category = state["concepts"][cid]["description"]["category"]
        exact_by_meaning.setdefault((category, definition["meaning_hash"]), []).append(cid)
    groups, signatures, exact_members = [], set(), set()
    def add(members, *, exact):
        members = sorted(members)
        prepared = prepare_alias_input(state, members, reviewed_at=reviewed_at)
        item = semantic_alias_item(state, prepared)
        group_id = item["id"]
        if group_id in skipped or group_id in signatures: return
        original_signal = None if exact else _original_signal(memory, state, item, as_of=reviewed_at,
            retry_seconds=retry_seconds, retry_not_before=retry_not_before)
        binding = _application_hash(state, prepared, item, aliases, original_signal=original_signal)
        if skip_group_bindings.get(group_id) == binding: return
        signatures.add(group_id)
        groups.append({"group_id": group_id, "member_ids": members, "input": prepared, "item": item, "exact": exact,
                       "input_hash": binding, "original_signal": original_signal})
    for members in exact_by_meaning.values():
        if not set(members) & targets: continue
        # One representative per already-reviewed component. A group larger
        # than20 is joined in bounded pieces around the stable actual anchor.
        representatives = {}
        for cid in sorted(members):
            if aliases[cid]["reason"]: continue
            representatives.setdefault(aliases[cid]["canonical_id"], cid)
        reps = sorted(representatives.values())
        if len(reps) < 2: continue
        exact_members.update(members)
        for start in range(1, len(reps), MAX_MEMBERS - 1):
            add([reps[0], *reps[start:start + MAX_MEMBERS - 1]], exact=True)
            if len(groups) >= limit: return groups, errors
    for cid in sorted(targets & definitions.keys()):
        if cid in exact_members or aliases[cid]["reason"]: continue
        definition = definitions[cid]["definition"]
        category = state["concepts"][cid]["description"]["category"]
        use, mechanism = _tokens(definition["primary_use"]), _tokens(definition["mechanism"])
        candidates = set()
        for token in use: candidates.update(use_index.get((category, token), ()))
        for token in mechanism: candidates.update(mechanism_index.get((category, token), ()))
        scored = []
        for other in candidates - {cid}:
            if aliases[other]["reason"] or aliases[other]["canonical_id"] == aliases[cid]["canonical_id"]: continue
            other_definition = definitions[other]["definition"]
            score = 2 * len(use & _tokens(other_definition["primary_use"])) + 3 * len(mechanism & _tokens(other_definition["mechanism"]))
            scored.append((-score, other))
        for _, other in sorted(scored)[:10]:
            pair = sorted([cid, other])
            prepared = prepare_alias_input(state, pair, reviewed_at=reviewed_at)
            cached = current_alias_review(state, prepared)
            if cached is not None and cached["decision"] != "same": continue
            add(pair, exact=False)
            if len(groups) >= limit: return groups, errors
    return groups, errors


def functional_alias_readiness(memory, state, concept_ids, *, as_of, runner_available=False,
                               limit=5, skip_group_ids=(), skip_group_bindings=None,
                               retry_seconds=300, retry_not_before=None):
    """Pure wake hint: original saved replies can arrive without a state write."""
    timestamp(as_of)
    groups, errors = _matching_groups(memory, state, concept_ids, reviewed_at=as_of, limit=min(limit, MAX_GROUPS),
                                     skip_group_ids=skip_group_ids, skip_group_bindings=skip_group_bindings,
                                     retry_seconds=retry_seconds, retry_not_before=retry_not_before)
    local, model, signal, statuses, waits, retry_hints = False, False, [], [], [], {}
    for group in groups:
        cached = current_alias_review(state, group["input"])
        item, job_id, files, retry, reason = group["item"], None, [], None, None
        if group["exact"] or cached is not None:
            status = "local_ready"; local = True
        else:
            original = group["original_signal"]
            if original is not None:
                job_id, attempt, files, retry = original
                existing = {name for name, value in files if value is not None}
                if existing & {"result.json", "completion.json"}:
                    status = "local_ready"; local = True
                else:
                    if retry is not None:
                        retry_hints[job_id] = retry
                        if retry["next_retry_at"] is not None and timestamp(retry["next_retry_at"]) > timestamp(as_of):
                            waits.append(retry["next_retry_at"])
                    if (existing & {"intent.json", "runner_execution.json", "execution_failure.json", "failure.json"}
                            or attempt.startswith("retry-")) and not (retry and retry["ready"]):
                        status = "original_pending"; reason = retry["reason"] if retry else "AI_JOB_PENDING_RECONCILIATION"
                    elif not _current_job_meanings(state, state["jobs"][job_id]["request"]["payload"]):
                        status = "original_pending"; reason = "FUNCTIONAL_ALIAS_ORIGINAL_BATCH_INPUT_CHANGED"
                    else:
                        status = "model_ready" if runner_available else "model_not_connected"; model |= runner_available
            else:
                status = "model_ready" if runner_available else "model_not_connected"; model |= runner_available
        statuses.append({"group_id": group["group_id"], "member_ids": group["member_ids"],
                         "input_hash": group["input_hash"], "state": status,
                         **({"reason": reason} if reason else {})})
        signal.append([group["group_id"], group["input_hash"], status, cached.get("record_id") if cached else None, job_id, files])
    return {"ready": local or model, "local_ready": local, "model_ready": model,
            "signal": signal, "groups": statuses, "waits": sorted(set(waits), key=timestamp),
            "retry_hints": retry_hints, "failures": errors}


def match_functional_aliases(memory, concept_ids, runner=None, *, reviewed_at, limit=5, skip_group_ids=(),
                             skip_group_bindings=None, retry_seconds=300, retry_not_before=None):
    """Use existing acceptance authority after a finite local/B review batch."""
    from .functional_aliases import accept_functional_alias, alias_projection
    groups, errors = _matching_groups(memory, memory.snapshot(), concept_ids, reviewed_at=reviewed_at,
                                     limit=min(limit, MAX_GROUPS), skip_group_ids=skip_group_ids,
                                     skip_group_bindings=skip_group_bindings,
                                     retry_seconds=retry_seconds, retry_not_before=retry_not_before)
    outcomes, semantic_groups = [], []
    for group in groups:
        if not group["exact"]:
            semantic_groups.append(group); continue
        try:
            result = accept_functional_alias(memory, group["input"], method="exact_meaning", reviewer="exact_saved_meanings",
                reason="The accepted functional meaning fields match exactly.")
            outcomes.append(dict(result, group_id=group["group_id"], member_ids=group["member_ids"],
                                 input_hash=group["input_hash"], decision=result.get("decision", "same")))
        except SafetyError as error:
            outcomes.append({"group_id": group["group_id"], "member_ids": group["member_ids"],
                "input_hash": group["input_hash"], "applied": False, "decision": "waiting", "reason": str(error)})
    if semantic_groups:
        reviewed = review_functional_aliases(memory, [g["member_ids"] for g in semantic_groups], runner, reviewed_at=reviewed_at,
            retry_seconds=retry_seconds, retry_not_before=retry_not_before)
        current_state = memory.snapshot(); current_aliases = alias_projection(current_state)
        for group in semantic_groups:
            group["input_hash"] = _application_hash(current_state, group["input"], group["item"], current_aliases,
                original_signal=_original_signal(memory, current_state, group["item"], as_of=reviewed_at,
                    retry_seconds=retry_seconds, retry_not_before=retry_not_before))
            current = reviewed["accepted"].get(group["group_id"])
            if current is None:
                outcomes.append({"group_id": group["group_id"], "member_ids": group["member_ids"],
                    "input_hash": group["input_hash"], "applied": False, "decision": "waiting",
                    "reason": reviewed["failures"].get(group["group_id"], "FUNCTIONAL_ALIAS_REVIEW_PENDING")})
                continue
            try:
                options = {}
                if current["method"] == "model":
                    proof = current["job_proof"]
                    options = {"job_id": proof["job_id"], "item_id": proof["item_id"]}
                result = accept_functional_alias(memory, current["input"], method=current["method"],
                    decision=current["decision"], reviewer="saved_functional_alias_review", reason=current["reason"], **options)
                outcomes.append(dict(result, group_id=group["group_id"], member_ids=group["member_ids"],
                    input_hash=group["input_hash"], decision=result.get("decision", current["decision"])))
            except SafetyError as error:
                outcomes.append({"group_id": group["group_id"], "member_ids": group["member_ids"],
                    "input_hash": group["input_hash"], "applied": False, "decision": "waiting", "reason": str(error)})
    return {"outcomes": outcomes, "failures": errors}


def _owner(state, item):
    """Resolve actual per-item Jobs ownership, never a derived aggregate ID."""
    owner = None
    for job_id, job in state.get("jobs", {}).items():
        request = job.get("request", {})
        if (request.get("role") != "B" or request.get("prompt_version") != PROMPT_VERSION
                or not any(row.get("id") == item["id"] for row in request.get("payload", {}).get("items", []))):
            continue
        _original_job(state, job_id, item)
        owner = job_id
    if owner is None:
        raise SafetyError("FUNCTIONAL_ALIAS_JOB_OWNER_MISSING")
    return owner


def _save_review(memory, prepared, item, job_id):
    state = memory.snapshot()
    _validate_input(state, prepared, current=True)
    verified = verify_alias_job(memory, state, prepared, job_id, item["id"])
    row = {"kind": REVIEW_KIND, "input": deepcopy(prepared), "item_id": item["id"], "method": "model",
           "result": deepcopy(verified["proof"]["accepted_item"]), "job_proof": verified["proof"]}
    rid = key("functional-alias-semantic-review", row); row["record_hash"] = digest(row)
    _review_record(state, rid, row)
    if rid in state["source_checks"]:
        if canonical(state["source_checks"][rid]) != canonical(row):
            raise SafetyError("FUNCTIONAL_ALIAS_REVIEW_RECORD_INVALID")
        return rid
    def unchanged(current):
        _validate_input(current, prepared, current=True)
        if digest(current["jobs"].get(job_id)) != verified["proof"]["job_hash"]:
            raise SafetyError("FUNCTIONAL_ALIAS_JOB_RESULT_CHANGED")
        try:
            actual = _fingerprint(verified["result_path"].stat())
        except OSError:
            raise SafetyError("FUNCTIONAL_ALIAS_JOB_RESULT_CHANGED") from None
        if actual != verified["result_fingerprint"]:
            raise SafetyError("FUNCTIONAL_ALIAS_JOB_RESULT_CHANGED")
    committed = memory.commit(key("save-functional-alias-review", rid), [change("source_checks", rid, row)], validate=unchanged)
    record_saved_decision(committed, record_kind=REVIEW_KIND, record_id=rid)
    return rid


class _SavedAliasRunner:
    """Restore exact original ownership without authorising a new model call."""
    def __init__(self, identity):
        self.identity = identity

    def __call__(self, role, payload, folder):
        raise SafetyError("AI_JOB_PENDING_RECONCILIATION")


def _current_job_meanings(state, payload):
    from .functional_types import effective_functional_definition
    groups = _definition_groups(state)
    for item in payload["items"]:
        for member in item["members"]:
            try:
                current = effective_functional_definition({**state,
                    "source_checks": groups.get(member["concept_id"], {})}, member["concept_id"])
            except SafetyError:
                return False
            if not current["accepted"] or current["meaning_hash"] != member["meaning_hash"]:
                return False
    return True


def review_functional_aliases(memory, member_groups, runner=None, *, reviewed_at=None,
                              retry_seconds=300, retry_not_before=None):
    """One bounded saved-input batch; healthy siblings survive local failures.

    Exact meanings use no model. Cached accepted same/different/unclear results
    need no new call. An unknown original stays pending through existing Jobs.
    No automatic alias acceptance or production entry point is enabled here.
    """
    from .research_ai import MAX_JOB_BYTES
    from .model_recovery import run_current_model_job
    if not isinstance(member_groups, (list, tuple)) or not 1 <= len(member_groups) <= MAX_GROUPS:
        raise SafetyError("FUNCTIONAL_ALIAS_GROUPS_INVALID")
    reviewed_at = reviewed_at or utc_now()
    if timestamp(reviewed_at) > timestamp(utc_now()):
        raise SafetyError("FUNCTIONAL_ALIAS_REVIEW_DATE_INVALID")
    ready, accepted, failures = {}, {}, {}
    state = memory.snapshot()
    for members in member_groups:
        # Preparation failures are tied to a stable local group identity, not
        # silently converted into a judgment about that group.
        group_id = key("functional-alias-group", members)
        try:
            prepared = prepare_alias_input(state, members, reviewed_at=reviewed_at)
            item = semantic_alias_item(state, prepared)
            if item["id"] in ready or item["id"] in accepted:
                continue
            cached = current_alias_review(state, prepared)
            if cached is not None:
                accepted[item["id"]] = dict(cached, input=prepared)
            else:
                ready[item["id"]] = (prepared, item)
        except SafetyError as error:
            failures[group_id] = str(error)
    checked = {"accepted": {}, "failures": {}}
    originals, novel = {}, []
    for item_id, (_, item) in ready.items():
        try:
            owner = _owner(state, item)
        except SafetyError as error:
            if str(error) == "FUNCTIONAL_ALIAS_JOB_OWNER_MISSING":
                novel.append(item)
            else:
                failures[item_id] = str(error)
        else:
            originals.setdefault(owner, []).append(item_id)
    launch_selected = False
    for job_id, item_ids in originals.items():
        request = state["jobs"][job_id]["request"]
        original_runner = _SavedAliasRunner(request["runner"])
        original = _original_signal(memory, state, ready[item_ids[0]][1], as_of=reviewed_at,
            retry_seconds=retry_seconds, retry_not_before=retry_not_before)
        _, attempt, files, retry = original
        saved = {name for name, value in files if value is not None}
        local = bool(saved & {"completion.json", "result.json"})
        blocked = bool(saved & {"intent.json", "runner_execution.json", "execution_failure.json", "failure.json"}) or attempt.startswith("retry-")
        if not local and blocked and not (retry and retry["ready"]):
            failures.update({item_id: retry["reason"] if retry else "AI_JOB_PENDING_RECONCILIATION" for item_id in item_ids})
            continue
        if not local:
            if launch_selected:
                failures.update({item_id: "FUNCTIONAL_ALIAS_REVIEW_BATCH_LIMIT" for item_id in item_ids})
                continue
            if runner is None or getattr(runner, "identity", "codex-chatgpt-high-v1") != request["runner"]:
                failures.update({item_id: "AI_RUNNER_NOT_ENABLED" for item_id in item_ids})
                continue
            if not _current_job_meanings(memory.snapshot(), request["payload"]):
                failures.update({item_id: "FUNCTIONAL_ALIAS_ORIGINAL_BATCH_INPUT_CHANGED" for item_id in item_ids})
                continue
            original_runner = runner
            launch_selected = True
        try:
            _, result = run_current_model_job(memory, original_runner, "B", request["payload"],
                declared_version=PROMPT_VERSION, as_of=reviewed_at, retry_seconds=retry_seconds,
                retry_not_before=retry_not_before)
        except (SafetyError, OSError) as error:
            code = str(error) if isinstance(error, SafetyError) else "FUNCTIONAL_ALIAS_JOB_LOCAL_FAILED"
            failures.update({item_id: code for item_id in item_ids})
        else:
            for field in checked:
                checked[field].update({item_id: value for item_id, value in result[field].items() if item_id in item_ids})
    if novel:
        if launch_selected:
            failures.update({item["id"]: "FUNCTIONAL_ALIAS_REVIEW_BATCH_LIMIT" for item in novel})
        elif runner is None:
            failures.update({item["id"]: "AI_RUNNER_NOT_ENABLED" for item in novel})
        else:
            selected = []
            for item in novel:
                candidate = {"mode": MODE, "alias_contract": CONTRACT, "items": selected + [item]}
                if len(canonical(candidate)) > MAX_JOB_BYTES:
                    code = ("FUNCTIONAL_ALIAS_REVIEW_SIZE_LIMIT" if len(canonical(dict(candidate, items=[item]))) > MAX_JOB_BYTES
                            else "FUNCTIONAL_ALIAS_REVIEW_BATCH_LIMIT")
                    failures[item["id"]] = code
                else:
                    selected.append(item)
            payload = {"mode": MODE, "alias_contract": CONTRACT, "items": selected}
            if selected:
                try:
                    _, result = run_current_model_job(memory, runner, "B", payload, declared_version=PROMPT_VERSION,
                        as_of=reviewed_at, retry_seconds=retry_seconds, retry_not_before=retry_not_before)
                except (SafetyError, OSError) as error:
                    code = str(error) if isinstance(error, SafetyError) else "FUNCTIONAL_ALIAS_JOB_LOCAL_FAILED"
                    failures.update({item["id"]: code for item in selected})
                else:
                    for field in checked: checked[field].update(result[field])
    failures.update(checked["failures"])
    for item_id in checked["accepted"]:
        prepared, item = ready[item_id]
        try:
            job_id = _owner(memory.snapshot(), item)
            rid = _save_review(memory, prepared, item, job_id)
            current = current_alias_review(memory.snapshot(), prepared)
            if current is None or current["record_id"] != rid:
                raise SafetyError("FUNCTIONAL_ALIAS_REVIEW_RECORD_INVALID")
            accepted[item_id] = dict(current, input=prepared)
        except (SafetyError, OSError) as error:
            failures[item_id] = str(error) if isinstance(error, SafetyError) else "FUNCTIONAL_ALIAS_REVIEW_SAVE_FAILED"
    return {"accepted": accepted, "failures": failures}
