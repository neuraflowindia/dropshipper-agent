"""Bounded free marketplace routes with immutable original capture receipts.

Amazon list ranks are list presence, not unit sales. Generic Amazon/Flipkart
search cards provide listing/price/photo context, never bestseller proof.
Parsing/replay/import do not navigate or call a model. Only capture navigates.
"""

import asyncio
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .source_deadline import (SourceActionDeadline, interrupted_transport, saved_completed_transport,
                              validate_interrupted_transport, source_phase, local_source_work)
from .runtime_storage import runtime_path
from .storage import load_json, safe_path

VERSION = "direct-marketplace-v1"
ROUTES = ("amazon-bestsellers", "amazon-search", "flipkart-search")
DATA_ROOT = runtime_path(Path(__file__).resolve().parents[2], "data/research/direct-marketplace")
MAX_HTML = 8 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
MAX_TEXT = 75000


def safe_source_retry_after(value):
    """Keep only the bounded public delay header, never cookies/full headers."""
    return value.strip() if (isinstance(value, str) and len(value) <= 256
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)) else None


_BLOCKED = re.compile(r"captcha|robot check|not a robot|verify (?:you are|that you are) human|"
                      r"automated access|enter the characters you see|access denied|too many requests", re.I)
_LISTS = {"amazon-bestsellers": "bestsellers"}

SNAPSHOT_JS = r"""() => {
 const text=document.body?.innerText || '';
 const html=document.documentElement.outerHTML;
 const amazon=location.hostname==='www.amazon.in';
 const list=amazon && /\/(?:gp\/bestsellers|zgbs)\//.test(location.pathname);
 let roots=[];
 if(list) roots=Array.from(document.querySelectorAll('.zg-bdg-text')).map(r=>r.closest('.zg-grid-general-faceout,.zg-item-immersion,#gridItemRoot')).filter(Boolean);
 else if(amazon) roots=Array.from(document.querySelectorAll('.s-result-item[data-asin]')).filter(e=>e.getAttribute('data-asin'));
 else {
  const anchors=Array.from(document.querySelectorAll('a[href*="/p/"]')).filter(a=>new URL(a.href).searchParams.has('pid'));
  for(const a of anchors){
   let root=a.closest('div[data-id],.tUxRFH,.slAVV4,._1AtVbE');
   if(!root){let node=a.parentElement;for(let n=0;node && n<5;n++,node=node.parentElement){
     const ids=new Set(Array.from(node.querySelectorAll('a[href*="/p/"]')).map(x=>new URL(x.href).searchParams.get('pid')).filter(Boolean));
     if(ids.size===1 && node.querySelector('.hZ3P6w,.Nx9bqj,._30jeq3')) root=node;
   }}
   if(root) roots.push(root);
  }
 }
 roots=Array.from(new Set(roots));
 const cards=roots.slice(0,100).map((card,i)=>{
  const link=card.querySelector(amazon?'a[href*="/dp/"],a[href*="/gp/product/"]':'a[href*="/p/"]')
    || (amazon ? card.querySelector('a[href*="/sspa/click"]') : null);
  const image=card.querySelector('img');
  const title=card.querySelector(list?'.p13n-sc-truncate-desktop-type2,.p13n-sc-truncate':amazon?'h2 span,h2 a,h2':'a[title],.KzDlHZ,._4rR01,.s1Q9rs,.wjcEIp');
  const price=card.querySelector(amazon?'.p13n-sc-price,.a-price:not(.a-text-price) .a-offscreen,.a-color-price':'.hZ3P6w,.Nx9bqj,._30jeq3');
  return {card_ref:'card-'+i,rank_text:list?(card.querySelector('.zg-bdg-text')?.innerText || ''):null,
   title:(title?.getAttribute('title') || title?.innerText || image?.alt || link?.innerText || '').trim().slice(0,4000),
   product_url:link?.href || null,raw_price:price?.innerText.trim().slice(0,1000) || null,
   raw_price_truncated:(price?.innerText.trim().length || 0)>1000,
   image_url:image?.currentSrc || image?.src || null};
 });
 const heading=Array.from(document.querySelectorAll('h1,.zg-selected,#zg_banner_text')).map(e=>e.innerText.trim()).filter(Boolean).join('\n').slice(0,8000);
 const query=document.querySelector(amazon?'#twotabsearchtextbox':'input[name=q]')?.value || null;
 const empty=!list && /no results for|did not match any products|sorry, no results found|no products found/i.test(text);
 return {url:location.href,title:document.title.slice(0,2000),heading,query,
  text:text.slice(0,75000),text_truncated:text.length>75000,card_count:roots.length,cards,empty,
  html:html.length<=8388608?html:null,html_characters:html.length};
}"""


