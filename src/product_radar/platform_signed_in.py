"""Dropdash, read in the owner's signed-in research profile (D1, D2).

Reading only. The page snapshot below copies the rendered page's title, text,
product cards (with their own title, badge and image elements), the product
block of a product page and category/next-page links; nothing is clicked,
typed, submitted or scrolled. Only configured pages, their numbered next
pages and product pages are ever opened (``read_only_url``, an allow-list);
order, cart, checkout, plan, membership, upgrade, trial, payment, contact,
import and account steps never are, and a read that lands on one (a redirect)
or shows a paid-plan or membership wall stops with ``DROPSHIP_PLAN_REQUIRED``
and is looked at again a day later. Nothing is ever paid or accepted.

A read starts only when the owner has signed in, the sign-in window is closed,
the desktop is available and pacing allows it (one read every 30 seconds, 300
a day; ``config/research/signed_in_reads.json``); otherwise the platform waits
calmly with that reason. A sign-in wall seen in the profile is recorded as an
expired sign-in (``OWNER_SIGN_IN_REQUIRED``).

The research account's identity (e-mail addresses, account query values,
Google's account label) is removed from the result and from the browser's
saved file before anything here reads them, also when a read is interrupted;
a saved file that cannot be cleaned is quarantined unread
(``signed_in_saved``). Only the product cards, product block and page text are
kept with the platform's saved reads.

The page layouts are not yet seen signed in: a catalogue or list page with no
recognisable product card stops that refresh cycle with
``DROPSHIP_PAGE_LAYOUT_UNRECOGNISED`` rather than recording an empty platform.
The catalogue read this way is a sample (a few pages deep), never the whole
platform.
"""

from datetime import timedelta
import json
import re
from urllib.parse import parse_qsl, urlsplit

from .dropship_platforms import LIST_ROLES, SIGNED_IN_ROUTES, body_json, platform_settings
from .native_public_browser import signed_in_budget
from .models import SafetyError, digest, timestamp, utc_now
from .research_memory import key
from .storage import safe_path

PARSER_VERSION = "signed-in-platform-v2"
PROFILE = "signed-in"
PLAN_WAIT_SECONDS = 24 * 3600
# Never opened, even inside an allowed address: any step that orders, pays,
# subscribes, upgrades, starts a trial, contacts or changes the account.
_NOT_READ_ONLY = frozenset("""order orders place-order cart add-to-cart buy buy-now checkout subscribe subscription
subscriptions plan plans pricing upgrade premium membership member trial free-trial payment payments pay billing
wallet recharge contact contact-us enquiry inquiry import add-to-store add-product push sync logout log-out login
signin sign-in signup sign-up register account my-account settings profile""".split())
_PLAN_WALL = re.compile(r"\b(?:upgrade (?:your |to )?(?:plan|premium|membership|account)|subscribe (?:now |to )?(?:to )?"
                        r"(?:continue|access|view|unlock)|(?:buy|choose|select|purchase|activate) (?:a |your |the )?"
                        r"(?:plan|membership|subscription|package)|(?:membership|subscription|plan) (?:is )?(?:required|expired)|"
                        r"your (?:free )?(?:trial|plan|subscription|membership) (?:has )?(?:ended|expired)|"
                        r"unlock (?:all|this|these|premium|winning|trending))\b", re.I)
_PRICE = re.compile(r"(MRP|M\.R\.P\.?|Dropship(?:ping)? price|Price|Selling price)?\s*[:\-]?\s*(?:₹|Rs\.?|INR)\s*"
                    r"([0-9][0-9,]{0,9}(?:\.[0-9]{1,2})?)(\s*(?:/|per)\s*(?:month|mo|year|yr|week|day)\b|\s*(?:monthly|yearly|annually)\b)?", re.I)
_BADGES = (("trending", re.compile(r"\btrending\b", re.I)), ("high_demand", re.compile(r"\bhigh[ -]demand\b", re.I)),
           ("winning", re.compile(r"\bwinning\b", re.I)), ("bestseller", re.compile(r"\bbest[ -]?sell(?:er|ing)\b", re.I)),
           ("new_arrival", re.compile(r"\b(?:new|new arrivals?|newly launched|just launched)\b", re.I)))
