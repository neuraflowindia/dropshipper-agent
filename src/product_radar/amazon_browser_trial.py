"""Isolated public Amazon India Bestsellers experiment; never imports research.

Only the CLI's explicit capture command navigates. Parsing, replay and summaries
are pure/offline. A saved acquisition is immutable, including a failed attempt.
"""

import asyncio
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from .models import SafetyError
from .runtime_storage import runtime_path

VERSION = "amazon-public-bestsellers-trial-v1"
ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = runtime_path(ROOT, "data/trials/amazon-bestsellers")
MEDIA_ROOT = ROOT / "media/trials/amazon-bestsellers"
MAX_CARDS = 100
MAX_SNAPSHOT = 2 * 1024 * 1024
MAX_HTML = 8 * 1024 * 1024
MAX_IMAGE = 5 * 1024 * 1024
MAX_ENVELOPE = MAX_HTML + MAX_SNAPSHOT + 7 * 1024 * 1024
CATEGORIES = {"kitchen": ("kitchen",), "baby": ("baby",),
              "luggage": ("luggage", "bags, wallets", "bags & wallets")}
LANDING = "https://www.amazon.in/gp/bestsellers"
BLOCKED = re.compile(r"captcha|not a robot|robot check|automated access|enter the characters you see|sorry.*something went wrong", re.I)

