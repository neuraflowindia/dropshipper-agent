"""Literal saved-page candidates for one scoped F review, never a new crawl.

Sections are supplied choices, not automatic product matches. F selects actual
plain-text offers and quotes their labels/units; code derives their references.
Structured markup is optional. No hostname or target label becomes identity.
"""

from copy import deepcopy
import re

from .models import SafetyError, digest, identifier
from .research_memory import key
from .supplier_terms import (VERSION, PRODUCT_FIELDS, scoped_item_from_choice,
                             validate_scoped_supplier_result)

KIND = "supplier-candidate-review-v1"
MODE = "supplier_scope_candidates"
WORKFLOW = "Single-piece purchase and direct customer dispatch; subscription and fees must be stated separately."


def _sections(page):
    rows, start, source = [], 0, page["text"]
    while start < len(source):
        end = min(len(source), start + 6000)
        if end < len(source):
            split = max(source.rfind("\n", start, end), source.rfind(" ", start, end))
            if split > start:
                end = split + 1
        excerpt = source[start:end]
        rows.append({"id": key("supplier-section", [page["url"], start, end, excerpt]),
                     "page_id": page["id"], "start": start, "end": end, "excerpt": excerpt})
        start = end
    return rows


def prepare_candidate_items(state, capture):
    from .functional_types import effective_functional_definition
    meaning = effective_functional_definition(state, capture["product"]["concept_id"])
    product = {name: deepcopy(meaning[name]) for name in PRODUCT_FIELDS}
    pages = capture["bundle"]["item"]["pages"]
    # A visible registered identifier is a literal option, not a supplier
    # guarantee. Other legal-name/address identities can be selected from the
    # primary page's ordinary text by F and checked against an exact quotation.
    patterns = (r"(?<!\w)\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9](?!\w)",
                r"(?<!\w)[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}(?!\w)")
    identity_pages = {}
    for page in pages:
        quoted = sorted({match.group(0) for pattern in patterns for match in re.finditer(pattern, page["text"])})
        if quoted:
            identity_pages[page["id"]] = dict(deepcopy(page), text="\n".join(quoted))
    result = []
    for page in pages:
        supporting = [deepcopy(p) for pid, p in sorted(identity_pages.items()) if pid != page["id"]]
        item = {"id": key("supplier-candidate", [page["url"], product, WORKFLOW]),
                "product": product, "workflow": WORKFLOW, "primary_page_id": page["id"],
                "section_refs": _sections(page), "pages": [deepcopy(page)] + supporting}
        validate_candidate_item(item)
        result.append(item)
    return result


def validate_candidate_item(item, *, aliased=False):
    if (not isinstance(item, dict) or set(item) != {"id", "product", "workflow", "primary_page_id", "section_refs", "pages"}
            or item["workflow"] != WORKFLOW or not isinstance(item["section_refs"], list)):
        raise SafetyError("SUPPLIER_CANDIDATE_INPUT_INVALID")
    from .suppliers import validate_supplier_input
    validate_supplier_input({"id": item["id"], "product": item["product"], "pages": item["pages"]})
    pages = {p["id"]: p for p in item["pages"]}
    if item["primary_page_id"] not in pages:
        raise SafetyError("SUPPLIER_CANDIDATE_PAGE_INVALID")
    page = pages[item["primary_page_id"]]
    def mapped(value, prefix):
        return prefix + "_" + digest(value)[:16] if aliased else value
    expected = _sections(page)
    for row in expected:
        row["id"] = mapped(row["id"], "section")
    if (item["section_refs"] != expected or not 1 <= len(expected) <= 8
            or item["id"] != mapped(key("supplier-candidate", [page["url"], item["product"], WORKFLOW]), "candidate")):
        raise SafetyError("SUPPLIER_CANDIDATE_BINDING_INVALID")
    # Reuse the exact functional meaning and actual-page validation of the
    # scoped contract without inventing a concept or an offer.
    scoped_item_from_choice(item["product"], item["pages"], {"scope_kind": "unclear",
        "page_ids": [page["id"]], "company_identity": None, "workflow": WORKFLOW, "offer": None})
    return item


def candidate_aliases(items):
    encoded, maps = [], {}
    for item in items:
        validate_candidate_item(item)
        refs = {item["id"]: "candidate_" + digest(item["id"])[:16]}
        refs.update({p["id"]: "page_" + digest(p["id"])[:16] for p in item["pages"]})
        refs.update({s["id"]: "section_" + digest(s["id"])[:16] for s in item["section_refs"]})
        if len(set(refs.values())) != len(refs):
            raise SafetyError("AI_SUPPLIER_ALIAS_COLLISION")
        def replace(value, field=None):
            if isinstance(value, str):
                return refs.get(value, value) if field in {"id", "page_id", "primary_page_id", "section_ids", "references"} else value
            if isinstance(value, list):
                return [replace(v, field) for v in value]
            if isinstance(value, dict):
                return {k: replace(v, k) for k, v in value.items()}
            return value
        row = replace(item)
        encoded.append(row); maps[row["id"]] = {"item_id": item["id"], "refs": {v: k for k, v in refs.items()}}
    return encoded, maps


