"""Reviewed product-fit policies and code-owned counts, with no model calls.

The original criterion IDs are stable. Missing information is not a negative
judgment, and a policy change never changes a product's identity or evidence.
"""

from .models import SafetyError

FIT_POLICY_ID = "fit-10-v1"
LEGACY_POLICY_ID = "fit-13-v1"
ACTIVE_CRITERION_IDS = (1, 2, 5, 6, 7, 8, 9, 11, 12, 13)
RETIRED_CRITERION_IDS = (3, 4, 10)
STATES = ("Meets", "Does not meet", "Unclear", "Not assessed")
BASES = ("observed", "inferred", "not_stated", "conflicting")


def policy_for(policy_id):
    """Return only a reviewed policy; callers cannot supply new thresholds."""
    if policy_id == FIT_POLICY_ID:
        return {"fit_policy_id": FIT_POLICY_ID, "active_ids": ACTIVE_CRITERION_IDS,
                "retired_ids": RETIRED_CRITERION_IDS, "total": 10,
                "minimum_matches": 5, "good_matches": 6}
    if policy_id == LEGACY_POLICY_ID:
        return {"fit_policy_id": LEGACY_POLICY_ID, "active_ids": tuple(range(1, 14)),
                "retired_ids": (), "total": 13, "minimum_matches": 7,
                "good_matches": 7}
    raise SafetyError("FIT_POLICY_UNKNOWN")


def policy_id_for(record, *, schema_version=None):
    """Absent policy markers are legacy only at a declared legacy boundary."""
    if not isinstance(record, dict):
        raise SafetyError("FIT_POLICY_RECORD_INVALID")
    if "fit_policy_id" not in record:
        version = record.get("schema_version") if schema_version is None else schema_version
        if type(version) is not int or version != 1:
            raise SafetyError("FIT_POLICY_MARKER_REQUIRED")
        return LEGACY_POLICY_ID
    policy_id = record["fit_policy_id"]
    policy_for(policy_id)
    return policy_id


def ordered_criteria(criteria, policy_id, *, description=None, description_contract=None):
    """Validate exactly one judgment per original active ID, then order it.

Extra descriptive metadata is permitted for existing display records. Model
totals are never read. Current positive and negative judgments need an actual
observed/inferred basis and the source references on which they rely.
"""
    policy = policy_for(policy_id)
    if not isinstance(criteria, list) or len(criteria) != policy["total"]:
        raise SafetyError("FIT_CRITERION_COVERAGE_INVALID")
    rows = {}
    for row in criteria:
        if (not isinstance(row, dict) or not {"id", "state", "basis", "references", "reason"} <= set(row)
                or type(row["id"]) is not int or row["id"] not in policy["active_ids"]
                or row["id"] in rows):
            raise SafetyError("FIT_CRITERION_ID_INVALID")
        if row["state"] not in STATES or row["basis"] not in BASES:
            raise SafetyError("FIT_CRITERION_STATE_INVALID")
        if not isinstance(row["reason"], str) or not row["reason"].strip() or len(row["reason"]) > 2000:
            raise SafetyError("FIT_CRITERION_REASON_INVALID")
        refs = row["references"]
        if (not isinstance(refs, list) or len(refs) > 100
                or any(not isinstance(ref, str) or not ref.strip() or len(ref) > 2000 for ref in refs)
                or len(set(refs)) != len(refs)):
            raise SafetyError("FIT_CRITERION_REFERENCE_INVALID")
        from .fit_description import validate_description_inference
        description_supported = validate_description_inference(
            row, description, description_contract=description_contract)
        if description_supported and policy_id != FIT_POLICY_ID:
            raise SafetyError("FIT_DESCRIPTION_POLICY_INVALID")
        supported = (row["basis"] in ("observed", "inferred") and bool(refs)) or description_supported
        if row["state"] == "Meets" and not supported:
            raise SafetyError("FIT_UNSUPPORTED_MEETS")
        if policy_id == FIT_POLICY_ID and row["state"] == "Does not meet" and not supported:
            raise SafetyError("FIT_UNSUPPORTED_NEGATIVE")
        if policy_id == LEGACY_POLICY_ID and row["id"] == 3 and row["state"] != "Not assessed":
            raise SafetyError("FIT_LEGACY_MARGIN_INVALID")
        rows[row["id"]] = row
    if set(rows) != set(policy["active_ids"]):
        raise SafetyError("FIT_CRITERION_COVERAGE_INVALID")
    return [rows[number] for number in policy["active_ids"]]


def evaluate_fit(criteria, policy_id, *, states_only=False, description=None, description_contract=None):
    """Count supported judgments once. Unknowns never become failures."""
    policy = policy_for(policy_id)
    if states_only:
        if (not isinstance(criteria, list) or len(criteria) != policy["total"]
                or any(not isinstance(row, dict) or set(row) != {"id", "state"}
                       or type(row["id"]) is not int or row["id"] not in policy["active_ids"]
                       or row["state"] not in STATES for row in criteria)
                or len({row["id"] for row in criteria}) != policy["total"]):
            raise SafetyError("FIT_CRITERION_COVERAGE_INVALID")
        rows = criteria
    else:
        rows = ordered_criteria(criteria, policy_id, description=description,
                                description_contract=description_contract)
    meets = sum(row["state"] == "Meets" for row in rows)
    negative = sum(row["state"] == "Does not meet" for row in rows)
    unknown = sum(row["state"] in ("Unclear", "Not assessed") for row in rows)
    eligible = meets >= policy["minimum_matches"]
    tier = "good" if meets >= policy["good_matches"] else "decent" if eligible else "below"
    reason = ("fit_passes" if eligible else "fit_information_pending"
              if meets + unknown >= policy["minimum_matches"] else "below_fit_threshold")
    return {"fit_policy_id": policy_id, "criterion_ids": list(policy["active_ids"]),
            "kpi_total": policy["total"], "kpi_matches": meets,
            "does_not_meet": negative, "unknown": unknown,
            "fit_tier": tier, "eligible": eligible, "reason": reason}
