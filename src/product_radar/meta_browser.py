"""Read visible public Meta library cards without Apify or an account action.

Only an actual visible library ID identifies an ad. Unreadable startup,
challenge, empty search and incomplete cards stay different outcomes. Raw
cards are saved before import; reconciliation never starts another browser.
"""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import ipaddress
import json
import re
from urllib.parse import urlencode, urlsplit, parse_qs

from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .native_public_browser import native_snapshot
from .research_memory import import_capture
from .source_deadline import (SourceActionDeadline, interrupted_transport, replay_native_deadline,
                              saved_completed_transport, validate_interrupted_transport, source_phase)
from .storage import Journal, atomic_bytes, atomic_json, load_json, safe_path

VERSION = "public-meta-library-v1"
LEGACY_PARSER_VERSION = "visible-cards-dates-v2"
PARSER_VERSION = "visible-cards-creative-v3"
MAX_BYTES = 4 * 1024 * 1024
EXTRACT = r"""JSON.stringify((()=>{
 const all=document.body?.innerText||'';const cards=[];const seen=new Set();
 for(const node of document.querySelectorAll('span,div,a')){
  const own=(node.innerText||'').trim();if(own.length>160)continue;
  const match=own.match(/Library ID:\s*(\d{1,40})/i);if(!match||seen.has(match[1]))continue;
  let card=node;for(let n=node.parentElement;n;n=n.parentElement){
   const text=(n.innerText||'');const ids=text.match(/Library ID:\s*\d+/gi)||[];
   if(text.length>12000||ids.length>1)break;card=n;
  }
  seen.add(match[1]);cards.push({ad_id:match[1],text:(card.innerText||'').slice(0,12000),
   links:[...card.querySelectorAll('a[href]')].map(a=>({url:a.href,text:(a.innerText||'').slice(0,500)})).slice(0,40),
   images:[...card.querySelectorAll('img[src]')].slice(0,12).map(i=>{
    const box=i.getBoundingClientRect(),style=getComputedStyle(i),link=i.closest('a[href]');
    return {url:i.currentSrc||i.src,alt:(i.alt||'').slice(0,500),
     natural_width:i.naturalWidth,natural_height:i.naturalHeight,
     display_width:Math.round(box.width),display_height:Math.round(box.height),
     visible:i.getClientRects().length>0&&style.display!=='none'&&style.visibility!=='hidden'&&style.opacity!=='0',
     link_url:link?link.href:null};})});
  if(cards.length>=50)break;
 }
 return {url:location.href,title:document.title,text:all.slice(0,70000),text_truncated:all.length>70000,cards};
})())"""


def prepare_meta_query(query, *, limit=30):
    if (not isinstance(query, str) or not query.strip() or len(query) > 160
            or any(ord(char) < 32 for char in query) or type(limit) is not int or not 1 <= limit <= 50):
        raise SafetyError("META_PUBLIC_QUERY_INVALID")
    query = " ".join(query.split())
    return {"query": query, "country": "IN", "limit": limit,
            "url": "https://www.facebook.com/ads/library/?" + urlencode({
                "active_status": "all", "ad_type": "all", "country": "IN", "q": query,
                "search_type": "keyword_unordered"})}


def _page_url(value):
    if not isinstance(value, str): return None
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname not in {"www.facebook.com", "facebook.com"}
                or url.username or url.password): return None
        if url.path.startswith(("/ads/", "/login", "/privacy", "/policies", "/help", "/share")): return None
        return value[:4096]
    except ValueError:
        return None


