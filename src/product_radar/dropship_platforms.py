"""The four Indian dropshipping platforms (owner decision D1, Plan R10 W4).

DeoDap and BaapStore publish official Shopify feeds (``platform_feeds``);
Dropship India and Dropdash are read in the owner's signed-in research profile
(``platform_signed_in``). Every read is kept as a file under
``<store>/dropship_platforms/<route>/raw/`` and merged into one current
catalogue per platform; only matched, relevant items become research events.
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
            roles {role: {first_seen_at, last_seen_at, via (list handles)}}}

The collection list is also the product-map material for the bigger map
(W10): ``collections`` keeps every platform category with its roles and, where
the platform has one, its ``parent`` handle.

Refreshing (``advance_platform``) works through one saved cycle at a time: a
full catalogue at most every ``full_refresh_days`` and the role lists (new
arrivals, bestsellers, trending ...) every ``list_refresh_hours``, a few paced
requests per programme round. A catalogue counts only after one full cycle
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
ROUTES = ("deodap", "baapstore", "dropship-india", "dropdash")
FEED_ROUTES = ("deodap", "baapstore")
SIGNED_IN_ROUTES = ("dropship-india", "dropdash")
LABELS = {"deodap": "DeoDap", "baapstore": "BaapStore", "dropship-india": "Dropship India", "dropdash": "Dropdash"}
# A relevant item on any of these lists is on a bestseller / trending /
# high-demand list; "new_arrival" is the separate newly-launched signal.
LIST_ROLES = ("bestseller", "trending", "high_demand", "winning", "state_bestseller")
ROLES = LIST_ROLES + ("new_arrival",)
CATALOGUE_VERSION = "dropship-catalogue-v2"
REFRESH_VERSION = "dropship-refresh-v1"
SETTINGS = Path(__file__).resolve().parents[2] / "config/research/dropship_platforms.json"
MAX_CATALOGUE = 128 * 1024 * 1024
MAX_RAW = 32 * 1024 * 1024
# Consecutive refusals of a platform wait longer each time (5 minutes, 1 hour,
# 6 hours, then a day), like the other research routes' access waits.
BACKOFF_SECONDS = (300, 3600, 6 * 3600, 24 * 3600)
SHORT_SPACING_SECONDS = 10
_HANDLE = re.compile(r"[a-z0-9][a-z0-9-]{0,119}")
_TEXT_LIMIT = 500


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
            expected = common | ({"page_size", "lists"} if feed else {"product_path", "product_pages_per_day", "pages"})
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
                re.compile(row["product_path"])
                if (type(row["product_pages_per_day"]) is not int or not 0 <= row["product_pages_per_day"] <= 300
                        or not row["pages"] or not any(page.get("role") == "catalogue" for page in row["pages"])):
                    raise ValueError
                for page in row["pages"]:
                    if (set(page) != {"role", "url", "max_pages"} or page["role"] not in ROLES + ("catalogue",)
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
    return {"item_id": item_id, "title": title, "url": url, "images": images, "price": price, "mrp": mrp,
            "price_basis": price_basis, "in_stock": in_stock if type(in_stock) is bool else None,
            "vendor": _text(row.get("vendor"), 120), "product_type": _text(row.get("product_type"), 120),
            "tags": tags, "created_at": created, "terms": _text(row.get("terms"), 1000), "badges": badges}


def merge_items(catalogue, items, *, seen_at, source_ref, handle=None, role=None, authoritative=True):
    """Add or refresh items; a list page also records its membership and role.

    Only an authoritative read (a platform feed, which states every field) may
    clear a known value. A signed-in card or product page shows less: what it
    leaves out is unknown, not removed. List roles come from the page's own
    role and from the card's badge elements only (``badges``).
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
                                                     "images", "tags", "terms")
            for field in kept:
                if row.get(field) in (None, []):
                    value[field] = old.get(field)
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
    ``read(request, folder, sequence, at)`` and ``parse(request, raw)``.
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
            if refresh["blocked_until"] and timestamp(at) < timestamp(refresh["blocked_until"]):
                outcome.update(state="waiting", reason=refresh["wait"].get("reason"), next_retry_at=refresh["blocked_until"])
                break
            if refresh["cycle"] is None:
                kind = ("full" if _due(refresh["completed"], "full", config["full_refresh_days"] * 24, at) else
                        "lists" if catalogue["full_completed_at"] and _due(refresh["completed"], "lists",
                                                                           config["list_refresh_hours"], at) else None)
                if kind is None:
                    outcome["state"] = "current"
                    break
                plan = reader.plan_full(config, catalogue) if kind == "full" else reader.plan_lists(config, catalogue)
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
            result = reader.read(request, folder, refresh["sequence"], at)
            if result["state"] == "not_started":
                outcome.update(state="waiting", reason=result["reason"], next_retry_at=result.get("next_retry_at"))
                refresh.update(wait={"reason": result["reason"], "at": at})
                _save_refresh(root, route, refresh)
                break
            reads += 1
            relative = save_raw(root, route, at, refresh["sequence"], result["receipt"], result["body"])
            if result["state"] == "read":
                try:
                    parsed = reader.parse(request, load_raw(root, route, relative))
                    _apply_read(catalogue, cycle, request, parsed, at, relative, config,
                                authoritative=getattr(reader, "authoritative", True))
                except SafetyError as error:
                    result = {"state": "failed", "reason": str(error)}  # unparsable: wait, never re-read every round
                    catalogue = load_catalogue(root, route)
            if result["state"] != "read":
                _fail(refresh, at, result["reason"], wait_seconds=result.get("wait_seconds"), drop_cycle=result.get("drop_cycle", False))
                _save_refresh(root, route, refresh)
                outcome.update(state="waiting", reason=result["reason"], next_retry_at=refresh["blocked_until"])
                break
            cycle["queue"] = parsed["follow"] + cycle["queue"][1:]
            refresh.update(failures=0, blocked_until=None, wait={})
            catalogue.update(updated_at=at, parser_version=reader.parser_version)
            save_catalogue(root, route, catalogue)
            _save_refresh(root, route, refresh)
            outcome["state"] = "reading"
        outcome["reads"] = reads
    return outcome