def build_marketplace_request(route, *, term=None, department=None, limit=30):
    """Build one first-page public request, keeping the product-family query."""
    if route not in ROUTES or type(limit) is not int or not 1 <= limit <= 100:
        raise SafetyError("MARKETPLACE_REQUEST_INVALID")
    if route in _LISTS:
        if (not isinstance(department, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}(?:/[0-9]{1,20})?", department)
                or term is not None):
            raise SafetyError("MARKETPLACE_LIST_SCOPE_INVALID")
        url = "https://www.amazon.in/gp/" + _LISTS[route] + "/" + department
    else:
        if (not isinstance(term, str) or not term.strip() or len(term) > 160
                or re.search(r"[\x00-\x1f\x7f]", term) or department is not None):
            raise SafetyError("MARKETPLACE_TERM_INVALID")
        term = " ".join(term.split())
        url = ("https://www.amazon.in/s?" + urlencode({"k": term}) if route == "amazon-search"
               else "https://www.flipkart.com/search?" + urlencode({"q": term}))
    return {"schema_version": 1, "route": route, "url": url, "term": term,
            "department": department, "limit": limit, "country": "IN", "pages": 1}


def validate_request(request):
    if (not isinstance(request, dict) or set(request) != {"schema_version", "route", "url", "term", "department", "limit", "country", "pages"}
            or request.get("schema_version") != 1 or type(request["schema_version"]) is not int
            or request.get("country") != "IN" or type(request.get("pages")) is not int or request["pages"] != 1):
        raise SafetyError("MARKETPLACE_REQUEST_INVALID")
    expected = build_marketplace_request(request["route"], term=request["term"], department=request["department"], limit=request["limit"])
    if expected != request:
        raise SafetyError("MARKETPLACE_REQUEST_INVALID")
    return request


def _same_scope(url, request):
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\\]", url):
        return False
    try:
        parsed, wanted = urlsplit(url), urlsplit(request["url"])
        if (parsed.scheme != "https" or parsed.netloc != wanted.netloc or parsed.fragment
                or parsed.username or parsed.password):
            return False
        if request["route"] in _LISTS:
            path = re.sub(r"/ref=[A-Za-z0-9_-]+/?$", "", parsed.path).rstrip("/")
            category_path = wanted.path.rstrip("/")
            if request["route"] == "amazon-bestsellers" and path.startswith("/zgbs/"):
                path = "/gp/bestsellers/" + path[len("/zgbs/"):]
            query = parse_qs(parsed.query, keep_blank_values=True)
            return (path == category_path and not set(query) - {"ref", "ref_", "pg"}
                    and all(len(values) == 1 for values in query.values()) and query.get("pg", ["1"]) == ["1"])
        name = "k" if request["route"] == "amazon-search" else "q"
        query = parse_qs(parsed.query, keep_blank_values=True)
        return (parsed.path == wanted.path and query.get(name) == [request["term"]]
                and query.get("page", ["1"]) == ["1"])
    except (ValueError, TypeError):
        return False


def _product(url, source):
    try:
        if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\\]", url):
            return None
        parsed = urlsplit(url)
        host = "www.amazon.in" if source == "amazon" else "www.flipkart.com"
        if parsed.scheme != "https" or parsed.netloc != host or parsed.username or parsed.password:
            return None
        if source == "amazon":
            # Sponsored cards encode the actual product destination in an
            # Amazon-owned link. Decode the saved link once, without clicking
            # the ad or navigating anywhere. Never infer an identity from text.
            if parsed.path == "/sspa/click":
                targets = parse_qs(parsed.query).get("url")
                if not targets or len(targets) != 1 or len(targets[0]) > 4096 or re.search(r"[\x00-\x20\\]", targets[0]):
                    return None
                parsed = urlsplit(urljoin("https://www.amazon.in/", targets[0]))
                if parsed.scheme != "https" or parsed.netloc != host or parsed.username or parsed.password:
                    return None
            match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", parsed.path)
            return (match[1], "https://www.amazon.in/dp/" + match[1]) if match else None
        values = parse_qs(parsed.query).get("pid")
        if not re.search(r"/p/[A-Za-z0-9]+/?$", parsed.path) or not values or len(values) != 1 or not re.fullmatch(r"[A-Z0-9]{6,40}", values[0]):
            return None
        return values[0], urlunsplit(("https", host, parsed.path, urlencode({"pid": values[0]}), ""))
    except (TypeError, ValueError):
        return None