def _creative_image(card, advertiser_url):
    """Prefer explicitly visible, adequately sized creative, not page avatars.

    This is a placement/size check, not a claim that the image matches a product.
    Old snapshots without this metadata cannot establish an image's role.
    """
    from .product_images import report_image_quality
    from .public_pages import _public_url
    candidates = []
    images = card.get("images", [])
    if not isinstance(images, list):
        return None
    for index, row in enumerate(images[:12]):
        if not isinstance(row, dict) or row.get("visible") is not True:
            continue
        try:
            if (not report_image_quality(row.get("natural_width"), row.get("natural_height"))
                    or not report_image_quality(row.get("display_width"), row.get("display_height"))):
                continue
            url, host, _, scheme = _public_url(row.get("url"))
            if scheme != "https":
                continue
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and (not address.is_global or address.is_multicast or address.is_reserved):
                continue
        except SafetyError:
            continue
        # A photo nested in the observed advertiser's page link is its avatar,
        # even if the CDN original is large. Do not infer this from alt text.
        link = _page_url(row.get("link_url"))
        if link and advertiser_url:
            link_parts, page_parts = urlsplit(link), urlsplit(advertiser_url)
            if (link_parts.path.rstrip("/").lower(), link_parts.query) == (page_parts.path.rstrip("/").lower(), page_parts.query):
                continue
        candidates.append((-(row["display_width"] * row["display_height"]),
            -(row["natural_width"] * row["natural_height"]), index, url))
    return min(candidates)[3] if candidates else None


@source_phase("parse")
def parse_meta_snapshot(snapshot, request, navigation_status, *, legacy_dates=False, legacy_images=False):
    if type(legacy_dates) is not bool or type(legacy_images) is not bool:
        raise SafetyError("META_PUBLIC_PARSER_OPTION_INVALID")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("cards"), list):
        raise SafetyError("META_PUBLIC_SNAPSHOT_INVALID")
    seen_url = urlsplit(snapshot.get("url", ""))
    query = parse_qs(seen_url.query)
    if (seen_url.hostname not in {"www.facebook.com", "facebook.com"}
            or seen_url.path.rstrip("/") != "/ads/library"
            or query.get("q") != [request["query"]] or query.get("country") != ["IN"]):
        return {"state": "blocked", "reason": "Meta did not return the requested public India search.", "records": []}
    if navigation_status in {401, 403, 429}:
        return {"state": "blocked", "reason": "Meta returned HTTP " + str(navigation_status) + ".", "records": []}
    if navigation_status != 200:
        return {"state": "failed", "reason": "The main Meta page response was not confirmed successful.", "records": []}
    records, rejected, ids = [], [], set()
    for index, card in enumerate(snapshot["cards"][:request["limit"]]):
        if (not isinstance(card, dict) or not isinstance(card.get("ad_id"), str)
                or not re.fullmatch(r"[0-9]{1,40}", card["ad_id"])
                or not isinstance(card.get("text"), str)
                or not re.search(r"Library ID:\s*" + re.escape(card["ad_id"]) + r"\b", card["text"], re.I)):
            rejected.append({"row": index, "reason": "Visible library identity was not present."}); continue
        if card["ad_id"] in ids: continue
        ids.add(card["ad_id"])
        body = card["text"][:12000]
        active = True if re.search(r"(?m)^\s*Active\s*$", body) else False if re.search(r"(?m)^\s*Inactive\s*$", body) else None
        date = re.search(r"Started running on\s+(" + (r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}" if legacy_dates else
                         r"(?:[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})") + r")", body, re.I)
        started = None
        if date:
            for style in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"):
                try: started = datetime.strptime(date[1], style).replace(tzinfo=timezone.utc).isoformat(); break
                except ValueError: continue
        links = [row for row in card.get("links", []) if isinstance(row, dict)]
        advertiser_url = next((_page_url(row.get("url")) for row in links if _page_url(row.get("url"))), None)
        advertiser_id = None
        if advertiser_url:
            numbers = parse_qs(urlsplit(advertiser_url).query).get("id", [])
            if len(numbers) == 1 and re.fullmatch(r"[0-9]{1,40}", numbers[0]): advertiser_id = numbers[0]
        if legacy_images:
            images = [row["url"] for row in card.get("images", []) if isinstance(row, dict)
                      and isinstance(row.get("url"), str) and row["url"].startswith("https://")]
            image = images[0] if images else None
        else:
            image = _creative_image(card, advertiser_url)
        limitations = [name for name, value in (("advertiser ID not visible", advertiser_id),
                         ("active status not visible", active), ("start date not visible", started)) if value is None]
        records.append({"ad_id": card["ad_id"], "advertiser_id": advertiser_id,
                        "advertiser_url": advertiser_url, "active": active, "started_at": started,
                        "library_url": "https://www.facebook.com/ads/library/?id=" + card["ad_id"],
                        "purpose": "validation", "raw_references": [{"row": index}], "limitations": limitations,
                        "presentation": {"body": body, **({"original_image_url": image} if image else {})}})
    text = snapshot.get("text", "")
    if records:
        return {"state": "partial" if rejected or len(snapshot["cards"]) > request["limit"] else "usable",
                "reason": "Visible public library cards were captured; product relevance still needs review.", "records": records, "rejected": rejected}
    if re.search(r"no ads (?:match|found)|no results (?:found|for)|we couldn.t find any ads", text, re.I):
        return {"state": "empty", "reason": "The requested Meta search explicitly returned no ads.", "records": [], "rejected": rejected}
    if re.search(r"temporarily blocked|too many requests|confirm you.re human|something went wrong", text, re.I):
        return {"state": "blocked", "reason": "Meta displayed an error or access challenge, not an empty search.", "records": [], "rejected": rejected}
    return {"state": "insufficient", "reason": "The page did not expose recognisable public ad cards or an explicit empty result.", "records": [], "rejected": rejected}