_TERMS = re.compile(r"\b(?:dropship|shipping|delivery|dispatch|cod|cash on delivery|returns?|rto|moq|minimum order)\b", re.I)
_WALL = re.compile(r"\b(?:log ?in|sign ?in)\b.{0,80}\b(?:password|otp|continue with google)\b", re.I | re.S)

# One DOM read; PRODUCT is the platform's product-address pattern (JSON text).
# Badges, titles and prices are read from their own elements, never from the
# page text around them.
EXPRESSION = """JSON.stringify((() => {
  const product = new RegExp(PRODUCT);
  const text = document.body?.innerText || '';
  const short = (nodes, limit) => Array.from(nodes).map(n => (n.innerText || '').trim()).filter(t => t && t.length <= limit);
  const seen = new Set(), cards = [];
  for (const a of document.querySelectorAll('a[href]')) {
    if (!product.test(a.href.split('?')[0]) || seen.has(a.href) || cards.length >= 200) continue;
    seen.add(a.href);
    const box = a.closest('article,li,[class*="card"],[class*="Card"],[class*="product"],[class*="Product"]') || a;
    const img = box.querySelector('img[src^="https://"]');
    const heading = box.querySelector('h1,h2,h3,h4,[class*="title"],[class*="Title"],[class*="name"],[class*="Name"]');
    cards.push({href:a.href, title:heading ? (heading.innerText || '').trim().slice(0,300) : null,
      text:(box.innerText || a.innerText || '').slice(0,600), image:img ? img.src : null,
      badges:short(box.querySelectorAll('[class*="badge"],[class*="Badge"],[class*="ribbon"],[class*="Ribbon"],[class*="tag"],[class*="Tag"],[class*="label"],[class*="Label"]'), 40).slice(0,8)});
  }
  const h1 = document.querySelector('h1');
  const block = h1 ? (h1.closest('[itemtype*="Product"],[class*="product-detail"],[class*="productDetail"],[class*="product-info"],[class*="productInfo"],main') || h1.parentElement) : null;
  const productBlock = block ? {title:(h1.innerText || '').trim().slice(0,300), text:(block.innerText || '').slice(0,8000),
    terms:short(block.querySelectorAll('[class*="term"],[class*="Term"],[class*="shipping"],[class*="Shipping"],[class*="delivery"],[class*="Delivery"],[class*="dispatch"],[class*="return"],[class*="Return"],[class*="cod"],[id*="term"],[id*="shipping"]'), 400).slice(0,10)} : null;
  const categories = Array.from(document.querySelectorAll('a[href]'))
    .filter(a => /\\/(categor(y|ies)|collections?)\\//.test(a.href) && (a.innerText || '').trim())
    .slice(0,200).map(a => ({href:a.href, text:a.innerText.trim().slice(0,120)}));
  const next = document.querySelector('a[rel="next"]');
  return {url:location.href, title:document.title.slice(0,300), text:text.slice(0,24000),
    password:!!document.querySelector('input[type="password"]'), cards, product:productBlock, categories, next:next ? next.href : null};
})())"""


def read_only_url(route, url):
    """The address when it is a configured page, one of its numbered next pages
    or a product page of the platform (an allow-list); else a refusal."""
    config = platform_settings(route)
    base = config["base_url"]
    if not isinstance(url, str) or not url.startswith(base + "/") or any(ord(c) < 33 or ord(c) == 127 for c in url) or "#" in url:
        raise SafetyError("DROPSHIP_PAGE_NOT_READ_ONLY")
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    words = {segment.casefold() for segment in parts.path.split("/")} | {name.casefold().replace("_", "-") for name, _ in query}
    if words & _NOT_READ_ONLY:
        raise SafetyError("DROPSHIP_PAGE_NOT_READ_ONLY")
    pages = {page["url"] for page in config["pages"]}
    plain = parts.scheme + "://" + parts.netloc + parts.path
    if (url in pages or plain in pages and len(query) == 1 and query[0][0] == "page" and re.fullmatch(r"[1-9][0-9]{0,2}", query[0][1])
            or not parts.query and re.fullmatch(config["product_path"], url)):
        return url
    raise SafetyError("DROPSHIP_PAGE_NOT_READ_ONLY")


