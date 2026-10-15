"""Crowding facts read from saved Amazon India and Flipkart first pages (W6).

Parser version 3 re-reads a saved capture's rendered page and page text: per
result card on the first results page only, the ratings count, star rating,
brand/seller text where the card shows one and whether it shows one
(``brand_shown``), "bought in past month" (Amazon),
the sponsored label and Amazon's "Best seller" badge; per page, the total result
count. Only the cards the capture kept are later counted (the first 30). It never navigates,
calls a model or changes the immutable capture receipt. Its output is a
derived side file next to the capture, bound to the hashes of the saved bytes,
so a newer parser version simply reads the saved page again.

Brands (checked on the saved first pages, 15 October): Amazon shows a brand or
store only on some cards, as a small heading above the title (about 12% of
saved cards); Flipkart's search cards show no brand or seller field at all
(the brand is only the start of the title and is not split off by guesswork).
A card without one is recorded as "brand not shown", never as no brand.
"""

from hashlib import sha256
from html.parser import HTMLParser
import re

from .models import SafetyError
from .storage import atomic_json, load_json, safe_path

CARD_PARSER_VERSION = 3
SIDE_FILE = "crowding-cards-v" + str(CARD_PARSER_VERSION) + ".json"
CARD_ROUTES = ("amazon-search", "flipkart-search", "amazon-bestsellers")
MAX_HTML = 8 * 1024 * 1024
MAX_SNAPSHOT = 2 * 1024 * 1024
MAX_SIDE = 2 * 1024 * 1024
MAX_CARDS = 120
_VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                   "source", "track", "wbr"))
_HIDDEN = frozenset(("script", "style", "noscript", "template"))
_ASIN = re.compile(r"[A-Z0-9]{10}")
_PID = re.compile(r"[A-Z0-9]{6,40}")
_COUNT = r"([0-9][0-9,]*)"
_RATINGS = (re.compile(r"^" + _COUNT + r" ratings?$", re.I),                       # Amazon search aria-label
            re.compile(r"out of 5 stars, " + _COUNT + r" ratings?$", re.I),         # Amazon list aria-label
            re.compile(r"^" + _COUNT + r" Ratings?$", re.I),                        # Flipkart list layout
            re.compile(r"^\(" + _COUNT + r"\)$"))                                   # Flipkart grid "(1,401)"
_COMPACT = re.compile(r"^\(([0-9]+(?:\.[0-9])?)([KM])\)$")                           # Amazon "(1.2K)"
_STARS = re.compile(r"\b([0-5](?:\.[0-9])?) out of 5 stars\b")
_GRID_STARS = re.compile(r"^([0-5](?:\.[0-9])?)$")
_BADGE = re.compile(r"^(?:#1 )?best\s?seller\b", re.I)                               # Amazon "Best seller in ..."
_BOUGHT = re.compile(r"^([0-9]+(?:\.[0-9])?)([KM]?)\+ bought in past month$", re.I)
_TOTAL = (re.compile(r"\b1\s*[-–]\s*[0-9]+ of (over )?" + _COUNT + r" results? for\b"),
          re.compile(r"\bShowing 1\s*[-–]\s*[0-9]+ of " + _COUNT + r" results? for\b"),
          re.compile(r"(?:^|\n)(over )?" + _COUNT + r" results? for\b"))


def _number(digits, unit=""):
    value = int(digits.replace(",", "")) if unit == "" else int(round(float(digits) * {"K": 1000, "M": 1000000}[unit.upper()]))
    return value if value <= 10 ** 10 else None


