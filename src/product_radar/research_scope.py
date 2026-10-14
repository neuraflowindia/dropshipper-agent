"""Explicit source-bound scope judgments; no collection or automatic market votes.

An accepted review is not a claim that every source supports a narrow type.
Relationship, scope, source support and admissibility remain separate. Original
records, their dates and their request/location context are retained unchanged.
"""

from copy import deepcopy

from .catalogue_expansion import effective_catalogue
from .functional_types import effective_functional_definition
from .models import SafetyError, canonical, digest, identifier, timestamp
from .operations import record_saved_decision
from .read_memo import same_object_digest
from .research_memory import change, key
from .research_records import (CATALOGUE_SCOPE_KIND, RESEARCH_QUESTION_KIND, SCOPED_EVIDENCE_KIND,
                               is_paid_query_task, iter_catalogue_scopes, iter_scoped_evidence)


CATALOGUE_RELATIONSHIPS = ("same_scope", "narrower", "broader_context", "unrelated", "unclear")
EVIDENCE_RELATIONSHIPS = ("exact_product", "relevant_family", "broader_context", "example_offer", "unrelated", "unclear")
EVIDENCE_SCOPES = ("narrow_type", "broader_context", "example_offer", "supplier_company", "supplier_offer")
SUPPORT_STATES = ("positive", "neutral", "not_positive", "insufficient")
_REFERENCE_TABLES = ("observations", "captures", "trends_evidence", "source_checks")
_SOURCE_CHECK_KINDS = ("supplier_route_search", "all_source_receipt_v1")
_MAX_RECORD_BYTES = 512 * 1024


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise SafetyError("RESEARCH_SCOPE_TEXT_INVALID")
    return value


