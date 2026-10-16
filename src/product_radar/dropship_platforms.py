"""The three Indian dropshipping platforms (owner decision D1, Plan R10 W4).

DeoDap and BaapStore publish official Shopify feeds (``platform_feeds``);
Dropdash is read in the owner's signed-in research profile
(``platform_signed_in``). The owner dropped a fourth, Dropship India, on
15 October 2026: it showed nothing useful, signed in or not. Every read is
kept as a file under ``<store>/dropship_platforms/<route>/raw/`` and merged
into one current catalogue per platform; only matched, relevant items become
research events.
A listing is supplier and presence information, never a market vote (that
waits for the ranking redesign).

Saved catalogue (``catalogue.json``, ``CATALOGUE_VERSION``)::

    {version, route, scope ("full" feed or signed-in "sample"), parser_version,
     updated_at, full_completed_at, lists_completed_at,
     collections: [COLLECTION], items: {item_id: ITEM}}

    COLLECTION = {handle, title, url, products_count, roles, parent}
    ITEM = {item_id, title, url, images (<=4), price, mrp (decimal text or
            None), price_basis, in_stock (bool or None), vendor, product_type,
            tags (<=20), created_at, terms (stated text or None),
            first_seen_at, last_seen_at, source_ref,
            collections {handle: seen_at},
            roles {role: {first_seen_at, last_seen_at, via (list handles)}},
            units_sold (only where the platform shows it: Dropdash's "5K+")}

The collection list is also the product-map material for the bigger map
(W10): ``collections`` keeps every platform category with its roles and, where
the platform has one, its ``parent`` handle. Only a full cycle replaces it; a
lists cycle (list pages only) never does.

Refreshing (``advance_platform``) works through one saved cycle at a time: a
full catalogue at most every ``full_refresh_days``, the role lists (new
arrivals, bestsellers, trending ...) and plain lists every
``list_refresh_hours``, and (a signed-in platform) its category pages every
``category_refresh_days``, a few paced requests per programme round. A catalogue counts only after one full cycle
finished with items; until then no per-type question is planned. A sampled
(signed-in) catalogue never treats an item it did not see again as delisted.
"""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import gzip
import json
from pathlib import Path
import re
import shutil
import time

from .models import SafetyError, canonical, decode_json, timestamp, utc_now
from .storage import atomic_bytes, atomic_json, file_lock, load_json, safe_path

GROUP = "dropship-platforms"
ROUTES = ("deodap", "baapstore", "dropdash")
FEED_ROUTES = ("deodap", "baapstore")
SIGNED_IN_ROUTES = ("dropdash",)
LABELS = {"deodap": "DeoDap", "baapstore": "BaapStore", "dropdash": "Dropdash"}
# A relevant item on any of these lists is on a bestseller / trending /
# high-demand list; "new_arrival" is the separate newly-launched signal.
LIST_ROLES = ("bestseller", "trending", "high_demand", "winning", "state_bestseller")
ROLES = LIST_ROLES + ("new_arrival",)
# A signed-in platform's configured pages: its catalogue page, its role lists
# (above), and plain product lists ("list": read with the lists, its items kept
# with their membership but given no role, so no list points and no role chip;
# owner decision of 16 October 2026 for Dropdash's "Profitable Products").
# "category" is never configured: the weekly categories cycle reads each
# category page the catalogue page links to.
PAGE_ROLES = ROLES + ("catalogue", "list")
CATEGORY_ROLE = "category"
# Dropdash's "units sold" range on a card or product page ("100-500", "1K-2K",
# "5K+"): kept with the item as an extra demand hint, never as score points.
_UNITS_SOLD = re.compile(r"[0-9]{1,4}(?:\.[0-9])?[KkLM]?(?:\+|\s?-\s?[0-9]{1,4}(?:\.[0-9])?[KkLM]?)?")
CATALOGUE_VERSION = "dropship-catalogue-v2"
REFRESH_VERSION = "dropship-refresh-v1"
SETTINGS = Path(__file__).resolve().parents[2] / "config/research/dropship_platforms.json"
MAX_CATALOGUE = 128 * 1024 * 1024
MAX_RAW = 32 * 1024 * 1024
# Consecutive refusals of a platform wait longer each time (5 minutes, 1 hour,
# 6 hours, then a day), like the other research routes' access waits.
BACKOFF_SECONDS = (300, 3600, 6 * 3600, 24 * 3600)
SHORT_SPACING_SECONDS = 10
# A weekly category page that fails for a reason of its own (empty or changed,
# sent elsewhere) is skipped for the week; any other failure (a sign-in or plan
# wall, a browser that could not read) waits as usual and the page is tried again.
_PAGE_ONLY = ("DROPSHIP_PAGE_LAYOUT_UNRECOGNISED", "DROPSHIP_PAGE_REDIRECTED")
_HANDLE = re.compile(r"[a-z0-9][a-z0-9-]{0,119}")
# Where a signed-in platform's own page shows each fact (CSS selectors, or
# null for the generic guess; ``platform_signed_in`` reads them, never clicks).
SELECTOR_KEYS = ("card", "card_title", "card_price", "card_mrp", "card_stock", "card_sold", "section", "section_title",
                 "category", "product", "product_title", "product_price", "product_mrp", "product_stock", "product_terms")
