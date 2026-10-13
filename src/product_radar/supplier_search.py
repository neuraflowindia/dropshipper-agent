"""Shared, sourcing-only public search leaves; no manufactured product rows.

One frozen request owns one HTTP read or one browser read. A saved search can
serve a catalogue direction and later a real type; returned URLs are only
leads for the existing company-page/F review, never a market vote.
"""

from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
import stat

from .models import SafetyError, decode_json, digest, identifier, timestamp, utc_now
from .research_memory import change, key
from .research_questions import physical_request_fingerprint, prepare_research_questions
from .storage import atomic_json, load_json, safe_path
from .supplier_route_acquisition import (MAX_RECORD_BYTES, MAX_RESULTS, PARSER_VERSION, SEARCH_ROUTES,
    VERSION, _actual, _product, _query, fetch_supplier_native_page, parse_supplier_route_receipt,
    supplier_route_url, replay_supplier_search, _supplier_native_output_receipt, _deadline_page_receipt)
from .source_deadline import SourceActionDeadline, source_phase

BRIDGE = "verified-supplier-search-response-v1"
CAPTURE_CONTRACT = "supplier-single-search-v1"
APPLICATION_KIND = "supplier-search-application-v1"
LEGACY_CONTRACT = "supplier-legacy-transport-v1"


def prepare_supplier_request(context):
    route, request = context["route"], context["request"]
    if route not in SEARCH_ROUTES or not isinstance(request, dict):
        raise SafetyError("SUPPLIER_SHARED_REQUEST_INVALID")
    query = _query(request.get("query")); transport = request.get("transport", "public-http")
    if transport not in {"public-http", "public-browser"}:
        raise SafetyError("SUPPLIER_SHARED_TRANSPORT_INVALID")
    wanted = {"query": query, "country": "IN", "url": supplier_route_url(route, query),
              "transport": transport, "limit": MAX_RESULTS}
    if request != wanted and request != {"query": query, "country": "IN"}:
        raise SafetyError("SUPPLIER_SHARED_REQUEST_INVALID")
    return wanted


def _fallback(state, route, request):
    """Only a closed, successful HTTP response with an unreadable layout.

    A parser exception, access refusal, absent body and uncertain original do
    not authorise another request. Browser is a separately frozen question.
    """
    from .source_acquisitions import _owners, _response
    from .source_coverage import source_response_closed
    http = dict(request, transport="public-http")
    owners = _owners(state, physical_request_fingerprint(route, http))
    if not owners: return None
    aid, owner = owners[-1]; response = _response(state, owner)
    if (response and response.get("bridge_contract") == BRIDGE and not response.get("legacy_workflow_id")
            and response["dispatch_started"] is True
            and response["source_state"] == "insufficient" and response["access"].get("http_status") == 200
            and response.get("parse_reason") == "SUPPLIER_ROUTE_LAYOUT_UNRECOGNISED"
            and response["coverage"]["body_complete"] and not response["coverage"]["byte_truncated"]
            and source_response_closed(response)):
        return {"acquisition_id": aid, "response_id": owner["active_response_id"], "response_hash": digest(response)}
    return None


def supplier_request_for_state(state, route, query):
    request = prepare_supplier_request({"route": route, "request": {"query": query, "country": "IN"}})
    # A genuinely retained old browser result is not another browser launch.
    from .source_acquisitions import _owners, _response
    browser = dict(request, transport="public-browser")
    owners = _owners(state, physical_request_fingerprint(route, browser))
    if owners:
        response = _response(state, owners[-1][1])
        if response and response.get("bridge_contract") == BRIDGE and response.get("legacy_workflow_id"):
            return browser
    return dict(request, transport="public-browser") if _fallback(state, route, request) else request


def _item(acquisition_id, request):
    transport = "http" if request["transport"] == "public-http" else "browser"
    return {"id": key("supplier-route-request", [acquisition_id, transport]), "url": request["url"]}


def _native_original(folder, request, acquisition_id):
    """Read a late original closed child, never start native browser recovery."""
    if request["transport"] != "public-browser": raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
    folder = safe_path(folder); native = safe_path(folder / "native")
    paths = [folder / "intent.json", native / "native_result.json", native / "native_dispatch.json"]
    if not all(path.is_file() for path in paths): raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
    from .source_acquisitions import _file
    originals = []
    def retained(path, limit):
        proof, _, raw = _file(path, [folder])
        if len(raw) > limit: raise SafetyError("SAVED_FILE_SIZE_LIMIT")
        originals.append({"path": proof["path"], "sha256": sha256(raw).hexdigest()})
        return decode_json(raw)
    intent = retained(paths[0], MAX_RECORD_BYTES)
    if intent != {"contract": CAPTURE_CONTRACT, "acquisition_id": acquisition_id, "request": request}:
        raise SafetyError("SUPPLIER_SHARED_ORIGINAL_INVALID")
    output = retained(paths[1], 12 * 1024 * 1024); dispatch = retained(paths[2], 16384)
    if (not isinstance(output, dict) or output.get("owned_process_exited") is not True
            or output.get("navigation_started") is not True):
        raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
    if (dispatch != {"url": request["url"], "navigation_started": True,
            "navigation_started_at": output.get("navigation_started_at")}
            or timestamp(output.get("navigation_started_at")) > timestamp(output.get("completed_at"))):
        raise SafetyError("SUPPLIER_SHARED_NATIVE_ORIGINAL_INVALID")
    receipt = _supplier_native_output_receipt(_item(acquisition_id, request), native, output, None)
    if receipt["checked_at"] != output["completed_at"]: raise SafetyError("SUPPLIER_SHARED_NATIVE_ORIGINAL_INVALID")
    page = folder / "page.json"
    if page.is_file(): retained(page, MAX_RECORD_BYTES)
    return {"contract": CAPTURE_CONTRACT, "acquisition_id": acquisition_id, "request": request,
            "receipt": receipt, "hash": digest(receipt), "native_origin": originals}


