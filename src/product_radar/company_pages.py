"""One owned supplier-company URL read, independently of its product callers.

Only transport bytes are shared. The existing supplier capture and F review
still bind every use to that caller's current product meaning. No search,
supplier confirmation, demand observation, paid collection or model call occurs
here. Unfinished originals are recoverable locally, never replacement reads.
"""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import stat

from .models import SafetyError, decode_json, digest, identifier, timestamp, utc_now
from .research_memory import key
from .research_questions import physical_request_fingerprint
from .storage import atomic_json, load_json, safe_path
from .supplier_capture import _allowed_url
from .supplier_effort import _receipt_ref, failed_reader_receipt

BRIDGE = "verified-supplier-company-response-v1"
CONTRACT = "supplier-company-transport-v1"
ORIGINAL_READINESS = "supplier-company-original-readiness-v1"
MAX_RECORD = 4 * 1024 * 1024


class CompanyPagePending(SafetyError):
    """The existing controller must reconcile this original, not freeze failure."""


def _request(url, transport):
    return {"url": _allowed_url(url)[0], "transport": transport}


def _item(aid, request):
    return {"id": key("supplier-company-read", aid), "url": request["url"]}


def _closed(receipt, transport):
    return (receipt.get("transport_contract") == "public-page-owned-http-v1"
            and receipt.get("owned_worker_exited") is True) if transport == "public-http" else (
                receipt.get("owned_process_exited") is True)


def _usable(receipt):
    from .supplier_route_acquisition import _CHALLENGES
    text = receipt.get("text")
    return bool(receipt.get("state") == "usable" and receipt.get("http_status") == 200
        and receipt.get("body_complete") is True and isinstance(text, str) and text.strip()
        and isinstance(receipt.get("discovered_links"), list) and len(receipt["discovered_links"]) <= 60
        and not any(word in text.casefold() for word in _CHALLENGES))


def _legacy_pending(state, url, owner_id):
    """Do not ignore an older composite reader whose exact URL is still live."""
    for task_id, task in state["query_tasks"].items():
        if (task_id == owner_id or task.get("kind") != "all_source_task_v1"
                or task.get("route") != "company-pages" or not task.get("acquisition_id")
                or not (task.get("reconcile_required") or task.get("status") == "dispatching")):
            continue
        if any(url in values for values in task.get("request", {}).get("candidate_urls", {}).values()):
            raise CompanyPagePending("SUPPLIER_COMPANY_LEGACY_ORIGINAL_PENDING")


def _adopt_legacy(memory, request, owner_id, root, at):
    """Use canonical old supplier effort, never an arbitrary loose page file."""
    from .source_acquisitions import _file, _install_legacy_origin
    from .supplier_capture_store import _validate, MAX_CAPTURE_BYTES
    from .supplier_effort import VERSION as EFFORT, _validated_record
    # Old captures do not prove which browser/HTTP transport produced an
    # unlabelled reply. Admit only explicitly retained transport identity.
    state = memory.snapshot()
    for check_id, check in state["source_checks"].items():
        if check.get("kind") != EFFORT: continue
        _validated_record(check)
        for reference in check["receipt_refs"]:
            path = safe_path(reference["reference"])
            if path.name != "capture.json" or path.parent.parent != memory.root / "supplier_pages": continue
            proof, stat, raw = _file(path, [memory.root]); capture = decode_json(raw)
            if len(raw) > MAX_CAPTURE_BYTES: raise SafetyError("SUPPLIER_COMPANY_ORIGINAL_INVALID")
            _validate(capture, capture_id=capture.get("capture_id"), request=capture.get("request"), product=capture.get("product"))
            for receipt in capture["bundle"]["receipts"]:
                if receipt.get("requested_url") != request["url"]: continue
                transport = ("public-http" if receipt.get("transport_contract") == "public-page-owned-http-v1"
                    or str(receipt.get("execution_ref", "")).startswith("public-fetch-")
                    else "public-browser" if receipt.get("html_basis") == "rendered_owned_native_dom" else None)
                if transport != request["transport"]: continue
                if reference.get("id") != receipt.get("id"): continue
                if reference.get("hash") != digest(receipt):
                    raise SafetyError("SUPPLIER_COMPANY_ORIGINAL_INVALID")
                aid = key("supplier-company-legacy", [str(path), receipt["id"], digest(receipt)])
                pointer = {"contract": CONTRACT, "acquisition_id": aid, "request": request,
                    "source": proof, "receipt_id": receipt["id"], "receipt_hash": digest(receipt),
                    "check_id": check_id, "check_hash": digest(check)}
                pointer_path = root / aid / "legacy-origin.json"
                if pointer_path.exists() and load_json(pointer_path, MAX_RECORD) != pointer:
                    raise SafetyError("SUPPLIER_COMPANY_ORIGINAL_INVALID")
                atomic_json(pointer_path, pointer)
                pointer_proof, pointer_stat, _ = _file(pointer_path, [root])
                _install_legacy_origin(memory, state, "company-pages", request, table="source_checks", record_id=check_id,
                    saved=check, acquisition_id=aid, owner_id=owner_id, as_of=at,
                    artifact_roots=[root], files=[(proof, stat), (pointer_proof, pointer_stat)],
                    original_attempt_id=receipt["id"], original_snapshot=True)
                return


