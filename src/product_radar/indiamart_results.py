"""IndiaMART search results (supplier parser version 6).

The page's own structured search data (``__NEXT_DATA__`` ->
``pageProps.searchResponse``) is read first. Without it, the visible cards
are read: a card is the largest page block around one product listing link
that holds no other listing and no page header, footer, navigation or form.
Either way each result gives the listing link (address and path; tracking
query strings dropped, so a title link and a picture link are one listing),
product title, supplier name and page, location, and the price and
minimum-order text as shown (copied, never converted). Listings in personal
blocks - recently viewed, recommendations, anything about the account - are
never results. Class names only help; nothing depends on them. Buttons such
as "Contact Supplier", "View number" or "Get best price" are only labels here:
never a title or name, and never pressed.
"""

from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlsplit

_SKIP = {"script", "style", "noscript", "template", "svg"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_CHROME = {"header", "footer", "nav", "form"}
_LANDMARKS = {"header", "footer", "nav"}
_EDGE = {"#root", "html", "body"}
_ACTIONS = {"contact supplier", "contact seller", "view number", "view mobile number", "view phone number",
            "get best price", "get latest price", "get best quote", "get quote", "send enquiry", "send inquiry",
            "call now", "ask price", "ask for price", "view details", "view more", "request callback"}
_PRICE = re.compile(r"(?:₹|\bRs\.?|\bINR)\s*[\d,]+(?:\.\d+)?(?:\s*(?:-|to)\s*(?:₹|Rs\.?)?\s*[\d,]+(?:\.\d+)?)?"
                    r"(?:\s*/\s*[A-Za-z]+(?:\s+(?:Feet|Foot|Meter|Metre|Inch|Yard))?)?", re.I)
_MOQ_LABEL = re.compile(r"\b(?:MOQ|Min(?:imum)?\.?\s*Order(?:\s*Qty|\s*Quantity)?)\b", re.I)
_MOQ = re.compile(r"\b(?:MOQ|Min(?:imum)?\.?\s*Order(?:\s*Qty|\s*Quantity)?)\s*[:\-]?\s*[\d,]+(?:\.\d+)?"
                  r"(?:\s*[A-Za-z][A-Za-z.]*){0,2}", re.I)
_LOCATION_HINT = (("city", "location", "locality", "address"), {"loc", "addr"})
_SUPPLIER_HINT = (("compan", "cmp", "seller", "supplier"), set())
_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")
_STATES = ("Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
           "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
           "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
           "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal", "Andaman and Nicobar Islands", "Chandigarh",
           "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep",
           "Puducherry")
_STATE_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,()/-]{0,80},\s*(?:" + "|".join(map(re.escape, _STATES)) + r")$", re.I)
_CATALOGUE = re.compile(r"/[a-z0-9][a-z0-9-]{1,80}/")
_PERSONAL_WORDS = ("recent", "recommend", "personal", "history", "viewed")
_PERSONAL_TEXT = re.compile(r"recently viewed|recommend|based on your|you may (?:also )?like|for you\b|"
                            r"your (?:history|activity|searches|enquiries)", re.I)
_DATA = re.compile(r"<script\b[^>]*\bid=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", re.I | re.S)
_DATA_FIELDS = {"title": ("title", "name", "itemname", "prd_name", "productname", "product_name", "pname"),
                "supplier_name": ("companyname", "company_name", "compname", "company", "seller_name", "suppliername"),
                "supplier_url": ("catalogurl", "catalog_url", "companyurl", "company_url", "cat_url", "companylink"),
                "location": ("city", "cityname", "city_name", "location", "address"),
                "price": ("price", "itemprice", "price_f", "prd_price", "standard_price"),
                "unit": ("unit", "priceunit", "price_unit", "unit_name"),
                "moq": ("moq", "min_order_qty", "minorderqty"),
                "moq_unit": ("moq_type", "moq_unit", "moqtype")}
FIELDS = ("url", "title", "supplier_name", "supplier_url", "location", "price_text", "moq_text")


