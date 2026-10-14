"""Join accepted saved research into allocator rows and factual report inputs.

No collection, model invocation, memory mutation, approval or publication occurs.
Supplier absence is allowed after a recorded cursory search; absence of that
search is a named remaining task. A bounded useful-update rule compares actual
published facts; timestamps or a supplied boolean never establish usefulness.
"""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from .discovery import latest_observations
from .models import SafetyError, digest, identifier, timestamp, utc_now
from .material_updates import material_snapshot, review_supplier_update, review_material_update, prepare_material_update
from .research_ai import STATES, FIT_PROMPT_VERSIONS
from .research_flow import candidate_summary, evidence_indexes, trends_description_current, fit_description_context
from .research_memory import key
from .service import CATEGORIES
from .suppliers import VERSION as SUPPLIER_VERSION, derive_supply_arrangement, rank_supplier_leads
from .brand_reviews import current_brand, brand_binding
from .fit_policy import evaluate_fit, policy_for, FIT_POLICY_ID, LEGACY_POLICY_ID
from .supplier_effort import current_supplier_effort
from .opportunity_policy import (evaluate_opportunity, opportunity_fields, opportunity_sort_key,
                                 opportunity_counts, opportunity_source_limit, CURRENT_OPPORTUNITY_POLICY_ID)
from .report_source_facts import (CONTRACT as SOURCE_ROUTE_CONTRACT, FIELDS as SOURCE_ROUTE_FIELDS,
                                  FUNCTIONAL_CONTRACT as FUNCTIONAL_SOURCE_ROUTE_CONTRACT,
                                  CONTRACTS as SOURCE_ROUTE_CONTRACTS,
                                  coverage_rows, route_dependencies, source_route_facts,
                                  functional_task_projection)


def _url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password
                or {"token", "access_token", "api_key", "apikey", "password", "secret", "authorization"}
                & {name.casefold() for name in parse_qs(parsed.query)}):
            return None
        return value
    except ValueError:
        return None


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    # Accept only an unambiguous displayed number, not a range, discount or text.
    raw = str(value).strip()
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,8})?", raw):
        return None
    try:
        number = Decimal(raw.replace(",", ""))
        return format(number, "f") if number.is_finite() and 0 <= number <= Decimal("1000000000") else None
    except InvalidOperation:
        return None


def _identity(state, concept_id, concept, associations):
    description = concept["description"]
    if description["category"] not in CATEGORIES:
        return "out_of_scope", "The category is outside the client's four categories."
    if current_brand(state, "concept", concept_id, description) == "established":
        review = concept.get("brand_review", {})
        return "established_brand", ("Source-backed operator review: " + review["reason"]
            if review.get("method") == "operator_source_review" and isinstance(review.get("reason"), str)
            else "The accepted description identifies an established brand.")
    if concept.get("identity_conflicted") or concept.get("status") in ("excluded", "merged", "retired"):
        return "identity_pending", "The canonical concept is conflicted or no longer active."
    if not associations:
        return "identity_pending", "No resolved source-to-concept association remains."
    for child_id, association in associations:
        semantic_id = association["semantic_id"]
        saved = state["descriptions"].get(semantic_id)
        if not saved:
            return "identity_pending", "A resolved association has no accepted description."
        result = saved["result"]
        current_children = {key("child", [semantic_id, index, digest(result)]) for index, _ in enumerate(result["products"])}
        if child_id not in current_children:
            return "identity_pending", "Identity refers to a superseded description; review that changed purchase unit."
    return None, None


def _assessment(state, concept_id, concept, as_of, by_concept, by_semantic, fit_policy_id=None, recommendation_contract=None):
    record = state["assessments"].get(concept_id)
    if not record:
        return None, "checklist_review_required", "No accepted checklist review is saved."
    from .fit_description import FUNCTIONAL_CONTRACT
    from .functional_types import functional_role_context
    if (record.get("contract_version") != FUNCTIONAL_CONTRACT
            and functional_role_context(state, concept_id, role="C") is not None):
        return None, "checklist_review_required", (
            "The accepted product type needs its own checklist review; the saved older review is retained but is not reused.")
    if record.get("contract_version") == FUNCTIONAL_CONTRACT:
        if recommendation_contract != "functional-type-examples-v1":
            return None, "functional_report_contract_required", (
                "The product-type checklist is saved. The matching report format must be connected before this type can enter a report.")
        from .research_flow import current_assessment_fit
        if current_assessment_fit(state, concept_id, expected_policy_id=FIT_POLICY_ID,
                                  indexes=(by_concept, by_semantic)) is None:
            return None, "checklist_review_required", "The product-type checklist has changed inputs; refresh only affected questions."
        return record, None, None
    evidence = []
    for semantic_id in sorted(by_concept.get(concept_id, set())):
        observations = by_semantic.get(semantic_id, {})
        if not observations:
            return None, "checklist_review_required", "A checklist input observation is missing."
        obs_id, observation = next(iter(observations.items()))
        if timestamp(observation["observed_at"]) > timestamp(as_of):
            return None, "checklist_review_required", "The accepted checklist uses evidence collected after this report time."
        evidence.append({"id": obs_id, "semantic_id": semantic_id, "source_text": observation["semantic"]})
    item = {"id": concept_id, "description": concept["description"], "observations": sorted(evidence, key=lambda row: row["id"])}
    if record.get("input_hash") != digest(item):
        return None, "checklist_review_required", "The description or checklist evidence changed; refresh only this review."
    result = record["result"]
    criteria = result.get("criteria", [])
    known = {row["id"] for row in evidence}
    declared = record.get("fit_policy_id", LEGACY_POLICY_ID)
    if fit_policy_id is not None and declared != fit_policy_id:
        return None, "checklist_review_required", "The saved checklist uses the old policy; review its saved facts, not the sources again."
    if declared == FIT_POLICY_ID and record.get("contract_version") not in FIT_PROMPT_VERSIONS:
        return None, "checklist_review_required", "The saved ten-point judgment lacks its matching scoring contract; review only this saved assessment."
    expected = policy_for(declared)
    if (len(criteria) != expected["total"] or [row.get("id") for row in criteria] != list(expected["active_ids"])
            or declared == LEGACY_POLICY_ID and criteria[2].get("state") != "Not assessed"
            or any(row.get("state") not in STATES or not set(row.get("references", [])) <= known for row in criteria)
            or any(row.get("id") not in known for row in result.get("relevance", []))):
        return None, "checklist_review_required", "The saved checklist states or evidence references are not valid current inputs."
    evaluate_fit(criteria, declared, **fit_description_context(record, concept["description"]))
    return record, None, None


def _supplier_scope(product):
    return {field: product.get(field) for field in ("concept_id", "label", "category", "function", "mechanism", "features")}


def _moq(terms):
    row = terms.get("purchase_moq", {})
    found = re.fullmatch(r"(\d+)\s+(?:piece|pieces|unit|units|item|items)", row.get("value", ""), re.IGNORECASE)
    return int(found[1]) if found and row.get("basis") == "observed" and int(found[1]) > 0 else None


def _supplier_price(row, pages, checked_at, relationship, moq):
    source = next((_url(pages[ref]["url"]) for ref in row["references"] if ref in pages), None)
    raw = row.get("quoted_amount", row.get("amount"))
    currency = row.get("currency")
    conditions = "; ".join(str(value) for value in (row.get("qualifier"), row.get("quantity_condition"),
                                                    row.get("reason")) if value)
    if row.get("amount_range"):
        conditions = f"Quoted range {raw}; not a single sourcing cost; " + conditions
    if relationship != "exact_product":
        conditions = "Company-level lead, not a confirmed quote for this product; " + conditions
    if currency != "INR":
        return None, f"Quoted {raw if raw is not None else 'amount not stated'}; currency {currency or 'not stated'}; {conditions}"
    unit_text = row.get("unit", "").casefold().strip()
    unit = "per_item" if unit_text in ("piece", "per piece", "unit", "per unit", "each", "item", "per item") else (
        "per_pack" if "pack" in unit_text or "set" in unit_text else "not_stated")
    if moq is not None and moq > 1:
        unit = "bulk_quote"
    amount = _amount(row.get("amount"))
    return {"kind": "supplier", "currency": "INR", "amount": amount, "unit": unit, "quantity": None,
            "moq": moq, "conditions": conditions, "source_url": source, "observed_at": checked_at,
            "date_basis": "supplier_interpretation_review", "relationship": relationship,
            "quoted_amount": raw, "amount_range": deepcopy(row.get("amount_range")), "original_unit": row.get("unit")}, None