def candidate_payload(items):
    from .suppliers import SUPPLIER_FIELDS
    encoded, _ = candidate_aliases(items)
    return {"items": encoded, "mode": MODE, "supplier_contract": VERSION,
            "supplier_fields": list(SUPPLIER_FIELDS), "supplier_reference_scope": "short-scoped-candidates-v1"}


def selection_choice(item, selection):
    """Translate only literal referenced sections/identity/offer choices."""
    if not isinstance(selection, dict) or set(selection) != {"section_ids", "scope_kind", "company_identity", "offer_identity", "relevance", "terms", "prices"}:
        raise SafetyError("AI_SUPPLIER_SELECTION_INVALID")
    sections = {s["id"]: s for s in item["section_refs"]}
    refs = selection["section_ids"]
    if (not isinstance(refs, list) or not refs or len(set(refs)) != len(refs) or not set(refs) <= set(sections)):
        raise SafetyError("AI_SUPPLIER_SECTION_INVALID")
    identity = deepcopy(selection["company_identity"])
    fields = {"kind", "scheme", "value", "name", "address", "page_id", "quotes"}
    if not isinstance(identity, dict) or set(identity) != fields:
        raise SafetyError("SUPPLIER_COMPANY_IDENTITY_INVALID")
    kind = identity["kind"]
    used = {"registered_identifier": {"kind", "scheme", "value", "quotes"},
            "page_identity": {"kind", "name", "address", "page_id", "quotes"}, "unknown": {"kind", "quotes"}}.get(kind)
    if used is None or any(identity[field] is not None for field in fields - used):
        raise SafetyError("SUPPLIER_COMPANY_IDENTITY_INVALID")
    if kind == "unknown":
        if identity["quotes"]:
            raise SafetyError("SUPPLIER_COMPANY_IDENTITY_INVALID")
        identity = None
    else:
        identity = {field: identity[field] for field in used}
    offer = deepcopy(selection["offer_identity"])
    if offer is not None:
        for quote in offer.get("quotes", []):
            if not any(quote.get("page_id") == sections[ref]["page_id"]
                and " ".join(quote.get("excerpt", "").split()) in " ".join(sections[ref]["excerpt"].split()) for ref in refs):
                raise SafetyError("AI_SUPPLIER_OFFER_SECTION_INVALID")
    return {"scope_kind": selection["scope_kind"], "page_ids": [item["primary_page_id"]],
            "company_identity": identity, "workflow": item["workflow"], "offer": offer}


def selected_scope(item, selection):
    """Derive canonical refs and validate the selected real terms together."""
    choice = selection_choice(item, selection)
    scoped = scoped_item_from_choice(item["product"], item["pages"], choice)
    result = {"id": scoped["id"], "scope": deepcopy(scoped["scope"]),
              **{field: deepcopy(selection[field]) for field in ("relevance", "terms", "prices")}}
    result = validate_scoped_supplier_result(scoped, result)
    return {"choice": choice, "item": scoped, "result": result}


def validate_candidate_result(item, value, *, aliased=False):
    validate_candidate_item(item, aliased=aliased)
    if (not isinstance(value, dict) or set(value) != {"id", "selections", "reason"} or value["id"] != item["id"]
            or not isinstance(value["selections"], list) or len(value["selections"]) > 8
            or not isinstance(value["reason"], str) or not value["reason"].strip() or len(value["reason"]) > 2000):
        raise SafetyError("AI_SUPPLIER_SELECTION_INVALID")
    scope_ids = []
    for selection in value["selections"]:
        derived = selected_scope(item, selection)
        scope_ids.append(derived["item"]["id"])
    if len(set(scope_ids)) != len(scope_ids):
        raise SafetyError("AI_SUPPLIER_SELECTION_DUPLICATED")
    return deepcopy(value)


def validate_candidate_review(state, record_id, row):
    """Authenticate a saved selection to its actual, unchanged Fv4 request."""
    from .research_ai import MODEL
    from .supplier_terms import PROMPT_VERSION
    try:
        if (set(row) != {"kind", "input", "input_hash", "job_id", "checked_at", "result", "record_hash"}
                or row["kind"] != KIND or row["record_hash"] != digest({k: v for k, v in row.items() if k != "record_hash"})
                or row["input_hash"] != digest(row["input"])):
            raise ValueError
        job = state["jobs"][row["job_id"]]["request"]
        payload = candidate_payload([row["input"]]); encoded = payload.pop("items")[0]
        if (job["role"] != "F" or job["model"] != MODEL or job["prompt_version"] != PROMPT_VERSION
                or row["job_id"] != key("job", job)
                or {k: v for k, v in job["payload"].items() if k != "items"} != payload
                or encoded not in job["payload"]["items"]
                or record_id != key("supplier-candidate-review", [row["input"], VERSION, MODEL, PROMPT_VERSION, job["runner"]])):
            raise ValueError
        from .models import timestamp
        timestamp(row["checked_at"])
        validate_candidate_result(row["input"], row["result"])
        return row
    except (KeyError, TypeError, ValueError, AttributeError, SafetyError):
        raise SafetyError("SAVED_SCOPED_SUPPLIER_INVALID") from None
