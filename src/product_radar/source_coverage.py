"""Per-product source coverage and a finite continuing acquisition dispatcher.

These records live in the existing research event store. A route's connection
or account check is not a product attempt. Collection scope, interpretation
scope and source independence remain separate. No provider/model is called by
planning or coverage views; only explicitly registered handlers can collect.
"""

from collections import Counter, deque
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
import os
import signal
import stat
import threading
import time
from urllib.parse import urlsplit

from .brand_reviews import current_brand
from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .research_memory import change, key
from .source_deadline import SourceActionDeadline, bounded_source_deadline, trace_source, source_phase
from .storage import Journal, atomic_json, load_json, safe_path
from .operations import record_event, record_error
from .supplier_effort import RETIRED_SUPPLIER_ROUTES
from .work_turns import active_work_turn, reserve_source_callback


TASK_KIND = "all_source_task_v1"
RUN_KIND = "all_source_run_v1"
CHECK_KIND = "all_source_receipt_v1"
DISPATCH_KIND = "all_source_dispatch_v1"
SAVED_RECOVERY_SCAN_LIMIT = 64
# Local inspection is independent of the allowance for actual callbacks. A
# cooperative yield happens between items, never inside an original import,
# canonical save or transport. One slow item can exceed the elapsed ceiling.
SOURCE_INSPECTION_SLICE_LIMIT = 64
SOURCE_INSPECTION_SLICE_SECONDS = 60
SOURCE_INSPECTION_PRIORITY_LIMIT = 8
SOURCE_INSPECTION_KIND = "source-inspection-slice-v1"
RELEVANCE_KIND = "all_source_relevance_v1"
ACCESS_SCOPE_KIND = "source-access-scope-v1"
ACCESS_FAILURE_KIND = "source-access-refusal-v1"
# Consecutive actual refusals of one access scope wait longer each time: 5
# minutes, 1 hour, 6 hours, then 24 hours (cap) for a challenge (401/403/429,
# captcha, gate, login wall). Plain server errors and product-image hosts stop
# at 1 hour. A refusal counts as the next one only when it is the due probe's
# own reply or was observed after the previous wait ended; any answer from the
# host that is not a refusal clears the wait and starts the count again. The
# applied wait is saved in each refusal record, so later retuning of these
# steps never invalidates an earlier record. A real Retry-After is never shortened.
ACCESS_BACKOFF_SECONDS = (300, 3600, 6 * 3600, 24 * 3600)
ACCESS_SHORT_BACKOFF_SECONDS = ACCESS_BACKOFF_SECONDS[:2]
_ACCESS_REFUSAL_STATUSES = {401, 403, 429, 500, 502, 503, 504}
_ACCESS_CHALLENGE_STATUSES = {401, 403, 429}
ROUTES = {
    "meta-ads": {"source": "meta", "demand_group": "meta"},
    "amazon-bestsellers": {"source": "marketplace", "demand_group": "marketplace"},
    "amazon-movers-and-shakers": {"source": "marketplace", "demand_group": "marketplace"},
    "amazon-search": {"source": "marketplace", "demand_group": "marketplace"},
    "flipkart-search": {"source": "marketplace", "demand_group": "marketplace"},
    "google-trends": {"source": "trends", "demand_group": "trends"},
    "blinkit": {"source": "quickcommerce", "demand_group": None},
    "zepto": {"source": "quickcommerce", "demand_group": None},
    "instamart": {"source": "quickcommerce", "demand_group": None},
    "google-suppliers": {"source": "supplier", "demand_group": None},
    "google-maps": {"source": "supplier", "demand_group": None},
    "indiamart": {"source": "supplier", "demand_group": None},
    "tradeindia": {"source": "supplier", "demand_group": None},
    "company-pages": {"source": "supplier", "demand_group": None},
}
# Routes the planner asks product questions for. A retired supplier route keeps
# its row and saved history but is never prepared, sent or waited on.
PLANNED_ROUTES = tuple(route for route in ROUTES if route not in RETIRED_SUPPLIER_ROUTES)
STATES = {"not_attempted", "not_connected", "in_progress", "completed_relevant",
          "completed_no_results", "interpretation_pending", "insufficient", "failed",
          "blocked", "not_applicable"}
_FINAL = {"acquired", "completed_no_results", "insufficient", "finished"}
_RELATIONSHIPS = {"exact_product", "relevant_family"}
_INTEGRITY_ERRORS = {"RESEARCH_EVENTS_INVALID", "RESEARCH_DEPENDENCY_CHANGED",
                     "RESEARCH_CAPTURE_CONFLICT", "RESEARCH_EVENT_CONFLICT",
                     "JOURNAL_CORRUPT_REVIEW_REQUIRED", "SYMLINK_PATH_REFUSED",
                     "SOURCE_RECEIPT_CONFLICT", "SOURCE_STAGED_RECEIPT_INVALID", "SOURCE_PROGRESS_STAGE_INVALID"}
_RECOVERABLE_PRELAUNCH = {"RUNNER_ALREADY_ACTIVE", "PUBLIC_FETCH_WORKERS_BUSY",
                         "SOURCE_OUTER_DEADLINE_UNAVAILABLE"}
# These are failures of our saved proof, not a refusal by Google. Keep the
# original intent/acquisition pending for saved-only reconciliation in either
# case; the different state makes the cause visible without authorising retry.
_LOCAL_RECEIPT_ERRORS = {
    "DIRECT_TRENDS_RECEIPT_TIME_INVALID", "DIRECT_TRENDS_DISPATCH_TIME_INVALID",
    "DIRECT_TRENDS_RECEIPT_INVALID", "DIRECT_TRENDS_RECEIPT_MISMATCH",
    "DIRECT_TRENDS_DISPATCH_INVALID", "DIRECT_TRENDS_DISPATCH_MISMATCH",
    "DIRECT_TRENDS_ARTIFACT_HASH_MISMATCH", "DIRECT_TRENDS_TRANSPORT_MISMATCH",
    "DIRECT_TRENDS_ORIGINAL_SCOPE_INVALID", "DIRECT_TRENDS_ORIGINAL_SCOPE_MISMATCH",
    "DIRECT_TRENDS_ORIGINAL_HTML_MISMATCH", "DIRECT_TRENDS_EXECUTION_UNVERIFIED",
    "SOURCE_DEADLINE_PROOF_INVALID", "SOURCE_DEADLINE_ORIGINAL_HASH_MISMATCH",
    "SOURCE_DEADLINE_ORIGINAL_INVALID", "SOURCE_DEADLINE_ORIGINAL_TIME_INVALID",
    "MARKETPLACE_DEADLINE_REPARSE_CONFLICT", "DIRECT_TRENDS_DEADLINE_REPARSE_CONFLICT",
    "META_PUBLIC_CAPTURE_CHANGED", "META_PUBLIC_MAPPING_CHANGED", "META_PUBLIC_RECEIPT_CONFLICT",
    "SAVED_SUPPLIER_ROUTE_INVALID",
}


def route_registry(handlers=None):
    """Describe actual registered implementations, without assuming access."""
    handlers = handlers or {}
    if not isinstance(handlers, dict) or not set(handlers) <= set(ROUTES):
        raise SafetyError("SOURCE_REGISTRY_INVALID")
    rows = {}
    for route, contract in ROUTES.items():
        value = handlers.get(route)
        callback = value.get("handler") if isinstance(value, dict) else value
        if callback is not None and not callable(callback):
            raise SafetyError("SOURCE_HANDLER_INVALID")
        rows[route] = {"route": route, **contract, "connected": callback is not None,
                       "handler": callback}
        if isinstance(value, dict):
            reconcile = value.get("reconcile")
            if reconcile is not None and not callable(reconcile):
                raise SafetyError("SOURCE_RECONCILER_INVALID")
            rows[route]["reconcile"] = reconcile
            recovery = value.get("recover_terminal")
            if recovery is not None and not callable(recovery):
                raise SafetyError("SOURCE_RECONCILER_INVALID")
            rows[route]["recover_terminal"] = recovery
            for hook in ("prepare_source", "resolve_source", "source_saved_hint", "source_admission", "source_due_hint",
                         "prepare_marketplace", "resolve_marketplace", "marketplace_saved_hint",
                         "marketplace_admission", "marketplace_due_hint", "source_access_preflight", "source_access_hint",
                         "continue_unstarted"):
                helper = value.get(hook)
                if helper is not None and not callable(helper):
                    raise SafetyError("SOURCE_MARKETPLACE_HOOK_INVALID")
                rows[route][hook] = helper
            for base in ("source_due_hint", "source_saved_hint", "source_access_hint",
                         "marketplace_due_hint", "marketplace_saved_hint"):
                paired = value.get(base + "_from_state")
                # Optional reuse metadata must not disable a valid legacy
                # callback/fallback. Only exact current callable identity
                # enables reuse; orphaned or replaced pairs remain inert.
                current = rows[route].get(base)
                rows[route][base + "_from_state"] = (
                    paired if callable(current) and paired is current else None)
    return rows


def shared_source_hooks(installed):
    """Actual installed neutral hooks, retaining the old marketplace ABI."""
    value = installed if isinstance(installed, dict) else {}
    return {name: value.get("source_" + name) or value.get("marketplace_" + name)
            for name in ("admission", "due_hint", "saved_hint")} | {
        "resolve": value.get("resolve_source") or value.get("resolve_marketplace"),
        "prepare": value.get("prepare_source") or value.get("prepare_marketplace")}


def source_readiness_hint(installed, kind, context, *, state=None, include_access_hint=True):
    """Reuse one verified state only in explicitly paired advisory callbacks.

    The paired registration must be the exact current base function, not a
    stale factory helper behind a replaced/custom callback. Standalone hooks
    and every resolver/admission retain their fresh-read behaviour. File wake
    checks still execute each time; neither their outputs nor metadata is cached.
    """
    if kind not in {"due_hint", "saved_hint"} or type(include_access_hint) is not bool:
        raise SafetyError("SOURCE_READINESS_HINT_INVALID")
    value = installed if isinstance(installed, dict) else {}
    names = (("source_due_hint", "source_access_hint", "marketplace_due_hint")
             if kind == "due_hint" and include_access_hint else
             ("source_due_hint", "marketplace_due_hint") if kind == "due_hint" else
             ("source_saved_hint", "marketplace_saved_hint"))
    for name in names:
        callback = value.get(name)
        if callback is None:
            continue
        if not callable(callback):
            raise SafetyError("SOURCE_MARKETPLACE_HOOK_INVALID")
        if state is not None and value.get(name + "_from_state") is callback:
            return callback(context, state=state)
        return callback(context)
    return None


def shared_source_caller_fields(installed, request):
    neutral = isinstance(installed, dict) and callable(installed.get("resolve_source"))
    prefix = "source" if neutral else "marketplace"
    return {prefix + "_physical_request": deepcopy(request),
            prefix + "_dispatch_contract": "shared-source-caller-v1" if neutral else "shared-marketplace-caller-v1"}


def source_access_scope(url, *, transport="public-browser", access_identity="owned-public-local",
                        account_key=None, location_key=None):
    """Describe an installed access route, not an invented logged-in session.

    Owned native/Playwright public browsers share this conservative local
    access identity. A transport change cannot bypass its saved host refusal.
    Other installed readers must supply their actual distinct identity.
    """
    if not isinstance(url, str): raise SafetyError("SOURCE_ACCESS_SCOPE_INVALID")
    try: parsed = urlsplit(url)
    except ValueError: raise SafetyError("SOURCE_ACCESS_SCOPE_INVALID") from None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SafetyError("SOURCE_ACCESS_SCOPE_INVALID")
    for value in (transport, access_identity, account_key, location_key):
        if value is not None: identifier(value)
    return {"host": parsed.hostname.lower(), "transport": transport, "access_identity": access_identity,
            "account_key": account_key, "location_key": location_key}


def acquisition_access_scope(owner, response=None, *, state=None, planned=False):
    """Use the actual reader identity; an image thread is not a browser."""
    request = owner["binding"]["request"]
    if owner["binding"]["route"] == "company-pages":
        if response is None and state is not None:
            from .source_acquisitions import _record, _stage
            original = owner
            if (planned or not original.get("active_response_id")) and original["binding"].get("previous_acquisition_id"):
                original = _record(state, original["binding"]["previous_acquisition_id"])
            if original.get("active_response_id"):
                response = _stage(state, original, original["active_response_id"])
        url = (response or {}).get("access", {}).get("scope_url", request["url"])
        return source_access_scope(url, transport="supplier-public", access_identity="owned-supplier-public")
    from .supplier_route_acquisition import SEARCH_ROUTES
    if owner["binding"]["route"] in SEARCH_ROUTES:
        # HTTP and browser are different reads, not a way around the same
        # public supplier host's real refusal. Google search/Maps share it.
        return source_access_scope(request["url"], transport="supplier-public", access_identity="owned-supplier-public")
    if owner["binding"]["route"] in {"blinkit", "zepto", "instamart"} and request.get("transport") == "funded-http":
        return source_access_scope(request["url"], transport="funded-http", access_identity="connected-quickcommerce-provider")
    if owner["binding"]["route"] == "public_image_http":
        if response is None and state is not None:
            from .source_acquisitions import _record, _stage
            original = owner
            if (planned or not original.get("active_response_id")) and original["binding"].get("previous_acquisition_id"):
                original = _record(state, original["binding"]["previous_acquisition_id"])
            if original.get("active_response_id"):
                response = _stage(state, original, original["active_response_id"])
        url = (response or {}).get("access", {}).get("scope_url", request["url"])
        return source_access_scope(url, transport="public-http", access_identity="owned-public-http")
    return source_access_scope(request["url"])


def source_response_closed(response):
    access = response.get("access", {})
    if access.get("ownership_kind") == "thread":
        return response.get("bridge_contract") in {"verified-image-response-v1", "verified-supplier-search-response-v1",
            "verified-supplier-company-response-v1"} and access.get("owned_worker_exited") is True
    return access.get("owned_process_exited") is True


def _access_failure(state, failure_id):
    from .source_acquisitions import _record, _stage
    row = state["source_checks"].get(failure_id, {})
    binding = row.get("binding", {})
    if (row.get("kind") != ACCESS_FAILURE_KIND or row.get("binding_hash") != digest(binding)
            or failure_id != key("source-access-refusal", binding)
            or binding.get("scope_hash") != digest(binding.get("scope"))):
        raise SafetyError("SOURCE_ACCESS_PROOF_INVALID")
    try:
        owner = _record(state, binding["acquisition_id"])
        response = _stage(state, owner, binding["response_id"])
        access = response.get("access", {})
        if (binding["response_id"] not in owner["response_ids"] or response["stage_type"] != "response"
                or digest(response) != binding["response_hash"]
                or response["dispatch_started"] is not True
                or access.get("http_status") != binding["http_status"]
                or access.get("retry_after") != binding["retry_after"]
                or response["observed_at"] != binding["observed_at"]
                or acquisition_access_scope(owner, response) != binding["scope"]
                or not (binding["http_status"] in _ACCESS_REFUSAL_STATUSES
                        or binding["challenge"] is True and response["source_state"] == "blocked")):
            raise SafetyError("SOURCE_ACCESS_PROOF_INVALID")
        timestamp(binding["observed_at"]); timestamp(binding["next_retry_at"])
        # Records saved before escalation carry neither field (a first refusal,
        # five minutes); later records bind their count and the wait applied.
        if ("attempt" in binding) != ("wait_seconds" in binding):
            raise SafetyError("SOURCE_ACCESS_PROOF_INVALID")
        attempt, wait_seconds = binding.get("attempt", 1), binding.get("wait_seconds", 300)
        if (type(attempt) is not int or not 1 <= attempt <= 100000
                or type(wait_seconds) is not int or not 1 <= wait_seconds <= 7 * 24 * 3600
                or binding["next_retry_at"] != _access_retry_at(binding["retry_after"], binding["observed_at"],
                                                                attempt, wait_seconds)):
            raise SafetyError("SOURCE_ACCESS_PROOF_INVALID")
    except (KeyError, TypeError, ValueError):
        raise SafetyError("SOURCE_ACCESS_PROOF_INVALID") from None
    return row


def _access_head(state, scope):
    wanted = digest(scope); found = None
    for rid, row in state["batch_runs"].items():
        if row.get("kind") != ACCESS_SCOPE_KIND: continue
        body = {k: v for k, v in row.items() if k != "record_hash"}
        if (row.get("record_hash") != digest(body) or row.get("scope_hash") != digest(row.get("scope"))
                or rid != key("source-access-scope", row.get("scope"))
                or not isinstance(row.get("failure_ids"), list)
                or any(not isinstance(i, str) for i in row["failure_ids"])
                or len(set(row["failure_ids"])) != len(row["failure_ids"])
                or type(row.get("revision")) is not int or row["revision"] < 1):
            raise SafetyError("SOURCE_ACCESS_RECORD_INVALID")
        # Verify before trusting the scope lookup key: corruption cannot hide
        # an existing refusal and make a fresh request look unblocked.
        for failure_id in row["failure_ids"]:
            failure = _access_failure(state, failure_id)
            if failure["binding"]["scope_hash"] != row["scope_hash"]:
                raise SafetyError("SOURCE_ACCESS_RECORD_INVALID")
        if row.get("active_failure_id") is not None and row["active_failure_id"] not in row["failure_ids"]:
            raise SafetyError("SOURCE_ACCESS_RECORD_INVALID")
        if row.get("probe") is not None:
            from .source_acquisitions import _record
            probe = row["probe"]
            if (not isinstance(probe, dict) or set(probe) != {"id", "failure_id", "acquisition_id", "caller_id", "admitted_at"}
                    or probe["failure_id"] != row.get("active_failure_id")
                    or probe["id"] != key("source-access-probe", {k: v for k, v in probe.items() if k != "id"})):
                raise SafetyError("SOURCE_ACCESS_RECORD_INVALID")
            timestamp(probe["admitted_at"])
            original = _access_failure(state, probe["failure_id"])["binding"]
            if timestamp(probe["admitted_at"]) < timestamp(original["next_retry_at"]):
                raise SafetyError("SOURCE_ACCESS_RECORD_INVALID")
            probe_owner = _record(state, probe["acquisition_id"])
            if acquisition_access_scope(probe_owner, state=state, planned=True) != row["scope"]:
                raise SafetyError("SOURCE_ACCESS_RECORD_INVALID")
        if row.get("probe_closed_by") is not None:
            from .source_acquisitions import _record, _stage
            proof = row["probe_closed_by"]
            try:
                owner = _record(state, proof["acquisition_id"])
                response = _stage(state, owner, proof["response_id"])
                probe = proof["probe"]
                if (digest(response) != proof["response_hash"] or not source_response_closed(response)
                        or probe["acquisition_id"] != proof["acquisition_id"]
                        or probe["failure_id"] not in row["failure_ids"]
                        or probe["id"] != key("source-access-probe", {k: v for k, v in probe.items() if k != "id"})
                        or acquisition_access_scope(owner, state=state, planned=True) != row["scope"]
                        # Closed by a reply from another host, or by no answer at all.
                        or (acquisition_access_scope(owner, response) == row["scope"]
                            and response.get("access", {}).get("http_status") is not None)
                        or timestamp(response["observed_at"]) < timestamp(probe["admitted_at"])):
                    raise SafetyError("SOURCE_ACCESS_RECORD_INVALID")
            except (KeyError, TypeError, ValueError):
                raise SafetyError("SOURCE_ACCESS_RECORD_INVALID") from None
        if row.get("active_failure_id") is None:
            from .source_acquisitions import _record, _stage
            proof = row.get("cleared_by") or {}
            try:
                owner = _record(state, proof["acquisition_id"])
                response = _stage(state, owner, proof["response_id"])
                probe = proof["probe"]
                failure = _access_failure(state, probe["failure_id"])["binding"]
                answer = response["access"].get("http_status")
                if (digest(response) != proof["response_hash"] or response["stage_type"] != "response"
                        or type(answer) is not int or answer in _ACCESS_REFUSAL_STATUSES
                        or response["source_state"] == "blocked"
                        or not source_response_closed(response) or response["dispatch_started"] is not True
                        or probe["acquisition_id"] != proof["acquisition_id"]
                        or probe["failure_id"] not in row["failure_ids"] or failure["scope"] != row["scope"]
                        or probe["id"] != key("source-access-probe", {k: v for k, v in probe.items() if k != "id"})
                        or timestamp(probe["admitted_at"]) < timestamp(failure["next_retry_at"])
                        or timestamp(response["observed_at"]) < timestamp(probe["admitted_at"])
                        or acquisition_access_scope(owner, response) != row["scope"]):
                    raise SafetyError("SOURCE_ACCESS_RECORD_INVALID")
            except (KeyError, TypeError, ValueError):
                raise SafetyError("SOURCE_ACCESS_RECORD_INVALID") from None
        if row["scope_hash"] == wanted:
            if found is not None: raise SafetyError("SOURCE_ACCESS_RECORD_INVALID")
            found = (rid, row)
    return found


def source_access_wait(state, scope, *, as_of):
    """Pure current wait/probe view; saved bytes do not need this launch gate."""
    timestamp(as_of)
    head = _access_head(state, scope)
    if not head or head[1]["active_failure_id"] is None: return None
    row = head[1]; failure = _access_failure(state, row["active_failure_id"])
    binding = failure["binding"]
    response = state["source_checks"][binding["response_id"]]
    phase = ("probe_pending" if row["probe"] else "execution_unknown"
             if not source_response_closed(response) else
             "waiting" if timestamp(as_of) < timestamp(binding["next_retry_at"]) else "due")
    return {"scope": deepcopy(scope), "scope_id": head[0], "scope_hash": row["scope_hash"],
            "failure_id": row["active_failure_id"], "failure_hash": digest(failure),
            "phase": phase, "next_retry_at": binding["next_retry_at"],
            "acquisition_id": binding["acquisition_id"], "response_id": binding["response_id"],
            "response_hash": binding["response_hash"], "probe": deepcopy(row["probe"]),
            "reason": "SOURCE_ACCESS_ORIGINAL_RECOVERY_REQUIRED" if phase == "execution_unknown" else
                      "SOURCE_ACCESS_PROBE_PENDING" if row["probe"] else "SOURCE_ACCESS_WAIT"}


def _access_row(row):
    value = {k: deepcopy(v) for k, v in row.items() if k != "record_hash"}
    value["record_hash"] = digest(value)
    return value


def _source_retry_at(value, observed_at, fallback_seconds=300):
    """Honor a real seconds/date header; never shorten it to an API retry cap."""
    observed = timestamp(observed_at)
    if isinstance(value, str) and len(value) <= 256 and not any(ord(c) < 32 or ord(c) == 127 for c in value):
        if value.isascii() and value.isdigit() and len(value) <= 9:
            try: return (observed + timedelta(seconds=int(value))).isoformat(timespec="seconds")
            except OverflowError: pass
        try:
            due = parsedate_to_datetime(value)
            if due.tzinfo is None: due = due.replace(tzinfo=timezone.utc)
            if due >= observed: return due.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError): pass
    return (observed + timedelta(seconds=fallback_seconds)).isoformat(timespec="seconds")


def _access_backoff_seconds(attempt, *, challenge, image):
    steps = ACCESS_BACKOFF_SECONDS if challenge and not image else ACCESS_SHORT_BACKOFF_SECONDS
    return steps[min(attempt, len(steps)) - 1]


def _access_retry_at(retry_after, observed_at, attempt, wait_seconds):
    """When the attempt-th consecutive refusal, waiting wait_seconds, ends.

    The first refusal keeps the original rule (Retry-After, else the wait).
    Later ones wait at least their back-off, or longer when the website's own
    Retry-After says so. Only the record's own values are used.
    """
    due = _source_retry_at(retry_after, observed_at, fallback_seconds=wait_seconds)
    if attempt > 1:
        due = max(due, (timestamp(observed_at) + timedelta(seconds=wait_seconds)).isoformat(timespec="seconds"),
                  key=timestamp)
    return due


def source_access_backoffs(state, *, as_of):
    """Read-only list of current access waits and their refusal counts."""
    timestamp(as_of)
    rows = []
    for rid, row in state["batch_runs"].items():
        if row.get("kind") != ACCESS_SCOPE_KIND or row.get("active_failure_id") is None:
            continue
        wait = source_access_wait(state, row["scope"], as_of=as_of)
        binding = _access_failure(state, row["active_failure_id"])["binding"]
        rows.append({"host": row["scope"]["host"], "transport": row["scope"]["transport"],
                     "attempt": binding.get("attempt", 1), "wait_seconds": binding.get("wait_seconds", 300),
                     "challenge": binding["challenge"],
                     "http_status": binding["http_status"], "phase": wait["phase"],
                     "next_retry_at": wait["next_retry_at"]})
    return sorted(rows, key=lambda row: (row["host"], str(row["transport"])))


