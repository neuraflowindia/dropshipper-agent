"""Reviewed equivalence over existing identities, without rewriting their past.

The accepted narrow definitions are the comparison inputs. Exact meanings can
be joined by code; differently worded meanings need a saved model/operator
judgment. Aliases affect duplicate prevention, never transfer market or supplier
facts. Original concepts, source associations and report bytes remain intact.
"""

from copy import deepcopy
import re

from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .operations import record_saved_decision
from .research_memory import change, key

KIND = "functional-alias-v1"
INPUT = "functional-alias-input-v1"
_HASH = re.compile(r"[a-f0-9]{64}\Z")
MAX_MEMBERS = 20


def prepare_alias_input(state, member_ids, *, reviewed_at):
    from .functional_types import effective_functional_definition
    timestamp(reviewed_at)
    if (not isinstance(member_ids, (list, tuple)) or not 2 <= len(member_ids) <= MAX_MEMBERS
            or len(set(member_ids)) != len(member_ids)):
        raise SafetyError("FUNCTIONAL_ALIAS_MEMBERS_INVALID")
    bindings = {}
    for cid in sorted(member_ids):
        identifier(cid)
        definition = effective_functional_definition(state, cid)
        if definition["accepted"] is not True:
            raise SafetyError("FUNCTIONAL_ALIAS_ACCEPTED_DEFINITION_REQUIRED")
        record = state["source_checks"][definition["definition_id"]]
        if timestamp(record["reviewed_at"]) > timestamp(reviewed_at):
            raise SafetyError("FUNCTIONAL_ALIAS_REVIEW_DATE_INVALID")
        bindings[cid] = {"definition_id": definition["definition_id"],
                         "record_hash": digest(record), "meaning_hash": definition["meaning_hash"]}
    categories = {state["concepts"][cid]["description"]["category"] for cid in bindings}
    if len(categories) != 1:
        raise SafetyError("FUNCTIONAL_ALIAS_CATEGORY_CONFLICT")
    value = {"contract": INPUT, "member_ids": sorted(bindings), "definition_bindings": bindings,
             "category": next(iter(categories)), "reviewed_at": reviewed_at}
    value["input_hash"] = digest(value)
    return value


def _validate_input(state, value, *, current=False):
    from .functional_types import _DEFINITION_INTENT_FIELDS, _verified_definition, effective_functional_definition
    fields = {"contract", "member_ids", "definition_bindings", "category", "reviewed_at", "input_hash"}
    if (not isinstance(value, dict) or set(value) != fields or value["contract"] != INPUT
            or value["input_hash"] != digest({k: v for k, v in value.items() if k != "input_hash"})):
        raise SafetyError("FUNCTIONAL_ALIAS_INPUT_INVALID")
    members = value["member_ids"]
    if (not isinstance(members, list) or not 2 <= len(members) <= MAX_MEMBERS
            or members != sorted(set(members)) or set(value["definition_bindings"]) != set(members)):
        raise SafetyError("FUNCTIONAL_ALIAS_MEMBERS_INVALID")
    timestamp(value["reviewed_at"])
    for cid in members:
        identifier(cid)
        binding = value["definition_bindings"][cid]
        if not isinstance(binding, dict) or set(binding) != {"definition_id", "record_hash", "meaning_hash"}:
            raise SafetyError("FUNCTIONAL_ALIAS_INPUT_INVALID")
        record = state.get("source_checks", {}).get(binding["definition_id"])
        if not isinstance(record, dict) or digest(record) != binding["record_hash"]:
            raise SafetyError("FUNCTIONAL_ALIAS_DEFINITION_CHANGED")
        _verified_definition(record)
        if (record["concept_id"] != cid or record["meaning_hash"] != binding["meaning_hash"]
                or key("functional-definition", {f: record[f] for f in _DEFINITION_INTENT_FIELDS}) != binding["definition_id"]
                or timestamp(record["reviewed_at"]) > timestamp(value["reviewed_at"])):
            raise SafetyError("FUNCTIONAL_ALIAS_DEFINITION_CHANGED")
        if current:
            definition = effective_functional_definition(state, cid)
            if definition["definition_id"] != binding["definition_id"]:
                raise SafetyError("FUNCTIONAL_ALIAS_INPUT_CHANGED")
            if state["concepts"][cid]["description"]["category"] != value["category"]:
                raise SafetyError("FUNCTIONAL_ALIAS_CATEGORY_CONFLICT")
    return value


