"""Manufacturer-independent meaning attached to existing concept identities.

These records are additive. They do not merge concepts, change checklist scores,
make market claims, or rewrite publication history. Accepting a definition is an
explicit, source-bound judgment; a legacy descriptor is never silently promoted
to that judgment. The functions here perform no provider or model calls.
"""

from copy import deepcopy
import re

from .models import SafetyError, canonical, digest, identifier, timestamp
from .operations import record_saved_decision
from .research_memory import change, key
from .research_records import (EXAMPLE_MEMBERSHIP_KIND, FUNCTIONAL_DEFINITION_KIND,
                               iter_example_memberships, iter_functional_definitions)
from .storage import load_json, safe_path


MEANING_FIELDS = ("primary_use", "mechanism", "use_context", "compatibility_constraints",
                  "included_distinctions", "excluded_distinctions")
DEFINITION_FIELDS = ("report_title",) + MEANING_FIELDS
LIST_FIELDS = MEANING_FIELDS[2:]
PHYSICAL_FACETS = {"weight": "weight", "packed_weight": "weight", "item_weight": "weight", "weight_kg": "weight",
    "dimensions": "dimensions", "item_dimensions": "dimensions", "packed_dimensions": "dimensions",
    "material": "material", "size": "size", "age": "age", "intended_age": "age",
    "audience": "audience", "compatibility": "compatibility"}
MAX_DEFINITION_BYTES = 32 * 1024
_DEFINITION_INTENT_FIELDS = {"kind", "concept_id", "definition", "meaning_hash", "presentation_hash",
                             "basis_refs", "reviewer", "reviewed_at", "job_id", "job_proof",
                             "example_decisions", "accepted"}


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise SafetyError("FUNCTIONAL_DEFINITION_TEXT_INVALID")
    # Whitespace is presentation. Do not strip numbers, units or meaningful
    # qualifiers, or assume two differently worded functions mean the same thing.
    return " ".join(value.split())


def validate_definition(value):
    if not isinstance(value, dict) or set(value) != set(DEFINITION_FIELDS):
        raise SafetyError("FUNCTIONAL_DEFINITION_FIELDS_INVALID")
    result = {field: _text(value[field]) for field in DEFINITION_FIELDS[:3]}
    for field in LIST_FIELDS:
        rows = value[field]
        if not isinstance(rows, list) or len(rows) > 40:
            raise SafetyError("FUNCTIONAL_DEFINITION_CONSTRAINTS_INVALID")
        result[field] = sorted(set(_text(row) for row in rows))
    if set(result["included_distinctions"]) & set(result["excluded_distinctions"]):
        raise SafetyError("FUNCTIONAL_DEFINITION_DISTINCTIONS_CONFLICT")
    if len(canonical(result)) > MAX_DEFINITION_BYTES:
        raise SafetyError("FUNCTIONAL_DEFINITION_SIZE_LIMIT")
    return result


def definition_meaning_hash(value):
    definition = validate_definition(value)
    return digest({field: definition[field] for field in MEANING_FIELDS})


def _concept(state, concept_id):
    identifier(concept_id)
    concept = state.get("concepts", {}).get(concept_id)
    if not isinstance(concept, dict) or not isinstance(concept.get("description"), dict):
        raise SafetyError("FUNCTIONAL_CONCEPT_NOT_FOUND")
    if concept.get("status") in {"merged", "retired", "excluded"}:
        raise SafetyError("FUNCTIONAL_CONCEPT_INACTIVE")
    return concept


def _legacy_definition(concept):
    description = concept["description"]
    return validate_definition({
        "report_title": description.get("label"),
        "primary_use": description.get("function"), "mechanism": description.get("mechanism"),
        "use_context": [], "compatibility_constraints": [],
        "included_distinctions": description.get("features", []), "excluded_distinctions": []})