def _load_leaf(folder, route, request, acquisition_id):
    """Reparse actual retained bytes; parsing alone cannot start a reader."""
    folder = safe_path(folder); page_path = folder / "page.json"
    legacy_path = folder / "legacy-origin.json"
    if not page_path.is_file() and legacy_path.is_file():
        legacy = load_json(legacy_path, MAX_RECORD_BYTES)
        if (not isinstance(legacy, dict) or set(legacy) != {"contract", "root", "task_id", "task", "request", "acquisition_id", "transport"}
                or legacy["contract"] != LEGACY_CONTRACT or legacy["request"] != request
                or legacy["acquisition_id"] != acquisition_id
                or legacy["transport"] != request["transport"]):
            raise SafetyError("SUPPLIER_SHARED_LEGACY_ORIGIN_INVALID")
        task = _legacy_task(legacy["task_id"], legacy["task"], route, request)
        saved = replay_supplier_search(safe_path(legacy["root"]), route=route, query=request["query"], acquisition_id=task["acquisition_id"])
        transport = "http" if request["transport"] == "public-http" else "browser"
        rows = [row for row in saved["record"]["records"] if row["transport"] == transport]
        if not rows: raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
        receipt = rows[0]["receipt"]
        item = _item(task["acquisition_id"], request)
        from .source_acquisitions import _file
        _, _, raw = _file(saved["reference"], [safe_path(legacy["root"])])
        if decode_json(raw) != saved["record"]: raise SafetyError("SUPPLIER_SHARED_ORIGINAL_INVALID")
        original = {"contract": CAPTURE_CONTRACT, "acquisition_id": acquisition_id, "request": request,
                    "receipt": receipt, "hash": digest(receipt),
                    "legacy_origin": {"workflow_id": task["acquisition_id"], "reference": saved["reference"],
                        "sha256": sha256(raw).hexdigest(), "pointer": str(legacy_path)}}
    else:
        if not page_path.is_file():
            original = _native_original(folder, request, acquisition_id)
        else:
            original = load_json(page_path, MAX_RECORD_BYTES)
            if (isinstance(original, dict) and isinstance(original.get("receipt"), dict)
                    and original["receipt"].get("outer_deadline") is True and request["transport"] == "public-browser"):
                try: original = _native_original(folder, request, acquisition_id)
                except SafetyError as error:
                    if str(error) != "SOURCE_ORIGINAL_RECEIPT_PENDING": raise
        item = _item(acquisition_id, request)
    if (not isinstance(original, dict) or set(original) not in ({"contract", "acquisition_id", "request", "receipt", "hash"},
                {"contract", "acquisition_id", "request", "receipt", "hash", "legacy_origin"},
                {"contract", "acquisition_id", "request", "receipt", "hash", "native_origin"})
            or original["contract"] != CAPTURE_CONTRACT or original["acquisition_id"] != acquisition_id
            or original["request"] != request or original["hash"] != digest(original["receipt"])):
        raise SafetyError("SUPPLIER_SHARED_ORIGINAL_INVALID")
    receipt = original["receipt"]
    if (not isinstance(receipt, dict) or receipt.get("id") != item["id"]
            or receipt.get("requested_url") != request["url"]):
        raise SafetyError("SUPPLIER_SHARED_ORIGINAL_INVALID")
    timestamp(receipt.get("checked_at"))
    parsed = parse_supplier_route_receipt(route, request["query"], item, receipt)
    interpretation = {"contract": CAPTURE_CONTRACT, "acquisition_id": acquisition_id, "request": request,
        "original_hash": digest(original), "parser_version": PARSER_VERSION, "parsed": parsed}
    interpretation["hash"] = digest(interpretation)
    suffix = "-" + digest(original)[:16] if original.get("legacy_origin") or original.get("native_origin") else ""
    path = folder / ("capture-parser-" + str(PARSER_VERSION) + suffix + ".json")
    if path.exists():
        if load_json(path, MAX_RECORD_BYTES) != interpretation: raise SafetyError("SUPPLIER_SHARED_PARSE_CHANGED")
    else:
        atomic_json(path, interpretation)
    return original, interpretation, path


def _legacy_task(task_id, task, route, request):
    if (not isinstance(task, dict) or task.get("kind") != "all_source_task_v1"
            or task.get("request_hash") != digest(task.get("request"))
            or task_id != key("source-task", [task.get("run_id"), task.get("route"), task.get("request")])
            or not task.get("acquisition_id") or task.get("source_dispatch_contract") == "shared-source-caller-v1"
            or task.get("route") != route or task.get("request") != {"query": request["query"], "country": "IN"}):
        raise SafetyError("SUPPLIER_SHARED_LEGACY_ORIGIN_INVALID")
    identifier(task["acquisition_id"])
    return task


def _adopt_legacy(memory, route, request, *, owner_id, as_of, root):
    """Pin real old workflow intent and each actually saved transport separately.

    A missing old browser receipt is never treated as a browser attempt or as
    permission to start one. The HTTP owner retains the old workflow ID; an
    actually saved browser uses its existing receipt ID, not a fabricated run.
    """
    from .source_acquisitions import _owners, _install_legacy_origin, _file
    state = memory.snapshot(); origins = {}
    for rid, row in state["query_tasks"].items():
        if row.get("kind") != "all_source_task_v1" or not row.get("acquisition_id"): continue
        if (row.get("request_hash") != digest(row.get("request"))
                or rid != key("source-task", [row.get("run_id"), row.get("route"), row.get("request")])):
            raise SafetyError("SUPPLIER_SHARED_LEGACY_ORIGIN_INVALID")
        if row.get("source_dispatch_contract") == "shared-source-caller-v1": continue
        if row.get("route") == route and row.get("request") == {"query": request["query"], "country": "IN"}:
            _legacy_task(rid, row, route, request)
            origins[row["acquisition_id"]] = (rid, row)
    if len(origins) > 1: return False
    if not origins: return True
    workflow_id, (rid, row) = next(iter(origins.items()))
    saved = None
    try:
        saved = replay_supplier_search(memory.root, route=route, query=request["query"], acquisition_id=workflow_id)
    except SafetyError as error:
        if str(error) != "SOURCE_ORIGINAL_RECEIPT_PENDING": raise
    transports = ["public-http"]
    if saved and any(part["transport"] == "browser" for part in saved["record"]["records"]):
        transports.append("public-browser")
    for transport in transports:
        physical = dict(request, transport=transport)
        aid = workflow_id if transport == "public-http" else _item(workflow_id, physical)["id"]
        state = memory.snapshot(); owners = _owners(state, physical_request_fingerprint(route, physical))
        if owners:
            if owners[-1][0] != aid:
                if owners[-1][1]["state"] == "reserved" and not owners[-1][1]["stage_ids"]: return False
                continue
        pointer = {"contract": LEGACY_CONTRACT, "root": str(safe_path(memory.root)), "task_id": rid,
                   "task": deepcopy(row), "request": physical, "acquisition_id": aid, "transport": transport}
        path = safe_path(root / aid / "legacy-origin.json")
        if path.exists():
            existing = load_json(path, MAX_RECORD_BYTES)
            # Task progress may advance later; its original intent remains.
            if any(existing.get(field) != pointer[field] for field in pointer if field != "task"):
                raise SafetyError("SUPPLIER_SHARED_LEGACY_ORIGIN_INVALID")
            pointer = existing
        else: atomic_json(path, pointer)
        if not owners:
            proof, fingerprint, _ = _file(path, [memory.root, root])
            _install_legacy_origin(memory, state, route, physical, table="query_tasks", record_id=rid, saved=row,
                acquisition_id=aid, owner_id=owner_id, as_of=as_of, artifact_roots=[root], files=[(proof, fingerprint)],
                original_attempt_id=_item(workflow_id, physical)["id"], original_snapshot=True)
        if saved and (transport == "public-http" or transport == "public-browser"):
            _record_response(memory, aid, folder=root / aid, as_of=as_of)
    return True