def _image(url, source):
    try:
        if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\\]", url):
            return None
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
        host = parsed.hostname or ""
        allowed = (host.endswith(".media-amazon.com") or host.endswith(".ssl-images-amazon.com")) if source == "amazon" else host.endswith(".flixcart.com")
        return url if allowed else None
    except (TypeError, ValueError):
        return None


def parse_marketplace_snapshot(snapshot, request, *, navigation_status=200):
    """Validate original cards without interpreting same-product relevance."""
    validate_request(request)
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("cards"), list) or len(snapshot["cards"]) > 100
            or not isinstance(snapshot.get("text", ""), str) or len(snapshot.get("text", "")) > MAX_TEXT
            or type(snapshot.get("card_count")) is not int or snapshot["card_count"] < len(snapshot["cards"])):
        raise SafetyError("MARKETPLACE_SNAPSHOT_INVALID")
    source = "flipkart" if request["route"] == "flipkart-search" else "amazon"
    list_type = _LISTS.get(request["route"], "product_search")
    result = {"schema_version": 1, "source": source, "route": request["route"], "list_type": list_type,
              "state": "insufficient", "reason": "MARKETPLACE_SCOPE_UNVERIFIED", "scope_verified": False,
              "records": [], "rejected": [], "duplicates": [], "observed_cards": snapshot["card_count"],
              "bounded_cards": request["limit"], "sales_proof": False}
    if navigation_status in (403, 429, 503) or _BLOCKED.search(snapshot.get("text", "") + "\n" + str(snapshot.get("title", ""))):
        return dict(result, state="blocked", reason="MARKETPLACE_ACCESS_CHALLENGE")
    if not _same_scope(snapshot.get("url"), request):
        return result
    heading = str(snapshot.get("heading", "")) + "\n" + str(snapshot.get("title", ""))
    if list_type != "product_search" and not re.search(r"best\s*sell(?:er|ers|ing)", heading, re.I):
        return result
    if list_type == "product_search" and snapshot.get("query") and " ".join(str(snapshot["query"]).split()).casefold() != request["term"].casefold():
        return result
    result["scope_verified"] = True
    if type(navigation_status) is not int or navigation_status != 200:
        return dict(result, reason="MARKETPLACE_HTTP_STATUS_UNVERIFIED")
    if not snapshot["cards"]:
        if list_type == "product_search" and snapshot.get("empty") is True:
            return dict(result, state="empty", reason="MARKETPLACE_EXPLICIT_NO_RESULTS")
        return dict(result, reason="MARKETPLACE_CARDS_UNRECOGNISED")
    seen, conflicts = {}, set()
    for index, card in enumerate(snapshot["cards"][:request["limit"]]):
        reference = card.get("card_ref", str(index)) if isinstance(card, dict) else str(index)
        if not isinstance(card, dict):
            result["rejected"].append({"card_ref": reference, "reason": "invalid_card"}); continue
        product = _product(card.get("product_url"), source)
        title = card.get("title")
        rank = re.fullmatch(r"#?\s*([1-9][0-9]{0,6})", str(card.get("rank_text", "")).strip()) if list_type != "product_search" else None
        if not product or not isinstance(title, str) or not title.strip() or len(title) > 4000 or (list_type != "product_search" and not rank):
            result["rejected"].append({"card_ref": reference, "reason": "product_identity_title_or_rank_missing"}); continue
        quote = card.get("raw_price")
        if quote is not None and (not isinstance(quote, str) or len(quote) > 1000):
            result["rejected"].append({"card_ref": reference, "reason": "price_type_or_size_invalid"}); continue
        quote = quote.strip() if quote and quote.strip() else None
        quantity = re.search(r"\b(?:pack\s+of\s+[0-9]+|[0-9]+\s*(?:pcs|pieces|pack|g|kg|ml|litres?|liters?))\b", title, re.I)
        record = {"identity": product[0], "title": title.strip(), "product_url": product[1],
                  "position": int(rank[1]) if rank else None, "search_position": index + 1 if not rank else None,
                  "position_kind": "explicit_list_rank_not_unit_sales" if rank else "search_order_not_sales_rank",
                  "list_type": list_type, "requested_list_url": request["url"], "observed_list_url": snapshot["url"],
                  "raw_price": quote, "currency": "INR" if quote and re.search(r"₹|\bINR\b|\bRs\.?\s*[0-9]", quote) else None,
                  "price_basis": "retail_displayed_quote", "unit_pack_basis": quantity[0] if quantity else "unknown",
                  "quote_complete": card.get("raw_price_truncated") is not True,
                  "image_url": _image(card.get("image_url"), source), "card_refs": [reference],
                  "limitations": [] if rank else ["product_search_not_sales_proof"]}
        if product[0] in seen:
            previous = seen[product[0]]
            comparable = lambda row: {field: value for field, value in row.items() if field not in ("card_refs", "search_position")}
            conflict = comparable(previous) != comparable(record)
            if conflict: conflicts.add(product[0])
            previous["card_refs"].append(reference)
            result["duplicates"].append({"identity": product[0], "card_ref": reference, "conflicting": conflict})
        else:
            seen[product[0]] = record
    for identity, row in seen.items():
        if identity in conflicts:
            result["rejected"].append({"identity": identity, "card_refs": row["card_refs"], "reason": "conflicting_duplicate_identity"})
        else: result["records"].append(row)
    if result["records"]:
        partial = bool(result["rejected"] or snapshot["card_count"] > request["limit"])
        result.update(state="partial" if partial else "usable", reason="MARKETPLACE_CARDS_PARTIALLY_USABLE" if partial else "MARKETPLACE_CARDS_OBSERVED")
    else:
        result["reason"] = "MARKETPLACE_CARDS_REJECTED"
    return result


