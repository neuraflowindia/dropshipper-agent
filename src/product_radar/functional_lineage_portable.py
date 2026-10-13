"""Bounded originals and directional release-history reads, without writes.

These hashes bind the actual selected local originals, not a sender's authority
or a claim that it supplied every relationship. Authentication and conditional
release mutations remain the existing owner's separate responsibility. No
source fact, recommendation, approval or supplier term is inherited here.
"""

from copy import deepcopy
import re

from .frozen_records import freeze_original_json, read_original_json
from .functional_lineage import _record
from .functional_types import _DEFINITION_INTENT_FIELDS, _verified_definition
from .models import SafetyError, canonical, digest, identifier, timestamp
from .research_memory import key


CONTRACT = "functional-lineage-portable-v1"
RECORD_CONTRACT = "functional-lineage-portable-record-v1"
MAX_RECORDS = 20
MAX_PROOF_BYTES = 480 * 1024
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_UTC = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)\Z")


def _date(value):
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise SafetyError("FUNCTIONAL_LINEAGE_PROOF_DATE_INVALID")
    return timestamp(value)


def _definition(did, row):
    if not isinstance(row, dict):
        raise SafetyError("FUNCTIONAL_LINEAGE_PROOF_DEFINITION_CHANGED")
    _verified_definition(row)
    if key("functional-definition", {f: row[f] for f in _DEFINITION_INTENT_FIELDS}) != did:
        raise SafetyError("FUNCTIONAL_DEFINITION_BINDING_MISMATCH")
    if not isinstance(row.get("previous_meaning_hash"), str) or not _HASH.fullmatch(row["previous_meaning_hash"]):
        raise SafetyError("FUNCTIONAL_DEFINITION_REVISION_CONFLICT")
    if row["previous_definition_id"] is not None: identifier(row["previous_definition_id"])


def _add_original(output, rid, row, budget):
    wrapper = freeze_original_json(row)
    if rid in output:
        if output[rid] != wrapper: raise SafetyError("FUNCTIONAL_LINEAGE_PROOF_ORIGINAL_CHANGED")
        return
    # Account as each actual wrapper is read, rather than first assembling an
    # arbitrarily large source/definition closure and only then rejecting it.
    size = budget["bytes"] + len(canonical({rid: wrapper})) - 2 + (1 if output else 0)
    if size > MAX_PROOF_BYTES: raise SafetyError("FUNCTIONAL_LINEAGE_PROOF_BATCH_REQUIRED")
    budget["bytes"], output[rid] = size, wrapper


def _chain(checks, did, output, budget):
    seen = set()
    while did is not None:
        if did in seen: raise SafetyError("FUNCTIONAL_DEFINITION_REVISION_CONFLICT")
        seen.add(did)
        row = checks.get(did)
        _definition(did, row)
        previous = checks.get(row["previous_definition_id"])
        if (row["revision"] == 1 and row["previous_definition_id"] is not None
                or row["revision"] > 1 and (not isinstance(previous, dict)
                    or previous.get("revision") != row["revision"] - 1
                    or previous.get("concept_id") != row["concept_id"]
                    or previous.get("meaning_hash") != row["previous_meaning_hash"])):
            raise SafetyError("FUNCTIONAL_DEFINITION_REVISION_CONFLICT")
        _add_original(output, did, row, budget)
        did = row["previous_definition_id"]


def portable_lineage_proof(state, record_ids):
    """Freeze <=20 explicitly selected real edges and their original proof.

    Definitions stop at each historically bound revision. A later heading or
    meaning correction therefore cannot change these immutable capsules.
    """
    if (not isinstance(record_ids, (list, tuple)) or not 1 <= len(record_ids) <= MAX_RECORDS
            or any(not isinstance(rid, str) for rid in record_ids) or len(set(record_ids)) != len(record_ids)):
        raise SafetyError("FUNCTIONAL_LINEAGE_PROOF_SELECTION_INVALID")
    checks = state.get("source_checks", {})
    records, definitions, observations, descriptions = {}, {}, {}, {}
    proof = {"contract": CONTRACT, "records": records, "definitions": definitions,
             "observations": observations, "descriptions": descriptions}
    budget = {"bytes": len(canonical(proof))}
    for rid in sorted(record_ids):
        identifier(rid)
        row = _record(state, rid, checks.get(rid))
        _add_original(records, rid, row, budget)
        for binding in row["input"]["definition_input"]["definition_bindings"].values():
            _chain(checks, binding["definition_id"], definitions, budget)
        for refs in row["input"]["grounding_bindings"].values():
            for oid in refs:
                observation = state["observations"][oid]
                semantic = observation["semantic_id"]
                _add_original(observations, oid, observation, budget)
                _add_original(descriptions, semantic, state["descriptions"][semantic], budget)
    if len(canonical(proof)) > MAX_PROOF_BYTES:
        raise SafetyError("FUNCTIONAL_LINEAGE_PROOF_BATCH_REQUIRED")
    return proof