def _apply_read(catalogue, cycle, request, parsed, at, relative, config, *, authoritative=True):
    if parsed["collections"] is not None:
        cycle["collections"].extend(parsed["collections"])
    handle = request.get("handle")
    if handle is not None and handle not in cycle["handles"] and request.get("kind") != "product":
        cycle["handles"].append(handle)  # a product page is not a list: reading one never ends another's membership
    clean = [row for row in (clean_item(row, price_basis=config["price_basis"]) for row in parsed["items"]) if row]
    cycle["items_seen"] += merge_items(catalogue, clean, seen_at=at, source_ref=relative, handle=handle,
                                       role=request.get("role") if request.get("role") != "catalogue" else None,
                                       authoritative=authoritative)
    if parsed.get("collections_complete"):
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
    cycles = [refresh["completed"][kind] for kind in ("full", "lists") if kind in refresh["completed"]]
    if not cycles:
        catalogue["parser_version"] = reader.parser_version
        save_catalogue(root, route, catalogue)
        return catalogue
    windows = sorted((timestamp(row["started_at"]), timestamp(row["completed_at"])) for row in cycles)
    replay = {"queue": [], "handles": [], "collections": [], "items_seen": 0}
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
        _apply_read(catalogue, replay, request, parsed, receipt["read_at"], relative, config,
                    authoritative=getattr(reader, "authoritative", True))
    full = refresh["completed"].get("full")
    if full:
        _finish_full(catalogue, full["started_at"], complete=getattr(reader, "complete_catalogue", True),
                     at=full["completed_at"], stale_days=4 * config["full_refresh_days"])
        catalogue["full_completed_at"] = full["completed_at"]
    lists = refresh["completed"].get("lists")
    if lists and replay["handles"]:
        _finish_lists(catalogue, set(replay["handles"]), lists["started_at"])
        catalogue["lists_completed_at"] = lists["completed_at"]
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