def _record_response(memory, acquisition_id, *, folder, as_of):
    from .source_acquisitions import _record, _file, _commit_stage, _stage
    from .supplier_effort import _receipt_ref
    state = memory.snapshot(); owner = _record(state, acquisition_id)
    route, request = owner["binding"]["route"], owner["binding"]["request"]
    original, interpreted, path = _load_leaf(folder, route, request, acquisition_id)
    receipt, parsed = original["receipt"], interpreted["parsed"]
    if timestamp(receipt["checked_at"]) > timestamp(as_of): raise SafetyError("SUPPLIER_SHARED_SOURCE_IN_FUTURE")
    roots = owner["binding"]["artifact_roots"]; files, artifacts = [], {}
    proof, fingerprint, raw = _file(path, roots)
    if decode_json(raw) != interpreted: raise SafetyError("SUPPLIER_SHARED_PARSE_CHANGED")
    files.append((proof, fingerprint))
    legacy = original.get("legacy_origin")
    if legacy:
        pointer, fingerprint, raw = _file(legacy["pointer"], roots)
        saved_pointer = decode_json(raw)
        stages = [_stage(state, owner, rid) for rid in owner["stage_ids"]]
        origin = next((row["origin"] for row in stages if row["stage_type"] == "legacy_origin"), None)
        original_task = origin.get("original") if origin else None
        if (not origin or origin["table"] != "query_tasks" or origin["id"] != saved_pointer["task_id"]
                or not isinstance(original_task, dict) or origin["hash"] != digest(original_task)
                or original_task.get("request") != saved_pointer["task"].get("request")
                or original_task.get("acquisition_id") != saved_pointer["task"].get("acquisition_id")
                or safe_path(saved_pointer["root"]) != safe_path(memory.root)):
            raise SafetyError("SUPPLIER_SHARED_LEGACY_ORIGIN_INVALID")
        _legacy_task(origin["id"], original_task, route, request)
        files.append((pointer, fingerprint)); artifacts["legacy-origin.json"] = pointer
        source, fingerprint, raw = _file(legacy["reference"], roots)
        if sha256(raw).hexdigest() != legacy["sha256"]: raise SafetyError("SUPPLIER_SHARED_ORIGINAL_INVALID")
        reference = legacy["reference"]
    elif original.get("native_origin"):
        if not owner.get("active_dispatch_intent_id"): raise SafetyError("SUPPLIER_SHARED_NATIVE_ORIGINAL_INVALID")
        dispatch = _stage(state, owner, owner["active_dispatch_intent_id"])
        if timestamp(receipt["attempt_started_at"]) < timestamp(dispatch["recorded_at"]):
            raise SafetyError("SUPPLIER_SHARED_NATIVE_ORIGINAL_INVALID")
        for position, saved in enumerate(original["native_origin"]):
            source, fingerprint, raw = _file(saved["path"], roots)
            if sha256(raw).hexdigest() != saved["sha256"]: raise SafetyError("SUPPLIER_SHARED_NATIVE_ORIGINAL_INVALID")
            files.append((source, fingerprint)); artifacts["native-original-" + str(position)] = source
        source, fingerprint, raw = _file(original["native_origin"][1]["path"], roots)
        reference = source["path"]
    else:
        source, fingerprint, raw = _file(Path(folder) / "page.json", roots)
        if decode_json(raw) != original: raise SafetyError("SUPPLIER_SHARED_ORIGINAL_INVALID")
        reference = str(Path(folder) / "page.json")
    files.append((source, fingerprint)); artifacts["original.json"] = source
    actual = _actual([receipt], reference)
    checked = _receipt_ref(receipt, reference)
    if actual is True and checked is None: raise SafetyError("SUPPLIER_SHARED_ORIGINAL_INVALID")
    complete = parsed["state"] == "complete" and actual is True
    source_state = ("usable" if parsed["results"] else "empty") if complete else (
        "insufficient" if parsed["state"] == "extraction_pending" else parsed["state"])
    access = {"http_status": receipt.get("http_status"), "retry_after": receipt.get("retry_after")}
    if request["transport"] == "public-http":
        access.update(ownership_kind="thread", owned_worker_exited=receipt.get("owned_worker_exited") is True)
    else:
        access["owned_process_exited"] = receipt.get("owned_process_exited")
    coverage = {"body_complete": receipt.get("body_complete") is True,
        "byte_truncated": receipt.get("text_truncated") is True, "scope_complete": parsed["state"] == "complete",
        "recognized": parsed["state"] == "complete", "delivered_count": len(parsed["results"]),
        "intentional_limit": len(parsed["results"]) == MAX_RESULTS}
    from .source_coverage import source_response_closed
    closed = source_response_closed({"bridge_contract": BRIDGE, "access": access})
    if request["transport"] == "public-http":
        closed = closed and receipt.get("transport_contract") == "public-page-owned-http-v1"
    complete = bool(complete and coverage["body_complete"] and not coverage["byte_truncated"] and closed)
    if not complete and source_state == "empty": source_state = "insufficient"
    if not complete and source_state == "usable": source_state = "partial"
    for prior_id in owner["response_ids"]:
        prior = _stage(state, owner, prior_id)
        recovered_page = next((row for row in original.get("native_origin", [])
                               if Path(row["path"]).name == "page.json"), None)
        original_failure = prior["artifacts"].get("original.json", {})
        older_original_recovery = bool(recovered_page and not prior["complete"]
            and original_failure.get("path") == recovered_page["path"]
            and original_failure.get("sha256") == recovered_page["sha256"])
        if (prior["dispatch_started"] is True and actual is not True
                or timestamp(prior["observed_at"]) > timestamp(receipt["checked_at"]) and not older_original_recovery):
            raise SafetyError("SOURCE_ACQUISITION_RECEIPT_CONFLICT")
    data = {"bridge_contract": BRIDGE, "receipt": proof, "artifacts": artifacts,
        "observed_at": receipt["checked_at"], "dispatch_started": actual, "source_state": source_state,
        "coverage": coverage, "complete": complete, "captures": [], "response_hash": digest(artifacts),
        "parse_reason": parsed["reason"], "access": access,
        "expense_proof": {"collection_channel": "unknown" if legacy else "direct_local",
                          "provider_requests": None if legacy else 0,
                          "provider_charge_usd": None if legacy else "0", "model_calls": 0}}
    if legacy: data["legacy_workflow_id"] = legacy["workflow_id"]
    status = "completed_empty" if complete and source_state == "empty" else (
        "failed_access" if source_state == "blocked" else "response_saved" if actual is True else "execution_unknown")
    result = _commit_stage(memory, owner, "response", data, as_of,
        {"state": status, "active_parse_id": None, "parse_state": "not_started"}, files=files)
    return result, original, parsed