def _saved_meta_receipt(folder, path, acquisition_id, request):
    receipt = load_json(path, MAX_BYTES)
    if (not isinstance(receipt, dict) or receipt.get("version") != VERSION or receipt.get("request") != request
            or receipt.get("acquisition_id") != acquisition_id):
        raise SafetyError("META_PUBLIC_RECEIPT_CONFLICT")
    parser_version = receipt.get("parser_version")
    if parser_version is not None and (not isinstance(parser_version, str)
            or parser_version not in {LEGACY_PARSER_VERSION, PARSER_VERSION}):
        raise SafetyError("META_PUBLIC_PARSER_VERSION_INVALID")
    snapshot_file = receipt.get("snapshot_file", "snapshot.json")
    if snapshot_file not in {"snapshot.json", "deadline_snapshot.json", "deadline_failed_snapshot.json"}:
        raise SafetyError("META_PUBLIC_CAPTURE_CHANGED")
    raw_path = safe_path(folder / snapshot_file)
    if not raw_path.is_file() or raw_path.stat().st_size > MAX_BYTES:
        raise SafetyError("META_PUBLIC_CAPTURE_CHANGED")
    raw = raw_path.read_bytes()
    if sha256(raw).hexdigest() != receipt.get("raw_hash"):
        raise SafetyError("META_PUBLIC_CAPTURE_CHANGED")
    snapshot = json.loads(raw)
    if receipt.get("outer_deadline") is True:
        artifacts = receipt.get("artifacts")
        if (snapshot is not None or not isinstance(artifacts, dict) or not artifacts
                or not set(artifacts) <= {"outer_deadline.json", "native_result.json", "native_dispatch.json", "native_outer_deadline.json", "snapshot.json"}
                or "outer_deadline.json" not in artifacts):
            raise SafetyError("META_PUBLIC_CAPTURE_CHANGED")
        for name, expected in artifacts.items():
            original = safe_path(folder / name)
            limit = 12 * 1024 * 1024 if name == "native_result.json" else MAX_BYTES if name == "snapshot.json" else 16384
            if (not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)
                    or not original.is_file() or original.stat().st_size > limit
                    or sha256(original.read_bytes()).hexdigest() != expected):
                raise SafetyError("META_PUBLIC_CAPTURE_CHANGED")
        proof = validate_interrupted_transport(load_json(folder / "outer_deadline.json", 16384), requested_url=request["url"])
        if "native_outer_deadline.json" in artifacts:
            native = replay_native_deadline(folder, requested_url=request["url"])
            for field in ("navigation_started", "navigation_started_at", "owned_process_exited"):
                if proof.get(field) != native.get(field):
                    raise SafetyError("META_PUBLIC_CAPTURE_CHANGED")
        if (receipt.get("dispatch_started") != proof["navigation_started"]
                or receipt.get("owned_process_exited") != proof["owned_process_exited"]):
            raise SafetyError("META_PUBLIC_CAPTURE_CHANGED")
        parsed = {"state": "failed", "reason": "SOURCE_ACTION_TIMEOUT", "records": [], "rejected": []}
    elif snapshot is not None:
        parsed = parse_meta_snapshot(snapshot, request, receipt.get("navigation_status"),
                                     legacy_dates=parser_version is None,
                                     legacy_images=parser_version in {None, LEGACY_PARSER_VERSION})
    else:
        parsed = None  # Historical null captures keep their original contract.
    if parsed is not None and (digest(parsed) != receipt.get("mapping_hash")
            or any(receipt.get(field) != value for field, value in parsed.items())):
        raise SafetyError("META_PUBLIC_MAPPING_CHANGED")
    timestamp(receipt.get("observed_at"))
    return receipt


