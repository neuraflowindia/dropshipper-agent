"""Interpret saved public supplier pages; no searches, contacts or publication."""

from decimal import Decimal, InvalidOperation
from copy import deepcopy
import re
from urllib.parse import urlsplit

from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .research_memory import change, key
from .model_recovery import run_current_model_job

SUPPLIER_FIELDS = (
    "subscription_requirement", "recurring_fee_frequency", "monthly_fee", "purchase_moq",
    "single_piece_purchase", "direct_customer_dispatch", "bulk_stock_prerequisite", "cod",
    "india_coverage", "white_label", "white_label_shipping", "neutral_packaging", "ecommerce_support", "order_integration",
    "setup_fee", "deposit", "per_order_fee",
)
YES_NO_FIELDS = {"single_piece_purchase", "direct_customer_dispatch", "bulk_stock_prerequisite", "cod",
                 "white_label", "white_label_shipping", "neutral_packaging", "ecommerce_support", "order_integration"}
VERSION = "supplier-terms-v1"


_INTERPRETATION_INTEGRITY_FAILURES = {
    "RESEARCH_EVENTS_INVALID", "RESEARCH_CAPTURE_CONFLICT", "RESEARCH_EVENT_CONFLICT",
    "RESEARCH_CHANGE_INVALID", "RESEARCH_CHANGES_INVALID", "SYMLINK_PATH_REFUSED",
    "JOURNAL_CORRUPT_REVIEW_REQUIRED", "PROGRAMME_CONTROL_INVALID",
    "PROGRAMME_BINDING_CHANGED", "PROGRAMME_CONTROL_CONFLICT", "PROGRAMME_SERVICE_CONTROL_INVALID",
    "SOURCE_RECEIPT_CONFLICT", "SOURCE_STAGED_RECEIPT_INVALID", "SOURCE_PROGRESS_STAGE_INVALID",
}


def _interpretation_failure(error, *, local_code):
    """Isolate an owned file operation, never bless corrupt shared history.

    OS messages can contain local paths or account details; expose a fixed
    waiting reason instead. Existing typed item failures retain their meaning.
    Programming errors and trusted store-integrity errors still propagate.
    """
    if isinstance(error, SafetyError):
        if str(error) in _INTERPRETATION_INTEGRITY_FAILURES:
            raise error
        return str(error)
    if isinstance(error, OSError):
        return local_code
    raise error


def validate_supplier_input(item):
    if not isinstance(item, dict) or set(item) != {"id", "product", "pages"}:
        raise SafetyError("SUPPLIER_INPUT_INVALID")
    identifier(item["id"])
    if not isinstance(item["product"], (dict, str)) or not isinstance(item["pages"], list) or not 1 <= len(item["pages"]) <= 8:
        raise SafetyError("SUPPLIER_INPUT_INVALID")
    ids = set()
    for page in item["pages"]:
        if not isinstance(page, dict) or set(page) != {"id", "url", "title", "text"}:
            raise SafetyError("SUPPLIER_PAGE_INVALID")
        identifier(page["id"])
        if page["id"] in ids:
            raise SafetyError("SUPPLIER_PAGE_ID_DUPLICATED")
        ids.add(page["id"])
        for name, limit in (("url", 4096), ("title", 2000), ("text", 24000)):
            if not isinstance(page[name], str) or len(page[name]) > limit:
                raise SafetyError("SUPPLIER_PAGE_INVALID")
        try:
            parsed = urlsplit(page["url"])
        except ValueError:
            raise SafetyError("SUPPLIER_PAGE_URL_INVALID") from None
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise SafetyError("SUPPLIER_PAGE_URL_INVALID")