class _Cards(HTMLParser):
    """Collect each result card's visible text pieces and accessible labels."""

    def __init__(self, route):
        super().__init__(convert_charrefs=True)
        self.route, self.stack, self.cards, self.card, self.hidden, self.brand_depth = route, [], [], None, 0, None

    def _root(self, attrs):
        if self.route == "amazon-search":
            # The same card roots the capture reader saved (".s-result-item[data-asin]"),
            # including results shown inside an in-page carousel.
            identity = attrs.get("data-asin") if "s-result-item" in attrs.get("class", "").split() else None
            return identity if identity and _ASIN.fullmatch(identity) else None
        if self.route == "amazon-bestsellers":
            identity = attrs.get("data-asin")
            return identity if identity and _ASIN.fullmatch(identity) else None
        identity = attrs.get("data-id")
        return identity if identity and _PID.fullmatch(identity) else None

    def handle_starttag(self, tag, attrs):
        attrs = {name: value or "" for name, value in attrs}
        if self.card is not None and attrs.get("aria-label"):
            self.card["labels"].append(" ".join(attrs["aria-label"].split()))
        if tag in _VOID:
            return
        self.stack.append(tag)
        if tag in _HIDDEN:
            self.hidden += 1
        if self.card is None and len(self.cards) < MAX_CARDS:
            identity = self._root(attrs)
            if identity:
                self.card = {"identity": identity, "depth": len(self.stack), "pieces": [], "labels": [], "brand": None}
                if attrs.get("aria-label"):
                    self.card["labels"].append(" ".join(attrs["aria-label"].split()))
        # Amazon shows the brand or store, when it shows one, as a small
        # heading above the title (class "a-size-mini"), never inside it.
        if (self.card is not None and self.route == "amazon-search" and tag == "h2"
                and "a-size-mini" in attrs.get("class", "").split() and self.card["brand"] is None):
            self.brand_depth, self.card["brand"] = len(self.stack), ""

    def handle_endtag(self, tag):
        if tag in _VOID or not self.stack:
            return
        if tag in _HIDDEN and self.hidden:
            self.hidden -= 1
        if self.brand_depth == len(self.stack):
            self.brand_depth = None
        if self.card is not None and len(self.stack) == self.card["depth"]:
            self.cards.append(self.card)
            self.card = None
        self.stack.pop()

    def handle_data(self, data):
        text = " ".join(data.split())
        if self.card is None or self.hidden or not text:
            return
        self.card["pieces"].append(text)
        if self.brand_depth is not None:
            self.card["brand"] = (self.card["brand"] + " " + text).strip()


def _card_facts(route, raw):
    pieces, labels = raw["pieces"], raw["labels"]
    ratings = None
    for value in labels + pieces:
        for pattern in _RATINGS[:3] if value in labels else _RATINGS[2:]:
            match = pattern.search(value)
            if match:
                ratings = _number(match[1]); break
        if ratings is not None:
            break
    if ratings is None:
        compact = next((_COMPACT.match(piece) for piece in pieces if _COMPACT.match(piece)), None)
        ratings = _number(compact[1], compact[2]) if compact else None
    stars = next((match[1] for value in labels + pieces for match in [_STARS.search(value)] if match), None)
    if stars is None and route == "flipkart-search":
        # Flipkart prints the star average as a bare number right before the count.
        for index, piece in enumerate(pieces[:-1]):
            if _GRID_STARS.match(piece) and any(pattern.match(pieces[index + 1]) for pattern in _RATINGS[2:]):
                stars = piece; break
    bought = next((match for piece in pieces for match in [_BOUGHT.match(piece)] if match), None)
    rank = next((int(piece[1:]) for piece in pieces if re.fullmatch(r"#[1-9][0-9]{0,4}", piece)), None)
    brand = raw["brand"].strip()[:200] if raw["brand"] and raw["brand"].strip() else None
    return {"ratings_count": ratings, "star_rating": stars, "brand": brand, "brand_shown": brand is not None,
            "bought_past_month": _number(bought[1], bought[2]) if bought else None,
            "bought_text": bought[0][:80] if bought else None,
            "sponsored": (any(piece in ("Sponsored", "Ad") for piece in pieces[:8])
                          or any(label.startswith("Sponsored Ad") for label in labels)),
            "list_rank": rank if route == "amazon-bestsellers" else None,
            # Amazon marks a search result that is a category bestseller with a badge.
            "bestseller_badge": route == "amazon-search" and any(_BADGE.match(value) for value in pieces[:6] + labels)}