def _record(state, rid, row):
    fields = {"kind", "input", "method", "decision", "reviewer", "reason", "job_proof", "record_hash"}
    if (not isinstance(row, dict) or set(row) != fields or row["kind"] != KIND
            or row["decision"] != "same" or row["method"] not in {"exact_meaning", "operator", "model"}
            or row["record_hash"] != digest({k: v for k, v in row.items() if k != "record_hash"})
            or rid != key("functional-alias", {k: v for k, v in row.items() if k != "record_hash"})):
        raise SafetyError("FUNCTIONAL_ALIAS_RECORD_INVALID")
    _validate_input(state, row["input"])
    for field in ("reviewer", "reason"):
        if not isinstance(row[field], str) or not row[field].strip() or len(row[field]) > 2000:
            raise SafetyError("FUNCTIONAL_ALIAS_RECORD_INVALID")
    if row["method"] == "exact_meaning":
        if len({b["meaning_hash"] for b in row["input"]["definition_bindings"].values()}) != 1 or row["job_proof"] is not None:
            raise SafetyError("FUNCTIONAL_ALIAS_EXACT_MEANING_REQUIRED")
    elif row["method"] == "model":
        from .functional_alias_review import validate_alias_job_proof
        validate_alias_job_proof(state, row["input"], row["job_proof"])
    elif row["job_proof"] is not None:
        raise SafetyError("FUNCTIONAL_ALIAS_RECORD_INVALID")
    return row


def alias_projection(state):
    """Permanent reviewed components; definition/name changes do not reset New.

    A component key is only a duplicate-prevention key. Consumers retain each
    real selected concept ID and its own evidence. Legacy redirects remain
    supported, with missing/cyclic targets reported rather than guessed.
    """
    concepts = state.get("concepts", {})
    parents = {cid: cid for cid in concepts}
    errors = {}
    def find(cid):
        while parents[cid] != cid:
            parents[cid] = parents[parents[cid]]
            cid = parents[cid]
        return cid
    def union(left, right):
        a, b = find(left), find(right)
        if a != b:
            parents[max(a, b)] = min(a, b)
    for cid in concepts:
        current, seen = cid, set()
        while current not in seen:
            seen.add(current)
            row = concepts.get(current)
            if row is None:
                errors[cid] = "ALIAS_TARGET_MISSING"; break
            if row.get("status") != "merged" or not row.get("merged_into"):
                for member in seen:
                    if member in parents: union(member, current)
                break
            current = row["merged_into"]
        else:
            errors[cid] = "ALIAS_CYCLE"
    refs = []
    for rid, row in sorted(state.get("source_checks", {}).items()):
        if row.get("kind") != KIND:
            continue
        _record(state, rid, row)
        members = row["input"]["member_ids"]
        if any(cid not in parents for cid in members):
            raise SafetyError("FUNCTIONAL_ALIAS_CONCEPT_MISSING")
        for cid in members[1:]: union(members[0], cid)
        refs.append((rid, row, members))
    groups = {}
    for cid in concepts: groups.setdefault(find(cid), []).append(cid)
    output = {}
    for cid in concepts:
        group = sorted(groups[find(cid)])
        records = [{"id": rid, "hash": digest(row)} for rid, row, members in refs if set(members) & set(group)]
        output[cid] = {"member_id": cid, "canonical_id": None if cid in errors else find(cid),
                       "member_ids": group, "record_refs": records, "ancestor_ids": [],
                       "reason": errors.get(cid)}
    from .functional_lineage import KIND as LINEAGE_KIND, lineage_projection
    if any(row.get("kind") == LINEAGE_KIND for row in state.get("source_checks", {}).values()):
        return lineage_projection(state, aliases=output)
    return output