def validate_supplier_lead_receipt(memory, request, acquisition_id, receipt):
    from .source_acquisitions import _record, _response
    from .source_coverage import source_response_closed
    reference = receipt.get("supplier_lead_capture_ref"); state = memory.snapshot()
    owner = _record(state, acquisition_id); response = _response(state, owner)
    expected = prepare_supplier_request({"route": receipt["route"], "request": request})
    if (not isinstance(reference, dict) or set(reference) != {"id", "hash"}
            or owner["binding"]["request"] != expected or owner["binding"]["route"] != receipt["route"]
            or not response or owner["active_response_id"] != reference["id"] or digest(response) != reference["hash"]
            or response.get("bridge_contract") != BRIDGE or response["source_state"] != receipt["state"]
            or response["observed_at"] != receipt["observed_at"] or response["receipt"]["path"] != receipt["reference"]
            or response["dispatch_started"] is not receipt["dispatch_started"]
            or type(receipt.get("supplier_original_pending")) is not bool
            or receipt["supplier_original_pending"] != (not source_response_closed(response))
            or receipt["capture_ids"] or receipt["observation_ids"]
            or response["source_state"] in {"usable", "empty"} and (not response["complete"] or response["dispatch_started"] is not True)):
        raise SafetyError("SUPPLIER_SHARED_LEAD_PROOF_INVALID")
    _, interpreted, path = _load_leaf(Path(receipt["reference"]).parent, receipt["route"], expected, acquisition_id)
    if str(path) != receipt["reference"] or digest(interpreted["parsed"]) != receipt.get("supplier_results_hash"):
        raise SafetyError("SUPPLIER_SHARED_LEAD_PROOF_INVALID")
    return response


def _parser_search_check(state, check_id, check, receipt, original, parsed):
    """Keep the first identity; version only a proved same-original reparse."""
    prior = state["source_checks"].get(check_id)
    if prior is None or prior == check:
        return check_id, None
    from .source_acquisitions import _record, _response, _stage, _file, _files_current
    aid = receipt["acquisition_id"]; owner = _record(state, aid)
    active = _response(state, owner); reference = receipt["supplier_lead_capture_ref"]
    if (not isinstance(prior, dict) or active is None or owner["active_response_id"] != reference["id"]
            or digest(active) != reference["hash"] or active.get("bridge_contract") != BRIDGE
            or owner["binding"]["route"] != check["route"] or owner["binding"]["request"] != original["request"]
            or active["receipt"]["path"] != check["reference"]):
        raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
    roots = owner["binding"]["artifact_roots"]; files = []
    def interpretation(response):
        proof, fingerprint, raw = _file(response["receipt"]["path"], roots)
        value = decode_json(raw)
        if (proof != response["receipt"] or not isinstance(value, dict)
                or set(value) != {"contract", "acquisition_id", "request", "original_hash", "parser_version", "parsed", "hash"}
                or value["contract"] != CAPTURE_CONTRACT or value["acquisition_id"] != aid
                or value["request"] != original["request"] or value["original_hash"] != digest(original)
                or type(value["parser_version"]) is not int or value["parser_version"] < 1
                or not isinstance(value["parsed"], dict)
                or value["hash"] != digest({field: item for field, item in value.items() if field != "hash"})):
            raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
        suffix = "-" + digest(original)[:16] if original.get("legacy_origin") or original.get("native_origin") else ""
        expected = Path(check["reference"]).parent / ("capture-parser-" + str(value["parser_version"]) + suffix + ".json")
        if proof["path"] != str(expected):
            raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
        files.append((proof, fingerprint))
        return value
    current = interpretation(active)
    if current["parser_version"] != PARSER_VERSION or current["parsed"] != parsed:
        raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
    for application_id, application in state["source_checks"].items():
        if application.get("kind") != APPLICATION_KIND:
            continue
        binding = application.get("binding", {})
        if not isinstance(binding, dict):
            raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
        if (binding.get("search_check_id") != check_id or binding.get("source_acquisition_id") != aid
                or binding.get("concept_id") != check["concept_id"]):
            continue
        if (application.get("binding_hash") != digest(binding)
                or application_id != key("supplier-search-application", binding)
                or application.get("source") != "supplier" or application.get("route") != check["route"]
                or binding.get("route") != check["route"] or binding.get("description_hash") != check["description_hash"]
                or binding.get("search_check_hash") != digest(prior)):
            raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
        saved_ref = binding.get("response", {})
        if (not isinstance(saved_ref, dict) or set(saved_ref) != {"id", "hash"}
                or saved_ref.get("id") not in owner["response_ids"]):
            raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
        previous_response = _stage(state, owner, saved_ref["id"])
        if (previous_response.get("stage_type") != "response" or previous_response.get("bridge_contract") != BRIDGE
                or digest(previous_response) != saved_ref.get("hash")
                or previous_response["receipt"]["path"] != prior.get("reference")):
            raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
        previous = interpretation(previous_response); old_parsed = previous["parsed"]
        if not previous["parser_version"] < current["parser_version"] or previous["hash"] == current["hash"]:
            raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
        target = "interpretation_pending" if old_parsed.get("results") else (
            "completed_no_results" if previous_response["source_state"] == "empty" else
            "insufficient" if previous_response["source_state"] == "partial" else previous_response["source_state"])
        expected_prior = dict(check, reference=previous_response["receipt"]["path"],
            state=target, candidates=old_parsed.get("results"), reason=old_parsed.get("reason"))
        if prior != expected_prior:
            raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
        originals = {check_id: digest(prior), application_id: digest(application),
            saved_ref["id"]: digest(previous_response)}
        def unchanged(current_state):
            if any(digest(current_state["source_checks"].get(rid)) != expected for rid, expected in originals.items()):
                raise SafetyError("SUPPLIER_SHARED_APPLICATION_CHANGED")
            _files_current(files)
        return key("supplier-route-search-check", [check_id, current["hash"]]), unchanged
    raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")