_TEXT_LIMIT = 500
# A signed-in platform's product-address pattern is read by Python
# (``re.fullmatch``) and by the page's JavaScript (``new RegExp``): only plain
# pieces both read alike are allowed. An escape is only of a punctuation mark
# (never \d, \A, \Z, \b or a back-reference), a group only ``(`` or ``(?:``
# (never ``(?P<``, ``(?i)``, ``(?>`` or a look-around), and a quantifier
# never follows another (a possessive ``*+`` is Python-only).
_PUNCTUATION = r"\\[!-/:-@\[-`{-~]"
_PATTERN_PIECE = re.compile("|".join((
    "(?P<escape>" + _PUNCTUATION + ")",
    r"(?P<set>\[\^?(?:[A-Za-z0-9._~%&=/:,;!@'+-]|" + _PUNCTUATION + r")+\])",
    r"(?P<open>\((?:\?:)?)", r"(?P<close>\))",
    r"(?P<repeat>(?:[?*+]|\{[0-9]{1,3}(?:,[0-9]{0,3})?\})\??)",
    r"(?P<either>\|)", r"(?P<plain>[A-Za-z0-9._~%&=/:,;!@'-])")))


def _browser_pattern(pattern, base_url):
    """Whether a product-address pattern is anchored to the platform's own
    address (``^`` + its escaped base address + ``/`` .. ``$``) and made only
    of pieces Python and JavaScript read alike."""
    prefix = "^" + re.escape(base_url) + "/"
    if not isinstance(pattern, str) or len(pattern) > 500 or not pattern.startswith(prefix) or not pattern.endswith("$"):
        return False
    body, position, depth, previous = pattern[len(prefix):-1], 0, 0, "open"
    if not body:
        return False
    while position < len(body):
        piece = _PATTERN_PIECE.match(body, position)
        if piece is None:
            return False  # an escaped "$" at the end leaves a lone "\" here
        kind, position = piece.lastgroup, piece.end()
        if kind == "set" and re.search(r"&&|\|\||--|~~", piece[0]):
            return False  # kept for set operations in a future Python
        if kind == "repeat" and previous not in ("escape", "set", "close", "plain"):
            return False  # nothing to repeat, or a quantifier on a quantifier
        if kind == "either" and depth == 0:
            return False  # a top-level "|" would escape the anchors
        if kind == "close" and (depth == 0 or previous == "open"):
            return False
        depth += {"open": 1, "close": -1}.get(kind, 0)
        previous = kind
    return depth == 0


def _text(value, limit=_TEXT_LIMIT):
    return " ".join(value.split())[:limit] if isinstance(value, str) and value.strip() else None


