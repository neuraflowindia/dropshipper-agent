"""Admission bookkeeping shared by the discovery lanes that nominate their own
captures: the dropshipping platforms (W4, ``platform_roles``) and wider
discovery (W8, ``wider_discovery``).

A capture's admission is over when every item is described and each of its
products decided (resolved, excluded or deferred) or recorded as a problem, or
when the capture is older than the lane's settle days and every item was at
least described, so an old capture never holds new nominations for ever but
an item the AI never saw is never given up on. Admission itself is the
existing candidate flow (``research_flow.run_flow``).
"""

from datetime import timedelta

from .models import digest, timestamp
from .research_memory import key


def unsettled(state, captures, as_of, settle_days):
    """The captures (each with ``acquisition_id`` and ``imported_at``) whose
    admission is not over, in their given order; one pass over the observations.
    Age settles only a capture whose every item was at least described (or
    failed): a never-described item keeps its capture pending, however old."""
    wanted = {row["acquisition_id"] for row in captures}
    semantics = {}
    for row in state["observations"].values():
        if row["acquisition_id"] in wanted:
            semantics.setdefault(row["acquisition_id"], set()).add(row["semantic_id"])
    problems = {row.get("affected_id") for row in state["problems"].values()}

    def decided(semantic):
        description = state["descriptions"].get(semantic)
        if description is None:
            return semantic in problems
        for index in range(len(description["result"]["products"])):
            child = key("child", [semantic, index, digest(description["result"])])
            if state["associations"].get(child, {}).get("status") not in ("resolved", "excluded", "deferred") \
                    and child not in problems:
                return False
        return True

    def over(row):
        items = semantics.get(row["acquisition_id"], ())
        if all(decided(item) for item in items):
            return True
        old = timestamp(as_of) - timestamp(row["imported_at"]) >= timedelta(days=settle_days)
        return old and all(item in state["descriptions"] or item in problems for item in items)
    return [row for row in captures if not over(row)]


def admit(memory, runner, acquisition_ids, *, baseline, as_of, flow_limit):
    """Continue admission of up to ten captures; returns the admitted types that
    did not exist when each capture's candidates were chosen (``baseline(id)``)."""
    from .catalogue_discovery import _acquisition_progress
    from .fit_policy import FIT_POLICY_ID
    from .research_flow import run_flow
    acquisition_ids = list(acquisition_ids)[:10]
    if not acquisition_ids:
        return []
    run_flow(memory, runner, acquisition_ids=acquisition_ids, limit=flow_limit, as_of=as_of, fit_policy_id=FIT_POLICY_ID)
    state, new = memory.snapshot(), set()
    for acquisition_id in acquisition_ids:
        new.update(_acquisition_progress(state, [acquisition_id], baseline(acquisition_id))["new_concept_ids"])
    return sorted(new)