def _load_original(folder, aid, request):
    from .source_acquisitions import _file
    legacy_path = folder / "legacy-origin.json"
    if legacy_path.is_file():
        pointer_proof, pointer_stat, raw = _file(legacy_path, [folder]); pointer = decode_json(raw)
        if (not isinstance(pointer, dict) or set(pointer) != {"contract", "acquisition_id", "request", "source",
                "receipt_id", "receipt_hash", "check_id", "check_hash"}
                or pointer["contract"] != CONTRACT or pointer["acquisition_id"] != aid or pointer["request"] != request):
            raise SafetyError("SUPPLIER_COMPANY_ORIGINAL_INVALID")
        proof, stat, raw = _file(pointer["source"]["path"], [folder.parent.parent.parent])
        if proof != pointer["source"]: raise SafetyError("SUPPLIER_COMPANY_ORIGINAL_INVALID")
        capture = decode_json(raw)
        from .supplier_capture_store import _validate
        _validate(capture, capture_id=capture.get("capture_id"), request=capture.get("request"), product=capture.get("product"))
        matches = [row for row in capture["bundle"]["receipts"] if row.get("id") == pointer["receipt_id"]
                   and row.get("requested_url") == request["url"] and digest(row) == pointer["receipt_hash"]]
        if len(matches) != 1: raise SafetyError("SUPPLIER_COMPANY_ORIGINAL_INVALID")
        return matches[0], [(pointer_proof, pointer_stat), (proof, stat)]
    intent_path, page_path = folder / "intent.json", folder / "page.json"
    expected = {"contract": CONTRACT, "acquisition_id": aid, "request": request, "item": _item(aid, request)}
    intent_proof, intent_stat, raw = _file(intent_path, [folder])
    if decode_json(raw) != expected: raise SafetyError("SUPPLIER_COMPANY_ORIGINAL_INVALID")
    files = [(intent_proof, intent_stat)]
    page = None
    if page_path.is_file():
        proof, stat, raw = _file(page_path, [folder]); page = decode_json(raw)
        if (not isinstance(page, dict) or set(page) != {"binding", "receipt", "hash"}
                or page["binding"] != expected or page["hash"] != digest(page["receipt"])):
            raise SafetyError("SUPPLIER_COMPANY_ORIGINAL_INVALID")
        files.append((proof, stat))
    receipt = page["receipt"] if page else None
    if request["transport"] == "public-browser" and (receipt is None or not _closed(receipt, "public-browser")
                                                      or receipt.get("outer_deadline") is True):
        native = folder / "native"
        if (native / "native_result.json").is_file() and (native / "native_dispatch.json").is_file():
            output_proof, output_stat, raw = _file(native / "native_result.json", [folder]); output = decode_json(raw)
            dispatch_proof, dispatch_stat, raw = _file(native / "native_dispatch.json", [folder]); dispatch = decode_json(raw)
            if (not isinstance(output, dict) or output.get("owned_process_exited") is not True
                    or output.get("navigation_started") is not True):
                raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING")
            if (dispatch != {"url": request["url"], "navigation_started": True,
                    "navigation_started_at": output.get("navigation_started_at")}
                    or timestamp(output["navigation_started_at"]) > timestamp(output.get("completed_at"))):
                raise SafetyError("SUPPLIER_COMPANY_NATIVE_ORIGINAL_INVALID")
            from .supplier_route_acquisition import _supplier_native_output_receipt
            receipt = _supplier_native_output_receipt(expected["item"], native, output, None)
            files.extend([(output_proof, output_stat), (dispatch_proof, dispatch_stat)])
    if receipt is None: raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING")
    proof = _receipt_ref(receipt, str(page_path))
    if (not proof or receipt.get("id") != expected["item"]["id"]
            or receipt.get("requested_url") != request["url"]):
        raise SafetyError("SUPPLIER_COMPANY_ORIGINAL_INVALID")
    return receipt, files