def amount(value):
    """A positive price as canonical decimal text ("119", "12.5"), or None.

    A zero price ("0.00", a placeholder variant) is no price.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None
    if not number.is_finite() or number <= 0:
        return None
    text = format(number.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _settings_value(value):
    platforms = value.get("platforms") if isinstance(value, dict) else None
    try:
        if (set(value) != {"schema_version", "kind", "platforms", "refresh", "matching", "discovery"}
                or value["schema_version"] != 1 or type(value["schema_version"]) is not int
                or value["kind"] != "dropship-platforms-v2" or not isinstance(platforms, dict) or set(platforms) != set(ROUTES)
                or set(value["refresh"]) != {"seconds_per_round"} or type(value["refresh"]["seconds_per_round"]) is not int
                or not 10 <= value["refresh"]["seconds_per_round"] <= 600):
            raise ValueError
        for route, row in platforms.items():
            feed = route in FEED_ROUTES
            common = {"transport", "base_url", "requests_per_round", "full_refresh_days", "list_refresh_hours",
                      "raw_retention_days", "price_basis", "stated_terms", "terms_url"}
            expected = common | ({"page_size", "lists"} if feed else {"product_path", "product_pages_per_day", "pages", "selectors",
                                                                      "sales_sections", "category_path",
                                                                      "category_refresh_days"})
            if (set(row) != expected or row["transport"] != ("shopify-feed" if feed else "signed-in-browser")
                    or not re.fullmatch(r"https://[a-z0-9.-]+", row["base_url"])
                    or any(type(row[name]) is not int or not low <= row[name] <= high for name, low, high in (
                        ("requests_per_round", 1, 20), ("full_refresh_days", 1, 30), ("list_refresh_hours", 1, 168),
                        ("raw_retention_days", 2, 90)))
                    # Saved reads outlive at least one whole catalogue refresh.
                    or row["raw_retention_days"] < row["full_refresh_days"] + 1
                    or not _text(row["price_basis"])
                    or row["stated_terms"] is not None and not _text(row["stated_terms"])
                    or row["terms_url"] is not None and not row["terms_url"].startswith(row["base_url"] + "/")):
                raise ValueError
            if feed:
                if type(row["page_size"]) is not int or not 1 <= row["page_size"] <= 250 or not row["lists"]:
                    raise ValueError
                for rule in row["lists"]:
                    if (set(rule) != {"role", "handles", "pattern", "max_pages"} or rule["role"] not in ROLES
                            or not isinstance(rule["handles"], list) or any(not isinstance(h, str) or not _HANDLE.fullmatch(h)
                                                                             for h in rule["handles"])
                            or rule["pattern"] is not None and not isinstance(rule["pattern"], str)
                            or not rule["handles"] and rule["pattern"] is None
                            or type(rule["max_pages"]) is not int or not 1 <= rule["max_pages"] <= 20):
                        raise ValueError
                    if rule["pattern"] is not None:
                        re.compile(rule["pattern"])
            else:
                # Anchored to the platform, read alike by Python and the page's
                # JavaScript, and one capturing group at most: the platform's own
                # product number, when its address has one.
                if not _browser_pattern(row["product_path"], row["base_url"]) or re.compile(row["product_path"]).groups > 1:
                    raise ValueError
                # A category page's address (read weekly, one page each), anchored to the
                # platform like the product address; never one of the never-opened steps.
                if (not _browser_pattern(row["category_path"], row["base_url"]) or re.compile(row["category_path"]).groups > 1
                        or type(row["category_refresh_days"]) is not int or not 1 <= row["category_refresh_days"] <= 30):
                    raise ValueError
                # The platform's own sales sections (category links that are no kind of product).
                sections = row["sales_sections"]
                if (not isinstance(sections, list) or len(sections) > 50 or len(set(map(str, sections))) != len(sections)
                        or any(not isinstance(url, str) or len(url) > 300 or not url.startswith(row["base_url"] + "/")
                               or "#" in url or re.search(r"[\x00-\x20\x7f]", url) for url in sections)):
                    raise ValueError
                selectors = row["selectors"]
                if (not isinstance(selectors, dict) or set(selectors) != set(SELECTOR_KEYS)
                        or any(selector is not None and (not isinstance(selector, str) or not 1 <= len(selector) <= 300
                                                         or re.search(r"[\x00-\x1f\x7f]", selector))
                               for selector in selectors.values())):
                    raise ValueError
                if (type(row["product_pages_per_day"]) is not int or not 0 <= row["product_pages_per_day"] <= 300
                        or not row["pages"] or not any(page.get("role") == "catalogue" for page in row["pages"])):
                    raise ValueError
                for page in row["pages"]:
                    if (set(page) != {"role", "url", "max_pages"} or page["role"] not in PAGE_ROLES
                            or not isinstance(page["url"], str) or not page["url"].startswith(row["base_url"] + "/")
                            or type(page["max_pages"]) is not int or not 1 <= page["max_pages"] <= 20):
                        raise ValueError
        matching, discovery = value["matching"], value["discovery"]
        from .service import CATEGORIES
        if (set(matching) != {"max_matches_per_type", "min_coverage", "max_terms"}
                or type(matching["max_matches_per_type"]) is not int or not 1 <= matching["max_matches_per_type"] <= 20
                or type(matching["max_terms"]) is not int or not 1 <= matching["max_terms"] <= 12
                or not Decimal("0.3") <= Decimal(matching["min_coverage"]) <= 1
                or set(discovery) != {"items_per_round", "daily_cap", "max_pending_captures", "settle_after_days",
                                      "categories", "exclude"}
                or any(type(discovery[name]) is not int or not low <= discovery[name] <= high for name, low, high in (
                    ("items_per_round", 1, 10), ("daily_cap", 1, 200), ("max_pending_captures", 1, 10),
                    ("settle_after_days", 1, 30)))
                or set(discovery["categories"]) != set(CATEGORIES)
                or any(not isinstance(words, list) or not words or any(not _text(word, 60) for word in words)
                       for words in list(discovery["categories"].values()) + [discovery["exclude"]])):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation, re.error):
        raise SafetyError("DROPSHIP_SETTINGS_INVALID") from None
    return value


def validate_settings(value):
    """The saved platform settings, or ``DROPSHIP_SETTINGS_INVALID``."""
    return _settings_value(value)


def settings():
    return _settings_value(load_json(SETTINGS, 64 * 1024))


def platform_settings(route):
    if route not in ROUTES:
        raise SafetyError("DROPSHIP_ROUTE_INVALID")
    return deepcopy(settings()["platforms"][route])


def platform_folder(root, route):
    if route not in ROUTES:
        raise SafetyError("DROPSHIP_ROUTE_INVALID")
    return safe_path(root, "dropship_platforms", route)


def catalogue_scope(route):
    """"full" for a platform whose feed lists its whole catalogue, "sample" for
    the signed-in platforms, whose catalogue pages are read only a few deep."""
    return "full" if route in FEED_ROUTES else "sample"


def _empty(route):
    return {"version": CATALOGUE_VERSION, "route": route, "scope": catalogue_scope(route), "parser_version": None,
            "updated_at": None, "full_completed_at": None, "lists_completed_at": None, "collections": [], "items": {}}


def load_catalogue(root, route):
    """The platform's saved catalogue (an empty one before the first read)."""
    path = platform_folder(root, route) / "catalogue.json"
    if not path.exists() and not path.is_symlink():
        return _empty(route)
    value = load_json(path, MAX_CATALOGUE)
    if (not isinstance(value, dict) or set(value) != set(_empty(route)) or value["version"] != CATALOGUE_VERSION
            or value["route"] != route or not isinstance(value["items"], dict) or not isinstance(value["collections"], list)):
        raise SafetyError("DROPSHIP_CATALOGUE_INVALID")
    return value


def save_catalogue(root, route, value):
    atomic_json(platform_folder(root, route) / "catalogue.json", value)