def _check_citations(row, pages, *, required=False):
    refs = row["references"]
    if len(set(refs)) != len(refs) or not set(refs) <= set(pages):
        raise SafetyError("AI_SUPPLIER_REFERENCE_INVALID")
    if required and (not refs or not row["quotes"]):
        raise SafetyError("AI_SUPPLIER_QUOTE_REQUIRED")
    for quote in row["quotes"]:
        if quote["page_id"] not in refs or not quote["excerpt"].strip():
            raise SafetyError("AI_SUPPLIER_QUOTE_INVALID")
        source = pages[quote["page_id"]]["text"]
        excerpt = quote["excerpt"].strip()
        # Preserve complete words/numbers: 100 must not match 1000 or 100.50.
        prefix = r"(?<!\w)(?<!\d[.,])" if excerpt[0].isalnum() else ""
        suffix = (r"(?!\w|[.,]\d)" if excerpt[-1].isdigit() else r"(?!\w)" if excerpt[-1].isalnum() else "")
        if len(excerpt) > 1 and excerpt[-1] in ".," and excerpt[-2].isdigit():
            suffix = r"(?!\d)"  # A quoted full stop must not clip a decimal or grouped amount.
        patterns = [re.escape(excerpt), r"\s+".join(re.escape(part) for part in excerpt.split())]
        match = next((found for pattern in patterns
                      if (found := re.search(prefix + pattern + suffix, source)) is not None), None)
        if match is None:
            raise SafetyError("AI_SUPPLIER_QUOTE_INVALID")
        # Restore the original span; case, words, punctuation and amounts stay.
        quote["excerpt"] = match.group(0)


def _price_amount(row):
    try:
        if not re.fullmatch(r"\d+(?:\.\d{1,2})?", row["amount"]):
            raise ValueError
        amount = Decimal(row["amount"])
        if not amount.is_finite() or amount < 0 or amount > Decimal("1000000000"):
            raise ValueError
        # Exact quote membership proves the source of the amount, not whether
        # the model understood the supplier's commercial condition correctly.
        numbers = [Decimal(s.replace(",", "")) for quote in row["quotes"]
                   for s in re.findall(r"(?<![\w.])\d[\d,]*(?:\.\d{1,2})?(?![\w.])", quote["excerpt"])]
        if amount not in numbers:
            raise ValueError
        currency = row["currency"]
        quotes = " ".join(q["excerpt"] for q in row["quotes"])
        if currency is not None:
            markers = {"INR": ("₹", "INR", "Rs", "Rs."), "USD": ("USD", "US$"), "EUR": ("EUR", "€")}
            if currency not in markers or not any(marker in quotes for marker in markers[currency]):
                raise ValueError
        return str(amount)
    except (InvalidOperation, ValueError):
        raise SafetyError("AI_SUPPLIER_PRICE_INVALID") from None


def _qualified_price(row):
    raw = row["amount"]
    number = r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?"
    bounds = re.fullmatch("(" + number + r")\s*[-–]\s*(" + number + ")", raw)
    if bounds:
        if not any(raw in quote["excerpt"] for quote in row["quotes"]):
            raise SafetyError("AI_SUPPLIER_PRICE_INVALID")
        lower, upper = (_price_amount(dict(row, amount=value.replace(",", ""))) for value in bounds.groups())
        if Decimal(lower) > Decimal(upper):
            raise SafetyError("AI_SUPPLIER_PRICE_INVALID")
        # A quoted range is not a single per-item sourcing cost. Preserve its
        # currency and quantity conditions instead of choosing one endpoint.
        return dict(row, amount=None, quoted_amount=raw, amount_range={"minimum": lower, "maximum": upper})
    if not re.fullmatch(number, raw):
        raise SafetyError("AI_SUPPLIER_PRICE_INVALID")
    return dict(row, amount=_price_amount(dict(row, amount=raw.replace(",", ""))))


