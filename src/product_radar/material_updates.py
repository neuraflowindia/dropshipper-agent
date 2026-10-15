"""Published-fact comparison and bounded saved material-update interpretation.

Pure readers never launch work. The explicit ``run_material_updates`` writer
uses the existing E/Jobs owner and shared turn allowance, not a new collection
or publication lane. Dates, IDs, seller names and formatting are not updates.
"""

from copy import deepcopy
from datetime import timedelta
import re

from .discovery import normalise
from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now

VERSION = "material-facts-v1"
RULE = "new-single-piece-customer-dispatch-v1"
SCOPED_VERSION = "material-facts-v2"
BASELINE_CONTRACT = "published-material-baseline-v1"
REVIEW_KIND = "material-update-review-v2"
UPDATE_PROMPT_VERSION = "research-e-material-update-v1"
UPDATE_MODE = "material_update_review"
UPDATE_JOB_PROOF = "material-update-original-acceptance-v1"
SOURCES = ("meta", "amazon", "flipkart", "google-trends", "blinkit", "zepto", "instamart")


def _text(value):
    return normalise(value) if isinstance(value, str) else value


def material_snapshot(fact, market):
    """Keep comparable content, not evidence collection or display metadata."""
    if fact.get("recommendation_contract") == "functional-type-examples-v1":
        return scoped_material_snapshot(fact, market)
    families = {}
    for family, signal in sorted(market.get("families", {}).items()):
        families[family] = {name: deepcopy(signal[name]) for name in (
            "state", "available", "positive", "advertisers", "active_ads", "recent_mean", "prior_mean", "ratio",
            "previous_mean_bounds", "recent_mean_bounds", "ratio_bounds") if name in signal}
    prices = [{name: _text(row.get(name)) for name in (
        "kind", "currency", "amount", "unit", "quantity", "moq", "conditions", "relationship", "raw_displayed")}
              for row in fact.get("prices", [])]
    suppliers = []
    for row in fact.get("suppliers", []):
        suppliers.append({name: _text(row.get(name)) for name in (
            "single_piece_dispatch", "subscription", "min_order", "ecommerce_support", "relationship", "supply_arrangement")})
    # Duplicate equivalent ads/sellers/supplier pages cannot create a change.
    def unique(rows):
        values = {digest(row): row for row in rows}
        return [values[key] for key in sorted(values)]
    return {"version": VERSION, "concept_id": fact["concept_id"],
            "market": {"label": market.get("label"), "families": families},
            "criteria": sorted([{"id": row["id"], "state": row["state"], "basis": row["basis"]}
                                for row in fact.get("criteria", [])], key=lambda row: row["id"]),
            "prices": unique(prices), "suppliers": unique(suppliers)}


def _unique(rows):
    values = {digest(row): row for row in rows}
    return [values[key] for key in sorted(values)]


def _conditions(row):
    """A commercial condition, not its seller/date/URL or application identity."""
    return {name: _text(row.get(name)) for name in ("currency", "amount", "quoted_amount", "amount_range",
        "kind", "price_kind", "unit", "quantity", "quantity_condition", "qualifier", "moq", "conditions", "relationship")
        if name in row}


def _market_scope(context, source):
    chosen = {name: deepcopy(context[name]) for name in ("route", "location", "query")
              if context.get(name) is not None}
    request = context.get("request")
    if isinstance(request, dict):
        if source == "google-trends":
            # Its source context also holds capture metadata. Only the actual
            # query/window is comparison scope; fetched-at and file IDs are not.
            request = {field: deepcopy(request[field]) for field in
                       ("term", "geo", "time_range", "property", "category") if field in request}
        else:
            request = {field: deepcopy(value) for field, value in request.items()
                       if field not in {"collected_at", "source_reference", "prepared_at"}}
        if request:
            chosen["request"] = request
    return chosen


def _useful_market_scope(scope):
    # A route name alone is not a comparable search/list/location. Unknown
    # None scopes cannot become two matching pieces of evidence.
    request = scope.get("request", {})
    return bool(scope.get("query") or any(request.get(field) not in (None, "", [], {}) for field in
        ("term", "query", "q", "url", "department", "node", "node_id", "list", "list_url")))


def scoped_material_snapshot(fact, market):
    """Seven real sites and offer-local conditions; no seller/date-only changes.

    Context is projected from the same frozen original proof used by the report.
    Missing query/location/list context stays missing, never guessed as India.
    """
    from .frozen_records import read_original_json
    proof = fact.get("functional_market_proof", {})
    contexts = {source: [] for source in SOURCES}
    for wrapped in proof.get("scoped_records", {}).values():
        row = read_original_json(wrapped)
        source = "google-trends" if row.get("source") == "trends" else row.get("source")
        if source not in contexts or row.get("scope") != "narrow_type" or row.get("admissibility") != "accepted":
            continue
        for context in row.get("source_contexts", []):
            chosen = _market_scope(context, source)
            if chosen:
                contexts[source].append(chosen)
        # An authenticated Meta request can live in the original model proof.
        for capture in (row.get("job_proof") or {}).get("capture_contexts", {}).values():
            request = capture.get("context", {}).get("request")
            if request is not None:
                contexts[source].append(_market_scope({"route": row["route"], "request": request}, source))
    sources = {}
    for source in SOURCES:
        signal = market.get("sources", {}).get(source, {})
        metrics = [{"route": row["route"], "metrics": [{"name": metric["name"], "value": deepcopy(metric["value"])}
                    for metric in row.get("measurements", [])]}
                   for row in fact.get("source_route_checks", []) if row.get("source_role") == source]
        sources[source] = {"signal": {name: deepcopy(signal[name]) for name in
            ("available", "positive", "advertisers", "active_ads", "recent_mean", "prior_mean",
             "ratio", "previous_mean_bounds", "recent_mean_bounds", "ratio_bounds") if name in signal},
            "scopes": _unique(contexts[source]), "measurements": _unique(metrics)}
    scoped = fact.get("supplier_scoped_terms") or {}
    suppliers = []
    for row in scoped.get("all", []):
        terms = [{"field": term["field"], "value": _text(term["value"]), "basis": term["basis"],
                  "applicability": term.get("applicability"), "qualified": (
                      term.get("basis") in ("observed", "conflicting") and term.get("applicability") == "applies"
                      and row["scope_kind"] != "unclear" and (row["scope_kind"] != "company_workflow"
                          or row.get("workflow_ref") is not None)),
                  "exceptions": sorted(_text(q["excerpt"]) for q in term.get("exceptions", []))}
                 # Keep literal scoped terms, not date-sensitive derived
                 # inheritance. Company rows are preserved separately below;
                 # their expiry alone must not become changed commercial news.
                 for term in row.get("terms", [])]
        suppliers.append({"scope_kind": row["scope_kind"], "purchase_unit": _text(row.get("purchase_unit")),
            "relationship": row["relevance"]["relationship"], "terms": sorted(terms, key=lambda t: t["field"]),
            "prices": _unique([_conditions(p) for p in row.get("prices", [])]),
            "fees": _unique([_conditions(p) for p in row.get("fees", [])]),
            # MOQ/arrangement are already explicit literal terms. Their current
            # derived display fields may change merely as a company page ages.
            })
    meaning = fact.get("functional_definition", {}).get("meaning_hash")
    if not isinstance(meaning, str) or not re.fullmatch(r"[a-f0-9]{64}", meaning):
        raise SafetyError("MATERIAL_MEANING_INVALID")
    return {"version": SCOPED_VERSION, "concept_id": fact["concept_id"], "meaning_hash": meaning,
        "market": {"sources": sources},
        "criteria": sorted([{"id": r["id"], "state": r["state"], "basis": r["basis"]}
                            for r in fact.get("criteria", [])], key=lambda r: r["id"]),
        "prices": _unique([_conditions(p) for p in fact.get("prices", [])]), "suppliers": _unique(suppliers)}