def _suppliers(records, concept_id, description, as_of, source_checks=()):
    wanted = _supplier_scope(dict(description, concept_id=concept_id))
    latest, discarded = {}, []
    for record_id, record in records:
        product = record.get("input", {}).get("product")
        if not isinstance(product, dict) or product.get("concept_id") != concept_id:
            continue
        if record.get("version") != SUPPLIER_VERSION or _supplier_scope(product) != wanted:
            discarded.append({"id": record_id, "reason": "Supplier interpretation belongs to an older product scope or version."})
            continue
        if record.get("input_hash") != digest(record["input"]) or record["result"].get("id") != record["input"]["id"]:
            raise SafetyError("SAVED_SUPPLIER_INPUT_INVALID")
        source_dates = record.get("source_dates")
        if source_dates is not None:
            if not isinstance(source_dates, dict) or set(source_dates) != {page["id"] for page in record["input"]["pages"]}:
                raise SafetyError("SAVED_SUPPLIER_INPUT_INVALID")
            if any(timestamp(date) > timestamp(as_of) for date in source_dates.values()):
                discarded.append({"id": record_id, "reason": "Supplier page capture is later than this report time."})
                continue
        if timestamp(record["checked_at"]) > timestamp(as_of):
            discarded.append({"id": record_id, "reason": "Supplier review is later than this report time."})
            continue
        lead_id = record["input"]["id"]
        old = latest.get(lead_id)
        if old is None or (timestamp(record["checked_at"]), record_id) > (timestamp(old[1]["checked_at"]), old[0]):
            latest[lead_id] = (record_id, record)
    leads, prices, raw_prices, refs, excluded, notes = [], [], [], [], discarded, []
    for lead_id, (_, record) in sorted(latest.items()):
        result, pages = record["result"], {page["id"]: page for page in record["input"]["pages"]}
        relationship = result["relevance"]["relationship"]
        if relationship not in ("exact_product", "relevant_company"):
            excluded.append({"id": lead_id, "relationship": relationship, "reason": result["relevance"]["reason"]})
            continue
        related = result["relevance"]["references"]
        first = next((pages[ref] for ref in related if ref in pages), next(iter(pages.values())))
        terms = {row["field"]: row for row in result["terms"]}
        def observed(field, value):
            row = terms.get(field, {})
            return row.get("basis") == "observed" and row.get("value") == value
        single = observed("single_piece_purchase", "Yes") or _moq(terms) == 1
        dispatch = ("not_supported" if observed("bulk_stock_prerequisite", "Yes") or observed("single_piece_purchase", "No")
                    or observed("direct_customer_dispatch", "No") else "supported" if single and observed("direct_customer_dispatch", "Yes")
                    else "not_stated")
        subscription = ("no_subscription_stated" if observed("subscription_requirement", "Not required") else
                        "subscription_stated" if observed("subscription_requirement", "Required") else "not_stated")
        arrangement = derive_supply_arrangement(result["terms"])
        reason = result["relevance"]["reason"] + " " + arrangement + "."
        extra = []
        for row in result["prices"]:
            raw_prices.append({"lead_id": lead_id, "reviewed_at": record["checked_at"], "relationship": relationship, **deepcopy(row)})
            if row["price_kind"] != "product":
                continue  # Membership/setup/order fees are not product sourcing costs.
            dates = record.get("source_dates", {})
            quoted_dates = [dates[ref] for ref in row["references"] if ref in dates]
            price_date = min(quoted_dates, key=timestamp) if quoted_dates else record["checked_at"]
            adapted, note = _supplier_price(row, pages, price_date, relationship, _moq(terms))
            if adapted:
                if quoted_dates:
                    adapted["date_basis"] = "supplier_page_capture"
                prices.append(dict(adapted, lead_id=lead_id))
            if note:
                extra.append(note)
                notes.append({"concept_id": concept_id, "code": "supplier_price_currency_unverified", "reason": note, "blocking": False})
        if terms.get("purchase_moq", {}).get("value") not in (None, "Not stated"):
            extra.append("Minimum order statement: " + terms["purchase_moq"]["value"] + ".")
        if terms.get("subscription_requirement", {}).get("value") == "Conflicting":
            extra.append("Subscription statements conflict.")
        leads.append({"lead_id": lead_id, "name": urlsplit(first["url"]).hostname or first["title"], "url": _url(first["url"]),
                      "single_piece_dispatch": dispatch, "subscription": subscription, "min_order": _moq(terms),
                      "ecommerce_support": terms.get("ecommerce_support", {}).get("value", "Not stated"),
                      "reason": " ".join([reason, *extra]), "relationship": relationship,
                      "supply_arrangement": arrangement, "terms": deepcopy(result["terms"]),
                      "reviewed_at": record["checked_at"],
                      "source_capture_at": min(record["source_dates"].values(), key=timestamp) if record.get("source_dates") else None,
                      "id": lead_id, "relevance": deepcopy(result["relevance"])})
        used = set(related)
        used.update(ref for row in result["terms"] for ref in row["references"])
        used.update(ref for row in result["prices"] for ref in row["references"])
        for page_id in sorted(used):
            if page_id in pages:
                refs.append({"id": key("supplier-ref", [lead_id, page_id]), "url": _url(pages[page_id]["url"]),
                             "source": "Supplier page (reviewed)",
                             "observed_at": record.get("source_dates", {}).get(page_id, record["checked_at"]),
                             "date_basis": "supplier_page_capture" if page_id in record.get("source_dates", {}) else "supplier_interpretation_review",
                             "original_page_id": page_id})
    ordered = rank_supplier_leads(leads)
    # Keep all inspected outcomes for audit; usually display only the best three.
    no_lead_checks = [(check_id, check) for check_id, check in source_checks
                      if check.get("source") == "supplier" and check.get("concept_id") == concept_id
                      and check.get("description_hash") == digest(description)
                      and check.get("state") == "usable" and check.get("supplier_search_status") == "complete"
                      and type(check.get("candidate_count")) is int and check["candidate_count"] == 0
                      and check.get("at") and timestamp(check["at"]) <= timestamp(as_of)]
    for check_id, check in no_lead_checks:
        notes.append({"concept_id": concept_id, "code": "supplier_search_no_results",
                      "reason": check["reason"], "blocking": False})
    effort = current_supplier_effort({"source_checks": dict(source_checks)}, concept_id, description,
                                    as_of=as_of, accepted_lead_refs=[latest[lead["lead_id"]][0] for lead in ordered])
    # Existing accepted page interpretation is separate legacy effort proof.
    # Do not manufacture a network receipt or mislabel rejected F rows usable.
    effort["legacy_interpretation_ids"] = sorted(record_id for record_id, _ in latest.values())
    effort["legacy_search_check_ids"] = sorted(check_id for check_id, _ in no_lead_checks)
    satisfied = bool(latest or no_lead_checks or effort["effort_satisfied"])
    status = ("complete" if latest or no_lead_checks else
              "blocked" if effort["internal_outcome"] in ("failed", "blocked") else "attempted") if satisfied else "not_started"
    return {"status": status, "checked": len(latest), "leads": ordered[:3], "effort_satisfied": satisfied,
            "effort": effort,
            "search_check_ids": sorted(set([check_id for check_id, _ in no_lead_checks] + effort["check_ids"])),
            "assessment_ids": sorted(record_id for record_id, _ in latest.values()),
            "all_leads": ordered, "prices": prices, "raw_prices": raw_prices, "source_refs": refs,
            "excluded": excluded, "notes": notes}


