"""Inactive, local physical-read ownership in the existing research journal.

Ownership is not financial admission, callback admission or permission to launch.
No function here invokes a source, a parser, a model or a process. Adapters must
still perform the existing admission and immediate-before-launch checks. Saved
responses and local parse receipts can be recovered without a new acquisition.

A saved read whose recorded files are proven LOST (an interrupted write left
them empty or only zero bytes, ``_damaged_files``) is set aside once, for good
and with its proof (``set_aside_damaged_acquisition``); the next claim of that
request then reserves one linked next generation, an ordinary inert
reservation that still needs every admission, budget, pacing and access wait.
Any other change to recorded bytes still refuses reuse and recollection.
"""

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from hashlib import sha256
import os
from pathlib import Path
import re
import stat as stat_module

from .models import SafetyError, decode_json, digest, identifier, timestamp
from .research_memory import change, key
from .research_questions import parse_fingerprint, physical_request_fingerprint
from .storage import safe_path


ACQUISITION_KIND = "source-acquisition-owner-v1"
STAGE_KIND = "source-acquisition-stage-v1"
DEFAULT_FRESHNESS_SECONDS = 7 * 24 * 60 * 60
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}")
_STATES = frozenset(("reserved", "dispatching", "response_saved", "parsed", "completed_empty",
                     "failed_access", "execution_unknown", "set_aside"))
DAMAGED_REASON = "SOURCE_ACQUISITION_ARTIFACT_DAMAGED"
_CHANNELS = frozenset(("direct_local", "funded_provider", "unknown"))
_FEES = frozenset(("known_zero", "reserved_paid", "unknown"))
_READINESS_OWNERS = ContextVar("source_readiness_owners", default=None)
_RESPONSE_NAMES = frozenset(("snapshot.json", "response.json", "raw_response.json", "rendered.html",
                             "response.html", "body.bin"))
CAPTURE_SOURCES = {"meta-ads": "meta", "amazon-bestsellers": "amazon",
    "amazon-search": "amazon", "flipkart-search": "flipkart", "google-trends": "trends",
    "blinkit": "blinkit", "zepto": "zepto", "instamart": "instamart", "google-suppliers": "supplier",
    "google-maps": "supplier", "indiamart": "supplier", "tradeindia": "supplier", "company-pages": "supplier",
    "deodap": "deodap", "baapstore": "baapstore", "dropdash": "dropdash",
    "meta-ads-depth": "meta"}