def _hash(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise SafetyError("RESEARCH_SCOPE_HASH_INVALID")
    return value


def _subject(state, concept_id, expected_meaning_hash=None):
    meaning = effective_functional_definition(state, concept_id)
    if expected_meaning_hash is not None and meaning["meaning_hash"] != _hash(expected_meaning_hash):
        raise SafetyError("RESEARCH_SCOPE_MEANING_CHANGED")
    return {"kind": "concept", "concept_id": concept_id, "definition_hash": meaning["meaning_hash"]}, meaning["basis"]


def _family(state, base_catalogue, family_id):
    identifier(family_id)
    family = next((row for row in effective_catalogue(base_catalogue, state)["families"]
                   if row["id"] == family_id), None)
    if family is None or family["disposition"] != "usable":
        raise SafetyError("RESEARCH_SCOPE_FAMILY_NOT_ACCEPTED")
    # Aliases, wording choices and review dates do not change this meaning.
    scope = {field: deepcopy(family[field]) for field in ("category", "definition", "included", "excluded")}
    return {"family_id": family_id, "scope": scope, "scope_hash": digest(scope)}


def _route_source(route):
    from .source_coverage import ROUTES
    if not isinstance(route, str) or route not in ROUTES:
        raise SafetyError("RESEARCH_SCOPE_ROUTE_INVALID")
    return ("meta" if route == "meta-ads" else "amazon" if route.startswith("amazon-") else
            "flipkart" if route == "flipkart-search" else route if route in ("blinkit", "zepto", "instamart") else
            "trends" if route == "google-trends" else "supplier")


def _reference_record(state, table, record_id):
    if table not in _REFERENCE_TABLES:
        raise SafetyError("RESEARCH_SCOPE_REFERENCE_INVALID")
    identifier(record_id)
    row = state.get(table, {}).get(record_id)
    if not isinstance(row, dict):
        raise SafetyError("RESEARCH_SCOPE_REFERENCE_MISSING")
    if table == "source_checks" and row.get("kind") not in _SOURCE_CHECK_KINDS:
        raise SafetyError("RESEARCH_SCOPE_REFERENCE_NOT_SOURCE")
    if table == "observations":
        identifier(row.get("semantic_id"))
        if not isinstance(row.get("facts"), dict) or not isinstance(row.get("semantic"), dict):
            raise SafetyError("RESEARCH_SCOPE_REFERENCE_INVALID")
    return row


def _source_context(table, record_id, row):
    """Copy actual context only; missing location/query stays explicitly absent."""
    context = {"table": table, "id": record_id, "source": row.get("source")}
    if table == "trends_evidence":
        metadata = row.get("parsed", {}).get("metadata", {})
        context.update(source="trends", observed_at=metadata.get("collected_at"),
                       request=deepcopy(metadata), original_reference=metadata.get("source_reference"))
    elif table == "source_checks":
        context.update(observed_at=row.get("at"), route=row.get("route"),
                       original_reference=deepcopy(row.get("reference")),
                       request_hash=row.get("request_hash"), query=row.get("query"))
        context["source"] = _route_source(row.get("route"))
        if isinstance(row.get("envelope"), dict):
            context["acquisition_id"] = row["envelope"].get("acquisition_id")
        else:
            context["acquisition_id"] = row.get("acquisition_id")
    else:
        context.update(observed_at=row.get("observed_at"), acquisition_id=row.get("acquisition_id"))
        if table == "captures":
            reference = row.get("reference")
            context["original_reference"] = deepcopy(reference)
            request = row.get("request")
            if request is None and isinstance(reference, dict):
                request = reference.get("request")
            context["request"] = deepcopy(request)
            context["route"] = reference.get("route") if isinstance(reference, dict) else row.get("route")
            context["capture_hash"] = row.get("capture_hash")
        else:
            facts = row.get("facts", {})
            context.update(capture_id=row.get("capture_id"), source_item=row.get("source_item"),
                           semantic_id=row.get("semantic_id"), location=deepcopy(facts.get("location")),
                           query=facts.get("query"), route=facts.get("source_route"),
                           original_reference=facts.get("url") or row.get("semantic", {}).get("product_url"))
            if context["route"] is None:
                context["route"] = ("meta-ads" if row.get("source") == "meta" else
                    row["source"] if row.get("source") in ("blinkit", "zepto", "instamart") else
                    {"bestsellers": "amazon-bestsellers", "movers-and-shakers": "amazon-movers-and-shakers",
                     "product-search": "amazon-search" if row.get("source") == "amazon" else "flipkart-search",
                     "product_search": "amazon-search" if row.get("source") == "amazon" else "flipkart-search"}.get(facts.get("list_type")))
    timestamp(context.get("observed_at"))
    return context


def _references(state, references, *, as_of, route=None):
    if not isinstance(references, list) or not 1 <= len(references) <= 40:
        raise SafetyError("RESEARCH_SCOPE_REFERENCES_INVALID")
    requested = []
    for ref in references:
        if not isinstance(ref, dict) or not {"table", "id"} <= set(ref) or not set(ref) <= {"table", "id", "hash"}:
            raise SafetyError("RESEARCH_SCOPE_REFERENCE_INVALID")
        row = _reference_record(state, ref["table"], ref["id"])
        if "hash" in ref and _hash(ref["hash"]) != digest(row):
            raise SafetyError("RESEARCH_SCOPE_REFERENCE_CHANGED")
        pair = (ref["table"], ref["id"])
        if pair in requested:
            raise SafetyError("RESEARCH_SCOPE_REFERENCE_DUPLICATED")
        requested.append(pair)
    pairs = set(requested)
    for table, record_id in requested:
        if table == "observations":
            observation = state[table][record_id]
            capture_id = observation.get("capture_id")
            capture = _reference_record(state, "captures", capture_id)
            if any(observation.get(field) != capture.get(field) for field in ("source", "acquisition_id", "observed_at")):
                raise SafetyError("RESEARCH_SCOPE_CAPTURE_MISMATCH")
            pairs.add(("captures", capture_id))
    bound, contexts = [], []
    expected_source = _route_source(route) if route is not None else None
    for table, record_id in sorted(pairs):
        row = _reference_record(state, table, record_id)
        context = _source_context(table, record_id, row)
        if timestamp(context["observed_at"]) > timestamp(as_of):
            raise SafetyError("RESEARCH_SCOPE_REVIEW_BEFORE_SOURCE")
        if expected_source is not None and context["source"] != expected_source:
            raise SafetyError("RESEARCH_SCOPE_SOURCE_MISMATCH")
        actual_route = context.get("route")
        if route is not None and actual_route is not None and actual_route != route:
            raise SafetyError("RESEARCH_SCOPE_SOURCE_ROUTE_MISMATCH")
        bound.append({"table": table, "id": record_id, "hash": same_object_digest(row)})
        contexts.append(context)
    semantic_ids = {state[table][record_id].get("semantic_id") for table, record_id in pairs if table == "observations"}
    associations = {record_id: same_object_digest(row) for record_id, row in sorted(state.get("associations", {}).items())
                    if row.get("semantic_id") in semantic_ids}
    # Original paid request bounds live on the genuine prepared owner, not in
    # a later question or the observation's short query label.
    attempts = {state[table][record_id].get("reference", {}).get("attempt_id")
                for table, record_id in pairs if table == "captures"
                and isinstance(state[table][record_id].get("reference"), dict)} - {None}
    requests = {}
    for task_id, task in state.get("query_tasks", {}).items():
        if is_paid_query_task(task) and task["attempt_id"] in attempts:
            original = {field: deepcopy(task[field]) for field in ("attempt_id", "config_hash", "request")}
            requests[task_id] = {**original, "hash": digest(original)}
    return bound, contexts, associations, requests


def _meta_request(value):
    """Retain an actual public request, never a local receipt/file location."""
    from .source_coverage import _request
    request = _request(value)
    def public(item):
        if isinstance(item, dict):
            for name, child in item.items():
                if name.casefold() in {"file", "filename", "path", "folder", "receipt", "receipt_file"}:
                    raise SafetyError("RESEARCH_SCOPE_META_REQUEST_INVALID")
                public(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                public(child)
        elif isinstance(item, str) and (item.startswith(("/", "\\", "file:"))
                or len(item) > 2 and item[1:3] in (":\\", ":/")):
            raise SafetyError("RESEARCH_SCOPE_META_REQUEST_INVALID")
    public(request)
    return request


def _meta_original(state, observation_id):
    observation = _reference_record(state, "observations", observation_id)
    capture_id = observation.get("capture_id")
    capture = _reference_record(state, "captures", capture_id)
    if observation.get("source") != "meta" or any(
            observation.get(field) != capture.get(field) for field in ("source", "acquisition_id", "observed_at")):
        raise SafetyError("RESEARCH_SCOPE_CAPTURE_MISMATCH")
    return observation, capture_id, capture


def _meta_task_request(state, observation_id, task_id, receipt_id):
    """An owning receipt must nominate BOTH the original ad and its capture."""
    from .source_coverage import CHECK_KIND, TASK_KIND
    observation, capture_id, _ = _meta_original(state, observation_id)
    task = state.get("query_tasks", {}).get(task_id, {})
    receipt = state.get("source_checks", {}).get(receipt_id, {})
    envelope = receipt.get("envelope", {})
    request = _meta_request(task.get("request"))
    acquisition_id = observation["acquisition_id"]
    if (task.get("kind") != TASK_KIND or task.get("route") != "meta-ads"
            or task_id != key("source-task", [task.get("run_id"), "meta-ads", request])
            or task.get("request_hash") != digest(request) or receipt_id not in task.get("receipt_refs", [])
            or receipt.get("kind") != CHECK_KIND or receipt.get("task_id") != task_id
            or receipt.get("route") != "meta-ads" or envelope.get("route") != "meta-ads"
            or receipt.get("request_hash") != digest(request) or receipt.get("envelope_hash") != digest(envelope)
            or receipt_id != key("source-receipt", [task_id, acquisition_id, digest(envelope)])
            or envelope.get("acquisition_id") != acquisition_id
            or acquisition_id not in set(task.get("attempt_refs", [])) | {task.get("acquisition_id")}
            or envelope.get("dispatch_started") is not True or envelope.get("observed_at") != observation["observed_at"]
            or observation_id not in envelope.get("observation_ids", []) or capture_id not in envelope.get("capture_ids", [])):
        raise SafetyError("RESEARCH_SCOPE_META_REQUEST_BINDING_INVALID")
    # Mutable task progress is not a new collection or a changed request.
    binding = {field: deepcopy(task[field]) for field in ("kind", "run_id", "route", "request", "request_hash", "prepared_at")}
    return request, binding


def meta_capture_context(state, observation_id):
    """Pure source-v4 input projection; no guessed query for orphan imports."""
    from .source_coverage import TASK_KIND
    observation, capture_id, capture = _meta_original(state, observation_id)
    context = {"state": "not_saved", "capture": {"table": "captures", "id": capture_id, "hash": digest(capture)},
        "observed_at": observation["observed_at"], "request": None, "request_basis": "not_saved",
        "original_task": None, "original_receipt": None}
    request = capture.get("request")
    if request is None and isinstance(capture.get("reference"), dict):
        request = capture["reference"].get("request")
    if request is not None:
        context.update(state="verified", request=_meta_request(request), request_basis="saved_capture_request")
        return context
    candidates = []
    for task_id, task in sorted(state.get("query_tasks", {}).items()):
        if task.get("kind") != TASK_KIND or task.get("route") != "meta-ads":
            continue
        for receipt_id in sorted(task.get("receipt_refs", [])):
            envelope = state.get("source_checks", {}).get(receipt_id, {}).get("envelope", {})
            if (observation_id not in envelope.get("observation_ids", [])
                    or capture_id not in envelope.get("capture_ids", [])):
                continue
            request, _ = _meta_task_request(state, observation_id, task_id, receipt_id)
            candidates.append((task_id, receipt_id, request))
    if candidates and len({digest(row[2]) for row in candidates}) == 1:
        task_id, receipt_id, request = candidates[0]
        context.update(state="verified", request=request, request_basis="receipt_bound_task_request",
            original_task={"table": "query_tasks", "id": task_id, "hash": digest(state["query_tasks"][task_id])},
            original_receipt={"table": "source_checks", "id": receipt_id, "hash": digest(state["source_checks"][receipt_id])})
    return context


def _meta_context_proof(state, observation_id, context, *, task_binding=None, task_snapshot=None, exact_task_hash=True):
    """Check input once exactly; thereafter pin original request, not progress."""
    from .research_ai import validate_source_capture_context
    validate_source_capture_context({"source": "meta", "observed_at": context.get("observed_at"), "capture_context": context})
    observation, capture_id, capture = _meta_original(state, observation_id)
    if (context["capture"] != {"table": "captures", "id": capture_id, "hash": digest(capture)}
            or context["observed_at"] != observation["observed_at"]):
        raise SafetyError("RESEARCH_SCOPE_META_REQUEST_BINDING_INVALID")
    if context["request_basis"] != "receipt_bound_task_request":
        if meta_capture_context(state, observation_id) != context:
            raise SafetyError("RESEARCH_SCOPE_META_REQUEST_BINDING_INVALID")
        return {"context": deepcopy(context), "original_task_binding": None, "original_task_snapshot": None}
    task_ref, receipt_ref = context["original_task"], context["original_receipt"]
    if (digest(state["source_checks"].get(receipt_ref["id"])) != receipt_ref["hash"]
            or exact_task_hash and digest(state["query_tasks"].get(task_ref["id"])) != task_ref["hash"]):
        raise SafetyError("RESEARCH_SCOPE_META_REQUEST_BINDING_INVALID")
    request, binding = _meta_task_request(state, observation_id, task_ref["id"], receipt_ref["id"])
    if request != context["request"] or task_binding is not None and binding != task_binding:
        raise SafetyError("RESEARCH_SCOPE_META_REQUEST_BINDING_INVALID")
    snapshot = state["query_tasks"][task_ref["id"]] if exact_task_hash else task_snapshot
    if (not isinstance(snapshot, dict) or digest(snapshot) != task_ref["hash"]
            or {field: snapshot.get(field) for field in binding} != binding):
        raise SafetyError("RESEARCH_SCOPE_META_REQUEST_BINDING_INVALID")
    return {"context": deepcopy(context), "original_task_binding": binding, "original_task_snapshot": deepcopy(snapshot)}


def _field_values(state, fields, bound):
    if not isinstance(fields, list) or len(fields) > 80:
        raise SafetyError("RESEARCH_SCOPE_USABLE_FIELDS_INVALID")
    refs = {(ref["table"], ref["id"]) for ref in bound}
    result = []
    for field in fields:
        if not isinstance(field, dict) or set(field) != {"reference", "path"}:
            raise SafetyError("RESEARCH_SCOPE_USABLE_FIELDS_INVALID")
        ref, path = field["reference"], field["path"]
        if (not isinstance(ref, dict) or set(ref) != {"table", "id"}
                or not isinstance(ref["table"], str) or not isinstance(ref["id"], str) or (ref["table"], ref["id"]) not in refs
                or not isinstance(path, list) or not 1 <= len(path) <= 12
                or any(not (isinstance(part, str) and part or type(part) is int and 0 <= part <= 10000) for part in path)):
            raise SafetyError("RESEARCH_SCOPE_USABLE_FIELDS_INVALID")
        row = state[ref["table"]][ref["id"]]
        value = row
        try:
            for part in path:
                if not (isinstance(value, dict) and isinstance(part, str) or isinstance(value, list) and type(part) is int):
                    raise SafetyError("RESEARCH_SCOPE_USABLE_FIELD_MISSING")
                value = value[part]
        except (KeyError, TypeError, IndexError):
            raise SafetyError("RESEARCH_SCOPE_USABLE_FIELD_MISSING") from None
        item = {"reference": deepcopy(ref), "path": deepcopy(path), "value": deepcopy(value), "hash": digest(value)}
        if item in result:
            raise SafetyError("RESEARCH_SCOPE_USABLE_FIELD_DUPLICATED")
        result.append(item)
    return sorted(result, key=lambda item: canonical([item["reference"], item["path"]]))


def _question(state, question_id, subject, route):
    if question_id is None:
        return None
    from .research_questions import validate_question_record, validate_subject
    identifier(question_id)
    row = state.get("query_tasks", {}).get(question_id)
    if not isinstance(row, dict) or row.get("kind") != RESEARCH_QUESTION_KIND:
        raise SafetyError("RESEARCH_SCOPE_QUESTION_INVALID")
    validate_question_record(row)
    binding = row.get("binding")
    if not isinstance(binding, dict) or digest(binding) != row.get("binding_hash"):
        raise SafetyError("RESEARCH_SCOPE_QUESTION_INVALID")
    actual_subject = validate_subject(state, row.get("subject"))
    if (actual_subject.get("kind") != "concept" or actual_subject.get("concept_id") != subject["concept_id"]
            or actual_subject.get("definition_hash") != subject["definition_hash"] or row.get("route") != route
            or any(row.get(field) != binding.get(field) for field in ("subject", "route", "purpose", "physical_request_fingerprint"))):
        raise SafetyError("RESEARCH_SCOPE_QUESTION_INVALID")
    for dependency in binding.get("dependencies", []):
        if digest(state.get(dependency["table"], {}).get(dependency["id"])) != dependency["hash"]:
            raise SafetyError("RESEARCH_SCOPE_QUESTION_DEPENDENCY_CHANGED")
    return {"id": question_id, "binding_hash": row["binding_hash"],
            "physical_request_fingerprint": row["physical_request_fingerprint"]}


def _job(memory, state, job_id, job_item_id, references, concept_id):
    if job_id is None:
        if job_item_id is not None:
            raise SafetyError("RESEARCH_SCOPE_JOB_INVALID")
        return None, None
    from .research_ai import MAX_JOB_BYTES, current_attempt, validate_results
    from .storage import load_json, safe_path
    identifier(job_id); identifier(job_item_id)
    job = state.get("jobs", {}).get(job_id)
    try:
        request = job["request"]
        payload = request["payload"]
        item = next(value for value in payload["items"] if value["id"] == job_item_id)
        if (item.get("id") != concept_id and item.get("concept_id") != concept_id
                and item.get("product", {}).get("concept_id") != concept_id):
            raise SafetyError("RESEARCH_SCOPE_JOB_SUBJECT_MISMATCH")
        context = {k: v for k, v in payload.items() if k != "items"}
        binding = digest([request["role"], item, context, request["model"], request["prompt_version"], request["runner"]])
        if job["binding_keys"].get(job_item_id) != binding:
            raise SafetyError("RESEARCH_SCOPE_JOB_INVALID")
        # An unrelated successful item cannot authenticate this review. Current
        # new modes can cite typed refs; existing C/E inputs cite full records.
        def contains(value, ref):
            if isinstance(value, dict):
                if value == ref:
                    return True
                if value.get("id") == ref["id"] and {k: v for k, v in value.items() if k != "id"} == state[ref["table"]][ref["id"]]:
                    return True
                return any(contains(child, ref) for child in value.values())
            return isinstance(value, list) and any(contains(child, ref) for child in value)
        for ref in references:
            bound = {"table": ref["table"], "id": ref["id"], "hash": digest(state[ref["table"]][ref["id"]])}
            if not contains(item, bound):
                raise SafetyError("RESEARCH_SCOPE_JOB_SOURCE_MISMATCH")
        path = current_attempt(safe_path(memory.root, "ai_jobs", job_id)) / "result.json"
        before = path.stat()
        saved = load_json(path, MAX_JOB_BYTES)
        after = path.stat()
        fingerprint = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if fingerprint(before) != fingerprint(after) or saved.get("request_hash") != digest(request):
            raise SafetyError("RESEARCH_SCOPE_JOB_INVALID")
        checked = validate_results(request["role"], payload, saved["response"], declared_version=request["prompt_version"])
        if job_item_id not in checked["accepted"]:
            raise SafetyError("RESEARCH_SCOPE_JOB_NOT_ACCEPTED")
        proof = {"job_id": job_id, "item_id": job_item_id, "job_hash": digest(job), "request_hash": digest(request),
                 "role": request["role"], "prompt_version": request["prompt_version"],
                 "binding_key": binding, "result_hash": digest(saved), "accepted_item_hash": digest(checked["accepted"][job_item_id])}
        from .research_ai import CATALOGUE_SCOPE_PROMPT_VERSION, FUNCTIONAL_SOURCE_PROMPT_VERSION
        if request["role"] == "B" and request["prompt_version"] == CATALOGUE_SCOPE_PROMPT_VERSION:
            proof["accepted_item"] = deepcopy(checked["accepted"][job_item_id])
        if request["role"] == "E" and request["prompt_version"] == FUNCTIONAL_SOURCE_PROMPT_VERSION:
            # State-only market readers authenticate the adopted semantic
            # judgment, including its observed/inferred basis, without opening
            # a model result file or guessing from a natural-language reason.
            proof["accepted_item"] = deepcopy(checked["accepted"][job_item_id])
            selected = {ref["id"] for ref in references if ref["table"] == "observations"}
            capture_contexts = {row["id"]: _meta_context_proof(state, row["id"], row["capture_context"])
                for row in item.get("observations", []) if row["id"] in selected and "capture_context" in row}
            if capture_contexts:
                proof["capture_contexts"] = capture_contexts
        return proof, (path, fingerprint(after))
    except (OSError, KeyError, TypeError, StopIteration):
        raise SafetyError("RESEARCH_SCOPE_JOB_INVALID") from None


def _verified(row, kind):
    if not isinstance(row, dict) or row.get("kind") != kind or row.get("accepted") is not True:
        raise SafetyError("RESEARCH_SCOPE_RECORD_INVALID")
    if row.get("record_hash") != digest({k: v for k, v in row.items() if k != "record_hash"}):
        raise SafetyError("RESEARCH_SCOPE_RECORD_HASH_MISMATCH")
    timestamp(row.get("reviewed_at")); _text(row.get("reviewer")); _text(row.get("reason"))
    common = {"concept_id", "subject", "meaning_basis", "references", "source_contexts", "association_hashes",
              "request_dependencies", "job_proof", "reviewer", "reviewed_at", "reason", "method", "accepted", "kind", "record_hash",
              "binding_key", "revision", "previous_binding_id"}
    required = common | ({"catalogue", "relationship"} if kind == CATALOGUE_SCOPE_KIND else
                         {"source", "route", "scope", "relationship", "support", "admissibility", "question", "usable_fields", "limitations"})
    if set(row) != required:
        raise SafetyError("RESEARCH_SCOPE_RECORD_INVALID")
    _text(row["method"])
    identifier(row["concept_id"])
    if (row["meaning_basis"] not in ("legacy_concept", "accepted_definition")
            or not isinstance(row["subject"], dict) or not isinstance(row["references"], list)
            or not isinstance(row["source_contexts"], list) or not isinstance(row["association_hashes"], dict)
            or not isinstance(row["request_dependencies"], dict)
            or row["job_proof"] is not None and not isinstance(row["job_proof"], dict)
            or kind == CATALOGUE_SCOPE_KIND and not isinstance(row["catalogue"], dict)
            or kind == SCOPED_EVIDENCE_KIND and (
                not isinstance(row["usable_fields"], list) or not isinstance(row["limitations"], list))):
        raise SafetyError("RESEARCH_SCOPE_RECORD_INVALID")
    if type(row["revision"]) is not int or row["revision"] < 1 or row["binding_key"] != _binding_key(row):
        raise SafetyError("RESEARCH_SCOPE_REVISION_INVALID")
    return row


def _current_dependencies(state, row, *, as_of=None):
    at = row["reviewed_at"]
    if as_of is not None and timestamp(at) > timestamp(as_of):
        return False
    try:
        subject, _ = _subject(state, row["concept_id"])
        if row.get("subject") != subject:
            return False
        bound, contexts, associations, requests = _references(state, row["references"], as_of=at, route=row.get("route"))
        if (bound != row["references"] or contexts != row["source_contexts"]
                or associations != row["association_hashes"] or requests != row["request_dependencies"]):
            return False
        if row.get("question") != _question(state, row.get("question", {}).get("id") if row.get("question") else None,
                                            subject, row.get("route")):
            return False
        job = row.get("job_proof")
        if job is not None and same_object_digest(state.get("jobs", {}).get(job["job_id"])) != job["job_hash"]:
            return False
        from .catalogue_scope_review import METHOD, PROMPT_VERSION, validate_scope_adoption
        if row["method"] == METHOD or (job or {}).get("prompt_version") == PROMPT_VERSION:
            validate_scope_adoption(state, row)
        for observation_id, proof in (job or {}).get("capture_contexts", {}).items():
            if _meta_context_proof(state, observation_id, proof["context"],
                    task_binding=proof["original_task_binding"], task_snapshot=proof["original_task_snapshot"],
                    exact_task_hash=False) != proof:
                return False
    except (SafetyError, KeyError, TypeError):
        return False
    return True


def _intent(record):
    return {k: v for k, v in record.items() if k not in (
        "reviewed_at", "meaning_basis", "record_hash", "binding_key", "revision", "previous_binding_id")}


def _record_id(record):
    return key("catalogue-scope" if record["kind"] == CATALOGUE_SCOPE_KIND else "scoped-evidence", _intent(record))


def _binding_key(row):
    try:
        return _scope_binding_key(row)
    except (KeyError, TypeError, AttributeError):
        raise SafetyError("RESEARCH_SCOPE_RECORD_INVALID") from None


def _scope_binding_key(row):
    job = row.get("job_proof")
    binding = {"kind": row["kind"], "subject": row["subject"], "method": row["method"],
               "role": job["role"] if job else "operator", "role_contract": job["prompt_version"] if job else "explicit-source-scope-v1",
               "references": row["references"]}
    if row["kind"] == CATALOGUE_SCOPE_KIND:
        binding["catalogue"] = row["catalogue"]
    else:
        binding.update(route=row["route"], scope=row["scope"], question=row["question"],
                       selected_fields=[{key: field[key] for key in ("reference", "path", "hash")} for field in row["usable_fields"]])
    return key("scope-binding", binding)


def _envelopes(state, kind):
    """Every verified record envelope of one kind, in saved order, up to the first bad one.

    Returns (rows, error); the reader raises that error at the same position.
    """
    reader = iter_catalogue_scopes if kind == CATALOGUE_SCOPE_KIND else iter_scoped_evidence
    rows = []
    for record_id, raw in reader(state):
        try:
            if raw.get("record_hash") != digest({k: v for k, v in raw.items() if k != "record_hash"}):
                raise SafetyError("RESEARCH_SCOPE_RECORD_HASH_MISMATCH")
            if record_id != _record_id(raw):
                raise SafetyError("RESEARCH_SCOPE_RECORD_ID_MISMATCH")
        except (SafetyError, KeyError, TypeError, ValueError, AttributeError) as error:
            # A data error is fixed by these saved records; _heads raises it in order.
            return rows, error
        rows.append((record_id, raw))
    return rows, None


def _heads(state, kind, concept_id=None, *, binding_key=None, as_of=None):
    from .read_memo import remember
    envelopes, error = remember("research_scope_envelopes", state, (kind,), lambda: _envelopes(state, kind),
                                tables=("source_checks",), copy=False)
    groups = {}
    for record_id, raw in envelopes:
        # Subject fields are lookup hints until the exact envelope is verified.
        # Keep expensive source/currentness checks scoped to the selected type.
        subject = raw.get("subject")
        if concept_id is not None and (not isinstance(subject, dict)
                or subject.get("kind") != "concept" or subject.get("concept_id") != concept_id):
            continue
        row = _verified(raw, kind)
        if record_id != _record_id(row):
            raise SafetyError("RESEARCH_SCOPE_RECORD_ID_MISMATCH")
        # A saved lookup key is not proof that a correction belongs elsewhere.
        # Validate its original binding before using it to exclude that review.
        if binding_key is not None and row["binding_key"] != binding_key:
            continue
        groups.setdefault(row["binding_key"], []).append((record_id, row))
    if error is not None:
        if type(error) is SafetyError:
            raise SafetyError(*error.args) from None
        raise error
    result = {}
    for binding, rows in groups.items():
        previous, previous_time, chosen = None, None, None
        for revision, (record_id, row) in enumerate(sorted(rows, key=lambda pair: pair[1]["revision"]), 1):
            reviewed = timestamp(row["reviewed_at"])
            if (row["revision"] != revision or row["previous_binding_id"] != previous
                    or previous_time is not None and reviewed < previous_time):
                raise SafetyError("RESEARCH_SCOPE_REVISION_CHAIN_INVALID")
            previous, previous_time = record_id, reviewed
            if as_of is None or reviewed <= timestamp(as_of):
                chosen = (record_id, row)
        if chosen is not None:
            result[binding] = chosen
    return result


def _commit(memory, state, record, *, dependency_check, artifact=None):
    # Original review time is provenance, not a reason to duplicate unchanged
    # reviewed meaning/facts. Exact repeat preparation retains the first date.
    record_id = _record_id(record)
    binding_key = _binding_key(record)
    head = _heads(state, record["kind"], record["concept_id"], binding_key=binding_key).get(binding_key)
    old = state["source_checks"].get(record_id)
    if old is not None:
        _verified(old, record["kind"])
        if _intent(old) != _intent(record):
            raise SafetyError("RESEARCH_SCOPE_RECORD_CONFLICT")
        if not dependency_check(state, old):
            raise SafetyError("RESEARCH_SCOPE_DEPENDENCY_CHANGED")
        return {"id": record_id, "reused": True, "current": head is not None and head[0] == record_id}
    if head is not None and timestamp(record["reviewed_at"]) < timestamp(head[1]["reviewed_at"]):
        raise SafetyError("RESEARCH_SCOPE_REVIEW_ORDER_INVALID")
    record.update(binding_key=binding_key, revision=1 if head is None else head[1]["revision"] + 1,
                  previous_binding_id=None if head is None else head[0])
    record["record_hash"] = digest(record)
    if len(canonical(record)) > _MAX_RECORD_BYTES:
        raise SafetyError("RESEARCH_SCOPE_SIZE_LIMIT")
    def unchanged(latest):
        latest_head = _heads(latest, record["kind"], record["concept_id"], binding_key=binding_key).get(binding_key)
        if latest_head != head:
            raise SafetyError("RESEARCH_SCOPE_REVISION_CHANGED")
        if not dependency_check(latest, record):
            raise SafetyError("RESEARCH_SCOPE_DEPENDENCY_CHANGED")
        if artifact is not None:
            path, expected = artifact
            try:
                actual = path.stat()
                if (actual.st_dev, actual.st_ino, actual.st_size, actual.st_mtime_ns, actual.st_ctime_ns) != expected:
                    raise SafetyError("RESEARCH_SCOPE_JOB_CHANGED")
            except OSError:
                raise SafetyError("RESEARCH_SCOPE_JOB_CHANGED") from None
    committed = memory.commit(key("accept-research-scope", record_id), [change("source_checks", record_id, record)], validate=unchanged)
    if record["kind"] in {CATALOGUE_SCOPE_KIND, SCOPED_EVIDENCE_KIND}:
        record_saved_decision(committed, record_kind=record["kind"],
            record_id=record_id, concept_id=record["concept_id"])
    return {"id": record_id, "reused": False, "current": True}


def accept_catalogue_scope(memory, base_catalogue, concept_id, family_id, *, relationship, references,
                           reason, reviewer, as_of, expected_meaning_hash, method="operator_source_review",
                           job_id=None, job_item_id=None):
    """Record an explicit heading/type relationship, never infer it from names."""
    if relationship not in CATALOGUE_RELATIONSHIPS:
        raise SafetyError("RESEARCH_SCOPE_RELATIONSHIP_INVALID")
    timestamp(as_of); _text(reason); _text(reviewer); _text(method); _hash(expected_meaning_hash)
    state = memory.snapshot()
    subject, basis = _subject(state, concept_id, expected_meaning_hash)
    family = _family(state, base_catalogue, family_id)
    bound, contexts, associations, requests = _references(state, references, as_of=as_of)
    job, artifact = _job(memory, state, job_id, job_item_id, references, concept_id)
    record = {"kind": CATALOGUE_SCOPE_KIND, "concept_id": concept_id, "subject": subject, "meaning_basis": basis,
              "catalogue": family, "relationship": relationship, "references": bound, "source_contexts": contexts,
              "association_hashes": associations, "request_dependencies": requests, "job_proof": job,
              "reviewer": reviewer, "reviewed_at": as_of, "reason": reason, "method": method, "accepted": True}
    def current(latest, row):
        try:
            return (_current_dependencies(latest, row)
                    and _family(latest, base_catalogue, family_id) == row["catalogue"])
        except SafetyError:
            return False
    return _commit(memory, state, record, dependency_check=current, artifact=artifact)


def current_catalogue_scopes(state, base_catalogue, concept_id=None, *, as_of=None):
    """Accepted current reviews, including explicit unrelated/unclear outcomes."""
    result = []
    for record_id, row in _heads(state, CATALOGUE_SCOPE_KIND, concept_id, as_of=as_of).values():
        if row.get("relationship") not in CATALOGUE_RELATIONSHIPS:
            raise SafetyError("RESEARCH_SCOPE_RELATIONSHIP_INVALID")
        try:
            current = _family(state, base_catalogue, row["catalogue"]["family_id"])
            if current == row["catalogue"] and _current_dependencies(state, row, as_of=as_of):
                result.append({"id": record_id, **deepcopy(row)})
        except SafetyError:
            continue
    return sorted(result, key=lambda row: row["id"])


def _evidence_contract(scope, relationship, support):
    if scope not in EVIDENCE_SCOPES or relationship not in EVIDENCE_RELATIONSHIPS or support not in SUPPORT_STATES:
        raise SafetyError("RESEARCH_SCOPE_EVIDENCE_INVALID")
    allowed = {"narrow_type": ("exact_product", "relevant_family"), "broader_context": ("broader_context",),
               "example_offer": ("exact_product", "relevant_family", "example_offer"),
               "supplier_company": ("relevant_family", "broader_context"),
               "supplier_offer": ("exact_product", "relevant_family", "example_offer")}
    if relationship not in (*allowed[scope], "unrelated", "unclear"):
        raise SafetyError("RESEARCH_SCOPE_RELATIONSHIP_SCOPE_MISMATCH")
    return relationship if relationship in ("unrelated", "unclear") else "accepted"


def _support_fields(support, fields, contexts, requests, *, source_requests=()):
    if support == "insufficient":
        return
    if not fields:
        raise SafetyError("RESEARCH_SCOPE_SUPPORT_FIELDS_REQUIRED")
    if not (requests or any(isinstance(request, dict) and request for request in source_requests)
            or any(isinstance(context.get("request"), dict) and context["request"]
            or isinstance(context.get("query"), str) and context["query"] for context in contexts)):
        raise SafetyError("RESEARCH_SCOPE_REQUEST_CONTEXT_REQUIRED")
    if not any((field["reference"]["table"] == "observations" and field["path"][0] in ("semantic", "facts")
            or field["reference"]["table"] == "trends_evidence" and field["path"][:2] == ["parsed", "series"]
            or field["reference"]["table"] == "source_checks" and field["path"][0] == "candidates")
            and field["value"] not in (None, "", [], {}) for field in fields):
        raise SafetyError("RESEARCH_SCOPE_SUBSTANTIVE_FIELDS_REQUIRED")


def accept_scoped_evidence(memory, concept_id, *, route, scope, relationship, support, references,
                           usable_fields, limitations, reason, reviewer, as_of, expected_meaning_hash,
                           method="operator_source_review", question_id=None, job_id=None, job_item_id=None):
    """Accept one reviewed source binding; no scores, market counts or writes elsewhere."""
    admissibility = _evidence_contract(scope, relationship, support)
    if not isinstance(limitations, list) or len(limitations) > 40:
        raise SafetyError("RESEARCH_SCOPE_LIMITATIONS_INVALID")
    limitations = [_text(item) for item in limitations]
    timestamp(as_of); _text(reason); _text(reviewer); _text(method); _hash(expected_meaning_hash)
    state = memory.snapshot()
    subject, basis = _subject(state, concept_id, expected_meaning_hash)
    source = _route_source(route)
    if scope.startswith("supplier_") != (source == "supplier"):
        raise SafetyError("RESEARCH_SCOPE_SUPPLIER_SCOPE_INVALID")
    bound, contexts, associations, requests = _references(state, references, as_of=as_of, route=route)
    fields = _field_values(state, usable_fields, bound)
    question = _question(state, question_id, subject, route)
    job, artifact = _job(memory, state, job_id, job_item_id, references, concept_id)
    _support_fields(support, fields, contexts, requests,
        source_requests=[proof["context"]["request"] for proof in (job or {}).get("capture_contexts", {}).values()
                         if proof["context"]["state"] == "verified"])
    record = {"kind": SCOPED_EVIDENCE_KIND, "concept_id": concept_id, "subject": subject, "meaning_basis": basis,
              "source": source, "route": route, "scope": scope, "relationship": relationship, "support": support,
              "admissibility": admissibility, "references": bound, "source_contexts": contexts,
              "association_hashes": associations, "request_dependencies": requests, "question": question,
              "usable_fields": fields, "limitations": limitations, "reason": reason, "method": method, "reviewer": reviewer,
              "reviewed_at": as_of, "job_proof": job, "accepted": True}
    return _commit(memory, state, record, dependency_check=_current_dependencies, artifact=artifact)


def validated_scoped_evidence(state, concept_id, *, as_of=None):
    """Current accepted reviews, not votes or proof of national demand/freshness.

    Broader, example and supplier findings remain distinguishable. Unrelated or
    unclear reviews remain auditable with non-accepted admissibility. Source
    freshness and market arithmetic belong to downstream policy readers.
    Inside one pure read scope the same answer is reused for the same state.
    Every call returns its own deep copy of each current record, as before.
    """
    from .read_memo import remember
    rows = remember("validated_scoped_evidence", state, (concept_id, as_of),
                    lambda: _validated_scoped_evidence(state, concept_id, as_of=as_of), copy=False)
    return [deepcopy(row) for row in rows]


def _validated_scoped_evidence(state, concept_id, *, as_of=None):
    """Rows over the unchanged saved records; the public reader copies them."""
    result = []
    for record_id, row in _heads(state, SCOPED_EVIDENCE_KIND, concept_id, as_of=as_of).values():
        expected = _evidence_contract(row.get("scope"), row.get("relationship"), row.get("support"))
        if row.get("admissibility") != expected or row.get("source") != _route_source(row.get("route")):
            raise SafetyError("RESEARCH_SCOPE_EVIDENCE_INVALID")
        if row["scope"].startswith("supplier_") != (row["source"] == "supplier"):
            raise SafetyError("RESEARCH_SCOPE_SUPPLIER_SCOPE_INVALID")
        if _current_dependencies(state, row, as_of=as_of):
            fields = [{"reference": field["reference"], "path": field["path"]} for field in row["usable_fields"]]
            if _field_values(state, fields, row["references"]) != row["usable_fields"]:
                raise SafetyError("RESEARCH_SCOPE_USABLE_FIELDS_CHANGED")
            _support_fields(row["support"], row["usable_fields"], row["source_contexts"], row["request_dependencies"],
                source_requests=[proof["context"]["request"] for proof in (row.get("job_proof") or {}).get("capture_contexts", {}).values()
                                 if proof["context"]["state"] == "verified"])
            result.append({"id": record_id, **row})
    return sorted(result, key=lambda row: row["id"])
