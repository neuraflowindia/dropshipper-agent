"""Local rolling report intake and exact work obligations; never collection.

Intake is a disposable selection projection over canonical research, not a
hold or a replacement product registry. Obligations keep original work owners
and are serviced by the existing programme turn, never by a second driver.
"""

from copy import deepcopy
from types import SimpleNamespace

from .models import SafetyError, digest, identifier, timestamp
from .research_memory import change, key, load_event
from .storage import safe_path

INTAKE_KIND = "reporting-intake-v1"
WORK_KIND = "inventory-work-v1"
CONTROL_KIND = "reporting-intake-cursor-v1"
ZERO = "0" * 64
_LOCAL_KINDS = {INTAKE_KIND, WORK_KIND, CONTROL_KIND}
_PURPOSES = {"fit", "source", "supplier", "match", "material_update", "local_repair", "report_contract"}


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _record_refs(record, field):
    refs = record.get(field, [])
    if (not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs)
            or len(refs) != len(set(refs))):
        raise SafetyError("INVENTORY_BINDING_INVALID")
    return refs


def _meaning(state, cid):
    from .functional_types import effective_functional_definition
    return effective_functional_definition(state, cid)["meaning_hash"]


def _binding_record(record, kind, prefix):
    if (not isinstance(record, dict) or record.get("kind") != kind
            or not isinstance(record.get("binding"), dict)
            or record.get("binding_hash") != digest(record["binding"])
            or record.get("id") != key(prefix, record["binding"])):
        raise SafetyError("INVENTORY_BINDING_INVALID")
    identifier(record["binding"].get("client_id"))
    return record


def validate_inventory_work(record):
    record = _binding_record(record, WORK_KIND, "inventory-work")
    binding = record["binding"]; subject = binding.get("subject")
    if (not isinstance(subject, dict) or subject.get("kind") != "concept"
            or binding.get("purpose") not in _PURPOSES
            or not isinstance(binding.get("needed"), list) or not binding["needed"]
            or not isinstance(record.get("progress"), dict)):
        raise SafetyError("INVENTORY_BINDING_INVALID")
    identifier(subject.get("concept_id"))
    progress = record["progress"]
    if (set(subject) != {"kind", "concept_id", "definition_hash"}
            or not _hash(subject.get("definition_hash")) or not _hash(binding.get("basis_hash"))
            or progress.get("state") not in {"ready", "waiting", "complete", "superseded"}
            or type(progress.get("last_served_turn")) is not int or progress["last_served_turn"] < 0
            or not isinstance(progress.get("deferrals"), dict)
            or any(not isinstance(row, dict) or not isinstance(row.get("code"), str) or not row["code"] for row in binding["needed"])
            or "source_collected" in progress and type(progress["source_collected"]) is not bool
            or "task_ids" in progress and (not isinstance(progress["task_ids"], list)
                or any(not isinstance(ref, str) or not ref for ref in progress["task_ids"])
                or len(progress["task_ids"]) != len(set(progress["task_ids"])))):
        raise SafetyError("INVENTORY_BINDING_INVALID")
    if binding["purpose"] == "material_update" and (binding.get("source") is not None
            or len(binding["needed"]) != 1 or binding["needed"][0].get("code") != "useful_update_review_required"
            or not _hash(binding["needed"][0].get("update_binding_hash"))):
        raise SafetyError("INVENTORY_BINDING_INVALID")
    try:
        timestamp(record["created_at"])
        if progress.get("next_retry_at") is not None:
            timestamp(progress["next_retry_at"])
    except (KeyError, SafetyError):
        raise SafetyError("INVENTORY_BINDING_INVALID") from None
    return record


def inventory_work_records(state, record, *, purpose=None):
    result = []
    for ref in _record_refs(record, "inventory_work_ids"):
        row = validate_inventory_work(state["batch_runs"].get(ref))
        if row["id"] != ref or row["binding"]["client_id"] != record["client_id"]:
            raise SafetyError("INVENTORY_BINDING_INVALID")
        if purpose is None or row["binding"]["purpose"] == purpose:
            result.append(row)
    return result


def eligibility_signal(product):
    """Membership facts of one eligible pool row, not its frozen proofs.

    Current-format rows carry proofs stamped with the pool's own as_of time,
    so a whole-row hash never matches a later pool. The pool itself re-checks
    eligibility; intake binds what changes membership: identity, New/Updated
    status, recommendation format and an Updated row's publication baseline.
    """
    review = product.get("material_update_review") if product.get("status") == "Updated" else None
    return {"concept_id": product.get("concept_id"), "status": product.get("status"),
            "recommendation_contract": product.get("recommendation_contract"),
            "publication_baseline": deepcopy((review or {}).get("baseline"))}


