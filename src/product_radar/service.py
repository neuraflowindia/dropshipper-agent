"""Offline service rules. No collection, inference, publication or credentials."""

from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
import re

from .models import SafetyError, canonical, identifier, usd
from .storage import load_json


CATEGORIES = ("Home and Decor", "Baby Products", "Home and Kitchen", "Travel and Outdoor")
CONFIG = Path(__file__).resolve().parents[2] / "config/service.json"


def load_policy(path=CONFIG):
    return _validate_policy(load_json(path))


def _validate_policy(policy):
    expected = {
        "schema_version": 1, "categories": list(CATEGORIES), "timezone": "Asia/Kolkata",
        "publication_weekdays": [1, 3, 5], "publication_time": None,
        "minimum_products": 10, "target_products": 15, "maximum_products": 20,
        "minimum_kpi_matches": 7, "maximum_update_fraction": "0.20",
        "apify_period_limit_usd": "4.50", "apify_protected_buffer_usd": "0.50",
        "apify_penny_tolerance_usd": "0.05",
        "initial_trial_limit_usd": "0.75", "initial_future_allocation_usd": "3.75",
        "bilingual_issue_count": 2, "trial_pdf_languages": ["en", "hi-Latn"],
        "ongoing_pdf_language": None, "shared_excel": True,
    }
    # Policy changes are reviewed code/config changes, not silent fallbacks.
    if not isinstance(policy, dict):
        raise SafetyError("SERVICE_POLICY_INVALID")
    if type(policy.get("schema_version")) is int and policy["schema_version"] == 3:
        expected.update(schema_version=3, minimum_kpi_matches=5,
                        fit_policy_id="fit-10-v1", good_fit_kpi_matches=6)
    if set(policy) != set(expected):
        raise SafetyError("SERVICE_POLICY_INVALID")
    if type(policy.get("schema_version")) is int and policy["schema_version"] in (2, 3):
        release_time = policy["publication_time"]
        language = policy["ongoing_pdf_language"]
        if release_time is not None and (not isinstance(release_time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", release_time)):
            raise SafetyError("SERVICE_PUBLICATION_TIME_INVALID")
        if language is not None and language not in ("en", "hi-Latn"):
            raise SafetyError("SERVICE_LANGUAGE_INVALID")
        expected.update(schema_version=policy["schema_version"], publication_time=release_time, ongoing_pdf_language=language)
    if canonical(policy) != canonical(expected):
        raise SafetyError("SERVICE_POLICY_REVIEW_REQUIRED")
    return policy


def policy_for_fit(fit_policy_id):
    """Reviewed historical/current rules; no arbitrary thresholds from files."""
    policy = dict(load_policy())
    if fit_policy_id == "fit-13-v1":
        policy.pop("fit_policy_id", None)
        policy.pop("good_fit_kpi_matches", None)
        policy.update(schema_version=2, minimum_kpi_matches=7)
    elif fit_policy_id == "fit-10-v1":
        policy.update(schema_version=3, minimum_kpi_matches=5,
                      fit_policy_id=fit_policy_id, good_fit_kpi_matches=6)
    else:
        raise SafetyError("FIT_POLICY_UNSUPPORTED")
    return _validate_policy(policy)


def product_fit(product, expected_policy_id=None):
    """Recompute protected counts; unmarked thirteen rows mean legacy only."""
    from .fit_policy import evaluate_fit, policy_for
    if not isinstance(product, dict):
        raise SafetyError("ISSUE_PRODUCT_INVALID")
    declared = product.get("fit_policy_id", "fit-13-v1")
    if "fit_description_contract" in product:
        from .fit_description import DESCRIPTION_INFERENCE_KIND, FUNCTIONAL_INFERENCE_KIND
        from .report_examples import RECOMMENDATION_CONTRACT
        expected = FUNCTIONAL_INFERENCE_KIND if product.get("recommendation_contract") == RECOMMENDATION_CONTRACT else DESCRIPTION_INFERENCE_KIND
        if declared != "fit-10-v1" or product["fit_description_contract"] != expected:
            raise SafetyError("ISSUE_FIT_DESCRIPTION_CONTRACT_INVALID")
    if expected_policy_id is not None and declared != expected_policy_id:
        raise SafetyError("ISSUE_FIT_POLICY_MISMATCH")
    reviewed = policy_for(declared)
    states = product.get("kpi_states")
    if (not isinstance(states, list) or len(states) != reviewed["total"]
            or any(state not in ("Meets", "Does not meet", "Unclear", "Not assessed") for state in states)):
        raise SafetyError("ISSUE_KPI_REQUIREMENTS_NOT_MET")
    if declared == "fit-13-v1":
        if states[2] != "Not assessed" or any(field in product for field in ("criterion_ids", "kpi_total", "fit_tier")):
            raise SafetyError("ISSUE_KPI_REQUIREMENTS_NOT_MET")
    elif (product.get("criterion_ids") != list(reviewed["active_ids"])
          or any(type(number) is not int for number in product["criterion_ids"])
          or type(product.get("kpi_total")) is not int or product["kpi_total"] != reviewed["total"]):
        raise SafetyError("ISSUE_KPI_REQUIREMENTS_NOT_MET")
    # Rows here are protected states, not source judgments. Fit arithmetic is
    # still code-owned; semantic rows are separately validated upstream.
    rows = [{"id": criterion_id, "state": state} for criterion_id, state in zip(reviewed["active_ids"], states)]
    fit = evaluate_fit(rows, declared, states_only=True)
    if declared != "fit-13-v1" and (product.get("fit_tier") != fit["fit_tier"]
            or type(product.get("kpi_matches")) is not int or product["kpi_matches"] != fit["kpi_matches"]):
        raise SafetyError("ISSUE_KPI_REQUIREMENTS_NOT_MET")
    from .opportunity_policy import validate_opportunity
    validate_opportunity(product, matches=fit["kpi_matches"])
    return fit


def issue_fit_policy(issue):
    """Bind the frozen header to one actual product policy, including legacy."""
    products = issue.get("products") if isinstance(issue, dict) else None
    if not isinstance(products, list) or not products or any(not isinstance(product, dict) for product in products):
        raise SafetyError("ISSUE_FIT_POLICY_MISMATCH")
    declared = [product.get("fit_policy_id", "fit-13-v1") for product in products]
    if any(not isinstance(value, str) for value in declared) or len(set(declared)) != 1:
        raise SafetyError("ISSUE_FIT_POLICY_MISMATCH")
    policy_id = declared[0]
    from .report_examples import RECOMMENDATION_CONTRACT
    contracts = [product.get("recommendation_contract") for product in products]
    if any(value is not None for value in contracts) or "recommendation_contract" in issue:
        if (policy_id != "fit-10-v1" or issue.get("recommendation_contract") != RECOMMENDATION_CONTRACT
                or any(value != RECOMMENDATION_CONTRACT for value in contracts)):
            raise SafetyError("ISSUE_RECOMMENDATION_CONTRACT_INVALID")
    if policy_id == "fit-13-v1":
        if any(field in issue for field in ("fit_policy_id", "criterion_ids", "kpi_total")):
            raise SafetyError("ISSUE_FIT_POLICY_MISMATCH")
    elif policy_id == "fit-10-v1":
        from .fit_policy import ACTIVE_CRITERION_IDS
        ids = issue.get("criterion_ids")
        if (issue.get("fit_policy_id") != policy_id or ids != list(ACTIVE_CRITERION_IDS)
                or any(type(number) is not int for number in ids)
                or type(issue.get("kpi_total")) is not int or issue["kpi_total"] != 10):
            raise SafetyError("ISSUE_FIT_POLICY_MISMATCH")
    else:
        raise SafetyError("ISSUE_FIT_POLICY_MISMATCH")
    from .opportunity_policy import OPPORTUNITY_POLICY_IDS, validate_opportunity
    priorities = [validate_opportunity(product) for product in products]
    marked = [priority is not None for priority in priorities]
    if any(marked):
        policy_ids = {product.get("opportunity_policy_id") for product in products}
        if (not all(marked) or len(policy_ids) != 1 or issue.get("opportunity_policy_id") not in OPPORTUNITY_POLICY_IDS
                or issue["opportunity_policy_id"] != next(iter(policy_ids))):
            raise SafetyError("ISSUE_OPPORTUNITY_POLICY_MISMATCH")
    elif "opportunity_policy_id" in issue:
        raise SafetyError("ISSUE_OPPORTUNITY_POLICY_MISMATCH")
    return policy_id


def calendar_plan(start, end, available_usd, policy=None):
    """Local publication dates in [start,end); envelopes, not billing resets.

    Caller supplies the remaining amount AFTER holds/tests/future commitments.
    Collection Monday can fall outside the date window; it is exposed, never
    silently assigned new-period funds. The configured release time alone does
    not enable publication or prove that the hosted route is ready.
    """
    policy = load_policy() if policy is None else _validate_policy(policy)
    try:
        first, stop = date.fromisoformat(start), date.fromisoformat(end)
    except (ValueError, TypeError):
        raise SafetyError("CALENDAR_RANGE_INVALID") from None
    if not 0 < (stop - first).days <= 366:
        raise SafetyError("CALENDAR_RANGE_INVALID")
    amount = usd(available_usd)
    if amount > usd(policy["apify_period_limit_usd"]):
        raise SafetyError("CALENDAR_BUDGET_EXCEEDS_CAP")
    dates = [first + timedelta(days=n) for n in range((stop-first).days)
             if (first + timedelta(days=n)).weekday() in policy["publication_weekdays"]]
    share = (amount / len(dates)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN) if dates else Decimal(0)
    issues = [{"date": d.isoformat(), "collection_monday": (d-timedelta(days=d.weekday())).isoformat(),
               "collection_before_window": d-timedelta(days=d.weekday()) < first,
               "allocation_usd": str(share)} for d in dates]
    return {"timezone": policy["timezone"], "publication_time": policy["publication_time"],
            "issues": issues, "unallocated_usd": str(amount-share*len(dates)),
            "minimum_entries": len(dates)*policy["minimum_products"],
            "minimum_new": len(dates)*8, "ready_for_scheduling": False}


def validate_issue(products, published_ids=(), queued_ids=(), policy=None):
    """Validate identity decisions already checked by research/operator.

    Passing this gate alone does not certify market evidence, supplier search,
    layout, approval or publication. A canonical ID must not be a seller SKU.
    """
    explicit_policy = policy is not None
    policy = load_policy() if policy is None else _validate_policy(policy)
    if not isinstance(products, list) or not policy["minimum_products"] <= len(products) <= policy["maximum_products"]:
        raise SafetyError("ISSUE_PRODUCT_COUNT_REVIEW_REQUIRED")
    published, queued, seen = set(published_ids), set(queued_ids), set()
    for value in published | queued:
        identifier(value)
    updates = 0
    declarations = [product.get("fit_policy_id", "fit-13-v1") for product in products if isinstance(product, dict)]
    if any(not isinstance(value, str) for value in declarations):
        raise SafetyError("ISSUE_FIT_POLICY_MISMATCH")
    declared_policies = set(declarations)
    if len(declared_policies) != 1:
        raise SafetyError("ISSUE_FIT_POLICY_MISMATCH")
    from .opportunity_policy import OPPORTUNITY_FIELDS
    marked = [any(field in product for field in OPPORTUNITY_FIELDS) for product in products]
    if any(marked) and not all(marked):
        raise SafetyError("ISSUE_OPPORTUNITY_POLICY_MISMATCH")
    if all(marked) and len({product.get("opportunity_policy_id") for product in products}) != 1:
        raise SafetyError("ISSUE_OPPORTUNITY_POLICY_MISMATCH")
    expected_policy = policy.get("fit_policy_id", "fit-13-v1") if explicit_policy else next(iter(declared_policies))
    for product in products:
        if not isinstance(product, dict):
            raise SafetyError("ISSUE_PRODUCT_INVALID")
        concept = identifier(product.get("concept_id"))
        if concept in seen or concept in queued:
            raise SafetyError("ISSUE_DUPLICATE_OR_QUEUED_PRODUCT")
        seen.add(concept)
        if product.get("category") not in policy["categories"] or product.get("identity_reviewed") is not True:
            raise SafetyError("ISSUE_IDENTITY_CATEGORY_REVIEW_REQUIRED")
        fit = product_fit(product, expected_policy)
        if not fit["eligible"]:
            raise SafetyError("ISSUE_KPI_REQUIREMENTS_NOT_MET")
        expected = "Updated" if concept in published else "New"
        if product.get("status") != expected:
            raise SafetyError("ISSUE_NOVELTY_MISMATCH")
        if expected == "Updated":
            reason = product.get("update_reason")
            if product.get("material_update_reviewed") is not True or not isinstance(reason, str) or not reason.strip():
                raise SafetyError("ISSUE_UPDATE_EVIDENCE_REQUIRED")
            updates += 1
    if updates > len(products)//5:
        raise SafetyError("ISSUE_TOO_MANY_UPDATES")
    return {"products": len(products), "new": len(products)-updates, "updates": updates}


def pdf_languages(published_issue_number, selected_language=None, policy=None):
    policy = load_policy() if policy is None else _validate_policy(policy)
    if type(published_issue_number) is not int or published_issue_number < 1:
        raise SafetyError("ISSUE_NUMBER_INVALID")
    if published_issue_number <= policy["bilingual_issue_count"]:
        return tuple(policy["trial_pdf_languages"])
    if selected_language is None:
        selected_language = policy["ongoing_pdf_language"]
    if selected_language not in policy["trial_pdf_languages"]:
        raise SafetyError("REPORT_LANGUAGE_DECISION_REQUIRED")
    return (selected_language,)
