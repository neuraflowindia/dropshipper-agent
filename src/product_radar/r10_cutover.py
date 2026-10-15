"""Plan R10 cutover (W13): what the new code finds in a store saved by the old one.

Run by the lead after research is stopped and the store is backed up
(``scripts/r10_cutover.py``). ``cutover_check`` only reads; ``apply_cutover``
switches on the Apify account list, releases Google Trends waits set by the
replaced hidden-browser route, and expires (development stage, each with a
recorded reason) the report stock frozen under the old rules: the Meta rule
before the free and deeper reads were merged (``meta_facts.FACTS_VERSION``) or
the four priority tiers the opportunity score replaced (``opportunity_score``),
so those types are judged again. The first apply saves a marker with its time;
later runs only look at records saved before it, so repeating apply changes
nothing new. ``score_preview`` works out crowding and the score on a copy of a
store (never the live store), for the lead's count before cutover.
"""

from pathlib import Path

from .inventory_intake import INTAKE_KIND, expire_intake_records
from .issue_allocation import ACTIVE_STATES, cancel_unpublished_issues
from .meta_facts import FACTS_VERSION
from .models import SafetyError, timestamp
from .research_memory import key

MARKER_ID = key("r10-cutover", "marker")
MARKER_KIND = "r10-cutover-v1"
REASON = ("R10 cutover: frozen under the Meta rule before " + FACTS_VERSION + " merged the free and deeper reads, "
          "or ranked by the retired priority tiers (development stage); the type is judged again under the current rules.")
CONFIG_FILES = ("config/research/signed_in_reads.json", "config/research/dropship_platforms.json",
                "config/research/crowding.json", "config/research/opportunity_score.json", "config/research/spend_watch.json",
                "config/research/apify_allocation.json", "config/research/quickcommerce_locations.json",
                "config/research/round_limits.json", "config/research/wider_discovery.json",
                "config/research/product_map.json", "config/benchmarks/meta_first.json")
ACCOUNT_LIST = "config/local/apify_accounts.json"


def _meta_witness(support):
    return isinstance(support, dict) and any(isinstance(row, dict) and row.get("source") == "meta"
                                             for row in support.get("sources", []))


def _tier_binding(value):
    """Frozen under the retired priority tiers: their witness, or an older policy id."""
    from .opportunity_score import POLICY_ID
    return isinstance(value, dict) and ("priority_support" in value or "opportunity_priority" in value
                                        or value.get("opportunity_policy_id") not in (None, POLICY_ID))