def _apply(memory, context, receipt, original, parsed):
    """Nominate saved URLs for a real current type, not supplier confirmation."""
    from .functional_types import effective_functional_definition
    from .product_queries import resolve_query_profile
    from .supplier_effort import record_supplier_effort
    results = []
    for cid in context.get("concept_ids", []):
        state = memory.snapshot(); product = _product(memory, cid); meaning = effective_functional_definition(state, cid)
        profile = resolve_query_profile(state, cid)
        if (not profile or not any(row["source"] == "supplier" and row["scope"] in {"subtype", "family"}
                and row["text"] == original["request"]["query"] for row in profile["queries"])):
            results.append({"concept_id": cid, "description_hash": digest(product["description"]), "state": "insufficient",
                "evidence_refs": [], "reason": "SUPPLIER_CURRENT_TYPE_QUERY_REVIEW_REQUIRED"})
            continue
        prepared = prepare_research_questions(memory, [{"subject": {"kind": "concept", "concept_id": cid,
            "definition_hash": meaning["meaning_hash"]}, "route": context["route"], "purpose": "sourcing",
            "request": original["request"], "question_options": {"search_scope": "current_type_supplier_leads"}}],
            as_of=max(context["as_of"], utc_now(), key=timestamp))
        question_id = prepared["question_ids"][0]
        target = "interpretation_pending" if parsed["results"] else (
            "completed_no_results" if receipt["state"] == "empty" else "insufficient" if receipt["state"] == "partial" else receipt["state"])
        aid = receipt["acquisition_id"]; path = receipt["reference"]
        check_id = key("supplier-route-search-check", [VERSION, cid, digest(product["description"]), aid, original["hash"]])
        check = {"kind": "supplier_route_search", "version": VERSION, "source": "supplier", "route": context["route"],
            "state": target, "at": receipt["observed_at"], "concept_id": cid, "description_hash": digest(product["description"]),
            "reference": path, "acquisition_id": aid, "query": original["request"]["query"],
            "candidates": deepcopy(parsed["results"]), "reason": parsed["reason"]}
        check_id, parser_current = _parser_search_check(state, check_id, check, receipt, original, parsed)
        transport_item = _item(original.get("legacy_origin", {}).get("workflow_id", aid), original["request"])
        effort = record_supplier_effort(memory, concept_id=cid, description=product["description"], pass_id=aid,
            receipts=[original["receipt"]], receipt_reference=path, expected_requests={transport_item["id"]: transport_item["url"]},
            outcome="completed_empty" if target == "completed_no_results" else "interpretation_pending" if parsed["results"] else
                "blocked" if target == "blocked" else "partial" if target == "insufficient" else "failed", route=context["route"])
        binding = {"route": context["route"], "concept_id": cid, "meaning_hash": meaning["meaning_hash"], "accepted": meaning["accepted"],
            "description_hash": digest(product["description"]), "query_profile_hash": digest(profile),
            "question_id": question_id, "question_binding_hash": prepared["records"][0]["binding_hash"],
            "source_acquisition_id": aid, "response": receipt["supplier_lead_capture_ref"],
            "search_check_id": check_id, "search_check_hash": digest(check),
            "effort_id": effort["check_id"], "effort_hash": digest(memory.snapshot()["source_checks"][effort["check_id"]])}
        application = {"kind": APPLICATION_KIND, "source": "supplier", "route": context["route"],
                       "binding": binding, "binding_hash": digest(binding)}
        application_id = key("supplier-search-application", binding)
        latest = memory.snapshot(); changes = []
        for rid, row in ((check_id, check), (application_id, application)):
            prior = latest["source_checks"].get(rid)
            if prior is not None and prior != row: raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
            if prior is None: changes.append(change("source_checks", rid, row))
        def current(current_state):
            from .source_acquisitions import _record, _response
            if parser_current is not None:
                parser_current(current_state)
            if (effective_functional_definition(current_state, cid)["meaning_hash"] != meaning["meaning_hash"]
                    or digest(current_state["concepts"][cid]["description"]) != binding["description_hash"]
                    or digest(resolve_query_profile(current_state, cid)) != binding["query_profile_hash"]
                    or digest(current_state["source_checks"].get(binding["response"]["id"])) != binding["response"]["hash"]
                    or digest(current_state["source_checks"].get(binding["effort_id"])) != binding["effort_hash"]
                    or current_state["query_tasks"].get(question_id, {}).get("binding_hash") != binding["question_binding_hash"]):
                raise SafetyError("SUPPLIER_SHARED_APPLICATION_CHANGED")
            if _response(current_state, _record(current_state, aid)) != current_state["source_checks"][binding["response"]["id"]]:
                raise SafetyError("SUPPLIER_SHARED_APPLICATION_CHANGED")
        if changes: memory.commit(key("apply-supplier-search", [binding, changes]), changes, validate=current)
        else: current(latest)
        results.append({"concept_id": cid, "description_hash": binding["description_hash"], "state": target,
                        "evidence_refs": [check_id, effort["check_id"], application_id], "reason": parsed["reason"]})
    return results


def current_supplier_search_checks(state, concept_id, *, description_hash):
    """Only meaning-current nominations reach new-mode company follow-ups."""
    from .functional_types import effective_functional_definition
    from .product_queries import resolve_query_profile
    from .research_questions import validate_question_record
    from .source_acquisitions import _record, _response, _stage
    from .supplier_effort import _validated_record
    meaning = effective_functional_definition(state, concept_id); result = {}
    # All applications in this pure read share one state and current type.
    # Resolve wording lazily once, but validate every original application.
    # This is not retained between calls or reused after a later state read.
    query_profile_hash = None
    for rid, row in state["source_checks"].items():
        if row.get("kind") != APPLICATION_KIND: continue
        binding = row.get("binding", {})
        if (row.get("binding_hash") != digest(binding) or rid != key("supplier-search-application", binding)
                or row.get("route") != binding.get("route") or row.get("source") != "supplier"):
            raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
        if binding.get("concept_id") != concept_id: continue
        if binding["meaning_hash"] != meaning["meaning_hash"] or binding["description_hash"] != description_hash: continue
        if query_profile_hash is None:
            query_profile_hash = digest(resolve_query_profile(state, concept_id))
        if binding["query_profile_hash"] != query_profile_hash: continue
        check = state["source_checks"].get(binding["search_check_id"])
        response = state["source_checks"].get(binding["response"]["id"])
        question = validate_question_record(state["query_tasks"].get(binding["question_id"]))
        owner = _record(state, binding["source_acquisition_id"])
        actual_response = _response(state, owner)
        if owner["active_response_id"] != binding["response"]["id"]:
            historical = _stage(state, owner, binding["response"]["id"])
            if (binding["response"]["id"] not in owner["response_ids"] or historical["stage_type"] != "response"
                    or digest(historical) != binding["response"]["hash"]):
                raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
            continue  # A genuine newer local recovery supersedes this view.
        effort = state["source_checks"].get(binding["effort_id"])
        if (not check or check.get("kind") != "supplier_route_search" or check.get("concept_id") != concept_id
                or check.get("description_hash") != description_hash or digest(check) != binding["search_check_hash"]
                or digest(response) != binding["response"]["hash"] or actual_response != response
                or response.get("bridge_contract") != BRIDGE or check["route"] != binding["route"]
                or digest(effort) != binding["effort_hash"] or question["binding_hash"] != binding["question_binding_hash"]
                or question["subject"] != {"kind": "concept", "concept_id": concept_id, "definition_hash": meaning["meaning_hash"]}
                or question["purpose"] != "sourcing" or question["request"] != owner["binding"]["request"]):
            raise SafetyError("SUPPLIER_SHARED_APPLICATION_INVALID")
        _validated_record(effort)
        result[binding["search_check_id"]] = check
    return result


