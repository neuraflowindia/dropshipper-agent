"""Opt-in scoped F interpretation and exact saved-recapture application.

Only explicit callers enter this contract. Legacy supplier extraction stays
unchanged. Preparation reads verified original captures; extraction uses the
existing Jobs owner/admission machinery and never fetches/contact suppliers.
"""

from copy import deepcopy

from .models import SafetyError, digest, timestamp, utc_now
from .research_memory import key, change
from .storage import atomic_json, load_json, safe_path
from .supplier_capture_store import load_saved_supplier_capture
from .supplier_terms import (VERSION, PROMPT_VERSION, APPLICATION_KIND, supplier_aliases,
    supplier_payload, original_supplier_result, prepare_scoped_supplier_input,
    validate_scoped_supplier_item, validate_scoped_supplier_result, validate_supplier_application)
from .suppliers import _interpretation_failure
from .model_recovery import run_current_model_job


def prepare_supplier_terms(memory, capture_path, choices):
    """Read one genuine captured bundle and prepare explicit grounded scopes.

    Each choice has scope_kind, page_ids, company_identity, workflow and offer.
    Unknown company identity is None; it cannot grant company-wide conditions.
    Dates/capture IDs remain in application bindings, not the F semantic key.
    """
    if not isinstance(choices, list) or not 1 <= len(choices) <= 20:
        raise SafetyError("SUPPLIER_SCOPE_CHOICES_INVALID")
    capture = load_saved_supplier_capture(memory, capture_path)
    if capture["bundle"]["item"] is None:
        raise SafetyError("SUPPLIER_CAPTURE_NO_USABLE_PAGES")
    state = memory.snapshot()
    prepared = [prepare_scoped_supplier_input(state, capture, choice) for choice in choices]
    if len({digest([row["item"], row["application"]]) for row in prepared}) != len(prepared):
        raise SafetyError("SUPPLIER_SCOPE_CHOICES_DUPLICATED")
    return prepared


def _semantic_id(item, runner):
    from .research_ai import MODEL
    return key("supplier-scoped", [item, VERSION, MODEL, PROMPT_VERSION,
        getattr(runner, "identity", "codex-chatgpt-high-v1")])


def _save_aliases(memory, runner, payload, mappings):
    from .research_ai import MODEL, prompt_version
    request = {"role": "F", "payload": payload, "model": MODEL,
        "prompt_version": prompt_version("F", payload),
        "runner": getattr(runner, "identity", "codex-chatgpt-high-v1")}
    job_id = key("job", request)
    path = safe_path(memory.root, "ai_jobs", job_id) / "supplier_scoped_reference_map.json"
    record = {"contract": VERSION, "request_hash": digest(request), "aliases": mappings}
    with memory.journal.locked():
        if path.exists():
            if load_json(path) != record:
                raise SafetyError("AI_SUPPLIER_ALIAS_MAP_CONFLICT")
        else:
            atomic_json(path, record)
    return job_id


def _apply(memory, prepared, assessment_id, semantic):
    # Re-read the actual original after F, rather than trusting a stale caller's
    # claimed capture time. This is saved-only and does not refresh a page.
    fresh = prepare_supplier_terms(memory, prepared["application"]["capture_path"], [prepared["choice"]])[0]
    if fresh != prepared:
        raise SafetyError("SUPPLIER_SCOPED_APPLICATION_CHANGED")
    binding = dict(deepcopy(fresh["application"]), assessment_id=assessment_id, assessment_hash=digest(semantic))
    application_id = key("supplier-application", binding)
    state = memory.snapshot()
    existing = state["source_checks"].get(application_id)
    if existing is not None:
        if existing.get("binding") != binding:
            raise SafetyError("SUPPLIER_SCOPED_APPLICATION_CONFLICT")
        if validate_supplier_application(state, existing, as_of=utc_now()) is None:
            raise SafetyError("SUPPLIER_SCOPED_APPLICATION_CHANGED")
        return application_id, True
    record = {"kind": APPLICATION_KIND, "id": application_id, "binding": binding,
              "binding_hash": digest(binding), "prepared_at": utc_now()}
    def valid_current(state):
        # This is a pure, bounded canonical check inside the actual commit.
        # F ran outside the lock. A meaning/effort change between the saved
        # page read and this write cannot admit a wrongly current application.
        if validate_supplier_application(state, record, as_of=record["prepared_at"]) is None:
            raise SafetyError("SUPPLIER_SCOPED_APPLICATION_CHANGED")
    valid_current(state)
    memory.commit(key("supplier-application-put", binding),
        [change("source_checks", application_id, record)], validate=valid_current)
    return application_id, False