def _record_marketplace_access(memory, acquisition_id, response_id, *, as_of):
    """Record only a verified closed original result; repeated replay is inert."""
    from .source_acquisitions import _record, _response, _files_current
    state = memory.snapshot(); owner = _record(state, acquisition_id); files = []
    response = _response(state, owner, files_out=files)
    if not response or owner["active_response_id"] != response_id:
        raise SafetyError("SOURCE_ACCESS_PROOF_INVALID")
    access = response.get("access", {})
    if response["dispatch_started"] is not True: return
    scope = acquisition_access_scope(owner, response)
    planned_scope = acquisition_access_scope(owner, state=state, planned=True)
    if planned_scope != scope and source_response_closed(response):
        planned_head = _access_head(state, planned_scope)
        if planned_head and (planned_head[1].get("probe") or {}).get("acquisition_id") == acquisition_id:
            head_id, original = planned_head
            closed = _access_row(dict(original, probe=None, revision=original["revision"] + 1,
                probe_closed_by={"acquisition_id": acquisition_id, "response_id": response_id,
                    "response_hash": digest(response), "probe": deepcopy(original["probe"])}))
            def closed_current(current):
                _files_current(files)
                if current["source_checks"].get(response_id) != response:
                    raise SafetyError("SOURCE_ACCESS_PROOF_INVALID")
                _access_head(current, planned_scope)
            memory.commit(key("source-access-probe-other-scope", [digest(closed), response_id]),
                [change("batch_runs", head_id, closed, original)], validate=closed_current)
            state = memory.snapshot()
    head = _access_head(state, scope); old = head[1] if head else None
    status = access.get("http_status")
    refused = status in _ACCESS_REFUSAL_STATUSES or response["source_state"] == "blocked"
    own_probe = bool(old and (old.get("probe") or {}).get("acquisition_id") == acquisition_id)
    row = deepcopy(old) if old else {"kind": ACCESS_SCOPE_KIND, "scope": scope, "scope_hash": digest(scope),
        "failure_ids": [], "active_failure_id": None, "probe": None, "cleared_by": None, "revision": 0}
    additions = []
    if refused:
        # Replay is inert: one saved response is one refusal, whatever its count.
        if any(_access_failure(state, fid)["binding"]["response_id"] == response_id for fid in row["failure_ids"]):
            return
        # An older in-flight/refreshed wrapper cannot shorten a still-active
        # actual Retry-After. Keep the longer proved refusal as the head.
        prior = _access_failure(state, row["active_failure_id"])["binding"] if row["active_failure_id"] else None
        # Consecutive refusals since the last answer that cleared this scope.
        # Only the due probe's own reply, or a refusal observed after the
        # previous wait ended, is the next one; a late replay, an older read
        # or another lane's in-flight read keeps the current count.
        if prior is None:
            attempt = 1
        else:
            probe = row.get("probe") or {}
            next_one = (own_probe and probe.get("failure_id") == row["active_failure_id"]
                        or timestamp(response["observed_at"]) >= timestamp(prior["next_retry_at"]))
            attempt = prior.get("attempt", 1) + (1 if next_one else 0)
        wait_seconds = _access_backoff_seconds(attempt,
            challenge=status in _ACCESS_CHALLENGE_STATUSES or response["source_state"] == "blocked",
            image=owner["binding"]["route"] == "public_image_http")
        binding = {"scope": scope, "scope_hash": digest(scope), "acquisition_id": acquisition_id,
            "response_id": response_id, "response_hash": digest(response), "observed_at": response["observed_at"],
            "http_status": status, "challenge": response["source_state"] == "blocked",
            "retry_after": access.get("retry_after"), "attempt": attempt, "wait_seconds": wait_seconds,
            "next_retry_at": _access_retry_at(access.get("retry_after"), response["observed_at"], attempt, wait_seconds)}
        failure_id = key("source-access-refusal", binding)
        failure = {"kind": ACCESS_FAILURE_KIND, "binding": binding, "binding_hash": digest(binding)}
        additions.append(change("source_checks", failure_id, failure))
        row["failure_ids"].append(failure_id)
        cleared_response = state["source_checks"].get((old.get("cleared_by") or {}).get("response_id"), {}) if old else {}
        older_than_clear = (cleared_response.get("observed_at") is not None
                            and timestamp(binding["observed_at"]) < timestamp(cleared_response["observed_at"]))
        if not older_than_clear and (prior is None or timestamp(binding["next_retry_at"]) >= timestamp(prior["next_retry_at"])):
            row.update(active_failure_id=failure_id, probe=None, cleared_by=None)
    elif own_probe and type(status) is int and source_response_closed(response):
        # The host answered the due probe without refusing (200, 404, ...):
        # one dead link must not hold a whole host's wait open forever.
        row.update(active_failure_id=None, probe=None,
                   cleared_by={"acquisition_id": acquisition_id, "response_id": response_id,
                               "response_hash": digest(response), "probe": deepcopy(old["probe"])})
    elif own_probe and status is None and source_response_closed(response):
        # The probe ended without any answer from the host. The refusal wait
        # stays; the probe is released so a later due caller may probe again.
        row.update(probe=None, probe_closed_by={"acquisition_id": acquisition_id, "response_id": response_id,
                   "response_hash": digest(response), "probe": deepcopy(old["probe"])})
    else: return
    row["revision"] += 1; row = _access_row(row)
    def validate(current):
        _files_current(files)
        if digest(current["source_checks"].get(response_id)) != digest(response):
            raise SafetyError("SOURCE_ACCESS_PROOF_INVALID")
        _access_head(current, scope)
    memory.commit(key("source-access-result", [digest(row), response_id]),
        [*additions, change("batch_runs", key("source-access-scope", scope), row, old)], validate=validate)


def marketplace_access_admission(memory, resolution, *, caller_id, as_of):
    """Prepare probe CAS changes for the caller's SAME callback/intent commit."""
    identifier(caller_id); timestamp(as_of)
    scope = resolution["access_scope"]
    state = memory.snapshot(); wait = source_access_wait(state, scope, as_of=as_of)
    if wait is None: return {"changes": [], "caller_fields": {}, "context_fields": {}}
    head_id, old = _access_head(state, scope)
    if wait["phase"] == "probe_pending":
        from .source_acquisitions import _record
        owner = _record(state, resolution["acquisition_id"])
        if (wait["probe"]["acquisition_id"] != resolution["acquisition_id"]
                or owner["state"] != "reserved" or owner["stage_ids"]):
            raise SafetyError(wait["reason"])
        if wait["probe"]["caller_id"] == caller_id:
            fields = {"marketplace_access_probe": wait["probe"]["id"]}
            return {"changes": [], "caller_fields": fields, "context_fields": fields}
    elif wait["phase"] != "due": raise SafetyError(wait["reason"])
    probe = {"failure_id": wait["failure_id"], "acquisition_id": resolution["acquisition_id"],
             "caller_id": caller_id, "admitted_at": as_of}
    probe["id"] = key("source-access-probe", probe)
    row = _access_row(dict(old, probe=probe, revision=old["revision"] + 1))
    fields = {"marketplace_access_probe": probe["id"]}
    return {"changes": [change("batch_runs", head_id, row, old)],
            "caller_fields": fields, "context_fields": fields}


def _marketplace_shared_wait(memory, state, route, task_id, at, *, request=None):
    """Actual shared-route failures apply across both caller lanes.

    A question-specific missing item or parser failure does not create a
    website-wide pause. A canonical host probe may be owned by a new caller,
    but supersedes only the exact older refusal it proves due or recovered.
    Saved local result application precedes this launch-only guard.
    """
    waits = []
    for run_key, run in state["batch_runs"].items():
        if run.get("kind") != RUN_KIND: continue
        pause = _effective_route_pauses(memory, state, run_key, run).get(route)
        if not pause: continue
        original = state["source_checks"].get(pause.get("receipt_id"), {})
        if (original.get("kind") != CHECK_KIND or original.get("route") != route
                or original.get("envelope", {}).get("failure_scope") != "route"):
            raise SafetyError("SOURCE_PROGRESS_STAGE_INVALID")
        if route in {"blinkit", "zepto", "instamart"} and (request or {}).get("transport") == "funded-http":
            from .quickcommerce_browser import _proved_public_only
            if _proved_public_only(memory.root, original["envelope"], route):
                continue  # An actual public refusal is not a funded-provider refusal.
        if (pause.get("task_id") == task_id and pause.get("next_retry_at")
                and timestamp(pause["next_retry_at"]) <= timestamp(at)):
            continue
        if request is not None and _route_pause_covered_by_access(state, route, task_id, at, request, pause, original):
            continue
        waits.append(pause)
    if waits:
        return {"action": "wait", "reason": "SOURCE_MARKETPLACE_ROUTE_WAIT",
                "next_retry_at": min((p["next_retry_at"] for p in waits if p.get("next_retry_at")), default=None)}
    return None


def _route_pause_covered_by_access(state, route, task_id, at, request, pause, original):
    """An actual same-scope recovery can supersede its old controller pause.

    This is not a new launch voucher: the leaf separately verifies this
    caller's atomic probe admission. Here, a due candidate can reach that
    admission instead of remaining blocked by another caller's old receipt.
    """
    if pause.get("next_retry_at") and timestamp(pause["next_retry_at"]) > timestamp(at):
        return False
    scope = acquisition_access_scope({"binding": {"route": route, "request": request}})
    head = _access_head(state, scope)
    if not head:
        return False
    wait = source_access_wait(state, scope, as_of=at)
    if wait:
        if wait["phase"] == "due":
            recovery_at = at  # Preparation only; launch still needs admission.
        elif wait["phase"] == "probe_pending" and wait["probe"]["caller_id"] == task_id:
            recovery_at = wait["probe"]["admitted_at"]
        else:
            return False
    else:
        cleared = head[1].get("cleared_by")
        if not cleared:
            return False
        recovery_at = cleared["probe"]["admitted_at"]
    envelope = original.get("envelope", {})
    if (original.get("envelope_hash") != digest(envelope)
            or pause.get("receipt_id") != key("source-receipt", [original.get("task_id"),
                envelope.get("acquisition_id"), digest(envelope)])):
        raise SafetyError("SOURCE_PROGRESS_STAGE_INVALID")
    for failure_id in head[1]["failure_ids"]:
        failure = _access_failure(state, failure_id)["binding"]
        response = state["source_checks"][failure["response_id"]]
        if (failure["acquisition_id"] == envelope.get("acquisition_id")
                and failure["observed_at"] == envelope.get("observed_at")
                and response["receipt"]["path"] == envelope.get("reference")
                and timestamp(failure["next_retry_at"]) <= timestamp(recovery_at)):
            return True
    return False


def guard_shared_source_launch(memory, context, request, *, validate=None):
    """The actual neutral leaf must have this caller's atomic launch admission."""
    from .source_acquisitions import mark_source_dispatching
    from .work_turns import check_turn_launch
    if context.get("source_admitted") is not True:
        raise SafetyError("SOURCE_SHARED_ADMISSION_REQUIRED")
    aid = context["acquisition_id"]; check_turn_launch(memory, "source")
    def caller_guard(state, binding):
        if validate is not None:
            validate(state)
        caller = context.get("source_caller", {})
        if caller.get("table") == "query_tasks":
            original = state["query_tasks"].get(caller.get("id"), {})
            valid = (original.get("kind") == TASK_KIND and original.get("status") == "dispatching"
                and original.get("request_hash") == digest(original.get("request"))
                and caller["id"] == key("source-task", [original.get("run_id"), context["route"], original.get("request")]))
        elif caller.get("table") == "batch_runs":
            from .catalogue_discovery import validate_discovery_wave
            cycle = state["batch_runs"].get(caller.get("id"), {})
            validate_discovery_wave(state, cycle)
            original = cycle.get("actions", {}).get(context["task_id"], {})
            valid = original.get("state") == "intent_saved"
        else:
            original, valid = {}, False
        if (not valid or original.get("source_dispatch_contract") != "shared-source-caller-v1"
                or original.get("acquisition_id") != aid or original.get("acquisition_owner_ref") != aid
                or original.get("source_physical_request") != binding["request"] or binding["request"] != request
                or binding["route"] != context["route"]):
            raise SafetyError("SOURCE_SHARED_CALLER_INTENT_REQUIRED")
        turn = active_work_turn(memory)
        if turn is not None and turn["state"] != "open": raise SafetyError("WORK_TURN_CLOSED")
        if _marketplace_shared_wait(memory, state, context["route"], context["task_id"], context["as_of"], request=request):
            raise SafetyError("SOURCE_SHARED_ROUTE_WAIT")
        wait = source_access_wait(state, acquisition_access_scope({"binding": binding}), as_of=context["as_of"])
        if wait and not (wait["phase"] == "probe_pending"
                and wait["probe"]["id"] == context.get("source_access_probe")
                and original.get("source_access_probe") == wait["probe"]["id"]
                and wait["probe"]["acquisition_id"] == aid and wait["probe"]["caller_id"] == context["task_id"]):
            raise SafetyError(wait["reason"])
    result = mark_source_dispatching(memory, aid, as_of=context["as_of"], source_guard=caller_guard)
    if result["action"] != "saved": raise SafetyError("SOURCE_SHARED_ORIGINAL_RECOVERY_REQUIRED")
    check_turn_launch(memory, "source")