def _facts(state, product, assessment, suppliers, as_of, current_observations, concept_reviews, route_coverage=None):
    concept_id = product["id"]
    relevance = {state["observations"][row["id"]]["semantic_id"]: row["relationship"]
                 for row in (assessment or {}).get("result", {}).get("relevance", [])}
    relevance_reasons = {state["observations"][row["id"]]["semantic_id"]: row.get("reason")
                         for row in (assessment or {}).get("result", {}).get("relevance", [])}
    from .market_validation import REVIEW_VERSION
    reviews = sorted(((review_id, review) for review_id, review in concept_reviews
                      if review.get("review_version") == REVIEW_VERSION
                      and review["description_hash"] == digest(state["concepts"][concept_id]["description"])
                      and timestamp(review["reviewed_at"]) <= timestamp(as_of)),
                     key=lambda row: (timestamp(row[1]["reviewed_at"]), row[1]["input_hash"]))
    current_review_by_semantic = {}
    for review_id, review in reviews:
        for row in review["relevance"]:
            if row["id"] in state["observations"] and timestamp(state["observations"][row["id"]]["observed_at"]) <= timestamp(as_of):
                relevance[state["observations"][row["id"]]["semantic_id"]] = row["relationship"]
                relevance_reasons[state["observations"][row["id"]]["semantic_id"]] = row.get("reason")
                current_review_by_semantic[state["observations"][row["id"]]["semantic_id"]] = review_id
    refs, checks, expiries, trend_ids = [], [], [], []
    for family, signal in product["market"]["families"].items():
        urls, dates = [], []
        for obs_id in signal["used_ids"]:
            observation = state["observations"].get(obs_id)
            if observation:
                url, at = _url(observation["facts"].get("url")), observation["observed_at"]
                refs.append({"id": obs_id, "url": url, "source": family, "observed_at": at,
                             "relationship": relevance.get(observation["semantic_id"], "unclear"),
                             "relevance_reason": relevance_reasons.get(observation["semantic_id"])})
                urls.append(url); dates.append(at)
                if signal["available"]:
                    expiries.append(timestamp(at) + timedelta(days=7))
            elif family == "trends":
                for trend_id, trend in state["trends_evidence"].items():
                    metadata = trend.get("parsed", {}).get("metadata", {})
                    if trend["concept_id"] == concept_id and metadata.get("source_reference") == obs_id:
                        at = metadata["collected_at"]
                        refs.append({"id": key("trend-ref", [trend_id, obs_id]), "url": _url(obs_id), "source": "Google Trends export",
                                     "observed_at": at, "original_reference": obs_id})
                        trend_ids.append(trend_id)
                        urls.append(_url(obs_id)); dates.append(at)
                        if signal["available"]:
                            expiries.append(timestamp(at) + timedelta(days=7))
        check = {"source": family, "state": signal["state"], "detail": signal["reason"], "evidence_ids": list(signal["used_ids"]),
                 "url": next((url for url in urls if url), None)}
        if dates:
            check["observed_at"] = max(dates, key=timestamp)
        checks.append(check)
    # Checklist meaning may cite an older original observation. Preserve that
    # dated citation without letting it contribute a current market signal.
    for criterion in product["criteria"]:
        for obs_id in criterion["references"]:
            observation = state["observations"][obs_id]
            refs.append({"id": obs_id, "url": _url(observation["facts"].get("url")),
                         "source": observation["source"], "observed_at": observation["observed_at"],
                         "relationship": relevance.get(observation["semantic_id"], "unclear"),
                         "relevance_reason": relevance_reasons.get(observation["semantic_id"])})
    prices, raw_retail = [], []
    for row in product["prices"]:
        observation = state["observations"][row["observation_id"]]
        raw_retail.append(dict(deepcopy(row), relationship=relevance.get(observation["semantic_id"], "unclear")))
        displayed = row.get("displayed")
        # Direct cards preserve the exact original quote. Removing only a
        # currency prefix admits an unambiguous number, never ranges, /100g,
        # discounts, pack conversions or missing prices turned into zero.
        numeric_displayed = re.sub(r"^(?:₹|INR\s+|Rs\.?\s*)\s*", "", displayed.strip()) if isinstance(displayed, str) else displayed
        amount = _amount(numeric_displayed) if row.get("currency") == "INR" else None
        if row.get("currency") != "INR" or row["observation_id"] not in current_observations:
            continue
        displayed_note = (f"Displayed quote: {row['displayed']}; not a single verified price. "
                          if amount is None and isinstance(row.get("displayed"), str) and row["displayed"].strip() else "")
        pack_text = row.get("pack_basis", observation["facts"].get("quantity"))
        pack_note = (f"Observed pack text: {pack_text}; no per-item price calculation. "
                     if isinstance(pack_text, str) and pack_text not in ("unknown", "not_verified", "not_stated") else "")
        prices.append({"kind": "retail", "currency": "INR", "amount": amount, "unit": "not_stated", "quantity": None,
                       "moq": None, "conditions": "Retail listing; pack quantity, tax and shipping not verified. " +
                       displayed_note + pack_note +
                       ("Relevant family example, not an exact product price." if relevance.get(observation["semantic_id"]) == "relevant_family" else
                        "Exact-product relevance is an accepted research judgment, not a supplier quote."),
                       "source_url": _url(row.get("source_url", observation["facts"].get("url"))), "observed_at": row["observed_at"],
                       "relationship": relevance.get(observation["semantic_id"]), "raw_displayed": row.get("displayed"),
                       "relevance_reason": relevance_reasons.get(observation["semantic_id"]),
                       "alternate_displayed": row.get("alternate_displayed")})
    contexts, image_candidates = {}, []
    for observation_id, observation in current_observations.items():
        relation = relevance.get(observation["semantic_id"], "unclear")
        if relation not in ("exact_product", "relevant_family"):
            continue
        source_facts = observation["facts"]
        source = observation["source"]
        if (source in ("amazon", "flipkart") and source_facts.get("list_type") == "product_search"
                or source in ("blinkit", "zepto", "instamart") and source_facts.get("list_type") == "quickcommerce_context"):
            route = source_facts.get("source_route") or (source + "-search" if source in ("amazon", "flipkart") else source)
            contexts.setdefault(route, []).append((observation_id, observation))
            refs.append({"id": observation_id, "url": _url(source_facts.get("url")), "source": route,
                         "observed_at": observation["observed_at"], "relationship": relation,
                         "relevance_reason": relevance_reasons.get(observation["semantic_id"])})
        urls = source_facts.get("image_urls", [])
        original = observation.get("semantic", {}).get("original_image_url")
        if original: urls = list(urls) + [original]
        for url in urls:
            clean = _url(url)
            if clean:
                image_candidates.append({"observation_id": observation_id, "url": clean, "source": source,
                                         "observed_at": observation["observed_at"], "relationship": relation})
    for route, rows in sorted(contexts.items()):
        unique_items = {observation["facts"].get("asin") or observation["facts"].get("product_id") or observation["source_item"]
                        for _, observation in rows}
        location = next((observation["facts"].get("location") for _, observation in rows if observation["facts"].get("location")), None)
        location_note = (" Observed location: " + str(location.get("name", "not stated")) + ".") if isinstance(location, dict) else ""
        checks.append({"source": route, "state": "Available",
                       "detail": f"{len(unique_items)} relevant listing examples; prices and availability are context, not sales or bestseller proof." + location_note,
                       "evidence_ids": [i for i, _ in rows], "url": _url(rows[0][1]["facts"].get("url")),
                       "observed_at": max((o["observed_at"] for _, o in rows), key=timestamp), "context_only": True})
    # Version-two source confirmations retain their original observation/chart
    # IDs in the factual packet. This also freshness-binds generic listings and
    # quick-commerce context rather than letting an older checklist date govern.
    for source, signal in (product["market"].get("sources", {})
                           if (assessment or {}).get("fit_policy_id") == FIT_POLICY_ID else {}).items():
        if signal.get("available") is not True:
            continue
        if signal.get("support_review_id"):
            support_detail = signal["support_reason"]
            if signal["support_basis"] == "inferred" and not support_detail.casefold().startswith("estimated:"):
                support_detail = "Estimated: " + support_detail
            checks.append({"source": source + " interpretation", "state": signal["support_judgment"],
                "detail": support_detail, "evidence_ids": list(next(
                    row["references"] for row in product["market"]["source_support"]
                    if row["review_id"] == signal["support_review_id"] and row["source"] == source)),
                "basis": signal["support_basis"], "market_review_id": signal["support_review_id"],
                "interpretation_only": True})
        for evidence_id in signal["used_ids"]:
            observation = state["observations"].get(evidence_id)
            if observation:
                at = observation["observed_at"]
                refs.append({"id": evidence_id, "url": _url(observation["facts"].get("url")), "source": source,
                             "observed_at": at, "relationship": relevance.get(observation["semantic_id"], "unclear"),
                             "relevance_reason": relevance_reasons.get(observation["semantic_id"])})
            elif source == "google-trends" and evidence_id in state["trends_evidence"]:
                evidence = state["trends_evidence"][evidence_id]
                metadata = evidence["parsed"]["metadata"]; at = metadata["collected_at"]
                refs.append({"id": evidence_id, "url": _url(metadata["source_reference"]), "source": "Google Trends export",
                             "observed_at": at, "original_reference": metadata["source_reference"]})
                trend_ids.append(evidence_id)
            else:
                raise SafetyError("OPPORTUNITY_SOURCE_REFERENCE_INVALID")
            expiries.append(timestamp(at) + timedelta(days=7))
    refs.extend(suppliers["source_refs"])
    refs = list({ref["id"]: ref for ref in refs}.values())
    # Transfer accepted E explanations verbatim; the writing model may shorten
    # them but must not invent advantages from the title or market label alone.
    # Source references and inferred/observed distinctions survive translation.
    policy_id = (assessment or {}).get("fit_policy_id", LEGACY_POLICY_ID)
    description = state["concepts"][concept_id]["description"]
    fit_context = fit_description_context(assessment, description) if assessment else {}
    fit = evaluate_fit(product["criteria"], policy_id, **fit_context) if assessment else None
    benefit_order = {number: rank for rank, number in enumerate((8, 6, 7, 11, 5, 10, 12, 9, 4, 2, 1, 13))}
    writing_facts = [{"text": criterion["reason"], "basis": criterion["basis"],
                      "references": deepcopy(criterion["references"]),
                      **({"description_inference": deepcopy(criterion["description_inference"])}
                         if criterion.get("description_inference") is not None else {})}
                     for criterion in sorted(product["criteria"], key=lambda row: benefit_order.get(row["id"], 99))
                     if criterion["id"] != 3 and criterion["state"] == "Meets"
                     and criterion.get("basis") in ("observed", "inferred")
                     and (criterion.get("references") or fit_context and criterion.get("description_inference") is not None)][:6]
    image_candidates = list({(row["observation_id"], row["url"]): row for row in image_candidates}.values())
    from .product_images import current_public_image
    photo = current_public_image(state, concept_id, image_candidates, as_of=as_of, require_report_quality=True)
    if photo and photo["source_ref"] not in {ref["id"] for ref in refs}:
        observation = state["observations"][photo["source_ref"]]
        refs.append({"id": photo["source_ref"], "url": _url(observation["facts"].get("url")),
                     "source": observation["source"], "observed_at": observation["observed_at"],
                     "relationship": photo["relationship"]})
    source_routes = None
    if policy_id == FIT_POLICY_ID and product["market"].get("source_policy_id") == CURRENT_OPPORTUNITY_POLICY_ID:
        related = {observation_id: observation for observation_id, observation in current_observations.items()
                   if relevance.get(observation["semantic_id"]) in ("exact_product", "relevant_family")}
        source_routes = source_route_facts(state, concept_id, route_coverage or [], product, related, as_of)
        for check in source_routes:
            check["url"] = _url(check["url"])
        from .live_outputs import SUPPLIER_TERMS_DISPLAY_VERSION
    return {"concept_id": concept_id, "title": product["label"],
            "market_label": (product["market"].get("source_label", product["market"]["label"])
                             if policy_id == FIT_POLICY_ID else product["market"]["label"]), "market_checks": checks,
            "prices": prices + suppliers["prices"], "suppliers": suppliers["all_leads"] if policy_id == FIT_POLICY_ID else suppliers["leads"], "source_refs": refs,
            "supplier_search_status": suppliers["status"], "supplier_leads_checked": suppliers["checked"],
            "supplier_effort_satisfied": suppliers["effort_satisfied"], "supplier_effort": deepcopy(suppliers["effort"]),
            "supplier_search_check_ids": suppliers["search_check_ids"],
            "supplier_leads_found": len(suppliers["all_leads"]), "supplier_decisions": suppliers["excluded"],
            "retail_price_observations": raw_retail, "supplier_price_observations": suppliers["raw_prices"],
            "criteria": deepcopy(product["criteria"]), "compliance_note": product["compliance_note"],
            "writing_facts": writing_facts,
            **({"fit_description_contract": assessment["fit_description_contract"]} if fit_context else {}),
            **({field: deepcopy(fit[field]) for field in ("fit_policy_id", "criterion_ids", "kpi_total", "kpi_matches", "fit_tier")}
               if fit and policy_id == FIT_POLICY_ID else {}),
            **({"brand_binding": deepcopy(product["brand_binding"])} if fit and policy_id == FIT_POLICY_ID else {}),
            **(opportunity_fields(evaluate_opportunity(fit, product["market"]))
               if fit and policy_id == FIT_POLICY_ID else {}),
            "market_review_ids": sorted(set(current_review_by_semantic.values()) |
                {row["review_id"] for row in product["market"].get("source_support", [])}), "trends_evidence_ids": sorted(set(trend_ids)),
            "supplier_assessment_ids": suppliers["assessment_ids"],
            "description": deepcopy(state["concepts"][concept_id]["description"]),
            "usable_until": min(expiries).isoformat() if expiries else None, "image_path": photo["path"] if photo else None,
            **({"image_provenance": photo} if photo else {}), "image_candidates": image_candidates,
            **({"source_route_contract": SOURCE_ROUTE_CONTRACT, "source_route_checks": source_routes,
                "supplier_terms_display_version": SUPPLIER_TERMS_DISPLAY_VERSION}
               if source_routes is not None else {})}


def _freeze_fit_references(state, assessment):
    from .frozen_records import freeze_original_json
    proof = assessment["functional_proof"]
    records = {table: {} for table in ("source_checks", "observations", "captures", "associations", "descriptions")}
    scoped = {}
    records["source_checks"][proof["definition_id"]] = freeze_original_json(state["source_checks"][proof["definition_id"]])
    for entries in proof["examples"].values():
        for entry in entries:
            if "example_review_id" in entry:
                review_id = entry["example_review_id"]
                review = state["source_checks"][review_id]
                from .functional_example_review import validate_example_review
                validate_example_review(review_id, review)
                if review["record_hash"] != entry.get("example_review_hash"):
                    raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
                records["source_checks"][review_id] = freeze_original_json(review)
            for table, rid in (("associations", entry["child_id"]), ("descriptions", entry["semantic_id"])):
                records[table][rid] = freeze_original_json(state[table][rid])
            for ref in entry["observation_hashes"]:
                records["observations"][ref] = freeze_original_json(state["observations"][ref])
    for entries in proof.get("scoped_facts", {}).values():
        for entry in entries:
            rid = entry["scoped_evidence_id"]
            row = state["source_checks"][rid]
            scoped[rid] = freeze_original_json(row)
            for ref in row["references"]:
                records.setdefault(ref["table"], {})[ref["id"]] = freeze_original_json(state[ref["table"]][ref["id"]])
    return {"scoped_records": scoped, "records": records}


FUNCTIONAL_SUPPLIER_CONTRACT = "functional-supplier-context-v1"
_FUNCTIONAL_SUPPLIER_FIELDS = ("suppliers", "supplier_limitation", "supplier_search_status", "supplier_leads_checked",
    "supplier_leads_found", "supplier_decisions", "supplier_effort_satisfied", "supplier_effort",
    "supplier_search_check_ids", "supplier_assessment_ids")


