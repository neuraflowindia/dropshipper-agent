"""Finite connected plan execution using the existing financial controller."""

from copy import deepcopy
from contextlib import nullcontext
from decimal import Decimal

from .models import SafetyError, digest, identifier, timestamp, utc_now
from .query_dispatch import find_existing_task
from .research_flow import candidate_summary, problem, run_flow
from .research_memory import import_saved_attempt, key
from .research_operations import (bind_prepared_action, sync_query_feedback,
                                  validation_targets, validation_target_result)
from .storage import Journal


def prepare_continuation(memory, plan_id, *, reason, as_of=None):
    """Carry forward only blocked/deferred preparations never submitted.

    This is a new finite continuation, not a reset/retry of the original batch.
    Paid, uncertain, reserved and completed attempts are never relaunched here.
    Existing exact task bindings preserve their original baseline and request.
    """
    identifier(plan_id)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise SafetyError("BATCH_CONTINUATION_REASON_REQUIRED")
    as_of = as_of or utc_now()
    timestamp(as_of)
    with Journal(memory.root / "batch_dispatch").locked():
        state = memory.snapshot()
        plan = state["batch_plans"].get(plan_id)
        run = state["batch_runs"].get(key("batch-run", plan_id))
        if not plan or not run or run["plan_hash"] != digest(plan):
            raise SafetyError("SAVED_EXECUTED_PLAN_REQUIRED")
        with memory.journal.locked():
            attempts = memory.journal.state()[1]
        actions = [deepcopy(action) for action in plan["actions"]
                   if (entry := run["actions"].get(action["action_id"]))
                   and entry["status"] in ("blocked", "deferred")
                   and entry.get("attempt_id") not in attempts]
        if not actions:
            return None
        continuation = deepcopy(plan)
        continuation.pop("id")
        continuation.update(actions=actions, as_of=as_of, continuation_of=plan_id,
                            parent_plan_hash=digest(plan), continuation_reason=reason.strip())
        unknown = any(a["cost_estimate_usd"] is None for a in actions)
        continuation["budget"]["discovery_estimate_usd"] = None if unknown else str(sum(
            (Decimal(a["cost_estimate_usd"]) for a in actions), Decimal(0)))
        continuation["budget"]["has_unknown_cost"] = unknown
        continuation["budget"]["note"] = "Unsubmitted work from the original envelope; not a new funding grant. The journal controls each first launch."
        continuation_id = key("plan", continuation)
        continuation["id"] = continuation_id
        memory.put("batch_plans", continuation_id, continuation)
        return continuation


def _save_run(memory, run_id, record):
    old = memory.snapshot()["batch_runs"].get(run_id)
    if old != record:
        # A recovery can revisit the same attempted/blocked states repeatedly.
        # A monotonic revision prevents a repeated transition from reusing an
        # earlier event ID and silently leaving the saved index behind.
        record["revision"] = (old or {}).get("revision", 0) + 1
        memory.put("batch_runs", run_id, deepcopy(record), previous=old,
                   event_id=key("batch-progress", [run_id, record["revision"], digest(record)]))


def _execution_time(plan):
    # The plan's choices remain frozen; collection and freshness are judged at
    # execution time, not before a newly completed capture could exist.
    return max(timestamp(plan["as_of"]), timestamp(utc_now())).isoformat(timespec="seconds")


def _attempt(memory, attempt_id):
    with memory.journal.locked():
        return memory.journal.state()[1].get(attempt_id)


def _unresolved_paid(memory):
    with memory.journal.locked():
        return any(a["submission"] in ("uncertain", "submitted") and
                   (a["remote_status"] is None or a["charge"] is None) for a in memory.journal.state()[1].values())


def _import_if_captured(memory, attempt_id):
    attempt = _attempt(memory, attempt_id)
    if not attempt or not attempt.get("capture"):
        return None
    imported = import_saved_attempt(memory, attempt_id)
    capture = memory.snapshot()["captures"][imported["capture_id"]]
    return {"acquisition_id": capture["acquisition_id"], "capture_id": imported["capture_id"],
            "new_observations": imported["new_observations"], "source_state": capture["mapping_status"],
            "observed_at": capture["observed_at"]}


