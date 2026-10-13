"""Explicit company/workflow and product-offer interpretations of saved pages.

No reader, model, payment or publication is invoked here.  Scope choices are
grounded in original saved pages; they are not company registrations.  Unknown
identity stays unknown, and an offer's price/MOQ never migrates to its sibling.
The legacy ``suppliers`` interpretation and the display marker remain separate.
"""

from copy import deepcopy
from datetime import timedelta
import re

from .models import SafetyError, digest, identifier, timestamp
from .research_memory import key
from .suppliers import (SUPPLIER_FIELDS, YES_NO_FIELDS, _check_citations,
                        validate_supplier_result, derive_supply_arrangement)

VERSION = "supplier-scoped-terms-v2"
PROMPT_VERSION = "research-f-v4"
APPLICATION_KIND = "scoped-supplier-application-v1"
SCOPES = ("company_workflow", "product_offer", "unclear")
MAX_AGE = timedelta(days=30)
PRODUCT_FIELDS = ("meaning_hash", "primary_use", "mechanism", "use_context", "compatibility_constraints",
                  "included_distinctions", "excluded_distinctions")


def _text(value, *, optional=False, limit=2000):
    if value is None and optional:
        return value
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise SafetyError("SUPPLIER_SCOPED_INPUT_INVALID")
    return value


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise SafetyError("SUPPLIER_SCOPED_BINDING_INVALID")
    return value


def _quotes(quotes, pages):
    if not isinstance(quotes, list) or not 1 <= len(quotes) <= 8:
        raise SafetyError("SUPPLIER_SCOPE_EVIDENCE_REQUIRED")
    if any(not isinstance(q, dict) or set(q) != {"page_id", "excerpt"}
           or not isinstance(q["page_id"], str) or not isinstance(q["excerpt"], str) for q in quotes):
        raise SafetyError("SUPPLIER_SCOPE_EVIDENCE_INVALID")
    row = {"references": sorted({q.get("page_id") for q in quotes if isinstance(q, dict)}),
           "quotes": deepcopy(quotes)}
    _check_citations(row, pages, required=True)
    return row["quotes"]


