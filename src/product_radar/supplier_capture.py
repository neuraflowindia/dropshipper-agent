"""Capture a known supplier's bounded public pages, without interpreting terms."""

from copy import deepcopy
import re
from urllib.parse import parse_qs, unquote, urlsplit

from .models import SafetyError, digest, identifier, timestamp, utc_now
from .public_pages import _public_url, fetch_public_page
from .supplier_effort import failed_reader_receipt
from .suppliers import validate_supplier_input

_TERMS = {
    "pricing": 0, "prices": 0, "pricing-plans": 0,
    "faq": 1, "faqs": 1, "frequently-asked-questions": 1,
    "shipping": 2, "shipping-policy": 2, "shipping-delivery": 2,
    "terms": 3, "terms-and-conditions": 3, "terms-of-service": 3, "terms-conditions": 3,
}
_UNSAFE_PATH = re.compile(r"(?:^|/)(?:login|signin|sign-in|customer-login|account|my-account|cart|checkout|contact|contact-us|contact-form|callback|oauth|auth|authorize|register|logout)(?:\.(?:html?|php|aspx?))?(?:/|$)")


def _allowed_url(value):
    clean, host, port, scheme = _public_url(value)
    parsed = urlsplit(clean)
    path = unquote(parsed.path).casefold().replace("_", "-").rstrip("/")
    if _UNSAFE_PATH.search(path) or {"callback", "redirect-uri", "redirect-url", "session"} & {
            name.casefold().replace("_", "-") for name in parse_qs(parsed.query, keep_blank_values=True)}:
        raise SafetyError("SUPPLIER_NON_RESEARCH_URL")
    return clean, (scheme, host.casefold(), port)


def _terms_priority(url):
    parts = unquote(urlsplit(url).path).casefold().replace("_", "-").strip("/").split("/")
    parts = [re.sub(r"\.(?:html?|php|aspx?)$", "", part) for part in parts]
    return min((_TERMS[part] for part in parts if part in _TERMS), default=None)