def _functional_supplier_projection(suppliers):
    """Retain dated company leads, never inherit new-type commercial promises."""
    leads = deepcopy(suppliers["all_leads"])
    limitation = "This dated supplier lead still needs matching to this product type."
    for lead in leads:
        lead.update(relationship="unclear", type_scope_limitation=limitation,
            relevance={"relationship": "unclear", "reason": limitation, "references": []},
            single_piece_dispatch="not_stated", min_order=None, subscription="not_stated",
            ecommerce_support="Not stated", supply_arrangement=limitation, reason=limitation)
        lead["terms"] = [{"field": term["field"], "value": "Not stated", "basis": "not_stated",
            "references": [], "quotes": [], "reason": limitation} for term in lead["terms"]]
    return {"suppliers": leads,
        "supplier_limitation": "These dated supplier leads still need matching to this product type." if leads
                               else "Supplier search did not produce any usable results.",
        "supplier_search_status": suppliers["status"], "supplier_leads_checked": suppliers["checked"],
        "supplier_leads_found": len(leads), "supplier_decisions": deepcopy(suppliers["excluded"]),
        "supplier_effort_satisfied": suppliers["effort_satisfied"], "supplier_effort": deepcopy(suppliers["effort"]),
        "supplier_search_check_ids": suppliers["search_check_ids"], "supplier_assessment_ids": suppliers["assessment_ids"]}


def _freeze_functional_suppliers(state, suppliers, concept_id, description, as_of):
    from .frozen_records import freeze_original_json
    records = {"supplier_assessments": {rid: state["supplier_assessments"][rid] for rid in suppliers["assessment_ids"]},
               "source_checks": {rid: state["source_checks"][rid] for rid in suppliers["search_check_ids"]}}
    # Reconstruct only the selected latest records. Irrelevant old history is
    # not frozen into each packet, and its old exclusion prose is not display.
    selected = _suppliers(records["supplier_assessments"].items(), concept_id, description, as_of,
                          records["source_checks"].items())
    return {"contract": FUNCTIONAL_SUPPLIER_CONTRACT, "as_of": as_of,
            "records": {table: {rid: freeze_original_json(value) for rid, value in values.items()}
                        for table, values in records.items()}}, selected


def _validate_frozen_functional_suppliers(product):
    from .frozen_records import read_original_json
    proof = product["functional_supplier_proof"]
    from .report_supplier_facts import CONTRACT as SCOPED_CONTRACT, validate_frozen_functional_supplier_terms
    if proof.get("contract") == SCOPED_CONTRACT:
        legacy_proof = proof["legacy_context"]
        legacy_records = {table: {rid: read_original_json(value) for rid, value in values.items()}
                          for table, values in legacy_proof["records"].items()}
        legacy = _suppliers(legacy_records["supplier_assessments"].items(), product["concept_id"],
            product["description"], product["example_as_of"], legacy_records["source_checks"].items())
        original_product = dict(product, functional_supplier_proof=legacy_proof,
                                **_functional_supplier_projection(legacy))
        legacy = _validate_frozen_functional_suppliers(original_product)
        scoped = validate_frozen_functional_supplier_terms(product)
        expected, combined = _joined_functional_suppliers(legacy, scoped, as_of=product["example_as_of"])
        if expected != {field: product[field] for field in _FUNCTIONAL_SUPPLIER_FIELDS}:
            raise SafetyError("REPORT_SCOPED_SUPPLIER_DISPLAY_CHANGED")
        return combined
    if (set(proof) != {"contract", "as_of", "records"} or proof["contract"] != FUNCTIONAL_SUPPLIER_CONTRACT
            or proof["as_of"] != product["example_as_of"] or set(proof["records"]) != {"supplier_assessments", "source_checks"}):
        raise SafetyError("REPORT_FUNCTIONAL_SUPPLIER_PROOF_INVALID")
    records = {table: {rid: read_original_json(value) for rid, value in values.items()}
               for table, values in proof["records"].items()}
    for table, values in records.items():
        for rid, original in values.items():
            if "source_revisions" in product and product["source_revisions"].get(table, {}).get(rid) != digest(original):
                raise SafetyError("REPORT_FUNCTIONAL_SUPPLIER_ORIGINAL_CHANGED")
    suppliers = _suppliers(records["supplier_assessments"].items(), product["concept_id"], product["description"],
                          proof["as_of"], records["source_checks"].items())
    if (set(records["supplier_assessments"]) != set(suppliers["assessment_ids"])
            or set(records["source_checks"]) != set(suppliers["search_check_ids"])):
        raise SafetyError("REPORT_FUNCTIONAL_SUPPLIER_PROOF_INVALID")
    expected = _functional_supplier_projection(suppliers)
    if expected != {field: product[field] for field in _FUNCTIONAL_SUPPLIER_FIELDS}:
        raise SafetyError("REPORT_FUNCTIONAL_SUPPLIER_DISPLAY_CHANGED")
    return suppliers


def _joined_functional_suppliers(legacy, scoped, *, as_of):
    """Keep literal offer groups separate from legacy dated-company context."""
    display = _functional_supplier_projection(legacy)
    display["suppliers"] = deepcopy(scoped["groups"]) + display["suppliers"]
    display["supplier_leads_found"] = len(display["suppliers"])
    display["supplier_leads_checked"] += scoped["coverage"]["selected_groups"]
    actual = {(ref["id"], ref["hash"]): deepcopy(ref) for ref in display["supplier_effort"]["actual_attempt_refs"]}
    checks = set(display["supplier_effort"]["check_ids"])
    for rid, row in scoped["state"]["source_checks"].items():
        if row.get("kind") != "supplier-effort-v1" or timestamp(row["at"]) > timestamp(as_of):
            continue
        checks.add(rid)
        for ref in row["receipt_refs"]:
            if (ref["proof"] != "none" and timestamp(ref["attempt_started_at"]) <= timestamp(as_of)
                    and timestamp(ref["checked_at"]) <= timestamp(as_of)):
                actual[(ref["id"], ref["hash"])] = dict(deepcopy(ref), check_id=rid, pass_id=row["pass_id"])
    effort = display["supplier_effort"]
    effort.update(check_ids=sorted(checks), actual_attempt_refs=sorted(actual.values(),
        key=lambda r: (timestamp(r["attempt_started_at"]), r["id"], r["hash"])),
        last_attempt_at=max((r["attempt_started_at"] for r in actual.values()), key=timestamp, default=None),
        effort_satisfied=bool(actual), usable_lead_count=display["supplier_leads_found"],
        scoped_interpretation_ids=deepcopy(scoped["scoped"]["assessment_ids"]))
    if display["supplier_leads_found"]:
        effort["internal_outcome"] = "usable_leads"
    display["supplier_effort_satisfied"] = display["supplier_effort_satisfied"] or bool(actual)
    if scoped["scoped"]["application_ids"]:
        display["supplier_search_status"] = "complete"
    display["supplier_search_check_ids"] = sorted(set(display["supplier_search_check_ids"]) | checks)
    display["supplier_limitation"] = ("Company information and each offer's own terms are shown separately."
        if scoped["groups"] else display["supplier_limitation"])
    combined = dict(legacy, leads=display["suppliers"], all_leads=display["suppliers"],
        source_refs=legacy["source_refs"] + scoped["source_refs"],
        effort=effort, effort_satisfied=display["supplier_effort_satisfied"],
        search_check_ids=display["supplier_search_check_ids"], status=display["supplier_search_status"])
    return display, combined


def _functional_source_refs(records, matches, supplier_refs):
    refs = []
    for ref, match in sorted(matches.items()):
        if ref in records["observations"]:
            observation = records["observations"][ref]
            url, at, source = observation["facts"].get("url"), observation["observed_at"], observation["source"]
        else:
            metadata = records["trends_evidence"][ref]["parsed"]["metadata"]
            url, at, source = metadata.get("source_reference"), metadata["collected_at"], "google-trends"
        refs.append({"id": ref, "url": _url(url), "source": source, "observed_at": at,
            "relationship": match["relationship"], "relevance_reason": match["reason"]})
    refs.extend(supplier_refs)
    return list({row["id"]: row for row in refs}.values())


