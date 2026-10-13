"""Finite public supplier searches, saved separately from supplier promises.

Two India-focused Google searches shortlist real destination URLs. Search
headings only nominate pages to inspect; existing F interpretation judges the
captured destination pages. Search HTML failures never become a no-lead result.
No paid provider, browser login, supplier contact or report publication occurs.
"""

from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .discovery import normalise, tokens
from .models import SafetyError, canonical, digest, timestamp, utc_now
from .public_pages import fetch_public_page
from .research_memory import key
from .storage import Journal, atomic_json, load_json, safe_path
from .supplier_capture import _allowed_url
from .supplier_capture_store import _product, capture_saved_supplier_bundle
from .supplier_effort import failed_reader_receipt, record_supplier_effort

VERSION = "supplier-search-google-v1"
MAX_SEARCH_BYTES = 4 * 1024 * 1024
MAX_RESULTS_PER_QUERY = 10
MAX_LEADS = 3
_GOOGLE_HOSTS = {"google.com", "www.google.com", "google.co.in", "www.google.co.in"}
_NO_RESULTS = ("did not match any documents", "no results found for")
_BLOCKED = ("our systems have detected unusual traffic", "before you continue to google", "verify you are human")


def _destination(href):
    """Unwrap Google's ordinary result redirects; reject its own navigation."""
    if not isinstance(href, str):
        return None
    linked = urljoin("https://www.google.com/", href)
    parsed = urlsplit(linked)
    if parsed.hostname in _GOOGLE_HOSTS and parsed.path == "/url":
        params = parse_qs(parsed.query)
        linked = next(iter(params.get("q", params.get("url", []))), "")
    try:
        clean, _ = _allowed_url(linked)
    except SafetyError:
        return None
    host = urlsplit(clean).hostname
    if host in _GOOGLE_HOSTS or host.endswith(".google.com") or host.endswith(".google.co.in"):
        return None
    return clean