def catalogue_wait(root, route):
    """None when the platform has a finished catalogue, else the calm reason.

    Reads only the small refresh record, so per-type questions can ask it every round.
    """
    refresh = load_refresh(root, route)
    if refresh["completed"].get("full", {}).get("items"):
        return None
    return refresh["wait"].get("reason") or "DROPSHIP_CATALOGUE_PENDING"


CATALOGUE_STATE_KIND = "dropship-catalogue-state-v1"


def catalogue_date(state, route):
    """When the platform's newest finished catalogue was completed, or None.

    Per-type questions are planned only once there is one
    (``record_catalogue_states`` writes one small record per finished full
    cycle), so no question waits for a platform that has never been read, and
    each new catalogue gets its own question.
    """
    dates = [row["full_completed_at"] for row in state.get("source_checks", {}).values()
             if row.get("kind") == CATALOGUE_STATE_KIND and row.get("route") == route]
    return max(dates, key=timestamp) if dates else None


def record_catalogue_states(memory, root):
    """Note each platform's newest finished full cycle in the research records."""
    from .research_memory import key
    recorded = []
    for route in ROUTES:
        full = load_refresh(root, route)["completed"].get("full")
        if not full or not full.get("items"):
            continue
        record_id = key("dropship-catalogue-state", [route, full["completed_at"]])
        if record_id not in memory.snapshot()["source_checks"]:
            memory.put("source_checks", record_id, {"kind": CATALOGUE_STATE_KIND, "route": route, "at": full["completed_at"],
                                                    "full_completed_at": full["completed_at"], "items": full["items"]},
                       event_id=key("dropship-catalogue-state-record", record_id))
            recorded.append(route)
    return recorded


def load_refresh(root, route):
    path = platform_folder(root, route) / "refresh.json"
    if not path.exists() and not path.is_symlink():
        return {"version": REFRESH_VERSION, "route": route, "cycle": None, "completed": {}, "failures": 0,
                "blocked_until": None, "wait": {}, "sequence": 0}
    value = load_json(path, 4 * 1024 * 1024)
    if (not isinstance(value, dict) or value.get("version") != REFRESH_VERSION or value.get("route") != route
            or set(value) != {"version", "route", "cycle", "completed", "failures", "blocked_until", "wait", "sequence"}):
        raise SafetyError("DROPSHIP_REFRESH_RECORD_INVALID")
    return value


def _save_refresh(root, route, value):
    atomic_json(platform_folder(root, route) / "refresh.json", value)


def save_raw(root, route, at, sequence, receipt, body):
    """Keep one read as a compressed file; returns its path inside the platform folder."""
    day = timestamp(at).date().isoformat()
    relative = "raw/" + day + "/" + str(sequence).zfill(6) + ".json.gz"
    path = platform_folder(root, route) / relative
    safe_path(path.parent.parent)
    record = canonical({"receipt": receipt, "body": body})
    atomic_bytes(path, gzip.compress(record, mtime=0))
    return relative


def load_raw(root, route, relative):
    if not isinstance(relative, str) or not re.fullmatch(r"raw/\d{4}-\d{2}-\d{2}/\d{6}\.json\.gz", relative):
        raise SafetyError("DROPSHIP_RAW_REFERENCE_INVALID")
    path = platform_folder(root, route) / relative
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RAW:
        raise SafetyError("DROPSHIP_RAW_UNAVAILABLE")
    try:
        value = decode_json(gzip.decompress(path.read_bytes()))
    except (OSError, EOFError):
        raise SafetyError("DROPSHIP_RAW_UNAVAILABLE") from None
    if not isinstance(value, dict) or set(value) != {"receipt", "body"} or not isinstance(value["receipt"], dict):
        raise SafetyError("DROPSHIP_RAW_UNAVAILABLE")
    return value


def raw_files(root, route):
    """Saved reads, oldest first."""
    base = platform_folder(root, route) / "raw"
    if not base.is_dir():
        return []
    return sorted(str(path.relative_to(platform_folder(root, route))) for path in base.glob("*/*.json.gz"))


def prune_raw(root, route, as_of, retention_days):
    """Drop whole days of saved reads, and signed-in browser read folders,
    older than the retention (settings keep it longer than a full refresh)."""
    folder = platform_folder(root, route)
    cutoff = timestamp(as_of) - timedelta(days=retention_days)
    removed = 0
    base = folder / "raw"
    for day in sorted(base.iterdir()) if base.is_dir() else []:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name) and day.name < cutoff.date().isoformat() and not day.is_symlink():
            shutil.rmtree(day)
            removed += 1
    reads = folder / "reads"
    for read in sorted(reads.iterdir()) if reads.is_dir() else []:
        if (re.fullmatch(r"r\d{6}", read.name) and not read.is_symlink() and read.is_dir()
                and read.stat().st_mtime < cutoff.timestamp()):
            shutil.rmtree(read)
            removed += 1
    return removed