def validate_frozen_functional_fit(product):
    """Verify new fit inputs/projections without mistaking them for source IDs."""
    from .fit_description import FUNCTIONAL_CONTRACT, FUNCTIONAL_INFERENCE_KIND, functional_inference_context
    from .functional_types import (MEANING_FIELDS, PHYSICAL_FACETS, definition_meaning_hash,
                                   _child_description, _verified_definition, _legacy_definition)
    from .research_scope import _verified, _record_id, _field_values, _source_context, _evidence_contract
    from .research_records import SCOPED_EVIDENCE_KIND
    from .frozen_records import read_original_json
    try:
        item = read_original_json(product["functional_fit_input"], expected_hash=product["functional_fit_input_hash"])
        proof = product["functional_fit_proof"]
        assessment = read_original_json(product["functional_fit_assessment"])
        if (assessment.get("record_hash") != digest({key: value for key, value in assessment.items() if key != "record_hash"})
                or assessment.get("contract_version") != FUNCTIONAL_CONTRACT or assessment.get("fit_policy_id") != FIT_POLICY_ID
                or assessment.get("input") != item or assessment.get("input_hash") != product["functional_fit_input_hash"]
                or assessment.get("functional_proof") != proof or assessment.get("functional_proof_hash") != product["functional_fit_proof_hash"]
                or assessment.get("result", {}).get("criteria") != product["criteria"]
                or assessment.get("result", {}).get("compliance_note") != product["compliance_note"]
                or "source_revisions" in product and product["source_revisions"].get("assessments", {}).get(product["concept_id"]) != digest(assessment)):
            raise SafetyError("REPORT_FUNCTIONAL_FIT_ASSESSMENT_CHANGED")
        if (digest(proof) != product["functional_fit_proof_hash"] or item["id"] != product["concept_id"]
                or item["functional_contract"] != FUNCTIONAL_CONTRACT
                or item["meaning_hash"] != product["functional_definition"]["meaning_hash"]
                or item["description"] != product["fit_description"]
                or product["fit_description_contract"] != FUNCTIONAL_INFERENCE_KIND
                or definition_meaning_hash(dict({field: item["description"][field] for field in MEANING_FIELDS},
                                                report_title="Functional type")) != item["meaning_hash"]):
            raise SafetyError("REPORT_FUNCTIONAL_FIT_INPUT_INVALID")
        functional_inference_context(item["description"])
        references = product["functional_fit_reference_proofs"]
        if set(references) != {"scoped_records", "records"}:
            raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
        state = {table: {rid: read_original_json(value) for rid, value in values.items()}
                 for table, values in references["records"].items()}
        definition = state["source_checks"][proof["definition_id"]]
        _verified_definition(definition)
        selected_definition = product["functional_definition_proof"]["record"]
        if selected_definition.get("contract") == "frozen-original-json-v1":
            selected_definition = read_original_json(selected_definition)
        _verified_definition(selected_definition)
        if (selected_definition["concept_id"] != product["concept_id"]
                or selected_definition["meaning_hash"] != item["meaning_hash"]):
            raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
        if definition["record_hash"] != proof["definition_record_hash"] or definition["meaning_hash"] != item["meaning_hash"]:
            raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
        if timestamp(definition["reviewed_at"]) > timestamp(product["example_as_of"]):
            raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
        features, audiences = set(), set()
        for ref, entries in proof["examples"].items():
            for entry in entries:
                association = state["associations"][entry["child_id"]]
                description = _child_description(state, entry["child_id"])
                if digest(association) != entry["association_hash"] or digest(description) != entry["description_hash"]:
                    raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
                if (association.get("concept_id") != product["concept_id"] or association.get("status") != "resolved"
                        or association.get("semantic_id") != entry["semantic_id"]):
                    raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
                judgment = definition["example_decisions"].get(entry["child_id"])
                if "example_review_id" in entry or "example_review_hash" in entry:
                    from .functional_example_review import validate_example_review
                    review_id = entry["example_review_id"]
                    review = state["source_checks"][review_id]
                    validate_example_review(review_id, review)
                    bound = review["binding"]["children"].get(entry["child_id"])
                    decisions = [row for row in review["result"]["decisions"]
                                 if row["child_id"] == entry["child_id"]]
                    selected_judgment = selected_definition["example_decisions"].get(entry["child_id"])
                    if (review["record_hash"] != entry["example_review_hash"]
                            or review["concept_id"] != product["concept_id"]
                            or review["meaning_hash"] != item["meaning_hash"]
                            or timestamp(review["reviewed_at"]) > timestamp(product["example_as_of"])
                            or not bound or len(decisions) != 1 or decisions[0]["decision"] != "same"
                            or bound["semantic_id"] != entry["semantic_id"]
                            or bound["association_hash"] != entry["association_hash"]
                            or bound["description_hash"] != entry["description_hash"]
                            or entry["observation_hashes"].get(bound["observation_id"]) != bound["observation_hash"]
                            or (selected_judgment is not None
                                and selected_judgment.get("association_hash") == entry["association_hash"]
                                and selected_judgment.get("description_hash") == entry["description_hash"])):
                        raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
                elif judgment is not None:
                    if (judgment["decision"] != "same" or judgment["association_hash"] != entry["association_hash"]
                            or judgment["description_hash"] != entry["description_hash"]):
                        raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
                elif definition_meaning_hash(_legacy_definition({"description": product["description"]})) != item["meaning_hash"]:
                    raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
                projected = {field: deepcopy(description.get(field)) for field in ("function", "mechanism", "features", "audience")}
                if key("functional-role-example", [product["concept_id"], item["meaning_hash"], projected]) != ref:
                    raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
                for obs_id, expected in entry["observation_hashes"].items():
                    original = state["observations"][obs_id]
                    if (digest(original) != expected or original["semantic_id"] != entry["semantic_id"]
                            or timestamp(original["observed_at"]) > timestamp(product["example_as_of"])):
                        raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
                features.update(projected.get("features") or [])
                if projected.get("audience"):
                    audiences.add(projected["audience"])
        if sorted(features) != item["description"]["example_features"] or sorted(audiences) != item["description"]["example_audiences"]:
            raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID")
        scoped = {rid: read_original_json(value) for rid, value in references["scoped_records"].items()}
        supplied = {row["id"]: row for row in item["observations"]}
        if set(supplied) != set(proof.get("scoped_facts", {})) or len(supplied) != len(item["observations"]):
            raise SafetyError("REPORT_FUNCTIONAL_FIT_REFERENCE_INVALID")
        if set(scoped) != {fact["scoped_evidence_id"] for values in proof.get("scoped_facts", {}).values() for fact in values}:
            raise SafetyError("REPORT_FUNCTIONAL_FIT_REFERENCE_INVALID")
        for ref, facts in proof.get("scoped_facts", {}).items():
            source_text = supplied[ref]["source_text"]
            if key("functional-fit-fact", [product["concept_id"], item["meaning_hash"], source_text]) != ref:
                raise SafetyError("REPORT_FUNCTIONAL_FIT_REFERENCE_INVALID")
            for fact in facts:
                row = scoped[fact["scoped_evidence_id"]]
                _verified(row, SCOPED_EVIDENCE_KIND)
                if (fact["scoped_evidence_id"] != _record_id(row) or row["record_hash"] != fact["scoped_evidence_hash"]
                        or row["subject"]["definition_hash"] != item["meaning_hash"]
                        or row["concept_id"] != product["concept_id"]
                        or row["scope"] != source_text["scope"] or row["limitations"] != source_text["limitations"]
                        or row["scope"] not in {"narrow_type", "example_offer", "supplier_offer"}
                        or row["admissibility"] != "accepted"
                        or row["admissibility"] != _evidence_contract(row["scope"], row["relationship"], row["support"])
                        or timestamp(row["reviewed_at"]) > timestamp(product["example_as_of"])
                        or not fact["path"] or PHYSICAL_FACETS.get(fact["path"][-1]) != source_text["field"]):
                    raise SafetyError("REPORT_FUNCTIONAL_FIT_REFERENCE_INVALID")
                for original_ref in row["references"]:
                    if digest(state[original_ref["table"]][original_ref["id"]]) != original_ref["hash"]:
                        raise SafetyError("REPORT_FUNCTIONAL_FIT_REFERENCE_INVALID")
                contexts = [_source_context(value["table"], value["id"], state[value["table"]][value["id"]]) for value in row["references"]]
                if contexts != row["source_contexts"] or any(timestamp(value["observed_at"]) > timestamp(row["reviewed_at"]) for value in contexts):
                    raise SafetyError("REPORT_FUNCTIONAL_FIT_REFERENCE_INVALID")
                selected = _field_values(state, [{"reference": fact["reference"], "path": fact["path"]}], row["references"])[0]
                if selected["value"] != source_text["value"] or selected["hash"] != fact["value_hash"]:
                    raise SafetyError("REPORT_FUNCTIONAL_FIT_REFERENCE_INVALID")
        if any(not set(row["references"]) <= set(supplied) for row in product["criteria"]):
            raise SafetyError("REPORT_FUNCTIONAL_FIT_REFERENCE_INVALID")
        fit = evaluate_fit(product["criteria"], FIT_POLICY_ID, description=item["description"],
                           description_contract=FUNCTIONAL_INFERENCE_KIND)
        for field in ("fit_policy_id", "criterion_ids", "kpi_total", "kpi_matches", "fit_tier"):
            if product.get(field) != fit[field]:
                raise SafetyError("REPORT_FUNCTIONAL_FIT_COUNTS_INVALID")
        expected_writing = [{"text": row["reason"], "basis": row["basis"], "references": deepcopy(row["references"]),
            **({"description_inference": deepcopy(row["description_inference"])} if row.get("description_inference") else {})}
            for row in product["criteria"] if row["state"] == "Meets" and row["basis"] in ("observed", "inferred")][:6]
        if product["writing_facts"] != expected_writing or any(product[field] != [] for field in
                ("prices", "retail_price_observations", "supplier_price_observations")):
            raise SafetyError("REPORT_FUNCTIONAL_DISPLAY_FACTS_CHANGED")
        suppliers = _validate_frozen_functional_suppliers(product)
        market_proof = product["functional_market_proof"]
        market_records = {table: {rid: read_original_json(value) for rid, value in values.items()}
                          for table, values in market_proof["records"].items()}
        expected_refs = _functional_source_refs(market_records, market_proof["matches"], suppliers["source_refs"])
        if product["source_refs"] != expected_refs:
            raise SafetyError("REPORT_FUNCTIONAL_SOURCE_DISPLAY_CHANGED")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError("REPORT_FUNCTIONAL_FIT_PROOF_INVALID") from None
    return product


def _functional_facts(state, product, assessment, suppliers, as_of, route_coverage):
    from .report_examples import (freeze_functional_examples, functional_image_candidates,
                                  freeze_functional_photo, validate_frozen_functional_photo)
    from .frozen_records import freeze_original_json
    from .market_validation import functional_market_context, functional_priority_support, freeze_functional_market
    from .fit_description import FUNCTIONAL_INFERENCE_KIND
    cid = product["id"]
    block = freeze_functional_examples(state, cid, as_of=as_of)
    context = functional_market_context(state, cid, as_of=as_of)
    fit = evaluate_fit(product["criteria"], FIT_POLICY_ID, description=assessment["input"]["description"],
                       description_contract=FUNCTIONAL_INFERENCE_KIND)
    support = functional_priority_support(state, context, fit, as_of=as_of)
    supplier_proof, suppliers = _freeze_functional_suppliers(state, suppliers, cid, state["concepts"][cid]["description"], as_of)
    related = {}
    for ref, match in context["matches"].items():
        if ref in state["observations"]:
            observation = state["observations"][ref]
            related[ref] = observation
    writing = [{"text": row["reason"], "basis": row["basis"], "references": deepcopy(row["references"]),
                **({"description_inference": deepcopy(row["description_inference"])} if row.get("description_inference") else {})}
               for row in product["criteria"] if row["state"] == "Meets" and row["basis"] in ("observed", "inferred")][:6]
    route_product = dict(product, functional_market_contract=context["contract"], functional_market_context=context)
    routes = source_route_facts(state, cid, route_coverage or [], route_product, related, as_of)
    for row in routes:
        row["url"] = _url(row["url"])
    supplier_display = _functional_supplier_projection(suppliers)
    supplier_block = {"functional_supplier_proof": supplier_proof,
                      "supplier_scoped_terms": None, "supplier_scope_coverage": None}
    from .supplier_terms import APPLICATION_KIND
    if any(row.get("kind") == APPLICATION_KIND and row.get("binding", {}).get("concept_id") == cid
           for row in state["source_checks"].values()):
        from .report_supplier_facts import freeze_functional_supplier_terms, validate_frozen_functional_supplier_terms
        supplier_block = freeze_functional_supplier_terms(state, cid, as_of=as_of, legacy_context=supplier_proof)
        scoped = validate_frozen_functional_supplier_terms(dict(supplier_block, concept_id=cid, example_as_of=as_of))
        supplier_display, suppliers = _joined_functional_suppliers(suppliers, scoped, as_of=as_of)
    refs = _functional_source_refs(state, context["matches"], suppliers["source_refs"])
    image_candidates = functional_image_candidates(state, context)
    from .product_images import current_public_image
    photo = current_public_image(state, cid, image_candidates, as_of=as_of, require_report_quality=True)
    from .report_source_facts import freeze_functional_route_proof, validate_functional_route_proof
    from .live_outputs import SUPPLIER_TERMS_DISPLAY_VERSION
    fact = {"concept_id": cid, "title": block["functional_definition"]["report_title"],
        "category": product["category"], "description": deepcopy(state["concepts"][cid]["description"]),
        "criteria": deepcopy(product["criteria"]), "compliance_note": product["compliance_note"],
        "writing_facts": writing, "market_label": product["market"]["source_label"],
        "market_checks": [{"source": source, "state": "Available" if signal["available"] else "Insufficient",
            "detail": signal["reason"], "evidence_ids": signal["used_ids"], "url": None}
            for source, signal in context["market"]["sources"].items()],
        "source_refs": refs, "prices": [], "retail_price_observations": [], "supplier_price_observations": [],
        **supplier_display, **supplier_block,
        "market_review_ids": [], "trends_evidence_ids": sorted(ref for ref in context["matches"] if ref in state["trends_evidence"]),
        "usable_until": support["support_valid_until"], "image_path": photo["path"] if photo else None,
        **({"image_provenance": photo} if photo else {}), "image_candidates": image_candidates,
        "functional_photo_proof": freeze_functional_photo(state, photo),
        "brand_binding": deepcopy(product["brand_binding"]),
        "fit_description_contract": FUNCTIONAL_INFERENCE_KIND, "fit_description": deepcopy(assessment["input"]["description"]),
        "functional_fit_input": freeze_original_json(assessment["input"]), "functional_fit_input_hash": assessment["input_hash"],
        "functional_fit_assessment": freeze_original_json(assessment),
        "functional_fit_proof": deepcopy(assessment["functional_proof"]), "functional_fit_proof_hash": assessment["functional_proof_hash"],
        "functional_fit_reference_proofs": _freeze_fit_references(state, assessment),
        "functional_market_contract": context["contract"], "functional_market_proof": freeze_functional_market(state, context, as_of=as_of),
        "source_route_contract": FUNCTIONAL_SOURCE_ROUTE_CONTRACT, "source_route_checks": routes,
        "functional_route_proof": freeze_functional_route_proof(state, cid, as_of=as_of),
        "supplier_terms_display_version": SUPPLIER_TERMS_DISPLAY_VERSION, **block,
        **{field: deepcopy(fit[field]) for field in ("fit_policy_id", "criterion_ids", "kpi_total", "kpi_matches", "fit_tier")},
        **opportunity_fields(support["opportunity"]),
        **{field: support[field] for field in ("priority_support", "priority_support_refs", "support_valid_until")}}
    validate_frozen_functional_fit(fact)
    from .market_validation import validate_frozen_functional_market
    validate_frozen_functional_market(fact)
    validate_frozen_functional_photo(fact)
    validate_functional_route_proof(fact)
    return fact