def current_intake_records(state, record, *, pool=None):
    eligible = {row["concept_id"]: row for row in pool["eligible"]} if pool is not None else None
    result = []
    for ref in _record_refs(record, "reporting_intake_ids"):
        row = _binding_record(state["batch_runs"].get(ref), INTAKE_KIND, "reporting-intake")
        if row.get("expired") is not None:
            continue  # Expired with a recorded reason: the type is judged again.
        binding = row["binding"]; cid = binding.get("concept_id")
        if (row["id"] != ref or binding["client_id"] != record["client_id"]
                or not _hash(binding.get("meaning_hash")) or not _hash(binding.get("eligibility_hash"))
                or binding.get("status") not in {"New", "Updated"}
                or not isinstance(binding.get("support_dependencies"), dict)):
            raise SafetyError("INVENTORY_BINDING_INVALID")
        identifier(cid)
        if cid not in state["concepts"] or binding["meaning_hash"] != _meaning(state, cid):
            continue
        if eligible is not None and (cid not in eligible
                                     or digest(eligibility_signal(eligible[cid])) != binding["eligibility_hash"]):
            continue
        result.append(row)
    return result


def expire_intake_records(memory, refs, *, as_of, reason):
    """Expire saved intake records in one save, each with the recorded reason.

    The binding (and so the record's id) is unchanged; readers skip an expired
    record and the type is judged again at the next intake refresh. Already
    expired records are left as they are. Returns the ids expired now.
    """
    timestamp(as_of)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise SafetyError("INVENTORY_EXPIRY_REASON_INVALID")
    state = memory.snapshot(); changes = []
    for ref in sorted(set(refs)):
        row = _binding_record(state["batch_runs"].get(identifier(ref)), INTAKE_KIND, "reporting-intake")
        if row.get("expired") is None:
            changes.append(change("batch_runs", ref, dict(row, expired={"at": as_of, "reason": reason}), row))
    if changes:
        memory.commit(key("expire-reporting-intake", [[row["id"], row["previous_hash"]] for row in changes]), changes)
    return [row["id"] for row in changes]


def reporting_intake_concept_ids(state, record, *, pool=None):
    return {row["binding"]["concept_id"] for row in current_intake_records(state, record, pool=pool)}


def _origins(state):
    from .programme import VERSION, _binding, _review_scopes
    result = {}
    programmes = [row for row in state["batch_runs"].values() if row.get("kind") == VERSION]
    for programme in sorted(programmes, key=lambda row: (row["created_at"], row["programme_id"])):
        for scope in _review_scopes(programme):
            origin = {"programme_id": programme["programme_id"], "scope_id": scope["programme_id"],
                "source_run_id": scope["source_run_id"], "binding_hash": digest(_binding(scope))}
            for cid in scope["concept_ids"]:
                result.setdefault(cid, origin)
    return result


def _changed_subjects(state, item):
    """Project one real committed change, not the global memory head."""
    table = item["table"]; ref = item["id"]
    after = item.get("value")
    rows = [after] if isinstance(after, dict) else []
    ids = set()
    if table in {"concepts", "assessments"}:
        ids.add(ref)
    if table == "batch_runs":
        # Programme counters/checkpoints and our own projections are not
        # research changes. A newly frozen scope does establish real owners.
        if any(row.get("kind") in _LOCAL_KINDS for row in rows):
            return []
        def scope_ids(row):
            if row.get("kind") != "research-programme-v1":
                return set()
            return set(row.get("concept_ids", [])) | {cid for child in row.get("follow_on_reviews", {}).values()
                                                     for cid in child.get("concept_ids", [])}
        if item.get("previous_hash") is None:
            ids |= scope_ids(after or {})
    for row in rows:
        ids.add(row.get("concept_id"))
        if table == "source_checks" and row.get("kind") == "material-update-review-v2":
            ids.add(row.get("binding", {}).get("concept_id"))
        if table == "source_checks" and row.get("kind") == "functional-alias-v1":
            ids.update(row.get("input", {}).get("member_ids", []))
        subject = row.get("subject", {})
        if isinstance(subject, dict) and subject.get("kind") == "concept":
            ids.add(subject.get("concept_id"))
        product = row.get("input", {}).get("product", {}) if isinstance(row.get("input"), dict) else {}
        if isinstance(product, dict):
            ids.add(product.get("concept_id"))
        if table == "query_tasks":
            ids.update(row.get("targets", {}))
        if table in {"issue_reservations", "published_history"}:
            ids.update(product.get("concept_id") for product in row.get("products", []) if isinstance(product, dict))
    semantic_ids = {ref} if table == "descriptions" else set()
    if table in {"observations", "captures"}:
        semantic_ids |= {row.get("semantic_id") for row in rows}
        if table == "captures":
            semantic_ids |= {row["semantic_id"] for row in state["observations"].values() if row.get("capture_id") == ref}
    if semantic_ids:
        ids |= {row.get("concept_id") for row in state["associations"].values() if row.get("semantic_id") in semantic_ids}
    # A reviewed scoped fact may point to an observation without changing an
    # association. Its direct subject is a real dependency too.
    if table in {"observations", "captures", "trends_evidence", "source_checks"}:
        ids |= {row.get("concept_id") for row in state["source_checks"].values()
                if any(isinstance(value, dict) and value.get("table") == table and value.get("id") == ref
                       for value in row.get("references", []))}
    return sorted(cid for cid in ids if isinstance(cid, str) and cid in state["concepts"])


