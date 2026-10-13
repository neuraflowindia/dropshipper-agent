"""Save and reuse one known supplier's bounded page capture; no interpretation.

An isolated per-capture lock prevents concurrent duplicate page reads. The
financial journal is used only for short research snapshots, never HTTP waits.
Saved receipts retain their original dates, including failures and partial work.
"""

from copy import deepcopy
from pathlib import Path
import re

from .models import SafetyError, canonical, digest, identifier, timestamp
from .brand_reviews import current_brand
from .public_pages import _public_url, fetch_public_page
from .research_memory import key
from .storage import Journal, atomic_json, load_json, safe_path
from .supplier_capture import capture_supplier_bundle
from .supplier_effort import record_supplier_effort
from .suppliers import validate_supplier_input

MAX_CAPTURE_BYTES = 8 * 1024 * 1024
_RECORD_FIELDS = {"schema_version", "capture_id", "request", "product", "description_hash",
                  "input_hash", "bundle", "bundle_hash"}
_BUNDLE_FIELDS = {"schema_version", "state", "concept_id", "lead_id", "input_hash", "item",
                  "receipts", "failures", "retrieval", "client_ready"}


def _request(request):
    if (not isinstance(request, dict) or set(request) not in ({"concept_id", "lead"}, {"concept_id", "lead", "refresh"})
            or not isinstance(request["lead"], dict) or set(request["lead"]) != {"id", "urls"}
            or not isinstance(request["lead"]["urls"], list)
            or not 1 <= len(request["lead"]["urls"]) <= 3
            or any(not isinstance(url, str) or len(url) > 4096 for url in request["lead"]["urls"])):
        raise SafetyError("SUPPLIER_CAPTURE_REQUEST_INVALID")
    identifier(request["concept_id"])
    identifier(request["lead"]["id"])
    # Unlike a transient rejected receipt, this store retains the full request.
    # Refuse credentials/secret query fields before they can enter a saved file.
    for url in request["lead"]["urls"]:
        _public_url(url)
    if "refresh" in request:
        refresh = request["refresh"]
        if (not isinstance(refresh, dict) or set(refresh) != {"previous_capture_id", "reason", "requested_at"}
                or not isinstance(refresh["previous_capture_id"], str)
                or not re.fullmatch(r"supplier-capture-[0-9a-f]{40}", refresh["previous_capture_id"])
                or not isinstance(refresh["reason"], str) or not 1 <= len(refresh["reason"]) <= 500
                or not refresh["reason"].strip()
                or not isinstance(refresh["requested_at"], str) or len(refresh["requested_at"]) > 64):
            raise SafetyError("SUPPLIER_CAPTURE_REFRESH_INVALID")
        try:
            timestamp(refresh["requested_at"])
        except SafetyError:
            raise SafetyError("SUPPLIER_CAPTURE_REFRESH_INVALID") from None
    return deepcopy(request)


def _capture_key(product, request):
    binding = [product, request["lead"]]
    if "refresh" in request:
        binding.append(request["refresh"])
    return key("supplier-capture", binding)


def _product(memory, concept_id, *, state=None):
    state = memory.snapshot() if state is None else state
    concept = state["concepts"].get(concept_id)
    if not concept:
        raise SafetyError("KNOWN_CONCEPT_REQUIRED")
    description = concept.get("description")
    if not isinstance(description, dict):
        raise SafetyError("SUPPLIER_CAPTURE_DESCRIPTION_INVALID")
    if (concept.get("status") in ("excluded", "merged", "retired")
            or concept.get("identity_conflicted") or current_brand(state, "concept", concept_id, description) == "established"):
        raise SafetyError("SUPPLIER_CAPTURE_CONCEPT_EXCLUDED")
    return {"concept_id": concept_id, "description": deepcopy(description)}