def validate_supplier_result(item, value):
    """Called after F shape validation; valid siblings survive invalid items."""
    validate_supplier_input(item)
    value = deepcopy(value)  # Saved raw model output remains unchanged.
    pages = {page["id"]: page for page in item["pages"]}
    relation = value["relevance"]
    if not relation["reason"].strip() or not set(relation["references"]) <= set(pages):
        raise SafetyError("AI_SUPPLIER_REFERENCE_INVALID")
    if relation["relationship"] in ("exact_product", "relevant_company") and not relation["references"]:
        raise SafetyError("AI_SUPPLIER_REFERENCE_INVALID")
    fields = {}
    for row in value["terms"]:
        field = row["field"]
        if field not in SUPPLIER_FIELDS or field in fields or not row["reason"].strip() or not row["value"].strip():
            raise SafetyError("AI_SUPPLIER_TERM_INVALID")
        if field in YES_NO_FIELDS and row["value"] not in ("Yes", "No", "Not stated", "Conflicting"):
            raise SafetyError("AI_SUPPLIER_TERM_INVALID")
        if field == "subscription_requirement" and row["value"] not in ("Required", "Optional", "Not required", "Not stated", "Conflicting"):
            raise SafetyError("AI_SUPPLIER_TERM_INVALID")
        if (row["basis"] == "not_stated") != (row["value"] == "Not stated") or (row["basis"] == "conflicting") != (row["value"] == "Conflicting"):
            raise SafetyError("AI_SUPPLIER_TERM_BASIS_INVALID")
        if field in YES_NO_FIELDS | {"subscription_requirement"} and row["value"] not in ("Not stated", "Conflicting") and row["basis"] != "observed":
            raise SafetyError("AI_SUPPLIER_CAPABILITY_NOT_OBSERVED")
        _check_citations(row, pages, required=row["basis"] != "not_stated")
        if row["basis"] == "conflicting" and len({(q["page_id"], " ".join(q["excerpt"].split())) for q in row["quotes"]}) < 2:
            raise SafetyError("AI_SUPPLIER_CONFLICT_EVIDENCE_REQUIRED")
        fields[field] = row
    for field in SUPPLIER_FIELDS:
        fields.setdefault(field, {"field": field, "value": "Not stated", "basis": "not_stated", "references": [],
                                  "quotes": [], "reason": "No explicit term was returned for this field."})
    prices = []
    for row in value["prices"]:
        _check_citations(row, pages, required=True)
        if not row["reason"].strip():
            raise SafetyError("AI_SUPPLIER_PRICE_INVALID")
        prices.append(_qualified_price(row))
    # A stated paid membership cannot become 'no subscription' by overlooking
    # a conflicting page. Keep both rather than erase the original quotations.
    mandatory = [p for p in prices if p["price_kind"] == "mandatory_recurring"]
    subscription = fields["subscription_requirement"]
    if mandatory and subscription["value"] == "Not stated":
        fields["subscription_requirement"] = {"field": "subscription_requirement", "value": "Required", "basis": "observed",
            "references": sorted({r for p in mandatory for r in p["references"]}),
            "quotes": [q for p in mandatory for q in p["quotes"]],
            "reason": "An explicitly interpreted mandatory recurring fee establishes a required subscription."}
    elif mandatory and subscription["value"] in ("Not required", "Optional"):
        fields["subscription_requirement"] = {"field": "subscription_requirement", "value": "Conflicting", "basis": "conflicting",
            "references": sorted(set(subscription["references"] + [r for p in mandatory for r in p["references"]])),
            "quotes": subscription["quotes"] + [q for p in mandatory for q in p["quotes"]],
            "reason": "The interpreted fee and subscription statements conflict; both source statements are retained."}
    return dict(value, terms=[fields[field] for field in SUPPLIER_FIELDS], prices=prices)


def derive_supply_arrangement(terms):
    fields = {row["field"]: row for row in terms}
    def stated(field, value):
        row = fields.get(field, {})
        return row.get("basis") == "observed" and row.get("value") == value
    direct = stated("direct_customer_dispatch", "Yes")
    bulk = stated("bulk_stock_prerequisite", "Yes")
    single = stated("single_piece_purchase", "Yes")
    moq = fields.get("purchase_moq", {})
    if moq.get("basis") == "observed" and re.fullmatch(r"1\s+(piece|pieces|unit|units)", moq.get("value", ""), re.IGNORECASE):
        single = True
    if bulk and direct:
        return "Bulk stock purchase followed by individual dispatch"
    if bulk:
        return "Bulk supply; individual dispatch not established"
    if single and direct:
        return "Single-piece purchase with direct customer dispatch"
    if single:
        return "Single-piece purchase; customer dispatch not stated"
    return "Supply arrangement not stated"


