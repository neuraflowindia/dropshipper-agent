"""Dropdash, read in the owner's signed-in research profile (D1, D2).

Reading only. The page snapshot below copies the rendered page's title, text,
product cards (with their own title, price, stock, badge, section and image
elements), the product block of a product page and category/next-page links;
nothing is clicked, typed, submitted or scrolled. Only configured pages, their
numbered next pages and product pages are ever opened (``read_only_url``, an
allow-list);
order, cart, checkout, plan, membership, upgrade, trial, payment, contact,
import and account steps never are, and a read that lands on one (a redirect)
or shows a paid-plan or membership wall stops with ``DROPSHIP_PLAN_REQUIRED``
and is looked at again a day later. Nothing is ever paid or accepted.

A read counts only for the page it asked for (``same_page``: the address
Chrome reports, ignoring the case of the host, a default port, a fragment, a
trailing slash and the order of the query's parameters). A list or catalogue
page that lands on any other page, even an allowed one (a list that moved now
opening the home page), is ``DROPSHIP_PAGE_REDIRECTED``: like a redirect off
the allowed pages, the cycle keeps what it already read and only that page is
tried again after the usual wait. A product page that lands on another
product, or on no product (a deleted product opening the home page), is kept
as a read but records no item, so the lists cycle moves on and the product is
not asked for again within that cycle.

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

Dropdash's signed-in pages were first seen on 16 October 2026. Its home page
shows each category (its own "Testing Products" and "Profitable Products"
among them) with six product cards; a category page
(``products?categoryId=..``) shows the first 18 and loads more only when
scrolled, which this read never does. A card links to
``productDetails?id=<product number>&sellerId=..`` and shows the price, the
MRP, the units sold (a range) and the stock count in elements of their own;
the rupee sign is a picture, so prices are read from those elements
(``selectors`` in the platform settings), not from the text. A selector that
finds nothing never stops the read; but a catalogue or list page with no
product card showing its price (no cards, or a changed layout) stops that
refresh cycle with ``DROPSHIP_PAGE_LAYOUT_UNRECOGNISED`` rather than recording
an empty platform or priceless items. The catalogue read this way is a sample
(one page per configured address), never the whole platform.

Categories: the home page's category links become the platform's categories
(product-map material). Two kinds of link are not kinds of product and are
never recorded as one: a configured list page's own link (Dropdash's
"Profitable Products" is its winning list) is recorded with that list's role,
like a feed's list collection; any other sales section named in the settings
(``sales_sections``: Dropdash's "Testing Products") is left out. Their cards
keep the section's name as a tag, so the membership stays with each item. The
catalogue page itself is a page, not a category, and is never recorded as one.
"""

from datetime import timedelta
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from .dropship_platforms import LIST_ROLES, SIGNED_IN_ROUTES, amount, body_json, platform_settings
from .native_public_browser import signed_in_budget
from .models import SafetyError, digest, timestamp, utc_now
from .research_memory import key
from .storage import safe_path

PARSER_VERSION = "signed-in-platform-v3"
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
_PER_PERIOD = re.compile(r"(?:/|per)\s*(?:month|mo|year|yr|week|day)\b|\b(?:monthly|yearly|annually)\b", re.I)
_FIGURE = re.compile(r"(?<![0-9.,])([0-9][0-9,]{0,9}(?:\.[0-9]{1,2})?)(?![0-9])")
_DISCOUNT = re.compile(r"[ \t\u00a0]*(?:off|discount)\b", re.I)  # "₹ 29 OFF" on one line, never "₹ 220" then "Off-white"
_TERMS = re.compile(r"\b(?:dropship|shipping|delivery|dispatch|cod|cash on delivery|returns?|rto|moq|minimum order)\b", re.I)
_WALL = re.compile(r"\b(?:log ?in|sign ?in)\b.{0,80}\b(?:password|otp|continue with google)\b", re.I | re.S)