def extract_scoped_supplier_terms(memory, prepared, runner=None):
    """Interpret/apply finite scopes; independent valid siblings keep running.

    One item is a company/workflow or one actual offer. Exact recaptures reuse
    the semantic record/job, then append a date-bound application. No date is
    inferred from F completion and unknown original Jobs are never replaced.
    """
    from .research_ai import Jobs
    if not isinstance(prepared, list) or not 1 <= len(prepared) <= 50:
        raise SafetyError("SUPPLIER_SCOPED_BATCH_INVALID")
    accepted, failures, failure_job_ids, applications, reused = {}, {}, {}, {}, []
    for entry in prepared:
        item_id, job_id = None, None
        try:
            if not isinstance(entry, dict) or set(entry) != {"item", "application", "choice"}:
                raise SafetyError("SUPPLIER_SCOPED_INPUT_INVALID")
            item = entry["item"]; validate_scoped_supplier_item(item); item_id = item["id"]
            # No model request is admitted from an unverified/mutated prepared
            # scope. The exact source/capture/application is rederived first.
            verified = prepare_supplier_terms(memory, entry["application"]["capture_path"], [entry["choice"]])[0]
            if verified != entry:
                raise SafetyError("SUPPLIER_SCOPED_APPLICATION_CHANGED")
            assessment_id = _semantic_id(item, runner)
            semantic = memory.snapshot()["supplier_assessments"].get(assessment_id)
            if semantic is None:
                payload = supplier_payload([item]); _, mappings = supplier_aliases([item])
                job_id = _save_aliases(memory, runner, payload, mappings)
                job_id, outcome = run_current_model_job(memory, runner, "F", payload, declared_version=PROMPT_VERSION)
                alias_id = payload["items"][0]["id"]
                if alias_id in outcome["failures"]:
                    failures[item_id] = outcome["failures"][alias_id]
                    failure_job_ids[item_id] = job_id
                    continue
                result = original_supplier_result(outcome["accepted"][alias_id], mappings[alias_id])
                result = validate_scoped_supplier_result(item, result)
                semantic = {"kind": VERSION, "version": VERSION, "input": deepcopy(item),
                    "input_hash": digest(item), "job_id": job_id, "checked_at": utc_now(), "result": result}
                semantic["record_hash"] = digest(semantic)
                memory.put("supplier_assessments", assessment_id, semantic)
            else:
                reused.append(item_id)
            application_id, _ = _apply(memory, entry, assessment_id, semantic)
            applications[item_id] = application_id
            accepted[item_id] = deepcopy(semantic["result"])
        except (SafetyError, OSError) as error:
            failure = _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")
            failed_id = item_id or key("invalid-supplier-scope", digest(entry))
            failures[failed_id] = failure
            if job_id:
                failure_job_ids[failed_id] = job_id
    return {"accepted": accepted, "failures": failures, "failure_job_ids": failure_job_ids,
            "application_ids": applications, "reused": reused, "source_calls": 0}


def prepare_supplier_candidates(memory, capture_path, *, state=None):
    """Prepare saved pages from one view; default extraction stays fresh."""
    from .supplier_scope_candidates import prepare_candidate_items
    # Only a caller's explicit preparation hint reuses its view. Extraction
    # omits that hint, preserving the original post-file-read product check
    # and fresh functional-meaning read at its execution boundary.
    capture = (load_saved_supplier_capture(memory, capture_path) if state is None
               else load_saved_supplier_capture(memory, capture_path, state=state))
    state = memory.snapshot() if state is None else state
    if capture["bundle"]["item"] is None:
        raise SafetyError("SUPPLIER_CAPTURE_NO_USABLE_PAGES")
    return [{"item": item, "capture_path": capture["path"], "capture_id": capture["capture_id"]}
            for item in prepare_candidate_items(state, capture)]


def candidate_review_id(item, runner):
    from .research_ai import MODEL
    return key("supplier-candidate-review", [item, VERSION, MODEL, PROMPT_VERSION,
        getattr(runner, "identity", "codex-chatgpt-high-v1")])


def current_candidate_review(state, item, *, as_of):
    """Pure selection currentness; application to a new receipt is separate."""
    from .supplier_scope_candidates import KIND, validate_candidate_review
    matches = [(rid, row) for rid, row in state["supplier_assessments"].items()
               if row.get("kind") == KIND and row.get("input_hash") == digest(item) and row.get("input") == item]
    current = []
    for rid, row in matches:
        validate_candidate_review(state, rid, row)
        if timestamp(row["checked_at"]) <= timestamp(as_of):
            current.append((rid, row))
    return max(current, key=lambda pair: (timestamp(pair[1]["checked_at"]), pair[0])) if current else None


