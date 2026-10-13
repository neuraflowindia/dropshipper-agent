"""Bounded review of a real discovery heading against an observed narrow type.

The discovery question keeps its original general subject. This producer links
it to a subsequently accepted concept; it never creates concepts, transfers
market evidence or treats a department-list nomination as proof of a match.
"""

from copy import deepcopy
import re

from .functional_types import (MEANING_FIELDS, definition_meaning_hash,
    effective_functional_definition, functional_role_context, validate_definition)
from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .research_memory import key
from .storage import safe_path

CONTRACT = "catalogue-scope-review-v1"
MODE = "catalogue_scope_review"
PROMPT_VERSION = "research-b-catalogue-scope-v1"
METHOD = "discovery_catalogue_scope_v1"
MAX_ITEMS = 5
MAX_BYTES = 48 * 1024
RELATIONSHIPS = ("same_scope", "narrower", "broader_context", "unrelated", "unclear")
_HASH = re.compile(r"[0-9a-f]{64}")


def _hash(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")


def validate_scope_item(item):
    try:
        if set(item) != {"id", "concept_id", "meaning_hash", "definition", "family", "origin", "anchor"}:
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")
        identifier(item["concept_id"]); _hash(item["meaning_hash"])
        if set(item["definition"]) != set(MEANING_FIELDS):
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")
        definition = validate_definition(dict(item["definition"], report_title="Accepted narrow type"))
        if (definition_meaning_hash(definition) != item["meaning_hash"]
                or canonical({f: definition[f] for f in MEANING_FIELDS}) != canonical(item["definition"])):
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_MEANING_INVALID")
        family = item["family"]
        if (set(family) != {"family_id", "scope", "scope_hash"}
                or set(family["scope"]) != {"category", "definition", "included", "excluded"}
                or digest(family["scope"]) != family["scope_hash"]):
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")
        identifier(family["family_id"])
        if not isinstance(family["scope"]["definition"], str) or not family["scope"]["definition"].strip():
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")
        for field in ("included", "excluded"):
            if not isinstance(family["scope"][field], list) or any(not isinstance(v, str) for v in family["scope"][field]):
                raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")
        origin = item["origin"]
        if set(origin) != {"wave_id", "wave_binding_hash", "question_id", "question_binding_hash",
                           "route", "request", "acquisition_id", "child_id", "semantic_id", "association_hash"}:
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")
        for field in ("wave_id", "question_id", "acquisition_id", "child_id", "semantic_id"):
            identifier(origin[field])
        for field in ("wave_binding_hash", "question_binding_hash", "association_hash"): _hash(origin[field])
        from .research_questions import physical_request_fingerprint
        physical_request_fingerprint(origin["route"], origin["request"])
        anchor = item["anchor"]
        if set(anchor) != {"observation_ref", "capture_ref", "child_description"}:
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")
        for name, table in (("observation_ref", "observations"), ("capture_ref", "captures")):
            ref = anchor[name]
            if set(ref) != {"table", "id", "hash"} or ref["table"] != table:
                raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")
            identifier(ref["id"]); _hash(ref["hash"])
        if (not isinstance(anchor["child_description"], dict)
                or set(anchor["child_description"]) != {"function", "mechanism", "features", "audience"}):
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")
        if item["id"] != key("catalogue-scope-review", {k: v for k, v in item.items() if k != "id"}):
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")
    except (TypeError, KeyError, AttributeError):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID") from None
    return item


def validate_scope_payload(payload):
    if (not isinstance(payload, dict) or set(payload) != {"mode", "scope_contract", "items"}
            or payload["mode"] != MODE or payload["scope_contract"] != CONTRACT
            or not isinstance(payload["items"], list) or not 1 <= len(payload["items"]) <= MAX_ITEMS
            or len(canonical(payload)) > MAX_BYTES):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")
    for item in payload["items"]: validate_scope_item(item)
    if len({row["id"] for row in payload["items"]}) != len(payload["items"]):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_INPUT_INVALID")
    return payload


def validate_scope_result(item, result):
    validate_scope_item(item)
    if (not isinstance(result, dict) or set(result) != {"id", "relationship", "references", "reason"}
            or result["id"] != item["id"] or result["relationship"] not in RELATIONSHIPS
            or result["references"] != [item["anchor"]["observation_ref"]["id"]]
            or not isinstance(result["reason"], str) or not result["reason"].strip() or len(result["reason"]) > 2000):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_RESULT_INVALID")
    return deepcopy(result)


def _current_item(state, item, *, catalogue=None):
    """Recheck actual immutable originals, not a self-hashed origin claim."""
    from .catalogue_discovery import validate_discovery_wave
    from .research_scope import _family
    from .functional_types import _child_description
    validate_scope_item(item)
    origin, anchor = item["origin"], item["anchor"]
    wave = validate_discovery_wave(state, state.get("batch_runs", {}).get(origin["wave_id"]))
    if wave["binding_hash"] != origin["wave_binding_hash"]:
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_ORIGIN_CHANGED")
    plans = [p for p in wave["planned_actions"] if p["questions"][0]["id"] == origin["question_id"]]
    if (len(plans) != 1 or plans[0]["family_id"] != item["family"]["family_id"]
            or plans[0]["questions"][0]["question_binding_hash"] != origin["question_binding_hash"]
            or plans[0]["questions"][0]["route"] != origin["route"]
            or plans[0]["questions"][0]["request"] != origin["request"]):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_ORIGIN_CHANGED")
    action = wave["actions"].get(origin["question_id"], {})
    oid, capid = anchor["observation_ref"]["id"], anchor["capture_ref"]["id"]
    if (action.get("acquisition_id") != origin["acquisition_id"]
            or oid not in action.get("observation_ids", []) or capid not in action.get("capture_ids", [])):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_ORIGIN_CHANGED")
    for ref in (anchor["observation_ref"], anchor["capture_ref"]):
        if digest(state.get(ref["table"], {}).get(ref["id"])) != ref["hash"]:
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_REFERENCE_CHANGED")
    observation, capture = state["observations"][oid], state["captures"][capid]
    if (observation["capture_id"] != capid or observation["semantic_id"] != origin["semantic_id"]
            or observation["acquisition_id"] != origin["acquisition_id"]
            or capture["acquisition_id"] != origin["acquisition_id"]
            or capture["observed_at"] != observation["observed_at"]):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_REFERENCE_CHANGED")
    association = state.get("associations", {}).get(origin["child_id"])
    if (not isinstance(association, dict) or digest(association) != origin["association_hash"] or association.get("status") != "resolved"
            or association.get("concept_id") != item["concept_id"] or association.get("semantic_id") != origin["semantic_id"]):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_ASSOCIATION_CHANGED")
    meaning = functional_role_context(state, item["concept_id"], role="C")
    if (meaning is None or meaning["meaning_hash"] != item["meaning_hash"]
            or meaning["definition"] != item["definition"]
            or not any(p["child_id"] == origin["child_id"] and oid in p["observation_hashes"]
                for proofs in meaning["proof"]["examples"].values() for p in proofs)):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_MEANING_CHANGED")
    child = _child_description(state, origin["child_id"])
    if {f: deepcopy(child.get(f)) for f in anchor["child_description"]} != anchor["child_description"]:
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_REFERENCE_CHANGED")
    if catalogue is not None and _family(state, catalogue, item["family"]["family_id"]) != item["family"]:
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_FAMILY_CHANGED")
    return item


def validate_scope_adoption(state, row):
    """Authenticate the exact adopted answer; do not turn its relation into a vote."""
    from .research_ai import validate_results
    proof = row["job_proof"]
    job = state.get("jobs", {}).get(proof["job_id"])
    try:
        request = job["request"]
        if (row["method"] != METHOD or request["role"] != "B" or request["prompt_version"] != PROMPT_VERSION
                or key("job", request) != proof["job_id"] or digest(job) != proof["job_hash"]):
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_JOB_INVALID")
        payload = validate_scope_payload(request["payload"])
        item = next(i for i in payload["items"] if i["id"] == proof["item_id"])
        _current_item(state, item)
        binding = digest(["B", item, {k: v for k, v in payload.items() if k != "items"},
                          request["model"], PROMPT_VERSION, request["runner"]])
        answer = validate_scope_result(item, proof["accepted_item"])
        checked = validate_results("B", payload, {"results": [answer]}, declared_version=PROMPT_VERSION)
        if (proof["binding_key"] != binding or job["binding_keys"].get(item["id"]) != binding
                or checked["accepted"].get(item["id"]) != answer or digest(answer) != proof["accepted_item_hash"]
                or row["subject"]["definition_hash"] != item["meaning_hash"] or row["concept_id"] != item["concept_id"]
                or row["catalogue"] != item["family"] or row["relationship"] != answer["relationship"]
                or row["reason"] != answer["reason"]
                or {r["id"] for r in row["references"] if r["table"] == "observations"} != set(answer["references"])
                or row["references"] != sorted([item["anchor"]["observation_ref"], item["anchor"]["capture_ref"]],
                                              key=lambda r: (r["table"], r["id"]))):
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_JOB_INVALID")
    except (KeyError, TypeError, StopIteration, AttributeError):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_JOB_INVALID") from None
    return answer


def _payload(items):
    return {"mode": MODE, "scope_contract": CONTRACT, "items": items}


def _candidates(state, catalogue, concept_ids, as_of):
    from .catalogue_discovery import WAVE_VERSION, validate_discovery_wave
    from .functional_types import _child_description
    from .research_scope import _family
    allowed = None if concept_ids is None else set(concept_ids)
    meanings, examples, errors = {}, {}, {}
    for cid in sorted(state.get("concepts", {})):
        if allowed is not None and cid not in allowed: continue
        if state["concepts"][cid].get("status") in {"retired", "merged", "excluded"}: continue
        try: meaning = functional_role_context(state, cid, role="C")
        except SafetyError as error: errors[cid] = str(error); continue
        if meaning is None: continue
        if timestamp(state["source_checks"][meaning["proof"]["definition_id"]]["reviewed_at"]) > timestamp(as_of): continue
        meanings[cid] = meaning
        for proofs in meaning["proof"]["examples"].values():
            for p in proofs: examples.setdefault(p["semantic_id"], []).append((cid, p["child_id"]))
    candidates = {}
    waves = [(wid, row) for wid, row in state.get("batch_runs", {}).items()
             if isinstance(row, dict) and row.get("kind") == WAVE_VERSION]
    for wid, wave in sorted(waves, key=lambda p: (p[1].get("prepared_at", ""), p[0])):
        try: validate_discovery_wave(state, wave)
        except SafetyError as error: errors[wid] = str(error); continue
        if timestamp(wave["prepared_at"]) > timestamp(as_of): continue
        for plan in wave["planned_actions"]:
            question = plan["questions"][0]; qid = question["id"]
            action = wave["actions"].get(qid, {})
            if action.get("state") not in {"acquired", "interpretation_pending"}: continue
            try: family = _family(state, catalogue, plan["family_id"])
            except SafetyError as error: errors[qid] = str(error); continue
            for oid in sorted(action.get("observation_ids", [])):
                observation = state.get("observations", {}).get(oid)
                if not observation or timestamp(observation["observed_at"]) > timestamp(as_of): continue
                for cid, child_id in examples.get(observation["semantic_id"], []):
                    pair = (cid, family["family_id"])
                    if pair in candidates: continue
                    meaning, capid = meanings[cid], observation["capture_id"]
                    child = _child_description(state, child_id)
                    item = {"concept_id": cid, "meaning_hash": meaning["meaning_hash"], "definition": meaning["definition"],
                        "family": family, "origin": {"wave_id": wid, "wave_binding_hash": wave["binding_hash"],
                            "question_id": qid, "question_binding_hash": question["question_binding_hash"],
                            "route": question["route"], "request": deepcopy(question["request"]),
                            "acquisition_id": action["acquisition_id"], "child_id": child_id,
                            "semantic_id": observation["semantic_id"], "association_hash": digest(state["associations"][child_id])},
                        "anchor": {"observation_ref": {"table": "observations", "id": oid, "hash": digest(observation)},
                            "capture_ref": {"table": "captures", "id": capid, "hash": digest(state["captures"][capid])},
                            "child_description": {f: deepcopy(child.get(f)) for f in ("function", "mechanism", "features", "audience")}}}
                    item["id"] = key("catalogue-scope-review", item)
                    try: _current_item(state, item, catalogue=catalogue)
                    except SafetyError as error: errors[item["id"]] = str(error); continue
                    candidates[pair] = item
    return list(candidates.values()), errors


def _owner(state, item):
    from .research_ai import _item_bindings
    found = []
    for jid, job in state.get("jobs", {}).items():
        request = job.get("request", {})
        if request.get("role") != "B" or request.get("prompt_version") != PROMPT_VERSION: continue
        payload = validate_scope_payload(request["payload"])
        if not any(i["id"] == item["id"] for i in payload["items"]): continue
        if (key("job", request) != jid or job["binding_keys"] != _item_bindings("B", payload, request["model"], PROMPT_VERSION, request["runner"])
                or next(i for i in payload["items"] if i["id"] == item["id"]) != item):
            raise SafetyError("CATALOGUE_SCOPE_REVIEW_JOB_INVALID")
        found.append(jid)
    if len(found) > 1: raise SafetyError("CATALOGUE_SCOPE_REVIEW_JOB_OWNER_CONFLICT")
    return next(iter(found), None)


def catalogue_scope_readiness(memory, *, catalogue=None, concept_ids=None, as_of=None, limit=MAX_ITEMS,
                              runner_available=False, skip_bindings=None, retry_seconds=300, retry_not_before=None,
                              state=None):
    """Pure original-file wake hints; share only an enclosing verified state.

    Supplying state saves a repeated history read, not the live original-file
    checks or the later fresh scope/model admission.
    """
    from .catalogue import load_catalogue
    from .research_ai import current_attempt, closed_model_retry_hint, prepared_closed_model_retry_hint
    from .research_scope import current_catalogue_scopes
    at = as_of or utc_now(); timestamp(at)
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS or (concept_ids is not None and not isinstance(concept_ids, (list, tuple, set))):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_SCOPE_INVALID")
    for cid in concept_ids or []: identifier(cid)
    skipped = {} if skip_bindings is None else skip_bindings
    if not isinstance(skipped, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in skipped.items()):
        raise SafetyError("CATALOGUE_SCOPE_REVIEW_SCOPE_INVALID")
    catalogue = load_catalogue() if catalogue is None else catalogue
    state = memory.snapshot() if state is None else state
    candidates, errors = _candidates(state, catalogue, concept_ids, at)
    ready, current, waiting, bindings, signal, retry_hints, waits = [], {}, dict(errors), {}, [], {}, []
    local_ready, model_ready = False, False
    current_rows = current_catalogue_scopes(state, catalogue, as_of=at)
    for item in candidates:
        iid = item["id"]
        exact_refs = sorted([item["anchor"]["observation_ref"], item["anchor"]["capture_ref"]],
                            key=lambda r: (r["table"], r["id"]))
        cached = [r for r in current_rows if r["concept_id"] == item["concept_id"]
            and r["catalogue"] == item["family"] and r["references"] == exact_refs]
        if cached:
            current[iid] = {"concept_id": item["concept_id"], "family_id": item["family"]["family_id"],
                            "scope_id": cached[0]["id"], "relationship": cached[0]["relationship"]}
            continue
        owner, files, status, retry = None, [], "model_ready", None
        try:
            owner = _owner(state, item)
            if owner is not None:
                base = safe_path(memory.root, "ai_jobs", owner); folder = current_attempt(base)
                for name in ("intent.json", "completion.json", "result.json", "runner_execution.json", "execution_failure.json", "failure.json"):
                    try:
                        st = (folder / name).lstat(); stat = [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
                    except FileNotFoundError: stat = None
                    files.append([name, stat])
                present = {name for name, stat in files if stat is not None}
                if present & {"result.json", "completion.json"}: status = "local_ready"
                else:
                    reader = prepared_closed_model_retry_hint if folder != base and not present else closed_model_retry_hint
                    retry = reader(memory, owner, as_of=at, retry_seconds=retry_seconds,
                        not_before=retry_not_before, state=state)
                    retry_hints[owner] = retry
                    if retry["next_retry_at"] is not None and timestamp(retry["next_retry_at"]) > timestamp(at):
                        waits.append(retry["next_retry_at"])
                    if present & {"intent.json", "runner_execution.json", "execution_failure.json", "failure.json"} or folder != base:
                        status = "model_ready" if retry["ready"] else "original_pending"
                    if status == "model_ready":
                        for old in state["jobs"][owner]["request"]["payload"]["items"]:
                            _current_item(state, old, catalogue=catalogue)
            wake = digest({"item": item, "owner": owner, "files": files, "closed_retry": retry})
            bindings[iid] = wake
            signal.append([iid, wake, status])
            if skipped.get(iid) == wake: continue
            if status == "original_pending": waiting[iid] = {"reason": retry["reason"] if retry else "AI_JOB_PENDING_RECONCILIATION", "binding_key": iid, "wake_binding": wake}; continue
            if status == "model_ready" and not runner_available:
                waiting[iid] = {"reason": "AI_RUNNER_NOT_ENABLED", "binding_key": iid, "wake_binding": wake}; continue
            row = {"item": item, "binding_key": iid, "wake_binding": wake, "status": status, "owner": owner}
            ready.append(row); local_ready |= status == "local_ready"; model_ready |= status == "model_ready"
        except (SafetyError, OSError) as error:
            wake = digest({"item": item, "owner": owner, "files": files, "closed_retry": retry, "reason": str(error)})
            bindings[iid] = wake
            waiting[iid] = {"reason": str(error), "binding_key": iid, "wake_binding": wake}
    # Local replies take precedence but pending originals do not starve siblings.
    ready.sort(key=lambda r: (r["status"] != "local_ready", r["binding_key"]))
    return {"ready": bool(ready), "local_ready": local_ready, "model_ready": model_ready,
        "pending": ready[:limit], "current": current, "current_count": len(current),
        "waiting": waiting, "waited_count": len(waiting), "bindings": bindings,
        "signal": digest({"requests": signal, "current": current, "waiting": waiting}),
        "retry_hints": retry_hints, "waits": sorted(set(waits), key=timestamp)}


class _SavedScopeRunner:
    def __init__(self, identity): self.identity = identity
    def __call__(self, role, payload, folder): raise SafetyError("AI_JOB_PENDING_RECONCILIATION")


def review_discovery_catalogue_scopes(memory, runner=None, *, catalogue=None, concept_ids=None,
                                     as_of=None, limit=MAX_ITEMS, skip_bindings=None,
                                     retry_seconds=300, retry_not_before=None):
    """Bounded B purpose work and exact original scope application, no new source calls."""
    from .catalogue import load_catalogue
    from .research_ai import current_attempt
    from .model_recovery import run_current_model_job
    from .research_scope import accept_catalogue_scope
    catalogue = load_catalogue() if catalogue is None else catalogue
    at = as_of or utc_now()
    hint = catalogue_scope_readiness(memory, catalogue=catalogue, concept_ids=concept_ids, as_of=at,
        limit=limit, runner_available=runner is not None, skip_bindings=skip_bindings,
        retry_seconds=retry_seconds, retry_not_before=retry_not_before)
    outcomes, originals, novel = [], {}, []
    for pending in hint["pending"]:
        if pending["owner"]: originals.setdefault(pending["owner"], []).append(pending)
        else: novel.append(pending)
    results, failures = {}, dict(hint["waiting"])
    state = memory.snapshot(); launched = False
    for owner, pending in originals.items():
        request = state["jobs"][owner]["request"]; folder = current_attempt(safe_path(memory.root, "ai_jobs", owner))
        selected_runner = _SavedScopeRunner(request["runner"])
        retry = hint["retry_hints"].get(owner)
        if not any((folder / name).exists() for name in ("intent.json", "completion.json", "result.json", "runner_execution.json", "execution_failure.json", "failure.json")) or retry is not None and retry["ready"]:
            if launched or runner is None or getattr(runner, "identity", "codex-chatgpt-high-v1") != request["runner"]:
                failures.update({r["binding_key"]: {"reason": "AI_RUNNER_NOT_ENABLED"} for r in pending}); continue
            try:
                for old in request["payload"]["items"]: _current_item(memory.snapshot(), old, catalogue=catalogue)
            except SafetyError as error:
                failures.update({r["binding_key"]: {"reason": str(error)} for r in pending}); continue
            selected_runner = runner; launched = True
        try:
            _, checked = run_current_model_job(memory, selected_runner, "B", request["payload"],
                declared_version=PROMPT_VERSION, as_of=at, retry_seconds=retry_seconds,
                retry_not_before=retry_not_before)
            ids = {r["binding_key"] for r in pending}
            results.update({iid: value for iid, value in checked["accepted"].items() if iid in ids})
            failures.update({iid: {"reason": value} for iid, value in checked["failures"].items() if iid in ids})
        except (SafetyError, OSError) as error: failures.update({r["binding_key"]: {"reason": str(error)} for r in pending})
    if novel and not launched:
        selected = []
        for row in novel:
            if len(canonical(_payload([r["item"] for r in selected + [row]]))) <= MAX_BYTES: selected.append(row)
            else: failures[row["binding_key"]] = {"reason": "CATALOGUE_SCOPE_REVIEW_SIZE_LIMIT"}
        if selected:
            try:
                _, checked = run_current_model_job(memory, runner, "B", _payload([r["item"] for r in selected]),
                    declared_version=PROMPT_VERSION, as_of=at, retry_seconds=retry_seconds,
                    retry_not_before=retry_not_before)
                results.update(checked["accepted"]); failures.update({iid: {"reason": v} for iid, v in checked["failures"].items()})
            except (SafetyError, OSError) as error: failures.update({r["binding_key"]: {"reason": str(error)} for r in selected})
    elif novel:
        failures.update({r["binding_key"]: {"reason": "CATALOGUE_SCOPE_REVIEW_BATCH_LIMIT"} for r in novel})
    for row in hint["pending"]:
        iid, item = row["binding_key"], row["item"]
        base = {"binding_key": iid, "wake_binding": row["wake_binding"], "concept_id": item["concept_id"],
                "family_id": item["family"]["family_id"]}
        if iid not in results:
            outcomes.append(dict(base, state="waiting", **failures.get(iid, {"reason": "CATALOGUE_SCOPE_REVIEW_PENDING"}))); continue
        answer = results[iid]
        try:
            state = memory.snapshot(); _current_item(state, item, catalogue=catalogue)
            owner = _owner(state, item)
            saved = accept_catalogue_scope(memory, catalogue, item["concept_id"], item["family"]["family_id"],
                relationship=answer["relationship"], references=[item["anchor"]["observation_ref"]],
                reason=answer["reason"], reviewer="bounded_B_catalogue_scope_review", as_of=at,
                expected_meaning_hash=item["meaning_hash"], method=METHOD, job_id=owner, job_item_id=iid)
            outcomes.append(dict(base, state="current", relationship=answer["relationship"], scope_id=saved["id"]))
        except (SafetyError, OSError) as error: outcomes.append(dict(base, state="waiting", reason=str(error)))
    return {"outcomes": outcomes, "current": hint["current"],
        "current_count": hint["current_count"] + sum(r["state"] == "current" for r in outcomes),
        "waited_count": sum(r["state"] == "waiting" for r in outcomes) + hint["waited_count"],
        "waiting": hint["waiting"], "bindings": hint["bindings"], "signal": hint["signal"],
        "client_publication_attempted": False}