def _changed_prefix(memory, snapshot, control, *, subject_limit, event_limit):
    cursor = deepcopy((control or {}).get("cursor", {"sequence": 0, "head": ZERO,
        "change_index": 0, "subject_offset": 0, "subject_hash": None, "event_hash": None}))
    if (not isinstance(cursor, dict) or set(cursor) != {"sequence", "head", "change_index", "subject_offset", "subject_hash", "event_hash"}
            or any(type(cursor[name]) is not int or cursor[name] < 0 for name in ("sequence", "change_index", "subject_offset"))
            or cursor["sequence"] > snapshot["event_count"] or not _hash(cursor["head"])
            or any(cursor[name] is not None and not _hash(cursor[name]) for name in ("subject_hash", "event_hash"))
            or cursor["subject_hash"] is None and cursor["subject_offset"] != 0
            or cursor["sequence"] == snapshot["event_count"] and any(cursor[name] for name in ("change_index", "subject_offset", "subject_hash", "event_hash"))):
        raise SafetyError("INVENTORY_CURSOR_INVALID")
    if cursor["sequence"] == 0:
        if cursor["head"] != ZERO:
            raise SafetyError("INVENTORY_CURSOR_INVALID")
    else:
        original = load_event(safe_path(memory.root, "events") / f"{cursor['sequence']:08d}.json")
        if (original.get("seq") != cursor["sequence"] or original.get("hash") != cursor["head"]
                or original["hash"] != digest({name: value for name, value in original.items() if name != "hash"})):
            raise SafetyError("INVENTORY_CURSOR_INVALID")
    ids, examined, relevant = set(), 0, False
    while cursor["sequence"] < snapshot["event_count"] and examined < event_limit:
        seq = cursor["sequence"] + 1
        event = load_event(safe_path(memory.root, "events") / f"{seq:08d}.json")
        body = {name: value for name, value in event.items() if name != "hash"}
        if (event.get("seq") != seq or event.get("previous") != cursor["head"] or event.get("hash") != digest(body)
                or event.get("evidence_type") != memory.evidence_type
                or cursor["event_hash"] is not None and cursor["event_hash"] != event["hash"]):
            raise SafetyError("INVENTORY_CURSOR_INVALID")
        cursor["event_hash"] = event["hash"]
        if cursor["change_index"] > len(event["changes"]):
            raise SafetyError("INVENTORY_CURSOR_INVALID")
        event_relevant = False
        while cursor["change_index"] < len(event["changes"]):
            subjects = _changed_subjects(snapshot["state"], event["changes"][cursor["change_index"]])
            event_relevant |= bool(subjects)
            subject_hash = digest(subjects)
            if cursor["subject_hash"] != subject_hash:
                cursor["subject_offset"] = 0; cursor["subject_hash"] = subject_hash
            while cursor["subject_offset"] < len(subjects):
                ids.add(subjects[cursor["subject_offset"]]); cursor["subject_offset"] += 1; relevant = True
                if len(ids) >= subject_limit:
                    return sorted(ids), cursor, relevant
            cursor.update(change_index=cursor["change_index"] + 1, subject_offset=0, subject_hash=None)
        cursor.update(sequence=seq, head=event["hash"], change_index=0, subject_offset=0, subject_hash=None, event_hash=None)
        # Our checkpoint tail and ordinary programme counters cannot consume
        # the entire next scan allowance while adding one more checkpoint.
        examined += int(event_relevant)
    return sorted(ids), cursor, relevant


