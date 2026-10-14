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


# Stages that finish a passing type, in execution order. This is the single
# place W6 adds the crowding-check stage (after market review, before supplier
# interpretation) once that stage exists in programme.STAGES, together with its
# pending-work test in finishing_work below.
NEARLY_READY_STAGES = ("acquire", "match", "suppliers")
_FINAL_TASK_STATES = frozenset(("acquired", "completed_no_results", "insufficient", "finished"))


def report_supply_short(record):
    """Unknown supply is short; only a saved zero allocator shortfall is enough."""
    need = record.get("inventory_release_need")
    shortfall = need.get("shortfall") if isinstance(need, dict) else None
    return not (type(shortfall) is int and shortfall == 0)


def report_stock_ids(state, record, *, as_of):
    """Admitted report stock that is still usable now needs no promotion.

    Expired stock (its support ends before now) is not current eligibility, so
    such a type can be finished again. The meaning check is the intake owner's.
    """
    from .inventory_intake import current_intake_records
    stock = set()
    for row in current_intake_records(state, record):
        until = row["binding"].get("usable_until")
        if until is None or timestamp(until) > timestamp(as_of):
            stock.add(row["binding"]["concept_id"])
    return stock


def passing_checklist(state, concept_id, fit):
    """A current functional-contract checklist with at least five Meets."""
    from .fit_description import FUNCTIONAL_CONTRACT
    return (fit is not None and fit.get("eligible") is True
            and state["assessments"][concept_id].get("contract_version") == FUNCTIONAL_CONTRACT)


def finishing_work(state, scope, candidates, *, as_of):
    """Which candidate types hold actual pending work in each finishing stage.

    acquire: an unfinished, due source task of this group's run (or none yet
    prepared); match: an open functional market-check question; suppliers: a
    saved supplier capture still waiting for interpretation. Read-only.
    """
    from .source_coverage import TASK_KIND
    from .source_interpretation import functional_source_questions
    wanted = set(candidates)
    work = {stage: set() for stage in NEARLY_READY_STAGES}
    if not wanted:
        return work
    now = timestamp(as_of)
    seen_in_run = set()
    for task in state["query_tasks"].values():
        if task.get("kind") != TASK_KIND:
            continue
        for cid in wanted.intersection(task.get("targets", {})):
            if (task.get("source") == "supplier"
                    and (task.get("target_results") or {}).get(cid, {}).get("state") == "interpretation_pending"):
                work["suppliers"].add(cid)
            if task.get("run_id") != scope["source_run_id"]:
                continue
            seen_in_run.add(cid)
            retry = task.get("next_retry_at")
            if (task.get("reconcile_required") or task.get("status") not in _FINAL_TASK_STATES
                    and (retry is None or timestamp(retry) <= now)):
                work["acquire"].add(cid)
    work["acquire"] |= wanted - seen_in_run
    questions = functional_source_questions(state, run_id=scope["source_run_id"], as_of=as_of,
                                            concept_ids=sorted(wanted))
    work["match"] = {row["concept_id"] for row in questions if row["unknown_ids"]}
    return work