def rank_supplier_leads(leads):
    def order(lead):
        terms = {row["field"]: row for row in lead["terms"]}
        relationship = lead["relevance"]["relationship"]
        arrangement = derive_supply_arrangement(lead["terms"])
        capability = 0 if arrangement == "Single-piece purchase with direct customer dispatch" else 2 if arrangement.startswith("Bulk") else 1
        sub = terms.get("subscription_requirement", {})
        fee = 0 if sub.get("basis") == "observed" and sub.get("value") == "Not required" else 2 if sub.get("value") == "Required" else 1
        return ({"exact_product": 0, "relevant_company": 1, "unclear": 2, "unrelated": 3}[relationship], capability, fee, lead["id"])
    return sorted(leads, key=order)


def _supplier_payload(items):
    aliased_items, _ = _supplier_aliases(items)
    return {"items": aliased_items, "supplier_fields": list(SUPPLIER_FIELDS),
            "supplier_reference_scope": "short-ids-v1",
            "product_match_rules": {
                "scope": "same-purchase-unit-v1",
                "identity": "Use each item's complete product description, especially function, mechanism and distinguishing features. Match one offered purchase unit; shared cleaning purpose or similar titles alone are insufficient. Ordinary seller, colour, pack and size variations may still match.",
                "catalogues": "Keep separate catalogue listings separate. Never assemble the requested mechanism from features belonging to sibling listings. A replaceable scouring-pad brush and a separate detachable-dustpan brush do not prove the combined tool is offered.",
                "partial_matches": "Retain a plausible supplier as relevant_company or unclear when only part of the mechanism is offered. Name the actual listing and explain the missing or conflicting distinguishing feature in relevance.reason; unknown minor details do not automatically disqualify a practical match.",
                "construction_uncertainty": "Conflicting material or construction statements within the same offer must be named in relevance.reason. A stainless-steel finish does not prove the base or handle material. Do not silently choose one statement or borrow a sibling listing's material. Put a brief material-confirmation caveat in its price qualifier or reason, retaining the correct matched listing and price. A practical same-mechanism match and correctly attributed price can remain useful; construction uncertainty alone does not require exclusion.",
                "attribution": "Product prices, purchase MOQ and product-specific promises must belong to the same matched purchase unit. Contextual verbatim quotations must connect its name/description with its own amount and quantity conditions. Do not borrow neighbouring listings' cheaper prices, order minimums, accessories or fulfillment promises.",
                "no_exact_offer": "If an exact target offer is not identified, return no product price rows and leave purchase_moq Not stated. Keep related-company evidence and explain related listings in relevance.reason; unrelated listing prices are not target sourcing costs. Company-wide commercial terms remain usable only within their explicit applicable scope."},
            "term_value_rules": {"yes_no_fields": sorted(YES_NO_FIELDS),
                "yes_no_values": ["Yes", "No", "Not stated", "Conflicting"],
                "subscription_values": ["Required", "Optional", "Not required", "Not stated", "Conflicting"],
                "instruction": "Use exactly these value strings for the listed fields and subscription_requirement. Put qualifications in reason, not value. An unstated capability stays value Not stated/basis not_stated; retain a general e-commerce claim without inventing specific capabilities."},
            "price_value_rules": {
                "amount": "Use a plain numeric string (for example 5000 or 5900), or the exact numeric range (for example 2500 - 5500). Never put a currency symbol, tax formula, words or a unit inside amount.",
                "quotation": "Copy the original source span exactly, including its currency symbol and words. Never replace ₹ with Rs, insert Approx. Price, expand abbreviations, or translate the quotation. Numeric amount and currency interpretation belong in their separate fields, not in a rewritten quote. If an encountered amount has unclear currency, retain currency null and the uncertainty rather than inventing a currency.",
                "conditions": "Preserve currency, tax, pack size, quantity conditions, applicable plan and qualifications in their separate fields/reason and exact source quotes. A plan price is not a product sourcing price; optional services do not establish a mandatory product fee. If no supported numeric price is stated, return no price row."}}


