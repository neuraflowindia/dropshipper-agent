"""Independent, reusable public supplier searches and discovered company reads.

Search cards only nominate real pages. They do not confirm suppliers, quotes,
subscription fees, single-piece dispatch or white labelling. Original public
receipts are cached by family request and retained on replay, including failed
reads. Each product gets its own description-bound interpretation question.
"""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

from .discovery import normalise, tokens
from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .public_pages import MAX_HTML, MAX_TEXT, fetch_public_page
from .research_memory import key
from .source_deadline import SourceActionDeadline, replay_native_deadline, saved_completed_transport, source_phase
from .storage import Journal, atomic_json, load_json, safe_path
from .supplier_capture import _allowed_url
from .supplier_capture_store import (MAX_CAPTURE_BYTES, _capture_key, _product,
    _validate as _validate_bundle_record, capture_saved_supplier_bundle, load_saved_supplier_capture)
from .supplier_discovery import _parse_receipt, _search_url
from .supplier_effort import _receipt_ref, failed_reader_receipt, record_supplier_effort

VERSION = "supplier-public-routes-v1"
PARSER_VERSION = 3
SEARCH_ROUTES = ("google-suppliers", "google-maps", "indiamart", "tradeindia")
ROUTES = (*SEARCH_ROUTES, "company-pages")
MAX_RESULTS = 10
MAX_COMPANIES = 6
MAX_RECORD_BYTES = 4 * 1024 * 1024
_HOSTS = {"google-suppliers": {"google.com", "www.google.com", "google.co.in", "www.google.co.in"},
          "google-maps": {"google.com", "www.google.com", "maps.google.com"},
          "indiamart": {"indiamart.com", "www.indiamart.com", "dir.indiamart.com"},
          "tradeindia": {"tradeindia.com", "www.tradeindia.com"}}
_CHALLENGES = ("our systems have detected unusual traffic", "verify you are human", "checking your browser",
               "before you continue to google", "access denied", "enable javascript and cookies to continue")
_NO_RESULTS = {"google-maps": ("google maps can't find", "no results found"),
               "indiamart": ("no products found", "no results found", "no product found"),
               "tradeindia": ("no products found", "no results found", "no product found", "no search results")}
_INDIAMART_SERVICE_HOSTS = {"dir", "my", "lens", "export", "seller", "help", "login", "buyer", "pay",
                          "apps", "app", "careers", "corporate", "news", "m", "proposal"}
_DOM = """JSON.stringify((() => {
  const html = document.documentElement.outerHTML;
  const text = document.body?.innerText || '';
  return {url:location.href,title:document.title.slice(0,2000),
    html:html.length > 1048576 ? null : html,
    text:text.slice(0,24000),text_truncated:text.length > 24000,
    links:Array.from(document.querySelectorAll('a[href]')).map(a=>a.href).filter(u=>/^https?:/.test(u)).slice(0,60)};
})())"""


def _query(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 120
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or re.search(r"https?://|www\.|[<>]", value, re.I)):
        raise SafetyError("SUPPLIER_ROUTE_QUERY_INVALID")
    return " ".join(value.split())


def supplier_route_url(route, query):
    """Ordinary public search destinations; no login or API-key endpoint."""
    query = _query(query)
    if route == "google-suppliers":
        return _search_url(query + " India manufacturer wholesaler supplier")
    if route == "google-maps":
        return "https://www.google.com/maps/search/?" + urlencode({"api": "1", "query": query + " manufacturer supplier India"})
    if route == "indiamart":
        return "https://dir.indiamart.com/search.mp?" + urlencode({"ss": query})
    if route == "tradeindia":
        return "https://www.tradeindia.com/search.html?" + urlencode({"keyword": query})
    raise SafetyError("SUPPLIER_ROUTE_INVALID")


def _host_allowed(route, host):
    return bool(host and host.casefold() in _HOSTS.get(route, set()))


def _lead_url(route, value, *, parser_version=PARSER_VERSION):
    try:
        clean, _ = _allowed_url(value)
        parsed = urlsplit(clean)
        host, path = parsed.hostname.casefold(), unquote(parsed.path).casefold()
        if route == "google-maps":
            return clean if _host_allowed(route, host) and path.startswith("/maps/place/") else None
        if route == "indiamart":
            if host == "www.indiamart.com" and (path.startswith("/proddetail/") or re.match(r"/[^/]+/(?:profile|products|aboutus)\.html$", path)):
                return clean
            excluded = {"dir", "my"} if parser_version == 1 else _INDIAMART_SERVICE_HOSTS
            if (host.endswith(".indiamart.com") and host[:-len(".indiamart.com")] not in excluded
                    and (parser_version == 1 or host != "www.indiamart.com")
                    and (parser_version < 3 or path not in ("", "/"))):
                return clean
        if route == "tradeindia" and _host_allowed(route, host):
            return clean if path.startswith(("/products/", "/seller-")) else None
    except (SafetyError, ValueError, AttributeError):
        pass
    return None


class _PublicCards(HTMLParser):
    def __init__(self, route, base, *, parser_version=PARSER_VERSION):
        super().__init__(convert_charrefs=True)
        self.route, self.base, self.anchor, self.skip, self.rows = route, base, None, [], []
        self.parser_version = parser_version

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self.skip.append(tag)
        if self.skip:
            return
        if tag == "a":
            attrs = dict(attrs)
            self.anchor = {"url": _lead_url(self.route, urljoin(self.base, attrs.get("href", "")), parser_version=self.parser_version),
                           "title": attrs.get("aria-label") or attrs.get("title") or "", "parts": []}

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag == "a" and self.anchor is not None:
            row = self.anchor
            title = " ".join((row["title"] or " ".join(row["parts"])).split())[:2000]
            if row["url"] and title:
                self.rows.append({"url": row["url"], "title": title})
            self.anchor = None

    def handle_data(self, value):
        if not self.skip and self.anchor is not None:
            self.anchor["parts"].append(value)