def expression(route):
    return EXPRESSION.replace("PRODUCT", json.dumps(platform_settings(route)["product_path"]))


def signed_in_marker(snapshot):
    """False for a sign-in wall, True for a page showing products, else None."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("text"), str):
        return None
    path = urlsplit(snapshot.get("url") or "").path.casefold()
    if snapshot.get("password") is True or re.search(r"/(?:login|signin|sign-in|auth)\b", path) or _WALL.search(snapshot["text"][:3000]):
        return False
    return True if snapshot.get("cards") or snapshot.get("product") else None


def prices(text):
    """(price, mrp) from one product's own text; a per-month/per-year figure (a plan banner) is never a price."""
    price = mrp = None
    for match in _PRICE.finditer(text or ""):
        if match[3]:
            continue
        if re.match(r"m\.?r\.?p", match[1] or "", re.I):
            mrp = mrp or match[2]
        elif price is None:
            price = match[2]
    return price, mrp


def _stock(text):
    lowered = (text or "").casefold()
    return False if "out of stock" in lowered or "sold out" in lowered else True if "in stock" in lowered else None


def _badges(values):
    return sorted({role for value in values or [] if isinstance(value, str) for role, rx in _BADGES if rx.search(value)})


def _card_item(card, product_path):
    href = card.get("href") if isinstance(card, dict) else None
    url = href.split("?")[0] if isinstance(href, str) else None
    if not url or not re.fullmatch(product_path, url):
        return None
    text = card.get("text") if isinstance(card.get("text"), str) else ""
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    badge_texts = {value.strip().casefold() for value in card.get("badges") or [] if isinstance(value, str)}
    title = card.get("title") if isinstance(card.get("title"), str) and card["title"].strip() else next(
        (line for line in lines if not _PRICE.search(line) and line.casefold() not in badge_texts and len(line) > 3), None)
    price, mrp = prices(text)
    return {"item_id": url.rstrip("/").rsplit("/", 1)[-1][:120], "title": title, "url": url,
            "images": [card["image"]] if isinstance(card.get("image"), str) else [], "price": price, "mrp": mrp,
            "in_stock": _stock(text), "vendor": None, "product_type": None, "tags": [], "created_at": None,
            "terms": None, "badges": _badges(card.get("badges"))}


def plan_wall(request, snapshot):
    """True when the page asks for a paid plan or membership instead of showing products."""
    text = (snapshot.get("text") or "")[:24000]
    if not _PLAN_WALL.search(text):
        return False
    if request["kind"] == "product":
        return prices((snapshot.get("product") or {}).get("text"))[0] is None
    return not snapshot.get("cards")


def parse_snapshot(route, request, snapshot):
    """Saved page snapshot -> items, categories and the next page to read."""
    config = platform_settings(route)
    if request["kind"] == "product":
        block = snapshot.get("product") if isinstance(snapshot.get("product"), dict) else {}
        text = block.get("text") if isinstance(block.get("text"), str) else ""
        price, mrp = prices(text)
        title = block.get("title") if isinstance(block.get("title"), str) and block["title"].strip() else request.get("title")
        terms = [" ".join(line.split()) for value in block.get("terms") or [] if isinstance(value, str)
                 for line in value.splitlines() if _TERMS.search(line) and not _PRICE.search(line)][:6]
        item = {"item_id": request["item_id"], "title": title, "url": request["url"], "images": [], "price": price,
                "mrp": mrp, "in_stock": _stock(text), "vendor": None, "product_type": None, "tags": [], "created_at": None,
                "terms": "; ".join(dict.fromkeys(terms))[:1000] or None, "badges": []}
        return {"items": [item], "collections": None, "follow": []}
    items, seen = [], set()
    for card in snapshot.get("cards") or []:
        row = _card_item(card, config["product_path"])
        if row and row["item_id"] not in seen:  # one product shown twice (another link form) is one item
            seen.add(row["item_id"])
            items.append(row)
    categories = []
    for row in snapshot.get("categories") or []:
        href = row.get("href") if isinstance(row, dict) else None
        if isinstance(href, str) and href.startswith(config["base_url"] + "/"):
            handle = re.sub(r"[^a-z0-9-]+", "-", urlsplit(href).path.strip("/").rsplit("/", 1)[-1].casefold()).strip("-")[:120]
            if handle:
                categories.append({"handle": handle, "title": " ".join(str(row.get("text")).split())[:200], "url": href.split("?")[0],
                                   "products_count": None, "roles": [], "parent": None})
    page = {"handle": request["handle"], "title": request["role"].replace("_", " ").title() + " page", "url": request["url"],
            "products_count": len(items), "roles": [request["role"]] if request["role"] != "catalogue" else [], "parent": None}
    follow, next_url = [], snapshot.get("next")
    if isinstance(next_url, str) and request["page"] < request["max_pages"]:
        try:
            follow = [dict(request, page=request["page"] + 1, url=read_only_url(route, next_url))]
        except SafetyError:
            follow = []
    return {"items": items, "collections": [page] + categories, "follow": follow, "collections_complete": True}