def _source_revisions(state, concept_id, fact, association_rows, by_concept, by_semantic):
    observation_ids = {ref["id"] for ref in fact["source_refs"] if ref["id"] in state["observations"]}
    for semantic_id in by_concept.get(concept_id, set()):
        # This is the original immutable observation used by the current E input.
        observation_ids.add(next(iter(by_semantic[semantic_id])))
    records = {"concepts": [concept_id], "assessments": [concept_id],
               "associations": [child_id for child_id, _ in association_rows],
               "descriptions": {row["semantic_id"] for _, row in association_rows},
               "observations": observation_ids, "market_reviews": fact["market_review_ids"],
               "supplier_assessments": fact["supplier_assessment_ids"], "trends_evidence": fact["trends_evidence_ids"],
               "source_checks": fact["supplier_search_check_ids"]}
    if fact.get("image_provenance"):
        records["source_checks"] = list(records["source_checks"]) + [fact["image_provenance"]["id"]]
    if fact.get("recommendation_contract") == "functional-type-examples-v1":
        records["source_checks"] = list(records["source_checks"]) + [fact["functional_definition"]["definition_id"]] + list(fact["example_bindings"])
        records["source_checks"].extend(fact.get("example_review_proofs", {}))
        from .frozen_records import read_original_json
        for rid, frozen in fact["functional_market_proof"]["scoped_records"].items():
            records["source_checks"].append(rid)
        for table, values in fact["functional_market_proof"]["records"].items():
            records[table] = list(records.get(table, [])) + list(values)
        for rid in fact["functional_fit_reference_proofs"]["scoped_records"]:
            records["source_checks"].append(rid)
        for table, values in fact["functional_fit_reference_proofs"]["records"].items():
            records[table] = list(records.get(table, [])) + list(values)
        for table, values in fact["functional_photo_proof"]["records"].items():
            records[table] = list(records.get(table, [])) + list(values)
        for table, values in fact["functional_route_proof"]["records"].items():
            records[table] = list(records.get(table, [])) + list(values)
        for table, values in fact["functional_supplier_proof"]["records"].items():
            records[table] = list(records.get(table, [])) + list(values)
    if fact.get("source_route_contract") in SOURCE_ROUTE_CONTRACTS:
        dependencies, _ = route_dependencies(state, concept_id,
            functional=fact.get("source_route_contract") == FUNCTIONAL_SOURCE_ROUTE_CONTRACT)
        for table, values in dependencies.items():
            records[table] = list(records.get(table, [])) + list(values)
        for check in fact["source_route_checks"]:
            for ref in check["evidence_ids"] + check["receipt_ids"]:
                for table in ("source_checks", "observations", "captures", "trends_evidence"):
                    if ref in state.get(table, {}):
                        records[table] = list(records.get(table, [])) + [ref]
        for observation_id in records["observations"]:
            capture_id = state["observations"][observation_id].get("capture_id")
            if capture_id in state.get("captures", {}):
                records["captures"] = list(records.get("captures", [])) + [capture_id]
    binding = brand_binding(state, "concept", concept_id, state["concepts"][concept_id]["description"])
    if binding.get("review_id") in state.get("brand_reviews", {}):
        records["brand_reviews"] = [binding["review_id"]]
    return {table: {record_id: digest(state[table][record_id]) for record_id in sorted(set(ids))}
            for table, ids in records.items() if ids}


