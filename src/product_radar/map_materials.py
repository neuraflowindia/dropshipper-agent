"""What the bigger product map is built from (Plan R10 W10, owner decision D8).

Five kinds of saved material, all read from files the programme already keeps
(nothing here reads a website or calls the AI):

* ``platform-collection``: a dropshipping platform's own categories (DeoDap's
  and BaapStore's collection trees, the signed-in platforms' category links)
  and its product types with at least ``product_type_min_items`` items;
* ``platform-item``: a platform catalogue item (list items first: bestsellers,
  trending, new arrivals ...);
* ``trends-rising``: a rising related query saved with a Google Trends read;
* ``meta-advertiser``: another active ad of a relevant advertiser (W8's read);
* ``supplier-listing``: an IndiaMART or TradeIndia listing found under a mapped
  category term (W8's read).

Each candidate is looked at once (``advance_staging``):

* Client-category gate: W4's excluded kinds (jewellery, cosmetics, clothing,
  phone accessories, pets ...) never pass (``platform_roles.excluded_kind``);
  the candidate must fit one of the four client categories by W4's category
  words or by the map's own sub-area words.
* One-word wording ("Lights", "Toys") is too broad to stage or to mark
  anything as known.
* Known: its words include all the words of a heading, alias, phrase or type
  (two words at least; ``wider_candidates.covers``, shared with W8).
* Variant: its words include those of staged material still waiting for its
  AI decision; it is looked at again once that decision is saved, so a
  declined example never hides its variants.
* Routed to one sub-area (``route``): within W4's category when W4 names one;
  the best sub-area needs a routing-word or heading hit and a clear margin.
  Otherwise ``no_sub_area``, looked at again once the map has grown by
  ``reexamine_after_headings`` headings.
* A routed candidate is saved once as seed material for the seed C call
  (``catalogue_seed``): a small page holding the exact source line. A full
  sub-area (``headings_per_sub_area``) takes no more.

Which candidates were looked at, and the compact list of fresh signals the
heading order reads (``heading_order``), are small local files under
``product_map/`` (speed aids: rebuilt when malformed; an unreadable file only
makes the round wait, it is never overwritten).
"""

from collections import Counter
from datetime import timedelta
from pathlib import Path
import re

from .discovery import normalise
from .models import SafetyError, digest, timestamp
from .research_memory import key
from .storage import Journal, atomic_json, load_json, safe_path

