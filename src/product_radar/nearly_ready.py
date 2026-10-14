"""Finish nearly report-ready types before new discovery (Plan R10 W9, P3).

A type that passes the ten-criterion checklist (at least five supported Meets
under the current functional contract) is close to a report. While the coming
report sets are short of eligible types, its remaining market review, crowding
check and supplier work is served before new discovery, but only in a stage
that really holds pending work for it. This is ordering only: each stage keeps
its exact jobs, waits, deferrals and turn allowances. The ten-call AI allowance
of a round is unchanged; while short, two of those calls are kept for discovery
until discovery has run (work_turns.DISCOVERY_RESERVED_EXECUTIONS).
"""

from .models import timestamp


# Stages that finish a passing type, in execution order: the crowding check
# (W6) runs after market review and before supplier interpretation, so a
# passing type's free Amazon/Flipkart crowding answer is worked out early.
NEARLY_READY_STAGES = ("acquire", "match", "crowding", "suppliers")


def report_supply_short(record):
    """Unknown supply is short; only a saved zero allocator shortfall is enough."""
    need = record.get("inventory_release_need")
    shortfall = need.get("shortfall") if isinstance(need, dict) else None
    return not (type(shortfall) is int and shortfall == 0)


def report_stock_ids(state, record, *, as_of, pool):
    """Admitted report stock that is still usable now needs no promotion.

    Only intake still eligible in the current report ``pool`` counts: a type
    gated again (a pending crowding check, say) is finished, not skipped.
    Expired stock (its support ends before now) is not current eligibility, so
    such a type can be finished again. The meaning check is the intake owner's.
    """
    from .inventory_intake import current_intake_records
    stock = set()
    for row in current_intake_records(state, record, pool=pool):
        until = row["binding"].get("usable_until")
        if until is None or timestamp(until) > timestamp(as_of):
            stock.add(row["binding"]["concept_id"])
    return stock


def passing_checklist(state, concept_id, fit):
    """A current functional-contract checklist with at least five Meets."""
    from .fit_description import FUNCTIONAL_CONTRACT
    return (fit is not None and fit.get("eligible") is True
            and state["assessments"][concept_id].get("contract_version") == FUNCTIONAL_CONTRACT)


def _acquire_pending(task, run, now):
    """The source stage would really work on this task now: an open dispatch or
    reconciliation, a prepared question that is due, or a retryable failure
    whose retry is due and not used up. Calm waits (sign-in, desktop, pacing)
    carry their retry time; closed, final and exhausted tasks are not work."""
    if task.get("reconcile_required") or task.get("status") == "dispatching":
        return True
    if run and (task.get("tries", 0) >= run["max_attempts"] or task.get("prelaunch_failures", 0) >= run["max_attempts"]):
        return False
    retry = task.get("next_retry_at")
    due = retry is None or timestamp(retry) <= now
    if task.get("status") == "prepared":
        return due
    return task.get("status") in {"failed", "blocked"} and task.get("retryable") is True and retry is not None and due


def finishing_work(state, scope, candidates, *, as_of):
    """Which candidate types hold actual pending work in each finishing stage.

    acquire: a due source task of this group's run (or none yet prepared); a
    target handed to a replacement question is that question's work; match: an
    open functional market-check question; crowding: saved reads or reviews
    the type's crowding answer has not yet taken in; suppliers: a saved
    supplier capture still waiting for interpretation. Read-only.
    """
    from .crowding import crowding_due, load_thresholds
    from .research_memory import key
    from .source_coverage import ROUTES, TASK_KIND, _target_superseded
    from .source_interpretation import functional_source_questions
    wanted = set(candidates)
    work = {stage: set() for stage in NEARLY_READY_STAGES}
    if not wanted:
        return work
    now = timestamp(as_of)
    run = state.get("batch_runs", {}).get(key("source-run", scope["source_run_id"]))
    seen_in_run = set()
    for task in state["query_tasks"].values():
        if task.get("kind") != TASK_KIND or task.get("route") not in ROUTES:
            continue  # A deleted route's saved question is history, never work.
        for cid in wanted.intersection(task.get("targets", {})):
            if (task.get("source") == "supplier"
                    and (task.get("target_results") or {}).get(cid, {}).get("state") == "interpretation_pending"):
                work["suppliers"].add(cid)
            if task.get("run_id") != scope["source_run_id"] or _target_superseded(state, task, cid):
                continue
            seen_in_run.add(cid)
            if _acquire_pending(task, run, now):
                work["acquire"].add(cid)
    work["acquire"] |= wanted - seen_in_run
    questions = functional_source_questions(state, run_id=scope["source_run_id"], as_of=as_of,
                                            concept_ids=sorted(wanted))
    work["match"] = {row["concept_id"] for row in questions if row["unknown_ids"]}
    thresholds = load_thresholds()
    work["crowding"] = {cid for cid in wanted if crowding_due(state, cid, as_of=as_of, thresholds=thresholds)}
    return work
