"""Explicit narrowing history over real identities, never evidence inheritance.

An accepted parent-to-child edge records a reviewed narrowing, not equivalence.
The child's history includes the parent's previous exposure; siblings remain
different identities. Existing source facts, reports and reservations are never
rewritten here. This module performs no source or model calls.
"""

from copy import deepcopy

from .functional_aliases import _validate_input as _validate_definitions, prepare_alias_input
from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .research_memory import change, key


KIND = "functional-lineage-v1"
INPUT = "functional-lineage-input-v1"
MAX_REFS = 20
MAX_INPUT_BYTES = 256 * 1024


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise SafetyError("FUNCTIONAL_LINEAGE_JUDGMENT_INVALID")
    return " ".join(value.split())


def _child_ids(state, semantic_id):
    saved = state.get("descriptions", {}).get(semantic_id, {})
    if not isinstance(saved, dict) or not isinstance(saved.get("result", {}), dict):
        raise SafetyError("FUNCTIONAL_LINEAGE_GROUNDING_INVALID")
    result = saved.get("result", {})
    if not isinstance(result.get("products", []), list):
        raise SafetyError("FUNCTIONAL_LINEAGE_GROUNDING_INVALID")
    return {key("child", [semantic_id, index, digest(result)])
            for index, _ in enumerate(result.get("products", []))}


def _grounding(state, members, references, reviewed_at):
    if not isinstance(references, dict) or set(references) != set(members):
        raise SafetyError("FUNCTIONAL_LINEAGE_GROUNDING_INVALID")
    output = {}
    for cid in members:
        refs = references[cid]
        if (not isinstance(refs, list) or not 1 <= len(refs) <= MAX_REFS
                or any(not isinstance(ref, str) for ref in refs) or len(set(refs)) != len(refs)):
            raise SafetyError("FUNCTIONAL_LINEAGE_GROUNDING_INVALID")
        output[cid] = {}
        for ref in sorted(refs):
            identifier(ref)
            observation = state.get("observations", {}).get(ref)
            if not isinstance(observation, dict):
                raise SafetyError("FUNCTIONAL_LINEAGE_GROUNDING_INVALID")
            if timestamp(observation.get("observed_at")) > timestamp(reviewed_at):
                raise SafetyError("FUNCTIONAL_LINEAGE_REVIEW_BEFORE_EVIDENCE")
            semantic_id = observation.get("semantic_id")
            children = _child_ids(state, semantic_id)
            matches = [(aid, row) for aid, row in state.get("associations", {}).items()
                       if aid in children and row.get("semantic_id") == semantic_id
                       and row.get("concept_id") == cid and row.get("status") == "resolved"]
            if not matches:
                raise SafetyError("FUNCTIONAL_LINEAGE_REFERENCE_NOT_ASSOCIATED")
            aid, association = min(matches)
            # The original association is retained for historical verification
            # after a later legitimate association correction. New acceptance
            # still pins the actual current association under the commit.
            output[cid][ref] = {"observation_hash": digest(observation), "association_id": aid,
                "association_hash": digest(association), "association": deepcopy(association)}
    return output


def prepare_lineage_input(state, parent_id, child_id, *, grounding_refs, reviewed_at):
    """Freeze current definitions and actual associated observations; no write."""
    identifier(parent_id); identifier(child_id)
    if parent_id == child_id:
        raise SafetyError("FUNCTIONAL_LINEAGE_SELF_EDGE")
    definitions = prepare_alias_input(state, [parent_id, child_id], reviewed_at=reviewed_at)
    value = {"contract": INPUT, "parent_id": parent_id, "child_id": child_id,
        "definition_input": definitions,
        "grounding_bindings": _grounding(state, definitions["member_ids"], grounding_refs, reviewed_at)}
    value["input_hash"] = digest(value)
    _validate_input(state, value, current=True)
    return value