SETTINGS = Path(__file__).resolve().parents[2] / "config/research/product_map.json"
KINDS = ("platform-collection", "platform-item", "trends-rising", "meta-advertiser", "supplier-listing")
ORIGIN = "product_map"
FOLDER = "product_map"
SIGNALS = "signals.json"
ROWS_VERSION = "product-map-rows-v1"  # the candidate row shape the speed-aid files hold
_ROWS_LIMIT = 64 * 1024 * 1024
_FILE_LIMIT = 16 * 1024 * 1024
EXAMINED_VERSION = 2
# Routing: the best sub-area's score must beat the next by this much.
ROUTE_MARGIN = 2


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def validate_settings(value):
    """The saved product-map settings, or ``PRODUCT_MAP_SETTINGS_INVALID``."""
    from .catalogue import load_catalogue
    try:
        build, renewal, order, words = value["build"], value["renewal"], value["order"], value["branch_words"]
        branches = {row["id"] for row in load_catalogue()["branches"] if row["parent_id"] is not None}
        if (set(value) != {"schema_version", "kind", "target", "build", "renewal", "order", "branch_words"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["kind"] != "product-map-v1" or set(value["target"]) != {"headings_per_sub_area"}
                or not _int(value["target"]["headings_per_sub_area"], 1, 200)
                or set(build) != {"materials_per_call", "stage_per_round", "unbuilt_per_branch", "examine_per_round",
                                  "product_type_min_items", "retry_minutes", "min_batch", "reexamine_after_headings"}
                or not _int(build["min_batch"], 1, 10) or not _int(build["reexamine_after_headings"], 1, 1000)
                or not _int(build["materials_per_call"], 1, 10) or not _int(build["stage_per_round"], 1, 200)
                or not _int(build["unbuilt_per_branch"], 1, 200) or not _int(build["examine_per_round"], 1, 5000)
                or not _int(build["product_type_min_items"], 1, 1000) or not _int(build["retry_minutes"], 1, 1440)
                or set(renewal) != {"concepts_per_call", "retry_minutes"} or not _int(renewal["concepts_per_call"], 1, 5)
                or not _int(renewal["retry_minutes"], 1, 1440)
                or set(order) != {"fresh_signal_days", "rising_max_age_days"}
                or not _int(order["fresh_signal_days"], 1, 90) or not _int(order["rising_max_age_days"], 1, 90)
                # Every sub-area of the catalogue has its routing words, and only those.
                or set(words) != branches):
            raise ValueError
        for rows in words.values():
            if (not isinstance(rows, list) or not rows or len(rows) > 40 or any(
                    not isinstance(word, str) or word != " ".join(word.split()) or not 2 <= len(word) <= 40
                    or not re.fullmatch(r"[a-z0-9 '-]+", word) for word in rows)):
                raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError("PRODUCT_MAP_SETTINGS_INVALID") from None
    return value


def settings():
    return validate_settings(load_json(SETTINGS, 64 * 1024))


def _clip(text, limit=160):
    """The first line of a text, at most ``limit`` characters, cut at a word."""
    line = next((" ".join(row.split()) for row in str(text or "").splitlines() if row.strip()), "")
    if len(line) <= limit:
        return line
    cut = line[:limit].rsplit(" ", 1)[0]
    return cut or line[:limit]


def excluded(text):
    """Whether a text names one of W4's excluded kinds (jewellery, cosmetics, pets ...)."""
    from .platform_roles import excluded_kind
    from .wider_candidates import category_words
    return excluded_kind(text, category_words())


def _row(kind, row_key, label, url, title, checked_at, line, reference, *, context="", tags=(), rank=1, signal_at=None):
    """One candidate: ``route_text`` (its words and its platform product type)
    places it; ``gate_text`` (also the platform tags) is checked for excluded kinds."""
    route_text = " ".join(part for part in (label, context) if part)
    return {"kind": kind, "key": row_key, "label": label, "route_text": route_text,
            "gate_text": " ".join([route_text, *tags]), "url": url, "title": title, "checked_at": checked_at,
            "lines": [line], "reference": reference, "rank": rank, "signal_at": signal_at}


def _platform_rows(root, route, min_items):
    """One platform catalogue's categories, product types and items."""
    from .dropship_platforms import LABELS, LIST_ROLES, load_catalogue
    catalogue = load_catalogue(root, route)
    # The original is the platform's own page (its URL): the saved catalogue is
    # rewritten by each refresh, so the staged page keeps the exact line itself.
    label = LABELS[route]
    at = catalogue["updated_at"]
    if at is None:
        return []
    rows, titles = [], {row["handle"]: row["title"] for row in catalogue["collections"]}
    for row in catalogue["collections"]:
        if row["roles"]:
            continue  # bestseller and trending lists are lists, not product areas
        parent = titles.get(row["parent"]) if row.get("parent") else None
        line = label + " category: " + row["title"] + (" (within " + parent + ")" if parent else "")
        rows.append(_row("platform-collection", route + ":collection:" + row["handle"], row["title"],
                         row["url"], label + " categories", at, line, row["url"], rank=0))
    types = Counter(item["product_type"] for item in catalogue["items"].values() if item.get("product_type"))
    examples = {}
    for item in catalogue["items"].values():
        examples.setdefault(item.get("product_type"), item["url"])
    for product_type, count in sorted(types.items()):
        if count >= min_items:
            rows.append(_row("platform-collection", route + ":type:" + normalise(product_type), product_type,
                             examples[product_type], label + " product types", at,
                             label + " product type: " + product_type, examples[product_type], rank=1))
    for item_id, item in catalogue["items"].items():
        roles = sorted(item["roles"])
        listed = [role for role in roles if role in LIST_ROLES]
        signal = max((dates["first_seen_at"] for dates in item["roles"].values()), key=timestamp, default=None)
        line = (label + " product: " + item["title"]
                + (" (product type: " + item["product_type"] + ")" if item.get("product_type") else "")
                + ("; on its " + ", ".join(role.replace("_", " ") for role in roles) + " list" if roles else ""))
        rows.append(_row("platform-item", route + ":item:" + item_id, item["title"], item["url"],
                         label + " catalogue", item["last_seen_at"], line, item["url"],
                         context=item.get("product_type") or "", tags=item.get("tags") or (),
                         rank=0 if listed else 1 if roles else 2, signal_at=signal))
    return rows


def _cached(memory, name, stamp, build):
    """Rows for one unchanged source, from the local speed-aid file when it matches."""
    path = safe_path(memory.root, FOLDER) / (name + ".json")
    try:
        saved = load_json(path, _ROWS_LIMIT) if path.is_file() else None
        if isinstance(saved, dict) and saved.get("stamp") == stamp and isinstance(saved.get("rows"), list):
            return saved["rows"]
    except (SafetyError, OSError):
        pass  # rebuilt below
    rows = build()
    try:
        atomic_json(path, {"version": 1, "stamp": stamp, "rows": rows})
    except OSError:
        pass  # the next round builds them again
    return rows


def _platforms(memory, config):
    from .dropship_platforms import ROUTES, platform_folder
    rows = []
    for route in ROUTES:
        path = platform_folder(memory.root, route) / "catalogue.json"
        if not path.is_file():
            continue
        info = path.stat()
        stamp = [ROWS_VERSION, info.st_mtime_ns, info.st_size, config["build"]["product_type_min_items"]]
        rows += _cached(memory, "rows-" + route, stamp,
                        lambda: _platform_rows(memory.root, route, config["build"]["product_type_min_items"]))
    return rows


def _rising(memory, at, config):
    from .trends_rising import FILE
    from .wider_candidates import rising_rows
    rows = []
    for folder, record in rising_rows(memory.root, at, config["order"]["rising_max_age_days"]):
        url = "https://trends.google.com/trends/explore?geo=IN&q=" + "+".join(str(record["term"] or "").split())
        for query in record["queries"]:
            text = " ".join(str(query.get("query") or "").split())
            if not text or len(text) > 120:
                continue
            line = ("Google Trends rising search related to " + str(record["term"]) + ": " + text
                    + " (" + str(query.get("raw_value")) + ")")
            rows.append(_row("trends-rising", normalise(text), text, url,
                             "Google Trends rising searches for " + str(record["term"]), record["observed_at"], line,
                             str(folder / FILE), rank=0 if query.get("growth_kind") == "breakout" else 1,
                             signal_at=record["observed_at"]))
    return rows


def _wider(memory, state, lane):
    """Rows from W8's finished reads of one lane, each read's file loaded once."""
    from .wider_candidates import reads
    from .wider_discovery import _LABELS, nomination_path
    done = [row for row in reads(state) if row["lane"] == lane and row["state"] == "read" and row.get("capture_id")]
    path = safe_path(memory.root, FOLDER) / ("rows-" + lane + ".json")
    try:
        saved = load_json(path, _ROWS_LIMIT) if path.is_file() else {}
        saved = saved.get("reads") if isinstance(saved, dict) and saved.get("version") == ROWS_VERSION else {}
        saved = saved if isinstance(saved, dict) else {}
    except (SafetyError, OSError):
        saved = {}
    found = {}
    for record in done:
        aid = record["acquisition_id"]
        if isinstance(saved.get(aid), list):
            found[aid] = saved[aid]
            continue
        try:
            nomination = load_json(nomination_path(record), 8 * 1024 * 1024)
        except (SafetyError, OSError):
            continue  # its read reports its own unreadable file
        rows, reference, at = [], str(nomination_path(record)), nomination.get("observed_at")
        for item in nomination.get("rows", []):
            if lane == "meta-advertisers":
                body = _clip(item.get("presentation", {}).get("body"))
                if body:
                    rows.append(_row("meta-advertiser", str(item["ad_id"]), body, item["library_url"],
                                     "Meta Ad Library: active ads of a relevant advertiser", at,
                                     "Active Meta ad of an advertiser of a reviewed product: " + body, reference,
                                     signal_at=at))
            else:
                title, site = item["semantic"]["title"], _LABELS[record["route"]]
                rows.append(_row("supplier-listing", record["route"] + ":" + item["facts"]["url"], title,
                                 item["facts"]["url"], site + " results for " + item["facts"]["query"], at,
                                 site + " supplier listing under '" + item["facts"]["query"] + "': " + title, reference))
        found[aid] = rows
    if found != saved:
        try:
            atomic_json(path, {"version": ROWS_VERSION, "reads": found})
        except OSError:
            pass
    return [row for rows in found.values() for row in rows]


def collect_rows(memory, state, at, config):
    """Every candidate row: the platforms' categories and product types first
    (broad areas, so their items' variants are known), then the other kinds
    taking turns; within a kind list items and breakouts first, shorter (more
    general) wording first, newest first."""
    groups = {kind: [] for kind in KINDS}
    for row in (_platforms(memory, config) + _rising(memory, at, config)
                + _wider(memory, state, "meta-advertisers") + _wider(memory, state, "supplier-listings")):
        groups[row["kind"]].append(row)
    for rows in groups.values():
        rows.sort(key=lambda row: (row["rank"], len(row["label"].split()), -timestamp(row["checked_at"]).timestamp(),
                                   row["key"]))
    ordered, index, others = groups.pop("platform-collection"), 0, list(groups.values())
    while any(index < len(rows) for rows in others):
        ordered += [rows[index] for rows in others if index < len(rows)]
        index += 1
    return ordered


def word_set(text):
    from .platform_matching import words
    return frozenset(words(text))


def catalogue_sets(state, catalogue):
    """Word sets already in the map (two words at least): headings, aliases,
    phrases and type names. One word ("toys") is too broad to mark anything known."""
    from .wider_candidates import known_words
    sets = set(known_words(state, catalogue))
    sets |= {word_set(text) for family in catalogue["families"] for text in [family["label"], *family["aliases"]]}
    return {found for found in sets if len(found) >= 2}


def branch_profiles(catalogue, config):
    """Per sub-area: its category, routing word sets, heading sets and vocabulary."""
    profiles = {}
    for branch in catalogue["branches"]:
        if branch["parent_id"] is None:
            continue
        families = [row for row in catalogue["families"] if row["parent_id"] == branch["id"]]
        labels = [word_set(text) for family in families for text in [family["label"], *family["aliases"]]]
        profiles[branch["id"]] = {"category": branch["category"],
            "words": [word_set(text) for text in config["branch_words"].get(branch["id"], [])],
            "labels": [found for found in labels if found], "vocabulary": frozenset().union(*labels) if labels else frozenset(),
            "headings": sum(row["disposition"] == "usable" for row in families)}
    return profiles


def _scores(found, profiles, category, strong):
    scores = []
    for branch_id, profile in profiles.items():
        if category is not None and profile["category"] != category:
            continue
        hits = (2 * sum(len(words) for words in profile["words"] if words and words <= found and (len(words) > 1 or not strong))
                + 3 * sum(len(words) for words in profile["labels"] if words <= found and (len(words) > 1 or not strong)))
        score = hits + (0 if strong else len(found & profile["vocabulary"]))
        if score:
            scores.append((hits, score, branch_id))
    scores.sort(reverse=True)
    return scores


def _best(scores):
    """The best (hits, score, sub-area): routing-word and heading hits decide, by
    a clear margin; shared vocabulary only orders equally strong sub-areas."""
    if not scores or not scores[0][0] or len(scores) > 1 and scores[0][0] - scores[1][0] < ROUTE_MARGIN:
        return None
    return scores[0]


def route(found, profiles, category=None):
    """The sub-area a text belongs to by its words, or None.

    Within ``category`` when one is named (W4's gate), unless another
    category's multi-word routing words or headings match at least as strongly
    ("table tennis" over "table"), or its routing words clearly more strongly
    ("piping bag ... icing" over "bag"). The best sub-area needs routing-word or
    whole-heading hits at least ``ROUTE_MARGIN`` above the next sub-area's;
    anything closer is left for later."""
    inside = _best(_scores(found, profiles, category, False))
    if category is None:
        return inside[2] if inside else None
    strength = inside[0] if inside else 0
    for across, needed in ((_best(_scores(found, profiles, None, True)), strength),
                           (_best(_scores(found, profiles, None, False)), strength + ROUTE_MARGIN)):
        if across and profiles[across[2]]["category"] != category and across[0] >= needed:
            return across[2]
    return inside[2] if inside else None


def stage_material(memory, branch, row):
    """Save one candidate as seed material: its exact source line on a small page.

    The same contract as ``catalogue_seed.stage_page_materials`` (the material
    file, its page and the example's source reference), with the page holding
    the saved source line instead of a whole fetched page.
    """
    from .catalogue_seed import MAX_SAVED_BYTES, VERSION
    text = "\n".join(row["lines"])
    if normalise(row["label"]) not in normalise(text):
        raise SafetyError("CATALOGUE_SEED_EXCERPT_NOT_SUPPLIED")
    receipt = {"schema_version": 1, "requested_url": row["url"], "final_url": row["url"],
               "checked_at": row["checked_at"], "title": row["title"], "text": text,
               "origin": {"kind": ORIGIN, "source": row["kind"], "key": row["key"]}}
    page_id = key("catalogue-material-page", [row["url"], row["title"], text])
    folder = safe_path(memory.root, "catalogue_materials", "pages", page_id)
    path = folder / "receipt.json"
    with Journal(folder).locked(timeout=5):
        if not path.exists():
            atomic_json(path, {"receipt": receipt, "reference": row["reference"], "hash": digest(receipt)})
        saved = load_json(path, MAX_SAVED_BYTES)
        if saved.get("hash") != digest(saved.get("receipt")) or saved["receipt"].get("text") != text:
            raise SafetyError("CATALOGUE_SEED_MATERIAL_CHANGED")
        receipt = saved["receipt"]
    material_id = key("catalogue-material", [VERSION, branch["id"], page_id, normalise(row["label"])])
    value = {"schema_version": 1, "version": VERSION, "id": material_id, "branch_id": branch["id"],
             "category": branch["category"], "label": row["label"].strip(), "basis": "observed",
             "source": {"id": key("catalogue-material-example", [page_id, normalise(row["label"])]),
                        "url": receipt["final_url"], "title": receipt["title"], "checked_at": receipt["checked_at"],
                        "excerpts": [row["label"].strip()]},
             "page_reference": str(path), "receipt_hash": saved["hash"], "original_reference": saved["reference"]}
    destination = safe_path(memory.root, "catalogue_materials", "items", material_id) / "material.json"
    if not destination.exists():
        atomic_json(destination, value)
    return value


def load_examined(memory, config_hash):
    """Which candidates were looked at (row name -> outcome), or None when the
    file cannot be read now (the round waits; the file is never overwritten).
    A malformed file is rebuilt; another configuration keeps only staged
    material and its variants (everything else is looked at again)."""
    path = safe_path(memory.root, FOLDER) / "examined.json"
    try:
        saved = load_json(path, _FILE_LIMIT) if path.is_file() else {}
    except OSError:
        return None
    except SafetyError as error:
        if str(error) == "SAVED_FILE_UNAVAILABLE":
            return None  # a read that failed now: the round waits
        return {}  # malformed: rebuilt; staging again finds the same material IDs
    rows = saved.get("rows") if isinstance(saved, dict) and saved.get("version") == EXAMINED_VERSION else None
    if not isinstance(rows, dict):
        return {}
    if saved.get("config") != config_hash:
        return {name: row for name, row in rows.items() if isinstance(row, dict)
                and row.get("outcome") in ("staged", "variant")}
    return rows


def save_examined(memory, config_hash, rows):
    try:
        atomic_json(safe_path(memory.root, FOLDER) / "examined.json",
                    {"version": EXAMINED_VERSION, "config": config_hash, "rows": rows})
    except OSError:
        pass


def built_material_ids(state):
    """Seed material with a finished admission (a resumable failure is not finished)."""
    from .catalogue_seed import VERSION
    return {row["material_id"] for row in state["catalogue_additions"].values()
            if row.get("seed_version") == VERSION and row.get("material_id") and not row.get("resumable", False)}


def staged_materials(examined):
    return {row["material_id"]: row["branch_id"] for row in examined.values()
            if isinstance(row, dict) and row.get("outcome") == "staged"}


def _reopen(examined, built, headings, config):
    """Look again at variants whose material now has its AI decision, and at
    unrouted candidates once the map has grown enough."""
    grown = config["build"]["reexamine_after_headings"]
    return {name: row for name, row in examined.items()
            if not (row.get("outcome") == "variant" and row.get("of") in built)
            and not (row.get("outcome") == "no_sub_area" and headings >= row.get("headings", 0) + grown)}


def advance_staging(memory, state, catalogue, rows, config, config_hash):
    """Look at new candidates (within the round's limits) and stage the useful
    ones; returns (summary, examined), examined None when its file cannot be read."""
    from .catalogue_seed import catalogue_branch
    from .wider_candidates import covers, gate, word_index
    examined = load_examined(memory, config_hash)
    if examined is None:
        return {"looked_at": 0, "outcomes": {}, "failures": [], "waiting": "PRODUCT_MAP_EXAMINED_UNREADABLE"}, None
    profiles = branch_profiles(catalogue, config)
    headings = sum(profile["headings"] for profile in profiles.values())
    built = built_material_ids(state)
    reopened = _reopen(examined, built, headings, config)
    changed, examined = reopened != examined, reopened
    waiting = {row["material_id"]: frozenset(row["words"]) for row in examined.values()
               if row.get("outcome") == "staged" and row["material_id"] not in built}
    backlog = Counter(row["branch_id"] for row in examined.values()
                      if row.get("outcome") == "staged" and row["material_id"] in waiting)
    known = word_index(catalogue_sets(state, catalogue))
    pending = {}
    for material_id, found in waiting.items():
        pending.setdefault(min(found), []).append((found, material_id))
    outcomes, failures, looked, visited, count = Counter(), [], 0, 0, 0
    build, cap = config["build"], config["target"]["headings_per_sub_area"]
    for row in rows:
        name = row["kind"] + ":" + row["key"]
        if name in examined:
            continue
        # Recorded look-ats count against the round's limit; a bounded number of
        # candidates for a busy sub-area may be passed over on the way.
        if looked >= build["examine_per_round"] or count >= build["stage_per_round"] or visited >= 4 * build["examine_per_round"]:
            break
        visited += 1
        found = word_set(row["label"])
        category = None if not found or excluded(row["gate_text"]) else gate(row["gate_text"])
        branch_id = None if not found or excluded(row["gate_text"]) else route(
            found | word_set(row["route_text"]), profiles, category)
        variant_of = next((material_id for word in found for words, material_id in pending.get(word, ())
                           if words <= found), None) if len(found) >= 2 else None
        if branch_id is None and category is None:
            entry = {"outcome": "outside_categories"}
        elif len(found) < 2:
            entry = {"outcome": "single_word"}
        elif covers(found, known):
            entry = {"outcome": "known"}
        elif variant_of is not None:
            entry = {"outcome": "variant", "of": variant_of}
        elif branch_id is None:
            entry = {"outcome": "no_sub_area", "headings": headings}
        elif profiles[branch_id]["headings"] >= cap:
            entry = {"outcome": "sub_area_full"}
        elif backlog[branch_id] >= build["unbuilt_per_branch"]:
            outcomes["sub_area_backlog"] += 1
            continue  # looked at again once the sub-area's waiting material is built
        else:
            try:
                material = stage_material(memory, catalogue_branch(catalogue, branch_id), row)
            except (SafetyError, OSError) as error:
                from .catalogue_discovery import _raise_shared_integrity
                _raise_shared_integrity(error)
                failures.append({"key": name, "reason": str(error) if isinstance(error, SafetyError)
                                 else "PRODUCT_MAP_STAGING_LOCAL_FAILURE"})
                continue
            entry = {"outcome": "staged", "material_id": material["id"], "branch_id": branch_id,
                     "kind": row["kind"], "words": sorted(found)}
            pending.setdefault(min(found), []).append((found, material["id"]))
            backlog[branch_id] += 1
            count += 1
        examined[name] = entry
        outcomes[entry["outcome"]] += 1
        looked += 1
    if looked or changed:
        save_examined(memory, config_hash, examined)
    return {"looked_at": looked, "outcomes": dict(outcomes), "failures": failures}, examined


def write_signals(memory, rows, at, config):
    """The compact fresh-signal list the heading order reads: list items, rising
    queries and advertisers' ads seen within ``fresh_signal_days``."""
    window = timedelta(days=config["order"]["fresh_signal_days"])
    signals = sorted({(" ".join(sorted(word_set(row["label"]))), row["signal_at"], row["kind"]) for row in rows
                      if row.get("signal_at") and not excluded(row["gate_text"])
                      and timedelta(0) <= timestamp(at) - timestamp(row["signal_at"]) <= window})
    value = {"version": 1, "rows": [{"words": text.split(), "at": when, "kind": kind}
                                    for text, when, kind in signals if text]}
    path = safe_path(memory.root, FOLDER) / SIGNALS
    try:
        if not path.is_file() or load_json(path, _FILE_LIMIT) != value:
            atomic_json(path, value)
    except (SafetyError, OSError):
        pass  # the heading order then works without fresh signals this round
    return len(value["rows"])


def load_signals(root):
    """Saved fresh signals, or none when the file is missing or unreadable."""
    try:
        path = safe_path(root, FOLDER) / SIGNALS
        value = load_json(path, _FILE_LIMIT) if path.is_file() else None
        rows = value.get("rows") if isinstance(value, dict) and value.get("version") == 1 else []
        return [(frozenset(row["words"]), row["at"]) for row in rows if isinstance(row, dict)
                and isinstance(row.get("words"), list) and isinstance(row.get("at"), str)]
    except (SafetyError, OSError, TypeError):
        return []


def verify_origin(origin):
    """A map page's origin names its material kind and source key."""
    if (not isinstance(origin, dict) or set(origin) != {"kind", "source", "key"} or origin["kind"] != ORIGIN
            or origin["source"] not in KINDS or not isinstance(origin["key"], str) or not origin["key"]):
        raise SafetyError("CATALOGUE_SEED_MATERIAL_CHANGED")