def _save_meta_deadline(folder, acquisition_id, request, started, error, completed):
    """Retain one failed wrapper and bind any completed original native bytes."""
    # A signal can arrive just after the successful atomic receipt write.
    # Preserve that original outcome; saved-only replay will verify it. Never
    # replace it with a failed receipt merely because Python had not returned.
    if (folder / "receipt.json").exists():
        return None
    proof = interrupted_transport(error, folder=folder, requested_url=request["url"], completed_transport=completed)
    atomic_json(folder / "outer_deadline.json", proof)
    artifacts = {}
    for name, limit in (("outer_deadline.json", 16384), ("native_dispatch.json", 16384),
                        ("native_outer_deadline.json", 16384), ("native_result.json", 12 * 1024 * 1024),
                        ("snapshot.json", MAX_BYTES)):
        path = safe_path(folder / name)
        if path.is_file() and path.stat().st_size <= limit:
            artifacts[name] = sha256(path.read_bytes()).hexdigest()
    parsed = {"state": "failed", "reason": "SOURCE_ACTION_TIMEOUT", "records": [], "rejected": []}
    raw = canonical(None)
    atomic_bytes(folder / "deadline_failed_snapshot.json", raw)
    observed = completed.get("completed_at") if isinstance(completed, dict) else None
    receipt = {"version": VERSION, "parser_version": PARSER_VERSION, "acquisition_id": acquisition_id, "request": request,
        "observed_at": observed or proof["completed_at"], **parsed, "raw_hash": sha256(raw).hexdigest(),
        "mapping_hash": digest(parsed), "dispatch_started": proof["navigation_started"],
        "navigation_status": proof.get("navigation_status"), "owned_process_exited": proof["owned_process_exited"],
        "apify_usd": "0", "model_calls": 0, "outer_deadline": True, "started_at": started,
        "completed_at": proof["completed_at"], "artifacts": artifacts, "transport_attempts": ["owned_native_windows_chrome"],
        "snapshot_file": "deadline_failed_snapshot.json"}
    atomic_json(folder / "receipt.json", receipt)
    return receipt