def publication_baseline(issue, product, receipt):
    """Freeze facts only at proved client publication, never at reservation."""
    from .issue_allocation import _fingerprint, selection_fingerprint
    snapshot = product.get("material_facts")
    if snapshot is None:
        return None  # Old packets without comparable facts remain history.
    if (not isinstance(snapshot, dict) or snapshot.get("version") not in (VERSION, SCOPED_VERSION)
            or snapshot.get("concept_id") != product.get("concept_id")
            or product.get("material_facts_hash") != digest(snapshot)
            or issue.get("evidence_fingerprint") != _fingerprint(issue["products"])
            or issue.get("selection_hash") != selection_fingerprint(issue)
            or receipt.get("verified") is not True or receipt.get("authority") != "release_index"
            or any(receipt.get(name) != issue.get(name) for name in ("issue_id", "revision", "selection_hash"))):
        raise SafetyError("PUBLISHED_UPDATE_BASELINE_INVALID")
    package_hash = receipt.get("package_hash")
    facts_hash = receipt.get("facts_hash")
    if (not isinstance(package_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", package_hash)
            or facts_hash is not None and (not isinstance(facts_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", facts_hash))):
        raise SafetyError("PUBLISHED_UPDATE_BASELINE_INVALID")
    value = {"contract": BASELINE_CONTRACT, "client_id": issue["client_id"], "issue_id": issue["issue_id"],
        "revision": issue["revision"], "concept_id": product["concept_id"], "selection_hash": issue["selection_hash"],
        "package_hash": package_hash, "package_facts_hash": facts_hash,
        "product_hash": digest(product), "material_facts_hash": digest(snapshot), "snapshot": deepcopy(snapshot),
        "evidence_ids": sorted(set(product.get("evidence_refs", [])))}
    return dict(value, binding_hash=digest(value))


def _immutable_baseline(record):
    value = record.get("publication_baseline")
    if value is None:
        return None
    fields = {"contract", "client_id", "issue_id", "revision", "concept_id", "selection_hash", "package_hash",
        "package_facts_hash", "product_hash", "material_facts_hash", "snapshot", "evidence_ids", "binding_hash"}
    receipt = record.get("activation_receipt", {})
    if (not isinstance(value, dict) or set(value) != fields or value["contract"] != BASELINE_CONTRACT
            or not isinstance(receipt, dict) or not isinstance(value["snapshot"], dict)
            or type(value["revision"]) is not int or value["revision"] < 1
            or any(not isinstance(value[name], str) or not re.fullmatch(r"[a-f0-9]{64}", value[name])
                for name in ("selection_hash", "package_hash", "product_hash", "material_facts_hash", "binding_hash"))
            or value["package_facts_hash"] is not None and (not isinstance(value["package_facts_hash"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", value["package_facts_hash"]))
            or value["binding_hash"] != digest({k: v for k, v in value.items() if k != "binding_hash"})
            or value["material_facts_hash"] != digest(value["snapshot"])
            or value["snapshot"].get("concept_id") != record.get("concept_id")
            or value["snapshot"].get("version") not in (VERSION, SCOPED_VERSION)
            or any(value[name] != record.get(name, "default" if name == "client_id" else None)
                   for name in ("client_id", "issue_id", "revision", "concept_id"))
            or receipt.get("verified") is not True or receipt.get("authority") != "release_index"
            or any(value[name] != receipt.get(name) for name in ("issue_id", "revision", "selection_hash", "package_hash"))
            or value["package_facts_hash"] != receipt.get("facts_hash")
            or not isinstance(value["evidence_ids"], list) or any(not isinstance(ref, str) for ref in value["evidence_ids"])
            or value["evidence_ids"] != sorted(set(value["evidence_ids"]))):
        raise SafetyError("PUBLISHED_UPDATE_BASELINE_INVALID")
    return value


def _members(concept_id, member_ids):
    identifier(concept_id)
    if member_ids is None:
        return [concept_id]
    if (not isinstance(member_ids, (list, tuple)) or not member_ids or any(not isinstance(cid, str) for cid in member_ids)
            or concept_id not in member_ids
            or len(set(member_ids)) != len(member_ids)):
        raise SafetyError("MATERIAL_UPDATE_IDENTITY_INVALID")
    for cid in member_ids:
        identifier(cid)
    return sorted(member_ids)


def latest_published_baseline_for_members(state, concept_id, client_id, as_of, *, member_ids):
    """Compare the latest actual exposure across an explicitly reviewed identity.

    The frozen old snapshot and its original concept ID are never rewritten.
    Membership prevents a false New label, not transfer of any source vote.
    """
    return latest_published_baseline(state, concept_id, client_id, as_of, member_ids=member_ids)


def latest_published_baseline(state, concept_id, client_id, as_of, *, member_ids=None):
    """Bind the newest actual publication to its unchanged frozen issue.

    Older records without content remain valid publication history, but cannot
    establish an invented comparison baseline. A replaced reservation cannot
    be substituted for the bytes/facts of an older published revision.
    """
    records = []
    members = _members(concept_id, member_ids)
    now = timestamp(as_of)
    for record_id, record in state.get("published_history", {}).items():
        if (record.get("status") != "published" or record.get("concept_id") not in members
                or record.get("client_id", "default") != client_id):
            continue
        if not record.get("available_at"):
            return None  # Legacy history has no reliably ordered content baseline.
        at = timestamp(record["available_at"])
        if at <= now:
            records.append((at, record.get("revision", 0), record_id, record))
    if not records:
        return None
    _, _, record_id, record = max(records, key=lambda row: row[:3])
    original = _immutable_baseline(record)
    if original is not None:
        return {"record_id": record_id, "issue_id": original["issue_id"], "revision": original["revision"],
            "available_at": record["available_at"], "snapshot": deepcopy(original["snapshot"]),
            "evidence_ids": deepcopy(original["evidence_ids"]), "history_hash": digest(record),
            "reservation_hash": None, "baseline_hash": original["binding_hash"],
            "package_hash": original["package_hash"], "package_facts_hash": original["package_facts_hash"],
            "selection_hash": original["selection_hash"]}
    issue = state.get("issue_reservations", {}).get(record.get("issue_id"))
    if not isinstance(issue, dict):
        return None
    receipt = record.get("activation_receipt", {})
    if (issue.get("client_id", "default") != client_id or issue.get("revision") != record.get("revision")
            or issue.get("evidence_fingerprint") != record.get("evidence_fingerprint")
            or receipt.get("verified") is not True or receipt.get("authority") != "release_index"
            or receipt.get("selection_hash") != issue.get("selection_hash")):
        return None
    from .issue_allocation import _fingerprint, selection_fingerprint
    expected = selection_fingerprint(issue)
    if expected != issue["selection_hash"] or issue["evidence_fingerprint"] != _fingerprint(issue["products"]):
        raise SafetyError("PUBLISHED_UPDATE_BASELINE_INVALID")
    original_id = record["concept_id"]
    products = [row for row in issue["products"] if row.get("concept_id") == original_id]
    if len(products) != 1:
        return None
    snapshot = products[0].get("material_facts")
    if (not isinstance(snapshot, dict) or snapshot.get("version") not in (VERSION, SCOPED_VERSION)
            or snapshot.get("concept_id") != original_id
            or products[0].get("material_facts_hash") != digest(snapshot)):
        return None
    return {"record_id": record_id, "issue_id": issue["issue_id"], "revision": issue["revision"],
            "available_at": record["available_at"], "snapshot": deepcopy(snapshot),
            "evidence_ids": deepcopy(products[0].get("evidence_refs", [])),
            "history_hash": digest(record), "reservation_hash": digest(issue)}


def find_material_changes(old, current, *, member_ids=None):
    """Return real changed groups; no semantic usefulness claim is made."""
    if (not isinstance(old, dict) or not isinstance(current, dict) or old.get("version") not in (VERSION, SCOPED_VERSION)
            or current.get("version") not in (VERSION, SCOPED_VERSION)
            or old.get("concept_id") not in _members(current.get("concept_id"), member_ids)):
        raise SafetyError("MATERIAL_COMPARISON_INVALID")
    if old.get("version") != current.get("version"):
        return [{"field_id": "comparison_scope", "old": old["version"], "new": current["version"]}]
    return [{"field_id": name, "old": deepcopy(old.get(name)), "new": deepcopy(current.get(name))}
            for name in ("market", "criteria", "prices", "suppliers") if old.get(name) != current.get(name)]


def update_baseline_current(product, history, reservations, client_id, *, member_ids=None):
    """Reject a saved automatic review after its latest publication changed.

    Legacy upstream review contracts remain supported. New automatic reviews
    must bind the actual latest history row and frozen reservation, not just a
    boolean flag. This check does not redefine publication or mutate history.
    """
    if "material_update_review" not in product:
        return True
    try:
        review = product["material_update_review"]
        baseline = review["baseline"]
        members = _members(product["concept_id"], member_ids)
        if (review.get("accepted") is not True or review.get("rule") not in (RULE, REVIEW_KIND)
                or review.get("decision") != "useful_update" or review.get("current_hash") != digest(product["material_facts"])
                or product.get("material_facts_hash") != review["current_hash"]):
            return False
        rows = history.items() if isinstance(history, dict) else [(str(n), row) for n, row in enumerate(history)]
        matching = [(timestamp(row["available_at"]), row.get("revision", 0), record_id, row)
                    for record_id, row in rows if row.get("status") == "published"
                    and row.get("concept_id") in members and row.get("client_id", "default") == client_id]
        if not matching:
            return False
        latest = max(matching, key=lambda row: row[:3])
        # Mapping keys preserve selection's tie-break. A legacy list of records
        # has no stable keys; accept the bound record only among newest ties.
        newest = [latest[3]] if isinstance(history, dict) else [row[3] for row in matching if row[:2] == latest[:2]]
        if not any(digest(row) == baseline["history_hash"] for row in newest):
            return False
        if baseline.get("baseline_hash") is not None:
            bound = next(row for row in newest if digest(row) == baseline["history_hash"])
            original = _immutable_baseline(bound)
            return (original is not None and original["binding_hash"] == baseline["baseline_hash"]
                    and original["issue_id"] == baseline["issue_id"] and original["revision"] == baseline["revision"])
        issues = reservations.values() if isinstance(reservations, dict) else reservations
        issue = next(row for row in issues if row.get("issue_id") == baseline["issue_id"])
        return digest(issue) == baseline["reservation_hash"]
    except (KeyError, TypeError, ValueError, StopIteration, SafetyError):
        return False


def review_supplier_update(state, fact, market, *, client_id, as_of, member_ids=None):
    """A newly substantiated single-piece dispatch option is a practical update.

    Code uses existing accepted F fields, not text matching or another supplier
    vote. A different supplier with the same already-known arrangement is not
    enough. Recheck ordinary checklist/market eligibility in the caller.
    """
    current = material_snapshot(fact, market)
    baseline = latest_published_baseline(state, fact["concept_id"], client_id, as_of, member_ids=member_ids)
    result = {"accepted": False, "decision": "baseline_unavailable", "rule": RULE,
              "reason": "The client's latest published product facts are not available for a reliable comparison.",
              "current": current, "current_hash": digest(current), "baseline": baseline,
              "changes": [], "old_evidence_ids": [], "new_evidence_ids": [row["id"] for row in fact.get("source_refs", [])]}
    if baseline is None:
        return result
    changes = find_material_changes(baseline["snapshot"], current, member_ids=member_ids)
    result.update(changes=changes, old_evidence_ids=baseline["evidence_ids"], decision="not_material" if not changes else "review_needed",
                  reason="The comparable published facts are unchanged." if not changes else
                         "Content changed, but this bounded rule does not yet establish a useful update.")
    previously_supported = any(row.get("single_piece_dispatch") == "supported" or _scoped_dispatch(row.get("terms", []))
                               for row in baseline["snapshot"].get("suppliers", []))
    now = timestamp(as_of)
    supported = []
    for lead in fact.get("suppliers", []):
        if lead.get("single_piece_dispatch") != "supported" or lead.get("relationship") not in ("exact_product", "relevant_company"):
            continue
        date = lead.get("source_capture_at") or lead.get("reviewed_at")
        if not date or not timedelta(0) <= now - timestamp(date) <= timedelta(days=30):
            continue
        supported.append(lead)
    scoped = fact.get("supplier_scoped_terms") or {}
    for lead in scoped.get("all", []):
        terms = lead.get("effective_terms", lead.get("terms", []))
        if not _scoped_dispatch(terms) or lead["relevance"]["relationship"] not in ("exact_product", "relevant_company"):
            continue
        dates = [term["observed_at"] for term in terms if term["field"] in ("single_piece_purchase", "direct_customer_dispatch")
                 and term.get("qualified") and term.get("observed_at")]
        if len(dates) == 2 and all(timedelta(0) <= now - timestamp(at) <= timedelta(days=30) for at in dates):
            supported.append({"lead_id": lead["lead_id"], "source_capture_at": min(dates, key=timestamp)})
    if not previously_supported and supported:
        usable_until = max(timestamp(row.get("source_capture_at") or row["reviewed_at"]) + timedelta(days=30)
                           for row in supported).isoformat()
        result.update(accepted=True, decision="useful_update",
                      reason="Public supplier information now states single-piece purchase with direct customer dispatch; this arrangement was not established in the client's previous report.",
                      practical_consequence="The client has a stated per-order sourcing and dispatch option to investigate, without assuming verified fulfilment.",
                      supporting_lead_ids=sorted(row["lead_id"] for row in supported), usable_until=usable_until)
    return result


def _scoped_dispatch(terms):
    rows = {r["field"]: r for r in terms if r.get("qualified") is True}
    return (all(rows.get(name, {}).get("value") in ("Yes", "yes") for name in
                ("single_piece_purchase", "direct_customer_dispatch"))
            and rows.get("bulk_stock_prerequisite", {}).get("value") not in ("Yes", "yes", "Conflicting", "conflicting"))


def _comparable_groups(old, current, changes):
    """Narrow the references E can actually cite to matching conditions."""
    groups = {}
    for change in changes:
        field = change["field_id"]
        if field == "market" and current["version"] == SCOPED_VERSION:
            old_sources, new_sources = old["market"]["sources"], current["market"]["sources"]
            sources = [source for source in SOURCES if old_sources[source] != new_sources[source]
                and old_sources[source]["signal"].get("available") is True
                and new_sources[source]["signal"].get("available") is True
                and old_sources[source]["scopes"] == new_sources[source]["scopes"]
                and any(_useful_market_scope(scope) for scope in old_sources[source]["scopes"])]
            if sources:
                groups[field] = ({"sources": {s: deepcopy(old_sources[s]) for s in sources}},
                                 {"sources": {s: deepcopy(new_sources[s]) for s in sources}})
        elif field == "market" and current["version"] == VERSION:
            # Old three-family summaries have no exact query/location scope.
            # Preserve their history; do not manufacture numerical comparability.
            continue
        elif field == "prices":
            condition = lambda row: digest({k: v for k, v in row.items() if k not in
                ("amount", "quoted_amount", "amount_range", "raw_displayed")})
            shared = {condition(row) for row in old[field]} & {condition(row) for row in current[field]}
            before = [row for row in old[field] if condition(row) in shared]
            after = [row for row in current[field] if condition(row) in shared]
            if before != after:
                groups[field] = (deepcopy(before), deepcopy(after))
        elif field == "suppliers" and old[field] and current[field]:
            if current["version"] == VERSION:
                groups[field] = (deepcopy(old[field]), deepcopy(current[field]))
            else:
                # Offer identity is not one supplier/manufacturer. It is the
                # actual narrow type plus the declared purchase unit; company
                # generalities cannot be turned into a unit-specific quotation.
                units = ({r.get("purchase_unit") for r in old[field] if r.get("scope_kind") == "product_offer"}
                         & {r.get("purchase_unit") for r in current[field] if r.get("scope_kind") == "product_offer"}) - {None}
                before = [r for r in old[field] if r.get("scope_kind") == "product_offer" and r.get("purchase_unit") in units]
                after = [r for r in current[field] if r.get("scope_kind") == "product_offer" and r.get("purchase_unit") in units]
                if before != after:
                    groups[field] = (deepcopy(before), deepcopy(after))
    return groups


def comparable_changes(old, current, changes):
    """Scope changes/missing evidence do not turn unequal numbers into proof."""
    if old["version"] != current["version"]:
        return {"comparable": False, "reason": "The previous and current facts use different comparison scopes.", "fields": []}
    if old.get("meaning_hash") != current.get("meaning_hash"):
        return {"comparable": False, "reason": "The functional meaning changed and the old facts cannot safely be substituted.", "fields": []}
    fields = sorted(_comparable_groups(old, current, changes))
    return {"comparable": bool(fields), "reason": "Changed facts have a matching saved comparison scope." if fields else
            "The changed facts do not yet support a comparable practical update.", "fields": fields}


def update_item(update):
    """Stable semantic input: actual proof IDs/dates stay in the application."""
    from .research_memory import key
    old, new = update["baseline"]["snapshot"], update["current"]
    comparison = comparable_changes(old, new, update["changes"])
    groups = _comparable_groups(old, new, update["changes"]) if comparison["comparable"] else {}
    refs = [{"id": side + ":" + field, "side": side, "field_id": field, "facts": deepcopy(facts)}
            for field in comparison["fields"] for side, facts in zip(("old", "new"), groups[field])]
    body = {"old": deepcopy(old), "current": deepcopy(new), "changes": deepcopy(update["changes"]),
            "comparison_subjects": sorted({old["concept_id"], new["concept_id"]}),
            "comparability": comparison, "reference_choices": refs}
    return dict(body, id=key("material-update", body))


def validate_update_item(item):
    from .research_memory import key
    try:
        if set(item) != {"id", "old", "current", "changes", "comparison_subjects", "comparability", "reference_choices"}:
            raise ValueError
        body = {k: v for k, v in item.items() if k != "id"}
        if (item["id"] != key("material-update", body)
                or item["comparison_subjects"] != sorted({item["old"]["concept_id"], item["current"]["concept_id"]})
                or item["changes"] != find_material_changes(item["old"], item["current"], member_ids=item["comparison_subjects"])
                or item["comparability"] != comparable_changes(item["old"], item["current"], item["changes"])
                or item["comparability"]["comparable"] is not True
                or item != update_item({"baseline": {"snapshot": item["old"]}, "current": item["current"], "changes": item["changes"]})
                or len(canonical(item)) > 160 * 1024):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, SafetyError):
        raise SafetyError("MATERIAL_UPDATE_INPUT_INVALID") from None
    return item


def validate_update_result(item, result):
    validate_update_item(item)
    try:
        if (set(result) != {"id", "decision", "reason", "practical_consequence", "changed_fields", "references"}
                or result["id"] != item["id"] or result["decision"] not in ("useful_update", "not_material", "unclear")
                or any(not isinstance(result[f], str) or len(result[f]) > 2000 for f in ("reason", "practical_consequence"))
                or not result["reason"].strip()
                or not isinstance(result["changed_fields"], list) or not isinstance(result["references"], list)
                or result["changed_fields"] != sorted(set(result["changed_fields"]))
                or not set(result["changed_fields"]) <= set(item["comparability"]["fields"])
                or len(result["references"]) != len(set(result["references"]))
                or not set(result["references"]) <= {r["id"] for r in item["reference_choices"]}):
            raise ValueError
        if result["decision"] == "useful_update":
            required = {side + ":" + field for field in result["changed_fields"] for side in ("old", "new")}
            if not required or not required <= set(result["references"]) or not result["practical_consequence"].strip():
                raise ValueError
        elif result["practical_consequence"].strip():
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SafetyError("MATERIAL_UPDATE_RESULT_INVALID") from None
    return deepcopy(result)


def update_payload(items):
    return {"items": deepcopy(items), "mode": UPDATE_MODE, "material_contract": SCOPED_VERSION}


def _dependencies(state, fact):
    result = {}
    ids = set(ref["id"] for ref in fact.get("source_refs", []))
    ids.add(fact["concept_id"])
    for table in ("concepts", "assessments", "observations", "captures", "trends_evidence", "source_checks", "supplier_assessments"):
        rows = {rid: digest(row) for rid, row in state.get(table, {}).items() if rid in ids}
        for rid in fact.get("source_revisions", {}).get(table, {}):
            if rid in state.get(table, {}):
                rows[rid] = digest(state[table][rid])
        if rows:
            result[table] = rows
    # Functional proof follows actual original records, not just display IDs.
    from .frozen_records import read_original_json
    for name in ("functional_market_proof", "functional_supplier_proof"):
        proof = fact.get(name, {})
        for table, rows in proof.get("records", {}).items():
            for rid, wrapped in rows.items():
                original = read_original_json(wrapped)
                if digest(state.get(table, {}).get(rid)) != digest(original):
                    raise SafetyError("MATERIAL_UPDATE_DEPENDENCY_CHANGED")
                result.setdefault(table, {})[rid] = digest(original)
        if name == "functional_market_proof":
            for rid, wrapped in proof.get("scoped_records", {}).items():
                original = read_original_json(wrapped)
                if digest(state.get("source_checks", {}).get(rid)) != digest(original):
                    raise SafetyError("MATERIAL_UPDATE_DEPENDENCY_CHANGED")
                result.setdefault("source_checks", {})[rid] = digest(original)
    return result


def prepare_material_update(state, fact, market, *, client_id, as_of, member_ids=None):
    """Only a real changed, comparable publication can enter bounded E work."""
    members = _members(fact["concept_id"], member_ids)
    identity_hash = None
    if member_ids is not None:
        from .functional_aliases import alias_projection
        identity = alias_projection(state)[fact["concept_id"]]
        if identity["reason"] or identity["member_ids"] != members:
            raise SafetyError("MATERIAL_UPDATE_IDENTITY_INVALID")
        identity_hash = digest(identity)
    update = review_supplier_update(state, fact, market, client_id=client_id, as_of=as_of, member_ids=members)
    if update["accepted"] or update["decision"] != "review_needed":
        return {"state": "not_needed", "update": update}
    item = update_item(update)
    if not item["comparability"]["comparable"] or not update["old_evidence_ids"] or not update["new_evidence_ids"]:
        update.update(decision="unclear", reason=item["comparability"]["reason"])
        return {"state": "not_comparable", "update": update}
    validate_update_item(item)
    from .report_selection import selection_scope
    route_contract = fact.get("source_route_contract")
    binding = {"client_id": client_id, "concept_id": fact["concept_id"],
        "baseline": {k: deepcopy(v) for k, v in update["baseline"].items() if k != "snapshot"},
        "current_hash": update["current_hash"], "input_hash": digest(item),
        "old_evidence_ids": update["old_evidence_ids"], "new_evidence_ids": sorted(set(update["new_evidence_ids"])),
        "dependencies": _dependencies(state, fact), "usable_until": fact.get("usable_until"),
        "identity_members": members, "identity_scope_hash": identity_hash,
        "source_route_contract": route_contract,
        "source_scope_hash": digest(selection_scope(state, fact["concept_id"], source_route_contract=route_contract))}
    return {"state": "prepared", "item": item, "binding": binding, "update": update}


def _validate_prepared(item, binding):
    validate_update_item(item)
    fields = {"client_id", "concept_id", "baseline", "current_hash", "input_hash", "old_evidence_ids", "new_evidence_ids",
        "dependencies", "usable_until", "identity_members", "identity_scope_hash", "source_route_contract", "source_scope_hash"}
    try:
        if (not isinstance(binding, dict) or set(binding) != fields
                or binding["concept_id"] != item["current"]["concept_id"]
                or binding["identity_members"] != _members(binding["concept_id"], binding["identity_members"])
                or not set(item["comparison_subjects"]) <= set(binding["identity_members"])
                or binding["current_hash"] != digest(item["current"]) or binding["input_hash"] != digest(item)
                or not isinstance(binding["baseline"], dict) or not isinstance(binding["dependencies"], dict)):
            raise ValueError
        identifier(binding["client_id"])
        for name in ("old_evidence_ids", "new_evidence_ids"):
            refs = binding[name]
            if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref for ref in refs):
                raise ValueError
        if binding["new_evidence_ids"] != sorted(set(binding["new_evidence_ids"])):
            raise ValueError
        for table, rows in binding["dependencies"].items():
            if not isinstance(table, str) or not isinstance(rows, dict):
                raise ValueError
            for rid, hashed in rows.items():
                identifier(rid)
                if not isinstance(hashed, str) or not re.fullmatch(r"[a-f0-9]{64}", hashed):
                    raise ValueError
        for name in ("source_scope_hash", "identity_scope_hash"):
            if name == "identity_scope_hash" and binding[name] is None:
                continue
            if not isinstance(binding[name], str) or not re.fullmatch(r"[a-f0-9]{64}", binding[name]):
                raise ValueError
        if binding["usable_until"] is not None:
            timestamp(binding["usable_until"])
    except (KeyError, TypeError, ValueError, AttributeError, SafetyError):
        raise SafetyError("MATERIAL_UPDATE_INPUT_INVALID") from None


def _binding_current(state, binding, *, item, as_of):
    _validate_prepared(item, binding)
    from .report_selection import selection_scope
    baseline = latest_published_baseline(state, binding["concept_id"], binding["client_id"], as_of,
        member_ids=binding["identity_members"])
    if binding["identity_scope_hash"] is not None:
        from .functional_aliases import alias_projection
        if digest(alias_projection(state).get(binding["concept_id"])) != binding["identity_scope_hash"]:
            return False
    return (baseline is not None and baseline["history_hash"] == binding["baseline"]["history_hash"]
        and binding["baseline"] == {k: v for k, v in baseline.items() if k != "snapshot"}
        and item["old"] == baseline["snapshot"] and binding["old_evidence_ids"] == baseline["evidence_ids"]
        and binding["source_scope_hash"] == digest(selection_scope(state, binding["concept_id"],
            source_route_contract=binding["source_route_contract"]))
        and all(digest(state.get(table, {}).get(rid)) == hashed for table, rows in binding["dependencies"].items()
                for rid, hashed in rows.items())
        and (binding["usable_until"] is None or timestamp(binding["usable_until"]) >= timestamp(as_of)))


def _material_original_job(state, job_id, item):
    """Resolve a real original owner, never an aggregate derived-result file."""
    from .research_ai import MODEL, _item_bindings
    from .research_memory import key
    try:
        identifier(job_id)
        job = state["jobs"][job_id]
        request, bindings = job["request"], job["binding_keys"]
        payload = request["payload"]
        if (set(request) != {"role", "payload", "model", "prompt_version", "runner"}
                or request["role"] != "E" or request["model"] != MODEL
                or request["prompt_version"] != UPDATE_PROMPT_VERSION
                or not isinstance(request["runner"], str) or not request["runner"]
                or job_id != key("job", request) or not isinstance(payload, dict)
                or set(payload) != {"items", "mode", "material_contract"}
                or payload["mode"] != UPDATE_MODE or payload["material_contract"] != SCOPED_VERSION
                or not isinstance(payload["items"], list) or not payload["items"]):
            raise ValueError
        for value in payload["items"]:
            validate_update_item(value)
        if (len({value["id"] for value in payload["items"]}) != len(payload["items"])
                or [value for value in payload["items"] if value["id"] == item["id"]] != [item]
                or bindings != _item_bindings("E", payload, MODEL, UPDATE_PROMPT_VERSION, request["runner"])):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, SafetyError):
        raise SafetyError("MATERIAL_UPDATE_ORIGINAL_JOB_INVALID") from None
    return job, request, bindings[item["id"]]


def validate_material_job_proof(state, item, proof):
    """Pure historical acceptance: retain the exact reply, not a mutable label."""
    from .research_ai import validate_results
    fields = {"contract", "job_id", "item_id", "original_job", "job_hash", "request_hash", "binding_key",
        "attempt", "result_receipt", "result_hash", "accepted_item", "accepted_item_hash"}
    try:
        if not isinstance(proof, dict) or set(proof) != fields or proof["contract"] != UPDATE_JOB_PROOF:
            raise ValueError
        job, request, binding = _material_original_job(state, proof["job_id"], item)
        original, receipt = proof["original_job"], proof["result_receipt"]
        if (not isinstance(original, dict) or digest(original) != proof["job_hash"]
                or original.get("request") != request or original.get("binding_keys") != job["binding_keys"]
                or proof["item_id"] != item["id"] or proof["binding_key"] != binding
                or proof["request_hash"] != digest(request)
                or not isinstance(proof["attempt"], str)
                or not re.fullmatch(r"original|retry-[1-9][0-9]*", proof["attempt"])
                or (proof["attempt"].startswith("retry-") and int(proof["attempt"][6:]) > 20)
                or not isinstance(receipt, dict) or set(receipt) != {"request_hash", "response", "usage", "completed_at"}
                or receipt["request_hash"] != digest(request) or digest(receipt) != proof["result_hash"]):
            raise ValueError
        timestamp(receipt["completed_at"])
        accepted = validate_results("E", request["payload"], receipt["response"],
            declared_version=UPDATE_PROMPT_VERSION)["accepted"].get(item["id"])
        if (accepted is None or accepted != proof["accepted_item"] or digest(accepted) != proof["accepted_item_hash"]):
            raise ValueError
        return validate_update_result(item, accepted)
    except (KeyError, TypeError, ValueError, AttributeError, SafetyError):
        raise SafetyError("MATERIAL_UPDATE_ORIGINAL_ACCEPTANCE_INVALID") from None


def _recover_material_completion(memory, item, job_id):
    """Only copy a genuine saved completion; never admit or launch model work."""
    from .research_ai import MAX_JOB_BYTES, current_attempt, validate_results
    from .research_memory import load_event
    from .storage import atomic_json, safe_path
    base = safe_path(memory.root, "ai_jobs", job_id)
    with memory._cache_lock, memory.journal.locked(timeout=5):
        _, request, _ = _material_original_job(memory.snapshot(), job_id, item)
        folder = current_attempt(base)
        result_path = folder / "result.json"
        if result_path.exists():
            return
        completion_path, intent_path = folder / "completion.json", folder / "intent.json"
        if not completion_path.exists() or not intent_path.exists():
            raise SafetyError("MATERIAL_UPDATE_ORIGINAL_RESULT_UNAVAILABLE")
        before = [memory._fingerprint(path) for path in (completion_path, intent_path)]
        completion = load_event(completion_path, limit=MAX_JOB_BYTES)
        intent = load_event(intent_path, limit=MAX_JOB_BYTES)
        after = [memory._fingerprint(path) for path in (completion_path, intent_path)]
        if (before != after or not isinstance(intent, dict) or intent.get("request") != request
                or not isinstance(completion, dict) or not {"response", "usage"} <= set(completion)):
            raise SafetyError("MATERIAL_UPDATE_ORIGINAL_RESULT_CHANGED")
        accepted = validate_results("E", request["payload"], completion["response"],
            declared_version=UPDATE_PROMPT_VERSION)["accepted"].get(item["id"])
        if accepted is None:
            raise SafetyError("MATERIAL_UPDATE_ORIGINAL_ACCEPTANCE_INVALID")
        if current_attempt(base) != folder:
            raise SafetyError("MATERIAL_UPDATE_ORIGINAL_RESULT_CHANGED")
        atomic_json(result_path, {"request_hash": digest(request), "response": completion["response"],
            "usage": completion["usage"], "completed_at": utc_now()})


def _verify_material_job(memory, state, item, *, runner_identity):
    """Freeze an actual current-attempt accepted reply before local application."""
    from .research_ai import MODEL, MAX_JOB_BYTES, _item_bindings, current_attempt, validate_results
    from .research_memory import load_event
    from .storage import safe_path
    # Jobs may return an aggregate ID while the item belongs to an older exact
    # batch. Find that original by its complete per-item/context binding.
    wanted = _item_bindings("E", update_payload([item]), MODEL, UPDATE_PROMPT_VERSION, runner_identity)[item["id"]]
    owners = [rid for rid, row in state.get("jobs", {}).items()
        if row.get("binding_keys", {}).get(item["id"]) == wanted]
    if len(owners) != 1:
        raise SafetyError("MATERIAL_UPDATE_ORIGINAL_JOB_INVALID")
    original_id = owners[0]
    job, request, binding = _material_original_job(state, original_id, item)
    base = safe_path(memory.root, "ai_jobs", original_id)
    folder = current_attempt(base)
    path = folder / "result.json"
    before = memory._fingerprint(path)
    receipt = load_event(path, limit=MAX_JOB_BYTES)
    after = memory._fingerprint(path)
    if before != after or not isinstance(receipt, dict) or receipt.get("request_hash") != digest(request):
        raise SafetyError("MATERIAL_UPDATE_ORIGINAL_RESULT_CHANGED")
    accepted = validate_results("E", request["payload"], receipt.get("response"),
        declared_version=UPDATE_PROMPT_VERSION)["accepted"].get(item["id"])
    if accepted is None:
        raise SafetyError("MATERIAL_UPDATE_ORIGINAL_ACCEPTANCE_INVALID")
    proof = {"contract": UPDATE_JOB_PROOF, "job_id": original_id, "item_id": item["id"],
        "original_job": deepcopy(job), "job_hash": digest(job), "request_hash": digest(request), "binding_key": binding,
        "attempt": "original" if folder == base else folder.name,
        "result_receipt": deepcopy(receipt), "result_hash": digest(receipt),
        "accepted_item": deepcopy(accepted), "accepted_item_hash": digest(accepted)}
    validate_material_job_proof(state, item, proof)
    return {"proof": proof, "base": base, "folder": folder, "result_path": path, "fingerprint": after}


def current_material_update(state, prepared, *, as_of):
    """Read an exact saved verdict; no model/fetch/automatic publication."""
    if prepared["state"] != "prepared":
        return None
    item, binding = prepared["item"], prepared["binding"]
    _validate_prepared(item, binding)
    from .research_memory import key
    matches = [(rid, row) for rid, row in state.get("source_checks", {}).items()
               if row.get("kind") == REVIEW_KIND and row.get("binding") == binding]
    # Genuine old applications stay immutable. Only an original-reply-backed,
    # hash-bound successor can authorize a current recommendation.
    candidates = [(rid, row) for rid, row in matches if "job_proof" in row]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise SafetyError("SAVED_MATERIAL_UPDATE_INVALID")
    for rid, row in candidates:
        try:
            fields = {"kind", "binding", "binding_hash", "input", "job_id", "result", "reviewed_at", "record_hash", "job_proof"}
            if (set(row) != fields
                    or row["input"] != item or row["binding_hash"] != digest(binding)
                    or rid != key("material-update-review", [binding, row["job_id"], digest(row["job_proof"])])
                    or row["record_hash"] != digest({k: v for k, v in row.items() if k != "record_hash"})):
                raise ValueError
            _material_original_job(state, row["job_id"], item)
            validate_update_result(item, row["result"])
            if (row["job_proof"]["job_id"] != row["job_id"]
                    or validate_material_job_proof(state, item, row["job_proof"]) != row["result"]):
                raise ValueError
            if timestamp(row["reviewed_at"]) > timestamp(as_of) or not _binding_current(state, binding, item=item, as_of=as_of):
                return None
            return row
        except (KeyError, TypeError, ValueError, AttributeError, SafetyError):
            raise SafetyError("SAVED_MATERIAL_UPDATE_INVALID") from None
    return None


def run_material_updates(memory, prepared, runner=None, *, as_of=None, limit=2):
    """One exact cached E owner per comparable change; healthy siblings proceed."""
    from .research_ai import Jobs
    from .research_memory import change, key
    if not isinstance(prepared, list) or type(limit) is not int or not 0 <= limit <= 10:
        raise SafetyError("MATERIAL_UPDATE_LIMIT_INVALID")
    at = as_of or utc_now(); timestamp(at)
    results = []
    for entry in prepared[:limit]:
        if not isinstance(entry, dict):
            raise SafetyError("MATERIAL_UPDATE_INPUT_INVALID")
        if entry.get("state") == "prepared":
            _validate_prepared(entry.get("item"), entry.get("binding"))
    for entry in prepared[:limit]:
        if entry.get("state") != "prepared":
            continue
        item, binding = entry["item"], entry["binding"]
        try:
            state = memory.snapshot()
            if not _binding_current(state, binding, item=item, as_of=at):
                raise SafetyError("MATERIAL_UPDATE_DEPENDENCY_CHANGED")
            existing = current_material_update(state, entry, as_of=at)
            if existing:
                results.append({"concept_id": binding["concept_id"], "state": "reused", "decision": existing["result"]["decision"]})
                continue
            legacy_rows = [(rid, row) for rid, row in state["source_checks"].items()
                if row.get("kind") == REVIEW_KIND and row.get("binding") == binding and "job_proof" not in row]
            if legacy_rows:
                if len(legacy_rows) != 1:
                    raise SafetyError("SAVED_MATERIAL_UPDATE_INVALID")
                legacy_id, legacy = legacy_rows[0]
                fields = {"kind", "binding", "binding_hash", "input", "job_id", "result", "reviewed_at", "record_hash"}
                if (set(legacy) != fields or legacy["input"] != item or legacy["binding_hash"] != digest(binding)
                        or legacy_id != key("material-update-review", [binding, legacy["job_id"]])
                        or legacy["record_hash"] != digest({k: v for k, v in legacy.items() if k != "record_hash"})):
                    raise SafetyError("SAVED_MATERIAL_UPDATE_INVALID")
                _, request, _ = _material_original_job(state, legacy["job_id"], item)
                _recover_material_completion(memory, item, legacy["job_id"])
                verified = _verify_material_job(memory, memory.snapshot(), item, runner_identity=request["runner"])
                result = validate_update_result(item, verified["proof"]["accepted_item"])
                if result != legacy["result"]:
                    raise SafetyError("SAVED_MATERIAL_UPDATE_INVALID")
            else:
                from .model_recovery import run_current_model_job
                job_id, outcome = run_current_model_job(memory, runner, "E", update_payload([item]),
                                                        declared_version=UPDATE_PROMPT_VERSION)
                if item["id"] in outcome["failures"]:
                    results.append({"concept_id": binding["concept_id"], "state": "waiting", "reason": outcome["failures"][item["id"]], "job_id": job_id})
                    continue
                result = validate_update_result(item, outcome["accepted"][item["id"]])
                verified = _verify_material_job(memory, memory.snapshot(), item,
                    runner_identity=getattr(runner, "identity", "codex-chatgpt-high-v1"))
            proof = verified["proof"]
            if proof["accepted_item"] != result:
                raise SafetyError("MATERIAL_UPDATE_ORIGINAL_ACCEPTANCE_INVALID")
            job_id = proof["job_id"]
            row = {"kind": REVIEW_KIND, "binding": deepcopy(binding), "binding_hash": digest(binding), "input": deepcopy(item),
                   "job_id": job_id, "result": result, "reviewed_at": at, "job_proof": proof}
            row["record_hash"] = digest(row)
            rid = key("material-update-review", [binding, job_id, digest(proof)])
            original_state = memory.snapshot()
            old = original_state["source_checks"].get(rid)
            legacy_id = key("material-update-review", [binding, job_id])
            legacy = original_state["source_checks"].get(legacy_id)
            if old is not None:
                if (set(old) != set(row)
                        or old["record_hash"] != digest({k: v for k, v in old.items() if k != "record_hash"})
                        or {k: v for k, v in old.items() if k not in ("reviewed_at", "record_hash")} != {
                            k: v for k, v in row.items() if k not in ("reviewed_at", "record_hash")}):
                    raise SafetyError("SAVED_MATERIAL_UPDATE_INVALID")
                if timestamp(old["reviewed_at"]) > timestamp(at):
                    raise SafetyError("MATERIAL_UPDATE_REVIEW_NOT_YET_CURRENT")
            if legacy is not None:
                legacy_fields = {"kind", "binding", "binding_hash", "input", "job_id", "result", "reviewed_at", "record_hash"}
                if (set(legacy) != legacy_fields
                        or legacy["record_hash"] != digest({k: v for k, v in legacy.items() if k != "record_hash"})
                        or {k: v for k, v in legacy.items() if k not in ("reviewed_at", "record_hash")} != {
                            k: v for k, v in row.items() if k not in ("reviewed_at", "record_hash", "job_proof")}):
                    raise SafetyError("SAVED_MATERIAL_UPDATE_INVALID")
            def validate(current):
                if not _binding_current(current, binding, item=item, as_of=at):
                    raise SafetyError("MATERIAL_UPDATE_DEPENDENCY_CHANGED")
                from .research_ai import current_attempt
                if (current_attempt(verified["base"]) != verified["folder"]
                        or memory._fingerprint(verified["result_path"]) != verified["fingerprint"]):
                    raise SafetyError("MATERIAL_UPDATE_ORIGINAL_RESULT_CHANGED")
                if validate_material_job_proof(current, item, proof) != result:
                    raise SafetyError("MATERIAL_UPDATE_ORIGINAL_ACCEPTANCE_INVALID")
                if current["source_checks"].get(rid) != old or current["source_checks"].get(legacy_id) != legacy:
                    raise SafetyError("SAVED_MATERIAL_UPDATE_INVALID")
            if old is None:
                memory.commit(key("apply-material-update", rid), [change("source_checks", rid, row)], validate=validate)
            results.append({"concept_id": binding["concept_id"], "state": "accepted", "decision": result["decision"], "review_id": rid})
        except (SafetyError, OSError) as error:
            if str(error) == "SAVED_MATERIAL_UPDATE_INVALID":
                raise
            results.append({"concept_id": binding["concept_id"], "state": "waiting", "reason": str(error)})
    return results


def review_material_update(state, fact, market, *, client_id, as_of, member_ids=None):
    """Existing pure pool entrypoint: deterministic rule, then saved E verdict."""
    prepared = prepare_material_update(state, fact, market, client_id=client_id, as_of=as_of, member_ids=member_ids)
    update = prepared["update"]
    if prepared["state"] != "prepared":
        return update
    saved = current_material_update(state, prepared, as_of=as_of)
    if saved is None:
        return update
    verdict = saved["result"]
    update.update(accepted=verdict["decision"] == "useful_update", decision=verdict["decision"], rule=REVIEW_KIND,
        reason=verdict["reason"], practical_consequence=verdict["practical_consequence"],
        supporting_lead_ids=[], usable_until=fact.get("usable_until"), review_id=next(rid for rid, r in state["source_checks"].items() if r == saved),
        model_references=deepcopy(verdict["references"]), changed_fields=deepcopy(verdict["changed_fields"]))
    return update
