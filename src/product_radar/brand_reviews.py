"""Source-bound brand judgments without rewriting accepted product identities.

Ordinary A judgments remain the default. A named correction can change one
eligibility verdict; it cannot clear category, identity or publication rules.
"""

from copy import deepcopy

from .models import SafetyError, digest, identifier, timestamp, utc_now
from .research_memory import change, key

VERSION = "brand-overlay-v1"
VERDICTS = ("generic_or_small", "established", "unclear")


def _target(state, kind, target_id):
    if kind == "concept":
        concept = state["concepts"].get(target_id)
        if not concept:
            raise SafetyError("BRAND_REVIEW_TARGET_REQUIRED")
        description = concept["description"]
        links = {i: a for i, a in state["associations"].items()
                 if a.get("concept_id") == target_id and a.get("status") == "resolved"}
        semantics = {a["semantic_id"] for a in links.values()}
        return description, None, links, semantics
    if kind != "child":
        raise SafetyError("BRAND_REVIEW_TARGET_INVALID")
    for semantic_id, saved in state["descriptions"].items():
        result = saved.get("result", {})
        for index, description in enumerate(result.get("products", [])):
            if key("child", [semantic_id, index, digest(result)]) == target_id:
                association = state["associations"].get(target_id)
                return description, digest(result), ({target_id: association} if association else {}), {semantic_id}
    raise SafetyError("BRAND_REVIEW_TARGET_REQUIRED")


def _sources(state, semantics, evidence_ids):
    if (not isinstance(evidence_ids, list) or not evidence_ids
            or len(evidence_ids) > 20 or any(not isinstance(value, str) for value in evidence_ids)
            or len(set(evidence_ids)) != len(evidence_ids)):
        raise SafetyError("BRAND_REVIEW_SOURCE_INVALID")
    result = {}
    for observation_id in sorted(evidence_ids):
        identifier(observation_id)
        observation = state["observations"].get(observation_id)
        if not observation or observation.get("semantic_id") not in semantics:
            raise SafetyError("BRAND_REVIEW_SOURCE_INVALID")
        result[observation_id] = digest(observation)
    return result


def prepare_brand_review(state, target_kind, target_id, *, verdict, reason,
                         evidence_ids, reviewer, reviewed_at):
    """Freeze one source-reviewed correction for explicit application."""
    identifier(target_id); identifier(reviewer)
    if verdict not in VERDICTS or not isinstance(reason, str) or not reason.strip() or len(reason) > 4096:
        raise SafetyError("BRAND_REVIEW_INVALID")
    timestamp(reviewed_at)
    if timestamp(reviewed_at) > timestamp(utc_now()):
        raise SafetyError("BRAND_REVIEW_IN_FUTURE")
    description, parent_hash, links, semantics = _target(state, target_kind, target_id)
    sources = _sources(state, semantics, evidence_ids)
    if any(timestamp(state["observations"][record_id]["observed_at"]) > timestamp(reviewed_at) for record_id in sources):
        raise SafetyError("BRAND_REVIEW_PREDATES_EVIDENCE")
    try:
        binding = brand_binding(state, target_kind, target_id, description)
    except SafetyError as error:
        if str(error) != "BRAND_ORIGIN_BINDING_CHANGED" or target_kind != "concept":
            raise
        # A named source-reviewed concept correction is the repair route for
        # a stale creation-origin link; it does not quietly restore that link.
        binding = {"review_id": state["concepts"][target_id].get("origin_brand_review_id")}
    return {"version": VERSION, "target_kind": target_kind, "target_id": target_id,
            "description_hash": digest(description), "parent_description_hash": parent_hash,
            "association_hashes": {i: digest(row) for i, row in links.items()},
            "source_hashes": sources,
            "verdict": verdict, "reason": reason.strip(), "reviewer": reviewer,
            "reviewed_at": reviewed_at, "supersedes": binding["review_id"]}


def _compatible(state, review, kind, target_id, description):
    """Check immutable description/source bindings, not mutable score totals."""
    if (review.get("version") != VERSION or review.get("target_kind") != kind
            or review.get("target_id") != target_id or review.get("verdict") not in VERDICTS
            or review.get("description_hash") != digest(description)):
        return False
    try:
        actual, parent_hash, _, semantics = _target(state, kind, target_id)
        return (actual == description and parent_hash == review.get("parent_description_hash")
                and _sources(state, semantics, list(review.get("source_hashes", {}))) == review["source_hashes"])
    except (SafetyError, KeyError):
        return False


def _overlay(state, kind, target_id, description):
    rows = [(record_id, row) for record_id, row in state.get("brand_reviews", {}).items()
            if record_id == key("brand-overlay", row) and _compatible(state, row, kind, target_id, description)]
    if not rows:
        return None
    # Supersession is explicit; dates cannot make an older review win ties.
    superseded = {row.get("supersedes") for _, row in rows}
    current = [(record_id, row) for record_id, row in rows if record_id not in superseded]
    if len(current) != 1:
        raise SafetyError("BRAND_REVIEW_BINDING_CONFLICT")
    return current[0]