def _record_original(memory, aid, request, folder, at):
    from .source_acquisitions import _record, _commit_stage, _stage, _response
    from .source_coverage import _record_marketplace_access
    from .supplier_route_acquisition import _actual, _CHALLENGES
    receipt, files = _load_original(folder, aid, request)
    owner = _record(memory.snapshot(), aid)
    _response(memory.snapshot(), owner)  # Revalidate any previously committed immutable source files.
    if owner["binding"]["request"] != request: raise SafetyError("SUPPLIER_COMPANY_ORIGINAL_INVALID")
    if timestamp(receipt["checked_at"]) > timestamp(at): raise SafetyError("SUPPLIER_COMPANY_SOURCE_IN_FUTURE")
    legacy = owner["active_dispatch_intent_id"] is None
    dispatch = (_stage(memory.snapshot(), owner, owner["active_dispatch_intent_id"])
                if not legacy else None)
    if legacy:
        stages = [_stage(memory.snapshot(), owner, rid) for rid in owner["stage_ids"]]
        origin = next((row.get("origin") for row in stages if row["stage_type"] == "legacy_origin"), None)
        pointer = load_json(folder / "legacy-origin.json", MAX_RECORD)
        if (not origin or origin["table"] != "source_checks" or origin["id"] != pointer["check_id"]
                or origin["hash"] != pointer["check_hash"] or digest(origin.get("original")) != origin["hash"]):
            raise SafetyError("SUPPLIER_COMPANY_ORIGINAL_INVALID")
    started = receipt.get("attempt_started_at")
    if not legacy and started is not None and timestamp(started) < timestamp(dispatch["recorded_at"]):
        raise SafetyError("SUPPLIER_COMPANY_NATIVE_ORIGINAL_INVALID")
    actual = _actual([receipt], files[-1][0]["path"])
    closed = _closed(receipt, request["transport"])
    refused = (receipt.get("http_status") in {401, 403, 429, 500, 502, 503, 504}
        or any(word in str(receipt.get("text", "")).casefold() for word in _CHALLENGES))
    usable = _usable(receipt)
    source_state = "blocked" if refused else "usable" if usable else "unavailable"
    access = {"http_status": receipt.get("http_status"), "retry_after": receipt.get("retry_after"),
              "scope_url": _allowed_url(receipt.get("final_url") or request["url"])[0]}
    if request["transport"] == "public-http":
        access.update(ownership_kind="thread", owned_worker_exited=closed)
    else: access["owned_process_exited"] = closed
    artifacts = {str(index) + "-" + Path(proof["path"]).name: proof for index, (proof, _) in enumerate(files)}
    data = {"bridge_contract": BRIDGE, "receipt": files[-1][0], "artifacts": artifacts,
        "observed_at": receipt["checked_at"], "dispatch_started": actual, "source_state": source_state,
        "coverage": {"body_complete": receipt.get("body_complete") is True,
            "byte_truncated": receipt.get("text_truncated") is True, "scope_complete": usable,
            "recognized": usable, "delivered_count": int(usable), "intentional_limit": False},
        "complete": bool(actual is True and closed and usable and not receipt.get("text_truncated")),
        "captures": [], "response_hash": digest(artifacts), "access": access,
        "expense_proof": {"collection_channel": "unknown" if legacy else "direct_local",
                          "provider_requests": None if legacy else 0,
                          "provider_charge_usd": None if legacy else "0", "model_calls": 0}}
    result = _commit_stage(memory, owner, "response", data, at,
        {"state": "failed_access" if refused and actual is True else "response_saved" if actual is True else "execution_unknown",
         "active_parse_id": None, "parse_state": "not_started"}, files=files)
    _record_marketplace_access(memory, aid, result["stage_id"], as_of=at)
    return receipt, result["stage_id"]