def _supplier_aliases(items):
    """Stable short IDs, independent of batch order; never fuzzy ID recovery."""
    aliased, references, seen_pages = [], {}, {}
    for item in items:
        lead_id = "lead_" + digest(item["id"])[:12]
        if lead_id in references and references[lead_id]["item_id"] != item["id"]:
            raise SafetyError("AI_SUPPLIER_ALIAS_COLLISION")
        pages, page_map = [], {}
        for page in item["pages"]:
            page_id = "page_" + digest(page["id"])[:12]
            if page_id in seen_pages and seen_pages[page_id] != page["id"]:
                raise SafetyError("AI_SUPPLIER_ALIAS_COLLISION")
            seen_pages[page_id] = page["id"]
            page_map[page_id] = page["id"]
            pages.append(dict(page, id=page_id))
        references[lead_id] = {"item_id": item["id"], "pages": page_map}
        aliased.append(dict(item, id=lead_id, pages=pages))
    return aliased, references


def _save_supplier_aliases(memory, runner, payload, references):
    """Keep the exact map beside immutable raw Jobs answers, before execution."""
    from .research_ai import MODEL, prompt_version
    from .storage import atomic_json, load_json, safe_path
    request = {"role": "F", "payload": payload, "model": MODEL, "prompt_version": prompt_version("F"),
               "runner": getattr(runner, "identity", "codex-chatgpt-high-v1")}
    job_id = key("job", request)
    path = safe_path(memory.root, "ai_jobs", job_id) / "supplier_reference_map.json"
    value = {"schema_version": 1, "request_hash": digest(request), "aliases": references}
    with memory.journal.locked():
        if path.exists():
            if load_json(path) != value:
                raise SafetyError("AI_SUPPLIER_ALIAS_MAP_CONFLICT")
        else:
            atomic_json(path, value)
    return job_id


def _original_supplier_result(value, reference):
    """Translate only already validated short references on an independent copy."""
    value = deepcopy(value)
    value["id"] = reference["item_id"]
    pages = reference["pages"]
    value["relevance"]["references"] = [pages[page] for page in value["relevance"]["references"]]
    for row in value["terms"] + value["prices"]:
        row["references"] = [pages[page] for page in row["references"]]
        for quote in row["quotes"]:
            quote["page_id"] = pages[quote["page_id"]]
    return value