class _Node:
    __slots__ = ("tag", "attrs", "parent", "children", "listings", "chrome")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children = tag, attrs, parent, []
        self.listings, self.chrome = set(), tag in _CHROME


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.stack, self.skip, self.depth = [self.root], None, 0

    def handle_starttag(self, tag, attrs):
        if self.skip is not None:
            self.depth += tag == self.skip
            return
        if tag in _SKIP:
            self.skip, self.depth = tag, 1
            return
        node = _Node(tag, {name: value or "" for name, value in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        if self.skip is None and tag not in _SKIP:
            self.stack[-1].children.append(_Node(tag, {name: value or "" for name, value in attrs}, self.stack[-1]))

    def handle_endtag(self, tag):
        if self.skip is not None:
            self.depth -= tag == self.skip
            if not self.depth:
                self.skip = None
            return
        for position in range(len(self.stack) - 1, 0, -1):
            if self.stack[position].tag == tag:
                del self.stack[position:]
                return

    def handle_data(self, value):
        if self.skip is None and value.strip():
            self.stack[-1].children.append(value)


def _walk(node):
    """Elements below ``node`` (itself included) in page order."""
    pending, found = [node], []
    while pending:
        current = pending.pop()
        found.append(current)
        pending.extend(child for child in reversed(current.children) if isinstance(child, _Node))
    return found


def _clean(value, limit=300):
    value = " ".join(value.split())
    return value[:limit] if value else None


def _text(node, memo):
    if id(node) not in memo:
        memo[id(node)] = " ".join(" ".join(child.split()) if isinstance(child, str) else _text(child, memo)
                                  for child in node.children if isinstance(child, str) or _text(child, memo))
    return memo[id(node)]


def _visible(node, memo):
    words = _clean(_text(node, memo))
    return None if words is None or words.casefold().rstrip(" .:") in _ACTIONS else words


def _label(node, memo):
    """Visible words of a link or element, or its own title/label/alt text."""
    words = _clean(_text(node, memo)) or _clean(node.attrs.get("title", "")) or _clean(node.attrs.get("aria-label", ""))
    if words is None:
        words = next((_clean(row.attrs.get("alt", "")) for row in _walk(node) if row.tag == "img" and row.attrs.get("alt")), None)
    return None if words is None or words.casefold().rstrip(" .:") in _ACTIONS else words


def _hinted(card, hint):
    """Elements whose class or id words name the field (``newLocationUi``, ``companyname``)."""
    starts, words = hint
    return [row for row in _walk(card)
            if any(word.startswith(starts) or word in words
                   for word in (part.casefold() for part in _WORD.findall(row.attrs.get("class", "") + " " + row.attrs.get("id", ""))))]


def _shown(card, label, pattern, memo):
    """The shown text matching ``pattern`` around the first ``label`` match, within 80 characters."""
    for row in _walk(card):
        if not any(isinstance(child, str) and label.search(child) for child in row.children):
            continue
        while row.parent is not card and row.parent is not None and len(_text(row.parent, memo)) <= 80:
            row = row.parent
        found = pattern.search(_text(row, memo))
        if found:
            return _clean(found.group(0), 120)
    return None


def _card(card, url, links, memo):
    anchors = [(row, links[id(row)]) for row in _walk(card) if id(row) in links]
    # Visible link words first, then a heading, then a picture's or link's own label.
    title = next((name for row, lead in anchors if lead == url and (name := _visible(row, memo))), None)
    if title is None:
        title = next((name for row in _walk(card) if row.tag in ("h1", "h2", "h3", "h4") and (name := _label(row, memo))), None)
    if title is None:
        title = next((name for row, lead in anchors if lead == url and (name := _label(row, memo))), None)
    if title is None:
        return None
    supplier_url, supplier = None, None
    for row, lead in anchors:
        if lead != url and not _listing(lead) and (name := _label(row, memo)):
            supplier_url, supplier = lead, name
            break
    if supplier is None:
        supplier = next((name for row in _hinted(card, _SUPPLIER_HINT)
                         if len(_text(row, memo)) <= 150 and not _PRICE.search(_text(row, memo))
                         and (name := _label(row, memo))), None)
    location = next((_clean(_text(row, memo), 120) for row in _hinted(card, _LOCATION_HINT)
                     if 2 <= len(_text(row, memo)) <= 120 and not _PRICE.search(_text(row, memo))
                     and _label(row, memo)), None)
    if location is None:
        location = next((_clean(child, 120) for row in _walk(card) for child in row.children
                         if isinstance(child, str) and _STATE_LINE.match(" ".join(child.split()))), None)
    return {"url": url, "title": _clean(title, 2000), "supplier_name": supplier, "supplier_url": supplier_url,
            "location": location, "price_text": _shown(card, _PRICE, _PRICE, memo),
            "moq_text": _shown(card, _MOQ_LABEL, _MOQ, memo)}


def _personal(node, memo):
    words = (part.casefold() for part in _WORD.findall(node.attrs.get("class", "") + " " + node.attrs.get("id", "")))
    if any(word.startswith(_PERSONAL_WORDS) for word in words):
        return True
    labels = [node.attrs.get("aria-label", ""), node.attrs.get("title", "")] + [
        _text(child, memo) for child in node.children
        if isinstance(child, _Node) and child.tag in ("h1", "h2", "h3", "h4", "h5", "h6")]
    return any(_PERSONAL_TEXT.search(label) for label in labels)


def _excluded(node, memo):
    """A link in the page's header, footer, navigation or a personal block
    (recently viewed, recommendations) is never a result."""
    while node is not None:
        if node.tag in _LANDMARKS or node.tag != "#root" and _personal(node, memo):
            return True
        node = node.parent
    return False


def _listing_key(url):
    """One listing whatever tracking query string a link carries."""
    return "https://www.indiamart.com" + urlsplit(url).path


def _listing(url):
    parsed = urlsplit(url)
    return parsed.hostname == "www.indiamart.com" and parsed.path.casefold().startswith("/proddetail/")


def _catalogue(url):
    """A supplier's own IndiaMART catalogue address, ``www.indiamart.com/<company>/``."""
    try:
        parsed = urlsplit(url)
        usable = (parsed.scheme == "https" and parsed.hostname == "www.indiamart.com" and not parsed.query
                  and not parsed.username and parsed.port is None and _CATALOGUE.fullmatch(parsed.path))
    except ValueError:
        return None
    return "https://www.indiamart.com" + parsed.path if usable else None


def _value(row, names):
    found = next((row[name] for name in names if name in row and row[name] not in (None, "")), None)
    return _clean(str(found), 300) if isinstance(found, (str, int, float)) and not isinstance(found, bool) else None


def _data_row(value, lead_url):
    """One structured search result, if this object names a product listing."""
    row = {str(name).casefold(): item for name, item in value.items()}
    if isinstance(row.get("fields"), dict):
        row.update({str(name).casefold(): item for name, item in row["fields"].items()})
    listing = next((_listing_key(lead) for item in row.values() if isinstance(item, str)
                    and (lead := lead_url(item)) and _listing(lead)), None)
    title = _value(row, _DATA_FIELDS["title"])
    if listing is None or title is None:
        return None
    supplier_url = _value(row, _DATA_FIELDS["supplier_url"])
    supplier_url = supplier_url and (lead_url(supplier_url) or _catalogue(supplier_url))
    price, unit = _value(row, _DATA_FIELDS["price"]), _value(row, _DATA_FIELDS["unit"])
    moq, moq_unit = _value(row, _DATA_FIELDS["moq"]), _value(row, _DATA_FIELDS["moq_unit"])
    return {"url": listing, "title": _clean(title, 2000), "supplier_name": _value(row, _DATA_FIELDS["supplier_name"]),
            "supplier_url": supplier_url if supplier_url and not _listing(supplier_url) else None,
            "location": _value(row, _DATA_FIELDS["location"]),
            "price_text": price and (price + " / " + unit if unit else price),
            "moq_text": moq and "MOQ: " + (moq + " " + moq_unit if moq_unit else moq)}


def _data_results(html, lead_url, limit):
    """Results from the page's own structured search data, in its order."""
    found = _DATA.search(html)
    try:
        response = json.loads(found[1])["props"]["pageProps"]["searchResponse"] if found else None
    except (ValueError, TypeError, KeyError):
        return []
    rows, seen, pending = [], set(), [(response, 0)]
    while pending and len(rows) < limit:
        value, depth = pending.pop(0)
        if isinstance(value, dict):
            row = _data_row(value, lead_url)
            if row is not None:
                if row["url"] not in seen:
                    rows.append(row); seen.add(row["url"])
                continue
            value = list(value.values())
        if isinstance(value, list) and depth < 8:
            pending[:0] = [(item, depth + 1) for item in value if isinstance(item, (dict, list))]
    return rows


def result_cards(html, base, lead_url, limit):
    """Up to ``limit`` product listings: structured search data, else cards.

    ``lead_url`` admits IndiaMART result links (listings and supplier pages);
    a supplier's catalogue address is kept only as that card's supplier page.
    """
    rows = _data_results(html, lead_url, limit)
    if rows:
        return rows
    tree = _Tree()
    tree.feed(html)
    tree.close()
    links, memo = {}, {}
    nodes = _walk(tree.root)
    for node in nodes:
        if node.tag == "a" and node.attrs.get("href"):
            try:
                address = urljoin(base, node.attrs["href"])
            except ValueError:
                continue  # a malformed link is not a result, and not the whole page
            lead = lead_url(address) or _catalogue(address)
            if lead:
                links[id(node)] = _listing_key(lead) if _listing(lead) else lead
    for node in reversed(nodes):  # children before their parents
        if id(node) in links and _listing(links[id(node)]):
            node.listings.add(links[id(node)])
        if node.parent is not None:
            node.parent.listings |= node.listings
            node.parent.chrome |= node.chrome
    rows, seen = [], set()
    for node in nodes:
        url = links.get(id(node))
        if url is None or url in seen or not _listing(url) or _excluded(node, memo):
            continue
        card = node
        while (card.parent is not None and card.parent.tag not in _EDGE
               and card.parent.listings == {url} and not card.parent.chrome):
            card = card.parent
        row = _card(card, url, links, memo)
        if row is not None:
            rows.append(row)
            seen.add(url)
        if len(rows) >= limit:
            break
    return rows