def _verify_preparation(action, prepared, existing=None):
    from urllib.parse import parse_qs, urlsplit
    if action["source"] != "meta" or action["kind"] not in ("collect", "reuse"):
        raise SafetyError("BATCH_SUPPORTED_META_ACTION_REQUIRED")
    request = action["request"]
    try:
        query = parse_qs(urlsplit(prepared.input["urls"][0]["url"]).query)
        if (prepared.build != request["build"] or prepared.purpose != request["purpose"]
                or query.get("q") != [request["term"]] or query.get("country") != [request["country"]]
                or prepared.input["limitPerSource"] != request["coverage"]["limit"]):
            raise SafetyError("PLANNED_REQUEST_MISMATCH")
    except (KeyError, TypeError, IndexError, ValueError):
        raise SafetyError("PLANNED_REQUEST_MISMATCH") from None
    if existing and (prepared.attempt_id != existing["attempt_id"] or prepared.config_hash != existing["config_hash"]):
        raise SafetyError("BATCH_EXISTING_PREPARATION_MISMATCH")


def execute_plan(memory, plan_id, *, prepare_action, controller_factory, runner=None, item_limit=10, action_limit=8):
    """Execute once, then reuse/recover saved work when this plan is replayed.

    prepare_action(plan, action) receives an existing_task field when an older
    equivalent binding exists. It must return that binding's exact preparation.
    controller_factory() returns the ordinary existing MetaTrial instance.
    No money lock is held during model interpretation. The separate batch lock
    prevents two dispatchers launching the same unreserved preparation.
    """
    identifier(plan_id)
    if type(action_limit) is not int or not 1 <= action_limit <= 8 or type(item_limit) is not int or not 1 <= item_limit <= 50:
        raise SafetyError("BATCH_LIMIT_INVALID")
    with Journal(memory.root / "batch_dispatch").locked():
        return _execute_plan(memory, plan_id, prepare_action, controller_factory, runner, item_limit, action_limit)


