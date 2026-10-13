"""One useful, saved Trends question through the public browser route.

Plans are bound to a known product and a reviewed search phrase. Code owns
collection, reuse and arithmetic; no model or paid fallback runs here.
"""

from pathlib import Path
import hashlib

from .models import SafetyError, digest, identifier, timestamp, utc_now
from .brand_reviews import brand_binding, current_brand
from .research_memory import change, key
from .research_operations import import_trends_evidence
from .report_selection import build_report_pool
from .runtime_storage import runtime_path
from .storage import Journal, atomic_json, load_json, safe_path
from .trends_capture import validate_saved_capture


def prepare_trends_check(memory, *, concept_id, term=None, as_of=None):
    identifier(concept_id)
    supplied_term = term is not None
    if term is None:
        from .product_queries import product_query_terms
        term = product_query_terms(memory, concept_id, source="trends", limit=1)[0]["text"]
    if (not isinstance(term, str) or not term.strip() or len(term) > 120
            or any(ord(c) < 32 for c in term)):
        raise SafetyError("TRENDS_TERM_INVALID")
    at = as_of or utc_now()
    timestamp(at)
    state = memory.snapshot()
    concept = state["concepts"].get(concept_id)
    if concept is None:
        raise SafetyError("KNOWN_CONCEPT_REQUIRED")
    from .service import load_policy
    from .fit_policy import FIT_POLICY_ID, LEGACY_POLICY_ID
    policy_id = load_policy().get("fit_policy_id", LEGACY_POLICY_ID)
    pool = build_report_pool(memory, as_of=at, fit_policy_id=policy_id)
    if not any(row["concept_id"] == concept_id for row in pool["needed_market_work"]):
        raise SafetyError("USEFUL_MARKET_CHECK_REQUIRED")
    value = {"schema_version": 1, "concept_id": concept_id,
             "description_hash": digest(concept["description"]), "term": term.strip(),
             "planned_at": at, "route": "public_playwright_csv",
             "reason": "A passing product needs another independent market check.",
             "phrase_basis": "operator_selected_product_type" if supplied_term else "shared_product_query_profile",
             "acquisition_id": key("trends-browser", [concept_id, term.strip(), at])}
    if policy_id == FIT_POLICY_ID:
        value.update(schema_version=2, fit_policy_id=policy_id,
                     brand_binding=brand_binding(state, "concept", concept_id, concept["description"]))
    value["id"] = key("trends-check", value)
    folder = safe_path(memory.root, "trends_checks", value["id"])
    path = folder / "request.json"
    if path.exists() and load_json(path) != value:
        raise SafetyError("TRENDS_CHECK_CONFLICT")
    atomic_json(path, value)
    return value


def _validate_request(memory, request):
    fields = {"schema_version", "concept_id", "description_hash", "term", "planned_at", "route",
              "reason", "phrase_basis", "acquisition_id", "id"}
    version = request.get("schema_version") if isinstance(request, dict) else None
    if version == 2:
        fields |= {"fit_policy_id", "brand_binding"}
    if (not isinstance(request, dict) or set(request) != fields or version not in (1, 2)
            or request["route"] != "public_playwright_csv"
            or key("trends-check", {k: v for k, v in request.items() if k != "id"}) != request["id"]):
        raise SafetyError("TRENDS_CHECK_INVALID")
    identifier(request["id"]); identifier(request["acquisition_id"])
    timestamp(request["planned_at"])
    folder = safe_path(memory.root, "trends_checks", request["id"])
    if load_json(folder / "request.json") != request:
        raise SafetyError("TRENDS_CHECK_CONFLICT")
    state = memory.snapshot()
    current = state["concepts"].get(request["concept_id"])
    if current is None or digest(current["description"]) != request["description_hash"]:
        raise SafetyError("TRENDS_PRODUCT_CHANGED")
    if current.get("status") in ("excluded", "retired", "merged") or current.get("identity_conflicted"):
        raise SafetyError("TRENDS_PRODUCT_INACTIVE")
    if current_brand(state, "concept", request["concept_id"], current["description"]) == "established":
        raise SafetyError("TRENDS_PRODUCT_INACTIVE")
    if version == 2:
        from .fit_policy import FIT_POLICY_ID
        if (request["fit_policy_id"] != FIT_POLICY_ID or
                request["brand_binding"] != brand_binding(state, "concept", request["concept_id"], current["description"])):
            raise SafetyError("TRENDS_PRODUCT_BRAND_CHANGED")
    return folder


