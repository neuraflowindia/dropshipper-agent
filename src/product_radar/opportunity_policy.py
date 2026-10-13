"""Code-owned recommendation priority, separate from checklist arithmetic.

Version one counts the three historical source groups. Version two counts
distinct reviewed platforms, not their sub-searches. Two usable sources do not
need to satisfy a growth threshold; one source needs supported positive context.
Original frozen packets retain their declared contract.
"""

from copy import deepcopy

from .fit_policy import FIT_POLICY_ID
from .models import SafetyError


OPPORTUNITY_POLICY_ID = "opportunity-priority-v1"
CURRENT_OPPORTUNITY_POLICY_ID = "opportunity-priority-v2"
OPPORTUNITY_POLICY_IDS = (OPPORTUNITY_POLICY_ID, CURRENT_OPPORTUNITY_POLICY_ID)
DEMAND_GROUPS = ("meta", "marketplace", "trends")
SOURCE_PLATFORMS = ("meta", "amazon", "flipkart", "google-trends", "blinkit", "zepto", "instamart")
SOURCE_LABELS = {"meta": "Facebook ads", "amazon": "Amazon India", "flipkart": "Flipkart",
                 "google-trends": "Google Trends", "blinkit": "Blinkit", "zepto": "Zepto", "instamart": "Instamart"}
OPPORTUNITY_FIELDS = ("opportunity_policy_id", "opportunity_priority", "opportunity_basis")


def _priority(matches, available, positive):
    if matches < 5:
        return None
    if available >= 2:
        return 1 if matches >= 6 else 2
    if available == 1 and positive >= 1:
        return 3 if matches >= 6 else 4
    return None


def evaluate_opportunity(fit, market):
    """Derive priority from protected fit counts and individual source results.

    Aggregate source counts and model-written scores are not used. The explicit
    reasonably_positive support marker is reserved for a validated source
    interpretation; missing data or simple listing absence never supplies it.
    """
    if (not isinstance(fit, dict) or fit.get("fit_policy_id") != FIT_POLICY_ID
            or type(fit.get("kpi_matches")) is not int or not 0 <= fit["kpi_matches"] <= 10
            or type(fit.get("kpi_total")) is not int or fit["kpi_total"] != 10):
        raise SafetyError("OPPORTUNITY_FIT_INVALID")
    if isinstance(market, dict) and "source_policy_id" in market:
        if market["source_policy_id"] != CURRENT_OPPORTUNITY_POLICY_ID:
            raise SafetyError("OPPORTUNITY_MARKET_INVALID")
        return _evaluate_platforms(fit, market)
    if isinstance(market, dict) and "sources" in market:
        raise SafetyError("OPPORTUNITY_MARKET_INVALID")
    families = market.get("families") if isinstance(market, dict) else None
    if not isinstance(families, dict):
        raise SafetyError("OPPORTUNITY_MARKET_INVALID")
    available, positive = 0, 0
    for source in DEMAND_GROUPS:
        result = families.get(source)
        if (not isinstance(result, dict) or type(result.get("available")) is not bool
                or result.get("positive") is not None and type(result["positive"]) is not bool):
            raise SafetyError("OPPORTUNITY_SOURCE_INVALID")
        if not result["available"]:
            continue
        available += 1
        if result.get("positive") is True or result.get("evidence_support") == "reasonably_positive":
            positive += 1
    matches = fit["kpi_matches"]
    priority = _priority(matches, available, positive)
    if matches < 5:
        reason = "fit_information_pending"
    elif available == 0:
        reason = "market_coverage_required"
    elif priority is None:
        reason = "market_support_pending"
    else:
        reason = "opportunity_prioritised"
    return {"opportunity_policy_id": OPPORTUNITY_POLICY_ID,
            "opportunity_priority": priority,
            "opportunity_basis": {"available_families": available, "positive_families": positive},
            "eligible": priority is not None, "reason": reason}


def _evaluate_platforms(fit, market):
    sources = market.get("sources")
    if not isinstance(sources, dict):
        raise SafetyError("OPPORTUNITY_MARKET_INVALID")
    confirmed, positive, evidence = [], [], {}
    for source in SOURCE_PLATFORMS:
        result = sources.get(source)
        if (not isinstance(result, dict) or type(result.get("available")) is not bool
                or result.get("positive") is not None and type(result["positive"]) is not bool):
            raise SafetyError("OPPORTUNITY_SOURCE_INVALID")
        if not result["available"]:
            continue
        refs = result.get("used_ids")
        if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref for ref in refs)
                or len(set(refs)) != len(refs)):
            raise SafetyError("OPPORTUNITY_SOURCE_REFERENCE_INVALID")
        confirmed.append(source); evidence[source] = sorted(refs)
        if result.get("positive") is True or result.get("evidence_support") == "reasonably_positive":
            positive.append(source)
    confirmed.sort(); positive.sort()
    basis = {"available_sources": len(confirmed), "positive_sources": len(positive),
             "source_ids": confirmed, "positive_source_ids": positive, "evidence_by_source": evidence}
    _validate_platform_basis(basis)
    matches = fit["kpi_matches"]; priority = _priority(matches, len(confirmed), len(positive))
    reason = ("fit_information_pending" if matches < 5 else "market_coverage_required" if not confirmed
              else "market_support_pending" if priority is None else "opportunity_prioritised")
    return {"opportunity_policy_id": CURRENT_OPPORTUNITY_POLICY_ID, "opportunity_priority": priority,
            "opportunity_basis": basis, "eligible": priority is not None, "reason": reason}