def _applied_supplier_targets(state, task_id, task, value, *, as_of):
    """Retain only exact saved interpretations of this unchanged receipt."""
    from .source_coverage import CHECK_KIND, RELEVANCE_KIND
    targets = task.get("target_results", {})
    if not isinstance(targets, dict):
        return False
    if targets == value["target_results"]:
        return True
    if set(targets) != set(value["target_results"]):
        return False
    unresolved = {cid for cid in targets if targets[cid] != value["target_results"][cid]}
    available = set(task["receipt_refs"])
    for ref in task["receipt_refs"]:
        check = state["source_checks"].get(ref, {}); envelope = check.get("envelope", {})
        if (check.get("kind") != CHECK_KIND or check.get("task_id") != task_id
                or check.get("request_hash") != task["request_hash"] or check.get("route") != task["route"]
                or check.get("envelope_hash") != digest(envelope)):
            return False
        available.update(envelope["capture_ids"] + envelope["observation_ids"] + envelope["evidence_refs"])
    for rid, row in state["source_checks"].items():
        if row.get("kind") != RELEVANCE_KIND or row.get("task_id") != task_id:
            continue
        if row.get("receipt_refs") != task["receipt_refs"]:
            continue
        rows = row.get("results")
        if (not isinstance(rows, list) or not rows or row.get("source") != "supplier"
                or row.get("state") != "reviewed"
                or row.get("reason") != "Accepted interpretation of the existing source acquisition."
                or row.get("route") != task["route"] or row.get("reference") != rid
                or rid != key("source-relevance", [task_id, rows]) or timestamp(row.get("at")) > timestamp(as_of)):
            return False
        for result in rows:
            cid = result.get("concept_id") if isinstance(result, dict) else None
            refs = result.get("evidence_refs") if isinstance(result, dict) else None
            if (cid not in targets or result.get("description_hash") != value["target_results"][cid]["description_hash"]
                    or result.get("state") not in {"completed_relevant", "completed_no_results", "interpretation_pending", "insufficient"}
                    or not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs)
                    or not set(refs) <= available or not isinstance(result.get("reason"), str) or not result["reason"]):
                return False
            if targets[cid] == {field: result[field] for field in ("description_hash", "state", "evidence_refs", "reason")}:
                unresolved.discard(cid)
    return not unresolved


def _unchanged_pending_caller(memory, context, receipt):
    """Prove this exact pending caller already applied the verified reply.

    Called only after collect has rechecked the original, current parser and
    meaning/profile-bound applications. A different consumer or new local
    evidence must still take the normal saved-result application path.
    """
    from .source_coverage import CHECK_KIND, _envelope, _has_unapplied_receipt, source_access_context
    state = memory.snapshot(); task_id = context["task_id"]
    task = state["query_tasks"].get(task_id)
    if (not isinstance(task, dict) or task.get("kind") != "all_source_task_v1"
            or task.get("route") != context["route"] or context["route"] not in SEARCH_ROUTES
            or task.get("supplier_original_pending") is not True or task.get("reconcile_required") is not True
            or receipt.get("supplier_original_pending") is not True or not task.get("receipt_refs")
            or task.get("acquisition_id") != receipt["acquisition_id"]
            or task.get("request_hash") != digest(task.get("request"))
            or task_id != key("source-task", [task.get("run_id"), task.get("route"), task.get("request")])
            or task.get("source_dispatch_contract") != "shared-source-caller-v1"
            or task.get("source_physical_request") != prepare_supplier_request(context)):
        return False
    if _has_unapplied_receipt(memory, state, task):
        return False  # A returned stage must reach ordinary original recovery.
    descriptions = context.get("description_hashes")
    if (not isinstance(descriptions, dict) or set(context.get("concept_ids", [])) != set(descriptions)
            or any(task.get("targets", {}).get(cid) != expected
                or digest(state["concepts"].get(cid, {}).get("description")) != expected
                for cid, expected in descriptions.items())):
        return False
    checked = source_access_context(state, task, as_of=context["as_of"])["task_receipt"]
    value = _envelope(receipt, dict(context, acquisition_id=receipt["acquisition_id"]))
    # _persist_receipt unions target refs before freezing its envelope. Compare
    # that actual envelope, not the adapter's unexpanded evidence list.
    refs = set(value["evidence_refs"])
    for target in value["target_results"].values():
        refs.update(target["evidence_refs"])
    value["evidence_refs"] = sorted(refs)
    if checked != value:
        return False
    bindings = {}
    for ref in refs:
        row = state["source_checks"].get(ref)
        if not isinstance(row, dict) or row.get("route") != context["route"]:
            return False
        bindings[ref] = digest(row)
    check_id = task["receipt_refs"][-1]
    expected_check = {"kind": CHECK_KIND, "source": "supplier", "route": context["route"],
        "at": value["observed_at"], "state": value["state"], "reason": value["reason"],
        "reference": value["reference"], "task_id": task_id, "request_hash": task["request_hash"],
        "envelope": value, "envelope_hash": digest(value), "evidence_bindings": bindings}
    if (check_id != key("source-receipt", [task_id, receipt["acquisition_id"], digest(value)])
            or state["source_checks"].get(check_id) != expected_check):
        return False
    actual = value["dispatch_started"] is True or task.get("dispatch_started") is True
    attempts = task.get("attempt_refs", [])
    if (task.get("tries") != len(set(attempts))
            or actual and receipt["acquisition_id"] not in attempts):
        return False
    retry_at = None
    if value["retryable"] and value["status"] in {"failed", "blocked"}:
        run = state["batch_runs"].get(key("source-run", task["run_id"]), {})
        delay = run.get("retry_delay_seconds")
        if type(delay) is not int:
            return False
        retry_at = (timestamp(value["observed_at"]) + timedelta(seconds=delay)).isoformat(timespec="seconds")
        if value.get("access_wait"):
            retry_at = value["access_wait"]["next_retry_at"]
    applied = {"status": value["status"], "collector_dispatched": actual,
        "reason": value["reason"], "next_retry_at": retry_at,
        "reconcile_required": value["dispatch_uncertain"] or value["supplier_original_pending"],
        "retryable": value["retryable"] and not value["dispatch_uncertain"]}
    return (all(task.get(field) == expected for field, expected in applied.items())
        and _applied_supplier_targets(state, task_id, task, value, as_of=context["as_of"]))