def _browser_basis(state, url):
    from .source_acquisitions import _owners, _response
    from .source_coverage import source_response_closed
    owners = _owners(state, physical_request_fingerprint("company-pages", _request(url, "public-http")))
    if not owners: return None
    aid, owner = owners[-1]; response = _response(state, owner)
    if (not response or response.get("bridge_contract") != BRIDGE or not source_response_closed(response)
            or response["source_state"] == "blocked" or response["dispatch_started"] is not True
            or response["access"].get("http_status") not in (None, 200)
            or response["coverage"]["byte_truncated"]
            or response["access"].get("http_status") == 200 and not response["coverage"]["body_complete"]):
        return None
    return {"acquisition_id": aid, "response_id": owner["active_response_id"], "response_hash": digest(response)}


def company_capture_requests(memory, binding, folder, *, as_of, saved_only):
    """Freeze actual capture refresh intents before any company's network read."""
    from .source_acquisitions import _file, DEFAULT_FRESHNESS_SECONDS
    from .supplier_capture_store import _capture_key, _request as validate_request, _validate
    from .supplier_effort import VERSION as EFFORT
    path = folder / "company_inputs.json"
    expected = {}
    for product in binding["products"]:
        cid = product["concept_id"]
        for candidate in binding["candidates"][cid]:
            expected[key("supplier-company-target", [cid, candidate["url"]])] = {"concept_id": cid,
                "lead": {"id": key("supplier-lead", candidate["url"]), "urls": [candidate["url"]]}}
    if path.is_file():
        record = load_json(path, MAX_RECORD)
        if (not isinstance(record, dict) or set(record) != {"binding", "requests", "content_hash"}
                or record["binding"] != binding or not isinstance(record["requests"], dict)
                or set(record["requests"]) != set(expected)
                or record["content_hash"] != digest({"binding": binding, "requests": record["requests"]})):
            raise SafetyError("SUPPLIER_COMPANY_CAPTURE_INTENT_INVALID")
        for rid, request in record["requests"].items():
            validate_request(request)
            if {field: value for field, value in request.items() if field != "refresh"} != expected[rid]:
                raise SafetyError("SUPPLIER_COMPANY_CAPTURE_INTENT_INVALID")
        return record["requests"]
    if saved_only: raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING")
    state = memory.snapshot(); references = set()
    products = {row["concept_id"]: row for row in binding["products"]}
    for row in state["source_checks"].values():
        if row.get("kind") == EFFORT and row.get("concept_id") in products:
            references.update(ref["reference"] for ref in row["receipt_refs"])
    requests = deepcopy(expected)
    for rid, request in expected.items():
        product = products[request["concept_id"]]
        base = memory.root / "supplier_pages" / _capture_key(product, request) / "capture.json"
        matches = []
        for reference in sorted(references | {str(base)}):
            capture_path = safe_path(reference)
            if (capture_path.name != "capture.json" or capture_path.parent.parent != memory.root / "supplier_pages"
                    or not capture_path.is_file()): continue
            _, _, raw = _file(capture_path, [memory.root]); capture = decode_json(raw)
            if capture.get("product") != product or capture.get("request", {}).get("lead") != request["lead"]: continue
            _validate(capture, capture_id=capture.get("capture_id"), request=capture.get("request"), product=product)
            dates = [row.get("checked_at") for row in capture["bundle"]["receipts"]
                     if row.get("requested_url") in request["lead"]["urls"] and row.get("checked_at")]
            if dates: matches.append((max(dates, key=timestamp), capture["capture_id"], capture["request"]))
        if matches:
            observed, previous, previous_request = max(matches, key=lambda row: (timestamp(row[0]), row[1]))
            if timestamp(as_of) >= timestamp(observed) + timedelta(seconds=DEFAULT_FRESHNESS_SECONDS):
                requests[rid]["refresh"] = {"previous_capture_id": previous, "requested_at": as_of,
                    "reason": "Company-page refresh for " + binding["acquisition_id"]}
            else:
                requests[rid] = deepcopy(previous_request)
    record = {"binding": binding, "requests": requests}; record["content_hash"] = digest(record)
    atomic_json(path, record)
    return requests


