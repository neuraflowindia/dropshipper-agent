"""An advertiser's other active ads in the public Meta Ad Library (Plan R10 W8).

The same free direct read as the heading searches (``meta_browser``): the
owned hidden browser opens the advertiser's page of the Ad Library (India,
active ads only), copies the visible cards with the searches' own expression
(``meta_browser.EXTRACT``) and reads them with the same card rules
(``meta_browser.card_outcome``). Nothing is clicked, no account is used and
no Apify Actor runs. The raw page is saved before it is parsed; a saved
receipt is reused, a browser result saved before a crash is parsed instead of
a new read, and a reconciliation never starts the browser.
"""

from hashlib import sha256
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from .meta_browser import EXTRACT, MAX_BYTES, card_outcome
from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .native_public_browser import native_snapshot
from .storage import atomic_bytes, atomic_json, load_json, safe_path

VERSION = "public-meta-advertiser-v1"
BRIDGE = "verified-meta-advertiser-response-v1"
PARSER_VERSION = "advertiser-cards-v1"


def prepare_advertiser_request(advertiser_id, *, limit=30):
    """The advertiser's active India ads in the Ad Library ("See all ads" of its page)."""
    if (not isinstance(advertiser_id, str) or not re.fullmatch(r"[0-9]{1,40}", advertiser_id)
            or type(limit) is not int or not 1 <= limit <= 50):
        raise SafetyError("META_ADVERTISER_REQUEST_INVALID")
    return {"advertiser_id": advertiser_id, "country": "IN", "limit": limit,
            "url": "https://www.facebook.com/ads/library/?" + urlencode({
                "active_status": "active", "ad_type": "all", "country": "IN", "view_all_page_id": advertiser_id,
                "search_type": "page", "media_type": "all"})}


def parse_advertiser_snapshot(snapshot, request, navigation_status):
    """The page's cards, only when Meta returned this advertiser's India page."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("cards"), list):
        raise SafetyError("META_PUBLIC_SNAPSHOT_INVALID")
    seen = urlsplit(snapshot.get("url", ""))
    query = parse_qs(seen.query)
    if (seen.hostname not in {"www.facebook.com", "facebook.com"} or seen.path.rstrip("/") != "/ads/library"
            or query.get("view_all_page_id") != [request["advertiser_id"]] or query.get("country") != ["IN"]):
        # Another page is not this advertiser's page; only an HTTP refusal or a
        # challenge shown on the page is a site refusal.
        return {"state": "insufficient", "reason": "Meta did not return the requested advertiser's India page.", "records": []}
    if navigation_status in {401, 403, 429}:
        return {"state": "blocked", "reason": "Meta returned HTTP " + str(navigation_status) + ".", "records": []}
    if navigation_status != 200:
        return {"state": "failed", "reason": "The main Meta page response was not confirmed successful.", "records": []}
    return card_outcome(snapshot, request["limit"])


def _saved(folder, path, acquisition_id, request):
    receipt = load_json(path, MAX_BYTES)
    raw = safe_path(folder / "snapshot.json")
    if (not isinstance(receipt, dict) or receipt.get("version") != VERSION or receipt.get("parser_version") != PARSER_VERSION
            or receipt.get("request") != request or receipt.get("acquisition_id") != acquisition_id
            or not raw.is_file() or raw.stat().st_size > MAX_BYTES
            or sha256(raw.read_bytes()).hexdigest() != receipt.get("raw_hash")):
        raise SafetyError("META_ADVERTISER_RECEIPT_CONFLICT")
    snapshot = json.loads(raw.read_bytes())
    parsed = (parse_advertiser_snapshot(snapshot, request, receipt.get("navigation_status")) if snapshot is not None
              else {name: receipt.get(name) for name in ("state", "reason", "records")})
    if digest(parsed) != receipt.get("mapping_hash"):
        raise SafetyError("META_ADVERTISER_MAPPING_CHANGED")
    timestamp(receipt.get("observed_at"))
    return receipt


def capture_advertiser_ads(root, acquisition_id, advertiser_id, *, timeout=45, browser=native_snapshot, reconcile=False):
    """One read of the advertiser's page; returns ``(receipt, folder)``."""
    identifier(acquisition_id)
    folder = safe_path(safe_path(root), acquisition_id)
    request = prepare_advertiser_request(advertiser_id)
    path, intent_path = folder / "receipt.json", folder / "intent.json"
    intent = {"version": VERSION, "acquisition_id": acquisition_id, "request": request}
    if intent_path.exists() and load_json(intent_path, MAX_BYTES) != intent:
        raise SafetyError("META_ADVERTISER_RECEIPT_CONFLICT")
    if path.is_file():
        return _saved(folder, path, acquisition_id, request), folder
    native = folder / "native_result.json"
    if native.is_file():
        # A crash after the browser returned is a save, not a new read.
        if not intent_path.is_file():
            raise SafetyError("META_ADVERTISER_RECEIPT_CONFLICT")
        result = load_json(native, 12 * 1024 * 1024)
    elif reconcile:
        raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
    else:
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(intent_path, intent)
        result = browser(request["url"], EXTRACT, folder, timeout=min(45, max(15, int(timeout))), headed=False)
    observed = result.get("completed_at") or utc_now()
    timestamp(observed)
    snapshot = result.get("snapshot")
    raw = canonical(snapshot)
    if len(raw) > MAX_BYTES:
        raise SafetyError("META_PUBLIC_CAPTURE_LIMIT")
    atomic_bytes(folder / "snapshot.json", raw)
    parsed = (parse_advertiser_snapshot(snapshot, request, result.get("navigation_status")) if snapshot is not None
              else {"state": "failed" if result.get("navigation_started") else "unavailable",
                    "reason": result.get("reason") or "META_ADVERTISER_READ_FAILED", "records": []})
    receipt = {"version": VERSION, "parser_version": PARSER_VERSION, "acquisition_id": acquisition_id, "request": request,
               "observed_at": observed, **parsed, "raw_hash": sha256(raw).hexdigest(), "mapping_hash": digest(parsed),
               "dispatch_started": result.get("navigation_started"), "navigation_status": result.get("navigation_status"),
               "owned_process_exited": result.get("owned_process_exited"), "retry_after": result.get("navigation_retry_after"),
               "apify_usd": "0", "model_calls": 0}
    atomic_json(path, receipt)
    return receipt, folder