def alias_binding(state):
    rows = {rid: digest(row) for rid, row in state.get("source_checks", {}).items() if row.get("kind") == KIND}
    from .functional_lineage import lineage_binding
    lineage = lineage_binding(state)
    if lineage is not None: rows["directional_lineage"] = lineage
    return digest(rows) if rows else None


def selected_alias_binding(state, concept_ids, *, aliases=None):
    if alias_binding(state) is None: return None
    aliases = alias_projection(state) if aliases is None else aliases
    selected = {cid: deepcopy(aliases[cid]) for cid in sorted(set(concept_ids))
                if cid in aliases and (aliases[cid]["record_refs"] or aliases[cid].get("lineage_refs"))}
    return digest(selected) if selected else None


def expand_identities(ids, aliases):
    result = set(ids)
    for cid in list(result):
        row = aliases.get(cid)
        if row:
            if row.get("reason") or row.get("canonical_id") is None:
                raise SafetyError("FUNCTIONAL_ALIAS_UNRESOLVED")
            result.update(row["member_ids"])
    return result


def _conflicts(state, members, aliases):
    group = expand_identities(members, aliases)
    exposed = {}
    for row in state.get("published_history", {}).values():
        if row.get("status") == "published" and row.get("concept_id") in group:
            exposed.setdefault(row.get("client_id", "default"), set()).add(row.get("issue_id"))
    pending = {}
    for rid, row in state.get("issue_reservations", {}).items():
        if row.get("state") not in {"reserved", "uploaded"}: continue
        selected = [p for p in row.get("products", []) if p.get("concept_id") in group]
        if selected:
            pending.setdefault(row.get("client_id", "default"), []).append((rid, selected))
    affected = set()
    for client, issues in pending.items():
        if len(issues) > 1 or any(len(products) > 1 for _, products in issues):
            affected.update(rid for rid, _ in issues)
        for rid, products in issues:
            if exposed.get(client, set()) - {rid} and any(p.get("status") == "New" for p in products):
                affected.add(rid)
    return sorted(affected)


def _contradictory_reviews(state, members, aliases):
    """Do not erase a still-current 'different' answer by transitive merging."""
    from .functional_alias_review import REVIEW_KIND, _review_record
    from .functional_types import effective_functional_definition
    group = expand_identities(members, aliases)
    conflicts = []
    for rid, row in state.get("source_checks", {}).items():
        if row.get("kind") != REVIEW_KIND: continue
        _review_record(state, rid, row)
        if row["result"]["decision"] != "different": continue
        reviewed = row.get("input", {}).get("member_ids", [])
        if not set(reviewed).issubset(group): continue
        # A genuinely changed meaning makes that old comparison historical,
        # not a veto over the newly reviewed definitions.
        if any(effective_functional_definition(state, cid)["meaning_hash"] !=
               row["input"]["definition_bindings"][cid]["meaning_hash"] for cid in reviewed): continue
        if len({aliases[cid]["canonical_id"] for cid in reviewed}) > 1: conflicts.append(rid)
    return sorted(conflicts)


