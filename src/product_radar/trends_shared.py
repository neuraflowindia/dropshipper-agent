"""One original type-level Trends chart, independently reused by real targets.

The existing browser/CSV verifier remains the collector. This adapter adds
physical ownership and term-level proof without inventing a product listing,
turning no-data into weak demand, or replacing an uncertain original browser.
Original two-field capture receipts stay intact.

Every shared Trends read is a signed-in read (Plan R10 W2): the bound request
carries ``transport="signed-in-browser"`` (so the owner's signed-in identity
holds its own access waits, and earlier hidden-browser acquisitions are saved
history, never adopted), and a read is admitted only when the owner's sign-in,
the desktop and pacing allow it (``trends_signed_in``).
"""

from datetime import timedelta
from pathlib import Path
import stat

from .models import SafetyError, decode_json, digest, timestamp, utc_now
from .research_questions import physical_request_fingerprint
from .storage import load_json, safe_path
from .trends_capture import explore_url
from .trends_controller import build_trends_request, plain_trends_handlers, reconcile_direct_trends, source_state

BRIDGE = "verified-trends-response-v1"


def build_shared_trends_request(query, *, country="IN"):
    from .source_coverage import SIGNED_IN_TRANSPORT
    request = build_trends_request(query, country=country)
    return dict(request, url=explore_url(request["query"]), date="today 12-m", property="web", category=0,
                transport=SIGNED_IN_TRANSPORT)


def prepare_shared_trends(context):
    """The signed-in physical request for a planned two-field Trends question."""
    request = context["request"]
    if context["route"] != "google-trends" or not isinstance(request, dict):
        raise SafetyError("DIRECT_TRENDS_REQUEST_INVALID")
    wanted = build_shared_trends_request(request.get("query"), country=request.get("country", "IN"))
    plain = build_trends_request(wanted["query"])
    if request != wanted and request != plain:
        raise SafetyError("DIRECT_TRENDS_REQUEST_INVALID")
    return wanted


def record_trends_response(memory, acquisition_id, *, folder, as_of):
    """Derive completeness from the actual chart/export verifier, not a flag."""
    from .source_acquisitions import _record, _file, _commit_stage, _stage
    state = memory.snapshot(); owner = _record(state, acquisition_id)
    request = prepare_shared_trends({"route": "google-trends", "request": owner["binding"]["request"]})
    receipt = reconcile_direct_trends(folder)
    if (owner["binding"]["route"] != "google-trends" or receipt["request"] != build_trends_request(request["query"])
            or receipt["acquisition_id"] != acquisition_id or receipt["evidence_type"] != memory.evidence_type
            or timestamp(receipt["observed_at"]) > timestamp(as_of)):
        raise SafetyError("DIRECT_TRENDS_RECEIPT_MISMATCH")
    roots = owner["binding"]["artifact_roots"]
    proof, fingerprint, raw = _file(Path(folder) / receipt.get("receipt_file", "receipt.json"), roots)
    if decode_json(raw) != receipt:
        raise SafetyError("DIRECT_TRENDS_RECEIPT_MISMATCH")
    files, artifacts = [(proof, fingerprint)], {}
    for name, expected in receipt["artifacts"].items():
        item, fingerprint, _ = _file(Path(folder) / name, roots)
        if item["sha256"] != expected:
            raise SafetyError("DIRECT_TRENDS_ARTIFACT_HASH_MISMATCH")
        artifacts[name] = item; files.append((item, fingerprint))
    count = 0
    if receipt["state"] == "usable":
        parsed = load_json(Path(folder) / receipt.get("interpretation_file", "interpretation.json"), 2 * 1024 * 1024)
        count = len(parsed["parsed"]["series"])
    complete = (receipt["state"] in {"usable", "no_data"} and receipt["scope_verified"] is True
                and receipt["dispatch_started"] is True and receipt.get("owned_process_exited") is True)
    state_seen = source_state(receipt)
    transport = load_json(Path(folder) / "transport.json", 2 * 1024 * 1024) if "transport.json" in artifacts else {}
    for stage_id in owner["response_ids"]:
        previous = _stage(state, owner, stage_id)
        if (previous["dispatch_started"] is True and receipt["dispatch_started"] is not True
                or timestamp(previous["observed_at"]) > timestamp(receipt["observed_at"])):
            raise SafetyError("SOURCE_ACQUISITION_RECEIPT_CONFLICT")
    coverage = {"body_complete": complete, "byte_truncated": False, "scope_complete": complete,
                "recognized": complete, "delivered_count": count, "intentional_limit": False}
    data = {"bridge_contract": BRIDGE, "receipt": proof, "artifacts": artifacts,
            "observed_at": receipt["observed_at"], "dispatch_started": receipt["dispatch_started"],
            "source_state": state_seen, "parse_reason": receipt["reason"], "coverage": coverage, "complete": complete, "captures": [],
            "response_hash": digest({name: ref for name, ref in artifacts.items()
                                     if name in {"multiTimeline.csv", "scope.json", "deadline_scope.json"}}),
            "expense_proof": {"collection_channel": "direct_local", "provider_requests": 0,
                              "model_calls": 0, "provider_charge_usd": "0"},
            "access": {"http_status": receipt.get("navigation_status"),
                       "retry_after": transport.get("navigation_retry_after", transport.get("retry_after")),
                       "owned_process_exited": receipt.get("owned_process_exited")}}
    status = ("completed_empty" if complete and state_seen == "empty" else
              "failed_access" if state_seen == "blocked" and receipt.get("owned_process_exited") is True else
              "response_saved" if receipt["dispatch_started"] is True else "execution_unknown")
    return _commit_stage(memory, owner, "response", data, as_of,
        {"state": status, "active_parse_id": None, "parse_state": "not_started"}, files=files)