def _reconcile_meta_deadline(folder, original, acquisition_id, request):
    with Journal(folder / "deadline_reparse_lock").locked():
        actual = saved_completed_transport(folder, original, snapshot_file="snapshot.json", requested_url=request["url"],
                                          snapshot_limit=MAX_BYTES)
        if actual is None:
            return original
        path = folder / "deadline_reparse.json"
        saved = _saved_meta_receipt(folder, path, acquisition_id, request) if path.exists() else None
        # A successful older reparse owns its exact parser and mapping. A
        # parser upgrade is not permission to replace that completed receipt.
        parser_version = saved.get("parser_version") if saved is not None else PARSER_VERSION
        parser_fields = ({"parser_version": parser_version}
            if saved is None or "parser_version" in saved else {})
        parsed = parse_meta_snapshot(actual["snapshot"], request, actual.get("navigation_status"),
            legacy_dates=parser_version is None, legacy_images=parser_version in {None, LEGACY_PARSER_VERSION})
        raw = canonical(actual["snapshot"])
        if len(raw) > MAX_BYTES:
            raise SafetyError("META_PUBLIC_CAPTURE_LIMIT")
        recovery = {"version": "source-deadline-reparse-v1",
            "original_receipt_sha256": sha256((folder / "receipt.json").read_bytes()).hexdigest(),
            "original_artifacts_hash": digest(original["artifacts"])}
        receipt = {"version": VERSION, **parser_fields, "acquisition_id": acquisition_id, "request": request,
            "observed_at": actual["completed_at"], **parsed, "raw_hash": sha256(raw).hexdigest(), "mapping_hash": digest(parsed),
            "dispatch_started": True, "navigation_status": actual.get("navigation_status"), "owned_process_exited": True,
            "apify_usd": "0", "model_calls": 0, "recovery": recovery,
            "receipt_file": "deadline_reparse.json", "snapshot_file": "deadline_snapshot.json"}
        if saved is not None:
            if saved != receipt:
                raise SafetyError("META_PUBLIC_CAPTURE_CHANGED")
        else:
            atomic_bytes(folder / "deadline_snapshot.json", raw)
            atomic_json(path, receipt)
        return receipt