def _verified_definition(record):
    if (record.get("kind") != FUNCTIONAL_DEFINITION_KIND
            or set(record) != _DEFINITION_INTENT_FIELDS | {"revision", "previous_definition_id", "previous_meaning_hash", "record_hash"}
            or type(record.get("revision")) is not int or record["revision"] < 1
            or record.get("accepted") is not True):
        raise SafetyError("FUNCTIONAL_DEFINITION_RECORD_INVALID")
    identifier(record.get("concept_id"))
    _text(record.get("reviewer"))
    timestamp(record.get("reviewed_at"))
    if not isinstance(record.get("basis_refs"), dict) or not record["basis_refs"]:
        raise SafetyError("FUNCTIONAL_DEFINITION_REFERENCES_INVALID")
    for ref, value in record["basis_refs"].items():
        identifier(ref)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise SafetyError("FUNCTIONAL_DEFINITION_REFERENCES_INVALID")
    if not isinstance(record.get("example_decisions"), dict):
        raise SafetyError("FUNCTIONAL_EXAMPLE_DECISIONS_INVALID")
    definition = validate_definition(record.get("definition"))
    if (record.get("meaning_hash") != definition_meaning_hash(definition)
            or record.get("presentation_hash") != digest({"report_title": definition["report_title"]})
            or record.get("record_hash") != digest({k: v for k, v in record.items() if k != "record_hash"})):
        raise SafetyError("FUNCTIONAL_DEFINITION_HASH_MISMATCH")
    proof = record.get("job_proof")
    if record.get("reviewer") == "bounded_B_definition_review" and (not isinstance(proof, dict)
            or "definition_acceptance" not in proof):
        raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTANCE_PROOF_REQUIRED")
    if isinstance(proof, dict) and "definition_acceptance" in proof:
        from .functional_definition_review import validate_definition_acceptance_proof
        capsule = proof["definition_acceptance"]
        if (set(proof) != {"request_hash", "result_hash", "definition_acceptance"}
                or not isinstance(capsule, dict) or record.get("job_id") != capsule.get("job_id")
                or proof["request_hash"] != capsule.get("request_hash")
                or proof["result_hash"] != capsule.get("result_hash")):
            raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTANCE_PROOF_INVALID")
        validate_definition_acceptance_proof(capsule, concept_id=record["concept_id"], definition=definition,
            example_decisions=record["example_decisions"], basis_refs=record["basis_refs"])
        if timestamp(record["reviewed_at"]) < timestamp(capsule["result_receipt"]["completed_at"]):
            raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_BEFORE_MODEL_RESULT")
    return definition


def _definition_envelopes(state):
    """Every authenticated definition envelope in saved order, up to the first bad one.

    Returns (rows, error): the scan stops at the first invalid envelope, as
    the per-type reader does, and keeps that exception to raise in place.
    """
    rows = []
    for definition_id, record in iter_functional_definitions(state):
        # Authenticate the cheap envelope before trusting its routing field.
        # Otherwise changing concept_id can hide an accepted definition and
        # silently restore the broader legacy view. Do not resolve source/job
        # dependencies for unrelated concepts during this scan.
        try:
            try:
                expected_id = key("functional-definition", {field: record[field] for field in _DEFINITION_INTENT_FIELDS})
            except (KeyError, TypeError):
                raise SafetyError("FUNCTIONAL_DEFINITION_RECORD_INVALID") from None
            if record.get("record_hash") != digest({k: v for k, v in record.items() if k != "record_hash"}):
                raise SafetyError("FUNCTIONAL_DEFINITION_HASH_MISMATCH")
            if definition_id != expected_id:
                raise SafetyError("FUNCTIONAL_DEFINITION_BINDING_MISMATCH")
        except (SafetyError, KeyError, TypeError, ValueError, AttributeError) as error:
            # A data error is fixed by these saved records; the reader raises it in order.
            return rows, error
        rows.append((definition_id, record))
    return rows, None


def effective_functional_definition(state, concept_id):
    """Read accepted meaning or an explicitly unaccepted legacy compatibility view.

    Marketing wording, price and observation dates cannot silently alter an
    accepted definition. A meaningful change needs a new explicit revision.
    Callers must retain the accepted/basis marker, not relabel the legacy view.
    Inside one pure read scope the same answer is reused for the same tables.
    """
    from .read_memo import remember
    return remember("effective_functional_definition", state, (concept_id,),
                    lambda: _effective_functional_definition(state, concept_id),
                    tables=("concepts", "source_checks"))


