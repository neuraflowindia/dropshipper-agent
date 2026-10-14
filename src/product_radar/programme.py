"""One resumable research programme; internal drafts, never client publication.

The existing source controller owns acquisitions, Jobs owns model intents, and
the report workflow owns frozen packages. This module joins those finite steps.
A work slice is not a report-sized finish condition. Failed targets wait with
their original input binding while healthy lanes continue.
"""

from collections import Counter
from copy import deepcopy
from datetime import date, timedelta
from functools import wraps
from pathlib import Path
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .research_memory import key
from .runtime_storage import records_root, runtime_path, runtime_root
from .storage import Journal, atomic_json, load_json, safe_path
from .operations import operation_writer, record_progress, record_error, record_event

VERSION = "research-programme-v1"
STAGES = ("profiles", "fit", "acquire", "match", "suppliers", "photos", "reports", "private_staging")
MODEL_ROUTE_FAILURES = {"AI_SUBSCRIPTION_LIMIT", "AI_RATE_LIMITED", "AI_MODEL_UNAVAILABLE",
    "AI_CHATGPT_SIGN_IN_REQUIRED", "AI_CODEX_NOT_INSTALLED", "AI_LOCAL_PERMISSION_DENIED",
    "AI_NETWORK_UNAVAILABLE", "AI_CLI_CONFIGURATION_REJECTED",
    "AI_PAID_CREDITS_USED", "AI_PLAN_LIMIT_CREDITS_PROTECTED", "AI_USAGE_READ_FAILED", "AI_SPEND_WATCH_UNAVAILABLE"}
INTEGRITY_FAILURES = {"RESEARCH_EVENTS_INVALID", "RESEARCH_CAPTURE_CONFLICT", "RESEARCH_CHANGE_INVALID",
    "RESEARCH_CHANGES_INVALID", "SYMLINK_PATH_REFUSED", "PROGRAMME_CONTROL_INVALID",
    "PROGRAMME_BINDING_CHANGED", "PROGRAMME_CONTROL_CONFLICT", "PROGRAMME_SERVICE_CONTROL_INVALID"}
LOCAL_WAIT = {"RUNNER_ALREADY_ACTIVE", "SAVED_FILE_UNAVAILABLE"}


def programme_service_state(memory):
    """An owner pause is separate from temporary route failures and run funds."""
    path = safe_path(memory.root, "programme_service") / "control.json"
    if not path.exists():
        return {"schema_version": 1, "enabled": True, "at": None}
    wrapper = load_json(path, 4096)
    if (not isinstance(wrapper, dict) or set(wrapper) != {"control", "hash"}
            or digest(wrapper["control"]) != wrapper["hash"]):
        raise SafetyError("PROGRAMME_SERVICE_CONTROL_INVALID")
    control = wrapper["control"]
    if (not isinstance(control, dict) or set(control) != {"schema_version", "enabled", "at"}
            or type(control["schema_version"]) is not int or control["schema_version"] != 1
            or type(control["enabled"]) is not bool):
        raise SafetyError("PROGRAMME_SERVICE_CONTROL_INVALID")
    try:
        timestamp(control["at"])
    except SafetyError:
        raise SafetyError("PROGRAMME_SERVICE_CONTROL_INVALID") from None
    return control


def set_programme_service_state(memory, *, enabled, as_of=None):
    """Pause/resume future stages without killing a request or resetting work."""
    if type(enabled) is not bool:
        raise SafetyError("PROGRAMME_SERVICE_CONTROL_INVALID")
    at = as_of or utc_now(); timestamp(at)
    with Journal(safe_path(memory.root, "programme_control_writer")).locked():
        current = programme_service_state(memory)
        if current["enabled"] == enabled and current["at"] is not None:
            return current
        control = {"schema_version": 1, "enabled": enabled, "at": at}
        atomic_json(safe_path(memory.root, "programme_service") / "control.json",
                    {"control": control, "hash": digest(control)})
        return control


def research_week(as_of=None):
    """Latest due Monday in India, including a missed start after reboot."""
    day = timestamp(as_of or utc_now()).astimezone(ZoneInfo("Asia/Kolkata")).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def release_slots(week, *, client_id="default", policy=None):
    from .release_index import scheduled_utc
    from .service import load_policy
    identifier(client_id)
    try:
        monday = date.fromisoformat(week)
    except (ValueError, TypeError):
        raise SafetyError("PROGRAMME_WEEK_INVALID") from None
    if monday.weekday() != 0:
        raise SafetyError("PROGRAMME_WEEK_INVALID")
    policy = load_policy() if policy is None else policy
    if (not isinstance(policy, dict) or policy.get("timezone") != "Asia/Kolkata"
            or policy.get("publication_time") != "09:00"
            or policy.get("publication_weekdays") != [1, 3, 5]):
        raise SafetyError("PROGRAMME_RELEASE_POLICY_CHANGED")
    return [{"issue_id": "issue-" + client_id + "-" + (monday + timedelta(days=offset)).isoformat(),
             "client_id": client_id, "date": (monday + timedelta(days=offset)).isoformat(),
             "revision": 1, "scheduled_at": scheduled_utc((monday + timedelta(days=offset)).isoformat(),
                 policy["publication_time"], zone=policy["timezone"])}
            for offset in policy["publication_weekdays"]]


def _binding(record):
    if not isinstance(record, dict):
        raise SafetyError("PROGRAMME_CONTROL_INVALID")
    try:
        binding = {field: deepcopy(record[field]) for field in
                ("kind", "programme_id", "source_run_id", "concept_ids", "week", "client_id", "release_slots", "location")}
        inventory = record.get("reporting_inventory_concept_ids", [])
        if (not isinstance(inventory, list) or any(not isinstance(cid, str) for cid in inventory)
                or len(set(inventory)) != len(inventory)
                or any(identifier(cid) != cid for cid in inventory)):
            raise SafetyError("PROGRAMME_CONTROL_INVALID")
        binding["reporting_inventory_concept_ids"] = deepcopy(inventory)
        return binding
    except KeyError:
        raise SafetyError("PROGRAMME_CONTROL_INVALID") from None


def _control_path(memory, programme_id):
    return safe_path(memory.root, "programme_runs", key("programme", identifier(programme_id))) / "pending-control.json"


def load_programme(memory, programme_id, *, state=None):
    """Recover a saved checkpoint without executing a source/model step."""
    programme_id = identifier(programme_id)
    state = memory.snapshot() if state is None else state
    canonical = state["batch_runs"].get(key("programme", programme_id))
    path = _control_path(memory, programme_id)
    if not path.exists():
        if canonical is None:
            raise SafetyError("PROGRAMME_REQUIRED")
        record = deepcopy(canonical)
    else:
        staged = load_json(path, 8 * 1024 * 1024)
        if (not isinstance(staged, dict) or set(staged) != {"record", "record_hash", "previous_hash"}
                or digest(staged["record"]) != staged["record_hash"]):
            raise SafetyError("PROGRAMME_CONTROL_INVALID")
        record = staged["record"]
        _binding(record)
        if canonical is not None and digest(canonical) != staged["record_hash"]:
            if (digest(canonical) != staged["previous_hash"]
                    or _binding(canonical) != _binding(record)):
                raise SafetyError("PROGRAMME_CONTROL_CONFLICT")
            _preserve_review_scopes(canonical, record)
        elif canonical is None and staged["previous_hash"] is not None:
            raise SafetyError("PROGRAMME_CONTROL_CONFLICT")
        record = deepcopy(record)
    _binding(record)
    if record.get("kind") != VERSION or record.get("programme_id") != programme_id:
        raise SafetyError("PROGRAMME_CONTROL_INVALID")
    _review_scopes(record)
    return record


def _save(memory, record, *, cache=None):
    """Keep the checkpoint before a short canonical save can be busy.

    A staged control record is only programme bookkeeping. It never certifies a
    capture, fit answer or paid attempt: each lower layer retains its own proof.
    A canonical conflict is not repaired by overwriting someone else's record.
    """
    record_id = key("programme", record["programme_id"])
    old = cache["canonical"] if cache is not None else memory.snapshot()["batch_runs"].get(record_id)
    if old is not None and _binding(old) != _binding(record):
        raise SafetyError("PROGRAMME_BINDING_CHANGED")
    if old is not None:
        _preserve_review_scopes(old, record)
    atomic_json(_control_path(memory, record["programme_id"]),
                {"record": record, "record_hash": digest(record), "previous_hash": digest(old) if old else None})
    try:
        if old != record:
            memory.put("batch_runs", record_id, deepcopy(record), previous=old)
    except SafetyError as error:
        if str(error) not in LOCAL_WAIT:
            raise
        return {"saved": "local_checkpoint", "reason": str(error)}
    if cache is not None:
        cache["canonical"] = deepcopy(record)
    return {"saved": "canonical", "reason": None}


def _prepare_programme_locked(memory, programme_id, *, source_run_id=None, concept_ids=None,
                      as_of=None, week=None, client_id="default", location=None,
                      reporting_inventory_concept_ids=None, reporting_inventory_reason=None):
    """Freeze the actual current product pool without any research calls."""
    from .source_coverage import _active_concepts
    at = as_of or utc_now(); timestamp(at)
    programme_id = identifier(programme_id); identifier(client_id)
    source_run_id = identifier(source_run_id or programme_id)
    week = week or research_week(at)
    state = memory.snapshot()
    ids = _active_concepts(state, concept_ids)
    inventory = _active_concepts(state, reporting_inventory_concept_ids) if reporting_inventory_concept_ids is not None else []
    inventory = sorted(set(inventory) - set(ids))
    proposed = {"kind": VERSION, "programme_id": programme_id, "source_run_id": source_run_id,
        "concept_ids": ids, "week": week, "client_id": client_id,
        "release_slots": release_slots(week, client_id=client_id), "location": deepcopy(location),
        "reporting_inventory_concept_ids": inventory}
    try:
        existing = load_programme(memory, programme_id)
    except SafetyError as error:
        if str(error) != "PROGRAMME_REQUIRED":
            raise
        existing = None
    if existing is not None:
        # Repreparing after discovery does not silently enlarge the frozen
        # pool. Explicit different IDs, week or location require a new run.
        if concept_ids is None:
            proposed["concept_ids"] = existing["concept_ids"]
        if reporting_inventory_concept_ids is None:
            proposed["reporting_inventory_concept_ids"] = existing.get("reporting_inventory_concept_ids", [])
        if _binding(existing) != proposed:
            raise SafetyError("PROGRAMME_BINDING_CHANGED")
        return existing
    record = {**proposed, "schema_version": 1, "plan_id": programme_id, "actions": {},
        "created_at": at, "updated_at": at, "cycles": 0, "status": "prepared",
        "deferrals": {}, "last_results": {}, "drafts": {}, "model_pause": None,
        "private_staging_receipts": {}, "reporting_inventory_reason": reporting_inventory_reason,
        "progress": None, "progress_signature": None, "next_retry_at": None,
        "discovery_state": "not_connected", "follow_on_reviews": {}, "client_publication_attempted": False}
    _save(memory, record)
    return record


@wraps(_prepare_programme_locked)
def prepare_programme(memory, programme_id, **options):
    """Freeze a pool under the same writer as selection and execution."""
    memory.snapshot()
    with Journal(safe_path(memory.root, "programme_writer")).locked():
        return _prepare_programme_locked(memory, programme_id, **options)


def _scope_state(state, record):
    ids = set(record["concept_ids"])
    return dict(state, concepts={cid: row for cid, row in state["concepts"].items() if cid in ids})


def _review_scopes(record):
    """Explicit immutable follow-on pools; never enlarge the original pool."""
    children = record.get("follow_on_reviews", {})
    if not isinstance(children, dict):
        raise SafetyError("PROGRAMME_CONTROL_INVALID")
    seen = set(record["concept_ids"]); runs = {record["source_run_id"]}
    scopes = [record]
    for scope_id, child in children.items():
        _binding(child)
        if (child.get("programme_id") != identifier(scope_id) or child.get("parent_programme_id") != record["programme_id"]
                or child.get("kind") != VERSION or child.get("follow_on_reviews")
                or child.get("reporting_inventory_concept_ids")
                or not isinstance(child.get("concept_ids"), list) or not child["concept_ids"]
                or any(not isinstance(cid, str) for cid in child["concept_ids"])
                or len(set(child["concept_ids"])) != len(child["concept_ids"])
                or any(identifier(cid) in seen for cid in child["concept_ids"])
                or identifier(child["source_run_id"]) in runs
                or any(child[field] != record[field] for field in ("week", "client_id", "location", "release_slots"))):
            raise SafetyError("PROGRAMME_CONTROL_INVALID")
        seen.update(child["concept_ids"]); runs.add(child["source_run_id"]); scopes.append(child)
    return scopes