def _validate(record, *, capture_id, request, product):
    """Refuse corrupt or differently bound saved evidence, without refetching."""
    try:
        expected_hash = digest([product, request["lead"]])
        if (not isinstance(record, dict) or set(record) != _RECORD_FIELDS
                or type(record["schema_version"]) is not int
                or record["schema_version"] != (2 if "refresh" in request else 1)
                or record["capture_id"] != capture_id or capture_id != _capture_key(product, request)
                or record["request"] != request
                or record["product"] != product
                or record["description_hash"] != digest(product["description"])
                or record["input_hash"] != expected_hash
                or record["bundle_hash"] != digest(record["bundle"])):
            raise ValueError
        bundle = record["bundle"]
        if (not isinstance(bundle, dict) or set(bundle) != _BUNDLE_FIELDS
                or type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1
                or bundle["concept_id"] != request["concept_id"]
                or bundle["lead_id"] != request["lead"]["id"]
                or bundle["input_hash"] != expected_hash or bundle["client_ready"] is not False
                or bundle["state"] not in ("usable", "partial", "no_usable_pages")
                or not isinstance(bundle["receipts"], list) or not isinstance(bundle["failures"], list)
                or len(bundle["receipts"]) > 7 or len(bundle["failures"]) > 7
                or not isinstance(bundle["retrieval"], dict)
                or set(bundle["retrieval"]) != {"requested", "fetched", "linked_page"}
                or bundle["retrieval"]["requested"] != len(request["lead"]["urls"])
                or type(bundle["retrieval"]["fetched"]) is not int
                or not 0 <= bundle["retrieval"]["fetched"] <= 4):
            raise ValueError
        for receipt in bundle["receipts"]:
            if not isinstance(receipt, dict):
                raise ValueError
        if bundle["retrieval"]["linked_page"] is not None and not isinstance(bundle["retrieval"]["linked_page"], str):
            raise ValueError
        item = bundle["item"]
        if item is None:
            if bundle["state"] != "no_usable_pages":
                raise ValueError
        else:
            validate_supplier_input(item)
            if (item["product"] != dict(product["description"], concept_id=product["concept_id"])
                    or item["id"] != "supplier-item-" + digest([product["concept_id"], request["lead"]["id"]])[:40]
                    or len(item["pages"]) > 4
                    or bundle["state"] != ("partial" if bundle["failures"] else "usable")):
                raise ValueError
            # A malformed reader receipt may be retained as failed evidence,
            # but only the original complete usable receipt can back an F page.
            for page in item["pages"]:
                receipts = [row for row in bundle["receipts"] if row.get("id") == page["id"]]
                if (len(receipts) != 1 or receipts[0].get("state") != "usable"
                        or receipts[0].get("body_complete") is not True
                        or receipts[0].get("http_status") != 200
                        or receipts[0].get("final_url") != page["url"]
                        or receipts[0].get("text") != page["text"]):
                    raise ValueError
                identifier(receipts[0]["id"])
                timestamp(receipts[0].get("checked_at"))
        return bundle
    except (KeyError, TypeError, ValueError, SafetyError):
        raise SafetyError("SAVED_SUPPLIER_CAPTURE_INVALID") from None


def _validate_previous_capture(memory, request, product):
    """Validate the named previous record once; never traverse its ancestors."""
    refresh = request.get("refresh")
    if refresh is None:
        return
    try:
        capture_id = refresh["previous_capture_id"]
        path = safe_path(memory.root, "supplier_pages", capture_id) / "capture.json"
        previous = load_json(path, MAX_CAPTURE_BYTES)
        previous_request = _request(previous["request"])
        if (previous_request["concept_id"] != request["concept_id"]
                or previous_request["lead"] != request["lead"]):
            raise ValueError
        _validate(previous, capture_id=capture_id, request=previous_request, product=product)
    except (KeyError, TypeError, ValueError, SafetyError):
        raise SafetyError("SUPPLIER_CAPTURE_REFRESH_PREVIOUS_INVALID") from None