def shared_company_reader(memory, *, http_reader, browser_reader=None, as_of,
                          owner_id, record_dispatch=None, saved_only=False, continuation=None):
    """Return the existing capture's fetcher, sharing only actual URL bytes."""
    from .public_pages import fetch_public_page
    from .source_acquisitions import (_owners, _record, _response, claim_source_acquisition,
                                     mark_source_dispatching, DEFAULT_FRESHNESS_SECONDS, _guard)
    from .source_coverage import (acquisition_access_scope, source_access_wait,
                                 marketplace_access_admission)
    from .source_deadline import SourceActionDeadline
    root = safe_path(memory.root / "supplier_routes" / "company_shared")
    identifier(owner_id); timestamp(as_of)

    def leaf(url, transport):
        request = _request(url, transport); state = memory.snapshot()
        basis = _browser_basis(state, url) if transport == "public-browser" else None
        if transport == "public-browser" and basis is None:
            raise CompanyPagePending("SUPPLIER_COMPANY_BROWSER_ORIGINAL_REQUIRED")
        owners = _owners(state, physical_request_fingerprint("company-pages", request))
        if not owners:
            _adopt_legacy(memory, request, owner_id, root, max(as_of, utc_now(), key=timestamp))
            state = memory.snapshot(); owners = _owners(state, physical_request_fingerprint("company-pages", request))
        if (saved_only or continuation is not None) and not owners:
            raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING")
        if not owners: _legacy_pending(state, url, owner_id)
        claim = (claim_source_acquisition(memory, "company-pages", request, owner_id=owner_id, as_of=as_of,
            artifact_roots=[root], collection_channel="direct_local", incremental_collection_fee="known_zero")
            if not saved_only and continuation is None else {"acquisition_id": owners[-1][0], "action": "join"})
        aid = claim["acquisition_id"]; folder = safe_path(root / aid)
        owner = _record(memory.snapshot(), aid)
        if owner["active_dispatch_intent_id"] or (folder / "legacy-origin.json").is_file():
            try:
                receipt, response_id = _record_original(memory, aid, request, folder, max(as_of, utc_now(), key=timestamp))
            except SafetyError as error:
                if str(error) in {"SOURCE_ACQUISITION_FILE_UNAVAILABLE", "SAVED_FILE_UNAVAILABLE"}:
                    raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING") from None
                raise
            legacy_bytes = (folder / "legacy-origin.json").is_file() and _usable(receipt)
            if not _closed(receipt, transport) and not legacy_bytes:
                raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING")
            response = memory.snapshot()["source_checks"][response_id]
            wait = source_access_wait(memory.snapshot(), acquisition_access_scope(owner, response), as_of=as_of)
            refresh_due = timestamp(response["observed_at"]) + timedelta(seconds=DEFAULT_FRESHNESS_SECONDS)
            if not saved_only and continuation is None and response["complete"] and timestamp(as_of) >= refresh_due:
                claim = claim_source_acquisition(memory, "company-pages", request, owner_id=owner_id, as_of=as_of,
                    artifact_roots=[root], collection_channel="direct_local", incremental_collection_fee="known_zero",
                    refresh_due_at=refresh_due.isoformat(timespec="seconds"),
                    refresh_reason="The saved supplier company page is at least seven days old.")
                aid = claim["acquisition_id"]; folder = safe_path(root / aid); owner = _record(memory.snapshot(), aid)
            elif not saved_only and continuation is None and wait and wait["phase"] == "due" and wait["acquisition_id"] == aid:
                recovery = {field: wait[field] for field in
                    ("failure_id", "failure_hash", "response_id", "response_hash", "next_retry_at")}
                claim = claim_source_acquisition(memory, "company-pages", request, owner_id=owner_id, as_of=as_of,
                    artifact_roots=[root], collection_channel="direct_local", incremental_collection_fee="known_zero",
                    access_recovery=recovery)
                aid = claim["acquisition_id"]; folder = safe_path(root / aid); owner = _record(memory.snapshot(), aid)
            else:
                return receipt, aid, response_id
        allowed = (continuation or {}).get("leaf_owners", {})
        if saved_only or continuation is not None and aid not in allowed:
            raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING")
        scope = acquisition_access_scope(owner, state=memory.snapshot(), planned=True)
        wait = source_access_wait(memory.snapshot(), scope, as_of=as_of)
        if wait and wait["phase"] != "due": raise CompanyPagePending(wait["reason"])
        admission = marketplace_access_admission(memory, {"access_scope": scope, "acquisition_id": aid},
                                                caller_id=owner_id, as_of=as_of)
        def guard(current, binding):
            _legacy_pending(current, url, owner_id)
            if continuation is not None:
                _validate_unstarted(current, owner_id, continuation, aid, root)
            if transport == "public-browser" and _browser_basis(current, url) != basis:
                raise CompanyPagePending("SUPPLIER_COMPANY_BROWSER_ORIGINAL_CHANGED")
            current_wait = source_access_wait(current, scope, as_of=as_of)
            if current_wait and not (current_wait["phase"] == "due" and wait
                    and current_wait["failure_hash"] == wait["failure_hash"]):
                raise CompanyPagePending(current_wait["reason"])
        marked = mark_source_dispatching(memory, aid, as_of=as_of, source_guard=guard,
                                         joined_changes=admission["changes"])
        if marked["action"] != "saved": raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING")
        item = _item(aid, request)
        intent = {"contract": CONTRACT, "acquisition_id": aid, "request": request, "item": item}
        atomic_json(folder / "intent.json", intent)
        callback = http_reader if transport == "public-http" else browser_reader
        from .work_turns import check_turn_launch
        check_turn_launch(memory, "source")
        _guard(memory, memory.snapshot(), owner["binding"], None)
        try:
            receipt = (callback(item, on_dispatch=record_dispatch, retain_transport=True)
                if callback is fetch_public_page else callback(item) if callback is not None else None)
            if receipt is None:
                from .supplier_route_acquisition import fetch_supplier_native_page
                receipt = fetch_supplier_native_page(item, route="company-pages", folder=folder / "native",
                                                     on_dispatch=record_dispatch)
        except SourceActionDeadline as error:
            from .supplier_route_acquisition import _deadline_page_receipt
            receipt = _deadline_page_receipt(item, error)
            atomic_json(folder / "page.json", {"binding": intent, "receipt": receipt, "hash": digest(receipt)})
            raise
        except Exception as error:
            receipt = failed_reader_receipt(item, "SUPPLIER_COMPANY_READER_FAILED", error)
        atomic_json(folder / "page.json", {"binding": intent, "receipt": receipt, "hash": digest(receipt)})
        proof = _receipt_ref(receipt, str(folder))
        if callback is not fetch_public_page and record_dispatch is not None and proof and proof["proof"] != "none":
            record_dispatch()
        receipt, response_id = _record_original(memory, aid, request, folder, max(as_of, utc_now(), key=timestamp))
        if not _closed(receipt, transport): raise CompanyPagePending("SUPPLIER_COMPANY_ORIGINAL_PENDING")
        return receipt, aid, response_id

    def fetch(item):
        target_id = identifier(item["id"]); url = _allowed_url(item["url"])[0]
        receipt, aid, response_id = leaf(url, "public-http")
        # A true host refusal is paced, not retried by changing transport.
        if (not _usable(receipt) and receipt.get("http_status") in (None, 200)
                and (receipt.get("http_status") is None or receipt.get("body_complete") is True)
                and not receipt.get("text_truncated")
                and memory.snapshot()["source_checks"][response_id]["source_state"] != "blocked"):
            receipt, aid, response_id = leaf(url, "public-browser")
        projected = dict(deepcopy(receipt), id=target_id,
            company_source_original={"acquisition_id": aid, "response_id": response_id,
                "receipt_id": receipt["id"], "receipt_hash": digest(receipt)})
        return projected
    return fetch