# Pool decision codes meaning the type does not pass the checklist right now.
_CHECKLIST_GAPS = frozenset(("checklist_review_required", "fit_information_pending", "below_checklist_threshold"))


def _new_work(state, client, cid, purpose, needed, origin, at, *, source=None):
    subject = {"kind": "concept", "concept_id": cid, "definition_hash": _meaning(state, cid)}
    from .research_flow import assessment_item
    from .fit_description import FUNCTIONAL_CONTRACT
    from .functional_types import functional_role_context
    if purpose == "fit":
        facts = assessment_item(state, cid, contract_version=FUNCTIONAL_CONTRACT
            if functional_role_context(state, cid) is not None else None)
    else:
        facts = {"description": state["concepts"][cid]["description"], "needed": needed, "source": source}
    binding = {"client_id": client, "subject": subject, "purpose": purpose, "basis_hash": digest(facts),
        "origin_scope": deepcopy(origin), "needed": deepcopy(needed), "source": deepcopy(source)}
    ref = key("inventory-work", binding)
    return {"id": ref, "kind": WORK_KIND, "binding": binding, "binding_hash": digest(binding),
        "created_at": at, "progress": {"state": "waiting" if purpose in {"local_repair", "report_contract"} else "ready",
        "reason": "report_contract_pending" if purpose == "report_contract" else needed[0]["code"],
        "next_retry_at": None, "deferrals": {}, "last_result": None, "last_served_turn": 0}}