def clean_item(row, *, price_basis):
    """A parser's item reduced to the saved item fields; None if unusable."""
    if not isinstance(row, dict):
        return None
    item_id, title, url = _text(row.get("item_id"), 120), _text(row.get("title"), 300), row.get("url")
    if not item_id or not title or not isinstance(url, str) or not re.fullmatch(r"https://[^\s\"'<>]{1,2000}", url):
        return None
    images = [src for src in row.get("images") or [] if isinstance(src, str) and re.fullmatch(r"https://[^\s\"'<>]{1,2000}", src)][:4]
    price, mrp = amount(row.get("price")), amount(row.get("mrp"))
    if price and mrp and Decimal(mrp) <= Decimal(price):
        mrp = None
    tags = [tag for tag in (_text(tag, 80) for tag in row.get("tags") or []) if tag][:20]
    badges = sorted({role for role in row.get("badges") or [] if role in ROLES})
    created = row.get("created_at")
    try:
        created = timestamp(created).isoformat() if created else None
    except SafetyError:
        created = None
    in_stock = row.get("in_stock")
    value = {"item_id": item_id, "title": title, "url": url, "images": images, "price": price, "mrp": mrp,
             "price_basis": price_basis, "in_stock": in_stock if type(in_stock) is bool else None,
             "vendor": _text(row.get("vendor"), 120), "product_type": _text(row.get("product_type"), 120),
             "tags": tags, "created_at": created, "terms": _text(row.get("terms"), 1000), "badges": badges}
    sold = units_sold(row.get("units_sold"))
    if sold:
        value["units_sold"] = sold  # only a platform that shows it (Dropdash) carries the field
    return value


def units_sold(value):
    """A platform's own "units sold" range ("100-500", "1K-2K", "5K+") in one
    plain spelling, or None for anything else."""
    if not isinstance(value, str) or len(value) > 20:
        return None
    text = " ".join(value.split())
    return re.sub(r"\s?-\s?", "-", text).upper() if _UNITS_SOLD.fullmatch(text) else None


def merge_items(catalogue, items, *, seen_at, source_ref, handle=None, role=None, authoritative=True):
    """Add or refresh items; a list page also records its membership and role.

    Only an authoritative read (a platform feed, which states every field) may
    clear a known value. A signed-in card or product page shows less: what it
    leaves out is unknown, not removed, and its tags (the section or category it
    was shown in) are added to the item's earlier ones. List roles come from the
    page's own role and from the card's badge elements only (``badges``).
    """
    merged = 0
    for row in items:
        row = dict(row)
        badges = row.pop("badges", [])
        old = catalogue["items"].get(row["item_id"])
        value = dict(old or {}, **row)
        value.update(first_seen_at=(old or {}).get("first_seen_at") or seen_at, last_seen_at=seen_at, source_ref=source_ref,
                     collections=dict((old or {}).get("collections", {})), roles=deepcopy((old or {}).get("roles", {})))
        if old:
            kept = ("terms",) if authoritative else ("price", "mrp", "in_stock", "vendor", "product_type", "created_at",
                                                     "images", "terms", "units_sold")
            for field in kept:
                if row.get(field) in (None, []) and field in old:
                    value[field] = old[field]
            if not authoritative:
                # Each signed-in page names only its own section or category: the newest first, the rest kept.
                value["tags"] = list(dict.fromkeys(list(row.get("tags") or []) + list(old.get("tags") or [])))[:20]
        if handle is not None:
            value["collections"][handle] = seen_at
        for held in ({role} - {None}) | (set(badges) if handle is not None else set()):
            old_role = value["roles"].get(held, {})
            value["roles"][held] = {"first_seen_at": old_role.get("first_seen_at") or seen_at, "last_seen_at": seen_at,
                                    "via": sorted(set(old_role.get("via", [])) | ({handle} if handle else set()))}
        catalogue["items"][row["item_id"]] = value
        merged += 1
    return merged


def _finish_lists(catalogue, handles, started_at):
    """Drop list memberships (and their roles) not seen again in this cycle."""
    start = timestamp(started_at)
    for item in catalogue["items"].values():
        item["collections"] = {handle: at for handle, at in item["collections"].items()
                               if handle not in handles or timestamp(at) >= start}
        roles = {}
        for role, dates in item["roles"].items():
            via = [handle for handle in dates["via"] if handle in item["collections"]]
            if via:
                roles[role] = dict(dates, via=via)
        item["roles"] = roles


def _finish_full(catalogue, started_at, *, complete, at, stale_days):
    """A whole-catalogue feed drops what it no longer lists. A sampled
    catalogue never treats an unseen item as delisted; it only forgets items
    not seen for ``stale_days``."""
    cutoff = timestamp(started_at) if complete else timestamp(at) - timedelta(days=stale_days)
    catalogue["items"] = {item_id: item for item_id, item in catalogue["items"].items()
                          if timestamp(item["last_seen_at"]) >= cutoff}


def _due(completed, kind, hours, at):
    done = completed.get(kind, {}).get("completed_at")
    return done is None or timestamp(at) - timestamp(done) >= timedelta(hours=hours)


def platform_state(root, route):
    """Owner/desktop view of one platform's catalogue (no read is made)."""
    catalogue, refresh = load_catalogue(root, route), load_refresh(root, route)
    return {"route": route, "label": LABELS[route], "items": len(catalogue["items"]), "scope": catalogue["scope"],
            "collections": len(catalogue["collections"]), "full_completed_at": catalogue["full_completed_at"],
            "lists_completed_at": catalogue["lists_completed_at"], "cycle": (refresh["cycle"] or {}).get("kind"),
            "wait": deepcopy(refresh["wait"]) or None, "blocked_until": refresh["blocked_until"],
            "ready": catalogue_wait(root, route) is None}