def _validate_input(state, value, *, current=False):
    fields = {"contract", "parent_id", "child_id", "definition_input", "grounding_bindings", "input_hash"}
    if (not isinstance(value, dict) or set(value) != fields or value["contract"] != INPUT
            or value["input_hash"] != digest({k: v for k, v in value.items() if k != "input_hash"})):
        raise SafetyError("FUNCTIONAL_LINEAGE_INPUT_INVALID")
    if len(canonical(value)) > MAX_INPUT_BYTES:
        raise SafetyError("FUNCTIONAL_LINEAGE_INPUT_SIZE_LIMIT")
    parent, child = value["parent_id"], value["child_id"]
    identifier(parent); identifier(child)
    if parent == child:
        raise SafetyError("FUNCTIONAL_LINEAGE_SELF_EDGE")
    definitions = _validate_definitions(state, value["definition_input"], current=current)
    if definitions["member_ids"] != sorted([parent, child]):
        raise SafetyError("FUNCTIONAL_LINEAGE_INPUT_INVALID")
    grounds = value["grounding_bindings"]
    if not isinstance(grounds, dict) or set(grounds) != {parent, child}:
        raise SafetyError("FUNCTIONAL_LINEAGE_GROUNDING_INVALID")
    for cid, refs in grounds.items():
        if not isinstance(refs, dict) or not 1 <= len(refs) <= MAX_REFS:
            raise SafetyError("FUNCTIONAL_LINEAGE_GROUNDING_INVALID")
        for ref, proof in refs.items():
            identifier(ref)
            if (not isinstance(proof, dict) or set(proof) != {"observation_hash", "association_id", "association_hash", "association"}):
                raise SafetyError("FUNCTIONAL_LINEAGE_GROUNDING_INVALID")
            identifier(proof["association_id"])
            observation = state.get("observations", {}).get(ref)
            association = proof["association"]
            if (not isinstance(observation, dict) or digest(observation) != proof["observation_hash"]
                    or not isinstance(association, dict) or digest(association) != proof["association_hash"]
                    or association.get("semantic_id") != observation.get("semantic_id")
                    or association.get("concept_id") != cid or association.get("status") != "resolved"
                    or proof["association_id"] not in _child_ids(state, observation.get("semantic_id"))):
                raise SafetyError("FUNCTIONAL_LINEAGE_GROUNDING_CHANGED")
            if timestamp(observation.get("observed_at")) > timestamp(definitions["reviewed_at"]):
                raise SafetyError("FUNCTIONAL_LINEAGE_REVIEW_BEFORE_EVIDENCE")
            if current and state.get("associations", {}).get(proof["association_id"]) != association:
                raise SafetyError("FUNCTIONAL_LINEAGE_GROUNDING_CHANGED")
    return value


def _record(state, rid, row):
    fields = {"kind", "input", "relationship", "semantic_basis", "distinctions", "reviewer", "reason", "record_hash"}
    if (not isinstance(row, dict) or set(row) != fields or row.get("kind") != KIND
            or row.get("relationship") != "narrower"
            or not isinstance(row.get("semantic_basis"), str)
            or row.get("semantic_basis") not in {"exact_use_mechanism", "operator_reviewed"}
            or row.get("record_hash") != digest({k: v for k, v in row.items() if k != "record_hash"})
            or rid != key("functional-lineage", {k: v for k, v in row.items() if k != "record_hash"})):
        raise SafetyError("FUNCTIONAL_LINEAGE_RECORD_INVALID")
    value = _validate_input(state, row["input"])
    _text(row["reviewer"]); _text(row["reason"])
    distinctions = row["distinctions"]
    if (not isinstance(distinctions, list) or not 1 <= len(distinctions) <= 20
            or any(_text(d) != d for d in distinctions) or distinctions != sorted(set(distinctions))):
        raise SafetyError("FUNCTIONAL_LINEAGE_JUDGMENT_INVALID")
    bindings = value["definition_input"]["definition_bindings"]
    parent = state["source_checks"][bindings[value["parent_id"]]["definition_id"]]["definition"]
    child = state["source_checks"][bindings[value["child_id"]]["definition_id"]]["definition"]
    if parent == child or all(parent[field] == child[field] for field in (
            "primary_use", "mechanism", "use_context", "compatibility_constraints", "included_distinctions", "excluded_distinctions")):
        raise SafetyError("FUNCTIONAL_LINEAGE_MEANINGFUL_NARROWING_REQUIRED")
    if row["semantic_basis"] == "exact_use_mechanism" and any(parent[f] != child[f] for f in ("primary_use", "mechanism")):
        raise SafetyError("FUNCTIONAL_LINEAGE_OPERATOR_REVIEW_REQUIRED")
    return row