def validate_trends_term_receipt(memory, request, acquisition_id, receipt):
    """Validate the retained term proof; never import a synthetic product."""
    from .source_acquisitions import _record, _response
    reference = receipt.get("term_capture_ref")
    if not isinstance(reference, dict) or set(reference) != {"id", "hash"}:
        raise SafetyError("DIRECT_TRENDS_TERM_PROOF_REQUIRED")
    state = memory.snapshot()
    owner = _record(state, acquisition_id)
    response = _response(state, owner)
    expected = prepare_shared_trends({"route": "google-trends", "request": request})
    if (owner["binding"]["route"] != "google-trends" or owner["binding"]["request"] != expected
            or owner["active_response_id"] != reference["id"] or not response
            or digest(response) != reference["hash"] or response.get("bridge_contract") != BRIDGE
            or response["complete"] is not (receipt["state"] in {"usable", "empty"})
            or response["source_state"] != receipt["state"]
            or response["observed_at"] != receipt["observed_at"] or response["receipt"]["path"] != receipt["reference"]
            or response["dispatch_started"] is not receipt["dispatch_started"]
            or receipt["state"] in {"usable", "empty"} and receipt["dispatch_started"] is not True
            or receipt["capture_ids"] or receipt["observation_ids"]):
        raise SafetyError("DIRECT_TRENDS_TERM_PROOF_INVALID")
    # Replay the original source verifier as well as the canonical references.
    original = reconcile_direct_trends(Path(receipt["reference"]).parent)
    if (original["request"] != build_trends_request(expected["query"])
            or original["acquisition_id"] != acquisition_id or original["observed_at"] != receipt["observed_at"]
            or source_state(original) != receipt["state"]):
        raise SafetyError("DIRECT_TRENDS_TERM_PROOF_INVALID")
    return response


def release_replaced_trends_waits(memory, *, as_of):
    """Cutover (W13): Trends questions whose retry time was set by the replaced
    hidden-browser route (its bound request has no signed-in transport) may be
    asked again at once in the owner's signed-in profile. Questions in flight
    or awaiting reconciliation are left alone. Returns the released ids."""
    from .research_memory import change, key
    state = memory.snapshot()
    released = replaced_trends_waits(state, as_of=as_of)
    changes = [change("query_tasks", task_id, dict(state["query_tasks"][task_id], next_retry_at=None),
                      state["query_tasks"][task_id]) for task_id in released]
    if changes:
        memory.commit(key("trends-signed-in-cutover", released), changes)
    return released


def replaced_trends_waits(state, *, as_of):
    """Read-only: the Trends questions ``release_replaced_trends_waits`` would release."""
    from .source_coverage import SIGNED_IN_TRANSPORT
    return [task_id for task_id, task in sorted(state["query_tasks"].items())
            if task.get("kind") == "all_source_task_v1" and task.get("route") == "google-trends"
            and task.get("status") != "dispatching" and not task.get("reconcile_required")
            and task.get("next_retry_at") and timestamp(task["next_retry_at"]) > timestamp(as_of)
            and (task.get("source_physical_request") or {}).get("transport") != SIGNED_IN_TRANSPORT]