def capture_supplier_bundle(product, lead, *, fetcher=fetch_public_page):
    """Return one F-compatible item plus original receipts, never a lead verdict.

    ``product`` has concept_id and current description (dict or text). ``lead``
    has id and 1–3 actually discovered URLs. No URL is synthesized. The caller
    persists/reuses this result; saved receipts retain their original dates.
    At most three seed requests plus one linked terms request are made.
    F receives the flattened current description plus the trusted concept_id,
    so its saved interpretation joins the same product in report selection.
    Text-only callers receive concept_id/description rather than a bare string.
    """
    if (not isinstance(product, dict) or not isinstance(product.get("description"), (dict, str))
            or not isinstance(lead, dict) or not isinstance(lead.get("urls"), list)
            or not 1 <= len(lead["urls"]) <= 3 or not callable(fetcher)
            or any(not isinstance(url, str) for url in lead["urls"])):
        raise SafetyError("SUPPLIER_BUNDLE_INPUT_INVALID")
    concept_id, lead_id = identifier(product.get("concept_id")), identifier(lead.get("id"))
    description = deepcopy(product["description"])
    scoped_product = dict(description, concept_id=concept_id) if isinstance(description, dict) else {
        "concept_id": concept_id, "description": description}
    receipts, failures, pages, seeds, ids = [], [], [], [], {}

    def page_id(url):
        value = "supplier-page-" + digest([lead_id, url])[:40]
        if value in ids and ids[value] != url:
            raise SafetyError("SUPPLIER_PAGE_ID_COLLISION")
        ids[value] = url
        return value

    # Validate and deduplicate all seeds before any fetch. Invalid URLs are
    # rejected without passing credentials or arbitrary text to the reader.
    for index, raw in enumerate(lead["urls"]):
        try:
            url, origin = _allowed_url(raw)
        except SafetyError as error:
            rejected_id = "rejected-page-" + str(index + 1)
            receipts.append({"id": rejected_id, "state": "rejected", "reason": str(error),
                             "requested_url": None, "final_url": None, "checked_at": utc_now(), "no_request": True})
            failures.append({"id": rejected_id, "state": "rejected", "reason": str(error)})
            continue
        if url not in {row[0] for row in seeds}:
            seeds.append((url, page_id(url), origin))
    seen, linked_candidates, fetched = set(), [], 0

    def retrieve(url, source_id, *, linked_origin=None):
        nonlocal fetched
        if url in seen:
            return
        seen.add(url)
        fetched += 1
        try:
            receipt = fetcher({"id": source_id, "url": url})
        except Exception as error:
            # A shared original which has not finished is not a completed
            # failed capture. Keep this product's bundle open for local replay.
            from .company_pages import CompanyPagePending
            if isinstance(error, CompanyPagePending):
                raise
            receipt = failed_reader_receipt({"id": source_id, "url": url}, "SUPPLIER_PUBLIC_FETCH_FAILED", error)
        if not isinstance(receipt, dict):
            receipt = {"id": source_id, "state": "unavailable", "reason": "SUPPLIER_PUBLIC_RECEIPT_INVALID",
                       "requested_url": url, "final_url": None, "checked_at": utc_now()}
        receipts.append(deepcopy(receipt))
        reason = None
        if receipt.get("state") != "usable":
            reason = receipt.get("reason", "SUPPLIER_PUBLIC_PAGE_UNAVAILABLE")
            if not isinstance(reason, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", reason):
                reason = "SUPPLIER_PUBLIC_PAGE_UNAVAILABLE"
        else:
            try:
                final, origin = _allowed_url(receipt.get("final_url"))
                if (receipt.get("id") != source_id or receipt.get("requested_url") != url
                        or receipt.get("http_status") != 200 or receipt.get("body_complete") is not True
                        or not isinstance(receipt.get("text"), str) or not receipt["text"].strip()
                        or not isinstance(receipt.get("discovered_links"), list)
                        or len(receipt["discovered_links"]) > 60):
                    raise SafetyError("SUPPLIER_PUBLIC_RECEIPT_INVALID")
                timestamp(receipt.get("checked_at"))
                if linked_origin is not None and origin != linked_origin:
                    raise SafetyError("SUPPLIER_LINKED_ORIGIN_CHANGED")
                page = {"id": source_id, "url": final, "title": receipt.get("title"), "text": receipt["text"]}
                validate_supplier_input({"id": lead_id, "product": scoped_product, "pages": [page]})
                pages.append(page)
                seen.add(final)
                if linked_origin is None:
                    for raw_link in receipt["discovered_links"]:
                        try:
                            linked, linked_site = _allowed_url(raw_link)
                            priority = _terms_priority(linked)
                            if linked_site == origin and priority is not None:
                                linked_candidates.append((priority, linked, origin))
                        except SafetyError:
                            continue
            except SafetyError as error:
                reason = str(error)
        if reason:
            failures.append({"id": source_id, "state": "unavailable" if receipt.get("state") == "usable" else receipt.get("state", "unavailable"),
                             "reason": reason})

    for url, source_id, _ in seeds:
        retrieve(url, source_id)
    linked_page = None
    for _, url, origin in sorted(set(linked_candidates)):
        if url not in seen:
            linked_page = url
            retrieve(url, page_id(url), linked_origin=origin)
            break  # No recursive crawl or second paid/read retry.
    item = None
    if pages:
        item = {"id": "supplier-item-" + digest([concept_id, lead_id])[:40], "product": scoped_product, "pages": pages}
        validate_supplier_input(item)
    return {"schema_version": 1, "state": "partial" if pages and failures else "usable" if pages else "no_usable_pages",
            "concept_id": concept_id, "lead_id": lead_id, "input_hash": digest([product, lead]),
            "item": item, "receipts": receipts, "failures": failures,
            "retrieval": {"requested": len(lead["urls"]), "fetched": fetched, "linked_page": linked_page},
            "client_ready": False}