@source_phase("parse")
def parse_supplier_route_receipt(route, query, item, receipt, *, _legacy_services=False, _parser_version=None):
    """A challenge, changed query or unrecognised layout is never zero suppliers."""
    if route not in SEARCH_ROUTES:
        raise SafetyError("SUPPLIER_ROUTE_INVALID")
    parser_version = _parser_version if _parser_version is not None else 1 if _legacy_services else PARSER_VERSION
    if type(parser_version) is not int or parser_version not in (1, 2, PARSER_VERSION):
        raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
    expected = supplier_route_url(route, query)
    if item.get("url") != expected:
        raise SafetyError("SUPPLIER_ROUTE_QUERY_CHANGED")
    if not isinstance(receipt, dict) or receipt.get("state") != "usable":
        status = receipt.get("http_status") if isinstance(receipt, dict) else None
        reason = receipt.get("reason", "SUPPLIER_ROUTE_RECEIPT_INVALID") if isinstance(receipt, dict) else "SUPPLIER_ROUTE_RECEIPT_INVALID"
        if not isinstance(reason, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", reason):
            reason = "SUPPLIER_ROUTE_READ_FAILED"
        blocked = status in (401, 403, 429) or any(word in reason for word in ("CHALLENGE", "BLOCKED", "ACCESS_DENIED"))
        return {"state": "blocked" if blocked else "failed", "reason": reason, "results": []}
    try:
        final = urlsplit(receipt.get("final_url", ""))
        if (receipt.get("id") != item["id"] or receipt.get("requested_url") != expected
                or not _host_allowed(route, final.hostname) or receipt.get("http_status") != 200
                or receipt.get("body_complete") is not True or not isinstance(receipt.get("html"), str)
                or len(receipt["html"].encode("utf-8")) > MAX_HTML or not isinstance(receipt.get("text"), str)):
            raise ValueError
        timestamp(receipt.get("checked_at"))
        text = normalise(receipt["text"])
        if any(marker in text for marker in _CHALLENGES) or final.path.startswith(("/sorry", "/accounts", "/login")):
            return {"state": "blocked", "reason": "SUPPLIER_ROUTE_CHALLENGE", "results": []}
        if route == "google-suppliers":
            parsed = _parse_receipt(receipt, query + " India manufacturer wholesaler supplier", item["id"])
            return {**parsed, "state": "complete" if parsed["state"] == "complete" else "extraction_pending"}
        params = parse_qs(final.query)
        if route == "indiamart" and (final.path != "/search.mp" or params.get("ss") != [query]):
            raise ValueError
        if route == "tradeindia" and (final.path != "/search.html" or params.get("keyword") != [query]):
            raise ValueError
        if route == "google-maps":
            requested_text = query + " manufacturer supplier India"
            if not (params.get("query") == [requested_text] or
                    final.path.startswith("/maps/search/") and normalise(requested_text) in normalise(unquote(final.path))):
                raise ValueError
        parser = _PublicCards(route, receipt["final_url"], parser_version=parser_version)
        parser.feed(receipt["html"])
        parser.close()
        rows, seen = [], set()
        for row in parser.rows:
            if row["url"] not in seen:
                rows.append(row)
                seen.add(row["url"])
            if len(rows) >= MAX_RESULTS:
                break
        if rows:
            return {"state": "complete", "reason": "Actual public result cards nominate pages; supplier relevance is not yet reviewed.", "results": rows}
        if any(marker in text for marker in _NO_RESULTS[route]):
            return {"state": "complete", "reason": "The readable bounded search explicitly reported no results.", "results": []}
        return {"state": "extraction_pending", "reason": "SUPPLIER_ROUTE_LAYOUT_UNRECOGNISED", "results": []}
    except (SafetyError, ValueError, TypeError):
        return {"state": "failed", "reason": "SUPPLIER_ROUTE_RECEIPT_INVALID", "results": []}


def _native_deadline_page_receipt(item, folder):
    """Project only the original failed native action, never supplier rows."""
    folder = safe_path(folder)
    original = replay_native_deadline(folder, requested_url=item["url"])
    actual = original["navigation_started"]
    return {"schema_version": 2, "id": item["id"], "requested_url": item["url"], "final_url": None,
        "state": "unavailable", "reason": "SOURCE_ACTION_TIMEOUT", "checked_at": original["completed_at"],
        "http_status": None, "body_complete": False, "html": None, "text": "", "title": "",
        "discovered_links": [], "text_truncated": False, "html_basis": "rendered_owned_native_dom",
        "native_reference": str(folder / "native_outer_deadline.json"), "native_hash": digest(original),
        "native_state": "failed", "native_reason": "SOURCE_ACTION_TIMEOUT", "native_stage": "outer_deadline",
        "owned_process_exited": original["owned_process_exited"], "outer_deadline": True,
        "execution_stage": "attempt_started" if actual is True else "not_dispatched" if actual is False else "unknown",
        "attempt_started_at": original["navigation_started_at"] if actual is True else None,
        "execution_ref": "supplier-browser-" + digest([item, original])[:32] if actual is True else None}


def _deadline_page_receipt(item, error):
    supplied = getattr(error, "execution_receipt", None)
    if isinstance(supplied, dict) and supplied.get("outer_deadline") is True:
        if supplied.get("native_wrapper_reference"):
            original = _saved_completed_supplier_page(item, Path(supplied["native_wrapper_reference"]).parent)
            if original != supplied:
                raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
            return original
        reference = supplied.get("native_reference")
        if not isinstance(reference, str) or Path(reference).name != "native_outer_deadline.json":
            raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
        original = _native_deadline_page_receipt(item, Path(reference).parent)
        if supplied != original:
            raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
        return original
    return dict(failed_reader_receipt(item, "SOURCE_ACTION_TIMEOUT", error), outer_deadline=True)


def _saved_completed_supplier_page(item, folder, *, reparse=False):
    """Use only native bytes bound at the interrupted wrapper boundary."""
    folder = safe_path(folder)
    path = folder / "supplier_outer_deadline.json"
    marker = load_json(path, 16384)
    if (not isinstance(marker, dict) or marker.get("version") != "supplier-native-deadline-v1"
            or marker.get("request") != item):
        raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
    actual = saved_completed_transport(folder, marker, snapshot_file="unused.json", requested_url=item["url"],
                                      snapshot_limit=MAX_RECORD_BYTES)
    if actual is None:
        raise SafetyError("SOURCE_DEADLINE_PROOF_INVALID")
    if reparse:
        receipt = _supplier_native_output_receipt(item, folder, actual, None)
        receipt.update(native_deadline_recovery={"reference": str(path), "sha256": sha256(path.read_bytes()).hexdigest()})
        return receipt
    return {"schema_version": 2, "id": item["id"], "requested_url": item["url"], "final_url": None,
        "state": "unavailable", "reason": "SOURCE_ACTION_TIMEOUT", "checked_at": marker["completed_at"],
        "http_status": None, "body_complete": False, "html": None, "text": "", "title": "",
        "discovered_links": [], "text_truncated": False, "html_basis": "rendered_owned_native_dom",
        "native_reference": str(folder / "native_result.json"), "native_wrapper_reference": str(path),
        "native_wrapper_sha256": sha256(path.read_bytes()).hexdigest(), "outer_deadline": True,
        "owned_process_exited": True, "execution_stage": "attempt_started",
        "attempt_started_at": actual["navigation_started_at"],
        "execution_ref": "supplier-browser-" + digest([item, marker])[:32]}


def fetch_supplier_native_page(item, *, route, folder, native_capture=None, on_dispatch=None):
    """Wrap one owned native Chrome navigation; retain its original proof file."""
    clean, _ = _allowed_url(item.get("url"))
    identifier(item.get("id"))
    if route not in ROUTES or route != "company-pages" and not _host_allowed(route, urlsplit(clean).hostname):
        raise SafetyError("SUPPLIER_ROUTE_BROWSER_SCOPE_INVALID")
    if native_capture is None:
        from .native_public_browser import native_snapshot
        native_capture = native_snapshot
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    deadline_path = safe_path(folder / "native_outer_deadline.json")
    if deadline_path.exists():
        return _native_deadline_page_receipt(item, folder)
    try:
        with source_phase("transport"):
            output = native_capture(clean, _DOM, folder)
    except SourceActionDeadline as error:
        if deadline_path.exists():
            error.execution_receipt = _native_deadline_page_receipt(item, folder)
        raise
    try:
        return _supplier_native_output_receipt(item, folder, output, on_dispatch)
    except SourceActionDeadline as error:
        try:
            native_path = safe_path(folder / "native_result.json")
            dispatch_path = safe_path(folder / "native_dispatch.json")
            if native_path.is_file() and dispatch_path.is_file() and native_path.stat().st_size <= 12 * 1024 * 1024:
                marker = {"version": "supplier-native-deadline-v1", "request": deepcopy(item),
                    "outer_deadline": True, "reason": "SOURCE_ACTION_TIMEOUT", "started_at": output.get("navigation_started_at"),
                    "completed_at": datetime.now(timezone.utc).isoformat(), "transport_attempts": ["owned_native_windows_chrome"],
                    "artifacts": {"native_result.json": sha256(native_path.read_bytes()).hexdigest(),
                                  "native_dispatch.json": sha256(dispatch_path.read_bytes()).hexdigest()}}
                atomic_json(folder / "supplier_outer_deadline.json", marker)
                error.execution_receipt = _saved_completed_supplier_page(item, folder)
        except (SafetyError, OSError, TypeError, ValueError):
            error.supplier_proof_save_error = "SOURCE_DEADLINE_PROOF_SAVE_FAILED"
        raise


def _supplier_native_output_receipt(item, folder, output, on_dispatch):
    clean, _ = _allowed_url(item["url"])
    checked = output.get("completed_at") or utc_now()
    timestamp(checked)
    actual = output.get("navigation_started")
    actual = actual if actual is True or actual is False else None
    status = output.get("navigation_status")
    status = status if type(status) is int and 100 <= status <= 599 else None
    native_path = safe_path(folder / "native_result.json")
    receipt = {"schema_version": 2, "id": item["id"], "requested_url": clean, "final_url": None,
               "state": "unavailable", "reason": "SUPPLIER_NATIVE_READ_FAILED", "checked_at": checked,
               "http_status": status, "body_complete": False, "html": None, "text": "", "title": "",
               "discovered_links": [], "text_truncated": False, "html_basis": "rendered_owned_native_dom",
               # Retain the expected pathname for late original recovery,
               # but do not claim that a failed child saved that output.
               "native_reference": str(native_path), "native_result_saved": native_path.is_file(),
               "native_state": output.get("state"), "native_reason": output.get("reason"),
               "native_stage": output.get("stage"),
               "execution_stage": "response_received" if actual and status is not None else "attempt_started" if actual else "not_dispatched" if actual is False else "unknown",
               "attempt_started_at": (output.get("navigation_started_at") or checked) if actual else None,
               "execution_ref": "supplier-browser-" + digest([item, output])[:32] if actual else None}
    retry_after = output.get("retry_after")
    if (isinstance(retry_after, str) and len(retry_after) <= 256
            and not any(ord(c) < 32 or ord(c) == 127 for c in retry_after)):
        receipt["retry_after"] = retry_after
    if type(output.get("owned_process_exited")) is bool:
        receipt["owned_process_exited"] = output["owned_process_exited"]
    if actual and on_dispatch is not None:
        # The native runner retains exact navigation proof on disk. Python can
        # notify only after that child returns; a interrupted child remains
        # uncertain in the controller rather than inventing a sent request.
        on_dispatch()
    snapshot = output.get("snapshot")
    if (output.get("state") != "captured" or output.get("owned_process_exited") is not True
            or not isinstance(snapshot, dict) or status != 200):
        return receipt
    try:
        final, _ = _allowed_url(snapshot.get("url"))
        html, text = snapshot.get("html"), snapshot.get("text")
        links = snapshot.get("links", [])
        if (not isinstance(html, str) or len(html.encode("utf-8")) > MAX_HTML
                or not isinstance(text, str) or not isinstance(links, list) or len(links) > 60):
            raise SafetyError("SUPPLIER_NATIVE_SNAPSHOT_INVALID")
        receipt.update(final_url=final, html=html, text=text[:MAX_TEXT], title=str(snapshot.get("title", ""))[:2000],
                       discovered_links=links, text_truncated=snapshot.get("text_truncated") is True,
                       body_complete=True, html_sha256=sha256(html.encode("utf-8")).hexdigest(),
                       text_sha256=sha256(text[:MAX_TEXT].encode("utf-8")).hexdigest(),
                       state="usable", reason="Owned public browser snapshot; supplier terms remain unreviewed.")
    except (SafetyError, TypeError, ValueError):
        receipt["reason"] = "SUPPLIER_NATIVE_SNAPSHOT_INVALID"
    return receipt


def _actual(receipts, reference):
    if any((ref := _receipt_ref(row, reference)) is not None and ref["proof"] != "none" for row in receipts):
        return True
    if any(isinstance(row, dict) and (row.get("execution_stage") == "unknown" or row.get("dispatch_known") is False)
           for row in receipts):
        return None
    return False


def _result(records):
    complete = [row["parsed"] for row in records if row["parsed"]["state"] == "complete"]
    selected = complete[0] if complete else records[-1]["parsed"]
    return deepcopy(selected)


def _reader_access_refusal(receipt, item, *, challenge=False):
    """Do not turn a real refusal into an immediate same-URL browser retry.

    A parser problem or failure before navigation may use the ordinary
    browser alternative. A claimed status without actual reader proof cannot
    suppress that alternative. This helper grants no retry or new acquisition.
    """
    proof = _receipt_ref(receipt, "saved-supplier-reader")
    return bool(proof and proof["proof"] != "none" and proof["execution_stage"] == "response_received"
        and proof["id"] == item["id"] and proof["requested_url"] == item["url"]
        and (proof["http_status"] in (401, 403, 429, 500, 502, 503, 504) or challenge is True))


def _search_access_failure(record, reference):
    """Only an actually attempted directory access block pauses its route.

    A browser refusing to start after an unreadable HTTP page is not a proved
    platform block. A useful fallback, parser issue or company-page failure
    likewise cannot pause every supplier question.
    """
    if record["result"]["state"] != "blocked":
        return False, False
    shared, retryable = False, False
    request = record["request"]
    for row in record["records"]:
        if row["parsed"]["state"] != "blocked":
            continue
        receipt = row["receipt"]
        proof = _receipt_ref(receipt, reference)
        if (proof is None or proof["proof"] == "none"
                or proof["execution_stage"] != "response_received"
                or proof["id"] != key("supplier-route-request", [record["acquisition_id"], row["transport"]])
                or proof["requested_url"] != request["url"]
                or (proof["final_url"] is not None
                    and not _host_allowed(request["route"], urlsplit(proof["final_url"]).hostname))):
            continue
        status = proof["http_status"]
        if status in (401, 403, 429) or row["parsed"]["reason"] == "SUPPLIER_ROUTE_CHALLENGE":
            shared = True
            retryable = retryable or status == 429
    return shared, retryable


def _load_search_record(path, request, acquisition_id):
    record = load_json(path, MAX_RECORD_BYTES)
    if (not isinstance(record, dict) or set(record) != {"schema_version", "acquisition_id", "request", "records", "result", "content_hash"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 1
            or record["acquisition_id"] != acquisition_id or record["request"] != request
            or not isinstance(record["records"], list) or not 1 <= len(record["records"]) <= 2
            or record["content_hash"] != digest([request, record["records"], record["result"]])):
        raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
    _validate_search_records(request, acquisition_id, record["records"])
    if record["result"] != _result(record["records"]):
        raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
    return record


def _validate_search_records(request, acquisition_id, records):
    if (not isinstance(records, list) or len(records) > 2
            or any(not isinstance(row, dict) or set(row) != {"transport", "receipt", "parsed"} for row in records)
            or [row.get("transport") for row in records if isinstance(row, dict)] != list(("http", "browser")[:len(records)])):
        raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
    for row in records:
        expected_id = key("supplier-route-request", [acquisition_id, row.get("transport")])
        item = {"id": expected_id, "url": request["url"]}
        receipt = row.get("receipt")
        if isinstance(receipt, dict) and receipt.get("outer_deadline") is True and receipt.get("native_reference"):
            original = (_saved_completed_supplier_page(item, Path(receipt["native_wrapper_reference"]).parent)
                        if receipt.get("native_wrapper_reference") else
                        _native_deadline_page_receipt(item, Path(receipt["native_reference"]).parent))
            if original != receipt:
                raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
        if type(request.get("parser_version", 1)) is not int or request.get("parser_version", 1) not in (1, 2, PARSER_VERSION):
            raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
        if row.get("transport") not in ("http", "browser") or row.get("parsed") != parse_supplier_route_receipt(
                request["route"], request["query"], item, row.get("receipt"),
                _parser_version=request.get("parser_version", 1)):
            raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")


def _saved_search_progress(path, request, acquisition_id):
    progress = load_json(path, MAX_RECORD_BYTES)
    if (not isinstance(progress, dict) or set(progress) != {"request", "records", "content_hash"}
            or progress["request"] != request or not isinstance(progress["records"], list)
            or not 1 <= len(progress["records"]) <= 2
            or progress["content_hash"] != digest([request, progress["records"]])):
        raise SafetyError("SAVED_SUPPLIER_ROUTE_PROGRESS_INVALID")
    _validate_search_records(request, acquisition_id, progress["records"])
    return progress


def _recover_search_dispatch(root, record, reference):
    """Correct only an old false wrapper flag from exact original navigation.

    Original failed search/reader bytes remain unchanged. This attribution
    cannot supply a supplier result, prove child cleanup or reopen collection.
    """
    original_reference = safe_path(reference)
    rows, proofs = deepcopy(record["records"]), {}
    completed_reparse = False
    for row in rows:
        receipt = row["receipt"]
        expected_id = key("supplier-route-request", [record["acquisition_id"], row["transport"]])
        if isinstance(receipt, dict) and receipt.get("native_wrapper_reference"):
            item = {"id": expected_id, "url": record["request"]["url"]}
            folder = safe_path(root, "supplier_routes", "native", expected_id)
            if safe_path(receipt["native_wrapper_reference"]) != folder / "supplier_outer_deadline.json":
                raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
            recovered = _saved_completed_supplier_page(item, folder, reparse=True)
            row.update(receipt=recovered, parsed=parse_supplier_route_receipt(record["request"]["route"],
                record["request"]["query"], item, recovered, _parser_version=record["request"].get("parser_version", 1)))
            proofs[expected_id] = {"original_receipt_hash": digest(receipt),
                                  "native_deadline_recovery": deepcopy(recovered["native_deadline_recovery"])}
            completed_reparse = True
            continue
        if (not isinstance(receipt, dict) or receipt.get("execution_stage") != "not_dispatched"
                or not isinstance(receipt.get("native_reference"), str)
                or receipt.get("state") == "usable"):
            continue
        folder = safe_path(root, "supplier_routes", "native", expected_id)
        if safe_path(receipt["native_reference"]) != folder / "native_result.json":
            raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
        dispatch_path = safe_path(folder / "native_dispatch.json")
        if not dispatch_path.exists():
            continue  # File absence is not proof of no navigation or a retry.
        dispatch = load_json(dispatch_path, 16384)
        if (not isinstance(dispatch, dict) or set(dispatch) != {"url", "navigation_started", "navigation_started_at"}
                or dispatch["url"] != record["request"]["url"] or dispatch["navigation_started"] is not True
                or timestamp(dispatch["navigation_started_at"]) > timestamp(receipt.get("checked_at"))):
            raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
        binding = {"original_receipt_hash": digest(receipt), "dispatch_reference": str(dispatch_path),
                   "dispatch_sha256": sha256(dispatch_path.read_bytes()).hexdigest()}
        receipt.update(execution_stage="attempt_started", attempt_started_at=dispatch["navigation_started_at"],
            execution_ref="supplier-browser-" + digest(binding)[:32], saved_dispatch_recovery=binding,
            owned_process_exited=None)
        proofs[expected_id] = binding
    if not proofs:
        return record, original_reference
    result = _result(rows)
    if not completed_reparse and (result != record["result"] or result.get("results")):
        raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
    recovered = dict(record, records=rows, result=result, content_hash=digest([record["request"], rows, result]))
    provenance = {"version": "supplier-original-dispatch-recovery-v1",
        "original_reference": str(original_reference),
        "original_sha256": sha256(original_reference.read_bytes()).hexdigest(),
        "original_content_hash": record["content_hash"], "dispatch_proofs": proofs, "recovered_content_hash": recovered["content_hash"]}
    path = original_reference.parent / "capture-dispatch-recovery.json"
    for target, value in ((original_reference.parent / "dispatch-recovery.json", provenance), (path, recovered)):
        if target.exists():
            if canonical(load_json(target, MAX_RECORD_BYTES)) != canonical(value):
                raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
        else:
            atomic_json(target, value)
    return _load_search_record(path, record["request"], record["acquisition_id"]), path


def replay_supplier_search(root, *, route, query, acquisition_id):
    """Finish only the original saved public read, never another transport.

    Reconciliation is different from collection: a missing browser reply is
    not permission to launch that browser. Original parser decisions/dates
    stay attached to the original acquisition, including partial work and
    blocked replies. A later parser-version capture is not a replacement.
    """
    if route not in SEARCH_ROUTES:
        raise SafetyError("SUPPLIER_ROUTE_INVALID")
    query, acquisition_id = _query(query), identifier(acquisition_id)
    physical = {"version": VERSION, "route": route, "query": query, "url": supplier_route_url(route, query)}
    folder = safe_path(root, "supplier_routes", acquisition_id)
    path, progress_path = folder / "capture.json", folder / "progress.json"
    with Journal(folder).locked():
        original_path = path if path.exists() else progress_path
        if not original_path.exists():
            raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
        original = load_json(original_path, MAX_RECORD_BYTES)
        request = original.get("request") if isinstance(original, dict) else None
        version = request.get("parser_version", 1) if isinstance(request, dict) else None
        if (type(version) is not int or version not in (1, 2, PARSER_VERSION)
                or request not in (physical, dict(physical, parser_version=version))):
            raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
        if original_path == path:
            record = _load_search_record(path, request, acquisition_id)
            if progress_path.exists():
                progress = _saved_search_progress(progress_path, request, acquisition_id)
                if canonical(progress["records"]) != canonical(record["records"]):
                    raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
        else:
            original = _saved_search_progress(progress_path, request, acquisition_id)
            result = _result(original["records"])
            record = {"schema_version": 1, "acquisition_id": acquisition_id, "request": request,
                      "records": original["records"], "result": result,
                      "content_hash": digest([request, original["records"], result])}
        dates = [row["receipt"].get("checked_at") for row in record["records"]
                 if isinstance(row["receipt"], dict) and row["receipt"].get("checked_at")]
        if not dates:
            # A malformed original reader reply is retained, but today's
            # resume time cannot be presented as its original observation.
            raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
        max(dates, key=timestamp)
        if original_path != path:
            atomic_json(path, record)
            record = _load_search_record(path, request, acquisition_id)
        record, path = _recover_search_dispatch(root, record, path)
    return {"record": record, "reference": str(path), "reused": True}


def acquire_supplier_search(root, *, route, query, acquisition_id=None, as_of=None,
                            http_reader=fetch_public_page, browser_reader=None, record_dispatch=None,
                            respect_access_waits=False):
    """Save one bounded public read without any product or financial writes.

    The connected collector consumes this same immutable record. Public reads
    can therefore finish while a separate assessment writer is busy; attaching
    their product-specific interpretation questions remains resumable work.
    """
    if (route not in SEARCH_ROUTES or not callable(http_reader) or browser_reader is not None and not callable(browser_reader)
            or type(respect_access_waits) is not bool):
        raise SafetyError("SUPPLIER_ROUTE_INPUT_INVALID")
    if record_dispatch is not None and not callable(record_dispatch):
        raise SafetyError("SUPPLIER_ROUTE_INPUT_INVALID")
    query = _query(query)
    at = as_of or utc_now(); timestamp(at)
    week = timestamp(at).isocalendar()
    physical_request = {"version": VERSION, "route": route, "query": query, "url": supplier_route_url(route, query)}
    request = dict(physical_request, parser_version=PARSER_VERSION)
    acquisition_id = identifier(acquisition_id) if acquisition_id is not None else key("supplier-route-acquisition", [physical_request, week.year, week.week])
    folder = safe_path(root, "supplier_routes", acquisition_id)
    path = folder / "capture.json"
    with Journal(folder).locked():
        seed = []
        progress_path = folder / "progress.json"
        # A parser repair reuses the original public response. Preserve the
        # old immutable record and validate it with its original contract,
        # then save a separately versioned interpretation of those receipts.
        existing = load_json(path, MAX_RECORD_BYTES) if path.exists() else None
        older_progress = load_json(progress_path, MAX_RECORD_BYTES) if existing is None and progress_path.exists() else None
        older = existing if existing is not None else older_progress
        older_request = older.get("request") if isinstance(older, dict) else None
        older_version = older_request.get("parser_version", 1) if isinstance(older_request, dict) else None
        if (type(older_version) is int and older_version < PARSER_VERSION
                and older_request in (physical_request, dict(physical_request, parser_version=older_version))):
            if existing is not None:
                original = _load_search_record(path, older_request, acquisition_id)
            else:
                if (set(older_progress) != {"request", "records", "content_hash"}
                        or older_progress["content_hash"] != digest([older_request, older_progress["records"]])):
                    raise SafetyError("SAVED_SUPPLIER_ROUTE_PROGRESS_INVALID")
                _validate_search_records(older_request, acquisition_id, older_progress["records"])
                original = older_progress
            # Retain an already performed browser fallback from an earlier
            # repair. A stricter card parser must not trigger another visit.
            for version in range(2, PARSER_VERSION):
                previous_path = folder / ("capture-parser-" + str(version) + ".json")
                if previous_path.exists():
                    original = _load_search_record(previous_path, dict(physical_request, parser_version=version), acquisition_id)
                else:
                    previous_progress = folder / ("progress-parser-" + str(version) + ".json")
                    if previous_progress.exists():
                        pending = load_json(previous_progress, MAX_RECORD_BYTES)
                        prior_request = dict(physical_request, parser_version=version)
                        if (not isinstance(pending, dict) or set(pending) != {"request", "records", "content_hash"}
                                or pending["request"] != prior_request
                                or pending["content_hash"] != digest([prior_request, pending["records"]])):
                            raise SafetyError("SAVED_SUPPLIER_ROUTE_PROGRESS_INVALID")
                        _validate_search_records(prior_request, acquisition_id, pending["records"])
                        original = pending
            for row in original["records"]:
                item = {"id": key("supplier-route-request", [acquisition_id, row["transport"]]), "url": request["url"]}
                seed.append({"transport": row["transport"], "receipt": row["receipt"],
                    "parsed": parse_supplier_route_receipt(route, query, item, row["receipt"])})
            path = folder / ("capture-parser-" + str(PARSER_VERSION) + ".json")
            progress_path = folder / ("progress-parser-" + str(PARSER_VERSION) + ".json")
        reused = path.exists()
        if reused:
            record = _load_search_record(path, request, acquisition_id)
        else:
            progress = load_json(progress_path, MAX_RECORD_BYTES) if progress_path.exists() else {
                "request": request, "records": seed, "content_hash": digest([request, seed])}
            if (not isinstance(progress, dict) or set(progress) != {"request", "records", "content_hash"}
                    or progress["request"] != request or progress["content_hash"] != digest([request, progress["records"]])):
                raise SafetyError("SAVED_SUPPLIER_ROUTE_PROGRESS_INVALID")
            records = progress["records"]
            _validate_search_records(request, acquisition_id, records)
            for transport, reader in (("http", http_reader), ("browser", browser_reader)):
                if reader is None or any(row.get("transport") == transport for row in records):
                    continue
                if records and (records[-1]["parsed"]["state"] == "complete" or
                        records[-1]["receipt"].get("outer_deadline") is True or
                        records[-1]["receipt"].get("reason") in ("PUBLIC_DESTINATION_REJECTED", "PUBLIC_URL_INVALID")):
                    break
                if respect_access_waits and records:
                    prior = records[-1]
                    original_item = {"id": key("supplier-route-request", [acquisition_id, prior["transport"]]),
                                     "url": request["url"]}
                    if _reader_access_refusal(prior["receipt"], original_item,
                            challenge=prior["parsed"]["reason"] == "SUPPLIER_ROUTE_CHALLENGE"):
                        break
                item = {"id": key("supplier-route-request", [acquisition_id, transport]), "url": request["url"]}
                try:
                    receipt = reader(item, on_dispatch=record_dispatch) if reader is fetch_public_page else reader(item)
                except SourceActionDeadline as error:
                    try:
                        receipt = _deadline_page_receipt(item, error)
                        parsed = parse_supplier_route_receipt(route, query, item, receipt)
                        records.append({"transport": transport, "receipt": receipt, "parsed": parsed})
                        atomic_json(progress_path, {"request": request, "records": records, "content_hash": digest([request, records])})
                    except (SafetyError, OSError):
                        error.supplier_proof_save_error = "SOURCE_DEADLINE_PROOF_SAVE_FAILED"
                    raise
                except Exception as error:
                    receipt = failed_reader_receipt(item, "SUPPLIER_ROUTE_FETCH_FAILED", error)
                parsed = parse_supplier_route_receipt(route, query, item, receipt)
                records.append({"transport": transport, "receipt": receipt, "parsed": parsed})
                if len(canonical(records)) > MAX_RECORD_BYTES - 8192:
                    raise SafetyError("SUPPLIER_ROUTE_SIZE_LIMIT")
                atomic_json(progress_path, {"request": request, "records": records, "content_hash": digest([request, records])})
                if record_dispatch is not None and _actual([receipt], str(progress_path)) and reader is not fetch_public_page:
                    record_dispatch()
            result = _result(records)
            record = {"schema_version": 1, "acquisition_id": acquisition_id, "request": request, "records": records,
                      "result": result, "content_hash": digest([request, records, result])}
            if len(canonical(record)) > MAX_RECORD_BYTES:
                raise SafetyError("SUPPLIER_ROUTE_SIZE_LIMIT")
            atomic_json(path, record)
            record = _load_search_record(path, request, acquisition_id)
    if reused:
        record, path = _recover_search_dispatch(root, record, path)
    return {"record": record, "reference": str(path), "reused": reused}


def collect_supplier_search(memory, *, route, concept_ids, query=None, acquisition_id=None, as_of=None,
                            http_reader=fetch_public_page, browser_reader=None, record_dispatch=None, saved_only=False,
                            respect_access_waits=False):
    """Bind a reusable family acquisition to each current product separately."""
    if (type(saved_only) is not bool or type(respect_access_waits) is not bool
            or not isinstance(concept_ids, (list, tuple)) or not concept_ids
            or any(not isinstance(cid, str) for cid in concept_ids) or len(set(concept_ids)) != len(concept_ids)):
        raise SafetyError("SUPPLIER_ROUTE_TARGETS_INVALID")
    products = [_product(memory, identifier(cid)) for cid in concept_ids]
    if query is None:
        from .product_queries import product_query_terms
        query = product_query_terms(memory, concept_ids[0], source="supplier", limit=1)[0]["text"]
    if saved_only:
        acquired = replay_supplier_search(memory.root, route=route, query=query, acquisition_id=acquisition_id)
    else:
        acquired = acquire_supplier_search(memory.root, route=route, query=query, acquisition_id=acquisition_id,
            as_of=as_of, http_reader=http_reader, browser_reader=browser_reader, record_dispatch=record_dispatch,
            respect_access_waits=respect_access_waits)
    record, path, reused = acquired["record"], Path(acquired["reference"]), acquired["reused"]
    acquisition_id, request = record["acquisition_id"], record["request"]
    query, at = request["query"], as_of or utc_now()
    receipts = [row["receipt"] for row in record["records"]]
    result = record["result"]
    outer_state = ("interpretation_pending" if result["results"] else "completed_no_results" if result["state"] == "complete"
                   else "insufficient" if result["state"] == "extraction_pending" else result["state"])
    observed = max((row.get("checked_at") for row in receipts if isinstance(row, dict) and row.get("checked_at")), key=timestamp, default=at)
    target_results = []
    for product in products:
        cid, description = product["concept_id"], product["description"]
        if _product(memory, cid) != product:
            target_results.append({"concept_id": cid, "description_hash": digest(description), "state": "insufficient",
                                   "evidence_refs": [], "reason": "The product changed while the public search ran; saved pages remain reusable."})
            continue
        check_id = key("supplier-route-search-check", [VERSION, cid, digest(description), acquisition_id, record["content_hash"]])
        check = {"kind": "supplier_route_search", "version": VERSION, "source": "supplier", "route": route, "state": outer_state,
                 "at": observed, "concept_id": cid, "description_hash": digest(description), "reference": str(path),
                 "acquisition_id": acquisition_id, "query": query, "candidates": deepcopy(result["results"]), "reason": result["reason"]}
        checks = memory.snapshot()["source_checks"]
        previous = checks.get(check_id)
        if saved_only and any(row.get("kind") == "supplier_route_search" and row.get("route") == route
                and row.get("concept_id") == cid and row.get("description_hash") == digest(description)
                and row.get("acquisition_id") == acquisition_id and row.get("reference") == str(path)
                and canonical(row) != canonical(check) for row in checks.values()):
            raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
        if previous is not None and canonical(previous) != canonical(check):
            raise SafetyError("SAVED_SUPPLIER_ROUTE_INVALID")
        if previous is None:
            memory.put("source_checks", check_id, check)
        requests = {key("supplier-route-request", [acquisition_id, row["transport"]]): request["url"] for row in record["records"]}
        effort = record_supplier_effort(memory, concept_id=cid, description=description, pass_id=acquisition_id,
            receipts=receipts, receipt_reference=str(path), outcome="completed_empty" if outer_state == "completed_no_results" else
            "interpretation_pending" if outer_state == "interpretation_pending" else "partial" if outer_state == "insufficient" else
            "blocked" if outer_state == "blocked" else "failed", route=route, expected_requests=requests)
        target_results.append({"concept_id": cid, "description_hash": digest(description), "state": outer_state,
                               "evidence_refs": [check_id, effort["check_id"]], "reason": result["reason"]})
    shared_failure, retryable = _search_access_failure(record, str(path))
    return {"schema_version": 1, "route": route, "acquisition_id": acquisition_id, "observed_at": observed,
            "state": outer_state, "dispatch_started": _actual(receipts, str(path)), "reference": str(path),
            "capture_ids": [], "observation_ids": [], "target_results": target_results, "reason": result["reason"],
            "failure_scope": "route" if shared_failure else "question", "retryable": retryable}


def _replay_company_pages(memory, *, path, products, candidates, input_errors, acquisition_id,
                          candidate_urls, candidate_check_ids, previous, capture_requests=None):
    """Rebuild a company reply from bound saved bundles, without any fetcher.

    Existing final passes must reproduce exactly. If the pass write was
    interrupted, all required original bundles must exist before projection;
    an absent page must not become a newly manufactured failed-page receipt.
    """
    failures = {}
    if previous is not None:
        fields = {"schema_version", "acquisition_id", "products", "candidates", "supplier_capture_ids", "receipt_hash",
                  "envelope", "target_failures", "candidate_urls", "candidate_check_ids"}
        if capture_requests is not None: fields.add("capture_requests")
        if (set(previous) != fields or type(previous.get("schema_version")) is not int
                or previous["schema_version"] != 1 or not isinstance(previous.get("target_failures"), dict)
                or not set(previous["target_failures"]) <= set(candidates)
                or not isinstance(previous.get("supplier_capture_ids"), list)
                or any(not isinstance(value, str) for value in previous["supplier_capture_ids"])
                or previous["supplier_capture_ids"] != sorted(set(previous["supplier_capture_ids"]))
                or not isinstance(previous.get("envelope"), dict)):
            raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
        failures = deepcopy(previous["target_failures"])
        for cid, rows in failures.items():
            allowed = {candidate["url"] for candidate in candidates[cid]}
            if (not isinstance(rows, list) or not rows or len(rows) > MAX_COMPANIES
                    or any(not isinstance(row, dict) or set(row) != {"url", "reason"}
                           or row["url"] not in allowed or not isinstance(row["reason"], str)
                           or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", row["reason"]) for row in rows)
                    or len({row["url"] for row in rows}) != len(rows)):
                raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
    elif not any(candidates.values()):
        raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
    bundles, captures, receipts, targets = [], [], [], []
    for product in products:
        cid = product["concept_id"]
        failed_urls = {row["url"] for row in failures.get(cid, [])}
        refs, usable = [], False
        for candidate in candidates[cid]:
            request = {"concept_id": cid,
                       "lead": {"id": key("supplier-lead", candidate["url"]), "urls": [candidate["url"]]}}
            if capture_requests is not None:
                request = capture_requests[key("supplier-company-target", [cid, candidate["url"]])]
            capture_id = _capture_key(product, request)
            if candidate["url"] in failed_urls and capture_id not in previous["supplier_capture_ids"]:
                continue  # No original saved pass claims this page was captured.
            capture_path = safe_path(memory.root, "supplier_pages", capture_id) / "capture.json"
            if not capture_path.exists():
                raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
            saved = load_saved_supplier_capture(memory, capture_path)
            if saved["request"] != request or saved["product"] != product:
                raise SafetyError("SAVED_SUPPLIER_CAPTURE_INVALID")
            bundles.append(saved)
            captures.append(capture_id)
            refs.append(saved["path"])
            receipts.extend(saved["bundle"]["receipts"])
            usable |= saved["bundle"]["item"] is not None
        targets.append({"concept_id": cid, "description_hash": digest(product["description"]),
            "state": "interpretation_pending" if usable else "failed" if refs else "insufficient",
            "evidence_refs": refs, "reason": input_errors[cid] if cid in input_errors else
            "Saved supplier pages need commercial-term interpretation." if usable else
            "Discovered supplier pages did not produce readable information." if refs else
            "Discovered supplier pages could not be captured; the saved receipt records the reason." if failed_urls else
            "No supplier destination page has been discovered for this product yet."})
    if previous is not None:
        observed = previous["envelope"].get("observed_at")
        timestamp(observed)
    else:
        observed = None
    observed = max((row.get("checked_at") for row in receipts if isinstance(row, dict) and row.get("checked_at")),
                   key=timestamp, default=observed)
    if observed is None:
        raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
    outcome = "interpretation_pending" if any(row["state"] == "interpretation_pending" for row in targets) else "failed" if captures else "insufficient"
    envelope = {"schema_version": 1, "route": "company-pages", "acquisition_id": acquisition_id, "observed_at": observed,
        "state": outcome, "dispatch_started": _actual(receipts, str(path)), "reference": str(path),
        "capture_ids": [], "observation_ids": [], "target_results": targets,
        "reason": "Actual discovered supplier pages are retained; relevance and commercial terms require F interpretation."}
    record = {"schema_version": 1, "acquisition_id": acquisition_id, "products": products, "candidates": candidates,
        "supplier_capture_ids": sorted(set(captures)), "receipt_hash": digest(receipts), "envelope": envelope,
        "target_failures": failures, "candidate_urls": candidate_urls, "candidate_check_ids": candidate_check_ids}
    if capture_requests is not None: record["capture_requests"] = capture_requests
    if previous is not None and canonical(previous) != canonical(record):
        raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
    # Verification above precedes every canonical effort write. No callable
    # capable of fetching is used even if a file disappears after validation.
    for saved in bundles:
        request, product, bundle = saved["request"], saved["product"], saved["bundle"]
        admitted = list(request["lead"]["urls"])
        if bundle["retrieval"]["linked_page"] is not None:
            admitted.append(bundle["retrieval"]["linked_page"])
        expected = {"supplier-page-" + digest([request["lead"]["id"], _allowed_url(url)[0]])[:40]: _allowed_url(url)[0]
                    for url in admitted}
        record_supplier_effort(memory, concept_id=product["concept_id"], description=product["description"],
            pass_id=saved["capture_id"], receipts=bundle["receipts"], receipt_reference=saved["path"],
            outcome="interpretation_pending" if bundle["item"] is not None else "failed", expected_requests=expected)
    if previous is None:
        with Journal(path.parent).locked():
            if path.exists() and canonical(load_json(path, MAX_RECORD_BYTES)) != canonical(record):
                raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
            atomic_json(path, record)
    return envelope


def _cached_page_reader(memory, reader, at, *, record_dispatch=None, browser_reader=None, respect_access_waits=False):
    week = timestamp(at).isocalendar()
    def fetch(item):
        request = {"id": identifier(item["id"]), "url": _allowed_url(item["url"])[0]}
        cache_id = key("supplier-route-page", [VERSION, request, week.year, week.week])
        folder = safe_path(memory.root, "supplier_routes", "pages", cache_id)
        path = folder / "receipt.json"
        with Journal(folder).locked():
            if path.exists():
                saved = load_json(path, MAX_RECORD_BYTES)
                if (not isinstance(saved, dict) or saved.get("request") != request or
                        saved.get("hash") != digest([saved.get("attempts"), saved.get("receipt")])):
                    raise SafetyError("SAVED_SUPPLIER_PAGE_ROUTE_INVALID")
                return deepcopy(saved["receipt"])
            def cancelled(error, previous_attempts):
                try:
                    failed = _deadline_page_receipt(request, error)
                    attempts = list(previous_attempts) + [failed]
                    atomic_json(path, {"request": request, "attempts": attempts, "receipt": failed,
                                       "hash": digest([attempts, failed])})
                    error.execution_receipt = failed
                    error.supplier_cached_page_reference = str(path)
                except (SafetyError, OSError):
                    error.supplier_proof_save_error = "SOURCE_DEADLINE_PROOF_SAVE_FAILED"
            try:
                receipt = reader(request, on_dispatch=record_dispatch) if reader is fetch_public_page else reader(request)
            except SourceActionDeadline as error:
                cancelled(error, [])
                raise
            except Exception as error:
                receipt = failed_reader_receipt(request, "SUPPLIER_ROUTE_PAGE_FAILED", error)
            attempts = [receipt]
            if record_dispatch is not None and reader is not fetch_public_page and _actual([receipt], str(path)):
                record_dispatch()
            if (browser_reader is not None and isinstance(receipt, dict) and receipt.get("state") != "usable"
                    and receipt.get("reason") not in ("PUBLIC_DESTINATION_REJECTED", "PUBLIC_URL_INVALID", "SUPPLIER_NON_RESEARCH_URL")
                    and not (respect_access_waits and _reader_access_refusal(receipt, request))):
                try:
                    rendered = browser_reader(request)
                except SourceActionDeadline as error:
                    cancelled(error, attempts)
                    raise
                except Exception as error:
                    rendered = failed_reader_receipt(request, "SUPPLIER_ROUTE_BROWSER_PAGE_FAILED", error)
                attempts.append(rendered)
                if isinstance(rendered, dict) and (rendered.get("state") == "usable" or _actual([rendered], str(path)) is True
                        or _actual([rendered], str(path)) is None and _actual([receipt], str(path)) is not True):
                    receipt = rendered
                if record_dispatch is not None and _actual([rendered], str(path)):
                    record_dispatch()
            saved = {"request": request, "attempts": attempts, "receipt": receipt, "hash": digest([attempts, receipt])}
            if len(canonical(saved)) > MAX_RECORD_BYTES:
                raise SafetyError("SUPPLIER_ROUTE_SIZE_LIMIT")
            atomic_json(path, saved)
            return receipt
    return fetch


def _replay_company_deadline(memory, path, expected):
    """A cancelled finite pass keeps unvisited destinations explicitly untried."""
    saved = load_json(path, MAX_RECORD_BYTES)
    if (not isinstance(saved, dict) or set(saved) != {"binding", "failed_concept_id", "page_reference", "page_hash",
            "receipt", "completed_capture_ids", "content_hash"} or saved["binding"] != expected
            or saved["content_hash"] != digest({field: value for field, value in saved.items() if field != "content_hash"})
            or saved["failed_concept_id"] not in expected["candidates"]
            or not isinstance(saved["completed_capture_ids"], list)
            or saved["completed_capture_ids"] != sorted(set(saved["completed_capture_ids"]))):
        raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
    page_path = safe_path(saved["page_reference"])
    if not page_path.is_relative_to(memory.root / "supplier_routes" / "pages") or page_path.name != "receipt.json":
        raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
    page = load_json(page_path, MAX_RECORD_BYTES)
    receipt = saved["receipt"]
    if (digest(page) != saved["page_hash"] or not isinstance(page, dict) or page.get("receipt") != receipt
            or page.get("hash") != digest([page.get("attempts"), receipt])
            or not isinstance(receipt, dict) or receipt.get("outer_deadline") is not True
            or receipt.get("reason") != "SOURCE_ACTION_TIMEOUT"
            or not isinstance(page.get("request"), dict)
            or receipt.get("id") != page["request"].get("id")
            or receipt.get("requested_url") != page["request"].get("url")):
        raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
    original = (_saved_completed_supplier_page(page["request"], Path(receipt["native_wrapper_reference"]).parent)
                if receipt.get("native_wrapper_reference") else
                _native_deadline_page_receipt(page["request"], Path(receipt["native_reference"]).parent)
                if receipt.get("native_reference") else receipt)
    if original != receipt:
        raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
    failed = saved["failed_concept_id"]
    origins = {_allowed_url(row["url"])[1] for row in expected["candidates"][failed]}
    if _allowed_url(page["request"]["url"])[1] not in origins:
        raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
    rows, all_receipts = {}, [receipt]
    for capture_id in saved["completed_capture_ids"]:
        identifier(capture_id)
        capture_path = safe_path(memory.root, "supplier_pages", capture_id) / "capture.json"
        capture = load_saved_supplier_capture(memory, capture_path)
        cid = capture["product"]["concept_id"]
        if (capture["product"] not in expected["products"]
                or capture["request"]["lead"]["urls"] not in [[row["url"]] for row in expected["candidates"][cid]]):
            raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
        rows.setdefault(cid, []).append(capture)
        all_receipts.extend(capture["bundle"]["receipts"])
    targets = []
    for product in expected["products"]:
        cid = product["concept_id"]
        captures = rows.get(cid, [])
        usable = any(row["bundle"]["item"] is not None for row in captures)
        refs = [row["path"] for row in captures]
        if cid == failed:
            refs.append(str(path))
        targets.append({"concept_id": cid, "description_hash": digest(product["description"]),
            "state": "interpretation_pending" if usable else "failed" if cid == failed or captures else "not_attempted",
            "evidence_refs": refs, "reason": "SOURCE_ACTION_TIMEOUT" if cid == failed else
                "Saved supplier pages need commercial-term interpretation." if usable else
                "Earlier supplier page reads are retained; the finite pass ended at its deadline." if captures else
                "This product's company pages were not visited before the source deadline."})
    return {"schema_version": 1, "route": "company-pages", "acquisition_id": expected["acquisition_id"],
        "observed_at": max((row["checked_at"] for row in all_receipts if isinstance(row, dict) and row.get("checked_at")), key=timestamp),
        "state": "failed", "dispatch_started": _actual(all_receipts, str(path)), "reference": str(path),
        "capture_ids": [], "observation_ids": [], "target_results": targets,
        "reason": "SOURCE_ACTION_TIMEOUT", "retryable": False, "failure_scope": "question"}


def collect_supplier_company_pages(memory, *, concept_ids, acquisition_id=None, as_of=None,
                                   page_reader=fetch_public_page, browser_reader=None, record_dispatch=None, max_companies=MAX_COMPANIES,
                                   candidate_urls=None, candidate_check_ids=None, saved_only=False, respect_access_waits=False,
                                   shared_acquisitions=False, owner_id=None, continuation=None):
    """Inspect actual discovered URLs; F, not result headings, judges terms."""
    if (type(saved_only) is not bool or type(respect_access_waits) is not bool or type(shared_acquisitions) is not bool
            or not isinstance(concept_ids, (list, tuple)) or not concept_ids or not callable(page_reader)
            or any(not isinstance(cid, str) for cid in concept_ids) or len(set(concept_ids)) != len(concept_ids)
            or browser_reader is not None and not callable(browser_reader)
            or record_dispatch is not None and not callable(record_dispatch)
            or type(max_companies) is not int or not 1 <= max_companies <= MAX_COMPANIES):
        raise SafetyError("SUPPLIER_ROUTE_TARGETS_INVALID")
    products = [_product(memory, identifier(cid)) for cid in concept_ids]
    for supplied, maximum in ((candidate_urls, 60), (candidate_check_ids, 200)):
        if supplied is not None and (not isinstance(supplied, dict) or set(supplied) != set(concept_ids)
                or any(not isinstance(values, list) or len(values) > maximum
                       or any(not isinstance(value, str) for value in values) or len(set(values)) != len(values)
                       for values in supplied.values())):
            raise SafetyError("SUPPLIER_COMPANY_FROZEN_INPUT_INVALID")
    if candidate_urls is not None:
        for urls in candidate_urls.values():
            for url in urls:
                if _allowed_url(url)[0] != url:
                    raise SafetyError("SUPPLIER_COMPANY_FROZEN_INPUT_INVALID")
    if candidate_check_ids is not None:
        for ids in candidate_check_ids.values():
            for record_id in ids:
                identifier(record_id)
    at = as_of or utc_now(); timestamp(at)
    state = memory.snapshot()
    candidates, input_errors = {}, {}
    for product in products:
        cid = product["concept_id"]
        rows = {record_id: row for record_id, row in state["source_checks"].items()
                if row.get("kind") == "supplier_route_search" and row.get("concept_id") == cid
                and row.get("description_hash") == digest(product["description"])}
        if candidate_check_ids is not None:
            if not set(candidate_check_ids[cid]) <= set(rows):
                candidates[cid] = []
                input_errors[cid] = "The saved supplier destinations no longer match this product; the other products can continue."
                continue
            rows = {record_id: rows[record_id] for record_id in candidate_check_ids[cid]}
        urls = {}
        for row in rows.values():
            for candidate in row["candidates"]:
                url, _ = _allowed_url(candidate["url"])
                urls.setdefault(url, candidate)
        if candidate_urls is not None:
            if not set(candidate_urls[cid]) <= set(urls):
                candidates[cid] = []
                input_errors[cid] = "A prepared supplier destination is not supported by this product's saved searches; the other products can continue."
                continue
            urls = {url: urls[url] for url in candidate_urls[cid]}
        words = tokens(product["description"].get("label", "") + " " + product["description"].get("function", ""))
        candidates[cid] = sorted(urls.values(), key=lambda row: (-len(words & tokens(row["title"])), row["url"]))[:max_companies]
    acquisition_id = identifier(acquisition_id) if acquisition_id is not None else key("supplier-company-pass", [VERSION, products, candidates])
    folder = safe_path(memory.root, "supplier_routes", acquisition_id)
    path = folder / "company_pass.json"
    deadline_path = folder / "company_deadline.json"
    deadline_binding = {"acquisition_id": acquisition_id, "products": products, "candidates": candidates,
                        "candidate_urls": candidate_urls, "candidate_check_ids": candidate_check_ids}
    if deadline_path.exists():
        return _replay_company_deadline(memory, deadline_path, deadline_binding)
    previous = load_json(path, MAX_RECORD_BYTES) if path.exists() else None
    if previous is not None and (not isinstance(previous, dict) or previous.get("products") != products
            or previous.get("candidates") != candidates or previous.get("acquisition_id") != acquisition_id
            or previous.get("candidate_urls") != candidate_urls or previous.get("candidate_check_ids") != candidate_check_ids):
        raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
    capture_requests = None
    if shared_acquisitions and (previous is None or "capture_requests" in previous):
        from .company_pages import company_capture_requests
        capture_requests = company_capture_requests(memory, deadline_binding, folder, as_of=at, saved_only=saved_only)
        if previous is not None and previous.get("capture_requests") != capture_requests:
            raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
    if saved_only and (not shared_acquisitions or previous is not None):
        return _replay_company_pages(memory, path=path, products=products, candidates=candidates, input_errors=input_errors,
            acquisition_id=acquisition_id, candidate_urls=candidate_urls, candidate_check_ids=candidate_check_ids, previous=previous,
            capture_requests=capture_requests)
    if shared_acquisitions:
        from .company_pages import shared_company_reader
        cached_reader = shared_company_reader(memory, http_reader=page_reader, browser_reader=browser_reader,
            as_of=at, owner_id=owner_id or acquisition_id, record_dispatch=record_dispatch, saved_only=saved_only,
            continuation=continuation)
    else:
        cached_reader = _cached_page_reader(memory, page_reader, at, record_dispatch=record_dispatch, browser_reader=browser_reader,
                                           respect_access_waits=respect_access_waits)
    targets, captures, all_receipts, target_failures = [], [], [], {}
    pending_original = None
    for product in products:
        cid = product["concept_id"]
        refs, usable, failures = [], False, []
        for candidate in candidates[cid]:
            try:
                request = ({"concept_id": cid, "lead": {"id": key("supplier-lead", candidate["url"]), "urls": [candidate["url"]]}}
                    if capture_requests is None else capture_requests[key("supplier-company-target", [cid, candidate["url"]])])
                capture = capture_saved_supplier_bundle(memory, request, fetcher=cached_reader)
                captures.append(capture["capture_id"])
                refs.append(capture["path"])
                all_receipts.extend(capture["bundle"]["receipts"])
                usable |= capture["bundle"]["item"] is not None
            except SourceActionDeadline as error:
                try:
                    page_reference = getattr(error, "supplier_cached_page_reference", None)
                    if page_reference is not None:
                        page = load_json(safe_path(page_reference), MAX_RECORD_BYTES)
                        deadline = {"binding": deadline_binding, "failed_concept_id": cid,
                            "page_reference": page_reference, "page_hash": digest(page), "receipt": page["receipt"],
                            "completed_capture_ids": sorted(set(captures))}
                        deadline["content_hash"] = digest(deadline)
                        atomic_json(deadline_path, deadline)
                except (SafetyError, OSError):
                    error.supplier_proof_save_error = "SOURCE_DEADLINE_PROOF_SAVE_FAILED"
                raise
            except SafetyError as error:
                from .company_pages import CompanyPagePending
                if isinstance(error, CompanyPagePending):
                    pending_original = error
                failures.append({"url": candidate["url"], "reason": str(error)})
                if str(error) in {"SAVED_FILE_UNAVAILABLE", "RUNNER_ALREADY_ACTIVE"}:
                    # A page can already be durable when its later canonical
                    # effort write fails. Keep that original actual reply in
                    # this pass instead of labelling it not dispatched. This
                    # read uses the already frozen product, not another fetch
                    # or an unverified current-memory snapshot.
                    request = ({"concept_id": cid, "lead": {"id": key("supplier-lead", candidate["url"]), "urls": [candidate["url"]]}}
                        if capture_requests is None else capture_requests[key("supplier-company-target", [cid, candidate["url"]])])
                    capture_id = _capture_key(product, request)
                    capture_path = safe_path(memory.root, "supplier_pages", capture_id) / "capture.json"
                    if capture_path.exists():
                        saved = load_json(capture_path, MAX_CAPTURE_BYTES)
                        bundle = _validate_bundle_record(saved, capture_id=capture_id, request=request, product=product)
                        captures.append(capture_id)
                        refs.append(str(capture_path))
                        all_receipts.extend(bundle["receipts"])
                        usable |= bundle["item"] is not None
        if failures:
            target_failures[cid] = failures
        targets.append({"concept_id": cid, "description_hash": digest(product["description"]),
                        "state": "interpretation_pending" if usable else "failed" if refs else "insufficient",
                        "evidence_refs": refs,
                        "reason": input_errors[cid] if cid in input_errors else
                                  "Saved supplier pages need commercial-term interpretation." if usable else
                                  "Discovered supplier pages did not produce readable information." if refs else
                                  "Discovered supplier pages could not be captured; the saved receipt records the reason." if failures else
                                  "No supplier destination page has been discovered for this product yet."})
    if pending_original is not None:
        # Healthy siblings above are durable. Do not freeze this finite pass
        # or its unfinished product bundle as an unsuccessful completed read.
        raise pending_original
    observed = max((row.get("checked_at") for row in all_receipts if isinstance(row, dict) and row.get("checked_at")),
                   key=timestamp, default=previous["envelope"]["observed_at"] if previous is not None else at)
    outcome = "interpretation_pending" if any(row["state"] == "interpretation_pending" for row in targets) else "failed" if captures else "insufficient"
    envelope = {"schema_version": 1, "route": "company-pages", "acquisition_id": acquisition_id, "observed_at": observed,
                "state": outcome, "dispatch_started": _actual(all_receipts, str(path)), "reference": str(path),
                "capture_ids": [], "observation_ids": [], "target_results": targets,
                "reason": "Actual discovered supplier pages are retained; relevance and commercial terms require F interpretation."}
    record = {"schema_version": 1, "acquisition_id": acquisition_id, "products": products, "candidates": candidates,
              "supplier_capture_ids": sorted(set(captures)), "receipt_hash": digest(all_receipts), "envelope": envelope,
              "target_failures": target_failures,
              "candidate_urls": candidate_urls, "candidate_check_ids": candidate_check_ids}
    if capture_requests is not None: record["capture_requests"] = capture_requests
    with Journal(folder).locked():
        if path.exists() and load_json(path, MAX_RECORD_BYTES) != record:
            raise SafetyError("SUPPLIER_COMPANY_PASS_CHANGED")
        atomic_json(path, record)
    for failures in target_failures.values():
        for failure in failures:
            if failure["reason"] in {"SAVED_FILE_UNAVAILABLE", "RUNNER_ALREADY_ACTIVE"}:
                # The verified original reply is durable. Let the controller
                # retain reconciliation-required state until its canonical
                # effort write has succeeded, not mark a half-saved task done.
                raise SafetyError(failure["reason"])
    return envelope


def create_supplier_handlers(memory, *, http_reader=fetch_public_page, browser_reader=None, page_reader=fetch_public_page,
                             respect_access_waits=False, shared_acquisitions=False):
    """Bridge the shared source controller; no second paid collection queue."""
    if type(respect_access_waits) is not bool or type(shared_acquisitions) is not bool:
        raise SafetyError("SUPPLIER_ROUTE_INPUT_INVALID")
    handlers = {}
    for route in ROUTES:
        def handle(context, selected_route=route, *, saved_only=False):
            request = context.get("request", {})
            if (not isinstance(request, dict) or context.get("route", selected_route) != selected_route
                    or context.get("request_hash", digest(request)) != digest(request)):
                raise SafetyError("SUPPLIER_ROUTE_QUERY_CHANGED")
            ids = context.get("concept_ids", request.get("concept_ids"))
            hashes = context.get("description_hashes")
            if hashes is not None:
                if (not isinstance(ids, (list, tuple)) or not isinstance(hashes, dict) or set(hashes) != set(ids)
                        or any(hashes[cid] != digest(_product(memory, identifier(cid))["description"]) for cid in ids)):
                    raise SafetyError("SUPPLIER_CAPTURE_DESCRIPTION_CHANGED")
            saved_only = saved_only or context.get("reconciling") is True
            continuation = context.get("company_unstarted_continuation")
            if continuation is not None:
                from .company_pages import company_unstarted_continuation
                if selected_route != "company-pages" or continuation != company_unstarted_continuation(memory, context):
                    raise SafetyError("SUPPLIER_COMPANY_UNSTARTED_PROOF_CHANGED")
                saved_only = False  # A separately admitted normal callback, not saved-only replay.
            dispatch = None if saved_only else context.get("record_dispatch")
            options = {"concept_ids": ids, "acquisition_id": context.get("acquisition_id"),
                       "as_of": context.get("as_of", request.get("as_of")), "record_dispatch": dispatch, "saved_only": saved_only}
            if respect_access_waits:
                options["respect_access_waits"] = True
            if selected_route == "company-pages":
                def render_page(item):
                    return fetch_supplier_native_page(item, route=selected_route,
                        folder=safe_path(memory.root, "supplier_routes", "native", item["id"]))
                return collect_supplier_company_pages(memory, page_reader=page_reader,
                    browser_reader=browser_reader or (None if shared_acquisitions else render_page), candidate_urls=request.get("candidate_urls"),
                    candidate_check_ids=request.get("candidate_check_ids"), shared_acquisitions=shared_acquisitions,
                    owner_id=context.get("task_id"), continuation=continuation, **options)
            query = request.get("query", request.get("term", context.get("family_query")))
            if isinstance(query, dict):
                query = query.get("text")
            render = browser_reader
            if render is None:
                def render(item):
                    return fetch_supplier_native_page(item, route=selected_route,
                        folder=safe_path(memory.root, "supplier_routes", "native", item["id"]))
            return collect_supplier_search(memory, route=selected_route, query=query,
                    http_reader=http_reader, browser_reader=render, **options)
        handlers[route] = {"handler": handle,
                           "reconcile": lambda context, callback=handle: callback(context, saved_only=True)}
        if route in SEARCH_ROUTES:
            # Explicitly saved-only: the controller can make one finite
            # correction to an old terminal false-dispatch wrapper without
            # changing its acquisition or admitting another collection.
            handlers[route]["recover_terminal"] = handlers[route]["reconcile"]
    if shared_acquisitions:
        from .supplier_search import create_shared_supplier_search_handlers
        handlers.update(create_shared_supplier_search_handlers(memory, http_reader=http_reader, browser_reader=browser_reader))
        from .company_pages import company_source_hints, company_unstarted_continuation
        due_hint, saved_hint = company_source_hints(memory)
        handlers["company-pages"].update(source_due_hint=due_hint, source_saved_hint=saved_hint,
                                        source_due_hint_from_state=due_hint, source_saved_hint_from_state=saved_hint,
                                        continue_unstarted=lambda context: company_unstarted_continuation(memory, context))
    return handlers