def create_shared_supplier_search_handlers(memory, *, http_reader, browser_reader=None):
    from .public_pages import fetch_public_page
    from .source_acquisitions import _owners, _record, _response, _install_legacy_origin, claim_source_acquisition
    from .source_coverage import (guard_shared_source_launch, marketplace_access_admission, acquisition_access_scope,
                                  source_access_wait, _record_marketplace_access, source_response_closed)
    root = safe_path(memory.root / "supplier_routes" / "shared"); handlers = {}
    for route in SEARCH_ROUTES:
        def prepare(context, route=route):
            return prepare_supplier_request(dict(context, route=route))

        def collect(context, *, reconcile=False, route=route, prepare=prepare):
            request = prepare(context); aid = identifier(context["acquisition_id"]); folder = safe_path(root / aid)
            if not reconcile:
                expected = _fallback(memory.snapshot(), route, request) if request["transport"] == "public-browser" else None
                if request["transport"] == "public-browser" and expected is None:
                    raise SafetyError("SUPPLIER_BROWSER_FALLBACK_PROOF_REQUIRED")
                def fallback_current(state):
                    if request["transport"] == "public-browser" and _fallback(state, route, request) != expected:
                        raise SafetyError("SUPPLIER_BROWSER_FALLBACK_PROOF_CHANGED")
                    # A concurrently active old composite caller cannot be
                    # ignored just because this new owner was reserved first.
                    from .source_acquisitions import _stage
                    owned_origins, current_id = set(), aid
                    while current_id:
                        if current_id in owned_origins: raise SafetyError("SOURCE_ACQUISITION_CHAIN_INVALID")
                        owned_origins.add(current_id); current = _record(state, current_id)
                        for stage_id in current["stage_ids"]:
                            stage = _stage(state, current, stage_id)
                            if stage["stage_type"] == "legacy_origin":
                                saved = stage["origin"].get("original")
                                if isinstance(saved, dict) and digest(saved) == stage["origin"]["hash"]:
                                    owned_origins.add(saved["acquisition_id"])
                        current_id = current["binding"]["previous_acquisition_id"]
                    for rid, task in state["query_tasks"].items():
                        if (task.get("kind") == "all_source_task_v1" and task.get("acquisition_id")
                                and task.get("source_dispatch_contract") != "shared-source-caller-v1"
                                and task.get("route") == route and task.get("request") == {"query": request["query"], "country": "IN"}):
                            _legacy_task(rid, task, route, request)
                            if task["acquisition_id"] not in owned_origins:
                                raise SafetyError("SUPPLIER_LEGACY_ORIGINAL_NEEDS_RECONCILIATION")
                guard_shared_source_launch(memory, context, request, validate=fallback_current)
            page_path = folder / "page.json"
            if not page_path.is_file() and not reconcile:
                intent = {"contract": CAPTURE_CONTRACT, "acquisition_id": aid, "request": request}
                folder.mkdir(parents=True, exist_ok=True)
                intent_path = folder / "intent.json"
                if intent_path.exists() and load_json(intent_path, MAX_RECORD_BYTES) != intent:
                    raise SafetyError("SUPPLIER_SHARED_ORIGINAL_INVALID")
                if not intent_path.exists(): atomic_json(intent_path, intent)
                item = _item(aid, request)
                try:
                    with source_phase("transport"):
                        if request["transport"] == "public-http":
                            value = (http_reader(item, on_dispatch=context.get("record_dispatch"), retain_transport=True)
                                     if http_reader is fetch_public_page else http_reader(item))
                        else:
                            value = (browser_reader(item) if browser_reader is not None else
                                fetch_supplier_native_page(item, route=route, folder=folder / "native"))
                except SourceActionDeadline as error:
                    value = _deadline_page_receipt(item, error)
                    atomic_json(page_path, {**intent, "receipt": value, "hash": digest(value)})
                    raise
                if context.get("record_dispatch") and _actual([value], str(page_path)) is True:
                    context["record_dispatch"]()
                atomic_json(page_path, {**intent, "receipt": value, "hash": digest(value)})
            commit_at = max(context["as_of"], utc_now(), key=timestamp)
            bridge, original, parsed = _record_response(memory, aid, folder=folder, as_of=commit_at)
            response = memory.snapshot()["source_checks"][bridge["stage_id"]]
            _record_marketplace_access(memory, aid, bridge["stage_id"], as_of=commit_at)
            receipt = {"schema_version": 1, "route": route, "acquisition_id": aid,
                "observed_at": response["observed_at"], "state": response["source_state"],
                "dispatch_started": response["dispatch_started"], "reference": response["receipt"]["path"],
                "capture_ids": [], "observation_ids": [], "target_results": [], "reason": parsed["reason"],
                "supplier_lead_capture_ref": {"id": bridge["stage_id"], "hash": digest(response)},
                "supplier_original_pending": not source_response_closed(response),
                "supplier_results_hash": digest(parsed), "retryable": response["access"].get("http_status") in {429, 500, 502, 503, 504},
                "failure_scope": "route" if response["source_state"] == "blocked" else "question",
                "access_wait": source_access_wait(memory.snapshot(), acquisition_access_scope(_record(memory.snapshot(), aid)),
                                                  as_of=context["as_of"])}
            receipt["target_results"] = _apply(memory, context, receipt, original, parsed)
            return receipt

        def resolve(context, route=route, prepare=prepare, collect=collect):
            request = prepare(context); state = memory.snapshot()
            owners = _owners(state, physical_request_fingerprint(route, request))
            # Even an installed uncertain legacy owner can later receive its
            # original file. Saved-only adoption does not start any transport.
            if not owners or not owners[-1][1].get("active_response_id"):
                if not _adopt_legacy(memory, route, request, owner_id=context["task_id"],
                        as_of=max(context["as_of"], utc_now(), key=timestamp), root=root):
                    return {"action": "wait", "reason": "SUPPLIER_LEGACY_ORIGINS_NEED_REVIEW"}
            claim = claim_source_acquisition(memory, route, request, owner_id=context["task_id"], as_of=context["as_of"],
                artifact_roots=[root], collection_channel="direct_local", incremental_collection_fee="known_zero",
                original_acquisition_id=None if context.get("refresh_due_at") else context["acquisition_id"],
                refresh_due_at=context.get("refresh_due_at"), refresh_reason=context.get("refresh_reason"))
            aid = claim["acquisition_id"]; owner = _record(memory.snapshot(), aid); scope = acquisition_access_scope(owner)
            if claim["action"] == "refresh_due": return {"action": "wait", "acquisition_id": aid, "reason": "SUPPLIER_SEARCH_REFRESH_DUE"}
            if owner["state"] == "reserved" and not owner["stage_ids"]:
                if request["transport"] == "public-browser" and not _fallback(memory.snapshot(), route, request):
                    return {"action": "wait", "acquisition_id": aid, "reason": "SUPPLIER_BROWSER_FALLBACK_PROOF_REQUIRED"}
                return {"action": "collect", "acquisition_id": aid, "request": request, "access_scope": scope}
            try:
                receipt = collect(dict(context, acquisition_id=aid, request=request, record_dispatch=None), reconcile=True)
            except (FileNotFoundError, SafetyError) as error:
                if isinstance(error, SafetyError) and str(error) not in {"SOURCE_ORIGINAL_RECEIPT_PENDING", "SOURCE_ACQUISITION_FILE_UNAVAILABLE"}: raise
                return {"action": "wait", "acquisition_id": aid, "reason": "SUPPLIER_ORIGINAL_SEARCH_PENDING"}
            wait = source_access_wait(memory.snapshot(), scope, as_of=context["as_of"])
            if not context.get("reconciling") and wait and wait["phase"] == "due" and wait["acquisition_id"] == aid:
                recovery = {name: wait[name] for name in ("failure_id", "failure_hash", "response_id", "response_hash", "next_retry_at")}
                renewed = claim_source_acquisition(memory, route, request, owner_id=context["task_id"], as_of=context["as_of"],
                    artifact_roots=[root], collection_channel="direct_local", incremental_collection_fee="known_zero", access_recovery=recovery)
                return {"action": "collect", "acquisition_id": renewed["acquisition_id"], "request": request, "access_scope": scope}
            if _unchanged_pending_caller(memory, context, receipt):
                return {"action": "wait", "acquisition_id": aid, "reason": "SUPPLIER_ORIGINAL_SEARCH_PENDING"}
            return {"action": "saved", "acquisition_id": aid, "request": request, "receipt": receipt, "original_folder": str(root / aid)}

        def admission(context, resolution):
            result = marketplace_access_admission(memory, resolution, caller_id=context["task_id"], as_of=context["as_of"])
            return dict(result, caller_fields={k.replace("marketplace_", "source_", 1): v for k, v in result["caller_fields"].items()},
                        context_fields={k.replace("marketplace_", "source_", 1): v for k, v in result["context_fields"].items()})

        def due_hint(context, route=route, prepare=prepare, *, state=None):
            request = prepare(context); state = memory.snapshot() if state is None else state
            owners = _owners(state, physical_request_fingerprint(route, request))
            aid, owner = owners[-1] if owners else (None, None); response = _response(state, owner) if owner else None
            wait = source_access_wait(state, acquisition_access_scope({"binding": {"route": route, "request": request}}), as_of=context["as_of"])
            local = bool(response and response["source_state"] in {"usable", "partial", "empty"}
                and timedelta(0) <= timestamp(context["as_of"]) - timestamp(response["observed_at"]) < timedelta(days=7))
            caller = state["query_tasks"].get(context.get("task_id"), {})
            if response and not response["complete"] and (caller.get("supplier_original_pending")
                    or caller.get("progress", {}).get("supplier_original_pending")):
                local = False  # Already applied partial bytes are not new work.
            if owner and not local and request["transport"] == "public-browser":
                try:
                    candidate = _native_original(root / aid, request, aid)
                    parsed_candidate = parse_supplier_route_receipt(route, request["query"], _item(aid, request), candidate["receipt"])
                    local = (parsed_candidate["state"] == "complete" and candidate["receipt"]["body_complete"] is True
                        and timestamp(candidate["receipt"]["checked_at"]) <= timestamp(context["as_of"]))
                except SafetyError as error:
                    if str(error) not in {"SOURCE_ORIGINAL_RECEIPT_PENDING", "JSON_RESPONSE_INVALID", "SAVED_FILE_UNAVAILABLE"}: raise
            ready = owner is None or owner["state"] == "reserved" and not owner["stage_ids"]
            if request["transport"] == "public-browser" and not _fallback(state, route, request): ready = False
            if wait: ready = wait["phase"] == "due" and (ready or wait["acquisition_id"] == aid)
            return {"ready": bool(ready or local), "local_saved": local,
                "next_retry_at": wait["next_retry_at"] if wait and wait["phase"] == "waiting" else None,
                "reason": wait["reason"] if wait else None,
                "signal": {"acquisition_id": aid, "state": owner["state"] if owner else None,
                           "response_id": owner.get("active_response_id") if owner else None,
                           "complete": response["complete"] if response else False,
                           "observed_at": response["observed_at"] if response else None, "wait": wait}}

        def saved_hint(context, route=route, prepare=prepare, *, state=None):
            state = memory.snapshot() if state is None else state
            owners = _owners(state, physical_request_fingerprint(route, prepare(context)))
            aid = owners[-1][0] if owners else None; files = []
            if aid:
                for name in ("intent.json", "page.json", "legacy-origin.json", "capture-parser-" + str(PARSER_VERSION) + ".json", "native/native_result.json", "native/native_dispatch.json"):
                    path = safe_path(root / aid / name)
                    try: info = path.stat()
                    except FileNotFoundError: continue
                    if not stat.S_ISREG(info.st_mode): raise SafetyError("SOURCE_ACQUISITION_FILE_INVALID")
                    files.append([name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
                pointer_path = root / aid / "legacy-origin.json"
                if pointer_path.exists():
                    pointer = load_json(pointer_path, MAX_RECORD_BYTES)
                    task = _legacy_task(pointer["task_id"], pointer["task"], route, prepare(context))
                    for name in ("capture.json", "progress.json", "capture-dispatch-recovery.json"):
                        path = safe_path(memory.root / "supplier_routes" / task["acquisition_id"] / name)
                        try: info = path.stat()
                        except FileNotFoundError: continue
                        if not stat.S_ISREG(info.st_mode): raise SafetyError("SOURCE_ACQUISITION_FILE_INVALID")
                        files.append([str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
            return {"kind": "saved-source-wake-hint-v1", "acquisition_id": aid, "files": files}

        handlers[route] = {"handler": collect, "reconcile": lambda context, collect=collect: collect(context, reconcile=True),
            "prepare_source": prepare, "resolve_source": resolve, "source_admission": admission,
            "source_due_hint": due_hint, "source_saved_hint": saved_hint,
            "source_due_hint_from_state": due_hint, "source_saved_hint_from_state": saved_hint}
    return handlers