def execute_trends_check(memory, request, *, root, capture, browser_path=None):
    """An unchanged request resumes its receipt, including a failed attempt.

    No implicit retry or transition to Apify. A failed request is a saved
    observation, not poor demand. Replaying valid evidence makes no model calls.
    """
    with Journal(memory.root / "trends_checks").locked():
        folder = _validate_request(memory, request)
        capture_root = runtime_path(root, "media/live/trends")
        receipt_path = folder / "capture.json"
        if receipt_path.exists():
            receipt = load_json(receipt_path)
        else:
            saved_receipt = safe_path(capture_root, request["acquisition_id"]) / "receipt.json"
            if saved_receipt.exists():
                receipt = load_json(saved_receipt)
            else:
                from .service import load_policy
                from .fit_policy import LEGACY_POLICY_ID
                pool = build_report_pool(memory, fit_policy_id=load_policy().get("fit_policy_id", LEGACY_POLICY_ID))
                if not any(row["concept_id"] == request["concept_id"] for row in pool["needed_market_work"]):
                    return {"request_id": request["id"], "concept_id": request["concept_id"],
                            "state": "No longer needed", "evidence_id": None,
                            "provider_requests": 0, "model_calls": 0, "client_ready": False}
                receipt = capture(request["term"], request["acquisition_id"], root=capture_root,
                                  browser_path=browser_path)
            atomic_json(receipt_path, receipt)
        _validate_request(memory, request)
        if not isinstance(receipt, dict):
            raise SafetyError("TRENDS_RECEIPT_INVALID")
        if receipt.get("acquisition_id") != request["acquisition_id"] or receipt.get("term") != request["term"]:
            raise SafetyError("TRENDS_RECEIPT_MISMATCH")
        at = receipt.get("collected_at", receipt.get("completed_at"))
        timestamp(at)
        if timestamp(at) < timestamp(request["planned_at"]):
            raise SafetyError("TRENDS_RECEIPT_PREDATES_REQUEST")
        state = receipt.get("state")
        if state not in ("Usable", "Blocked", "Insufficient", "Failed", "Unavailable"):
            raise SafetyError("TRENDS_RECEIPT_INVALID")
        imported = None
        if state == "Usable":
            required = ("csv_path", "term", "geo", "time_range", "kind", "property", "scope_verified", "source_reference")
            if any(field not in receipt for field in required) or not isinstance(receipt["csv_path"], str):
                raise SafetyError("TRENDS_RECEIPT_INVALID")
            csv_path = safe_path(receipt["csv_path"])
            expected_csv = safe_path(capture_root, request["acquisition_id"]) / "multiTimeline.csv"
            if csv_path != expected_csv:
                raise SafetyError("TRENDS_CAPTURE_PATH_INVALID")
            original = validate_saved_capture(capture_root, request["acquisition_id"], request["term"])
            if original != receipt:
                raise SafetyError("TRENDS_RECEIPT_CONFLICT")
            raw = csv_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != receipt.get("csv_sha256"):
                raise SafetyError("TRENDS_CAPTURE_HASH_MISMATCH")
            metadata = {k: receipt[k] for k in ("term", "geo", "time_range", "kind", "property", "scope_verified", "source_reference")}
            metadata["collected_at"] = at
            from .providers.trends_browser import parse_trends_csv
            if parse_trends_csv(raw, metadata)["state"] != "Usable":
                raise SafetyError("TRENDS_RECEIPT_NOT_USABLE")
            imported = import_trends_evidence(memory, concept_id=request["concept_id"],
                acquisition_id=request["acquisition_id"], csv_bytes=raw, metadata=metadata)
            if imported["parsed"]["state"] != "Usable":
                raise SafetyError("TRENDS_RECEIPT_NOT_USABLE")
        check = {"source": "trends", "route": request["route"], "at": at,
                 "state": state.lower(), "reason": receipt.get("reason", state),
                 "reference": str(receipt_path), "concept_id": request["concept_id"],
                 "description_hash": request["description_hash"], "term": request["term"]}
        memory.put("source_checks", key("source-check", [request["id"], digest(check)]), check)
        result = {"request_id": request["id"], "concept_id": request["concept_id"],
                  "state": state, "evidence_id": imported["id"] if imported else None,
                  "provider_requests": 0, "model_calls": 0, "client_ready": False}
        atomic_json(folder / "result.json", result)
        return result


def review_trends_scope(memory, review):
    """Correct one chart's product meaning without editing its CSV or date."""
    fields = {"evidence_id", "expected_evidence_hash", "expected_description_hash", "relationship",
              "reason", "reviewer", "reviewed_at"}
    if (not isinstance(review, dict) or set(review) != fields
            or review["relationship"] not in ("exact_product", "relevant_family", "unrelated", "unclear")
            or not isinstance(review["reason"], str) or not review["reason"].strip()):
        raise SafetyError("TRENDS_SCOPE_REVIEW_INVALID")
    identifier(review["evidence_id"]); identifier(review["reviewer"]); timestamp(review["reviewed_at"])
    if timestamp(review["reviewed_at"]) > timestamp(utc_now()):
        raise SafetyError("TRENDS_SCOPE_REVIEW_IN_FUTURE")
    review_id = key("trends-scope-review", review)
    state = memory.snapshot()
    prior = state["source_checks"].get(review_id)
    if prior:
        if prior.get("review") != review:
            raise SafetyError("TRENDS_SCOPE_REVIEW_CONFLICT")
        return {"applied": False, "review_id": review_id}
    evidence = state["trends_evidence"].get(review["evidence_id"])
    def unchanged(current):
        actual = current["trends_evidence"].get(review["evidence_id"])
        product = current["concepts"].get(actual.get("concept_id")) if actual else None
        if (not actual or not product or digest(actual) != review["expected_evidence_hash"]
                or digest(product["description"]) != review["expected_description_hash"]):
            raise SafetyError("TRENDS_SCOPE_REVIEW_DEPENDENCY_CHANGED")
        collected_at = actual.get("parsed", {}).get("metadata", {}).get("collected_at")
        if collected_at and timestamp(collected_at) > timestamp(review["reviewed_at"]):
            raise SafetyError("TRENDS_SCOPE_REVIEW_PREDATES_EVIDENCE")
    unchanged(state)
    record = {"kind": "trends_scope_review", "version": "trends-scope-review-v1",
              "source": "trends", "route": "saved_scope_review", "reference": review_id,
              "at": review["reviewed_at"], "state": "usable" if review["relationship"] in ("exact_product", "relevant_family") else "insufficient",
              "concept_id": evidence["concept_id"], "reason": review["reason"], "review": dict(review)}
    applied = memory.commit(review_id, [change("source_checks", review_id, record)], validate=unchanged)
    return {"applied": applied, "review_id": review_id}
