"""Explicit source-backed identity merges before any report allocation.

The operator judges semantic equivalence. Code checks saved dependencies and
concrete conflicts, then appends one correction; it never calls a model or
collects, transfers supplier facts, or rewrites frozen/published reports.
"""

from copy import deepcopy
import re

from .discovery import explicit_conflict
from .brand_reviews import brand_binding, current_brand
from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .research_ai import PRODUCT, checked_schema, text
from .research_memory import change, key
from .service import CATEGORIES
from .storage import Journal


MERGE_VERSION = "unallocated-identity-merge-v1"
_FIELDS = {"survivor_id", "concept_hashes", "association_hashes", "evidence_ids",
           "author", "reason", "reviewed_at"}


def _hashes(values):
    if not isinstance(values, dict) or not values:
        raise SafetyError("IDENTITY_MERGE_REQUEST_INVALID")
    for record_id, value in values.items():
        identifier(record_id)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise SafetyError("IDENTITY_MERGE_REQUEST_INVALID")


def _description(value, *, effective_brand=None):
    checked_schema(value, PRODUCT)
    for field in ("label", "function", "mechanism", "audience", "reason"):
        text(value[field])
    for feature in value["features"]:
        text(feature)
    if value["category"] not in CATEGORIES or (value["brand"] if effective_brand is None else effective_brand) == "established":
        raise SafetyError("IDENTITY_MERGE_CONCEPT_EXCLUDED")


def _request(value):
    if (not isinstance(value, dict) or not _FIELDS <= set(value)
            or set(value) - _FIELDS - {"survivor_description"}):
        raise SafetyError("IDENTITY_MERGE_REQUEST_INVALID")
    request = deepcopy(value)
    identifier(request["survivor_id"])
    _hashes(request["concept_hashes"])
    _hashes(request["association_hashes"])
    members = set(request["concept_hashes"])
    if not 2 <= len(members) <= 20 or request["survivor_id"] not in members:
        raise SafetyError("IDENTITY_MERGE_REQUEST_INVALID")
    text(request["author"])
    text(request["reason"])
    if timestamp(request["reviewed_at"]) > timestamp(utc_now()):
        raise SafetyError("IDENTITY_MERGE_REVIEW_IN_FUTURE")
    evidence = request["evidence_ids"]
    if not isinstance(evidence, dict) or set(evidence) != members:
        raise SafetyError("IDENTITY_MERGE_SOURCE_INVALID")
    for concept_id, values in evidence.items():
        if not isinstance(values, list) or not values:
            raise SafetyError("IDENTITY_MERGE_SOURCE_INVALID")
        for observation_id in values:
            identifier(observation_id)
        if len(set(values)) != len(values):
            raise SafetyError("IDENTITY_MERGE_SOURCE_INVALID")
        evidence[concept_id] = sorted(values)
    if "survivor_description" in request:
        _description(request["survivor_description"])
    if len(canonical(request)) > 1024 * 1024:
        raise SafetyError("IDENTITY_MERGE_REQUEST_INVALID")
    return request


def _links(state, members):
    links = {i: row for i, row in state["associations"].items()
             if row.get("concept_id") in members}
    if any(row.get("status") != "resolved" for row in links.values()):
        raise SafetyError("IDENTITY_MERGE_SOURCE_INVALID")
    return links


def _protected(state, members):
    def touches(concept_id):
        # Existing incoming redirects cannot hide an older report commitment.
        seen = set()
        while concept_id and concept_id not in seen:
            if concept_id in members:
                return True
            seen.add(concept_id)
            concept = state["concepts"].get(concept_id, {})
            concept_id = concept.get("merged_into") if concept.get("status") == "merged" else None
        return False

    if any(row.get("status") == "published" and touches(row.get("concept_id"))
           for row in state["published_history"].values()):
        raise SafetyError("IDENTITY_MERGE_RELEASE_REVIEW_REQUIRED")
    if any(row.get("state") in ("reserved", "uploaded", "published")
           and any(touches(product.get("concept_id")) for product in row.get("products", []))
           for row in state["issue_reservations"].values()):
        raise SafetyError("IDENTITY_MERGE_RELEASE_REVIEW_REQUIRED")


def _functional_review_required(state, members):
    """Legacy merge reviews did not include the new accepted meaning records.

    Keep their raw events readable, but do not use an older descriptor-only
    review to erase a newly reviewed functional boundary. This stops only the
    affected correction; the original concepts remain independently usable.
    A future versioned alias review will carry the actual definition bindings.
    """
    from .research_records import iter_functional_definitions
    if any(record.get("accepted") is True and record.get("concept_id") in members
           for _, record in iter_functional_definitions(state)):
        raise SafetyError("IDENTITY_MERGE_FUNCTIONAL_REVIEW_REQUIRED")