def refresh_inventory_intake(memory, record, *, as_of, pool=None, subject_limit=100, event_limit=25):
    """Prepare current intake and work locally; commit the exact cursor last.

    At most 100 affected subjects / 25 relevant original events are inspected.
    An event with many subjects resumes inside that same immutable event.
    Current-format eligible rows in the supplied pool are also admitted each
    time, independent of the cursor (intake only; no extra work obligations).
    Deadlines are deliberately handled separately by inventory_work_readiness.
    """
    timestamp(as_of)
    if type(subject_limit) is not int or not 1 <= subject_limit <= 100 or type(event_limit) is not int or not 1 <= event_limit <= 100:
        raise SafetyError("INVENTORY_SLICE_INVALID")
    snapshot = memory.snapshot_with_provenance(); state = snapshot["state"]
    client = identifier(record["client_id"]); control_id = key("inventory-cursor", client)
    old = state["batch_runs"].get(control_id)
    if old and (old.get("kind") != CONTROL_KIND or old.get("client_id") != client):
        raise SafetyError("INVENTORY_CURSOR_INVALID")
    ids, cursor, relevant = _changed_prefix(memory, snapshot, old, subject_limit=subject_limit, event_limit=event_limit)
    if pool is None:
        from .report_selection import build_report_pool
        from .fit_policy import FIT_POLICY_ID
        pool = build_report_pool(SimpleNamespace(snapshot=lambda: state, evidence_type=memory.evidence_type),
            as_of=as_of, client_id=client, fit_policy_id=FIT_POLICY_ID)
    eligible = {row["concept_id"]: row for row in pool["eligible"]}; origins = _origins(state)
    own = {slot["issue_id"] for slot in record["release_slots"]}
    # A labelled published hold is released only after canonical history
    # confirms every selected exposure. Missing reconciliation stays protected.
    from .issue_allocation import _held
    from .functional_aliases import alias_projection
    held = _held([row for row in state["issue_reservations"].values() if row.get("issue_id") not in own],
        client_id=client, aliases=alias_projection(state), history=state["published_history"])
    changes, work, created_intake = [], [], 0
    facts = {}
    update_questions = {row["binding"]["concept_id"]: row for row in pool.get("needed_material_update_work", [])
                        if row.get("state") == "prepared"}
    for field in ("needed_fact_work", "needed_market_work", "needed_supplier_work"):
        for row in pool.get(field, []):
            facts.setdefault(row["concept_id"], []).append((field, row))
    from .source_interpretation import _questions
    pending = _questions(state, as_of=as_of) if pool.get("needed_market_work") else []
    unpassed = {row["concept_id"] for row in pool.get("decisions", [])
                if set(row.get("codes", [])) & _CHECKLIST_GAPS}
    for ref, row in list(state["batch_runs"].items()):
        if (row.get("kind") != WORK_KIND or row.get("binding", {}).get("client_id") != client
                or row.get("progress", {}).get("state") in {"complete", "superseded"}):
            continue
        validate_inventory_work(row)
        cid = row["binding"]["subject"]["concept_id"]
        if cid in ids and (cid not in state["concepts"]
                or row["binding"]["subject"]["definition_hash"] != _meaning(state, cid)):
            revised = deepcopy(row)
            revised["progress"].update(state="superseded", next_retry_at=None,
                reason="The accepted product meaning changed; original source and model owners are retained.")
            changes.append(change("batch_runs", ref, revised, row)); state["batch_runs"][ref] = revised
        elif (cid in ids and cid in unpassed and row["binding"]["needed"][0]["code"] == "supplier_search_required"
                and "checklist_hash" in row["binding"]["needed"][0]):
            # An early supplier search belongs to the passing checklist that
            # queued it. A later failing or pending checklist closes it; a new
            # passing checklist queues a new obligation with its own binding.
            revised = deepcopy(row)
            revised["progress"].update(state="superseded", next_retry_at=None,
                reason="The type no longer passes the ten-point checklist; its early supplier search is closed.")
            changes.append(change("batch_runs", ref, revised, row)); state["batch_runs"][ref] = revised
    def admit(cid):
        nonlocal created_intake
        product = eligible.get(cid)
        if product and cid not in held:
            updated = product.get("status") == "Updated"
            verdict = product.get("material_update_review") if updated else None
            if not updated or product.get("material_update_reviewed") is True and isinstance(verdict, dict) and verdict.get("accepted") is True:
                binding = {"client_id": client, "concept_id": cid, "meaning_hash": _meaning(state, cid),
                    "origin_scope": deepcopy(origins.get(cid)), "eligibility_hash": digest(eligibility_signal(product)),
                    "support_dependencies": deepcopy(product.get("source_revisions", {})),
                    "source_scope_hash": product.get("source_scope_hash"), "status": "Updated" if updated else "New",
                    "usable_until": product.get("support_valid_until", product.get("usable_until")),
                    "priority_support": deepcopy(product.get("priority_support")),
                    "publication_baseline": deepcopy(verdict.get("baseline")) if verdict else None,
                    "update_verdict": deepcopy(verdict)}
                ref = key("reporting-intake", binding)
                row = {"id": ref, "kind": INTAKE_KIND, "binding": binding, "binding_hash": digest(binding), "admitted_at": as_of}
                old = state["batch_runs"].get(ref)
                if old is None or old.get("expired") is not None:
                    # An expired intake judged again to the same binding is admitted afresh.
                    changes.append(change("batch_runs", ref, row, old)); state["batch_runs"][ref] = row
                    created_intake += 1
    # Current-format stock is the only stock explicit turns can allocate. Admit
    # every such eligible row now, not only rows the (possibly far behind)
    # change cursor reaches. Unchanged bindings are not rewritten, and no
    # extra work obligations are created for them here.
    from .report_examples import RECOMMENDATION_CONTRACT
    # An expired intake's type is judged again now, whatever the cursor reaches.
    rejudged = {row["binding"].get("concept_id") for row in state["batch_runs"].values()
                if row.get("kind") == INTAKE_KIND and row.get("expired") is not None
                and row.get("binding", {}).get("client_id") == client}
    for cid in sorted(cid for cid, row in eligible.items()
                      if (row.get("recommendation_contract") == RECOMMENDATION_CONTRACT or cid in rejudged)
                      and cid not in ids):
        admit(cid)
    for cid in ids:
        admit(cid)
        for field, needed in facts.get(cid, []):
            code = needed["code"]
            purpose = ("report_contract" if code == "functional_report_contract_required" else
                "material_update" if code == "useful_update_review_required" and cid in update_questions else
                "fit" if code in {"checklist_review_required", "fit_information_pending"} else
                "source" if field == "needed_market_work" else
                "supplier" if field == "needed_supplier_work" else "local_repair")
            if purpose == "material_update":
                needed = dict(needed, update_binding_hash=digest(update_questions[cid]["binding"]))
            # Completed stock is not refreshed merely because this scan sees
            # old evidence. Actual selected expiry needs an explicit source
            # witness obligation from admit_due_inventory_work.
            source = None
            collected = False
            if purpose in {"source", "supplier"}:
                tasks = [(ref, task) for ref, task in state["query_tasks"].items()
                    if task.get("kind") == "all_source_task_v1" and cid in task.get("targets", {})
                    and (task.get("status") not in {"acquired", "completed_no_results", "insufficient", "finished"}
                         or task.get("reconcile_required"))
                    and (task.get("source") == "supplier") == (purpose == "supplier")]
                if tasks:
                    task_id, task = min(tasks, key=lambda value: value[0])
                    source = {"route": task["route"], "request": deepcopy(task["request"]), "original_task_id": task_id,
                        "original_acquisition_id": task.get("acquisition_id"), "expired_support_refs": [],
                        "refresh_due_at": None, "refresh_reason": None}
                else:
                    questions = [row for row in pending if row["concept_id"] == cid]
                    owners = {ref for row in questions for ref in row.get("original_task_ids", [row["task_id"]])}
                    originals = {ref for row in questions for ref in row.get("observation_ids", [])}
                    for ref, candidate in state["query_tasks"].items():
                        if candidate.get("kind") != "all_source_task_v1" or cid not in candidate.get("targets", {}):
                            continue
                        captured = {value for rid in candidate.get("receipt_refs", [])
                            for field in ("observation_ids", "evidence_refs")
                            for value in state["source_checks"].get(rid, {}).get("envelope", {}).get(field, [])}
                        if captured & originals or purpose == "supplier" and candidate.get("source") == "supplier":
                            owners.add(ref)
                    saved = [(ref, task) for ref, task in state["query_tasks"].items()
                             if ref in owners and task.get("status") == "acquired"
                             and cid in task.get("targets", {})
                             and (task.get("source") == "supplier") == (purpose == "supplier")]
                    if saved:
                        task_id, task = min(saved, key=lambda value: value[0]); collected = True
                        purpose = "match" if purpose == "source" else "supplier"
                        source = {"route": task["route"], "request": deepcopy(task["request"]), "original_task_id": task_id,
                            "original_acquisition_id": task.get("acquisition_id"), "expired_support_refs": [],
                            "refresh_due_at": None, "refresh_reason": None}
                    else:
                        checked = [task for task in state["query_tasks"].values()
                            if task.get("kind") == "all_source_task_v1" and cid in task.get("targets", {})
                            and (task.get("source") == "supplier") == (purpose == "supplier")]
                        if purpose == "source" and checked and not questions:
                            # A completed negative/neutral/empty answer can
                            # leave too little support for recommendation;
                            # it is not an unperformed research check.
                            continue
                        purpose = "local_repair"
            row = _new_work(state, client, cid, purpose, [needed], origins.get(cid), as_of, source=source)
            if collected:
                row["progress"].update(source_collected=True, linked_run_id=task["run_id"], task_ids=[task_id])
            if row["id"] not in state["batch_runs"]:
                changes.append(change("batch_runs", row["id"], row)); state["batch_runs"][row["id"]] = row
            work.append(row["id"])
    # Do not create an endless tail of cursor-only writes for our own commits.
    # A later research event safely crosses that ignored local suffix again.
    if old is None or relevant or cursor["sequence"] < snapshot["event_count"]:
        control = {"kind": CONTROL_KIND, "client_id": client, "cursor": cursor}
        if old != control:
            changes.append(change("batch_runs", control_id, control, old))
    if changes:
        memory.commit(key("refresh-inventory-intake", changes), changes)
    newest = {}
    for ref, row in state["batch_runs"].items():
        if row.get("kind") != INTAKE_KIND or row.get("binding", {}).get("client_id") != client:
            continue
        valid = current_intake_records(state, {**record, "reporting_intake_ids": [ref]}, pool=pool)
        cid = row["binding"]["concept_id"]
        # Several saved bindings can stay current for one concept (support
        # changed, membership did not). Carry its newest one only.
        if valid and cid not in held and (cid not in newest or (timestamp(row["admitted_at"]), ref) > newest[cid]):
            newest[cid] = (timestamp(row["admitted_at"]), ref)
    intake_ids = [newest[cid][1] for cid in sorted(newest)]
    work_ids = sorted({*record.get("inventory_work_ids", []), *work,
        *(ref for ref, row in state["batch_runs"].items() if row.get("kind") == WORK_KIND
          and row.get("binding", {}).get("client_id") == client
          and row.get("progress", {}).get("state") not in {"complete", "superseded"})})
    return {"reporting_intake_ids": intake_ids, "inventory_work_ids": work_ids,
        "affected_concept_ids": ids, "created_intake": created_intake, "cursor": cursor,
        "backlog_remaining": cursor["sequence"] < snapshot["event_count"], "pool": pool}