class _SearchHeadings(HTMLParser):
    """Accept externally linked H3 result headings, not arbitrary page links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchor, self.heading, self.skip, self.rows = None, False, [], []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.skip.append(tag)
        if self.skip:
            return
        if tag == "a":
            self.anchor = {"url": _destination(dict(attrs).get("href")), "title": [], "result_heading": False}
        elif tag == "h3" and self.anchor is not None:
            self.anchor["result_heading"] = True
            self.heading = True

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag == "h3":
            self.heading = False
        elif tag == "a" and self.anchor is not None:
            row = self.anchor
            title = " ".join("".join(row["title"]).split())[:2000]
            if row["url"] and row["result_heading"] and title:
                self.rows.append({"url": row["url"], "title": title})
            self.anchor, self.heading = None, False

    def handle_data(self, data):
        if not self.skip and self.heading and self.anchor is not None:
            self.anchor["title"].append(data)


def plan_supplier_search(memory, concept_id, *, route="google-public-html"):
    """Build two finite supplier queries from shared compact type wording."""
    if route not in ("google-public-html", "google-browser-html"):
        raise SafetyError("SUPPLIER_SEARCH_ROUTE_INVALID")
    product = _product(memory, concept_id)
    description = product["description"]
    if not isinstance(description.get("label"), str) or not isinstance(description.get("function", ""), str):
        raise SafetyError("SUPPLIER_SEARCH_LABEL_REQUIRED")
    from .product_queries import product_query_terms
    terms = product_query_terms(memory, concept_id, source="supplier", limit=2)
    first, second = terms[0]["text"], terms[-1]["text"]
    # Search hints are not exact-product/single-piece/dispatch confirmation.
    # F continues to judge the actual captured purchase unit and terms.
    queries = [first + " India manufacturer wholesaler supplier", second + " India dropshipping supplier single piece"]
    return {"version": VERSION, "route": route, "product": product, "description_hash": digest(description),
            "queries": queries, "country": "IN", "max_leads": MAX_LEADS,
            "client_ready": False}


def _validate_frozen_plan(memory, plan, *, concept_id, route):
    """Old frozen wording remains replayable after a query-profile upgrade."""
    if (route not in ("google-public-html", "google-browser-html")
            or not isinstance(plan, dict) or set(plan) != {"version", "route", "product", "description_hash", "queries",
                                                  "country", "max_leads", "client_ready"}
            or plan["version"] != VERSION or plan["route"] != route or plan["country"] != "IN"
            or plan["max_leads"] != MAX_LEADS or plan["client_ready"] is not False
            or plan["product"] != _product(memory, concept_id)
            or plan["description_hash"] != digest(plan["product"]["description"])
            or not isinstance(plan["queries"], list) or len(plan["queries"]) != 2
            or any(not isinstance(query, str) or not query.strip() or len(query) > 1000
                   or any(ord(c) < 32 or ord(c) == 127 for c in query) for query in plan["queries"])):
        raise SafetyError("SAVED_SUPPLIER_SEARCH_INVALID")
    return plan


def _search_url(query):
    return "https://www.google.com/search?" + urlencode({"q": query, "num": "10", "gl": "in", "hl": "en"})


def _parse_receipt(receipt, query, source_id):
    """Distinguish a readable result sample, explicit zero hits and a failure."""
    if not isinstance(receipt, dict) or receipt.get("state") != "usable":
        reason = receipt.get("reason") if isinstance(receipt, dict) else None
        if not isinstance(reason, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", reason):
            reason = "SUPPLIER_SEARCH_PAGE_UNAVAILABLE"
        return {"state": "unavailable", "reason": reason, "results": []}
    try:
        final = urlsplit(receipt.get("final_url", ""))
        if (receipt.get("id") != source_id or receipt.get("requested_url") != _search_url(query)
                or final.hostname not in _GOOGLE_HOSTS or final.path != "/search"
                or parse_qs(final.query).get("q") != [query]
                or receipt.get("http_status") != 200 or receipt.get("body_complete") is not True
                or not isinstance(receipt.get("html"), str) or len(receipt["html"]) > 1024 * 1024
                or not isinstance(receipt.get("text"), str)):
            raise ValueError
        timestamp(receipt.get("checked_at"))
        text = normalise(receipt["text"])
        if any(marker in text for marker in _BLOCKED):
            return {"state": "unavailable", "reason": "SUPPLIER_SEARCH_CHALLENGE", "results": []}
        parser = _SearchHeadings()
        parser.feed(receipt["html"])
        parser.close()
        results, seen = [], set()
        for row in parser.rows:
            if row["url"] not in seen:
                seen.add(row["url"])
                results.append(row)
            if len(results) == MAX_RESULTS_PER_QUERY:
                break
        if results:
            return {"state": "complete", "reason": "Bounded public result headings inspected; supplier relevance is unreviewed.", "results": results}
        if any(marker in text for marker in _NO_RESULTS):
            return {"state": "complete", "reason": "The readable search page explicitly reported no results.", "results": []}
        return {"state": "unavailable", "reason": "SUPPLIER_SEARCH_LAYOUT_UNRECOGNISED", "results": []}
    except (TypeError, ValueError, SafetyError):
        return {"state": "unavailable", "reason": "SUPPLIER_SEARCH_RECEIPT_INVALID", "results": []}


def _derive(plan, receipts):
    if not isinstance(receipts, list) or len(receipts) != 2:
        raise SafetyError("SAVED_SUPPLIER_SEARCH_INVALID")
    attempts, candidates = [], {}
    words = tokens(plan["product"]["description"]["label"] + " " + plan["product"]["description"].get("function", ""))
    for index, (query, receipt) in enumerate(zip(plan["queries"], receipts)):
        source_id = key("supplier-query", [plan["product"], index, query])
        parsed = _parse_receipt(receipt, query, source_id)
        attempts.append({"query": query, "receipt_id": source_id, **parsed})
        for row in parsed["results"]:
            lead_id = key("supplier-lead", row["url"])
            if lead_id not in candidates:
                candidates[lead_id] = {"id": lead_id, **row, "urls": [row["url"]],
                                       "query_indices": [], "review_state": "unreviewed"}
            candidates[lead_id]["query_indices"].append(index)
    ordered = sorted(candidates.values(), key=lambda row: (-len(words & tokens(row["title"])), row["id"]))
    # Prefer several companies before inspecting another page on one company.
    selected, deferred, hosts = [], [], set()
    for row in ordered:
        host = urlsplit(row["url"]).hostname
        if host not in hosts and len(selected) < MAX_LEADS:
            hosts.add(host)
            selected.append(row)
        else:
            deferred.append(row)
    selected.extend(deferred[:MAX_LEADS - len(selected)])
    completed = sum(row["state"] == "complete" for row in attempts)
    state = "complete" if completed == 2 else "partial" if completed else "blocked"
    return {"state": state, "attempts": attempts, "candidates": ordered, "selected_leads": selected,
            "candidate_count": len(candidates), "client_ready": False}


def discover_saved_supplier_leads(memory, concept_id, *, fetcher=fetch_public_page, route="google-public-html", search_id=None):
    """Save two search receipts once; replay retains both failures and dates.

    A successful finite search is not successful supplier confirmation. The
    result includes unreviewed URLs for ``capture_discovered_supplier_pages``.
    A no-lead completion is possible only after two recognised readable pages
    report no results, never after an HTTP/layout/challenge failure.
    """
    if not callable(fetcher):
        raise SafetyError("SUPPLIER_SEARCH_READER_INVALID")
    if route == "google-browser-html" and fetcher is fetch_public_page:
        raise SafetyError("SUPPLIER_BROWSER_READER_REQUIRED")
    if search_id is None:
        plan = plan_supplier_search(memory, concept_id, route=route)
        search_id = key("supplier-search", plan)
    else:
        from .models import identifier
        identifier(search_id)
        saved = load_json(safe_path(memory.root, "supplier_searches", search_id) / "search.json", MAX_SEARCH_BYTES)
        plan = _validate_frozen_plan(memory, saved.get("plan"), concept_id=concept_id, route=route)
        if key("supplier-search", plan) != search_id:
            raise SafetyError("SAVED_SUPPLIER_SEARCH_INVALID")
    folder = safe_path(memory.root, "supplier_searches", search_id)
    path = folder / "search.json"
    progress_path = folder / "progress.json"
    with Journal(folder).locked():
        reused = path.exists()
        if path.is_symlink():
            raise SafetyError("SAVED_SUPPLIER_SEARCH_INVALID")
        if reused:
            record = load_json(path, MAX_SEARCH_BYTES)
        else:
            if progress_path.exists():
                progress = load_json(progress_path, MAX_SEARCH_BYTES)
                try:
                    if (not isinstance(progress, dict) or set(progress) != {"schema_version", "search_id", "plan", "receipts", "started_at", "content_hash"}
                            or type(progress["schema_version"]) is not int or progress["schema_version"] != 1
                            or progress["search_id"] != search_id or progress["plan"] != plan
                            or not isinstance(progress["receipts"], list) or len(progress["receipts"]) > 2
                            or progress["content_hash"] != digest([plan, progress["receipts"], progress["started_at"]])):
                        raise ValueError
                    timestamp(progress["started_at"])
                except (KeyError, TypeError, ValueError, SafetyError):
                    raise SafetyError("SAVED_SUPPLIER_SEARCH_PROGRESS_INVALID") from None
            else:
                started_at = utc_now()
                progress = {"schema_version": 1, "search_id": search_id, "plan": plan, "receipts": [],
                            "started_at": started_at, "content_hash": digest([plan, [], started_at])}
                atomic_json(progress_path, progress)
            receipts = deepcopy(progress["receipts"])
            for index in range(len(receipts), len(plan["queries"])):
                query = plan["queries"][index]
                item = {"id": key("supplier-query", [plan["product"], index, query]), "url": _search_url(query)}
                try:
                    receipt = fetcher(item)
                except Exception as error:
                    receipt = failed_reader_receipt(item, "SUPPLIER_SEARCH_FETCH_FAILED", error)
                receipts.append(receipt)
                progress = {**progress, "receipts": deepcopy(receipts),
                            "content_hash": digest([plan, receipts, progress["started_at"]])}
                if len(canonical(progress)) > MAX_SEARCH_BYTES:
                    raise SafetyError("SUPPLIER_SEARCH_SIZE_LIMIT")
                atomic_json(progress_path, progress)
            result = _derive(plan, receipts)
            record = {"schema_version": 1, "search_id": search_id, "plan": plan, "receipts": receipts,
                      "attempted_at": progress["started_at"], "result": result,
                      "content_hash": digest([plan, receipts, result, progress["started_at"]])}
            if len(canonical(record)) > MAX_SEARCH_BYTES:
                raise SafetyError("SUPPLIER_SEARCH_SIZE_LIMIT")
            atomic_json(path, record)
        try:
            if (not isinstance(record, dict) or set(record) != {"schema_version", "search_id", "plan", "receipts", "attempted_at", "result", "content_hash"}
                    or type(record["schema_version"]) is not int or record["schema_version"] != 1
                    or record["search_id"] != search_id or record["plan"] != plan
                    or record["content_hash"] != digest([plan, record["receipts"], record["result"], record["attempted_at"]])
                    or record["result"] != _derive(plan, record["receipts"])):
                raise ValueError
            timestamp(record["attempted_at"])
        except (KeyError, TypeError, ValueError, SafetyError):
            raise SafetyError("SAVED_SUPPLIER_SEARCH_INVALID") from None
        _validate_frozen_plan(memory, plan, concept_id=concept_id, route=route)
        # Only parsed complete receipts can date a successful search check.
        completed_dates = [record["receipts"][i]["checked_at"] for i, attempt in enumerate(record["result"]["attempts"])
                           if attempt["state"] == "complete"]
        at = max(completed_dates, key=timestamp) if completed_dates else None
        expected_requests = {key("supplier-query", [plan["product"], index, query]): _search_url(query)
                             for index, query in enumerate(plan["queries"])}
        result = record["result"]
        outcome = ("completed_empty" if result["state"] == "complete" and not result["candidate_count"] else
                   "blocked" if result["state"] == "blocked" else "partial" if result["state"] == "partial" else
                   "interpretation_pending")
        effort = record_supplier_effort(memory, concept_id=concept_id, description=plan["product"]["description"],
             pass_id=search_id, receipts=record["receipts"], receipt_reference=str(path), outcome=outcome, route=route,
             expected_requests=expected_requests)
        return {"search_id": search_id, "path": str(path), "plan": deepcopy(plan),
                "result": deepcopy(record["result"]), "searched_at": at,
                "attempted_at": record["attempted_at"], "reused": reused, "client_ready": False,
                "effort_check_id": effort["check_id"], "effort_satisfied": effort["record"]["effort_satisfied"]}


def supplier_search_check(search):
    """Return a scoped check; zero hits is complete only on readable searches.

    Nonempty results remain attempted until captured-page F review. This check
    records searching, never pretends an unreviewed candidate is a supplier.
    """
    result = search["result"]
    no_results = result["state"] == "complete" and result["candidate_count"] == 0
    return {"source": "supplier", "route": search["plan"]["route"], "at": search["searched_at"] or search["attempted_at"],
            "state": "usable" if no_results else "insufficient" if result["state"] in ("complete", "partial") else "blocked",
            "reason": "Two readable bounded searches returned no supplier result headings." if no_results else
                      "Discovered destinations require captured-page relevance review." if result["candidate_count"] else
                      "Search pages were unavailable or their result layout was not recognised.",
            "reference": search["path"], "concept_id": search["plan"]["product"]["concept_id"],
            "description_hash": search["plan"]["description_hash"], "search_id": search["search_id"],
            "supplier_search_status": "complete" if no_results else "attempted" if result["candidate_count"] else "blocked",
            "candidate_count": result["candidate_count"]}


def capture_discovered_supplier_pages(memory, search, *, fetcher=fetch_public_page):
    """Capture up to three discovered destinations through the existing store.

    Return trusted capture paths for ordinary F interpretation. Commercial
    terms must come from these pages, not Google result headings. Failure to
    fetch nominated pages remains visible and does not assert no supplier.
    """
    if (not isinstance(search, dict) or not isinstance(search.get("plan"), dict)
            or not isinstance(search.get("result"), dict) or not callable(fetcher)):
        raise SafetyError("SUPPLIER_SEARCH_RESULT_INVALID")
    # Read the saved result without performing a search to prevent an arbitrary
    # caller-provided URL list being relabelled as discovered source evidence.
    try:
        concept_id = search["plan"]["product"]["concept_id"]
        saved_path = Path(search.get("path", "")).absolute()
        expected_id = key("supplier-search", search["plan"])
        expected = safe_path(memory.root, "supplier_searches", expected_id) / "search.json"
        if saved_path != expected or search.get("search_id") != expected_id:
            raise ValueError
        record = load_json(expected, MAX_SEARCH_BYTES)
        if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
                or record["schema_version"] != 1 or record.get("search_id") != expected_id
                or record.get("plan") != search["plan"] or record.get("result") != search["result"]
                or record.get("content_hash") != digest([record["plan"], record["receipts"], record["result"], record["attempted_at"]])
                or _derive(record["plan"], record["receipts"]) != search["result"]
                or _validate_frozen_plan(memory, search["plan"], concept_id=concept_id,
                                         route=search["plan"]["route"]) != search["plan"]):
            raise ValueError
    except (KeyError, TypeError, ValueError, SafetyError):
        raise SafetyError("SUPPLIER_SEARCH_RESULT_INVALID") from None
    captures, failures = [], []
    for lead in search["result"]["selected_leads"]:
        result = capture_saved_supplier_bundle(memory, {"concept_id": concept_id,
             "lead": {"id": lead["id"], "urls": lead["urls"]}}, fetcher=fetcher)
        captures.append(result)
        if result["bundle"]["state"] == "no_usable_pages":
            failures.append({"lead_id": lead["id"], "reason": "SUPPLIER_DESTINATION_UNAVAILABLE"})
    return {"search_id": search["search_id"], "captures": captures, "failures": failures,
            "pending_interpretation_paths": [row["path"] for row in captures if row["bundle"]["item"] is not None],
            "no_results": search["result"]["state"] == "complete" and search["result"]["candidate_count"] == 0,
            "client_ready": False}