# One DOM read. __PRODUCT_PATTERN__ is the platform's product-address pattern
# and __PAGE_SELECTORS__ its own page selectors (JSON text, ``selectors`` in the
# settings; a null selector falls back to the generic guess). Titles, prices,
# stock, badges and sections are read from their own elements, never from the
# page text around them. A bad selector finds nothing; it never stops the read.
EXPRESSION = """JSON.stringify((() => {
  const product = new RegExp(__PRODUCT_PATTERN__), pick = __PAGE_SELECTORS__;
  const text = document.body?.innerText || '';
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const all = (root, selector) => { try { return root && selector ? Array.from(root.querySelectorAll(selector)) : []; } catch (e) { return []; } };
  const near = (node, selector) => { try { return selector ? node.closest(selector) : null; } catch (e) { return null; } };
  const field = (root, selector, limit) => { const node = all(root, selector)[0]; const value = node ? norm(node.innerText) : ''; return value && value.length <= limit ? value : null; };
  const short = (nodes, limit) => Array.from(nodes).map(n => (n.innerText || '').trim()).filter(t => t && t.length <= limit);
  const productUrl = href => product.test(href) ? href : product.test(href.split('?')[0]) ? href.split('?')[0] : null;
  const boxes = new Set(), cards = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = productUrl(a.href);
    if (!href || cards.length >= 200) continue;
    const box = near(a, pick.card) || a.closest('article,li,[class*="card"],[class*="Card"],[class*="product"],[class*="Product"]') || a;
    if (boxes.has(box)) continue;
    boxes.add(box);
    const img = box.querySelector('img[src^="https://"]');
    const heading = pick.card_title ? all(box, pick.card_title)[0] : box.querySelector('h1,h2,h3,h4,[class*="title"],[class*="Title"],[class*="name"],[class*="Name"]');
    const section = near(box, pick.section);
    cards.push({href, title:heading ? (heading.innerText || '').trim().slice(0,300) : null,
      text:(box.innerText || a.innerText || '').slice(0,600), image:img ? img.src : null,
      badges:short(box.querySelectorAll('[class*="badge"],[class*="Badge"],[class*="ribbon"],[class*="Ribbon"],[class*="tag"],[class*="Tag"],[class*="label"],[class*="Label"]'), 40).slice(0,8),
      price:field(box, pick.card_price, 40), mrp:field(box, pick.card_mrp, 40), stock:field(box, pick.card_stock, 40),
      sold:field(box, pick.card_sold, 40), section:section ? field(section, pick.section_title, 120) : null});
  }
  let block = null, heading = null;
  if (pick.product) {
    block = all(document, pick.product)[0] || null;
    heading = block ? all(block, pick.product_title || 'h1')[0] || null : null;
  } else {
    heading = document.querySelector('h1');
    block = heading ? (heading.closest('[itemtype*="Product"],[class*="product-detail"],[class*="productDetail"],[class*="product-info"],[class*="productInfo"],main') || heading.parentElement) : null;
  }
  const productBlock = block ? {title:heading ? (heading.innerText || '').trim().slice(0,300) : '', text:(block.innerText || '').slice(0,8000),
    terms:pick.product_terms ? all(block, pick.product_terms).map(n => norm(n.innerText)).filter(t => t && t.length <= 400).slice(0,10)
      : short(block.querySelectorAll('[class*="term"],[class*="Term"],[class*="shipping"],[class*="Shipping"],[class*="delivery"],[class*="Delivery"],[class*="dispatch"],[class*="return"],[class*="Return"],[class*="cod"],[id*="term"],[id*="shipping"]'), 400).slice(0,10),
    price:field(block, pick.product_price, 40), mrp:field(block, pick.product_mrp, 40), stock:field(block, pick.product_stock, 40)} : null;
  const categories = (pick.category
      ? all(document, pick.category).map(n => { const a = n.matches('a[href]') ? n : n.querySelector('a[href]') || n.closest('a[href]'); return a ? {href:a.href, text:norm(n.innerText).slice(0,120)} : null; }).filter(Boolean)
      : Array.from(document.querySelectorAll('a[href]')).filter(a => /\\/(categor(y|ies)|collections?)\\//.test(a.href)).map(a => ({href:a.href, text:(a.innerText || '').trim().slice(0,120)})))
    .filter(row => row.text).slice(0,200);
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
            or re.fullmatch(config["product_path"], url)):
        return url
    raise SafetyError("DROPSHIP_PAGE_NOT_READ_ONLY")


def _address(url):
    """What names a page in an address as Chrome reports it (``location.href``):
    the scheme and host in lower case, no default port, no fragment, no
    trailing slash, and the query's parameters (decoded) in one fixed order.
    None for an unreadable address."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url.split("#", 1)[0])
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.casefold()
    return (scheme, parts.hostname or "", None if port == {"https": 443, "http": 80}.get(scheme) else port,
            parts.path.rstrip("/") or "/", tuple(sorted(parse_qsl(parts.query, keep_blank_values=True))))