class SignedInReader:
    """The signed-in transport for ``dropship_platforms.advance_platform``."""

    parser_version = PARSER_VERSION
    complete_catalogue = False  # a few catalogue pages: a sample, never proof that an item left the platform
    authoritative = False  # a card or product page shows less than the item: empty means unknown
    worst_case_seconds = signed_in_budget()  # the longest one signed-in page read can take

    def __init__(self, route, *, pacing_root, browser=None):
        if route not in SIGNED_IN_ROUTES:
            raise SafetyError("DROPSHIP_ROUTE_INVALID")
        self.route, self.pacing_root, self.browser = route, pacing_root, browser
        self.config = platform_settings(route)

    def _pages(self, roles):
        return [{"kind": "page", "handle": "page-" + page["role"].replace("_", "-"), "role": page["role"], "page": 1,
                 "max_pages": page["max_pages"], "url": read_only_url(self.route, page["url"])}
                for page in self.config["pages"] if page["role"] in roles]

    def plan_full(self, config, catalogue):
        return self._pages({"catalogue"}) + self._pages(set(LIST_ROLES) | {"new_arrival"})

    def plan_lists(self, config, catalogue):
        """The list pages, then (for listed items) product pages not read within a full refresh."""
        now = timestamp(utc_now())
        due = [item for item in sorted(catalogue["items"].values(), key=lambda row: row["item_id"])
               if item["roles"] and (item["collections"].get("product-page") is None or now - timestamp(
                   item["collections"]["product-page"]) >= timedelta(days=config["full_refresh_days"]))]
        products = [{"kind": "product", "item_id": item["item_id"], "handle": "product-page", "role": None, "page": 1,
                     "max_pages": 1, "title": item["title"], "url": read_only_url(self.route, item["url"])}
                    for item in due][:config["product_pages_per_day"]]
        return self._pages(set(LIST_ROLES) | {"new_arrival"}) + products

    def hold(self, at):
        from .signed_in_attempts import signed_in_hold
        return signed_in_hold(self.pacing_root, self.route, as_of=at)

    def read(self, request, folder, sequence, at):
        from .signed_in_attempts import CALM_CODES, SignedInWait, begin_signed_in_read, release_unsent_read
        from .signed_in_browser import signed_in_read, signed_in_recheck_at
        url = read_only_url(self.route, request["url"])
        try:
            # The owner's sign-in, window and desktop, then pacing: only these (and a busy record) are calm.
            counted = begin_signed_in_read(self.pacing_root, self.route, as_of=at)
        except SignedInWait as error:
            return {"state": "not_started", "reason": str(error), "next_retry_at": error.next_retry_at}
        target = safe_path(folder, "reads", "r" + str(sequence).zfill(6))
        target.mkdir(parents=True, exist_ok=False)
        started = utc_now()

        def cleaned(url, expression, target, **options):
            from .native_public_browser import native_snapshot
            from .signed_in_browser import redact_signed_in
            from .signed_in_saved import protect_saved
            try:
                return redact_signed_in((self.browser or native_snapshot)(url, expression, target, **options))
            finally:
                protect_saved(target / "native_result.json", redact_signed_in)  # also when interrupted

        def launched():
            return any((target / name).exists() for name in ("owned_process.json", "native_dispatch.json", "native_result.json"))

        def not_started(reason):
            release_unsent_read(self.pacing_root, self.route, counted)
            return {"state": "not_started", "reason": reason, "next_retry_at": signed_in_recheck_at(self.pacing_root, reason)}
        try:
            result = signed_in_read(self.route, url, expression(self.route), target, marker=signed_in_marker,
                                    root=self.pacing_root, route=self.route, browser=cleaned)
        except SafetyError as error:
            # Only the signed-in browser's own waits and a busy record are calm;
            # anything else is a failure that must be seen (and backs off).
            if str(error) in CALM_CODES and not launched():
                return not_started(str(error))
            result = {"state": "failed", "reason": str(error), "navigation_started": None if launched() else False,
                      "snapshot": None}
        except OSError as error:
            # As for Trends and IndiaMART, a browser that could not start (not a
            # signed-in wait) is a failure that must be seen and backs off.
            started_browser = launched()
            result = {"state": "failed", "snapshot": None, "navigation_started": None if started_browser else False,
                      "reason": "SIGNED_IN_BROWSER_READ_FAILED" if started_browser or isinstance(error, TimeoutError)
                                else "SIGNED_IN_BROWSER_START_FAILED"}
        if result.get("state") in ("waiting", "unavailable") and result.get("navigation_started") is False:
            reason = result.get("reason") or "OWNER_SIGN_IN_REQUIRED"
            if reason in CALM_CODES:
                return not_started(reason)
            result = dict(result, state="failed")
        snapshot = result.get("snapshot") if result.get("state") == "captured" else None
        body, wait_seconds = None, None
        if result.get("signed_in") is False:
            state, reason = "failed", "OWNER_SIGN_IN_REQUIRED"
        elif not isinstance(snapshot, dict):
            state, reason = "failed", result.get("reason") if result.get("state") == "failed" and str(
                result.get("reason") or "").startswith("SIGNED_IN_") else "DROPSHIP_PAGE_READ_FAILED"
        else:
            state, reason = "read", None
            try:
                read_only_url(self.route, (snapshot.get("url") or "").split("#")[0])
            except SafetyError:
                landed = urlsplit(snapshot.get("url") or "").path.casefold()
                # Redirected off the allowed pages: to a plan step it is a plan wall, elsewhere a refusal.
                state, reason = "failed", ("DROPSHIP_PLAN_REQUIRED" if set(landed.split("/")) & _NOT_READ_ONLY
                                           else "DROPSHIP_PAGE_REDIRECTED")
            if state == "read" and plan_wall(request, snapshot):
                state, reason = "failed", "DROPSHIP_PLAN_REQUIRED"
            if state == "read" and request["kind"] == "page" and not snapshot.get("cards"):
                state, reason = "failed", "DROPSHIP_PAGE_LAYOUT_UNRECOGNISED"
            if reason == "DROPSHIP_PLAN_REQUIRED":
                wait_seconds = PLAN_WAIT_SECONDS  # never clicked or paid: looked at again a day later
            if state == "read":
                body = json.dumps({field: snapshot.get(field) for field in ("url", "title", "text", "cards", "product",
                                                                             "categories", "next")},
                                  ensure_ascii=False, sort_keys=True)
        navigated = result.get("navigation_started") is not False
        receipt = {"schema_version": 2, "id": key("dropship-page-read", [self.route, url, started]), "requested_url": url,
                   "final_url": None, "checked_at": utc_now(), "http_status": None,
                   "execution_stage": "attempt_started" if navigated else "not_dispatched",
                   "attempt_started_at": started if navigated else None,
                   "execution_ref": "supplier-browser-" + digest([self.route, url, started])[:32] if navigated else None,
                   "state": state, "reason": reason, "route": self.route, "request": request, "read_at": at,
                   "profile": PROFILE, "signed_in": result.get("signed_in"), "read_folder": target.name}
        if state == "read":
            receipt["final_url"] = snapshot["url"].split("#")[0]
        return {"state": state, "reason": reason, "receipt": receipt, "body": body, "wait_seconds": wait_seconds,
                "drop_cycle": reason == "DROPSHIP_PAGE_LAYOUT_UNRECOGNISED"}

    def parse(self, request, raw):
        snapshot = body_json(raw)
        if not isinstance(snapshot, dict):
            raise SafetyError("DROPSHIP_FEED_BODY_INVALID")
        return parse_snapshot(self.route, request, snapshot)
