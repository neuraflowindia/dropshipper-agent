"""Location-specific Blinkit, Zepto and Instamart reads using funded credits.

The selected provider documents /v1/search at quickcommerceapi.com/docs.
These are catalogue/price/availability observations, not sales or demand proof.
No order, supplier contact, billing purchase, paid upgrade or Apify call exists.
"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .models import SafetyError, canonical, decode_json, digest, identifier, timestamp, utc_now
from .public_pages import _public_url
from .research_memory import change, key
from .storage import atomic_json, load_json, safe_path

VERSION = "quickcommerce-location-v1"
ENDPOINT = "https://api.quickcommerceapi.com/v1/search"
PLATFORMS = {"blinkit": "BlinkIt", "zepto": "Zepto", "instamart": "Swiggy"}
HOSTS = {"blinkit": {"blinkit.com", "www.blinkit.com"},
         "zepto": {"zepto.com", "www.zepto.com", "zeptonow.com", "www.zeptonow.com"},
         "instamart": {"swiggy.com", "www.swiggy.com"}}
MAX_BYTES = 3 * 1024 * 1024


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def prepare_query(platform, query, location):
    if (platform not in PLATFORMS or not isinstance(query, str) or not query.strip()
            or len(query) > 180 or any(ord(char) < 32 for char in query)
            or not isinstance(location, dict) or set(location) != {"name", "lat", "lon"}
            or not isinstance(location["name"], str) or not location["name"].strip()):
        raise SafetyError("QUICKCOMMERCE_QUERY_INVALID")
    coordinates = {}
    for field, lower, upper in (("lat", -90, 90), ("lon", -180, 180)):
        value = location[field]
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise SafetyError("QUICKCOMMERCE_LOCATION_INVALID")
        try:
            number = Decimal(str(value))
            if not number.is_finite() or not lower <= number <= upper:
                raise ValueError
        except (ValueError, InvalidOperation):
            raise SafetyError("QUICKCOMMERCE_LOCATION_INVALID") from None
        coordinates[field] = format(number, "f")
    return {"version": VERSION, "platform": platform, "provider_platform": PLATFORMS[platform],
            "query": " ".join(query.split()), "country": "IN",
            "location": {"name": location["name"].strip(), **coordinates}, "limit": 50}


def _number(value):
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
        return format(result, "f") if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def _coordinate(value):
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _validate_request(request):
    if (not isinstance(request, dict)
            or prepare_query(request.get("platform"), request.get("query"), request.get("location")) != request):
        raise SafetyError("QUICKCOMMERCE_QUERY_INVALID")
    return request


def map_response(payload, request):
    """Validate successful response scope before reading any product values."""
    _validate_request(request)
    if (not isinstance(payload, dict) or payload.get("status") != "success"
            or not isinstance(payload.get("data"), dict)):
        raise SafetyError("QUICKCOMMERCE_RESPONSE_INVALID")
    data = payload["data"]
    if data.get("query") != request["query"] or data.get("platform") != request["provider_platform"]:
        raise SafetyError("QUICKCOMMERCE_SCOPE_CHANGED")
    if any(_coordinate(data.get(field)) != _coordinate(request["location"][field]) for field in ("lat", "lon")):
        raise SafetyError("QUICKCOMMERCE_LOCATION_CHANGED")
    rows, total = data.get("products"), data.get("total_results")
    if (not isinstance(rows, list) or len(rows) > 500 or type(total) is not int or total < len(rows)):
        raise SafetyError("QUICKCOMMERCE_RESULTS_INVALID")
    products, rejected, seen = [], [], set()
    for index, row in enumerate(rows[:request["limit"]]):
        if (not isinstance(row, dict) or not isinstance(row.get("id"), (str, int)) or isinstance(row["id"], bool)
                or not str(row["id"]).strip() or len(str(row["id"])) > 200
                or not isinstance(row.get("name"), str) or not row["name"].strip() or len(row["name"]) > 1000):
            rejected.append({"row": index, "reason": "QUICKCOMMERCE_PRODUCT_INVALID"}); continue
        item_id = str(row["id"])
        if item_id in seen:
            rejected.append({"row": index, "reason": "QUICKCOMMERCE_DUPLICATE_ITEM"}); continue
        seen.add(item_id)
        url = None
        if row.get("deeplink"):
            try:
                clean, host, _, _ = _public_url(row["deeplink"])
                if host not in HOSTS[request["platform"]]:
                    raise SafetyError("QUICKCOMMERCE_PRODUCT_HOST_CHANGED")
                url = clean
            except SafetyError:
                rejected.append({"row": index, "reason": "QUICKCOMMERCE_PRODUCT_URL_INVALID"})
        images = []
        for candidate in row.get("images", [])[:4] if isinstance(row.get("images"), list) else []:
            try:
                images.append(_public_url(candidate)[0])
            except SafetyError:
                continue
        products.append({"id": item_id, "title": row["name"].strip(), "url": url,
            "available": row.get("available") if type(row.get("available")) is bool else None,
            "quantity": row.get("quantity") if isinstance(row.get("quantity"), str) else None,
            "brand": row.get("brand") if isinstance(row.get("brand"), str) else None,
            "price": {"currency": "INR", "displayed": _number(row.get("offer_price")),
                      "mrp": _number(row.get("mrp"))}, "image_urls": list(dict.fromkeys(images)),
            "rating": _number(row.get("rating")),
            "rating_count": row.get("rating_count") if type(row.get("rating_count")) is int and row["rating_count"] >= 0 else None,
            "returned_position": index + 1, "raw_row": index})
    omitted = max(0, len(rows) - request["limit"])
    state = ("captured" if products else "completed_no_results" if total == 0 and not rows
             else "failed")
    return {"state": state, "reason": None if products or state == "completed_no_results" else "QUICKCOMMERCE_NO_PARSEABLE_PRODUCTS",
            "products": products, "rejected": rejected, "omitted": omitted, "total_results": total,
            "partial": bool(rejected or omitted or total > len(rows)),
            "credits_remaining": payload.get("credits_remaining") if type(payload.get("credits_remaining")) is int and payload["credits_remaining"] >= 0 else None,
            "provider_request_id": payload.get("request_id") if isinstance(payload.get("request_id"), str) else None}


class QuickCommerceClient:
    def __init__(self, api_key, *, opener=None):
        if not isinstance(api_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", api_key):
            raise SafetyError("QUICKCOMMERCE_CREDENTIAL_INVALID")
        self._key = api_key
        self._opener = opener or build_opener(_NoRedirects())

    def search(self, request, *, on_dispatch=None, retain_transport=False):
        # Validate even a caller-provided saved preparation before access.
        _validate_request(request)
        if type(retain_transport) is not bool: raise SafetyError("QUICKCOMMERCE_TRANSPORT_OPTION_INVALID")
        closed = False; retry_after = None
        params = {"platform": request["provider_platform"], "q": request["query"],
                  "lat": request["location"]["lat"], "lon": request["location"]["lon"]}
        url = ENDPOINT + "?" + urlencode(params)
        prepared = Request(url, headers={"X-API-Key": self._key, "Accept": "application/json",
                                       "User-Agent": "NeuraFlow-Public-Research/1.0"}, method="GET")
        receipt = {"version": VERSION, "request": deepcopy(request), "observed_at": utc_now(),
                   "requested_url": url, "dispatch_started": False, "http_status": None,
                   "execution_ref": "quickcommerce-" + uuid4().hex, "state": "failed", "reason": None,
                   "products": [], "provider_credits_expected": 1, "apify_usd": "0", "purchase_made": False}
        try:
            if on_dispatch:
                on_dispatch()
            receipt["dispatch_started"] = True
            with self._opener.open(prepared, timeout=25) as response:
                receipt["http_status"] = response.status
                headers = getattr(response, "headers", {})
                retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
                if response.status != 200:
                    raise SafetyError("QUICKCOMMERCE_HTTP_UNEXPECTED")
                raw = response.read(MAX_BYTES + 1)
            closed = True
            if len(raw) > MAX_BYTES:
                raise SafetyError("QUICKCOMMERCE_RESPONSE_TOO_LARGE")
            payload = decode_json(raw)
            mapped = map_response(payload, request)
            # Keep only public successful payload fields. Error bodies and
            # request headers are never saved or passed to model jobs.
            receipt.update(mapped, raw_response={"status": payload["status"], "data": payload["data"]},
                           body_complete=True, raw_sha256=sha256(raw).hexdigest())
            receipt["public_response_hash"] = digest(receipt["raw_response"])
        except HTTPError as error:
            retry_after = error.headers.get("Retry-After") if error.headers is not None else None
            receipt["http_status"] = error.code
            receipt["reason"] = {401: "QUICKCOMMERCE_AUTH_REJECTED", 402: "QUICKCOMMERCE_CREDITS_EXHAUSTED",
                                 404: "QUICKCOMMERCE_LOCATION_OR_PLATFORM_NOT_FOUND", 429: "QUICKCOMMERCE_RATE_LIMITED"}.get(error.code, "QUICKCOMMERCE_PROVIDER_HTTP_FAILED")
            receipt["state"] = "blocked" if error.code in (401, 402, 403, 429) else "failed"
            error.close()
            closed = True
        except SafetyError as error:
            receipt["reason"] = str(error)
        except (URLError, TimeoutError, OSError):
            receipt["reason"] = "QUICKCOMMERCE_NETWORK_FAILED"
        except (ValueError, UnicodeError):
            receipt["reason"] = "QUICKCOMMERCE_RESPONSE_INVALID"
        receipt["observed_at"] = utc_now()
        if retain_transport:
            receipt.update(transport_contract="quickcommerce-owned-http-v1", owned_request_closed=closed)
            if (isinstance(retry_after, str) and len(retry_after) <= 256
                    and not any(ord(c) < 32 or ord(c) == 127 for c in retry_after)):
                receipt["retry_after"] = retry_after
        return receipt


def validate_capture_receipt(receipt):
    """Pure validation of the actual original provider reply and its mapping."""
    if (not isinstance(receipt, dict) or receipt.get("version") != VERSION
            or type(receipt.get("dispatch_started")) is not bool
            or receipt.get("purchase_made") is not False
            or receipt.get("apify_usd") != "0"
            or receipt.get("state") not in {"captured", "completed_no_results", "failed", "blocked"}
            or not isinstance(receipt.get("products"), list)):
        raise SafetyError("QUICKCOMMERCE_CAPTURE_INVALID")
    timestamp(receipt.get("observed_at"))
    request = _validate_request(receipt.get("request"))
    raw_response = receipt.get("raw_response")
    if raw_response is not None:
        mapped = map_response(raw_response, request)
        comparable = ("state", "reason", "products", "rejected", "omitted", "total_results", "partial")
        if (receipt.get("public_response_hash") != digest(raw_response)
                or any(receipt.get(field) != mapped[field] for field in comparable)
                or receipt.get("body_complete") is not True or not receipt["dispatch_started"]
                or receipt.get("http_status") != 200):
            raise SafetyError("QUICKCOMMERCE_CAPTURE_MAPPING_CHANGED")
    elif receipt["products"] or receipt["state"] in {"captured", "completed_no_results"}:
        raise SafetyError("QUICKCOMMERCE_CAPTURE_MAPPING_REQUIRED")
    if len(canonical(receipt)) > MAX_BYTES * 2:
        raise SafetyError("QUICKCOMMERCE_CAPTURE_TOO_LARGE")
    return request


def save_capture(memory, acquisition_id, receipt):
    """Immutable public capture/import; no paid ledger or new-product inference."""
    identifier(acquisition_id)
    request = validate_capture_receipt(receipt)
    folder = safe_path(memory.root, "quickcommerce_captures", acquisition_id)
    destination = folder / "capture.json"
    if destination.exists():
        if load_json(destination, MAX_BYTES * 2) != receipt:
            raise SafetyError("QUICKCOMMERCE_CAPTURE_CONFLICT")
    else:
        atomic_json(destination, receipt)
    capture_id = key("capture", [request["platform"], acquisition_id])
    state = memory.snapshot()
    old = state["captures"].get(capture_id)
    if old:
        if old["capture_hash"] != digest(receipt):
            raise SafetyError("QUICKCOMMERCE_CAPTURE_CONFLICT")
        return {"capture_id": capture_id, "observation_ids": old["observation_ids"], "reused": True,
                "reference": str(destination)}
    products = receipt.get("products", [])
    observation_ids = [key("obs", [request["platform"], acquisition_id, row["id"]]) for row in products]
    capture = {"source": request["platform"], "acquisition_id": acquisition_id, "observed_at": receipt["observed_at"],
               "capture_hash": digest(receipt), "mapping_hash": digest(products), "reference": str(destination),
               "imported_at": utc_now(), "mapping_status": receipt["state"], "rejected": receipt.get("rejected", []),
               "mapper_version": VERSION, "observation_ids": observation_ids, "request": deepcopy(request)}
    changes = [change("captures", capture_id, capture)]
    for observation_id, row in zip(observation_ids, products):
        semantic = {"title": row["title"], "product_url": row["url"], "quantity": row["quantity"], "brand": row["brand"]}
        facts = {"url": row["url"], "price": deepcopy(row["price"]), "quantity": row["quantity"],
                 "available": row["available"], "image_urls": row["image_urls"], "rating": row["rating"],
                 "rating_count": row["rating_count"], "location": deepcopy(request["location"]), "query": request["query"],
                 "list_type": "quickcommerce_context", "returned_position": row["returned_position"], "limitations": [], "raw_row": row["raw_row"]}
        changes.append(change("observations", observation_id, {"source": request["platform"], "source_item": row["id"],
            "acquisition_id": acquisition_id, "capture_id": capture_id, "observed_at": receipt["observed_at"],
            "semantic_id": key("item", semantic), "semantic": semantic, "facts": facts}))
    memory.commit(capture_id, changes)
    return {"capture_id": capture_id, "observation_ids": observation_ids, "reused": False, "reference": str(destination)}


def adapter(memory, client):
    """Connect all-source tasks to the three concrete platform transports."""
    def collect(context):
        route, request = context["route"], context["request"]
        prepared = prepare_query(route, request["query"], request["location"])
        acquisition_id = context["acquisition_id"]
        saved = safe_path(memory.root, "quickcommerce_captures", acquisition_id) / "capture.json"
        receipt = load_json(saved, MAX_BYTES * 2) if saved.exists() else client.search(prepared, on_dispatch=context.get("record_dispatch"))
        if receipt["request"] != prepared:
            raise SafetyError("QUICKCOMMERCE_CAPTURE_CONFLICT")
        imported = save_capture(memory, acquisition_id, receipt)
        target_state = ("interpretation_pending" if receipt["products"] else
                        receipt["state"] if receipt["dispatch_started"] else "not_attempted")
        state = ("partial" if receipt.get("partial") else "usable") if receipt["products"] else receipt["state"]
        reason = receipt["reason"] or ("Local catalogue results were saved; product-family matching is pending."
                                      if receipt["products"] else "The platform returned no listings for this query and location.")
        return {"schema_version": 1, "route": route, "acquisition_id": acquisition_id,
                "observed_at": receipt["observed_at"], "state": state, "dispatch_started": receipt["dispatch_started"],
                "reference": imported["reference"], "capture_ids": [imported["capture_id"]],
                "observation_ids": imported["observation_ids"],
                "target_results": [{"concept_id": target, "description_hash": context["description_hashes"][target],
                    "state": target_state, "evidence_refs": imported["observation_ids"],
                    "reason": "Saved local catalogue results require product-family relevance review." if receipt["products"] else reason}
                    for target in context["concept_ids"]], "reason": reason,
                "retryable": receipt.get("http_status") in (429, 500, 502, 503, 504)
                             or receipt.get("reason") in {"QUICKCOMMERCE_NETWORK_FAILED", "RUNNER_ALREADY_ACTIVE"},
                **({"failure_scope": "route"} if receipt.get("http_status") in (401, 402, 403, 429) else {})}
    return collect