def selection_scope(state, concept_id, *, source_route_contract=None):
    """Current concept-scoped factual dependencies, including inserted reviews.

    This is independent of wall-clock time and the global memory head. Evidence
    collection dates remain factual inputs; job IDs, progress and completion
    timestamps do not. Existing-record source_revisions remain a separate check.
    """
    identifier(concept_id)
    concept = state.get("concepts", {}).get(concept_id)
    scope = {table: {} for table in ("concepts", "descriptions", "associations", "assessments",
                                    "market_reviews", "supplier_assessments", "observations", "trends_evidence", "source_checks")}
    if not concept:
        return scope
    from .research_records import iter_functional_definitions
    if any(row.get("concept_id") == concept_id for _, row in iter_functional_definitions(state)):
        from .functional_types import effective_functional_definition, current_example_memberships
        from .research_scope import validated_scoped_evidence
        meaning = effective_functional_definition(state, concept_id)
        scoped_heads = validated_scoped_evidence(state, concept_id)
        # Original replacements govern globally before subject filtering. An
        # unreviewed recapture of the same listing must invalidate an old vote
        # even when its new semantic description belongs to another type.
        identities = {(state["observations"][ref["id"]]["source"],
                       state["observations"][ref["id"]]["source_item"],
                       digest(state["observations"][ref["id"]].get("facts", {}).get("location"))
                       if state["observations"][ref["id"]]["source"] in ("blinkit", "zepto", "instamart") else None)
                      for row in scoped_heads for ref in row["references"] if ref["table"] == "observations"}
        replacements = {}
        for oid, observation in state["observations"].items():
            identity = (observation["source"], observation["source_item"],
                        digest(observation.get("facts", {}).get("location"))
                        if observation["source"] in ("blinkit", "zepto", "instamart") else None)
            if identity not in identities:
                continue
            order = timestamp(observation["observed_at"]), oid
            if identity not in replacements or order > replacements[identity][0]:
                replacements[identity] = order, oid, digest(observation)
        chart_fields = ("term", "geo", "time_range", "property", "category")
        chart_identities = {digest({field: state["trends_evidence"][ref["id"]].get("parsed", {}).get("metadata", {}).get(field)
                                   for field in chart_fields})
                            for row in scoped_heads for ref in row["references"] if ref["table"] == "trends_evidence"}
        chart_replacements = {}
        for tid, chart in state.get("trends_evidence", {}).items():
            metadata = chart.get("parsed", {}).get("metadata", {})
            identity = digest({field: metadata.get(field) for field in chart_fields})
            if identity not in chart_identities or chart.get("evidence_kind") == "related_queries" or not metadata.get("collected_at"):
                continue
            order = chart.get("parsed", {}).get("state") == "Usable", timestamp(metadata["collected_at"]), tid
            if identity not in chart_replacements or order > chart_replacements[identity][0]:
                chart_replacements[identity] = order, tid, digest(chart)
        scope["functional_type"] = {"meaning_hash": meaning["meaning_hash"], "presentation_hash": meaning["presentation_hash"],
            "definition_id": meaning["definition_id"],
            "memberships": {row["membership_id"]: digest(state["source_checks"][row["membership_id"]])
                            for row in current_example_memberships(state, concept_id)},
            "scoped_heads": {row["binding_key"]: row["record_hash"] for row in scoped_heads},
            "current_originals": {oid: original_hash for _, oid, original_hash in replacements.values()},
            "current_chart_originals": {tid: original_hash for _, tid, original_hash in chart_replacements.values()}}
        from .supplier_terms import APPLICATION_KIND
        scope["functional_type"]["supplier_applications"] = {rid: digest(row) for rid, row in state["source_checks"].items()
            if row.get("kind") == APPLICATION_KIND and row.get("binding", {}).get("concept_id") == concept_id}
    concept_scope = {field: concept.get(field) for field in ("description", "status", "identity_conflicted")}
    binding = brand_binding(state, "concept", concept_id, concept["description"])
    if binding.get("source") != "original":
        concept_scope["effective_brand"] = binding
    scope["concepts"][concept_id] = digest(concept_scope)
    effort_ids = set(current_supplier_effort(state, concept_id, concept["description"])["check_ids"])
    for check_id, check in state.get("source_checks", {}).items():
        if (check.get("source") == "supplier" and check.get("concept_id") == concept_id
                and (check.get("description_hash") == digest(concept["description"])
                     or check_id in effort_ids)):
            scope["source_checks"][check_id] = digest(check)
        if (check.get("kind") == "public_product_image" and check.get("state") == "usable"
                and check.get("concept_id") == concept_id
                and check.get("description_hash") == digest(concept["description"])):
            scope["source_checks"][check_id] = digest(check)
    semantic_ids = set()
    for child_id, association in state.get("associations", {}).items():
        if association.get("concept_id") != concept_id or association.get("status") != "resolved":
            continue
        semantic_id = association["semantic_id"]
        semantic_ids.add(semantic_id)
        scope["associations"][child_id] = digest({field: association.get(field) for field in
            ("semantic_id", "concept_id", "status", "reason", "method")})
        description = state["descriptions"].get(semantic_id)
        if description:
            scope["descriptions"][semantic_id] = digest(description.get("result"))
    assessment = state.get("assessments", {}).get(concept_id)
    if assessment:
        fields = ("input_hash", "result") + (("fit_policy_id", "review_id") if "fit_policy_id" in assessment else ())
        scope["assessments"][concept_id] = digest({field: assessment.get(field) for field in fields})
    from .market_validation import REVIEW_VERSION
    latest_reviews = {}
    for insertion, (review_id, review) in enumerate(state.get("market_reviews", {}).items()):
        if (review.get("concept_id") != concept_id or review.get("review_version") != REVIEW_VERSION
                or review.get("description_hash") != digest(concept["description"])):
            continue
        for row in review.get("relevance", []):
            observation = state["observations"].get(row["id"])
            if not observation:
                continue
            semantic_id = observation["semantic_id"]
            semantic_ids.add(semantic_id)
            # candidate_summary's stable sort retains event insertion order on
            # equal timestamps/input hashes; use the same latest-judgment tie.
            order = (timestamp(review["reviewed_at"]), review["input_hash"], insertion)
            if semantic_id not in latest_reviews or order > latest_reviews[semantic_id][0]:
                latest_reviews[semantic_id] = (order, review_id, review)
    scoped_reviews = {review_id: review for _, review_id, review in latest_reviews.values()}
    from .market_validation import SOURCE_SUPPORT_VERSION
    # Chart-only source interpretation has no marketplace semantic row. Its
    # opinion still needs to invalidate a current selection if it changes.
    support_by_source = {}
    for insertion, (review_id, review) in enumerate(state.get("market_reviews", {}).items()):
        if (review.get("concept_id") == concept_id and review.get("description_hash") == digest(concept["description"])
                and review.get("support_version") == SOURCE_SUPPORT_VERSION):
            for support in review.get("source_support", []):
                order = (timestamp(review["reviewed_at"]), review["input_hash"], insertion)
                source = support["source"]
                if source not in support_by_source or order > support_by_source[source][0]:
                    support_by_source[source] = order, review_id, review
    scoped_reviews.update({review_id: review for _, review_id, review in support_by_source.values()})
    for review_id, review in scoped_reviews.items():
        fields = ("concept_id", "review_version", "description_hash", "input_hash", "relevance")
        if "support_version" in review:
            fields += ("support_version", "source_support", "evidence_hashes", "scope_evidence_hashes")
        scope["market_reviews"][review_id] = digest({field: review.get(field) for field in fields})
    wanted, latest_suppliers = _supplier_scope(dict(concept["description"], concept_id=concept_id)), {}
    for record_id, record in state.get("supplier_assessments", {}).items():
        product = record.get("input", {}).get("product")
        if not isinstance(product, dict) or record.get("version") != SUPPLIER_VERSION or _supplier_scope(product) != wanted:
            continue
        lead_id = record["input"]["id"]
        order = (timestamp(record["checked_at"]), record_id)
        if lead_id not in latest_suppliers or order > latest_suppliers[lead_id][0]:
            latest_suppliers[lead_id] = (order, record_id, record)
    for _, record_id, record in latest_suppliers.values():
        supplier_facts = {field: record.get(field) for field in ("input_hash", "input", "version", "result")}
        if "source_dates" in record:
            supplier_facts["source_dates"] = record["source_dates"]
        scope["supplier_assessments"][record_id] = digest(supplier_facts)
    # Resolve source replacements before concept filtering: the same advert may
    # now describe another product, invalidating its old support here.
    newest = {}
    for observation_id, observation in state.get("observations", {}).items():
        source_identity = (observation["source"], observation["source_item"])
        at = timestamp(observation["observed_at"])
        if source_identity not in newest or at > newest[source_identity][0]:
            newest[source_identity] = (at, observation_id, observation)
    for _, observation_id, observation in newest.values():
        if observation["semantic_id"] in semantic_ids:
            scope["observations"][observation_id] = digest(observation)
    trends = [(trend_id, row) for trend_id, row in state.get("trends_evidence", {}).items()
              if row.get("concept_id") == concept_id and row.get("parsed", {}).get("metadata", {}).get("collected_at")
              and row.get("evidence_kind") != "related_queries"
              and trends_description_current(state, concept_id, row)]
    usable = [(trend_id, row) for trend_id, row in trends if row["parsed"].get("state") == "Usable"]
    candidates = usable or trends
    if candidates:
        trend_id, trend = max(candidates, key=lambda row: (timestamp(row[1]["parsed"]["metadata"]["collected_at"]), row[0]))
        scope["trends_evidence"][trend_id] = digest({"concept_id": concept_id, "parsed": trend["parsed"]})
        from .trends_related import current_related_support
        support = current_related_support(dict(id=trend_id, **trend),
            [dict(id=i, **row) for i, row in state["trends_evidence"].items()])
        if support is not None:
            scope["trends_evidence"][support["id"]] = digest(support)
    if source_route_contract is not None:
        if source_route_contract not in SOURCE_ROUTE_CONTRACTS:
            raise SafetyError("LIVE_SOURCE_ROUTE_CONTRACT_INVALID")
        functional = source_route_contract == FUNCTIONAL_SOURCE_ROUTE_CONTRACT
        dependencies, runs = route_dependencies(state, concept_id, functional=functional)
        scope["query_tasks"] = {task_id: digest(functional_task_projection(task, concept_id) if functional else task)
                                for task_id, task in dependencies["query_tasks"].items()}
        scope["source_checks"].update({ref: digest(record) for ref, record in dependencies["source_checks"].items()})
        scope["source_route_progress"] = {ref: digest(record) for ref, record in runs.items()}
    return {table: dict(sorted(rows.items())) for table, rows in scope.items()}


def _seen(state, client_id="default", identities=None):
    """Publication inheritance is directional, not source-fact equivalence."""
    from .functional_aliases import alias_projection
    from .functional_lineage import history_ids
    if identities is None:
        identities = alias_projection(state)
    published = {row["concept_id"] for row in state.get("published_history", {}).values()
                 if row.get("status") == "published" and row.get("client_id", "default") == client_id}
    return {cid for cid, identity in identities.items() if not identity.get("reason")
            and history_ids(cid, identities) & published}


def build_report_pool(memory, as_of=None, client_id="default", *, fit_policy_id=None, recommendation_contract=None):
    """Return saved eligible pool, immutable-fact inputs and concrete next work.

    product_facts contains the eligible pool's rows, not a selected issue: the
    caller selects matching concept IDs after allocation before building its
    dataset. facts_by_concept also retains examined-but-ineligible facts.
    One build reads one unchanging state, so its pure per-type readers reuse
    their answers within this call only (see read_memo); nothing is kept after.
    """
    from .read_memo import pure_read_scope
    with pure_read_scope():
        return _build_report_pool(memory, as_of, client_id, fit_policy_id=fit_policy_id,
                                  recommendation_contract=recommendation_contract)