def brand_binding(state, target_kind, target_id, description):
    """Small dependency token for eligibility/selection/question consumers."""
    value = {"verdict": description.get("brand", "unclear"), "review_id": None,
             "description_hash": digest(description), "source": "original"}
    overlay = _overlay(state, target_kind, target_id, description)
    if overlay:
        return dict(value, verdict=overlay[1]["verdict"], review_id=overlay[0], source="overlay")
    if target_kind == "concept":
        concept = state["concepts"].get(target_id, {})
        old = concept.get("brand_review")
        if (isinstance(old, dict) and old.get("method") == "operator_source_review"
                and old.get("decision") == "established" and description.get("brand") == "established"
                and any(digest(dict(description, brand=verdict)) == old.get("expected_description_hash")
                        for verdict in VERDICTS)):
            return dict(value, verdict="established", review_id=key("brand-review", old), source="operator")
        child_id, review_id = concept.get("origin_child_id"), concept.get("origin_brand_review_id")
        if child_id or review_id:
            child_review = state.get("brand_reviews", {}).get(review_id, {})
            association = state["associations"].get(child_id, {})
            if (not child_id or not review_id or association.get("status") != "resolved"
                    or association.get("concept_id") != target_id
                    or association.get("origin_brand_review_id") != review_id
                    or concept.get("origin_description_hash") != digest(description)
                    or association.get("origin_description_hash") != digest(description)
                    or not _compatible(state, child_review, "child", child_id, description)):
                raise SafetyError("BRAND_ORIGIN_BINDING_CHANGED")
            return dict(value, verdict=child_review["verdict"], review_id=review_id, source="origin")
    return value


def current_brand(state, target_kind, target_id, description):
    return brand_binding(state, target_kind, target_id, description)["verdict"]


def _verify_request(state, request):
    if (request["target_kind"] == "concept"
            and state["concepts"].get(request["target_id"], {}).get("status") in ("excluded", "retired", "merged")):
        raise SafetyError("BRAND_REVIEW_TARGET_INACTIVE")
    rebuilt = prepare_brand_review(state, request["target_kind"], request["target_id"],
        verdict=request["verdict"], reason=request["reason"], evidence_ids=list(request["source_hashes"]),
        reviewer=request["reviewer"], reviewed_at=request["reviewed_at"])
    if rebuilt != request:
        raise SafetyError("BRAND_REVIEW_DEPENDENCY_CHANGED")


def apply_brand_review(memory, request):
    """Append one overlay; reopen only a brand-only excluded uncreated child.

    Already-applied review IDs are checked first, including after resolution or
    a later review. Replay cannot reopen the child or overwrite current work.
    """
    fields = {"version", "target_kind", "target_id", "description_hash", "parent_description_hash",
              "association_hashes", "source_hashes", "verdict", "reason", "reviewer", "reviewed_at", "supersedes"}
    if (not isinstance(request, dict) or set(request) != fields or request.get("version") != VERSION
            or not isinstance(request.get("source_hashes"), dict) or not isinstance(request.get("association_hashes"), dict)):
        raise SafetyError("BRAND_REVIEW_INVALID")
    review_id = key("brand-overlay", request)
    state = memory.snapshot()
    old = state.get("brand_reviews", {}).get(review_id)
    if old:
        if old != request:
            raise SafetyError("BRAND_REVIEW_CONFLICT")
        return {"applied": False, "review_id": review_id, "target_id": request["target_id"], "status": "accepted"}
    _, _, links, _ = _target(state, request["target_kind"], request["target_id"])
    _verify_request(state, request)
    changes = [change("brand_reviews", review_id, deepcopy(request))]
    reopened = False
    if request["target_kind"] == "child" and request["verdict"] != "established":
        association = links.get(request["target_id"])
        if association and association.get("status") == "excluded":
            if association.get("reason") != "established_brand" or association.get("concept_id") is not None:
                raise SafetyError("BRAND_REVIEW_OTHER_EXCLUSION")
            updated = dict(association, status="brand_review_pending", brand_review_id=review_id,
                           previous_association_hash=digest(association), reason="named_brand_review")
            changes.append(change("associations", request["target_id"], updated, association))
            reopened = True
        elif association and association.get("status") == "brand_review_pending":
            # A newer named verdict supersedes the pending one; ordinary
            # resolution still runs once under this exact review binding.
            updated = dict(association, brand_review_id=review_id)
            changes.append(change("associations", request["target_id"], updated, association))
            reopened = True
    applied = memory.commit(review_id, changes, validate=lambda current: _verify_request(current, request))
    return {"applied": applied, "review_id": review_id, "target_id": request["target_id"],
            "status": "brand_review_pending" if reopened else "accepted"}