_OWNER_LINES = {
    "DROPSHIP_PLAN_REQUIRED": "{label} shows a paid plan or membership page instead of products. The program never pays "
                              "or accepts a plan; it looks again in a day. Nothing to do unless you choose to upgrade.",
    "OWNER_SIGN_IN_REQUIRED": "{label} needs you to sign in (Sign in to research sites).",
    "DROPSHIP_PAGE_LAYOUT_UNRECOGNISED": "{label}'s product pages were not recognised; the program waits a day and tries again.",
    "DROPSHIP_PAGE_REDIRECTED": "{label} sent one of its product lists to a different page; the program waits and tries "
                                "again (at most a day). If this keeps showing, the list may have moved.",
}


def platform_lines(root):
    """Plain sentences for the owner about platforms that wait on something visible."""
    lines = []
    for route in ROUTES:
        reason = load_refresh(root, route)["wait"].get("reason")
        if reason in _OWNER_LINES:
            lines.append(_OWNER_LINES[reason].format(label=LABELS[route]))
    return lines


def _fail(refresh, at, reason, *, wait_seconds=None, drop_cycle=False):
    """A read that did not give a usable answer: back off 5 min, 1 h, 6 h, then a day."""
    refresh["failures"] += 1
    wait = wait_seconds or BACKOFF_SECONDS[min(refresh["failures"], len(BACKOFF_SECONDS)) - 1]
    refresh.update(blocked_until=(timestamp(at) + timedelta(seconds=wait)).isoformat(), wait={"reason": reason, "at": at})
    if drop_cycle:
        refresh["cycle"] = None  # an unrecognised page layout: nothing of this cycle is trusted


def advance_platform(root, route, reader, *, as_of=None, limit=None, clock=utc_now, sleep=time.sleep,
                     deadline=None, before_read=None, monotonic=time.monotonic):
    """Advance the platform's saved refresh by at most ``limit`` reads.

    ``reader`` supplies the transport: ``parser_version``, ``complete_catalogue``,
    ``authoritative``, ``plan_full(settings, catalogue)``, ``plan_lists(settings,
    catalogue)``, ``hold(at)`` (None or a calm ``{reason, next_retry_at}``),
    ``read(request, folder, sequence, at)`` and ``parse(request, raw)``; a
    signed-in reader also ``plan_categories(settings, catalogue)``: its weekly
    categories cycle (one page per category the catalogue page links to, once
    the full and lists cycles are current), saved like the others so a restart
    resumes it instead of starting again.
    ``deadline`` (a ``monotonic`` time) bounds the round, including a read in
    progress (``reader.worst_case_seconds``, when set); ``before_read`` is
    asked before every read and refuses it (``SafetyError``) when the owner has
    paused research or the work turn is closed. Returns what happened; a
    refused, failed or unparsable read makes the platform wait with its reason.
    """
    config = platform_settings(route)
    limit = config["requests_per_round"] if limit is None else limit
    folder = platform_folder(root, route)
    folder.mkdir(parents=True, exist_ok=True)
    reads, outcome = 0, {"route": route, "reads": 0, "state": "idle", "reason": None, "next_retry_at": None}
    with file_lock(folder / "refresh.lock", 0, "DROPSHIP_REFRESH_BUSY"):
        catalogue = load_catalogue(root, route)
        if catalogue["parser_version"] not in (None, reader.parser_version):
            catalogue = replay_platform(root, route, reader)  # a new parser re-reads the saved feeds
        while reads < limit:
            at = as_of or clock()
            refresh = load_refresh(root, route)
            if refresh["cycle"] is not None and not getattr(reader, "cycle_current", lambda cycle: True)(refresh["cycle"]):
                # Planned under earlier settings (a list made plain, a page that moved):
                # planned again from the current ones. No failure of the platform, and
                # failures counted under the earlier settings no longer lengthen its waits.
                refresh.update(cycle=None, failures=0)
                _save_refresh(root, route, refresh)
            if refresh["blocked_until"] and timestamp(at) < timestamp(refresh["blocked_until"]):
                outcome.update(state="waiting", reason=refresh["wait"].get("reason"), next_retry_at=refresh["blocked_until"])
                break
            if refresh["cycle"] is None:
                kind = ("full" if _due(refresh["completed"], "full", config["full_refresh_days"] * 24, at) else
                        "lists" if catalogue["full_completed_at"] and _due(refresh["completed"], "lists",
                                                                           config["list_refresh_hours"], at) else
                        "categories" if (catalogue["full_completed_at"] and hasattr(reader, "plan_categories")
                                         and _due(refresh["completed"], "categories",
                                                  config["category_refresh_days"] * 24, at)) else None)
                if kind is None:
                    outcome["state"] = "current"
                    break
                plan = (reader.plan_full(config, catalogue) if kind == "full" else
                        reader.plan_lists(config, catalogue) if kind == "lists" else reader.plan_categories(config, catalogue))
                refresh["cycle"] = {"kind": kind, "started_at": at, "queue": plan, "handles": [], "collections": [], "items_seen": 0}
                _save_refresh(root, route, refresh)
            cycle = refresh["cycle"]
            if not cycle["queue"]:
                _complete(root, route, catalogue, refresh, at, outcome, reader)
                continue
            # A read must fit in what is left of the round: one whose worst
            # case (a signed-in page) would run past it waits for the next round.
            if deadline is not None and monotonic() + getattr(reader, "worst_case_seconds", 0) >= deadline:
                outcome["state"] = "round_time_spent"
                break
            if before_read is not None:
                try:
                    before_read()
                except SafetyError as error:
                    outcome.update(state="waiting", reason=str(error))
                    break
            hold = reader.hold(at)
            if hold and as_of is None and hold["reason"] == "SIGNED_IN_READ_SPACING" and hold.get("next_retry_at"):
                pause = (timestamp(hold["next_retry_at"]) - timestamp(at)).total_seconds()
                if 0 < pause <= SHORT_SPACING_SECONDS:
                    sleep(pause)  # a feed's few seconds of spacing is waited out inside the round
                    continue
            if hold:
                refresh.update(wait={"reason": hold["reason"], "at": at})
                _save_refresh(root, route, refresh)
                outcome.update(state="waiting", reason=hold["reason"], next_retry_at=hold.get("next_retry_at"))
                break
            request = cycle["queue"][0]
            refresh["sequence"] += 1
            _save_refresh(root, route, refresh)  # a read's file name is never reused, even after a crash
            read_clock = time.monotonic()
            result = reader.read(request, folder, refresh["sequence"], at)
            read_seconds, receipt = time.monotonic() - read_clock, result.get("receipt") or {}
            if result["state"] == "not_started":
                _log_read(route, cycle, request, "waiting", result["reason"], read_seconds, receipt)
                outcome.update(state="waiting", reason=result["reason"], next_retry_at=result.get("next_retry_at"))
                refresh.update(wait={"reason": result["reason"], "at": at})
                _save_refresh(root, route, refresh)
                break
            reads += 1
            relative = save_raw(root, route, at, refresh["sequence"], result["receipt"], result["body"])
            parsed_failed = False
            if result["state"] == "read":
                try:
                    parsed = reader.parse(request, load_raw(root, route, relative))
                    _apply_read(catalogue, cycle, request, parsed, at, relative, config,
                                authoritative=getattr(reader, "authoritative", True))
                except SafetyError as error:
                    result = {"state": "failed", "reason": str(error)}  # unparsable: wait, never re-read every round
                    catalogue = load_catalogue(root, route)
                    parsed_failed = True
            if result["state"] != "read":
                _log_read(route, cycle, request, "failed", result["reason"], read_seconds, receipt)
                if (cycle["kind"] == "categories" and request.get("role") == CATEGORY_ROLE
                        and (result["reason"] in _PAGE_ONLY or parsed_failed)):
                    # That one category page (empty, moved, changed or unreadable) is not
                    # asked again this week; it is no failure of the platform, so it never
                    # lengthens the platform's waits or holds the other categories.
                    cycle["queue"] = cycle["queue"][1:]
                    refresh.update(wait={"reason": "DROPSHIP_CATEGORY_PAGE_SKIPPED", "at": at})
                    _save_refresh(root, route, refresh)
                    outcome.update(state="waiting", reason=result["reason"], next_retry_at=None)
                    break
                _fail(refresh, at, result["reason"], wait_seconds=result.get("wait_seconds"), drop_cycle=result.get("drop_cycle", False))
                _save_refresh(root, route, refresh)
                outcome.update(state="waiting", reason=result["reason"], next_retry_at=refresh["blocked_until"])
                break
            _log_read(route, cycle, request, "read", None, read_seconds, receipt,
                      items=len(parsed["items"]), next_pages=len(parsed["follow"]))
            cycle["queue"] = parsed["follow"] + cycle["queue"][1:]
            refresh.update(failures=0, blocked_until=None, wait={})
            catalogue.update(updated_at=at, parser_version=reader.parser_version)
            save_catalogue(root, route, catalogue)
            _save_refresh(root, route, refresh)
            outcome["state"] = "reading"
        outcome["reads"] = reads
    return outcome