def _build_report_pool(memory, as_of, client_id, *, fit_policy_id, recommendation_contract):
    as_of = as_of or utc_now()
    timestamp(as_of); identifier(client_id)
    if recommendation_contract not in (None, "functional-type-examples-v1"):
        raise SafetyError("REPORT_RECOMMENDATION_CONTRACT_INVALID")
    state = memory.snapshot()
    current_observations = latest_observations(state["observations"], as_of)
    by_concept, by_semantic = evidence_indexes(state)
    associations_by_concept, supplier_records, market_reviews = {}, {}, {}
    for record_id, record in state.get("supplier_assessments", {}).items():
        product = record.get("input", {}).get("product")
        if isinstance(product, dict) and isinstance(product.get("concept_id"), str):
            supplier_records.setdefault(product["concept_id"], []).append((record_id, record))
    for review_id, review in state.get("market_reviews", {}).items():
        if isinstance(review.get("concept_id"), str):
            market_reviews.setdefault(review["concept_id"], []).append((review_id, review))
    for child_id, association in state["associations"].items():
        if association.get("concept_id") is not None and association.get("status") == "resolved":
            associations_by_concept.setdefault(association["concept_id"], []).append((child_id, association))
    valid_concepts, assessments, decisions = {}, {}, []
    supplier_work, market_work, fact_work = [], [], []
    for concept_id, concept in sorted(state["concepts"].items()):
        try:
            code, reason = _identity(state, concept_id, concept, associations_by_concept.get(concept_id, []))
            if code:
                decisions.append({"concept_id": concept_id, "decision": "excluded" if code in ("out_of_scope", "established_brand") else "deferred",
                                  "codes": [code], "reasons": [reason]})
                if code == "identity_pending":
                    fact_work.append({"concept_id": concept_id, "code": code, "reason": reason, "blocking": True})
                continue
            valid_concepts[concept_id] = concept
            record, code, reason = _assessment(state, concept_id, concept, as_of, by_concept, by_semantic, fit_policy_id,
                                               recommendation_contract)
            if record:
                assessments[concept_id] = record
            else:
                fact_work.append({"concept_id": concept_id, "code": code, "reason": reason, "blocking": True})
        except (KeyError, TypeError, ValueError, SafetyError):
            valid_concepts.pop(concept_id, None)
            reason = "Saved product or interpretation inputs need correction; other products can continue."
            decisions.append({"concept_id": concept_id, "decision": "deferred", "codes": ["saved_facts_invalid"], "reasons": [reason]})
            fact_work.append({"concept_id": concept_id, "code": "saved_facts_invalid", "reason": reason, "blocking": True})
    frozen = dict(state, concepts=valid_concepts, assessments=assessments)
    summary = candidate_summary(SimpleNamespace(snapshot=lambda: frozen, evidence_type=memory.evidence_type), as_of=as_of)
    route_coverage = coverage_rows(memory, state,
        [concept_id for concept_id, assessment in assessments.items() if assessment.get("fit_policy_id") == FIT_POLICY_ID], as_of)
    from .functional_aliases import alias_projection
    material_identities = alias_projection(state)
    published = _seen(state, client_id, material_identities)
    eligible, facts, update_work = [], {}, []
    for product in sorted(summary["products"], key=lambda row: row["id"]):
        concept_id, codes, reasons = product["id"], [], []
        try:
            suppliers = _suppliers(supplier_records.get(concept_id, []), concept_id,
                                  valid_concepts[concept_id]["description"], as_of, state["source_checks"].items())
            from .fit_description import FUNCTIONAL_CONTRACT
            functional = bool(assessments.get(concept_id, {}).get("contract_version") == FUNCTIONAL_CONTRACT)
            fact = (_functional_facts(state, product, assessments[concept_id], suppliers, as_of, route_coverage.get(concept_id))
                    if functional else _facts(state, product, assessments.get(concept_id), suppliers, as_of,
                        current_observations, market_reviews.get(concept_id, []), route_coverage.get(concept_id)))
        except (KeyError, TypeError, ValueError, SafetyError) as error:
            reason = "Saved supplier or source facts need correction; other products can continue."
            diagnostic = str(error) if isinstance(error, SafetyError) else "SAVED_SUPPLIER_SOURCE_SHAPE_INVALID"
            decisions.append({"concept_id": concept_id, "decision": "deferred",
                              "codes": ["saved_supplier_or_source_facts_invalid"], "reasons": [reason], "diagnostic_code": diagnostic})
            fact_work.append({"concept_id": concept_id, "code": "saved_supplier_or_source_facts_invalid",
                              "reason": reason, "blocking": True, "diagnostic_code": diagnostic})
            continue
        facts[concept_id] = fact
        fact_work.extend(suppliers["notes"])
        if concept_id not in assessments:
            codes.append("checklist_review_required"); reasons.append("The current checklist still needs review.")
        else:
            fit = evaluate_fit(product["criteria"], assessments[concept_id].get("fit_policy_id", LEGACY_POLICY_ID),
                               **fit_description_context(assessments[concept_id], fact.get("fit_description", fact["description"])))
            if not fit["eligible"]:
                code = "fit_information_pending" if fit["reason"] == "fit_information_pending" and fit["fit_policy_id"] == FIT_POLICY_ID else "below_checklist_threshold"
                codes.append(code); reasons.append("Some fit points still need a useful judgment." if code == "fit_information_pending"
                    else "The supported checklist judgments are below the inclusion threshold.")
        opportunity = (evaluate_opportunity(fit, product["market"])
                       if concept_id in assessments and fit["fit_policy_id"] == FIT_POLICY_ID else None)
        if opportunity is not None:
            if not opportunity["eligible"] and fit["eligible"]:
                codes.append(opportunity["reason"]); reasons.append(product["market_follow_up"])
            # Eligibility and completion are separate. Remaining source work
            # stays visible even after a product can appear in a report; it
            # must not become an implicit second/third-source inclusion gate.
            available, positive = opportunity_counts(opportunity)
            if fit["eligible"] and concept_id not in published and available < opportunity_source_limit(opportunity):
                market_work.append({"concept_id": concept_id,
                                    "code": opportunity["reason"] if not opportunity["eligible"] else "market_completion_optional",
                                    "reason": product["market_follow_up"],
                                    "available_families": product["market"]["available_families"],
                                    "positive_families": product["market"]["positive_families"],
                                    "available_sources": available, "positive_sources": positive,
                                    "positive_signal_needed": False,
                                    "blocking": not opportunity["eligible"], **opportunity_fields(opportunity)})
        elif product["market"]["available_families"] < 2:
            codes.append("market_coverage_required"); reasons.append(product["market_follow_up"])
            if concept_id in assessments and fit["eligible"] and concept_id not in published:
                market_work.append({"concept_id": concept_id, "code": "market_coverage_required", "reason": product["market_follow_up"],
                                    "available_families": product["market"]["available_families"],
                                    "positive_families": product["market"]["positive_families"],
                                    "positive_signal_needed": product["market"]["positive_families"] == 0, "blocking": True})
        elif product["market"]["positive_families"] < 1:
            codes.append("weak_market_signal"); reasons.append("Two assessable source families exist, but neither supplies a positive signal.")
        if not suppliers["effort_satisfied"]:
            codes.append("supplier_search_required"); reasons.append("No accepted cursory supplier search is linked to this product yet.")
            if not codes[:-1] and concept_id not in published:
                supplier_work.append({"concept_id": concept_id, "code": "supplier_search_required", "reason": reasons[-1], "blocking": True,
                                      **(opportunity_fields(opportunity) if opportunity else {})})
        update = None
        snapshot = material_snapshot(fact, product["market"])
        if concept_id in published:
            try:
                # A parent's exposure prevents a false New label, but does not
                # turn its broad market/supplier facts into a child baseline.
                # Only actual equivalent-member publications are comparable.
                members = material_identities[concept_id]["member_ids"]
                update = review_material_update(state, fact, product["market"], client_id=client_id, as_of=as_of, member_ids=members)
                if update["decision"] == "review_needed":
                    prepared = prepare_material_update(state, fact, product["market"], client_id=client_id, as_of=as_of, member_ids=members)
                    if prepared["state"] == "prepared":
                        update_work.append(prepared)
            except (KeyError, TypeError, ValueError, SafetyError):
                update = {"accepted": False, "decision": "baseline_invalid", "changes": [],
                          "reason": "The saved published comparison facts need correction; other products can continue."}
            if not update["accepted"]:
                codes.append("useful_update_review_required"); reasons.append(update["reason"])
                if update["decision"] != "not_material":
                    fact_work.append({"concept_id": concept_id, "code": codes[-1], "reason": reasons[-1], "blocking": True,
                                      "changed_fields": [change["field_id"] for change in update["changes"]]})
        outcome = "excluded" if (set(codes) & {"below_checklist_threshold", "weak_market_signal"}
                                  or update is not None and update["decision"] == "not_material") else "deferred" if codes else "included"
        decisions.append({"concept_id": concept_id, "decision": outcome, "codes": codes,
                          "reasons": reasons or ["Current checklist and source support meet the agreed priority, and a supplier search was recorded."
                                                 if opportunity else "Accepted identity and checklist, sufficient positive market coverage, and a recorded supplier search."],
                          "supplier_leads_checked": suppliers["checked"], "supplier_leads_found": len(suppliers["all_leads"]),
                          **({"useful_update_decision": update["decision"]} if update is not None else {}),
                          **(opportunity_fields(opportunity) if opportunity else {})})
        if not codes:
            row = {"concept_id": concept_id, "category": product["category"], "identity_reviewed": True, "identity_conflicted": False,
                   "kpi_states": [criterion["state"] for criterion in product["criteria"]], "eligible": True,
                   "status": "Updated" if update is not None else "New",
                   "evidence_refs": [ref["id"] for ref in fact["source_refs"]],
                   "evidence_fingerprint": digest({"market": product["market"], "criteria": product["criteria"], "suppliers": suppliers}),
                   "usable_until": fact["usable_until"], "positive_families": product["market"]["positive_families"],
                   "available_families": product["market"]["available_families"],
                   "material_facts": snapshot, "material_facts_hash": digest(snapshot)}
            if assessments[concept_id].get("fit_policy_id") == FIT_POLICY_ID:
                row.update({field: deepcopy(fit[field]) for field in ("fit_policy_id", "criterion_ids", "kpi_total", "kpi_matches", "fit_tier")})
                row["brand_binding"] = deepcopy(product["brand_binding"])
                row.update(opportunity_fields(opportunity))
            if "fit_description_contract" in fact:
                row["fit_description_contract"] = fact["fit_description_contract"]
            if fact.get("source_route_contract") in SOURCE_ROUTE_CONTRACTS:
                row.update({field: deepcopy(fact[field]) for field in SOURCE_ROUTE_FIELDS})
                row["supplier_terms_display_version"] = fact["supplier_terms_display_version"]
                row["evidence_fingerprint"] = digest({"market": product["market"], "criteria": product["criteria"],
                    "suppliers": suppliers, **{field: fact[field] for field in SOURCE_ROUTE_FIELDS}})
            if fact.get("recommendation_contract") == "functional-type-examples-v1":
                from .report_examples import functional_report_fields
                fields = functional_report_fields(fact)
                row.update({field: deepcopy(fact[field]) for field in fields})
                row["evidence_fingerprint"] = digest({"legacy_facts": row["evidence_fingerprint"],
                    "functional_facts": {field: row[field] for field in fields}})
            row["source_revisions"] = _source_revisions(state, concept_id, fact,
                associations_by_concept.get(concept_id, []), by_concept, by_semantic)
            if fact.get("image_provenance"):
                row["image_provenance"] = deepcopy(fact["image_provenance"])
            if update is not None:
                baseline = update["baseline"]
                # Keep private audit content bounded: no nested old reservation
                # or duplicate whole current/baseline facts in future reports.
                review = {name: deepcopy(update[name]) for name in (
                    "accepted", "decision", "rule", "reason", "current_hash", "changes", "old_evidence_ids",
                    "new_evidence_ids", "practical_consequence", "supporting_lead_ids", "usable_until")}
                review["baseline"] = {name: baseline[name] for name in (
                    "record_id", "issue_id", "revision", "available_at", "history_hash", "reservation_hash",
                    "baseline_hash", "package_hash", "package_facts_hash", "selection_hash") if name in baseline}
                if update.get("review_id"):
                    review.update({name: deepcopy(update[name]) for name in ("review_id", "model_references", "changed_fields")})
                row.update(material_update_reviewed=True, update_reason=update["reason"], material_update_review=review)
                dates = [value for value in (row["usable_until"], review["usable_until"]) if value]
                row["usable_until"] = min(dates, key=timestamp) if dates else None
                row["source_revisions"]["published_history"] = {baseline["record_id"]: baseline["history_hash"]}
                if baseline["reservation_hash"] is not None:
                    row["source_revisions"].setdefault("issue_reservations", {})[baseline["issue_id"]] = baseline["reservation_hash"]
                if update.get("review_id"):
                    row["source_revisions"].setdefault("source_checks", {})[update["review_id"]] = digest(state["source_checks"][update["review_id"]])
            row["source_scope_hash"] = digest(selection_scope(state, concept_id,
                source_route_contract=fact.get("source_route_contract")))
            eligible.append(row)
    eligible.sort(key=lambda row: (*opportunity_sort_key(row), row["concept_id"]))
    market_work.sort(key=lambda row: (row.get("opportunity_priority") if row.get("opportunity_priority") is not None else 5,
                                      -row.get("positive_sources", row["positive_families"]), row["concept_id"]))
    supplier_work.sort(key=lambda row: (row.get("opportunity_priority") if row.get("opportunity_priority") is not None else 5,
                                        row["concept_id"]))
    source_decisions = [{"child_id": child_id, "semantic_id": row.get("semantic_id"),
                         "concept_id": row.get("concept_id"), "decision": row.get("status"),
                         "reason": row.get("reason"), "method": row.get("method"), "job_id": row.get("job_id")}
                        for child_id, row in sorted(state["associations"].items())]
    return {"schema_version": 1, "as_of": as_of, "client_id": client_id, "evidence_type": memory.evidence_type,
            "eligible": eligible, "product_facts": [facts[row["concept_id"]] for row in eligible], "facts_by_concept": facts,
            "decisions": sorted(decisions, key=lambda row: row["concept_id"]), "source_decisions": source_decisions,
            "needed_supplier_work": supplier_work,
            "needed_market_work": market_work, "needed_fact_work": fact_work, "needed_material_update_work": update_work,
            "client_report": False, "client_ready": False}
