"""Plan R10 cutover (W13): what the new code finds in a store saved by the old one.

Run by the lead after research is stopped and the store is backed up
(``scripts/r10_cutover.py``). ``cutover_check`` only reads; ``apply_cutover``
switches on the Apify account list, releases Google Trends waits set by the
replaced hidden-browser route, and expires (development stage, each with a
recorded reason) the report stock frozen under the Meta rule before the free
and deeper reads were merged (``meta_facts.FACTS_VERSION``), so those types
are judged again. The first apply saves a marker with its time; later runs
only look at records saved before it, so repeating apply changes nothing new.
"""

from pathlib import Path

from .inventory_intake import INTAKE_KIND, expire_intake_records
from .issue_allocation import ACTIVE_STATES, cancel_unpublished_issues
from .meta_facts import FACTS_VERSION
from .models import SafetyError, timestamp
from .research_memory import key

MARKER_ID = key("r10-cutover", "marker")
MARKER_KIND = "r10-cutover-v1"
REASON = ("R10 cutover: frozen under the Meta rule before " + FACTS_VERSION + " merged the free and deeper reads "
          "(development stage); the type is judged again under the current rule.")
CONFIG_FILES = ("config/research/signed_in_reads.json", "config/research/dropship_platforms.json",
                "config/research/crowding.json", "config/research/spend_watch.json",
                "config/research/apify_allocation.json", "config/research/quickcommerce_locations.json",
                "config/research/round_limits.json", "config/research/wider_discovery.json",
                "config/research/product_map.json", "config/benchmarks/meta_first.json")
ACCOUNT_LIST = "config/local/apify_accounts.json"


def _meta_witness(support):
    return isinstance(support, dict) and any(isinstance(row, dict) and row.get("source") == "meta"
                                             for row in support.get("sources", []))


def _old_meta_product(product):
    """A reserved product whose Meta facts or Meta witness predate the merged rule."""
    if not isinstance(product, dict):
        return False
    meta = ((product.get("market") or {}).get("sources") or {}).get("meta")
    old_facts = isinstance(meta, dict) and (meta.get("depth") or {}).get("version") != FACTS_VERSION
    return old_facts or _meta_witness(product.get("priority_support"))


def _before(value, cutoff):
    return cutoff is None or isinstance(value, str) and timestamp(value) < timestamp(cutoff)


def marker(state):
    row = state["batch_runs"].get(MARKER_ID)
    return row if isinstance(row, dict) and row.get("kind") == MARKER_KIND else None


def cutover_check(memory, *, root, as_of):
    """Read only: everything the cutover would change or the lead should know."""
    from .source_coverage import _FINAL, CLOSED_ROUTES, TASK_KIND
    from .trends_shared import replaced_trends_waits
    timestamp(as_of)
    state = memory.snapshot()
    saved = marker(state)
    cutoff = saved and saved["applied_at"]
    intakes = sorted(ref for ref, row in state["batch_runs"].items()
                     if isinstance(row, dict) and row.get("kind") == INTAKE_KIND and row.get("expired") is None
                     and _meta_witness(row.get("binding", {}).get("priority_support"))
                     and _before(row.get("admitted_at"), cutoff))
    reserved, published = [], []
    for issue_id, row in sorted(state["issue_reservations"].items()):
        if (row.get("state") in ACTIVE_STATES and _before(row.get("reserved_at"), cutoff)
                and any(_old_meta_product(product) for product in row.get("products", []))):
            (reserved if row["state"] == "reserved" and "activation_receipt" not in row else published).append(issue_id)
    retired = {}
    for task_id, task in sorted(state["query_tasks"].items()):
        # Still open: closed (never sent) by the next source pass, as history.
        if (task.get("kind") == TASK_KIND and task.get("route") in CLOSED_ROUTES
                and task.get("status") not in _FINAL | {"retired"}):
            retired.setdefault(task["route"], []).append(task_id)
    history = {}
    for table in ("batch_runs", "query_tasks"):
        for row in state[table].values():
            if not isinstance(row, dict):
                continue
            route = (row.get("binding") or {}).get("route") if table == "batch_runs" else row.get("route")
            if route in CLOSED_ROUTES:
                history[route] = history.get(route, 0) + 1
    project = Path(root)
    return {"marker": saved, "old_meta_intake_ids": intakes, "old_meta_reserved_issue_ids": reserved,
            "old_meta_published_issue_ids_need_owner": published,
            "old_identity_trends_waits": replaced_trends_waits(state, as_of=as_of),
            "retired_route_open_tasks": retired, "retired_route_saved_records": history,
            "missing_config_files": [name for name in CONFIG_FILES if not (project / name).is_file()],
            "account_list_present": (project / ACCOUNT_LIST).is_file()}


def apply_cutover(memory, *, root, as_of, enable_accounts=None):
    """Make the cutover changes; each step reports its own result, and a failed
    step never hides the others. Returns ``(result, ok)``."""
    from .accounts import enable_account_list
    from .trends_shared import release_replaced_trends_waits
    found = cutover_check(memory, root=root, as_of=as_of)
    result, ok = {}, True
    try:
        result["account_list"] = (enable_accounts or enable_account_list)(root)
    except SafetyError as error:
        result["account_list"], ok = {"written": False, "reason": str(error)}, False
    result["trends_waits_released"] = release_replaced_trends_waits(memory, as_of=as_of)
    result["intake_expired"] = expire_intake_records(memory, found["old_meta_intake_ids"], as_of=as_of, reason=REASON)
    state = memory.snapshot()
    expected = {issue_id: state["issue_reservations"][issue_id]["selection_hash"]
                for issue_id in found["old_meta_reserved_issue_ids"]}
    result["issues_cancelled"] = sorted(cancel_unpublished_issues(memory, expected, as_of=as_of, reason=REASON)) if expected else []
    result["published_issues_need_owner"] = found["old_meta_published_issue_ids_need_owner"]
    if found["marker"] is None:
        value = {"kind": MARKER_KIND, "applied_at": as_of, "reason": REASON}
        memory.put("batch_runs", MARKER_ID, value, event_id=key("r10-cutover-marker", as_of))
        result["marker_saved"] = True
    else:
        result["marker_saved"] = False
    return result, ok