def lineage_projection(state, aliases=None):
    """Permanent directional ancestry through equivalence groups, not a union.

    An alias reducer augmenting its own output must pass that raw output here,
    instead of calling this default from inside itself.
    """
    if aliases is None:
        from .functional_aliases import alias_projection
        aliases = alias_projection(state)
    output = deepcopy(aliases)
    components = {}
    for cid, row in output.items():
        if row.get("reason") or row.get("canonical_id") is None:
            continue
        components.setdefault(row["canonical_id"], set()).update(row["member_ids"])
        row["ancestor_ids"], row["lineage_refs"] = [], []
    edges, refs, direct_refs = {cid: set() for cid in components}, [], {}
    for rid, row in sorted(state.get("source_checks", {}).items()):
        if row.get("kind") != KIND:
            continue
        _record(state, rid, row)  # Authenticate before trusting routing fields.
        parent_id, child_id = row["input"]["parent_id"], row["input"]["child_id"]
        if parent_id not in output or child_id not in output or any(output[cid].get("reason") for cid in (parent_id, child_id)):
            raise SafetyError("FUNCTIONAL_LINEAGE_IDENTITY_UNRESOLVED")
        parent, child = output[parent_id]["canonical_id"], output[child_id]["canonical_id"]
        if parent == child:
            raise SafetyError("FUNCTIONAL_LINEAGE_CYCLE")
        edges[child].add(parent)
        refs.append((rid, row, parent, child))
        direct_refs.setdefault(child, set()).add(rid)
    # Parent-first topological walk avoids recursion limits on long history.
    dependents = {cid: set() for cid in components}
    remaining = {cid: len(parents) for cid, parents in edges.items()}
    for child, parents in edges.items():
        for parent in parents: dependents[parent].add(child)
    ready = sorted(cid for cid, count in remaining.items() if count == 0)
    ancestors, relevant = {}, {}
    while ready:
        cid = ready.pop()
        ancestors[cid], relevant[cid] = set(), set()
        for parent in edges[cid]:
            ancestors[cid].add(parent); ancestors[cid].update(ancestors[parent])
            relevant[cid].update(relevant[parent])
        relevant[cid].update(direct_refs.get(cid, ()))
        for child in sorted(dependents[cid]):
            remaining[child] -= 1
            if remaining[child] == 0: ready.append(child)
    if len(ancestors) != len(components):
        raise SafetyError("FUNCTIONAL_LINEAGE_CYCLE")
    hashes = {rid: digest(row) for rid, row, _, _ in refs}
    for row in output.values():
        cid = row.get("canonical_id")
        if cid not in ancestors:
            continue
        row["ancestor_ids"] = sorted(member for ancestor in ancestors[cid] for member in components[ancestor])
        row["lineage_refs"] = [{"id": rid, "hash": hashes[rid]} for rid in sorted(relevant[cid])]
    return output


def history_ids(concept_id, projection):
    """IDs whose genuine exposure the selected identity inherits."""
    identifier(concept_id)
    row = projection.get(concept_id)
    if not row or row.get("reason") or row.get("canonical_id") is None:
        raise SafetyError("FUNCTIONAL_LINEAGE_IDENTITY_UNRESOLVED")
    return set(row["member_ids"]) | set(row.get("ancestor_ids", []))


def hold_overlap(left, right, projection):
    """Equivalent or ancestor/descendant holds overlap; siblings do not."""
    left_history, right_history = history_ids(left, projection), history_ids(right, projection)
    left_members, right_members = set(projection[left]["member_ids"]), set(projection[right]["member_ids"])
    return bool(left_members & right_history or right_members & left_history)


def lineage_binding(state):
    rows = {rid: digest(_record(state, rid, row)) for rid, row in state.get("source_checks", {}).items()
            if row.get("kind") == KIND}
    return digest(rows) if rows else None