def capture_meta_search(root, acquisition_id, query, *, timeout=45, headed=False, browser=native_snapshot, reconcile=False,
                        retain_transport=False):
    identifier(acquisition_id); root = safe_path(root)
    folder = safe_path(root, acquisition_id); request = prepare_meta_query(query)
    path = folder / "receipt.json"
    intent = {"version": VERSION, "acquisition_id": acquisition_id, "request": request}
    intent_path = folder / "intent.json"
    if intent_path.exists() and load_json(intent_path, MAX_BYTES) != intent:
        raise SafetyError("META_PUBLIC_RECEIPT_CONFLICT")
    if path.is_file():
        receipt = _saved_meta_receipt(folder, path, acquisition_id, request)
        if reconcile and receipt.get("outer_deadline") is True:
            receipt = _reconcile_meta_deadline(folder, receipt, acquisition_id, request)
        return receipt, folder
    folder.mkdir(parents=True, exist_ok=True)
    native_file = safe_path(folder / "native_result.json")
    started = datetime.now(timezone.utc).isoformat(); result = None
    try:
        if (folder / "native_outer_deadline.json").is_file():
            if not intent_path.is_file():
                raise SafetyError("META_PUBLIC_RECEIPT_CONFLICT")
            original = replay_native_deadline(folder, requested_url=request["url"])
            error = SourceActionDeadline(); error.native_result = original
            receipt = _save_meta_deadline(folder, acquisition_id, request, original.get("navigation_started_at") or original["completed_at"], error, None)
            return receipt, folder
        if native_file.is_file():
            # A crash after browser capture is a save/import recovery, not a
            # new request. Preserve the original child completion date.
            result = load_json(native_file, 12 * 1024 * 1024)
            if result.get("navigation_started_at"):
                started = result["navigation_started_at"]
        elif reconcile and retain_transport and (folder / "transport.json").is_file() and (folder / "snapshot.json").is_file():
            # A parser/local save error after the real read leaves its owned
            # bytes and returned transport intact. Recover only those bytes;
            # lack of a parser receipt never authorises another navigation.
            proof = load_json(folder / "transport.json", MAX_BYTES)
            snapshot = load_json(folder / "snapshot.json", MAX_BYTES)
            if (not intent_path.is_file() or load_json(intent_path, MAX_BYTES) != intent
                    or proof.get("contract") != "meta-original-transport-v1"
                    or proof.get("requested_url") != request["url"] or proof.get("snapshot_hash") != digest(snapshot)):
                raise SafetyError("META_PUBLIC_CAPTURE_CHANGED")
            result = dict(proof, snapshot=snapshot)
            result["navigation_retry_after"] = proof.get("retry_after")
            started = proof.get("navigation_started_at") or started
        elif reconcile:
            raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
        else:
            if not intent_path.is_file():
                atomic_json(intent_path, intent)
            with source_phase("transport"):
                result = browser(request["url"], EXTRACT, folder, timeout=min(45, max(15, int(timeout))), headed=headed)
        observed = result.get("completed_at") or utc_now(); timestamp(observed)
        if retain_transport:
            # Supporting original transport facts, not a new response/date.
            # Keep old receipt/parser bytes and default behaviour unchanged.
            proof = {field: result.get(field) for field in ("navigation_started", "navigation_started_at",
                "navigation_status", "owned_process_exited", "completed_at")}
            proof.update(contract="meta-original-transport-v1", requested_url=request["url"],
                         snapshot_hash=digest(result.get("snapshot")), retry_after=result.get("navigation_retry_after"))
            proof["completed_at"] = observed
            transport_path = folder / "transport.json"
            if transport_path.exists() and load_json(transport_path, MAX_BYTES) != proof:
                raise SafetyError("META_PUBLIC_CAPTURE_CHANGED")
            if not transport_path.exists(): atomic_json(transport_path, proof)
        snapshot = result.get("snapshot"); raw = canonical(snapshot)
        if len(raw) > MAX_BYTES: raise SafetyError("META_PUBLIC_CAPTURE_LIMIT")
        atomic_bytes(folder / "snapshot.json", raw)
        parsed = (parse_meta_snapshot(snapshot, request, result.get("navigation_status")) if snapshot is not None
                  else {"state": "failed" if result.get("navigation_started") else "unavailable",
                        "reason": result["reason"], "records": [], "rejected": []})
        receipt = {"version": VERSION, "parser_version": PARSER_VERSION, "acquisition_id": acquisition_id, "request": request,
                   "observed_at": observed, **parsed, "raw_hash": sha256(raw).hexdigest(), "mapping_hash": digest(parsed),
                   "dispatch_started": result.get("navigation_started"), "navigation_status": result.get("navigation_status"),
                   "owned_process_exited": result.get("owned_process_exited"), "apify_usd": "0", "model_calls": 0}
        atomic_json(path, receipt)
        return receipt, folder
    except SourceActionDeadline as error:
        try:
            _save_meta_deadline(folder, acquisition_id, request, started, error, result)
        except (OSError, SafetyError, TypeError, ValueError):
            error.meta_proof_save_error = "SOURCE_DEADLINE_PROOF_SAVE_FAILED"
        raise