def _effective_functional_definition(state, concept_id):
    from .read_memo import remember
    concept = _concept(state, concept_id)
    envelopes, error = remember("functional_definition_envelopes", state, (),
                                lambda: _definition_envelopes(state), tables=("source_checks",), copy=False)
    rows = [(definition_id, record) for definition_id, record in envelopes if record.get("concept_id") == concept_id]
    if error is not None:
        if type(error) is SafetyError:
            raise SafetyError(*error.args) from None
        raise error
    if rows:
        revisions = [record.get("revision") for _, record in rows]
        if any(type(value) is not int or value < 1 for value in revisions) or len(set(revisions)) != len(revisions):
            raise SafetyError("FUNCTIONAL_DEFINITION_REVISION_CONFLICT")
        definition_id, record = max(rows, key=lambda row: row[1]["revision"])
        definition = _verified_definition(record)
        if definition_id != key("functional-definition", {field: record[field] for field in _DEFINITION_INTENT_FIELDS}):
            raise SafetyError("FUNCTIONAL_DEFINITION_BINDING_MISMATCH")
        previous_id = None
        previous_hash = None
        for older_id, older in sorted(rows, key=lambda row: row[1]["revision"]):
            _verified_definition(older)
            if older_id != key("functional-definition", {field: older[field] for field in _DEFINITION_INTENT_FIELDS}):
                raise SafetyError("FUNCTIONAL_DEFINITION_BINDING_MISMATCH")
            if (older["revision"] != 1 and (older["previous_definition_id"] != previous_id
                    or older["previous_meaning_hash"] != previous_hash)):
                raise SafetyError("FUNCTIONAL_DEFINITION_REVISION_CONFLICT")
            previous_id, previous_hash = older_id, older["meaning_hash"]
        if min(revisions) != 1 or max(revisions) != len(revisions) or min(rows, key=lambda row: row[1]["revision"])[1]["previous_definition_id"] is not None:
            raise SafetyError("FUNCTIONAL_DEFINITION_REVISION_CONFLICT")
        return {"concept_id": concept_id, "definition_id": definition_id,
                "revision": record["revision"], "meaning_hash": record["meaning_hash"],
                "presentation_hash": record["presentation_hash"], "accepted": True,
                "basis": "accepted_definition", **deepcopy(definition)}
    definition = _legacy_definition(concept)
    return {"concept_id": concept_id, "definition_id": None, "revision": 0,
            "meaning_hash": definition_meaning_hash(definition),
            "presentation_hash": digest({"report_title": definition["report_title"]}),
            "accepted": False, "basis": "legacy_concept", **definition}


def _basis_observations(state, concept_id, references):
    if (not isinstance(references, list) or not references or len(references) > 100
            or any(not isinstance(ref, str) for ref in references) or len(set(references)) != len(references)):
        raise SafetyError("FUNCTIONAL_DEFINITION_REFERENCES_INVALID")
    semantics = {row.get("semantic_id") for row in state.get("associations", {}).values()
                 if row.get("concept_id") == concept_id and row.get("status") == "resolved"}
    result = {}
    for ref in sorted(references):
        identifier(ref)
        observation = state.get("observations", {}).get(ref)
        if not isinstance(observation, dict) or observation.get("semantic_id") not in semantics:
            raise SafetyError("FUNCTIONAL_DEFINITION_REFERENCE_NOT_ASSOCIATED")
        result[ref] = digest(observation)
    return result


def _completed_job(memory, state, job_id):
    if job_id is None:
        return None
    from .research_ai import MAX_JOB_BYTES, current_attempt, validate_results
    identifier(job_id)
    job = state.get("jobs", {}).get(job_id)
    if not isinstance(job, dict) or not isinstance(job.get("request"), dict):
        raise SafetyError("FUNCTIONAL_DEFINITION_JOB_NOT_COMPLETED")
    request = job["request"]
    try:
        folder = current_attempt(safe_path(memory.root, "ai_jobs", job_id))
        saved = load_json(folder / "result.json", MAX_JOB_BYTES)
        if saved.get("request_hash") != digest(request):
            raise SafetyError("FUNCTIONAL_DEFINITION_JOB_NOT_COMPLETED")
        validated = validate_results(request["role"], request["payload"], saved["response"],
                                     declared_version=request["prompt_version"])
        if not validated["accepted"]:
            raise SafetyError("FUNCTIONAL_DEFINITION_JOB_NOT_COMPLETED")
        return {"request_hash": digest(request), "result_hash": digest(saved)}
    except (OSError, KeyError, TypeError, SafetyError):
        raise SafetyError("FUNCTIONAL_DEFINITION_JOB_NOT_COMPLETED") from None