def selected_lineage_binding(state, concept_ids, *, aliases=None):
    projection = lineage_projection(state, aliases=aliases)
    selected = {cid: {field: projection[cid][field] for field in ("member_ids", "ancestor_ids", "lineage_refs")}
                for cid in sorted(set(concept_ids)) if cid in projection and projection[cid].get("lineage_refs")}
    return digest(selected) if selected else None


def _conflicts(state, before, after):
    exposed = [(row.get("client_id", "default"), row.get("issue_id"), row["concept_id"])
               for row in state.get("published_history", {}).values() if row.get("status") == "published"]
    exposure_ids = {(client, issue, cid) for client, issue, cid in exposed}
    holds = []
    for rid, row in state.get("issue_reservations", {}).items():
        if row.get("state") not in {"reserved", "uploaded", "published"}: continue
        client, issue = row.get("client_id", "default"), row.get("issue_id", rid)
        for product in row.get("products", []):
            cid = product.get("concept_id")
            if row["state"] == "published" and (client, issue, cid) in exposure_ids: continue
            if cid in after and not after[cid].get("reason"):
                holds.append((client, issue, cid, product.get("status")))
    affected = set()
    for index, (client, issue, cid, status) in enumerate(holds):
        for other_client, other_issue, other_cid, _ in holds[index + 1:]:
            if client == other_client and hold_overlap(cid, other_cid, after) and not hold_overlap(cid, other_cid, before):
                affected.update((issue, other_issue))
        if status == "New" and any(client == seen_client and issue != seen_issue
                and seen_cid in history_ids(cid, after) and seen_cid not in history_ids(cid, before)
                for seen_client, seen_issue, seen_cid in exposed):
            affected.add(issue)
    return sorted(affected)


def accept_functional_lineage(memory, prepared, *, reviewer, reason, distinctions,
                              semantic_basis="exact_use_mechanism"):
    """Accept an explicit grounded narrowing or identify exact held conflicts.

    ``operator_reviewed`` is the operator's explicit judgment that differently
    worded definitions retain the same use/mechanism with a meaningful narrower
    boundary. It is not a code inference or an automatic exemption for a truly
    different function. Both modes require the operator's explanation and
    grounding; literal equality alone never creates an edge.
    """
    reviewer, reason = _text(reviewer), _text(reason)
    if not isinstance(distinctions, list): raise SafetyError("FUNCTIONAL_LINEAGE_JUDGMENT_INVALID")
    row = {"kind": KIND, "input": deepcopy(prepared), "relationship": "narrower", "semantic_basis": semantic_basis,
           "distinctions": sorted(set(_text(value) for value in distinctions)), "reviewer": reviewer, "reason": reason}
    rid = key("functional-lineage", row); row["record_hash"] = digest(row)
    state = memory.snapshot(); _record(state, rid, row)
    existing = state.get("source_checks", {}).get(rid)
    if existing is not None:
        if canonical(existing) != canonical(row): raise SafetyError("FUNCTIONAL_LINEAGE_RECORD_INVALID")
        lineage_projection(state)
        return {"applied": False, "reused": True, "record_id": rid, "affected_issue_ids": []}
    _validate_input(state, prepared, current=True)
    if timestamp(prepared["definition_input"]["reviewed_at"]) > timestamp(utc_now()):
        raise SafetyError("FUNCTIONAL_LINEAGE_REVIEW_DATE_INVALID")
    def proposed(current):
        before = lineage_projection(current)
        changed = dict(current, source_checks={**current.get("source_checks", {}), rid: row})
        after = lineage_projection(changed)
        return before, after
    before, after = proposed(state)
    affected = _conflicts(state, before, after)
    if affected:
        return {"applied": False, "reused": False, "decision": "release_review_required", "affected_issue_ids": affected,
            "reason": "Saved reports need replacement or confirmed withdrawal before applying this narrowing history."}
    def unchanged(current):
        _validate_input(current, prepared, current=True)
        before, after = proposed(current)
        if _conflicts(current, before, after):
            raise SafetyError("FUNCTIONAL_LINEAGE_RELEASE_REVIEW_REQUIRED")
    memory.commit(key("accept-functional-lineage", rid), [change("source_checks", rid, row)], validate=unchanged)
    return {"applied": True, "reused": False, "record_id": rid, "affected_issue_ids": []}