def _sources(state, request, links, *, replay=False):
    """Check current accepted child identities, not title-based equivalence."""
    descriptions, children = {}, []
    members = set(request["concept_hashes"])
    member_semantics = {i: set() for i in members}
    survivor = request["survivor_id"]
    by_semantic = {}
    for observation_id, observation in state["observations"].items():
        by_semantic.setdefault(observation["semantic_id"], {})[observation_id] = observation
    for child_id, association in links.items():
        semantic_id = association.get("semantic_id")
        saved = state["descriptions"].get(semantic_id)
        if not saved:
            raise SafetyError("IDENTITY_MERGE_SOURCE_INVALID")
        result = saved.get("result", {})
        matched = [product for index, product in enumerate(result.get("products", []))
                   if key("child", [semantic_id, index, digest(result)]) == child_id]
        if len(matched) != 1:
            raise SafetyError("IDENTITY_MERGE_DESCRIPTION_CHANGED")
        product = matched[0]
        _description(product, effective_brand=current_brand(state, "child", child_id, product))
        children.append(product)
        descriptions[semantic_id] = digest(saved)
        origin = (association.get("identity_correction", {}).get("previous_concept_id")
                  if replay and association.get("identity_correction", {}).get("review_id") == key("identity-merge", request)
                  else association["concept_id"])
        if origin not in members:
            raise SafetyError("IDENTITY_MERGE_SOURCE_INVALID")
        member_semantics[origin].add(semantic_id)
    category = state["concepts"][survivor]["description"]["category"]
    def check_observation(observation_id, allowed_semantics):
        observation = state["observations"].get(observation_id)
        if not observation or observation.get("semantic_id") not in allowed_semantics:
            raise SafetyError("IDENTITY_MERGE_SOURCE_INVALID")
        semantic_id = observation["semantic_id"]
        capture = state["captures"].get(observation.get("capture_id"))
        if (observation.get("source") not in ("meta", "amazon")
                or key("item", observation.get("semantic")) != semantic_id
                or key("obs", [observation.get("source"), observation.get("acquisition_id"),
                               observation.get("source_item")]) != observation_id
                or not capture or capture.get("source") != observation["source"]
                or capture.get("acquisition_id") != observation["acquisition_id"]
                or capture.get("observed_at") != observation.get("observed_at")
                or timestamp(observation["observed_at"]) > timestamp(request["reviewed_at"])):
            raise SafetyError("IDENTITY_MERGE_SOURCE_INVALID")
    # Every moved child must still have the stable original source that normal
    # checklist execution uses, even if the review names only representative
    # evidence for that member. Do not admit an orphaned association.
    for semantic_id in descriptions:
        observations = by_semantic.get(semantic_id)
        if not observations:
            raise SafetyError("IDENTITY_MERGE_SOURCE_INVALID")
        check_observation(next(iter(observations)), {semantic_id})
    for concept_id, observation_ids in request["evidence_ids"].items():
        if not member_semantics[concept_id]:
            raise SafetyError("IDENTITY_MERGE_SOURCE_INVALID")
        for observation_id in observation_ids:
            check_observation(observation_id, member_semantics[concept_id])
    products = children + [state["concepts"][i]["description"] for i in members]
    if "survivor_description" in request:
        products.append(request["survivor_description"])
    if any(product["category"] != category for product in products):
        raise SafetyError("IDENTITY_MERGE_CATEGORY_CONFLICT")
    if any(explicit_conflict(left, right) for index, left in enumerate(products) for right in products[index + 1:]):
        raise SafetyError("IDENTITY_MERGE_KNOWN_CONFLICT")
    return descriptions