def capture_saved_supplier_bundle(memory, request, *, fetcher=fetch_public_page):
    """Capture explicit known-lead URLs once and durably retain original receipts.

    Return ``capture_id``, ``path``, ``bundle`` and ``reused``. A repeat includes
    saved failures; it neither retries pages nor refreshes dates. An explicit
    refresh intent names a matching prior capture and creates a separate saved
    revisit; replay of that intent also reuses its original receipt dates.
    Changing the trusted description or lead produces a different immutable
    capture key.
    This operation never invokes F, changes concepts or confirms suppliers.
    After the immutable capture is verified, an idempotent canonical source
    check records actual supplier effort, including bounded failed reads.
    """
    request = _request(request)
    if not callable(fetcher):
        raise SafetyError("SUPPLIER_CAPTURE_REQUEST_INVALID")
    product = _product(memory, request["concept_id"])
    capture_id = _capture_key(product, request)
    folder = safe_path(memory.root, "supplier_pages", capture_id)
    path = folder / "capture.json"
    # This lock is deliberately NOT memory.journal (financial authority).
    with Journal(folder).locked():
        if _product(memory, request["concept_id"]) != product:
            raise SafetyError("SUPPLIER_CAPTURE_DESCRIPTION_CHANGED")
        _validate_previous_capture(memory, request, product)
        if path.is_symlink():
            raise SafetyError("SAVED_SUPPLIER_CAPTURE_INVALID")
        reused = path.exists()
        if reused:
            try:
                record = load_json(path, MAX_CAPTURE_BYTES)
            except SafetyError:
                raise SafetyError("SAVED_SUPPLIER_CAPTURE_INVALID") from None
        else:
            bundle = capture_supplier_bundle(product, request["lead"], fetcher=fetcher)
            record = {"schema_version": 2 if "refresh" in request else 1, "capture_id": capture_id, "request": request,
                      "product": product, "description_hash": digest(product["description"]),
                      "input_hash": digest([product, request["lead"]]),
                      "bundle": bundle, "bundle_hash": digest(bundle)}
            _validate(record, capture_id=capture_id, request=request, product=product)
            if len(canonical(record)) > MAX_CAPTURE_BYTES:
                raise SafetyError("SAVED_SUPPLIER_CAPTURE_SIZE_LIMIT")
            atomic_json(path, record)
        bundle = _validate(record, capture_id=capture_id, request=request, product=product)
        # Preserve completed capture even if the concept changes during HTTP;
        # it cannot then be mistaken for current interpretation input.
        if _product(memory, request["concept_id"]) != product:
            raise SafetyError("SUPPLIER_CAPTURE_DESCRIPTION_CHANGED")
        # Failed/malformed sibling receipts are retained verbatim in the saved
        # bundle. Only an independently bound page ID/URL may prove effort.
        admitted_urls = {row for row in request["lead"]["urls"]}
        if bundle["retrieval"]["linked_page"] is not None:
            admitted_urls.add(bundle["retrieval"]["linked_page"])
        admitted_urls = {_public_url(url)[0] for url in admitted_urls}
        expected_requests = {"supplier-page-" + digest([request["lead"]["id"], url])[:40]: url for url in admitted_urls}
        effort = record_supplier_effort(memory, concept_id=request["concept_id"], description=product["description"],
                 pass_id=capture_id, receipts=bundle["receipts"], receipt_reference=str(path),
                 outcome="interpretation_pending" if bundle["item"] is not None else "failed",
                 expected_requests=expected_requests)
        return {"capture_id": capture_id, "path": str(path), "bundle": deepcopy(bundle), "reused": reused,
                "effort_check_id": effort["check_id"], "effort_satisfied": effort["record"]["effort_satisfied"]}


def load_saved_supplier_capture(memory, path, *, state=None):
    """Read one verified current bundle, including its original failed reads.

    Failed pages are proof of an unsuccessful bounded read, not model input or
    proof that a supplier does not exist. Exact request, product, bundle and
    original receipt checks are identical to the successful-input loader.
    A supplied verified state is an invocation-local preparation view only;
    execution/application callers must retain their fresh currentness check.
    """
    if not isinstance(path, (str, Path)):
        raise SafetyError("SUPPLIER_CAPTURE_PATH_INVALID")
    candidate = Path(path).absolute()
    root = safe_path(memory.root, "supplier_pages")
    if candidate.name != "capture.json" or candidate.parent.parent != root:
        raise SafetyError("SUPPLIER_CAPTURE_PATH_INVALID")
    safe_path(candidate.parent)
    try:
        record = load_json(candidate, MAX_CAPTURE_BYTES)
        request = _request(record["request"])
        product = record["product"]
        if (not isinstance(product, dict) or set(product) != {"concept_id", "description"}
                or product["concept_id"] != request["concept_id"] or not isinstance(product["description"], dict)):
            raise ValueError
        capture_id = _capture_key(product, request)
        if candidate != root / capture_id / "capture.json":
            raise SafetyError("SUPPLIER_CAPTURE_PATH_INVALID")
        bundle = _validate(record, capture_id=capture_id, request=request, product=product)
    except SafetyError as error:
        if str(error) == "SUPPLIER_CAPTURE_PATH_INVALID":
            raise
        raise SafetyError("SAVED_SUPPLIER_CAPTURE_INVALID") from None
    except (KeyError, TypeError, ValueError):
        raise SafetyError("SAVED_SUPPLIER_CAPTURE_INVALID") from None
    if _product(memory, request["concept_id"], state=state) != product:
        raise SafetyError("SUPPLIER_CAPTURE_DESCRIPTION_CHANGED")
    return {"product": deepcopy(product), "request": deepcopy(request),
            "bundle": deepcopy(bundle), "capture_id": capture_id, "path": str(candidate)}


def load_saved_supplier_input(memory, path):
    """Read trusted current F input without fetching or refreshing anything.

    Original receipt dates accompany the unchanged model item; a new
    interpretation date must not be mistaken for a new page observation.
    """
    captured = load_saved_supplier_capture(memory, path)
    bundle, capture_id = captured["bundle"], captured["capture_id"]
    if bundle["item"] is None:
        raise SafetyError("SUPPLIER_CAPTURE_NO_USABLE_PAGES")
    page_dates = {page["id"]: next(row["checked_at"] for row in bundle["receipts"] if row.get("id") == page["id"])
                  for page in bundle["item"]["pages"]}
    return {"item": deepcopy(bundle["item"]), "page_dates": page_dates, "capture_id": capture_id}