def _log_read(route, cycle, request, state, reason, seconds, receipt, *, items=None, next_pages=None):
    """One run-log row per platform page visit (a feed request or a signed-in
    page): which page, what happened, how long it took, how many items it
    showed and, for a signed-in page, whether it looked signed in. No address,
    page text or account detail is logged."""
    from .operations import record_event
    counts = {name: value for name, value in (("items_found", items), ("next_pages", next_pages)) if value is not None}
    record_event("platform_read", route=route, cycle_kind=cycle.get("kind"), page_kind=request.get("kind"),
                 record_kind=request.get("role"), state=state, reason=reason,
                 http_status=receipt.get("http_status"), signed_in=receipt.get("signed_in"),
                 timings={"read_seconds": seconds}, counts=counts)


def _apply_read(catalogue, cycle, request, parsed, at, relative, config, *, authoritative=True):
    # A signed-in parser names the page's list role under the current settings
    # (a list the owner made plain gives no role, also when saved reads are replayed).
    role = parsed["role"] if "role" in parsed else request.get("role")
    if parsed["collections"] is not None:
        cycle["collections"].extend(parsed["collections"])
    handle = request.get("handle")
    if handle is not None and handle not in cycle["handles"] and request.get("kind") != "product":
        cycle["handles"].append(handle)  # a product page is not a list: reading one never ends another's membership
    clean = [row for row in (clean_item(row, price_basis=config["price_basis"]) for row in parsed["items"]) if row]
    cycle["items_seen"] += merge_items(catalogue, clean, seen_at=at, source_ref=relative, handle=handle,
                                       role=role if role in ROLES else None,
                                       authoritative=authoritative)
    # Only a full cycle replaces the saved category list: a lists cycle reads
    # only the list pages, never the whole category list (a feed's lists cycle
    # gives none; a signed-in list page gives just its own).
    if parsed.get("collections_complete") and cycle.get("kind") == "full":
        unique = {}
        for row in cycle["collections"]:
            unique.setdefault(row["handle"], row)
        catalogue["collections"] = deepcopy(list(unique.values()))


