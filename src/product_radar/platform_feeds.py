"""DeoDap and BaapStore through their official public Shopify feeds (D1).

``collections.json``, each list collection's ``products.json`` and the store's
``products.json`` are read with a plain GET: no cookies, sign-in, cart,
search or form. Reads are paced like every research site (one request every
few seconds, a daily ceiling; ``config/research/signed_in_reads.json``) and
saved whole before they are parsed (``dropship_platforms.save_raw``). The
connection is pinned to a checked public address, as for supplier pages.
"""

from decimal import Decimal
import http.client
import json
import re
import socket
import time
import zlib
from hashlib import sha256
from urllib.parse import urlsplit

from .dropship_platforms import FEED_ROUTES, amount, body_json, platform_settings
from .models import SafetyError, utc_now
from .public_pages import _checked_addresses, _Control, _PinnedConnection, _public_url
from .research_memory import key

PARSER_VERSION = "shopify-feed-v1"
MAX_FEED = 24 * 1024 * 1024
TIMEOUT_SECONDS = 30
_REFUSED = {401, 403, 429}


def _read_body(response, sock, control):
    encoding = (response.getheader("Content-Encoding") or "identity").casefold().strip()
    if encoding not in ("identity", "gzip", "x-gzip", "deflate"):
        raise SafetyError("DROPSHIP_FEED_ENCODING_UNSUPPORTED")
    decoder = zlib.decompressobj(31 if encoding in ("gzip", "x-gzip") else zlib.MAX_WBITS) if encoding != "identity" else None
    chunks, size = [], 0
    while not response.isclosed():
        sock.settimeout(control.remaining())
        block = response.read1(65536)
        if not block:
            break
        piece = decoder.decompress(block, MAX_FEED + 1 - size) if decoder else block
        size += len(piece)
        if size > MAX_FEED or decoder and decoder.unconsumed_tail:
            raise SafetyError("DROPSHIP_FEED_TOO_LARGE")
        chunks.append(piece)
    if decoder:
        chunks.append(decoder.flush())
    return b"".join(chunks)


def fetch_feed(url, *, timeout=TIMEOUT_SECONDS, resolver=None, connection_factory=None):
    """One GET of a public JSON feed: ``(receipt, body_text_or_None)``.

    The receipt follows the supplier readers' schema (execution stage, start
    time and ``public-fetch-...`` reference), so a relevant listing can later
    prove supplier effort. Redirects are not followed.
    """
    clean, host, port, scheme = _public_url(url)
    control = _Control(time.monotonic() + timeout)
    receipt = {"schema_version": 2, "id": key("dropship-feed-read", [clean, utc_now("microseconds")]),
               "requested_url": clean, "final_url": None, "checked_at": None, "http_status": None,
               "execution_stage": "not_dispatched", "attempt_started_at": None, "execution_ref": None,
               "state": "failed", "reason": "DROPSHIP_FEED_UNAVAILABLE", "body_sha256": None}
    body = None
    try:
        receipt.update(control.start_attempt())
        address = _checked_addresses(host, port, resolver or socket.getaddrinfo)[0]
        connection = (connection_factory or _PinnedConnection)(host, port, scheme, address, control)
        try:
            parsed = urlsplit(clean)
            connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers={
                "User-Agent": "NeuraFlow-Research/1.0", "Accept": "application/json",
                "Accept-Encoding": "identity,gzip", "Connection": "close"})
            # http.client hands the socket to the response after "Connection:
            # close" (connection.sock becomes None), so keep it for the timeouts.
            active_socket = connection.sock
            response = connection.getresponse()
            receipt.update(control.response_received(), http_status=response.status, final_url=clean)
            content = (response.getheader("Content-Type") or "").split(";", 1)[0].casefold().strip()
            if response.status in _REFUSED:
                receipt["reason"] = "DROPSHIP_FEED_REFUSED"
            elif response.status != 200 or content != "application/json":
                receipt["reason"] = "DROPSHIP_FEED_UNAVAILABLE"
            else:
                raw = _read_body(response, active_socket, control)
                body = raw.decode("utf-8")
                json.loads(body)
                receipt.update(state="read", reason=None, body_sha256=sha256(raw).hexdigest())
            response.close()
        finally:
            connection.close()
    except SafetyError as error:
        receipt["reason"] = str(error) if str(error).startswith("DROPSHIP_") else "DROPSHIP_FEED_UNAVAILABLE"
        body = None
    except (OSError, http.client.HTTPException, zlib.error, ValueError, UnicodeError):
        receipt["reason"], body = "DROPSHIP_FEED_UNAVAILABLE", None
    receipt.update(control.execution_snapshot(), checked_at=utc_now())
    if receipt["state"] != "read":
        receipt["body_sha256"], body = None, None
    return receipt, body


def _tags(value):
    if isinstance(value, str):
        value = value.split(",")
    return [tag.strip() for tag in value if isinstance(tag, str) and tag.strip()] if isinstance(value, list) else []