def create_shared_trends_handlers(memory, *, data_root=None, capture=None, replay=None, signed_in_root=None):
    """``signed_in_root`` holds the signed-in browser's records (the project by default)."""
    from .signed_in_attempts import SignedInWait, collectable
    from .signed_in_browser import PROJECT_ROOT, signed_in_recheck_at
    from .source_acquisitions import _owners, _response, _record, claim_source_acquisition
    from .source_coverage import (marketplace_access_admission, guard_shared_source_launch,
                                 acquisition_access_scope, source_access_wait, _record_marketplace_access)
    from .trends_signed_in import (begin_signed_in_read, not_started_pending, release_unsent_read, reopen_unsent,
                                   signed_in_hold, unsent_read)
    signed_in_root = PROJECT_ROOT if signed_in_root is None else signed_in_root
    root = safe_path(data_root or (memory.root / "direct_trends"))
    plain = plain_trends_handlers(memory, data_root=root, capture=capture, replay=replay,
                                  signed_in_root=signed_in_root, paced=False)["google-trends"]

    def collect(context, *, reconcile=False):
        request = prepare_shared_trends(context); aid = context["acquisition_id"]
        folder = safe_path(root / aid); counted = None
        if not reconcile:
            # Counted before the launch guard: no second read can slip in
            # between the hold and this read starting.
            counted = begin_signed_in_read(signed_in_root, as_of=context["as_of"])
            try:
                guard_shared_source_launch(memory, context, request)
            except BaseException:
                release_unsent_read(signed_in_root, counted)
                raise
        two_field = dict(context, request=build_trends_request(request["query"]))
        try:
            receipt = plain["reconcile" if reconcile else "handler"](two_field)
        except SignedInWait as error:
            release_unsent_read(signed_in_root, counted)
            unsent_read(memory, root, aid, error, signed_in_root=signed_in_root, as_of=context["as_of"])
            raise
        commit_at = max(context["as_of"], utc_now(timespec="microseconds"), key=timestamp)
        bridged = record_trends_response(memory, aid, folder=folder, as_of=commit_at)
        response_id = bridged["stage_id"]
        response = memory.snapshot()["source_checks"][response_id]
        _record_marketplace_access(memory, aid, response_id, as_of=commit_at)
        if not reconcile:
            route_wait(aid, commit_at)
        return dict(receipt, term_capture_ref={"id": response_id, "hash": digest(response)})

    def route_wait(aid, at):
        """Keep the signed-in route's wait for the plain status line; a busy
        record only delays that line, never the read."""
        from .trends_proof_gate import note_route_wait
        wait = source_access_wait(memory.snapshot(), acquisition_access_scope(_record(memory.snapshot(), aid)), as_of=at)
        response = memory.snapshot()["source_checks"].get((wait or {}).get("response_id"), {})
        try:
            note_route_wait(signed_in_root, response.get("parse_reason") or (wait or {}).get("reason") or "",
                            wait["next_retry_at"] if wait and wait["phase"] == "waiting" else None)
        except (SafetyError, OSError):
            pass

    def held(aid, hold):
        return {"action": "wait", "acquisition_id": aid, "calm_wait": True, "reason": hold["reason"],
                "next_retry_at": hold["next_retry_at"]}

    def resolve(context):
        request = prepare_shared_trends(context)
        claim = claim_source_acquisition(memory, "google-trends", request, owner_id=context["task_id"],
            as_of=context["as_of"], artifact_roots=[root], collection_channel="direct_local", incremental_collection_fee="known_zero",
            original_acquisition_id=None if context.get("refresh_due_at") else context["acquisition_id"],
            refresh_due_at=context.get("refresh_due_at"), refresh_reason=context.get("refresh_reason"))
        aid = claim["acquisition_id"]; owner = _record(memory.snapshot(), aid)
        if claim["action"] == "refresh_due":
            return {"action": "wait", "acquisition_id": aid, "reason": "DIRECT_TRENDS_REFRESH_DUE"}
        if not_started_pending(root, aid, memory.snapshot(), owner):
            # A signed-in start that never navigated, not yet reopened.
            try:
                reopen_unsent(memory, root, aid, context["as_of"])
            except (SafetyError, OSError):
                return held(aid, {"reason": "SIGNED_IN_START_RECOVERY_PENDING", "next_retry_at": signed_in_recheck_at(
                    signed_in_root, "SIGNED_IN_START_RECOVERY_PENDING", as_of=context["as_of"])})
            owner = _record(memory.snapshot(), aid)
        if collectable(memory.snapshot(), owner) and not (root / aid).exists():
            hold = signed_in_hold(signed_in_root, as_of=context["as_of"])
            if hold:
                return held(aid, hold)
            return {"action": "collect", "acquisition_id": aid, "request": request, "access_scope": acquisition_access_scope(owner)}
        try:
            receipt = collect(dict(context, acquisition_id=aid, request=request, record_dispatch=None), reconcile=True)
        except (FileNotFoundError, SafetyError) as error:
            if isinstance(error, SafetyError) and str(error) not in {"SAVED_FILE_UNAVAILABLE", "SOURCE_ACQUISITION_FILE_UNAVAILABLE"}:
                raise
            return {"action": "wait", "acquisition_id": aid, "reason": "DIRECT_TRENDS_ORIGINAL_RECEIPT_PENDING"}
        wait = source_access_wait(memory.snapshot(), acquisition_access_scope(owner), as_of=context["as_of"])
        if not context.get("reconciling") and wait and wait["phase"] == "due" and wait["acquisition_id"] == aid:
            hold = signed_in_hold(signed_in_root, as_of=context["as_of"])
            if hold:
                return held(aid, hold)
            recovery = {name: wait[name] for name in ("failure_id", "failure_hash", "response_id", "response_hash", "next_retry_at")}
            renewed = claim_source_acquisition(memory, "google-trends", request, owner_id=context["task_id"],
                as_of=context["as_of"], artifact_roots=[root], collection_channel="direct_local", incremental_collection_fee="known_zero",
                access_recovery=recovery)
            return {"action": "collect", "acquisition_id": renewed["acquisition_id"], "request": request,
                    "access_scope": acquisition_access_scope(memory.snapshot()["batch_runs"][renewed["acquisition_id"]])}
        return {"action": "saved", "acquisition_id": aid, "request": request, "receipt": receipt, "original_folder": str(root / aid)}

    def admission(context, resolution):
        result = marketplace_access_admission(memory, resolution, caller_id=context["task_id"], as_of=context["as_of"])
        return dict(result, caller_fields={name.replace("marketplace_", "source_", 1): value for name, value in result["caller_fields"].items()},
                    context_fields={name.replace("marketplace_", "source_", 1): value for name, value in result["context_fields"].items()})

    def due_hint(context, *, state=None):
        request = prepare_shared_trends(context); state = memory.snapshot() if state is None else state
        owners = _owners(state, physical_request_fingerprint("google-trends", request))
        aid, owner = owners[-1] if owners else (None, None)
        response = _response(state, owner) if owner else None
        wait = source_access_wait(state, acquisition_access_scope({"binding": {"route": "google-trends", "request": request}}), as_of=context["as_of"])
        local = bool(response and response["complete"])
        fresh = local and timedelta(0) <= timestamp(context["as_of"]) - timestamp(response["observed_at"]) < timedelta(days=7)
        ready = owner is None or collectable(state, owner) or not_started_pending(root, aid, state, owner)
        if wait:
            ready = (wait["phase"] == "due" and (ready or wait["acquisition_id"] == aid)
                     or wait["phase"] == "probe_pending" and ready and wait["probe"]["acquisition_id"] == aid)
        hold = signed_in_hold(signed_in_root, as_of=context["as_of"]) if ready and not fresh else None
        return {"ready": bool(ready and not hold or fresh), "local_saved": bool(fresh),
                "next_retry_at": wait["next_retry_at"] if wait and wait["phase"] == "waiting" else hold["next_retry_at"] if hold else None,
                "reason": hold["reason"] if hold else wait["reason"] if wait else None,
                "signal": {"acquisition_id": aid, "state": owner["state"] if owner else None,
                           "response_id": owner.get("active_response_id") if owner else None, "wait": wait,
                           "complete": bool(response and response["complete"]),
                           "observed_at": response["observed_at"] if response else None}}

    def saved_hint(context, *, state=None):
        request = prepare_shared_trends(context); state = memory.snapshot() if state is None else state
        owners = _owners(state, physical_request_fingerprint("google-trends", request))
        aid = owners[-1][0] if owners else None; files = []
        if aid:
            for name in ("intent.json", "receipt.json", "transport.json", "scope.json", "multiTimeline.csv", "interpretation.json", "deadline_reparse.json", "native_result.json", "native_outer_deadline.json"):
                path = safe_path(root / aid / name)
                try: info = path.stat()
                except FileNotFoundError: continue
                if not stat.S_ISREG(info.st_mode): raise SafetyError("SOURCE_ACQUISITION_FILE_INVALID")
                files.append([name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
        return {"kind": "saved-source-wake-hint-v1", "acquisition_id": aid, "files": files}

    return {"google-trends": {"handler": collect, "reconcile": lambda context: collect(context, reconcile=True),
        "prepare_source": prepare_shared_trends, "resolve_source": resolve, "source_admission": admission,
        "source_due_hint": due_hint, "source_saved_hint": saved_hint,
        "source_due_hint_from_state": due_hint, "source_saved_hint_from_state": saved_hint}}