def accept_functional_alias(memory, prepared, *, decision="same", reviewer, reason,
                            method="operator", job_proof=None, job_id=None, item_id=None):
    """Append exact reviewed edge or return affected releases needing repair.

    This operation cannot withdraw an uploaded report or mutate sign-in/client
    publication. Existing owner-authorized replacement/withdrawal must reconcile
    conflicting frozen holds first; unrelated research remains runnable.
    """
    if decision != "same":
        if decision not in {"different", "unclear"}: raise SafetyError("FUNCTIONAL_ALIAS_DECISION_INVALID")
        return {"applied": False, "decision": decision, "affected_issue_ids": []}
    state = memory.snapshot()
    verified = None
    if method == "model":
        from .functional_alias_review import verify_alias_job
        if job_id is None or item_id is None: raise SafetyError("FUNCTIONAL_ALIAS_ORIGINAL_JOB_REQUIRED")
        verified = verify_alias_job(memory, state, prepared, job_id, item_id)
        if (verified["decision"] != decision or verified["reason"] != reason
                or job_proof is not None and canonical(job_proof) != canonical(verified["proof"])):
            raise SafetyError("FUNCTIONAL_ALIAS_ORIGINAL_JOB_MISMATCH")
        job_proof = verified["proof"]
    elif job_id is not None or item_id is not None:
        raise SafetyError("FUNCTIONAL_ALIAS_RECORD_INVALID")
    row = {"kind": KIND, "input": deepcopy(prepared), "method": method, "decision": decision,
           "reviewer": reviewer, "reason": reason, "job_proof": deepcopy(job_proof)}
    rid = key("functional-alias", row); row["record_hash"] = digest(row)
    _record(state, rid, row)
    if rid in state.get("source_checks", {}):
        if canonical(state["source_checks"][rid]) != canonical(row): raise SafetyError("FUNCTIONAL_ALIAS_RECORD_INVALID")
        return {"applied": False, "reused": True, "record_id": rid, "affected_issue_ids": []}
    _validate_input(state, prepared, current=True)
    if timestamp(prepared["reviewed_at"]) > timestamp(utc_now()): raise SafetyError("FUNCTIONAL_ALIAS_REVIEW_DATE_INVALID")
    aliases = alias_projection(state)
    contradictions = _contradictory_reviews(state, prepared["member_ids"], aliases)
    if contradictions:
        return {"applied": False, "reused": False, "decision": "waiting", "review_ids": contradictions,
            "affected_issue_ids": [], "reason": "FUNCTIONAL_ALIAS_CONTRADICTORY_REVIEW"}
    proposed = dict(state, source_checks={**state.get("source_checks", {}), rid: row})
    proposed_aliases = alias_projection(proposed)
    affected = _conflicts(state, prepared["member_ids"], aliases)
    from .functional_lineage import _conflicts as lineage_conflicts
    affected = sorted(set(affected) | set(lineage_conflicts(state, aliases, proposed_aliases)))
    if affected:
        return {"applied": False, "reused": False, "decision": "release_review_required",
                "affected_issue_ids": affected, "reason": "Saved reports need replacement or confirmed withdrawal before joining these identities."}
    def unchanged(current):
        _validate_input(current, prepared, current=True)
        if verified is not None:
            if memory._fingerprint(verified["result_path"]) != verified["result_fingerprint"]:
                raise SafetyError("FUNCTIONAL_ALIAS_ORIGINAL_JOB_CHANGED")
            if digest(current.get("jobs", {}).get(job_id)) != verified["proof"]["job_hash"]:
                raise SafetyError("FUNCTIONAL_ALIAS_ORIGINAL_JOB_CHANGED")
            _record(current, rid, row)
        aliases = alias_projection(current)
        if _contradictory_reviews(current, prepared["member_ids"], aliases):
            raise SafetyError("FUNCTIONAL_ALIAS_CONTRADICTORY_REVIEW")
        proposed = dict(current, source_checks={**current.get("source_checks", {}), rid: row})
        proposed_aliases = alias_projection(proposed)
        if (_conflicts(current, prepared["member_ids"], aliases)
                or lineage_conflicts(current, aliases, proposed_aliases)):
            raise SafetyError("FUNCTIONAL_ALIAS_RELEASE_REVIEW_REQUIRED")
    committed = memory.commit(key("accept-functional-alias", rid), [change("source_checks", rid, row)], validate=unchanged)
    record_saved_decision(committed, record_kind=KIND, record_id=rid)
    return {"applied": True, "reused": False, "record_id": rid, "affected_issue_ids": []}