def _child_description(state, child_id):
    for semantic_id, saved in state.get("descriptions", {}).items():
        result = saved.get("result", {})
        for index, description in enumerate(result.get("products", [])):
            if key("child", [semantic_id, index, digest(result)]) == child_id:
                return description
    raise SafetyError("FUNCTIONAL_EXAMPLE_CHILD_NOT_FOUND")


def _example_decisions(state, concept_id, decisions):
    if not isinstance(decisions, dict) or len(decisions) > 100:
        raise SafetyError("FUNCTIONAL_EXAMPLE_DECISIONS_INVALID")
    result = {}
    for child_id, decision in sorted(decisions.items()):
        identifier(child_id)
        association = state["associations"].get(child_id, {})
        if (not isinstance(decision, dict) or set(decision) != {"decision", "reason"}
                or decision.get("decision") not in {"same", "different", "unclear"}
                or association.get("concept_id") != concept_id or association.get("status") != "resolved"):
            raise SafetyError("FUNCTIONAL_EXAMPLE_DECISIONS_INVALID")
        result[child_id] = {"decision": decision["decision"], "reason": _text(decision["reason"]),
                            "association_hash": digest(association),
                            "description_hash": digest(_child_description(state, child_id))}
    return result


def _inherited_example_decisions(state, current, next_meaning_hash):
    if not current["definition_id"] or current["meaning_hash"] != next_meaning_hash:
        return {}
    original = state["source_checks"][current["definition_id"]].get("example_decisions", {})
    result = {}
    for child_id, row in original.items():
        association = state["associations"].get(child_id)
        try:
            if (not association or association.get("status") != "resolved"
                    or association.get("concept_id") != current["concept_id"]
                    or digest(association) != row["association_hash"]
                    or digest(_child_description(state, child_id)) != row["description_hash"]):
                continue
        except (SafetyError, KeyError):
            continue
        result[child_id] = {field: row[field] for field in ("decision", "reason")}
    return result