def _old_product(product):
    """A reserved product whose Meta facts or witness predate the merged rule, or ranked by the tiers."""
    if not isinstance(product, dict):
        return False
    meta = ((product.get("market") or {}).get("sources") or {}).get("meta")
    old_facts = isinstance(meta, dict) and (meta.get("depth") or {}).get("version") != FACTS_VERSION
    return old_facts or _tier_binding(product) or _meta_witness(product.get("score_support"))


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
                     and (_tier_binding(row.get("binding")) or _meta_witness(row.get("binding", {}).get("score_support")))
                     and _before(row.get("admitted_at"), cutoff))
    reserved, published = [], []
    for issue_id, row in sorted(state["issue_reservations"].items()):
        if (row.get("state") in ACTIVE_STATES and _before(row.get("reserved_at"), cutoff)
                and any(_old_product(product) for product in row.get("products", []))):
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
    return {"marker": saved, "old_stock_intake_ids": intakes, "old_stock_reserved_issue_ids": reserved,
            "old_stock_published_issue_ids_need_owner": published,
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
    result["intake_expired"] = expire_intake_records(memory, found["old_stock_intake_ids"], as_of=as_of, reason=REASON)
    state = memory.snapshot()
    expected = {issue_id: state["issue_reservations"][issue_id]["selection_hash"]
                for issue_id in found["old_stock_reserved_issue_ids"]}
    result["issues_cancelled"] = sorted(cancel_unpublished_issues(memory, expected, as_of=as_of, reason=REASON)) if expected else []
    result["published_issues_need_owner"] = found["old_stock_published_issue_ids_need_owner"]
    if found["marker"] is None:
        value = {"kind": MARKER_KIND, "applied_at": as_of, "reason": REASON}
        memory.put("batch_runs", MARKER_ID, value, event_id=key("r10-cutover-marker", as_of))
        result["marker_saved"] = True
    else:
        result["marker_saved"] = False
    return result, ok


# Score preview on a store copy -------------------------------------------------

_CAPTURED = ("data", "research", "captured")
# Stores the preview must never open, besides the configured runtime store: the
# live store's path is machine-local, so it is named here, never in the code.
PROTECTED_STORES_ENV = "PRODUCT_RADAR_LIVE_STORE"
PROVISIONAL_MATCH_VERSIONS = ("research-e-source-support-v4",)


def preview_store_refused(store, project_root, environ=None):
    """Why a store path may not be previewed (a protected or the configured store), else None."""
    import os
    from .runtime_storage import runtime_root
    store = Path(store).resolve()
    named = [Path(value) for value in (environ if environ is not None else os.environ).get(PROTECTED_STORES_ENV, "").split(os.pathsep)
             if value]
    for refused, reason in ([(path.resolve(), "SCORE_PREVIEW_LIVE_STORE_REFUSED") for path in named]
                            + [(Path(runtime_root(project_root)).resolve(), "SCORE_PREVIEW_RUNTIME_STORE_REFUSED")]):
        if store == refused or refused in store.parents or store in refused.parents:
            return reason
    if not (store.joinpath(*_CAPTURED)).is_dir():
        return "SCORE_PREVIEW_STORE_NOT_FOUND"
    return None


def _names_itself(state, store):
    """The store's own records point into it: it is an original store, not a copy
    (checked by spelling only, so nothing outside the copy is opened)."""
    import os
    root = os.path.abspath(store)
    for capture in state["captures"].values():
        reference = capture.get("reference") if isinstance(capture, dict) else None
        folder = reference.get("folder") if isinstance(reference, dict) else None
        if isinstance(folder, str) and os.path.abspath(folder).startswith(root + os.sep):
            return True
    return False


def _rebased(folder, store):
    """A saved page folder inside the copy (saved paths name the original store)."""
    path = Path(folder)
    parts = path.parts
    for index in range(len(parts) - len(_CAPTURED) + 1):
        if parts[index:index + len(_CAPTURED)] == _CAPTURED:
            target = Path(store).joinpath(*parts[index:])
            if Path(store).resolve() not in target.resolve().parents:
                break
            return str(target)
    raise SafetyError("SCORE_PREVIEW_PAGE_OUTSIDE_COPY")


def copy_page_reader(store):
    """(card reader, folder map) that read and cache saved pages only inside the copy.

    Records keep their original folders (their hashes bind them); each folder is
    mapped into the copy at read time, and nothing is read or written outside it.
    """
    from .marketplace_cards import read_capture_cards
    root = Path(store).resolve()
    def folders(folder):
        return _rebased(folder, root)  # refuses anything that would resolve outside the copy
    def reader(folder, route):
        return read_capture_cards(folders(folder), route)
    return reader, folders


def _tally(state, memory, accepted, as_of, top):
    """Crowding levels, score labels and the top types from one view of the store."""
    from types import SimpleNamespace
    from .crowding import NOT_CHECKED, crowding_for
    from .opportunity_score import LABELS, crowding_level, score_rank
    from .research_flow import candidate_summary
    frozen = SimpleNamespace(snapshot=lambda: state, evidence_type=memory.evidence_type, root=memory.root)
    products = {row["id"]: row for row in candidate_summary(frozen, as_of=as_of)["products"] if row["id"] in accepted}
    levels = dict.fromkeys(("low", "medium", "high", NOT_CHECKED), 0)
    for cid in accepted:
        levels[crowding_for(state, cid)["level"]] += 1
    scored = [row for row in products.values() if row.get("opportunity") is not None]
    labels = dict.fromkeys(LABELS, 0)
    for row in scored:
        if row["opportunity"]["opportunity_basis"]["reason"] in ("demand_too_quiet", "opportunity_scored"):
            labels[row["opportunity"]["opportunity_label"]] += 1
    eligible = [row for row in scored if row["opportunity"]["eligible"]]
    ranked = sorted(eligible, key=lambda row: (*score_rank(row["opportunity"]), row["id"]))
    return {"assessed": len(scored),
            "waiting_for_checklist_review": sum(row.get("opportunity") is None for row in products.values()),
            "five_or_more_meets": sum(row["checklist_meets"] is not None and row["checklist_meets"] >= 5 for row in scored),
            "crowding": levels, "labels": labels, "score_eligible": len(eligible),
            "top": [{"concept_id": row["id"], "label": row["label"], "score": row["opportunity"]["opportunity_score"],
                     "score_label": row["opportunity"]["opportunity_label"],
                     "parts": {part: row["opportunity"]["opportunity_basis"]["parts"][part]["points"]
                               for part in ("want", "early", "room", "product")},
                     "crowding": crowding_level(row["opportunity"])}
                    for row in ranked[:top]]}


def _rereview(state, accepted, as_of):
    """Types and saved-record cohorts still waiting for the current relevance review."""
    from .research_ai import FUNCTIONAL_SOURCE_PROMPT_VERSION
    from .source_interpretation import functional_source_questions
    per_type = {}
    for row in functional_source_questions(state, as_of=as_of, concept_ids=accepted):
        if row["unknown_ids"]:
            per_type[row["concept_id"]] = per_type.get(row["concept_id"], 0) + 1
    most = max(per_type.values(), default=0)
    return {"prompt_version": FUNCTIONAL_SOURCE_PROMPT_VERSION, "types_waiting": len(per_type),
            "cohorts_waiting": sum(per_type.values()), "most_cohorts_for_one_type": most, "rounds_estimate": most,
            "note": "Estimate: about one cohort per type per round, so the type with the most cohorts sets the rounds."}


def _provisional(memory, state, accepted, as_of, reader, folders, top):
    """Preview only: saved v4 relevance answers counted as if re-reviewed
    unchanged. Crowding is worked out without saving and scored on an
    in-memory view; no record is written and no research path sees it."""
    from .crowding import assess_crowding, fact_id, load_thresholds
    from .market_validation import PREVIEW_MATCH_VERSIONS
    token = PREVIEW_MATCH_VERSIONS.set(PROVISIONAL_MATCH_VERSIONS)
    try:
        thresholds, memo = load_thresholds(), {}
        facts = {fact_id(cid): dict(assess_crowding(state, cid, as_of=as_of, thresholds=thresholds, card_reader=reader,
                                                    memo=memo, folders=folders), assessed_at=as_of) for cid in accepted}
        view = dict(state, batch_runs=dict(state["batch_runs"], **facts))
        result = _tally(view, memory, accepted, as_of, top)
    finally:
        PREVIEW_MATCH_VERSIONS.reset(token)
    return {"label": "PROVISIONAL, preview only: saved " + ", ".join(PROVISIONAL_MATCH_VERSIONS)
                     + " relevance answers treated as current; nothing is saved", **result}


def score_preview(memory, *, store, as_of, top=15):
    """Crowding (free re-reads of the copy's saved pages) and the score for every
    accepted type, as counts and the top types, plus the types waiting for the
    current relevance review and a labelled provisional count. No network and
    no model call; it refuses a store whose records point into itself."""
    from .crowding import refresh_crowding
    from .functional_types import effective_functional_definition
    timestamp(as_of)
    reader, folders = copy_page_reader(store)
    state = memory.snapshot()
    if _names_itself(state, store):
        raise SafetyError("SCORE_PREVIEW_STORE_IS_ORIGINAL")
    accepted = sorted(cid for cid, row in state["concepts"].items()
                      if row.get("status") not in ("excluded", "merged", "retired")
                      and effective_functional_definition(state, cid)["accepted"])
    rounds = 0
    while True:
        rounds += 1
        result = (refresh_crowding(memory, accepted, as_of=as_of, limit=500, card_reader=reader, folders=folders)
                  if accepted else {"more_due": False})
        if not result["more_due"] or rounds >= 100:
            break
    state = memory.snapshot()
    return {"as_of": as_of, "accepted_types": len(accepted), **_tally(state, memory, accepted, as_of, top),
            "note": ("score_eligible: 5+ Meets, 5+ want points and a known crowding level. A report also needs a "
                     "finished supplier search and the other report checks."),
            "rereview": _rereview(state, accepted, as_of),
            "provisional_saved_v4_matches": _provisional(memory, state, accepted, as_of, reader, folders, top)}