def portable_alias_proof(state, record_ids):
    """Freeze real reviewed edges for an explicitly approved owner mutation.

    Only the explicitly selected bounded edge batch is included, together with
    exact definition chains and applicable original negative comparisons.
    The caller separately checks component coverage and resumes other batches.
    This is not a public-source fact, an approval or a claim that a remote reader
    can discover local records that the authenticated sender omitted.
    """
    from .frozen_records import freeze_original_json
    from .functional_alias_review import REVIEW_KIND, _review_record
    from .functional_types import _verified_definition, _DEFINITION_INTENT_FIELDS, FUNCTIONAL_DEFINITION_KIND
    if (not isinstance(record_ids, (list, tuple)) or not record_ids or len(record_ids) > MAX_MEMBERS
            or any(not isinstance(rid, str) for rid in record_ids) or len(set(record_ids)) != len(record_ids)):
        raise SafetyError("FUNCTIONAL_ALIAS_PROOF_SELECTION_INVALID")
    checks = state.get("source_checks", {})
    aliases = alias_projection(state)
    members, affected, selected = set(), set(), set(record_ids)
    for rid in record_ids:
        row = checks.get(rid)
        _record(state, rid, row)
        members.update(row["input"]["member_ids"])
        affected.update(expand_identities(row["input"]["member_ids"], aliases))
    records, definitions, reviews = {}, {}, {}

    def add_chain(did):
        seen = set()
        while did is not None:
            if did in seen: raise SafetyError("FUNCTIONAL_DEFINITION_REVISION_CONFLICT")
            seen.add(did)
            row = checks.get(did)
            if not isinstance(row, dict): raise SafetyError("FUNCTIONAL_ALIAS_DEFINITION_CHANGED")
            _verified_definition(row)
            if key("functional-definition", {field: row[field] for field in _DEFINITION_INTENT_FIELDS}) != did:
                raise SafetyError("FUNCTIONAL_DEFINITION_BINDING_MISMATCH")
            previous = checks.get(row["previous_definition_id"])
            if (row["revision"] == 1 and row["previous_definition_id"] is not None
                    or row["revision"] > 1 and (not isinstance(previous, dict)
                        or previous.get("revision") != row["revision"] - 1
                        or previous.get("concept_id") != row["concept_id"]
                        or previous.get("meaning_hash") != row["previous_meaning_hash"])):
                raise SafetyError("FUNCTIONAL_DEFINITION_REVISION_CONFLICT")
            definitions[did] = freeze_original_json(row)
            did = row["previous_definition_id"]

    for rid in sorted(selected):
        row = _record(state, rid, checks.get(rid))
        records[rid] = freeze_original_json(row)
        for binding in row["input"]["definition_bindings"].values(): add_chain(binding["definition_id"])
    for rid, row in sorted(checks.items()):
        if row.get("kind") != REVIEW_KIND: continue
        _review_record(state, rid, row)
        if row["result"]["decision"] != "different" or not set(row["input"]["member_ids"]).issubset(affected): continue
        reviews[rid] = freeze_original_json(row)
        members.update(row["input"]["member_ids"])
        for binding in row["input"]["definition_bindings"].values(): add_chain(binding["definition_id"])
    # Historical accepted members may now be marked merged. Read their real
    # immutable definition chains, not the active-concept convenience reader,
    # and do not invent a portable link for an unrelated legacy redirect.
    heads, revisions = {}, set()
    for did, row in checks.items():
        if row.get("kind") != FUNCTIONAL_DEFINITION_KIND: continue
        _verified_definition(row)
        if key("functional-definition", {field: row[field] for field in _DEFINITION_INTENT_FIELDS}) != did:
            raise SafetyError("FUNCTIONAL_DEFINITION_BINDING_MISMATCH")
        if row["concept_id"] in members:
            revision_key = (row["concept_id"], row["revision"])
            if revision_key in revisions: raise SafetyError("FUNCTIONAL_DEFINITION_REVISION_CONFLICT")
            revisions.add(revision_key)
            old = heads.get(row["concept_id"])
            if old is None or old[1]["revision"] < row["revision"]: heads[row["concept_id"]] = (did, row)
    for did, _ in heads.values(): add_chain(did)
    proof = {"contract": "functional-alias-portable-v1", "records": records,
             "definitions": definitions, "reviews": reviews}
    if len(canonical(proof)) > 480 * 1024:
        raise SafetyError("FUNCTIONAL_ALIAS_PROOF_BATCH_REQUIRED")
    return proof