def validate_lineage_proof(proof, *, as_of):
    """Verify portable originals independently of mutable local convenience state."""
    fields = {"contract", "records", "definitions", "observations", "descriptions"}
    if (not isinstance(proof, dict) or set(proof) != fields or proof.get("contract") != CONTRACT
            or any(not isinstance(proof[f], dict) for f in fields - {"contract"})):
        raise SafetyError("FUNCTIONAL_LINEAGE_PROOF_INVALID")
    if not 1 <= len(proof["records"]) <= MAX_RECORDS or len(canonical(proof)) > MAX_PROOF_BYTES:
        raise SafetyError("FUNCTIONAL_LINEAGE_PROOF_BATCH_REQUIRED")
    at = _date(as_of)
    originals = {}
    for table in ("records", "definitions", "observations", "descriptions"):
        originals[table] = {}
        for rid, wrapper in proof[table].items():
            identifier(rid)
            originals[table][rid] = read_original_json(wrapper)
    definitions = originals["definitions"]
    groups = {}
    for did, row in definitions.items():
        _definition(did, row)
        if _date(row["reviewed_at"]) > at:
            raise SafetyError("FUNCTIONAL_LINEAGE_PROOF_DEFINITION_FUTURE")
        groups.setdefault(row["concept_id"], []).append((did, row))
    for rows in groups.values():
        rows.sort(key=lambda pair: pair[1]["revision"])
        for index, (did, row) in enumerate(rows):
            if (row["revision"] != index + 1
                    or row["previous_definition_id"] != (rows[index - 1][0] if index else None)
                    or index and row["previous_meaning_hash"] != rows[index - 1][1]["meaning_hash"]):
                raise SafetyError("FUNCTIONAL_DEFINITION_REVISION_CONFLICT")
    state = {"source_checks": definitions, "observations": originals["observations"],
             "descriptions": originals["descriptions"]}
    used_definitions, used_observations, used_descriptions, records = set(), set(), set(), {}
    for rid, row in originals["records"].items():
        _record(state, rid, row)
        if _date(row["input"]["definition_input"]["reviewed_at"]) > at:
            raise SafetyError("FUNCTIONAL_LINEAGE_PROOF_REVIEW_FUTURE")
        for binding in row["input"]["definition_input"]["definition_bindings"].values():
            did = binding["definition_id"]
            while did is not None:
                used_definitions.add(did)
                did = definitions[did]["previous_definition_id"]
        for refs in row["input"]["grounding_bindings"].values():
            for oid in refs:
                used_observations.add(oid)
                used_descriptions.add(originals["observations"][oid]["semantic_id"])
        records[rid] = {"row": deepcopy(row), "hash": digest(row), "wrapper": deepcopy(proof["records"][rid])}
    if (set(definitions) != used_definitions or set(originals["observations"]) != used_observations
            or set(originals["descriptions"]) != used_descriptions):
        raise SafetyError("FUNCTIONAL_LINEAGE_PROOF_UNUSED_ORIGINAL")
    return {"records": records, "definitions": deepcopy(definitions),
            "observations": deepcopy(originals["observations"]),
            "descriptions": deepcopy(originals["descriptions"]), "proof_hash": digest(proof)}