def same_page(landed, requested):
    """Whether the address a read landed on names the page it asked for."""
    found = _address(landed)
    return found is not None and found == _address(requested)


def _item_id(match, url):
    """The platform's own product number when its product address has one, else the address's last part."""
    return (match[1] if match.re.groups and match[1] else url.rstrip("/").rsplit("/", 1)[-1])[:120]


def _same_product(landed, request, product_path):
    """Whether a product page read landed on the product it asked for (its own
    address, or another address of the same product number)."""
    if not isinstance(landed, str):
        return False
    landed = landed.split("#", 1)[0]
    if same_page(landed, request.get("url")):
        return True
    match = re.fullmatch(product_path, landed)
    return match is not None and _item_id(match, landed) == request.get("item_id")


def expression(route):
    config = platform_settings(route)
    # One pass: a selector or pattern that happens to contain the other's
    # placeholder text is data, never substituted again.
    values = {"__PAGE_SELECTORS__": json.dumps(config["selectors"], sort_keys=True),
              "__PRODUCT_PATTERN__": json.dumps(config["product_path"])}
    return re.sub("__PAGE_SELECTORS__|__PRODUCT_PATTERN__", lambda found: values[found[0]], EXPRESSION)


def signed_in_marker(snapshot):
    """False for a sign-in wall, True for a page showing products, else None."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("text"), str):
        return None
    path = urlsplit(snapshot.get("url") or "").path.casefold()
    if snapshot.get("password") is True or re.search(r"/(?:login|signin|sign-in|auth)\b", path) or _WALL.search(snapshot["text"][:3000]):
        return False
    return True if snapshot.get("cards") or snapshot.get("product") else None


def prices(text):
    """(price, mrp) from one product's own text; a per-month/per-year figure (a
    plan banner) and a discount ("₹ 29 OFF") are never a price."""
    price = mrp = None
    for match in _PRICE.finditer(text or ""):
        if match[3] or _DISCOUNT.match(text, match.end()):
            continue
        if re.match(r"m\.?r\.?p", match[1] or "", re.I):
            mrp = mrp or match[2]
        elif price is None:
            price = match[2]
    return price, mrp


def _stock(text):
    lowered = (text or "").casefold()
    return False if "out of stock" in lowered or "sold out" in lowered else True if "in stock" in lowered else None


def _figure(value):
    """The number in one short element of its own ("₹ 1,299", "240", "97 units"),
    as plain digits; a per-month/per-year figure (a plan) is never one."""
    if not isinstance(value, str) or len(value) > 40 or _PER_PERIOD.search(value):
        return None
    match = _FIGURE.search(value)
    return match[1].replace(",", "") if match else None


def _own_prices(block, text):
    """(price, mrp) from the block's own price elements, else from its text."""
    if _figure(block.get("price")) is not None:
        return _figure(block["price"]), _figure(block.get("mrp"))
    return prices(text)


def _in_stock(block, text):
    """From the block's own stock count ("97", "0 units", "1.2K"), else from its text."""
    count = _figure(block.get("stock"))
    return float(count) > 0 if count is not None else _stock(text)


def _badges(values):
    return sorted({role for value in values or [] if isinstance(value, str) for role, rx in _BADGES if rx.search(value)})


def _card_item(card, product_path):
    href = card.get("href") if isinstance(card, dict) else None
    if not isinstance(href, str):
        return None
    # The whole address when the platform's product address has a query (Dropdash's
    # ``productDetails?id=..&sellerId=..``), else the address without one.
    url = href if re.fullmatch(product_path, href) else href.split("?")[0]
    match = re.fullmatch(product_path, url)
    if not match:
        return None
    text = card.get("text") if isinstance(card.get("text"), str) else ""
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    badge_texts = {value.strip().casefold() for value in card.get("badges") or [] if isinstance(value, str)}
    title = card.get("title") if isinstance(card.get("title"), str) and card["title"].strip() else next(
        (line for line in lines if not _PRICE.search(line) and line.casefold() not in badge_texts and len(line) > 3), None)
    price, mrp = _own_prices(card, text)
    section = " ".join(card["section"].split())[:80] if isinstance(card.get("section"), str) and card["section"].strip() else None
    return {"item_id": _item_id(match, url), "title": title, "url": url,
            "images": [card["image"]] if isinstance(card.get("image"), str) else [], "price": price, "mrp": mrp,
            "in_stock": _in_stock(card, text), "vendor": None, "product_type": None, "tags": [section] if section else [],
            "created_at": None, "terms": None, "badges": _badges(card.get("badges"))}