def _identity(identity, pages):
    """A name/host alone cannot establish a company or merge placements."""
    if identity is None:
        return None, None
    if not isinstance(identity, dict):
        raise SafetyError("SUPPLIER_COMPANY_IDENTITY_INVALID")
    kind = identity.get("kind")
    if kind == "registered_identifier" and set(identity) == {"kind", "scheme", "value", "quotes"}:
        scheme, value = identity["scheme"], identity["value"]
        patterns = {"GSTIN": r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]",
                    "CIN": r"[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}"}
        if scheme not in patterns or not isinstance(value, str) or not re.fullmatch(patterns[scheme], value):
            raise SafetyError("SUPPLIER_COMPANY_IDENTITY_INVALID")
        quotes = _quotes(identity["quotes"], pages)
        if not any(re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", q["excerpt"]) for q in quotes):
            raise SafetyError("SUPPLIER_COMPANY_IDENTITY_INVALID")
        basis = {"kind": kind, "scheme": scheme, "value": value}
    elif kind == "page_identity" and set(identity) == {"kind", "name", "address", "page_id", "quotes"}:
        name = _text(identity["name"], limit=300)
        address = _text(identity["address"], limit=1000)
        if len(address.split()) < 4 or len(address) < 20 or identity["page_id"] not in pages:
            raise SafetyError("SUPPLIER_COMPANY_IDENTITY_INVALID")
        quotes = _quotes(identity["quotes"], pages)
        if (any(q["page_id"] != identity["page_id"] for q in quotes)
                or not any(name in q["excerpt"] and address in q["excerpt"] for q in quotes)):
            raise SafetyError("SUPPLIER_COMPANY_IDENTITY_INVALID")
        # Keep an unregistered placement local to its exact identity page.
        # Another directory URL with the same trading name is not a merge.
        basis = {"kind": kind, "url": pages[identity["page_id"]]["url"], "name": name, "address": address}
    else:
        raise SafetyError("SUPPLIER_COMPANY_IDENTITY_INVALID")
    return key("supplier-company", basis), dict(deepcopy(identity), quotes=quotes)


def scoped_item_from_choice(meaning, source_pages, choice):
    """Derive refs from literal supplied choices, never from target name/host."""
    if (not isinstance(choice, dict) or set(choice) != {"scope_kind", "page_ids", "company_identity", "workflow", "offer"}
            or choice["scope_kind"] not in SCOPES):
        raise SafetyError("SUPPLIER_SCOPED_INPUT_INVALID")
    try:
        pages = {page["id"]: page for page in source_pages}
        selected = choice["page_ids"]
        if (not isinstance(selected, list) or not selected or len(selected) > 8
                or len(set(selected)) != len(selected) or not set(selected) <= set(pages)):
            raise SafetyError("SUPPLIER_SCOPE_PAGE_INVALID")
        company_ref, identity = _identity(choice["company_identity"], pages)
        workflow = _text(choice["workflow"], optional=True, limit=500)
        workflow_ref = key("supplier-workflow", [company_ref, workflow]) if company_ref and workflow else None
        offer, offer_ref, unit = choice["offer"], None, None
        if choice["scope_kind"] == "product_offer":
            if (not isinstance(offer, dict) or set(offer) != {"page_id", "label", "purchase_unit", "quotes"}
                    or offer["page_id"] not in selected):
                raise SafetyError("SUPPLIER_OFFER_BINDING_INVALID")
            label, unit = _text(offer["label"], limit=500), _text(offer["purchase_unit"], optional=True, limit=500)
            quotes = _quotes(offer["quotes"], pages)
            if (any(q["page_id"] != offer["page_id"] for q in quotes)
                    or not any(label in q["excerpt"] and (unit is None or unit in q["excerpt"]) for q in quotes)):
                raise SafetyError("SUPPLIER_OFFER_BINDING_INVALID")
            offer = dict(deepcopy(offer), quotes=quotes)
            offer_ref = key("supplier-offer", [pages[offer["page_id"]]["url"], label, unit])
        elif offer is not None:
            raise SafetyError("SUPPLIER_OFFER_BINDING_INVALID")
        if choice["scope_kind"] == "company_workflow" and workflow is None:
            raise SafetyError("SUPPLIER_WORKFLOW_REQUIRED")
        # Identity pages are real dependencies even when not a terms page.
        dependency_ids = set(selected) | {q["page_id"] for q in (identity or {}).get("quotes", [])}
        scope = {"scope_kind": choice["scope_kind"], "company_ref": company_ref,
                 "workflow_ref": workflow_ref, "offer_ref": offer_ref, "purchase_unit": unit}
        product = ({name: deepcopy(meaning[name]) for name in PRODUCT_FIELDS}
                   if choice["scope_kind"] != "company_workflow" else None)
        model_pages = []
        for p in sorted(dependency_ids):
            projected = deepcopy(pages[p])
            if p not in selected:
                # An identity-only page proves identity, not another item's
                # commercial conditions. Its unrelated fee/offer text must
                # not reopen every independent offer's model interpretation.
                projected["text"] = "\n".join(q["excerpt"] for q in identity["quotes"] if q["page_id"] == p)
            model_pages.append(projected)
        item = {"id": key("supplier-scope", [scope, product, sorted(pages[p]["url"] for p in selected)]),
                "scope": scope, "product": product, "workflow": workflow,
                "company_identity": identity, "offer_identity": offer,
                "scope_page_ids": sorted(selected), "pages": model_pages}
        validate_scoped_supplier_item(item)
        return item
    except (KeyError, TypeError, AttributeError):
        raise SafetyError("SUPPLIER_SCOPED_INPUT_INVALID") from None


def prepare_scoped_supplier_input(state, capture, choice):
    """Prepare one verified scope, keeping capture dates outside F's key."""
    try:
        from .functional_types import effective_functional_definition
        cid = capture["product"]["concept_id"]
        meaning = effective_functional_definition(state, cid)
        source = capture["bundle"]["item"]
        pages = {page["id"]: page for page in source["pages"]}
        item = scoped_item_from_choice(meaning, source["pages"], choice)
        model_pages = item["pages"]
        dependency_ids = {p["id"] for p in model_pages}
        receipts = {row["id"]: row for row in capture["bundle"]["receipts"] if row.get("id") in dependency_ids}
        if set(receipts) != dependency_ids:
            raise SafetyError("SUPPLIER_ORIGINAL_RECEIPT_REQUIRED")
        by_model_page = {p["id"]: p for p in model_pages}
        proof = [{"page_id": p, "url": pages[p]["url"], "content_hash": digest(by_model_page[p]),
                  "original_content_hash": digest(pages[p]),
                  "receipt_hash": digest(receipts[p]), "observed_at": receipts[p]["checked_at"]} for p in sorted(dependency_ids)]
        for row in proof:
            timestamp(row["observed_at"])
        effort = [(rid, row) for rid, row in state["source_checks"].items()
                  if row.get("kind") == "supplier-effort-v1" and row.get("pass_id") == capture["capture_id"]
                  and row.get("concept_id") == cid and row.get("receipts_hash") == digest(capture["bundle"]["receipts"])]
        if len(effort) != 1:
            raise SafetyError("SUPPLIER_ORIGINAL_EFFORT_REQUIRED")
        from .supplier_effort import _validated_record
        _validated_record(effort[0][1])
        application = {"concept_id": cid, "meaning_hash": meaning["meaning_hash"],
            "capture_id": capture["capture_id"], "capture_path": capture["path"],
            "capture_request": deepcopy(capture["request"]), "capture_product": deepcopy(capture["product"]),
            "lead_id": capture["request"]["lead"]["id"], "original_item_id": source["id"],
            "effort_id": effort[0][0], "effort_hash": digest(effort[0][1]), "pages": proof,
            "input_hash": digest(item)}
        validate_scoped_supplier_item(item)
        return {"item": item, "application": application, "choice": deepcopy(choice)}
    except (KeyError, TypeError, AttributeError):
        raise SafetyError("SUPPLIER_SCOPED_INPUT_INVALID") from None


def validate_scoped_supplier_item(item, *, aliased=False):
    if (not isinstance(item, dict) or set(item) != {"id", "scope", "product", "workflow", "company_identity", "offer_identity", "scope_page_ids", "pages"}
            or not isinstance(item["scope"], dict) or set(item["scope"]) != {"scope_kind", "company_ref", "workflow_ref", "offer_ref", "purchase_unit"}
            or item["scope"]["scope_kind"] not in SCOPES or not isinstance(item["pages"], list)):
        raise SafetyError("SUPPLIER_SCOPED_INPUT_INVALID")
    identifier(item["id"])
    from .suppliers import validate_supplier_input
    validate_supplier_input({"id": item["id"], "product": item["product"] or {}, "pages": item["pages"]})
    pages = {p["id"]: p for p in item["pages"]}
    selected = item["scope_page_ids"]
    if not isinstance(selected, list) or not selected or len(set(selected)) != len(selected) or not set(selected) <= set(pages):
        raise SafetyError("SUPPLIER_SCOPE_PAGE_INVALID")
    def mapped(ref, prefix):
        return prefix + "_" + digest(ref)[:16] if aliased and ref is not None else ref
    company_ref, identity = _identity(item["company_identity"], pages)
    if mapped(company_ref, "company") != item["scope"]["company_ref"] or identity != item["company_identity"]:
        raise SafetyError("SUPPLIER_COMPANY_IDENTITY_INVALID")
    workflow = _text(item["workflow"], optional=True, limit=500)
    expected_workflow = key("supplier-workflow", [company_ref, workflow]) if company_ref and workflow else None
    if mapped(expected_workflow, "workflow") != item["scope"]["workflow_ref"]:
        raise SafetyError("SUPPLIER_WORKFLOW_BINDING_INVALID")
    if item["scope"]["scope_kind"] == "company_workflow":
        if item["product"] is not None or workflow is None or item["offer_identity"] is not None:
            raise SafetyError("SUPPLIER_SCOPED_INPUT_INVALID")
    else:
        from .functional_types import MEANING_FIELDS, validate_definition, definition_meaning_hash
        if (not isinstance(item["product"], dict)
                or set(item["product"]) != {"meaning_hash", *MEANING_FIELDS}):
            raise SafetyError("SUPPLIER_SCOPED_INPUT_INVALID")
        product_definition = validate_definition(dict(report_title="Supplier research subject",
            **{name: item["product"][name] for name in MEANING_FIELDS}))
        if (_hash(item["product"].get("meaning_hash")) != definition_meaning_hash(product_definition)
                or any(item["product"][name] != product_definition[name] for name in MEANING_FIELDS)):
            raise SafetyError("SUPPLIER_SCOPED_MEANING_INVALID")
    offer = item["offer_identity"]
    if item["scope"]["scope_kind"] == "product_offer":
        if not isinstance(offer, dict) or set(offer) != {"page_id", "label", "purchase_unit", "quotes"} or offer["page_id"] not in selected:
            raise SafetyError("SUPPLIER_OFFER_BINDING_INVALID")
        label = _text(offer["label"], limit=500)
        unit = _text(offer["purchase_unit"], optional=True, limit=500)
        quotes = _quotes(offer["quotes"], pages)
        if (any(q["page_id"] != offer["page_id"] for q in quotes)
                or not any(offer["label"] in q["excerpt"] and (unit is None or unit in q["excerpt"]) for q in quotes)
                or item["scope"]["offer_ref"] != mapped(key("supplier-offer", [pages[offer["page_id"]]["url"], offer["label"], offer["purchase_unit"]]), "offer")
                or item["scope"]["purchase_unit"] != offer["purchase_unit"]):
            raise SafetyError("SUPPLIER_OFFER_BINDING_INVALID")
    elif offer is not None or item["scope"]["offer_ref"] is not None or item["scope"]["purchase_unit"] is not None:
        raise SafetyError("SUPPLIER_OFFER_BINDING_INVALID")
    original_scope = dict(item["scope"], company_ref=company_ref, workflow_ref=expected_workflow,
        offer_ref=key("supplier-offer", [pages[offer["page_id"]]["url"], offer["label"], offer["purchase_unit"]]) if offer else None)
    if item["id"] != mapped(key("supplier-scope", [original_scope, item["product"], sorted(pages[p]["url"] for p in selected)]), "scope"):
        raise SafetyError("SUPPLIER_SCOPE_BINDING_INVALID")
    return item


def validate_scoped_supplier_result(item, result, *, aliased=False):
    """Shape is checked by F; this checks real scope/quote relationships."""
    validate_scoped_supplier_item(item, aliased=aliased)
    if not isinstance(result, dict) or set(result) != {"id", "scope", "relevance", "terms", "prices"}:
        raise SafetyError("AI_SUPPLIER_RESULT_INVALID")
    value = deepcopy(result)
    if value.get("scope") != item["scope"]:
        raise SafetyError("AI_SUPPLIER_SCOPE_INVALID")
    pages = {p["id"]: p for p in item["pages"]}
    legacy = {"id": value["id"], "relevance": value["relevance"], "terms": [], "prices": []}
    extras = {}
    for row in value["terms"]:
        if row["applicability"] not in ("applies", "unclear", "excluded"):
            raise SafetyError("AI_SUPPLIER_APPLICABILITY_INVALID")
        exceptions = _quotes(row["exceptions"], pages) if row["exceptions"] else []
        if not (set(row["references"]) | {q["page_id"] for q in exceptions}) <= set(item["scope_page_ids"]):
            raise SafetyError("AI_SUPPLIER_TERM_SCOPE_INVALID")
        if (row["field"] == "purchase_moq" and row["basis"] != "not_stated" and item["offer_identity"]
                and not any(item["offer_identity"]["label"] in q["excerpt"]
                    and (item["scope"]["purchase_unit"] is None or item["scope"]["purchase_unit"] in q["excerpt"]) for q in row["quotes"])):
            raise SafetyError("AI_SUPPLIER_MOQ_SCOPE_INVALID")
        extras[row["field"]] = {"applicability": row["applicability"], "exceptions": exceptions}
        legacy["terms"].append({k: deepcopy(v) for k, v in row.items() if k not in {"applicability", "exceptions"}})
    for row in value["prices"]:
        if not set(row["references"]) <= set(item["scope_page_ids"]):
            raise SafetyError("AI_SUPPLIER_PRICE_SCOPE_INVALID")
        if (row["price_kind"] == "product" and (item["scope"]["scope_kind"] != "product_offer"
                or value["relevance"]["relationship"] != "exact_product")):
            raise SafetyError("AI_SUPPLIER_PRICE_SCOPE_INVALID")
        if (row["price_kind"] == "product" and not any(item["offer_identity"]["label"] in q["excerpt"]
                and (item["scope"]["purchase_unit"] is None or item["scope"]["purchase_unit"] in q["excerpt"]) for q in row["quotes"])):
            raise SafetyError("AI_SUPPLIER_PRICE_SCOPE_INVALID")
        normalized = deepcopy(row)
        if normalized.get("amount") is None and normalized.get("quoted_amount") is not None:
            normalized["amount"] = normalized.pop("quoted_amount")
            normalized.pop("amount_range", None)
        if item["scope"]["scope_kind"] == "product_offer":
            # One neighbouring quote cannot lend an amount to the correctly
            # named offer. At least one *same* contextual quotation must
            # support both this offer/unit and the full quoted amount.
            from .suppliers import _qualified_price
            contextual = [q for q in normalized["quotes"]
                if item["offer_identity"]["label"] in q["excerpt"]
                and (item["scope"]["purchase_unit"] is None or item["scope"]["purchase_unit"] in q["excerpt"])]
            if value["relevance"]["relationship"] != "exact_product" or not contextual:
                raise SafetyError("AI_SUPPLIER_PRICE_SCOPE_INVALID")
            try:
                _qualified_price(dict(normalized, quotes=contextual))
            except SafetyError:
                raise SafetyError("AI_SUPPLIER_PRICE_SCOPE_INVALID") from None
        legacy["prices"].append(normalized)
    legacy = validate_supplier_result({"id": item["id"], "product": item["product"] or {}, "pages": item["pages"]}, legacy)
    if item["scope"]["scope_kind"] != "product_offer" and any(r["field"] == "purchase_moq" and r["basis"] != "not_stated" for r in legacy["terms"]):
        raise SafetyError("AI_SUPPLIER_MOQ_SCOPE_INVALID")
    if item["scope"]["scope_kind"] == "company_workflow" and legacy["relevance"]["relationship"] == "exact_product":
        raise SafetyError("AI_SUPPLIER_SCOPE_INVALID")
    value.update(legacy)
    mandatory = [row for row in legacy["prices"] if row["price_kind"] == "mandatory_recurring"]
    value["terms"] = [dict(row, **extras.get(row["field"],
        {"applicability": "applies" if row["field"] == "subscription_requirement" and mandatory else "unclear",
         "exceptions": []})) for row in legacy["terms"]]
    # A derived subscription conflict is still scope-local, never a sibling rule.
    return value


def supplier_aliases(items):
    """Exact maps for every company/workflow/offer/page ID, before Jobs runs."""
    aliased, mappings = [], {}
    for item in items:
        validate_scoped_supplier_item(item)
        refs = {item["id"]: "scope_" + digest(item["id"])[:16]}
        for page in item["pages"]:
            refs[page["id"]] = "page_" + digest(page["id"])[:16]
        for field in ("company_ref", "workflow_ref", "offer_ref"):
            if item["scope"][field] is not None:
                refs[item["scope"][field]] = field.split("_")[0] + "_" + digest(item["scope"][field])[:16]
        if len(set(refs.values())) != len(refs):
            raise SafetyError("AI_SUPPLIER_ALIAS_COLLISION")
        def replace(value, field=None):
            if isinstance(value, str):
                return refs.get(value, value) if field in {"id", "page_id", "company_ref", "workflow_ref", "offer_ref", "references", "scope_page_ids"} else value
            if isinstance(value, list):
                return [replace(v, field) for v in value]
            if isinstance(value, dict):
                return {k: replace(v, k) for k, v in value.items()}
            return value
        encoded = replace(item)
        mappings[encoded["id"]] = {"item_id": item["id"], "refs": {v: k for k, v in refs.items()}}
        aliased.append(encoded)
    return aliased, mappings


def original_supplier_result(value, mapping):
    def replace(value, field=None):
        if isinstance(value, str):
            return mapping["refs"].get(value, value) if field in {"id", "page_id", "company_ref", "workflow_ref", "offer_ref", "references", "scope_page_ids", "section_ids", "primary_page_id"} else value
        if isinstance(value, list):
            return [replace(v, field) for v in value]
        if isinstance(value, dict):
            return {k: replace(v, k) for k, v in value.items()}
        return value
    return replace(deepcopy(value))


def supplier_payload(items):
    aliased, _ = supplier_aliases(items)
    return {"items": aliased, "mode": "supplier_scoped_terms", "supplier_contract": VERSION,
            "supplier_fields": list(SUPPLIER_FIELDS), "supplier_reference_scope": "short-scoped-ids-v2"}


def validate_supplier_application(state, record, *, as_of):
    """Pure reader: original job, exact application and receipt dates must agree."""
    try:
        if (record.get("kind") != APPLICATION_KIND or set(record) != {"kind", "id", "binding", "binding_hash", "prepared_at"}
                or record["binding_hash"] != digest(record["binding"])
                or record["id"] != key("supplier-application", record["binding"])):
            raise ValueError
        binding = record["binding"]
        if set(binding) != {"assessment_id", "assessment_hash", "concept_id", "meaning_hash", "capture_id", "capture_path", "capture_request", "capture_product", "lead_id", "original_item_id", "effort_id", "effort_hash", "pages", "input_hash"}:
            raise ValueError
        timestamp(record["prepared_at"])
        semantic = state["supplier_assessments"][binding["assessment_id"]]
        from .research_ai import MODEL
        item = semantic["input"]
        validate_scoped_supplier_item(item)
        if (semantic.get("kind") != VERSION or semantic.get("version") != VERSION
                or semantic.get("record_hash") != digest({k: v for k, v in semantic.items() if k != "record_hash"})
                or binding["assessment_hash"] != digest(semantic) or semantic["input_hash"] != digest(item)
                or binding["input_hash"] != digest(item)):
            raise ValueError
        if timestamp(semantic["checked_at"]) > timestamp(as_of) or timestamp(record["prepared_at"]) > timestamp(as_of):
            return None
        job = state["jobs"][semantic["job_id"]]["request"]
        if "candidate_review_id" in semantic:
            from .supplier_scope_candidates import validate_candidate_review, selected_scope
            review_id = semantic["candidate_review_id"]
            review = validate_candidate_review(state, review_id, state["supplier_assessments"][review_id])
            index = semantic["selection_index"]
            if type(index) is not int or not 0 <= index < len(review["result"]["selections"]):
                raise ValueError
            selected = selected_scope(review["input"], review["result"]["selections"][index])
            if (semantic["candidate_review_hash"] != digest(review) or selected["item"] != item
                    or selected["result"] != semantic["result"] or semantic["job_id"] != review["job_id"]
                    or semantic["checked_at"] != review["checked_at"]
                    or binding["assessment_id"] != key("supplier-selected-scope", [review_id, index, item])):
                raise ValueError
        else:
            expected = supplier_payload([item])
            encoded = expected.pop("items")[0]
            if (job.get("role") != "F" or job.get("prompt_version") != PROMPT_VERSION or job.get("model") != MODEL
                    or {k: v for k, v in job["payload"].items() if k != "items"} != expected
                    or encoded not in job["payload"]["items"] or semantic["job_id"] != key("job", job)
                    or binding["assessment_id"] != key("supplier-scoped", [item, VERSION, MODEL, PROMPT_VERSION, job["runner"]])):
                raise ValueError
        from .functional_types import effective_functional_definition
        if effective_functional_definition(state, binding["concept_id"])["meaning_hash"] != binding["meaning_hash"]:
            return None
        effort = state["source_checks"][binding["effort_id"]]
        from .supplier_effort import _validated_record
        _validated_record(effort)
        from .supplier_capture_store import _request, _capture_key
        request = _request(binding["capture_request"])
        product = binding["capture_product"]
        if (digest(effort) != binding["effort_hash"] or effort["pass_id"] != binding["capture_id"]
                or effort["concept_id"] != binding["concept_id"] or effort["reference"] != binding["capture_path"]
                or binding["capture_id"] != _capture_key(product, request) or product["concept_id"] != binding["concept_id"]
                or request["concept_id"] != binding["concept_id"] or request["lead"]["id"] != binding["lead_id"]
                or binding["original_item_id"] != "supplier-item-" + digest([binding["concept_id"], binding["lead_id"]])[:40]
                or effort["description_hash"] != digest(product["description"])):
            raise ValueError
        actual = {p["id"]: p for p in effort["receipt_refs"]}
        pages = {p["id"]: p for p in item["pages"]}
        if len(binding["pages"]) != len(pages) or {p["page_id"] for p in binding["pages"]} != set(pages):
            raise ValueError
        for proof in binding["pages"]:
            original = actual[proof["page_id"]]
            if set(proof) != {"page_id", "url", "content_hash", "original_content_hash", "receipt_hash", "observed_at"}:
                raise ValueError
            _hash(proof["original_content_hash"])
            if (proof["content_hash"] != digest(pages[proof["page_id"]]) or proof["url"] != pages[proof["page_id"]]["url"]
                    or proof["receipt_hash"] != original["hash"] or proof["observed_at"] != original["checked_at"]):
                raise ValueError
            if timestamp(proof["observed_at"]) > timestamp(as_of):
                return None
        result = validate_scoped_supplier_result(item, semantic["result"])
        if result != semantic["result"]:
            raise ValueError
        return {"application": deepcopy(record), "assessment": deepcopy(semantic), "result": result}
    except (KeyError, TypeError, ValueError, AttributeError, SafetyError):
        raise SafetyError("SAVED_SCOPED_SUPPLIER_INVALID") from None


def current_supplier_terms(state, *, concept_id, as_of):
    """Current/datetime fields and offer-local derivation; never a supplier gate.

    Recapture application dates, not F completion, govern the 30-day rule.
    A stale useful lead remains in ``dated``. Legacy mixed-scope rows are not
    silently promoted and remain available to their original reader.
    """
    identifier(concept_id); now = timestamp(as_of)
    latest = {}
    for record_id, record in state["source_checks"].items():
        if record.get("kind") != APPLICATION_KIND or record.get("binding", {}).get("concept_id") != concept_id:
            continue
        if record.get("id") != record_id:
            raise SafetyError("SAVED_SCOPED_SUPPLIER_INVALID")
        checked = validate_supplier_application(state, record, as_of=as_of)
        if checked is None:
            continue
        item = checked["assessment"]["input"]
        date = min((p["observed_at"] for p in record["binding"]["pages"]), key=timestamp)
        order = timestamp(date), record["id"]
        if item["id"] not in latest or order > latest[item["id"]][0]:
            latest[item["id"]] = order, checked
    projected = []
    for _, checked in latest.values():
        app, semantic, result = checked["application"], checked["assessment"], checked["result"]
        dates = {p["page_id"]: p["observed_at"] for p in app["binding"]["pages"]}
        rows = []
        for row in result["terms"]:
            refs = set(row["references"]) | {q["page_id"] for q in row["exceptions"]}
            observed = min((dates[p] for p in refs), key=timestamp, default=None)
            current = observed is not None and timedelta(0) <= now - timestamp(observed) <= MAX_AGE
            relevant = (result["relevance"]["relationship"] == "exact_product" if result["scope"]["scope_kind"] == "product_offer"
                        else result["relevance"]["relationship"] == "relevant_company")
            qualified = (current and relevant and row["applicability"] == "applies" and row["basis"] in {"observed", "conflicting"}
                and result["scope"]["scope_kind"] != "unclear"
                and (result["scope"]["scope_kind"] != "company_workflow" or result["scope"]["workflow_ref"] is not None))
            rows.append(dict(deepcopy(row), observed_at=observed,
                valid_until=(timestamp(observed) + MAX_AGE).isoformat() if observed else None,
                current=current, qualified=qualified, **deepcopy(result["scope"]),
                application_id=app["id"], assessment_id=app["binding"]["assessment_id"],
                support_application_ids=[app["id"]], support_assessment_ids=[app["binding"]["assessment_id"]]))
        prices = []
        for price in result["prices"]:
            observed = min((dates[p] for p in price["references"]), key=timestamp)
            prices.append(dict(deepcopy(price), observed_at=observed,
                valid_until=(timestamp(observed) + MAX_AGE).isoformat(),
                current=timedelta(0) <= now - timestamp(observed) <= MAX_AGE,
                qualified=(timedelta(0) <= now - timestamp(observed) <= MAX_AGE
                    and (result["relevance"]["relationship"] == "exact_product" if result["scope"]["scope_kind"] == "product_offer"
                         else result["relevance"]["relationship"] == "relevant_company"
                              and result["scope"]["scope_kind"] == "company_workflow"
                              and result["scope"]["workflow_ref"] is not None)),
                **deepcopy(result["scope"]), application_id=app["id"],
                assessment_id=app["binding"]["assessment_id"],
                support_application_ids=[app["id"]], support_assessment_ids=[app["binding"]["assessment_id"]]))
        projected.append({"id": semantic["input"]["id"], **deepcopy(result["scope"]),
            "application_id": app["id"], "assessment_id": app["binding"]["assessment_id"],
            "lead_id": app["binding"]["lead_id"], "relevance": deepcopy(result["relevance"]),
            "terms": rows, "prices": [p for p in prices if p["price_kind"] == "product"],
            "fees": [p for p in prices if p["price_kind"] != "product"],
            "source_capture_at": min(dates.values(), key=timestamp), "reviewed_at": semantic["checked_at"],
            "current": any(r["current"] for r in rows) or any(p["current"] for p in prices),
            "scoped_usable": any(r["qualified"] for r in rows) or any(p["qualified"] for p in prices),
            "page_refs": deepcopy(app["binding"]["pages"])})
    # Frozen record maps can reload in sorted-key order rather than original
    # event insertion order. Select the same primary conflict origin and quote
    # order before deriving inherited fields, not merely before displaying them.
    projected.sort(key=lambda r: (r["scope_kind"], r["company_ref"] or "", r["offer_ref"] or "", r["id"]))
    # Apply explicitly scoped company/workflow rules only to the same proven
    # company AND requested workflow. Offer-specific restrictions take priority.
    for offer in projected:
        if offer["scope_kind"] != "product_offer":
            continue
        fields = {r["field"]: deepcopy(r) for r in offer["terms"] if r["qualified"]}
        # A dated product-specific restriction cannot silently disappear just
        # because a general company page was recaptured more recently.
        local_blocks = {r["field"] for r in offer["terms"] if r["basis"] != "not_stated"}
        company = [r for r in projected if r["scope_kind"] == "company_workflow" and r["company_ref"] is not None
                   and (r["company_ref"], r["workflow_ref"]) == (offer["company_ref"], offer["workflow_ref"])]
        for row in company:
            for term in row["terms"]:
                field = term["field"]
                if not term["qualified"] or field == "purchase_moq" or field in local_blocks:
                    continue
                if field not in fields:
                    fields[field] = deepcopy(term)
                elif fields[field]["value"] != term["value"]:
                    previous = fields[field]
                    fields[field] = dict(previous, value="Conflicting", basis="conflicting",
                        references=sorted(set(previous["references"] + term["references"])),
                        quotes=previous["quotes"] + term["quotes"],
                        exceptions=previous["exceptions"] + term["exceptions"],
                        observed_at=min((previous["observed_at"], term["observed_at"]), key=timestamp),
                        valid_until=min((previous["valid_until"], term["valid_until"]), key=timestamp),
                        support_application_ids=sorted(set(previous["support_application_ids"] + term["support_application_ids"])),
                        support_assessment_ids=sorted(set(previous["support_assessment_ids"] + term["support_assessment_ids"])),
                        reason="Applicable company statements conflict; both were retained.")
        moq = fields.get("purchase_moq", {}).get("value", "")
        minimum = re.fullmatch(r"(\d+)\s+(piece|pieces|unit|units|pack|packs)", moq, re.I)
        offer["min_order"] = int(minimum[1]) if minimum else None
        offer["min_order_unit"] = minimum[2].lower() if minimum else None
        offer["effective_terms"] = [fields[f] for f in SUPPLIER_FIELDS if f in fields]
        offer["support_application_ids"] = sorted({offer["application_id"]} |
            {ref for row in offer["effective_terms"] for ref in row["support_application_ids"]})
        offer["support_assessment_ids"] = sorted({offer["assessment_id"]} |
            {ref for row in offer["effective_terms"] for ref in row["support_assessment_ids"]})
        offer["scoped_usable"] = any(r["qualified"] for r in offer["effective_terms"] + offer["prices"] + offer["fees"])
        offer["supply_arrangement"] = derive_supply_arrangement(offer["effective_terms"])
        for price in offer["prices"]:
            minimum_kind = "pack" if minimum and minimum[2].lower().startswith("pack") else "piece" if minimum else None
            price_kind = "pack" if re.search(r"\bpack", price["unit"], re.I) else "piece" if re.search(r"\b(piece|unit)", price["unit"], re.I) else None
            price["moq"] = offer["min_order"] if minimum_kind == price_kind else None
            price["moq_unit"] = offer["min_order_unit"]
    return {"contract": VERSION, "concept_id": concept_id, "current": [r for r in projected if r["current"]],
            "dated": [r for r in projected if not r["current"]], "all": projected,
            "application_ids": sorted(r["application_id"] for r in projected),
            "assessment_ids": sorted({r["assessment_id"] for r in projected})}