def _text(value, code="SOURCE_ACQUISITION_TEXT_INVALID"):
    if (not isinstance(value, str) or not value.strip() or len(value) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise SafetyError(code)
    return value


def _date(value):
    timestamp(value)
    return value


def _record(state, acquisition_id):
    row = state["batch_runs"].get(identifier(acquisition_id))
    if not isinstance(row, dict) or row.get("kind") != ACQUISITION_KIND:
        raise SafetyError("SOURCE_ACQUISITION_REQUIRED")
    try:
        binding = row.get("binding")
        if (not isinstance(binding, dict) or binding.get("contract") != ACQUISITION_KIND
            or digest(binding) != row.get("binding_hash")
            or binding.get("acquisition_id") != acquisition_id
            or row.get("state") not in _STATES
            or type(binding.get("generation")) is not int or binding["generation"] < 1
            or physical_request_fingerprint(binding["route"], binding["request"], method=binding["method"],
                 capture_options=binding["capture_options"], history=True)
               != binding.get("physical_request_fingerprint")):
            raise SafetyError("SOURCE_ACQUISITION_RECORD_INVALID")
        identifier(binding["owner_id"]); identifier(binding["original_attempt_id"])
        _date(row["created_at"]); _date(row["updated_at"])
        _roots(binding["artifact_roots"])
        for field in ("stage_ids", "response_ids", "parse_ids"):
            if (not isinstance(row.get(field), list)
                    or any(not isinstance(item, str) for item in row[field])
                    or len(set(row[field])) != len(row[field])):
                raise SafetyError("SOURCE_ACQUISITION_RECORD_INVALID")
        if (not set(row["response_ids"]) <= set(row["stage_ids"])
                or not set(row["parse_ids"]) <= set(row["stage_ids"])
                or row.get("active_response_id") is not None and row["active_response_id"] not in row["response_ids"]
                or row.get("active_parse_id") is not None and row["active_parse_id"] not in row["parse_ids"]):
            raise SafetyError("SOURCE_ACQUISITION_RECORD_INVALID")
        if (row.get("active_dispatch_intent_id") is not None
                and row["active_dispatch_intent_id"] not in row["stage_ids"]):
            raise SafetyError("SOURCE_ACQUISITION_RECORD_INVALID")
    except (KeyError, TypeError, ValueError):
        raise SafetyError("SOURCE_ACQUISITION_RECORD_INVALID") from None
    return row


@contextmanager
def source_readiness_owner_scope(state):
    """Share validated owners only within one pure, point-in-time decision.

    The caller keeps this snapshot unchanged. This is neither a persisted index
    nor launch/admission proof. Mutation controllers read a different fresh
    snapshot and therefore cannot borrow this advisory lookup.
    """
    token = _READINESS_OWNERS.set({"state": state, "index": None})
    try:
        yield
    finally:
        _READINESS_OWNERS.reset(token)


def _owners(state, fingerprint):
    scope = _READINESS_OWNERS.get()
    scoped = scope is not None and scope["state"] is state
    index = scope["index"] if scoped else None
    if index is None:
        from .source_coverage import CLOSED_ROUTES  # Lazy: the coverage module imports this one.
        index = {}
        for record_id, value in state["batch_runs"].items():
            if value.get("kind") != ACQUISITION_KIND:
                continue
            # Validate every actual request before trusting its lookup key.
            # A damaged fingerprint cannot hide an original owner. In a pure
            # readiness scope, do this once, not once per queued search.
            verified = _record(state, record_id)
            if verified["binding"]["route"] in CLOSED_ROUTES:
                continue  # Closed history: verified, but never an owner to join or reuse.
            index.setdefault(verified["binding"]["physical_request_fingerprint"], []).append((record_id, verified))
        if scoped:
            scope["index"] = index
    rows = list(index.get(fingerprint, []))
    rows.sort(key=lambda item: item[1]["binding"]["generation"])
    previous = None
    for generation, (record_id, row) in enumerate(rows, 1):
        if (row["binding"]["generation"] != generation
                or row["binding"]["previous_acquisition_id"] != previous):
            raise SafetyError("SOURCE_ACQUISITION_CHAIN_INVALID")
        previous = record_id
    return rows


def _signature(rows):
    return digest([[record_id, digest(row)] for record_id, row in rows])


def _roots(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 8:
        raise SafetyError("SOURCE_ACQUISITION_ROOTS_INVALID")
    result = []
    for value in values:
        if not isinstance(value, (str, Path)):
            raise SafetyError("SOURCE_ACQUISITION_ROOTS_INVALID")
        path = safe_path(value)
        if not path.is_absolute() or len(path.parts) < 3 or ".." in path.parts:
            raise SafetyError("SOURCE_ACQUISITION_ROOTS_INVALID")
        result.append(str(path))
    return sorted(set(result))


def _file(path, roots):
    # Original bytes are read outside the short commit. An unchanged descriptor
    # fingerprint is then rechecked while committing their hash-bound reference.
    path = safe_path(path)
    if ".." in path.parts or not any(path.is_relative_to(Path(root)) for root in roots):
        raise SafetyError("SOURCE_ACQUISITION_FILE_SCOPE_INVALID")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        before = os.fstat(descriptor)
        if not stat_module.S_ISREG(before.st_mode) or before.st_size > MAX_ARTIFACT_BYTES:
            raise SafetyError("SOURCE_ACQUISITION_FILE_INVALID")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise SafetyError("SOURCE_ACQUISITION_FILE_INVALID")
    except OSError:
        raise SafetyError("SOURCE_ACQUISITION_FILE_UNAVAILABLE") from None
    finally:
        if descriptor is not None: os.close(descriptor)
    fingerprint = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    if fingerprint != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise SafetyError("SOURCE_ACQUISITION_FILE_CHANGED")
    return {"path": str(path), "sha256": sha256(raw).hexdigest()}, fingerprint, raw


def _files_current(files):
    for proof, expected in files:
        try:
            path = safe_path(proof["path"])
            stat = path.stat()
        except OSError:
            raise SafetyError("SOURCE_ACQUISITION_FILE_UNAVAILABLE") from None
        actual = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if actual != expected:
            raise SafetyError("SOURCE_ACQUISITION_FILE_CHANGED")


def _original_receipt(memory, row, receipt_path, as_of):
    proof, fingerprint, raw = _file(receipt_path, row["binding"]["artifact_roots"])
    receipt = decode_json(raw)
    binding = row["binding"]
    if not isinstance(receipt, dict):
        raise SafetyError("SOURCE_ACQUISITION_RECEIPT_SCOPE_INVALID")
    if (receipt.get("route") != binding["route"]
            or receipt.get("acquisition_id") != binding["acquisition_id"]
            or physical_request_fingerprint(receipt["route"], receipt.get("request"),
                 method=receipt.get("method", "GET"), capture_options=receipt.get("capture_options", {}))
               != binding["physical_request_fingerprint"]
            or receipt.get("dispatch_started") not in (True, False, None)
            or "dispatch_started" not in receipt
            or (receipt.get("dispatch_started") is not None and type(receipt["dispatch_started"]) is not bool)):
        raise SafetyError("SOURCE_ACQUISITION_RECEIPT_SCOPE_INVALID")
    observed = receipt.get("observed_at", receipt.get("completed_at"))
    if timestamp(observed) > timestamp(as_of):
        raise SafetyError("SOURCE_ACQUISITION_SOURCE_DATE_INVALID")
    if receipt.get("attempt_id", binding["original_attempt_id"]) != binding["original_attempt_id"]:
        raise SafetyError("SOURCE_ACQUISITION_ATTEMPT_INVALID")
    files, artifacts = [(proof, fingerprint)], {}
    manifest = receipt.get("artifacts", {})
    if not isinstance(manifest, dict) or len(manifest) > 40:
        raise SafetyError("SOURCE_ACQUISITION_ARTIFACTS_INVALID")
    for name, expected in manifest.items():
        if (not isinstance(name, str) or Path(name).name != name or name in ("", ".", "..")
                or not isinstance(expected, str) or not _HEX.fullmatch(expected)):
            raise SafetyError("SOURCE_ACQUISITION_ARTIFACTS_INVALID")
        artifact, stat, _ = _file(Path(proof["path"]).parent / name, binding["artifact_roots"])
        if artifact["sha256"] != expected:
            raise SafetyError("SOURCE_ACQUISITION_ARTIFACT_CHANGED")
        files.append((artifact, stat)); artifacts[name] = artifact
    return receipt, proof, artifacts, files, observed


def _stage(state, row, stage_id):
    stage = state["source_checks"].get(stage_id)
    if (not isinstance(stage, dict) or stage.get("kind") != STAGE_KIND
            or stage.get("acquisition_id") != row["binding"]["acquisition_id"]
            or stage.get("binding_hash") != row["binding_hash"]
            or stage_id not in row["stage_ids"]
            or key("source-acquisition-stage", {k: v for k, v in stage.items() if k not in {"record_hash", "recorded_at"}}) != stage_id
            or digest({k: v for k, v in stage.items() if k != "record_hash"}) != stage.get("record_hash")):
        raise SafetyError("SOURCE_ACQUISITION_STAGE_INVALID")
    return stage


def _response(state, row, *, files_out=None):
    if not row.get("active_response_id"):
        return None
    value = _stage(state, row, row["active_response_id"])
    if value.get("stage_type") != "response" or row["active_response_id"] not in row["response_ids"]:
        raise SafetyError("SOURCE_ACQUISITION_STAGE_INVALID")
    # Local metadata touch does not refresh the original collection date.
    for proof in [value["receipt"], *value["artifacts"].values()]:
        current, fingerprint, _ = _file(proof["path"], row["binding"]["artifact_roots"])
        if current != proof:
            raise SafetyError("SOURCE_ACQUISITION_ARTIFACT_CHANGED")
        if files_out is not None: files_out.append((current, fingerprint))
    for reference in value["captures"]:
        if digest(state["captures"].get(reference["id"])) != reference["hash"]:
            raise SafetyError("SOURCE_ACQUISITION_CAPTURE_CHANGED")
    return value


def _damaged_files(row, response):
    """The recorded files of one saved response that are proven LOST, never changed.

    A write cut short by a power cut or forced shutdown can leave a file whose
    recorded bytes never reached the disk: it is now empty, or holds only zero
    bytes. That is the only damage accepted here. Any other difference is a
    real content change (a person, an agent or a bug edited saved evidence):
    it raises ``SOURCE_ACQUISITION_ARTIFACT_CHANGED`` and is never set aside,
    so it can never be used to force a new read. A missing or unreadable file
    proves nothing (a mounted drive may be away) and raises as before.
    """
    roots = row["binding"]["artifact_roots"]
    damaged, files = [], []
    for role, name, proof in [("receipt", None, response["receipt"]),
                              *[("artifact", name, proof) for name, proof in sorted(response["artifacts"].items())]]:
        current, fingerprint, raw = _file(proof["path"], roots)
        if current == proof:
            continue
        if raw.strip(b"\x00"):
            raise SafetyError("SOURCE_ACQUISITION_ARTIFACT_CHANGED")
        if len(raw) != fingerprint[2]:
            # Fewer bytes than the file system says the file holds: an
            # unsettled read, not proof that the recorded bytes are gone.
            raise SafetyError("SOURCE_ACQUISITION_FILE_UNAVAILABLE")
        damaged.append({"role": role, "name": name, "path": proof["path"], "recorded_sha256": proof["sha256"],
                        "found_sha256": current["sha256"], "found_bytes": len(raw),
                        "damage": "empty" if not raw else "zero_filled"})
        files.append((current, fingerprint))
    return damaged, files


def _set_aside_stage(state, row):
    """The one set-aside stage of a set-aside owner (validated), else an error."""
    stages = [(stage_id, _stage(state, row, stage_id)) for stage_id in row["stage_ids"]]
    found = [(stage_id, stage) for stage_id, stage in stages if stage["stage_type"] == "set_aside"]
    if (row["state"] != "set_aside" or len(found) != 1 or found[0][0] != row["stage_ids"][-1]
            or row.get("active_response_id") is not None or found[0][1]["response_id"] not in row["response_ids"]):
        raise SafetyError("SOURCE_ACQUISITION_RECORD_INVALID")
    return found[0]


def set_aside_damaged_acquisition(memory, acquisition_id, *, as_of):
    """Set a saved read aside, for good, when its recorded files are proven lost.

    Only ``_damaged_files`` damage qualifies; a real content change still
    raises. Nothing is deleted or rewritten: the original response stage stays
    in the record, and this set-aside stage names every lost file with its
    recorded and found hash. The owner keeps its generation and is never
    trusted, replayed or given another stage again. ``claim_source_acquisition``
    may then reserve ONE linked next generation for the same request.
    """
    _date(as_of)
    state = memory.snapshot(); old = _record(state, acquisition_id)
    if old["state"] == "set_aside":
        _set_aside_stage(state, old)
        return _result("set_aside", acquisition_id, old)
    response_id = old.get("active_response_id")
    response = _stage(state, old, response_id) if response_id else None
    if not response or response.get("stage_type") != "response":
        raise SafetyError("SOURCE_ACQUISITION_DAMAGE_NOT_PROVEN")
    damaged, files = _damaged_files(old, response)
    if not damaged:
        raise SafetyError("SOURCE_ACQUISITION_DAMAGE_NOT_PROVEN")
    saved = _commit_stage(memory, old, "set_aside", {"reason": DAMAGED_REASON, "response_id": response_id,
            "response_hash": digest(response), "parse_id": old.get("active_parse_id"), "damaged_files": damaged},
        max(as_of, old["updated_at"], key=timestamp),
        {"state": "set_aside", "active_response_id": None, "active_parse_id": None, "parse_state": "not_started"},
        files=files, references=[{"table": "source_checks", "id": response_id, "hash": digest(response)}],
        guard=lambda *_: None)
    return dict(saved, action="set_aside")


def damage_replacement_allowed(row):
    """One fresh read may replace a lost saved read, not a lost replacement.

    A replacement that is itself lost again stops here
    (``SOURCE_ACQUISITION_DAMAGE_REPEATED``): repeated loss means something
    keeps damaging saved files, and re-reading every round would hide it.
    """
    return row["binding"].get("damage_recovery") is None


def saved_response_or_replacement(state, owner):
    """``(response, replace)`` for pure readiness and planning reads.

    ``replace`` is True when the next claim of this request will reserve a
    fresh read in place of a saved read that is set aside, or whose recorded
    files are proven lost (``_damaged_files``); its response is then None.
    Reads files like ``_response`` and writes nothing. A real content change
    still raises ``SOURCE_ACQUISITION_ARTIFACT_CHANGED``, exactly as before.
    """
    if owner is None:
        return None, False
    if owner["state"] == "set_aside":
        return None, damage_replacement_allowed(owner)
    try:
        return _response(state, owner), False
    except SafetyError as error:
        if str(error) != "SOURCE_ACQUISITION_ARTIFACT_CHANGED":
            raise
    _damaged_files(owner, _stage(state, owner, owner["active_response_id"]))
    return None, damage_replacement_allowed(owner)


def _owner_enabled(memory):
    from .programme import programme_service_state
    return programme_service_state(memory)["enabled"]


def _guard(memory, state, binding, source_guard):
    if not _owner_enabled(memory):
        raise SafetyError("PROGRAMME_OWNER_PAUSED")
    # An adapter can apply the actual account/session/source wait here. This
    # hook must be local and read-only. It is NOT permission or fee admission.
    if source_guard is not None:
        source_guard(state, deepcopy(binding))


def _result(action, acquisition_id=None, row=None):
    return {"action": action, "acquisition_id": acquisition_id,
            "generation": row["binding"]["generation"] if row else None,
            "owner_id": row["binding"]["owner_id"] if row else None,
            "original_attempt_id": row["binding"]["original_attempt_id"] if row else None,
            "state": row["state"] if row else None, "launch_authority": False,
            "response_id": row.get("active_response_id") if row else None}


def claim_source_acquisition(memory, route, request, *, owner_id, as_of, method="GET", capture_options=None,
                             freshness_seconds=DEFAULT_FRESHNESS_SECONDS, refresh_due_at=None,
                             refresh_reason=None, original_acquisition_id=None, original_attempt_id=None,
                             collection_channel="unknown", incremental_collection_fee="unknown",
                             origin_account_key=None, source_guard=None, artifact_roots=(),
                             access_recovery=None, image_recovery=None):
    """Reserve at most one global origin, reuse fresh bytes, or join its owner.

    New claims are inert ownership reservations. A joined/replayed claim is not
    an invocation voucher. Another callback requires its own fresh turn slot.
    Legacy intent adoption is deliberately not inferred here: its later bridge
    must inspect real legacy execution proof and install the original ID/state.

    A previous saved read whose recorded files are proven lost is set aside
    first (``set_aside_damaged_acquisition``); a set-aside previous owner then
    gets one linked next generation, bound to that set-aside proof
    (``binding["damage_recovery"]``), instead of a join or a reuse. That is an
    ordinary reservation: no admission, callback slot, pacing, access wait or
    route pause is skipped. A lost replacement is not replaced again.
    """
    identifier(owner_id); _date(as_of)
    if type(freshness_seconds) is not int or not 0 <= freshness_seconds <= 366 * 24 * 60 * 60:
        raise SafetyError("SOURCE_ACQUISITION_FRESHNESS_INVALID")
    if (not isinstance(collection_channel, str) or collection_channel not in _CHANNELS
            or not isinstance(incremental_collection_fee, str) or incremental_collection_fee not in _FEES
            or (source_guard is not None and not callable(source_guard))):
        raise SafetyError("SOURCE_ACQUISITION_ORIGIN_INVALID")
    if origin_account_key is not None: identifier(origin_account_key)
    if incremental_collection_fee == "reserved_paid" and (collection_channel != "funded_provider" or not origin_account_key):
        raise SafetyError("SOURCE_ACQUISITION_PAID_ORIGIN_REQUIRED")
    if collection_channel == "funded_provider" and incremental_collection_fee == "known_zero":
        raise SafetyError("SOURCE_ACQUISITION_FEE_UNKNOWN")
    for value in (original_acquisition_id, original_attempt_id):
        if value is not None: identifier(value)
    if (refresh_due_at is None) != (refresh_reason is None):
        raise SafetyError("SOURCE_ACQUISITION_REFRESH_INVALID")
    if refresh_due_at is not None: _date(refresh_due_at); _text(refresh_reason)
    if access_recovery is not None and (refresh_due_at is not None or not isinstance(access_recovery, dict)):
        raise SafetyError("SOURCE_ACQUISITION_ACCESS_RECOVERY_INVALID")
    if image_recovery is not None and (route != "public_image_http" or access_recovery is not None
            or refresh_due_at is not None or not isinstance(image_recovery, dict)):
        raise SafetyError("SOURCE_IMAGE_RECOVERY_INVALID")
    capture_options = deepcopy({} if capture_options is None else capture_options)
    fingerprint = physical_request_fingerprint(route, request, method=method, capture_options=capture_options)
    if not isinstance(artifact_roots, (tuple, list)):
        raise SafetyError("SOURCE_ACQUISITION_ROOTS_INVALID")
    roots = _roots([memory.root, *artifact_roots])
    for _ in range(4):
        state = memory.snapshot(); owners = _owners(state, fingerprint)
        previous_id, previous = owners[-1] if owners else (None, None)
        generation = 1
        response, response_files = None, []
        damage_recovery = None
        if previous is not None and previous["state"] == "set_aside":
            # Its saved bytes are proven lost: never joined, reused or refreshed.
            if access_recovery is not None:
                raise SafetyError("SOURCE_ACQUISITION_ACCESS_RECOVERY_INVALID")
            if image_recovery is not None:
                raise SafetyError("SOURCE_IMAGE_RECOVERY_INVALID")
            set_aside_id, set_aside = _set_aside_stage(state, previous)
            if not damage_replacement_allowed(previous):
                raise SafetyError("SOURCE_ACQUISITION_DAMAGE_REPEATED")
            damage_recovery = {"acquisition_id": previous_id, "set_aside_id": set_aside_id,
                               "set_aside_hash": digest(set_aside), "reason": set_aside["reason"]}
            generation = previous["binding"]["generation"] + 1
        elif previous is not None:
            try:
                response = _response(state, previous, files_out=response_files)
            except SafetyError as error:
                if str(error) != "SOURCE_ACQUISITION_ARTIFACT_CHANGED":
                    raise
                # Proven lost bytes are set aside once (then the loop reserves
                # the replacement); a real content change raises here as before.
                try:
                    set_aside_damaged_acquisition(memory, previous_id, as_of=as_of)
                except SafetyError as conflict:
                    if str(conflict) != "SOURCE_ACQUISITION_CONFLICT": raise
                continue
            if (route == "public_image_http" and previous["binding"]["generation"] >= 3
                    and response and not response["complete"]):
                return _result("join", previous_id, previous)
            if response and response["complete"]:
                due = timestamp(response["observed_at"]) + timedelta(seconds=freshness_seconds)
                if timestamp(as_of) < due:
                    return _result("reuse", previous_id, previous)
                if refresh_due_at is None:
                    return _result("refresh_due", previous_id, previous)
                if timestamp(refresh_due_at) < due or timestamp(refresh_due_at) > timestamp(as_of):
                    raise SafetyError("SOURCE_ACQUISITION_REFRESH_NOT_DUE")
                generation = previous["binding"]["generation"] + 1
            elif access_recovery is not None:
                # A due recovery is not a parser retry or uncertain-origin
                # replacement. It names the actual closed access refusal and
                # retains that original as a previous immutable generation.
                from .source_coverage import source_access_wait, acquisition_access_scope, source_response_closed
                scope = acquisition_access_scope(previous, response)
                wait = source_access_wait(state, scope, as_of=as_of)
                expected = {field: wait[field] for field in
                    ("failure_id", "failure_hash", "response_id", "response_hash", "next_retry_at")} if wait else None
                if (not wait or wait["phase"] != "due" or access_recovery != expected
                        or wait["acquisition_id"] != previous_id or not response
                        or response["dispatch_started"] is not True
                        or not source_response_closed(response)
                        or previous["active_response_id"] != wait["response_id"]):
                    raise SafetyError("SOURCE_ACQUISITION_ACCESS_RECOVERY_INVALID")
                generation = previous["binding"]["generation"] + 1
            elif image_recovery is not None:
                expected = source_image_recovery(state, previous_id, as_of=as_of)
                if expected != image_recovery or expected is None:
                    raise SafetyError("SOURCE_IMAGE_RECOVERY_INVALID")
                generation = previous["binding"]["generation"] + 1
            else:
                # Incomplete, failed and uncertain originals remain original-
                # only. Consumer/parser changes never create a new attempt.
                return _result("join", previous_id, previous)
        elif refresh_due_at is not None or access_recovery is not None or image_recovery is not None:
            raise SafetyError("SOURCE_ACQUISITION_REFRESH_ORIGIN_REQUIRED")
        # A replacement's identity follows the request chain, never a caller's
        # proposed ID (a reconciling caller still names the set-aside owner).
        acquisition_id = (key("source-acquisition", [fingerprint, generation]) if damage_recovery
                          else original_acquisition_id or key("source-acquisition", [fingerprint, generation]))
        # Likewise its attempt: a proposed attempt ID (an original's, maybe a
        # paid one) never names the replacement's own new read.
        attempt_id = acquisition_id if damage_recovery else original_attempt_id or acquisition_id
        binding = {"contract": ACQUISITION_KIND, "route": route, "request": deepcopy(request),
            "method": method, "capture_options": capture_options, "physical_request_fingerprint": fingerprint,
            "acquisition_id": acquisition_id, "original_attempt_id": attempt_id, "owner_id": owner_id,
            "generation": generation, "previous_acquisition_id": previous_id,
            "collection_channel": collection_channel, "incremental_collection_fee": incremental_collection_fee,
            "origin_account_key": origin_account_key,
            "artifact_roots": roots,
            "refresh": ({"due_at": refresh_due_at, "reason": refresh_reason}
                        if refresh_due_at and not damage_recovery else None)}
        if access_recovery is not None:
            binding["access_recovery"] = deepcopy(access_recovery)
        if image_recovery is not None:
            binding["image_recovery"] = deepcopy(image_recovery)
        if damage_recovery is not None:
            binding["damage_recovery"] = damage_recovery
        row = {"kind": ACQUISITION_KIND, "binding": binding, "binding_hash": digest(binding),
            "state": "reserved", "created_at": as_of, "updated_at": as_of,
            "stage_ids": [], "response_ids": [], "parse_ids": [], "active_response_id": None,
            "active_parse_id": None, "active_dispatch_intent_id": None, "parse_state": "not_started"}
        expected = _signature(owners)
        def validate(current):
            if _signature(_owners(current, fingerprint)) != expected:
                raise SafetyError("SOURCE_ACQUISITION_CONFLICT")
            if acquisition_id in current["batch_runs"]:
                raise SafetyError("SOURCE_ACQUISITION_ID_OCCUPIED")
            if response is not None:
                if digest(current["source_checks"].get(previous["active_response_id"])) != digest(response):
                    raise SafetyError("SOURCE_ACQUISITION_STAGE_INVALID")
                _files_current(response_files)
                for reference in response["captures"]:
                    if digest(current["captures"].get(reference["id"])) != reference["hash"]:
                        raise SafetyError("SOURCE_ACQUISITION_CAPTURE_CHANGED")
            if access_recovery is not None:
                from .source_coverage import source_access_wait, acquisition_access_scope
                current_wait = source_access_wait(current, acquisition_access_scope(previous, response), as_of=as_of)
                if not current_wait or current_wait["phase"] != "due" or current_wait["failure_hash"] != access_recovery["failure_hash"]:
                    raise SafetyError("SOURCE_ACQUISITION_ACCESS_RECOVERY_INVALID")
            if image_recovery is not None and source_image_recovery(current, previous_id, as_of=as_of) != image_recovery:
                raise SafetyError("SOURCE_IMAGE_RECOVERY_INVALID")
            if (damage_recovery is not None and digest(current["source_checks"].get(damage_recovery["set_aside_id"]))
                    != damage_recovery["set_aside_hash"]):
                raise SafetyError("SOURCE_ACQUISITION_STAGE_INVALID")
            _guard(memory, current, binding, source_guard)
        _guard(memory, state, binding, source_guard)
        try:
            committed = memory.commit(key("source-acquisition-claim", binding),
                          [change("batch_runs", acquisition_id, row)], validate=validate)
            # A same-event contender may already own the identical reservation.
            return _result("reserved" if committed else "join", acquisition_id, _record(memory.snapshot(), acquisition_id))
        except SafetyError as error:
            if str(error) != "SOURCE_ACQUISITION_CONFLICT": raise
    raise SafetyError("SOURCE_ACQUISITION_CONFLICT")


def _commit_stage(memory, old, stage_type, data, as_of, updated, *, files=(), captures=(), references=(), guard=None,
                  photo_admission=False, joined_changes=()):
    acquisition_id = old["binding"]["acquisition_id"]
    if old["state"] == "set_aside":
        # Lost saved bytes are never re-bound, replayed or extended on this owner.
        raise SafetyError("SOURCE_ACQUISITION_SET_ASIDE")
    stage = {"kind": STAGE_KIND, "stage_type": stage_type, "acquisition_id": acquisition_id,
             "binding_hash": old["binding_hash"], **deepcopy(data)}
    stage_id = key("source-acquisition-stage", stage)
    state = memory.snapshot()
    if stage_id in state["source_checks"]:
        current = _record(state, acquisition_id)
        saved = _stage(state, current, stage_id)
        if {k: v for k, v in saved.items() if k not in {"record_hash", "recorded_at"}} != stage:
            raise SafetyError("SOURCE_ACQUISITION_STAGE_INVALID")
        return {"stage_id": stage_id, "reused": True, **_result("recovered", acquisition_id, current)}
    if timestamp(as_of) < timestamp(old["updated_at"]):
        raise SafetyError("SOURCE_ACQUISITION_TIME_REVERSED")
    stage["recorded_at"] = as_of; stage["record_hash"] = digest(stage)
    new = dict(old, **deepcopy(updated), updated_at=as_of, stage_ids=old["stage_ids"] + [stage_id])
    if stage_type == "response":
        new.update(response_ids=old["response_ids"] + [stage_id], active_response_id=stage_id)
    if stage_type == "parse":
        new.update(parse_ids=old["parse_ids"] + [stage_id], active_parse_id=stage_id)
    if stage_type == "dispatch_intent":
        new["active_dispatch_intent_id"] = stage_id
    def validate(current):
        if digest(_record(current, acquisition_id)) != digest(old):
            raise SafetyError("SOURCE_ACQUISITION_CONFLICT")
        _files_current(files)
        for reference in captures:
            if digest(current["captures"].get(reference["id"])) != reference["hash"]:
                raise SafetyError("SOURCE_ACQUISITION_CAPTURE_CHANGED")
        for reference in references:
            if digest(current[reference["table"]].get(reference["id"])) != reference["hash"]:
                raise SafetyError("SOURCE_ACQUISITION_DEPENDENCY_CHANGED")
        if guard: _guard(memory, current, old["binding"], guard)
    changes = [change("source_checks", stage_id, stage), change("batch_runs", acquisition_id, new, old), *joined_changes]
    from .work_turns import active_work_turn, reserve_stage_items
    if photo_admission and active_work_turn(memory) is not None:
        reserve_stage_items(memory, "photos", acquisition_id, 1, at=as_of, changes=changes, validate=validate)
    else:
        memory.commit(stage_id, changes, validate=validate)
    return {"stage_id": stage_id, "reused": False, **_result("saved", acquisition_id, new)}


def mark_source_dispatching(memory, acquisition_id, *, as_of, source_guard=None, process_ownership_token_hash=None,
                            photo_admission=False, joined_changes=()):
    """Save intent only, not proof of execution, admission or launch authority."""
    _date(as_of)
    state = memory.snapshot(); old = _record(state, acquisition_id)
    if type(photo_admission) is not bool or photo_admission and old["binding"]["route"] != "public_image_http":
        raise SafetyError("SOURCE_IMAGE_ADMISSION_INVALID")
    if old["state"] != "reserved": return _result("join", acquisition_id, old)
    _guard(memory, state, old["binding"], source_guard)
    sequence = sum(_stage(state, old, stage_id)["stage_type"] == "dispatch_intent" for stage_id in old["stage_ids"]) + 1
    token = process_ownership_token_hash or digest(["owned-source-process-v1", old["binding_hash"], sequence])
    if not isinstance(token, str) or not _HEX.fullmatch(token):
        raise SafetyError("SOURCE_ACQUISITION_PROCESS_TOKEN_INVALID")
    return _commit_stage(memory, old, "dispatch_intent", {"intent_sequence": sequence,
                         "process_ownership_token_hash": token}, as_of,
                         {"state": "dispatching"}, guard=source_guard or (lambda *_: None),
                         photo_admission=photo_admission, joined_changes=joined_changes)


def mark_source_execution_unknown(memory, acquisition_id, *, reason, as_of):
    _date(as_of); _text(reason)
    old = _record(memory.snapshot(), acquisition_id)
    if old["active_response_id"] or old["state"] in {"parsed", "completed_empty", "failed_access", "set_aside"}:
        raise SafetyError("SOURCE_ACQUISITION_TRANSITION_INVALID")
    return _commit_stage(memory, old, "execution_unknown", {"reason": reason}, as_of,
                         {"state": "execution_unknown"})


def _install_legacy_origin(memory, state, route, request, *, table, record_id, saved,
                           acquisition_id, owner_id, as_of, question_id=None,
                           artifact_roots=(), files=(), original_attempt_id=None, original_snapshot=False):
    """An actual retained caller is unknown execution, never a fresh intent."""
    fingerprint = physical_request_fingerprint(route, request)
    identifier(acquisition_id)
    if original_attempt_id is not None: identifier(original_attempt_id)
    if type(original_snapshot) is not bool: raise SafetyError("SOURCE_ACQUISITION_ORIGIN_INVALID")
    owners = _owners(state, fingerprint)
    if owners:
        return _result("join", *owners[-1])
    roots = _roots([memory.root, *artifact_roots])
    binding = {"contract": ACQUISITION_KIND, "route": route, "request": deepcopy(request),
        "method": "GET", "capture_options": {}, "physical_request_fingerprint": fingerprint,
        "acquisition_id": acquisition_id,
        "original_attempt_id": original_attempt_id if original_attempt_id is not None else acquisition_id, "owner_id": owner_id,
        "generation": 1, "previous_acquisition_id": None, "collection_channel": "unknown",
        "incremental_collection_fee": "unknown", "origin_account_key": None,
        "artifact_roots": roots, "refresh": None}
    origin = {"table": table, "id": record_id, "hash": digest(saved), "question_id": question_id}
    if original_snapshot:
        origin["original"] = deepcopy(saved)
    stage = {"kind": STAGE_KIND, "stage_type": "legacy_origin", "acquisition_id": acquisition_id,
             "binding_hash": digest(binding), "origin": origin}
    stage_id = key("source-acquisition-stage", stage)
    stage["recorded_at"] = as_of; stage["record_hash"] = digest(stage)
    row = {"kind": ACQUISITION_KIND, "binding": binding, "binding_hash": digest(binding),
        "state": "execution_unknown", "created_at": as_of, "updated_at": as_of,
        "stage_ids": [stage_id], "response_ids": [], "parse_ids": [], "active_response_id": None,
        "active_parse_id": None, "active_dispatch_intent_id": None, "parse_state": "not_started"}
    def validate(current):
        if digest(current[table].get(record_id)) != origin["hash"]:
            raise SafetyError("SOURCE_ACQUISITION_DEPENDENCY_CHANGED")
        if _owners(current, fingerprint) or acquisition_id in current["batch_runs"]:
            raise SafetyError("SOURCE_ACQUISITION_CONFLICT")
        _files_current(files)
        _guard(memory, current, binding, None)
    _guard(memory, state, binding, None)
    memory.commit(key("source-legacy-origin", [binding, origin]),
        [change("batch_runs", acquisition_id, row), change("source_checks", stage_id, stage)], validate=validate)
    return _result("join", acquisition_id, row)


def adopt_meta_origin(memory, request, *, table, record_id, as_of, question_id=None, artifact_roots=()):
    """Retain an actual old Meta caller or verified direct import, saved-only."""
    from .meta_browser import VERSION, _saved_meta_receipt, prepare_meta_query
    _date(as_of); identifier(record_id)
    state = memory.snapshot(); saved = state.get(table, {}).get(record_id)
    if not isinstance(saved, dict): raise SafetyError("SOURCE_META_ORIGIN_INVALID")
    files = []
    if table == "query_tasks" and saved.get("kind") == "all_source_task_v1":
        actual = saved.get("request"); aid = saved.get("acquisition_id"); owner_id = record_id
        if (saved.get("route") != "meta-ads" or not aid or type(saved.get("generation")) is not int
                or saved["generation"] < 1 or saved.get("request_hash") != digest(actual)
                or record_id != key("source-task", [saved.get("run_id"), "meta-ads", actual])
                or not saved.get("dispatched_at")):
            raise SafetyError("SOURCE_META_ORIGIN_INVALID")
        _date(saved["dispatched_at"])
    elif table == "batch_runs" and saved.get("kind") == "catalogue-discovery-cycle-v1":
        identifier(question_id)
        questions = [q for row in saved.get("planned_actions", []) for q in row.get("questions", [])
                     if q.get("id") == question_id]
        action = saved.get("actions", {}).get(question_id, {})
        if (len(questions) != 1 or questions[0].get("route") != "meta-ads"
                or action.get("acquisition_id") != key("discovery-acquisition", [record_id, question_id])
                or not action.get("updated_at")):
            raise SafetyError("SOURCE_META_ORIGIN_INVALID")
        _date(action["updated_at"])
        actual, aid, owner_id = questions[0]["request"], action["acquisition_id"], question_id
    elif table == "captures" and saved.get("source") == "meta" and isinstance(saved.get("reference"), str):
        path = safe_path(saved["reference"]); roots = _roots([memory.root, *artifact_roots])
        proof, stat, raw = _file(path, roots); receipt = decode_json(raw)
        if not isinstance(receipt, dict) or receipt.get("version") != VERSION:
            raise SafetyError("SOURCE_META_ORIGIN_INVALID")
        actual, aid, owner_id = receipt.get("request"), saved.get("acquisition_id"), record_id
        _saved_meta_receipt(path.parent, path, aid, actual)
        if saved.get("observed_at") != receipt["observed_at"] or saved.get("capture_hash") != receipt["raw_hash"]:
            raise SafetyError("SOURCE_META_ORIGIN_INVALID")
        snapshot, snapshot_stat, _ = _file(path.parent / receipt.get("snapshot_file", "snapshot.json"), roots)
        files = [(proof, stat), (snapshot, snapshot_stat)]
    else:
        raise SafetyError("SOURCE_META_ORIGIN_INVALID")
    if (not isinstance(actual, dict) or not set(actual) <= {"query", "country", "limit", "url"}
            or actual.get("country", "IN") != "IN" or actual.get("limit", 30) != 30
            or prepare_meta_query(actual.get("query")) != request
            or "url" in actual and actual != request):
        raise SafetyError("SOURCE_META_ORIGIN_SCOPE_INVALID")
    return _install_legacy_origin(memory, state, "meta-ads", request, table=table, record_id=record_id,
        saved=saved, acquisition_id=aid, owner_id=owner_id, as_of=as_of, question_id=question_id,
        artifact_roots=artifact_roots, files=files)


def adopt_quickcommerce_origin(memory, request, *, table, record_id, as_of, question_id=None, artifact_roots=()):
    """A saved paid question or capture is adopted as unknown, never launched again."""
    from .quickcommerce import validate_capture_receipt
    from .quickcommerce_handlers import prepare_funded_query
    route = request.get("platform"); _date(as_of); identifier(record_id)
    state = memory.snapshot(); saved = state.get(table, {}).get(record_id); files = []
    if route not in {"blinkit", "zepto", "instamart"} or not isinstance(saved, dict):
        raise SafetyError("SOURCE_QUICKCOMMERCE_ORIGIN_INVALID")
    if table == "query_tasks" and saved.get("kind") == "all_source_task_v1":
        actual, aid, owner_id = saved.get("request"), saved.get("acquisition_id"), record_id
        if (saved.get("route") != route or not aid or type(saved.get("generation")) is not int
                or saved["generation"] < 1 or saved.get("request_hash") != digest(actual)
                or record_id != key("source-task", [saved.get("run_id"), route, actual]) or not saved.get("dispatched_at")):
            raise SafetyError("SOURCE_QUICKCOMMERCE_ORIGIN_INVALID")
        _date(saved["dispatched_at"])
        # If already frozen, the actual leaf cannot be changed on restart.
        actual = saved.get("source_physical_request", actual)
    elif table == "captures" and saved.get("source") == route and isinstance(saved.get("reference"), str):
        proof, stat, raw = _file(saved["reference"], _roots([memory.root, *artifact_roots]))
        receipt = decode_json(raw); actual = validate_capture_receipt(receipt)
        aid, owner_id = saved.get("acquisition_id"), record_id
        if (Path(proof["path"]).parent.name != aid or saved.get("capture_hash") != digest(receipt)
                or saved.get("observed_at") != receipt["observed_at"] or saved.get("request") != actual
                or request.get("transport") != "funded-http"):
            raise SafetyError("SOURCE_QUICKCOMMERCE_ORIGIN_INVALID")
        files = [(proof, stat)]
    else:
        raise SafetyError("SOURCE_QUICKCOMMERCE_ORIGIN_INVALID")
    if prepare_funded_query(route, actual) != request:
        raise SafetyError("SOURCE_QUICKCOMMERCE_ORIGIN_SCOPE_INVALID")
    return _install_legacy_origin(memory, state, route, request, table=table, record_id=record_id, saved=saved,
        acquisition_id=aid, owner_id=owner_id, as_of=as_of, question_id=question_id, artifact_roots=artifact_roots, files=files)


def adopt_marketplace_origin(memory, route, request, *, table, record_id, as_of,
                             question_id=None, artifact_roots=()):
    """Retain a real legacy caller intent/capture, never infer it unstarted.

    Only the installed direct-marketplace adapters use this bridge. The
    reference is verified here against its actual original saved shape, then
    pinned in the same commit that installs an execution-unknown owner. A
    later local receipt verification may resolve it; adoption cannot launch.
    """
    from .marketplace_browser import build_marketplace_request, validate_request
    if route not in {"amazon-bestsellers", "amazon-search", "flipkart-search"}:
        raise SafetyError("SOURCE_MARKETPLACE_ORIGIN_INVALID")
    _date(as_of); identifier(record_id)
    state = memory.snapshot()
    saved = state.get(table, {}).get(record_id)
    if not isinstance(saved, dict): raise SafetyError("SOURCE_MARKETPLACE_ORIGIN_INVALID")
    if table == "query_tasks" and saved.get("kind") == "all_source_task_v1":
        acquisition_id = saved.get("acquisition_id")
        actual_request, actual_route = saved.get("request"), saved.get("route")
        if (not acquisition_id or type(saved.get("generation")) is not int or saved["generation"] < 1
                or saved.get("request_hash") != digest(actual_request)
                or record_id != key("source-task", [saved.get("run_id"), actual_route, actual_request])
                or not saved.get("dispatched_at")):
            raise SafetyError("SOURCE_MARKETPLACE_ORIGIN_INVALID")
        _date(saved["dispatched_at"])
        owner_id = record_id
    elif table == "batch_runs" and saved.get("kind") == "catalogue-discovery-cycle-v1":
        identifier(question_id)
        questions = [q for row in saved.get("planned_actions", []) for q in row.get("questions", [])
                     if q.get("id") == question_id]
        action = saved.get("actions", {}).get(question_id)
        if len(questions) != 1 or not isinstance(action, dict) or not action.get("acquisition_id"):
            raise SafetyError("SOURCE_MARKETPLACE_ORIGIN_INVALID")
        actual_request, actual_route = questions[0].get("request"), questions[0].get("route")
        acquisition_id, owner_id = action["acquisition_id"], question_id
        if acquisition_id != key("discovery-acquisition", [record_id, question_id]) or not action.get("updated_at"):
            raise SafetyError("SOURCE_MARKETPLACE_ORIGIN_INVALID")
        _date(action["updated_at"])
    elif table == "captures":
        ref = saved.get("reference", {})
        actual_request, actual_route = ref.get("request"), ref.get("route")
        acquisition_id, owner_id = saved.get("acquisition_id"), record_id
        if saved.get("source") != CAPTURE_SOURCES[route] or not ref.get("folder"):
            raise SafetyError("SOURCE_MARKETPLACE_ORIGIN_INVALID")
    else:
        raise SafetyError("SOURCE_MARKETPLACE_ORIGIN_INVALID")
    if not isinstance(actual_request, dict): raise SafetyError("SOURCE_MARKETPLACE_ORIGIN_INVALID")
    if "url" not in actual_request:
        actual_request = build_marketplace_request(actual_route,
            term=actual_request.get("query", actual_request.get("term")),
            department=actual_request.get("department"), limit=actual_request.get("limit", 30))
    validate_request(actual_request)
    fingerprint = physical_request_fingerprint(route, request)
    if actual_route != route or physical_request_fingerprint(actual_route, actual_request) != fingerprint:
        raise SafetyError("SOURCE_MARKETPLACE_ORIGIN_SCOPE_INVALID")
    identifier(acquisition_id)
    owners = _owners(state, fingerprint)
    if owners:
        return _result("join", *owners[-1])
    roots = _roots([memory.root, *artifact_roots])
    binding = {"contract": ACQUISITION_KIND, "route": route, "request": deepcopy(request),
        "method": "GET", "capture_options": {}, "physical_request_fingerprint": fingerprint,
        "acquisition_id": acquisition_id, "original_attempt_id": acquisition_id, "owner_id": owner_id,
        "generation": 1, "previous_acquisition_id": None, "collection_channel": "unknown",
        "incremental_collection_fee": "unknown", "origin_account_key": None,
        "artifact_roots": roots, "refresh": None}
    # Local legacy intents are proof of ownership, not proof of zero execution.
    origin = {"table": table, "id": record_id, "hash": digest(saved), "question_id": question_id}
    stage = {"kind": STAGE_KIND, "stage_type": "legacy_origin", "acquisition_id": acquisition_id,
             "binding_hash": digest(binding), "origin": origin}
    stage_id = key("source-acquisition-stage", stage)
    stage["recorded_at"] = as_of; stage["record_hash"] = digest(stage)
    row = {"kind": ACQUISITION_KIND, "binding": binding, "binding_hash": digest(binding),
        "state": "execution_unknown", "created_at": as_of, "updated_at": as_of,
        "stage_ids": [stage_id], "response_ids": [], "parse_ids": [], "active_response_id": None,
        "active_parse_id": None, "active_dispatch_intent_id": None, "parse_state": "not_started"}
    def validate(current):
        if digest(current[table].get(record_id)) != origin["hash"]:
            raise SafetyError("SOURCE_ACQUISITION_DEPENDENCY_CHANGED")
        if _owners(current, fingerprint) or acquisition_id in current["batch_runs"]:
            raise SafetyError("SOURCE_ACQUISITION_CONFLICT")
        _guard(memory, current, binding, None)
    _guard(memory, state, binding, None)
    memory.commit(key("marketplace-origin", [binding, origin]),
        [change("batch_runs", acquisition_id, row), change("source_checks", stage_id, stage)], validate=validate)
    return _result("join", acquisition_id, row)


def record_marketplace_response(memory, acquisition_id, *, folder, as_of, capture_ids=()):
    """Verify actual installed marketplace proof without rewriting old receipts.

    The bridge derives completeness from the hash-bound source snapshot and
    parser scope, not from the mere existence of an import row. Failed parsing
    retains original bytes with incomplete scope; it never means no results.
    """
    from .marketplace_browser import MAX_JSON, reconcile_marketplace
    _date(as_of)
    state = memory.snapshot(); old = _record(state, acquisition_id)
    if (not isinstance(capture_ids, (tuple, list)) or len(capture_ids) > 40
            or any(not isinstance(item, str) for item in capture_ids) or len(set(capture_ids)) != len(capture_ids)):
        raise SafetyError("SOURCE_ACQUISITION_CAPTURES_INVALID")
    receipt = reconcile_marketplace(folder)
    path = Path(folder) / receipt.get("receipt_file", "receipt.json")
    actual, proof, artifacts, files, observed = _original_receipt(memory, old, path, as_of)
    if actual != receipt: raise SafetyError("SOURCE_MARKETPLACE_RECEIPT_MISMATCH")
    if memory.evidence_type != receipt.get("evidence_type"):
        raise SafetyError("RESEARCH_NAMESPACE_INVALID")
    parsed_name = receipt.get("parsed_file", "parsed.json")
    parsed = snapshot = None
    if parsed_name in artifacts:
        _, _, raw = _file(artifacts[parsed_name]["path"], old["binding"]["artifact_roots"])
        parsed = decode_json(raw)
    if "snapshot.json" in artifacts:
        _, _, raw = _file(artifacts["snapshot.json"]["path"], old["binding"]["artifact_roots"])
        if len(raw) > MAX_JSON: raise SafetyError("SOURCE_ACQUISITION_FILE_INVALID")
        snapshot = decode_json(raw)
    recognized = bool(parsed and parsed.get("state") in {"usable", "partial", "empty"}
                      and parsed.get("scope_verified") is True)
    coverage = {"body_complete": isinstance(snapshot, dict),
        "byte_truncated": bool(snapshot and snapshot.get("text_truncated") is True),
        "scope_complete": recognized, "recognized": recognized,
        "delivered_count": len(parsed["records"]) if parsed else 0,
        "intentional_limit": bool(parsed and parsed.get("bounded_cards", 0) < parsed.get("observed_cards", 0))}
    complete = bool(coverage["body_complete"] and not coverage["byte_truncated"] and recognized
                    and receipt["dispatch_started"] is True and receipt.get("owned_process_exited") is True
                    and receipt["state"] in {"usable", "partial", "empty"})
    captures = []
    for capture_id in capture_ids:
        capture = state["captures"].get(identifier(capture_id))
        if (not capture or capture.get("acquisition_id") != acquisition_id
            or capture.get("source") != CAPTURE_SOURCES[old["binding"]["route"]]
            or capture.get("observed_at") != observed or capture.get("capture_hash") != digest(receipt)):
            raise SafetyError("SOURCE_ACQUISITION_CAPTURE_SCOPE_INVALID")
        captures.append({"id": capture_id, "hash": digest(capture)})
    for previous_id in old["response_ids"]:
        previous = _stage(state, old, previous_id)
        if (previous["dispatch_started"] is True and receipt["dispatch_started"] is not True
                or timestamp(previous["observed_at"]) > timestamp(observed)):
            raise SafetyError("SOURCE_ACQUISITION_RECEIPT_CONFLICT")
    data = {"receipt": proof, "artifacts": artifacts, "observed_at": observed,
        "dispatch_started": receipt["dispatch_started"], "source_state": receipt["state"],
        "coverage": coverage, "complete": complete, "captures": captures,
        "response_hash": digest({name: value for name, value in artifacts.items() if name in _RESPONSE_NAMES}),
        "expense_proof": {"collection_channel": "direct_local", "provider_requests": receipt["apify_requests"],
                          "model_calls": receipt["model_calls"], "provider_charge_usd": receipt["apify_charge_usd"]},
        "bridge_contract": "verified-marketplace-response-v1"}
    data["access"] = {"http_status": receipt.get("navigation_status"),
        "retry_after": receipt.get("retry_after"), "owned_process_exited": receipt.get("owned_process_exited")}
    status = ("completed_empty" if complete and receipt["state"] == "empty" else
              "failed_access" if receipt["state"] == "blocked" and receipt.get("owned_process_exited") is True else
              "response_saved" if receipt["dispatch_started"] is True else "execution_unknown")
    return _commit_stage(memory, old, "response", data, as_of,
        {"state": status, "active_parse_id": None, "parse_state": "not_started"}, files=files, captures=captures)


def record_meta_response(memory, acquisition_id, *, folder, as_of, capture_ids=()):
    """Bind the installed Meta receipt, raw cards and actual returned transport.

    Replaying an old parser is local work. It neither creates another source
    date nor makes a missing/blocked page into an empty result. Legacy imports
    lacking the additive original transport proof remain incomplete reads.
    """
    from .meta_browser import MAX_BYTES, _saved_meta_receipt, capture_meta_search, meta_returned_scope_matches
    _date(as_of); state = memory.snapshot(); old = _record(state, acquisition_id)
    request = old["binding"]["request"]
    if old["binding"]["route"] != "meta-ads": raise SafetyError("SOURCE_META_RECEIPT_SCOPE_INVALID")
    folder = safe_path(folder)
    if folder.name != acquisition_id: raise SafetyError("SOURCE_META_RECEIPT_SCOPE_INVALID")
    receipt, _ = capture_meta_search(folder.parent, acquisition_id, request["query"], reconcile=True)
    path = folder / receipt.get("receipt_file", "receipt.json")
    receipt_proof, receipt_stat, raw = _file(path, old["binding"]["artifact_roots"])
    if decode_json(raw) != receipt or _saved_meta_receipt(folder, path, acquisition_id, request) != receipt:
        raise SafetyError("SOURCE_META_RECEIPT_SCOPE_INVALID")
    observed = receipt["observed_at"]
    if timestamp(observed) > timestamp(as_of): raise SafetyError("SOURCE_ACQUISITION_SOURCE_DATE_INVALID")
    files, artifacts = [(receipt_proof, receipt_stat)], {}
    names = {receipt.get("snapshot_file", "snapshot.json"), *receipt.get("artifacts", {})}
    # These are actual immutable original files, not a new wrapper pretending
    # to be a collection. Their contents are rechecked during the short commit.
    for name in ("transport.json", "native_result.json", "intent.json"):
        if (folder / name).is_file(): names.add(name)
    for name in sorted(names):
        if not isinstance(name, str) or Path(name).name != name:
            raise SafetyError("SOURCE_ACQUISITION_ARTIFACTS_INVALID")
        proof, stat, _ = _file(folder / name, old["binding"]["artifact_roots"])
        files.append((proof, stat)); artifacts[name] = proof
    _, _, snapshot_raw = _file(folder / receipt.get("snapshot_file", "snapshot.json"), old["binding"]["artifact_roots"])
    if len(snapshot_raw) > MAX_BYTES or sha256(snapshot_raw).hexdigest() != receipt["raw_hash"]:
        raise SafetyError("SOURCE_ACQUISITION_ARTIFACT_CHANGED")
    snapshot = decode_json(snapshot_raw); transport = None
    if "transport.json" in artifacts:
        _, _, transport_raw = _file(folder / "transport.json", old["binding"]["artifact_roots"])
        transport = decode_json(transport_raw)
        if (not isinstance(transport, dict) or transport.get("contract") != "meta-original-transport-v1"
                or transport.get("requested_url") != request["url"] or transport.get("snapshot_hash") != digest(snapshot)
                or transport.get("navigation_started") is not receipt.get("dispatch_started")
                or transport.get("navigation_status") != receipt.get("navigation_status")
                or transport.get("owned_process_exited") is not receipt.get("owned_process_exited")
                or transport.get("completed_at") != observed):
            # Deadline reparsing has its own exact native/original proof and
            # may supersede an interrupted first transport; it is not a new
            # navigation. No stale transport can supply its new completion.
            if not receipt.get("recovery"): raise SafetyError("SOURCE_META_TRANSPORT_INVALID")
            transport = None
    closed = bool(transport and transport.get("owned_process_exited") is True)
    if receipt.get("outer_deadline") is True or receipt.get("recovery"):
        closed = receipt.get("owned_process_exited") is True
    if "native_result.json" in artifacts and transport:
        _, _, native_raw = _file(folder / "native_result.json", old["binding"]["artifact_roots"])
        native = decode_json(native_raw)
        if (not isinstance(native, dict) or any(native.get(k) != transport.get(k) for k in
                ("navigation_started", "navigation_started_at", "navigation_status", "owned_process_exited", "completed_at"))
                or digest(native.get("snapshot")) != transport["snapshot_hash"]):
            raise SafetyError("SOURCE_META_TRANSPORT_INVALID")
    # Every requested parameter must come back unchanged; only Meta's own
    # known display defaults may be added (see META_RETURNED_DEFAULTS).
    same_scope = bool(isinstance(snapshot, dict) and meta_returned_scope_matches(request["url"], snapshot.get("url")))
    recognized = same_scope and receipt["state"] in {"usable", "partial", "empty"}
    coverage = {"body_complete": isinstance(snapshot, dict),
        "byte_truncated": bool(isinstance(snapshot, dict) and snapshot.get("text_truncated") is True),
        "scope_complete": recognized, "recognized": recognized,
        "delivered_count": len(receipt["records"]),
        "intentional_limit": bool(isinstance(snapshot, dict) and len(snapshot.get("cards", [])) > request["limit"])}
    complete = bool(closed and receipt["dispatch_started"] is True and recognized
                    and coverage["body_complete"] and not coverage["byte_truncated"])
    if (not isinstance(capture_ids, (tuple, list)) or len(capture_ids) > 40
            or len(set(capture_ids)) != len(capture_ids)):
        raise SafetyError("SOURCE_ACQUISITION_CAPTURES_INVALID")
    captures = []
    for capture_id in capture_ids:
        capture = state["captures"].get(identifier(capture_id))
        if (not capture or capture.get("source") != "meta" or capture.get("acquisition_id") != acquisition_id
                or capture.get("observed_at") != observed or capture.get("capture_hash") != receipt["raw_hash"]
                or capture.get("reference") != str(path)):
            raise SafetyError("SOURCE_ACQUISITION_CAPTURE_SCOPE_INVALID")
        captures.append({"id": capture_id, "hash": digest(capture)})
    for previous_id in old["response_ids"]:
        previous = _stage(state, old, previous_id)
        if timestamp(previous["observed_at"]) > timestamp(observed):
            raise SafetyError("SOURCE_ACQUISITION_RECEIPT_CONFLICT")
    source_state = ("insufficient" if not same_scope and receipt["state"] == "blocked"
                    and receipt.get("navigation_status") not in {401, 403, 429, 500, 502, 503, 504} else receipt["state"])
    data = {"receipt": receipt_proof, "artifacts": artifacts, "observed_at": observed,
        "dispatch_started": receipt["dispatch_started"], "source_state": source_state,
        "coverage": coverage, "complete": complete, "captures": captures,
        "response_hash": receipt["raw_hash"], "bridge_contract": "verified-meta-response-v1",
        "expense_proof": {"collection_channel": "direct_local", "provider_charge_usd": receipt["apify_usd"],
                          "model_calls": receipt["model_calls"]},
        "access": {"http_status": receipt.get("navigation_status"), "owned_process_exited": closed,
                   "retry_after": transport.get("retry_after") if transport else None}}
    status = ("completed_empty" if complete and receipt["state"] == "empty" else
              "failed_access" if source_state == "blocked" and closed else
              "response_saved" if receipt["dispatch_started"] is True else "execution_unknown")
    return _commit_stage(memory, old, "response", data, as_of,
        {"state": status, "active_parse_id": None, "parse_state": "not_started"}, files=files, captures=captures)


def record_quickcommerce_response(memory, acquisition_id, *, reference, as_of, capture_ids=()):
    """Verify the one paid provider original, preserving its source date."""
    from .quickcommerce import validate_capture_receipt
    from .quickcommerce_handlers import TRANSPORT, prepare_funded_query
    _date(as_of); state = memory.snapshot(); old = _record(state, acquisition_id)
    request = old["binding"]["request"]; route = old["binding"]["route"]
    if route not in {"blinkit", "zepto", "instamart"} or request.get("transport") != TRANSPORT:
        raise SafetyError("SOURCE_QUICKCOMMERCE_SCOPE_INVALID")
    proof, stat, raw = _file(reference, old["binding"]["artifact_roots"]); original = decode_json(raw)
    if Path(proof["path"]).parent.name != acquisition_id: raise SafetyError("SOURCE_QUICKCOMMERCE_SCOPE_INVALID")
    files, artifacts = [(proof, stat)], {}
    actual = validate_capture_receipt(original)
    if prepare_funded_query(route, actual) != request:
        raise SafetyError("SOURCE_QUICKCOMMERCE_SCOPE_INVALID")
    recognized = original["state"] in {"captured", "completed_no_results"} and original.get("raw_response") is not None
    closed = original.get("transport_contract") == "quickcommerce-owned-http-v1" and original.get("owned_request_closed") is True
    complete = bool(recognized and original.get("body_complete") is True and closed)
    observed, dispatched, source_state = original["observed_at"], original["dispatch_started"], original["state"]
    count, limit = len(original["products"]), bool(original.get("omitted"))
    expense = {"collection_channel": "funded_provider", "provider_credits_expected": original.get("provider_credits_expected"),
               "provider_charge_usd": None, "apify_usd": original["apify_usd"]}
    access = {"http_status": original.get("http_status"), "retry_after": original.get("retry_after"),
              "owned_process_exited": closed}
    if timestamp(observed) > timestamp(as_of): raise SafetyError("SOURCE_ACQUISITION_SOURCE_DATE_INVALID")
    if (not isinstance(capture_ids, (list, tuple)) or len(capture_ids) > 40
            or any(not isinstance(item, str) for item in capture_ids) or len(set(capture_ids)) != len(capture_ids)):
        raise SafetyError("SOURCE_ACQUISITION_CAPTURES_INVALID")
    captures = []
    for capture_id in capture_ids:
        capture = state["captures"].get(identifier(capture_id))
        if (not capture or capture.get("acquisition_id") != acquisition_id or capture.get("source") != route
                or capture.get("observed_at") != observed or capture.get("reference") != str(reference)
                or capture.get("capture_hash") != digest(original)):
            raise SafetyError("SOURCE_ACQUISITION_CAPTURE_SCOPE_INVALID")
        captures.append({"id": capture_id, "hash": digest(capture)})
    data = {"receipt": proof, "artifacts": artifacts, "observed_at": observed, "dispatch_started": dispatched,
        "source_state": source_state, "complete": complete, "captures": captures,
        "response_hash": digest(original.get("raw_response")),
        "coverage": {"body_complete": bool(original.get("body_complete")), "byte_truncated": False,
            "scope_complete": recognized, "recognized": recognized, "delivered_count": count, "intentional_limit": limit},
        "expense_proof": expense, "access": access, "bridge_contract": "verified-quickcommerce-response-v1"}
    status = ("completed_empty" if complete and source_state == "completed_no_results" else
              "failed_access" if source_state == "blocked" and closed else "response_saved" if dispatched is True else "execution_unknown")
    return _commit_stage(memory, old, "response", data, as_of,
        {"state": status, "active_parse_id": None, "parse_state": "not_started"}, files=files, captures=captures)


def record_source_response(memory, acquisition_id, *, receipt_path, as_of, capture_ids=()):
    """Bind an actual saved original receipt; missing completeness stays missing.

    New adapters should save explicit coverage in the original receipt. Older
    receipts are accepted without relabelling them complete. A bounded card
    limit is complete when scope_complete is true; it need not cover a whole
    catalogue. Bytes missing/truncated cannot become a parser-only recovery.
    """
    _date(as_of)
    state = memory.snapshot(); old = _record(state, acquisition_id)
    receipt, proof, artifacts, files, observed = _original_receipt(memory, old, receipt_path, as_of)
    if (not isinstance(capture_ids, (tuple, list)) or len(capture_ids) > 40
            or any(not isinstance(item, str) for item in capture_ids) or len(set(capture_ids)) != len(capture_ids)):
        raise SafetyError("SOURCE_ACQUISITION_CAPTURES_INVALID")
    captures = []
    expected_source = CAPTURE_SOURCES[old["binding"]["route"]]
    for capture_id in capture_ids:
        capture = state["captures"].get(identifier(capture_id))
        if (not capture or capture.get("acquisition_id") != acquisition_id
                or capture.get("source") != expected_source or capture.get("observed_at") != observed):
            raise SafetyError("SOURCE_ACQUISITION_CAPTURE_SCOPE_INVALID")
        captures.append({"id": capture_id, "hash": digest(capture)})
    coverage = receipt.get("coverage")
    if coverage is not None and (not isinstance(coverage, dict)
            or set(coverage) != {"body_complete", "byte_truncated", "scope_complete", "recognized", "delivered_count", "intentional_limit"}
            or any(type(coverage[field]) is not bool for field in ("body_complete", "byte_truncated", "scope_complete", "recognized", "intentional_limit"))
            or type(coverage["delivered_count"]) is not int or coverage["delivered_count"] < 0):
        raise SafetyError("SOURCE_ACQUISITION_COVERAGE_INVALID")
    complete = bool(coverage and coverage["body_complete"] and not coverage["byte_truncated"]
                    and coverage["scope_complete"] and coverage["recognized"]
                    and set(artifacts) & _RESPONSE_NAMES and receipt["dispatch_started"] is True
                    and receipt.get("state") in {"usable", "partial", "empty", "captured", "completed_no_results"})
    if receipt.get("state") not in {"usable", "partial", "empty", "captured", "completed_no_results", "blocked", "failed", "unavailable"}:
        raise SafetyError("SOURCE_ACQUISITION_RECEIPT_STATE_INVALID")
    for previous_id in old["response_ids"]:
        previous = _stage(state, old, previous_id)
        if (previous["dispatch_started"] is True and receipt["dispatch_started"] is not True
                or timestamp(previous["observed_at"]) > timestamp(observed)):
            raise SafetyError("SOURCE_ACQUISITION_RECEIPT_CONFLICT")
    response_files = {name: value for name, value in artifacts.items() if name in _RESPONSE_NAMES}
    data = {"receipt": proof, "artifacts": artifacts, "observed_at": observed,
            "dispatch_started": receipt["dispatch_started"], "source_state": receipt.get("state"),
            "coverage": deepcopy(coverage), "complete": complete, "captures": captures,
            "response_hash": digest(response_files)}
    if receipt["dispatch_started"] is None:
        status = "execution_unknown"
    elif receipt.get("state") in {"blocked", "failed", "unavailable"}:
        status = "failed_access" if receipt["dispatch_started"] else "execution_unknown"
    elif receipt["dispatch_started"] is True:
        status = "response_saved"
    else:
        status = "execution_unknown"
    return _commit_stage(memory, old, "response", data, as_of,
        {"state": status, "active_parse_id": None, "parse_state": "not_started"}, files=files, captures=captures)


def image_failure_policy(receipt):
    """A saved original failure has a finite trigger, not a blind new GET."""
    from .source_coverage import _source_retry_at
    at = _date(receipt.get("checked_at"))
    if receipt.get("state") == "usable":
        return {"classification": "usable", "next_retry_at": None}
    if receipt.get("owned_worker_exited") is not True:
        return {"classification": "original_pending", "next_retry_at": None}
    if receipt.get("access_wait") is not None:
        wait = receipt["access_wait"]
        return ({"classification": "retryable", "next_retry_at": wait["next_retry_at"]}
                if wait["phase"] in {"waiting", "due"} else
                {"classification": "original_pending", "next_retry_at": None})
    transient = receipt.get("http_status") in {408, 425, 429, 500, 502, 503, 504} or receipt.get("reason") in {
        "PRODUCT_IMAGE_DEADLINE", "PRODUCT_IMAGE_TRANSPORT_FAILED", "PRODUCT_IMAGE_READERS_BUSY"}
    if transient:
        return {"classification": "retryable", "next_retry_at": _source_retry_at(receipt.get("retry_after"), at)}
    return {"classification": "terminal", "next_retry_at": None}


def source_image_recovery(state, acquisition_id, *, as_of):
    """Exact closed transient result eligible for one next linked generation."""
    from .source_coverage import source_response_closed
    owner = _record(state, acquisition_id)
    response = _response(state, owner)
    policy = response.get("image_failure") if response else None
    if (owner["binding"]["route"] != "public_image_http" or owner["binding"]["generation"] >= 3
            or not response or response.get("bridge_contract") != "verified-image-response-v1"
            or response["source_state"] != "failed" or not source_response_closed(response)
            or not policy or policy["classification"] != "retryable"
            or timestamp(as_of) < timestamp(policy["next_retry_at"])):
        return None
    return {"response_id": owner["active_response_id"], "response_hash": digest(response),
            "next_retry_at": policy["next_retry_at"]}


def record_image_response(memory, acquisition_id, *, response_path, as_of):
    """Bind the actual atomic image receipt/body, with honest thread closure.

    This supporting read creates no capture/observation or market-vote source.
    A local image decode/storage problem cannot masquerade as an access block.
    """
    import base64
    import binascii
    from .product_images import VERSION, SHARED_READ_KIND, _shared_record, inspect_image
    from .public_pages import _public_url
    _date(as_of)
    state = memory.snapshot(); old = _record(state, acquisition_id)
    if old["binding"]["route"] != "public_image_http":
        raise SafetyError("SOURCE_IMAGE_RESPONSE_INVALID")
    proof, stat, raw = _file(response_path, old["binding"]["artifact_roots"])
    saved = decode_json(raw)
    for previous_id in old["response_ids"]:
        if _stage(state, old, previous_id)["receipt"] != proof:
            raise SafetyError("SOURCE_IMAGE_RESPONSE_CHANGED")
    value = saved.get("value") if isinstance(saved, dict) else None
    if (not isinstance(value, dict) or saved.get("hash") != digest(value)
            or set(value) != {"kind", "reference", "request", "origin_reference", "original", "dispatch_intent_id", "receipt", "body_base64"}
            or value["kind"] != "public-product-image-response-v1" or value["reference"] != acquisition_id
            or value["request"] != old["binding"]["request"]):
        raise SafetyError("SOURCE_IMAGE_RESPONSE_INVALID")
    receipt = value["receipt"]
    if (not isinstance(receipt, dict) or receipt.get("state") not in {"usable", "failed"}
            or receipt.get("requested_url") != old["binding"]["request"]["url"]
            or receipt.get("dispatch_started") not in (True, False, None)
            or type(receipt.get("dispatch_started")) not in (bool, type(None))
            or "dispatch_started" not in receipt or timestamp(receipt.get("checked_at")) > timestamp(as_of)):
        raise SafetyError("SOURCE_IMAGE_RESPONSE_INVALID")
    dependencies = []
    if value["original"] is not None:
        origin = value["original"]
        actual = state["source_checks"].get(origin.get("id")) if isinstance(origin, dict) else None
        if (not isinstance(origin, dict) or set(origin) != {"table", "id", "hash"}
                or origin["table"] != "source_checks" or not isinstance(actual, dict)
                or digest(actual) != origin["hash"] or value["origin_reference"] != origin["id"]
                or actual.get("kind") not in {"public_product_image", "public-product-image-read-v1"}
                or actual.get("source_url") != receipt["requested_url"] or actual.get("receipt") != receipt
                or actual.get("at") != receipt["checked_at"]):
            raise SafetyError("SOURCE_IMAGE_ORIGINAL_INVALID")
        dependencies.append(origin)
        if actual["kind"] == SHARED_READ_KIND:
            _shared_record(actual, origin["id"], receipt["requested_url"])
        else:
            legacy_binding = {field: actual.get(field) for field in ("concept_id", "description_hash",
                "observation_id", "source_url", "observed_at", "relationship")}
            if actual.get("version") != VERSION or origin["id"] != key("product-image", [VERSION, legacy_binding]):
                raise SafetyError("SOURCE_IMAGE_ORIGINAL_INVALID")
    elif (value["origin_reference"] != acquisition_id
            or value["dispatch_intent_id"] != old.get("active_dispatch_intent_id")
            or not value["dispatch_intent_id"]):
        raise SafetyError("SOURCE_IMAGE_INTENT_INVALID")
    body = None
    if value["body_base64"] is not None:
        try:
            if not isinstance(value["body_base64"], str): raise ValueError
            body = base64.b64decode(value["body_base64"], validate=True)
        except (binascii.Error, ValueError):
            raise SafetyError("SOURCE_IMAGE_RESPONSE_INVALID") from None
    complete = receipt["state"] == "usable"
    if complete:
        if (receipt["dispatch_started"] is not True or receipt.get("body_complete") is not True
                or inspect_image(body) != receipt.get("metadata")):
            raise SafetyError("SOURCE_IMAGE_BYTES_INVALID")
    elif body is not None:
        raise SafetyError("SOURCE_IMAGE_RESPONSE_INVALID")
    from .source_coverage import _access_failure, source_access_scope
    scope_url = _public_url(receipt.get("final_url") or receipt["requested_url"])[0]
    source_access_scope(scope_url, transport="public-http", access_identity="owned-public-http")
    if receipt.get("access_wait") is not None:
        wait = receipt["access_wait"]
        if (not isinstance(wait, dict) or set(wait) != {"scope", "scope_id", "scope_hash", "failure_id",
                "failure_hash", "phase", "next_retry_at", "acquisition_id", "response_id", "response_hash", "probe", "reason"}):
            raise SafetyError("SOURCE_IMAGE_WAIT_PROOF_INVALID")
        failure = _access_failure(state, wait["failure_id"])
        scope_url = _public_url(receipt.get("guard_url"))[0]
        if (failure["binding"]["scope"] != source_access_scope(scope_url, transport="public-http", access_identity="owned-public-http")
                or digest(failure) != wait["failure_hash"]
                or wait["scope"] != failure["binding"]["scope"] or wait["scope_hash"] != digest(wait["scope"])
                or wait["acquisition_id"] != failure["binding"]["acquisition_id"]
                or wait["response_id"] != failure["binding"]["response_id"]
                or wait["response_hash"] != failure["binding"]["response_hash"]
                or failure["binding"]["next_retry_at"] != wait["next_retry_at"]
                or timestamp(failure["binding"]["observed_at"]) > timestamp(receipt["checked_at"])
                or wait["phase"] not in {"waiting", "due", "probe_pending", "execution_unknown"}
                or receipt.get("http_status") is not None or receipt["state"] != "failed"):
            raise SafetyError("SOURCE_IMAGE_WAIT_PROOF_INVALID")
        dependencies.append({"table": "source_checks", "id": wait["failure_id"], "hash": digest(failure)})
    data = {"receipt": proof, "artifacts": {"response.json": proof}, "observed_at": receipt["checked_at"],
        "dispatch_started": receipt["dispatch_started"], "source_state": receipt["state"],
        "coverage": {"body_complete": complete, "byte_truncated": False, "scope_complete": complete,
                     "recognized": complete, "delivered_count": int(complete), "intentional_limit": False},
        "complete": complete, "captures": [], "response_hash": digest({"response.json": proof}),
        "bridge_contract": "verified-image-response-v1", "image_failure": image_failure_policy(receipt),
        "fetch_receipt": deepcopy(receipt), "body_sha256": sha256(body).hexdigest() if body is not None else None,
        "access": {"http_status": receipt.get("http_status"), "retry_after": receipt.get("retry_after"),
                   "scope_url": scope_url, "ownership_kind": "thread", "owned_worker_exited": receipt.get("owned_worker_exited")}}
    status = "response_saved" if complete else "failed_access" if receipt["dispatch_started"] is True else "execution_unknown"
    return _commit_stage(memory, old, "response", data, as_of,
        {"state": status, "active_parse_id": None, "parse_state": "not_started"},
        files=[(proof, stat)], references=dependencies)


def record_source_parse(memory, acquisition_id, *, parsed_path, parser_contract, as_of):
    """Save one local interpretation receipt without changing the read identity."""
    _date(as_of)
    state = memory.snapshot(); old = _record(state, acquisition_id)
    response_files = []
    response = _response(state, old, files_out=response_files)
    if not response or response["dispatch_started"] is not True:
        raise SafetyError("SOURCE_ACQUISITION_RESPONSE_REQUIRED")
    proof, stat, raw = _file(parsed_path, old["binding"]["artifact_roots"]); parsed = decode_json(raw)
    if (not isinstance(parsed, dict) or parsed.get("state") not in {"usable", "partial", "empty", "failed", "blocked", "unavailable"}
            or not isinstance(parsed.get("records"), list)):
        raise SafetyError("SOURCE_ACQUISITION_PARSE_INVALID")
    if (parsed.get("contract") != "saved-source-parse-v1"
            or parsed.get("source_response_hash") != response["response_hash"]
            or parsed.get("parser_contract") != parser_contract):
        raise SafetyError("SOURCE_ACQUISITION_PARSE_INPUT_INVALID")
    if parsed["state"] == "empty" and parsed["records"]:
        raise SafetyError("SOURCE_ACQUISITION_PARSE_INVALID")
    fingerprint = parse_fingerprint(response["response_hash"], parser_contract)
    accepted = parsed["state"] in {"usable", "partial", "empty"}
    status = ("completed_empty" if accepted and parsed["state"] == "empty" and response["complete"] else
              "parsed" if accepted else "response_saved")
    return _commit_stage(memory, old, "parse", {"response_id": old["active_response_id"],
        "response_hash": response["response_hash"], "parsed": proof, "parser_contract": deepcopy(parser_contract),
        "parse_fingerprint": fingerprint, "parse_state": "accepted" if accepted else "failed",
        "source_state": parsed["state"], "record_count": len(parsed["records"])}, as_of,
        {"state": status, "parse_state": "accepted" if accepted else "failed"}, files=[(proof, stat), *response_files],
        captures=response["captures"], references=[{"table": "source_checks", "id": old["active_response_id"], "hash": digest(response)}])


def recover_source_prelaunch(memory, acquisition_id, *, receipt_path, as_of):
    """Reopen only the SAME origin after hash-bound owned-process zero proof.

    The process-exit artifact is an adapter contract, not an inferred absence
    of dispatch. Existing adapters must produce it from their owned process;
    a bare dispatch_started=False or known_zero fee is deliberately insufficient.
    """
    _date(as_of)
    old = _record(memory.snapshot(), acquisition_id)
    state = memory.snapshot()
    if (old["state"] not in {"reserved", "dispatching", "execution_unknown"}
            or any(_stage(state, old, item)["dispatch_started"] is True for item in old["response_ids"])):
        raise SafetyError("SOURCE_ACQUISITION_TRANSITION_INVALID")
    receipt, proof, artifacts, files, observed = _original_receipt(memory, old, receipt_path, as_of)
    if receipt["dispatch_started"] is not False or "process-exit.json" not in artifacts:
        raise SafetyError("SOURCE_ACQUISITION_ZERO_PROOF_REQUIRED")
    _, _, raw = _file(artifacts["process-exit.json"]["path"], old["binding"]["artifact_roots"])
    exit_proof = decode_json(raw)
    binding = old["binding"]
    intent_id = old.get("active_dispatch_intent_id")
    intent = _stage(state, old, intent_id) if intent_id else None
    if (not isinstance(exit_proof, dict) or exit_proof.get("contract") != "owned-source-process-exit-v1"
            or not intent or intent.get("stage_type") != "dispatch_intent"
            or exit_proof.get("dispatch_intent_id") != intent_id
            or exit_proof.get("intent_sequence") != intent["intent_sequence"]
            or exit_proof.get("acquisition_id") != acquisition_id
            or exit_proof.get("attempt_id") != binding["original_attempt_id"]
            or exit_proof.get("physical_request_fingerprint") != binding["physical_request_fingerprint"]
            or exit_proof.get("owned_process_exited") is not True or exit_proof.get("request_sent") is not False
            or type(exit_proof.get("process_id")) is not int or exit_proof["process_id"] < 1
            or not isinstance(exit_proof.get("ownership_token_hash"), str)
            or not _HEX.fullmatch(exit_proof["ownership_token_hash"])
            or exit_proof["ownership_token_hash"] != intent["process_ownership_token_hash"]
            or timestamp(exit_proof.get("started_at")) < timestamp(intent["recorded_at"])
            or timestamp(exit_proof.get("started_at")) > timestamp(exit_proof.get("exited_at"))
            or timestamp(exit_proof["exited_at"]) > timestamp(observed)):
        raise SafetyError("SOURCE_ACQUISITION_ZERO_PROOF_REQUIRED")
    return _commit_stage(memory, old, "prelaunch_zero", {"receipt": proof, "artifacts": artifacts,
        "observed_at": observed, "process_proof_hash": digest(exit_proof)}, as_of,
        {"state": "reserved", "active_response_id": None, "active_parse_id": None, "parse_state": "not_started"}, files=files)