# Rank -> nearest known list-card root keeps the title/link/quote together.
# Unknown DOM is recorded as unrecognised, never a successful empty list.
SNAPSHOT_JS = r"""() => {
  const text = document.body?.innerText || '';
  const html = document.documentElement.outerHTML;
  const ranks = Array.from(document.querySelectorAll('.zg-bdg-text'));
  const cards = ranks.map((rank, index) => {
    let card = rank.closest('.zg-grid-general-faceout,.zg-item-immersion,#gridItemRoot');
    if (!card) {
      let node=rank.parentElement;
      for(let n=0;node && n<6 && !['BODY','HTML'].includes(node.tagName);n++,node=node.parentElement) {
        if(node.querySelectorAll('.zg-bdg-text').length===1 &&
           node.querySelectorAll('.zg-grid-general-faceout,.zg-item-immersion').length===1) {card=node;break;}
      }
    }
    if (!card) return null;
    const link = card.querySelector('a[href*="/dp/"],a[href*="/gp/product/"]');
    const image = card.querySelector('img');
    const title = card.querySelector('.p13n-sc-truncate-desktop-type2,.p13n-sc-truncate');
    const price = card.querySelector('.p13n-sc-price,.a-price:not(.a-text-price) .a-offscreen')
      || card.querySelector('.a-color-price');
    const quote = price?.closest('.a-color-secondary') || price;
    return {card_ref:'rank-card-'+index,rank_text:rank.innerText.trim(),
      title:(title?.innerText || image?.alt || link?.innerText || '').trim().slice(0,4000),
      product_url:link?.href || null,raw_price:quote?.innerText.trim().slice(0,1000) || null,
      raw_price_truncated:(quote?.innerText.trim().length || 0)>1000,
      image_url:image?.currentSrc || image?.src || null};
  }).filter(Boolean);
  const heading = Array.from(document.querySelectorAll('h1,.zg-selected,#zg_banner_text'))
    .map(e=>e.innerText.trim()).filter(Boolean).join('\n').slice(0,8000);
  const links = Array.from(document.querySelectorAll('a[href]'))
    .filter(a=>/^https:\/\/www\.amazon\.in\/gp\/bestsellers\/(kitchen|baby|luggage)(\/|$)/.test(a.href))
    .slice(0,50).map(a=>({label:a.innerText.trim().slice(0,300),url:a.href}));
  return {url:location.href,title:document.title.slice(0,2000),heading,
    text:text.slice(0,75000),text_truncated:text.length>75000,
    html:html.length<=8388608?html:null,html_characters:html.length,
    rank_nodes:ranks.length,card_count:cards.length,cards:cards.slice(0,100),links};
}"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise SafetyError("AMAZON_TRIAL_ID_INVALID")
    return value


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _path(root, *parts):
    result = Path(root).absolute().joinpath(*(_id(part) for part in parts))
    if any(path.is_symlink() for path in [result, *result.parents]):
        raise SafetyError("AMAZON_TRIAL_SYMLINK_REFUSED")
    return result


def _save(path, raw):
    # Only freshly created trial directories are written. Existing files are
    # never replaced; interrupted acquisition folders require explicit review.
    if path.is_symlink() or path.exists():
        raise SafetyError("AMAZON_TRIAL_ARTIFACT_EXISTS")
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _read(path, limit):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise SafetyError("AMAZON_TRIAL_ARTIFACT_INVALID")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise SafetyError("AMAZON_TRIAL_ARTIFACT_INVALID")
    return raw


def list_scope(url, *, landing=False):
    """Only the three actually inspected list departments, page one, India."""
    try:
        if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20]", url):
            return None
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.amazon.in" or parsed.fragment:
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) - {"ref_", "ref", "pg"} or any(len(values) != 1 for values in query.values()):
            return None
        if "pg" in query and query["pg"] != ["1"]:
            return None
        if landing and re.fullmatch(r"/gp/bestsellers/?", parsed.path) and not query:
            return "landing"
        match = re.fullmatch(r"/gp/bestsellers/(kitchen|baby|luggage)/?(?:ref=[A-Za-z0-9_-]+/?)?", parsed.path)
        return match[1] if match else None
    except (TypeError, ValueError):
        return None


def _product(url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "www.amazon.in" or re.search(r"[\x00-\x20]", url):
            return None
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", parsed.path + ("?" if parsed.query else ""))
        return match[1] if match else None
    except ValueError:
        return None


def allowed_browser_request(url, method="GET", *, navigation=False):
    """No forms, personal login, arbitrary navigation or private destinations."""
    try:
        parsed = urlsplit(url)
        if (method != "GET" or parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or not parsed.hostname):
            return False
        host = parsed.hostname
        if navigation:
            return host == "www.amazon.in" and (list_scope(url, landing=True) is not None
                or parsed.path.startswith("/errors/"))
        return (host == "www.amazon.in" or host.endswith(".amazon.in")
                or host.endswith(".media-amazon.com") or host.endswith(".ssl-images-amazon.com"))
    except (TypeError, ValueError):
        return False


def parse_snapshot(snapshot, requested_url, *, navigation_status=200, kind="list"):
    """Return extraction/scope results, never functional identity or eligibility."""
    category = list_scope(requested_url, landing=kind == "landing")
    if not category or kind not in ("landing", "list") or (kind == "landing") != (category == "landing"):
        raise SafetyError("AMAZON_TRIAL_URL_INVALID")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("cards"), list) or len(snapshot["cards"]) > MAX_CARDS:
        raise SafetyError("AMAZON_TRIAL_SNAPSHOT_INVALID")
    text = snapshot.get("text", "")
    if not isinstance(text, str) or len(text) > 75000:
        raise SafetyError("AMAZON_TRIAL_SNAPSHOT_INVALID")
    result = {"state": "insufficient", "reason": "scope_unverified", "category": category,
              "access_success": navigation_status == 200, "scope_verified": False,
              "records": [], "rejected": [], "duplicates": [], "cases": [],
              "observed_cards": snapshot.get("card_count"), "functional_products": None,
              "qualifying_products": None}
    if navigation_status in (403, 429, 503) or BLOCKED.search(text):
        result.update(state="blocked", reason="access_challenge_or_source_unavailable", access_success=False)
        return result
    final = list_scope(snapshot.get("url"), landing=kind == "landing")
    heading = str(snapshot.get("heading", "")) + "\n" + str(snapshot.get("title", ""))
    if final != category or not re.search(r"best\s*sell(?:er|ers|ing)", heading, re.I):
        return result
    if kind == "list" and not any(label in heading.casefold() for label in CATEGORIES[category]):
        return result
    result["scope_verified"] = True
    if navigation_status != 200:
        result.update(reason="navigation_status_unverified")
        return result
    if kind == "landing":
        seen = set()
        for link in snapshot.get("links", []):
            if isinstance(link, dict) and list_scope(link.get("url")) and link["url"] not in seen:
                seen.add(link["url"])
                result["cases"].append({"category": list_scope(link["url"]), "label": link.get("label", ""), "url": link["url"]})
        result.update(state="usable" if result["cases"] else "insufficient", reason="landing_links_observed" if result["cases"] else "landing_links_unrecognised")
        return result
    for field in ("rank_nodes", "card_count"):
        if type(snapshot.get(field)) is not int or snapshot[field] < len(snapshot["cards"]):
            raise SafetyError("AMAZON_TRIAL_SNAPSHOT_COUNTS_INVALID")
    records, references, conflicts = {}, {}, set()
    for index, card in enumerate(snapshot["cards"]):
        if not isinstance(card, dict):
            result["rejected"].append({"card_ref": index, "reason": "invalid_card"})
            continue
        ref = card.get("card_ref", str(index))
        asin = _product(card.get("product_url"))
        rank = re.fullmatch(r"#?\s*([1-9][0-9]*)", str(card.get("rank_text", "")).strip())
        title = card.get("title")
        if not asin or not rank or not isinstance(title, str) or not title.strip() or len(title) > 4000:
            result["rejected"].append({"card_ref": ref, "reason": "missing_valid_asin_title_or_explicit_rank"})
            continue
        quote = card.get("raw_price")
        quote = quote.strip() if isinstance(quote, str) and quote.strip() else None
        if quote is not None and len(quote) > 1000:
            raise SafetyError("AMAZON_TRIAL_SNAPSHOT_INVALID")
        record = {"asin": asin, "title": title.strip(), "position": int(rank[1]),
                  "position_kind": "explicit_list_rank_not_unit_sales", "product_url": card["product_url"],
                  "raw_price": quote, "currency": "INR" if quote and re.search(r"₹|\bINR\b", quote) else None,
                  "price_basis": "retail_displayed_quote", "unit_pack_basis": "unknown",
                  "quote_complete": card.get("raw_price_truncated") is not True,
                  "image_url": card.get("image_url") if isinstance(card.get("image_url"), str) else None}
        if asin in records:
            same = {key: value for key, value in record.items() if key != "product_url"} == {
                key: value for key, value in records[asin].items() if key != "product_url"}
            if not same:
                conflicts.add(asin)
            result["duplicates"].append({"asin": asin, "card_ref": ref, "conflicting": not same})
        else:
            records[asin] = record
        references.setdefault(asin, []).append(ref)
    for asin, record in records.items():
        if asin in conflicts:
            result["rejected"].append({"asin": asin, "card_refs": references[asin], "reason": "conflicting_duplicate_asin"})
        else:
            result["records"].append(dict(record, card_refs=references[asin]))
    result["records"].sort(key=lambda row: (row["position"], row["asin"]))
    if result["records"]:
        partial = bool(result["rejected"] or snapshot["card_count"] > MAX_CARDS
                       or snapshot["rank_nodes"] > snapshot["card_count"]
                       or any(row["quote_complete"] is False for row in result["records"]))
        result.update(state="partial" if partial else "usable", reason="ranked_cards_partially_usable" if partial else "ranked_cards_observed")
    else:
        result.update(reason="ranked_cards_unrecognised" if not snapshot["cards"] else "ranked_cards_not_usable")
    return result


def replay(trial_id, acquisition_id, *, data_root=DATA_ROOT, media_root=MEDIA_ROOT):
    folder, media = _path(data_root, trial_id, acquisition_id), _path(media_root, trial_id, acquisition_id)
    try:
        receipt = json.loads(_read(folder / "receipt.json", 32768))
        intent = json.loads(_read(folder / "intent.json", 16384))
        if (not isinstance(receipt, dict) or not isinstance(intent, dict)
                or receipt.get("version") != VERSION or receipt.get("trial_id") != trial_id
                or receipt.get("acquisition_id") != acquisition_id or receipt.get("intent_hash") != _hash(_bytes(intent))
                or intent.get("version") != VERSION or intent.get("pages") != 1 or intent.get("max_cards") != MAX_CARDS
                or intent.get("kind") not in ("list", "landing") or receipt.get("kind") != intent.get("kind")
                or intent.get("url") != receipt.get("requested_url") or not list_scope(intent.get("url"), landing=intent.get("kind") == "landing")
                or receipt.get("state") not in ("usable", "partial", "insufficient", "blocked", "failed", "unavailable")):
            raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
        if (type(intent.get("timeout_seconds")) is not int or not 10 <= intent["timeout_seconds"] <= 60
                or not isinstance(receipt.get("artifacts"), dict)
                or receipt.get("apify_requests") != 0 or receipt.get("model_calls") != 0
                or receipt.get("apify_charge_usd") != "0"):
            raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
        started, completed = (datetime.fromisoformat(receipt[field]) for field in ("started_at", "completed_at"))
        if not started.tzinfo or not completed.tzinfo or started > completed:
            raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
        for name, expected in receipt["artifacts"].items():
            limits = {"snapshot.json": MAX_SNAPSHOT, "parsed.json": MAX_SNAPSHOT, "rendered.html": MAX_HTML,
                      "page.png": MAX_IMAGE, "browser_envelope.json": MAX_ENVELOPE}
            if name not in limits:
                raise SafetyError("AMAZON_TRIAL_ARTIFACT_INVALID")
            actual = _read((media if name == "page.png" else folder) / name, limits[name])
            if _hash(actual) != expected:
                raise SafetyError("AMAZON_TRIAL_HASH_MISMATCH")
        if intent.get("mode") == "codex_playwright_fresh_owned_context":
            if "browser_envelope.json" not in receipt["artifacts"]:
                raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
            envelope = json.loads(_read(folder / "browser_envelope.json", MAX_ENVELOPE))
            _validate_browser_envelope(envelope)
            for field in ("started_at", "collected_at", "completed_at", "elapsed_seconds", "navigation_status",
                          "public_page_navigations", "owned_context_closed", "stage", "mode"):
                if receipt.get(field) != envelope.get(field):
                    raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
            if envelope["requested_url"] != intent["url"] or envelope["kind"] != intent["kind"]:
                raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
            if "snapshot.json" not in receipt["artifacts"] or envelope["snapshot"] != json.loads(_read(folder / "snapshot.json", MAX_SNAPSHOT)):
                raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
            if envelope["owned_context_closed"] is False and receipt.get("state") != "failed":
                raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
        if "parsed.json" in receipt["artifacts"]:
            if "snapshot.json" not in receipt["artifacts"]:
                raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
            snapshot = json.loads(_read(folder / "snapshot.json", MAX_SNAPSHOT))
            parsed = parse_snapshot(snapshot, intent["url"], navigation_status=receipt.get("navigation_status"), kind=intent["kind"])
            if parsed != json.loads(_read(folder / "parsed.json", MAX_SNAPSHOT)):
                raise SafetyError("AMAZON_TRIAL_PARSED_MISMATCH")
            if any(receipt.get(field) != parsed[field] for field in ("access_success", "scope_verified")):
                raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
            if receipt.get("state") not in ("failed", "unavailable") and any(receipt.get(field) != parsed[field]
                    for field in ("state", "reason")):
                raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
            if receipt.get("actual_url") is not None and receipt["actual_url"] != snapshot.get("url"):
                raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
            if receipt.get("usable_cards") != len(parsed["records"]) or receipt.get("discovered_cases") != parsed["cases"]:
                raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
        elif receipt.get("state") not in ("failed", "unavailable"):
            raise SafetyError("AMAZON_TRIAL_RECEIPT_INVALID")
        return receipt
    except (OSError, ValueError, TypeError, KeyError):
        raise SafetyError("AMAZON_TRIAL_SAVED_CAPTURE_INVALID_OR_INCOMPLETE") from None


def _validate_browser_envelope(envelope):
    """A public capture already performed and closed by the connected browser tool.

    This is an evidence writer, not a browser transport or a profile importer.
    The caller supplies the tool's actual timestamps/status, never trial estimates.
    """
    allowed = {"mode", "kind", "requested_url", "started_at", "collected_at", "completed_at", "elapsed_seconds",
               "stage", "navigation_status", "public_page_navigations", "owned_context_closed", "snapshot",
               "html_saved", "html", "screenshot_base64"}
    if (not isinstance(envelope, dict) or set(envelope) - allowed
            or envelope.get("mode") != "codex_playwright_fresh_owned_context"
            or envelope.get("kind") not in ("list", "landing")
            or type(envelope.get("navigation_status")) is not int or not 100 <= envelope["navigation_status"] <= 599
            or type(envelope.get("public_page_navigations")) is not int or envelope["public_page_navigations"] != 1
            or type(envelope.get("owned_context_closed")) is not bool
            or envelope.get("stage") not in ("snapshot", "screenshot", "context_close", "complete")
            or type(envelope.get("elapsed_seconds")) not in (int, float)
            or not 0 <= envelope["elapsed_seconds"] <= 60):
        raise SafetyError("AMAZON_TRIAL_BROWSER_ENVELOPE_INVALID")
    try:
        times = [datetime.fromisoformat(envelope[field]) for field in ("started_at", "collected_at", "completed_at")]
        if (any(not value.tzinfo or value.utcoffset().total_seconds() != 0 for value in times)
                or not times[0] <= times[1] <= times[2]
                or abs((times[2] - times[0]).total_seconds() - envelope["elapsed_seconds"]) > 2):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SafetyError("AMAZON_TRIAL_BROWSER_TIMESTAMPS_INVALID") from None
    snapshot = envelope.get("snapshot")
    snapshot_fields = {"url", "title", "heading", "text", "text_truncated", "html_characters",
                       "rank_nodes", "card_count", "cards", "links"}
    card_fields = {"card_ref", "rank_text", "title", "product_url", "raw_price", "raw_price_truncated", "image_url"}
    if (not isinstance(snapshot, dict) or set(snapshot) - snapshot_fields or len(_bytes(snapshot)) > MAX_SNAPSHOT
            or not isinstance(snapshot.get("cards"), list)
            or any(not isinstance(card, dict) or set(card) - card_fields for card in snapshot["cards"])
            or not isinstance(snapshot.get("links", []), list)
            or any(not isinstance(link, dict) or set(link) - {"label", "url"} for link in snapshot.get("links", []))):
        raise SafetyError("AMAZON_TRIAL_SNAPSHOT_INVALID")
    parse_snapshot(snapshot, envelope["requested_url"], navigation_status=envelope["navigation_status"], kind=envelope["kind"])
    html = envelope.get("html")
    if html is not None and (not isinstance(html, str) or len(html.encode()) > MAX_HTML):
        raise SafetyError("AMAZON_TRIAL_CAPTURE_TOO_LARGE")
    if "html_saved" in envelope and (type(envelope["html_saved"]) is not bool or envelope["html_saved"] != (html is not None)):
        raise SafetyError("AMAZON_TRIAL_BROWSER_ENVELOPE_INVALID")
    png = None
    if envelope.get("screenshot_base64") is not None:
        encoded = envelope["screenshot_base64"]
        if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE + 2) // 3 * 4:
            raise SafetyError("AMAZON_TRIAL_CAPTURE_TOO_LARGE")
        try:
            png = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise SafetyError("AMAZON_TRIAL_SCREENSHOT_INVALID") from None
        if len(png) > MAX_IMAGE or not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise SafetyError("AMAZON_TRIAL_SCREENSHOT_INVALID")
    return png


def save_browser_capture(trial_id, acquisition_id, envelope, *, data_root=DATA_ROOT, media_root=MEDIA_ROOT):
    """Save an actual fresh connected-tool capture offline, without navigation."""
    png = _validate_browser_envelope(envelope)
    raw_envelope = _bytes(envelope)
    if len(raw_envelope) > MAX_ENVELOPE:
        raise SafetyError("AMAZON_TRIAL_CAPTURE_TOO_LARGE")
    folder, media = _path(data_root, trial_id, acquisition_id), _path(media_root, trial_id, acquisition_id)
    if folder.exists():
        saved = replay(trial_id, acquisition_id, data_root=data_root, media_root=media_root)
        if saved["artifacts"].get("browser_envelope.json") != _hash(raw_envelope):
            raise SafetyError("AMAZON_TRIAL_ACQUISITION_INTENT_MISMATCH")
        return saved
    folder.mkdir(parents=True, exist_ok=False)
    media.mkdir(parents=True, exist_ok=False)
    intent = {"version": VERSION, "url": envelope["requested_url"], "kind": envelope["kind"], "pages": 1,
              "max_cards": MAX_CARDS, "timeout_seconds": 60, "mode": envelope["mode"]}
    _save(folder / "intent.json", _bytes(intent))
    parsed = parse_snapshot(envelope["snapshot"], intent["url"], navigation_status=envelope["navigation_status"], kind=intent["kind"])
    receipt = {"version": VERSION, "trial_id": trial_id, "acquisition_id": acquisition_id,
               "intent_hash": _hash(_bytes(intent)), "requested_url": intent["url"], "kind": intent["kind"],
               "artifacts": {}, "apify_requests": 0, "model_calls": 0, "apify_charge_usd": "0",
               "state": parsed["state"], "reason": parsed["reason"], "actual_url": envelope["snapshot"].get("url"),
               "access_success": parsed["access_success"], "scope_verified": parsed["scope_verified"],
               "usable_cards": len(parsed["records"]), "discovered_cases": parsed["cases"],
               "owned_browser_closed": None}
    for field in ("started_at", "collected_at", "completed_at", "elapsed_seconds", "navigation_status",
                  "public_page_navigations", "owned_context_closed", "stage", "mode"):
        receipt[field] = envelope[field]
    if envelope["owned_context_closed"] is False:
        receipt.update(state="failed", reason="owned_context_cleanup_unverified")
    artifacts = {"browser_envelope.json": raw_envelope, "snapshot.json": _bytes(envelope["snapshot"]), "parsed.json": _bytes(parsed)}
    if envelope.get("html") is not None:
        artifacts["rendered.html"] = envelope["html"].encode()
    if png is not None:
        artifacts["page.png"] = png
    for name, raw in artifacts.items():
        _save((media if name == "page.png" else folder) / name, raw)
        receipt["artifacts"][name] = _hash(raw)
    _save(folder / "receipt.json", _bytes(receipt))
    return receipt


async def capture_async(url, trial_id, acquisition_id, *, kind="list", data_root=DATA_ROOT,
                        media_root=MEDIA_ROOT, executable_path=None, timeout=45, page=None):
    """One page and a whole-case deadline. Injected page is fixtures only."""
    scope = list_scope(url, landing=kind == "landing")
    if not scope or kind not in ("landing", "list") or (kind == "landing") != (scope == "landing"):
        raise SafetyError("AMAZON_TRIAL_URL_INVALID")
    if type(timeout) is not int or not 10 <= timeout <= 60:
        raise SafetyError("AMAZON_TRIAL_TIMEOUT_INVALID")
    if executable_path is not None and (not Path(executable_path).is_absolute() or not Path(executable_path).is_file()):
        raise SafetyError("AMAZON_TRIAL_EXECUTABLE_INVALID")
    folder, media = _path(data_root, trial_id, acquisition_id), _path(media_root, trial_id, acquisition_id)
    try:
        folder.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        saved = replay(trial_id, acquisition_id, data_root=data_root, media_root=media_root)
        if saved["requested_url"] != url or saved["kind"] != kind:
            raise SafetyError("AMAZON_TRIAL_ACQUISITION_INTENT_MISMATCH")
        return saved
    media.mkdir(parents=True, exist_ok=False)
    intent = {"version": VERSION, "url": url, "kind": kind, "pages": 1, "max_cards": MAX_CARDS,
              "timeout_seconds": timeout, "mode": "injected_fixture" if page is not None else "fresh_owned_headless_browser"}
    _save(folder / "intent.json", _bytes(intent))
    receipt = {"version": VERSION, "trial_id": trial_id, "acquisition_id": acquisition_id,
               "intent_hash": _hash(_bytes(intent)), "requested_url": url, "kind": kind,
               "started_at": _now(), "state": "failed", "reason": "capture_incomplete", "artifacts": {},
               "navigation_status": None, "public_page_navigations": 0, "apify_requests": 0,
               "model_calls": 0, "apify_charge_usd": "0", "owned_browser_closed": True if page is not None else None,
               "blocked_browser_requests": 0, "mode": intent["mode"], "stage": "prepared",
               "driver_start_attempted": False, "driver_started": False, "driver_cleanup_verified": page is not None}
    browser = driver = starter = None
    start = time.monotonic()

    def save(name, raw, *, image=False):
        limit = MAX_IMAGE if image else MAX_HTML if name == "rendered.html" else MAX_SNAPSHOT
        if len(raw) > limit:
            raise SafetyError("AMAZON_TRIAL_CAPTURE_TOO_LARGE")
        _save((media if image else folder) / name, raw)
        receipt["artifacts"][name] = _hash(raw)

    async def run():
        nonlocal browser, driver, starter, page
        if page is None:
            receipt["stage"] = "driver_import"
            from playwright.async_api import async_playwright
            starter = async_playwright()
            receipt.update(stage="driver_start", driver_start_attempted=True)
            driver = await starter.start()
            receipt.update(stage="browser_launch", driver_started=True)
            browser = await driver.chromium.launch(headless=True, **({"executable_path": str(executable_path)} if executable_path else {}))
            receipt["owned_browser_closed"] = False
            receipt["stage"] = "context_create"
            context = await browser.new_context(locale="en-IN", viewport={"width": 1440, "height": 1100})
            async def gate(route):
                request = route.request
                if allowed_browser_request(request.url, request.method, navigation=request.is_navigation_request()):
                    await route.continue_()
                else:
                    receipt["blocked_browser_requests"] += 1
                    await route.abort()
            await context.route("**/*", gate)
            receipt["stage"] = "page_create"
            page = await context.new_page()
        receipt["stage"] = "navigation"
        receipt["public_page_navigations"] = 1
        response = await page.goto(url, wait_until="domcontentloaded", timeout=(timeout - 8) * 1000)
        receipt["navigation_status"] = response.status if response else None
        receipt["stage"] = "readiness"
        try:
            await page.wait_for_function("() => document.querySelector('.zg-bdg-text') || /captcha|robot|Best Sellers/i.test(document.body?.innerText || '')", timeout=8000)
        except Exception:
            pass
        receipt["stage"] = "snapshot"
        snapshot = await page.evaluate(SNAPSHOT_JS)
        html = snapshot.pop("html", None)
        if isinstance(html, str) and len(html.encode()) <= MAX_HTML:
            save("rendered.html", html.encode())
        else:
            receipt["html_unavailable_or_oversize"] = True
        save("snapshot.json", _bytes(snapshot))
        parsed = parse_snapshot(snapshot, url, navigation_status=receipt["navigation_status"], kind=kind)
        save("parsed.json", _bytes(parsed))
        receipt.update(state=parsed["state"], reason=parsed["reason"], actual_url=snapshot.get("url"),
                       collected_at=_now(), access_success=parsed["access_success"], scope_verified=parsed["scope_verified"],
                       usable_cards=len(parsed["records"]), discovered_cases=parsed["cases"])
        try:
            receipt["stage"] = "screenshot"
            raw = await page.screenshot(type="png", timeout=3000)
            save("page.png", raw, image=True)
        except Exception:
            receipt["screenshot_unavailable"] = True
        receipt["stage"] = "complete"

    try:
        await asyncio.wait_for(run(), timeout=timeout - 5)
    except ImportError:
        receipt.update(state="unavailable", reason="browser_dependency_unavailable", failure_stage=receipt["stage"])
    except asyncio.TimeoutError:
        receipt.update(state="failed", reason="whole_case_deadline", failure_stage=receipt["stage"],
                       diagnostic_code=receipt["stage"] + "_deadline")
    except Exception as error:
        receipt.update(state="failed", reason=str(error) if isinstance(error, SafetyError) else "owned_browser_capture_error",
                       failure_stage=receipt["stage"], exception_type=type(error).__name__)
        # Allowlisted diagnosis only: arbitrary browser text may contain cookies,
        # paths or credentials. Never copy the original exception into evidence.
        message = str(error).casefold()
        for needle, code in (("executable doesn't exist", "browser_executable_missing"),
                             ("epipe", "browser_driver_pipe_closed"), ("sandbox", "browser_sandbox_launch_failure"),
                             ("target closed", "browser_target_closed")):
            if needle in message:
                receipt["diagnostic_code"] = code
                break
    finally:
        async def close():
            try:
                if browser is not None:
                    await browser.close()
                    receipt["owned_browser_closed"] = True
            finally:
                if driver is not None:
                    await driver.stop()
                    receipt["driver_cleanup_verified"] = True
                elif starter is not None and hasattr(starter, "__aexit__"):
                    # start() may have spawned its owned Node transport before
                    # cancellation. Stop that exact manager even without a driver.
                    await starter.__aexit__(None, None, None)
                    receipt["driver_cleanup_verified"] = True
                if browser is None and receipt["driver_cleanup_verified"]:
                    receipt["owned_browser_closed"] = True
        try:
            await asyncio.wait_for(close(), timeout=5)
        except Exception:
            receipt.update(cleanup_reason="owned_browser_cleanup_unverified", owned_browser_closed=False,
                           driver_cleanup_verified=False)
            if receipt["state"] not in ("failed", "unavailable"):
                receipt.update(state="failed", reason="owned_browser_cleanup_unverified")
        if receipt["driver_start_attempted"] and not receipt["driver_cleanup_verified"]:
            receipt.update(owned_browser_closed=False, cleanup_reason="owned_driver_cleanup_unverified")
        receipt.update(completed_at=_now(), elapsed_seconds=round(time.monotonic() - start, 3))
        _save(folder / "receipt.json", _bytes(receipt))
    return receipt


def capture(*args, **kwargs):
    return asyncio.run(capture_async(*args, **kwargs))


def summary(trial_id, *, data_root=DATA_ROOT, media_root=MEDIA_ROOT):
    """Offline attempted-page metrics. No functional novelty/fit inference."""
    folder = _path(data_root, trial_id)
    receipts, cases, previous = [], [], {}
    for child in sorted(folder.iterdir()):
        if not child.is_dir():
            continue
        receipt = replay(trial_id, child.name, data_root=data_root, media_root=media_root)
        receipts.append(receipt)
    for receipt in sorted(receipts, key=lambda row: (row["started_at"], row["acquisition_id"])):
        if receipt["kind"] == "landing":
            continue
        parsed_file = folder / receipt["acquisition_id"] / "parsed.json"
        parsed = json.loads(_read(parsed_file, MAX_SNAPSHOT)) if parsed_file.exists() else {}
        category = list_scope(receipt["requested_url"])
        asins = {row["asin"] for row in parsed.get("records", [])}
        known = previous.get(category)
        cases.append({"acquisition_id": receipt["acquisition_id"], "category": category,
                      "state": receipt["state"], "reason": receipt["reason"], "usable_cards": len(asins),
                      "prices_present": sum(row["raw_price"] is not None for row in parsed.get("records", [])),
                      "images_present": sum(row["image_url"] is not None for row in parsed.get("records", [])),
                      "previous_overlap": len(asins & known) if known is not None else None,
                      "new_asins_since_previous": len(asins - known) if known is not None else None})
        if asins:
            previous[category] = asins
    attempted = [row for row in receipts if row["kind"] == "list"]
    return {"version": VERSION, "trial_id": trial_id, "list_attempts": len(attempted),
            "access_successes": sum(row.get("access_success") is True for row in attempted),
            "correct_scope_successes": sum(row.get("scope_verified") is True for row in attempted),
            "usable_captures": sum(row["state"] == "usable" for row in attempted),
            "partially_usable_captures": sum(row["state"] == "partial" for row in attempted),
            "cases": cases, "apify_charge_usd": "0", "model_calls": 0,
            "functional_novelty": "Not assessed", "recommendation_yield": "Not assessed"}