def accept_functional_definition(memory, concept_id, definition, *, basis_refs, reviewer,
                                 reviewed_at, expected_meaning_hash, job_id=None, example_decisions=None,
                                 model_acceptance=None):
    """Commit one explicit definition with compare-before-write dependency checks.

    A job reference is optional for an operator judgment. When supplied it must
    name a saved completed job; its existence is not treated as semantic proof.
    No identity, checklist, publication or financial record is touched.
    """
    definition = validate_definition(definition)
    reviewer = _text(reviewer)
    timestamp(reviewed_at)
    state = memory.snapshot()
    current = effective_functional_definition(state, concept_id)
    basis = _basis_observations(state, concept_id, basis_refs)
    if any(timestamp(state["observations"][ref]["observed_at"]) > timestamp(reviewed_at) for ref in basis):
        raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_BEFORE_EVIDENCE")
    job_proof = _completed_job(memory, state, job_id)
    if job_id is not None and state["jobs"][job_id]["request"].get("prompt_version") == "research-b-functional-definition-v1" and model_acceptance is None:
        raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTANCE_PROOF_REQUIRED")
    if reviewer == "bounded_B_definition_review" and model_acceptance is None:
        raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTANCE_PROOF_REQUIRED")
    meaning_hash = definition_meaning_hash(definition)
    decisions_input = (_inherited_example_decisions(state, current, meaning_hash)
                       if example_decisions is None else deepcopy(example_decisions))
    example_bindings = _example_decisions(state, concept_id, decisions_input)
    if model_acceptance is not None:
        from .functional_definition_review import (validate_current_definition_acceptance,
            validate_definition_acceptance_proof)
        validate_definition_acceptance_proof(model_acceptance, concept_id=concept_id, definition=definition,
            example_decisions=example_bindings, basis_refs=basis)
        validate_current_definition_acceptance(memory, state, model_acceptance)
        if timestamp(reviewed_at) < timestamp(model_acceptance["result_receipt"]["completed_at"]):
            raise SafetyError("FUNCTIONAL_DEFINITION_REVIEW_BEFORE_MODEL_RESULT")
        if (job_id != model_acceptance["job_id"] or job_proof != {
                "request_hash": model_acceptance["request_hash"], "result_hash": model_acceptance["result_hash"]}):
            raise SafetyError("FUNCTIONAL_DEFINITION_ACCEPTANCE_PROOF_INVALID")
        job_proof = {**job_proof, "definition_acceptance": deepcopy(model_acceptance)}
    intent = {"kind": FUNCTIONAL_DEFINITION_KIND, "concept_id": concept_id,
              "definition": definition, "meaning_hash": meaning_hash,
              "presentation_hash": digest({"report_title": definition["report_title"]}),
              "basis_refs": basis, "reviewer": reviewer, "reviewed_at": reviewed_at,
              "job_id": job_id, "job_proof": job_proof, "example_decisions": example_bindings, "accepted": True}
    # Retry is identified by exact reviewed judgment, not by a new clock value.
    definition_id = key("functional-definition", intent)
    previous = state["source_checks"].get(definition_id)
    if previous is not None:
        _verified_definition(previous)
        return {"definition_id": definition_id, "meaning_hash": previous["meaning_hash"], "reused": True}
    if current["meaning_hash"] != expected_meaning_hash:
        raise SafetyError("FUNCTIONAL_DEFINITION_MEANING_CHANGED")
    record = {**intent, "revision": current["revision"] + 1,
              "previous_definition_id": current["definition_id"],
              "previous_meaning_hash": current["meaning_hash"]}
    record["record_hash"] = digest(record)

    def unchanged(latest):
        actual = effective_functional_definition(latest, concept_id)
        if (actual["definition_id"], actual["meaning_hash"], actual["presentation_hash"]) != (
                current["definition_id"], current["meaning_hash"], current["presentation_hash"]):
            raise SafetyError("FUNCTIONAL_DEFINITION_BASE_CHANGED")
        try:
            actual_basis = _basis_observations(latest, concept_id, basis_refs)
        except SafetyError:
            raise SafetyError("FUNCTIONAL_DEFINITION_EVIDENCE_CHANGED") from None
        if actual_basis != basis:
            raise SafetyError("FUNCTIONAL_DEFINITION_EVIDENCE_CHANGED")
        if _example_decisions(latest, concept_id, decisions_input) != example_bindings:
            raise SafetyError("FUNCTIONAL_EXAMPLE_BASE_CHANGED")
        original_proof = {field: job_proof[field] for field in ("request_hash", "result_hash")} if job_proof is not None else None
        if job_id is not None and (_completed_job(memory, latest, job_id) != original_proof
                or latest["jobs"].get(job_id) != state["jobs"].get(job_id)):
            raise SafetyError("FUNCTIONAL_DEFINITION_JOB_CHANGED")
        if model_acceptance is not None:
            validate_current_definition_acceptance(memory, latest, model_acceptance)

    committed = memory.commit(key("accept-functional", definition_id),
                  [change("source_checks", definition_id, record)], validate=unchanged)
    record_saved_decision(committed, record_kind=FUNCTIONAL_DEFINITION_KIND,
        record_id=definition_id, concept_id=concept_id)
    return {"definition_id": definition_id, "meaning_hash": record["meaning_hash"], "reused": False}