def _nominated_company_leaf(task, owner):
    """A stable task ID alone does not nominate an older orphaned URL."""
    binding = owner["binding"]; request = binding["request"]
    transport = request.get("transport")
    if transport not in {"public-http", "public-browser"}:
        return False
    nominated = {_request(url, transport)["url"]
        for urls in task.get("request", {}).get("candidate_urls", {}).values() for url in urls}
    return (request.get("url") in nominated
        and request == _request(request["url"], transport)
        and binding["physical_request_fingerprint"] == physical_request_fingerprint("company-pages", request))


def _validate_unstarted(state, task_id, proof, aid, root):
    from .source_acquisitions import _record
    task = state["query_tasks"].get(task_id, {})
    owner = _record(state, aid)
    if (proof.get("contract") != "supplier-company-unstarted-continuation-v1"
            or proof.get("task_id") != task_id or task.get("kind") != "all_source_task_v1"
            or task.get("route") != "company-pages" or not task.get("reconcile_required")
            or task.get("acquisition_id") != proof.get("acquisition_id")
            or task.get("request_hash") != proof.get("request_hash")
            or task.get("request_hash") != digest(task.get("request"))
            or task.get("targets") != proof.get("targets")
            or owner["binding"]["owner_id"] != task_id
            or not _nominated_company_leaf(task, owner)
            or owner["state"] != "reserved" or owner["stage_ids"]
            or digest(owner) != proof.get("leaf_owners", {}).get(aid)
            or any((root / aid / name).exists() for name in
                   ("intent.json", "page.json", "legacy-origin.json", "native/native_result.json", "native/native_dispatch.json"))):
        raise CompanyPagePending("SUPPLIER_COMPANY_UNSTARTED_PROOF_CHANGED")


