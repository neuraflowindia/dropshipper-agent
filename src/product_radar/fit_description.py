"""Explicit description-grounded fit estimates, never market observations.

This attribution names the exact saved description and the fields the model
actually selected. It does not create an observation, verify a measurement or
allow a cached historical answer to acquire a new meaning silently.
"""

import re

from .models import SafetyError, digest


DESCRIPTION_INFERENCE_KIND = "product-description-inference-v1"
DESCRIPTION_CONTRACT = "research-e-fit10-description-v3"
DESCRIPTION_STYLE = "practical-description-estimates-v3"
DESCRIPTION_FIELDS = ("label", "function", "mechanism", "features", "audience")
FUNCTIONAL_CONTRACT = "research-e-fit10-functional-v4"
FUNCTIONAL_INFERENCE_KIND = "functional-type-inference-v1"
FUNCTIONAL_STYLE = "practical-functional-estimates-v4"
FUNCTIONAL_RESPONSE_VERSION = "code-owned-functional-fields-v5"
FUNCTIONAL_SELECTION_KIND = "functional-field-selection-v1"
FIT_ESTIMATE_VALIDATION_VERSION = "practical-estimate-validation-v2"
FUNCTIONAL_FIELDS = ("primary_use", "mechanism", "use_context", "compatibility_constraints",
                     "included_distinctions", "excluded_distinctions", "example_features", "example_audiences")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_MEASUREMENT = re.compile(
    r"(?:\d+(?:[.,]\d+)?\s*(?:%|percent\b|per\s+cent\b|kg\b|kilograms?\b|grams?\b|"
    r"cm\b|centimet(?:er|re)s?\b|mm\b|millimet(?:er|re)s?\b|met(?:er|re)s?\b|"
    r"inches\b|lbs?\b|pounds?\b|minutes?\b|mins?\b|seconds?\b|secs?\b|hours?\b|"
    r"rupees?\b|dollars?\b|sales\b|orders\b)|[₹$€£]\s*\d)", re.IGNORECASE,
)
_COMMERCIAL_NUMBER = re.compile(
    r"(?:[₹$€£]\s*\d|\d+(?:[.,]\d+)?\s*(?:rupees?\b|dollars?\b|sales\b|orders\b)|"
    r"\b(?:sales|orders|profit|margin|revenue|demand|growth)\b[^.;!?\n]{0,35}\d|"
    r"\d[^.;!?\n]{0,35}\b(?:sales|orders|profit|margin|revenue|demand|growth)\b)", re.I)
_APPROXIMATE = re.compile(r"\b(?:estimat(?:ed|e)|about|around|roughly|approximately|approx|likely|typical|projected|ballpark)\b", re.I)
_MEASURED = re.compile(r"\b(?:measured|verified|guaranteed|exactly|tested|confirmed)\b", re.I)


def _valid_estimate_numbers(reason, description, fields, functional):
    """Allow described specifications and practical estimates, not sales math.

    This is an output-quality check, not a measurement or semantic proof.
    The structured inferred basis owns the displayed estimate label.
    """
    if _COMMERCIAL_NUMBER.search(reason):
        return False
    measurements = list(_MEASUREMENT.finditer(reason))
    if not measurements:
        return True
    selected = [entry["field"] for entry in fields] if functional else fields
    supplied = " ".join(str(description[field]) for field in selected).casefold()
    compact_supplied = re.sub(r"\s+", "", supplied)
    for match in measurements:
        clause_start = max(reason.rfind(char, 0, match.start()) for char in ".;!?\n") + 1
        ends = [position for char in ".;!?\n" if (position := reason.find(char, match.end())) >= 0]
        clause = reason[clause_start:min(ends) if ends else len(reason)]
        stated = re.sub(r"\s+", "", match.group().casefold()) in compact_supplied
        if _MEASURED.search(clause) and not stated:
            return False
        if not stated and not _APPROXIMATE.search(clause):
            return False
    return True


def display_fit_reason(row):
    """Presentation only. Never rewrite the saved model answer."""
    reason = row["reason"]
    return "Estimated: " + reason if row.get("basis") == "inferred" and not reason.casefold().startswith("estimated:") else reason


def _populated(description, field):
    value = description.get(field)
    if field == "features":
        return (isinstance(value, list) and 0 < len(value) <= 100
                and all(isinstance(item, str) and item.strip() and len(item) <= 2000 for item in value))
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2000


def description_inference_context(description):
    """Supply one immutable basis; the model must choose its own actual fields."""
    if not isinstance(description, dict) or not description:
        raise SafetyError("FIT_DESCRIPTION_INPUT_INVALID")
    fields = [field for field in DESCRIPTION_FIELDS if _populated(description, field)]
    if not fields:
        raise SafetyError("FIT_DESCRIPTION_INPUT_INVALID")
    return {"kind": DESCRIPTION_INFERENCE_KIND, "description_hash": digest(description),
            "allowed_fields": fields}


def validate_description_context(description, context):
    if context != description_inference_context(description):
        raise SafetyError("FIT_DESCRIPTION_CONTEXT_INVALID")
    return context


def functional_inference_context(description):
    """Meaning and selected field hashes, not presentation or citation dates."""
    from .functional_types import MEANING_FIELDS, definition_meaning_hash
    if not isinstance(description, dict) or set(description) != set(FUNCTIONAL_FIELDS):
        raise SafetyError("FIT_FUNCTIONAL_INPUT_INVALID")
    definition = {field: description[field] for field in MEANING_FIELDS}
    # Reuse the definition owner's strict shape validator through its hash.
    meaning_hash = definition_meaning_hash(dict(definition, report_title="Functional type"))
    for field in ("example_features", "example_audiences"):
        value = description[field]
        if (not isinstance(value, list) or len(value) > 1000
                or any(not isinstance(entry, str) or not entry.strip() or len(entry) > 2000 for entry in value)
                or value != sorted(set(value))):
            raise SafetyError("FIT_FUNCTIONAL_INPUT_INVALID")
    return {"kind": FUNCTIONAL_INFERENCE_KIND, "meaning_hash": meaning_hash,
            "allowed_fields": [{"field": field, "hash": digest(description[field])}
                               for field in FUNCTIONAL_FIELDS if description[field]]}


