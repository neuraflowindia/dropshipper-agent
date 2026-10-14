"""What wider discovery may read next (Plan R10 W8); choosing reads nothing.

Three lanes (``wider_discovery`` reads them):

* ``meta-advertisers``: the advertisers behind relevant reviewed ads of
  accepted types (a narrow-type, accepted, exact-product or relevant-family
  review of a Meta ad), newest review first. An advertiser is read again only
  after ``recheck_days``.
* ``trends-rising``: rising related queries saved with Google Trends reads
  (``trends_rising``; Breakout first, then the largest growth, newest read
  first), each used once as a Meta search. A query whose words equal an
  existing catalogue phrase or type name is already known.
* ``supplier-listings``: IndiaMART's and TradeIndia's results pages for the
  mapped category terms of ``config/research/wider_discovery.json``: a read
  still to happen first (never sent, or its browser read next), then the
  least recently read term (categories in turn), each again after
  ``recheck_days``.

Every query, term and advertiser's type passes the client-category gate of
the dropshipping platforms (``platform_roles.item_category``) before it is
chosen, so nothing outside the four client categories reaches a read or an AI
call. A failed, refused or uncertain read of a key is tried again only after
``retry_after_hours``; a read of unknown outcome or an unfinished or failed
original parks its key until ``recheck_days``; a site's refusal holds its
reads through the shared access wait of every lane (``wider_access``).
"""

from datetime import timedelta
from pathlib import Path
import re

from .discovery import normalise
from .models import SafetyError, timestamp
from .service import CATEGORIES
from .storage import atomic_json, load_json