def _execute_plan(memory, plan_id, prepare_action, controller_factory, runner, item_limit, action_limit):
    state = memory.snapshot()
    plan = state["batch_plans"].get(plan_id)
    if not plan or not isinstance(plan.get("actions"), list):
        raise SafetyError("SAVED_BATCH_PLAN_REQUIRED")
    timestamp(plan["as_of"])
    run_id = key("batch-run", plan_id)
    saved = state["batch_runs"].get(run_id)
    replay = saved is not None
    if saved and saved["plan_hash"] != digest(plan):
        raise SafetyError("BATCH_PLAN_CHANGED")
    selected = plan["actions"][:action_limit] if saved is None else [
        action for action in plan["actions"] if action["action_id"] in saved["selected_actions"]]
    record = deepcopy(saved) if saved else {
        "plan_id": plan_id, "plan_hash": digest(plan), "started_at": utc_now(),
        "selected_actions": [action["action_id"] for action in selected], "actions": {}}
    _save_run(memory, run_id, record)
    execution_as_of = _execution_time(plan)
    acquisitions = set()
    controller = None
    pool_enabled = False
    stopped_paid = _unresolved_paid(memory)
    for action in selected:
        action_id = identifier(action["action_id"])
        previous = record["actions"].get(action_id)
        entry = deepcopy(previous) if previous else {"action_id": action_id, "family_id": action["family_id"],
                                                  "status": "pending", "reason": "Action is not dispatched."}
        if action.get("target"):
            entry["target"] = deepcopy(action["target"])
        attempt_id = entry.get("attempt_id")
        try:
            existing = find_existing_task(memory, action, as_of=execution_as_of)
            prior_attempt = _attempt(memory, attempt_id) if attempt_id else None
            recoverable = bool(prior_attempt and prior_attempt["submission"] in ("uncertain", "submitted")
                               and (prior_attempt["remote_status"] is None or prior_attempt["charge"] is None
                                    or prior_attempt.get("capture_status") == "not_captured"))
            if previous and previous["status"] in ("blocked", "deferred") and not recoverable:
                # Failed optional sources stay recorded, not automatic paid
                # retries. Any committed capture can still be imported below.
                imported = _import_if_captured(memory, attempt_id) if attempt_id else None
                if imported:
                    acquisitions.add(imported["acquisition_id"])
                    entry.update(status="captured", capture=imported,
                                 reason="Recovered a committed saved capture; no new collection.")
                record["actions"][action_id] = entry
                _save_run(memory, run_id, record)
                continue
            if previous and previous["status"] in ("captured", "reused"):
                imported = _import_if_captured(memory, attempt_id)
                if imported:
                    acquisitions.add(imported["acquisition_id"])
                    entry.update(capture=imported, reason="Replayed saved capture; no new collection.")
                else:
                    entry.update(status="blocked", reason="Recorded capture is unavailable; inspect saved evidence without reposting.")
            elif existing and existing["reuse_kind"] == "fresh_capture":
                attempt_id = existing["attempt_id"]
                entry.update(attempt_id=attempt_id, task_id=existing["task_id"], status="reused", reason=existing["reuse_reason"])
                imported = _import_if_captured(memory, attempt_id)
                if not imported:
                    raise SafetyError("BATCH_CAPTURE_REUSE_UNAVAILABLE")
                entry["capture"] = imported
                acquisitions.add(imported["acquisition_id"])
            else:
                attempt_id = existing["attempt_id"] if existing else attempt_id
                attempt = _attempt(memory, attempt_id) if attempt_id else None
                if controller is None:
                    controller = controller_factory()
                    pool_enabled = getattr(controller, "pool_enabled", False) is True
                    if pool_enabled:
                        stopped_paid = False
                if stopped_paid and attempt is None:
                    entry.update(status="deferred", reason="An earlier provider submission or charge is unresolved; saved analysis continues.")
                elif replay and attempt is None:
                    entry.update(status="deferred", reason="Replay does not start another collection; retain this unstarted action for a later plan.")
                else:
                    if action.get("target") and attempt is None:
                        targets, _ = validation_targets(memory, as_of=execution_as_of,
                                                        client_id=plan.get("client_id", "default"))
                        current = targets.get(action["target"]["concept_id"])
                        if current is None or current["question_fingerprint"] != action["target"]["question_fingerprint"]:
                            entry.update(status="deferred", reason="The target facts changed or this Meta check is no longer needed; no new submission.")
                            record["actions"][action_id] = deepcopy(entry)
                            _save_run(memory, run_id, record)
                            continue
                    supplied_action = dict(action, existing_task=existing) if existing else action
                    prepared = prepare_action(plan, supplied_action)
                    _verify_preparation(action, prepared, existing)
                    attempt_id = prepared.attempt_id
                    if existing:
                        task_id = existing["task_id"]
                    else:
                        # Existing controller task binding is immutable and
                        # saved before preflight or any possible charge.
                        binding = bind_prepared_action(memory, plan_id, action_id, prepared)
                        task_id = binding["id"]
                    entry.update(attempt_id=attempt_id, task_id=task_id, status="attempted",
                                 reason="Exact preparation bound before controller execution.")
                    record["actions"][action_id] = deepcopy(entry)
                    _save_run(memory, run_id, record)
                    imported = _import_if_captured(memory, attempt_id)
                    if not imported:
                        with nullcontext() if getattr(controller, "short_locks", False) else memory.journal.locked():
                            if attempt is None:
                                proof = controller.preflight(attempt_id)
                                if proof.get("ready_for_live") is not True:
                                    raise SafetyError("BATCH_PREFLIGHT_NOT_READY")
                            result = controller.run(attempt_id, resume_only=attempt is not None)
                        imported = _import_if_captured(memory, attempt_id)
                        if not imported:
                            entry.update(status="blocked", reason=result.get("code", "Provider output is not available."),
                                         controller_result=result)
                    if imported:
                        acquisitions.add(imported["acquisition_id"])
                        entry.update(status="captured", capture=imported,
                                     reason="Committed saved output imported; source quality and interpretation remain separate.")
            stopped_paid = stopped_paid or (not pool_enabled and _unresolved_paid(memory))
        except (SafetyError, OSError) as error:
            reason = str(error) if isinstance(error, SafetyError) else "BATCH_LOCAL_OPERATION_FAILED"
            entry.update(status="blocked", reason=reason)
            if attempt_id:
                entry["attempt_id"] = attempt_id
            problem(memory, "batch_dispatch", action_id, reason, revision=digest(action))
            stopped_paid = stopped_paid or (not pool_enabled and _unresolved_paid(memory))
        record["actions"][action_id] = deepcopy(entry)
        _save_run(memory, run_id, record)
    analysis_as_of = _execution_time(plan)
    summary = candidate_summary(memory, as_of=analysis_as_of)
    analysis_reason = None
    if acquisitions:
        try:
            from .service import load_policy
            summary = run_flow(memory, runner, limit=item_limit, as_of=analysis_as_of, acquisition_ids=sorted(acquisitions),
                               fit_policy_id=load_policy().get("fit_policy_id", "fit-13-v1"))
        except SafetyError as error:
            analysis_reason = str(error)
            problem(memory, "batch_analysis", plan_id, analysis_reason, revision=digest(sorted(acquisitions)))
    try:
        outcomes = sync_query_feedback(memory, as_of=analysis_as_of)
    except SafetyError as error:
        outcomes = []
        analysis_reason = analysis_reason or str(error)
        problem(memory, "batch_feedback", plan_id, str(error))
    if any(action.get("target") for action in selected):
        targets, _ = validation_targets(memory, as_of=analysis_as_of,
                                        client_id=plan.get("client_id", "default"))
        state = memory.snapshot()
        for action in selected:
            if not action.get("target"):
                continue
            entry = record["actions"][action["action_id"]]
            result = validation_target_result(action, summary, state=state)
            current = targets.get(action["target"]["concept_id"])
            if current:
                result["question_fingerprint"] = current["question_fingerprint"]
            # The shared acquisition's interpretation state applies to every
            # action that uses it, but another task's target coverage does not.
            # Above, the result is measured against this exact saved action's
            # concept, description and question; never overwrite that judgment
            # with a different target merely because acquisition IDs match.
            matching = [state["query_outcomes"][i] for i in outcomes
                        if state["query_outcomes"][i].get("acquisition_id") == entry.get("capture", {}).get("acquisition_id")]
            if result["status"] not in ("target_changed", "target_unavailable"):
                if any(outcome["status"] == "failed" for outcome in matching):
                    result.update(status="source_unavailable", useful_progress=None)
                elif any(outcome.get("pending_count", 0) > 0 or outcome.get("reviewed") is not True
                         for outcome in matching):
                    result.update(status="interpretation_pending", useful_progress=None)
            if entry["status"] == "blocked":
                result.update(status="source_unavailable", useful_progress=None)
            elif entry["status"] == "deferred":
                result.update(status="target_changed_or_deferred", useful_progress=None)
            entry["target_result"] = result
        _save_run(memory, run_id, record)
    return {"id": run_id, "plan_id": plan_id, "replay": replay, "as_of": analysis_as_of,
            "actions": list(record["actions"].values()),
            "paid_dispatch_stopped": stopped_paid, "analysis_reason": analysis_reason,
            "feedback_ids": outcomes, "summary": summary, "acquisition_ids": sorted(acquisitions),
            "pending_actions": sum(a["status"] in ("pending", "attempted", "deferred") for a in record["actions"].values()),
            "blocked_actions": sum(a["status"] == "blocked" for a in record["actions"].values()),
            "unselected_actions": max(0, len(plan["actions"]) - len(record["selected_actions"])),
            "client_report": False, "client_ready": False}