def _membership_records(state, concept_id, *, as_of):
    meaning = effective_functional_definition(state, concept_id)
    result, pending = [], []
    legacy_hash = definition_meaning_hash(_legacy_definition(_concept(state, concept_id)))
    decisions = dict(state["source_checks"][meaning["definition_id"]].get("example_decisions", {})
                     if meaning["definition_id"] else {})
    if meaning["accepted"]:
        from .functional_example_review import current_example_judgments
        decisions.update(current_example_judgments(state, concept_id, meaning["meaning_hash"], as_of=as_of))
    for child_id, association in sorted(state["associations"].items()):
        if association.get("concept_id") != concept_id or association.get("status") != "resolved":
            continue
        observations = {obs_id: digest(row) for obs_id, row in sorted(state["observations"].items())
                        if row.get("semantic_id") == association["semantic_id"]}
        if not observations:
            # An incomplete old association cannot be invented into an example.
            continue
        judgment = decisions.get(child_id)
        if judgment is not None:
            if (judgment.get("association_hash") != digest(association)
                    or judgment.get("description_hash") != digest(_child_description(state, child_id))):
                pending.append(child_id)
                continue
            if judgment.get("decision") != "same":
                if judgment.get("decision") == "unclear":
                    pending.append(child_id)
                continue
        elif meaning["meaning_hash"] != legacy_hash:
            # An old resolved group cannot prove that each example belongs in
            # a newly narrowed definition. Keep the specific judgment pending.
            pending.append(child_id)
            continue
        binding = {"kind": EXAMPLE_MEMBERSHIP_KIND, "concept_id": concept_id,
                   "child_id": child_id, "semantic_id": association["semantic_id"],
                   "meaning_hash": meaning["meaning_hash"], "meaning_basis": meaning["basis"],
                   "association_hash": digest(association), "observation_hashes": observations,
                   "accepted": True, "method": "definition_review" if judgment else "existing_resolved_association",
                   "reason": judgment["reason"] if judgment else association.get("reason", "Existing resolved association.")}
        if judgment and judgment.get("review_id"):
            binding["method"] = "bounded_B_example_review"
            binding["example_review"] = {"id": judgment["review_id"], "record_hash": judgment["review_hash"]}
        membership_id = key("concept-example", binding)
        record = {**binding, "prepared_at": as_of}
        record["record_hash"] = digest(record)
        result.append((membership_id, record))
    return meaning, result, pending


def prepare_example_memberships(memory, concept_id, *, as_of):
    """Attach actual existing examples; never infer a seller, price, image or pack.

    Dates and offer facts stay in their original observations. A membership is
    only a binding to those exact records, and is not another discovery result.
    """
    timestamp(as_of)
    state = memory.snapshot()
    meaning, records, pending = _membership_records(state, concept_id, as_of=as_of)
    for record_id, record in records:
        if record_id in state["source_checks"]:
            _verify_membership(record_id, state["source_checks"][record_id], expected=record)
    changes = [change("source_checks", record_id, record) for record_id, record in records
               if record_id not in state["source_checks"]]
    if changes:
        def unchanged(latest):
            latest_meaning, latest_records, latest_pending = _membership_records(latest, concept_id, as_of=as_of)
            if (latest_meaning["meaning_hash"] != meaning["meaning_hash"]
                    or latest_records != records or latest_pending != pending):
                raise SafetyError("FUNCTIONAL_EXAMPLE_BASE_CHANGED")
            for record_id, record in records:
                if record_id in state["source_checks"]:
                    _verify_membership(record_id, latest["source_checks"].get(record_id), expected=record)
        memory.commit(key("prepare-examples", changes), changes, validate=unchanged)
    return {"concept_id": concept_id, "meaning_hash": meaning["meaning_hash"],
            "membership_ids": [record_id for record_id, _ in records], "pending_child_ids": pending}


def example_membership_preparation(state, concept_id):
    """Stable, saved-only signal for deterministic missing example bindings.

    The caller bounds the programme IDs it inspects. A missing original or a
    changed child judgment is not invented into membership; only an already
    admissible binding that the existing writer can save is marked missing.
    Preparation dates do not create another work question.
    """
    meaning, records, pending = _membership_records(state, concept_id, as_of=None)
    bindings, missing = {}, []
    for rid, expected in records:
        bindings[rid] = digest({field: value for field, value in expected.items()
                                if field not in {"prepared_at", "record_hash"}})
        saved = state["source_checks"].get(rid)
        if saved is None:
            missing.append(rid)
        else:
            _verify_membership(rid, saved, expected=expected)
    return {"concept_id": concept_id, "meaning_hash": meaning["meaning_hash"],
            "bindings": bindings, "missing_ids": missing, "pending_child_ids": pending}