SETTINGS = Path(__file__).resolve().parents[2] / "config/research/wider_discovery.json"
READ_KIND = "wider-discovery-read-v1"
LANES = ("meta-advertisers", "trends-rising", "supplier-listings")
SUPPLIER_ROUTES = ("indiamart", "tradeindia")
# Reviewed Meta ads come from the free read and the deeper Apify read (W11).
META_REVIEW_ROUTES = ("meta-ads", "meta-ads-depth")
CLOSED = ("read", "empty")
STATES = CLOSED + ("intent_saved", "not_started", "refused", "failed", "uncertain")
_RELEVANT = ("exact_product", "relevant_family")


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def validate_settings(value):
    """The saved wider-discovery settings, or ``WIDER_DISCOVERY_SETTINGS_INVALID``."""
    try:
        meta, rising, supplier, admission = (value["meta_advertisers"], value["trends_rising"],
                                             value["supplier_listings"], value["admission"])
        terms = supplier["terms"]
        if (set(value) != {"schema_version", "kind", "meta_advertisers", "trends_rising", "supplier_listings",
                           "admission", "retry_after_hours"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["kind"] != "wider-discovery-v1"
                or set(meta) != {"reads_per_round", "ads_per_read", "recheck_days"}
                or not _int(meta["reads_per_round"], 0, 5) or not _int(meta["ads_per_read"], 1, 30)
                or not _int(meta["recheck_days"], 1, 365)
                or set(rising) != {"reads_per_round", "cards_per_read", "max_age_days"}
                or not _int(rising["reads_per_round"], 0, 5) or not _int(rising["cards_per_read"], 1, 30)
                or not _int(rising["max_age_days"], 1, 90)
                or set(supplier) != {"reads_per_round", "listings_per_read", "recheck_days", "terms"}
                or set(supplier["reads_per_round"]) != set(SUPPLIER_ROUTES)
                or any(not _int(cap, 0, 5) for cap in supplier["reads_per_round"].values())
                or not _int(supplier["listings_per_read"], 1, 10) or not _int(supplier["recheck_days"], 1, 365)
                or set(terms) != set(CATEGORIES)
                or any(not isinstance(rows, list) or not rows or len(rows) > 20 for rows in terms.values())
                or set(admission) != {"max_pending_reads", "settle_after_days", "daily_reads"}
                or not _int(admission["max_pending_reads"], 1, 10) or not _int(admission["settle_after_days"], 1, 30)
                or not _int(admission["daily_reads"], 1, 500)
                or not _int(value["retry_after_hours"], 1, 720)):
            raise ValueError
        for category, rows in terms.items():
            for term in rows:
                # A mapped term is a route search phrase, and in its own category.
                if (not isinstance(term, str) or term != " ".join(term.split()) or not 2 <= len(term) <= 80
                        or not re.fullmatch(r"[A-Za-z0-9 &'-]+", term) or gate(term) != category):
                    raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError("WIDER_DISCOVERY_SETTINGS_INVALID") from None
    return value


def settings():
    return validate_settings(load_json(SETTINGS, 64 * 1024))


_CATEGORY_WORDS = {}


def _category_words():
    """W4's category words, read again only when its settings file changes."""
    from .dropship_platforms import SETTINGS as PLATFORM_SETTINGS, settings as platform_settings
    info = PLATFORM_SETTINGS.stat()
    stamp = (info.st_mtime_ns, info.st_size)
    if _CATEGORY_WORDS.get("stamp") != stamp:
        _CATEGORY_WORDS.update(stamp=stamp, value=platform_settings()["discovery"])
    return _CATEGORY_WORDS["value"]


def gate(text):
    """The client category a text fits by W4's category words, or None (also
    for the excluded kinds: jewellery, cosmetics, clothing, phone accessories ...)."""
    from .platform_roles import item_category
    if not isinstance(text, str) or not text.strip():
        return None
    return item_category({"title": text}, _category_words())


def reads(state):
    """Every saved wider-discovery read, oldest first."""
    return sorted((row for row in state["batch_runs"].values() if row.get("kind") == READ_KIND),
                  key=lambda row: (timestamp(row["started_at"]), row["id"]))


def latest_reads(rows):
    """The newest read of each (lane, key)."""
    return {(row["lane"], row["key"]): row for row in rows}


# Outcomes whose request is never asked again: a read of unknown outcome, or
# an unfinished or failed original. The key is parked like a finished one.
PARKED_REASONS = ("SOURCE_ORIGINAL_INCOMPLETE",)


def due(row, at, config, recheck_days):
    """Whether a key may be read now: never read, a read that never started, a
    finished or parked read (uncertain, or an unfinished or failed original)
    older than ``recheck_days`` (None: never again), or a failed or refused
    one older than ``retry_after_hours``."""
    if row is None or row["state"] == "not_started":
        return True
    age = timestamp(at) - timestamp(row["updated_at"])
    if row["state"] in CLOSED or row["state"] == "uncertain" or row.get("reason") in PARKED_REASONS:
        return recheck_days is not None and age >= timedelta(days=recheck_days)
    return age >= timedelta(hours=config["retry_after_hours"])


def known_words(state, catalogue):
    """Word sets already known: every catalogue phrase and every type's name."""
    from .platform_matching import words
    known = {frozenset(words(phrase["text"])) for family in catalogue["families"] for phrase in family["phrases"]}
    known |= {frozenset(words(concept["description"].get("label") or "")) for concept in state["concepts"].values()
              if concept.get("status") not in ("merged", "retired")}
    known.discard(frozenset())
    return known


def advertiser_targets(state, at, config, latest):
    """Advertisers of relevant reviewed Meta ads of accepted types, newest review first."""
    from .catalogue_discovery import _raise_shared_integrity
    from .market_validation import FUNCTIONAL_MATCH_METHOD, _functional_match
    from .research_records import SCOPED_EVIDENCE_KIND
    from .research_scope import validated_scoped_evidence
    lane = config["meta_advertisers"]
    concepts = sorted({row["concept_id"] for row in state["source_checks"].values()
                       if row.get("kind") == SCOPED_EVIDENCE_KIND and row.get("route") in META_REVIEW_ROUTES
                       and row.get("concept_id") in state["concepts"]})
    found = {}
    for concept_id in concepts:
        concept = state["concepts"][concept_id]
        category = concept["description"].get("category")
        if concept.get("status") in ("merged", "retired") or category not in CATEGORIES:
            continue
        try:
            rows = validated_scoped_evidence(state, concept_id, as_of=at)
        except SafetyError as error:
            _raise_shared_integrity(error)
            continue  # one type's unreadable reviews never hide the others' advertisers
        for row in rows:
            if (row["route"] not in META_REVIEW_ROUTES or row["scope"] != "narrow_type" or row["admissibility"] != "accepted"
                    or row["relationship"] not in _RELEVANT
                    or row["method"] not in (FUNCTIONAL_MATCH_METHOD, "operator_source_review")):
                continue
            for ref in row["references"]:
                observation = state["observations"].get(ref["id"]) if ref["table"] == "observations" else None
                advertiser = observation["facts"].get("advertiser_id") if observation else None
                if (observation is None or observation["source"] != "meta" or not isinstance(advertiser, str)
                        or not re.fullmatch(r"[0-9]{1,40}", advertiser) or not _functional_match(row, ref["id"])):
                    continue
                if advertiser not in found or timestamp(row["reviewed_at"]) > timestamp(found[advertiser]["reviewed_at"]):
                    found[advertiser] = {"reviewed_at": row["reviewed_at"], "concept_id": concept_id,
                                         "category": category, "ad_id": observation["facts"].get("ad_id")}
    targets = []
    for advertiser, basis in sorted(found.items(), key=lambda pair: (-timestamp(pair[1]["reviewed_at"]).timestamp(), pair[0])):
        if len(targets) >= lane["reads_per_round"]:
            break
        if due(latest.get(("meta-advertisers", advertiser)), at, config, lane["recheck_days"]):
            targets.append({"lane": "meta-advertisers", "route": "meta-ads", "key": advertiser,
                            "category": basis["category"], "request": {"advertiser_id": advertiser},
                            "basis": {"concept_id": basis["concept_id"], "ad_id": basis["ad_id"],
                                      "reviewed_at": basis["reviewed_at"]}})
    return targets


RISING_INDEX = "rising-index.json"


def _rising_record(path):
    """A saved rising-query list in compact form, or None when it holds no queries."""
    from .trends_rising import KIND, PARSER_VERSION
    try:
        record = load_json(path, 262144)
        if (not isinstance(record, dict) or record.get("kind") != KIND or record.get("parser_version") != PARSER_VERSION
                or record.get("state") != "read" or not isinstance(record.get("queries"), list)):
            return None
        timestamp(record["observed_at"])
    except (SafetyError, OSError, KeyError, TypeError, ValueError):
        return None  # an unreadable list is not a direction; its read's own replay reports it
    return {name: record.get(name) for name in ("term", "acquisition_id", "observed_at", "queries")}


def _rising_rows(root, at, max_age_days):
    """Saved rising-query lists of recent Trends reads. A small index of each
    file's size and change time (a local speed aid: an unreadable index is
    rebuilt) means only new or changed lists are read again."""
    from .trends_rising import FILE
    folder, index_path = Path(root) / "direct_trends", Path(root) / "wider_discovery" / RISING_INDEX
    if not folder.is_dir():
        return []
    try:
        saved = load_json(index_path, 16 * 1024 * 1024) if index_path.is_file() else {}
        saved = saved["files"] if isinstance(saved, dict) and saved.get("version") == 1 and isinstance(saved.get("files"), dict) else {}
    except (SafetyError, OSError):
        saved = {}
    files, rows = {}, []
    for path in sorted(folder.glob("*/" + FILE)):
        info = path.stat()
        stamp = [info.st_mtime_ns, info.st_size]
        old = saved.get(path.parent.name)
        record = old["record"] if isinstance(old, dict) and old.get("stamp") == stamp else _rising_record(path)
        files[path.parent.name] = {"stamp": stamp, "record": record}
        if record is not None and timedelta(0) <= timestamp(at) - timestamp(record["observed_at"]) <= timedelta(days=max_age_days):
            rows.append((path.parent, record))
    if files != saved:
        try:
            atomic_json(index_path, {"version": 1, "files": files})
        except OSError:
            pass  # the next round reads the lists again
    return rows


def rising_targets(root, state, at, config, latest, catalogue):
    """Rising related queries not yet known or used, in the client categories."""
    from .platform_matching import words
    from .trends_rising import rising_queries
    lane = config["trends_rising"]
    if not lane["reads_per_round"]:
        return []
    known, found = known_words(state, catalogue), {}
    for folder, record in _rising_rows(root, at, lane["max_age_days"]):
        for row in record["queries"]:
            text = " ".join(str(row.get("query") or "").split())
            query_key, found_words = normalise(text), frozenset(words(text))
            # A query covering a known phrase or type name is a variant of it, not a new direction.
            if (not found_words or any(word_set <= found_words for word_set in known) or len(text) > 120
                    or not due(latest.get(("trends-rising", query_key)), at, config, None)):
                continue
            category = gate(text)
            if category is None:
                continue
            rank = (row.get("growth_kind") != "breakout", -(row.get("growth_percent") or 0),
                    -timestamp(record["observed_at"]).timestamp(), query_key)
            if query_key not in found or rank < found[query_key]["rank"]:
                found[query_key] = {"rank": rank, "text": text, "category": category, "folder": folder,
                                    "term": record.get("term"), "acquisition_id": record.get("acquisition_id")}
    targets = []
    for row in sorted(found.values(), key=lambda value: value["rank"]):
        if len(targets) >= lane["reads_per_round"]:
            break
        try:
            # The official reader confirms the list still belongs to its saved read.
            current = rising_queries(row["folder"])
        except (SafetyError, OSError, KeyError, TypeError, ValueError):
            continue
        if not current or current.get("state") != "read" or row["text"] not in {
                " ".join(str(value.get("query") or "").split()) for value in current["queries"]}:
            continue
        targets.append({"lane": "trends-rising", "route": "meta-ads", "key": normalise(row["text"]),
                        "category": row["category"], "request": {"query": row["text"]},
                        "basis": {"trends_term": row["term"], "trends_acquisition_id": row["acquisition_id"]}})
    return targets


def supplier_targets(state, at, config, latest):
    """Mapped category terms per route, least recently read first, categories in turn."""
    lane = config["supplier_listings"]
    order = list(CATEGORIES)
    targets = []
    for route in SUPPLIER_ROUTES:
        rows = []
        for category, terms in lane["terms"].items():
            for index, term in enumerate(terms):
                term_key = route + ":" + normalise(term)
                previous = latest.get(("supplier-listings", term_key))
                if due(previous, at, config, lane["recheck_days"]):
                    # A read that has not happened yet (never sent, or its browser read
                    # comes next) goes first, then the term read longest ago.
                    waiting = previous is not None and previous["state"] == "not_started"
                    last = timestamp(previous["updated_at"]).timestamp() if previous and not waiting else 0
                    rows.append((not waiting, last, index, order.index(category), term_key, category, term))
        rows.sort()
        targets += [{"lane": "supplier-listings", "route": route, "key": row[4], "category": row[5],
                     "request": {"term": row[6]}, "basis": {}} for row in rows[:lane["reads_per_round"][route]]]
    return targets