def parse_products(rows, base_url):
    """Shopify ``products`` rows as platform items (unusable rows are skipped)."""
    items = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or type(row.get("id")) is not int or not isinstance(row.get("handle"), str):
            continue
        if not re.fullmatch(r"[A-Za-z0-9._~%-]{1,300}", row["handle"]):
            continue
        variants = [value for value in row.get("variants") or [] if isinstance(value, dict)]
        # A zero ("0.00") variant price is a placeholder, not a price.
        prices = [amount(value.get("price")) for value in variants if amount(value.get("price"))]
        compare = [amount(value.get("compare_at_price")) for value in variants if amount(value.get("compare_at_price"))]
        available = [value["available"] for value in variants if type(value.get("available")) is bool]
        items.append({"item_id": str(row["id"]), "title": row.get("title"), "url": base_url + "/products/" + row["handle"],
                      "images": [image.get("src") for image in row.get("images") or [] if isinstance(image, dict)],
                      "price": min(prices, key=Decimal) if prices else None,
                      "mrp": max(compare, key=Decimal) if compare else None,
                      "in_stock": any(available) if available else None, "vendor": row.get("vendor"),
                      "product_type": row.get("product_type"), "tags": _tags(row.get("tags")),
                      "created_at": row.get("created_at"), "terms": None})
    return items


def collection_roles(handle, config):
    return sorted({rule["role"] for rule in config["lists"]
                   if handle in rule["handles"] or rule["pattern"] and re.fullmatch(rule["pattern"], handle)})


def parse_collections(rows, base_url, config):
    """Shopify ``collections`` rows as product-map categories with their list roles."""
    result = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("handle"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", row["handle"]):
            continue
        count = row.get("products_count")
        result.append({"handle": row["handle"], "title": " ".join(str(row.get("title") or row["handle"]).split())[:200],
                       "url": base_url + "/collections/" + row["handle"],
                       "products_count": count if type(count) is int and count >= 0 else None,
                       "roles": collection_roles(row["handle"], config), "parent": None})
    return result


class FeedReader:
    """The Shopify transport for ``dropship_platforms.advance_platform``."""

    parser_version = PARSER_VERSION
    complete_catalogue = True  # products.json lists the whole store: an item no longer listed is delisted
    authoritative = True  # every feed field is stated, so an empty value is a real "none"

    def __init__(self, route, *, pacing_root, fetcher=fetch_feed):
        if route not in FEED_ROUTES:
            raise SafetyError("DROPSHIP_ROUTE_INVALID")
        self.route, self.pacing_root, self.fetcher = route, pacing_root, fetcher
        self.config = platform_settings(route)
        self.base = self.config["base_url"]

    def _url(self, path, page):
        return self.base + path + "?limit=" + str(self.config["page_size"]) + "&page=" + str(page)

    def plan_full(self, config, catalogue):
        return [{"kind": "collections", "page": 1, "url": self._url("/collections.json", 1)}]

    def plan_lists(self, config, catalogue):
        known = {row["handle"] for row in catalogue["collections"]}
        queue, seen = [], set()
        for rule in config["lists"]:
            handles = [handle for handle in rule["handles"] if handle in known] + sorted(
                handle for handle in known if rule["pattern"] and re.fullmatch(rule["pattern"], handle))
            for handle in handles:
                if (handle, rule["role"]) in seen:
                    continue
                seen.add((handle, rule["role"]))
                queue.append({"kind": "collection", "handle": handle, "role": rule["role"], "page": 1,
                              "max_pages": rule["max_pages"], "url": self._url("/collections/" + handle + "/products.json", 1)})
        return queue

    def hold(self, at):
        from .signed_in_pacing import signed_in_pace
        return signed_in_pace(self.pacing_root, self.route, as_of=at)

    def read(self, request, folder, sequence, at):
        from .signed_in_attempts import SignedInWait, count_signed_in_read, release_unsent_read
        try:
            counted = count_signed_in_read(self.pacing_root, self.route)  # pacing or a busy record: a calm wait
        except SignedInWait as error:
            return {"state": "not_started", "reason": str(error), "next_retry_at": error.next_retry_at}
        receipt, body = self.fetcher(request["url"])
        if receipt["execution_stage"] == "not_dispatched":
            release_unsent_read(self.pacing_root, self.route, counted)
            return {"state": "not_started", "reason": receipt["reason"], "next_retry_at": None}
        receipt = dict(receipt, route=self.route, request=request, read_at=at)
        return {"state": receipt["state"], "reason": receipt["reason"], "receipt": receipt, "body": body}

    def parse(self, request, raw):
        payload = body_json(raw)
        if not isinstance(payload, dict):
            raise SafetyError("DROPSHIP_FEED_BODY_INVALID")
        size, page = self.config["page_size"], request["page"]
        if request["kind"] == "collections":
            rows = payload.get("collections")
            if not isinstance(rows, list):
                raise SafetyError("DROPSHIP_FEED_BODY_INVALID")
            more = len(rows) >= size
            follow = ([{"kind": "collections", "page": page + 1, "url": self._url("/collections.json", page + 1)}] if more
                      else [{"kind": "products", "page": 1, "url": self._url("/products.json", 1)}])
            return {"items": [], "collections": parse_collections(rows, self.base, self.config), "follow": follow,
                    "collections_complete": not more}
        rows = payload.get("products")
        if not isinstance(rows, list) or request["kind"] not in ("products", "collection"):
            raise SafetyError("DROPSHIP_FEED_BODY_INVALID")
        follow = []
        if len(rows) >= size and (request["kind"] == "products" or page < request["max_pages"]):
            path = "/products.json" if request["kind"] == "products" else "/collections/" + request["handle"] + "/products.json"
            follow = [dict(request, page=page + 1, url=self._url(path, page + 1))]
        return {"items": parse_products(rows, self.base), "collections": None, "follow": follow}