def inventory_work_readiness(state, record, *, as_of):
    """A deadline is checked even when no committed research head changed."""
    now = timestamp(as_of); ready, waits, signal = [], [], []
    for row in inventory_work_records(state, record):
        progress = row["progress"]; binding = row["binding"]; cid = binding["subject"]["concept_id"]
        if progress.get("state") in {"complete", "superseded"}:
            continue
        if cid not in state["concepts"] or _meaning(state, cid) != binding["subject"]["definition_hash"]:
            signal.append([row["id"], "meaning_changed"]); continue
        retry = progress.get("next_retry_at"); due = bool(retry and timestamp(retry) <= now)
        signal.append([row["id"], progress.get("state"), due, binding["basis_hash"]])
        if retry and not due:
            waits.append(retry)
        elif progress.get("state") == "ready" or due:
            ready.append(row)
    ready.sort(key=lambda row: (row["progress"].get("last_served_turn", 0), row["id"]))
    return {"ready": ready, "signal": signal, "next_retry_at": min(waits, key=timestamp) if waits else None}


def admit_selected_inventory_work(memory, record, *, as_of, pool, limit=10):
    """Refresh selected support, never every unused item on an expiry timer.

    The real allocator determines upcoming minima. A shortfall may nominate a
    bounded formerly supported shortlist, not weak/no-result stock or updates
    invented to fill a quota. Only a proved completed source owner can become
    a refresh; uncertain/prepared work retains its ordinary original owner.
    """
    if type(limit) is not int or not 1 <= limit <= 50:
        raise SafetyError("INVENTORY_SLICE_INVALID")
    now = timestamp(as_of); state = memory.snapshot()
    from .issue_allocation import allocate_issues
    from .programme import release_slots, research_week
    from .source_coverage import CHECK_KIND
    from datetime import timedelta
    slots = list(record["release_slots"])
    if not any(timestamp(slot["scheduled_at"]) > now for slot in slots):
        week = (timestamp(research_week(as_of) + "T00:00:00+00:00") + timedelta(days=7)).date().isoformat()
        slots += release_slots(week, client_id=record["client_id"])
    slots = [slot for slot in sorted(slots, key=lambda slot: timestamp(slot["scheduled_at"]))
        if timestamp(slot["scheduled_at"]) > now and state["issue_reservations"].get(slot["issue_id"], {}).get("state")
        not in {"reserved", "uploaded", "published", "cancelled"}][:3]
    if not slots:
        return {"inventory_work_ids": record.get("inventory_work_ids", []), "shortfall": 0, "upcoming_slots": []}
    allocations = allocate_issues(pool["eligible"], slots, state["published_history"], state["issue_reservations"])
    shortfall = sum(issue["shortfall"] for issue in allocations["issues"])
    intended = {product["concept_id"]: slot for issue, slot in zip(allocations["issues"], slots)
                for product in issue["products"][:10]}
    eligible = {row["concept_id"]: row for row in pool["eligible"]}
    origins = _origins(state); requests = []
    candidates = list(intended)
    if shortfall:
        # The allocator correctly leaves currently fresh evidence out of a
        # later release when it expires first. That exclusion is itself an
        # upcoming shortfall, not a reason to overlook the needed refresh.
        shortlist = [row["concept_id"] for row in pool["eligible"]
            if row["concept_id"] not in intended and row.get("support_valid_until", row.get("usable_until"))
            and timestamp(row.get("support_valid_until", row.get("usable_until"))) < timestamp(slots[0]["scheduled_at"])]
        shortlist += [row["concept_id"] for row in pool.get("needed_market_work", [])
                      if row["concept_id"] not in intended and row["concept_id"] not in shortlist]
        candidates += shortlist[:min(limit, shortfall)]
    for cid in candidates:
        if len(requests) >= limit:
            break
        product = eligible.get(cid)
        slot = intended.get(cid, slots[0])
        valid_until = product.get("support_valid_until", product.get("usable_until")) if product else None
        if product and (not valid_until or timestamp(valid_until) >= timestamp(slot["scheduled_at"])):
            continue
        # A functional recommendation carries its actual sufficient witness.
        # It must not rely on legacy target_results, which correctly remain
        # unpromoted when the accepted meaning is assessed separately.
        witness = product.get("priority_support") if product else None
        if not product:
            prior = [row["binding"] for row in state["batch_runs"].values()
                     if row.get("kind") == INTAKE_KIND and row.get("binding", {}).get("client_id") == record["client_id"]
                     and row.get("expired") is None
                     and row["binding"].get("concept_id") == cid and row["binding"].get("meaning_hash") == _meaning(state, cid)
                     and row["binding"].get("priority_support")]
            if prior:
                witness = max(prior, key=lambda row: (timestamp(row["usable_until"]), digest(row)))["priority_support"]
        required_refs = {ref["id"]: ref for site in (witness or {}).get("sources", []) for ref in site["references"]}
        from .functional_types import effective_functional_definition
        accepted = effective_functional_definition(state, cid)["accepted"]
        # Without a previously qualified scoped witness, an expired accepted
        # type is not invented as formerly supportive stock.
        if accepted and not required_refs:
            continue
        supported, physical = [], set()
        for task_id, task in sorted(state["query_tasks"].items()):
            target = task.get("target_results", {}).get(cid, {})
            if (task.get("kind") != "all_source_task_v1" or task.get("source") == "supplier"
                    or task.get("status") != "acquired" or task.get("reconcile_required")
                    or (not accepted and target.get("state") != "completed_relevant")
                    or not task.get("acquisition_id")):
                continue
            receipt = state["source_checks"].get((task.get("receipt_refs") or [None])[-1], {})
            envelope = receipt.get("envelope", {})
            if (receipt.get("kind") != CHECK_KIND or receipt.get("envelope_hash") != digest(envelope)
                    or envelope.get("acquisition_id") != task["acquisition_id"] or envelope.get("dispatch_started") is not True):
                continue
            owner = (task["route"], digest(task["request"]), task["acquisition_id"])
            if owner in physical:
                continue
            newer = any(other.get("kind") == "all_source_task_v1" and other.get("route") == task["route"]
                and other.get("request") == task["request"] and other.get("status") in {"acquired", "completed_no_results"}
                and any(timestamp(state["source_checks"].get(ref, {}).get("at", envelope["observed_at"])) > timestamp(envelope["observed_at"])
                        for ref in other.get("receipt_refs", [])) for other in state["query_tasks"].values())
            if newer:
                continue
            # Exact original receipt membership is the authority for refresh.
            receipt_refs = set(envelope.get("observation_ids", [])) | set(envelope.get("evidence_refs", []))
            refs = set(required_refs) & receipt_refs if accepted else set(target.get("evidence_refs", [])) & receipt_refs
            frozen = []
            for ref in sorted(refs):
                table = "observations" if ref in state["observations"] else "trends_evidence" if ref in state["trends_evidence"] else None
                if table and (not accepted or required_refs[ref]["hash"] == digest(state[table][ref])):
                    frozen.append({"table": table, "id": ref, "hash": digest(state[table][ref])})
            if not frozen:
                continue
            expiry = timestamp(envelope["observed_at"]) + timedelta(days=7)
            due = expiry.isoformat()
            if expiry >= timestamp(slot["scheduled_at"]):
                continue
            physical.add(owner)
            supported.append((expiry, task_id, task, frozen, due))
        if not supported:
            continue
        chosen = supported if accepted else [max(supported, key=lambda value: (value[0], value[1]))]
        for _, task_id, task, refs, due in sorted(chosen, key=lambda value: (value[0], value[1])):
            if len(requests) >= limit:
                break
            source = {"route": task["route"], "request": deepcopy(task["request"]), "original_task_id": task_id,
                "original_acquisition_id": task["acquisition_id"], "expired_support_refs": refs,
                "refresh_due_at": due, "refresh_reason": "Selected support expires before the intended release."}
            needed = [{"code": "selected_support_refresh", "refs": refs}]
            row = _new_work(state, record["client_id"], cid, "source", needed, origins.get(cid), as_of, source=source)
            row["progress"].update(intended_releases=[slot["issue_id"]], next_retry_at=due,
                                   state="ready" if timestamp(due) <= now else "waiting")
            requests.append(row)
    changes = []
    refs = set(record.get("inventory_work_ids", []))
    for row in requests:
        refs.add(row["id"])
        old = state["batch_runs"].get(row["id"])
        if old is None:
            changes.append(change("batch_runs", row["id"], row))
        else:
            validate_inventory_work(old)
            # Joining another intended issue never resets the original state,
            # retry wait, linked run, counter or uncertain owner.
            merged = deepcopy(old)
            merged["progress"]["intended_releases"] = sorted(set(old["progress"].get("intended_releases", []))
                | set(row["progress"]["intended_releases"]))
            if merged != old:
                changes.append(change("batch_runs", row["id"], merged, old))
    if changes:
        memory.commit(key("admit-inventory-selected-work", changes), changes)
    return {"inventory_work_ids": sorted(refs), "shortfall": shortfall, "upcoming_slots": slots}