def lineage_record_capsule(proof, record_id, *, as_of):
    """Select one exact immutable stored original, never later definition heads."""
    checked = validate_lineage_proof(proof, as_of=as_of)
    if record_id not in checked["records"]:
        raise SafetyError("FUNCTIONAL_LINEAGE_PROOF_SELECTION_INVALID")
    row = checked["records"][record_id]["row"]
    definitions, observations, descriptions = {}, {}, {}
    for binding in row["input"]["definition_input"]["definition_bindings"].values():
        did = binding["definition_id"]
        while did is not None:
            definitions[did] = deepcopy(proof["definitions"][did])
            did = checked["definitions"][did]["previous_definition_id"]
    for refs in row["input"]["grounding_bindings"].values():
        for oid in refs:
            observations[oid] = deepcopy(proof["observations"][oid])
            sid = checked["observations"][oid]["semantic_id"]
            descriptions[sid] = deepcopy(proof["descriptions"][sid])
    return {"contract": RECORD_CONTRACT, "record_id": record_id,
            "record": deepcopy(proof["records"][record_id]), "definitions": definitions,
            "observations": observations, "descriptions": descriptions}


def validate_lineage_record_capsule(capsule, *, as_of, expected_record_id=None, expected_hash=None):
    fields = {"contract", "record_id", "record", "definitions", "observations", "descriptions"}
    if (not isinstance(capsule, dict) or set(capsule) != fields or capsule["contract"] != RECORD_CONTRACT
            or expected_record_id is not None and capsule["record_id"] != expected_record_id):
        raise SafetyError("FUNCTIONAL_LINEAGE_RECORD_CAPSULE_INVALID")
    identifier(capsule["record_id"])
    proof = {"contract": CONTRACT, "records": {capsule["record_id"]: capsule["record"]},
             **{field: capsule[field] for field in ("definitions", "observations", "descriptions")}}
    checked = validate_lineage_proof(proof, as_of=as_of)
    record = checked["records"][capsule["record_id"]]
    if expected_hash is not None and record["hash"] != expected_hash:
        raise SafetyError("FUNCTIONAL_LINEAGE_RECORD_CAPSULE_CHANGED")
    return checked


def lineage_key(parent_id, child_id, direction=0):
    identifier(parent_id); identifier(child_id)
    if parent_id == child_id or type(direction) is not int or direction not in (0, 1):
        raise SafetyError("RELEASE_LINEAGE_KEY_INVALID")
    endpoint, other = (parent_id, child_id) if direction == 0 else (child_id, parent_id)
    return digest(endpoint) + str(direction) + digest(other)


def _group(tree, cid, overrides, cache):
    identifier(cid)

    def raw(member):
        if member not in cache:
            cache[member] = overrides[member] if overrides and member in overrides else tree.get("aliases", digest(member))
        return cache[member]

    def validate(member, row):
        if (not isinstance(row, dict) or set(row) != {"member_id", "canonical_id", "member_ids", "record_refs", "ancestor_ids"}
                or row["member_id"] != member or not isinstance(row["member_ids"], list) or member not in row["member_ids"]
                or any(not isinstance(cid, str) for cid in row["member_ids"])
                or row["member_ids"] != sorted(set(row["member_ids"]))
                or row["canonical_id"] != row["member_ids"][0] or row["ancestor_ids"] != []
                or not isinstance(row["record_refs"], list)):
            raise SafetyError("RELEASE_ALIAS_PROJECTION_INVALID")
        for cid in row["member_ids"]: identifier(cid)
        ids = []
        for ref in row["record_refs"]:
            if (not isinstance(ref, dict) or set(ref) != {"id", "hash"}
                    or not isinstance(ref["hash"], str) or not _HASH.fullmatch(ref["hash"])):
                raise SafetyError("RELEASE_ALIAS_PROJECTION_INVALID")
            identifier(ref["id"]); ids.append(ref["id"])
        if ids != sorted(set(ids)): raise SafetyError("RELEASE_ALIAS_PROJECTION_INVALID")

    row = raw(cid)
    if row is None: return {"canonical_id": cid, "member_ids": [cid], "ancestor_ids": []}
    validate(cid, row)
    expected = {field: value for field, value in row.items() if field != "member_id"}
    for member in row["member_ids"]:
        peer = raw(member)
        validate(member, peer)
        if {field: value for field, value in peer.items() if field != "member_id"} != expected:
            raise SafetyError("RELEASE_ALIAS_PROJECTION_INVALID")
    return row