def current_supplier_capture_review(state, prepared, *, as_of):
    """A capture is answered only when each actual page and application is done.

    A semantic review alone does not apply its dates to a different recapture.
    Empty/unrelated reviews are still completed checks, never claimed supply.
    """
    from .supplier_scope_candidates import selected_scope
    relationships, applications, reviews = [], [], []
    complete = True
    for entry in prepared:
        current = current_candidate_review(state, entry["item"], as_of=as_of)
        if current is None:
            complete = False
            continue
        review_id, review = current; reviews.append(review_id)
        for index, selection in enumerate(review["result"]["selections"]):
            scope = selected_scope(entry["item"], selection)
            semantic_id = key("supplier-selected-scope", [review_id, index, scope["item"]])
            found = False
            for app_id, app in state["source_checks"].items():
                if (app.get("kind") != APPLICATION_KIND or app.get("binding", {}).get("capture_id") != entry["capture_id"]
                        or app["binding"].get("assessment_id") != semantic_id):
                    continue
                if app.get("id") != app_id:
                    raise SafetyError("SAVED_SCOPED_SUPPLIER_INVALID")
                if validate_supplier_application(state, app, as_of=as_of) is not None:
                    applications.append(app_id); found = True
            if not found:
                complete = False
            relationships.append(scope["result"]["relevance"]["relationship"])
    return {"complete": complete, "relationships": relationships,
            "application_ids": sorted(set(applications)), "review_ids": sorted(set(reviews))}


def extract_supplier_candidates(memory, prepared, runner=None):
    """One bounded existing F owner selects and interprets each actual page.

    Its literal plain-text selections are validated before their scope refs
    are derived. An identical recapture applies the same saved interpretation
    to new verified dates; it never causes another model call.
    """
    from .research_ai import Jobs
    from .supplier_scope_candidates import (KIND, candidate_payload, candidate_aliases,
        validate_candidate_item, validate_candidate_review, selected_scope)
    if not isinstance(prepared, list) or not 1 <= len(prepared) <= 50:
        raise SafetyError("SUPPLIER_SCOPED_BATCH_INVALID")
    accepted, failures, failure_job_ids, applications, reused = {}, {}, {}, {}, []
    for entry in prepared:
        item_id, job_id = None, None
        try:
            if not isinstance(entry, dict) or set(entry) != {"item", "capture_path", "capture_id"}:
                raise SafetyError("SUPPLIER_CANDIDATE_INPUT_INVALID")
            item = entry["item"]; validate_candidate_item(item); item_id = item["id"]
            verified = prepare_supplier_candidates(memory, entry["capture_path"])
            if entry not in verified:
                raise SafetyError("SUPPLIER_SCOPED_APPLICATION_CHANGED")
            review_id = candidate_review_id(item, runner)
            state = memory.snapshot(); review = state["supplier_assessments"].get(review_id)
            if review is None:
                payload = candidate_payload([item]); _, mappings = candidate_aliases([item])
                job_id = _save_aliases(memory, runner, payload, mappings)
                job_id, outcome = run_current_model_job(memory, runner, "F", payload, declared_version=PROMPT_VERSION)
                alias = payload["items"][0]["id"]
                if alias in outcome["failures"]:
                    failures[item_id] = outcome["failures"][alias]; failure_job_ids[item_id] = job_id
                    continue
                result = original_supplier_result(outcome["accepted"][alias], mappings[alias])
                review = {"kind": KIND, "input": deepcopy(item), "input_hash": digest(item),
                    "job_id": job_id, "checked_at": utc_now(), "result": result}
                review["record_hash"] = digest(review)
                validate_candidate_review(memory.snapshot(), review_id, review)
                memory.put("supplier_assessments", review_id, review)
            else:
                validate_candidate_review(state, review_id, review); reused.append(item_id)
            applied = []
            for index, selection in enumerate(review["result"]["selections"]):
                scope = selected_scope(item, selection)
                prepared_scope = prepare_supplier_terms(memory, entry["capture_path"], [scope["choice"]])[0]
                if prepared_scope["item"] != scope["item"]:
                    raise SafetyError("SUPPLIER_SCOPED_APPLICATION_CHANGED")
                semantic_id = key("supplier-selected-scope", [review_id, index, scope["item"]])
                semantic = {"kind": VERSION, "version": VERSION, "input": deepcopy(scope["item"]),
                    "input_hash": digest(scope["item"]), "job_id": review["job_id"], "checked_at": review["checked_at"],
                    "result": scope["result"], "candidate_review_id": review_id,
                    "candidate_review_hash": digest(review), "selection_index": index}
                semantic["record_hash"] = digest(semantic)
                existing = memory.snapshot()["supplier_assessments"].get(semantic_id)
                if existing is not None and existing != semantic:
                    raise SafetyError("SAVED_SCOPED_SUPPLIER_INVALID")
                if existing is None:
                    memory.put("supplier_assessments", semantic_id, semantic)
                application_id, _ = _apply(memory, prepared_scope, semantic_id, semantic)
                applied.append(application_id)
            applications[item_id] = applied
            accepted[item_id] = deepcopy(review["result"])
        except (SafetyError, OSError) as error:
            if str(error) == "SAVED_SCOPED_SUPPLIER_INVALID":
                raise
            failures[item_id or key("invalid-supplier-candidate", digest(entry))] = _interpretation_failure(
                error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")
            if job_id and item_id:
                failure_job_ids[item_id] = job_id
    return {"accepted": accepted, "failures": failures, "failure_job_ids": failure_job_ids,
            "application_ids": applications, "reused": reused, "source_calls": 0}