def validate_functional_context(description, context):
    if context != functional_inference_context(description):
        raise SafetyError("FIT_FUNCTIONAL_CONTEXT_INVALID")
    return context


def functional_changed_criteria(parent, item):
    """Small exact reopening set; a changed type meaning always reopens ten."""
    from .fit_policy import ACTIVE_CRITERION_IDS
    if not parent or parent.get("contract_version") != FUNCTIONAL_CONTRACT:
        return list(ACTIVE_CRITERION_IDS)
    old = parent.get("input")
    if (not isinstance(old, dict) or parent.get("input_hash") != digest(old)
            or parent.get("record_hash") != digest({key: value for key, value in parent.items() if key != "record_hash"})
            or parent.get("functional_proof_hash") != digest(parent.get("functional_proof"))
            or old.get("meaning_hash") != item.get("meaning_hash")):
        return list(ACTIVE_CRITERION_IDS)
    changed_fields = {field for field in FUNCTIONAL_FIELDS
                      if old.get("description", {}).get(field) != item["description"][field]}
    affected = set()
    if "example_audiences" in changed_fields:
        affected.add(13)
    if "example_features" in changed_fields:
        affected.update((1, 2))
    old_refs = {row["id"]: row for row in old.get("observations", [])}
    new_refs = {row["id"]: row for row in item["observations"]}
    changed_refs = {ref for ref in old_refs.keys() | new_refs.keys() if old_refs.get(ref) != new_refs.get(ref)}
    facets = {row["source_text"]["field"] for ref in changed_refs
              for row in (old_refs.get(ref), new_refs.get(ref)) if row is not None}
    mapping = {"weight": (1, 2), "dimensions": (1, 2), "audience": (13,), "age": (13,),
               "material": (1, 2, 6, 11, 12), "size": (1, 2, 6), "compatibility": (1, 2, 6, 8)}
    for facet in facets:
        affected.update(mapping.get(facet, ACTIVE_CRITERION_IDS))
    for row in parent.get("result", {}).get("criteria", []):
        marker = row.get("description_inference") or {}
        used = {entry.get("field") for entry in marker.get("fields", []) if isinstance(entry, dict)}
        if used & changed_fields or set(row.get("references", [])) & changed_refs:
            affected.add(row["id"])
    return [number for number in ACTIVE_CRITERION_IDS if number in affected]


def validate_description_inference(row, description, *, description_contract=None):
    """Validate a separate estimate basis against an explicitly supplied owner.

    A null/absent marker remains the original observation-reference contract.
    A copied hash alone is not sufficient: the caller supplies the current full
    description and expressly opts into this contract.
    """
    from .fit_policy import ACTIVE_CRITERION_IDS
    marker = row.get("description_inference")
    if marker is None:
        return False
    functional = description_contract == FUNCTIONAL_INFERENCE_KIND
    if description_contract not in (DESCRIPTION_INFERENCE_KIND, FUNCTIONAL_INFERENCE_KIND):
        raise SafetyError("FIT_DESCRIPTION_CONTRACT_REQUIRED")
    if description is None:
        raise SafetyError("FIT_DESCRIPTION_CONTEXT_REQUIRED")
    if functional:
        context = functional_inference_context(description)
        if (not isinstance(marker, dict) or set(marker) != {"kind", "meaning_hash", "fields"}
                or marker.get("kind") != FUNCTIONAL_INFERENCE_KIND
                or marker.get("meaning_hash") != context["meaning_hash"]):
            raise SafetyError("FIT_DESCRIPTION_BINDING_INVALID")
        fields = marker["fields"]
        if (not isinstance(fields, list) or not fields or len(fields) > len(FUNCTIONAL_FIELDS)
                or any(not isinstance(entry, dict) or entry not in context["allowed_fields"] for entry in fields)
                or len({entry["field"] for entry in fields}) != len(fields)):
            raise SafetyError("FIT_DESCRIPTION_FIELDS_INVALID")
    elif (not isinstance(marker, dict) or set(marker) != {"kind", "description_hash", "fields"}
            or marker.get("kind") != DESCRIPTION_INFERENCE_KIND
            or not isinstance(marker.get("description_hash"), str)
            or not _HASH.fullmatch(marker["description_hash"])
            or marker["description_hash"] != digest(description)):
        raise SafetyError("FIT_DESCRIPTION_BINDING_INVALID")
    if not functional:
        context = description_inference_context(description)
        fields = marker["fields"]
        if (not isinstance(fields, list) or not 1 <= len(fields) <= len(DESCRIPTION_FIELDS)
                or any(not isinstance(field, str) or field not in context["allowed_fields"] for field in fields)
                or len(set(fields)) != len(fields)):
            raise SafetyError("FIT_DESCRIPTION_FIELDS_INVALID")
    reason = row.get("reason")
    if (type(row.get("id")) is not int or row["id"] not in ACTIVE_CRITERION_IDS
            or row.get("state") not in ("Meets", "Does not meet") or row.get("basis") != "inferred"
            or row.get("references") != [] or not isinstance(reason, str)
            or not reason.strip() or len(reason) > 2000
            or not _valid_estimate_numbers(reason, description, fields, functional)):
        raise SafetyError("FIT_DESCRIPTION_INFERENCE_INVALID")
    return True