def _verify_membership(record_id, record, *, expected=None):
    if (not isinstance(record, dict) or record.get("kind") != EXAMPLE_MEMBERSHIP_KIND
            or record.get("record_hash") != digest({k: v for k, v in record.items() if k != "record_hash"})):
        raise SafetyError("FUNCTIONAL_EXAMPLE_HASH_MISMATCH")
    if (record.get("method") == "bounded_B_example_review") != ("example_review" in record):
        raise SafetyError("FUNCTIONAL_EXAMPLE_MEMBERSHIP_PROOF_INVALID")
    if "example_review" in record:
        ref = record["example_review"]
        if (not isinstance(ref, dict) or set(ref) != {"id", "record_hash"}
                or not isinstance(ref.get("record_hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", ref["record_hash"])):
            raise SafetyError("FUNCTIONAL_EXAMPLE_MEMBERSHIP_PROOF_INVALID")
        identifier(ref["id"])
    timestamp(record.get("prepared_at"))
    binding = {k: v for k, v in record.items() if k not in {"record_hash", "prepared_at"}}
    if record_id != key("concept-example", binding):
        raise SafetyError("FUNCTIONAL_EXAMPLE_BINDING_MISMATCH")
    if expected is not None and binding != {k: v for k, v in expected.items() if k not in {"record_hash", "prepared_at"}}:
        raise SafetyError("FUNCTIONAL_EXAMPLE_BINDING_MISMATCH")


def current_example_memberships(state, concept_id):
    """Return exact current example facts, not stale bindings after a correction."""
    meaning = effective_functional_definition(state, concept_id)
    _, admissible, _ = _membership_records(state, concept_id, as_of=None)
    admissible_by_id = dict(admissible)
    result = []
    for record_id, record in iter_example_memberships(state, concept_id=concept_id):
        if (record_id not in admissible_by_id or record.get("meaning_hash") != meaning["meaning_hash"]
                or record.get("accepted") is not True):
            continue
        _verify_membership(record_id, record, expected=admissible_by_id[record_id])
        association = state["associations"].get(record.get("child_id"))
        if (not association or association.get("status") != "resolved"
                or association.get("concept_id") != concept_id or digest(association) != record.get("association_hash")):
            continue
        bound = record.get("observation_hashes")
        if not isinstance(bound, dict) or not bound:
            raise SafetyError("FUNCTIONAL_EXAMPLE_REFERENCES_INVALID")
        observations = []
        for obs_id, expected_hash in bound.items():
            observation = state["observations"].get(obs_id)
            if (not observation or digest(observation) != expected_hash
                    or observation.get("semantic_id") != record.get("semantic_id")):
                break
            observations.append({"observation_id": obs_id, **deepcopy(observation)})
        else:
            result.append({"membership_id": record_id, "child_id": record["child_id"],
                           "meaning_hash": meaning["meaning_hash"], "observations": observations})
    return result


def functional_role_context(state, concept_id, *, role="C"):
    """Pure, role-owned meaning and separate exact original-record proof.

    No saved membership needs refreshing merely to plan search wording. Derive
    admissibility from the same reviewed child bindings as the membership
    writer, but keep dates, offers, retail titles and record revision IDs out
    of the semantic model input. The proof is provenance, not a model input or
    an assertion of demand. An accepted definition needs no minimum examples.
    Legacy callers retain their original exact input shape.
    """
    identifier(concept_id)
    if role not in ("C", "E"):
        raise SafetyError("FUNCTIONAL_ROLE_CONTEXT_UNSUPPORTED")
    from .read_memo import remember
    envelopes, error = remember("functional_definition_envelopes", state, (),
                                lambda: _definition_envelopes(state), tables=("source_checks",), copy=False)
    if error is not None:
        if type(error) is SafetyError:
            raise SafetyError(*error.args) from None
        raise error
    found = any(record.get("concept_id") == concept_id for _, record in envelopes)
    if not found:
        return None
    meaning, memberships, pending = _membership_records(state, concept_id, as_of=None)
    examples, proofs = {}, {}
    for _, membership in memberships:
        child_id = membership["child_id"]
        description = _child_description(state, child_id)
        supplied = {field: deepcopy(description.get(field))
                    for field in ("function", "mechanism", "features", "audience")}
        reference = key("functional-role-example", [concept_id, meaning["meaning_hash"], supplied])
        examples[reference] = {"id": reference, "source_text": supplied}
        entry = {"child_id": child_id,
            "semantic_id": membership["semantic_id"], "association_hash": membership["association_hash"],
            "description_hash": digest(description), "observation_hashes": membership["observation_hashes"]}
        if membership.get("example_review"):
            entry.update(example_review_id=membership["example_review"]["id"],
                         example_review_hash=membership["example_review"]["record_hash"])
        proofs.setdefault(reference, []).append(entry)
    record = state["source_checks"][meaning["definition_id"]]
    result = {"concept_id": concept_id, "accepted": True, "meaning_hash": meaning["meaning_hash"],
            "definition": {field: deepcopy(meaning[field]) for field in MEANING_FIELDS},
            "examples": [examples[ref] for ref in sorted(examples)],
            "proof": {"definition_id": meaning["definition_id"], "definition_record_hash": record["record_hash"],
                      "examples": proofs, "pending_child_ids": pending}}
    if role == "E":
        from .research_scope import validated_scoped_evidence
        features, audiences = set(), set()
        for example in result["examples"]:
            supplied = example["source_text"]
            features.update(supplied.get("features") or [])
            if supplied.get("audience"):
                audiences.add(supplied["audience"])
        result["description"] = {**deepcopy(result["definition"]),
                                 "example_features": sorted(features), "example_audiences": sorted(audiences)}
        observations, factual_proofs = {}, {}
        for scoped in validated_scoped_evidence(state, concept_id):
            if (scoped["admissibility"] != "accepted" or scoped["scope"] not in
                    {"narrow_type", "example_offer", "supplier_offer"}):
                continue
            for field in scoped["usable_fields"]:
                path = field["path"]
                facet = PHYSICAL_FACETS.get(path[-1]) if isinstance(path[-1], str) else None
                if facet is None or field["value"] in (None, "", [], {}):
                    continue
                source_text = {"field": facet, "value": deepcopy(field["value"]), "scope": scoped["scope"],
                               "limitations": deepcopy(scoped["limitations"])}
                ref = key("functional-fit-fact", [concept_id, meaning["meaning_hash"], source_text])
                observations[ref] = {"id": ref, "source_text": source_text}
                factual_proofs.setdefault(ref, []).append({"scoped_evidence_id": scoped["id"],
                    "scoped_evidence_hash": scoped["record_hash"], "reference": deepcopy(field["reference"]),
                    "path": deepcopy(path), "value_hash": field["hash"]})
        result["observations"] = [observations[ref] for ref in sorted(observations)]
        result["proof"]["scoped_facts"] = factual_proofs
    return result


def functional_type_inventory(state):
    """Read-only migration inventory; no legacy record is rewritten or scored."""
    active, inactive, unresolved, rows = 0, 0, [], []
    for concept_id, concept in sorted(state.get("concepts", {}).items()):
        if concept.get("status") in {"merged", "retired", "excluded"}:
            inactive += 1
            continue
        active += 1
        try:
            view = effective_functional_definition(state, concept_id)
            rows.append({"concept_id": concept_id, "meaning_hash": view["meaning_hash"],
                         "accepted_definition": view["accepted"], "basis": view["basis"],
                         "saved_examples": len(current_example_memberships(state, concept_id))})
        except SafetyError as error:
            unresolved.append({"concept_id": concept_id, "reason": str(error)})
    return {"kind": "functional-type-inventory-v1", "active_concepts": active,
            "inactive_concepts": inactive, "accepted_definitions": sum(row["accepted_definition"] for row in rows),
            "legacy_views": sum(not row["accepted_definition"] for row in rows),
            "unresolved": unresolved, "concepts": rows}