def meta_direct_source_handlers(memory, *, data_root=None, browser=native_snapshot, headed=False, shared_acquisitions=False):
    if type(shared_acquisitions) is not bool: raise SafetyError("SOURCE_META_SHARED_OPTION_INVALID")
    root = safe_path(data_root or memory.root / "direct_meta")
    def prepare(context):
        request = context["request"]
        if (not isinstance(request, dict) or not set(request) <= {"query", "country", "limit", "url"}
                or request.get("country", "IN") != "IN" or request.get("limit", 30) != 30):
            raise SafetyError("SOURCE_META_REQUEST_INVALID")
        actual = prepare_meta_query(request.get("query"))
        if "url" in request and request != actual: raise SafetyError("SOURCE_META_REQUEST_INVALID")
        return actual
    def collect(context, reconcile=False):
        if shared_acquisitions and not reconcile:
            from .source_coverage import guard_shared_source_launch
            guard_shared_source_launch(memory, context, prepare(context))
        original_folder = safe_path(context.get("source_original_folder", root / context["acquisition_id"]))
        if original_folder.name != context["acquisition_id"]: raise SafetyError("SOURCE_META_REQUEST_INVALID")
        receipt, folder = capture_meta_search(original_folder.parent, context["acquisition_id"], context["request"]["query"],
            timeout=context.get("timeout_seconds", 45), headed=headed, browser=browser, reconcile=reconcile,
            retain_transport=shared_acquisitions)
        if receipt["dispatch_started"] is True and context.get("record_dispatch"):
            context["record_dispatch"]()
        mapped = {"status": "usable" if receipt["state"] == "usable" else "partial" if receipt["records"] else "unusable",
                  "records": receipt["records"], "rejected": receipt.get("rejected", [])}
        reference = str(folder / receipt.get("receipt_file", "receipt.json"))
        kwargs = {"recovery": receipt["recovery"]} if receipt.get("recovery") is not None else {}
        imported = import_capture(memory, source="meta", acquisition_id=context["acquisition_id"],
            observed_at=receipt["observed_at"], mapped=mapped, capture_hash=receipt["raw_hash"], reference=reference, **kwargs)
        state = memory.snapshot()
        observations = sorted(i for i, row in state["observations"].items() if row["capture_id"] == imported["capture_id"])
        source_state, reason = receipt["state"], receipt["reason"]
        target = "interpretation_pending" if observations else "completed_no_results" if source_state == "empty" else \
                 "not_attempted" if receipt["dispatch_started"] is False else "failed" if receipt["state"] == "unavailable" else receipt["state"]
        if shared_acquisitions:
            from .source_acquisitions import record_meta_response
            from .source_coverage import _record_marketplace_access, source_access_scope, source_access_wait
            commit_at = max(context["as_of"], utc_now(), key=timestamp)
            response = record_meta_response(memory, context["acquisition_id"], folder=folder,
                capture_ids=[imported["capture_id"]], as_of=commit_at)
            _record_marketplace_access(memory, context["acquisition_id"], response["response_id"], as_of=commit_at)
            original = memory.snapshot()["source_checks"][response["response_id"]]
            if source_state == "empty" and not original["complete"]:
                source_state, target, reason = "insufficient", "insufficient", "META_EMPTY_RESPONSE_COVERAGE_INCOMPLETE"
            elif source_state == "usable" and not original["complete"]:
                source_state = "partial"
            elif source_state == "blocked" and original["source_state"] == "insufficient":
                source_state, target, reason = "insufficient", "insufficient", "META_ORIGINAL_SCOPE_MISMATCH"
        return {"schema_version": 1, "route": "meta-ads", "acquisition_id": context["acquisition_id"],
                "observed_at": receipt["observed_at"], "state": source_state, "dispatch_started": receipt["dispatch_started"],
                "reference": reference, "capture_ids": [imported["capture_id"]], "observation_ids": observations,
                "target_results": [{"concept_id": cid, "description_hash": context["description_hashes"][cid],
                    "state": target, "evidence_refs": observations, "reason": reason} for cid in context["concept_ids"]],
                "reason": reason, "retryable": receipt.get("navigation_status") in {429, 500, 502, 503, 504},
                "failure_scope": "route" if source_state == "blocked" else "question",
                **({"access_wait": source_access_wait(memory.snapshot(), source_access_scope(prepare(context)["url"]),
                                                       as_of=context["as_of"])} if shared_acquisitions else {})}
    handlers = {"handler": collect, "reconcile": lambda context: collect(context, reconcile=True)}
    if shared_acquisitions:
        from .source_coverage import direct_source_resolution_hooks
        handlers.update(direct_source_resolution_hooks(memory, route="meta-ads", root=root, prepare=prepare,
            saved=lambda context: collect(context, reconcile=True)))
    return handlers