def company_unstarted_continuation(memory, context, *, state=None):
    """Authorize only known reservations which never reached transport intent."""
    from .source_acquisitions import _record
    from .source_coverage import acquisition_access_scope, source_access_wait
    state = memory.snapshot() if state is None else state
    task_id = context.get("task_id")
    task = state["query_tasks"].get(task_id, {})
    if (task.get("kind") != "all_source_task_v1" or task.get("route") != "company-pages"
            or not task.get("reconcile_required") or task.get("request") != context.get("request")
            or task.get("request_hash") != digest(task.get("request"))
            or task.get("acquisition_id") != context.get("acquisition_id")):
        return None
    root = safe_path(memory.root / "supplier_routes" / "company_shared")
    proof = {"contract": "supplier-company-unstarted-continuation-v1", "task_id": task_id,
        "acquisition_id": task["acquisition_id"], "request_hash": task["request_hash"],
        "targets": deepcopy(task["targets"]), "leaf_owners": {}}
    for aid, row in state["batch_runs"].items():
        if (row.get("kind") != "source-acquisition-owner-v1" or row.get("binding", {}).get("route") != "company-pages"
                or row["binding"]["owner_id"] != task_id or row["state"] != "reserved" or row["stage_ids"]):
            continue
        owner = _record(state, aid)
        if not _nominated_company_leaf(task, owner): continue
        wait = source_access_wait(state, acquisition_access_scope(owner, state=state, planned=True), as_of=context["as_of"])
        if wait and wait["phase"] != "due": continue
        proof["leaf_owners"][aid] = digest(owner)
        try: _validate_unstarted(state, task_id, proof, aid, root)
        except CompanyPagePending: proof["leaf_owners"].pop(aid)
    return proof if proof["leaf_owners"] else None