def total_results(text):
    """The page's own "of N results" count; "over N" is a lower bound."""
    if not isinstance(text, str):
        return None, False
    for pattern in _TOTAL:
        match = pattern.search(text)
        if match:
            groups = match.groups()
            over, digits = (groups[0], groups[1]) if len(groups) == 2 else (None, groups[0])
            return _number(digits), bool(over)
    return None, False


def merge_card_facts(first, second):
    """One product shown twice (an advert and an ordinary result, or two saved
    pages) is one listing: the larger counts, the first shown text."""
    merged = dict(first)
    for field in ("ratings_count", "bought_past_month"):
        values = [value for value in (first.get(field), second.get(field)) if value is not None]
        merged[field] = max(values) if values else None
    for field in ("star_rating", "brand", "bought_text", "list_rank"):
        merged[field] = first.get(field) if first.get(field) is not None else second.get(field)
    merged["brand_shown"] = merged["brand"] is not None
    merged["sponsored"] = first.get("sponsored") is True or second.get("sponsored") is True
    merged["bestseller_badge"] = first.get("bestseller_badge") is True or second.get("bestseller_badge") is True
    return merged


def parse_saved_cards(route, html, text):
    """Pure parser v3 over one saved first page; no relevance judgment."""
    if route not in CARD_ROUTES or not isinstance(html, str) or len(html) > MAX_HTML or not isinstance(text, str):
        raise SafetyError("CROWDING_CARD_INPUT_INVALID")
    parser = _Cards(route)
    parser.feed(html); parser.close()
    cards, position = {}, 0
    for raw in parser.cards:
        position += 1
        facts = dict(_card_facts(route, raw), position=position)
        saved = cards.get(raw["identity"])
        cards[raw["identity"]] = facts if saved is None else merge_card_facts(saved, facts)
    total, lower_bound = total_results(text)
    state = "read" if cards else "no_cards"
    return {"parser_version": CARD_PARSER_VERSION, "route": route, "state": state,
            "reason": "CROWDING_CARDS_READ" if cards else "CROWDING_CARDS_NOT_FOUND",
            "total_results": total, "total_results_lower_bound": lower_bound, "cards": cards}


def _hash(path, limit):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        return None
    return sha256(path.read_bytes()).hexdigest()


def read_capture_cards(folder, route):
    """Parser v2 facts for one saved capture folder, reusing a current side file.

    A missing or oversized saved page is an unreadable read ("unreadable"),
    never an empty one. The side file is rewritten when the saved bytes or the
    parser version differ; the receipt and original files are never touched.
    """
    if route not in CARD_ROUTES:
        raise SafetyError("CROWDING_CARD_ROUTE_INVALID")
    folder = safe_path(folder)
    html_path, snapshot_path, side = folder / "rendered.html", folder / "snapshot.json", folder / SIDE_FILE
    hashes = {"rendered.html": _hash(html_path, MAX_HTML), "snapshot.json": _hash(snapshot_path, MAX_SNAPSHOT)}
    if hashes["rendered.html"] is None or hashes["snapshot.json"] is None:
        return {"parser_version": CARD_PARSER_VERSION, "route": route, "state": "unreadable",
                "reason": "CROWDING_SAVED_PAGE_UNAVAILABLE", "total_results": None,
                "total_results_lower_bound": False, "cards": {}}
    if side.is_file() and not side.is_symlink():
        try:
            saved = load_json(side, MAX_SIDE)
            if (saved.get("parser_version") == CARD_PARSER_VERSION and saved.get("route") == route
                    and saved.get("source_hashes") == hashes):
                return {field: value for field, value in saved.items() if field != "source_hashes"}
        except (SafetyError, AttributeError):
            pass  # A damaged derived file is read again from the saved page.
    snapshot = load_json(snapshot_path, MAX_SNAPSHOT)
    text = snapshot.get("text") if isinstance(snapshot, dict) else None
    result = parse_saved_cards(route, html_path.read_text(encoding="utf-8", errors="replace"),
                               text if isinstance(text, str) else "")
    atomic_json(side, dict(result, source_hashes=hashes))
    return result