def direct_source_resolution_hooks(memory, *, route, root, prepare, saved, access_hint=None):
    """Join the installed Meta reader to the existing physical owner/host wait.

    Resolution and original-file replay are local, before callback admission.
    A retained legacy intent is unknown execution, not a chance to start over.
    The new neutral ABI leaves old marketplace callers byte-compatible.
    """
    from .source_acquisitions import (_owners, _response, adopt_meta_origin, adopt_quickcommerce_origin,
                                      claim_source_acquisition)
    from .research_questions import physical_request_fingerprint
    commerce = route in {"blinkit", "zepto", "instamart"}
    if route != "meta-ads" and not commerce: raise SafetyError("SOURCE_DIRECT_HOOK_ROUTE_INVALID")
    root = safe_path(root); trusted_roots = [root]
    def original_folder(state, aid, request):
        for row in state["captures"].values():
            if row.get("acquisition_id") == aid and row.get("source") == (route if commerce else "meta") and isinstance(row.get("reference"), str):
                path = safe_path(row["reference"])
                if path.name in {"receipt.json", "deadline_reparse.json", "capture.json"} and path.parent.name == aid:
                    return path.parent
        if commerce:
            return (memory.root / "quickcommerce_captures" / aid if request["transport"] == "funded-http" else
                    memory.root / "quickcommerce_free" / "acquisitions" / aid)
        return root / aid

    def resolve(context):
        request = prepare(context); state = memory.snapshot()
        original_task = state["query_tasks"].get(context.get("task_id"), {})
        if (context.get("reconciling") and original_task.get("kind") == TASK_KIND
                and original_task.get("source_dispatch_contract") == "shared-source-caller-v1"
                and original_task.get("acquisition_id") == context.get("acquisition_id")
                and original_task.get("acquisition_owner_ref") == context.get("acquisition_id")):
            request = prepare(dict(context, request=original_task["source_physical_request"]))
        fingerprint = physical_request_fingerprint(route, request)
        owners = _owners(state, fingerprint)
        if not owners:
            origins = {}
            def consider(table, rid, candidate, aid, qid=None):
                if not aid or candidate.get("route") != route: return
                if candidate.get("source_dispatch_contract") == "shared-source-caller-v1" and candidate.get("source_physical_request"):
                    candidate = dict(candidate, request=candidate["source_physical_request"])
                try: actual = prepare(candidate)
                except SafetyError: return
                if actual == request: origins.setdefault(aid, (table, rid, qid))
            for rid, row in state["query_tasks"].items():
                if row.get("kind") == TASK_KIND and row.get("generation", 0) > 0:
                    consider("query_tasks", rid, row, row.get("acquisition_id"))
            for rid, row in state["batch_runs"].items():
                if row.get("kind") != "catalogue-discovery-cycle-v1": continue
                for planned in row.get("planned_actions", []):
                    for question in planned.get("questions", []):
                        consider("batch_runs", rid, question, row.get("actions", {}).get(question["id"], {}).get("acquisition_id"), question["id"])
            from .meta_browser import VERSION
            for rid, row in state["captures"].items():
                reference = row.get("reference")
                if row.get("source") != (route if commerce else "meta") or not isinstance(reference, str) or not reference.startswith("/"): continue
                path = safe_path(reference)
                if not any(path.is_relative_to(Path(p)) for p in [memory.root, root]) or not path.is_file(): continue
                original = load_json(path, 8 * 1024 * 1024)
                if isinstance(original, dict) and (commerce and original.get("version") == "quickcommerce-location-v1"
                                                    or not commerce and original.get("version") == VERSION):
                    consider("captures", rid, {"route": route, "request": original.get("request")}, row.get("acquisition_id"))
            if len(origins) > 1:
                return {"action": "wait", "reason": "SOURCE_DIRECT_LEGACY_ORIGINS_NEED_REVIEW"}
            if origins:
                _, (table, rid, qid) = next(iter(origins.items()))
                adopter = adopt_quickcommerce_origin if commerce else adopt_meta_origin
                adopter(memory, request, table=table, record_id=rid, question_id=qid,
                        as_of=context["as_of"], artifact_roots=trusted_roots)
        channel = "funded_provider" if request.get("transport") == "funded-http" else "direct_local"
        fee = "unknown" if channel == "funded_provider" else "known_zero"
        claim = claim_source_acquisition(memory, route, request, owner_id=context["task_id"], as_of=context["as_of"],
            original_acquisition_id=None if context.get("refresh_due_at") else context["acquisition_id"],
            refresh_due_at=context.get("refresh_due_at"), refresh_reason=context.get("refresh_reason"),
            collection_channel=channel, incremental_collection_fee=fee, artifact_roots=trusted_roots)
        aid = claim["acquisition_id"]; state = memory.snapshot(); owner = state["batch_runs"][aid]
        folder = original_folder(state, aid, request)
        if not any(folder.is_relative_to(Path(p)) for p in owner["binding"]["artifact_roots"]):
            raise SafetyError("SOURCE_ACQUISITION_FILE_SCOPE_INVALID")
        if claim["action"] == "refresh_due":
            return {"action": "wait", "reason": "SOURCE_DIRECT_REFRESH_DUE", "acquisition_id": aid}
        scope = acquisition_access_scope(owner)
        if owner["state"] == "reserved" and not owner["stage_ids"]:
            if access_hint is not None:
                advice = access_hint(dict(context, request=request, acquisition_id=aid))
                if not advice["ready"]:
                    return {"action": "wait", "acquisition_id": aid, "reason": advice["reason"], "next_retry_at": advice.get("next_retry_at")}
            pause = _marketplace_shared_wait(memory, state, route, context["task_id"], context["as_of"], request=request)
            if pause: return dict(pause, acquisition_id=aid)
            wait = source_access_wait(state, scope, as_of=context["as_of"])
            if wait and not (wait["phase"] == "due" or wait["phase"] == "probe_pending" and wait["probe"]["acquisition_id"] == aid):
                return {"action": "wait", "reason": wait["reason"], "next_retry_at": wait["next_retry_at"], "acquisition_id": aid}
            return {"action": "collect", "acquisition_id": aid, "request": request, "access_scope": scope}
        try:
            receipt = saved(dict(context, acquisition_id=aid, request=request, record_dispatch=None,
                                 reconciling=True, source_original_folder=str(folder)))
        except (FileNotFoundError, SafetyError) as error:
            if isinstance(error, SafetyError) and str(error) not in {"SOURCE_ORIGINAL_RECEIPT_PENDING", "SOURCE_ACQUISITION_FILE_UNAVAILABLE", "QUICKCOMMERCE_SAVED_CAPTURE_REQUIRED"}: raise
            return {"action": "wait", "reason": "SOURCE_DIRECT_ORIGINAL_RECEIPT_PENDING", "acquisition_id": aid}
        wait = source_access_wait(memory.snapshot(), scope, as_of=context["as_of"])
        if not context.get("reconciling") and wait and wait["phase"] == "due" and wait["acquisition_id"] == aid:
            recovery = {field: wait[field] for field in ("failure_id", "failure_hash", "response_id", "response_hash", "next_retry_at")}
            renewed = claim_source_acquisition(memory, route, request, owner_id=context["task_id"], as_of=context["as_of"],
                collection_channel=channel, incremental_collection_fee=fee, artifact_roots=trusted_roots, access_recovery=recovery)
            return {"action": "collect", "acquisition_id": renewed["acquisition_id"], "request": request, "access_scope": scope}
        return {"action": "saved", "acquisition_id": aid, "request": request, "receipt": receipt, "original_folder": str(folder)}

    def admission(context, resolution):
        result = marketplace_access_admission(memory, resolution, caller_id=context["task_id"], as_of=context["as_of"])
        return dict(result, caller_fields={k.replace("marketplace_", "source_", 1): v for k, v in result["caller_fields"].items()},
                    context_fields={k.replace("marketplace_", "source_", 1): v for k, v in result["context_fields"].items()})

    def due_hint(context, *, state=None):
        request = prepare(context); state = memory.snapshot() if state is None else state
        owners = _owners(state, physical_request_fingerprint(route, request)); aid, owner = owners[-1] if owners else (None, None)
        response = _response(state, owner) if owner else None
        local_saved = bool(response and response["complete"])
        ready = owner is None or owner["state"] == "reserved" and not owner["stage_ids"]
        wait = source_access_wait(state, acquisition_access_scope({"binding": {"route": route, "request": request}}), as_of=context["as_of"])
        advice = access_hint(dict(context, request=request, acquisition_id=aid)) if access_hint is not None else None
        if wait:
            ready = (wait["phase"] == "due" and (ready or wait["acquisition_id"] == aid)
                     or wait["phase"] == "probe_pending" and ready and wait["probe"]["acquisition_id"] == aid)
        if advice is not None: ready = ready and advice["ready"]
        return {"ready": bool(ready or local_saved), "local_saved": local_saved,
            "next_retry_at": wait["next_retry_at"] if wait and wait["phase"] == "waiting" else advice.get("next_retry_at") if advice else None,
            "reason": wait["reason"] if wait else advice["reason"] if advice and not advice["ready"] else "SOURCE_DIRECT_ORIGINAL_RECOVERY_REQUIRED" if not ready and not local_saved else None,
            "signal": {"wait": wait, "acquisition_id": aid, "state": owner["state"] if owner else None,
                       "response_id": owner["active_response_id"] if owner else None,
                       "complete": bool(response and response["complete"]),
                       "observed_at": response["observed_at"] if response else None,
                       "access_hint": advice}}

    def saved_hint(context, *, state=None):
        request = prepare(context); state = memory.snapshot() if state is None else state
        owners = _owners(state, physical_request_fingerprint(route, request)); aid, owner = owners[-1] if owners else (None, None)
        files = []
        if aid:
            folder = original_folder(state, aid, request)
            if not any(folder.is_relative_to(Path(p)) for p in owner["binding"]["artifact_roots"]):
                raise SafetyError("SOURCE_ACQUISITION_FILE_SCOPE_INVALID")
            for name in ("receipt.json", "capture.json", "probe.json", "native_result.json", "native_outer_deadline.json", "outer_deadline.json",
                         "snapshot.json", "transport.json", "original_response.json", "funded_intent.json",
                         "native/native_result.json", "native/native_outer_deadline.json", "deadline_reparse.json", "deadline_snapshot.json"):
                path = safe_path(folder / name)
                try: info = path.stat()
                except FileNotFoundError: continue
                if not stat.S_ISREG(info.st_mode): raise SafetyError("SOURCE_ACQUISITION_FILE_INVALID")
                files.append([name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
        return {"kind": "saved-source-wake-hint-v1", "acquisition_id": aid, "files": files}
    return {"prepare_source": prepare, "resolve_source": resolve, "source_admission": admission,
            "source_due_hint": due_hint, "source_saved_hint": saved_hint,
            "source_due_hint_from_state": due_hint, "source_saved_hint_from_state": saved_hint}


def create_marketplace_handlers(memory, *, data_root=None, transport="auto",
                                capture=None, replay=None, importer=None,
                                shared_acquisitions=False, artifact_roots=()):
    """Connect installed free capture/import functions; retain paid imports.

    Reconciliation only verifies the original owned folder. It never starts a
    replacement browser. A list acquisition can serve many targets, but each
    target remains pending until an accepted product-family relevance review.
    """
    from .marketplace_browser import (build_marketplace_request, capture_marketplace,
                                      reconcile_marketplace, replay_marketplace, validate_request)
    from .research_memory import import_direct_marketplace
    if type(shared_acquisitions) is not bool:
        raise SafetyError("SOURCE_MARKETPLACE_SHARED_OPTION_INVALID")
    capture = capture or capture_marketplace
    replay = replay or replay_marketplace
    importer = importer or import_direct_marketplace
    installed_recovery = replay is replay_marketplace and importer is import_direct_marketplace
    root = safe_path(data_root or (memory.root / "direct_marketplace"))
    if not isinstance(artifact_roots, (tuple, list)):
        raise SafetyError("SOURCE_ACQUISITION_ROOTS_INVALID")
    trusted_roots = [root, *artifact_roots]
    if shared_acquisitions:
        from .source_acquisitions import _roots
        _roots([memory.root, *trusted_roots])
    handlers = {}
    def prepare(context):
        request = context["request"]
        if "url" not in request:
            if shared_acquisitions and (not set(request) <= {"query", "term", "country", "department", "limit"}
                    or request.get("country", "IN") != "IN"
                    or request.get("query") is not None and request.get("term") is not None
                       and request["query"] != request["term"]):
                raise SafetyError("SOURCE_MARKETPLACE_SPARSE_SCOPE_UNSUPPORTED")
            request = build_marketplace_request(context["route"], term=request.get("query", request.get("term")),
                department=request.get("department"), limit=request.get("limit", 30))
        validate_request(request)
        if request["route"] != context["route"]:
            raise SafetyError("SOURCE_MARKETPLACE_ROUTE_MISMATCH")
        return deepcopy(request)

    def collect(context, *, reconcile=False):
        request = prepare(context)
        acquisition_id = context["acquisition_id"]
        folder = safe_path(context.get("marketplace_original_folder", root / acquisition_id))
        if shared_acquisitions and not reconcile:
            from .source_acquisitions import mark_source_dispatching
            from .work_turns import check_turn_launch
            if context.get("marketplace_admitted") is not True:
                raise SafetyError("SOURCE_MARKETPLACE_ADMISSION_REQUIRED")
            check_turn_launch(memory, "source")
            def caller_guard(state, binding):
                caller = context.get("marketplace_caller", {})
                if caller.get("table") == "query_tasks":
                    original = state["query_tasks"].get(caller.get("id"), {})
                    valid = (original.get("kind") == TASK_KIND and original.get("status") == "dispatching"
                             and original.get("marketplace_dispatch_contract") == "shared-marketplace-caller-v1")
                elif caller.get("table") == "batch_runs":
                    cycle = state["batch_runs"].get(caller.get("id"), {})
                    original = cycle.get("actions", {}).get(context["task_id"], {})
                    cycle_valid = cycle.get("kind") == "catalogue-discovery-cycle-v1"
                    if cycle.get("kind") == "discovery-wave-v2":
                        from .catalogue_discovery import validate_discovery_wave
                        validate_discovery_wave(state, cycle)
                        cycle_valid = True
                    valid = (cycle_valid
                             and original.get("state") == "intent_saved"
                             and original.get("marketplace_dispatch_contract") == "shared-marketplace-caller-v1")
                else:
                    original, valid = {}, False
                if (not valid or original.get("acquisition_id") != acquisition_id
                    or original.get("acquisition_owner_ref") != acquisition_id
                    or original.get("marketplace_physical_request") != binding["request"]):
                    raise SafetyError("SOURCE_MARKETPLACE_CALLER_INTENT_REQUIRED")
                turn = active_work_turn(memory)
                if turn is not None and turn["state"] != "open":
                    raise SafetyError("WORK_TURN_CLOSED")
                if _marketplace_shared_wait(memory, state, context["route"], context["task_id"], context["as_of"], request=request):
                    raise SafetyError("SOURCE_MARKETPLACE_ROUTE_WAIT")
                wait = source_access_wait(state, source_access_scope(request["url"]), as_of=context["as_of"])
                if wait and not (wait["phase"] == "probe_pending"
                        and wait["probe"]["id"] == context.get("marketplace_access_probe")
                        and original.get("marketplace_access_probe") == wait["probe"]["id"]
                        and wait["probe"]["acquisition_id"] == acquisition_id
                        and wait["probe"]["caller_id"] == context["task_id"]):
                    raise SafetyError(wait["reason"])
            result = mark_source_dispatching(memory, acquisition_id, as_of=context["as_of"], source_guard=caller_guard)
            if result["action"] != "saved":
                raise SafetyError("SOURCE_MARKETPLACE_ORIGINAL_RECOVERY_REQUIRED")
            check_turn_launch(memory, "source")
        if reconcile:
            receipt = reconcile_marketplace(folder) if installed_recovery else replay(folder)
        else:
            receipt = capture(request, acquisition_id, data_root=root, transport=transport,
                              timeout=min(45, max(15, int(context.get("timeout_seconds", 45)))))
        if receipt.get("request") != request or receipt.get("acquisition_id") != acquisition_id:
            raise SafetyError("SOURCE_MARKETPLACE_RECEIPT_MISMATCH")
        # Native child navigation proof is retained by its immutable receipt.
        # Startup alone never calls this actual-dispatch notification.
        if receipt.get("dispatch_started") is True and context.get("record_dispatch"):
            context["record_dispatch"]()
        imported = importer(memory, folder, saved_only_recovery=True) if reconcile and installed_recovery else importer(memory, folder)
        complete = None
        if shared_acquisitions:
            from .source_acquisitions import record_marketplace_response
            commit_at = max(context["as_of"], utc_now(timespec="microseconds"), key=timestamp)
            bridged = record_marketplace_response(memory, acquisition_id, folder=folder,
                as_of=commit_at, capture_ids=[imported["capture_id"]])
            complete = memory.snapshot()["source_checks"][bridged["response_id"]]["complete"]
            _record_marketplace_access(memory, acquisition_id, bridged["response_id"],
                                      as_of=commit_at)
        state = receipt["state"]
        reason = receipt["reason"]
        if complete is False and state == "empty":
            state, reason = "insufficient", "MARKETPLACE_EMPTY_RESPONSE_COVERAGE_INCOMPLETE"
        elif complete is False and state == "usable":
            state = "partial"
        target_state = ("interpretation_pending" if state in {"usable", "partial"} else
                        "completed_no_results" if state == "empty" else
                        "not_attempted" if state == "unavailable" and not receipt["dispatch_started"] else
                        "failed" if state == "unavailable" else state)
        return {"schema_version": 1, "route": context["route"], "acquisition_id": acquisition_id,
                "observed_at": receipt["observed_at"], "state": state,
                "dispatch_started": receipt["dispatch_started"], "reference": str(folder / receipt.get("receipt_file", "receipt.json")),
                "capture_ids": [imported["capture_id"]], "observation_ids": imported["observation_ids"],
                "target_results": [{"concept_id": cid, "description_hash": context["description_hashes"][cid],
                    "state": target_state, "evidence_refs": imported["observation_ids"],
                    "reason": "Actual marketplace rows need product-family relevance interpretation." if target_state == "interpretation_pending" else reason}
                    for cid in context["concept_ids"]], "reason": reason,
                "retryable": receipt.get("navigation_status") in {429, 500, 502, 503, 504},
                "access_wait": source_access_wait(memory.snapshot(), source_access_scope(request["url"]),
                                                  as_of=context["as_of"]) if shared_acquisitions else None}

    def resolve(context):
        """Local-only resolution runs before a caller reserves callback time.

        A missing result for an original intent is a wait, not another call to
        capture. Real original tasks/cycles/imports are adopted before a new
        owner can be created; unsupported origins remain a named local wait.
        """
        from .source_acquisitions import (ACQUISITION_KIND, adopt_marketplace_origin,
                                          claim_source_acquisition)
        from .research_questions import physical_request_fingerprint
        request = prepare(context); route = context["route"]
        wanted = physical_request_fingerprint(route, request)
        state = memory.snapshot()
        # claim() verifies all positively typed owner records; this scan merely
        # finds actual retained legacy origins if no exact owner exists yet.
        exact_owner = any(row.get("kind") == ACQUISITION_KIND
                          and row.get("binding", {}).get("physical_request_fingerprint") == wanted
                          for row in state["batch_runs"].values())
        origins = {}
        if not exact_owner:
            def consider(table, rid, candidate, aid, qid=None, folder=None):
                if not aid or candidate.get("route") != route:
                    return
                try:
                    actual = prepare(candidate)
                except SafetyError:
                    return
                if physical_request_fingerprint(route, actual) == wanted:
                    origins.setdefault(aid, (table, rid, qid, folder))
            for rid, task in state["query_tasks"].items():
                if task.get("kind") == TASK_KIND and task.get("generation", 0) > 0:
                    consider("query_tasks", rid, task, task.get("acquisition_id"))
            for rid, cycle in state["batch_runs"].items():
                if cycle.get("kind") != "catalogue-discovery-cycle-v1": continue
                for planned in cycle.get("planned_actions", []):
                    for question in planned.get("questions", []):
                        action = cycle.get("actions", {}).get(question["id"], {})
                        consider("batch_runs", rid, question, action.get("acquisition_id"), question["id"])
            for rid, original in state["captures"].items():
                ref = original.get("reference", {})
                if isinstance(ref, dict) and isinstance(ref.get("request"), dict) and ref.get("folder"):
                    consider("captures", rid, {"route": ref.get("route"), "request": ref["request"]},
                             original.get("acquisition_id"), folder=ref["folder"])
                    if original.get("acquisition_id") in origins:
                        old = origins[original["acquisition_id"]]
                        origins[original["acquisition_id"]] = (*old[:3], ref["folder"])
            if len(origins) > 1:
                return {"action": "wait", "reason": "SOURCE_MARKETPLACE_LEGACY_ORIGINS_NEED_REVIEW"}
            if origins:
                aid, (table, rid, qid, folder) = next(iter(origins.items()))
                # Explicit configured artifact areas, never filesystem root.
                adopt_marketplace_origin(memory, route, request, table=table, record_id=rid,
                    question_id=qid, as_of=context["as_of"], artifact_roots=trusted_roots)
        claim = claim_source_acquisition(memory, route, request,
            owner_id=context["task_id"], as_of=context["as_of"],
            original_acquisition_id=None if context.get("refresh_due_at") else context["acquisition_id"],
            refresh_due_at=context.get("refresh_due_at"), refresh_reason=context.get("refresh_reason"),
            collection_channel="direct_local", incremental_collection_fee="known_zero", artifact_roots=trusted_roots)
        aid = claim["acquisition_id"]
        owner = memory.snapshot()["batch_runs"][aid]
        original_folder = root / aid
        # A verified import may point to the actual old configured area. Do
        # not copy it or replace its acquisition/date with this consumer's.
        for capture_row in state["captures"].values():
            if capture_row.get("acquisition_id") == aid:
                ref = capture_row.get("reference", {})
                if isinstance(ref, dict) and ref.get("folder"):
                    original_folder = safe_path(ref["folder"]); break
        if not any(original_folder.is_relative_to(Path(allowed)) for allowed in owner["binding"]["artifact_roots"]):
            raise SafetyError("SOURCE_ACQUISITION_FILE_SCOPE_INVALID")
        if claim["action"] == "refresh_due":
            return {"action": "wait", "reason": "SOURCE_MARKETPLACE_REFRESH_DUE", "acquisition_id": aid}
        if (owner["state"] == "reserved" and owner["active_dispatch_intent_id"] is None
                and not owner["stage_ids"]):
            # Only this opt-in collector can enter capture, and it first
            # commits the dispatch stage. A reserved owner with NO such stage
            # is therefore genuinely before that leaf, even if an earlier
            # caller admission was saved. Another eligible caller may service
            # that same origin; it cannot create another acquisition or refund
            # the old callback slot. Legacy adoption is execution_unknown.
            pause = _marketplace_shared_wait(memory, memory.snapshot(), route, context["task_id"], context["as_of"], request=request)
            if pause: return dict(pause, acquisition_id=aid)
            access_scope = source_access_scope(request["url"])
            wait = source_access_wait(memory.snapshot(), access_scope, as_of=context["as_of"])
            if wait and not (wait["phase"] == "due" or wait["phase"] == "probe_pending"
                             and wait["probe"]["acquisition_id"] == aid):
                return {"action": "wait", "acquisition_id": aid, "reason": wait["reason"],
                        "next_retry_at": wait["next_retry_at"]}
            return {"action": "collect", "acquisition_id": aid, "request": request,
                    "access_scope": access_scope, "resume_reserved": bool(context.get("reconciling"))}
        local = dict(context, acquisition_id=aid, request=request, reconciling=True,
                     record_dispatch=None, marketplace_original_folder=str(original_folder))
        try:
            receipt = collect(local, reconcile=True)
        except FileNotFoundError:
            return {"action": "wait", "acquisition_id": aid,
                    "reason": "SOURCE_MARKETPLACE_ORIGINAL_RECEIPT_PENDING"}
        wait = source_access_wait(memory.snapshot(), source_access_scope(request["url"]), as_of=context["as_of"])
        if (not context.get("reconciling") and wait and wait["phase"] == "due"
                and wait["acquisition_id"] == aid):
            recovery = {field: wait[field] for field in
                        ("failure_id", "failure_hash", "response_id", "response_hash", "next_retry_at")}
            renewed = claim_source_acquisition(memory, route, request, owner_id=context["task_id"],
                as_of=context["as_of"], collection_channel="direct_local", incremental_collection_fee="known_zero",
                artifact_roots=trusted_roots, access_recovery=recovery)
            return {"action": "collect", "acquisition_id": renewed["acquisition_id"], "request": request,
                    "access_scope": source_access_scope(request["url"])}
        return {"action": "saved", "acquisition_id": aid, "request": request,
                "receipt": receipt, "original_folder": str(original_folder)}

    def admission(context, resolution):
        return marketplace_access_admission(memory, resolution, caller_id=context["task_id"], as_of=context["as_of"])

    def due_hint(context, *, state=None):
        from .source_acquisitions import _owners, _stage
        from .research_questions import physical_request_fingerprint
        request = prepare(context)
        state = memory.snapshot() if state is None else state
        wait = source_access_wait(state, source_access_scope(request["url"]), as_of=context["as_of"])
        owners = _owners(state, physical_request_fingerprint(context["route"], request))
        aid, owner = owners[-1] if owners else (None, None)
        response = _stage(state, owner, owner["active_response_id"]) if owner and owner["active_response_id"] else None
        local_saved = bool(response and response.get("complete") is True)
        ready = owner is None or owner["state"] == "reserved" and not owner["stage_ids"]
        if wait:
            ready = (wait["phase"] == "due" and (ready or wait["acquisition_id"] == aid)
                     or wait["phase"] == "probe_pending" and ready and wait["probe"]["acquisition_id"] == aid)
        ready = ready or local_saved
        return {"ready": bool(ready), "local_saved": local_saved,
                "next_retry_at": wait["next_retry_at"] if wait and wait["phase"] == "waiting" else None,
                "reason": wait["reason"] if wait else "SOURCE_MARKETPLACE_ORIGINAL_RECOVERY_REQUIRED" if not ready else None,
                "signal": {"wait": wait, "acquisition_id": aid, "state": owner["state"] if owner else None,
                           "response_id": owner["active_response_id"] if owner else None}}

    def saved_hint(context, *, state=None):
        """Pure bounded wake hint, NOT original-result or launch proof.

        A native child can finish saving after the controller's last canonical
        event. Stat only its exact original filenames so a settled programme
        wakes to verify those bytes. Do not import, adopt, parse or navigate.
        """
        from .source_acquisitions import _owners
        from .research_questions import physical_request_fingerprint
        request = prepare(context); state = memory.snapshot() if state is None else state
        owners = _owners(state, physical_request_fingerprint(context["route"], request))
        aid, row = owners[-1] if owners else (None, None)
        allowed_roots = row["binding"]["artifact_roots"] if row else [str(memory.root), *map(str, trusted_roots)]
        if aid is None:
            # An actual old caller may not have been adopted yet. Its original
            # canonical acquisition is a change hint, not an unstarted origin.
            original = state["query_tasks"].get(context.get("task_id"), {})
            if (original.get("kind") == TASK_KIND and original.get("acquisition_id")
                    and prepare(original) == request):
                aid = original["acquisition_id"]
        folder = root / aid if aid else None
        if aid:
            for capture_row in state["captures"].values():
                ref = capture_row.get("reference", {})
                if capture_row.get("acquisition_id") == aid and isinstance(ref, dict) and ref.get("folder"):
                    folder = safe_path(ref["folder"]); break
        files = []
        if folder is not None:
            folder = safe_path(folder)
            if not any(folder.is_relative_to(Path(allowed)) for allowed in allowed_roots):
                raise SafetyError("SOURCE_ACQUISITION_FILE_SCOPE_INVALID")
            for name in ("intent.json", "receipt.json", "native_result.json", "native_dispatch.json",
                         "native_outer_deadline.json", "outer_deadline.json", "snapshot.json", "transport.json",
                         "deadline_reparse.json", "deadline_parsed.json"):
                path = safe_path(folder / name)
                try:
                    info = path.stat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise SafetyError("SOURCE_ACQUISITION_FILE_INVALID")
                files.append([name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
        return {"kind": "saved-marketplace-wake-hint-v1", "acquisition_id": aid, "files": files}
    for route in ("amazon-bestsellers", "amazon-movers-and-shakers", "amazon-search", "flipkart-search"):
        handlers[route] = {"handler": collect, "reconcile": lambda context: collect(context, reconcile=True)}
        if shared_acquisitions:
            if not installed_recovery:
                raise SafetyError("SOURCE_MARKETPLACE_SHARED_INSTALLED_RECOVERY_REQUIRED")
            handlers[route].update(prepare_marketplace=prepare, resolve_marketplace=resolve,
                                   marketplace_saved_hint=saved_hint, marketplace_admission=admission,
                                   marketplace_due_hint=due_hint,
                                   marketplace_saved_hint_from_state=saved_hint,
                                   marketplace_due_hint_from_state=due_hint)
    return handlers


def installed_source_handlers(memory, *, marketplace_options=None, supplier_options=None,
                              trends_options=None, meta_options=None, quickcommerce_options=None, quickcommerce_client=None,
                              meta_handler=None, trends_handler=None):
    """Compose concrete installed functions, not imaginary available sources.

    Optional account clients are supplied privately by the existing connection
    layer. This function does not read keys, start a browser, inspect balances,
    install dependencies or upgrade billing. Missing routes remain Not connected.
    """
    handlers = {}
    from .supplier_route_acquisition import create_supplier_handlers
    from .trends_controller import create_trends_handlers
    from .meta_browser import meta_direct_source_handlers
    from .quickcommerce_browser import create_quickcommerce_handlers
    handlers.update(create_marketplace_handlers(memory, **(marketplace_options or {})))
    handlers.update(create_supplier_handlers(memory, **(supplier_options or {})))
    handlers.update(create_trends_handlers(memory, **(trends_options or {})))
    handlers["meta-ads"] = meta_direct_source_handlers(memory, **(meta_options or {}))
    commerce = create_quickcommerce_handlers(memory, funded_client=quickcommerce_client,
                                             **(quickcommerce_options or {}))
    def commerce_reconciler(callback):
        def reconcile(context):
            acquisition = context["acquisition_id"]
            funded = safe_path(memory.root, "quickcommerce_captures", acquisition) / "capture.json"
            public = safe_path(memory.root, "quickcommerce_free", "acquisitions", acquisition)
            # All three are original source proof, never authority to search.
            # The native case lets its own parser finish a saved-stage capture.
            if not any(path.is_file() for path in (funded, public / "probe.json", public / "native/native_result.json",
                                                   public / "native/native_outer_deadline.json")):
                raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
            return callback(dict(context, reconciling=True))
        return reconcile
    for route, value in commerce.items():
        callback = value["handler"] if isinstance(value, dict) else value
        handlers[route] = dict(value if isinstance(value, dict) else {},
                               handler=callback, reconcile=value["reconcile"] if isinstance(value, dict)
                                   and callable(value.get("resolve_source")) else commerce_reconciler(callback))
    for route, handler in (("meta-ads", meta_handler), ("google-trends", trends_handler)):
        if handler is not None:
            handlers[route] = handler
    route_registry(handlers)
    return handlers


def _functional_task_sparse(state, task_id, concept_id, meaning_hash, *, as_of):
    """An original probe may advance only after its own retained rows settle.

    Functional reviews are overlays, not changes to frozen legacy targets.
    Neither another task's result nor a new global replacement can stand in
    for an accepted interpretation of this original capture. A relevant row,
    including neutral/weak demand, completes this wording rather than asking
    another phrase in search of a more encouraging answer.
    """
    from .source_interpretation import (FUNCTIONAL_MATCH_METHOD, functional_source_questions)
    from .research_scope import validated_scoped_evidence
    from .market_validation import _functional_match
    task = state["query_tasks"][task_id]
    questions = functional_source_questions(state, run_id=task["run_id"], concept_ids=[concept_id],
        task_ids=[task_id], as_of=as_of)
    retained = {ref for receipt_id in task.get("receipt_refs", [])
        for ref in (state["source_checks"][receipt_id]["envelope"].get("observation_ids", [])
                    + state["source_checks"][receipt_id]["envelope"].get("evidence_refs", []))
        if ref in state["observations"] or ref in state["trends_evidence"]}
    if not retained:
        receipts = [state["source_checks"][ref]["envelope"] for ref in task.get("receipt_refs", [])]
        return bool(receipts and task.get("status") == "completed_no_results"
                    and receipts[-1].get("status") == "completed_no_results"
                    and receipts[-1].get("dispatch_started") is True)
    if (not questions
            or any(row["meaning_hash"] != meaning_hash or row["route"] != task["route"]
                   or row["unknown_ids"] for row in questions)
            or {ref for row in questions for ref in row["observation_ids"]} != retained):
        return False
    scoped = validated_scoped_evidence(state, concept_id, as_of=as_of)
    for ref in retained:
        matches = [row for row in scoped if row["method"] == FUNCTIONAL_MATCH_METHOD
                   and row["route"] == task["route"] and any(
                       value["id"] == ref and value["table"] in {"observations", "trends_evidence"}
                       for value in row["references"]) and _functional_match(row, ref)]
        if not matches or any(row["relationship"] not in {"unrelated", "unclear"} for row in matches):
            return False
    return True


def product_source_request_builder(*, location=None, catalogue=None, run_id=None, shared_suppliers=False):
    """Use accepted product-type wording, shared list scopes and real page URLs.

    The profile/cache is frozen by the caller's planning snapshot. There is no
    hidden model step or full-title fallback. Company reads are dependency work:
    no discovered destination means a recorded preparation gap, not a fake read.
    """
    if run_id is not None:
        identifier(run_id)
    if type(shared_suppliers) is not bool:
        raise SafetyError("SOURCE_SUPPLIER_SHARED_OPTION_INVALID")
    profiles, histories = {}, {}
    planning_state, planning_run = None, None
    departments = {"Home and Decor": "kitchen/1380374031", "Home and Kitchen": "kitchen",
                   "Baby Products": "baby", "Travel and Outdoor": "luggage"}
    def build(context):
        nonlocal planning_state, planning_run, profiles, histories
        from .product_queries import resolve_query_profile
        from .discovery import normalise
        cid, state, route = context["concept_id"], context["state"], context["route"]
        if route in RETIRED_SUPPLIER_ROUTES:
            raise SafetyError("SUPPLIER_ROUTE_RETIRED")  # Never planned; fail closed if asked.
        current_run = run_id or context.get("run_id")
        if state is not planning_state or current_run != planning_run:
            planning_state, planning_run, profiles, histories = state, current_run, {}, {}
            for task_id, task in state["query_tasks"].items():
                if task.get("kind") != TASK_KIND or (current_run and task.get("run_id") != current_run):
                    continue
                for target, scope in task.get("targets", {}).items():
                    histories.setdefault((target, scope, task["route"]), []).append((task_id, task))
        if route in {"amazon-bestsellers", "amazon-movers-and-shakers"}:
            from .marketplace_browser import build_marketplace_request
            department = departments.get(context["description"]["category"])
            if not department:
                raise SafetyError("SOURCE_LIST_DEPARTMENT_PENDING")
            if department == "kitchen/1380374031":
                context["supersedes_task_ids"] = [task_id for task_id, task in
                    histories.get((cid, context["description_hash"], route), [])
                    if task.get("request", {}).get("department") == "home" and not task.get("reconcile_required")]
            return build_marketplace_request(route, department=department)
        if route == "company-pages":
            search_checks = state["source_checks"]
            if shared_suppliers:
                from .supplier_search import current_supplier_search_checks
                search_checks = current_supplier_search_checks(state, cid, description_hash=context["description_hash"])
            checks = [(ref, row) for ref, row in search_checks.items()
                      if row.get("kind") == "supplier_route_search" and row.get("concept_id") == cid
                      and row.get("description_hash") == context["description_hash"]
                      and row.get("candidates")]
            urls = sorted({candidate["url"] for _, row in checks for candidate in row["candidates"]})
            if not urls:
                if shared_suppliers:
                    # An old raw-description task can finish while the type's
                    # accepted meaning later changes. Only current reviewed
                    # source applications can close this new meaning's search.
                    searched = {row["route"] for row in search_checks.values()
                        if row.get("state") in {"completed_no_results", "failed", "blocked", "insufficient"}
                        and state["source_checks"][state["batch_runs"][row["acquisition_id"]]["active_response_id"]]["dispatch_started"] is True}
                else:
                    searched = {task["route"] for task in state["query_tasks"].values()
                                if task.get("kind") == TASK_KIND and task.get("targets", {}).get(cid) == context["description_hash"]
                                and task.get("attempt_refs") and task.get("status") in
                                {"completed_no_results", "failed", "blocked", "insufficient"}
                                and not task.get("reconcile_required")}
                # A retired search is never sent, so it cannot hold this product's
                # company-page answer open; only the active searches must finish.
                from .supplier_route_acquisition import ACTIVE_SEARCH_ROUTES
                if set(ACTIVE_SEARCH_ROUTES) <= searched:
                    return {"not_applicable_reason": "Actual supplier checks did not identify a company page to read."}
                raise SafetyError("SOURCE_SUPPLIER_DESTINATIONS_PENDING")
            return {"candidate_urls": {cid: urls}, "candidate_check_ids": {cid: sorted(ref for ref, _ in checks)}, "country": "IN"}
        if cid not in profiles:
            profiles[cid] = resolve_query_profile(state, cid, catalogue=catalogue)
        profile = profiles[cid]
        if profile is None:
            raise SafetyError("PRODUCT_QUERY_PROFILE_REQUIRED")
        group = ROUTES[route]["source"]
        queries = [row for row in profile["queries"] if row["source"] == group and row["scope"] != "context"]
        queries.sort(key=lambda row: {"subtype": 0, "family": 1}[row["scope"]])
        if not queries:
            raise SafetyError("PRODUCT_QUERY_PROFILE_REQUIRED")
        candidates, seen = [], set()
        # Only reviewed retrieval meanings are alternatives. Broad context,
        # seller titles and random synonyms cannot become new product claims.
        extra = []
        if not profile.get("meaning_hash"):
            extra = [{"text": profile["core_type"], "scope": "family", "reason": "Reviewed broader product family."}]
            extra.extend({"text": text, "scope": "family", "reason": "Reviewed alternate product-type wording."}
                         for text in profile["aliases"])
        for row in [*queries, *extra]:
            identity = normalise(row["text"])
            if identity not in seen:
                candidates.append(row)
                seen.add(identity)
            if len(candidates) == 3:
                break
        def make_request(query):
            if shared_suppliers and group == "supplier":
                from .supplier_search import supplier_request_for_state
                return supplier_request_for_state(state, route, query)
            if route in {"amazon-search", "flipkart-search"}:
                from .marketplace_browser import build_marketplace_request
                return build_marketplace_request(route, term=query)
            if group == "quickcommerce":
                if location is None:
                    raise SafetyError("SOURCE_QUICKCOMMERCE_LOCATION_PENDING")
                from .quickcommerce import prepare_query
                prepared = prepare_query(route, query, location)
                return {"query": prepared["query"], "location": prepared["location"]}
            return {"query": query, "country": "IN"}
        previous_tasks, previous_receipts = [], []
        applicable = histories.get((cid, context["description_hash"], route), [])
        for index, candidate in enumerate(candidates):
            request = make_request(candidate["text"])
            matches = [(task_id, task) for task_id, task in applicable if task.get("request") == request]
            saved = max(matches, key=lambda row: (row[1].get("prepared_at", ""), row[0])) if matches else None
            if saved:
                task_id, task = saved
                target = task.get("target_results", {}).get(cid, {})
                reviewed_sparse = (target.get("description_hash") == context["description_hash"]
                                   and target.get("state") in {"completed_no_results", "insufficient"})
                if profile.get("meaning_hash") and group != "supplier":
                    reviewed_sparse = _functional_task_sparse(state, task_id, cid, profile["meaning_hash"],
                        as_of=context.get("as_of") or utc_now())
                can_broaden = (bool(task.get("attempt_refs")) and bool(task.get("receipt_refs"))
                               and not task.get("reconcile_required")
                               and task.get("status") in {"completed_no_results", "insufficient", "acquired"}
                               and reviewed_sparse)
                if can_broaden and index < len(candidates) - 1:
                    previous_tasks.append(task_id)
                    previous_receipts.extend(task["receipt_refs"])
                    continue
            context["query_plan"] = {"profile_hash": digest(profile), "term_index": index + 1,
                "term_count": len(candidates), "scope": candidate["scope"],
                "reason": ("Use a different reviewed type/family phrase after saved sparse or unrelated results."
                           if previous_tasks else "Use the primary reviewed product-type phrase."),
                "previous_task_ids": previous_tasks, "previous_receipt_refs": sorted(set(previous_receipts))}
            return request
        raise SafetyError("PRODUCT_QUERY_PROFILE_REQUIRED")
    # This built-in planner reads its frozen snapshot without changing it.
    # Only that explicit contract may share advisory ownership validation;
    # arbitrary custom builders retain their existing uncached behaviour.
    build._pure_source_planning = True
    return build


def _active_concepts(state, concept_ids=None):
    selected = (sorted(i for i, row in state["concepts"].items()
                       if row.get("status") not in ("merged", "retired"))
                if concept_ids is None else list(concept_ids))
    if len(selected) != len(set(selected)):
        raise SafetyError("SOURCE_CONCEPTS_INVALID")
    for concept_id in selected:
        identifier(concept_id)
        if concept_id not in state["concepts"]:
            raise SafetyError("KNOWN_CONCEPT_REQUIRED")
    return selected


def _scope(state, concept_id):
    return digest(state["concepts"][concept_id].get("description"))


def _product_context(state, concept_id):
    """An incomplete product is a local gap, not a whole-route exception."""
    concept = state["concepts"][concept_id]
    description = concept.get("description")
    if (not isinstance(description, dict) or not isinstance(description.get("label"), str)
            or not description["label"].strip()):
        raise SafetyError("SOURCE_PRODUCT_DESCRIPTION_INVALID")
    excluded = (concept.get("status") in {"excluded", "merged", "retired"}
                or current_brand(state, "concept", concept_id, description) == "established")
    return description, excluded


def _reason(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise SafetyError("SOURCE_REASON_INVALID")
    return value.strip()


def _request(value):
    """Public question only; credentials belong to existing private clients."""
    if not isinstance(value, dict) or not value:
        raise SafetyError("SOURCE_REQUEST_INVALID")
    def visit(item):
        if isinstance(item, dict):
            for name, child in item.items():
                if not isinstance(name, str) or name.casefold() in {
                    "api_key", "apikey", "token", "password", "client_secret",
                    "authorization", "cookie", "cookies", "credentials"}:
                    raise SafetyError("SOURCE_PRIVATE_REQUEST_REFUSED")
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif isinstance(item, str) and item.startswith(("http://", "https://")):
            from .public_pages import _public_url
            _public_url(item)
    visit(value)
    canonical(value)
    return deepcopy(value)


def _upsert(memory, table, record_id, value, old):
    if old != value:
        value = dict(value, revision=(old or {}).get("revision", 0) + 1)
        memory.put(table, record_id, value, previous=old,
                   event_id=key("source-progress", [table, record_id, digest(old), digest(value)]))


def _shared_acquisition(state, route, request, run_ids, cycle_ids):
    """An explicit follow-on scope may replay an exact existing acquisition.

    Scope names are supplied by the owning programme, not inferred from all
    historical data. This never copies another product's relevance verdict.
    The registered saved-only importer still verifies the original files.
    """
    for task_id, task in state["query_tasks"].items():
        if (task.get("kind") != TASK_KIND or task.get("run_id") not in run_ids
                or task.get("route") != route or task.get("request") != request
                or task.get("status") not in _FINAL or task.get("reconcile_required")
                or not task.get("acquisition_id") or not task.get("receipt_refs")
                or task.get("acquisition_id") not in task.get("attempt_refs", [])):
            continue
        check = state["source_checks"].get(task["receipt_refs"][-1], {})
        envelope = check.get("envelope", {})
        if (check.get("kind") == CHECK_KIND and check.get("request_hash") == digest(request)
                and check.get("envelope_hash") == digest(envelope)
                and envelope.get("acquisition_id") == task["acquisition_id"]
                and envelope.get("dispatch_started") is True):
            return {"acquisition_id": task["acquisition_id"], "reference": task_id}
    for cycle_id in cycle_ids:
        cycle = state["batch_runs"].get(cycle_id, {})
        if cycle.get("kind") != "catalogue-discovery-cycle-v1":
            raise SafetyError("SOURCE_REUSE_SCOPE_INVALID")
        for planned in cycle.get("planned_actions", []):
            for question in planned.get("questions", []):
                action = cycle.get("actions", {}).get(question.get("id"), {})
                if (question.get("route") == route and question.get("request") == request
                        and action.get("state") in {"acquired", "interpretation_pending", "empty"}
                        and action.get("dispatch_started") is True and action.get("acquisition_id")):
                    return {"acquisition_id": action["acquisition_id"],
                            "reference": {"cycle_id": cycle_id, "question_id": question["id"]}}
    return None


def prepare_source_tasks(memory, questions, *, run_id, as_of=None,
                         max_attempts=2, retry_delay_seconds=300,
                         reuse_run_ids=None, reuse_discovery_cycle_ids=None, inventory_repair=None):
    """Freeze a finite run; equal physical questions share one acquisition.

    Each question is {route, request, concept_ids}. Optional not-applicable
    decisions instead use {route, concept_ids, not_applicable_reason}. A run ID
    is the caller's existing week/envelope identity, not a new funding grant.
    Preparing again joins targets but never resets a completed or uncertain
    acquisition. New source results can add explicit new questions to the same
    finite run; this does not stop merely because one report is full.
    """
    identifier(run_id)
    reuse_run_ids = [] if reuse_run_ids is None else reuse_run_ids
    reuse_discovery_cycle_ids = [] if reuse_discovery_cycle_ids is None else reuse_discovery_cycle_ids
    if (not isinstance(reuse_run_ids, list) or not isinstance(reuse_discovery_cycle_ids, list)
            or any(identifier(value) == run_id for value in reuse_run_ids)
            or any(not identifier(value) for value in reuse_discovery_cycle_ids)):
        raise SafetyError("SOURCE_REUSE_SCOPE_INVALID")
    at = as_of or utc_now()
    timestamp(at)
    if (not isinstance(questions, list) or type(max_attempts) is not int or max_attempts < 1
            or type(retry_delay_seconds) is not int or retry_delay_seconds < 0):
        raise SafetyError("SOURCE_PLAN_INVALID")
    with Journal(memory.root / "all_source_dispatch").locked(timeout=5):
        state = memory.snapshot()
        run_key = key("source-run", run_id)
        old_run = state["batch_runs"].get(run_key)
        if inventory_repair is not None:
            _inventory_source_binding(state, inventory_repair, as_of=at)
        if old_run and (old_run.get("kind") != RUN_KIND
                        or old_run.get("max_attempts") != max_attempts
                        or old_run.get("retry_delay_seconds") != retry_delay_seconds):
            raise SafetyError("SOURCE_RUN_POLICY_CHANGED")
        task_ids = list((old_run or {}).get("task_ids", []))
        changes = []
        for question in questions:
            if not isinstance(question, dict) or question.get("route") not in ROUTES:
                raise SafetyError("SOURCE_QUESTION_INVALID")
            ids = _active_concepts(state, question.get("concept_ids", []))
            if not ids:
                raise SafetyError("SOURCE_TARGETS_REQUIRED")
            route = question["route"]
            if "not_applicable_reason" in question:
                reason = _reason(question["not_applicable_reason"])
                for concept_id in ids:
                    record = {"kind": "all_source_applicability_v1", "source": ROUTES[route]["source"],
                              "route": route, "at": at, "state": "not_applicable", "reason": reason,
                              "concept_id": concept_id, "description_hash": _scope(state, concept_id),
                              "reference": key("source-applicability", [run_id, route, concept_id, reason])}
                    record_id = record["reference"]
                    if record_id not in state["source_checks"]:
                        changes.append(change("source_checks", record_id, record))
                        state["source_checks"][record_id] = record
                continue
            request = _request(question.get("request"))
            if inventory_repair is not None:
                work, _ = _inventory_source_binding(state, inventory_repair, as_of=at)
                source = work["binding"]["source"]
                if route != source["route"] or request != source["request"] or ids != [work["binding"]["subject"]["concept_id"]]:
                    raise SafetyError("SOURCE_INVENTORY_REPAIR_INVALID")
            task_id = key("source-task", [run_id, route, request])
            old = state["query_tasks"].get(task_id)
            targets = deepcopy((old or {}).get("targets", {}))
            for concept_id in ids:
                targets[concept_id] = _scope(state, concept_id)
            value = deepcopy(old) if old else {
                "kind": TASK_KIND, "run_id": run_id, "route": route,
                "source": ROUTES[route]["source"], "request": request,
                "request_hash": digest(request), "prepared_at": at, "status": "prepared",
                "attempt_id": None, "collector_dispatched": False, "tries": 0,
                "generation": 0, "prelaunch_failures": 0,
                "attempt_refs": [], "receipt_refs": [], "next_retry_at": None,
                "reconcile_required": False, "target_results": {}, "reason": "Prepared; no source attempt yet.",
            }
            if not old:
                shared = _shared_acquisition(state, route, request, reuse_run_ids, reuse_discovery_cycle_ids)
                if shared:
                    value.update(acquisition_id=shared["acquisition_id"], status="dispatching",
                                 reconcile_required=True, dispatch_started=False,
                                 reused_acquisition_ref=shared["reference"],
                                 reason="Exact original acquisition is saved; replay it for this new target scope.")
            if old and (old.get("kind") != TASK_KIND or old.get("request") != request):
                raise SafetyError("SOURCE_TASK_SCOPE_CONFLICT")
            value["targets"] = targets
            if inventory_repair is not None:
                value["inventory_repair"] = deepcopy(inventory_repair)
            if "query_plan" in question:
                plan = _request(question["query_plan"])
                if (set(plan) != {"profile_hash", "term_index", "term_count", "scope", "reason", "previous_task_ids", "previous_receipt_refs"}
                        or type(plan["term_index"]) is not int or type(plan["term_count"]) is not int
                        or not 1 <= plan["term_index"] <= plan["term_count"] <= 3
                        or plan["scope"] not in {"subtype", "family"}
                        or not isinstance(plan["profile_hash"], str)
                        or not isinstance(plan["previous_task_ids"], list)
                        or not isinstance(plan["previous_receipt_refs"], list)):
                    raise SafetyError("SOURCE_QUERY_PLAN_INVALID")
                _reason(plan["reason"])
                for cid in ids:
                    prior = [state["query_tasks"].get(ref, {}) for ref in plan["previous_task_ids"]]
                    if (any(row.get("kind") != TASK_KIND or row.get("route") != route
                            or row.get("run_id") != run_id or row.get("targets", {}).get(cid) != _scope(state, cid)
                            or not row.get("attempt_refs") for row in prior)
                            or not set(plan["previous_receipt_refs"]) <= {ref for row in prior for ref in row.get("receipt_refs", [])}):
                        raise SafetyError("SOURCE_QUERY_PLAN_REFERENCE_INVALID")
                value["query_plans"] = {**value.get("query_plans", {}), **{cid: plan for cid in ids}}
            if old != value:
                value["revision"] = (old or {}).get("revision", 0) + 1
                changes.append(change("query_tasks", task_id, value, old))
                state["query_tasks"][task_id] = value
            if task_id not in task_ids:
                task_ids.append(task_id)
            # The public India `home` department was proved undefined. A new
            # verified decor-node question can supersede its future work, but
            # never rewrite the original request, receipt, cost or uncertainty.
            superseded = question.get("supersedes_task_ids", [])
            if not isinstance(superseded, list) or any(not isinstance(ref, str) for ref in superseded):
                raise SafetyError("SOURCE_SUPERSEDED_BINDING_INVALID")
            for prior_id in superseded:
                prior = state["query_tasks"].get(prior_id)
                if (not prior or prior.get("kind") != TASK_KIND or prior.get("run_id") != run_id
                        or prior.get("route") != route or route not in {"amazon-bestsellers", "amazon-movers-and-shakers"}
                        or prior.get("request", {}).get("department") != "home"
                        or request.get("department") != "kitchen/1380374031"
                        or prior.get("reconcile_required")
                        or any(prior.get("targets", {}).get(cid) != targets[cid]
                               or state["concepts"][cid]["description"].get("category") != "Home and Decor" for cid in ids)):
                    raise SafetyError("SOURCE_SUPERSEDED_BINDING_INVALID")
                updated = deepcopy(prior)
                updated["superseded_targets"] = {**updated.get("superseded_targets", {}), **{cid: {
                    "task_id": task_id, "request_hash": digest(request),
                    "reason": "The old Amazon India home department is undefined; use the verified Home and Decor node."}
                    for cid in ids}}
                if updated != prior:
                    updated["revision"] = prior.get("revision", 0) + 1
                    changes.append(change("query_tasks", prior_id, updated, prior)); state["query_tasks"][prior_id] = updated
        run = dict(old_run or {}, kind=RUN_KIND, run_id=run_id, plan_id=run_id,
                   actions={}, task_ids=task_ids, max_attempts=max_attempts,
                   retry_delay_seconds=retry_delay_seconds, prepared_at=(old_run or {}).get("prepared_at", at),
                   status="prepared" if not old_run else old_run.get("status", "prepared"))
        if inventory_repair is not None:
            if old_run and old_run.get("inventory_repair") != inventory_repair:
                raise SafetyError("SOURCE_INVENTORY_REPAIR_CONFLICT")
            run["inventory_repair"] = deepcopy(inventory_repair)
        if run != old_run:
            run["revision"] = (old_run or {}).get("revision", 0) + 1
            changes.append(change("batch_runs", run_key, run, old_run))
        if changes:
            def current(actual):
                if inventory_repair is not None:
                    _inventory_source_binding(actual, inventory_repair, as_of=at)
            memory.commit(key("source-plan", [run_key, digest(changes)]), changes,
                          **({"validate": current} if inventory_repair is not None else {}))
        return {"run_id": run_id, "task_ids": task_ids, "physical_questions": len(task_ids),
                "product_questions": sum(len(state["query_tasks"][task_id]["targets"]) for task_id in task_ids),
                "source_attempts_created": 0}


def validate_inventory_source_original(state, source, *, concept_id):
    """Verify the exact saved task, including a once-unallocated original.

    A prepared work item may precede its first acquisition ID. Subsequent
    recovery still needs that task's actual saved intent/dispatch/receipt or
    shared owner; None is not a wildcard for an arbitrary replacement read.
    """
    if not isinstance(source, dict):
        raise SafetyError("SOURCE_INVENTORY_ORIGINAL_CHANGED")
    tid = identifier(source.get("original_task_id"))
    if source.get("original_acquisition_id") is not None:
        identifier(source["original_acquisition_id"])
    old = state["query_tasks"].get(tid, {})
    if (old.get("kind") != TASK_KIND or old.get("route") != source.get("route")
            or old.get("request") != source.get("request") or old.get("request_hash") != digest(source.get("request"))
            or concept_id not in old.get("targets", {})
            or tid != key("source-task", [old.get("run_id"), old.get("route"), old.get("request")])):
        raise SafetyError("SOURCE_INVENTORY_ORIGINAL_CHANGED")
    pinned, actual = source.get("original_acquisition_id"), old.get("acquisition_id")
    if pinned == actual:
        return old
    if pinned is not None or actual is None or type(old.get("generation")) is not int or old["generation"] < 1:
        raise SafetyError("SOURCE_INVENTORY_ORIGINAL_CHANGED")
    proved = False
    for ref in old.get("receipt_refs", []):
        receipt = state["source_checks"].get(ref, {}); envelope = receipt.get("envelope", {})
        if (receipt.get("kind") == CHECK_KIND and receipt.get("task_id") == tid
                and receipt.get("route") == old["route"] and receipt.get("request_hash") == old["request_hash"]
                and receipt.get("envelope_hash") == digest(envelope) and envelope.get("acquisition_id") == actual):
            proved = True
    dispatch = state["source_checks"].get(key("source-dispatch", [tid, actual]), {})
    if (dispatch.get("kind") == DISPATCH_KIND and dispatch.get("task_id") == tid
            and dispatch.get("acquisition_id") == actual and dispatch.get("request_hash") == old["request_hash"]
            and dispatch.get("route") == old["route"]):
        proved = True
    physical = old.get("source_physical_request", old.get("marketplace_physical_request"))
    if old.get("acquisition_owner_ref") == actual and physical:
        from .source_acquisitions import _record
        owner = _record(state, actual)
        if (owner["binding"]["route"] == old["route"]
                and owner["binding"]["request"] == physical):
            proved = True
    if (actual == key("source-acquisition", [tid, old["generation"]])
            and old.get("reconcile_required") is True and old.get("dispatched_at") is not None
            and old.get("status") in {"dispatching", "failed", "blocked"}):
        timestamp(old["dispatched_at"])
        proved = True  # The ordinary controller's saved original caller intent.
    if not proved:
        raise SafetyError("SOURCE_INVENTORY_ORIGINAL_CHANGED")
    return old


def _inventory_source_binding(state, marker, *, as_of):
    from .inventory_intake import validate_inventory_work
    from .functional_types import effective_functional_definition
    if not isinstance(marker, dict) or set(marker) != {"work_id", "binding_hash"}:
        raise SafetyError("SOURCE_INVENTORY_REPAIR_INVALID")
    work = validate_inventory_work(state["batch_runs"].get(marker["work_id"]))
    binding = work["binding"]; subject = binding["subject"]; source = binding.get("source")
    cid = subject["concept_id"]
    if (marker["binding_hash"] != work["binding_hash"] or binding["purpose"] != "source"
            or effective_functional_definition(state, cid)["meaning_hash"] != subject["definition_hash"]
            or not isinstance(source, dict) or set(source) != {"route", "request", "original_task_id",
                "original_acquisition_id", "expired_support_refs", "refresh_due_at", "refresh_reason"}
            or source["route"] not in ROUTES or ROUTES[source["route"]]["source"] == "supplier"):
        raise SafetyError("SOURCE_INVENTORY_REPAIR_INVALID")
    old = validate_inventory_source_original(state, source, concept_id=cid)
    _request(source["request"])
    refs = source["expired_support_refs"]
    if not isinstance(refs, list):
        raise SafetyError("SOURCE_INVENTORY_SUPPORT_INVALID")
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {"table", "id", "hash"}
                or ref["table"] not in {"observations", "captures", "trends_evidence", "source_checks"}
                or ref["id"] not in state[ref["table"]] or digest(state[ref["table"]][ref["id"]]) != ref["hash"]):
            raise SafetyError("SOURCE_INVENTORY_SUPPORT_CHANGED")
    due, reason = source["refresh_due_at"], source["refresh_reason"]
    if (due is None) != (reason is None):
        raise SafetyError("SOURCE_INVENTORY_REFRESH_INVALID")
    if due is not None:
        timestamp(due); _reason(reason)
        if not refs or old["status"] not in _FINAL or old.get("reconcile_required") or not old.get("receipt_refs"):
            raise SafetyError("SOURCE_INVENTORY_REFRESH_INVALID")
        original = state["source_checks"].get(old["receipt_refs"][-1], {})
        envelope = original.get("envelope", {})
        if (original.get("kind") != CHECK_KIND or original.get("envelope_hash") != digest(envelope)
                or original.get("task_id") != source["original_task_id"] or original.get("route") != source["route"]
                or original.get("request_hash") != digest(source["request"])
                or envelope.get("acquisition_id") != source["original_acquisition_id"]
                or envelope.get("dispatch_started") is not True
                or any(not isinstance(envelope.get(field), list) for field in ("capture_ids", "observation_ids", "evidence_refs"))
                or envelope.get("observed_at") is None
                or timestamp(due) < timestamp(envelope["observed_at"]) + timedelta(days=7)):
            raise SafetyError("SOURCE_INVENTORY_REFRESH_INVALID")
        # Existing records elsewhere in memory are not this original's proof.
        # Scoped support may accompany its indivisible original citations, but
        # never substitute a different capture/company/source question.
        allowed = {("captures", ref) for ref in envelope["capture_ids"]}
        allowed.update(("observations", ref) for ref in envelope["observation_ids"])
        allowed.update((table, ref) for ref in envelope["evidence_refs"]
                       for table in ("observations", "trends_evidence") if ref in state[table])
        from .research_scope import validated_scoped_evidence
        scoped = {row["id"]: row for row in validated_scoped_evidence(state, cid, as_of=as_of)}
        originals = set()
        for ref in refs:
            pair = ref["table"], ref["id"]
            if pair in allowed:
                originals.add(pair)
            elif pair == ("source_checks", old["receipt_refs"][-1]):
                continue
            elif ref["table"] == "source_checks" and ref["id"] in scoped:
                row = scoped[ref["id"]]
                cited = {(value["table"], value["id"]) for value in row["references"]
                         if value["table"] in {"observations", "trends_evidence", "captures"}}
                if row["route"] != source["route"] or not cited or not cited <= allowed:
                    raise SafetyError("SOURCE_INVENTORY_SUPPORT_ORIGINAL_MISMATCH")
                originals.update(cited)
            else:
                raise SafetyError("SOURCE_INVENTORY_SUPPORT_ORIGINAL_MISMATCH")
        if not any(table in {"observations", "trends_evidence"} for table, _ in originals):
            raise SafetyError("SOURCE_INVENTORY_SUPPORT_ORIGINAL_MISMATCH")
        if any(state[table][ref].get("acquisition_id") != source["original_acquisition_id"]
               for table, ref in originals if table in {"observations", "captures"}):
            raise SafetyError("SOURCE_INVENTORY_SUPPORT_ORIGINAL_MISMATCH")
    return work, old


def prepare_inventory_source_repair(memory, obligation, *, as_of):
    """Prepare one checked due question, or service its unfinished original.

    Never launches. An existing uncertain task retains its run/acquisition;
    callers pass the returned task_ids subset to the ordinary dispatcher.
    """
    from .inventory_intake import validate_inventory_work
    timestamp(as_of)
    work = validate_inventory_work(obligation)
    if work["binding"].get("source") is None:
        return {"state": "waiting_input", "run_id": None, "task_ids": [],
                "reason": "SOURCE_INVENTORY_REQUEST_REQUIRED", "source_attempts_created": 0}
    marker = {"work_id": work["id"], "binding_hash": work["binding_hash"]}
    state = memory.snapshot(); saved, original = _inventory_source_binding(state, marker, as_of=as_of)
    if saved["binding"] != work["binding"]:
        raise SafetyError("SOURCE_INVENTORY_REPAIR_INVALID")
    source = work["binding"]["source"]; cid = work["binding"]["subject"]["concept_id"]
    if original["status"] not in _FINAL or original.get("reconcile_required"):
        if original["targets"].get(cid) != _scope(state, cid):
            raise SafetyError("SOURCE_INVENTORY_ORIGINAL_TARGET_CHANGED")
        return {"state": "prepared", "run_id": original["run_id"], "task_ids": [source["original_task_id"]],
                "reused_original": True, "source_attempts_created": 0}
    if source["refresh_due_at"] is None:
        return {"state": "prepared", "run_id": original["run_id"], "task_ids": [source["original_task_id"]],
                "reused_original": True, "source_already_collected": True, "source_attempts_created": 0}
    if timestamp(source["refresh_due_at"]) > timestamp(as_of):
        return {"state": "waiting", "run_id": None, "task_ids": [], "next_retry_at": source["refresh_due_at"],
                "reason": "SOURCE_INVENTORY_REFRESH_NOT_DUE", "source_attempts_created": 0}
    # Any equivalent uncertain original remains original-only, even when a
    # different previous completed read provided this report's stale support.
    pending = [(tid, task) for tid, task in state["query_tasks"].items() if task.get("kind") == TASK_KIND
               and task.get("route") == source["route"] and task.get("request") == source["request"]
               and (task.get("reconcile_required") or task.get("status") == "dispatching")]
    if pending:
        origins = {task.get("acquisition_id") for _, task in pending}
        if len(origins) != 1:
            raise SafetyError("SOURCE_INVENTORY_ORIGINS_NEED_REVIEW")
        tid, task = pending[0]
        if task.get("targets", {}).get(cid) != _scope(state, cid):
            raise SafetyError("SOURCE_INVENTORY_ORIGINAL_TARGET_CHANGED")
        return {"state": "prepared", "run_id": task["run_id"], "task_ids": [tid],
                "reused_original": True, "source_attempts_created": 0}
    run_id = key("inventory-source-repair", marker)
    result = prepare_source_tasks(memory, [{"route": source["route"], "request": source["request"], "concept_ids": [cid]}],
                                  run_id=run_id, as_of=as_of, inventory_repair=marker)
    return dict(result, state="prepared", reused_original=False)


def _inventory_refresh_context(state, task, *, as_of):
    marker = task.get("inventory_repair")
    if marker is None:
        return {}
    work, _ = _inventory_source_binding(state, marker, as_of=as_of)
    source = work["binding"]["source"]
    if task["route"] != source["route"] or task["request"] != source["request"]:
        raise SafetyError("SOURCE_INVENTORY_REPAIR_INVALID")
    if timestamp(source["refresh_due_at"]) > timestamp(as_of):
        raise SafetyError("SOURCE_INVENTORY_REFRESH_NOT_DUE")
    return {"refresh_due_at": source["refresh_due_at"], "refresh_reason": source["refresh_reason"]}


def prepare_all_source_tasks(memory, *, run_id, request_builder, concept_ids=None,
                             as_of=None, max_attempts=2, retry_delay_seconds=300,
                             reuse_run_ids=None, reuse_discovery_cycle_ids=None):
    """Visit every active product/planned route; a builder supplies actual public scope.

    Builder(context) returns a request or {not_applicable_reason: reason}. It
    may raise a fixed dependency code (for example, query profile pending).
    Such gaps are recorded, not fabricated attempts or global source failures.
    The builder never collects; use already-reviewed family wording and include
    actual list department/location/URLs in each request's collection identity.
    The built-in pure planner shares validated owners only during this frozen
    walk. Fresh preparation, saving and execution remain outside that scope.
    """
    if not callable(request_builder):
        raise SafetyError("SOURCE_REQUEST_BUILDER_REQUIRED")
    at = as_of or utc_now()
    timestamp(at)
    # Advisory milestones name the last local step entered, not a completed
    # website search. They leave the enclosing phase clock/deadline intact.
    # Never log a raw description, question, response or per-owner/file read.
    def progress(step, **values):
        record_event("source_preparation_progress", stage="acquire", phase_state=step,
                     useful_result=False, **values)
    progress("read_saved_products")
    state = memory.snapshot()
    ids = _active_concepts(state, concept_ids)
    questions, pending = [], []
    total = len(ids) * len(PLANNED_ROUTES)
    from .source_acquisitions import source_readiness_owner_scope
    planning_scope = (source_readiness_owner_scope(state)
        if getattr(request_builder, "_pure_source_planning", False) is True else nullcontext())
    with planning_scope:
        for product_index, concept_id in enumerate(ids):
            progress("product_context", concept_id=concept_id,
                     scanned=product_index * len(PLANNED_ROUTES), remaining=total - product_index * len(PLANNED_ROUTES))
            try:
                description, excluded = _product_context(state, concept_id)
            except SafetyError as error:
                if str(error) in _INTEGRITY_ERRORS:
                    raise
                pending.extend({"concept_id": concept_id, "description_hash": _scope(state, concept_id),
                    "route": route, "state": "not_attempted", "reason": str(error)} for route in PLANNED_ROUTES)
                continue
            for route_index, route in enumerate(PLANNED_ROUTES):
                scanned = product_index * len(PLANNED_ROUTES) + route_index
                progress("question", concept_id=concept_id, route=route,
                         scanned=scanned, remaining=total - scanned,
                         counts={"questions_total": total, "questions_prepared": len(questions),
                                 "questions_pending": len(pending)})
                if excluded:
                    questions.append({"route": route, "concept_ids": [concept_id],
                                      "not_applicable_reason": "Known established brand or excluded working identity."})
                    continue
                context = {"route": route, **ROUTES[route], "concept_id": concept_id,
                           "description": deepcopy(description),
                           "description_hash": _scope(state, concept_id), "state": state, "run_id": run_id,
                           "as_of": at}
                try:
                    request = request_builder(context)
                    if not isinstance(request, dict) or not request:
                        raise SafetyError("SOURCE_QUESTION_INPUT_PENDING")
                    if set(request) == {"not_applicable_reason"}:
                        questions.append({"route": route, "concept_ids": [concept_id], **request})
                    else:
                        question = {"route": route, "concept_ids": [concept_id], "request": request}
                        if "query_plan" in context:
                            question["query_plan"] = context["query_plan"]
                        if context.get("supersedes_task_ids"):
                            question["supersedes_task_ids"] = context["supersedes_task_ids"]
                        questions.append(question)
                except SafetyError as error:
                    if str(error) in _INTEGRITY_ERRORS:
                        raise
                    pending.append({"concept_id": concept_id, "description_hash": _scope(state, concept_id),
                                    "route": route, "state": "not_attempted", "reason": str(error)})
    counts = {"questions_total": total, "questions_prepared": len(questions),
              "questions_pending": len(pending)}
    # Keep diagnostic writes outside the task preparer's journal lock. Its
    # fresh validation, actual saves and failure semantics remain unchanged.
    progress("prepare_tasks", scanned=total, remaining=0, counts=counts)
    prepared = prepare_source_tasks(memory, questions, run_id=run_id, as_of=at,
                                    max_attempts=max_attempts, retry_delay_seconds=retry_delay_seconds,
                                    reuse_run_ids=reuse_run_ids, reuse_discovery_cycle_ids=reuse_discovery_cycle_ids)
    run_key = key("source-run", run_id)
    progress("read_prepared_run", scanned=total, remaining=0, counts=counts)
    old = memory.snapshot()["batch_runs"][run_key]
    progress("save_pending_questions", scanned=total, remaining=0, counts=counts)
    _upsert(memory, "batch_runs", run_key, dict(old, pending_preparation=pending), old)
    progress("complete", scanned=total, remaining=0, counts=counts)
    return dict(prepared, products=len(ids), routes=len(PLANNED_ROUTES), pending_preparation=pending)


def source_run_progress(memory, run_id, *, state=None):
    """Cheap verified work status, without source reads or a full evidence join.

    The caller can reuse its existing verified snapshot. This is deliberately
    task progress, not a claim that every captured row has been interpreted or
    that every route in the product grid is ready for a client report.
    """
    identifier(run_id)
    state = memory.snapshot() if state is None else state
    run = state["batch_runs"].get(key("source-run", run_id))
    if not run or run.get("kind") != RUN_KIND:
        raise SafetyError("SOURCE_RUN_REQUIRED")
    tasks = [state["query_tasks"][task_id] for task_id in run["task_ids"]]
    targets = {(cid, task["route"]) for task in tasks for cid in task["targets"]}
    latest = [row for task in tasks for row in task.get("target_results", {}).values()]
    attempts = {ref for task in tasks for ref in task.get("attempt_refs", [])}
    return {"schema_version": 1, "run_id": run_id, "status": run["status"],
            "physical_questions": len(tasks), "planned_product_route_questions": len(targets),
            "task_states": dict(Counter(task["status"] for task in tasks)),
            "captured_target_states": dict(Counter(row["state"] for row in latest)),
            "controller_acquisition_attempts": len(attempts),
            "reused_acquisitions": len({task["acquisition_id"] for task in tasks if task.get("reused_acquisition_ref")}),
            "saved_receipts": sum(len(task["receipt_refs"]) for task in tasks),
            "pending_preparation": len(run.get("pending_preparation", [])),
            "pending_preparation_reasons": dict(Counter(row["reason"] for row in run.get("pending_preparation", []))),
            "route_pauses": _effective_route_pauses(memory, state, key("source-run", run_id), run),
            "last_pass_at": run.get("last_pass_at"), "client_report": False,
            "note": "Source work status only; captured results still need their own accepted interpretation."}


def continue_source_run(memory, run_id, *, request_builder, handlers=None,
                        concept_ids=None, action_limit=None, timeout_seconds=45,
                        bounded_call=None, on_progress=None, on_pass=None,
                        max_attempts=2, retry_delay_seconds=300,
                        reuse_run_ids=None, reuse_discovery_cycle_ids=None, shared_marketplace=False):
    """Finish presently actionable work, including newly found company pages.

    The original product set is frozen for this finite programme. After a pass,
    newly discovered supplier URLs may prepare real page reads; all already
    saved acquisitions stay reused. A fixed point with only waiting, uncertain
    or interpretation work returns those exact remaining items instead of
    sleeping indefinitely, retrying an unchanged failure, or publishing fiction.
    The owner driver can resume the same run after its next actual recovery.
    """
    if action_limit is not None and (type(action_limit) is not int or action_limit < 1):
        raise SafetyError("SOURCE_EXECUTION_LIMIT_INVALID")
    if on_pass is not None and not callable(on_pass):
        raise SafetyError("SOURCE_PROGRESS_CALLBACK_INVALID")
    state = memory.snapshot()
    ids = _active_concepts(state, concept_ids)
    turn = active_work_turn(memory, state=state)
    existing = (state["batch_runs"].get(key("source-run", run_id))
                if isinstance(run_id, str) else None)
    saved_only = bool(turn is not None and (turn["state"] != "open"
        or turn["counters"]["source_callbacks"] >= turn["policy"]["source_callbacks"])
        and isinstance(existing, dict) and existing.get("kind") == RUN_KIND)
    preparation_wait = None
    if saved_only:
        # Skipping fresh planning must not skip its caller/policy checks. The
        # existing dispatcher still validates current targets and original
        # receipts before applying them, and owns the finite saved-only sweep.
        if not callable(request_builder):
            raise SafetyError("SOURCE_REQUEST_BUILDER_REQUIRED")
        identifier(run_id)
        run_ids = [] if reuse_run_ids is None else reuse_run_ids
        cycle_ids = [] if reuse_discovery_cycle_ids is None else reuse_discovery_cycle_ids
        if (not isinstance(run_ids, list) or not isinstance(cycle_ids, list)
                or any(identifier(value) == run_id for value in run_ids)
                or any(not identifier(value) for value in cycle_ids)):
            raise SafetyError("SOURCE_REUSE_SCOPE_INVALID")
        if (type(max_attempts) is not int or max_attempts < 1
                or type(retry_delay_seconds) is not int or retry_delay_seconds < 0):
            raise SafetyError("SOURCE_PLAN_INVALID")
        if (existing.get("max_attempts") != max_attempts
                or existing.get("retry_delay_seconds") != retry_delay_seconds):
            raise SafetyError("SOURCE_RUN_POLICY_CHANGED")
        preparation_wait = {"state": "not_attempted",
            "reason": "WORK_TURN_CLOSED" if turn["state"] != "open" else "WORK_TURN_SOURCE_LIMIT",
            "next_action": "continue_in_next_work_turn"}
    def prepare_current():
        started = time.monotonic()
        record_event("phase_started", stage="acquire", phase="source_preparation",
                     deadline_seconds=None, useful_result=False)
        try:
            return prepare_all_source_tasks(memory, run_id=run_id, request_builder=request_builder,
                concept_ids=ids, max_attempts=max_attempts, retry_delay_seconds=retry_delay_seconds,
                reuse_run_ids=reuse_run_ids, reuse_discovery_cycle_ids=reuse_discovery_cycle_ids)
        finally:
            record_event("phase_finished", stage="acquire", phase="source_preparation",
                next_phase="acquire", timings={"source_preparation": time.monotonic() - started},
                deadline_seconds=None, useful_result=False)
    passes, total_calls = [], 0
    inspection_pending, inspection_scanned, inspection_remaining = False, 0, 0
    previous_signature = None
    while True:
        if saved_only:
            prepared = {"pending_preparation": existing.get("pending_preparation", [])}
        else:
            prepared = prepare_current()
            state = memory.snapshot()
        run = state["batch_runs"][key("source-run", run_id)]
        signature = digest([{field: task.get(field) for field in
                             ("route", "request_hash", "targets", "status", "generation", "tries",
                              "prelaunch_failures", "receipt_refs", "next_retry_at", "reconcile_required", "reason")}
                            for task in [state["query_tasks"][task_id] for task_id in run["task_ids"]]])
        if signature == previous_signature or (action_limit is not None and total_calls >= action_limit):
            # This preparation may have appended a company page just before
            # the overall callback limit was reached. Retain that real suffix
            # even though this wrapper must not start another execution pass.
            continuation = _effective_source_inspection(memory, key("source-run", run_id), run, state=state)
            if continuation is not None:
                inspection_remaining = continuation["remaining"]
                inspection_pending |= inspection_remaining > 0
            break
        previous_signature = signature
        remaining = None if action_limit is None else action_limit - total_calls
        if saved_only:
            recovery_started = time.monotonic()
            record_event("phase_started", stage="acquire", phase="source_saved_recovery",
                         deadline_seconds=None, useful_result=False)
        try:
            result = execute_source_run(memory, run_id, handlers=handlers, action_limit=remaining,
                                        timeout_seconds=timeout_seconds, bounded_call=bounded_call,
                                        on_progress=on_progress, shared_marketplace=shared_marketplace)
        finally:
            if saved_only:
                record_event("phase_finished", stage="acquire", phase="source_saved_recovery",
                    next_phase="acquire", timings={"source_saved_recovery": time.monotonic() - recovery_started},
                    deadline_seconds=None, useful_result=False)
        total_calls += result["handler_calls"]
        inspection_scanned += result.get("inspection_scanned", 0)
        inspection_pending = result.get("inspection_pending", False)
        inspection_remaining = result.get("inspection_remaining", 0)
        from .supplier_route_acquisition import SEARCH_ROUTES
        if saved_only and any(row.get("receipt_id") and row.get("route") in SEARCH_ROUTES
                for row in result["results"]):
            # A genuinely imported supplier reply can create a company-page
            # dependency. Materialise that next-turn work using the existing
            # planner; mere reused/waiting rows cannot trigger another build.
            prepared = prepare_current()
        # Preparation above may append a real company-page dependency after a
        # completed frozen cohort. Keep that suffix visible without reopening
        # unchanged old questions or defeating the finite pass's yield.
        latest_state = memory.snapshot()
        latest_run = latest_state["batch_runs"][key("source-run", run_id)]
        continuation = _effective_source_inspection(memory, key("source-run", run_id), latest_run,
                                                    state=latest_state)
        if continuation is not None:
            inspection_remaining = continuation["remaining"]
            inspection_pending |= inspection_remaining > 0
        passes.append({"handler_calls": result["handler_calls"], "counts": result["counts"],
                       "pending_preparation": len(prepared["pending_preparation"]),
                       "inspection_pending": inspection_pending,
                       "inspection_scanned": result.get("inspection_scanned", 0)})
        if on_pass:
            try:
                on_pass(source_run_progress(memory, run_id))
            except Exception:
                # Console observers are not source acquisition authorities.
                pass
        # Yield an unfinished traversal, not a newly created original-recovery
        # obligation alone. The latter may receive its one fixed-point recovery
        # in this same wrapper, under the unchanged callback/action admission.
        # An unchanged unanswered recovery then settles instead of looping.
        traversal_pending = inspection_pending and (continuation is None
            or continuation["ordinary_remaining"] > 0 or continuation["priority_remaining"] > 0)
        if traversal_pending or not result["handler_calls"]:
            break
    return {"run_id": run_id, "passes": passes, "handler_calls": total_calls,
            "progress": source_run_progress(memory, run_id), "products": len(ids),
            "routes": len(PLANNED_ROUTES), "product_route_questions": len(ids) * len(PLANNED_ROUTES),
            "inspection_pending": inspection_pending, "inspection_scanned": inspection_scanned,
            "inspection_remaining": inspection_remaining,
            "report_filled_does_not_stop_work": True, "client_publication_attempted": False,
            **({"preparation_wait": preparation_wait} if preparation_wait is not None else {})}


def _bounded_call(callback, context, timeout):
    """Interrupt an owned finite handler; never leave a detached late writer.

    Installed transports also own their HTTP/browser deadlines and cleanup.
    POSIX main-thread dispatch supplies an outer deadline for a broken handler.
    Environments without that mechanism must use their bounded adapter runner,
    rather than pretending an uninterruptible arbitrary callable is safe.
    """
    with bounded_source_deadline(timeout):
        return callback(context)


def _envelope(receipt, context):
    if not isinstance(receipt, dict):
        raise SafetyError("SOURCE_RECEIPT_INVALID")
    observed = receipt.get("observed_at", receipt.get("checked_at"))
    timestamp(observed)
    if (receipt.get("route") != context["route"] or receipt.get("acquisition_id") != context["acquisition_id"]
            or "dispatch_started" not in receipt
            or (type(receipt["dispatch_started"]) is not bool and receipt["dispatch_started"] is not None)):
        raise SafetyError("SOURCE_RECEIPT_SCOPE_INVALID")
    reference = receipt.get("reference")
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 4096:
        raise SafetyError("SOURCE_RECEIPT_REFERENCE_REQUIRED")
    raw_state = str(receipt.get("state", "")).lower()
    statuses = {"usable": "acquired", "partial": "acquired", "interpretation_pending": "acquired", "empty": "completed_no_results",
                "completed_no_results": "completed_no_results", "insufficient": "insufficient",
                "blocked": "blocked", "failed": "failed", "unavailable": "failed"}
    if raw_state not in statuses:
        raise SafetyError("SOURCE_RECEIPT_STATE_INVALID")
    status = statuses[raw_state]
    uncertain = receipt["dispatch_started"] is None
    if uncertain:
        status = "blocked"
    if status == "insufficient" and not receipt["dispatch_started"]:
        status = "waiting_input"
    capture_ids = receipt.get("capture_ids", [])
    observation_ids = receipt.get("observation_ids", [])
    evidence_refs = receipt.get("evidence_refs", [])
    for refs in (capture_ids, observation_ids, evidence_refs):
        if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs) or len(set(refs)) != len(refs):
            raise SafetyError("SOURCE_RECEIPT_EVIDENCE_INVALID")
    default = ("blocked" if uncertain else "completed_no_results" if status == "completed_no_results" else
               "interpretation_pending" if status == "acquired" else raw_state)
    if receipt["dispatch_started"] is False and default in {"unavailable", "failed", "blocked"}:
        default = "not_attempted"
    target_results = {}
    for row in receipt.get("target_results", []):
        if (not isinstance(row, dict) or row.get("concept_id") not in context["description_hashes"]
                or row.get("concept_id") in target_results
                or row.get("description_hash") != context["description_hashes"][row["concept_id"]]
                or row.get("state") not in STATES - {"not_connected", "not_applicable", "in_progress"}
                or not isinstance(row.get("evidence_refs", []), list)):
            raise SafetyError("SOURCE_TARGET_RECEIPT_INVALID")
        refs = row.get("evidence_refs", [])
        if any(not isinstance(ref, str) or not ref for ref in refs):
            raise SafetyError("SOURCE_TARGET_EVIDENCE_INVALID")
        if row["state"] == "completed_relevant" and (not refs or status != "acquired"):
            raise SafetyError("SOURCE_TARGET_RELEVANCE_REQUIRED")
        if row["state"] in {"completed_relevant", "completed_no_results"} and receipt["dispatch_started"] is not True:
            raise SafetyError("SOURCE_COMPLETION_DISPATCH_REQUIRED")
        target_results[row["concept_id"]] = {
            "description_hash": row["description_hash"], "state": "blocked" if uncertain else row["state"],
            "evidence_refs": refs, "reason": _reason(row.get("reason", receipt.get("reason"))),
        }
    if status in _FINAL and receipt["dispatch_started"] is not True:
        raise SafetyError("SOURCE_COMPLETION_DISPATCH_REQUIRED")
    for concept_id, description_hash in context["description_hashes"].items():
        target_results.setdefault(concept_id, {"description_hash": description_hash,
                                  "state": default, "evidence_refs": [],
                                  "reason": _reason(receipt.get("reason", raw_state))})
    value = {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
            "observed_at": observed, "state": raw_state, "status": status,
            "dispatch_started": receipt["dispatch_started"], "dispatch_uncertain": uncertain, "reference": reference,
            "capture_ids": capture_ids, "observation_ids": observation_ids, "evidence_refs": evidence_refs,
            "target_results": target_results, "reason": _reason(receipt.get("reason", raw_state)),
            "retryable": receipt.get("retryable") is True,
            "failure_scope": "route" if receipt.get("failure_scope") == "route" else "question",
            "reused": receipt.get("reused") is True}
    if receipt.get("access_wait") is not None:
        value["access_wait"] = deepcopy(receipt["access_wait"])
    if receipt.get("public_access_wait") is not None:
        value["public_access_wait"] = deepcopy(receipt["public_access_wait"])
    if receipt.get("supplier_lead_capture_ref") is not None:
        value["supplier_lead_capture_ref"] = deepcopy(receipt["supplier_lead_capture_ref"])
        value["supplier_results_hash"] = receipt.get("supplier_results_hash")
        value["supplier_original_pending"] = receipt.get("supplier_original_pending")
    return value


def _persist_receipt(memory, task_id, context, receipt):
    value = _envelope(receipt, context)
    state = memory.snapshot()
    old = state["query_tasks"][task_id]
    if old.get("acquisition_id") != context["acquisition_id"]:
        raise SafetyError("SOURCE_TASK_EXECUTION_CHANGED")
    supplier_response = None
    if value.get("supplier_lead_capture_ref") is not None:
        from .source_acquisitions import _record, _response
        from .supplier_search import validate_supplier_lead_receipt
        owner = _record(state, context["acquisition_id"]); response = _response(state, owner)
        supplier_response = validate_supplier_lead_receipt(memory, owner["binding"]["request"], context["acquisition_id"], receipt)
        ref = value["supplier_lead_capture_ref"]
        if (not isinstance(ref, dict) or set(ref) != {"id", "hash"}
                or not response or supplier_response != response or owner["active_response_id"] != ref["id"] or digest(response) != ref["hash"]
                or response.get("bridge_contract") != "verified-supplier-search-response-v1"
                or owner["binding"]["route"] != context["route"] or response["source_state"] != value["state"]
                or type(value.get("supplier_original_pending")) is not bool
                or value["supplier_original_pending"] != (not source_response_closed(response))):
            raise SafetyError("SOURCE_SUPPLIER_ORIGINAL_PROOF_INVALID")
    if old.get("dispatch_started") and value["dispatch_started"] is False:
        raise SafetyError("SOURCE_RECEIPT_DISPATCH_CONFLICT")
    # Importers own exact capture/file verification. Check canonical evidence
    # IDs here so an adapter cannot claim an import it did not save.
    if (not set(value["capture_ids"]) <= set(state["captures"])
            or not set(value["observation_ids"]) <= set(state["observations"])):
        raise SafetyError("SOURCE_RECEIPT_IMPORT_REQUIRED")
    expected_source = ("meta" if context["route"] == "meta-ads" else
                       "amazon" if context["route"].startswith("amazon-") else
                       "flipkart" if context["route"] == "flipkart-search" else context["route"])
    for ref in value["capture_ids"]:
        original = state["captures"][ref]
        if original.get("source") != expected_source or original.get("acquisition_id") != context["acquisition_id"]:
            raise SafetyError("SOURCE_RECEIPT_CAPTURE_SCOPE_INVALID")
    for ref in value["observation_ids"]:
        original = state["observations"][ref]
        if (original.get("source") != expected_source or original.get("acquisition_id") != context["acquisition_id"]
                or original.get("capture_id") not in value["capture_ids"]):
            raise SafetyError("SOURCE_RECEIPT_OBSERVATION_SCOPE_INVALID")
    bindings = {}
    refs = set(value["evidence_refs"])
    for target in value["target_results"].values():
        refs.update(target["evidence_refs"])
    for ref in refs:
        if ref in state["captures"]:
            bindings[ref] = digest(state["captures"][ref])
        elif ref in state["observations"]:
            bindings[ref] = digest(state["observations"][ref])
        elif ref in state["trends_evidence"]:
            evidence = state["trends_evidence"][ref]
            if (context["route"] != "google-trends" or evidence.get("concept_id") not in context["concept_ids"]
                    or evidence.get("description_hash") != context["description_hashes"][evidence["concept_id"]]):
                raise SafetyError("SOURCE_TARGET_EVIDENCE_ROUTE_MISMATCH")
            bindings[ref] = digest(evidence)
        elif ref in state["source_checks"]:
            original = state["source_checks"][ref]
            if original.get("route") != context["route"]:
                raise SafetyError("SOURCE_TARGET_EVIDENCE_ROUTE_MISMATCH")
            bindings[ref] = digest(original)
        else:
            path = safe_path(ref)
            if not path.is_relative_to(memory.root) or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                raise SafetyError("SOURCE_TARGET_EVIDENCE_INVALID")
            bindings[ref] = sha256(path.read_bytes()).hexdigest()
    value["evidence_refs"] = sorted(refs)
    # New verified stages may resolve an uncertain original acquisition. Keep
    # its first receipt unchanged and add the new stage, rather than rewriting
    # that uncertainty or launching a replacement request.
    check_id = key("source-receipt", [task_id, context["acquisition_id"], digest(value)])
    check = {"kind": CHECK_KIND, "source": ROUTES[context["route"]]["source"],
             "route": context["route"], "at": value["observed_at"], "state": value["state"],
             "reason": value["reason"], "reference": value["reference"], "task_id": task_id,
             "request_hash": old["request_hash"], "envelope": value, "envelope_hash": digest(value),
             "evidence_bindings": bindings}
    existing = state["source_checks"].get(check_id)
    if existing is not None and existing != check:
        raise SafetyError("SOURCE_RECEIPT_CONFLICT")
    at = timestamp(value["observed_at"])
    retry = (at + timedelta(seconds=context["retry_delay_seconds"])).isoformat(timespec="seconds")
    if value.get("access_wait"):
        # Only the installed shared adapter's verified canonical wait can
        # extend this task. A wrapper cannot invent a host delay.
        supplied = value["access_wait"]
        actual_failure = _access_failure(state, supplied.get("failure_id"))
        if (digest(actual_failure) != supplied.get("failure_hash")
                or actual_failure["binding"]["scope"] != supplied.get("scope")
                or actual_failure["binding"]["next_retry_at"] != supplied.get("next_retry_at")):
            raise SafetyError("SOURCE_ACCESS_PROOF_INVALID")
        retry = actual_failure["binding"]["next_retry_at"]
    public_wait = None
    if value.get("public_access_wait") is not None:
        from .quickcommerce_browser import validate_public_access_wait
        public_wait = validate_public_access_wait(value["public_access_wait"], context["route"], context["acquisition_id"])
        retry = public_wait["next_retry_at"]
    actual = value["dispatch_started"] is True or old.get("dispatch_started") is True
    attempts = list(old["attempt_refs"])
    if actual and context["acquisition_id"] not in attempts:
        attempts.append(context["acquisition_id"])
    local_failures = old.get("prelaunch_failures", 0)
    terminal_recovery = context.get("terminal_recovery") == "supplier-original-dispatch-recovery-v1"
    if terminal_recovery and actual and existing is None and context["acquisition_id"] not in old["attempt_refs"]:
        # The old wrapper called this same original navigation untried. Its
        # exact saved proof now corrects that one attribution, not a new run.
        local_failures = max(0, local_failures - 1)
    elif (value["dispatch_started"] is False and existing is None and not terminal_recovery
          and not (public_wait and public_wait["guard_skipped"])):
        local_failures += 1
    task = dict(old, status=value["status"], collector_dispatched=actual,
                tries=len(attempts), attempt_refs=attempts, prelaunch_failures=local_failures,
                receipt_refs=list(dict.fromkeys(old["receipt_refs"] + [check_id])),
                target_results=value["target_results"], reason=value["reason"],
                next_retry_at=retry if value["retryable"] and value["status"] in {"failed", "blocked"} else None,
                reconcile_required=value["dispatch_uncertain"] or value.get("supplier_original_pending") is True,
                retryable=value["retryable"] and not value["dispatch_uncertain"]
                    and not (public_wait and retry is None))
    if "supplier_original_pending" in value:
        task["supplier_original_pending"] = value["supplier_original_pending"]
    task["revision"] = old.get("revision", 0) + 1
    if terminal_recovery:
        task["dispatch_started"] = actual
    changes = [change("query_tasks", task_id, task, old)]
    if existing is None:
        changes.append(change("source_checks", check_id, check))
    def validate_public_wait(_current):
        if public_wait is not None:
            from .quickcommerce_browser import validate_public_access_wait
            validate_public_access_wait(value["public_access_wait"], context["route"], context["acquisition_id"])
        if supplier_response is not None:
            from .source_acquisitions import _record, _response
            current_owner = _record(_current, context["acquisition_id"])
            if (current_owner["active_response_id"] != value["supplier_lead_capture_ref"]["id"]
                    or _response(_current, current_owner) != supplier_response):
                raise SafetyError("SOURCE_SUPPLIER_ORIGINAL_PROOF_INVALID")
    commit_options = {"validate": validate_public_wait} if public_wait is not None or supplier_response is not None else {}
    memory.commit(key("source-apply", [task_id, digest(check), digest(old)]), changes, **commit_options)
    return dict(value, receipt_id=check_id)


def _stage_receipt(memory, context, receipt):
    """Retain a returned public reply before a contended canonical commit.

    The hash-addressed file is immutable source-stage proof, not a completed
    check. Its small pointer is only local recovery metadata. Canonical import,
    current-target binding and receipt validation still run on reconciliation.
    """
    envelope = _envelope(receipt, context)
    refs = set(envelope["evidence_refs"])
    for target in envelope["target_results"].values():
        refs.update(target["evidence_refs"])
    envelope["evidence_refs"] = sorted(refs)
    root = safe_path(memory.root, "all_source_dispatch", "returned_receipts", context["acquisition_id"])
    content_hash = digest(receipt)
    path = root / (content_hash + ".json")
    if path.is_file():
        if load_json(path, limit=8 * 1024 * 1024) != receipt:
            raise SafetyError("SOURCE_RECEIPT_CONFLICT")
    else:
        atomic_json(path, receipt)
    atomic_json(root / "pending.json", {"receipt_hash": content_hash,
        "task_id": context["task_id"], "request_hash": context["request_hash"], "route": context["route"],
        "acquisition_id": context["acquisition_id"], "envelope_hash": digest(envelope)})


def _has_unapplied_receipt(memory, state, task):
    if not task.get("acquisition_id"):
        return False
    path = safe_path(memory.root, "all_source_dispatch", "returned_receipts", task["acquisition_id"]) / "pending.json"
    if not path.is_file():
        return False
    pointer = load_json(path)
    task_id = key("source-task", [task["run_id"], task["route"], task["request"]])
    if task.get("reused_acquisition_ref") and pointer.get("task_id") != task_id:
        # Target-specific Trends/supplier import must run once for the new
        # scope. Another task's staged target rows cannot substitute for it.
        return False
    return not any(state["source_checks"].get(ref, {}).get("envelope_hash") == pointer.get("envelope_hash")
                   for ref in task.get("receipt_refs", []))


def _progress_base_hash(run):
    # Preparing genuine new dependencies is not a route recovery. A later
    # applied pass or explicit recovery is; only those invalidate this stage.
    return digest({field: run.get(field) for field in
                   ("status", "last_pass_at", "last_results", "calls_this_pass", "route_pauses", "route_recovery_ref")})


def _effective_route_pauses(memory, state, run_key, run):
    pauses = deepcopy(run.get("route_pauses", {}))
    path = safe_path(memory.root, "all_source_dispatch", "pass_returns") / (run_key + ".json")
    if not path.is_file():
        return pauses
    saved = load_json(path)
    current = (saved.get("base_progress_hash") == _progress_base_hash(run)
               if "base_progress_hash" in saved else saved.get("run_revision") == run.get("revision", 0))
    if not current:
        return pauses
    restored = saved.get("route_pauses")
    if saved.get("run_id") != run["run_id"] or not isinstance(restored, dict) or not set(restored) <= set(ROUTES):
        raise SafetyError("SOURCE_PROGRESS_STAGE_INVALID")
    for route, pause in restored.items():
        original = state["source_checks"].get(pause.get("receipt_id"), {}) if isinstance(pause, dict) else {}
        if (original.get("kind") != CHECK_KIND or original.get("route") != route
                or original.get("task_id") not in run["task_ids"]
                or original.get("envelope", {}).get("failure_scope") != "route"):
            raise SafetyError("SOURCE_PROGRESS_STAGE_INVALID")
    return deepcopy(restored)


def _effective_saved_recovery(memory, run_key, run):
    """Advisory finite scan position; never dispatch/evidence authority."""
    recovery = run.get("saved_recovery")
    path = safe_path(memory.root, "all_source_dispatch", "pass_returns") / (run_key + ".json")
    if path.is_file():
        saved = load_json(path)
        if (saved.get("run_id") == run["run_id"]
                and saved.get("base_progress_hash") == _progress_base_hash(run)):
            recovery = saved.get("saved_recovery", recovery)
    if (not isinstance(recovery, dict) or set(recovery) != {"scope_hash", "cursor", "remaining", "turn_id"}
            or type(recovery["remaining"]) is not int or not 0 <= recovery["remaining"] <= len(run["task_ids"])
            or recovery["cursor"] not in run["task_ids"]
            or not isinstance(recovery["scope_hash"], str)):
        return None
    return deepcopy(recovery)


def _inspection_record(value, members):
    """Validate an advisory position against an actual append-only prefix."""
    if value is None:
        return None
    required = {"contract", "scope_hash", "ceiling", "next_index", "sweep", "turn_id"}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {"priority_next_index", "priority_pending_ids", "priority_scan_remaining",
                                       "rotation_index", "service_next_index", "original_recovery_basis"}
            or value.get("contract") != SOURCE_INSPECTION_KIND
            or any(type(value.get(field)) is not int for field in ("ceiling", "next_index", "sweep"))
            or not 0 <= value["next_index"] <= value["ceiling"] <= len(members)
            or value["sweep"] < 0
            or value["scope_hash"] != digest(members[:value["ceiling"]])
            or type(value.get("priority_next_index", 0)) is not int
            or not 0 <= value.get("priority_next_index", 0) <= len(members)
            or type(value.get("priority_scan_remaining", 0)) is not int
            or not 0 <= value.get("priority_scan_remaining", 0) <= len(members)
            or type(value.get("rotation_index", 0)) is not int
            or not 0 <= value.get("rotation_index", 0) < max(1, value["ceiling"])
            or type(value.get("service_next_index", 0)) is not int
            or not 0 <= value.get("service_next_index", 0) < max(1, len(members))
            or "original_recovery_basis" in value and (
                not isinstance(value["original_recovery_basis"], str)
                or len(value["original_recovery_basis"]) != 64
                or any(c not in "0123456789abcdef" for c in value["original_recovery_basis"]))
            or not isinstance(value.get("priority_pending_ids", []), list)
            or len(value.get("priority_pending_ids", [])) > SOURCE_INSPECTION_PRIORITY_LIMIT
            or any(not isinstance(tid, str) for tid in value.get("priority_pending_ids", []))
            or len(set(value.get("priority_pending_ids", []))) != len(value.get("priority_pending_ids", []))
            or not set(value.get("priority_pending_ids", [])) <= set(members)):
        raise SafetyError("SOURCE_PROGRESS_STAGE_INVALID")
    if value["turn_id"] is not None:
        identifier(value["turn_id"])
    return deepcopy(value)


def _effective_source_inspection_records(memory, run_key, run):
    """Merge only a current staged position; it never authorises a request."""
    full = run.get("inspection_continuation")
    subsets = run.get("inspection_subsets", {})
    path = safe_path(memory.root, "all_source_dispatch", "pass_returns") / (run_key + ".json")
    if path.is_file():
        saved = load_json(path)
        if (saved.get("run_id") == run["run_id"]
                and saved.get("base_progress_hash") == _progress_base_hash(run)):
            full = saved.get("inspection_continuation", full)
            subsets = saved.get("inspection_subsets", subsets)
    full = _inspection_record(full, run["task_ids"])
    if not isinstance(subsets, dict):
        raise SafetyError("SOURCE_PROGRESS_STAGE_INVALID")
    checked = {}
    membership = set(run["task_ids"])
    for scope, saved in subsets.items():
        if (not isinstance(saved, dict) or set(saved) != {"task_ids", "continuation", "saved_recovery"}
                or not isinstance(saved["task_ids"], list)
                or any(not isinstance(tid, str) for tid in saved["task_ids"])
                or len(set(saved["task_ids"])) != len(saved["task_ids"])
                or not set(saved["task_ids"]) <= membership
                or scope != digest(saved["task_ids"])):
            raise SafetyError("SOURCE_PROGRESS_STAGE_INVALID")
        selected = set(saved["task_ids"])
        if saved["task_ids"] != [tid for tid in run["task_ids"] if tid in selected]:
            raise SafetyError("SOURCE_PROGRESS_STAGE_INVALID")
        _inspection_record(saved["continuation"], saved["task_ids"])
        recovery = saved["saved_recovery"]
        if recovery is not None:
            if (not isinstance(recovery, dict)
                    or set(recovery) != {"scope_hash", "cursor", "remaining", "turn_id"}
                    or recovery["scope_hash"] != digest(sorted(saved["task_ids"]))
                    or recovery["cursor"] not in saved["task_ids"]
                    or type(recovery["remaining"]) is not int
                    or not 0 <= recovery["remaining"] <= len(saved["task_ids"])):
                raise SafetyError("SOURCE_PROGRESS_STAGE_INVALID")
            if recovery["turn_id"] is not None:
                identifier(recovery["turn_id"])
        checked[scope] = deepcopy(saved)
    return full, checked


def _source_inspection_order(members, position):
    """Frozen membership with a separately retained callback-fair order."""
    ceiling = position["ceiling"]
    rotation = position.get("rotation_index", 0)
    return members[rotation:ceiling] + members[:rotation] + members[ceiling:]


def _original_recovery_input(memory, state, members):
    """Actual original-recovery inputs, excluding clocks and diagnostic edits.

    A prepared question becoming an uncertain original creates one new local
    recovery obligation. Repeating the same unanswered reconciliation does
    not. This hash is advisory only; the original receipt/dispatch validation
    and remaining callback admission still own every recovery operation.
    """
    rows = [[task_id, task.get("acquisition_id"), task.get("targets"), task.get("receipt_refs")]
            for task_id in members for task in [state["query_tasks"][task_id]]
            if task.get("acquisition_id")
            and (task.get("reconcile_required") or task.get("status") == "dispatching")
            and not _has_unapplied_receipt(memory, state, task)]
    return digest(rows), bool(rows)


def _effective_source_inspection(memory, run_key, run, *, state=None):
    """Full-run continuation for readiness, including an appended next cohort.

    The stored ceiling freezes membership. A newly appended suffix is pending
    without resetting that ceiling or skipping its unfinished original prefix.
    This read is advisory only; dispatch still verifies every current binding.
    """
    value, _ = _effective_source_inspection_records(memory, run_key, run)
    if value is None:
        return None
    ordinary_remaining = len(run["task_ids"]) - value["next_index"]
    # Pending exact replies overlap with the ordinary suffix only once. The
    # finite priority sweep has its own remaining visits; these are scheduling
    # work, not a count of fresh searches, callbacks or confirmed source replies.
    pending = set(value.get("priority_pending_ids", []))
    order = _source_inspection_order(run["task_ids"], value)
    priority_remaining = len(pending - set(order[value["next_index"]:]))
    priority_remaining += value.get("priority_scan_remaining", 0)
    state = memory.snapshot() if state is None else state
    recovery_basis, has_originals = _original_recovery_input(memory, state, run["task_ids"])
    recovery_pending = has_originals and recovery_basis != value.get("original_recovery_basis")
    # This is one finite scheduling obligation, not another website result.
    # An unfinished ordinary cohort already carries the continuation signal.
    recovery_remaining = int(recovery_pending and not ordinary_remaining and not priority_remaining)
    return dict(value, cohort_remaining=value["ceiling"] - value["next_index"],
                ordinary_remaining=ordinary_remaining, priority_remaining=priority_remaining,
                original_recovery_pending=recovery_pending,
                remaining=ordinary_remaining + priority_remaining + recovery_remaining)


def _source_inspection_position(previous, members, turn, *, exhausted,
                                original_recovery_basis=None, has_originals=False):
    turn_id = turn["id"] if turn else None
    rotation = previous.get("rotation_index", 0) if previous else 0
    basis = previous.get("original_recovery_basis") if previous else original_recovery_basis
    if previous is None:
        start, ceiling, sweep = 0, len(members), 0
    elif previous["next_index"] < previous["ceiling"]:
        start, ceiling, sweep = previous["next_index"], previous["ceiling"], previous["sweep"]
    elif previous.get("priority_pending_ids") or previous.get("priority_scan_remaining", 0):
        # A new work turn does not reset an unfinished priority-only sweep.
        # Finish the frozen cohort before beginning any appended next cohort.
        start, ceiling, sweep = previous["next_index"], previous["ceiling"], previous["sweep"]
    elif previous["ceiling"] < len(members):
        start, ceiling, sweep = previous["ceiling"], len(members), previous["sweep"] + 1
        rotation = 0
    elif not exhausted and (turn is None or previous["turn_id"] != turn_id):
        start, ceiling, sweep = 0, len(members), previous["sweep"] + 1
        # Inspection is not service. In a new genuine work turn start after
        # the last actual callback, so one unresolved original cannot spend
        # every successive allowance before its unserved siblings get a turn.
        # Standalone callers retain their existing original-first behaviour.
        rotation = previous.get("service_next_index", 0) if turn is not None else 0
        basis = original_recovery_basis
    elif (not exhausted and turn is not None and has_originals
            and original_recovery_basis != previous.get("original_recovery_basis")):
        # A cold caller can recover a newly uncertain original in the SAME
        # still-open turn. Never reset merely for diagnostics, unused slots or
        # another instance: one unchanged failed recovery then settles again.
        start, ceiling, sweep = 0, len(members), previous["sweep"] + 1
        rotation = previous.get("service_next_index", 0)
        basis = original_recovery_basis
    else:
        start, ceiling, sweep = previous["next_index"], previous["ceiling"], previous["sweep"]
    result = {"contract": SOURCE_INSPECTION_KIND, "scope_hash": digest(members[:ceiling]),
            "ceiling": ceiling, "next_index": start, "sweep": sweep, "turn_id": turn_id,
            "priority_next_index": previous.get("priority_next_index", 0) if previous else 0,
            "priority_pending_ids": list(previous.get("priority_pending_ids", [])) if previous else [],
            "priority_scan_remaining": previous.get("priority_scan_remaining", 0) if previous else 0,
            "rotation_index": rotation,
            "service_next_index": previous.get("service_next_index", 0) if previous else 0}
    if basis is not None:
        result["original_recovery_basis"] = basis
    return result


def _source_inspection_priority(memory, state, members, position):
    """Bounded wake-hint scan for returned originals, not a second resolver.

    Only actual staged folders are candidates. Rotate over at most 64 such
    candidates and select at most eight replies per visit, so old applied
    pointers cannot hold an ordinary cursor at the head forever. Every chosen
    reply still enters the existing complete staged-receipt validation path.
    """
    root = safe_path(memory.root, "all_source_dispatch", "returned_receipts")
    try:
        with os.scandir(root) as entries:
            folders = {entry.name for entry in entries}
    except FileNotFoundError:
        position["priority_scan_remaining"] = 0
        return list(position.get("priority_pending_ids", []))
    # Resume selected-but-unvisited replies before searching for another
    # batch. Their exact original receipt is still revalidated on application.
    ready = list(position.get("priority_pending_ids", []))
    if len(ready) >= SOURCE_INSPECTION_PRIORITY_LIMIT or not members:
        return ready
    start = position.get("priority_next_index", 0) % len(members)
    remaining = position.get("priority_scan_remaining", 0) or len(members)
    examined = 0
    for offset in range(remaining):
        index = (start + offset) % len(members)
        task_id = members[index]
        task = state["query_tasks"][task_id]
        position["priority_next_index"] = (index + 1) % len(members)
        position["priority_scan_remaining"] = remaining - offset - 1
        if task.get("acquisition_id") not in folders:
            continue
        examined += 1
        if task_id not in ready and _has_unapplied_receipt(memory, state, task):
            ready.append(task_id)
        if examined >= SOURCE_INSPECTION_SLICE_LIMIT or len(ready) >= SOURCE_INSPECTION_PRIORITY_LIMIT:
            break
    return ready


def _staged_receipt(memory, context):
    root = safe_path(memory.root, "all_source_dispatch", "returned_receipts", context["acquisition_id"])
    pointer = load_json(root / "pending.json")
    content_hash = pointer.get("receipt_hash", "")
    if (pointer.get("request_hash") != context["request_hash"] or pointer.get("route") != context["route"]
            or pointer.get("task_id", context["task_id"]) != context["task_id"]
            or pointer.get("acquisition_id") != context["acquisition_id"]
            or not isinstance(content_hash, str) or len(content_hash) != 64
            or any(c not in "0123456789abcdef" for c in content_hash)):
        raise SafetyError("SOURCE_STAGED_RECEIPT_INVALID")
    receipt = load_json(root / (content_hash + ".json"), limit=8 * 1024 * 1024)
    if digest(receipt) != content_hash:
        raise SafetyError("SOURCE_STAGED_RECEIPT_INVALID")
    # Added or changed targets never inherit an older product's interpretation.
    # The same physical acquisition can still be imported for its real scope.
    receipt["target_results"] = [row for row in receipt.get("target_results", [])
        if context["description_hashes"].get(row.get("concept_id")) == row.get("description_hash")]
    return receipt


def _target_superseded(state, task, concept_id):
    marker = task.get("superseded_targets", {}).get(concept_id)
    if marker is None:
        return False
    replacement = state["query_tasks"].get(marker.get("task_id")) if isinstance(marker, dict) else None
    if (not replacement or replacement.get("kind") != TASK_KIND or replacement.get("run_id") != task.get("run_id")
            or replacement.get("route") != task.get("route")
            or task.get("request", {}).get("department") != "home"
            or replacement.get("request", {}).get("department") != "kitchen/1380374031"
            or marker.get("request_hash") != replacement.get("request_hash")
            or replacement.get("targets", {}).get(concept_id) != task.get("targets", {}).get(concept_id)
            or task.get("reconcile_required")):
        raise SafetyError("SOURCE_SUPERSEDED_BINDING_INVALID")
    return True


def source_access_context(state, task, *, as_of, pause=None):
    """Exact local preflight/readiness input; this is not launch permission."""
    task_id = key("source-task", [task["run_id"], task["route"], task["request"]])
    def receipt(check_id, *, latest=False):
        if check_id is None:
            return None
        check = state["source_checks"].get(check_id, {})
        envelope = check.get("envelope", {})
        if (check.get("kind") != CHECK_KIND or check.get("route") != task["route"]
                or envelope.get("route") != task["route"] or check.get("envelope_hash") != digest(envelope)
                or latest and (check.get("task_id") != task_id or check.get("request_hash") != task["request_hash"])):
            raise SafetyError("SOURCE_ACCESS_RECEIPT_INVALID")
        return deepcopy(envelope)
    reconciling = bool(task.get("reconcile_required") or task["status"] == "dispatching")
    original = task.get("acquisition_id")
    return {"route": task["route"], "request": deepcopy(task["request"]),
            "acquisition_id": original if reconciling else key("source-acquisition", [task_id, task.get("generation", task["tries"]) + 1]),
            "original_acquisition_id": original, "task_id": task_id, "as_of": as_of,
            "reconciling": reconciling, "task": deepcopy(task),
            "concept_ids": sorted(task["targets"]), "description_hashes": deepcopy(task["targets"]),
            "task_receipt": receipt(task["receipt_refs"][-1], latest=True) if task.get("receipt_refs") else None,
            "pause_receipt": receipt(pause.get("receipt_id")) if pause else None,
            **_inventory_refresh_context(state, task, as_of=as_of)}


def execute_source_run(memory, run_id, *, handlers=None, action_limit=None,
                       as_of=None, timeout_seconds=45, bounded_call=None, on_progress=None,
                       shared_marketplace=False, task_ids=None):
    """Run each due finite task once, continuing after source-local failures.

    No fit/source quorum or filled-report flag controls this run. A timeout or
    interrupted acquisition waits for its original receipt/reconciliation;
    known failed reads can have bounded later retries. No unbounded same-pass
    retry, arbitrary sleep, paid-account switch or phantom source attempt.
    """
    identifier(run_id)
    at = as_of or utc_now()
    timestamp(at)
    if type(shared_marketplace) is not bool:
        raise SafetyError("SOURCE_MARKETPLACE_SHARED_OPTION_INVALID")
    if (action_limit is not None and (type(action_limit) is not int or action_limit < 1)
            or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0):
        raise SafetyError("SOURCE_EXECUTION_LIMIT_INVALID")
    registry = route_registry(handlers)
    runner = bounded_call or _bounded_call
    if not callable(runner):
        raise SafetyError("SOURCE_BOUNDED_RUNNER_INVALID")
    if on_progress is not None and not callable(on_progress):
        raise SafetyError("SOURCE_PROGRESS_CALLBACK_INVALID")
    progress_errors = []
    local_save_errors = []
    def notify_progress(row):
        if on_progress:
            try:
                on_progress(deepcopy(row))
            except Exception as error:
                record_error("source_progress_failed", error, task_id=row["task_id"], stage="acquire")
                progress_errors.append({"task_id": row["task_id"], "reason": "SOURCE_PROGRESS_CALLBACK_FAILED"})
    class Outcomes(list):
        # Every branch appends exactly one local outcome before moving on.
        # Do not buffer wait/skip reasons until a large pass eventually ends.
        def append(self, row, *, outcome_reused=False):
            super().append(row)
            task_route = state["query_tasks"].get(row["task_id"], {}).get("route")
            record_event("source_outcome", stage="acquire", **{**row, "route": task_route,
                "phase": row.get("failure_phase"),
                "outcome_reused": outcome_reused,
                "next_action": row.get("next_action", "continue_to_next_ready_item")})
        def __deepcopy__(self, memo):
            # Copying a returned result or saving run metadata is not another
            # source visit and must never emit the same outcome a second time.
            return deepcopy(list(self), memo)
    results = Outcomes()
    with Journal(memory.root / "all_source_dispatch").locked(timeout=5):
        run_key = key("source-run", run_id)
        state = memory.snapshot()
        run = state["batch_runs"].get(run_key)
        if not run or run.get("kind") != RUN_KIND:
            raise SafetyError("SOURCE_RUN_REQUIRED")
        if task_ids is not None and (not isinstance(task_ids, (list, tuple))
                or any(not isinstance(tid, str) for tid in task_ids)
                or len(set(task_ids)) != len(task_ids) or not set(task_ids) <= set(run["task_ids"])):
            raise SafetyError("SOURCE_EXECUTION_TASK_SCOPE_INVALID")
        paused_routes = _effective_route_pauses(memory, state, run_key, run)
        staged_progress = safe_path(memory.root, "all_source_dispatch", "pass_returns") / (run_key + ".json")
        due_probes = {pause["task_id"] for pause in paused_routes.values()
                      if pause.get("next_retry_at") and timestamp(pause["next_retry_at"]) <= timestamp(at)}
        # Recover original dispatched replies before spending another read.
        # Untried questions then get a turn before due failed-question retries.
        # A due shared-route probe is the one exception: it must prove recovery
        # before untried products can be sent to that same failing route.
        ordered = sorted(run["task_ids"] if task_ids is None else task_ids, key=lambda i: (
                         not (state["query_tasks"][i].get("reconcile_required")
                              or state["query_tasks"][i]["status"] == "dispatching"),
                         i not in due_probes, state["query_tasks"][i]["tries"] > 0,
                         state["query_tasks"][i]["prepared_at"], i))
        calls = 0
        turn = active_work_turn(memory, state=state)
        initially_exhausted = bool(turn is not None and (turn["state"] != "open"
            or turn["counters"]["source_callbacks"] >= turn["policy"]["source_callbacks"]))
        full_inspection, subset_inspections = _effective_source_inspection_records(memory, run_key, run)
        selected = None if task_ids is None else set(task_ids)
        members = list(run["task_ids"]) if selected is None else [tid for tid in run["task_ids"] if tid in selected]
        subset_key = None if selected is None else digest(members)
        previous_subset = subset_inspections.get(subset_key)
        previous_inspection = (full_inspection if selected is None else
                               previous_subset["continuation"] if previous_subset else None)
        recovery_basis, has_originals = _original_recovery_input(memory, state, members)
        inspection = _source_inspection_position(previous_inspection, members, turn,
            exhausted=initially_exhausted, original_recovery_basis=recovery_basis,
            has_originals=has_originals)
        inspection_started = time.monotonic()
        priority_queue = deque(_source_inspection_priority(memory, state, members, inspection))
        priority_turn = True
        normal_scanned = inspection_scanned = 0
        # Only the legacy spent-allowance scanner uses the mutable priority
        # order. Normal continuation walks the frozen append-only prefix.
        inspection_order = _source_inspection_order(members, inspection)
        queue = deque(ordered if initially_exhausted else
                      inspection_order[inspection["next_index"]:inspection["ceiling"]])
        visited = set()
        recovery_queue = None
        recovery_scanned = 0
        full_recovery = _effective_saved_recovery(memory, run_key, run)
        recovery = full_recovery if selected is None else previous_subset.get("saved_recovery") if previous_subset else None
        recovery_cursor = recovery["cursor"] if recovery else run.get("saved_recovery_cursor") if selected is None else None
        # Task priority can change after applying a result. The finite scope
        # is its membership, not that changing order.
        recovery_scope = digest(sorted(ordered))
        positions = {task_id: index for index, task_id in enumerate(ordered)}
        while queue or recovery_queue or priority_queue:
            turn = active_work_turn(memory, state=state)
            exhausted = ((action_limit is not None and calls >= action_limit)
                or turn is not None and (turn["state"] != "open"
                    or turn["counters"]["source_callbacks"] >= turn["policy"]["source_callbacks"]))
            # This is a yield between complete local items, not an interrupt
            # of a resolver/import/save. Reserve an ordinary step when an
            # unusually slow priority reply used the elapsed slice itself.
            if (inspection_scanned and time.monotonic() - inspection_started >= SOURCE_INSPECTION_SLICE_SECONDS
                    and (normal_scanned or not queue or exhausted)):
                break
            if not exhausted and normal_scanned >= SOURCE_INSPECTION_SLICE_LIMIT:
                break
            if priority_queue and (priority_turn or not queue and not recovery_queue):
                task_id = priority_queue.popleft()
                priority_turn = False
            elif exhausted:
                if recovery_queue is None:
                    start = positions.get(recovery_cursor, 0)
                    recovery_queue = deque(tid for tid in ordered[start:] + ordered[:start] if tid not in visited)
                    turn_id = turn["id"] if turn else None
                    if recovery and recovery["scope_hash"] == recovery_scope and not calls:
                        if recovery["remaining"] > 0:
                            recovery_queue = deque(list(recovery_queue)[:recovery["remaining"]])
                        elif recovery["turn_id"] == turn_id:
                            recovery_queue.clear()
                    queue.clear()
                if not recovery_queue or recovery_scanned >= SAVED_RECOVERY_SCAN_LIMIT:
                    if priority_queue:
                        priority_turn = True
                        continue
                    break
                task_id = recovery_queue.popleft()
                recovery_scanned += 1
                recovery_cursor = ordered[(positions[task_id] + 1) % len(ordered)]
            else:
                task_id = queue.popleft()
                inspection["next_index"] += 1
                normal_scanned += 1
                priority_turn = True
            if task_id in visited:
                continue  # A priority reply never jumps the ordinary pointer.
            visited.add(task_id)
            inspection_scanned += 1
            # This source guard owns the finite pass. Skipping a saved,
            # unconnected, paused or slice-exhausted task changes no state;
            # do not re-verify thousands of mounted-drive files per row.
            # A real returned/failed handler refreshes the verified state.
            task = state["query_tasks"][task_id]
            route = task["route"]
            record_event("source_inspection", stage="acquire", task_id=task_id, route=route)
            callback = registry[route]["handler"]
            hooks = shared_source_hooks(registry[route])
            resolver = hooks["resolve"] if shared_marketplace else None
            # One additive saved-only visit to an old false-not-dispatched
            # terminal supplier wrapper. The installed supplier reconciler
            # validates the original URL/time/hash proof before attributing a
            # request. It never launches a page or increments the generation.
            terminal_recovery = bool(registry[route].get("recover_terminal")
                and task.get("status") == "failed" and task.get("reason") == "SUPPLIER_NATIVE_READ_FAILED"
                and task.get("generation", 0) > 0 and task.get("acquisition_id")
                and task.get("tries") == 0 and task.get("dispatch_started") is False
                and not task.get("attempt_refs") and not task.get("retryable")
                and not task.get("reconcile_required") and not task.get("original_dispatch_recovery"))
            if task["status"] in _FINAL and not (task.get("supplier_original_pending") and task.get("reconcile_required")):
                results.append({"task_id": task_id, "state": "reused", "reason": "Original acquisition is already saved; interpret it without recollecting."})
                continue
            staged = _has_unapplied_receipt(memory, state, task)
            # A route without a handler (not installed, or retired) can still
            # finish an already-started original through saved-only replay;
            # it never starts anything new.
            recovery_callback = (registry[route].get("recover_terminal") if terminal_recovery
                                 else registry[route].get("reconcile"))
            original_recovery = bool((task.get("reconcile_required") or task["status"] == "dispatching"
                                      or terminal_recovery) and recovery_callback)
            if exhausted and not staged and not callback and not original_recovery:
                # Spent allowance is irrelevant: this route sends nothing new.
                results.append({"task_id": task_id, "state": "not_connected", "actual_attempt": False})
                continue
            if exhausted and not staged:
                # These hints only inspect the supplied verified state and
                # exact original files. Never call a mutating resolver merely
                # to discover that this turn cannot start another request.
                hint_context = source_access_context(state, task, as_of=at, pause=paused_routes.get(route))
                try:
                    advice = source_readiness_hint(registry[route], "due_hint", hint_context,
                        state=state, include_access_hint=False) if resolver else None
                    saved_hint = (source_readiness_hint(registry[route], "saved_hint", hint_context, state=state)
                                  if resolver and not (advice or {}).get("local_saved") else None)
                    original_saved = bool((advice or {}).get("local_saved") or
                        (advice or {}).get("signal", {}).get("state") is not None and
                        any(row[0] not in {"intent.json", "native_dispatch.json", "native/native_dispatch.json"}
                            for row in (saved_hint or {}).get("files", [])))
                except (SafetyError, OSError) as error:
                    results.append({"task_id": task_id, "state": "waiting", "actual_attempt": False,
                        "reason": str(error) if isinstance(error, SafetyError) else "SOURCE_SAVED_HINT_UNREADABLE"})
                    continue
                if not original_saved:
                    results.append({"task_id": task_id, "state": "not_attempted", "actual_attempt": False,
                        "reason": "WORK_TURN_SOURCE_LIMIT" if turn is not None and
                            turn["counters"]["source_callbacks"] >= turn["policy"]["source_callbacks"]
                            else "SOURCE_CALLBACK_ALLOWANCE_SPENT", "next_action": "continue_in_next_work_turn"})
                    continue
            try:
                refresh_context = _inventory_refresh_context(state, task, as_of=at)
            except SafetyError as error:
                results.append({"task_id": task_id, "state": "waiting", "actual_attempt": False, "reason": str(error)})
                continue
            if task.get("targets") and all(_target_superseded(state, task, cid) for cid in task["targets"]):
                results.append({"task_id": task_id, "state": "superseded", "actual_attempt": False,
                                "reason": "Original unsupported list scope is retained; its verified replacement owns future work."})
                continue
            if not callback and not staged and not original_recovery:
                results.append({"task_id": task_id, "state": "not_connected", "actual_attempt": False})
                continue
            access_resolution = None
            access_hook = registry[route].get("source_access_preflight")
            if access_hook and not staged:
                # The installed hook may read/parse only exact saved source
                # bytes here. Any fresh request still needs the caller intent
                # and callback allowance below.
                try:
                    eligible_targets = {cid: expected for cid, expected in task["targets"].items()
                        if cid in state["concepts"] and _scope(state, cid) == expected
                        and not _target_superseded(state, task, cid)
                        and state["concepts"][cid].get("status") not in {"merged", "retired", "excluded"}}
                    if not eligible_targets:
                        results.append({"task_id": task_id, "state": "not_attempted", "actual_attempt": False,
                                        "reason": "Target descriptions changed; prepare current questions instead."})
                        continue
                    preflight_context = source_access_context(state, task, as_of=at, pause=paused_routes.get(route))
                    preflight_context.update(concept_ids=sorted(eligible_targets), description_hashes=eligible_targets)
                    access_resolution = access_hook(preflight_context)
                    if not isinstance(access_resolution, dict) or access_resolution.get("action") not in {"collect", "saved", "wait"}:
                        raise SafetyError("SOURCE_ACCESS_PREFLIGHT_INVALID")
                except (SafetyError, OSError) as error:
                    results.append({"task_id": task_id, "state": "blocked", "actual_attempt": False,
                                    "reason": str(error) if isinstance(error, SafetyError) else "SOURCE_ACCESS_LOCAL_PROOF_PENDING"})
                    continue
                if access_resolution["action"] == "wait":
                    results.append({"task_id": task_id, "state": "waiting", "actual_attempt": False,
                                    "reason": access_resolution["reason"], "next_retry_at": access_resolution.get("next_retry_at")})
                    continue
            access_saved = bool(access_resolution and access_resolution["action"] == "saved")
            public_bypass = bool(access_resolution and access_resolution.get("bypass_public_pause") is True)
            if route in paused_routes and not resolver and not staged and not access_saved and not public_bypass and not (task.get("reconcile_required") or task["status"] == "dispatching" or terminal_recovery):
                pause = paused_routes[route]
                is_probe = task_id in due_probes and task_id == pause["task_id"]
                if not is_probe:
                    results.append({"task_id": task_id, "state": "blocked", "actual_attempt": False,
                                    "reason": "This route is waiting after an actual shared route failure.",
                                    "blocked_by": pause["receipt_id"], "next_retry_at": pause.get("next_retry_at")})
                    continue
            if task.get("reconcile_required") or task["status"] == "dispatching" or terminal_recovery:
                callback = registry[route]["recover_terminal"] if terminal_recovery else \
                    (lambda context: _staged_receipt(memory, context)) if staged else registry[route].get("reconcile")
                if not callback:
                    results.append({"task_id": task_id, "state": "blocked", "actual_attempt": False,
                                    "reason": "Original acquisition needs its saved receipt; do not launch it again."})
                    continue
                reconciling = True
                # A composite supplier pass can contain a separately proved
                # reservation that never reached its transport intent. Admit
                # only that normal continuation, preserving this SAME outer
                # acquisition and generation. Staged replies still win, and
                # ordinary original-only reconciliation never starts a read.
                continuation_hook = registry[route].get("continue_unstarted")
                if route == "company-pages" and not staged and not terminal_recovery and continuation_hook:
                    continuation = continuation_hook(source_access_context(state, task, as_of=at))
                    if continuation is not None:
                        ordinary = registry[route]["handler"]
                        callback = lambda context, ordinary=ordinary, continuation=continuation: ordinary(
                            dict(context, company_unstarted_continuation=continuation))
            else:
                reconciling = False
                prior_launch = task.get("generation", task["tries"]) > 0
                if not access_saved and (task["tries"] >= run["max_attempts"]
                        or task.get("prelaunch_failures", 0) >= run["max_attempts"]
                        or task["status"] == "waiting_input"
                        or (prior_launch and task["status"] != "prepared" and not task.get("retryable"))):
                    results.append({"task_id": task_id, "state": task["status"], "actual_attempt": False,
                                    "reason": "Original finite question has no due retry; other questions continue."},
                                   outcome_reused=True)
                    continue
                if not access_saved and not public_bypass and task["next_retry_at"] and timestamp(task["next_retry_at"]) > timestamp(at):
                    results.append({"task_id": task_id, "state": "waiting", "actual_attempt": False,
                                    "next_retry_at": task["next_retry_at"]})
                    continue
            current_targets = {i: scope for i, scope in task["targets"].items()
                               if i in state["concepts"] and _scope(state, i) == scope
                               and not _target_superseded(state, task, i)
                               and state["concepts"][i].get("status") not in {"merged", "retired", "excluded"}}
            if not current_targets:
                results.append({"task_id": task_id, "state": "not_attempted", "actual_attempt": False,
                                "reason": "Target descriptions changed; prepare current questions instead."})
                continue
            if route == "company-pages" and reconciling and not staged and not terminal_recovery:
                # The installed company reader can prove that its exact outer
                # input/reply has not arrived. Do not spend another callback
                # merely to raise that same saved-only wait. This is not a
                # generic ready=False veto: older/custom handlers without the
                # explicit bound contract retain their ordinary behaviour.
                from .company_pages import ORIGINAL_READINESS
                try:
                    advice = source_readiness_hint(registry[route], "due_hint",
                        source_access_context(state, task, as_of=at, pause=paused_routes.get(route)), state=state)
                except (SafetyError, OSError) as error:
                    if isinstance(error, SafetyError) and str(error) in _INTEGRITY_ERRORS: raise
                    results.append({"task_id": task_id, "state": "waiting", "actual_attempt": False,
                        "reason": str(error) if isinstance(error, SafetyError) else "SOURCE_SAVED_HINT_UNREADABLE"})
                    continue
                pending = (advice or {}).get("company_original_wait")
                if pending == {"contract": ORIGINAL_READINESS, "task_id": task_id,
                        "acquisition_id": task.get("acquisition_id"), "request_hash": task["request_hash"]}:
                    results.append({"task_id": task_id, "state": "waiting", "actual_attempt": False,
                        "reason": "SUPPLIER_COMPANY_ORIGINAL_PENDING",
                        "next_action": "wait_for_original_reply_and_continue_to_next_ready_item"})
                    continue
            resolution = None
            if resolver:
                proposed = task.get("acquisition_id") if reconciling else key("source-acquisition", [task_id, task.get("generation", task["tries"]) + 1])
                try:
                    resolution = resolver({"task_id": task_id, "route": route, "request": deepcopy(task["request"]),
                        "acquisition_id": proposed, "concept_ids": sorted(current_targets),
                        "description_hashes": current_targets, "reconciling": reconciling or exhausted, "as_of": at,
                        **({} if exhausted else refresh_context)})
                except (SafetyError, OSError) as error:
                    results.append({"task_id": task_id, "state": "blocked", "actual_attempt": False,
                        "reason": str(error) if isinstance(error, SafetyError) else "SOURCE_MARKETPLACE_LOCAL_PROOF_PENDING"})
                    state = memory.snapshot()
                    continue
                state = memory.snapshot()
                if resolution["action"] == "wait":
                    results.append({"task_id": task_id, "state": "waiting", "actual_attempt": False,
                                    "reason": resolution["reason"], "next_retry_at": resolution.get("next_retry_at")})
                    continue
                if resolution["action"] == "collect":
                    callback = registry[route]["handler"]
                    reconciling = False
                if resolution["action"] == "collect" and route in paused_routes and task_id not in due_probes and not public_bypass:
                    results.append({"task_id": task_id, "state": "blocked", "actual_attempt": False,
                        "reason": "This route is waiting after an actual shared route failure.",
                        "next_retry_at": paused_routes[route].get("next_retry_at")})
                    continue
            local_saved = access_saved or bool(resolution and resolution["action"] == "saved")
            if (not local_saved and not staged and not public_bypass and task.get("next_retry_at")
                    and timestamp(task["next_retry_at"]) > timestamp(at)):
                results.append({"task_id": task_id, "state": "waiting", "actual_attempt": False,
                                "next_retry_at": task["next_retry_at"]})
                continue
            if not local_saved and not staged and exhausted:
                results.append({"task_id": task_id, "state": "not_attempted", "actual_attempt": False,
                                "reason": "Still queued within the same finite run."})
                continue
            turn = active_work_turn(memory)
            callback_admitted = False
            access_admission = {"changes": [], "caller_fields": {}, "context_fields": {}}
            if local_saved:
                acquisition_id = access_resolution["acquisition_id"] if access_saved else resolution["acquisition_id"]
                intent = dict(task, status="dispatching", acquisition_id=acquisition_id,
                    reconcile_required=True)
                if not access_saved:
                    intent.update(acquisition_owner_ref=acquisition_id,
                                  reused_acquisition_ref={"kind": "source-acquisition-owner-v1", "id": acquisition_id},
                                  **shared_source_caller_fields(registry[route], resolution["request"]))
                _upsert(memory, "query_tasks", task_id, intent, task)
            elif not reconciling:
                if resolution and hooks["admission"]:
                    try:
                        access_admission = hooks["admission"](
                            {"task_id": task_id, "as_of": at}, resolution)
                    except SafetyError as error:
                        if str(error) not in {"SOURCE_ACCESS_WAIT", "SOURCE_ACCESS_PROBE_PENDING", "SOURCE_ACCESS_ORIGINAL_RECOVERY_REQUIRED"}: raise
                        results.append({"task_id": task_id, "state": "waiting", "actual_attempt": False, "reason": str(error)})
                        continue
                generation = (task.get("generation", task["tries"]) if resolution and resolution.get("resume_reserved")
                              else task.get("generation", task["tries"]) + 1)
                acquisition_id = resolution["acquisition_id"] if resolution else key("source-acquisition", [task_id, generation])
                intent = dict(task, generation=generation, status="dispatching", acquisition_id=acquisition_id,
                              dispatch_started=False, reconcile_required=True, dispatched_at=at)
                if resolution:
                    intent.update(acquisition_owner_ref=acquisition_id,
                                  **shared_source_caller_fields(registry[route], resolution["request"]))
                    intent.update(access_admission["caller_fields"])
                try:
                    if turn is None:
                        memory.commit(key("source-caller-admission", [task_id, digest(intent), access_admission["changes"]]),
                            [change("query_tasks", task_id, intent, task), *access_admission["changes"]])
                    else:
                        owner = key("source-service", [turn["id"], turn["counters"]["source_callbacks"],
                                                       task_id, acquisition_id, "collect"])
                        reserve_source_callback(memory, owner, at=at,
                                                changes=[change("query_tasks", task_id, intent, task), *access_admission["changes"]])
                        callback_admitted = True
                except SafetyError as error:
                    if access_admission["changes"] and str(error) == "RESEARCH_DEPENDENCY_CHANGED":
                        results.append({"task_id": task_id, "state": "waiting", "actual_attempt": False,
                                        "reason": "SOURCE_ACCESS_PROBE_PENDING"})
                        state = memory.snapshot()
                        continue
                    if str(error) in {"WORK_TURN_SOURCE_LIMIT", "WORK_TURN_CLOSED"}:
                        results.append({"task_id": task_id, "state": "not_attempted", "actual_attempt": False,
                                        "reason": str(error), "next_action": "continue_in_next_work_turn"})
                        continue
                    if str(error) != "RUNNER_ALREADY_ACTIVE":
                        raise
                    # No handler has been called. In particular, do not count
                    # this local admission conflict as an external request.
                    row = {"task_id": task_id, "state": "not_attempted", "actual_attempt": False,
                           "reason": "RUNNER_ALREADY_ACTIVE", "local_save_pending": True,
                           "reconcile_required": False}
                    results.append(row)
                    local_save_errors.append({"task_id": task_id, "stage": "intent", "reason": str(error)})
                    notify_progress(row)
                    continue
            else:
                acquisition_id = task["acquisition_id"]
            if turn is not None and not callback_admitted and not local_saved and not staged:
                try:
                    owner = key("source-service", [turn["id"], turn["counters"]["source_callbacks"],
                                                   task_id, acquisition_id, "saved_reconciliation"])
                    reserve_source_callback(memory, owner, at=at)
                except SafetyError as error:
                    if str(error) not in {"WORK_TURN_SOURCE_LIMIT", "WORK_TURN_CLOSED"}:
                        raise
                    results.append({"task_id": task_id, "state": "not_attempted", "actual_attempt": False,
                                    "reason": str(error), "next_action": "continue_in_next_work_turn"})
                    continue
            if terminal_recovery:
                updated = dict(task, reconcile_required=True,
                    original_dispatch_recovery={"version": "supplier-original-dispatch-recovery-v1",
                        "acquisition_id": acquisition_id, "request_hash": task["request_hash"]})
                try:
                    _upsert(memory, "query_tasks", task_id, updated, task)
                except SafetyError as error:
                    if str(error) != "RUNNER_ALREADY_ACTIVE":
                        raise
                    row = {"task_id": task_id, "state": "failed", "actual_attempt": False,
                        "reason": "RUNNER_ALREADY_ACTIVE", "local_save_pending": True,
                        "reconcile_required": False}
                    results.append(row)
                    local_save_errors.append({"task_id": task_id, "stage": "original_dispatch_recovery", "reason": str(error)})
                    notify_progress(row)
                    continue
            context = {"task_id": task_id, "acquisition_id": acquisition_id, "route": route,
                       "request": deepcopy(resolution["request"] if resolution else task["request"]), "request_hash": task["request_hash"],
                       "concept_ids": sorted(current_targets), "description_hashes": current_targets,
                       "retry_delay_seconds": run["retry_delay_seconds"], "timeout_seconds": timeout_seconds,
                       "reconciling": reconciling, "as_of": at}
            if resolution:
                context.update(marketplace_admitted=not local_saved, shared_marketplace=True,
                               marketplace_caller={"table": "query_tasks", "id": task_id})
                if callable(registry[route].get("resolve_source")):
                    context.update(source_admitted=not local_saved,
                                   source_caller={"table": "query_tasks", "id": task_id})
                context.update(access_admission["context_fields"])
            def record_dispatch(*_args, **_kwargs):
                current = memory.snapshot()["query_tasks"][task_id]
                if current["acquisition_id"] != acquisition_id:
                    raise SafetyError("SOURCE_TASK_EXECUTION_CHANGED")
                if current.get("reused_acquisition_ref"):
                    # Saved-only adapters can repeat their notification of the
                    # original receipt. Do not turn that into a new request.
                    return current["reused_acquisition_ref"]
                dispatch_id = key("source-dispatch", [task_id, acquisition_id])
                if current.get("dispatch_started"):
                    return dispatch_id
                now = utc_now()
                check = {"kind": DISPATCH_KIND, "source": ROUTES[route]["source"], "route": route,
                         "at": now, "state": "in_progress", "reason": "Actual route attempt started.",
                         "reference": dispatch_id, "task_id": task_id, "acquisition_id": acquisition_id,
                         "request_hash": task["request_hash"], "targets": deepcopy(current_targets)}
                updated = dict(current, dispatch_started=True, collector_dispatched=True,
                               attempt_refs=list(dict.fromkeys(current["attempt_refs"] + [acquisition_id])),
                               tries=len(set(current["attempt_refs"] + [acquisition_id])),
                               revision=current.get("revision", 0) + 1)
                memory.commit(dispatch_id, [change("query_tasks", task_id, updated, current),
                                           change("source_checks", dispatch_id, check)])
                return dispatch_id
            context["record_dispatch"] = record_dispatch
            if terminal_recovery or task.get("original_dispatch_recovery") == {
                    "version": "supplier-original-dispatch-recovery-v1", "acquisition_id": acquisition_id,
                    "request_hash": task["request_hash"]}:
                context["terminal_recovery"] = "supplier-original-dispatch-recovery-v1"
            if not local_saved and not staged:
                calls += 1
                inspection["service_next_index"] = (members.index(task_id) + 1) % len(members)
            try:
                with trace_source(dict(context, deadline_seconds=timeout_seconds)) as trace:
                    with source_phase("handler"):
                        receipt = (access_resolution["receipt"] if access_saved else resolution["receipt"]) if local_saved else runner(callback, context, timeout_seconds)
                    with source_phase("response_save"):
                        _stage_receipt(memory, context, receipt)
                    trace.response_saved = True
                    with source_phase("apply"):
                        result = _persist_receipt(memory, task_id, context, receipt)
                state = memory.snapshot()
                row = {"task_id": task_id, "state": result["status"],
                                "actual_attempt": result["dispatch_started"] is True,
                                "dispatch_uncertain": result["dispatch_uncertain"], "receipt_id": result["receipt_id"],
                                "timings": dict(trace.durations), "route": route}
                results.append(row)
                notify_progress(row)
                if result["failure_scope"] == "route" and result["status"] in {"failed", "blocked"}:
                    saved_task = state["query_tasks"][task_id]
                    remaining_retry = (result["retryable"] and saved_task["tries"] < run["max_attempts"]
                                       and saved_task.get("prelaunch_failures", 0) < run["max_attempts"])
                    paused_routes[route] = {"receipt_id": result["receipt_id"], "task_id": task_id,
                        "reason": result["reason"], "next_retry_at": saved_task["next_retry_at"] if remaining_retry else None}
                elif task_id in due_probes and result["status"] in _FINAL:
                    paused_routes.pop(route, None)
            except (KeyboardInterrupt, SystemExit):
                raise  # Durable intent/actual dispatch survives; no replacement launch.
            except (SourceActionDeadline, Exception) as error:
                record_error("source_action_failed", error, stage="acquire", task_id=task_id, route=route,
                    phase=getattr(error, "phase", trace.failure_phase or trace.phase), timings=dict(trace.durations),
                    response_saved=trace.response_saved)
                if isinstance(error, SafetyError) and str(error) in _INTEGRITY_ERRORS:
                    raise
                reason = (str(error) if isinstance(error, SafetyError) else
                          "SOURCE_ACTION_TIMEOUT" if isinstance(error, (SourceActionDeadline, TimeoutError)) else "SOURCE_ACTION_FAILED")
                current = memory.snapshot()["query_tasks"][task_id]
                error_state = "failed" if isinstance(error, SourceActionDeadline) or reason in _LOCAL_RECEIPT_ERRORS else "blocked"
                updated = dict(current, status=error_state, reconcile_required=True, retryable=False,
                               reason=reason, next_retry_at=None)
                try:
                    _upsert(memory, "query_tasks", task_id, updated, current)
                    save_pending = False
                except SafetyError as save_error:
                    if str(save_error) != "RUNNER_ALREADY_ACTIVE":
                        raise
                    # The earlier dispatching intent remains durable, and a
                    # returned reply/source adapter proof stays replayable.
                    save_pending = True
                    local_save_errors.append({"task_id": task_id, "stage": "outcome", "reason": str(save_error)})
                state = memory.snapshot()
                row = {"task_id": task_id, "state": error_state, "actual_attempt": current.get("dispatch_started") is True,
                                "dispatch_uncertain": current.get("dispatch_started") is not True,
                                "reason": reason, "reconcile_required": True, "local_save_pending": save_pending,
                                "route": route, "failure_phase": getattr(error, "phase", trace.failure_phase or trace.phase),
                                "timings": dict(trace.durations), "response_saved": trace.response_saved}
                results.append(row)
                notify_progress(row)
        # A staged-priority visit may finish the leading ordinary positions
        # without a normal queue pop (especially with a spent allowance).
        # Advance only across exact IDs visited in this pass, never over an
        # untouched sibling merely because another reply had priority.
        while (inspection["next_index"] < inspection["ceiling"]
                and inspection_order[inspection["next_index"]] in visited):
            inspection["next_index"] += 1
        latest_state = memory.snapshot()
        old_run = latest_state["batch_runs"][run_key]
        if recovery_queue is not None and recovery_cursor is not None:
            recovery = {"scope_hash": recovery_scope, "cursor": recovery_cursor,
                "remaining": len(recovery_queue), "turn_id": turn["id"] if turn else None}
            if not recovery_queue and recovery_scope == digest(sorted(members)):
                # The existing spent-allowance sweep covered this exact
                # membership, including the normal prefix left at exhaustion.
                # A different subset/cohort cannot settle the full-run cursor.
                inspection["next_index"] = inspection["ceiling"]
        elif recovery is not None and inspection["next_index"] >= inspection["ceiling"]:
            # A normal pass already inspected its whole scope. A previously
            # pending advisory scan must not keep unchanged work busy forever.
            recovery = dict(recovery, remaining=0)
        if selected is None:
            full_inspection, full_recovery = inspection, recovery
            stored_recovery_cursor = recovery_cursor
        else:
            subset_inspections[subset_key] = {"task_ids": members, "continuation": inspection,
                                               "saved_recovery": recovery}
            stored_recovery_cursor = full_recovery["cursor"] if full_recovery else old_run.get("saved_recovery_cursor")
        inspection["priority_pending_ids"] = list(priority_queue)
        ordinary_remaining = len(members) - inspection["next_index"]
        priority_remaining = len(set(priority_queue) - set(inspection_order[inspection["next_index"]:]))
        priority_remaining += inspection["priority_scan_remaining"]
        recovery_basis, has_originals = _original_recovery_input(memory, latest_state, members)
        recovery_pending = has_originals and recovery_basis != inspection.get("original_recovery_basis")
        inspection_remaining = ordinary_remaining + priority_remaining + int(
            recovery_pending and not ordinary_remaining and not priority_remaining)
        inspection_pending = inspection_remaining > 0
        record_event("source_pass_finished", stage="acquire", scanned=inspection_scanned,
            remaining=max(inspection_remaining, len(recovery_queue) if recovery_queue is not None else 0),
            counts=dict(Counter(row["state"] for row in results)),
            next_action="continue_to_next_ready_stage")
        updated_run = dict(old_run, status="pass_finished", last_pass_at=at,
                           last_results=list(results), calls_this_pass=calls, route_pauses=paused_routes,
                           saved_recovery_cursor=stored_recovery_cursor, saved_recovery=full_recovery,
                           inspection_continuation=full_inspection, inspection_subsets=subset_inspections)
        # This is derived recovery metadata backed by the actual immutable
        # failure receipts. If the progress commit is busy, the next pass must
        # still respect the observed route pause instead of probing every item.
        atomic_json(staged_progress, {"schema_version": 1, "run_id": run_id,
            "run_revision": old_run.get("revision", 0), "base_progress_hash": _progress_base_hash(old_run),
            "route_pauses": paused_routes, "saved_recovery": full_recovery,
            "inspection_continuation": full_inspection, "inspection_subsets": subset_inspections})
        try:
            _upsert(memory, "batch_runs", run_key, updated_run, old_run)
        except SafetyError as error:
            if str(error) != "RUNNER_ALREADY_ACTIVE":
                raise
            local_save_errors.append({"stage": "pass_progress", "reason": str(error)})
    return {"run_id": run_id, "handler_calls": calls, "results": results,
            "counts": dict(Counter(row["state"] for row in results)),
            "report_filled_does_not_stop_work": True, "progress_errors": progress_errors,
            "local_save_errors": local_save_errors, "saved_recovery_scanned": recovery_scanned,
            "saved_recovery_cursor": recovery_cursor,
            "saved_recovery_pending": bool(recovery and recovery["remaining"] > 0),
            "inspection_scanned": inspection_scanned, "inspection_normal_scanned": normal_scanned,
            "inspection_remaining": inspection_remaining, "inspection_pending": inspection_pending}


def retry_source_prelaunch_task(memory, task_id, *, reason, as_of=None):
    """Reopen a proven local pre-dispatch failure after that cause is repaired.

    This does not spend a source attempt, delete a failed receipt, reset an
    actual-request budget or waive an uncertain launch. It requires the latest
    saved receipt to prove that no request started and to name a supported
    local failure, rather than interpreting an absence of proof as no request.
    """
    identifier(task_id)
    reason = _reason(reason)
    at = as_of or utc_now(); timestamp(at)
    with Journal(memory.root / "all_source_dispatch").locked(timeout=5):
        state = memory.snapshot()
        task = state["query_tasks"].get(task_id)
        if not task or task.get("kind") != TASK_KIND or not task.get("receipt_refs"):
            raise SafetyError("SOURCE_PRELAUNCH_RECEIPT_REQUIRED")
        check_id = task["receipt_refs"][-1]
        check = state["source_checks"].get(check_id, {})
        envelope = check.get("envelope", {})
        if (check.get("kind") != CHECK_KIND or check.get("task_id") != task_id
                or check.get("envelope_hash") != digest(envelope)
                or envelope.get("acquisition_id") != task.get("acquisition_id")
                or envelope.get("dispatch_started") is not False
                or task.get("dispatch_started") or task.get("reconcile_required")
                or task.get("acquisition_id") in task.get("attempt_refs", [])
                or envelope.get("reason") not in _RECOVERABLE_PRELAUNCH
                or envelope.get("status") not in {"failed", "blocked"}
                or task.get("status") not in {"failed", "blocked"}):
            raise SafetyError("SOURCE_PRELAUNCH_RECOVERY_NOT_PROVEN")
        run_key = key("source-run", task["run_id"])
        run = state["batch_runs"].get(run_key)
        actual_tries = len(set(task["attempt_refs"]))
        if not run or run.get("kind") != RUN_KIND or actual_tries >= run["max_attempts"]:
            raise SafetyError("SOURCE_ACTUAL_ATTEMPT_BUDGET_EXHAUSTED")
        record_id = key("source-prelaunch-retry", [task_id, check_id, at, reason])
        record = {"kind": "all_source_prelaunch_recovery_v1", "source": task["source"],
                  "route": task["route"], "at": at, "state": "resumed", "reason": reason,
                  "reference": record_id, "task_id": task_id, "original_receipt": check_id,
                  "original_acquisition_id": task["acquisition_id"], "actual_attempts_unchanged": actual_tries}
        updated = dict(task, status="prepared", retryable=True, next_retry_at=at,
                       tries=actual_tries,
                       generation=max(task.get("generation", task["tries"]), len(task["receipt_refs"])),
                       prelaunch_failures=0, reconcile_required=False,
                       reason="Recorded local failure was repaired; no source request started.",
                       revision=task.get("revision", 0) + 1)
        changes = [change("source_checks", record_id, record), change("query_tasks", task_id, updated, task)]
        pause = run.get("route_pauses", {}).get(task["route"])
        if pause and pause.get("task_id") == task_id and pause.get("receipt_id") == check_id:
            pauses = deepcopy(run["route_pauses"]); pauses.pop(task["route"])
            changes.append(change("batch_runs", run_key, dict(run, route_pauses=pauses,
                                  revision=run.get("revision", 0) + 1), run))
        memory.commit(record_id, changes)
        return record_id


def resume_source_route(memory, run_id, route, *, reason, as_of=None):
    """Clear only a route wait after a real recorded recovery/input change.

    This is a local resumption instruction, not another source attempt or an
    override of unresolved original jobs, funding or retry budgets. Healthy
    untouched questions can continue; uncertain jobs still need reconciliation.
    """
    identifier(run_id)
    if route not in ROUTES:
        raise SafetyError("SOURCE_ROUTE_INVALID")
    reason = _reason(reason)
    at = as_of or utc_now(); timestamp(at)
    with Journal(memory.root / "all_source_dispatch").locked(timeout=5):
        state = memory.snapshot()
        run_key = key("source-run", run_id)
        run = state["batch_runs"].get(run_key)
        if not run or run.get("kind") != RUN_KIND:
            raise SafetyError("SOURCE_RUN_REQUIRED")
        effective_pauses = _effective_route_pauses(memory, state, run_key, run)
        pause = effective_pauses.get(route)
        if not pause:
            return None
        pauses = deepcopy(effective_pauses); pauses.pop(route)
        record_id = key("source-route-resume", [run_key, route, pause, at, reason])
        record = {"kind": "all_source_route_resume_v1", "source": ROUTES[route]["source"],
                  "route": route, "at": at, "state": "resumed", "reason": reason,
                  "reference": record_id, "previous_pause": pause, "run_id": run_id}
        updated = dict(run, route_pauses=pauses, route_recovery_ref=record_id, revision=run.get("revision", 0) + 1)
        memory.commit(record_id, [change("source_checks", record_id, record),
                                  change("batch_runs", run_key, updated, run)])
        return record_id


def bind_source_relevance(memory, task_id, target_results, *, as_of=None):
    """Attach accepted source-specific interpretation, never recollect or vote.

    The caller owns model/reference validation. This function binds exact saved
    capture/observation/receipt references and the current product description.
    A result can say relevant, no relevant matches, unclear or insufficient;
    mere presence of a search result is not a demand or supplier confirmation.
    """
    identifier(task_id)
    at = as_of or utc_now()
    timestamp(at)
    state = memory.snapshot()
    task = state["query_tasks"].get(task_id)
    if not task or task.get("kind") != TASK_KIND or task["status"] != "acquired":
        raise SafetyError("SOURCE_SAVED_ACQUISITION_REQUIRED")
    available = set()
    for ref in task["receipt_refs"]:
        envelope = state["source_checks"][ref]["envelope"]
        available.update(envelope["capture_ids"] + envelope["observation_ids"] + envelope["evidence_refs"] + [ref])
    if not isinstance(target_results, list) or not target_results:
        raise SafetyError("SOURCE_RELEVANCE_INVALID")
    results = deepcopy(task["target_results"])
    for row in target_results:
        concept_id = row.get("concept_id") if isinstance(row, dict) else None
        if (concept_id not in task["targets"] or concept_id not in state["concepts"]
                or row.get("description_hash") != _scope(state, concept_id)
                or row.get("state") not in {"completed_relevant", "completed_no_results", "interpretation_pending", "insufficient"}
                or not isinstance(row.get("evidence_refs"), list) or not row["evidence_refs"]
                or not set(row["evidence_refs"]) <= available):
            raise SafetyError("SOURCE_RELEVANCE_BINDING_INVALID")
        results[concept_id] = {field: deepcopy(row[field]) for field in ("description_hash", "state", "evidence_refs", "reason")}
        _reason(row["reason"])
    record_id = key("source-relevance", [task_id, target_results])
    record = {"kind": RELEVANCE_KIND, "source": task["source"], "route": task["route"],
              "at": at, "state": "reviewed", "reason": "Accepted interpretation of the existing source acquisition.",
              "reference": record_id, "task_id": task_id, "results": deepcopy(target_results),
              "receipt_refs": task["receipt_refs"]}
    if record_id in state["source_checks"]:
        if state["source_checks"][record_id] != record:
            # The interpretation's original date is part of its receipt.
            old = state["source_checks"][record_id]
            if dict(record, at=old["at"]) != old:
                raise SafetyError("SOURCE_RELEVANCE_CONFLICT")
        return record_id
    targets = dict(task["targets"])
    targets.update({row["concept_id"]: row["description_hash"] for row in target_results})
    updated = dict(task, target_results=results, targets=targets, revision=task.get("revision", 0) + 1)
    def current(actual):
        for row in target_results:
            if _scope(actual, row["concept_id"]) != row["description_hash"]:
                raise SafetyError("SOURCE_TARGET_DESCRIPTION_CHANGED")
    memory.commit(record_id, [change("source_checks", record_id, record),
                              change("query_tasks", task_id, updated, task)], validate=current)
    return record_id


def _legacy_coverage(state, ids, as_of):
    """Use bound actual evidence, never source-wide route-availability checks."""
    from .research_flow import current_assessment_fit, evidence_indexes, trends_description_current
    from .discovery import latest_observations
    from .market_validation import REVIEW_VERSION
    indexes = evidence_indexes(state)
    current_observations = latest_observations(state["observations"], as_of, max_age_days=None)
    rows = {}
    from .supplier_effort import (VERSION as SUPPLIER_EFFORT_VERSION, SUPPLIER_ROUTE_ALIASES as supplier_routes,
                                  _validated_record, supplier_scope)
    supplier_rows = {}
    for ref, record in state["source_checks"].items():
        if record.get("kind") != SUPPLIER_EFFORT_VERSION or record.get("concept_id") not in ids:
            continue
        concept_id = record["concept_id"]
        route = supplier_routes.get(record.get("route"), record.get("route"))
        if route not in ROUTES or ROUTES[route]["source"] != "supplier":
            continue
        _validated_record(record)
        if (record["sourcing_scope"] != supplier_scope(concept_id, state["concepts"][concept_id]["description"])
                or timestamp(record["at"]) > timestamp(as_of)):
            continue
        outcomes = {"completed_empty": "completed_no_results", "usable_leads": "completed_relevant",
                    "blocked": "blocked", "failed": "failed", "partial": "insufficient",
                    "interpretation_pending": "interpretation_pending", "prelaunch_failed": "not_attempted"}
        candidate = {"state": outcomes[record["internal_outcome"]], "at": record["at"],
                     "actual_attempt": record["effort_satisfied"], "evidence_refs": [ref],
                     "reason": "Actual supplier effort is saved; its original result is " + record["internal_outcome"].replace("_", " ") + "."}
        slot = (concept_id, route)
        old = supplier_rows.get(slot)
        if old is None or timestamp(candidate["at"]) > timestamp(old["at"]):
            supplier_rows[slot] = candidate
    rows.update(supplier_rows)
    for concept_id in ids:
        relevance = {}
        assessment = state["assessments"].get(concept_id)
        if assessment and current_assessment_fit(state, concept_id, indexes=indexes) is not None:
            relevance.update({state["observations"][r["id"]]["semantic_id"]: r["relationship"]
                              for r in assessment["result"].get("relevance", [])})
        scoped_reviews = [review for review in state["market_reviews"].values()
                          if review.get("concept_id") == concept_id
                          and review.get("description_hash") == _scope(state, concept_id)
                          and timestamp(review["reviewed_at"]) <= timestamp(as_of)]
        # A retired interpretation still records which actual acquisition was
        # examined for this product. It may prove work happened, not a current
        # accepted match or an encouraging demand judgment.
        captured_scope = {state["observations"][row["id"]]["semantic_id"]
                          for review in scoped_reviews for row in review.get("relevance", [])
                          if row["id"] in state["observations"]}
        reviews = sorted((review for review in scoped_reviews if review.get("review_version") == REVIEW_VERSION),
                         key=lambda review: (timestamp(review["reviewed_at"]), review["input_hash"]))
        for review in reviews:
            for result in review.get("relevance", []):
                observation = state["observations"].get(result["id"])
                if observation is not None and timestamp(observation["observed_at"]) <= timestamp(as_of):
                    relevance[observation["semantic_id"]] = result["relationship"]
        semantic_ids = indexes[0].get(concept_id, set())
        scoped_items = {(row["source"], row["source_item"]) for row in state["observations"].values()
                        if row.get("semantic_id") in semantic_ids or row.get("semantic_id") in relevance
                        or row.get("semantic_id") in captured_scope}
        observations = {i: row for i, row in current_observations.items()
                        if row.get("semantic_id") in semantic_ids or row.get("semantic_id") in relevance
                        or row.get("semantic_id") in captured_scope
                        or (row["source"], row["source_item"]) in scoped_items}
        for obs_id, observation in observations.items():
            capture = state["captures"].get(observation.get("capture_id"))
            if not capture or timestamp(observation["observed_at"]) > timestamp(as_of):
                continue
            reference = capture.get("reference", {})
            route = reference.get("route") if isinstance(reference, dict) else capture.get("route")
            if route not in ROUTES:
                if observation["source"] == "meta":
                    route = "meta-ads"
                elif observation["source"] in {"amazon", "flipkart"}:
                    kind = observation.get("facts", {}).get("list_type")
                    route = ({"bestsellers": "amazon-bestsellers", "movers-and-shakers": "amazon-movers-and-shakers",
                              "product-search": "amazon-search" if observation["source"] == "amazon" else "flipkart-search",
                              "product_search": "amazon-search" if observation["source"] == "amazon" else "flipkart-search"}).get(kind)
                elif observation["source"] in {"blinkit", "zepto", "instamart"}:
                    route = observation["source"]
            if route not in ROUTES:
                continue
            relationship = relevance.get(observation["semantic_id"])
            status = ("completed_relevant" if relationship in _RELATIONSHIPS else
                      "completed_no_results" if relationship == "unrelated" else
                      "insufficient" if relationship == "unclear" else "interpretation_pending")
            candidate = {"state": status, "at": observation["observed_at"], "actual_attempt": True,
                         "reason": "Existing source capture has bound product relevance." if status == "completed_relevant" else
                                   "The reviewed source example does not match this product type." if status == "completed_no_results" else
                                   "The source example was reviewed, but the product match remains unclear." if status == "insufficient" else
                                   "Existing source capture still needs current product relevance interpretation.",
                         "evidence_refs": [obs_id, observation["capture_id"]]}
            slot = (concept_id, route)
            old = rows.get(slot)
            if (old is None or status == "completed_relevant" and old["state"] != status
                    or old["state"] != "completed_relevant" and timestamp(candidate["at"]) > timestamp(old["at"])
                    or status == old["state"] and timestamp(candidate["at"]) > timestamp(old["at"])):
                rows[slot] = candidate
        for evidence_id, evidence in state["trends_evidence"].items():
            if (evidence.get("concept_id") == concept_id and evidence.get("parsed", {}).get("state") == "Usable"
                    and trends_description_current(state, concept_id, evidence, as_of=as_of)):
                parsed = evidence["parsed"]
                at = parsed.get("collected_at", parsed.get("observed_at"))
                if at is None:
                    at = parsed.get("metadata", {}).get("collected_at")
                if at is not None and timestamp(at) <= timestamp(as_of):
                    rows[(concept_id, "google-trends")] = {"state": "completed_relevant", "at": at,
                        "actual_attempt": True, "reason": "Existing India chart has current bound product scope.",
                        "evidence_refs": [evidence_id]}
    return rows


def _functional_coverage(state, ids, as_of):
    """Meaning-bound interpretation overlay; original task targets stay exact."""
    from .source_interpretation import FUNCTIONAL_MATCH_METHOD, functional_source_questions
    from .research_scope import validated_scoped_evidence
    from .market_validation import _functional_match
    cohorts = functional_source_questions(state, as_of=as_of, concept_ids=ids)
    rows, by_route = {}, {}
    for cohort in cohorts:
        by_route.setdefault((cohort["concept_id"], cohort["route"]), []).append(cohort)
    for cid in ids:
        scoped = validated_scoped_evidence(state, cid, as_of=as_of)
        for route in ROUTES:
            questions = by_route.get((cid, route), [])
            if not questions:
                continue
            evidence = {ref for question in questions for ref in question["observation_ids"]}
            pending = {ref for question in questions for ref in question["unknown_ids"]}
            matching = [row for row in scoped if row["method"] == FUNCTIONAL_MATCH_METHOD and row["route"] == route
                        and any(ref["id"] in evidence and ref["table"] in ("observations", "trends_evidence")
                                and _functional_match(row, ref["id"])
                                for ref in row["references"])]
            narrow = [row for row in matching if row["scope"] == "narrow_type" and row["admissibility"] == "accepted"]
            status = ("completed_relevant" if narrow else "interpretation_pending" if pending else
                      "completed_no_results" if matching and all(row["relationship"] == "unrelated" for row in matching)
                      else "insufficient")
            original_dates = [source_evidence["observed_at"] for row in matching for source_evidence in row["source_contexts"]]
            if not original_dates:
                from .market_validation import source_evidence
                original_dates = [source_evidence(state, ref)[1] for ref in evidence]
            rows[(cid, route)] = {"state": status, "actual_attempt": True,
                "at": max(original_dates, key=timestamp), "evidence_refs": sorted(evidence),
                "scoped_evidence_refs": sorted(row["id"] for row in matching),
                "meaning_hash": questions[0]["meaning_hash"],
                "interpretation_state": "pending" if pending else "accepted",
                "captured_observations": len(evidence), "reviewed_observations": len(evidence - pending),
                "remaining_observations": len(pending),
                "reason": ("Saved source rows have a reviewed narrow-type match; remaining rows continue independently."
                    if narrow else "Saved source rows still need this accepted type's own scope review." if pending else
                    "The saved rows were reviewed; none matched this narrow type." if status == "completed_no_results" else
                    "The saved rows were reviewed, but supply only broader/example context or an unclear match.")}
    return rows


def source_coverage(memory, *, handlers=None, concept_ids=None, as_of=None, state=None, include_staged_progress=True):
    """Read all product/route rows, with genuine attempted versus untried counts."""
    if type(include_staged_progress) is not bool:
        raise SafetyError("SOURCE_COVERAGE_OPTIONS_INVALID")
    at = as_of or utc_now()
    timestamp(at)
    state = memory.snapshot() if state is None else state
    ids = _active_concepts(state, concept_ids)
    registry = route_registry(handlers)
    contexts, invalid = {}, {}
    for concept_id in ids:
        try:
            contexts[concept_id] = _product_context(state, concept_id)
        except SafetyError as error:
            if str(error) in _INTEGRITY_ERRORS:
                raise
            invalid[concept_id] = str(error)
    from .functional_types import functional_role_context
    functional_ids = {cid for cid in contexts if functional_role_context(state, cid, role="C") is not None}
    legacy = _legacy_coverage(state, [cid for cid in contexts if cid not in functional_ids], at)
    # Supplier work status is not a market vote. Keep its existing source-bound
    # effort/relevance projection while the supplier-scope producer is migrated
    # separately; accepted definitions must not erase completed real effort.
    legacy.update({slot: value for slot, value in _legacy_coverage(state, sorted(functional_ids), at).items()
                   if ROUTES[slot[1]]["source"] == "supplier"})
    functional = _functional_coverage(state, sorted(functional_ids), at)
    tasks = [dict(task, task_id=task_id) for task_id, task in state["query_tasks"].items() if task.get("kind") == TASK_KIND]
    applicability = [row for row in state["source_checks"].values() if row.get("kind") == "all_source_applicability_v1"]
    pending = [gap for run in state["batch_runs"].values() if run.get("kind") == RUN_KIND for gap in run.get("pending_preparation", [])]
    pauses = {task_id: pause for run_key, run in state["batch_runs"].items() if run.get("kind") == RUN_KIND
              for route, pause in (_effective_route_pauses(memory, state, run_key, run)
                                   if include_staged_progress else run.get("route_pauses", {})).items()
              for task_id in run["task_ids"] if state["query_tasks"][task_id]["route"] == route}
    rows = []
    for concept_id in ids:
        if concept_id in invalid:
            rows.extend({"concept_id": concept_id, "label": concept_id, "route": route, **ROUTES[route],
                "description_hash": _scope(state, concept_id), "state": "not_attempted", "actual_attempt": False,
                "evidence_refs": [], "task_ids": [], "reason": invalid[concept_id], "at": None} for route in ROUTES)
            continue
        description, excluded = contexts[concept_id]
        scope = _scope(state, concept_id)
        for route in ROUTES:
            row = {"concept_id": concept_id, "label": description["label"], "route": route, **ROUTES[route],
                   "description_hash": scope, "state": "not_attempted", "actual_attempt": False,
                   "evidence_refs": [], "task_ids": [], "reason": "No actual product check is saved for this route.", "at": None}
            explicit = [r for r in applicability if r.get("concept_id") == concept_id and r.get("route") == route and r.get("description_hash") == scope]
            candidates = [t for t in tasks if t["route"] == route and t["targets"].get(concept_id) == scope
                          and not _target_superseded(state, t, concept_id)]
            latest_applicability = max(explicit, key=lambda r: timestamp(r["at"])) if explicit else None
            newer_question = latest_applicability is not None and any(
                timestamp(task["prepared_at"]) > timestamp(latest_applicability["at"]) for task in candidates)
            if excluded or (explicit and not newer_question and concept_id not in functional_ids):
                row.update(state="not_applicable", reason=max(explicit, key=lambda r: timestamp(r["at"]))["reason"] if explicit else
                           "Known established brand or excluded working identity.")
                rows.append(row)
                continue
            if (concept_id, route) in legacy:
                row.update(legacy[(concept_id, route)])
            if (concept_id, route) in functional:
                row.update(functional[(concept_id, route)])
            row["task_ids"] = [t["task_id"] for t in candidates]
            for task in sorted(candidates, key=lambda t: (t.get("dispatched_at", t["prepared_at"]), t["task_id"])):
                target = task["target_results"].get(concept_id)
                proof = bool(task["attempt_refs"])
                physical_only = bool(target and target.get("state") in {"completed_no_results", "blocked", "failed"}
                                     and not any(state["source_checks"][ref].get("envelope", {}).get("observation_ids")
                                                 for ref in task.get("receipt_refs", [])))
                if (target and target.get("description_hash") == scope and
                        (concept_id not in functional_ids or ROUTES[route]["source"] == "supplier" or physical_only)):
                    candidate = dict(target, actual_attempt=proof,
                                     at=(state["source_checks"][task["receipt_refs"][-1]]["at"] if task["receipt_refs"] else None))
                    if row["state"] != "completed_relevant" or candidate["state"] == "completed_relevant":
                        row.update(candidate)
                elif task.get("reconcile_required"):
                    if row["state"] != "completed_relevant":
                        row.update(state="in_progress" if task["status"] == "dispatching" else
                                   "failed" if task["status"] == "failed" else "blocked",
                                   actual_attempt=proof, reason=task["reason"], at=task.get("dispatched_at"))
                elif task["task_id"] in pauses and row["state"] == "not_attempted":
                    pause = pauses[task["task_id"]]
                    row.update(state="blocked", actual_attempt=False, blocked_by=pause["receipt_id"],
                               reason="This product has not been sent to the route; the route is waiting after a shared failure.")
            # A completed cached acquisition joined later needs its own bound
            # interpretation. Never silently reuse another product's verdict.
            if candidates and any(t["status"] == "acquired" for t in candidates) and row["state"] == "not_attempted":
                row.update(state="interpretation_pending", reason="Shared source acquisition is saved; this target needs its own relevance check.")
            if row["state"] == "not_attempted":
                gaps = [p for p in pending if p["concept_id"] == concept_id and p["route"] == route and p["description_hash"] == scope]
                if gaps:
                    row["reason"] = gaps[-1]["reason"]
                if route in RETIRED_SUPPLIER_ROUTES:
                    # Fail closed: not pending and not a failed check, just
                    # unavailable. Saved outcomes above remain as history.
                    row.update(state="not_connected", reason=RETIRED_SUPPLIER_ROUTES[route])
                elif not registry[route]["connected"]:
                    row.update(state="not_connected", reason="This route has no installed acquisition handler in this run.")
            rows.append(row)
    counts = dict(Counter(row["state"] for row in rows))
    return {"schema_version": 1, "as_of": at, "products": len(ids), "routes": len(ROUTES),
            "product_route_questions": len(rows), "states": counts,
            "actually_attempted_product_routes": sum(row["actual_attempt"] for row in rows),
            "physical_attempts": len({ref for task in tasks for ref in task["attempt_refs"]}),
            "controller_acquisition_attempts": len({ref for task in tasks for ref in task["attempt_refs"]}),
            "rows": rows, "client_report": False,
            "note": "Coverage is work status, not an independent source vote or a product-failure verdict."}