def _acquisition_scopes(record, limit):
    """Share one uninterrupted source-pass limit; preserve admission rotation."""
    children = _review_scopes(record)[1:]
    if not children:
        return [(record, limit)]
    sweep = record.get("acquisition_sweep", 0)
    if type(sweep) is not int or sweep < 0:
        raise SafetyError("PROGRAMME_CONTROL_INVALID")
    ordinals = [child["acquisition_ordinal"] for child in children if "acquisition_ordinal" in child]
    if (any(type(value) is not int or value < 1 for value in ordinals)
            or len(set(ordinals)) != len(ordinals)):
        raise SafetyError("PROGRAMME_CONTROL_INVALID")
    highest = max(ordinals, default=0)
    for child in children:
        if "acquisition_ordinal" not in child:
            highest += 1; child["acquisition_ordinal"] = highest
    cursor = record.get("acquisition_cursor", {"last": 0, "ceiling": highest})
    if (not isinstance(cursor, dict) or set(cursor) != {"last", "ceiling"}
            or any(type(cursor[name]) is not int for name in cursor)
            or not 0 <= cursor["last"] <= cursor["ceiling"] <= highest):
        raise SafetyError("PROGRAMME_CONTROL_INVALID")
    cursor = dict(cursor)
    eligible = [child for child in children
                if cursor["last"] < child["acquisition_ordinal"] <= cursor["ceiling"]]
    if not eligible:
        # Freeze a finite rotation cohort. Constant new admissions join the
        # next sweep rather than keeping earlier products behind new arrivals.
        cursor = {"last": 0, "ceiling": highest}
        sweep += 1
    # A monotonic sweep prevents reversible cursor checkpoints from returning
    # to an earlier immutable value-derived event ID within the same round.
    # It is bookkeeping, not evidence of actual research progress.
    record["acquisition_sweep"] = sweep
    record["acquisition_cursor"] = cursor
    ordered = sorted(children, key=lambda row: (
        0 if cursor["last"] < row["acquisition_ordinal"] <= cursor["ceiling"] else
        1 if row["acquisition_ordinal"] <= cursor["last"] else 2,
        row["acquisition_ordinal"]))
    # Children with no ready actions do not consume the shared allowance or
    # hide another child's ready work. Each is visited at most once per pass.
    follow_on = [(child, None) for child in ordered]
    if limit == 1 and record["cycles"] % 2:
        return [*follow_on, (record, None)]
    # None means the remaining global allowance, including unused first quota.
    # A second original-pool pass runs only if it filled its first allowance.
    return [(record, (limit + 1) // 2), *follow_on, (record, None)]


def _priority_acquisition_plan(plan, record, priority):
    """Serve follow-on groups holding priority types first, keeping every share.

    The parent's capped first pass and its final remaining-allowance pass stay
    in their own places; only follow-on groups (each with no fixed share) are
    reordered among themselves, so no group borrows another's read quota.
    """
    if not priority:
        return plan
    children = sorted((pair for pair in plan if pair[0] is not record),
                      key=lambda pair: not bool(priority.intersection(pair[0]["concept_ids"])))
    ordered = iter(children)
    return [pair if pair[0] is record else next(ordered) for pair in plan]


def _preserve_review_scopes(old, record):
    previous = {row["programme_id"]: _binding(row) for row in _review_scopes(old)[1:]}
    current = {row["programme_id"]: _binding(row) for row in _review_scopes(record)[1:]}
    if any(current.get(scope_id) != binding for scope_id, binding in previous.items()):
        raise SafetyError("PROGRAMME_BINDING_CHANGED")


def _reporting_ids(record, state=None):
    intake = set()
    if record.get("reporting_intake_ids"):
        if state is None:
            raise SafetyError("INVENTORY_VERIFIED_STATE_REQUIRED")
        from .inventory_intake import reporting_intake_concept_ids
        intake = reporting_intake_concept_ids(state, record)
    return sorted(intake | set(record.get("reporting_inventory_concept_ids", []))
                  | {cid for scope in _review_scopes(record) for cid in scope["concept_ids"]})


def _register_discovered_products(memory, record, discovery, at):
    """Queue genuine newly admitted IDs for all lanes in a separate source run."""
    from .source_coverage import _active_concepts
    supplied = discovery.get("new_products_need_full_source_review", discovery.get("new_concept_ids", []))
    if not isinstance(supplied, list) or any(not isinstance(cid, str) for cid in supplied):
        raise SafetyError("PROGRAMME_DISCOVERY_RESULT_INVALID")
    # Reporting-only membership is not an actual completed research scope.
    known = {cid for scope in _review_scopes(record) for cid in scope["concept_ids"]}
    ids = sorted(set(supplied) - known)
    if not ids:
        return []
    ids = _active_concepts(memory.snapshot(), ids)
    if not ids:
        return []
    scope_id = key("programme-followon", [record["programme_id"], ids])
    child = {**_binding(record), "programme_id": scope_id,
        "source_run_id": key("source-followon", [record["programme_id"], ids]), "concept_ids": ids,
        "reporting_inventory_concept_ids": [],
        "parent_programme_id": record["programme_id"], "schema_version": 1, "plan_id": scope_id,
        "actions": {}, "created_at": at, "updated_at": at, "cycles": 0, "status": "prepared",
        "deferrals": {}, "last_results": {}, "drafts": {}, "model_pause": None,
        "progress": None, "next_retry_at": None, "discovery_state": "covered_by_parent_cycle",
        "client_publication_attempted": False}
    record.setdefault("follow_on_reviews", {})[scope_id] = child
    _review_scopes(record)
    return ids


def _programme_report_pool(memory, record, *, as_of, state=None, functional_packets=None):
    """Use new packets in explicit turns or an explicitly opted-in pure reader."""
    from .fit_policy import FIT_POLICY_ID
    from .report_selection import build_report_pool
    from .report_examples import RECOMMENDATION_CONTRACT
    from .work_turns import active_work_turn
    source = memory if state is None else SimpleNamespace(snapshot=lambda: state, evidence_type=memory.evidence_type)
    if functional_packets is not None and type(functional_packets) is not bool:
        raise SafetyError("PROGRAMME_REPORT_CONTRACT_INVALID")
    enabled = active_work_turn(memory) is not None if functional_packets is None else functional_packets
    options = {"recommendation_contract": RECOMMENDATION_CONTRACT} if enabled else {}
    return build_report_pool(source, as_of=as_of, client_id=record["client_id"], fit_policy_id=FIT_POLICY_ID, **options)


def refresh_reporting_intake(memory, record, *, as_of, pool=None, functional_packets=None):
    """Local boundary preparation; caller owns the existing programme writer."""
    from .inventory_intake import refresh_inventory_intake
    pool = _programme_report_pool(memory, record, as_of=as_of,
                                 functional_packets=functional_packets) if pool is None else pool
    result = refresh_inventory_intake(memory, record, as_of=as_of, pool=pool)
    record.update({name: result[name] for name in ("reporting_intake_ids", "inventory_work_ids")})
    # The intake scanner intentionally ignores a tail containing only its own
    # bookkeeping. Persist its actual committed checkpoint, not the transient
    # scan position: saving that ignored tail here would create another tail
    # on every unchanged waiting tick.
    control = memory.snapshot()["batch_runs"].get(key("inventory-cursor", record["client_id"]))
    record["inventory_refresh"] = {"cursor": deepcopy(control["cursor"] if control else result["cursor"]),
                                   "backlog_remaining": result["backlog_remaining"]}
    return result


def admit_due_inventory_work(memory, record, *, as_of, pool=None):
    """Admit only actual selected/upcoming-shortfall support obligations."""
    from .inventory_intake import admit_selected_inventory_work
    pool = _programme_report_pool(memory, record, as_of=as_of) if pool is None else pool
    result = admit_selected_inventory_work(memory, record, as_of=as_of, pool=pool)
    record["inventory_work_ids"] = result["inventory_work_ids"]
    record["inventory_release_need"] = {"shortfall": result["shortfall"],
        "upcoming_issue_ids": [slot["issue_id"] for slot in result["upcoming_slots"]]}
    return result


def _refresh_inventory_boundary(memory, record, *, as_of):
    before = deepcopy(record)
    try:
        # This boundary belongs only to the explicit cooperative selector and
        # runs before its work turn is opened. Its pure pool must already use
        # the same functional contract as the turn, not the legacy default.
        refreshed = refresh_reporting_intake(memory, record, as_of=as_of, functional_packets=True)
        # Explicit turns allocate current-format sets only, so only that stock
        # can be intended for an upcoming date or count against its shortfall.
        from .report_examples import RECOMMENDATION_CONTRACT
        pool = dict(refreshed["pool"], eligible=[row for row in refreshed["pool"]["eligible"]
                                                 if row.get("recommendation_contract") == RECOMMENDATION_CONTRACT])
        admit_due_inventory_work(memory, record, as_of=as_of, pool=pool)
        record.pop("inventory_preparation_wait", None)
    except (SafetyError, OSError) as error:
        if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
            raise
        # A malformed optional stock projection cannot grant report access,
        # but also cannot cancel independent discovery/source work.
        record["inventory_preparation_wait"] = str(error) if isinstance(error, SafetyError) else "INVENTORY_LOCAL_FILE_WAIT"
        # A stale "no shortfall" must not stop nearly-ready types finishing.
        record.pop("inventory_release_need", None)
    if record != before:
        _save(memory, record)
    return record


def _supplier_inventory_tasks_settled(state, concept_id, progress, *, as_of):
    """Only this obligation's actually bound outcomes can finish its checks.

    Zero readable capture inputs is not proof: its own original file may be
    unreadable, or a saved answer may be newer than an explicit cutoff. An
    unrelated unreadable capture must not hold up genuinely settled work.
    """
    from .source_coverage import TASK_KIND, RELEVANCE_KIND, _FINAL
    ids = progress.get("task_ids")
    if not ids or concept_id not in state["concepts"]:
        return False
    expected = digest(state["concepts"][concept_id]["description"])
    for ref in ids:
        task = state["query_tasks"].get(ref, {})
        target = task.get("target_results", {}).get(concept_id, {})
        if (task.get("kind") != TASK_KIND or task.get("source") != "supplier"
                or task.get("run_id") != progress.get("linked_run_id")
                or task.get("status") not in _FINAL
                or task.get("targets", {}).get(concept_id) != expected
                or target.get("description_hash") != expected
                or target.get("state") not in
                    {"completed_relevant", "completed_no_results", "insufficient", "failed", "blocked"}
                or not task.get("receipt_refs")):
            return False
        envelopes = [state["source_checks"][receipt]["envelope"] for receipt in task["receipt_refs"]]
        if any(timestamp(envelope["observed_at"]) > timestamp(as_of) for envelope in envelopes):
            return False
        # A later interpretation can settle a much older page receipt. Date
        # the exact target outcome too, not only when its page was collected.
        if not any(envelope["target_results"].get(concept_id) == target for envelope in envelopes):
            expected_result = dict(deepcopy(target), concept_id=concept_id)
            if not any(row.get("kind") == RELEVANCE_KIND and row.get("task_id") == ref
                       and row.get("source") == task["source"] and row.get("route") == task["route"]
                       and row.get("receipt_refs") == task["receipt_refs"]
                       and expected_result in row.get("results", [])
                       and timestamp(row["at"]) <= timestamp(as_of)
                       for row in state["source_checks"].values()):
                return False
    return True


def _run_inventory_stage(memory, record, context, stage):
    """Service narrow saved obligations within this same durable work turn."""
    from .inventory_intake import inventory_work_records, inventory_work_readiness
    from .work_turns import active_work_turn
    turn = active_work_turn(memory)
    if turn is None:
        return
    state = memory.snapshot()
    ready = {row["id"] for row in inventory_work_readiness(state, record, as_of=context["at"])["ready"]}
    purpose = {"fit": {"fit", "material_update"}, "acquire": {"source", "supplier"},
               "match": {"source", "match"}, "suppliers": {"supplier"}}.get(stage, set())
    ordinary = {cid for scope in _review_scopes(record) for cid in scope["concept_ids"]}
    def carried(row):
        source = row["binding"].get("source") or {}
        return row["binding"]["subject"]["concept_id"] not in ordinary or bool(source.get("refresh_due_at"))
    rows = [row for row in inventory_work_records(state, record)
            if row["binding"]["purpose"] in purpose
            and row["progress"].get("state") not in {"complete", "superseded"}
            and carried(row)
            and (stage != "acquire" or not row["progress"].get("source_collected"))
            and (stage not in {"match", "suppliers"} or row["progress"].get("source_collected"))]
    # Exact late saved replies may arrive without a canonical write and may
    # be applied during a provider wait. Such advisory probes are bounded by
    # the ordinary turn checkpoint; they never grant launch authority.
    rows.sort(key=lambda row: (row["id"] not in ready, row["progress"].get("last_served_turn", 0), row["id"]))
    for original in rows[:min(10, context.get({"fit": "fit_limit", "match": "match_limit", "suppliers": "supplier_limit"}.get(stage, "source_limit"), 10))]:
        row = deepcopy(original); progress = row["progress"]; binding = row["binding"]; cid = binding["subject"]["concept_id"]
        from .functional_types import effective_functional_definition
        latest = memory.snapshot()
        try:
            meaning = effective_functional_definition(latest, cid)["meaning_hash"] if cid in latest["concepts"] else None
        except SafetyError as error:
            if str(error) in INTEGRITY_FAILURES:
                raise
            progress.update(state="waiting", next_retry_at=None, reason=str(error))
            meaning = binding["subject"]["definition_hash"]
            # Do not use a malformed current subject as launch permission.
            from .research_memory import change
            if row != original:
                memory.commit(key("inventory-work-progress", [row["id"], row["progress"]]),
                    [change("batch_runs", row["id"], row, original)])
            continue
        if meaning != binding["subject"]["definition_hash"]:
            progress.update(state="superseded", reason="The accepted product meaning changed.")
        else:
            narrow = {**record, "concept_ids": [cid], "follow_on_reviews": {}, "reporting_inventory_concept_ids": [],
                "reporting_intake_ids": [], "inventory_work_ids": [], "deferrals": deepcopy(progress.get("deferrals", {}))}
            progress["last_served_turn"] = turn["turn_sequence"]
            result = None
            try:
                if stage == "fit" and binding["purpose"] == "material_update":
                    from .material_updates import REVIEW_KIND, current_material_update
                    expected = binding["needed"][0]["update_binding_hash"]
                    def exact_saved_update():
                        current_state = memory.snapshot()
                        for saved in current_state["source_checks"].values():
                            if saved.get("kind") == REVIEW_KIND and digest(saved.get("binding")) == expected:
                                prepared = {"state": "prepared", "binding": saved["binding"], "item": saved["input"]}
                                accepted = current_material_update(current_state, prepared, as_of=context["at"])
                                if accepted is not None:
                                    return accepted
                        return None
                    saved = exact_saved_update()
                    questions = _material_update_work(memory, narrow, as_of=context["at"])
                    if saved is None and not any(digest(q["binding"]) == expected for q in questions):
                        progress.update(state="superseded", next_retry_at=None,
                            reason="The current comparable update question changed; its original model owner is retained.")
                    elif saved is None:
                        result = _review_material_updates(memory, narrow, context)
                        saved = exact_saved_update()
                    if saved is not None:
                        progress.update(state="complete", completed_basis_hash=binding["basis_hash"], next_retry_at=None,
                            reason="The exact comparable update judgment is saved: " + saved["result"]["decision"] + ".")
                elif stage == "fit":
                    from .research_flow import current_assessment_fit
                    from .fit_policy import FIT_POLICY_ID
                    from .fit_description import FUNCTIONAL_CONTRACT
                    from .functional_types import functional_role_context
                    result = context.get("inventory_functions", {}).get("fit", _fit)(memory, narrow, context)
                    latest = memory.snapshot(); current = current_assessment_fit(latest, cid, expected_policy_id=FIT_POLICY_ID)
                    exact_contract = (functional_role_context(latest, cid) is None
                        or latest["assessments"].get(cid, {}).get("contract_version") == FUNCTIONAL_CONTRACT)
                    if current is not None and exact_contract:
                        progress.update(state="complete", completed_basis_hash=binding["basis_hash"], next_retry_at=None,
                                        reason="The current ten-point review is saved.")
                elif stage == "acquire" and not progress.get("source_collected"):
                    from . import source_coverage
                    prepare = getattr(source_coverage, "prepare_inventory_source_repair", None)
                    if binding["purpose"] == "supplier":
                        source = binding.get("source") or {}
                        task_id = source.get("original_task_id")
                        latest = memory.snapshot()
                        task = source_coverage.validate_inventory_source_original(latest, source, concept_id=cid)
                        if (task.get("source") != "supplier"
                                or task.get("targets", {}).get(cid) != digest(latest["concepts"][cid]["description"])):
                            raise SafetyError("SOURCE_INVENTORY_ORIGINAL_CHANGED")
                        prepared = {"run_id": task["run_id"], "task_ids": [task_id]}
                    elif prepare is None:
                        progress.update(state="waiting", next_retry_at=None, reason="inventory_source_repair_not_connected")
                        prepared = None
                    else:
                        prepared = prepare(memory, row, as_of=context["at"])
                    if prepared is not None:
                        if not prepared.get("run_id"):
                            progress.update(state="waiting", next_retry_at=prepared.get("next_retry_at"),
                                            reason=prepared.get("reason", "Inventory source preparation needs follow-up."))
                        else:
                            progress.update(linked_run_id=prepared["run_id"], task_ids=prepared["task_ids"])
                            result = source_coverage.execute_source_run(memory, prepared["run_id"], handlers=context["handlers"],
                                task_ids=prepared["task_ids"], action_limit=1, as_of=context["at"], shared_marketplace=True)
                            latest = memory.snapshot()
                            tasks = [latest["query_tasks"][ref] for ref in prepared["task_ids"]]
                            if all(task["status"] in source_coverage._FINAL for task in tasks):
                                progress.update(source_collected=True, state="ready", next_retry_at=None,
                                                reason="The actual source result is saved; finish its interpretation.")
                            else:
                                deadlines = [task["next_retry_at"] for task in tasks if task.get("next_retry_at")]
                                progress.update(state="waiting", next_retry_at=min(deadlines, key=timestamp) if deadlines else None,
                                                reason=tasks[0].get("reason", "The original source result needs follow-up."))
                elif stage == "match" and progress.get("source_collected"):
                    from .source_interpretation import _questions, review_source_results
                    run_id = progress["linked_run_id"]
                    result = review_source_results(memory, context["runner"], run_id=run_id,
                        task_ids=progress["task_ids"], concept_ids=[cid], limit=context["match_limit"], as_of=context["at"])
                    if not _questions(memory.snapshot(), run_id=run_id, task_ids=progress["task_ids"],
                                      concept_ids=[cid], as_of=context["at"]):
                        progress.update(state="complete", completed_basis_hash=binding["basis_hash"], next_retry_at=None,
                                        reason="The captured source check and its interpretation are complete.")
                elif stage == "suppliers" and progress.get("source_collected"):
                    result = context.get("inventory_functions", {}).get("suppliers", _suppliers)(memory, narrow, context)
                    if (result.get("remaining_inputs") == 0
                            and not set(result.get("failures", {})) - set(result.get("terminal_inputs", {}))
                            and _supplier_inventory_tasks_settled(memory.snapshot(), cid, progress,
                                                                 as_of=_context_time(context))):
                        progress.update(state="complete", completed_basis_hash=binding["basis_hash"], next_retry_at=None,
                                        reason=("The bounded supplier checks finished with a saved assessment limitation."
                                                if result.get("terminal_inputs") else
                                                "The bounded supplier checks have saved outcomes for this product."))
                    else:
                        progress.update(state="waiting", next_retry_at=None,
                                        reason="The saved supplier check still needs an outcome for this product.")
                if result is not None and progress.get("state") != "complete":
                    deadlines = [value["next_retry_at"] for value in narrow["deferrals"].values() if value.get("next_retry_at")]
                    if deadlines:
                        progress.update(state="waiting", next_retry_at=min(deadlines, key=timestamp),
                                        reason="The exact saved work is waiting for its recorded retry or reply.")
                progress["deferrals"] = narrow["deferrals"]
                if result is not None:
                    progress["last_result"] = result
            except (SafetyError, OSError) as error:
                if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                    raise
                reason = str(error) if isinstance(error, SafetyError) else "INVENTORY_LOCAL_FILE_WAIT"
                if reason not in {"WORK_TURN_SOURCE_LIMIT", "WORK_TURN_MODEL_LIMIT", "WORK_TURN_PURPOSE_LIMIT"}:
                    progress.update(state="waiting", next_retry_at=None, reason=reason)
        if row != original:
            from .research_memory import change
            memory.commit(key("inventory-work-progress", [row["id"], row["progress"]]),
                [change("batch_runs", row["id"], row, original)])


def _current_profiles(state):
    """Use the shared legacy/functional currentness rule and one profile index."""
    from .product_queries import current_query_profile
    accepted = set()
    profiles = {}
    for row in state["source_checks"].values():
        cid = row.get("concept_id")
        if row.get("kind") == "product_query_profile" and cid in state["concepts"]:
            profiles.setdefault(cid, []).append(row)
    for cid, records in profiles.items():
        try:
            if current_query_profile(state, cid, records=records) is not None:
                accepted.add(cid)
        except (SafetyError, KeyError, TypeError):
            continue
    return accepted


def _membership_turn_signal(state, record):
    """Pure current exact-example dependencies; no preparation or AI call."""
    from .functional_types import example_membership_preparation
    from .research_records import iter_functional_definitions
    scoped = {cid for scope in _review_scopes(record) for cid in scope["concept_ids"]}
    ids = sorted({row.get("concept_id") for _, row in iter_functional_definitions(state)
                  if row.get("concept_id") in scoped})
    signal, missing = [], set()
    for cid in ids:
        try:
            value = example_membership_preparation(state, cid)
            signal.append(value)
            if value["missing_ids"]:
                missing.add(cid)
        except (SafetyError, OSError) as error:
            if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                raise
            signal.append({"concept_id": cid, "reason": str(error) if isinstance(error, SafetyError)
                           else "FUNCTIONAL_EXAMPLE_LOCAL_FILE_WAIT"})
    return {"signal": signal, "missing_concept_ids": missing}


def _signature(state, record):
    """Only useful input/accepted work changes, not clock/diagnostic writes."""
    scopes = _review_scopes(record)
    ids = set(_reporting_ids(record, state)); runs = {scope["source_run_id"] for scope in scopes}
    tasks = {task_id: {field: row.get(field) for field in ("request_hash", "targets", "status", "generation",
        "tries", "receipt_refs", "target_results", "next_retry_at", "reconcile_required")}
        for task_id, row in state["query_tasks"].items() if row.get("run_id") in runs}
    from .catalogue_discovery import SEED_CYCLE_VERSION
    seed_progress = {ref: {"planned_branches": [row.get("branch_id") for row in batch.get("planned_branches", [])],
        "branches": {branch_id: {field: result.get(field) for field in
            ("state", "addition_ids", "pending_material_ids", "eligible_material_ids")}
            for branch_id, result in batch.get("branches", {}).items()}}
        for ref, batch in state["batch_runs"].items()
        if batch.get("kind") == SEED_CYCLE_VERSION and batch.get("programme_id") == record["programme_id"]}
    return digest({"concepts": {cid: row for cid, row in state["concepts"].items() if cid in ids},
        "assessments": {cid: row for cid, row in state["assessments"].items() if cid in ids},
        "profiles": sorted(_current_profiles(dict(state, concepts={cid: state["concepts"][cid] for cid in ids if cid in state["concepts"]}))),
        "scope_bindings": [_binding(scope) for scope in scopes], "tasks": tasks,
        "catalogue_additions": state["catalogue_additions"], "seed_progress": seed_progress,
        "market_reviews": state["market_reviews"], "supplier_assessments": state["supplier_assessments"],
        "observations": sorted(state["observations"]), "associations": state["associations"],
        "photos": {ref: row for ref, row in state["source_checks"].items()
                   if row.get("kind") == "public_product_image" and row.get("concept_id") in ids},
        "jobs": {ref: {field: row.get(field) for field in ("status", "result_hash", "result", "failure")}
                 for ref, row in state["jobs"].items()}, "drafts": record["drafts"]})


def _cooperative_signature(state, record, *, membership_signal=None):
    """Retry scheduling is a wake deadline, not accepted research progress.

    Keep the legacy fixed-point signature unchanged. Original-only recovery
    can update a retry time or failure diagnostic without producing new work;
    such writes must not renew cooperative rounds indefinitely.
    """
    view = dict(state,
        query_tasks={ref: {name: value for name, value in row.items() if name != "next_retry_at"}
                     for ref, row in state["query_tasks"].items()},
        jobs={ref: {name: value for name, value in row.items() if name != "failure"}
              for ref, row in state["jobs"].items()})
    from .product_queries import query_profile_scope_hash
    from .inventory_intake import inventory_work_records
    inventory_rows = inventory_work_records(state, record)
    subjects = set(_reporting_ids(record, state)) | {row["binding"]["subject"]["concept_id"] for row in inventory_rows
        if row["progress"].get("state") not in {"complete", "superseded"}}
    # An accepted meaning can change before a query profile exists. Retain
    # that real dependency in the wake key without treating display-only
    # changes to the definition as new research work.
    meanings = {cid: query_profile_scope_hash(state, cid) for cid in subjects
                if cid in state["concepts"]}
    from .fit_description import FUNCTIONAL_CONTRACT
    from .research_flow import assessment_item
    fit_inputs = {}
    for cid in meanings:
        try:
            fit_inputs[cid] = digest(assessment_item(state, cid, contract_version=FUNCTIONAL_CONTRACT))
        except SafetyError as error:
            if str(error) in INTEGRITY_FAILURES:
                raise
            # Legacy products have no functional input. A malformed reviewed
            # input is a stable repair dependency, not an endless clock wake.
            fit_inputs[cid] = (None if str(error) == "FIT_FUNCTIONAL_DEFINITION_REQUIRED"
                               else {"reason": str(error)})
    inventory = {row["id"]: {"binding_hash": row["binding_hash"],
        "state": row["progress"].get("state"), "linked_run_id": row["progress"].get("linked_run_id"),
        "completed_basis_hash": row["progress"].get("completed_basis_hash")}
        for row in inventory_rows}
    from .material_updates import REVIEW_KIND
    update_applications = {rid: row.get("record_hash") for rid, row in state["source_checks"].items()
        if row.get("kind") == REVIEW_KIND and row.get("binding", {}).get("concept_id") in subjects}
    from .functional_aliases import alias_projection
    aliases = alias_projection(state)
    scoped_aliases = {cid: aliases[cid] for cid in subjects if cid in aliases}
    cursor = record.get("inventory_refresh", {})
    return digest({"research": _signature(view, record), "query_meanings": meanings,
                   "functional_fit_inputs": fit_inputs, "inventory_work": inventory,
                   "material_update_reviews": update_applications,
                   "reviewed_identity_components": scoped_aliases,
                   "example_memberships": (_membership_turn_signal(state, record)["signal"]
                       if membership_signal is None else membership_signal),
                   "inventory_cursor": cursor.get("cursor") if cursor.get("backlog_remaining") else None})


def programme_progress(memory, programme_id, *, handlers=None, as_of=None, record=None, state=None):
    """Plain finite counts, never a fabricated whole-project completion percentage."""
    from .fit_review import PRACTICAL_CONTRACT
    from .fit_description import DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT
    from .functional_types import functional_role_context
    from .inventory_intake import inventory_work_records
    from .fit_policy import FIT_POLICY_ID
    from .research_flow import current_assessment_fit, evidence_indexes
    from .source_coverage import installed_source_handlers, source_run_progress, source_coverage
    from .source_coverage import _product_context
    at = as_of or utc_now(); timestamp(at)
    record = load_programme(memory, programme_id) if record is None else record
    state = memory.snapshot() if state is None else state
    handlers = installed_source_handlers(memory) if handlers is None else handlers
    scoped = _scope_state(state, record); indexes = evidence_indexes(state)
    fits, excluded = [], []
    for cid in record["concept_ids"]:
        concept = state["concepts"].get(cid)
        if concept is None:
            fits.append("product_missing"); continue
        try:
            _, is_excluded = _product_context(state, cid)
            accepted_contracts = ((FUNCTIONAL_CONTRACT,)
                if functional_role_context(state, cid, role="C") is not None
                else (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT))
        except SafetyError as error:
            if str(error) in INTEGRITY_FAILURES:
                raise
            fits.append("product_information_needs_repair"); continue
        if is_excluded:
            excluded.append(cid); continue
        assessment = state["assessments"].get(cid)
        current = current_assessment_fit(state, cid, expected_policy_id=FIT_POLICY_ID, indexes=indexes)
        fits.append("accepted" if current and assessment.get("contract_version") in
                    accepted_contracts else "needs_review")
    try:
        sources = source_run_progress(memory, record["source_run_id"], state=state)
    except SafetyError as error:
        if str(error) != "SOURCE_RUN_REQUIRED":
            raise
        sources = {"physical_questions": 0, "task_states": {}, "pending_preparation": 0,
                   "controller_acquisition_attempts": 0, "saved_receipts": 0}
    grid = source_coverage(memory, handlers=handlers, concept_ids=record["concept_ids"], as_of=at, state=state)
    photos = {row["concept_id"] for row in scoped["source_checks"].values()
              if row.get("kind") == "public_product_image" and row.get("state") == "usable"
              and row.get("concept_id") in scoped["concepts"]
              and row.get("description_hash") == digest(scoped["concepts"][row["concept_id"]]["description"])}
    follow_on = [programme_progress(memory, row["programme_id"], handlers=handlers, as_of=at,
                                   record=row, state=state) for row in _review_scopes(record)[1:]]
    memberships = _membership_turn_signal(state, dict(record, follow_on_reviews={}))
    return {"programme_id": record["programme_id"], "source_run_id": record["source_run_id"],
        "products": len(record["concept_ids"]), "fit_products": len(fits), "excluded_fit_products": len(excluded),
        "fit_states": dict(Counter(fits)),
        "current_query_profiles": len(_current_profiles(scoped) - memberships["missing_concept_ids"]),
        "products_needing_example_membership_preparation": len(memberships["missing_concept_ids"]),
        "source_work": sources, "product_route_questions": grid["product_route_questions"],
        "source_states": grid["states"], "actually_attempted_product_routes": grid["actually_attempted_product_routes"],
        "saved_usable_photo_records": len(photos), "draft_states": dict(Counter(row.get("state", "waiting") for row in record["drafts"].values())),
        "deferred_targets": len(record["deferrals"]), "model_pause": record.get("model_pause"),
        "setup_failures": deepcopy(record.get("setup_failures", {})),
        "discovery_state": record["discovery_state"], "next_retry_at": record.get("next_retry_at"),
        "service_enabled": programme_service_state(memory)["enabled"],
        "follow_on_reviews": follow_on, "follow_on_products": sum(row["products"] for row in follow_on),
        "reporting_products": len(_reporting_ids(record, state)),
        "reporting_intake_products": len(set(_reporting_ids(record, state)) - set(_reporting_ids(
            dict(record, reporting_intake_ids=[])))),
        "inventory_work_states": dict(Counter(row["progress"].get("state") for row in inventory_work_records(state, record))),
        "reporting_inventory_products": len(record.get("reporting_inventory_concept_ids", [])),
        "reporting_inventory_reason": record.get("reporting_inventory_reason"),
        "private_staging_states": dict(Counter(row.get("state", "waiting")
                                               for row in record.get("private_staging_receipts", {}).values())),
        "client_publication_attempted": False, "at": at}


def _summary_input(function, failures, name):
    """Optional reporting inputs cannot disable healthy research lanes."""
    try:
        return function()
    except (SafetyError, OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        code = str(error) if isinstance(error, SafetyError) else "PROGRAMME_SUMMARY_INPUT_UNREADABLE"
        if code in INTEGRITY_FAILURES or code in {"JOURNAL_CORRUPT_REVIEW_REQUIRED", "JOURNAL_STATE_INVALID",
                "EVENT_ID_CONFLICT"}:
            raise
        failures[name] = code
        return None


def _summary_financial(journal):
    """One local journal read, without extending ownership to other work."""
    if journal._locked:
        return journal.state()
    with journal.locked(timeout=0):
        return journal.state()


def _owner_measurement_input(function, failures, name):
    """Derivative measurement failure does not disable canonical research.

    This wrapper is only for the public baseline and reduced measurement cache.
    Core research/financial reads still use the stricter integrity boundary.
    An unsafe optional path is skipped, never followed or silently repaired.
    """
    try:
        return function()
    except (SafetyError, OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        failures[name] = str(error) if isinstance(error, SafetyError) else "PROGRAMME_OWNER_MEASUREMENT_UNREADABLE"
        return None


def _owner_effort_projection(checkpoint):
    """Keep owner checkpoints small; individual attempt rows stay in the cache."""
    if not isinstance(checkpoint, dict):
        return None
    names = ("version", "owner_only", "state", "checked_at", "cache_checked_at", "cache_age_seconds",
             "coverage", "receipt_freshness", "provider_calls", "model_calls", "financial_writes", "canonical_writes")
    value = {name: deepcopy(checkpoint[name]) for name in names if name in checkpoint}
    value["unavailable_reasons"] = dict(Counter(checkpoint.get("unavailable", {}).values()))
    value["diagnostic_reasons"] = dict(Counter(row.get("reason", "MODEL_EFFORT_DIAGNOSTIC_UNKNOWN")
        for row in checkpoint.get("diagnostics", []) if isinstance(row, dict)))
    refresh = checkpoint.get("refresh", {})
    if refresh.get("saved") is False and refresh.get("skipped") != "not_due":
        value.update(measurement_persistence="observed_not_saved", cache_checked_at=None, cache_age_seconds=None,
            receipt_freshness="These reduced measurements were observed, but their cache save failed.")
    else:
        value["measurement_persistence"] = "saved_cache" if value.get("cache_checked_at") else "not_available"
    return value


def _refresh_owner_measurements(memory, record, context, *, state):
    """Optional bounded housekeeping at a saved research boundary, not progress.

    The outer programme writer already owns this call. A local interval avoids
    even cache reads in rapid cycles; the persisted cache gate also survives a
    restart. No provider/model request, money write or history reset is made.
    """
    from .operational_effort_cache import refresh_model_effort_checkpoint
    if not programme_service_state(memory)["enabled"]:
        return None
    at = _context_time(context)
    prior = context.get("owner_measurements_checked_at")
    if prior is not None and 0 <= (timestamp(at) - timestamp(prior)).total_seconds() < 300:
        return None
    context["owner_measurements_checked_at"] = at
    failures = {}
    checkpoint = _owner_measurement_input(lambda: refresh_model_effort_checkpoint(
        memory, state, job_limit=25, as_of=at, minimum_interval_seconds=300), failures, "model_effort_checkpoint")
    if context.get("root") is not None:
        # Apify allowance and monthly money flags, from the local ledger only.
        from .spend_watch import refresh_ledger_allowances
        _owner_measurement_input(lambda: refresh_ledger_allowances(context["root"], memory.journal, as_of=at),
                                 failures, "spend_watch")
    refresh = checkpoint.get("refresh", {}) if isinstance(checkpoint, dict) else {}
    record["owner_measurement_refresh"] = {"at": at, "measurement": _owner_effort_projection(checkpoint),
        "refresh": {name: refresh[name] for name in ("probed_jobs", "reduced_jobs", "reused_jobs",
            "metadata_probes", "saved", "skipped", "next_refresh_at", "remaining_sweep_jobs") if name in refresh},
        "optional_input_failures": failures}
    return checkpoint


def programme_owner_summary(memory, record, *, root, state, as_of, financial_state=None,
                            account_status=None, catalogue=None, report_pool=None, allocations=None,
                            policy=None, effort_rows=None, forecast=None, cost_baseline=None,
                            effort_checkpoint=None):
    """One owner-only checkpoint from one verified state, never a new request.

    Explicit optional inputs are already verified outputs. Otherwise only small
    local configuration, one reduced effort cache and the saved financial
    journal are read. No provider, model, account-balance poll, original receipt
    scan, cache refresh or research-store replay occurs.
    """
    from .accounts import account_pool_status
    from .catalogue import load_catalogue
    from .catalogue_expansion import effective_catalogue
    from .fit_policy import FIT_POLICY_ID
    from .issue_allocation import allocate_issues
    from .operational_effort_cache import load_cached_model_effort
    from .programme_cost_snapshot import build_owner_cost_snapshot
    from .programme_summary import programme_cost_capacity_summary
    from .report_examples import RECOMMENDATION_CONTRACT
    from .report_selection import build_report_pool
    from .service import load_policy
    timestamp(as_of)
    failures = {}; root = Path(root)
    if financial_state is None:
        financial_state = _summary_input(lambda: _summary_financial(memory.journal), failures, "financial_state")
    if account_status is None and financial_state is not None:
        # Every financial consumer sees the same saved pair. Calling spendable
        # on this facade reuses the existing math without rereading the journal.
        financial = SimpleNamespace(state=lambda: financial_state)
        financial.spendable = lambda namespace, current_available=None: Journal.spendable(
            financial, namespace, current_available)
        account_status = _summary_input(lambda: account_pool_status(root, financial, now=as_of),
                                        failures, "account_status")
    if catalogue is None:
        catalogue = _summary_input(lambda: effective_catalogue(
            load_catalogue(root / "config/research/catalogue.json"), state), failures, "catalogue")
    if report_pool is None:
        report_pool = _summary_input(lambda: _programme_report_pool(memory, record,
            as_of=as_of, state=state, functional_packets=True), failures, "report_pool")
    allocation_basis = "supplied_plan" if allocations is not None else "current_research_proposal"
    legacy_compatible = None
    if report_pool is not None:
        scoped = _summary_input(lambda: _reporting_eligible(record, state, report_pool),
                                failures, "report_selection")
        if scoped is not None:
            legacy_compatible = len({row["concept_id"] for row in scoped
                                     if row.get("recommendation_contract") is None})
            if allocation_basis == "current_research_proposal":
                # The compatibility pool deliberately retains old packets.
                # Current proposals, stock and forecasts must instead share
                # the exact format selected by the functional report writer.
                report_pool = dict(report_pool, eligible=[row for row in scoped
                    if row.get("recommendation_contract") == RECOMMENDATION_CONTRACT])
        elif allocation_basis == "current_research_proposal":
            # A failed optional selection is unknown, not mixed-format stock.
            report_pool = None
    if policy is None:
        policy = _summary_input(lambda: load_policy(root / "config/service.json"), failures, "allocation_policy")
    if allocations is None and report_pool is not None:
        if policy is not None:
            allocations = _summary_input(lambda: allocate_issues(report_pool["eligible"],
                record["release_slots"], state["published_history"], state["issue_reservations"], policy=policy,
                identity_state=state),
                failures, "allocations")
    if cost_baseline is None:
        cost_baseline = _owner_measurement_input(lambda: load_json(
            root / "config/research/collection_cost_baseline.json", 16 * 1024), failures, "cost_baseline")
    if effort_rows is None:
        if effort_checkpoint is None:
            effort_checkpoint = _owner_measurement_input(lambda: load_cached_model_effort(
                memory, state, as_of=as_of), failures, "model_effort_checkpoint")
        effort_rows = {}
        if isinstance(effort_checkpoint, dict) and effort_checkpoint.get("model") is not None:
            effort_rows["model"] = effort_checkpoint["model"]
    else:
        effort_rows = deepcopy(effort_rows)
    cost_snapshot = build_owner_cost_snapshot(record, state, financial_state, baseline=cost_baseline,
        policy=policy, account_status=account_status, as_of=as_of, project_root=records_root(memory.root, root), report_pool=report_pool,
        model_effort=effort_rows.get("model") if isinstance(effort_rows, dict) else None)
    lineage = cost_snapshot["supporting_paid_capture_lineage"]
    if isinstance(effort_rows, dict) and "paid_attempt_ids" not in effort_rows and lineage.get("state") == "known":
        effort_rows["paid_attempt_ids"] = lineage["paid_attempt_ids"]
    summary = programme_cost_capacity_summary(record, state=state, financial_state=financial_state,
        account_status=account_status, catalogue=catalogue, report_pool=report_pool, allocations=allocations,
        effort_rows=effort_rows or None, forecast=forecast, as_of=as_of, cost_snapshot=cost_snapshot,
        allocation_basis=allocation_basis, project_root=records_root(memory.root, root))
    summary["report_supply"].update(legacy_compatible_eligible_scoped_products=legacy_compatible,
        current_proposal_recommendation_contract=(RECOMMENDATION_CONTRACT
            if allocation_basis == "current_research_proposal" else None))
    summary["model_effort_checkpoint"] = _owner_effort_projection(effort_checkpoint)
    summary["local_backup"] = deepcopy(record.get("owner_backup_status", {"state": "not_checked"}))
    summary["optional_input_failures"] = failures
    return summary


def _refresh_owner_backup(memory, record, context):
    """Optional local copies never certify research or grant new authority."""
    callback = context.get("backup_step")
    if callback is None or not programme_service_state(memory)["enabled"]:
        return None
    try:
        value = callback(memory, record, context)
        states = {"saved", "saved_with_limitations", "failed", "waiting", "disabled",
                  "not_configured", "owner_paused"}
        if not isinstance(value, dict) or value.get("state") not in states:
            raise SafetyError("PROGRAMME_BACKUP_RESULT_INVALID")
        fields = {"state", "day", "reason", "backup_id", "destination", "files", "bytes",
                  "file_count_basis", "planned_files",
                  "manifest_hash", "completed_at", "manifest_created_at", "limitations", "backup_action_started",
                  "reused_saved_result", "independent_backup_proved", "original_layout_resume_proved",
                  "provider_calls", "model_calls", "financial_writes", "research_writes"}
        status = {field: deepcopy(value[field]) for field in fields if field in value}
        if (status.get("destination", "data/backups") != "data/backups"
                or any(status.get(field, 0) != 0 for field in
                       ("provider_calls", "model_calls", "financial_writes", "research_writes"))
                or any(status.get(field, False) is not False for field in
                       ("independent_backup_proved", "original_layout_resume_proved"))):
            raise SafetyError("PROGRAMME_BACKUP_RESULT_INVALID")
    except Exception as error:
        code = str(error) if isinstance(error, SafetyError) else "PROGRAMME_LOCAL_BACKUP_FAILED"
        if not (code.isascii() and code.replace("_", "").isalnum() and code.isupper() and len(code) <= 160):
            code = "PROGRAMME_LOCAL_BACKUP_FAILED"
        status = {"state": "waiting", "reason": code, "destination": "data/backups"}
    status["at"] = _context_time(context)
    record["owner_backup_status"] = status
    return status


def run_programme_housekeeping(memory, programme_id, *, root, backup_step=None, as_of=None, clock=utc_now):
    """An executing retry-wait tick may save one due copy, never run research.

    The same programme leader is required even when source/model work is waiting.
    Read-only status commands never call this function.
    """
    if backup_step is not None and not callable(backup_step):
        raise SafetyError("PROGRAMME_STEPS_INVALID")
    with Journal(safe_path(memory.root, "programme_writer")).locked(), operation_writer():
        record = load_programme(memory, programme_id)
        at = as_of or clock(); timestamp(at)
        if not programme_service_state(memory)["enabled"]:
            return {"state": "owner_paused", "local_backup": None}
        context = {"root": Path(root), "clock": clock, "fixed_as_of": as_of, "at": at,
                   "backup_step": backup_step}
        if backup_step is None:
            return {"state": "not_configured", "local_backup": None}
        _save(memory, record)
        result = _refresh_owner_backup(memory, record, context)
        if result is not None:
            _save(memory, record)
        return {"state": "housekeeping_checked", "local_backup": result,
                "new_research_started": False, "client_publication_attempted": False}


def _due(record, stage, target, binding, at):
    saved = record["deferrals"].get(stage + ":" + target)
    return (not saved or saved["binding"] != binding
            or timestamp(saved["next_retry_at"]) <= timestamp(at))


def _context_time(context):
    """Real failures get real time; explicit historical/test cutoffs stay fixed."""
    at = context["clock"]() if context.get("clock") is not None and context.get("fixed_as_of") is None else context["at"]
    timestamp(at)
    return at


def _defer(record, stage, target, binding, reason, context):
    # A local fair-work ceiling is not an outage or a reason to delay healthy
    # work for five minutes. The next durable turn has its own saved allowance.
    if reason in {"WORK_TURN_MODEL_LIMIT", "WORK_TURN_PURPOSE_LIMIT", "WORK_TURN_SOURCE_LIMIT"}:
        return
    at = _context_time(context)
    record["deferrals"][stage + ":" + target] = {"stage": stage, "target": target,
        "binding": binding, "reason": reason, "at": at,
        "next_retry_at": (timestamp(at) + timedelta(seconds=context["retry_seconds"])).isoformat()}


def _clear(record, stage, target):
    record["deferrals"].pop(stage + ":" + target, None)


def _model_preparation_scope(memory, state, concept_ids, *, purpose, role, modes=None,
                             locally_reusable=(), review=False):
    """Avoid preparing proved-owner-free questions after a turn is spent.

    This is only a conservative selection hint. The ordinary helpers still
    validate exact current inputs and recover each original job, including
    derived batches, current reservations and carried batches. A possible
    owner is never proof that a new execution is allowed. An unfamiliar
    original shape stays in scope rather than concealing its saved reply.
    ``review`` lets definition questions see the turn's protected share.
    """
    from .work_turns import new_model_work_wait
    wait = new_model_work_wait(memory, purpose, state=state, review=review)
    if wait is None:
        return list(concept_ids), None
    owners = set(locally_reusable)
    meanings = {}
    for row in state.get("source_checks", {}).values():
        if not isinstance(row, dict):
            return list(concept_ids), None
        if isinstance(row.get("meaning_hash"), str) and isinstance(row.get("concept_id"), str):
            meanings.setdefault(row["meaning_hash"], set()).add(row["concept_id"])
    for job in state.get("jobs", {}).values():
        if not isinstance(job, dict):
            return list(concept_ids), None
        request = job.get("request")
        if not isinstance(request, dict):
            return list(concept_ids), None
        if request.get("role") != role:
            continue
        payload = request.get("payload")
        if not isinstance(payload, dict):
            return list(concept_ids), None
        if modes is not None and payload.get("mode") not in modes:
            continue
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return list(concept_ids), None
        for item in items:
            if not isinstance(item, dict):
                return list(concept_ids), None
            # These are explicit subject fields in the existing contracts,
            # not title guesses or a replacement for the owner's validator.
            subjects = set()
            if isinstance(item.get("concept_id"), str): subjects.add(item["concept_id"])
            if isinstance(item.get("id"), str) and item["id"] in state.get("concepts", {}):
                subjects.add(item["id"])
            if isinstance(item.get("comparison_subjects"), list):
                subjects.update(cid for cid in item["comparison_subjects"] if isinstance(cid, str))
            if isinstance(item.get("members"), list):
                subjects.update(row["concept_id"] for row in item["members"]
                    if isinstance(row, dict) and isinstance(row.get("concept_id"), str))
            product = item.get("product")
            if isinstance(product, dict):
                if isinstance(product.get("concept_id"), str): subjects.add(product["concept_id"])
                if isinstance(product.get("meaning_hash"), str):
                    subjects.update(meanings.get(product["meaning_hash"], ()))
                    # A scoped supplier request with a now-retired meaning
                    # has no current subject, but is still a recognised shape.
                    if not subjects: continue
            if not subjects:
                return list(concept_ids), None
            owners.update(subjects)
    selected = [cid for cid in concept_ids if cid in owners]
    waiting = [cid for cid in concept_ids if cid not in owners]
    return selected, ({"reason": wait, "concept_ids": waiting,
        "preparation_skipped": True, "model_job_created": False} if waiting else None)


def _observe(record, context, **values):
    # The runner is also used by smaller saved-work helpers. Diagnostics must
    # not make their optional whole-programme fields a new execution gate.
    programme_id, cycles = record.get("programme_id"), record.get("cycles")
    row = {"programme_id": programme_id, "stage": context.get("stage"),
           "cycle": cycles + 1 if type(cycles) is int else None,
           "review_scope_id": context.get("review_scope_id", programme_id), **values}
    record_progress(row)
    callback = context.get("on_progress")
    if callback is not None:
        try:
            callback(row)
        except Exception as error:
            record_error("progress_output_failed", error, stage=context.get("stage"))


class _ProgrammeRunner:
    """An account-wide model wait never disables public or saved-only work."""
    def __init__(self, runner, record, context):
        self.runner, self.record, self.context = runner, record, context
        self.identity = getattr(runner, "identity", "codex-chatgpt-high-v1")
        if context.get("cooperative_mode"):
            self.closed_retry_policy = {
                "as_of": lambda: _context_time(self.context),
                "retry_seconds": context["retry_seconds"],
                "retry_not_before": lambda: (self.record.get("model_pause") or {}).get("next_retry_at")}

    def check_new_execution(self):
        """A local pause is checked before intent, not saved as provider failure."""
        if not programme_service_state(self.context["memory"])["enabled"]:
            raise SafetyError("PROGRAMME_OWNER_PAUSED")
        pause = self.record.get("model_pause")
        if pause and timestamp(pause["next_retry_at"]) > timestamp(_context_time(self.context)):
            raise SafetyError(pause["reason"])
        guard = getattr(self.runner, "check_new_execution", None)
        if guard is not None:
            try:
                guard()  # the wrapped runner's own guard (the paid-credits watch)
            except SafetyError as error:
                self._pause_model_route(error)
                raise

    def _pause_model_route(self, error):
        if str(error) in MODEL_ROUTE_FAILURES:
            at = _context_time(self.context)
            self.record["model_pause"] = {"reason": str(error), "at": at,
                "next_retry_at": (timestamp(at) + timedelta(seconds=self.context["retry_seconds"])).isoformat()}

    def __call__(self, role, payload, folder):
        self.check_new_execution()
        count = len(payload["items"]) if isinstance(payload, dict) and isinstance(payload.get("items"), list) else None
        _observe(self.record, self.context, phase_state="runner_call_started", role=role, supplied_items=count)
        try:
            value = self.runner(role, payload, folder)
        except SafetyError as error:
            self._pause_model_route(error)
            _observe(self.record, self.context, phase_state="runner_call_failed", role=role,
                     supplied_items=count, reason=str(error))
            raise
        except Exception as error:
            _observe(self.record, self.context, phase_state="runner_call_failed", role=role,
                     supplied_items=count, error_type=type(error).__name__)
            raise
        self.record["model_pause"] = None
        _observe(self.record, self.context, phase_state="runner_call_returned", role=role, supplied_items=count)
        return value


def _profiles(memory, record, context):
    from .product_queries import ensure_query_profiles, query_profile_scope_hash
    from .source_coverage import _product_context
    from .functional_types import prepare_example_memberships
    from .research_records import iter_functional_definitions
    # Membership preparation is a deterministic write, not a model or a side
    # effect of the pure profile/report readers. Current C profiles still need
    # renewed exact example bindings after an actual offer recapture.
    initial = memory.snapshot()
    candidates = sorted({row.get("concept_id") for _, row in iter_functional_definitions(initial)
                         if row.get("concept_id") in record["concept_ids"]})
    # Legacy compact/catalogue wording and current membership refreshes are
    # local work. Only an owner-free new functional C question must wait.
    reusable = set(record["concept_ids"]) - set(candidates)
    reusable.update(row.get("concept_id") for row in initial["source_checks"].values()
                    if row.get("kind") == "product_query_profile")
    profile_ids, preparation_wait = _model_preparation_scope(memory, initial, record["concept_ids"],
        purpose="C:product_query_profile", role="C", modes={"product_query_profile"},
        locally_reusable=reusable)
    priority = set(context.get("priority_concept_ids", {}).get("profiles", []))
    # Local selection only: the frozen programme and child bindings retain
    # their original concept order and all original question owners.
    profile_ids.sort(key=lambda cid: cid not in priority)
    candidates = [cid for cid in candidates if cid in profile_ids]
    cursor = record.get("example_membership_cursor")
    if cursor in candidates:
        offset = candidates.index(cursor) + 1
        candidates = candidates[offset:] + candidates[:offset]
    membership_results = []
    for cid in candidates[:context["profile_limit"]]:
        try:
            prepared = prepare_example_memberships(memory, cid, as_of=context["at"])
            membership_results.append(dict(prepared, state="prepared"))
        except (SafetyError, OSError) as error:
            membership_results.append({"concept_id": cid, "state": "waiting", "reason": str(error)})
        record["example_membership_cursor"] = cid
    state = _scope_state(memory.snapshot(), record); current = _current_profiles(state)
    missing = []
    for cid in profile_ids:
        if cid in current or cid not in state["concepts"]:
            continue
        binding = query_profile_scope_hash(state, cid)
        try:
            _, excluded = _product_context(state, cid)
        except SafetyError as error:
            if str(error) in INTEGRITY_FAILURES:
                raise
            _defer(record, "profiles", cid, binding, str(error), context); continue
        if not excluded and _due(record, "profiles", cid, binding, context["at"]):
            missing.append(cid)
    if missing or preparation_wait is None:
        result = ensure_query_profiles(memory, context["runner"], concept_ids=missing,
            limit=context["profile_limit"], as_of=context["at"])
    else:
        result = {"records": [], "reused": [], "pending": [], "job_ids": [],
            "recovered_job_ids": [], "contract_repairs": [], "local_failures": [],
            "provider_requests": 0, "client_ready": False}
    for row in result["pending"]:
        cid = row["concept_id"]
        _defer(record, "profiles", cid, query_profile_scope_hash(state, cid), row["reason"], context)
    for cid in current:
        _clear(record, "profiles", cid)
    if membership_results:
        result["example_memberships"] = membership_results
    if preparation_wait is not None:
        result["preparation_wait"] = preparation_wait
    return result


def _material_update_work(memory, record, *, as_of, state=None):
    """Pure bounded-scope questions, not a new programme or collection lane."""
    state = memory.snapshot() if state is None else state
    ids = set(record["concept_ids"])
    from .functional_aliases import alias_projection
    identities = alias_projection(state)
    history_ids = {member for cid in ids for member in identities.get(cid, {}).get("member_ids", [cid])}
    if not any(row.get("status") == "published" and row.get("concept_id") in history_ids
               and row.get("client_id", "default") == record["client_id"] for row in state["published_history"].values()):
        return []
    pool = _programme_report_pool(memory, record, as_of=as_of, state=state, functional_packets=True)
    return [row for row in pool.get("needed_material_update_work", []) if row["binding"]["concept_id"] in ids]


def _review_material_updates(memory, record, context):
    from .material_updates import REVIEW_KIND, UPDATE_MODE, run_material_updates
    state = memory.snapshot()
    reusable = [row.get("binding", {}).get("concept_id") for row in state["source_checks"].values()
                if row.get("kind") == REVIEW_KIND]
    scope, preparation_wait = _model_preparation_scope(memory, state, record["concept_ids"],
        purpose="E", role="E", modes={UPDATE_MODE}, locally_reusable=reusable)
    if not scope and preparation_wait is not None:
        return {"selected": 0, "remaining": None, "outcomes": [], "preparation_wait": preparation_wait}
    work = _material_update_work(memory, dict(record, concept_ids=scope), as_of=context["at"], state=state)
    cursor = record.get("material_update_cursor")
    work.sort(key=lambda row: row["binding"]["concept_id"])
    ids = [row["binding"]["concept_id"] for row in work]
    if cursor in ids:
        offset = ids.index(cursor) + 1
        work = work[offset:] + work[:offset]
    selected = [row for row in work if _due(record, "material_updates", row["binding"]["concept_id"],
        digest(row["binding"]), context["at"])][:min(2, context["fit_limit"])]
    result = run_material_updates(memory, selected, context["runner"], as_of=context["at"], limit=2)
    bindings = {row["binding"]["concept_id"]: digest(row["binding"]) for row in selected}
    for row in result:
        cid = row["concept_id"]
        record["material_update_cursor"] = cid
        if row["state"] in ("accepted", "reused"):
            _clear(record, "material_updates", cid)
        else:
            _defer(record, "material_updates", cid, bindings[cid], row["reason"], context)
    return {"selected": len(selected), "remaining": max(0, len(work) - len(selected)), "outcomes": result,
            **({"preparation_wait": preparation_wait} if preparation_wait is not None else {})}


def _fit(memory, record, context):
    from .fit_review import prepare_practical_fit_reviews, run_fit_reviews
    from .fit_review import (PRACTICAL_CONTRACT, _bound_jobs, _current_base, _payload,
                            repair_practical_fit_review, fit_review_repair_jobs)
    from .research_ai import (MAX_JOB_BYTES, current_attempt, validate_results,
                              prepared_closed_model_retry_hint)
    from .fit_description import DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT
    from .functional_types import functional_role_context
    from .fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID
    from .research_flow import assessment_item, current_assessment_fit, evidence_indexes
    from .source_coverage import _product_context
    from .work_turns import active_work_turn
    turn = active_work_turn(memory)
    update_results = None
    # Explicit cooperative execution only. Alternate the first claim on the
    # existing ten model slots; a long fit backlog cannot starve useful updates.
    if turn is not None and turn["programme_turn_sequence"] % 2 == 0:
        update_results = _review_material_updates(memory, record, context)
    state = memory.snapshot(); indexes = evidence_indexes(state); missing, existing, named = [], [], []
    desired = {}
    def current_item(cid):
        return assessment_item(state, cid, indexes=indexes,
                               contract_version=FUNCTIONAL_CONTRACT if desired[cid] == FUNCTIONAL_CONTRACT else None)
    partial_bases = {}
    for base in state["source_checks"].values():
        binding = base.get("binding")
        requested = binding.get("requested_criterion_ids") if isinstance(binding, dict) else None
        if (base.get("kind") != "fit_review_base" or not isinstance(binding, dict)
                or base.get("state") != "prepared" or binding.get("mode") != "fit_partial_review"
                or binding.get("contract_version") not in (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT)
                or not isinstance(requested, list) or not 0 < len(requested) < len(ACTIVE_CRITERION_IDS)
                or any(type(number) is not int for number in requested)
                or requested != [number for number in ACTIVE_CRITERION_IDS if number in requested]
                or key("fit-review-apply", base["reference"]) in state["source_checks"]):
            continue
        partial_bases.setdefault(binding.get("concept_id"), []).append(base)
    for cid in record["concept_ids"]:
        concept = state["concepts"].get(cid)
        if concept is None:
            continue
        try:
            desired[cid] = (FUNCTIONAL_CONTRACT if functional_role_context(state, cid, role="C") is not None
                            else DESCRIPTION_CONTRACT)
            _, excluded = _product_context(state, cid)
        except SafetyError as error:
            if str(error) in INTEGRITY_FAILURES:
                raise
            _defer(record, "fit", cid, digest(concept), str(error), context); continue
        current = current_assessment_fit(state, cid, expected_policy_id=FIT_POLICY_ID, indexes=indexes)
        accepted_contracts = ((FUNCTIONAL_CONTRACT,) if desired[cid] == FUNCTIONAL_CONTRACT
                              else (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT))
        if current and state["assessments"][cid].get("contract_version") in accepted_contracts:
            parent_hash = digest(state["assessments"][cid])
            for base in partial_bases.get(cid, []):
                binding = base["binding"]
                if (binding.get("parent_assessment_hash") != parent_hash
                        or binding.get("input_hash") != state["assessments"][cid]["input_hash"]
                        or not isinstance(binding.get("review_reason"), str) or not binding["review_reason"].strip()
                        or timestamp(base["at"]) > timestamp(context["at"])):
                    continue
                try:
                    _current_base(state, base)
                except SafetyError as error:
                    if str(error) in INTEGRITY_FAILURES:
                        raise
                    if str(error) == "FIT_REVIEW_BASE_CHANGED":
                        continue
                    _defer(record, "fit", cid, digest(base["reference"]), str(error), context)
                    break
                if _due(record, "fit", cid, digest(base["reference"]), context["at"]):
                    named.append(base)
                break
            else:
                existing.append(cid); _clear(record, "fit", cid)
            # A deliberately prepared partial question owns its exact parent.
            # Do not clear its wait or replace it with another full assessment.
            continue
        if excluded:
            _clear(record, "fit", cid); continue
        binding = digest(current_item(cid))
        if _due(record, "fit", cid, binding, context["at"]):
            missing.append(cid)
    priority = set(context.get("priority_concept_ids", {}).get("fit", []))
    missing.sort(key=lambda cid: cid not in priority)
    # Activation of a new contract must not bypass an earlier sent question.
    # A base or jobs-table row is only preparation, not proof of dispatch.
    # Reconcile the original finite job owner without handing it a live runner.
    named_selected = named[:context["fit_limit"]]
    targets = missing[:max(0, context["fit_limit"] - len(named_selected))]
    recoverable, held, recovery_results = {}, set(), []

    def sent(job_id, *, include_prior=False):
        base_folder = safe_path(memory.root, "ai_jobs", job_id)
        folder = current_attempt(base_folder)
        folders = [folder]
        if include_prior and folder != base_folder:
            # current_attempt validates the finite (at most20) retry record.
            # An empty new retry does not erase the original sent question.
            folders = [base_folder] + [safe_path(base_folder, "retry-" + str(number))
                for number in range(1, int(folder.name[6:]) + 1)]
        paths = [attempt / name for attempt in folders
                 for name in ("intent.json", "result.json", "completion.json")]
        for path in paths:
            if path.is_symlink():
                raise SafetyError("SYMLINK_PATH_REFUSED")
        return any(path.exists() for path in paths)

    def hold(cid, base, reason, result=None):
        held.add(cid)
        _defer(record, "fit", cid, digest(current_item(cid)), reason, context)
        recovery_results.append(result or {"review_id": base["reference"],
            "status": "needs_reconciliation", "applied": False, "reason": reason})

    def checked_prepared_retry(job_id):
        # Only the current cooperative owner may resume a hash-bound automatic
        # retry prepared before a crash. An arbitrary empty/manual retry still
        # retains its original sent-question wait. The calling paths first
        # validate the full current base; Jobs rechecks all original inputs.
        if (turn is None or context["runner"] is None
                or getattr(context["runner"], "closed_retry_policy", None) is None):
            return None
        hint = prepared_closed_model_retry_hint(memory, job_id, as_of=context["at"],
            retry_seconds=context["retry_seconds"],
            not_before=(record.get("model_pause") or {}).get("next_retry_at"))
        return hint if hint["ready"] else None

    named_results = []
    for base in named_selected:
        cid = base["binding"]["concept_id"]
        try:
            jobs = _bound_jobs(state, base, _payload(base))
            related_jobs = [job_id for job_id, _ in jobs] + fit_review_repair_jobs(state, base,
                original_job_ids=[job_id for job_id, _ in jobs])
            if any(not sent(job_id) and sent(job_id, include_prior=True)
                   and checked_prepared_retry(job_id) is None for job_id in related_jobs):
                result = {"review_id": base["reference"], "status": "pending", "applied": False,
                          "reason": "AI_JOB_PENDING_RECONCILIATION"}
            else:
                result = run_fit_reviews(memory, context["runner"], [base["reference"]], limit=1)[0]
            if result.get("reason") in INTEGRITY_FAILURES:
                raise SafetyError(result["reason"])
        except (SafetyError, OSError) as error:
            reason = str(error) if isinstance(error, SafetyError) else "SAVED_FILE_UNAVAILABLE"
            if reason in INTEGRITY_FAILURES:
                raise
            result = {"review_id": base["reference"], "status": "needs_reconciliation", "applied": False,
                      "reason": reason}
        named_results.append(result)
        if result["status"] == "accepted":
            _clear(record, "fit", cid)
        else:
            _defer(record, "fit", cid, digest(base["reference"]), result.get("reason", result["status"]), context)

    for base in state["source_checks"].values():
        if base.get("kind") != "fit_review_base":
            continue
        binding = base.get("binding", {})
        cid = binding.get("concept_id")
        if cid not in targets or cid in held:
            continue
        old_contract = binding.get("contract_version")
        functional_transition = desired[cid] == FUNCTIONAL_CONTRACT and old_contract in (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT)
        if not functional_transition and (old_contract != PRACTICAL_CONTRACT
                or binding.get("mode") != "fit_assessment"
                or binding.get("requested_criterion_ids") != list(ACTIVE_CRITERION_IDS)):
            continue
        if functional_transition and binding.get("mode") not in ("fit_assessment", "fit_partial_review"):
            continue
        try:
            _current_base(state, base)
            jobs = _bound_jobs(state, base, _payload(base))
            if not jobs:
                continue
            if not sent(jobs[0][0]):
                if sent(jobs[0][0], include_prior=True):
                    if checked_prepared_retry(jobs[0][0]) is None:
                        hold(cid, base, "AI_JOB_PENDING_RECONCILIATION")
                        continue
                else:
                    continue
            if cid in recoverable:
                hold(cid, base, "FIT_REVIEW_JOB_CONFLICT")
            else:
                recoverable[cid] = (base, jobs[0][0])
        except (SafetyError, OSError) as error:
            reason = str(error) if isinstance(error, SafetyError) else "SAVED_FILE_UNAVAILABLE"
            if reason in INTEGRITY_FAILURES:
                raise
            if reason == "FIT_REVIEW_BASE_CHANGED":
                continue  # This is a different product/input, not a saved answer to the current question.
            hold(cid, base, reason)

    recovered = []
    for cid, (base, job_id) in recoverable.items():
        if cid in held:
            continue
        try:
            # A proved-ended obsolete question is not an unknown original.
            # Do not rerun an older fit contract merely to unlock the current
            # contract. Its immutable failed attempt and actual wait remain
            # authoritative; only a due, checked terminal outcome permits the
            # new semantic question below. Unflagged recovery is unchanged.
            if (turn is not None and context["runner"] is not None
                    and getattr(context["runner"], "closed_retry_policy", None) is not None
                    and base["binding"]["contract_version"] != desired[cid]):
                from .research_ai import closed_model_retry_hint
                terminal = closed_model_retry_hint(memory, job_id, as_of=context["at"],
                    retry_seconds=context["retry_seconds"],
                    not_before=(record.get("model_pause") or {}).get("next_retry_at"))
                if not terminal["ready"]:
                    terminal = checked_prepared_retry(job_id) or terminal
                if terminal["ready"]:
                    recovery_results.append({"review_id": base["reference"], "job_id": job_id,
                        "status": "obsolete_closed_failure", "applied": False,
                        "reason": "FIT_REVIEW_SUPERSEDED_AFTER_CLOSED_TRANSIENT_FAILURE",
                        "original_retry_signal": terminal["signal"]})
                    continue
            result = run_fit_reviews(memory, None, [base["reference"]], limit=1, repair_rejected=False)[0]
            if result.get("reason") in INTEGRITY_FAILURES:
                raise SafetyError(result["reason"])
            if result["status"] == "rejected":
                folder = current_attempt(safe_path(memory.root, "ai_jobs", job_id))
                if not (folder / "result.json").exists():
                    hold(cid, base, result.get("reason", "AI_JOB_PENDING_RECONCILIATION"),
                         dict(result, status="pending"))
                    continue
                # Reshaping a batch can return an operational/integrity error
                # in its failures map. Only an actual validated output
                # rejection, not that map alone, permits a contract upgrade.
                saved = load_json(folder / "result.json", MAX_JOB_BYTES)
                request = state["jobs"][job_id]["request"]
                if not isinstance(saved, dict) or saved.get("request_hash") != digest(request):
                    raise SafetyError("AI_JOB_RESULT_CONFLICT")
                checked = validate_results("E", request["payload"], saved.get("response"),
                    declared_version=base["binding"]["contract_version"])
                if checked["failures"].get(cid) != result.get("reason"):
                    hold(cid, base, result.get("reason", "FIT_REVIEW_JOB_CONFLICT"), dict(result, status="pending"))
                    continue
                # A saved correction, if actually sent, is another original
                # owner to replay. Never start its abandoned preparation here.
                repairs = fit_review_repair_jobs(state, base, original_job_ids=[job_id])
                dispatched_repairs = [saved_id for saved_id in repairs if sent(saved_id, include_prior=True)]
                if dispatched_repairs and not all(sent(saved_id) for saved_id in dispatched_repairs):
                    hold(cid, base, "AI_JOB_PENDING_RECONCILIATION")
                    continue
                if dispatched_repairs:
                    result = repair_practical_fit_review(memory, None, base["reference"])
            if result.get("reason") in INTEGRITY_FAILURES:
                raise SafetyError(result["reason"])
            if result["status"] == "accepted":
                recovered.append(cid); _clear(record, "fit", cid)
                recovery_results.append(result)
            elif result["status"] == "rejected":
                recovery_results.append(result)  # A completed rejection may enter the new explicit contract once.
            else:
                hold(cid, base, result.get("reason", result["status"]), result)
        except (SafetyError, OSError) as error:
            reason = str(error) if isinstance(error, SafetyError) else "SAVED_FILE_UNAVAILABLE"
            if reason in INTEGRITY_FAILURES:
                raise
            hold(cid, base, reason)
    # Reuse one indexed read for selection, and freeze only this finite group.
    # Never rebuild every historical observation index separately for all153
    # products on every source/matching round.
    # Only unfinished/new/changed products enter the explicit estimate
    # contract. Already accepted unchanged v2/v3 work above remains reusable.
    # An accepted OLD reply closes its exact original owner; it does not
    # answer the newly accepted functional-type question. Prepare that new
    # contract in the same finite turn only after original recovery settled.
    latest = memory.snapshot()
    recovered_current = {cid for cid in recovered if desired[cid] != FUNCTIONAL_CONTRACT
                         or latest["assessments"].get(cid, {}).get("contract_version") == FUNCTIONAL_CONTRACT}
    pending_targets = [cid for cid in targets if cid not in held and cid not in recovered_current]
    pending_targets, preparation_wait = _model_preparation_scope(memory, latest, pending_targets,
        purpose="E:fit", role="E", modes={None, "fit_assessment", "fit_partial_review"})
    rows = []
    for contract in (DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT):
        group = [cid for cid in pending_targets if desired[cid] == contract]
        if group or contract == DESCRIPTION_CONTRACT and not pending_targets and preparation_wait is None:
            rows.extend(prepare_practical_fit_reviews(memory, concept_ids=group,
                as_of=context["at"], contract_version=contract))
    order = {cid: index for index, cid in enumerate(pending_targets)}
    rows.sort(key=lambda row: order[row["concept_id"]])
    selected = [row["review_id"] for row in rows if row["status"] == "prepared"]
    results = run_fit_reviews(memory, context["runner"], selected, limit=context["fit_limit"]) if selected else []
    by_review = {row["review_id"]: row for row in rows if row.get("review_id")}
    for result in results:
        row = by_review[result["review_id"]]
        if result["status"] == "accepted":
            _clear(record, "fit", row["concept_id"])
        else:
            _defer(record, "fit", row["concept_id"], row["input_hash"], result.get("reason", result["status"]), context)
    for row in rows:
        if row["status"] == "accepted":
            _clear(record, "fit", row["concept_id"])
        elif row["status"] == "not_reviewable":
            _defer(record, "fit", row["concept_id"], digest(current_item(row["concept_id"])),
                   row["reason"], context)
    if turn is not None and update_results is None:
        update_results = _review_material_updates(memory, record, context)
    return {"prepared": len(selected) + len(named_selected), "outcomes": named_results + recovery_results + results,
            "current_accepted_reused": len(existing), "saved_v2_accepted_recovered": len(recovered),
            "remaining_to_prepare": max(0, len(missing) - len(rows) - len(held) - len(recovered_current)),
            "named_partial_reviews": len(named_selected), "named_partial_waiting": len(named) - len(named_selected),
            "selected_fit_states": dict(Counter(row["status"] for row in rows)),
            **({"preparation_wait": preparation_wait} if preparation_wait is not None else {}),
            **({"material_updates": update_results} if update_results is not None else {})}


def _acquire(memory, record, context):
    from .source_coverage import continue_source_run
    from .work_turns import active_work_turn
    before = memory.snapshot()
    original = {task_id: task for task_id, task in before["query_tasks"].items()
                if task.get("run_id") == record["source_run_id"]}
    previous_attempts = {ref for task in original.values() for ref in task.get("attempt_refs", [])}
    recovering = {task["acquisition_id"] for task in original.values() if task.get("acquisition_id")
                  and (task.get("reconcile_required") or task.get("status") == "dispatching")}
    def observed(row):
        _observe(record, context, phase_state="source_result", source_progress={name: row[name]
                 for name in ("task_id", "route", "state", "reason", "dispatch_started", "actual_attempt", "dispatch_uncertain", "timings") if name in row})
    shared_options = {"shared_marketplace": True} if active_work_turn(memory) is not None else {}
    result = continue_source_run(memory, record["source_run_id"], request_builder=context["request_builder"],
        handlers=context["handlers"], concept_ids=record["concept_ids"], action_limit=context["source_limit"],
        retry_delay_seconds=context["retry_seconds"], on_progress=observed if context.get("on_progress") else None,
        reuse_run_ids=context.get("reuse_run_ids"), reuse_discovery_cycle_ids=context.get("reuse_discovery_cycle_ids"),
        **shared_options)
    # A recovered receipt may still say dispatch_started=True for its original
    # request. It is not a fresh dispatch in this pass; neither is a shared
    # acquisition imported for a newly admitted product.
    current = memory.snapshot()
    current_tasks = {task_id: task for task_id, task in current["query_tasks"].items()
                     if task.get("run_id") == record["source_run_id"]}
    # A supplier adapter can prove an old terminal attempt during this call.
    # Its marker may be new, but its exact acquisition must already have owned
    # this unchanged task before the call. Never exempt a future generation.
    for task_id, task in current_tasks.items():
        old = original.get(task_id, {})
        acquisition_id = old.get("acquisition_id")
        if (acquisition_id and old.get("request_hash") == task.get("request_hash")
                and task.get("original_dispatch_recovery") == {
                    "version": "supplier-original-dispatch-recovery-v1",
                    "acquisition_id": acquisition_id, "request_hash": old["request_hash"]}):
            recovering.add(acquisition_id)
    # Shared tasks append their original borrowed acquisition first. A later
    # genuine retry of that same question must not be excluded with the borrow.
    borrowed = {task["attempt_refs"][0] for task in current_tasks.values()
                if task.get("reused_acquisition_ref") and task.get("attempt_refs")}
    new_attempts = ({ref for task in current_tasks.values() for ref in task.get("attempt_refs", [])}
                    - previous_attempts - recovering - borrowed)
    return dict(result, fresh_dispatches=len(new_attempts),
                callbacks_without_new_dispatch=result["handler_calls"] - len(new_attempts))


def _definition_review_order(state, concept_ids, *, defined=()):
    """Serve candidates closest to report eligibility first; ID order breaks ties.

    Older-format stock has already passed the checklist, market and supplier
    checks, so an accepted definition is its next current-format step. Rank
    products selected for an older-format report draft (in any hold state)
    first, then a current eligible checklist, a recorded supplier search and
    completed relevant source checks. This reads saved state only: no model,
    source or write, and it never decides whether a question is ready.
    """
    from .fit_policy import FIT_POLICY_ID
    from .research_flow import current_assessment_fit, evidence_indexes
    ids = list(concept_ids)
    pending = [cid for cid in ids if cid not in set(defined)]
    if len(pending) < 2:
        return ids
    wanted = set(pending)
    reported = {product.get("concept_id") for row in state.get("issue_reservations", {}).values()
                if isinstance(row, dict) and row.get("recommendation_contract") is None
                for product in row.get("products", []) if isinstance(product, dict)}
    suppliers = set()
    for row in state.get("supplier_assessments", {}).values():
        supplied = row.get("input") if isinstance(row, dict) else None
        product = supplied.get("product") if isinstance(supplied, dict) else None
        if isinstance(product, dict) and product.get("concept_id") in wanted:
            suppliers.add(product["concept_id"])
    relevant = Counter(cid for task in state.get("query_tasks", {}).values()
                       for cid, target in (task.get("target_results") or {}).items()
                       if cid in wanted and isinstance(target, dict) and target.get("state") == "completed_relevant")
    try:
        indexes = evidence_indexes(state)
    except (KeyError, TypeError, AttributeError):
        indexes = None  # A malformed hint input only loses the checklist tier.

    def rank(cid):
        if cid not in wanted:
            return (True, True, True, 1, cid)  # Already defined: readiness reports it as current.
        try:
            fit = (current_assessment_fit(state, cid, expected_policy_id=FIT_POLICY_ID, indexes=indexes)
                   if indexes is not None else None)
        except (SafetyError, KeyError, TypeError, ValueError):
            fit = None
        return (cid not in reported, not (fit or {}).get("eligible"), cid not in suppliers,
                -min(relevant[cid], 2), cid)
    return sorted(ids, key=rank)


def _match(memory, record, context):
    from .source_interpretation import _questions, review_source_results
    from .work_turns import active_work_turn
    aliases, definitions, examples, catalogue_scopes = None, None, None, None
    state = memory.snapshot()
    turn = active_work_turn(memory, state=state)

    def review_sources():
        # Rebuild at the actual execution boundary: on B-first rounds a new
        # accepted meaning may have introduced a genuine source question.
        questions = _questions(memory.snapshot(), run_id=record["source_run_id"], as_of=context["at"])
        bindings = {(row["task_id"], row["concept_id"]): digest(row) for row in questions}
        skip = [pair for pair, binding in bindings.items()
                if not _due(record, "match", ":".join(pair), binding, context["at"])]
        result = review_source_results(memory, context["runner"], run_id=record["source_run_id"],
                                      limit=context["match_limit"], as_of=context["at"], skip_targets=skip,
                                      priority_concept_ids=context.get("priority_concept_ids", {}).get("match", []))
        for row in result["outcomes"]:
            target = row["task_id"] + ":" + row["concept_id"]
            if row["state"] == "waiting":
                _defer(record, "match", target, bindings.get((row["task_id"], row["concept_id"])), row["reason"], context)
            else:
                _clear(record, "match", target)
        for row in result["reused"]:
            _clear(record, "match", row["task_id"] + ":" + row["concept_id"])
        return result

    # Alternate first claim on the same finite model allowance. Only an
    # actually due question about already captured, accepted-type material
    # earns source-first service. This pure choice prepares no model job.
    source_first = False
    if turn is not None and turn["programme_turn_sequence"] % 2 == 0:
        from .source_interpretation import functional_source_questions
        source_first = any(row["unknown_ids"] and _due(record, "match",
            row["task_id"] + ":" + row["concept_id"], digest(row), context["at"])
            for row in functional_source_questions(state, run_id=record["source_run_id"], as_of=context["at"]))
    result = review_sources() if source_first else None
    if source_first:
        # E may have consumed the final model slot or applied an original
        # answer. B preparation must see those exact current owners/counters.
        state = memory.snapshot()
    if turn is not None:
        from .functional_definition_review import MODE as DEFINITION_MODE, review_functional_definitions
        from .research_records import iter_functional_definitions
        definition_records = [row for _, row in iter_functional_definitions(state)]
        definition_ids, definition_wait = _model_preparation_scope(memory, state, record["concept_ids"],
            purpose="B", role="B", modes={DEFINITION_MODE},
            locally_reusable=[row.get("concept_id") for row in definition_records], review=True)
        definition_ids = _definition_review_order(state, definition_ids,
            defined={row.get("concept_id") for row in definition_records})
        skipped_definitions = {row["target"]: row["binding"] for row in record["deferrals"].values()
            if row["stage"] == "functional_definition" and isinstance(row["binding"], str)
            and len(row["binding"]) == 64 and all(c in "0123456789abcdef" for c in row["binding"])
            and timestamp(row["next_retry_at"]) > timestamp(context["at"])}
        try:
            if definition_ids or definition_wait is None:
                definitions = review_functional_definitions(memory, definition_ids, context["runner"],
                    as_of=context["at"], limit=min(5, context["match_limit"]),
                    skip_concept_bindings=skipped_definitions, retry_seconds=context["retry_seconds"],
                    retry_not_before=(record.get("model_pause") or {}).get("next_retry_at"))
            else:
                definitions = {"accepted": {}, "failures": {}, "bindings": {}}
        except SafetyError as error:
            if str(error) in INTEGRITY_FAILURES: raise
            definitions = {"accepted": {}, "failures": {"review": {"reason": str(error)}}, "bindings": {}}
        if definition_wait is not None:
            definitions["preparation_wait"] = definition_wait
        for cid, row in definitions["failures"].items():
            if (cid in definitions.get("skipped_concept_ids", [])
                    and skipped_definitions.get(cid) == definitions["bindings"].get(cid)):
                continue  # Preserve the original retry time on unchanged waits.
            _defer(record, "functional_definition", cid, definitions["bindings"].get(cid), row["reason"], context)
        for cid in definitions["accepted"]:
            _clear(record, "functional_definition", cid)
        from .functional_example_review import MODE as EXAMPLE_MODE, review_functional_examples
        example_ids, example_wait = _model_preparation_scope(memory, memory.snapshot(),
            [cid for cid in record["concept_ids"] if cid in definitions["accepted"]],
            purpose="B", role="B", modes={EXAMPLE_MODE})
        skipped_examples = {row["target"]: row["binding"] for row in record["deferrals"].values()
            if row["stage"] == "functional_example" and isinstance(row["binding"], str)
            and len(row["binding"]) == 64 and all(c in "0123456789abcdef" for c in row["binding"])
            and timestamp(row["next_retry_at"]) > timestamp(context["at"])}
        try:
            if example_ids:
                examples = review_functional_examples(memory, example_ids, context["runner"],
                    as_of=context["at"], limit=min(5, context["match_limit"]), skip_concept_bindings=skipped_examples,
                    retry_seconds=context["retry_seconds"],
                    retry_not_before=(record.get("model_pause") or {}).get("next_retry_at"))
            else:
                examples = {"accepted": {}, "failures": {}, "bindings": {}}
        except (SafetyError, OSError) as error:
            if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES: raise
            reason = str(error) if isinstance(error, SafetyError) else "FUNCTIONAL_EXAMPLE_LOCAL_FILE_WAIT"
            examples = {"accepted": {}, "failures": {"review": {"reason": reason}}, "bindings": {}}
        if example_wait is not None:
            examples["preparation_wait"] = example_wait
        for cid, row in examples["failures"].items():
            if (row["reason"] == "FUNCTIONAL_EXAMPLE_REVIEW_BATCH_LIMIT"
                    or cid in examples.get("skipped_concept_ids", []) and skipped_examples.get(cid) == examples["bindings"].get(cid)):
                continue
            _defer(record, "functional_example", cid, examples["bindings"].get(cid), row["reason"], context)
        for cid in examples["accepted"]:
            _clear(record, "functional_example", cid)
        if not record.get("parent_programme_id"):
            from .catalogue_scope_review import MODE as SCOPE_MODE, review_discovery_catalogue_scopes
            # Discovery can finish before the new type has a definition. Join
            # after that definition exists, once for the actual combined pool,
            # rather than repeating the same review in every follow-on lane.
            scope_ids = sorted({cid for scope in _review_scopes(record) for cid in scope["concept_ids"]})
            scope_ids, scope_wait = _model_preparation_scope(memory, memory.snapshot(), scope_ids,
                purpose="B", role="B", modes={SCOPE_MODE})
            skipped_scopes = {row["target"]: row["binding"] for row in record["deferrals"].values()
                if row["stage"] == "catalogue_scope" and isinstance(row["binding"], str)
                and timestamp(row["next_retry_at"]) > timestamp(context["at"])}
            try:
                if scope_ids or scope_wait is None:
                    catalogue_scopes = review_discovery_catalogue_scopes(memory, context["runner"],
                        catalogue=context.get("discovery_catalogue"), concept_ids=scope_ids,
                        as_of=context["at"], limit=min(5, context["match_limit"]), skip_bindings=skipped_scopes,
                        retry_seconds=context["retry_seconds"],
                        retry_not_before=(record.get("model_pause") or {}).get("next_retry_at"))
                else:
                    catalogue_scopes = {"outcomes": [], "waiting": {}, "bindings": {}}
            except (SafetyError, OSError) as error:
                if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES: raise
                reason = str(error) if isinstance(error, SafetyError) else "CATALOGUE_SCOPE_LOCAL_FILE_WAIT"
                catalogue_scopes = {"outcomes": [], "waiting": {"review": {"reason": reason}}, "bindings": {}}
            if scope_wait is not None:
                catalogue_scopes["preparation_wait"] = scope_wait
            for row in catalogue_scopes["outcomes"]:
                if row["state"] == "current":
                    _clear(record, "catalogue_scope", row["binding_key"])
            for iid in catalogue_scopes.get("current", {}):
                _clear(record, "catalogue_scope", iid)
            waits = {row["binding_key"]: row for row in catalogue_scopes["outcomes"] if row["state"] == "waiting"}
            waits.update(catalogue_scopes.get("waiting", {}))
            for iid, row in waits.items():
                binding = row.get("wake_binding", catalogue_scopes["bindings"].get(iid))
                if row["reason"] == "CATALOGUE_SCOPE_REVIEW_BATCH_LIMIT" or not _due(record, "catalogue_scope", iid, binding, context["at"]):
                    continue
                _defer(record, "catalogue_scope", iid, binding, row["reason"], context)
        from .functional_alias_review import MODE as ALIAS_MODE, match_functional_aliases
        # Exact equal-meaning joins need no model. Keep those deterministic
        # targets as well as every possible original semantic-review owner.
        alias_state = memory.snapshot()
        meaning_groups = {}
        for _, row in iter_functional_definitions(alias_state):
            meaning_groups.setdefault(row.get("meaning_hash"), set()).add(row.get("concept_id"))
        exact_ids = {cid for group in meaning_groups.values() if len(group) > 1 for cid in group}
        alias_ids, alias_wait = _model_preparation_scope(memory, alias_state, record["concept_ids"],
            purpose="B", role="B", modes={ALIAS_MODE}, locally_reusable=exact_ids)
        # The opt-in work turn admits only real accepted narrow definitions.
        # The helper leaves legacy concepts and source receipts unchanged.
        skip_aliases = {row["target"]: row["binding"] for row in record["deferrals"].values()
            if row["stage"] == "functional_alias" and timestamp(row["next_retry_at"]) > timestamp(context["at"])}
        try:
            if alias_ids or alias_wait is None:
                aliases = match_functional_aliases(memory, alias_ids, context["runner"],
                    reviewed_at=context["at"], limit=context["match_limit"], skip_group_bindings=skip_aliases,
                    retry_seconds=context["retry_seconds"],
                    retry_not_before=(record.get("model_pause") or {}).get("next_retry_at"))
            else:
                aliases = {"outcomes": [], "failures": {}}
        except SafetyError as error:
            if str(error) in INTEGRITY_FAILURES: raise
            aliases = {"outcomes": [], "failures": {"review": str(error)}}
        if alias_wait is not None:
            aliases["preparation_wait"] = alias_wait
        for row in aliases["outcomes"]:
            if row.get("decision") in {"waiting", "release_review_required"}:
                _defer(record, "functional_alias", row["group_id"], row["input_hash"],
                    row.get("reason", "FUNCTIONAL_ALIAS_REVIEW_PENDING"), context)
            else:
                _clear(record, "functional_alias", row["group_id"])
    # Saved source confirmations and genuine empty-result bindings can finish
    # deterministically without any E owner. Leave that finite local path
    # intact; Jobs still stops an actually fresh model request before its guard.
    if not source_first:
        result = review_sources()
    if aliases is not None:
        result = dict(result, functional_aliases=aliases, functional_definitions=definitions,
                      functional_examples=examples, catalogue_scopes=catalogue_scopes)
    return result


def _suppliers(memory, record, context):
    from .source_interpretation import review_supplier_results
    skip = [row["target"] for row in record["deferrals"].values() if row["stage"] == "suppliers"
            and timestamp(row["next_retry_at"]) > timestamp(context["at"])]
    # Normal F work can finish after the stage began. Its final saved-result
    # binding must use that completion time; fixed replay/test cutoffs stay fixed.
    cutoff = None if context.get("clock") is not None and context.get("fixed_as_of") is None else context["at"]
    from .functional_types import effective_functional_definition
    state = memory.snapshot()
    supplier_ids, preparation_wait = _model_preparation_scope(memory, state, record["concept_ids"],
        purpose="F", role="F")
    if not supplier_ids and preparation_wait is not None:
        # No capture was opened or judged in this branch. Do not report zero
        # remaining inputs (which would falsely finish an inventory obligation).
        return {"selected_capture_ids": [], "remaining_inputs": None, "reused_capture_ids": [],
            "failures": {}, "source_bindings": [], "skipped_captures": [], "completed_results": 0,
            "preparation_wait": preparation_wait}
    scoped_ids = [cid for cid in supplier_ids if effective_functional_definition(state, cid)["accepted"]]
    scoped_options = {"scoped_concept_ids": scoped_ids} if scoped_ids else {}
    result = review_supplier_results(memory, context["runner"], limit=context["supplier_limit"],
                                     as_of=cutoff, skip_capture_ids=skip, concept_ids=supplier_ids, **scoped_options,
                                     priority_concept_ids=context.get("priority_concept_ids", {}).get("suppliers", []))
    grouped = {}
    for row in result["selected_inputs"]:
        grouped.setdefault(row["capture_id"], []).append(row["item_id"])
    for capture_id, item_ids in grouped.items():
        if all(item_id in result["accepted"] or item_id in result.get("terminal_inputs", {}) for item_id in item_ids):
            _clear(record, "suppliers", capture_id)
        else:
            _defer(record, "suppliers", capture_id, capture_id,
                   next((result["failures"][item_id] for item_id in item_ids if item_id in result["failures"]),
                        "SUPPLIER_INTERPRETATION_PENDING"), context)
    for row in result.get("terminal_inputs", {}).values():
        _clear(record, "suppliers", row["capture_id"])
    return {**{field: result[field] for field in ("selected_capture_ids", "remaining_inputs", "reused_capture_ids", "failures", "source_bindings", "skipped_captures")},
            "completed_results": len(result["accepted"]),
            "terminal_inputs": result.get("terminal_inputs", {}),
            # The selected original-owned subgroup can finish, but a zero
            # for that subgroup must not finish untouched supplier subjects.
            **({"preparation_wait": preparation_wait, "remaining_inputs": None}
               if preparation_wait is not None else {})}


def _photos(memory, record, context):
    from .product_images import capture_product_image, current_public_image, product_image_readiness
    from .work_turns import active_work_turn, stage_item_wait
    pool = _programme_report_pool(memory, record, as_of=context["at"])
    state = memory.snapshot(); outcomes = []; processed = 0
    turn = active_work_turn(memory)
    admission_waits = {"WORK_TURN_MODEL_LIMIT", "WORK_TURN_SOURCE_LIMIT", "WORK_TURN_PURPOSE_LIMIT",
        "WORK_TURN_CLOSED", "PROGRAMME_OWNER_PAUSED", "PRODUCT_IMAGE_RETRY_WAIT",
        "PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING", "SOURCE_ACCESS_WAIT",
        "SOURCE_ACCESS_PROBE_PENDING", "SOURCE_ACCESS_ORIGINAL_RECOVERY_REQUIRED"}
    for cid, fact in sorted(pool["facts_by_concept"].items()):
        if cid not in record["concept_ids"]:
            continue
        # Keep the full finite set in its binding. A bad/finished first image
        # must not permanently conceal a genuine fourth alternative.
        candidates = sorted(fact.get("image_candidates", []), key=lambda row: row["relationship"] != "exact_product")
        binding = digest({"description": state["concepts"][cid]["description"], "candidates": candidates})
        if current_public_image(state, cid, candidates, media_root=context.get("media_root"),
                                as_of=_context_time(context), require_report_quality=True):
            _clear(record, "photos", cid); continue
        if turn is None and not _due(record, "photos", cid, binding, context["at"]):
            continue
        if not candidates:
            _defer(record, "photos", cid, binding, "No relevant original photo link was captured.", context)
            continue
        if processed >= context["photo_limit"]:
            break
        ready, photo_waits = [], []
        for candidate in candidates:
            hint = product_image_readiness(memory, cid, candidate, as_of=_context_time(context), state=state,
                                           require_report_quality=True)
            if hint["ready"]:
                ready.append((candidate, hint["local_saved"]))
            elif hint["next_retry_at"] is not None:
                photo_waits.append(hint["next_retry_at"])
        if not ready:
            _clear(record, "photos", cid)
            if photo_waits:
                record["deferrals"]["photos:" + cid] = {"stage": "photos", "target": cid, "binding": binding,
                    "reason": "PRODUCT_IMAGE_RETRY_WAIT", "at": _context_time(context),
                    "next_retry_at": min(photo_waits, key=timestamp)}
            # Unchanged terminal/uncertain or unsuitable responses have their
            # own saved wake signals, not a manufactured five-minute retry.
            continue
        allowance_wait = stage_item_wait(memory, "photos", state=state) if turn is not None else None
        if allowance_wait is not None:
            # This turn's photo items are spent. A fresh read would first
            # claim an owner and save local intents (about a minute) only to
            # be refused. Saved originals need no item; the rest record the
            # same admission wait without that attempt. The next turn has its
            # own allowance, so this is not an outage or a timed deferral.
            ready = [row for row in ready if row[1]]
            if not ready:
                processed += 1
                _observe(record, context, phase_state="photo_result", concept_id=cid,
                         counts={"candidate": 0, "candidates": len(candidates), "products": processed},
                         state="waiting", reason=allowance_wait, photo_applied=False,
                         next_action="continue_to_next_photo_product")
                outcomes.append({"concept_id": cid, "state": "waiting", "reason": allowance_wait, "references": []})
                continue
        processed += 1; result = {"concept_id": cid, "state": "not_attempted", "references": []}
        attempts = 0
        for ordinal, (candidate, local_saved) in enumerate(ready, 1):
            if attempts >= 3:
                break
            attempts += 1
            counts = {"candidate": ordinal, "candidates": len(ready), "products": processed}
            # These are advisory item updates, not a new timed network phase:
            # capture also verifies and applies saved originals locally.
            _observe(record, context, phase_state="photo_candidate", concept_id=cid,
                     counts=counts, state="running", next_action="save_or_recover_photo")
            started = time.monotonic()
            error_type = None
            try:
                options = {"media_root": context["media_root"]} if context.get("media_root") else {}
                if turn is not None:
                    options["shared_acquisitions"] = True
                    options["renewable_acquisitions"] = True
                    options["as_of"] = context["at"] if context.get("fixed_as_of") is not None else None
                if context.get("photo_fetcher"):
                    options["fetcher"] = context["photo_fetcher"]
                receipt = capture_product_image(memory, cid, candidate, **options)
                state = memory.snapshot()
                result.update(state=receipt["state"], reason=receipt["reason"])
                result["references"].append(receipt["reference"])
                if receipt["state"] == "usable" and current_public_image(state, cid, [candidate],
                        media_root=context.get("media_root"), as_of=_context_time(context),
                        require_report_quality=True) is None:
                    # Decoding proves a download, not a report photograph.
                    # Retain its original successful receipt unchanged and
                    # continue to another URL without report-progress credit.
                    result.update(state="omitted", reason="PRODUCT_IMAGE_REPORT_PHOTO_UNUSABLE")
            except (SafetyError, OSError) as error:
                if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                    _observe(record, context, phase_state="photo_result", concept_id=cid,
                             counts=counts, state="failed", reason=str(error), error_type=type(error).__name__,
                             photo_applied=False, next_action="stop_for_integrity_review",
                             timings={"photo_candidate": time.monotonic() - started})
                    raise
                result.update(state="waiting", reason=str(error) if isinstance(error, SafetyError) else "PRODUCT_IMAGE_LOCAL_SAVE_FAILED")
                if local_saved and isinstance(error, SafetyError) and str(error) == "PRODUCT_IMAGE_SAVED_BYTES_CHANGED":
                    # A failed adoption of already saved bytes made no fresh
                    # image request. Keep its error, but do not let three bad
                    # local originals conceal a healthy fourth alternative.
                    # Only this proved local failure is exempt; a fresh read
                    # or a generic error still consumes its bounded place.
                    attempts -= 1
                # An admission/backoff wait is not a failed website or a
                # broken local save. Keep its precise existing reason/state.
                if not isinstance(error, SafetyError) or str(error) not in admission_waits:
                    error_type = type(error).__name__
            # An existing verified photo was skipped before attempting this
            # product. Usable here therefore includes genuine local recovery
            # (missing bytes/new consumer), even when the physical reply was
            # reused. A subsequent unchanged pass skips it without credit.
            applied = result["state"] == "usable"
            # A redirect guard can return a failed receipt without contacting
            # the paused destination. Only its diagnostic classification is a
            # wait; the original capture/outcome/deferral remain unchanged.
            diagnostic_state = "waiting" if not applied and result.get("reason") in admission_waits else result["state"]
            _observe(record, context, phase_state="photo_result", concept_id=cid,
                     counts=counts, state=diagnostic_state, error_type=error_type,
                     reason="PRODUCT_IMAGE_USABLE_SAVED" if applied else result.get("reason"),
                     photo_applied=applied, next_action="continue_to_next_photo_product" if applied or attempts >= 3 or ordinal == len(ready)
                         else "continue_to_next_photo_candidate",
                     timings={"photo_candidate": time.monotonic() - started})
            if applied:
                break
        if result["state"] in {"usable", "omitted"}:
            _clear(record, "photos", cid)
        else:
            _defer(record, "photos", cid, binding, result.get("reason", "PRODUCT_IMAGE_PENDING"), context)
        outcomes.append(result)
    # Internal programme progress only; report/photo proof contracts stay
    # unchanged. Stage completion must not credit these same item events twice.
    return {"outcomes": outcomes, "item_progress_recorded": True,
            "apify_usd": "0", "client_publication_attempted": False}


def _report_revision(root, issue, facts, evidence_type):
    """Reuse an exact frozen package or append a revision; never overwrite it."""
    folder = safe_path(runtime_path(root, "data", "outputs"), issue["issue_id"])
    revisions = sorted((int(path.name[9:]), path) for path in folder.glob("revision-*")
                       if path.name[9:].isdigit() and path.is_dir())
    for revision, path in reversed(revisions):
        if not (path / "package-input.json").exists():
            continue
        old = load_json(path / "package-input.json", 8 * 1024 * 1024)
        candidate = deepcopy(issue); candidate["revision"] = revision
        from .issue_allocation import selection_fingerprint
        candidate["selection_hash"] = selection_fingerprint(candidate)
        comparable = lambda value: {name: row for name, row in value.items() if name not in {"input_hash", "selection_hash", "revision"}}
        if (isinstance(old, dict) and old.get("evidence_type") == evidence_type
                and comparable(old.get("issue", {})) == comparable(candidate) and old.get("product_facts") == facts):
            return deepcopy(old["issue"])
    result = deepcopy(issue); result["revision"] = revisions[-1][0] + 1 if revisions else 1
    from .issue_allocation import selection_fingerprint
    result["selection_hash"] = selection_fingerprint(result)
    return result


def _functional_report_revision(root, issue, facts, evidence_type, *, reservation=None, media_root=None, source_state=None):
    """Prepare optional images, choose the owner, then freeze exact display bytes.

    A content plan has no revision/hash dependency. Only after that plan is
    known can its owned receipt participate in the final selection hash.
    Existing immutable inputs win over building a replacement in place.
    """
    from .live_outputs import LOGO
    from .report_assets import prepare_report_asset_plan, freeze_report_assets, attach_report_assets, validate_report_asset_receipt, owned_plan_matches
    folder = safe_path(runtime_path(root, "data", "outputs"), issue["issue_id"])
    media = safe_path(runtime_path(root, "media", "live", "reports"), issue["issue_id"])
    revision_numbers = {int(path.name[9:]) for parent in (folder, media) for path in parent.glob("revision-*")
                        if path.name[9:].isdigit() and path.is_dir()}
    revisions = [(revision, folder / ("revision-" + str(revision))) for revision in sorted(revision_numbers)]
    minimum = reservation["revision"] + 1 if reservation else 1
    # Owned bytes are already authoritative after their receipt was saved,
    # even if input persistence did not finish before the process stopped.
    for candidate_revision, path in reversed(revisions):
        receipt_path = media / ("revision-" + str(candidate_revision)) / "assets" / "receipt.json"
        if candidate_revision < minimum or (path / "package-input.json").exists() or not receipt_path.exists():
            continue
        saved_receipt = validate_report_asset_receipt(load_json(receipt_path, 256 * 1024),
            issue_id=issue["issue_id"], revision=candidate_revision)
        recovered_facts, recovered_issue = deepcopy(facts), deepcopy(issue)
        if source_state is not None:
            from .product_images import current_public_image
            from .report_examples import freeze_functional_photo, validate_frozen_functional_photo
            from .frozen_records import read_original_json
            owned = {entry["source"]["owner_id"]: entry["source"] for entry in saved_receipt["plan"]["entries"]
                     if entry["source"]["kind"] == "product_photo"}
            selected_products = {row["concept_id"]: row for row in recovered_issue["products"]}
            for fact in recovered_facts:
                source = owned.get(fact["concept_id"], {})
                if fact.get("image_provenance") or "id" not in source:
                    continue
                candidates = [row for row in fact.get("image_candidates", [])
                    if row["observation_id"] == source["source_ref"] and row["url"] == source["source_url"]]
                photo = current_public_image(source_state, fact["concept_id"], candidates, media_root=media_root,
                    as_of=fact["example_as_of"], verify_bytes=False)
                if photo is None or any(photo.get(name) != source[name] for name in
                        ("id", "source_ref", "sha256", "source_url", "observed_at", "relationship")):
                    continue
                # A lost shared download is not a lost report-owned copy.
                # Recover only the same original URL/check and current narrow
                # match, not another seller's visually similar photograph.
                proof = freeze_functional_photo(source_state, photo)
                fact.update(image_path=photo["path"], image_provenance=photo, functional_photo_proof=proof)
                selected = selected_products[fact["concept_id"]]
                selected.update(image_provenance=deepcopy(photo), functional_photo_proof=deepcopy(proof))
                for table, records in proof["records"].items():
                    selected.setdefault("source_revisions", {}).setdefault(table, {}).update(
                        {rid: digest(read_original_json(original)) for rid, original in records.items()})
                validate_frozen_functional_photo(fact)
        if owned_plan_matches(saved_receipt, recovered_facts, media_root=media_root, logo_path=LOGO):
            freeze_report_assets({"plan": saved_receipt["plan"], "plan_hash": saved_receipt["plan_hash"],
                "staging_root": str(safe_path(runtime_path(root, "media", "live", "report-assets-stage")))},
                root=root, issue_id=issue["issue_id"], revision=candidate_revision)
            recovered_issue["revision"] = candidate_revision
            return attach_report_assets(recovered_issue, recovered_facts, saved_receipt, root=root)
    bundle = prepare_report_asset_plan(facts, staging_root=safe_path(runtime_path(root, "media", "live", "report-assets-stage")),
                                      media_root=media_root, logo_path=LOGO)
    comparable = lambda value: {name: row for name, row in value.items() if name not in {"input_hash", "selection_hash", "revision"}}
    for revision, path in reversed(revisions):
        source = path / "package-input.json"
        if not source.exists():
            continue
        old = load_json(source, 8 * 1024 * 1024)
        if not isinstance(old, dict) or not isinstance(old.get("issue"), dict):
            raise SafetyError("REPORT_FROZEN_INPUT_INVALID")
        original = old["issue"]
        receipt = original.get("report_asset_receipt", {})
        if not isinstance(receipt, dict):
            raise SafetyError("REPORT_ASSET_RECEIPT_INVALID")
        if (old.get("evidence_type") != evidence_type or receipt.get("plan_hash") != bundle["plan_hash"]
                or revision < minimum and (not reservation or original.get("selection_hash") != reservation.get("selection_hash"))):
            continue
        candidate = deepcopy(issue); candidate["revision"] = revision
        candidate, projected = attach_report_assets(candidate, facts, receipt, root=root)
        if comparable(original) == comparable(candidate) and old.get("product_facts") == projected:
            return deepcopy(original), deepcopy(old["product_facts"])
    # An owned asset receipt may survive before input or hold persistence.
    # Reuse it only for the exact content plan; never overwrite another plan.
    revision = max(minimum, revisions[-1][0] + 1 if revisions else 1)
    for candidate_revision, path in reversed(revisions):
        receipt_path = media / ("revision-" + str(candidate_revision)) / "assets" / "receipt.json"
        if candidate_revision < minimum or (path / "package-input.json").exists() or not receipt_path.exists():
            continue
        saved_receipt = validate_report_asset_receipt(load_json(receipt_path, 256 * 1024),
            issue_id=issue["issue_id"], revision=candidate_revision)
        if saved_receipt["plan_hash"] == bundle["plan_hash"]:
            revision = candidate_revision
            break
    selected = deepcopy(issue); selected["revision"] = revision
    receipt = freeze_report_assets(bundle, root=root, issue_id=selected["issue_id"], revision=revision)
    return attach_report_assets(selected, facts, receipt, root=root)


def _reporting_eligible(record, state, pool):
    """Keep the exact reporting/history/hold filter shared by drafts and counts."""
    scoped = set(_reporting_ids(record, state))
    from .inventory_intake import current_intake_records
    intakes = {row["binding"]["concept_id"]: row["binding"] for row in current_intake_records(state, record, pool=pool)}
    declared_intake = {row["binding"]["concept_id"] for row in current_intake_records(state, record)}
    # Membership is frozen, but eligibility/history/holds are checked again.
    # An unused older item published elsewhere cannot become an automatic
    # repeated "Updated" entry simply because this new week retained its ID.
    from .issue_allocation import ACTIVE_STATES, _published
    inventory = set(record.get("reporting_inventory_concept_ids", []))
    own_issues = {row["issue_id"] for row in record["release_slots"]}
    held_elsewhere = {product["concept_id"] for row in state["issue_reservations"].values()
                      if row.get("state") in ACTIVE_STATES and row.get("client_id", "default") == record["client_id"]
                      and row.get("issue_id") not in own_issues for product in row.get("products", [])}
    unavailable_inventory = (held_elsewhere | _published(state["published_history"], record["client_id"])) if inventory else set()
    return [row for row in pool["eligible"] if row["concept_id"] in scoped
                and row["concept_id"] not in held_elsewhere
                and (row["concept_id"] not in inventory or row["concept_id"] not in unavailable_inventory
                     or row["concept_id"] in intakes and intakes[row["concept_id"]]["status"] == "Updated")
                and (row["concept_id"] not in declared_intake
                     or row["concept_id"] in intakes)]


def _frozen_report_input(root, slot, draft, reservation, evidence_type):
    """Recover this logical issue's original writer input, never a new selection.

    A reservation can survive a crash before the package writer saves its
    input. Conversely, the input can survive before programme bookkeeping does.
    A shortfall has neither a frozen package nor a ten-product reservation.
    """
    folder = safe_path(runtime_path(root, "data", "outputs"), slot["issue_id"])
    revision = (draft.get("revision") if draft and draft.get("state") != "shortfall"
                and draft.get("selection_reserved") is not False else None)
    if revision is None and reservation and reservation.get("state") == "reserved":
        revision = reservation.get("revision")
    if revision is None:
        saved = sorted((int(path.name[9:]), path) for path in folder.glob("revision-*")
                       if path.name[9:].isdigit() and path.is_dir()
                       and (path / "package-input.json").exists())
        if not saved:
            return None
        revision = saved[-1][0]
    if type(revision) is not int or not 1 <= revision <= 9007199254740991:
        raise SafetyError("REPORT_FROZEN_INPUT_INVALID")
    path = safe_path(folder, "revision-" + str(revision)) / "package-input.json"
    if not path.exists():
        if not reservation or reservation.get("revision") != revision or reservation.get("state") != "reserved":
            raise SafetyError("REPORT_FROZEN_INPUT_MISSING")
        issue = {field: deepcopy(value) for field, value in reservation.items()
                 if field not in {"state", "reserved_at"}}
        return {"issue": issue, "product_facts": None, "evidence_type": evidence_type,
                "schema_version": 1}
    frozen = load_json(path, 8 * 1024 * 1024)
    if (not isinstance(frozen, dict) or set(frozen) - {"schema_version", "issue", "product_facts", "evidence_type", "presentation_source"}
            or type(frozen.get("schema_version")) is not int or frozen["schema_version"] != 1
            or frozen.get("evidence_type") != evidence_type or not isinstance(frozen.get("issue"), dict)
            or not isinstance(frozen.get("product_facts"), list)):
        raise SafetyError("REPORT_FROZEN_INPUT_INVALID")
    issue = frozen["issue"]
    if (any(issue.get(field) != slot[field] for field in ("issue_id", "client_id", "date"))
            or type(issue.get("revision")) is not int or issue["revision"] != revision
            or not isinstance(issue.get("input_hash"), str)
            or draft and draft.get("input_hash") is not None and draft["input_hash"] != digest(frozen)):
        raise SafetyError("REPORT_FROZEN_INPUT_INVALID")
    presentation = frozen.get("presentation_source")
    if presentation is not None and (not isinstance(presentation, dict)
            or type(presentation.get("revision")) is not int or not 1 <= presentation["revision"] < revision):
        raise SafetyError("REPORT_FROZEN_INPUT_INVALID")
    return frozen


def _current_report_selection(issue, eligible, state, record, *, expected_replaceable_hold=None):
    """Check the frozen selection, not unrelated catalogue/research changes."""
    from .issue_allocation import ACTIVE_STATES, _fingerprint, _held, _published, draft_ordinal, selection_fingerprint, _fresh_at_release
    from .material_updates import update_baseline_current
    from .opportunity_policy import OPPORTUNITY_FIELDS
    from .service import issue_fit_policy, validate_issue
    from .functional_aliases import alias_binding, alias_projection, selected_alias_binding
    aliases = alias_projection(state) if alias_binding(state) is not None else {}
    issue_fit_policy(issue)
    if issue.get("identity_context_hash") != selected_alias_binding(state,
            [p["concept_id"] for p in issue["products"]], aliases=aliases):
        return "REPORT_FUNCTIONAL_IDENTITY_CHANGED"
    if len({aliases.get(p["concept_id"], {}).get("canonical_id", p["concept_id"])
            for p in issue["products"]}) != len(issue["products"]):
        return "REPORT_DUPLICATE_FUNCTIONAL_IDENTITY"
    from .functional_lineage import hold_overlap
    selected_ids = [p["concept_id"] for p in issue["products"]]
    if aliases and any(hold_overlap(left, right, aliases)
           for offset, left in enumerate(selected_ids) for right in selected_ids[offset + 1:]):
        return "REPORT_DUPLICATE_FUNCTIONAL_IDENTITY"
    counts = validate_issue(issue["products"], _published(state["published_history"], issue["client_id"],
        exclude_issue_id=issue["issue_id"], aliases=aliases), _held(state["issue_reservations"],
        own_issue_id=issue["issue_id"], client_id=issue["client_id"], aliases=aliases, history=state["published_history"]))
    if (issue.get("counts") != counts or issue.get("ready_for_review") is not True
            or issue.get("shortfall") != 0
            or issue.get("evidence_fingerprint") != _fingerprint(issue["products"])
            or any(field not in issue for field in
                ("issue_id", "client_id", "revision", "date", "products", "evidence_fingerprint"))
            or issue.get("selection_hash") != selection_fingerprint(issue)):
        raise SafetyError("REPORT_FROZEN_SELECTION_INVALID")
    own = state["issue_reservations"].get(issue["issue_id"])
    # A saved, checked upgrade input can survive before its new hold. Permit
    # only this exact older reserved owner to be replaced; do not remove it
    # from the real state or waive any foreign hold/currentness.
    replacement = (expected_replaceable_hold is not None and own is not None
        and own == expected_replaceable_hold and own.get("state") == "reserved"
        and issue.get("recommendation_contract") == "functional-type-examples-v1"
        and type(own.get("revision")) is int and type(issue.get("revision")) is int
        and issue["revision"] > own["revision"])
    if (expected_replaceable_hold is not None and not replacement
            or own and (own.get("state") not in ACTIVE_STATES
                        or own.get("selection_hash") != issue["selection_hash"]) and not replacement):
        return "REPORT_RESERVATION_CHANGED"
    # Produced sets already own an active hold (counted from the reservations
    # themselves). An unfilled, missed or cancelled release date is not a set
    # and must not consume one of the first two bilingual positions.
    if issue.get("projected_ordinal") != draft_ordinal(issue["issue_id"], issue["date"],
            state["published_history"], state["issue_reservations"], client_id=issue["client_id"]):
        return "REPORT_LANGUAGE_ORDINAL_CHANGED"
    for product in issue["products"]:
        cid = product["concept_id"]; current = eligible.get(cid)
        if (current is None or current.get("fit_policy_id") != product.get("fit_policy_id")
                or any(current.get(field) != product.get(field) for field in
                    ("category", "identity_reviewed", "kpi_states", *OPPORTUNITY_FIELDS))
                or current.get("recommendation_contract") != product.get("recommendation_contract")
                or not _fresh_at_release(current, issue)):
            return "REPORT_CURRENT_ELIGIBILITY_CHANGED"
        if product.get("source_scope_hash") is not None and current.get("source_scope_hash") != product["source_scope_hash"]:
            return "REPORT_SELECTED_SOURCE_CHANGED"
        revisions = product.get("source_revisions", {})
        if not isinstance(revisions, dict) or any(table not in state or not isinstance(rows, dict)
                for table, rows in revisions.items()):
            raise SafetyError("REPORT_FROZEN_SELECTION_INVALID")
        from .report_source_facts import source_revisions_current
        if not source_revisions_current(state, product):
            return "REPORT_SELECTED_SOURCE_CHANGED"
        if product["status"] == "Updated" and not update_baseline_current(product,
                state["published_history"], state["issue_reservations"], issue["client_id"],
                member_ids=aliases.get(cid, {}).get("member_ids")):
            return "REPORT_USEFUL_UPDATE_BASELINE_CHANGED"
    return None


def _functional_report_allocations(eligible, slots, state, *, include_shortfalls=False):
    """Preview one disjoint new-format allocation without changing real holds.

    A current private legacy packet is replaceable only when the actual
    allocator can fill its slot with current type/example records. One shared
    chronological preview also prevents promising the same ten to two slots.
    """
    from .issue_allocation import allocate_issues
    from .report_examples import RECOMMENDATION_CONTRACT
    functional = [row for row in eligible
                  if row.get("recommendation_contract") == RECOMMENDATION_CONTRACT]
    if not slots or not functional and not include_shortfalls:
        return []
    preview = allocate_issues(functional, slots, state["published_history"],
        state["issue_reservations"], identity_state=state)
    if include_shortfalls:
        return preview["issues"]
    return [issue for issue in preview["issues"] if issue.get("ready_for_review") is True
            and issue.get("recommendation_contract") == RECOMMENDATION_CONTRACT]


def _report_upgrade_binding(issue, language):
    """Bind selected current producers and projections, not generated cutoffs.

    Example/proof ``as_of`` values are the time we froze them, not a change
    to the source research. Keep real expiry-sensitive supplier/route/support
    projections and every selected membership/order exact, however.
    """
    from .opportunity_policy import OPPORTUNITY_FIELDS
    issue_fields = ("issue_id", "client_id", "date", "recommendation_contract",
        "identity_context_hash", "projected_ordinal", "language_state", "fit_policy_id",
        "criterion_ids", "kpi_total", "opportunity_policy_id")
    product_fields = ("concept_id", "category", "identity_reviewed", "identity_conflicted",
        "kpi_states", "fit_policy_id", "criterion_ids", "kpi_total", "kpi_matches", "fit_tier",
        *OPPORTUNITY_FIELDS, "status", "material_update_reviewed", "update_reason", "material_update_review",
        "source_scope_hash", "source_revisions", "functional_definition_hash", "usable_until",
        "priority_support", "priority_support_refs", "support_valid_until", "example_products",
        "example_bindings", "example_coverage", "example_review_proofs", "example_selection",
        "supplier_scoped_terms", "supplier_scope_coverage", "supplier_terms_display_version",
        "source_route_contract", "source_route_checks")
    selected = {name: issue.get(name) for name in issue_fields}
    selected["products"] = [{name: row.get(name) for name in product_fields} for row in issue["products"]]
    return digest(["functional-report-upgrade-v2", selected, language])


def _saved_functional_upgrade(root, issue, facts, *, state, record, eligible, reservation,
                              evidence_type, language, at, media_root=None):
    """Recover only an exact checked unheld replacement, preserving its bytes."""
    from .issue_allocation import _fresh_at_release
    from .live_outputs import LOGO, build_issue_dataset, validate_dataset
    from .report_assets import freeze_report_assets, owned_plan_matches, validate_report_asset_receipt
    from .report_examples import RECOMMENDATION_CONTRACT
    if (not reservation or reservation.get("state") != "reserved"
            or state["issue_reservations"].get(issue["issue_id"]) != reservation):
        return None
    folder = safe_path(runtime_path(root, "data", "outputs"), issue["issue_id"])
    revisions = sorted((int(path.name[9:]), path) for path in folder.glob("revision-*")
        if path.name[9:].isdigit() and path.is_dir() and (path / "package-input.json").exists())
    binding = _report_upgrade_binding(issue, language)
    slot = {name: issue[name] for name in ("issue_id", "client_id", "date")}
    for revision, _ in reversed(revisions):
        if revision <= reservation["revision"]:
            continue
        saved = _frozen_report_input(root, slot, {"revision": revision}, reservation, evidence_type)
        original = saved["issue"]
        if (original.get("recommendation_contract") != RECOMMENDATION_CONTRACT
                or _report_upgrade_binding(original, language) != binding
                or _current_report_selection(original, eligible, state, record,
                    expected_replaceable_hold=reservation) is not None
                or any(not _fresh_at_release(row, original, as_of=at) for row in original["products"])):
            continue
        receipt = validate_report_asset_receipt(original.get("report_asset_receipt"),
            issue_id=original["issue_id"], revision=revision)
        if not owned_plan_matches(receipt, facts, media_root=media_root, logo_path=LOGO):
            continue
        freeze_report_assets({"plan": receipt["plan"], "plan_hash": receipt["plan_hash"],
            "staging_root": str(safe_path(runtime_path(root, "media", "live", "report-assets-stage")))},
            root=root, issue_id=original["issue_id"], revision=revision)
        validate_dataset(original, build_issue_dataset(original, saved["product_facts"],
            evidence_type=evidence_type), root=root)
        return deepcopy(original), deepcopy(saved["product_facts"])
    return None


def _prepare_reporting_examples(memory, record, context, *, state, eligible):
    """Save already-approved example bindings before final report selection.

    A rotated round can reach reports before profiles. Fit and source support
    can legitimately be current by then, while the pure report readers still
    see no materialized examples. Reuse the existing deterministic writer,
    not the profile/model lane or a new quota. Each eligible functional type
    is visited once; an item-local failure does not suppress healthy siblings.
    Genuinely absent or not-yet-approved examples remain optional.
    """
    from .functional_types import example_membership_preparation, prepare_example_memberships
    from .report_examples import RECOMMENDATION_CONTRACT
    result = {"attempted": 0, "prepared": [], "waiting": []}
    subjects = {row["concept_id"]: row for row in eligible
                if row.get("recommendation_contract") == RECOMMENDATION_CONTRACT}
    for cid, product in sorted(subjects.items()):
        if not programme_service_state(memory)["enabled"]:
            # Stop is an owner instruction, not a failed product or a retry.
            # Finish no more saves and leave unvisited types pending.
            result["state"] = "owner_paused"
            return result
        attempted = False
        binding = digest({"concept_id": cid,
                          "functional_definition_hash": product.get("functional_definition_hash")})
        try:
            signal = example_membership_preparation(state, cid)
            binding = digest(signal)
            if not signal["missing_ids"]:
                _clear(record, "report_examples", cid)
                continue
            if not _due(record, "report_examples", cid, binding, context["at"]):
                saved = record["deferrals"]["report_examples:" + cid]
                result["waiting"].append({"concept_id": cid, "reason": saved["reason"],
                    "next_retry_at": saved["next_retry_at"]})
                continue
            attempted = True
            result["attempted"] += 1
            _observe(record, context, phase_state="example_preparation_started", concept_id=cid,
                     counts={"missing_memberships": len(signal["missing_ids"])})
            prepared = prepare_example_memberships(memory, cid, as_of=context["at"])
            result["prepared"].append(prepared)
            _clear(record, "report_examples", cid)
            _observe(record, context, phase_state="example_preparation_finished", concept_id=cid,
                     counts={"bound_memberships": len(prepared["membership_ids"])})
        except (SafetyError, OSError) as error:
            if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                raise
            reason = str(error) if isinstance(error, SafetyError) else "REPORT_EXAMPLE_LOCAL_FILE_WAIT"
            record_error("report_example_preparation_failed", error, stage="reports", concept_id=cid)
            if attempted:
                # A successful canonical save can lose its reply. Only exact
                # current original bindings, not the exception or old view,
                # can recover it without another write or a needless delay.
                try:
                    current = example_membership_preparation(memory.snapshot(), cid)
                    if (not current["missing_ids"] and all(current[field] == signal[field]
                            for field in ("meaning_hash", "bindings", "pending_child_ids"))):
                        result["prepared"].append({"concept_id": cid, "meaning_hash": current["meaning_hash"],
                            "membership_ids": sorted(current["bindings"]), "pending_child_ids": current["pending_child_ids"],
                            "recovered_after_error": True, "original_failure": reason})
                        _clear(record, "report_examples", cid)
                        _observe(record, context, phase_state="example_preparation_recovered", concept_id=cid,
                                 state="recovered", reason=reason, counts={"bound_memberships": len(current["bindings"])})
                        continue
                except (SafetyError, OSError) as recovery_error:
                    if isinstance(recovery_error, SafetyError) and str(recovery_error) in INTEGRITY_FAILURES:
                        raise
                    record_error("report_example_recovery_failed", recovery_error, stage="reports", concept_id=cid)
            _defer(record, "report_examples", cid, binding, reason, context)
            waiting = {"concept_id": cid, "reason": reason}
            deferred = record["deferrals"].get("report_examples:" + cid)
            if deferred is not None:
                waiting["next_retry_at"] = deferred["next_retry_at"]
            result["waiting"].append(waiting)
    return result


def _reports(memory, record, context):
    from .fit_policy import FIT_POLICY_ID
    from .issue_allocation import (allocate_issues, allocation_snapshot_hash, reserve_issue,
                                  selection_fingerprint, draft_ordinal)
    from .live_outputs import build_issue_dataset
    from .report_workflow import prepare_review_package, freeze_review_input
    from .report_examples import RECOMMENDATION_CONTRACT
    from .service import load_policy
    pool = _programme_report_pool(memory, record, as_of=context["at"])
    state = memory.snapshot()
    eligible = _reporting_eligible(record, state, pool)
    example_preparation = _prepare_reporting_examples(memory, record, context, state=state, eligible=eligible)
    if example_preparation.get("state") == "owner_paused" or not programme_service_state(memory)["enabled"]:
        # Also catch Stop during the last save or an example-free selection.
        # No report input, reservation or writing may start from this pass.
        return {"state": "owner_paused", "eligible_products": len(eligible), "outcomes": [],
                "example_preparation": example_preparation,
                "approved": False, "client_publication_attempted": False}
    if example_preparation["attempted"]:
        # This also covers a commit whose reply was lost. Read current originals
        # before allocating; never freeze the preliminary empty example block.
        state = memory.snapshot()
        pool = _programme_report_pool(memory, record, as_of=context["at"], state=state)
        eligible = _reporting_eligible(record, state, pool)
    waiting_examples = {row["concept_id"] for row in example_preparation["waiting"]}
    eligible = [row for row in eligible if row["concept_id"] not in waiting_examples]
    language = load_policy()["ongoing_pdf_language"]
    outcomes, pending, frozen, fixed_ids, adoption_wait = [], [], [], set(), set()
    functional_slots, functional_reasons = set(), {}
    eligible_by_id = {row["concept_id"]: row for row in eligible}
    for slot in record["release_slots"]:
        draft = record["drafts"].get(slot["issue_id"])
        reservation = state["issue_reservations"].get(slot["issue_id"])
        if reservation and reservation.get("recommendation_contract") == RECOMMENDATION_CONTRACT:
            functional_slots.add(slot["issue_id"])
        if reservation and reservation.get("state") in {"uploaded", "published", "cancelled"}:
            # A local research round has no authority to replace/reactivate a
            # remotely managed or explicitly cancelled logical issue.
            result = {"issue_id": slot["issue_id"], "state": "protected_issue", "complete": False,
                      "reason": "REPORT_RESERVATION_PROTECTED", "client_publication_attempted": False}
            if draft:
                record["drafts"][slot["issue_id"]] = dict(draft, **result,
                    retained_complete=draft.get("complete") is True or draft.get("retained_complete") is True)
            outcomes.append(result)
            continue
        try:
            # A checked replacement hold may survive a lost programme-save
            # reply. Its newer canonical owner wins over older draft metadata;
            # never apply the old input hash to the new immutable input.
            recovered_draft = None if _newer_report_hold(draft or {}, reservation) else draft
            saved = _frozen_report_input(context["root"], slot, recovered_draft, reservation, memory.evidence_type)
            if saved is None:
                if (context.get("cooperative_mode") is True
                        and timestamp(slot["scheduled_at"]) <= timestamp(context["at"])):
                    # Explicit turns never start a set for a release time that
                    # has already passed: it would be a late report that could
                    # take a bilingual position. Existing holds and their
                    # revisions above are unaffected (as in inventory intake).
                    result = {"issue_id": slot["issue_id"], "state": "release_passed", "complete": False,
                              "approved": False, "client_ready": False, "reason": "REPORT_RELEASE_TIME_PASSED",
                              "client_publication_attempted": False}
                    record["drafts"][slot["issue_id"]] = dict(draft or {}, **result)
                    outcomes.append(result)
                    continue
                pending.append(slot); continue
            issue = saved["issue"]; facts = saved["product_facts"]
            if issue.get("recommendation_contract") == RECOMMENDATION_CONTRACT:
                functional_slots.add(issue["issue_id"])
            reason = _current_report_selection(issue, eligible_by_id, state, record)
            if reason is None and facts is None:
                facts = [pool["facts_by_concept"][row["concept_id"]] for row in issue["products"]]
            if facts is not None:
                dataset = build_issue_dataset(issue, facts, evidence_type=memory.evidence_type)
                if reason is None and issue.get("recommendation_contract") == RECOMMENDATION_CONTRACT:
                    from .report_assets import freeze_report_assets
                    from .live_outputs import validate_dataset
                    receipt = issue["report_asset_receipt"]
                    try:
                        freeze_report_assets({"plan": receipt["plan"], "plan_hash": receipt["plan_hash"],
                            "staging_root": str(safe_path(runtime_path(context["root"], "media", "live", "report-assets-stage")))},
                            root=context["root"], issue_id=issue["issue_id"], revision=issue["revision"])
                        validate_dataset(issue, dataset, root=context["root"])
                    except SafetyError as error:
                        if str(error) not in {"REPORT_ASSET_BYTES_MISSING", "REPORT_ASSET_EXACT_RECOVERY_MISSING",
                                "REPORT_ASSET_OWNED_BYTES_CHANGED", "REPORT_DISPLAY_BYTES_CHANGED"}:
                            raise
                        # The old immutable packet stays untouched. Only this
                        # unprotected issue may propose a new bounded revision;
                        # its old hold is swapped after the new input is checked.
                        functional_reasons[issue["issue_id"]] = str(error)
                        pending.append(slot)
                        continue
            if reason is not None:
                # This issue owns its snapshot; unrelated productive research
                # must not make its replacement wait for a global fixed point.
                # Exact selected-input/revision/G reuse already coalesces
                # unchanged work without another waiting mechanism.
                if reason not in {"REPORT_RESERVATION_CHANGED"}:
                    functional_reasons[issue["issue_id"]] = reason
                    pending.append(slot); continue
                result = {"issue_id": issue["issue_id"], "revision": issue["revision"],
                          "state": "frozen_inputs_changed", "complete": False, "approved": False,
                          "client_ready": False, "reason": reason,
                          "retained_revision": issue["revision"], "retained_complete": bool(draft and
                              (draft.get("complete") is True or draft.get("retained_complete") is True))}
                if draft and draft.get("package_hash"):
                    result["package_hash"] = draft["package_hash"]
                record["drafts"][issue["issue_id"]] = dict(draft or {}, **result)
                fixed_ids.update(row["concept_id"] for row in issue["products"])
                outcomes.append(result); continue
            fixed_ids.update(row["concept_id"] for row in issue["products"])
            if reservation is None:
                # Adopt a genuine old/orphan frozen input before allocating
                # later issue ordinals. The same existing reservation gate
                # rechecks its facts; no synthetic hold is supplied to allocation.
                adoption_wait.add(issue["issue_id"])
                reserve_issue(memory, issue, expected_input_hash=issue["input_hash"],
                    expected_snapshot_hash=allocation_snapshot_hash(memory.snapshot()), as_of=context["at"],
                    **({"report_root": context["root"]} if issue.get("recommendation_contract") == RECOMMENDATION_CONTRACT else {}))
                state = memory.snapshot()
                adoption_wait.discard(issue["issue_id"])
            frozen.append((issue, facts, saved.get("presentation_source")))
        except (SafetyError, OSError) as error:
            if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                raise
            result = {"issue_id": slot["issue_id"], "revision": draft.get("revision") if draft else None,
                      "state": "waiting", "complete": False, "approved": False, "client_ready": False,
                      "reason": str(error) if isinstance(error, SafetyError) else "REPORT_LOCAL_SAVE_FAILED"}
            record["drafts"][slot["issue_id"]] = dict(draft or {}, **result)
            outcomes.append(result)
            state = memory.snapshot()
    # Real reservations already protect current packets across programmes.
    # These IDs also protect genuine legacy frozen inputs before their first
    # compatible reservation is adopted; no parallel hold record is created.
    # A verified legacy packet without its real hold still owns its original
    # ordinal. If adoption genuinely waits, do not assign a later report the
    # wrong first/second-language position. This wait is report-only.
    unreserved_frozen = any(ref not in state["issue_reservations"] for ref in adoption_wait)
    legacy = {issue["issue_id"]: issue for issue, _, _ in frozen
              if issue.get("recommendation_contract") is None}
    preview_ids = set(legacy) | {slot["issue_id"] for slot in pending}
    preview_slots = [slot for slot in record["release_slots"] if slot["issue_id"] in preview_ids]
    available = [row for row in eligible if row["concept_id"] not in fixed_ids]
    functional_preview = (_functional_report_allocations(available, preview_slots, state, include_shortfalls=True)
                          if not unreserved_frozen else [])
    proposed = [issue for issue in functional_preview if issue.get("ready_for_review") is True
                and issue.get("recommendation_contract") == RECOMMENDATION_CONTRACT]
    selected_slots = {issue["issue_id"] for issue in proposed}
    preview_by_id = {issue["issue_id"]: issue for issue in functional_preview}
    shortage_slots = {slot["issue_id"] for slot in pending
                      if slot["issue_id"] in functional_slots and slot["issue_id"] not in selected_slots
                      and slot["issue_id"] in preview_by_id}
    for ref in sorted(shortage_slots):
        previous = record["drafts"].get(ref, {})
        own = state["issue_reservations"].get(ref, {})
        preview = preview_by_id[ref]
        retained = previous.get("revision", own.get("revision"))
        result = {"issue_id": ref, "revision": retained, "state": "functional_shortfall", "complete": False,
            "approved": False, "client_ready": False, "reason": "REPORT_FUNCTIONAL_COHORT_SHORTFALL",
            "selection_reason": functional_reasons.get(ref, "REPORT_CURRENT_ELIGIBILITY_CHANGED"),
            "candidate_products": len(preview["products"]), "shortfall": preview["shortfall"],
            "retained_revision": (retained if previous.get("complete") is True else
                                  previous.get("retained_revision", retained)),
            "retained_complete": previous.get("complete") is True or previous.get("retained_complete") is True,
            "client_publication_attempted": False}
        # This is a proved candidate shortage, not a failed external action.
        # Preserve the exact old input/hold and a stable honest status; do not
        # invent a timed outage retry or send this slot to legacy fallback.
        record["drafts"][ref] = dict(previous, **result)
        outcomes.append(result)
    pending = [slot for slot in pending if slot["issue_id"] not in shortage_slots]
    functional = [issue for issue in proposed if issue["issue_id"] not in legacy
                  or _due(record, "report_upgrade", issue["issue_id"],
                          _report_upgrade_binding(issue, language), context["at"])]
    upgrading = {issue["issue_id"] for issue in functional} & set(legacy)
    upgrade_bindings = {issue["issue_id"]: _report_upgrade_binding(issue, language)
                        for issue in functional if issue["issue_id"] in upgrading}
    selected_ids = {row["concept_id"] for issue in proposed for row in issue["products"]}
    if selected_slots:
        # Keep the exact preview selections. Sending these slots back through a
        # mixed-format allocator can choose the old cohort again. Actual holds
        # remain untouched until the ordinary checked reservation below.
        frozen = [row for row in frozen if row[0]["issue_id"] not in upgrading]
        retired_ids = {row["concept_id"] for ref in upgrading for row in legacy[ref]["products"]}
        fixed_ids.difference_update(retired_ids)
        fixed_ids.update(row["concept_id"] for issue, _, _ in frozen for row in issue["products"])
        pending = [slot for slot in pending if slot["issue_id"] not in selected_slots]
        from .functional_aliases import alias_binding, alias_projection, expand_identities
        from .functional_lineage import hold_overlap
        aliases = alias_projection(state) if alias_binding(state) is not None else {}
        excluded = expand_identities(selected_ids, aliases)
        excluded.update(cid for cid in aliases if any(chosen in aliases and hold_overlap(cid, chosen, aliases)
                        for chosen in selected_ids))
        available = [row for row in eligible if row["concept_id"] not in fixed_ids | excluded]
    if context.get("cooperative_mode") is True:
        # Explicit turns produce current-format sets only. Older-format stock
        # stays separately labelled and must not refill a new release date.
        available = [row for row in available if row.get("recommendation_contract") == RECOMMENDATION_CONTRACT]
    allocated = (allocate_issues(available, pending, state["published_history"], state["issue_reservations"], identity_state=state)
                 if pending and not unreserved_frozen else {"issues": []})
    if proposed:
        # The fallback allocator has a smaller slot list, not a new schedule.
        # Count the sets actually produced by both allocations (never an
        # unfilled date) before their checked reservations are committed.
        produced = [issue for issue in [*proposed, *allocated["issues"]] if issue.get("ready_for_review") is True]
        for issue in allocated["issues"]:
            ordinal = draft_ordinal(issue["issue_id"], issue["date"], state["published_history"],
                state["issue_reservations"], client_id=issue["client_id"], planned=produced)
            issue.update(projected_ordinal=ordinal, language_state=("bilingual_trial" if ordinal <= 2 else
                "language_pending" if language is None else "selected_language"))
        allocated["issues"] = sorted([*functional, *allocated["issues"]],
                                     key=lambda issue: (issue["date"], issue["issue_id"]))
    work = list(frozen)
    for issue in allocated["issues"]:
        try:
            facts = [pool["facts_by_concept"][row["concept_id"]] for row in issue["products"]]
            old = state["issue_reservations"].get(issue["issue_id"])
            if issue.get("recommendation_contract") == RECOMMENDATION_CONTRACT:
                recovered = (_saved_functional_upgrade(context["root"], issue, facts, state=state, record=record,
                    eligible=eligible_by_id, reservation=old, evidence_type=memory.evidence_type,
                    language=language, at=context["at"], media_root=context.get("media_root"))
                    if issue["issue_id"] in upgrading else None)
                issue, facts = (recovered if recovered is not None else _functional_report_revision(
                    context["root"], issue, facts, memory.evidence_type,
                    reservation=old, media_root=context.get("media_root"), source_state=state))
            else:
                issue = _report_revision(context["root"], issue, facts, memory.evidence_type)
                if old and old.get("state") == "reserved" and issue["revision"] <= old["revision"]:
                    issue["revision"] = old["revision"] + 1
                    issue["selection_hash"] = selection_fingerprint(issue)
            work.append((issue, facts, None))
        except (SafetyError, OSError) as error:
            if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                raise
            result = {"issue_id": issue["issue_id"], "revision": issue["revision"], "state": "waiting",
                      "complete": False, "approved": False, "client_ready": False,
                      "reason": str(error) if isinstance(error, SafetyError) else "REPORT_LOCAL_SAVE_FAILED"}
            previous = record["drafts"].get(issue["issue_id"], {})
            if issue["issue_id"] in upgrading and previous.get("complete") is True:
                result.update(retained_revision=previous["revision"], retained_complete=True)
                if previous.get("package_hash"):
                    result["retained_package_hash"] = previous["package_hash"]
            if issue["issue_id"] in upgrading:
                _defer(record, "report_upgrade", issue["issue_id"],
                    upgrade_bindings[issue["issue_id"]], result["reason"], context)
            if issue["issue_id"] not in upgrading:
                record["drafts"][issue["issue_id"]] = dict(previous, **result)
            # A failed upgrade preparation owns no replacement input or hold.
            # Keep the actual old revision/hash so the next healthy pass can
            # preview and recover the exact replacement normally.
            outcomes.append(result)
    for issue, facts, presentation in work:
        previous = record["drafts"].get(issue["issue_id"], {})
        legacy_saved_only = (context.get("cooperative_mode") is True
                             and issue.get("recommendation_contract") != RECOMMENDATION_CONTRACT)
        binding = digest([issue, facts, language])
        if not _due(record, "reports", issue["issue_id"], binding, context["at"]):
            continue
        selection_reserved = False
        try:
            if issue.get("ready_for_review") is True:
                if issue.get("recommendation_contract") == RECOMMENDATION_CONTRACT:
                    freeze_review_input(memory, issue, facts, root=context["root"])
                reserve_issue(memory, issue, expected_input_hash=issue["input_hash"],
                    expected_snapshot_hash=allocation_snapshot_hash(memory.snapshot()), as_of=context["at"],
                    **({"report_root": context["root"]} if issue.get("recommendation_contract") == RECOMMENDATION_CONTRACT else {}))
                selection_reserved = True
                if issue.get("recommendation_contract") == RECOMMENDATION_CONTRACT:
                    # Reusing the old coherent legacy packet does not own its
                    # waiting replacement proposal. Only the checked new-format
                    # hold (including exact newer-hold recovery) clears it.
                    _clear(record, "report_upgrade", issue["issue_id"])
            options = ({"presentation_source_revision": presentation["revision"]} if presentation else {})
            result = prepare_review_package(memory, issue, facts, root=context["root"], runner=context["runner"],
                render_pdf=context.get("render_pdf"), inspect_pdf=context.get("inspect_pdf"), selected_language=language,
                allow_fresh_legacy_model=not legacy_saved_only, **options)
        except (SafetyError, OSError) as error:
            if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                raise
            result = {"issue_id": issue["issue_id"], "revision": issue["revision"], "state": "waiting",
                      "reason": str(error) if isinstance(error, SafetyError) else "REPORT_LOCAL_SAVE_FAILED",
                      "approved": False, "client_ready": False}
            if issue.get("ready_for_review") is True and not selection_reserved:
                # A commit may have succeeded before its reply was lost. Only
                # the exact canonical hold can establish that writing was
                # authorised; a confirmed rejection is not a frozen G intent.
                try:
                    saved_hold = memory.snapshot()["issue_reservations"].get(issue["issue_id"])
                    selection_reserved = bool(saved_hold and saved_hold.get("state") == "reserved"
                        and saved_hold.get("selection_hash") == issue["selection_hash"])
                except (SafetyError, OSError) as recovery_error:
                    if isinstance(recovery_error, SafetyError) and str(recovery_error) in INTEGRITY_FAILURES:
                        raise
                    selection_reserved = None  # uncertainty never authorises a blind new writer
        if previous.get("revision") != issue["revision"] and (
                previous.get("complete") is True or previous.get("retained_complete") is True):
            retained = (previous.get("revision") if previous.get("complete") is True
                        else previous.get("retained_revision"))
            result.update(retained_revision=retained, retained_complete=True)
            old_hash = (previous.get("package_hash") if previous.get("complete") is True else
                        previous.get("retained_package_hash", previous.get("package_hash")))
            if old_hash:
                result["retained_package_hash"] = old_hash
        if issue["issue_id"] not in upgrading or selection_reserved is True:
            record["drafts"][issue["issue_id"]] = {**result, "revision": issue["revision"],
                                                    "binding": binding, "products": len(issue["products"]),
                                                    "selection_reserved": selection_reserved}
        elif issue["issue_id"] in upgrading:
            _defer(record, "report_upgrade", issue["issue_id"],
                upgrade_bindings[issue["issue_id"]], result.get("reason", result["state"]), context)
        stages = result.get("stage_outcomes") or {}
        workbook = stages.get("workbook") or {}
        editions = stages.get("editions") or {}
        local_failure = (workbook.get("state") not in (None, "completed") or any(
            row.get("state") == "waiting" and row.get("reason") != "REPORT_WRITING_NOT_COMPLETE"
            for row in editions.values()))
        if result.get("complete") or (legacy_saved_only and result.get("state") == "narration_pending"
                                      and not local_failure):
            # Missing old prose cannot become actionable merely because five
            # minutes passed. Exact saved replies/holds and new-format inputs
            # still wake normal work; real rendering/file failures keep retry.
            _clear(record, "reports", issue["issue_id"])
        else:
            _defer(record, "reports", issue["issue_id"], binding, result.get("reason", result["state"]), context)
        outcomes.append(result)
    return {"eligible_products": len(eligible), "outcomes": outcomes,
            "example_preparation": example_preparation,
            "approved": False, "client_publication_attempted": False}


def _private_packet(memory, record, context, issue_id, draft):
    """Validate current local bytes; retain only immutable metadata, never files."""
    from .private_drafts import prepare_private_draft
    revision = draft.get("revision")
    manifest, files = prepare_private_draft(context["root"], issue_id, revision)
    if manifest["client_id"] != record["client_id"] or draft.get("package_hash") != manifest["package_hash"]:
        raise SafetyError("PROGRAMME_PRIVATE_PACKET_BINDING_CHANGED")
    return {"issue_id": issue_id, "revision": revision, "package_hash": manifest["package_hash"],
            "manifest_hash": digest(manifest), "client_id": manifest["client_id"],
            "files": [{name: row[name] for name in ("name", "sha256", "size")} for row in files]}


def _private_result(receipt, packet):
    """A callback is not an upload claim unless every exact file was verified."""
    fields = ("issue_id", "revision", "package_hash", "client_id")
    if (not isinstance(receipt, dict) or any(receipt.get(name) != packet[name] for name in fields)
            or any(type(receipt.get(name)) is not str for name in ("issue_id", "package_hash", "client_id"))
            or type(receipt.get("revision")) is not int or not 1 <= receipt["revision"] <= 9007199254740991
            or not isinstance(receipt.get("state"), str)
            or any(receipt.get(name) is not False for name in
                   ("approved", "release_ready", "client_available", "index_write_attempted", "publication_attempted"))):
        raise SafetyError("PROGRAMME_PRIVATE_STAGING_RESULT_INVALID")
    identifier(receipt["issue_id"]); identifier(receipt["client_id"])
    if receipt.get("index_unchanged") is not None and type(receipt["index_unchanged"]) is not bool:
        raise SafetyError("PROGRAMME_PRIVATE_STAGING_RESULT_INVALID")
    outcomes = receipt.get("outcomes", [])
    if not isinstance(outcomes, list) or any(not isinstance(row, dict) for row in outcomes):
        raise SafetyError("PROGRAMME_PRIVATE_STAGING_RESULT_INVALID")
    wanted = {row["name"]: row for row in packet["files"]}
    required = {"shortlist.xlsx", "final_report_data.json", "delivery-manifest.json"}
    pdf_names = {"report-en.pdf", "report-hi-Latn.pdf"}
    if (len(wanted) != len(packet["files"]) or not required <= set(wanted)
            or not set(wanted) <= required | pdf_names or not set(wanted) & pdf_names):
        raise SafetyError("PROGRAMME_PRIVATE_STAGING_RESULT_INVALID")
    names = [row.get("name") for row in outcomes]
    if any(not isinstance(name, str) or name not in wanted for name in names) or len(set(names)) != len(names):
        raise SafetyError("PROGRAMME_PRIVATE_STAGING_RESULT_INVALID")
    verified = [row for row in outcomes if row.get("state") == "verified"]
    if any(type(row.get("sha256")) is not str or row.get("sha256") != wanted[row["name"]]["sha256"]
           or type(row.get("size")) is not int or row.get("size") != wanted[row["name"]]["size"]
           for row in verified):
        raise SafetyError("PROGRAMME_PRIVATE_STAGING_RESULT_INVALID")
    if "verified_file_count" in receipt and (type(receipt["verified_file_count"]) is not int
            or receipt["verified_file_count"] != len(verified)):
        raise SafetyError("PROGRAMME_PRIVATE_STAGING_RESULT_INVALID")
    success = (receipt["state"] == "private_bytes_verified" and receipt.get("index_unchanged") is True
               and len(verified) == len(wanted) and set(names) == set(wanted))
    if receipt["state"] == "private_bytes_verified" and not success:
        raise SafetyError("PROGRAMME_PRIVATE_STAGING_RESULT_INVALID")
    reason = receipt.get("reason", "Private draft storage needs follow-up.")
    if not isinstance(reason, str) or len(reason) > 200:
        raise SafetyError("PROGRAMME_PRIVATE_STAGING_RESULT_INVALID")
    return {**deepcopy(packet), "state": receipt["state"], "index_unchanged": receipt.get("index_unchanged"),
            "verified_file_count": len(verified), "reason": None if success else reason,
            "outcomes": [{name: row[name] for name in ("name", "state", "size", "sha256", "reason") if name in row}
                         for row in outcomes], "client_publication_attempted": False}


def _private_staging(memory, record, context):
    """Independent private delivery of complete drafts, never approve/release."""
    callback = context.get("private_staging"); outcomes = []
    ledger = record.setdefault("private_staging_receipts", {})
    for issue_id, draft in sorted(record["drafts"].items()):
        if draft.get("complete") is not True:
            continue
        target = issue_id + ":" + str(draft.get("revision"))
        binding = digest({name: draft.get(name) for name in ("revision", "package_hash", "binding")})
        if not _due(record, "private_staging", target, binding, context["at"]):
            # A proven unreadable local input waits on its same draft binding;
            # healthy research rounds must not keep reopening that file or
            # move its retry time forward on every unrelated source result.
            continue
        try:
            packet = _private_packet(memory, record, context, issue_id, draft)
            binding = digest(packet)
            previous = ledger.get(binding)
            if previous and previous.get("state") == "private_bytes_verified":
                _clear(record, "private_staging", target)
                outcomes.append({"issue_id": issue_id, "revision": packet["revision"], "state": "reused",
                                 "package_hash": packet["package_hash"]})
                continue
            if not _due(record, "private_staging", target, binding, context["at"]):
                continue
            if callback is None:
                result = {**packet, "state": "setup_waiting", "verified_file_count": 0,
                          "reason": context.get("setup_failures", {}).get("private_staging",
                                      "Private draft service access has not been configured."),
                          "client_publication_attempted": False}
            else:
                result = _private_result(callback(issue_id, packet["revision"]), packet)
        except Exception as error:
            reason = str(error) if isinstance(error, SafetyError) else "PRIVATE_DRAFT_LOCAL_OR_STORAGE_FAILED"
            # This packet's corrupt/inaccessible local input cannot prevent a
            # different valid packet or continued public research completing.
            result = {"issue_id": issue_id, "revision": draft.get("revision"), "package_hash": draft.get("package_hash"),
                      "state": "waiting", "reason": reason, "verified_file_count": 0,
                      "client_publication_attempted": False}
        ledger[binding] = dict(result, checked_at=context["at"])
        if result["state"] == "private_bytes_verified":
            _clear(record, "private_staging", target)
        else:
            _defer(record, "private_staging", target, binding, result["reason"], context)
        outcomes.append({name: result.get(name) for name in
                         ("issue_id", "revision", "package_hash", "state", "reason", "verified_file_count")})
    return {"outcomes": outcomes, "client_publication_attempted": False}


def _private_pending(record):
    """Delivery waits remain resumable after research itself is checked."""
    successful = {(row.get("issue_id"), row.get("revision"), row.get("package_hash"))
                  for row in record.get("private_staging_receipts", {}).values()
                  if row.get("state") == "private_bytes_verified"}
    return any(draft.get("complete") is True and (issue_id, draft.get("revision"), draft.get("package_hash")) not in successful
               for issue_id, draft in record.get("drafts", {}).items())


DEFAULT_STEPS = {"profiles": _profiles, "fit": _fit, "acquire": _acquire, "match": _match,
                 "suppliers": _suppliers, "photos": _photos, "reports": _reports, "private_staging": _private_staging}


def _programme_stage_plan(state, record, turn, *, at, inputs, model_available):
    """Order due accepted-type prerequisites without granting execution rights.

    The ordinary helpers still own exact jobs, waits and turn allowances. A
    missing accepted C profile prevents meaningful type searches; a current
    legacy checklist is not the accepted type's checklist. Service those
    existing dependencies before their consumers, once, on this same state.
    All other stages keep their fair rotated order, and genuine future waits
    remain intact. Discovery follows only the promoted prefix on odd turns.
    While the coming report sets are short of eligible types, a passing type
    that is not yet report stock also promotes its finishing stages (market
    review, crowding check, supplier work) ahead of new discovery.
    """
    start = ((turn["programme_turn_sequence"] - 1) // 2) % len(STAGES) if turn else 0
    rotated = list(STAGES[start:] + STAGES[:start])
    discovery = "before" if turn is None or turn["programme_turn_sequence"] % 2 else "after"
    plan = {"stage_start": start, "stage_order": rotated,
        "discovery_position": discovery, "discovery_after_stage": None,
        "priority_concept_ids": {}, "planning_waits": [], "report_supply_short": False}
    if turn is None or not model_available:
        return plan
    from .fit_description import FUNCTIONAL_CONTRACT
    from .fit_policy import FIT_POLICY_ID
    from .functional_types import functional_role_context
    from .product_queries import current_query_profile, query_profile_scope_hash
    from .research_flow import assessment_item, current_assessment_fit, evidence_indexes
    from .research_records import iter_functional_definitions
    from .source_coverage import _product_context
    from .nearly_ready import (NEARLY_READY_STAGES, finishing_work, passing_checklist, report_stock_ids,
                               report_supply_short)
    accepted_ids = {row.get("concept_id") for _, row in iter_functional_definitions(state)}
    indexes = evidence_indexes(state)
    priorities = {"profiles": [], "fit": [], **{stage: [] for stage in NEARLY_READY_STAGES}}
    short = report_supply_short(record)
    plan["report_supply_short"] = short
    stock = set()
    if short:
        try:
            stock = report_stock_ids(state, record, as_of=at)
        except SafetyError as error:
            if str(error) in INTEGRITY_FAILURES:
                raise
            # Unreadable stock only loses this exclusion; promotion is ordering.
            plan["planning_waits"].append({"concept_id": None, "reason": str(error)})
    for scope in _review_scopes(record):
        def lane_due(stage):
            return _due(scope, "lane", stage, digest({"stage": stage, "inputs": inputs}), at)
        passing = []
        for cid in scope["concept_ids"]:
            if cid not in accepted_ids:
                continue
            try:
                _, excluded = _product_context(state, cid)
                if excluded or functional_role_context(state, cid, role="C") is None:
                    continue
                if lane_due("profiles") and current_query_profile(state, cid) is None:
                    if _due(scope, "profiles", cid, query_profile_scope_hash(state, cid), at):
                        priorities["profiles"].append(cid)
                fit_due = lane_due("fit")
                if not fit_due and not short:
                    continue
                current = current_assessment_fit(state, cid, expected_policy_id=FIT_POLICY_ID, indexes=indexes)
                missing = (current is None or state["assessments"][cid].get("contract_version") != FUNCTIONAL_CONTRACT)
                if fit_due and missing and _due(scope, "fit", cid, digest(assessment_item(state, cid, indexes=indexes,
                        contract_version=FUNCTIONAL_CONTRACT)), at):
                    priorities["fit"].append(cid)
                if short and cid not in stock and passing_checklist(state, cid, current):
                    passing.append(cid)
            except SafetyError as error:
                if str(error) in INTEGRITY_FAILURES:
                    raise
                # The ordinary stage records the precise local failure too;
                # it must not keep a healthy sibling from being considered.
                plan["planning_waits"].append({"concept_id": cid, "reason": str(error)})
        if not passing:
            continue
        try:
            # Promote a stage only where it holds actual pending work.
            work = finishing_work(state, scope, passing, as_of=at)
        except SafetyError as error:
            if str(error) in INTEGRITY_FAILURES:
                raise
            plan["planning_waits"].append({"concept_id": None, "reason": str(error)})
            continue
        for stage in NEARLY_READY_STAGES:
            if lane_due(stage):
                priorities[stage].extend(cid for cid in passing if cid in work[stage])
    plan["priority_concept_ids"] = {stage: ids for stage, ids in priorities.items() if ids}
    prefix = list(plan["priority_concept_ids"])
    plan["stage_order"] = prefix + [stage for stage in rotated if stage not in prefix]
    if prefix and discovery == "before":
        plan.update(discovery_position="after_prerequisites", discovery_after_stage=prefix[-1])
    return plan


def _advance_programme_locked(memory, programme_id, *, root, runner=None, handlers=None, request_builder=None,
                      max_cycles=None, source_limit=12, profile_limit=20, fit_limit=10, match_limit=5,
                      supplier_limit=5, photo_limit=10, retry_seconds=300, as_of=None, clock=utc_now,
                      on_progress=None, steps=None, discovery_step=None, render_pdf=None, inspect_pdf=None,
                      media_root=None, photo_fetcher=None, setup_failures=None, private_staging=None,
                      backup_step=None):
    """Continue dependency-ready lanes to a fixed point or explicit time slice.

    Only the programme leader is held over external work, not the money lock.
    Saved-only reconciliation is still allowed when a model route is paused.
    A fixed point means currently waiting work, not completion of the project.
    """
    from .source_coverage import installed_source_handlers, product_source_request_builder
    limits = ((profile_limit, 200), (fit_limit, 50), (match_limit, 20), (supplier_limit, 50), (photo_limit, 200))
    if (any(type(value) is not int or not 1 <= value <= maximum for value, maximum in limits)
            or type(source_limit) is not int or source_limit < 1
            or type(retry_seconds) is not int or retry_seconds < 1
            or max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1)):
        raise SafetyError("PROGRAMME_SLICE_INVALID")
    functions = dict(DEFAULT_STEPS)
    if steps is not None:
        if not isinstance(steps, dict) or not set(steps) <= set(STAGES) or any(not callable(fn) for fn in steps.values()):
            raise SafetyError("PROGRAMME_STEPS_INVALID")
        functions.update(steps)
    if (discovery_step is not None and not callable(discovery_step)
            or private_staging is not None and not callable(private_staging)
            or backup_step is not None and not callable(backup_step)):
        raise SafetyError("PROGRAMME_STEPS_INVALID")
    if (setup_failures is not None and (not isinstance(setup_failures, dict)
            or not set(setup_failures) <= {"model", "funded_quickcommerce", "native_renderer", "catalogue_discovery",
                                           "catalogue_seed", "private_staging"}
            or any(not isinstance(value, str) or not value or len(value) > 200 for value in setup_failures.values()))):
        raise SafetyError("PROGRAMME_SETUP_RESULT_INVALID")
    # Factories prepare callbacks, never navigate/charge on construction.
    from .work_turns import DISCOVERY_RESERVED_EXECUTIONS, active_work_turn, hold_for_discovery
    shared_mode = active_work_turn(memory) is not None
    if handlers is None:
        shared_options = {"marketplace_options": {"shared_acquisitions": True},
                          "meta_options": {"shared_acquisitions": True},
                          "trends_options": {"shared_acquisitions": True},
                          "supplier_options": {"respect_access_waits": True, "shared_acquisitions": True},
                          "quickcommerce_options": {"respect_access_waits": True, "shared_acquisitions": True}} if shared_mode else {}
        handlers = installed_source_handlers(memory, **shared_options)
    record = load_programme(memory, programme_id)
    cache = {"canonical": memory.snapshot()["batch_runs"].get(key("programme", programme_id))}
    record["setup_failures"] = deepcopy(setup_failures or {})
    if record["setup_failures"].get("model"):
        at = as_of or clock(); timestamp(at)
        record["model_pause"] = {"reason": record["setup_failures"]["model"], "at": at,
            "next_retry_at": (timestamp(at) + timedelta(seconds=retry_seconds)).isoformat()}
    builder_options = {"shared_suppliers": True} if shared_mode else {}
    request_builder = request_builder or product_source_request_builder(location=record["location"],
        run_id=record["source_run_id"], **builder_options)
    builders = {record["programme_id"]: request_builder}
    context = {"root": Path(root), "memory": memory, "handlers": handlers, "request_builder": request_builder,
        "clock": clock, "fixed_as_of": as_of,
        "profile_limit": profile_limit, "fit_limit": fit_limit, "source_limit": source_limit,
        "match_limit": match_limit, "supplier_limit": supplier_limit, "photo_limit": photo_limit,
        "retry_seconds": retry_seconds, "cooperative_mode": shared_mode,
        "render_pdf": render_pdf, "inspect_pdf": inspect_pdf,
        "media_root": media_root, "photo_fetcher": photo_fetcher, "on_progress": on_progress,
        "private_staging": private_staging, "backup_step": backup_step,
        "discovery_catalogue": getattr(discovery_step, "discovery_catalogue", None),
        "setup_failures": record["setup_failures"], "inventory_functions": functions}
    cycles, last_save, owner_paused = 0, None, False
    from .work_turns import active_work_turn
    active_turn = active_work_turn(memory)
    execution_order = _programme_stage_plan({}, record, active_turn, at=as_of, inputs=None, model_available=False)
    execution_order["visited_stages"] = []
    execution_order["discovery_attempted"] = False
    while max_cycles is None or cycles < max_cycles:
        if not programme_service_state(memory)["enabled"]:
            owner_paused = True; record["tick_state"] = "owner_paused"; break
        at = as_of or clock(); timestamp(at); context["at"] = at
        pause = record.get("model_pause")
        paused = pause and timestamp(pause["next_retry_at"]) > timestamp(at)
        context["runner"] = _ProgrammeRunner(runner, record, context) if runner is not None and not paused else None
        before_state = memory.snapshot()
        before = _signature(before_state, record)
        execution_order = _programme_stage_plan(before_state, record, active_turn,
            at=at, inputs=before, model_available=context["runner"] is not None)
        execution_order["visited_stages"] = []
        execution_order["discovery_attempted"] = False
        context["priority_concept_ids"] = execution_order["priority_concept_ids"]
        # Use the existing privacy-filtered recording vocabulary so this
        # remains visible even if an open turn is interrupted before close.
        record_event("programme_order", programme_id=record["programme_id"],
            state=execution_order["discovery_position"], next_phase=execution_order["discovery_after_stage"],
            counts={**{stage + "_order": index for index, stage in enumerate(execution_order["stage_order"], 1)},
                **{stage + "_priority": len(ids) for stage, ids in execution_order["priority_concept_ids"].items()},
                "planning_waits": len(execution_order["planning_waits"])})
        at = as_of or clock(); timestamp(at); context["at"] = at
        record.update(status="running", updated_at=at)
        def run_discovery():
            nonlocal last_save, owner_paused
            if not programme_service_state(memory)["enabled"]:
                owner_paused = True; record["tick_state"] = "owner_paused"; return
            if discovery_step is None or not _due(record, "lane", "discovery", before, context["at"]):
                return
            execution_order["discovery_attempted"] = True
            hold_for_discovery(memory, 0)  # Discovery's own questions use the held share.
            context["stage"] = "discovery"; context["review_scope_id"] = record["programme_id"]
            _observe(record, context, phase_state="started", frozen_products=len(record["concept_ids"]))
            try:
                discovery = discovery_step(memory, deepcopy(record), context)
                if not isinstance(discovery, dict) or not isinstance(discovery.get("state"), str):
                    raise SafetyError("PROGRAMME_DISCOVERY_RESULT_INVALID")
                discovery["queued_follow_on_products"] = len(_register_discovered_products(memory, record, discovery, context["at"]))
                _clear(record, "lane", "discovery")
            except Exception as error:
                record_error("discovery_failed", error, stage="discovery", programme_id=record["programme_id"])
                reason = str(error) if isinstance(error, SafetyError) else "PROGRAMME_DISCOVERY_FAILED"
                if reason in INTEGRITY_FAILURES:
                    raise
                discovery = {"state": "waiting", "reason": reason, "error_type": type(error).__name__}
                _defer(record, "lane", "discovery", before, reason, context)
            record["discovery_state"] = discovery["state"]
            record["last_results"]["discovery"] = discovery
            last_save = _save(memory, record, cache=cache)
            _observe(record, context, phase_state="finished", result=deepcopy(discovery), control_save=last_save)
        # While short, finishing stages before discovery leave it two calls.
        hold_for_discovery(memory, DISCOVERY_RESERVED_EXECUTIONS if (
            active_turn is not None and discovery_step is not None and execution_order["report_supply_short"]
            and _due(record, "lane", "discovery", before, context["at"])) else 0)
        if execution_order["discovery_position"] == "before":
            run_discovery()
        for stage in execution_order["stage_order"]:
            if not programme_service_state(memory)["enabled"]:
                owner_paused = True; record["tick_state"] = "owner_paused"; break
            execution_order["visited_stages"].append(stage)
            inventory_first = active_turn is not None and active_turn["programme_turn_sequence"] % 2 == 0
            if inventory_first:
                context["stage"] = stage
                _run_inventory_stage(memory, record, context, stage)
            priority = set(execution_order["priority_concept_ids"].get(stage, []))
            acquire_plan = (_priority_acquisition_plan(_acquisition_scopes(record, source_limit), record, priority)
                            if stage == "acquire" else [])
            scopes = ([scope for scope, _ in acquire_plan] if stage == "acquire" else
                      [record] if stage in {"reports", "private_staging"} else _review_scopes(record))
            if priority and stage != "acquire":
                scopes.sort(key=lambda scope: not bool(priority.intersection(scope["concept_ids"])))
            source_used, callback_counts, fresh_counts = 0, [], []
            acquisition_results, acquisition_limits, served_sources = {}, {}, []
            for scope_index, scope in enumerate(scopes):
                if not programme_service_state(memory)["enabled"]:
                    owner_paused = True; record["tick_state"] = "owner_paused"; break
                at = as_of or clock(); timestamp(at); context["at"] = at
                pause = record.get("model_pause")
                paused = pause and timestamp(pause["next_retry_at"]) > timestamp(at)
                context["runner"] = _ProgrammeRunner(runner, record, context) if runner is not None and not paused else None
                stage_binding = digest({"stage": stage, "inputs": before})
                if not _due(scope, "lane", stage, stage_binding, at):
                    continue
                if stage == "acquire":
                    quota = acquire_plan[scope_index][1]
                    remaining = source_limit - source_used
                    if remaining <= 0:
                        continue
                    prior = acquisition_results.get(scope["programme_id"])
                    if prior is not None and (prior.get("handler_calls") != acquisition_limits[scope["programme_id"]]
                                              or not prior.get("handler_calls")
                                              or prior.get("inspection_pending") is True):
                        # A finite inspection slice gives the other stages
                        # their turn even when it left paid-call capacity.
                        # Continue its saved position in the next turn, not
                        # through this optional second visit to the parent.
                        continue
                    context["source_limit"] = remaining if quota is None else min(quota, remaining)
                    acquisition_limits.setdefault(scope["programme_id"], context["source_limit"])
                context["stage"] = stage; context["review_scope_id"] = scope["programme_id"]
                if scope["programme_id"] not in builders:
                    builders[scope["programme_id"]] = product_source_request_builder(location=scope["location"],
                        run_id=scope["source_run_id"], **builder_options)
                context["request_builder"] = builders[scope["programme_id"]]
                context["reuse_run_ids"] = ([other["source_run_id"] for other in _review_scopes(record)
                                             if other["source_run_id"] != scope["source_run_id"]]
                                            if scope is not record else None)
                cycle_id = record["last_results"].get("discovery", {}).get("cycle_id")
                context["reuse_discovery_cycle_ids"] = [cycle_id] if scope is not record and cycle_id else None
                _observe(record, context, phase_state="started", frozen_products=len(scope["concept_ids"]),
                         maximum_group_size=context.get({"profiles": "profile_limit", "fit": "fit_limit",
                             "acquire": "source_limit", "match": "match_limit", "suppliers": "supplier_limit",
                             "photos": "photo_limit"}.get(stage, "")))
                try:
                    result = functions[stage](memory, scope, context)
                    if not isinstance(result, dict):
                        raise SafetyError("PROGRAMME_STAGE_RESULT_INVALID")
                    if stage == "acquire" and ("handler_calls" in result or "fresh_dispatches" in result):
                        if (type(result.get("handler_calls")) is not int
                                or not 0 <= result["handler_calls"] <= context["source_limit"]
                                or "fresh_dispatches" in result and
                                (type(result["fresh_dispatches"]) is not int
                                 or not 0 <= result["fresh_dispatches"] <= result["handler_calls"])):
                            raise SafetyError("PROGRAMME_ACQUISITION_COUNTS_INVALID")
                    _clear(scope, "lane", stage)
                except Exception as error:
                    record_error("stage_failed", error, stage=stage, programme_id=record["programme_id"],
                                 review_scope_id=scope["programme_id"])
                    if isinstance(error, SafetyError):
                        reason = str(error)
                    elif isinstance(error, OSError):
                        reason = "PROGRAMME_LOCAL_FILE_FAILED"
                    else:
                        reason = "PROGRAMME_STAGE_FAILED"
                    if reason in INTEGRITY_FAILURES:
                        raise
                    result = {"state": "waiting", "reason": reason, "error_type": type(error).__name__}
                    _defer(scope, "lane", stage, stage_binding, reason, context)
                if stage == "acquire":
                    callbacks = result.get("handler_calls")
                    callback_counts.append(callbacks)
                    fresh_counts.append(result.get("fresh_dispatches"))
                    # Missing/failed local return is uncertain, not zero
                    # work. Reserve its entire quota before a sibling runs.
                    source_used += callbacks if callbacks is not None else context["source_limit"]
                    served_sources.append(scope["programme_id"])
                    if scope is not record:
                        cursor = record["acquisition_cursor"]
                        ordinal = scope["acquisition_ordinal"]
                        if ordinal <= cursor["last"] or ordinal > cursor["ceiling"]:
                            record["acquisition_sweep"] += 1
                            cursor["ceiling"] = max(child["acquisition_ordinal"] for child in _review_scopes(record)[1:])
                        cursor["last"] = ordinal
                    previous = acquisition_results.get(scope["programme_id"])
                    if previous is not None:
                        result = dict(result,
                            passes=previous.get("passes", []) + result.get("passes", []),
                            handler_calls=previous["handler_calls"] + callbacks if callbacks is not None else None,
                            fresh_dispatches=previous.get("fresh_dispatches", 0) + result["fresh_dispatches"]
                                if previous.get("fresh_dispatches") is not None and result.get("fresh_dispatches") is not None else None)
                        result["callbacks_without_new_dispatch"] = (result["handler_calls"] - result["fresh_dispatches"]
                            if result["handler_calls"] is not None and result["fresh_dispatches"] is not None else None)
                    acquisition_results[scope["programme_id"]] = result
                scope["last_results"][stage] = result
                pause = record.get("model_pause")
                if pause and timestamp(pause["next_retry_at"]) > timestamp(at):
                    context["runner"] = None
                scope["updated_at"] = at; record["updated_at"] = at
                last_save = _save(memory, record, cache=cache)
                _observe(record, context, phase_state="finished", result=deepcopy(result), control_save=last_save)
            if active_turn is not None and not inventory_first and not owner_paused:
                context["stage"] = stage
                _run_inventory_stage(memory, record, context, stage)
            if stage == "acquire":
                context["source_limit"] = source_limit
                callbacks = sum(callback_counts) if all(value is not None for value in callback_counts) else None
                fresh = sum(fresh_counts) if all(value is not None for value in fresh_counts) else None
                record["acquisition_budget"] = {"limit": source_limit,
                    "budget_units_consumed": source_used, "handler_calls": callbacks,
                    "fresh_dispatches": fresh,
                    "callbacks_without_new_dispatch": callbacks - fresh if callbacks is not None and fresh is not None else None,
                    "selected_scope_ids": served_sources}
                last_save = _save(memory, record, cache=cache)
            if execution_order["discovery_after_stage"] == stage and not owner_paused:
                context["at"] = as_of or clock(); timestamp(context["at"])
                run_discovery()
        if execution_order["discovery_position"] == "after" and not owner_paused:
            context["at"] = as_of or clock(); timestamp(context["at"])
            run_discovery()
        hold_for_discovery(memory, 0)
        cycles += 1; record["cycles"] += 1
        cycle_state = memory.snapshot()
        after = _signature(cycle_state, record)
        record["progress_signature"] = after
        if not programme_service_state(memory)["enabled"]:
            owner_paused = True; record["tick_state"] = "owner_paused"
        if owner_paused:
            break
        _refresh_owner_measurements(memory, record, context, state=cycle_state)
        if backup_step is not None:
            # Save the latest work first. Only then may the optional copy
            # select that original checkpoint and its referenced files.
            last_save = _save(memory, record, cache=cache)
            _refresh_owner_backup(memory, record, context)
            last_save = _save(memory, record, cache=cache)
        if before == after:
            inspection_pending = any(scope["last_results"].get("acquire", {}).get("inspection_pending") is True
                                     for scope in _review_scopes(record))
            record["tick_state"] = ("work_slice_finished_remaining_work_kept" if inspection_pending else
                                    "fixed_point_with_saved_remaining_work")
            break
    else:
        record["tick_state"] = "work_slice_finished_remaining_work_kept"
    record["status"] = "work_remaining"
    # Captures may have completed minutes after this round started. Use a
    # fresh observation time for the final status, not the earlier clock.
    final_at = as_of or clock(); timestamp(final_at)
    record["last_served_at"] = final_at
    retry_times = [row["next_retry_at"] for row in record["deferrals"].values()]
    retry_times.extend(row["next_retry_at"] for scope in _review_scopes(record)[1:]
                       for row in scope["deferrals"].values())
    pause = record.get("model_pause")
    if pause:
        retry_times.append(pause["next_retry_at"])
    final_state = memory.snapshot()
    try:
        from .source_coverage import source_run_progress
        for scope in _review_scopes(record):
            try:
                progress = source_run_progress(memory, scope["source_run_id"], state=final_state)
            except SafetyError as error:
                if str(error) == "SOURCE_RUN_REQUIRED":
                    continue
                raise
            retry_times.extend(row["next_retry_at"] for row in progress.get("route_pauses", {}).values() if row.get("next_retry_at"))
    except SafetyError as error:
        if str(error) != "SOURCE_RUN_REQUIRED":
            raise
    record["next_retry_at"] = (min(retry_times, key=timestamp) if retry_times else
        (timestamp(final_at) + timedelta(seconds=retry_seconds)).isoformat())
    record["progress"] = programme_progress(memory, programme_id, handlers=handlers,
        as_of=final_at, record=record, state=final_state)
    current = record["progress"]
    scope_progress = {row["programme_id"]: row for row in [current, *current["follow_on_reviews"]]}
    for scope in _review_scopes(record):
        counts = scope_progress[scope["programme_id"]]
        settled_sources = set(counts["source_states"]) <= {"completed_relevant", "completed_no_results", "not_applicable", "insufficient"}
        supplier_remaining = scope["last_results"].get("suppliers", {}).get("remaining_inputs")
        scope["research_state"] = ("current_pool_checked" if settled_sources
            and counts["fit_states"].get("accepted", 0) == counts["fit_products"]
            and supplier_remaining == 0 else "work_remaining")
        counts["research_state"] = scope["research_state"]
    if any(scope["research_state"] != "current_pool_checked" for scope in _review_scopes(record)[1:]):
        record["research_state"] = "work_remaining"
    if record["discovery_state"] not in {"checked", "not_connected"}:
        record["research_state"] = "discovery_work_remaining"
    # Owner approval/language and a missing photo do not hold the next
    # research week hostage. They remain separately recorded draft work.
    if record["research_state"] == "current_pool_checked":
        record["status"] = "research_pool_finished"
    if not programme_service_state(memory)["enabled"]:
        owner_paused = True; record["tick_state"] = "owner_paused"
    if owner_paused:
        record["status"] = "owner_paused"
    record["progress"]["research_state"] = record["research_state"]
    effort_checkpoint = (_refresh_owner_measurements(memory, record, context, state=final_state)
                         if not owner_paused else None)
    if not owner_paused and backup_step is not None:
        last_save = _save(memory, record, cache=cache)
        _refresh_owner_backup(memory, record, context)
    record["owner_summary"] = programme_owner_summary(memory, record, root=root,
        state=final_state, as_of=final_at, effort_checkpoint=effort_checkpoint)
    last_save = _save(memory, record, cache=cache)
    return {"programme_id": programme_id, "tick_state": record["tick_state"], "cycles_this_tick": cycles,
        "progress": record["progress"], "owner_summary": record["owner_summary"], "control_save": last_save,
        "execution_order": deepcopy(execution_order),
        "report_filled_does_not_stop_work": True, "client_publication_attempted": False}



def _warm_research_history(memory):
    started = time.monotonic()
    record_event("phase_started", stage="saved_history", phase="history_verification")
    try:
        state = memory.snapshot()
        diagnostic = getattr(memory, "last_checkpoint_diagnostic", None)
        if diagnostic is not None:
            # The canonical read succeeded. A derived-checkpoint diagnostic
            # must remain separate, filtered evidence rather than silently
            # claiming a cache failure caused every slow subsequent read.
            record_event("research_checkpoint_diagnostic", stage="saved_history",
                         reason=diagnostic, useful_result=False)
        return state
    finally:
        record_event("phase_finished", stage="preparation", phase="history_verification",
                     timings={"history_verification": time.monotonic() - started})


@wraps(_advance_programme_locked)
def advance_programme(memory, programme_id, **options):
    """Own the single programme writer for the complete legacy advance."""
    with Journal(safe_path(memory.root, "programme_writer")).locked(), operation_writer(), \
            memory.defer_lookup_projection(flush_if=lambda: programme_service_state(memory)["enabled"]):
        _warm_research_history(memory)  # A losing scheduled wake must not replay history.
        return _advance_programme_locked(memory, programme_id, **options)


def _advance_programme_turn_locked(memory, programme_id, *, root, turn_policy=None, ready_lanes=None,
                           report_ready=False, as_of=None, clock=utc_now, review_ready=False, **options):
    """One recoverable bounded round using the existing programme dispatcher.

    The installed recurring task opts in to this explicit entry point.
    It shares admission across every Jobs instance, source controller, discovery
    callback and photo stage using this memory. An interrupted turn stays open;
    a later call resumes its counters rather than granting a fresh allowance.
    """
    from .work_turns import bind_work_turn, close_work_turn, discovery_reserve_scope, open_work_turn
    if "max_cycles" in options:
        raise SafetyError("PROGRAMME_TURN_IS_ONE_ROUND")
    at = as_of or clock(); timestamp(at)
    if not programme_service_state(memory)["enabled"]:
        raise SafetyError("PROGRAMME_OWNER_PAUSED")
    def owner_allows_launch():
        if not programme_service_state(memory)["enabled"]:
            raise SafetyError("PROGRAMME_OWNER_PAUSED")
    owner_allows_launch()
    original = load_programme(memory, programme_id)
    before = _signature(memory.snapshot(), original)
    turn = open_work_turn(memory, programme_id, at=at, policy=turn_policy,
                          ready_lanes=ready_lanes, report_ready=report_ready,
                          preserve_open_policy=True, review_ready=review_ready)
    with bind_work_turn(memory, turn["id"], before_model=owner_allows_launch,
                        before_source=owner_allows_launch), discovery_reserve_scope(memory):
        result = _advance_programme_locked(memory, programme_id, root=root, max_cycles=1,
                                            as_of=as_of, clock=clock, **options)
    latest = load_programme(memory, programme_id)
    changed = _signature(memory.snapshot(), latest) != before
    order = result["execution_order"]
    closed = close_work_turn(memory, turn["id"], at=as_of or clock(), reason=result["tick_state"],
        cursors={"stage_start": order["stage_start"], "stage_order": order["stage_order"],
            "visited_stages": order["visited_stages"],
            "discovery_attempted": order["discovery_attempted"],
            "discovery_position": order["discovery_position"],
            "discovery_after_stage": order["discovery_after_stage"],
            "priority_counts": {stage: len(ids) for stage, ids in order["priority_concept_ids"].items()}})
    return {**result, "work_turn": {"id": closed["id"], "turn_sequence": closed["turn_sequence"],
            "state": closed["state"], "counters": deepcopy(closed["counters"])},
            "meaningful_progress": changed,
            "next_action": "owner_paused" if result["tick_state"] == "owner_paused" else
                           "continue_ready_work" if changed else "wait_for_saved_trigger"}


@wraps(_advance_programme_turn_locked)
def advance_programme_turn(memory, programme_id, **options):
    """Own selection bindings, actual work and closure through one writer."""
    with Journal(safe_path(memory.root, "programme_writer")).locked(), operation_writer(), \
            memory.defer_lookup_projection(flush_if=lambda: programme_service_state(memory)["enabled"]):
        _warm_research_history(memory)
        return _advance_programme_turn_locked(memory, programme_id, **options)


def _source_turn_readiness(memory, state, record, handlers, at):
    """Advisory local eligibility only; the controller still owns launch proof."""
    from .source_coverage import (RUN_KIND, TASK_KIND, _FINAL, _effective_route_pauses, _effective_saved_recovery,
                                  _effective_source_inspection,
                                  _has_unapplied_receipt, _target_superseded, route_registry, source_access_context,
                                  shared_source_hooks, source_readiness_hint)
    registry = route_registry(handlers)
    callback_ready, fresh_callback_ready = False, False
    local_ready, recovery_pending, waits, signal = False, False, [], []
    owners = [(key("source-run", scope["source_run_id"]), None) for scope in _review_scopes(record)]
    from .inventory_intake import inventory_work_records
    for work in inventory_work_records(state, record):
        if work["progress"].get("state") in {"complete", "superseded"}:
            continue
        source = work["binding"].get("source")
        if not source:
            continue
        task_ids = work["progress"].get("task_ids") or [source["original_task_id"]]
        for task_id in task_ids:
            task = state["query_tasks"].get(task_id)
            if task is not None:
                owners.append((key("source-run", task["run_id"]), {task_id}))
    seen = set()
    for run_key, subset in owners:
        run = state["batch_runs"].get(run_key)
        if not run or run.get("kind") != RUN_KIND:
            continue
        recovery = _effective_saved_recovery(memory, run_key, run)
        if recovery and recovery["remaining"] > 0:
            recovery_pending = local_ready = True
            signal.append([run_key, "saved_recovery", recovery])
        # A finite scan may yield without spending every callback slot. Its
        # saved next position wakes the next ordinary turn, even after the
        # active turn has closed. It is not accepted research progress and
        # therefore does not change the useful-work signatures.
        inspection = _effective_source_inspection(memory, run_key, run, state=state)
        if inspection and (inspection["ordinary_remaining"] > 0 or inspection["priority_remaining"] > 0):
            recovery_pending = local_ready = True
            signal.append([run_key, "inspection_continuation", inspection])
        original_recovery_ready = False
        pauses = _effective_route_pauses(memory, state, run_key, run)
        for pause in pauses.values():
            if pause.get("next_retry_at") and timestamp(pause["next_retry_at"]) > timestamp(at):
                waits.append(pause["next_retry_at"])
        for task_id in run["task_ids"]:
            if task_id in seen or subset is not None and task_id not in subset:
                continue
            seen.add(task_id)
            task = state["query_tasks"][task_id]
            if task.get("kind") != TASK_KIND or (task["status"] in _FINAL
                    and not (task.get("supplier_original_pending") is True and task.get("reconcile_required"))):
                continue
            if not any(cid in state["concepts"] and digest(state["concepts"][cid].get("description")) == expected
                       and state["concepts"][cid].get("status") not in {"merged", "retired", "excluded"}
                       and not _target_superseded(state, task, cid)
                       for cid, expected in task.get("targets", {}).items()):
                continue
            staged = _has_unapplied_receipt(memory, state, task)
            local_ready |= staged
            route = task["route"]; registered = registry[route]
            reconciling = task.get("reconcile_required") or task["status"] == "dispatching"
            access = None
            access_hint = (registered.get("source_due_hint") or registered.get("source_access_hint")
                           or shared_source_hooks(registered)["due_hint"])
            if access_hint is not None:
                try:
                    access = source_readiness_hint(registered, "due_hint", source_access_context(
                        state, task, as_of=at, pause=pauses.get(route)), state=state)
                except (SafetyError, OSError) as error:
                    if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                        raise
                    access = {"ready": False, "local_saved": False, "bypass_public_pause": False,
                              "reason": str(error) if isinstance(error, SafetyError) else "SOURCE_ACCESS_HINT_READ_FAILED"}
                local_ready |= bool(access.get("local_saved"))
                if access.get("next_retry_at") and timestamp(access["next_retry_at"]) > timestamp(at):
                    waits.append(access["next_retry_at"])
            hint = None
            saved_hint = shared_source_hooks(registered)["saved_hint"]
            if saved_hint is not None:
                try:
                    hint = source_readiness_hint(registered, "saved_hint", {
                        "route": route, "request": deepcopy(task["request"]),
                        "task_id": task_id, "acquisition_id": task.get("acquisition_id")}, state=state)
                except (SafetyError, OSError) as error:
                    if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                        raise
                    hint = {"reason": str(error) if isinstance(error, SafetyError) else "SOURCE_SAVED_HINT_READ_FAILED"}
            signal.append([task_id, staged, bool(registered.get("handler")), bool(registered.get("reconcile")), hint, access])
            saved_only = staged or bool(access and access.get("local_saved"))
            public_bypass = bool(access and access.get("bypass_public_pause"))
            retry = task.get("next_retry_at")
            if retry and timestamp(retry) > timestamp(at) and not saved_only and not public_bypass:
                waits.append(retry); continue
            if reconciling:
                available_recovery = (bool(registered.get("reconcile")) or staged) and (access is None or access.get("ready") is True)
                callback_ready |= available_recovery
                if task.get("acquisition_id") and not staged and (available_recovery or saved_only):
                    original_recovery_ready = True
                continue
            if not registered["connected"]:
                continue
            if (task["tries"] >= run["max_attempts"] or task.get("prelaunch_failures", 0) >= run["max_attempts"]
                    or task["status"] == "waiting_input"
                    or task.get("generation", task["tries"]) > 0 and task["status"] != "prepared" and not task.get("retryable")):
                continue
            pause = pauses.get(route)
            if pause and not saved_only and not public_bypass:
                if not pause.get("next_retry_at") or timestamp(pause["next_retry_at"]) > timestamp(at) or pause["task_id"] != task_id:
                    continue
            available_callback = access is None or access.get("ready") is True
            callback_ready |= available_callback
            fresh_callback_ready |= available_callback
        # A newly uncertain original is not unfinished ordinary inspection.
        # Preserve its one recovery obligation, but wake it only when that
        # original's existing due/retry rules allow recovery or saved proof is
        # present. A future wait must not manufacture repeated empty turns.
        if inspection and inspection["original_recovery_pending"] and original_recovery_ready:
            recovery_pending = local_ready = True
            signal.append([run_key, "original_recovery", inspection])
    # A proved selected-expiry obligation has no replacement task yet. It
    # still participates in fair backlog capacity before discovery runs; the
    # actual existing controller will prepare and recheck its exact owner.
    from .source_coverage import validate_inventory_source_original
    for work in inventory_work_records(state, record):
        source = work["binding"].get("source") or {}; progress = work["progress"]
        due = source.get("refresh_due_at"); retry = progress.get("next_retry_at")
        if (work["binding"]["purpose"] != "source" or progress.get("state") in {"complete", "superseded"}
                or progress.get("source_collected") or progress.get("task_ids") or not due
                or timestamp(due) > timestamp(at) or retry and timestamp(retry) > timestamp(at)):
            continue
        registered = registry[source["route"]]
        if not registered["connected"]:
            continue
        try:
            task = validate_inventory_source_original(state, source, concept_id=work["binding"]["subject"]["concept_id"])
            context = source_access_context(state, task, as_of=at)
            context.update(refresh_due_at=due, refresh_reason=source["refresh_reason"])
            hook = shared_source_hooks(registered)["due_hint"]
            hint = source_readiness_hint(registered, "due_hint", context, state=state,
                                         include_access_hint=False) if hook else {"ready": True}
            callback_ready |= hint.get("ready") is True
            fresh_callback_ready |= hint.get("ready") is True
            if hint.get("next_retry_at") and timestamp(hint["next_retry_at"]) > timestamp(at):
                waits.append(hint["next_retry_at"])
            signal.append([work["id"], "selected_expiry", hint])
        except (SafetyError, OSError) as error:
            if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                raise
            signal.append([work["id"], "selected_expiry_wait", str(error)])
    return {"callback_ready": callback_ready, "fresh_callback_ready": fresh_callback_ready,
            "local_ready": local_ready, "recovery_pending": recovery_pending,
            "waits": waits, "signal": signal}


def _discovery_turn_readiness(memory, state, record, handlers, at, *, discovery_step=None, runner_available=False):
    """Read the existing finite cycle; do not prepare or launch a new one."""
    from .catalogue_discovery import (VERSION as DISCOVERY_KIND, WAVE_VERSION, _callable, _receipt_path,
                                     discovery_wave_readiness, catalogue_seed_lane_hint)
    from .source_coverage import shared_source_hooks, source_readiness_hint
    if getattr(discovery_step, "discovery_wave_contract", None) == WAVE_VERSION:
        result = discovery_wave_readiness(memory, record["programme_id"], discovery_step.discovery_catalogue,
            handlers=handlers, as_of=at, runner_available=runner_available, state=state,
            location=record.get("location"),
            **discovery_step.discovery_wave_options)
        seed = catalogue_seed_lane_hint(memory, state, record["programme_id"], discovery_step, as_of=at)
        result["local_ready"] |= seed["ready"]
        result["signal"].append(["original_seed", seed["signal"]])
        retries = [value for value in (result["next_retry_at"], seed["next_retry_at"]) if value]
        result["next_retry_at"] = min(retries, key=timestamp) if retries else None
        return result
    ready, fresh_ready, local, projection, signals = False, False, False, {}, []
    for ref, cycle in state["batch_runs"].items():
        if cycle.get("kind") != DISCOVERY_KIND or cycle.get("programme_id") != record["programme_id"]:
            continue
        projection[ref] = {qid: {name: action.get(name) for name in
            ("state", "dispatch_started", "acquisition_id", "capture_ids", "observation_ids")}
            for qid, action in cycle.get("actions", {}).items()}
        for planned in cycle["planned_actions"]:
            for question in planned["questions"]:
                old = cycle.get("actions", {}).get(question["id"], {})
                if old.get("state") in {"acquired", "interpretation_pending", "empty"}:
                    break
                if old.get("state") in {"failed", "blocked", "insufficient"}:
                    continue
                saved = _receipt_path(memory, cycle, question).is_file()
                local |= saved
                registered = handlers.get(question["route"])
                hint_callback = shared_source_hooks(registered)["saved_hint"]
                if hint_callback is not None:
                    try:
                        hint = source_readiness_hint(registered, "saved_hint", {
                            "route": question["route"], "request": deepcopy(question["request"]),
                            "acquisition_id": old.get("acquisition_id")}, state=state)
                    except (SafetyError, OSError) as error:
                        if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                            raise
                        hint = {"reason": str(error) if isinstance(error, SafetyError) else "SOURCE_SAVED_HINT_READ_FAILED"}
                    signals.append([ref, question["id"], hint])
                uncertain = old.get("state") in {"intent_saved", "execution_uncertain"}
                callback = not saved and _callable(handlers.get(question["route"]), reconciliation=uncertain) is not None
                ready |= callback
                fresh_ready |= callback and not uncertain
                break
    return {"callback_ready": ready, "fresh_callback_ready": fresh_ready,
            "backlog_ready": False, "local_ready": local,
            "projection": projection, "signal": signals, "next_retry_at": None}


def _saved_job_signals(memory, state, record, *, as_of=None, retry_seconds=300, retry_not_before=None):
    """Filesystem arrival is a wake hint, not a validated model answer."""
    from .research_ai import (current_attempt, closed_model_retry_hint,
                              prepared_closed_model_retry_hint)
    from .inventory_intake import inventory_work_records
    ids, job_ids = set(_reporting_ids(record, state)), set()
    ids.update(row["binding"]["subject"]["concept_id"] for row in inventory_work_records(state, record))
    def references(value, depth=0):
        if depth > 10:
            return
        if isinstance(value, dict):
            for name, item in value.items():
                if name in {"job_id", "job_ids", "source_job_ids", "recovered_job_ids"}:
                    job_ids.update([item] if isinstance(item, str) else
                                   [ref for ref in item if isinstance(ref, str)] if isinstance(item, list) else [])
                elif isinstance(item, (list, dict)):
                    references(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:1000]:
                references(item, depth + 1)
    for scope in _review_scopes(record):
        references(scope["last_results"])
    for row in inventory_work_records(state, record):
        references(row["progress"].get("last_result"))
    # A/B work uses original observation/candidate IDs, not necessarily a
    # product ID. Its validated saved admission is the ownership link.
    from .work_turns import TURN_KIND, _record as work_turn_record
    for ref, turn in state["batch_runs"].items():
        if turn.get("kind") == TURN_KIND and turn.get("programme_id") == record["programme_id"]:
            checked = work_turn_record(state, ref)
            job_ids.update(item["job_id"] for item in checked["reservations"]["model"].values())
    for ref, job in state["jobs"].items():
        items = job.get("request", {}).get("payload", {}).get("items", [])
        if any(isinstance(item, dict) and item.get("id") in ids for item in items):
            job_ids.add(ref)
    signals = {}
    for ref in sorted(job_ids):
        if ref not in state["jobs"]:
            continue
        folder = current_attempt(safe_path(memory.root, "ai_jobs", ref))
        found = []
        for name in ("result.json", "completion.json"):
            path = folder / name
            if path.is_symlink():
                raise SafetyError("SYMLINK_PATH_REFUSED")
            if path.is_file():
                stat = path.stat(); found.append([name, stat.st_size, stat.st_mtime_ns])
        if found:
            signals[ref] = found
        elif as_of is not None:
            try:
                hint = closed_model_retry_hint(memory, ref, as_of=as_of,
                    retry_seconds=retry_seconds, not_before=retry_not_before, state=state)
                if not hint["ready"] and folder.name.startswith("retry-"):
                    prepared = prepared_closed_model_retry_hint(memory, ref, as_of=as_of,
                        retry_seconds=retry_seconds, not_before=retry_not_before, state=state)
                    if prepared["ready"] or prepared["next_retry_at"] is not None:
                        hint = prepared
                signals[ref] = {"closed_retry": hint}
            except (SafetyError, OSError) as error:
                if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                    raise
                # A pure wake hint never authorises execution or turns an
                # incomplete old original into a fresh request. Its owning
                # stage records the precise recovery problem when serviced.
                signals[ref] = {"original_wait": str(error) if isinstance(error, SafetyError)
                                else "AI_ORIGINAL_LOCAL_FILE_WAIT"}
    return signals


def _report_writing_model_ready(memory, state, dataset, runner, *, languages, at, retry_seconds):
    """Distinguish actual G questions from completed prose awaiting local apply."""
    from .report_narration import (make_narrative_input, _completed_items, _contract_repair_available,
        _contract_repairable, _repair_descriptor, ROMAN_REPAIR_ERROR, _checked_saved_output,
        _writing_item_ref, _narrative_product, _has_roman_context, _has_contract_context)
    from .research_ai import MODEL, MAX_JOB_BYTES, current_attempt, prompt_version
    functional = dataset.get("schema_version") == 3
    view = SimpleNamespace(root=memory.root, snapshot=lambda: state)
    correction = (not functional and (_has_roman_context(view, dataset) or _has_contract_context(view, dataset)))
    identity = getattr(runner, "identity", "codex-chatgpt-high-v1")

    def edition(payload):
        context = {name: value for name, value in payload.items() if name != "items"}
        completed = _completed_items(memory, context, payload["items"], state=state,
                                     runner_identity=None if functional or correction else identity)
        accepted, refs = {}, {}
        for item in payload["items"]:
            original = completed.get(item["id"])
            question, origin = dict(context, items=[item]), original
            if original is None:
                # Reuse v4 across identities only after exact completed proof.
                # An uncertain foreign original is not a new model question.
                owners = [job for job in state["jobs"].values() if job["request"].get("role") == "G"
                    and job["request"].get("prompt_version") == prompt_version("G", context)
                    and {name: value for name, value in job["request"]["payload"].items() if name != "items"} == context
                    and item in job["request"]["payload"]["items"]]
                if functional and owners and any(job["request"]["runner"] != identity for job in owners):
                    continue
            elif original["accepted"] is None:
                roman = context["language"] == "hi-Latn" and original["failure"] == ROMAN_REPAIR_ERROR
                contract = _contract_repair_available(context, item) and _contract_repairable(original["failure"])
                if not (roman and original["rejected_output"] is not None or contract):
                    continue
                repair = _repair_descriptor(context, item, original, contract=not roman)
                question["narration_repair"] = repair
                owners = [(job_id, job) for job_id, job in state["jobs"].items()
                    if job["request"].get("role") == "G" and isinstance(job["request"]["payload"].get("narration_repair"), dict)
                    and job["request"]["payload"]["narration_repair"].get("scope_id") == repair["scope_id"]]
                if len(owners) > 1:
                    continue  # The owning stage records the exact conflict.
                if owners:
                    job_id, job = owners[0]; request = job["request"]
                    if (request["payload"] != question or request["model"] != MODEL
                            or request["prompt_version"] != prompt_version("G", context) or key("job", request) != job_id):
                        continue
                    folder = current_attempt(safe_path(memory.root, "ai_jobs", job_id))
                    result, completion = folder / "result.json", folder / "completion.json"
                    if result.exists() or completion.exists():
                        intent = load_json(folder / "intent.json", MAX_JOB_BYTES)
                        if not isinstance(intent, dict) or intent.get("request") != request:
                            continue
                        saved = load_json(result if result.exists() else completion, MAX_JOB_BYTES)
                        if not isinstance(saved, dict) or "response" not in saved:
                            continue  # Uncertain local receipt is not permission to run a new correction.
                        if result.exists() and saved.get("request_hash") != digest(request):
                            continue
                        checked = _checked_saved_output(question, saved["response"], request["prompt_version"])
                        value = checked["accepted"].get(item["id"])
                        if value is not None:
                            origin = {"job_id": job_id, "accepted": value}
                        else:
                            continue  # A completed failed correction has no further model route.
                    elif request["runner"] != identity:
                        continue
            if origin is not None and origin["accepted"] is not None:
                accepted[item["id"]] = origin["accepted"]
                if functional:
                    refs[item["id"]] = _writing_item_ref(memory, context, item, origin["accepted"], origin["job_id"], state=state)
            elif _model_question_ready(memory, state, runner, "G", question, at=at, retry_seconds=retry_seconds):
                return True, accepted, refs
        return False, accepted, refs

    english = make_narrative_input(dataset, "en")
    ready, accepted, refs = edition(english)
    if ready or "hi-Latn" not in languages:
        return ready
    title = english["items"][0]["id"]
    ids = [product["concept_id"] for product in dataset["products"]]
    source = {"title": accepted[title]["title"] if title in accepted else None,
              "products": [_narrative_product(cid, accepted[cid]) for cid in ids if cid in accepted]}
    if not functional:
        return (len(accepted) == len(english["items"]) and edition(make_narrative_input(dataset, "hi-Latn", english=source))[0])
    hi_title = key("report-title", [dataset["issue_id"], "hi-Latn", "writing-input-v1"])
    available = ([hi_title] if title in accepted else []) + [cid for cid in ids if cid in accepted]
    source_refs = {}
    for cid in available:
        original = title if cid == hi_title else cid
        projected = {"title": source["title"]} if cid == hi_title else next(row for row in source["products"] if row["concept_id"] == cid)
        source_refs[cid] = dict(refs[original], source_english_hash=digest(projected))
    return edition(make_narrative_input(dataset, "hi-Latn", english=source, item_ids=available, english_item_refs=source_refs))[0]


def _newer_report_hold(draft, reservation):
    """A genuine checked replacement can outlive its programme metadata save."""
    if (not isinstance(draft, dict) or not isinstance(reservation, dict)
            or reservation.get("state") != "reserved"
            or type(reservation.get("revision")) is not int or type(draft.get("revision")) is not int
            or reservation["revision"] <= draft["revision"]):
        return False
    from .issue_allocation import selection_fingerprint
    try:
        return reservation.get("selection_hash") == selection_fingerprint(reservation)
    except (SafetyError, KeyError, TypeError, ValueError):
        return False


def _cached_legacy_edition_complete(edition, root):
    """A bounded byte check for a local-only wake hint, not packet acceptance."""
    from .live_outputs import digest_bytes
    if not isinstance(edition, dict) or edition.get("state") != "completed":
        return False
    for artifact, hash_field in (("html", "sha256"), ("pdf", "pdf_sha256")):
        proof = edition.get(artifact)
        if (not isinstance(proof, dict) or not isinstance(proof.get("path"), str)
                or type(proof.get("bytes")) is not int or not 0 < proof["bytes"] <= 32 * 1024 * 1024):
            return False
        path = safe_path(proof["path"])
        if not path.is_relative_to(safe_path(runtime_root(root))) or not path.is_file():
            return False
        with path.open("rb") as stream:
            raw = stream.read(proof["bytes"] + 1)
        if len(raw) != proof["bytes"] or digest_bytes(raw) != proof.get(hash_field):
            return False
    return True


def _frozen_writing_ready(memory, state, record, *, root, at, detailed=False,
                          runner=None, allow_fresh_legacy_model=True):
    """Exact frozen G work, plus one local recovery for a newer checked hold.

    An old complete packet cannot conceal its genuine newer canonical owner.
    That metadata recovery needs no model allowance; missing new prose still
    uses the exact frozen question and the usual Jobs checks.
    """
    if type(allow_fresh_legacy_model) is not bool:
        raise SafetyError("REPORT_NARRATION_OPTIONS_INVALID")
    result = {"ready": False, "local_ready": False, "model_inputs": []}
    lane_wait = record["deferrals"].get("lane:reports")
    if (lane_wait and timestamp(lane_wait["next_retry_at"]) > timestamp(at)
            and not _due(record, "lane", "reports",
                digest({"stage": "reports", "inputs": _signature(state, record)}), at)):
        return result if detailed else False
    candidates = []
    for slot in record["release_slots"]:
        draft = record["drafts"].get(slot["issue_id"], {})
        reservation = state["issue_reservations"].get(slot["issue_id"])
        newer_hold = _newer_report_hold(draft, reservation)
        if (draft.get("complete") and not newer_hold
                or reservation and reservation.get("state") in {"uploaded", "published", "cancelled"}):
            continue
        try:
            frozen = _frozen_report_input(root, slot, None if newer_hold else draft, reservation, memory.evidence_type)
            if frozen and frozen.get("product_facts") is not None:
                candidates.append((frozen, newer_hold))
        except (SafetyError, OSError):
            continue  # Actual report execution records its exact repair reason.
    if not candidates:
        return result if detailed else False
    from .live_outputs import build_issue_dataset
    from .service import load_policy
    language = load_policy()["ongoing_pdf_language"]
    pool = _programme_report_pool(memory, record, as_of=at, state=state, functional_packets=True)
    eligible = {row["concept_id"]: row for row in _reporting_eligible(record, state, pool)}
    for frozen, newer_hold in candidates:
        try:
            issue = frozen["issue"]
            if _current_report_selection(issue, eligible, state, record) is not None:
                continue
            binding = digest([issue, frozen["product_facts"], language])
            if not _due(record, "reports", issue["issue_id"], binding, at):
                continue
            dataset = build_issue_dataset(issue, frozen["product_facts"], evidence_type=memory.evidence_type)
            folder = safe_path(runtime_path(root, "data", "outputs"), issue["issue_id"], "revision-" + str(issue["revision"]))
            result["local_ready"] |= newer_hold
            if not (folder / "delivery-manifest.json").exists():
                languages = ("en", "hi-Latn") if issue["projected_ordinal"] <= 2 else (language,) if language else ()
                if dataset.get("schema_version") != 3 and not allow_fresh_legacy_model:
                    if languages and not frozen.get("presentation_source"):
                        from .report_narration import saved_legacy_narratives
                        saved = saved_legacy_narratives(memory, issue, dataset, runner, languages=languages, state=state)
                        status_path = folder / "package-status.json"
                        status = load_json(status_path, 512 * 1024) if status_path.exists() else {}
                        stages = (status.get("stage_outcomes") if isinstance(status, dict)
                            and status.get("input_hash") == digest(frozen) else None)
                        editions = stages.get("editions") if isinstance(stages, dict) else None
                        editions = editions if isinstance(editions, dict) else {}
                        for cached_language in saved["complete_languages"]:
                            edition = editions.get(cached_language, {})
                            if not _cached_legacy_edition_complete(edition, root):
                                result["local_ready"] = True
                        # Both cached editions can finish the packet even if
                        # their internal renders already exist. One rendered
                        # EN edition with absent HI is not a recurring wake.
                        result["local_ready"] |= set(languages) <= set(saved["complete_languages"])
                    elif frozen.get("presentation_source"):
                        result["local_ready"] = True
                    continue
                result["ready"] = True
                if not detailed:
                    return True
                if languages and not frozen.get("presentation_source"):
                    result["model_inputs"].append({"dataset": dataset, "languages": languages})
        except (SafetyError, OSError):
            continue
    return result if detailed else False


def _photo_turn_ready(memory, state, record, at, *, media_root=None, detailed=False):
    from .product_images import current_public_image, product_image_readiness
    pool = _programme_report_pool(memory, record, as_of=at, state=state, functional_packets=True)
    result = {"ready": False, "signal": [], "waits": []}
    for scope in _review_scopes(record):
        for cid in scope["concept_ids"]:
            facts = pool["facts_by_concept"].get(cid, {})
            candidates = sorted(facts.get("image_candidates", []), key=lambda row: row["relationship"] != "exact_product")
            if not candidates or current_public_image(state, cid, candidates, media_root=media_root,
                                                      as_of=at, require_report_quality=True):
                continue
            binding = digest({"description": state["concepts"][cid]["description"], "candidates": candidates})
            for candidate in candidates:
                hint = product_image_readiness(memory, cid, candidate, as_of=at, state=state,
                                               require_report_quality=True)
                if detailed:
                    result["signal"].append([scope["programme_id"], cid, candidate, hint["signal"], hint["ready"], hint["reason"]])
                    result["ready"] |= hint["ready"]
                    if hint["next_retry_at"] and timestamp(hint["next_retry_at"]) > timestamp(at):
                        result["waits"].append(hint["next_retry_at"])
                elif hint["ready"] and _due(scope, "photos", cid, binding, at):
                    return True
    return result if detailed else False


def _model_question_ready(memory, state, runner, role, payload, *, at, retry_seconds, declared_version=None,
                          runner_identity=None, detailed=False):
    """Pure advisory view of one actual current question, not permission to run.

    Missing work is not ready merely because a count is below its target.
    An exact original with an uncertain execution or a future retry stays a
    wait. The normal owning stage still validates its complete original batch
    and Jobs rechecks its files/admission before any execution.
    """
    from .research_ai import (MODEL, _item_bindings, current_attempt, prompt_version,
                              closed_model_retry_hint, prepared_closed_model_retry_hint)
    identity = runner_identity or getattr(runner, "identity", "codex-chatgpt-high-v1")
    version = prompt_version(role, payload, declared_version)
    bindings = _item_bindings(role, payload, MODEL, version, identity)
    wanted = set(bindings.values())
    owners = {value: job_id for job_id, job in state["jobs"].items() for value in job["binding_keys"].values()}
    result = {"fresh_items": [item for item in payload["items"] if bindings[item["id"]] not in owners],
              "original_item_counts": []}
    for job_id in {owners[value] for value in wanted if value in owners}:
        base = safe_path(memory.root, "ai_jobs", job_id); folder = current_attempt(base)
        files = [folder / name for name in ("intent.json", "runner_execution.json", "execution_failure.json", "failure.json",
                                           "result.json", "completion.json")]
        if any(path.is_symlink() for path in files):
            raise SafetyError("SYMLINK_PATH_REFUSED")
        if any(path.exists() for path in files[-2:]):
            continue  # Applying a completed original is local work, not a new model slot.
        if not any(path.exists() for path in files):
            if folder == base:
                hint = {"ready": True}
            else:
                hint = prepared_closed_model_retry_hint(memory, job_id, as_of=at,
                    retry_seconds=retry_seconds, state=state)
        else:
            hint = closed_model_retry_hint(memory, job_id, as_of=at,
                retry_seconds=retry_seconds, state=state)
        if hint["ready"]:
            result["original_item_counts"].append(len(state["jobs"][job_id]["request"]["payload"]["items"]))
    result["ready"] = bool(result["fresh_items"] or result["original_item_counts"])
    return result if detailed else result["ready"]


def _closed_source_allowance_continuation(state, record, *, ready_lanes):
    """Continue honest ready source work after its original finite turn closes.

    Scheduling capacity is not a lifetime search allowance. Inspect the
    latest original policy and reservations without changing them. A newer
    unused turn supersedes this witness, so an optimistic source hint cannot
    keep creating empty turns. Provider/controller checks still own launch.
    """
    from .work_turns import TURN_KIND, _record
    if not any(ready_lanes.values()):
        return None
    turns = [row for row in state["batch_runs"].values()
             if row.get("kind") == TURN_KIND and row.get("programme_id") == record["programme_id"]]
    if not turns:
        return None
    if any(type(row.get("turn_sequence")) is not int for row in turns):
        raise SafetyError("WORK_TURN_RECORD_INVALID")
    turn = _record(state, max(turns, key=lambda row: row["turn_sequence"])["id"])
    consumed = turn["counters"]["source_callbacks"]
    if turn["state"] != "closed" or consumed == 0:
        return None
    policy, lanes = turn["policy"], turn["ready_lanes"]
    for lane, ready in ready_lanes.items():
        if not ready:
            continue
        blocked = False
        if all(lanes.values()) and policy["source_callbacks"] == 1:
            preferred = "exploration" if turn["programme_turn_sequence"] % 2 else "backlog"
            blocked = lane != preferred
        other = "exploration" if lane == "backlog" else "backlog"
        used_other = sum(item["lane"] == other for item in turn["reservations"]["source"].values())
        protected = policy["exploration_slots"] if other == "exploration" else 1
        if all(lanes.values()):
            protected = min(max(1, protected), policy["source_callbacks"] - 1)
        withheld = max(0, protected - used_other) if lanes[other] else 0
        if blocked or consumed >= policy["source_callbacks"] - withheld:
            return {"turn_id": turn["id"], "turn_sequence": turn["turn_sequence"],
                    "lane": lane, "reason": "WORK_TURN_SOURCE_LIMIT"}
    return None


def _closed_model_allowance_continuation(memory, state, record, *, runner, at, retry_seconds,
                                       model_wait, semantic_hints, report_inputs, source_questions,
                                       update_questions, catalogue=None, discovery_step=None,
                                       allow_fresh_legacy_model=True, review_hints=()):
    """One genuine next turn may service ready work blocked by a closed allowance.

    Read the latest original turn, including checkpoints saved by older code.
    Never reset/resume a closed allowance or use an unrelated spent purpose.
    A subsequent empty turn has unused counters and supersedes this witness,
    so unchanged work cannot open an endless sequence of empty rounds.

    Existing semantic/writing hints are reused. Other lanes prepare only
    current, due questions until one qualifying question is found, sharing
    this verified state rather than replaying research history per target.
    """
    if type(allow_fresh_legacy_model) is not bool:
        raise SafetyError("REPORT_NARRATION_OPTIONS_INVALID")
    from .work_turns import TURN_KIND, _record, protected_model_slots
    turns = [row for row in state["batch_runs"].values()
             if row.get("kind") == TURN_KIND and row.get("programme_id") == record["programme_id"]]
    if runner is None or model_wait or not turns:
        return None
    if any(type(row.get("turn_sequence")) is not int for row in turns):
        raise SafetyError("WORK_TURN_RECORD_INVALID")
    turn = _record(state, max(turns, key=lambda row: row["turn_sequence"])["id"])
    if turn["state"] != "closed":
        return None  # An interrupted original must retain its actual counters.
    counter, policy = turn["counters"], turn["policy"]

    def spent(purpose, item_count=1, review=False):
        withheld = protected_model_slots(turn, purpose, review=review, jobs=state["jobs"])
        if counter["model_executions"] > 0 and counter["model_executions"] >= policy["model_executions"] - withheld:
            return "WORK_TURN_MODEL_LIMIT"
        if (purpose in policy["purpose_items"] and counter["purpose_items"][purpose] > 0
                and counter["purpose_items"][purpose] + item_count > policy["purpose_items"][purpose]):
            return "WORK_TURN_PURPOSE_LIMIT"
        return None

    batch_sizes = {"C:product_query_profile": 10, "E:fit": 5, "E:source_support": 5, "F": 5}
    reasons, groups, grouping_scope = {}, {}, None

    def possibly_spent(purpose, review=False):
        return spent(purpose, batch_sizes.get(purpose, 1), review)

    def witness(purpose, review=False):
        return {"turn_id": turn["id"], "turn_sequence": turn["turn_sequence"],
                "purpose": purpose, "reason": reasons.get(purpose) or spent(purpose, review=review)}

    def ready(purpose, role, payload, *, declared_version=None, runner_identity=None, batch_size=None, review=False):
        if not possibly_spent(purpose, review):
            return False
        from .research_ai import MAX_JOB_BYTES
        question = _model_question_ready(memory, state, runner, role, payload, at=at,
            retry_seconds=retry_seconds, declared_version=declared_version,
            runner_identity=runner_identity, detailed=True)
        if not question["ready"]:
            return False
        for count in [1, *question["original_item_counts"]]:
            reason = spent(purpose, count, review)
            if reason is not None:
                reasons[purpose] = reason
                return True
        # Normal profile/fit/source/supplier helpers group compatible fresh
        # items. A genuine 3-item group cannot fit an original allowance with
        # only 2 items left, even though its counter is below the maximum.
        context = {name: value for name, value in payload.items() if name != "items"}
        group = groups.setdefault(digest([grouping_scope, purpose, role, declared_version,
            runner_identity or getattr(runner, "identity", "codex-chatgpt-high-v1"), context]), [])
        for item in question["fresh_items"]:
            if item in group or len(canonical(dict(context, items=[item]))) > MAX_JOB_BYTES:
                continue
            trial = group + [item]
            if group and (len(trial) > (batch_size or batch_sizes.get(purpose, 1))
                          or len(canonical(dict(context, items=trial))) > MAX_JOB_BYTES):
                group.clear()
            group.append(item)
            reason = spent(purpose, len(group), review)
            if reason is not None:
                reasons[purpose] = reason
                return True
        return False

    if spent("B") and any(hint.get("model_ready") for hint in semantic_hints):
        return witness("B")
    if spent("B", review=True) and any(hint.get("model_ready") for hint in review_hints):
        return witness("B", review=True)
    if spent("G"):
        for packet in report_inputs:
            if not allow_fresh_legacy_model and packet["dataset"].get("schema_version") != 3:
                continue
            try:
                if _report_writing_model_ready(memory, state, packet["dataset"], runner,
                        languages=packet["languages"], at=at, retry_seconds=retry_seconds):
                    return witness("G")
            except (SafetyError, OSError) as error:
                if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                    raise
    if discovery_step is not None and spent("A"):
        for purpose, role, payload, version in _discovery_model_questions(memory, state, record,
                discovery_step=discovery_step, at=at):
            if ready(purpose, role, payload, declared_version=version):
                return witness(purpose)
    from .source_coverage import _product_context
    from .product_queries import (query_profile_input, query_profile_scope_hash, current_query_profile,
                                  _catalogue_profile, _short_label, _grounded_catalogue_profile,
                                  _payload as profile_payload)
    from .fit_review import (PRACTICAL_CONTRACT, _facts, _review_base, _current_base, _bound_jobs,
                             _payload as fit_payload)
    from .fit_description import (DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT, FUNCTIONAL_RESPONSE_VERSION,
                                  FUNCTIONAL_REVIEW_REASON,
                                  functional_changed_criteria)
    from .fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID
    from .functional_types import functional_role_context, effective_functional_definition
    from .research_flow import current_assessment_fit
    from .source_interpretation import _item as source_item
    from .research_ai import FUNCTIONAL_SOURCE_STYLE
    from .material_updates import update_payload, UPDATE_PROMPT_VERSION

    def fit_ready(base):
        payload = fit_payload(base)
        original = _bound_jobs(state, base, payload)
        identity = None
        if original:
            identity = original[0][1]
            payload = dict(deepcopy(state["jobs"][original[0][0]]["request"]["payload"]),
                           items=payload["items"])
        elif base["binding"]["contract_version"] == FUNCTIONAL_CONTRACT:
            payload["response_attribution"] = FUNCTIONAL_RESPONSE_VERSION
        return ready("E:fit", "E", payload, declared_version=base["binding"]["contract_version"],
                     runner_identity=identity)

    for scope in _review_scopes(record):
        grouping_scope = scope["programme_id"]
        for cid in scope["concept_ids"] if possibly_spent("C:product_query_profile") or possibly_spent("E:fit") else ():
            try:
                _, excluded = _product_context(state, cid)
                if excluded:
                    continue
                if possibly_spent("C:product_query_profile") and not current_query_profile(state, cid):
                    item = query_profile_input(state, cid)
                    if (_due(scope, "profiles", cid, query_profile_scope_hash(state, cid), at)
                            and not (_catalogue_profile(state, item, catalogue) or _short_label(item)
                                     or _grounded_catalogue_profile(state, item, catalogue))
                            and ready("C:product_query_profile", "C", profile_payload([item]))):
                        return witness("C:product_query_profile")
                if not possibly_spent("E:fit"):
                    continue
                contract = (FUNCTIONAL_CONTRACT if functional_role_context(state, cid, role="C") is not None
                            else DESCRIPTION_CONTRACT)
                parent = state["assessments"].get(cid)
                current = current_assessment_fit(state, cid, expected_policy_id=FIT_POLICY_ID)
                accepted_contracts = ((FUNCTIONAL_CONTRACT,) if contract == FUNCTIONAL_CONTRACT
                                      else (PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT))
                bases = [base for base in state["source_checks"].values()
                    if base.get("kind") == "fit_review_base" and base.get("state") == "prepared"
                    and base.get("binding", {}).get("concept_id") == cid
                    and key("fit-review-apply", base.get("reference")) not in state["source_checks"]]
                for base in bases:
                    try:
                        _current_base(state, base)
                        if base["binding"]["contract_version"] != contract:
                            continue
                        binding = (digest(base["reference"]) if current and parent.get("contract_version") in accepted_contracts
                                   else base["binding"]["input_hash"])
                        if (_due(scope, "fit", cid, binding, at)
                                and fit_ready(base)):
                            return witness("E:fit")
                    except (SafetyError, OSError):
                        continue
                if current and parent.get("contract_version") in accepted_contracts:
                    continue
                item = _facts(state, cid, contract_version=contract)
                if not _due(scope, "fit", cid, digest(item), at):
                    continue
                requested = (functional_changed_criteria(parent, item) if contract == FUNCTIONAL_CONTRACT
                             else list(ACTIVE_CRITERION_IDS)) or list(ACTIVE_CRITERION_IDS)
                base = _review_base(state, cid, item, parent, requested,
                    FUNCTIONAL_REVIEW_REASON if contract == FUNCTIONAL_CONTRACT
                    else "full_practical_description_estimates_v3_review", at, contract_version=contract)
                if fit_ready(base):
                    return witness("E:fit")
            except (SafetyError, OSError) as error:
                if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                    raise
                continue  # A bad target is not evidence that a healthy sibling must wait.
        if possibly_spent("E:source_support"):
            for question in source_questions.get(scope["programme_id"], []):
                if (not question["unknown_ids"] or question.get("kind") != "functional_source_scope"
                        and question.get("kind") != "saved_source_support" and question["relevant_ids"]):
                    continue
                target = question["task_id"] + ":" + question["concept_id"]
                if not _due(scope, "match", target, digest(question), at):
                    continue
                try:
                    functional = question.get("kind") == "functional_source_scope"
                    style = FUNCTIONAL_SOURCE_STYLE if functional else "short-references-support-v3"
                    payload = {"mode": "marketplace_relevance_only", "relevance_style": style,
                               "criteria": [], "items": [source_item(state, question, 30)]}
                    if ready("E:source_support", "E", payload, review=functional):
                        return witness("E:source_support", review=functional)
                except (SafetyError, OSError) as error:
                    if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                        raise
        if spent("E"):
            for entry in update_questions:
                cid = entry["binding"]["concept_id"]
                if cid in scope["concept_ids"] and _due(scope, "material_updates", cid, digest(entry["binding"]), at):
                    try:
                        if ready("E", "E", update_payload([entry["item"]]), declared_version=UPDATE_PROMPT_VERSION):
                            return witness("E")
                    except (SafetyError, OSError) as error:
                        if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                            raise
    if possibly_spent("F"):
        from .supplier_capture_store import load_saved_supplier_capture
        from .source_interpretation import _current_supplier_record
        from .supplier_quote_repair import supplier_model_question, supplier_original_retry_question
        from .suppliers import _supplier_payload
        from .supplier_scope_candidates import prepare_candidate_items, candidate_payload
        from .supplier_checks import candidate_review_id
        from .supplier_terms import PROMPT_VERSION
        view = SimpleNamespace(root=memory.root, snapshot=lambda: state)
        legacy_groups = {}
        for path in sorted(safe_path(memory.root, "supplier_pages").glob("supplier-capture-*/capture.json")):
            try:
                capture = load_saved_supplier_capture(view, path); item = capture["bundle"]["item"]
                if item is None:
                    continue
                cid = capture["product"]["concept_id"]
                scopes = [scope for scope in _review_scopes(record) if cid in scope["concept_ids"]
                          and _due(scope, "suppliers", capture["capture_id"], capture["capture_id"], at)]
                if not scopes:
                    continue
                grouping_scope = tuple(scope["programme_id"] for scope in scopes)
                dates = {page["id"]: next(row["checked_at"] for row in capture["bundle"]["receipts"]
                                         if row.get("id") == page["id"]) for page in item["pages"]}
                if any(timestamp(date) > timestamp(at) for date in dates.values()):
                    continue
                if effective_functional_definition(state, cid)["accepted"]:
                    for candidate in prepare_candidate_items(state, capture):
                        if (candidate_review_id(candidate, runner) not in state["supplier_assessments"]
                                and ready("F", "F", candidate_payload([candidate]), declared_version=PROMPT_VERSION,
                                          batch_size=1)):
                            return witness("F")
                elif not any(_current_supplier_record(state, row, {"item": item, "page_dates": dates}, as_of=at)
                             for row in state["supplier_assessments"].values()):
                    group = legacy_groups.setdefault(grouping_scope, {})
                    if item["id"] in group and group[item["id"]] != item:
                        raise SafetyError("SUPPLIER_CAPTURE_ORIGINAL_CHANGED")
                    group[item["id"]] = item
            except (SafetyError, OSError) as error:
                if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                    raise
        for grouping_scope, items in legacy_groups.items():
            current_items = _supplier_payload(list(items.values()))["items"]
            for item in items.values():
                try:
                    question = supplier_original_retry_question(memory,
                        supplier_model_question(memory, item, state=state), runner,
                        current_items=current_items, as_of=at, retry_seconds=retry_seconds, state=state)
                    if (question["state"] in {"original", "repair"} and question["payload"] is not None
                            and not question["local_ready"]
                            and question["runner_identity"] in
                                (None, getattr(runner, "identity", "codex-chatgpt-high-v1"))
                            and ready("F", "F", question["payload"],
                                      runner_identity=question["runner_identity"],
                                      batch_size=1 if question["state"] == "repair" else None)):
                        return witness("F")
                except (SafetyError, OSError) as error:
                    if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                        raise
    return None


def _discovery_model_questions(memory, state, record, *, discovery_step, at):
    """Actual saved A/B admission and original C seed questions, not counts."""
    from .catalogue_discovery import VERSION as CYCLE, WAVE_VERSION, SEED_CYCLE_VERSION, _seed_cycle_id
    from .research_flow import DESCRIPTION_SCOPE, DESCRIPTION_RULES, QUALITY_FAILURES
    from .discovery import find_possible_matches
    from .brand_reviews import brand_binding
    from .service import CATEGORIES
    held = record["deferrals"].get("lane:discovery")
    if held and held["binding"] == _signature(state, record) and timestamp(held["next_retry_at"]) > timestamp(at):
        return
    for wave in state["batch_runs"].values():
        if wave.get("kind") not in (CYCLE, WAVE_VERSION) or wave.get("programme_id") != record["programme_id"]:
            continue
        acquisitions = {action["acquisition_id"] for action in wave.get("actions", {}).values()
                        if action.get("state") in ("acquired", "interpretation_pending") and action.get("acquisition_id")}
        unique = {row["semantic_id"]: row["semantic"] for row in state["observations"].values()
                  if row["acquisition_id"] in acquisitions}
        deferred = {row["affected_id"] for row in state["problems"].values()
            if row["stage"] == "description" and row["reason"] in QUALITY_FAILURES
            and state["jobs"].get(row.get("job_id"), {}).get("request", {}).get("payload", {}).get("description_scope") == DESCRIPTION_SCOPE}
        pending = [{"id": sid, "source_text": text} for sid, text in unique.items()
                   if sid not in state["descriptions"] and sid not in deferred]
        for offset in range(0, len(pending), 10):
            yield "A", "A", {"items": pending[offset:offset + 10], "description_scope": DESCRIPTION_SCOPE,
                              "description_rules": DESCRIPTION_RULES}, None
        for sid in unique:
            description = state["descriptions"].get(sid)
            for index, product in enumerate(description["result"]["products"] if description else []):
                child = key("child", [sid, index, digest(description["result"])])
                association = state["associations"].get(child)
                if association and association.get("status") != "brand_review_pending":
                    continue
                try:
                    brand = brand_binding(state, "child", child, product)
                    if (product["category"] not in CATEGORIES or brand["verdict"] == "established"
                            or association and brand["review_id"] != association.get("brand_review_id")):
                        continue
                    candidates = find_possible_matches(product, state["concepts"])["candidates"]
                    if candidates:
                        yield "B", "B", {"items": [{"id": child, "description": product, "candidates": candidates}]}, None
                except SafetyError as error:
                    if str(error) in INTEGRITY_FAILURES:
                        raise
    seed = state["batch_runs"].get(_seed_cycle_id(record["programme_id"]))
    catalogue = getattr(discovery_step, "discovery_catalogue", None)
    if not seed or seed.get("kind") != SEED_CYCLE_VERSION or catalogue is None:
        return
    from .catalogue_seed import (load_prepared_catalogue_branch, VERSION, PROMPT_VERSION,
                                 _branch, _load_material, _verify_prepared_item)
    from .catalogue_expansion import effective_catalogue, _exact_match
    view = SimpleNamespace(root=memory.root, snapshot=lambda: state, evidence_type=memory.evidence_type)
    current = effective_catalogue(catalogue, state)
    for planned in seed["planned_branches"]:
        progress = seed["branches"].get(planned["branch_id"], {})
        if (progress.get("state") == "completed" or progress.get("next_retry_at")
                and timestamp(progress["next_retry_at"]) > timestamp(at)):
            continue
        try:
            prepared = load_prepared_catalogue_branch(view, catalogue, planned)
            branch, items = _branch(current, planned["branch_id"]), []
            for item in prepared["payload"]["items"]:
                _verify_prepared_item(current, branch, _load_material(view, item["id"]), item)
                if (_exact_match(current, item["material"]["label"]) is None
                        and not any(row.get("seed_version") == VERSION and row.get("input") == item
                                    and not row.get("resumable", False) for row in state["catalogue_additions"].values())):
                    items.append(item)
            if items:
                yield "C", "C", dict(prepared["payload"], items=items), PROMPT_VERSION
        except (SafetyError, OSError) as error:
            if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                raise


def _programme_turn_readiness(memory, record, *, root, handlers=None, runner=None,
                             discovery_step=None, steps=None, private_staging=None,
                             media_root=None, as_of=None, state=None, retry_seconds=300):
    """Non-launching wake decision. Collectors/Jobs recheck their own authority.

    A finite inspection may settle an optimistic readiness hint. Its exact
    checkpoint then suppresses an unchanged empty round until saved inputs,
    configuration, an original reply or an actual deadline changes.
    """
    at = as_of or utc_now(); timestamp(at)
    if type(retry_seconds) is not int or retry_seconds < 1:
        raise SafetyError("PROGRAMME_SLICE_INVALID")
    state = memory.snapshot() if state is None else state
    handlers = {} if handlers is None else handlers
    checkpoint = record.get("cooperative_checkpoint")
    if checkpoint is not None and (not isinstance(checkpoint, dict) or checkpoint.get("version") != 1
            or type(checkpoint.get("continue_ready")) is not bool or not isinstance(checkpoint.get("wake_hash"), str)):
        raise SafetyError("PROGRAMME_TURN_CHECKPOINT_INVALID")
    sources = _source_turn_readiness(memory, state, record, handlers, at)
    from .inventory_intake import inventory_work_readiness
    inventory = inventory_work_readiness(state, record, as_of=at)
    ordinary = {cid for scope in _review_scopes(record) for cid in scope["concept_ids"]}
    inventory_ready = [row for row in inventory["ready"]
        if row["binding"]["subject"]["concept_id"] not in ordinary
        or bool((row["binding"].get("source") or {}).get("refresh_due_at"))]
    pause = record.get("model_pause")
    model_wait = bool(pause and timestamp(pause["next_retry_at"]) > timestamp(at))
    discovery = {"callback_ready": False, "backlog_ready": False, "local_ready": False,
                 "projection": {}, "signal": [], "next_retry_at": None}
    if discovery_step is not None:
        try:
            discovery = _discovery_turn_readiness(memory, state, record, handlers, at,
                discovery_step=discovery_step, runner_available=runner is not None and not model_wait)
        except Exception as error:
            # A broken new-direction hint cannot stop existing products,
            # source checks or private drafts. The actual discovery stage will
            # save its own failure/deadline; this hint never authorizes a read.
            # Untrusted shared history/control still cannot authorize any lane.
            if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
                raise
            reason = (str(error) if isinstance(error, SafetyError) else
                      "PROGRAMME_DISCOVERY_LOCAL_FILE_WAIT" if isinstance(error, OSError) else
                      "PROGRAMME_DISCOVERY_READINESS_FAILED")
            discovery["signal"] = [["discovery_wait", reason, type(error).__name__,
                digest(getattr(discovery_step, "discovery_catalogue", None)),
                digest(state.get("catalogue_additions", {}))]]
    waits = list(sources["waits"])
    if inventory["next_retry_at"]:
        waits.append(inventory["next_retry_at"])
    if discovery.get("next_retry_at") and timestamp(discovery["next_retry_at"]) > timestamp(at):
        waits.append(discovery["next_retry_at"])
    if model_wait:
        waits.append(pause["next_retry_at"])
    from .functional_alias_review import functional_alias_readiness
    from .functional_definition_review import functional_definition_readiness
    from .functional_example_review import functional_example_readiness
    from .catalogue_scope_review import catalogue_scope_readiness
    skipped_scopes = {row["target"]: row["binding"] for row in record["deferrals"].values()
        if row["stage"] == "catalogue_scope" and isinstance(row["binding"], str)
        and timestamp(row["next_retry_at"]) > timestamp(at)}
    try:
        scope_hint = catalogue_scope_readiness(memory,
            catalogue=getattr(discovery_step, "discovery_catalogue", None), concept_ids=sorted(ordinary),
            as_of=at, runner_available=runner is not None and not model_wait, skip_bindings=skipped_scopes,
            retry_seconds=retry_seconds, retry_not_before=(pause or {}).get("next_retry_at"), state=state)
    except (SafetyError, OSError) as error:
        if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES: raise
        scope_hint = {"local_ready": False, "model_ready": False, "signal": ["scope_wait", str(error)], "waits": []}
    waits.extend(scope_hint["waits"])
    alias_hints, definition_hints, example_hints = [], [], []
    for scope in _review_scopes(record):
        skipped_definitions = {row["target"]: row["binding"] for row in scope["deferrals"].values()
            if row["stage"] == "functional_definition" and isinstance(row["binding"], str)
            and len(row["binding"]) == 64 and all(c in "0123456789abcdef" for c in row["binding"])
            and timestamp(row["next_retry_at"]) > timestamp(at)}
        definition_hints.append(functional_definition_readiness(memory, scope["concept_ids"], as_of=at,
            skip_concept_bindings=skipped_definitions, retry_seconds=retry_seconds,
            retry_not_before=(pause or {}).get("next_retry_at"), state=state))
        waits.extend(definition_hints[-1].get("waits", []))
        skipped_examples = {row["target"]: row["binding"] for row in scope["deferrals"].values()
            if row["stage"] == "functional_example" and isinstance(row["binding"], str)
            and len(row["binding"]) == 64 and all(c in "0123456789abcdef" for c in row["binding"])
            and timestamp(row["next_retry_at"]) > timestamp(at)}
        example_hints.append(functional_example_readiness(memory,
            [cid for cid in scope["concept_ids"] if cid in definition_hints[-1]["current"]], as_of=at,
            skip_concept_bindings=skipped_examples, retry_seconds=retry_seconds,
            retry_not_before=(pause or {}).get("next_retry_at"), state=state))
        waits.extend(example_hints[-1].get("waits", []))
        skipped = {row["target"]: row["binding"] for row in scope["deferrals"].values()
            if row["stage"] == "functional_alias" and timestamp(row["next_retry_at"]) > timestamp(at)}
        hint = functional_alias_readiness(memory, state, scope["concept_ids"], as_of=at,
            runner_available=runner is not None and not model_wait, skip_group_bindings=skipped,
            retry_seconds=retry_seconds, retry_not_before=(pause or {}).get("next_retry_at"))
        alias_hints.append(hint)
        waits.extend(hint["waits"])
    deadlines = []
    for scope in _review_scopes(record):
        for ref, row in scope["deferrals"].items():
            due = timestamp(row["next_retry_at"]) <= timestamp(at)
            deadlines.append([scope["programme_id"], ref, row["binding"], due])
            if not due:
                waits.append(row["next_retry_at"])
    jobs = _saved_job_signals(memory, state, record, as_of=at, retry_seconds=retry_seconds,
                              retry_not_before=(pause or {}).get("next_retry_at"))
    for signal in jobs.values():
        retry = signal.get("closed_retry") if isinstance(signal, dict) else None
        if retry and retry["next_retry_at"] and timestamp(retry["next_retry_at"]) > timestamp(at):
            waits.append(retry["next_retry_at"])
    capabilities = {"routes": {route: [bool(value.get("handler")), bool(value.get("reconcile"))]
        if isinstance(value, dict) else [callable(value), False] for route, value in sorted(handlers.items())},
        "model": runner is not None, "model_wait": model_wait,
        "discovery": discovery_step is not None, "private_staging": private_staging is not None,
        "custom_stages": sorted(steps or {})}
    memberships = _membership_turn_signal(state, record)
    update_questions = [row for scope in _review_scopes(record)
        for row in _material_update_work(memory, scope, as_of=at, state=state)]
    work_signature = digest([_cooperative_signature(state, record, membership_signal=memberships["signal"]), discovery["projection"]])
    writing = _frozen_writing_ready(memory, state, record, root=root, at=at, detailed=True,
        runner=runner, allow_fresh_legacy_model=False)
    report_ready = bool(writing["ready"] and runner is not None and not model_wait)
    photo_state = _photo_turn_ready(memory, state, record, at, media_root=media_root, detailed=True)
    photo_ready = photo_state["ready"]
    waits.extend(photo_state["waits"])
    wake_hash = digest([work_signature, jobs, sources["signal"], discovery["signal"], inventory["signal"], capabilities, deadlines,
                       sources["callback_ready"], sources["local_ready"], discovery["callback_ready"],
                       discovery["backlog_ready"], discovery["local_ready"], report_ready, writing["local_ready"],
                       photo_ready, photo_state["signal"],
                       [[row["binding"]["concept_id"], digest(row["binding"])] for row in update_questions],
                       [hint["signal"] for hint in alias_hints], [hint["signal"] for hint in definition_hints],
                       [hint["signal"] for hint in example_hints], scope_hint["signal"]])
    progress = record.get("progress") or {}
    scopes = [progress, *progress.get("follow_on_reviews", [])]
    model_pending = any(row.get("current_query_profiles", 0) < row.get("fit_products", 0)
        or row.get("fit_states", {}).get("accepted", 0) < row.get("fit_products", 0) for row in scopes)
    for scope in _review_scopes(record):
        suppliers = scope["last_results"].get("suppliers", {})
        model_pending |= bool(suppliers.get("remaining_inputs") or
            (suppliers.get("preparation_wait") or {}).get("concept_ids"))
    model_pending |= bool(update_questions)
    model_pending |= any(hint["model_ready"] for hint in alias_hints)
    model_pending |= any(hint["model_ready"] for hint in definition_hints)
    model_pending |= any(hint["model_ready"] for hint in example_hints)
    model_pending |= scope_hint["model_ready"]
    from .source_interpretation import _questions
    source_questions = {scope["programme_id"]: _questions(state, run_id=scope["source_run_id"], as_of=at)
                        for scope in _review_scopes(record)}
    model_pending |= any(source_questions.values())
    # Definition and functional market-check questions unlock current-format
    # report stock. When one is due, the turn protects its review share of the
    # existing model allowance (like writing); the total is unchanged.
    functional_checks_due = any(question.get("kind") == "functional_source_scope" and question["unknown_ids"]
        and _due(scope, "match", question["task_id"] + ":" + question["concept_id"], digest(question), at)
        for scope in _review_scopes(record) for question in source_questions[scope["programme_id"]])
    review_ready = bool((any(hint["model_ready"] for hint in definition_hints) or functional_checks_due)
                        and runner is not None and not model_wait)
    model_continuation = _closed_model_allowance_continuation(memory, state, record,
        runner=runner, at=at, retry_seconds=retry_seconds, model_wait=model_wait,
        semantic_hints=[*alias_hints, *example_hints, scope_hint], review_hints=definition_hints,
        report_inputs=writing["model_inputs"], source_questions=source_questions, update_questions=update_questions,
        catalogue=getattr(discovery_step, "discovery_catalogue", None), discovery_step=discovery_step,
        allow_fresh_legacy_model=False)
    ready_source_lanes = {"backlog": bool(sources["callback_ready"] or discovery["backlog_ready"]),
                          "exploration": bool(discovery["callback_ready"])}
    # An unchanged uncertain original is not an unserved new question. Its
    # saved/due signal can wake recovery, but merely spending a reconciliation
    # callback must not mint endless turns for that same unanswered request.
    continuation_lanes = {
        "backlog": bool(sources["fresh_callback_ready"] or discovery["backlog_ready"]),
        "exploration": bool(discovery.get("fresh_callback_ready", discovery["callback_ready"])),
    }
    source_continuation = _closed_source_allowance_continuation(state, record, ready_lanes=continuation_lanes)
    local_ready = (sources["local_ready"] or discovery["local_ready"] or writing["local_ready"]
                   or bool(memberships["missing_concept_ids"])
                   or any(hint["local_ready"] for hint in alias_hints)
                   or any(hint["local_ready"] for hint in definition_hints)
                   or any(hint["local_ready"] for hint in example_hints) or scope_hint["local_ready"])
    available = (sources["callback_ready"] or discovery["callback_ready"] or discovery["backlog_ready"] or local_ready or report_ready
                 or bool(private_staging is not None and _private_pending(record))
                 # These stages also contain local catalogue decisions and
                 # exact saved-answer application. A paused/absent runner
                 # does not suppress them; unchanged probes settle once.
                 or photo_ready or model_pending or bool(steps) or bool(inventory_ready)
                 or bool(record.get("inventory_refresh", {}).get("backlog_remaining"))
                 or bool(not model_wait and record.get("status") != "research_pool_finished"))
    opened = any(row.get("kind") == "work-turn-v1" and row.get("programme_id") == record["programme_id"]
                 and row.get("state") == "open" for row in state["batch_runs"].values())
    changed = checkpoint is not None and checkpoint["wake_hash"] != wake_hash
    closed_allowance_ready = bool(model_continuation and checkpoint
        and checkpoint.get("turn_sequence") == model_continuation["turn_sequence"])
    closed_source_ready = bool(source_continuation and checkpoint
        and checkpoint.get("turn_sequence") == source_continuation["turn_sequence"])
    ready = (opened or checkpoint is None or changed or bool(checkpoint["continue_ready"] and available)
             or closed_allowance_ready or closed_source_ready or writing["local_ready"])
    reason = ("resume_original_turn" if opened else "inspect_current_saved_work" if checkpoint is None else
              "saved_input_reply_configuration_or_deadline_changed" if changed else
              "closed_model_allowance_has_ready_work" if closed_allowance_ready else
              "closed_source_allowance_has_ready_work" if closed_source_ready else
              "newer_checked_report_hold_needs_local_recovery" if writing["local_ready"] else
              "independent_ready_work" if ready else "all_remaining_work_is_waiting")
    return {"ready": ready, "reason": reason, "wake_hash": wake_hash, "work_signature": work_signature,
        "source_recovery_pending": sources["recovery_pending"],
        "source_allowance_continuation": source_continuation,
        "model_allowance_continuation": model_continuation,
        "available": bool(available), "ready_lanes": ready_source_lanes, "report_ready": report_ready,
        "review_ready": review_ready, "report_local_recovery_ready": writing["local_ready"],
        "next_retry_at": min(waits, key=timestamp) if waits else None,
        "next_trigger": "saved_retry_time" if waits else "saved_input_reply_or_connection_change"}


def programme_turn_readiness(memory, record, *, root, handlers=None, runner=None,
                             discovery_step=None, steps=None, private_staging=None,
                             media_root=None, as_of=None, state=None, retry_seconds=300):
    """One non-mutating readiness decision over one verified research state."""
    at = as_of or utc_now(); timestamp(at)
    if type(retry_seconds) is not int or retry_seconds < 1:
        raise SafetyError("PROGRAMME_SLICE_INVALID")
    state = memory.snapshot() if state is None else state
    from .source_acquisitions import source_readiness_owner_scope
    with source_readiness_owner_scope(state):
        return _programme_turn_readiness(memory, record, root=root, handlers=handlers, runner=runner,
            discovery_step=discovery_step, steps=steps, private_staging=private_staging,
            media_root=media_root, as_of=at, state=state, retry_seconds=retry_seconds)


def advance_ready_programme_turn(memory, *, root, programme_id=None, client_id="default", location=None,
                                 as_of=None, clock=utc_now, turn_policy=None, **options):
    """Opt-in existing-writer select → one saved round → close; never a driver."""
    if "max_cycles" in options:
        raise SafetyError("PROGRAMME_TURN_IS_ONE_ROUND")
    if options.get("handlers") is None:
        from .source_coverage import installed_source_handlers
        # Prepare the same installed callbacks for both advisory readiness
        # and the later admitted round. Factory construction makes no reads
        # from product sources and cannot grant execution authority.
        options["handlers"] = installed_source_handlers(memory,
            marketplace_options={"shared_acquisitions": True},
            meta_options={"shared_acquisitions": True},
            trends_options={"shared_acquisitions": True},
            supplier_options={"respect_access_waits": True, "shared_acquisitions": True},
            quickcommerce_options={"respect_access_waits": True, "shared_acquisitions": True})
    at = as_of or clock(); timestamp(at)
    with Journal(safe_path(memory.root, "programme_writer")).locked(), operation_writer(), \
            memory.defer_lookup_projection(flush_if=lambda: programme_service_state(memory)["enabled"]):
        if not programme_service_state(memory)["enabled"]:
            return {"state": "owner_paused", "round_completed": False, "client_publication_attempted": False}
        selection_state = _warm_research_history(memory)
        decisions = {}
        def decision(record):
            started = time.monotonic()
            record_event("phase_started", stage="preparation", phase="readiness", programme_id=record["programme_id"])
            try:
                value = programme_turn_readiness(memory, record, root=root, as_of=at,
                    retry_seconds=options.get("retry_seconds", 300),
                    **{name: options.get(name) for name in ("handlers", "runner", "discovery_step", "steps", "private_staging", "media_root")})
            finally:
                record_event("phase_finished", stage="preparation", phase="readiness",
                             programme_id=record["programme_id"], timings={"readiness": time.monotonic() - started})
            decisions[record["programme_id"]] = value
            return value
        selection_started = time.monotonic()
        record_event("phase_started", stage="preparation", phase="programme_selection",
                     deadline_seconds=None, useful_result=False)
        try:
            record = (load_programme(memory, programme_id, state=selection_state) if programme_id is not None else
                      _due_programme_locked(memory, as_of=at, client_id=client_id, location=location,
                                            cooperative=True, readiness=decision, state=selection_state,
                                            inventory_refresh=lambda row: _refresh_inventory_boundary(memory, row, as_of=at)))
            if programme_id is not None:
                record = _refresh_inventory_boundary(memory, record, as_of=at)
        finally:
            record_event("phase_finished", stage="preparation", phase="programme_selection",
                         timings={"programme_selection": time.monotonic() - selection_started},
                         deadline_seconds=None, useful_result=False)
        before = decisions.get(record["programme_id"]) or decision(record)
        if not before["ready"]:
            record_event("programme_waiting", programme_id=record["programme_id"], state="waiting",
                         reason=before["reason"], next_action="wait_for_saved_trigger")
            return {"programme_id": record["programme_id"], "state": "all_waiting", "round_completed": False,
                "reason": before["reason"], "next_retry_at": before["next_retry_at"],
                "next_trigger": before["next_trigger"], "client_publication_attempted": False}
        result = _advance_programme_turn_locked(memory, record["programme_id"], root=root,
            turn_policy=turn_policy, ready_lanes=before["ready_lanes"], report_ready=before["report_ready"],
            review_ready=before["review_ready"], as_of=as_of, clock=clock, **options)
        latest = load_programme(memory, record["programme_id"])
        after = decision(latest)
        changed = before["work_signature"] != after["work_signature"]
        continue_ready = (changed or after["source_recovery_pending"] or after.get("source_allowance_continuation")
                          or after.get("model_allowance_continuation")) and after["available"]
        latest["cooperative_checkpoint"] = {"version": 1, "wake_hash": after["wake_hash"],
            "work_signature": after["work_signature"], "continue_ready": bool(continue_ready),
            "turn_sequence": result["work_turn"]["turn_sequence"], "next_retry_at": after["next_retry_at"],
            "next_trigger": after["next_trigger"]}
        checkpoint_save = _save(memory, latest)
        next_action = ("owner_paused" if result["tick_state"] == "owner_paused" else
                       "continue_ready_work" if continue_ready else "wait_for_saved_trigger")
        record_event("round_finished", programme_id=record["programme_id"], turn_id=result["work_turn"]["id"],
                     state="finished", next_action=next_action)
        return {**result, "round_completed": True, "meaningful_progress": changed,
            "next_action": next_action,
            "continuation_checkpoint": checkpoint_save, "next_retry_at": after["next_retry_at"],
            "next_trigger": after["next_trigger"]}


def _unused_reporting_inventory(memory, state, records, *, client_id, as_of):
    """Reuse only eligible unused earlier inventory, not historical research jobs."""
    from .fit_policy import FIT_POLICY_ID
    from .issue_allocation import _held, _published
    from .report_selection import build_report_pool
    try:
        for row in state["published_history"].values():
            if (not isinstance(row, dict) or not isinstance(row.get("status"), str)
                    or not isinstance(row.get("concept_id"), str) or not isinstance(row.get("issue_id"), str)):
                raise SafetyError("PROGRAMME_REPORTING_HISTORY_UNTRUSTED")
            identifier(row["concept_id"]); identifier(row["issue_id"])
        for row in state["issue_reservations"].values():
            if (not isinstance(row, dict) or not isinstance(row.get("state"), str)
                    or not isinstance(row.get("issue_id"), str)
                    or not isinstance(row.get("products"), list)
                    or any(not isinstance(product, dict) or not isinstance(product.get("concept_id"), str)
                           for product in row["products"])):
                raise SafetyError("PROGRAMME_REPORTING_HISTORY_UNTRUSTED")
            identifier(row["issue_id"])
            for product in row["products"]:
                identifier(product["concept_id"])
        previous = {cid for record in records for cid in _reporting_ids(record, state)}
        from .functional_aliases import alias_projection
        identities = alias_projection(state)
        unavailable = _held(state["issue_reservations"], client_id=client_id, aliases=identities,
            history=state["published_history"]) | _published(state["published_history"], client_id, aliases=identities)
        available = previous - unavailable
        if not available:
            return [], None
        # Reuse this already verified immutable selection state rather than
        # replay the mounted event store again for each retained old product.
        pool = _programme_report_pool(memory, {"client_id": client_id}, as_of=as_of,
                                      state=state, functional_packets=True)
        return sorted({row["concept_id"] for row in pool["eligible"] if row["concept_id"] in available
                       and state["concepts"].get(row["concept_id"], {}).get("status") == "active"}), None
    except (SafetyError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, SafetyError) and str(error) in INTEGRITY_FAILURES:
            raise
        return [], "Earlier unused report inventory could not be verified; new research can continue."


def _due_programme_locked(memory, *, as_of=None, client_id="default", location=None,
                          cooperative=False, readiness=None, inventory_refresh=None, state=None):
    """Fair backlog/fresh-week rotation, never cloning an old product pool.

    Legacy selection waits for an old saved fixed point. The explicit
    cooperative selection admits the current week after a closed round and
    fairly serves actual ready old/new work. Its research pool is empty;
    genuine discovery admits explicit follow-on products, while eligible
    unused earlier inventory may be separately carried for reporting.
    Original IDs, receipts, fees and recovery times remain unchanged.
    """
    if (type(cooperative) is not bool or readiness is not None and not callable(readiness)
            or inventory_refresh is not None and not callable(inventory_refresh)):
        raise SafetyError("PROGRAMME_TURN_READINESS_INVALID")
    at = as_of or utc_now(); timestamp(at); identifier(client_id)
    if not programme_service_state(memory)["enabled"]:
        raise SafetyError("PROGRAMME_OWNER_PAUSED")
    # A caller may supply its just-verified point-in-time read for this pure
    # choice. New preparation/intake still refreshes below; readiness and real
    # turn/dispatch admission retain their own fresh checks. Default callers
    # obtain a fresh read, never an unverified persistent cache.
    selection_state = memory.snapshot() if state is None else state
    records = [row for row in selection_state["batch_runs"].values()
               if row.get("kind") == VERSION and row.get("client_id") == client_id]
    # Include a checkpoint whose first canonical commit was locally busy.
    for path in safe_path(memory.root, "programme_runs").glob("programme-*/pending-control.json"):
        staged = load_json(path, 8 * 1024 * 1024)
        if not isinstance(staged, dict) or not isinstance(staged.get("record"), dict):
            raise SafetyError("PROGRAMME_CONTROL_INVALID")
        row = staged["record"]
        if row.get("client_id") == client_id:
            verified = load_programme(memory, row.get("programme_id"), state=selection_state)
            records = [old for old in records if old["programme_id"] != verified["programme_id"]] + [verified]
    week = research_week(at)
    existing = [row for row in records if row["week"] == week]
    unfinished = [row for row in records if row.get("status") != "research_pool_finished" or _private_pending(row)]
    if cooperative:
        from .work_turns import TURN_KIND
        opened = [row for row in selection_state["batch_runs"].values()
                  if row.get("kind") == TURN_KIND and row.get("state") == "open"
                  and any(record["programme_id"] == row["programme_id"] for record in records)]
        if opened:
            # An interrupted round retains its original allowance before the
            # next safe boundary. Unknown acquisitions themselves still remain
            # original-only inside their owning source/model controllers.
            return load_programme(memory, min(opened, key=lambda row: row["turn_sequence"])["programme_id"],
                                  state=selection_state)
        if not existing:
            programme_id = "programme-" + client_id + "-" + week
            if location is None and records:
                location = deepcopy(max(records, key=lambda row: row["created_at"]).get("location"))
            inventory, inventory_reason = _unused_reporting_inventory(
                memory, selection_state, records, client_id=client_id, as_of=at)
            current = _prepare_programme_locked(memory, programme_id, concept_ids=[] if records else None,
                as_of=at, week=week, client_id=client_id, location=location,
                reporting_inventory_concept_ids=inventory, reporting_inventory_reason=inventory_reason)
            records.append(current); existing = [current]
            selection_state = memory.snapshot()
        if inventory_refresh is not None:
            # One actual current-week consumer receives rolling intake. Old
            # frozen scopes/release slots are not cloned or rewritten.
            current = min(existing, key=lambda row: (timestamp(row["created_at"]), row["programme_id"]))
            current = inventory_refresh(load_programme(memory, current["programme_id"], state=selection_state))
            records = [current if row["programme_id"] == current["programme_id"] else row for row in records]
            existing = [current if row["programme_id"] == current["programme_id"] else row for row in existing]
            selection_state = memory.snapshot()
        served = {}
        for turn in selection_state["batch_runs"].values():
            if turn.get("kind") == TURN_KIND and turn.get("state") == "closed":
                served[turn["programme_id"]] = max(served.get(turn["programme_id"], 0), turn["turn_sequence"])
        def fair(row):
            return (served.get(row["programme_id"], 0), timestamp(row["created_at"]), row["programme_id"])
        decisions = {row["programme_id"]: readiness(row) for row in records} if readiness is not None else {}
        ready = [row for row in records if decisions.get(row["programme_id"], {}).get("ready") is True]
        if ready:
            return load_programme(memory, min(ready, key=fair)["programme_id"], state=selection_state)
        waiting = [row for row in records if decisions.get(row["programme_id"], {}).get("next_retry_at")]
        if waiting:
            return load_programme(memory, min(waiting, key=lambda row: (
                timestamp(decisions[row["programme_id"]]["next_retry_at"]), fair(row)))["programme_id"], state=selection_state)
        return load_programme(memory, min(existing, key=fair)["programme_id"], state=selection_state)
    def rotation(row):
        return (timestamp(row.get("last_served_at") or row["created_at"]), row["week"], row["programme_id"])
    def source_in_flight(row):
        run_ids = {scope["source_run_id"] for scope in _review_scopes(row)}
        return any(task.get("run_id") in run_ids and task.get("status") == "dispatching"
                   for task in selection_state["query_tasks"].values())
    moving = [row for row in unfinished if source_in_flight(row) or row.get("status") != "research_pool_finished"
              and (row.get("status") == "running" or row.get("tick_state") != "fixed_point_with_saved_remaining_work"
                   or not row.get("progress_signature") or not row.get("last_served_at"))]
    if not existing and moving:
        # A prepared, progressing or interrupted run gets resumed before a
        # different week can be admitted. The outer guard also refuses any
        # attempt to create/execute while another writer is actually alive.
        return load_programme(memory, min(moving, key=rotation)["programme_id"], state=selection_state)
    if not existing:
        programme_id = "programme-" + client_id + "-" + week
        if location is None and records:
            location = deepcopy(max(records, key=lambda row: row["created_at"]).get("location"))
        inventory, inventory_reason = _unused_reporting_inventory(memory, selection_state, records,
                                                                  client_id=client_id, as_of=at)
        return _prepare_programme_locked(memory, programme_id, concept_ids=[] if records else None,
                                 as_of=at, week=week, client_id=client_id, location=location,
                                 reporting_inventory_concept_ids=inventory,
                                 reporting_inventory_reason=inventory_reason)
    ready = [row for row in unfinished if not row.get("next_retry_at") or timestamp(row["next_retry_at"]) <= timestamp(at)]
    if ready:
        return load_programme(memory, min(ready, key=rotation)["programme_id"], state=selection_state)
    if unfinished:
        chosen = min(unfinished, key=lambda row: (timestamp(row["next_retry_at"]), rotation(row)))
        return load_programme(memory, chosen["programme_id"], state=selection_state)
    if existing:
        # A manually named accepted current run is still this week's work.
        # Finishing its pool cannot accidentally create a second identity and
        # repeat every physical query before the next due research week.
        return load_programme(memory, min(existing, key=lambda row: row["created_at"])["programme_id"], state=selection_state)
    raise SafetyError("PROGRAMME_SELECTION_INVALID")


@wraps(_due_programme_locked)
def due_programme(memory, **options):
    """Select/recheck a real due week under the existing programme writer."""
    memory.snapshot()
    with Journal(safe_path(memory.root, "programme_writer")).locked():
        return _due_programme_locked(memory, **options)


def _meter_baseline_path(root):
    return safe_path(runtime_path(root, "data/operations")) / "meter-baseline.json"


def _meter_financial(journal):
    """One short ledger read; waits briefly for a research transaction."""
    if journal._locked:
        return journal.state()
    with journal.locked(timeout=5):
        return journal.state()


def _meter_inputs(memory, *, as_of, failures, state=None):
    from .fit_policy import FIT_POLICY_ID
    from .report_examples import RECOMMENDATION_CONTRACT
    from .report_selection import build_report_pool
    state = memory.snapshot() if state is None else state
    source = SimpleNamespace(snapshot=lambda: state, evidence_type=memory.evidence_type)
    pool = _summary_input(lambda: build_report_pool(source, as_of=as_of, fit_policy_id=FIT_POLICY_ID,
                          recommendation_contract=RECOMMENDATION_CONTRACT), failures, "report_pool")
    programmes = [row for row in state["batch_runs"].values() if row.get("kind") == VERSION]
    return state, pool, programmes, RECOMMENDATION_CONTRACT


def load_meter_baseline(root):
    from .programme_summary import validate_meter_baseline
    wrapper = load_json(_meter_baseline_path(root), 4 * 1024 * 1024)
    if not isinstance(wrapper, dict) or set(wrapper) != {"baseline", "hash"} or digest(wrapper["baseline"]) != wrapper["hash"]:
        raise SafetyError("METER_BASELINE_INVALID")
    return validate_meter_baseline(wrapper["baseline"])


def save_meter_baseline(memory, *, root, reason, as_of=None):
    """Start the owner meter once, at an explicit fresh start.

    Carried financial attempts keep guarding spending but are excluded from
    the meter's "since start" money. Counts already saved are subtracted.
    Nothing is reset: this writes one small new file under the programme writer.
    """
    from .programme_summary import METER_BASELINE_VERSION, meter_counts, validate_meter_baseline
    from .research_costs import collection_cost_totals
    at = as_of or utc_now(); timestamp(at)
    with Journal(safe_path(memory.root, "programme_writer")).locked():
        path = _meter_baseline_path(root)
        if path.exists() or path.is_symlink():
            raise SafetyError("METER_BASELINE_ALREADY_SAVED")
        failures = {}
        state, pool, programmes, contract = _meter_inputs(memory, as_of=at, failures=failures)
        if failures:
            raise SafetyError("METER_BASELINE_INPUT_UNREADABLE")
        counts = meter_counts(state, pool, programmes, runtime_root(root), contract)
        financial_state = _meter_financial(memory.journal)
        totals = collection_cost_totals(financial_state)
        value = validate_meter_baseline({"schema_version": 1, "kind": METER_BASELINE_VERSION, "started_at": at,
            "reason": reason, "carried_attempt_ids": sorted(financial_state[1]),
            "carried_known_usd": totals["known_actual_usd"], "carried_unsettled_hold_usd": totals["unsettled_hold_usd"],
            "counts": {name: counts[name] for name in counts if name != "unreadable_type_records"}})
        atomic_json(path, {"baseline": value, "hash": digest(value)})
        return value


def programme_meter(memory, *, root, as_of=None):
    """Owner meter since the saved fresh start, from local saved records only.

    No provider, model, website, research or financial write. The research
    snapshot is a verified read; the ledger read waits at most five seconds.
    """
    from .operations import operation_log_summary, read_operation_status
    from .programme_summary import owner_meter
    at = as_of or utc_now(); timestamp(at)
    failures = {}
    baseline = _summary_input(lambda: load_meter_baseline(root), failures, "baseline")
    state, pool, programmes, contract = _meter_inputs(memory, as_of=at, failures=failures)
    financial_state = _summary_input(lambda: _meter_financial(memory.journal), failures, "financial_state")
    run_log = (_summary_input(lambda: operation_log_summary(root, since=baseline["started_at"]), failures, "run_log")
               if baseline is not None else None)
    result = owner_meter(baseline=baseline, state=state, financial_state=financial_state, report_pool=pool,
        programmes=programmes, activity=read_operation_status(root, as_of=at), run_log=run_log, as_of=at,
        project_root=runtime_root(root), recommendation_contract=contract)
    from .spend_watch import allowance_summary
    result["allowances"] = _summary_input(lambda: allowance_summary(root, as_of=at), failures, "allowances")
    result["optional_input_failures"] = failures
    return result