def _validate_platform_basis(basis):
    fields = {"available_sources", "positive_sources", "source_ids", "positive_source_ids", "evidence_by_source"}
    if (not isinstance(basis, dict) or set(basis) != fields
            or type(basis["available_sources"]) is not int or type(basis["positive_sources"]) is not int
            or not 0 <= basis["positive_sources"] <= basis["available_sources"] <= len(SOURCE_PLATFORMS)):
        raise SafetyError("OPPORTUNITY_BASIS_INVALID")
    for field in ("source_ids", "positive_source_ids"):
        values = basis[field]
        if (not isinstance(values, list) or any(not isinstance(value, str) or value not in SOURCE_PLATFORMS for value in values)
                or values != sorted(set(values))):
            raise SafetyError("OPPORTUNITY_BASIS_INVALID")
    refs = basis["evidence_by_source"]
    if (len(basis["source_ids"]) != basis["available_sources"]
            or len(basis["positive_source_ids"]) != basis["positive_sources"]
            or not set(basis["positive_source_ids"]) <= set(basis["source_ids"])
            or not isinstance(refs, dict) or set(refs) != set(basis["source_ids"])):
        raise SafetyError("OPPORTUNITY_BASIS_INVALID")
    seen = set()
    from .models import identifier
    for values in refs.values():
        if not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values):
            raise SafetyError("OPPORTUNITY_BASIS_INVALID")
        if values != sorted(set(values)) or set(values) & seen:
            raise SafetyError("OPPORTUNITY_SOURCE_REFERENCE_INVALID")
        for value in values:
            identifier(value)
        seen.update(values)
    return basis["available_sources"], basis["positive_sources"]


def opportunity_counts(record):
    """Read the declared count vocabulary; never silently rename old groups."""
    basis = record["opportunity_basis"]
    if record["opportunity_policy_id"] == CURRENT_OPPORTUNITY_POLICY_ID:
        return basis["available_sources"], basis["positive_sources"]
    return basis["available_families"], basis["positive_families"]


def opportunity_source_limit(record):
    return len(SOURCE_PLATFORMS) if record["opportunity_policy_id"] == CURRENT_OPPORTUNITY_POLICY_ID else len(DEMAND_GROUPS)


def opportunity_workbook_headers(policy_id):
    if policy_id == CURRENT_OPPORTUNITY_POLICY_ID:
        return ("Priority", "Usable sources", "Positive sources", "Confirmed sources")
    return ("Priority", "Usable source groups", "Positive source groups")


def opportunity_workbook_values(product):
    available, positive = opportunity_counts(product)
    values = [product["opportunity_priority"], available, positive]
    if product["opportunity_policy_id"] == CURRENT_OPPORTUNITY_POLICY_ID:
        values.append(", ".join(SOURCE_LABELS[source] for source in product["opportunity_basis"]["source_ids"]))
    return values


def opportunity_fields(result):
    """Copy the explicit decision fields into facts and frozen selections."""
    return {field: deepcopy(result[field]) for field in OPPORTUNITY_FIELDS}


def validate_opportunity(product, *, matches=None):
    """Check a marked selected record; do not retroactively mark older packets."""
    if not isinstance(product, dict):
        raise SafetyError("OPPORTUNITY_RECORD_INVALID")
    if not any(field in product for field in OPPORTUNITY_FIELDS):
        return None
    if (not all(field in product for field in OPPORTUNITY_FIELDS)
            or product["opportunity_policy_id"] not in OPPORTUNITY_POLICY_IDS
            or product.get("fit_policy_id") != FIT_POLICY_ID):
        raise SafetyError("OPPORTUNITY_POLICY_INVALID")
    basis = product["opportunity_basis"]
    if product["opportunity_policy_id"] == CURRENT_OPPORTUNITY_POLICY_ID:
        available, positive = _validate_platform_basis(basis)
        cited = {ref for refs in basis["evidence_by_source"].values() for ref in refs}
        if "source_refs" in product:
            refs = product["source_refs"]
            if (not isinstance(refs, list) or any(not isinstance(ref, dict)
                                                or not isinstance(ref.get("id"), str) for ref in refs)
                    or not cited <= {ref.get("id") for ref in refs}):
                raise SafetyError("OPPORTUNITY_SOURCE_REFERENCE_INVALID")
        if "evidence_refs" in product:
            refs = product["evidence_refs"]
            if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs) or not cited <= set(refs):
                raise SafetyError("OPPORTUNITY_SOURCE_REFERENCE_INVALID")
    else:
        if (not isinstance(basis, dict) or set(basis) != {"available_families", "positive_families"}
                or any(type(basis[field]) is not int for field in basis)
                or not 0 <= basis["positive_families"] <= basis["available_families"] <= len(DEMAND_GROUPS)):
            raise SafetyError("OPPORTUNITY_BASIS_INVALID")
        available, positive = basis["available_families"], basis["positive_families"]
    matches = product.get("kpi_matches") if matches is None else matches
    if type(matches) is not int or not 0 <= matches <= 10:
        raise SafetyError("OPPORTUNITY_FIT_INVALID")
    expected = _priority(matches, available, positive)
    if (type(product["opportunity_priority"]) is not int
            or product["opportunity_priority"] not in (1, 2, 3, 4)
            or product["opportunity_priority"] != expected):
        raise SafetyError("OPPORTUNITY_PRIORITY_INVALID")
    return expected


def opportunity_sort_key(product):
    """Shared priority ordering, preserving the old unmarked fit-only order."""
    priority = validate_opportunity(product)
    matches = product["kpi_matches"] if "kpi_matches" in product else product["kpi_states"].count("Meets")
    available, positive = (opportunity_counts(product) if priority is not None
                          else (product.get("available_families", 0), product.get("positive_families", 0)))
    return (priority if priority is not None else 5,
            product.get("fit_tier") == "decent",
            -matches,
            -positive, -available)