def extract_supplier_terms(memory, items, runner=None, *, source_dates=None, current_guard=None):
    """Use saved page text only. Retain partial results by immutable input hash."""
    from .research_ai import MAX_JOB_BYTES, MODEL, SUPPLIER_PROMPT_VERSION
    from .supplier_quote_repair import resolve_supplier_question, supplier_original_retry_question
    if current_guard is not None and not callable(current_guard):
        raise SafetyError("SUPPLIER_CURRENT_GUARD_INVALID")
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise SafetyError("SUPPLIER_BATCH_INVALID")
    for item in items:
        validate_supplier_input(item)
    if len({item["id"] for item in items}) != len(items) or len(canonical(items)) > 1024 * 1024:
        raise SafetyError("SUPPLIER_BATCH_INVALID")
    if source_dates is not None:
        if not isinstance(source_dates, dict) or set(source_dates) != {item["id"] for item in items}:
            raise SafetyError("SUPPLIER_SOURCE_DATES_INVALID")
        for item in items:
            dates = source_dates[item["id"]]
            if not isinstance(dates, dict) or set(dates) != {page["id"] for page in item["pages"]}:
                raise SafetyError("SUPPLIER_SOURCE_DATES_INVALID")
            for date in dates.values():
                timestamp(date)
    accepted, failures, failure_job_ids, reused, pending = {}, {}, {}, [], []
    state = memory.snapshot()
    records = state["supplier_assessments"]
    for item in items:
        binding = [item, VERSION, MODEL, SUPPLIER_PROMPT_VERSION, getattr(runner, "identity", "codex-chatgpt-high-v1")]
        if source_dates is not None:
            binding.append({"source_dates": source_dates[item["id"]]})
        assessment_id = key("supplier", binding)
        if assessment_id in records:
            try:
                if current_guard is not None:
                    current_guard(item, memory.snapshot())
                accepted[item["id"]] = records[assessment_id]["result"]
                reused.append(item["id"])
            except (SafetyError, OSError) as error:
                failures[item["id"]] = _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")
        else:
            pending.append((assessment_id, item))

    if not pending:
        # Reusing current saved assessments needs no fresh alias group,
        # correction lookup or execution preparation. Current guards above
        # still apply, including their precise local failures.
        return {"accepted": accepted, "failures": failures,
                "failure_job_ids": failure_job_ids, "reused": reused}

    def apply(assessment_id, item, value, job_id):
        value = dict(value, supply_arrangement=derive_supply_arrangement(value["terms"]), client_ready=False)
        record = {"input_hash": digest(item), "input": item,
                  "version": VERSION, "job_id": job_id, "checked_at": utc_now(), "result": value}
        if source_dates is not None:
            record["source_dates"] = dict(source_dates[item["id"]])
        if current_guard is None:
            # Preserve the direct-call file-recovery seam. The actual capture
            # path supplies a guard and validates inside the canonical commit.
            memory.put("supplier_assessments", assessment_id, record)
        else:
            current_guard(item, memory.snapshot())
            def unchanged(current):
                current_guard(item, current)
                if current["jobs"].get(job_id, {}).get("request", {}).get("payload", {}).get("supplier_repair"):
                    from .supplier_quote_repair import supplier_model_question
                    hint = supplier_model_question(memory, item, state=current)
                    if hint["job_id"] != job_id or hint["accepted"] is None:
                        raise SafetyError("SUPPLIER_QUOTE_REPAIR_ORIGINAL_CHANGED")
                    _, refs = _supplier_aliases([item])
                    saved = _original_supplier_result(hint["accepted"], refs[hint["accepted"]["id"]])
                    saved = dict(saved, supply_arrangement=derive_supply_arrangement(saved["terms"]), client_ready=False)
                    if saved != value:
                        raise SafetyError("SUPPLIER_QUOTE_REPAIR_ORIGINAL_CHANGED")
            memory.commit(key("supplier-apply", [assessment_id, job_id]),
                [change("supplier_assessments", assessment_id, record)],
                validate=unchanged)
        accepted[item["id"]] = value

    # Exact completed originals/corrections precede runner-sensitive Jobs
    # identity and chunking. A runner change is not a second repair allowance.
    fresh = []
    current_aliased_items = _supplier_payload(items)["items"]
    for assessment_id, item in pending:
        try:
            hint = (resolve_supplier_question(memory, item, runner, current_guard=current_guard)
                    if SUPPLIER_PROMPT_VERSION == "research-f-v3" else None)
            if hint is not None:
                hint = supplier_original_retry_question(memory, hint, runner, current_items=current_aliased_items)
            if hint is None or hint["state"] == "original" and hint["accepted"] is None:
                fresh.append((assessment_id, item))
            elif hint["accepted"] is not None:
                _, references = _supplier_aliases([item])
                value = _original_supplier_result(hint["accepted"], references[hint["accepted"]["id"]])
                apply(assessment_id, item, value, hint["job_id"])
            else:
                failures[item["id"]] = hint["failure"] or "SUPPLIER_INTERPRETATION_PENDING"
                if hint["job_id"]:
                    failure_job_ids[item["id"]] = hint["job_id"]
        except (SafetyError, OSError) as error:
            failures[item["id"]] = _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")
    pending = fresh
    before_original_execution = None
    if current_guard is not None:
        # Jobs may recover a differently grouped original. Freeze the full
        # supplied group, and prove every actually dispatched original item
        # again immediately before intent creation, not only before aliases
        # or retry preparation. A missing sibling is not current by default.
        expected_payload = deepcopy(_supplier_payload(items))
        _, expected_references = _supplier_aliases(items)
        supplied_originals = {item["id"]: deepcopy(item) for item in items}
        originals_by_alias = {alias: supplied_originals[reference["item_id"]]
                              for alias, reference in expected_references.items()}
        expected_items = {item["id"]: item for item in expected_payload["items"]}
        expected_context = {name: value for name, value in expected_payload.items() if name != "items"}
        expected_identity = getattr(runner, "identity", "codex-chatgpt-high-v1")

        def before_original_execution(request, folder):
            actual = request.get("payload")
            if (request.get("role") != "F" or request.get("model") != MODEL
                    or request.get("prompt_version") != SUPPLIER_PROMPT_VERSION
                    or request.get("runner") != expected_identity or not isinstance(actual, dict)
                    or {name: value for name, value in actual.items() if name != "items"} != expected_context
                    or not isinstance(actual.get("items"), list) or not actual["items"]
                    or any(not isinstance(item, dict) or expected_items.get(item.get("id")) != item
                           for item in actual["items"])):
                raise SafetyError("AI_JOB_PENDING_RECONCILIATION")
            current = memory.snapshot()
            for item in actual["items"]:
                current_guard(originals_by_alias[item["id"]], current)

    chunks, chunk = [], []
    for entry in pending:
        proposed = chunk + [entry]
        if chunk and (len(proposed) > 5 or len(canonical(_supplier_payload([item for _, item in proposed]))) > MAX_JOB_BYTES):
            chunks.append(chunk)
            chunk = []
        chunk.append(entry)
    if chunk:
        chunks.append(chunk)
    for chunk in chunks:
        original_items = [item for _, item in chunk]
        payload = _supplier_payload(original_items)
        _, references = _supplier_aliases(original_items)
        job_id = None
        try:
            if current_guard is not None:
                current = memory.snapshot()
                for item in original_items:
                    current_guard(item, current)
            job_id = _save_supplier_aliases(memory, runner, payload, references)
            job_id, results = run_current_model_job(memory, runner, "F", payload,
                current_items=_supplier_payload([item for _, item in pending])["items"],
                **({"before_new_execution": before_original_execution} if current_guard is not None else {}))
        except (SafetyError, OSError) as error:
            failure = _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")
            failures.update({item["id"]: failure for _, item in chunk})
            if job_id:
                failure_job_ids.update({item["id"]: job_id for _, item in chunk})
            continue
        for alias_id, error in results["failures"].items():
            original_id = references[alias_id]["item_id"]
            failures[original_id] = error
            failure_job_ids[original_id] = job_id
        # A newly completed original may contain one invalid quotation while
        # its siblings are valid. Correct only that rejected named item.
        corrected = {}
        if SUPPLIER_PROMPT_VERSION == "research-f-v3":
            for _, item in chunk:
                alias_id = next(alias for alias, ref in references.items() if ref["item_id"] == item["id"])
                if results["failures"].get(alias_id) != "AI_SUPPLIER_QUOTE_INVALID":
                    continue
                try:
                    hint = resolve_supplier_question(memory, item, runner, current_guard=current_guard)
                    if hint["accepted"] is not None:
                        corrected[alias_id] = hint
                        failures.pop(item["id"], None)
                        failure_job_ids.pop(item["id"], None)
                    else:
                        failures[item["id"]] = hint["failure"] or "SUPPLIER_INTERPRETATION_PENDING"
                        if hint["job_id"]:
                            failure_job_ids[item["id"]] = hint["job_id"]
                except (SafetyError, OSError) as error:
                    failures[item["id"]] = _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")
        by_original = {references[alias_id]["item_id"]: _original_supplier_result(value, references[alias_id])
                       for alias_id, value in results["accepted"].items()}
        by_original.update({references[alias_id]["item_id"]:
            _original_supplier_result(hint["accepted"], references[alias_id]) for alias_id, hint in corrected.items()})
        for assessment_id, item in chunk:
            value = by_original.get(item["id"])
            if value is None:
                continue
            try:
                alias_id = next(alias for alias, ref in references.items() if ref["item_id"] == item["id"])
                answer_job = corrected[alias_id]["job_id"] if alias_id in corrected else job_id
                apply(assessment_id, item, value, answer_job)
            except (SafetyError, OSError) as error:
                # The original model reply is already durable. Isolate a local
                # application failure; replay reuses it instead of recollecting
                # pages or issuing the same model request again.
                failures[item["id"]] = _interpretation_failure(error, local_code="SUPPLIER_REVIEW_LOCAL_FILE_FAILED")
                failure_job_ids[item["id"]] = answer_job
    return {"accepted": accepted, "failures": failures, "failure_job_ids": failure_job_ids, "reused": reused}