def record_advertiser_response(memory, acquisition_id, *, folder, as_of):
    """Bind the saved advertiser page to its source acquisition (``BRIDGE``),
    with the access answer the shared site wait reads. Saved files only."""
    from .source_acquisitions import _commit_stage, _file, _record, _stage
    state = memory.snapshot()
    old = _record(state, acquisition_id)
    request, roots = old["binding"]["request"], old["binding"]["artifact_roots"]
    folder = safe_path(folder)
    advertiser = parse_qs(urlsplit(request.get("url", "")).query).get("view_all_page_id", [None])[0]
    if old["binding"]["route"] != "meta-ads" or folder.name != acquisition_id or advertiser is None:
        raise SafetyError("META_ADVERTISER_RECEIPT_SCOPE_INVALID")
    receipt, _ = capture_advertiser_ads(folder.parent, acquisition_id, advertiser, reconcile=True)
    if {name: receipt["request"][name] for name in ("url", "country", "limit")} != request:
        raise SafetyError("META_ADVERTISER_RECEIPT_SCOPE_INVALID")
    observed = receipt["observed_at"]
    if timestamp(observed) > timestamp(as_of):
        raise SafetyError("SOURCE_ACQUISITION_SOURCE_DATE_INVALID")
    proof, stat, _ = _file(folder / "receipt.json", roots)
    files, artifacts = [(proof, stat)], {}
    for name in ("snapshot.json", "intent.json", "native_result.json"):
        if (folder / name).is_file():
            artifact, artifact_stat, raw = _file(folder / name, roots)
            files.append((artifact, artifact_stat)); artifacts[name] = artifact
    snapshot = json.loads((folder / "snapshot.json").read_bytes())
    for previous_id in old["response_ids"]:
        if timestamp(_stage(state, old, previous_id)["observed_at"]) > timestamp(observed):
            raise SafetyError("SOURCE_ACQUISITION_RECEIPT_CONFLICT")
    closed = receipt.get("owned_process_exited") is True
    recognized = receipt["state"] in ("usable", "partial", "empty")
    coverage = {"body_complete": isinstance(snapshot, dict),
                "byte_truncated": bool(isinstance(snapshot, dict) and snapshot.get("text_truncated") is True),
                "scope_complete": recognized, "recognized": recognized, "delivered_count": len(receipt["records"]),
                "intentional_limit": bool(isinstance(snapshot, dict) and len(snapshot.get("cards", [])) > request["limit"])}
    complete = bool(closed and receipt["dispatch_started"] is True and recognized and coverage["body_complete"]
                    and not coverage["byte_truncated"])
    data = {"bridge_contract": BRIDGE, "receipt": proof, "artifacts": artifacts, "observed_at": observed,
            "dispatch_started": receipt["dispatch_started"], "source_state": receipt["state"], "coverage": coverage,
            "complete": complete, "captures": [], "response_hash": receipt["raw_hash"],
            "expense_proof": {"collection_channel": "direct_local", "provider_charge_usd": receipt["apify_usd"],
                              "model_calls": receipt["model_calls"]},
            "access": {"http_status": receipt.get("navigation_status"), "owned_process_exited": closed,
                       "retry_after": receipt.get("retry_after")}}
    status = ("completed_empty" if complete and receipt["state"] == "empty" else
              "failed_access" if receipt["state"] == "blocked" and closed else
              "response_saved" if receipt["dispatch_started"] is True else "execution_unknown")
    return _commit_stage(memory, old, "response", data, as_of,
                         {"state": status, "active_parse_id": None, "parse_state": "not_started"}, files=files)