def apply_unallocated_identity_merge(memory, request):
    """Append one reviewed merge of 2–20 source-bound, unallocated concepts.

    Request pins every member record and every resolved association into the
    group. Optional survivor_description is an operator's source-backed generic
    description, not an automatic rewrite. Exact replay verifies the resulting
    bindings and adds no event. Published/held merges and splits are unsupported.
    """
    request = _request(request)
    members, survivor = set(request["concept_hashes"]), request["survivor_id"]
    review_id = key("identity-merge", request)
    with Journal(memory.root).locked():
        state = memory.snapshot()
        concepts = {i: state["concepts"].get(i) for i in members}
        if any(not row for row in concepts.values()):
            raise SafetyError("IDENTITY_MERGE_CONCEPT_REQUIRED")
        existing = concepts[survivor].get("identity_corrections", {}).get(review_id)
        links = _links(state, members)
        if existing:
            canonical_concept = deepcopy(concepts[survivor])
            canonical_concept["identity_corrections"].pop(review_id)
            if not canonical_concept["identity_corrections"] and not existing["survivor_review_field_present"]:
                canonical_concept.pop("identity_corrections")
            current = {"concepts": {i: digest(canonical_concept if i == survivor else row)
                                    for i, row in concepts.items()},
                       "associations": {i: digest(row) for i, row in links.items()}}
            if existing.get("request") != request or current != existing.get("result_bindings"):
                raise SafetyError("IDENTITY_MERGE_INPUT_CHANGED")
            if _sources(state, request, links, replay=True) != existing["description_hashes"]:
                raise SafetyError("IDENTITY_MERGE_DESCRIPTION_CHANGED")
            return {"applied": False, "review_id": review_id, "survivor_id": survivor,
                    "merged_ids": sorted(members - {survivor}), "reassessment_required": True}
        _protected(state, members)
        _functional_review_required(state, members)
        if ({i: digest(row) for i, row in concepts.items()} != request["concept_hashes"]
                or {i: digest(row) for i, row in links.items()} != request["association_hashes"]):
            raise SafetyError("IDENTITY_MERGE_INPUT_CHANGED")
        for concept_id, concept in concepts.items():
            if (concept.get("status") in ("excluded", "merged", "retired")
                    or concept.get("identity_conflicted") or concept.get("merged_into")):
                raise SafetyError("IDENTITY_MERGE_CONCEPT_EXCLUDED")
            _description(concept["description"], effective_brand=current_brand(state, "concept", concept_id, concept["description"]))
        description_hashes = _sources(state, request, links)
        brands = {i: brand_binding(state, "concept", i, concepts[i]["description"]) for i in members}
        child_brands = {}
        for child_id, association in links.items():
            products = state["descriptions"][association["semantic_id"]]["result"]
            description = next(product for index, product in enumerate(products["products"])
                               if key("child", [association["semantic_id"], index, digest(products)]) == child_id)
            child_brands[child_id] = brand_binding(state, "child", child_id, description)
        def unchanged(current):
            _protected(current, members)
            _functional_review_required(current, members)
            current_links = _links(current, members)
            if ({i: digest(current["concepts"].get(i)) for i in members} != request["concept_hashes"]
                    or {i: digest(row) for i, row in current_links.items()} != request["association_hashes"]
                    or _sources(current, request, current_links) != description_hashes
                    or any(brand_binding(current, "concept", i, current["concepts"][i]["description"]) != brands[i] for i in members)):
                raise SafetyError("IDENTITY_MERGE_INPUT_CHANGED")
            for child_id, association in current_links.items():
                products = current["descriptions"][association["semantic_id"]]["result"]
                description = next(product for index, product in enumerate(products["products"])
                                   if key("child", [association["semantic_id"], index, digest(products)]) == child_id)
                if brand_binding(current, "child", child_id, description) != child_brands[child_id]:
                    raise SafetyError("IDENTITY_MERGE_INPUT_CHANGED")
        updated = deepcopy(concepts)
        if "survivor_description" in request:
            updated[survivor]["description"] = deepcopy(request["survivor_description"])
        for concept_id in members - {survivor}:
            updated[concept_id].update(status="merged", merged_into=survivor, identity_correction=review_id)
        reassigned = deepcopy(links)
        for child_id, association in reassigned.items():
            if association["concept_id"] != survivor:
                previous = links[child_id]
                association.update(concept_id=survivor, reason=request["reason"], method="operator_source_review",
                                   identity_correction={"review_id": review_id,
                                     "previous_concept_id": previous["concept_id"],
                                     "previous_reason": previous.get("reason"),
                                     "previous_method": previous.get("method"),
                                     "previous_job_id": previous.get("job_id")})
        review = {"version": MERGE_VERSION, "method": "operator_source_review", "request": request,
                  "survivor_review_field_present": "identity_corrections" in concepts[survivor],
                  "description_hashes": description_hashes,
                  "result_bindings": {"concepts": {i: digest(row) for i, row in updated.items()},
                                      "associations": {i: digest(row) for i, row in reassigned.items()}}}
        updated[survivor].setdefault("identity_corrections", {})[review_id] = review
        changes = [change("concepts", i, updated[i], concepts[i]) for i in sorted(members)]
        changes += [change("associations", i, reassigned[i], links[i])
                    for i in sorted(links) if reassigned[i] != links[i]]
        memory.commit(review_id, changes, validate=unchanged)
    return {"applied": True, "review_id": review_id, "survivor_id": survivor,
            "merged_ids": sorted(members - {survivor}), "reassessment_required": True}