def plan_wall(request, snapshot, product_path):
    """True when the page asks for a paid plan or membership instead of showing
    products: a product page without its price, or a list page without one
    priced product card (cards shown unpriced behind the wall are no products)."""
    text = (snapshot.get("text") or "")[:24000]
    if not _PLAN_WALL.search(text):
        return False
    if request["kind"] == "product":
        block = snapshot.get("product") if isinstance(snapshot.get("product"), dict) else {}
        return _own_prices(block, block.get("text"))[0] is None
    return not shows_priced_products(snapshot, product_path)


def shows_priced_products(snapshot, product_path):
    """True when at least one card links to a product page and shows its price.
    A changed layout (selectors that now find nothing; the rupee sign is a
    picture, so the text has no price either) shows none, and is never
    recorded as a read of priceless items."""
    for card in snapshot.get("cards") or []:
        row = _card_item(card, product_path)
        if row is not None and amount(row["price"]) is not None:  # "0" (a placeholder) is no price
            return True
    return False


def parse_snapshot(route, request, snapshot):
    """Saved page snapshot -> items, categories and the next page to read."""
    config = platform_settings(route)
    if request["kind"] == "product":
        if not _same_product(snapshot.get("url"), request, config["product_path"]):
            # Another product's page, or no product's (a deleted product that now
            # opens the home page): nothing it shows is this item's.
            return {"items": [], "collections": None, "follow": []}
        block = snapshot.get("product") if isinstance(snapshot.get("product"), dict) else {}
        text = block.get("text") if isinstance(block.get("text"), str) else ""
        price, mrp = _own_prices(block, text)
        title = block.get("title") if isinstance(block.get("title"), str) and block["title"].strip() else request.get("title")
        terms = [" ".join(line.split()) for value in block.get("terms") or [] if isinstance(value, str)
                 for line in value.splitlines() if _TERMS.search(line) and not _PRICE.search(line)][:6]
        item = {"item_id": request["item_id"], "title": title, "url": request["url"], "images": [], "price": price,
                "mrp": mrp, "in_stock": _in_stock(block, text), "vendor": None, "product_type": None, "tags": [], "created_at": None,
                "terms": "; ".join(dict.fromkeys(terms))[:1000] or None, "badges": []}
        return {"items": [item], "collections": None, "follow": []}
    items, seen = [], {}
    for card in snapshot.get("cards") or []:
        row = _card_item(card, config["product_path"])
        if not row:
            continue
        kept = seen.get(row["item_id"])
        if kept is None:
            seen[row["item_id"]] = row
            items.append(row)
        else:  # one product shown twice (another link form, another section) is one item with both
            kept["tags"] = list(dict.fromkeys(kept["tags"] + row["tags"]))[:20]
            kept["badges"] = sorted(set(kept["badges"]) | set(row["badges"]))
    # A configured list page's own link is that list (its role), never a kind of
    # product; another sales section of the platform is left out (its cards keep
    # the section's name as a tag).
    lists = {_address(page["url"]): page["role"] for page in config["pages"] if page["role"] != "catalogue"}
    sales = {_address(url) for url in config["sales_sections"]}
    categories = []
    for row in snapshot.get("categories") or []:
        href = row.get("href") if isinstance(row, dict) else None
        if isinstance(href, str) and href.startswith(config["base_url"] + "/"):
            parts = urlsplit(href.split("#")[0])
            # A category chosen by a query (Dropdash's ``products?categoryId=282``) keeps that query.
            chosen = [(name, value) for name, value in parse_qsl(parts.query) if re.search(r"categor|collection", name, re.I)]
            name = "-".join([parts.path.strip("/").rsplit("/", 1)[-1]] + [value for _, value in chosen])
            handle = re.sub(r"[^a-z0-9-]+", "-", name.casefold()).strip("-")[:120]
            url = parts.scheme + "://" + parts.netloc + parts.path + ("?" + urlencode(chosen) if chosen else "")
            role = lists.get(_address(url))
            if handle and (role is not None or _address(url) not in sales):
                categories.append({"handle": handle, "title": " ".join(str(row.get("text")).split())[:200], "url": url,
                                   "products_count": None, "roles": [role] if role else [], "parent": None})
    # A list page is recorded as that list; the catalogue page is a page, not a category.
    pages = [] if request["role"] == "catalogue" else [{
        "handle": request["handle"], "title": request["role"].replace("_", " ").title() + " page", "url": request["url"],
        "products_count": len(items), "roles": [request["role"]], "parent": None}]
    follow, next_url = [], snapshot.get("next")
    if isinstance(next_url, str) and request["page"] < request["max_pages"]:
        try:
            follow = [dict(request, page=request["page"] + 1, url=read_only_url(route, next_url))]
        except SafetyError:
            follow = []
    # Complete as far as this cycle has read: only a full cycle's reads replace
    # the saved category list (``dropship_platforms._apply_read``).
    return {"items": items, "collections": pages + categories, "follow": follow, "collections_complete": True}


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
                   item["collections"]["product-page"]) >= timedelta(days=config["full_refresh_days"]))
               and self._allowed(item["url"])]  # an address the settings no longer allow is never planned
        products = [{"kind": "product", "item_id": item["item_id"], "handle": "product-page", "role": None, "page": 1,
                     "max_pages": 1, "title": item["title"], "url": read_only_url(self.route, item["url"])}
                    for item in due][:config["product_pages_per_day"]]
        return self._pages(set(LIST_ROLES) | {"new_arrival"}) + products

    def _allowed(self, url):
        try:
            return read_only_url(self.route, url) is not None
        except SafetyError:
            return False

    def hold(self, at):
        from .signed_in_attempts import signed_in_hold
        return signed_in_hold(self.pacing_root, self.route, as_of=at)

    def read(self, request, folder, sequence, at):
        from .signed_in_attempts import CALM_CODES, SignedInWait, begin_signed_in_read, release_unsent_read
        from .signed_in_browser import signed_in_read, signed_in_recheck_at
        if not self._allowed(request.get("url")):
            # Saved in a cycle planned under earlier settings (an address no longer
            # configured): never opened, nothing counted; the cycle is dropped and
            # the next one is planned from the current settings.
            requested, checked = request.get("url") if isinstance(request.get("url"), str) else None, utc_now()
            receipt = {"schema_version": 2, "id": key("dropship-page-read", [self.route, requested, checked]),
                       "requested_url": requested, "final_url": None, "checked_at": checked, "http_status": None,
                       "execution_stage": "not_dispatched", "attempt_started_at": None, "execution_ref": None,
                       "state": "failed", "reason": "DROPSHIP_PAGE_NOT_READ_ONLY", "route": self.route, "request": request,
                       "read_at": at, "profile": PROFILE, "signed_in": None, "read_folder": None}
            return {"state": "failed", "reason": "DROPSHIP_PAGE_NOT_READ_ONLY", "receipt": receipt, "body": None,
                    "wait_seconds": None, "drop_cycle": True}
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
            landed = snapshot["url"].split("#")[0] if isinstance(snapshot.get("url"), str) else ""
            if not same_page(landed, url):
                try:
                    read_only_url(self.route, landed)
                except SafetyError:
                    # Redirected off the allowed pages: to a plan step it is a plan wall, elsewhere a refusal.
                    try:
                        path = urlsplit(landed).path.casefold()
                    except ValueError:  # an address that cannot be read is no plan step
                        path = ""
                    state, reason = "failed", ("DROPSHIP_PLAN_REQUIRED" if set(path.split("/")) & _NOT_READ_ONLY
                                               else "DROPSHIP_PAGE_REDIRECTED")
                else:
                    if request["kind"] == "page":
                        # Another allowed page (a list that moved now opens the home
                        # page): its cards are not this page's. Like any redirect, the
                        # cycle keeps what it read and tries this page again later.
                        state, reason = "failed", "DROPSHIP_PAGE_REDIRECTED"
                    # A product page that opened another product (or none) stays a
                    # read: ``parse_snapshot`` records no item from it.
            if state == "read" and plan_wall(request, snapshot, self.config["product_path"]):
                state, reason = "failed", "DROPSHIP_PLAN_REQUIRED"
            if state == "read" and request["kind"] == "page" and not shows_priced_products(snapshot, self.config["product_path"]):
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