def lineage_neighbors(tree, concept_id, direction):
    identifier(concept_id)
    if type(direction) is not int or direction not in (0, 1):
        raise SafetyError("RELEASE_LINEAGE_LOOKUP_INVALID")
    prefix, after, output = digest(concept_id) + str(direction), None, []
    while True:
        page = tree.scan("lineage", prefix=prefix, after=after, limit=50)
        for item in page["rows"]:
            row = item["value"]
            if (not isinstance(row, dict) or set(row) != {"parent_id", "child_id", "record_refs"}
                    or (row["parent_id"] if direction == 0 else row["child_id"]) != concept_id
                    or item["key"] != lineage_key(row["parent_id"], row["child_id"], direction)
                    or not isinstance(row["record_refs"], list) or not row["record_refs"]):
                raise SafetyError("RELEASE_LINEAGE_PROJECTION_INVALID")
            refs = row["record_refs"]
            ids = []
            for ref in refs:
                if (not isinstance(ref, dict) or set(ref) != {"id", "hash"}
                        or not isinstance(ref["hash"], str) or not _HASH.fullmatch(ref["hash"])):
                    raise SafetyError("RELEASE_LINEAGE_PROJECTION_INVALID")
                identifier(ref["id"]); ids.append(ref["id"])
            if ids != sorted(set(ids)): raise SafetyError("RELEASE_LINEAGE_PROJECTION_INVALID")
            if tree.get("lineage", lineage_key(row["parent_id"], row["child_id"], 1 - direction)) != row:
                raise SafetyError("RELEASE_LINEAGE_MIRROR_INVALID")
            output.append(row)
            if len(output) > 4096: raise SafetyError("ADMIN_LINEAGE_GRAPH_BATCH_REQUIRED")
        if not page["more"]: return output
        after = page["last_key"]


def lineage_type_context(tree, concept_id, *, projection_overrides=None):
    """Read actual paired pages; ancestry/descendants remain separate from aliases."""
    if projection_overrides is not None and not isinstance(projection_overrides, dict):
        raise SafetyError("RELEASE_ALIAS_PROJECTION_INVALID")
    group_cache = {}
    start = _group(tree, concept_id, projection_overrides, group_cache)
    refs = {}

    def walk(direction):
        pending, groups, edges = [start], {}, {}
        while pending:
            group = pending.pop()
            cid = group["canonical_id"]
            if cid in groups: continue
            groups[cid], edges[cid] = group, set()
            if len(groups) > 256: raise SafetyError("ADMIN_LINEAGE_GRAPH_BATCH_REQUIRED")
            for member in group["member_ids"]:
                for row in lineage_neighbors(tree, member, direction):
                    for ref in row["record_refs"]:
                        if ref["id"] in refs and refs[ref["id"]] != ref["hash"]:
                            raise SafetyError("RELEASE_LINEAGE_RECORD_CHANGED")
                        refs[ref["id"]] = ref["hash"]
                    other = _group(tree, row["child_id"] if direction == 0 else row["parent_id"], projection_overrides, group_cache)
                    if other["canonical_id"] == cid: raise SafetyError("RELEASE_LINEAGE_CYCLE")
                    edges[cid].add(other["canonical_id"]); pending.append(other)
        degrees = {cid: 0 for cid in groups}
        for children in edges.values():
            for cid in children: degrees[cid] += 1
        ready, visited = [cid for cid, degree in degrees.items() if degree == 0], 0
        while ready:
            cid = ready.pop(); visited += 1
            for child in edges[cid]:
                degrees[child] -= 1
                if degrees[child] == 0: ready.append(child)
        if visited != len(groups): raise SafetyError("RELEASE_LINEAGE_CYCLE")
        return {member for cid, group in groups.items() if cid != start["canonical_id"] for member in group["member_ids"]}

    ancestors, descendants = walk(1), walk(0)
    return {"canonical_id": start["canonical_id"], "member_ids": deepcopy(start["member_ids"]),
            "ancestor_ids": sorted(set(start["ancestor_ids"]) | ancestors), "descendant_ids": sorted(descendants),
            "lineage_refs": [{"id": rid, "hash": refs[rid]} for rid in sorted(refs)]}