def verified_list_observation(observation):
    """Preserve the old paid contract; verify the additive direct-list marker."""
    facts = observation.get("facts", {})
    if (observation.get("source") != "amazon" or facts.get("limitations")
            or facts.get("list_type") != "bestsellers"):
        return False
    if "direct_capture" in facts:
        return (facts.get("scope_verified") is True and type(facts.get("position")) is int
                and facts["position"] > 0 and isinstance(facts["direct_capture"], dict))
    return True


@local_source_work("response_save")
def _write_new(path, raw):
    if path.is_symlink() or path.exists():
        raise SafetyError("MARKETPLACE_ARTIFACT_EXISTS")
    with path.open("xb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


async def _playwright_snapshot(request, timeout, browser_path=None):
    browser = driver = starter = None
    result = {"state": "failed", "reason": "MARKETPLACE_BROWSER_FAILED", "snapshot": None,
              "navigation_started": False, "stage": "browser_launch"}
    async def run():
        nonlocal browser, driver, starter
        from playwright.async_api import async_playwright
        starter = async_playwright(); driver = await starter.start()
        browser = await driver.chromium.launch(headless=True, timeout=timeout * 1000,
                    **({"executable_path": str(browser_path)} if browser_path else {}))
        context = await browser.new_context(locale="en-IN", accept_downloads=False, service_workers="block")
        page = await context.new_page()
        result.update(stage="navigation", navigation_started=True, navigation_started_at=utc_now())
        response = await page.goto(request["url"], wait_until="domcontentloaded", timeout=(timeout - 8) * 1000)
        result["navigation_status"] = response.status if response else None
        result["retry_after"] = safe_source_retry_after(response.headers.get("retry-after")) if response else None
        try: await page.wait_for_function("() => document.querySelector('.zg-bdg-text,.s-result-item[data-asin],a[href*=\"/p/\"]') || /captcha|no results|robot|access denied/i.test(document.body?.innerText || '')", timeout=3000)
        except Exception: pass
        result["stage"] = "snapshot"
        result["snapshot"] = await page.evaluate(SNAPSHOT_JS)
        result.update(state="captured", reason="One rendered public page was captured", stage="complete")
    try:
        await asyncio.wait_for(run(), timeout=timeout - 4)
    except ImportError:
        result.update(state="unavailable", reason="MARKETPLACE_BROWSER_DEPENDENCY_UNAVAILABLE")
    except Exception as error:
        code = "MARKETPLACE_BROWSER_DEADLINE" if isinstance(error, asyncio.TimeoutError) else "MARKETPLACE_BROWSER_FAILED"
        result.update(state="failed", reason=code, exception_type=type(error).__name__)
    finally:
        async def close():
            if browser is not None: await browser.close()
            if driver is not None: await driver.stop()
            elif starter is not None: await starter.__aexit__(None, None, None)
        try:
            await asyncio.wait_for(close(), timeout=4)
            result["owned_process_exited"] = True
        except Exception:
            result.update(state="failed", reason="MARKETPLACE_BROWSER_CLEANUP_UNVERIFIED", owned_process_exited=False)
    return result


def replay_marketplace(folder):
    """Verify one immutable capture, including failed/blocked attempts."""
    folder = safe_path(folder)
    intent = load_json(folder / "intent.json", 16384)
    receipt = load_json(folder / "receipt.json", MAX_JSON)
    request = validate_request(intent.get("request"))
    deadline = receipt.get("outer_deadline") is True
    if (intent.get("version") != VERSION or receipt.get("version") != VERSION
            or receipt.get("intent_hash") != digest(intent) or receipt.get("request") != request
            or receipt.get("route") != request["route"] or receipt.get("acquisition_id") != intent.get("acquisition_id")
            or receipt.get("evidence_type") != intent.get("evidence_type")
            or receipt.get("state") not in ("usable", "partial", "empty", "insufficient", "blocked", "failed", "unavailable")
            or receipt.get("apify_requests") != 0 or receipt.get("model_calls") != 0 or receipt.get("apify_charge_usd") != "0"
            or (type(receipt.get("dispatch_started")) is not bool and not (deadline and receipt.get("dispatch_started") is None))
            or not isinstance(receipt.get("artifacts"), dict)):
        raise SafetyError("MARKETPLACE_RECEIPT_INVALID")
    identifier(receipt["acquisition_id"])
    timestamp(receipt["started_at"]); timestamp(receipt["completed_at"])
    if timestamp(receipt["started_at"]) > timestamp(receipt["completed_at"]):
        raise SafetyError("MARKETPLACE_RECEIPT_INVALID")
    for name, expected in receipt["artifacts"].items():
        limits = {"snapshot.json": MAX_JSON, "parsed.json": MAX_JSON, "rendered.html": MAX_HTML, "transport.json": MAX_JSON,
                  "native_result.json": 12 * 1024 * 1024, "owned_process.json": 16384,
                  "native_dispatch.json": 16384, "native_outer_deadline.json": 16384, "outer_deadline.json": 16384}
        if name not in limits:
            raise SafetyError("MARKETPLACE_ARTIFACT_INVALID")
        path = safe_path(folder / name)
        if not path.is_file() or path.stat().st_size > limits[name] or sha256(path.read_bytes()).hexdigest() != expected:
            raise SafetyError("MARKETPLACE_ARTIFACT_HASH_MISMATCH")
    if deadline:
        if (receipt["state"] != "failed" or receipt["reason"] != "SOURCE_ACTION_TIMEOUT"
                or "outer_deadline.json" not in receipt["artifacts"]):
            raise SafetyError("MARKETPLACE_RECEIPT_INVALID")
        proof = validate_interrupted_transport(load_json(folder / "outer_deadline.json", 16384), requested_url=request["url"])
        if (receipt["dispatch_started"] != proof["navigation_started"]
                or receipt.get("dispatch_started_at") != proof.get("navigation_started_at")
                or receipt.get("owned_process_exited") != proof.get("owned_process_exited")):
            raise SafetyError("MARKETPLACE_TRANSPORT_MISMATCH")
        if proof["navigation_started"] is True:
            if "native_dispatch.json" not in receipt["artifacts"]:
                raise SafetyError("MARKETPLACE_TRANSPORT_MISMATCH")
            dispatch = load_json(folder / "native_dispatch.json", 16384)
            if (dispatch.get("url") != request["url"] or dispatch.get("navigation_started") is not True
                    or dispatch.get("navigation_started_at") != proof["navigation_started_at"]):
                raise SafetyError("MARKETPLACE_TRANSPORT_MISMATCH")
        elif proof["navigation_started"] is False:
            if "native_outer_deadline.json" not in receipt["artifacts"]:
                raise SafetyError("MARKETPLACE_TRANSPORT_MISMATCH")
            native = load_json(folder / "native_outer_deadline.json", 16384)
            if native.get("launch_invoked") is not False or native.get("requested_url") != request["url"]:
                raise SafetyError("MARKETPLACE_TRANSPORT_MISMATCH")
    elif "transport.json" in receipt["artifacts"]:
        transport = load_json(folder / "transport.json", MAX_JSON)
        if (receipt["dispatch_started"] != (transport.get("navigation_started") is True)
                or receipt.get("dispatch_started_at") != transport.get("navigation_started_at")
                or receipt.get("navigation_status") != transport.get("navigation_status")
                or receipt.get("retry_after") != safe_source_retry_after(transport.get("retry_after"))
                or receipt.get("owned_process_exited") != transport.get("owned_process_exited")):
            raise SafetyError("MARKETPLACE_TRANSPORT_MISMATCH")
        if receipt["state"] in ("usable", "partial", "empty") and (
                not receipt["dispatch_started"] or transport.get("state") != "captured"
                or receipt.get("owned_process_exited") is not True):
            raise SafetyError("MARKETPLACE_TRANSPORT_MISMATCH")
    if "parsed.json" in receipt["artifacts"] and not deadline:
        if "snapshot.json" not in receipt["artifacts"]:
            raise SafetyError("MARKETPLACE_RECEIPT_INVALID")
        snapshot = load_json(folder / "snapshot.json", MAX_JSON)
        parsed = parse_marketplace_snapshot(snapshot, request, navigation_status=receipt.get("navigation_status"))
        if parsed != load_json(folder / "parsed.json", MAX_JSON) or receipt.get("parsed_hash") != digest(parsed):
            raise SafetyError("MARKETPLACE_PARSED_MISMATCH")
        if (receipt.get("usable_cards") != len(parsed["records"]) or receipt.get("scope_verified") != parsed["scope_verified"]
                or (receipt["state"] not in ("failed", "unavailable") and any(receipt[field] != parsed[field] for field in ("state", "reason")))):
            raise SafetyError("MARKETPLACE_RECEIPT_INVALID")
        if (receipt.get("transport_attempts", [])[-1:] == ["owned_native_windows_chrome"]
                and "native_result.json" in receipt["artifacts"]):
            native = load_json(folder / "native_result.json", 12 * 1024 * 1024)
            original = deepcopy(native.get("snapshot"))
            if not isinstance(original, dict): raise SafetyError("MARKETPLACE_ORIGINAL_SNAPSHOT_INVALID")
            html = original.pop("html", None)
            if original != snapshot or native.get("navigation_status") != receipt.get("navigation_status"):
                raise SafetyError("MARKETPLACE_ORIGINAL_SNAPSHOT_MISMATCH")
            if receipt.get("retry_after") != safe_source_retry_after(native.get("retry_after")):
                raise SafetyError("MARKETPLACE_TRANSPORT_MISMATCH")
            if isinstance(html, str) and ("rendered.html" not in receipt["artifacts"] or sha256(html.encode()).hexdigest() != receipt["artifacts"]["rendered.html"]):
                raise SafetyError("MARKETPLACE_ORIGINAL_HTML_MISMATCH")
    elif receipt["state"] not in ("failed", "unavailable"):
        raise SafetyError("MARKETPLACE_RECEIPT_INVALID")
    return receipt


def reconcile_marketplace(folder):
    """Optionally reparse a fully saved original child, without recollecting.

    The first failed receipt and any earlier failed capture remain unchanged.
    This is a separately bound parsing attribution with the original date and
    request; ordinary historical replay still returns the first receipt.
    """
    from .storage import Journal
    folder = safe_path(folder)
    with Journal(folder / "deadline_reparse_lock").locked():
        original = replay_marketplace(folder)
        actual = saved_completed_transport(folder, original, snapshot_file="snapshot.json",
            requested_url=original["request"]["url"], snapshot_limit=MAX_JSON)
        if actual is None:
            return original
        snapshot = deepcopy(actual["snapshot"]); snapshot.pop("html", None)
        parsed = parse_marketplace_snapshot(snapshot, original["request"], navigation_status=actual.get("navigation_status"))
        if parsed["state"] != "blocked" and parsed["scope_verified"] is not True:
            return original
        parsed_raw = canonical(parsed)
        recovery = {"version": "source-deadline-reparse-v1",
            "original_receipt_sha256": sha256((folder / "receipt.json").read_bytes()).hexdigest(),
            "original_artifacts_hash": digest(original["artifacts"])}
        receipt = dict(original, outer_deadline=False, recovery=recovery,
            receipt_file="deadline_reparse.json", parsed_file="deadline_parsed.json",
            state=parsed["state"], reason=parsed["reason"], observed_at=actual["completed_at"],
            dispatch_started=True, dispatch_started_at=actual["navigation_started_at"],
            navigation_status=actual.get("navigation_status"), retry_after=safe_source_retry_after(actual.get("retry_after")), owned_process_exited=True,
            scope_verified=parsed["scope_verified"], usable_cards=len(parsed["records"]), parsed_hash=digest(parsed),
            artifacts=dict(original["artifacts"], **{"deadline_parsed.json": sha256(parsed_raw).hexdigest()}))
        for name, raw in (("deadline_parsed.json", parsed_raw), ("deadline_reparse.json", canonical(receipt))):
            path = safe_path(folder / name)
            if path.exists():
                if path.read_bytes() != raw:
                    raise SafetyError("MARKETPLACE_DEADLINE_REPARSE_CONFLICT")
            else:
                _write_new(path, raw)
        return receipt


def capture_marketplace(request, acquisition_id, *, data_root=DATA_ROOT, transport="auto", timeout=45,
                        browser_path=None, headed=False, snapshot_reader=None):
    """One bounded public page, durable receipt, no paid/provider fallback.

    An injected reader is explicitly marked synthetic and cannot enter captured
    research. Automatic fallback applies only to a pre-navigation startup
    failure, not to solving a provider challenge or repeatedly retrying it.
    """
    validate_request(request); identifier(acquisition_id)
    if transport not in ("auto", "native", "playwright") or type(timeout) is not int or not 15 <= timeout <= 45 or type(headed) is not bool:
        raise SafetyError("MARKETPLACE_CAPTURE_OPTIONS_INVALID")
    folder = safe_path(data_root, acquisition_id)
    intent = {"version": VERSION, "request": deepcopy(request), "acquisition_id": acquisition_id, "timeout": timeout, "transport": transport,
              "evidence_type": "synthetic" if snapshot_reader else "captured"}
    if folder.exists():
        saved = replay_marketplace(folder)
        if saved["intent_hash"] != digest(intent): raise SafetyError("MARKETPLACE_ACQUISITION_CONFLICT")
        return saved
    folder.mkdir(parents=True, exist_ok=False)
    _write_new(folder / "intent.json", canonical(intent))
    receipt = {"version": VERSION, "route": request["route"], "request": deepcopy(request), "acquisition_id": acquisition_id,
               "intent_hash": digest(intent), "folder": str(folder), "started_at": utc_now(), "completed_at": None,
               "observed_at": None, "state": "failed", "reason": "MARKETPLACE_CAPTURE_INCOMPLETE", "artifacts": {},
               "apify_requests": 0, "model_calls": 0, "apify_charge_usd": "0", "evidence_type": intent["evidence_type"],
               "dispatch_started": False, "scope_verified": False, "usable_cards": 0, "transport_attempts": []}
    started = time.monotonic()
    deadline_error = None
    actual = None
    def save(name, raw):
        limit = MAX_HTML if name == "rendered.html" else MAX_JSON
        if len(raw) > limit: raise SafetyError("MARKETPLACE_CAPTURE_SIZE_LIMIT")
        _write_new(folder / name, raw); receipt["artifacts"][name] = sha256(raw).hexdigest()
    try:
        if snapshot_reader is not None:
            with source_phase("transport"):
                actual = snapshot_reader(request)
            receipt["transport_attempts"].append("synthetic_fixture")
        else:
            actual = None
            if transport in ("auto", "native"):
                from .native_public_browser import native_snapshot
                receipt["transport_attempts"].append("owned_native_windows_chrome")
                with source_phase("transport"):
                    actual = native_snapshot(request["url"], "JSON.stringify((" + SNAPSHOT_JS + ")())", folder,
                                             timeout=timeout, headed=headed)
            remaining = timeout - (time.monotonic() - started)
            if transport == "playwright" or (transport == "auto" and remaining >= 15 and actual.get("navigation_started") is False and actual.get("state") in ("failed", "unavailable")):
                receipt["transport_attempts"].append("owned_playwright_browser")
                with source_phase("transport"):
                    actual = asyncio.run(_playwright_snapshot(request, min(timeout, int(remaining)), browser_path=browser_path))
        receipt.update(dispatch_started=actual.get("navigation_started") is True,
                       dispatch_started_at=actual.get("navigation_started_at"), navigation_status=actual.get("navigation_status"),
                       retry_after=safe_source_retry_after(actual.get("retry_after")),
                       execution_stage=actual.get("stage"), owned_process_exited=actual.get("owned_process_exited"),
                       state=actual.get("state", "failed"), reason=actual.get("reason", "MARKETPLACE_TRANSPORT_FAILED"))
        snapshot = deepcopy(actual.get("snapshot"))
        transport_record = {field: deepcopy(value) for field, value in actual.items() if field != "snapshot"}
        save("transport.json", canonical(transport_record))
        if snapshot is not None:
            html = snapshot.pop("html", None)
            if isinstance(html, str): save("rendered.html", html.encode("utf-8"))
            save("snapshot.json", canonical(snapshot))
            with source_phase("parse"):
                parsed = parse_marketplace_snapshot(snapshot, request, navigation_status=receipt.get("navigation_status"))
            save("parsed.json", canonical(parsed))
            receipt.update(observed_at=actual.get("completed_at") or utc_now(), parsed_hash=digest(parsed), usable_cards=len(parsed["records"]),
                           scope_verified=parsed["scope_verified"])
            if actual.get("state") == "captured": receipt.update(state=parsed["state"], reason=parsed["reason"])
        elif receipt["state"] not in ("failed", "unavailable"):
            receipt.update(state="failed", reason="MARKETPLACE_TRANSPORT_SNAPSHOT_MISSING")
        for name, limit in (("native_result.json", 12 * 1024 * 1024), ("owned_process.json", 16384)):
            path = folder / name
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= limit:
                receipt["artifacts"][name] = sha256(path.read_bytes()).hexdigest()
    except SourceActionDeadline as error:
        deadline_error = error
        proof = interrupted_transport(error, folder=folder, requested_url=request["url"], completed_transport=actual)
        receipt.update(outer_deadline=True, state="failed", reason="SOURCE_ACTION_TIMEOUT",
                       dispatch_started=proof["navigation_started"], dispatch_started_at=proof.get("navigation_started_at"),
                       owned_process_exited=proof.get("owned_process_exited"))
        try:
            save("outer_deadline.json", canonical(proof))
            for name in ("native_dispatch.json", "native_outer_deadline.json", "native_result.json"):
                path = folder / name
                limit = 12 * 1024 * 1024 if name == "native_result.json" else 16384
                if path.is_file() and not path.is_symlink() and path.stat().st_size <= limit:
                    receipt["artifacts"][name] = sha256(path.read_bytes()).hexdigest()
        except (SafetyError, OSError):
            # A failed local projection remains cancellation with unresolved
            # original proof, never the default "no navigation" or a retry.
            receipt["deadline_proof_save_error"] = "SOURCE_DEADLINE_PROOF_SAVE_FAILED"
        raise
    except Exception as error:
        receipt.update(state="failed", reason=str(error) if isinstance(error, SafetyError) else "MARKETPLACE_CAPTURE_FAILED",
                       exception_type=type(error).__name__)
    finally:
        receipt.update(completed_at=utc_now(), elapsed_seconds=round(time.monotonic() - started, 3))
        if receipt["observed_at"] is None: receipt["observed_at"] = receipt["completed_at"]
        try:
            _write_new(folder / "receipt.json", canonical(receipt))
        except (SafetyError, OSError):
            if deadline_error is None:
                raise
            raise deadline_error
    return receipt