def _complete(root, route, catalogue, refresh, at, outcome, reader):
    cycle = refresh["cycle"]
    config = platform_settings(route)
    if cycle["kind"] == "full" and not cycle["items_seen"]:
        # No item at all is not an empty platform: keep the old catalogue and look again tomorrow.
        refresh.update(cycle=None, blocked_until=(timestamp(at) + timedelta(hours=24)).isoformat(),
                       wait={"reason": "DROPSHIP_CATALOGUE_EMPTY", "at": at})
        _save_refresh(root, route, refresh)
        outcome.update(state="waiting", reason="DROPSHIP_CATALOGUE_EMPTY", next_retry_at=refresh["blocked_until"])
        return
    if cycle["kind"] == "full":
        _finish_full(catalogue, cycle["started_at"], complete=getattr(reader, "complete_catalogue", True), at=at,
                     stale_days=4 * config["full_refresh_days"])
        catalogue["full_completed_at"] = at
    if cycle["handles"]:
        _finish_lists(catalogue, set(cycle["handles"]), cycle["started_at"])
        if cycle["kind"] != "categories":  # category pages are no lists
            catalogue["lists_completed_at"] = at
    catalogue["updated_at"] = at
    save_catalogue(root, route, catalogue)
    refresh["completed"][cycle["kind"]] = {"started_at": cycle["started_at"], "completed_at": at,
                                           "items": len(catalogue["items"])}
    if cycle["kind"] == "full" and cycle["handles"]:
        refresh["completed"]["lists"] = {"started_at": cycle["started_at"], "completed_at": at,
                                         "items": len(catalogue["items"])}
    refresh.update(cycle=None, wait={})
    _save_refresh(root, route, refresh)
    prune_raw(root, route, at, config["raw_retention_days"])
    outcome["state"] = "completed_" + cycle["kind"]


def replay_platform(root, route, reader):
    """Rebuild the catalogue from the saved reads of the latest finished cycles.

    Used when the parser version changes: no platform is read again.
    """
    config, refresh = platform_settings(route), load_refresh(root, route)
    catalogue = _empty(route)
    cycles = [refresh["completed"][kind] for kind in ("full", "lists", "categories") if kind in refresh["completed"]]
    if not cycles:
        catalogue["parser_version"] = reader.parser_version
        save_catalogue(root, route, catalogue)
        return catalogue
    windows = sorted((timestamp(row["started_at"]), timestamp(row["completed_at"])) for row in cycles)
    full = refresh["completed"].get("full")
    full_window = (timestamp(full["started_at"]), timestamp(full["completed_at"])) if full else None
    replay = {"kind": None, "queue": [], "handles": [], "collections": [], "items_seen": 0}
    categories = {"kind": "categories", "queue": [], "handles": [], "collections": [], "items_seen": 0}
    for relative in raw_files(root, route):
        raw = load_raw(root, route, relative)
        receipt = raw["receipt"]
        try:
            read_at = timestamp(receipt["read_at"])
        except (KeyError, SafetyError):
            continue
        if receipt.get("state") != "read" or not any(start <= read_at <= end for start, end in windows):
            continue
        request = receipt["request"]
        parsed = reader.parse(request, raw)
        if parsed["collections"] is not None and request.get("page") == 1 and request.get("kind") == "collections":
            replay["collections"] = []
        # As when it was read: only the full cycle's reads replace the category list,
        # and a category page's membership ends only with its own weekly cycle.
        replay["kind"] = "full" if full_window and full_window[0] <= read_at <= full_window[1] else "lists"
        _apply_read(catalogue, categories if request.get("role") == CATEGORY_ROLE else replay, request, parsed,
                    receipt["read_at"], relative, config, authoritative=getattr(reader, "authoritative", True))
    if full:
        _finish_full(catalogue, full["started_at"], complete=getattr(reader, "complete_catalogue", True),
                     at=full["completed_at"], stale_days=4 * config["full_refresh_days"])
        catalogue["full_completed_at"] = full["completed_at"]
    lists = refresh["completed"].get("lists")
    if lists and replay["handles"]:
        _finish_lists(catalogue, set(replay["handles"]), lists["started_at"])
        catalogue["lists_completed_at"] = lists["completed_at"]
    weekly = refresh["completed"].get("categories")
    if weekly and categories["handles"]:
        _finish_lists(catalogue, set(categories["handles"]), weekly["started_at"])
    catalogue.update(parser_version=reader.parser_version,
                     updated_at=max((row["completed_at"] for row in cycles), key=timestamp))
    save_catalogue(root, route, catalogue)
    return catalogue


def item_receipt(root, route, item):
    """The saved read receipt behind an item's latest sighting."""
    return load_raw(root, route, item["source_ref"])["receipt"]


def body_json(raw):
    """A saved feed body (text) as JSON."""
    try:
        return json.loads(raw["body"])
    except (TypeError, ValueError):
        raise SafetyError("DROPSHIP_FEED_BODY_INVALID") from None