def company_source_hints(memory):
    """Pure wake hints for all real leaves of one composite company caller."""
    from .source_acquisitions import _record, _response
    from .source_coverage import acquisition_access_scope, source_access_wait
    root = safe_path(memory.root / "supplier_routes" / "company_shared")

    def selected(context, *, state=None):
        state = memory.snapshot() if state is None else state
        nominated = {url for values in context.get("request", {}).get("candidate_urls", {}).values() for url in values}
        owners = [(aid, _record(state, aid)) for aid, row in state["batch_runs"].items()
            if row.get("kind") == "source-acquisition-owner-v1" and row.get("binding", {}).get("route") == "company-pages"
            and (row["binding"]["request"]["url"] in nominated or row["binding"]["owner_id"] == context.get("task_id"))]
        latest = {}
        for aid, owner in owners:
            fingerprint = owner["binding"]["physical_request_fingerprint"]
            if fingerprint not in latest or owner["binding"]["generation"] > latest[fingerprint][1]["binding"]["generation"]:
                latest[fingerprint] = (aid, owner)
        return state, sorted(latest.values(), key=lambda row: row[0]), nominated

    def outer_originals(context, state):
        """Inspect only the exact original caller, without recreating its input.

        Missing capture intent proves that saved-only collection cannot yet
        proceed. A legacy final pass or deadline can still own its original
        reply without that newer intent file. Presence is a wake candidate,
        not acceptance: the unchanged collector validates every saved byte.
        """
        task_id = context.get("task_id")
        task = state["query_tasks"].get(task_id, {})
        aid = task.get("acquisition_id")
        if (task.get("kind") != "all_source_task_v1" or task.get("route") != "company-pages"
                or not (task.get("reconcile_required") or task.get("status") == "dispatching")
                or not aid or aid != context.get("acquisition_id")
                or task.get("request") != context.get("request")
                or task.get("request_hash") != digest(task.get("request"))
                or key("source-task", [task["run_id"], "company-pages", task["request"]]) != task_id):
            return None
        folder = safe_path(memory.root, "supplier_routes", identifier(aid))
        files = []
        for name in ("company_inputs.json", "company_pass.json", "company_deadline.json"):
            path = safe_path(folder / name)
            try: info = path.stat()
            except FileNotFoundError: continue
            if not stat.S_ISREG(info.st_mode): raise SafetyError("SOURCE_ACQUISITION_FILE_INVALID")
            files.append([aid, name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
        return {"contract": ORIGINAL_READINESS, "task_id": task_id, "acquisition_id": aid,
                "request_hash": task["request_hash"], "files": files}

    def saved_hint(context, *, state=None):
        state, owners, _ = selected(context, state=state); files = []
        outer = outer_originals(context, state)
        if outer: files.extend(outer["files"])
        for aid, owner in owners:
            for name in ("intent.json", "page.json", "legacy-origin.json", "native/native_result.json", "native/native_dispatch.json",
                         "native/native_outer_deadline.json", "native/supplier_outer_deadline.json"):
                path = safe_path(root / aid / name)
                try: info = path.stat()
                except FileNotFoundError: continue
                if not stat.S_ISREG(info.st_mode): raise SafetyError("SOURCE_ACQUISITION_FILE_INVALID")
                files.append([aid, name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
        return {"kind": "saved-source-wake-hint-v1", "company_owners": [[aid, owner["active_response_id"]] for aid, owner in owners],
                "files": files}

    def due_hint(context, *, state=None):
        state, owners, nominated = selected(context, state=state); at = context["as_of"]
        local, ready, waits, signals = False, False, [], []
        outer = outer_originals(context, state)
        if outer:
            # Final outer replies also wake old company acquisitions whose
            # physical leaves predate the shared reader. Their actual replay
            # still verifies bindings, sources and immutable hashes.
            local = any(row[1] in {"company_pass.json", "company_deadline.json"} for row in outer["files"])
            signals.append(["company_original", outer])
        represented = set()
        for aid, owner in owners:
            request = owner["binding"]["request"]; represented.add(request["url"])
            response = _response(state, owner)
            wait = source_access_wait(state, acquisition_access_scope(owner, response, state=state), as_of=at)
            if wait and wait["phase"] == "waiting": waits.append(wait["next_retry_at"])
            pending = owner["active_dispatch_intent_id"] or (root / aid / "legacy-origin.json").is_file()
            if pending:
                try:
                    original, _ = _load_original(root / aid, aid, request)
                    if _closed(original, request["transport"]) and (not response or not _closed_from_response(response)
                            or not context.get("reconciling")):
                        local = True
                except SafetyError as error:
                    if str(error) not in {"SUPPLIER_COMPANY_ORIGINAL_PENDING", "SOURCE_ACQUISITION_FILE_UNAVAILABLE", "SAVED_FILE_UNAVAILABLE"}: raise
            if not context.get("reconciling") and not wait and owner["state"] == "reserved" and not owner["stage_ids"]:
                ready = True
            if not context.get("reconciling") and wait and wait["phase"] == "due": ready = True
            signals.append([aid, owner["state"], owner["active_response_id"], wait])
        if not context.get("reconciling") and nominated - represented: ready = True
        continuation = (company_unstarted_continuation(memory, context, state=state)
                        if context.get("reconciling") else None)
        if continuation: ready = True
        original_wait = None
        if outer and not outer["files"] and not continuation:
            # Even a separately saved leaf cannot reconstruct the missing
            # caller capture/refresh intent. Preserve its ownership and wait
            # for the exact original files, rather than reissue the request.
            local = ready = False
            original_wait = {field: value for field, value in outer.items() if field != "files"}
        return {"ready": bool(ready or local), "local_saved": local,
                "next_retry_at": min(waits, key=timestamp) if waits else None,
                "reason": "SUPPLIER_COMPANY_ORIGINAL_PENDING" if not ready and not local else None,
                "signal": signals,
                **({"company_original_wait": original_wait} if original_wait is not None else {})}
    return due_hint, saved_hint


def _closed_from_response(response):
    from .source_coverage import source_response_closed
    return source_response_closed(response)
