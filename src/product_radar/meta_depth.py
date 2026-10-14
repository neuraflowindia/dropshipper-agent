"""Deeper Meta reads through the free monthly Apify allowance (Plan R10 W11, D3).

``meta-ads-depth`` is a paid follow-up to the free Meta read, never a second
Meta site: its ads become ordinary Meta observations (marked with this route),
are reviewed like any Meta capture, and are counted once with the free read's
ads, which they complete (``meta_facts``).

Admission (``depth_request``): a type is asked only after its free Meta read
has finished for the current description and every item that read saved has
been reviewed; only when, judged from that free read's own relevant ads, the
Meta check is still undecided (Insufficient) and at least one relevant ad
lacks an advertiser identity (page ID or page address) or a start date. The
same set of lacking ads is deepened once: a later free read showing the same
gaps is not paid for again. Nearly report-ready types are served first by the
source stage's own priority (``nearly_ready``).

Money (``meta_depth_money``): the existing account registry, ledger and
reserve-before-spend controller (``benchmark.MetaTrial``:
``accounts.choose_account`` with the account list, the Apify transport,
``map_meta_rows``) with the reviewed production request: at most US$0.03 for
the run plus US$0.02 held for reading its results, which stays an unknown hold
until settled. A paid call whose outcome is uncertain is only ever recovered,
never repeated, and a question is never finished while its bill is unknown.
The ledger refuses to reserve past the protected US$0.50 buffer; the route
then waits calmly for the next monthly allowance, which it records itself.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .meta_depth_money import (MetaDepthWait, account_transport, money_check, notice_once, read_limits,
                               renew_period, show, wait_for)
from .models import SafetyError, as_production_request, digest, identifier, prepared_request, timestamp
from .storage import atomic_json, load_json, safe_path

ROUTE = "meta-ads-depth"
FREE_ROUTE = "meta-ads"
VERSION = "meta-depth-v2"
DEPTH_MAPPER = "meta-depth-profile-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT_ROOT / "config/benchmarks/meta_first.json"
LIMIT = 10  # the reviewed actor's validation sample (models.PreparedMeta)
HANDLER_SECONDS = 600  # one preflight (at most 120 s) and one bounded run (at most 480 s)
FREE_SETTLED = {"acquired", "completed_no_results", "insufficient"}
RUN_RECHECK = timedelta(minutes=10)
MEMO_LIMIT = 2048
_MEMO = {}
_INDEX = {"state": None}


def attempt_id_for(acquisition_id):
    """One ledger attempt per acquisition, so a repeat can only ever resume it."""
    return "meta-depth-" + digest(identifier(acquisition_id))[:32]


# ----- admission ------------------------------------------------------------

def _index(state):
    """Meta questions by (route, type, description) and saved reviews by type, once per state."""
    if _INDEX["state"] is not state:
        from .source_coverage import TASK_KIND
        tasks, reviews = {}, {}
        for tid, task in sorted(state["query_tasks"].items()):
            if task.get("kind") == TASK_KIND and task.get("route") in (FREE_ROUTE, ROUTE):
                for cid, scope in task.get("targets", {}).items():
                    tasks.setdefault((task["route"], cid, scope), []).append((tid, task))
        for rid, row in state["source_checks"].items():
            subject = row.get("subject") if isinstance(row, dict) else None
            cid = subject.get("concept_id") if isinstance(subject, dict) else row.get("concept_id")
            if isinstance(cid, str):
                reviews.setdefault(cid, []).append(rid)
        _INDEX.clear()
        _INDEX.update(state=state, tasks=tasks, reviews={cid: tuple(sorted(ids)) for cid, ids in reviews.items()})
    return _INDEX


def _free_gaps(state, concept_id, settled, *, as_of):
    from .market_validation import functional_market_context
    from .meta_facts import meta_state, relevant_ads
    from .read_memo import pure_read_scope
    from .source_interpretation import functional_source_questions
    with pure_read_scope():  # one scoped-evidence validation per type, not one per reader
        if any(row["unknown_ids"] for row in functional_source_questions(
                state, as_of=as_of, concept_ids=[concept_id], task_ids=[tid for tid, _ in settled])):
            raise SafetyError("META_DEPTH_FREE_REVIEW_PENDING")
        context = functional_market_context(state, concept_id, as_of=as_of)
    if context is None:
        raise SafetyError("META_DEPTH_TYPE_MEANING_PENDING")
    owners = {tid: {ref for receipt in task.get("receipt_refs", [])
                    for ref in state["source_checks"][receipt]["envelope"].get("observation_ids", [])
                    if ref in state["observations"]} for tid, task in settled}
    free = set().union(*owners.values())
    # Only the free capture's own ads decide; a deeper read never re-admits itself.
    rows = [(ref, state["observations"][ref]) for ref in sorted(context["matches"])
            if ref in free and state["observations"][ref]["source"] == "meta"]
    ads, conflicts = relevant_ads(rows)
    valid = [ad for ad_id, ad in ads.items() if ad_id not in conflicts]
    lacking = sorted(ad_id for ad_id, ad in ads.items() if ad_id not in conflicts
                     and (ad["advertiser"] is None or ad["started_at"] is None))
    seen = {tid: {state["observations"][ref]["facts"]["ad_id"] for ref in refs} for tid, refs in owners.items()}
    # The free search that saw most of the lacking ads is the one deepened.
    task_id = max(seen, key=lambda tid: (len(seen[tid] & set(lacking)), timestamp(dict(settled)[tid]["prepared_at"]), tid))
    return {"relevant_ads": len(valid), "meta_state": meta_state(valid, conflicts), "lacking_ads": lacking,
            "free_task_id": task_id, "query": dict(settled)[task_id]["request"].get("query"),
            "basis": digest(lacking)}


def depth_gaps(state, concept_id, *, as_of):
    """Why a type is or is not asked for a deeper read, from saved facts only.

    Raises a pending code while the free read has not finished or its items
    still need review. The answer is remembered for the same free questions,
    the same saved reviews of the type and the same hour, so a planning pass
    repeats only the cheap checks.
    """
    scope = digest(state["concepts"][concept_id]["description"])
    index = _index(state)
    settled = [(tid, task) for tid, task in index["tasks"].get((FREE_ROUTE, concept_id, scope), [])
               if task.get("status") in FREE_SETTLED and task.get("attempt_refs")]
    if not settled:
        raise SafetyError("META_DEPTH_FREE_READ_PENDING")
    signature = (concept_id, scope, timestamp(as_of).astimezone(timezone.utc).strftime("%Y-%m-%dT%H"),
                 tuple((tid, task["status"], tuple(task["receipt_refs"]), tuple(task["attempt_refs"])) for tid, task in settled),
                 index["reviews"].get(concept_id, ()))
    if signature not in _MEMO:
        try:
            _MEMO[signature] = ("value", _free_gaps(state, concept_id, settled, as_of=as_of))
        except SafetyError as error:
            if type(error) is not SafetyError or not str(error).startswith("META_DEPTH_"):
                raise
            _MEMO[signature] = ("error", str(error))
        while len(_MEMO) > MEMO_LIMIT:
            _MEMO.pop(next(iter(_MEMO)))
    kind, value = _MEMO[signature]
    if kind == "error":
        raise SafetyError(value)
    return deepcopy(value)


def depth_request(state, concept_id, context):
    """The planner's question for one type: a request, or why it does not apply."""
    gaps = depth_gaps(state, concept_id, as_of=context["as_of"])
    if not gaps["relevant_ads"]:
        return {"not_applicable_reason": "The free Meta read found no relevant ads to read more deeply."}
    if gaps["meta_state"] != "Insufficient":
        return {"not_applicable_reason": "The free Meta read already settles the Meta check; a deeper read would not change it."}
    if not gaps["lacking_ads"]:
        return {"not_applicable_reason": "The free Meta read already shows every relevant ad's advertiser and start date."}
    query = gaps["query"]
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
        return {"not_applicable_reason": "The free Meta search words do not fit the reviewed deeper read."}
    scope = digest(state["concepts"][concept_id]["description"])
    if any(task["request"].get("basis") == gaps["basis"] and task.get("run_id") != context.get("run_id")
           and task.get("attempt_refs") for _, task in _index(state)["tasks"].get((ROUTE, concept_id, scope), [])):
        return {"not_applicable_reason": "These Meta ads were already read more deeply."}
    return {"query": " ".join(query.split()), "country": "IN", "limit": LIMIT, "basis": gaps["basis"],
            "version": VERSION}


def _validated(request):
    if (not isinstance(request, dict) or set(request) != {"query", "country", "limit", "basis", "version"}
            or request["country"] != "IN" or request["limit"] != LIMIT or request["version"] != VERSION
            or not isinstance(request["query"], str) or not 1 <= len(request["query"]) <= 120
            or not isinstance(request["basis"], str) or len(request["basis"]) != 64):
        raise SafetyError("META_DEPTH_REQUEST_INVALID")
    return request


# ----- the reviewed paid request ----------------------------------------------

def prepared_for(attempt_id, query):
    """The reviewed production request for one deeper read (offline)."""
    from .benchmark import prepare_meta
    config = load_json(CONFIG, 16384)
    config.update(term=query, purpose="validation")
    envelope = prepare_meta(config, attempt_id)
    request = as_production_request(prepared_request(envelope["request"]))
    return request, dict(envelope, request=request.to_dict(), config_hash=request.config_hash)


def validate_reviewed_config(value):
    """The reviewed request settings, checked offline exactly as ``prepared_for``
    turns them into a production request (also when they are backed up)."""
    from .benchmark import prepare_meta
    envelope = prepare_meta(deepcopy(value), "meta-depth-settings-check")
    as_production_request(prepared_request(envelope["request"]))
    return value


def save_preparation(root, attempt_id, query, *, now):
    """Save the offline preparation once, where the controller reads it."""
    from .runtime_storage import runtime_path
    base = safe_path(runtime_path(root, "data/benchmarks"))
    request, envelope = prepared_for(attempt_id, query)
    saved = sorted(base.glob("*/" + attempt_id + "/prepared.json"))
    if saved:
        if load_json(saved[0], 1024 * 1024) != envelope:
            raise SafetyError("SAVED_PREPARATION_CONFLICT")
        return request
    atomic_json(base / now.date().isoformat() / attempt_id / "prepared.json", envelope)
    return request


# ----- the route ------------------------------------------------------------

def _ledger_attempt(journal, attempt_id):
    with journal.locked(timeout=5):
        return deepcopy(journal.state()[1].get(attempt_id))


def import_depth_capture(memory, acquisition_id, attempt_id, request):
    """Import a committed deeper read as Meta observations of this route (idempotent).

    Mapper ``meta-depth-profile-v1``: the reviewed row mapping plus each row's
    own page address (``page_profile_uri``), so a page known by its address in
    the free read and by its ID here is recognised as one advertiser. The
    ledger's saved mapping of the provider rows is unchanged.
    """
    from .meta_browser import _page_url
    from .research_memory import import_capture, saved_attempt_mapping
    attempt, prepared, manifest, mapped, rows = saved_attempt_mapping(memory, attempt_id, timeout=5)
    if prepared.attempt_id != attempt_id_for(acquisition_id):
        raise SafetyError("META_DEPTH_CAPTURE_MISMATCH")
    records = []
    for record in mapped["records"]:
        row = rows[record["raw_references"][0]["row"]]
        address = _page_url(row.get("page_profile_uri"))
        records.append(dict(record, source_route=ROUTE, **({"advertiser_url": address} if address else {})))
    imported = import_capture(memory, source="meta", acquisition_id=acquisition_id, observed_at=manifest["observed_at"],
        mapped=dict(mapped, records=records, depth_mapper=DEPTH_MAPPER), capture_hash=attempt["capture"],
        reference={"attempt_id": attempt_id, "file": "captured_rows.json", "route": ROUTE, "request": deepcopy(request),
                   "mapper": DEPTH_MAPPER})
    state = memory.snapshot()
    observations = sorted(ref for ref, row in state["observations"].items() if row["capture_id"] == imported["capture_id"])
    return imported["capture_id"], observations, manifest, mapped


def _receipt(context, *, state, dispatch_started, observed_at, reference, reason, capture_ids=(), observations=(),
             retryable=False):
    target = ("interpretation_pending" if observations else "completed_no_results" if state == "empty" else None)
    return {"schema_version": 1, "route": ROUTE, "acquisition_id": context["acquisition_id"], "observed_at": observed_at,
            "state": state, "dispatch_started": dispatch_started, "reference": reference,
            "capture_ids": list(capture_ids), "observation_ids": list(observations),
            "target_results": [] if target is None else [{"concept_id": cid, "description_hash": context["description_hashes"][cid],
                "state": target, "evidence_refs": list(observations), "reason": reason} for cid in context["concept_ids"]],
            "reason": reason, "retryable": retryable, "failure_scope": "question"}


def _live_available(root, controller, spend_root, *, now):
    """Apify's own figure of what is left this month, or None to rely on the ledger.

    A key that may not read it is an owner notice, not a stop: the ledger and
    the dated opening balance still bound every reservation.
    """
    try:
        transport = account_transport(root, controller.account_registry, controller.transport_factory)
        return read_limits(transport, now=now)["available_usd"]
    except SafetyError as error:
        if str(error) == "APIFY_PAID_PLAN_REFUSED":
            raise
        if str(error) == "APIFY_LIMITS_READ_DENIED":
            try:
                notice_once(spend_root, str(error), at=now.isoformat(), period=now.strftime("%Y-%m"))
            except (SafetyError, OSError):
                pass
        return None


def _admit(memory, controller, attempt_id, prepared, *, root, spend_root, instant):
    """Before any reservation: the money check (recording a new month when due), then the controller's preflight."""
    registry = controller.account_registry
    live = _live_available(root, controller, spend_root, now=instant)
    try:
        money_check(root, memory.journal, registry, prepared, now=instant, available=live)
    except MetaDepthWait as wait:
        if str(wait) != "META_DEPTH_PERIOD_RENEWAL_PENDING":
            raise
        renew_period(root, memory.journal, registry, controller.transport_factory, now=instant)
        money_check(root, memory.journal, registry, prepared, now=instant)
    show(spend_root, at=instant.isoformat(), proceed=True)
    controller.preflight(attempt_id)
    show(spend_root, at=instant.isoformat(), settled=True)


def collect_depth(memory, context, *, root, spend_root, controller_factory, now, reconcile=False):
    """One deeper Meta read for one question: reserve, run once, import; or resume.

    A ledger attempt that exists is only resumed (recovery and status reads),
    never launched again. Before any reservation every refusal is a calm
    wait. A question is finished only once its run's bill is known.
    """
    request = _validated(context["request"])
    aid = identifier(context["acquisition_id"])
    attempt_id = attempt_id_for(aid)
    folder = safe_path(memory.root, "meta_depth", aid)
    path = folder / "receipt.json"
    if path.is_file():
        saved = load_json(path, 1024 * 1024)
        if saved.get("version") != VERSION or saved.get("acquisition_id") != aid or saved.get("request") != request:
            raise SafetyError("META_DEPTH_RECEIPT_CONFLICT")
        if saved["capture"]:
            capture_id, observations, _, _ = import_depth_capture(memory, aid, attempt_id, request)
            if capture_id != saved["capture"] or observations != saved["observation_ids"]:
                raise SafetyError("META_DEPTH_RECEIPT_CONFLICT")
        return saved["envelope"]
    instant = now()
    attempt = _ledger_attempt(memory.journal, attempt_id)
    controller = None
    if attempt is None:
        if reconcile:
            # Nothing was reserved, so nothing was paid or sent.
            return _receipt(context, state="failed", dispatch_started=False, observed_at=instant.isoformat(),
                            reference=str(path), reason="META_DEPTH_NOT_STARTED", retryable=True)
        prepared = save_preparation(root, attempt_id, request["query"], now=instant)
        controller = controller_factory()
        try:
            _admit(memory, controller, attempt_id, prepared, root=root, spend_root=spend_root, instant=instant)
        except SafetyError as error:
            if str(error) == "RUNNER_ALREADY_ACTIVE":
                raise
            wait = error if isinstance(error, MetaDepthWait) else wait_for(
                (getattr(error, "account_reasons", None) or [{"reason": str(error)}])[0]["reason"], now=instant)
            show(spend_root, at=instant.isoformat(), journal=memory.journal, wait=wait)
            raise wait from None
    elif attempt["submission"] in ("reserved", "not_submitted"):
        # A reservation without a saved send intent is provably unsent.
        controller = controller_factory()
        if attempt["submission"] == "reserved":
            controller.resolve_non_submission(attempt_id)
        show(spend_root, at=instant.isoformat(), journal=memory.journal)
        return _receipt(context, state="failed", dispatch_started=False, observed_at=instant.isoformat(),
                        reference=str(path), reason="META_DEPTH_RESERVATION_CANCELLED", retryable=True)
    controller = controller or controller_factory()
    result = controller.run(attempt_id, resume_only=attempt is not None)
    attempt = _ledger_attempt(memory.journal, attempt_id)
    show(spend_root, at=now().isoformat(), journal=memory.journal)
    if attempt is None:
        # Refused before any reservation: nothing was paid or sent.
        raise wait_for(result.get("code", "META_DEPTH_NOT_STARTED"), now=instant)
    if attempt["submission"] in ("uncertain", "submitted") and callable(context.get("record_dispatch")):
        context["record_dispatch"]()
    finished = attempt["remote_status"] and attempt["charge"] is not None
    if attempt["capture"] and finished:
        capture_id, observations, manifest, mapped = import_depth_capture(memory, aid, attempt_id, request)
        # A returned total of zero is Meta's own "no ads" answer (or no rows).
        state = ("usable" if mapped["status"] == "usable" and observations else "partial" if observations else
                 "empty" if manifest["returned_total"] == 0 else "insufficient")
        reason = ("Deeper Meta ads were saved; their relevance review follows." if observations else
                  "The deeper Meta read found no ads for this search." if state == "empty" else
                  "The deeper Meta read returned no usable ads.")
        envelope = _receipt(context, state=state, dispatch_started=True, observed_at=manifest["observed_at"],
                            reference=str(path), reason=reason, capture_ids=[capture_id], observations=observations)
    elif finished and (attempt["capture_status"] == "provider_confirmed_no_output" or reconcile):
        # A finished paid run is never repeated; a second failed read of its
        # output ends the question with the controller's reason.
        envelope = _receipt(context, state="failed", dispatch_started=True, observed_at=now().isoformat(),
                            reference=str(path), reason=result.get("code", "META_DEPTH_OUTPUT_UNAVAILABLE"))
    else:
        # Sent or possibly sent, or its bill not yet known: kept for resumption only.
        code = ("META_DEPTH_CHARGE_UNKNOWN" if attempt["remote_status"] and attempt["charge"] is None
                else result.get("code", "META_DEPTH_RUN_PENDING"))
        if reconcile:
            raise MetaDepthWait(code, next_retry_at=(now() + RUN_RECHECK).isoformat())
        raise SafetyError(code)
    folder.mkdir(parents=True, exist_ok=True)
    atomic_json(path, {"version": VERSION, "acquisition_id": aid, "request": request, "attempt_id": attempt_id,
                       "capture": envelope["capture_ids"][0] if envelope["capture_ids"] else None,
                       "observation_ids": envelope["observation_ids"], "envelope": envelope})
    return envelope


def create_meta_depth_handlers(memory, *, root=None, spend_root=None, controller_factory=None,
                               now=lambda: datetime.now(timezone.utc)):
    """Installed handlers for the deeper Meta route; nothing is read until called.

    ``root`` holds the private account files (the project by default) and
    ``spend_root`` the spend-watch records. Questions that cannot reserve wait
    before any callback (``source_access_preflight``); the programme's
    readiness sees the same wait (``source_access_hint``).
    """
    root = Path(root or PROJECT_ROOT)
    spend_root = Path(spend_root or root)
    if controller_factory is None:
        controllers = []
        def controller_factory():
            # One controller per process keeps its recent billing checks;
            # short ledger locks let research saves continue during a paid run.
            if not controllers:
                from .benchmark import MetaTrial
                controllers.append(MetaTrial(root, memory.journal, short_locks=True))
            return controllers[0]

    def collect(context, reconcile=False):
        return collect_depth(memory, context, root=root, spend_root=spend_root,
                             controller_factory=controller_factory, now=now, reconcile=reconcile)

    def hold(context):
        if context["reconciling"] or _ledger_attempt(memory.journal, attempt_id_for(context["acquisition_id"])):
            return None  # an existing attempt is only resumed, whatever the allowance
        from .accounts import load_account_registry
        # The question's own check time, so repeated readiness checks agree.
        instant = timestamp(context["as_of"]) if context.get("as_of") else now()
        prepared, _ = prepared_for(attempt_id_for(context["acquisition_id"]), _validated(context["request"])["query"])
        registry = load_account_registry(root)
        try:
            money_check(root, memory.journal, registry, prepared, now=instant)
        except MetaDepthWait as wait:
            if str(wait) == "META_DEPTH_PERIOD_RENEWAL_PENDING" and registry["pool_enabled"]:
                return None  # the handler records the new month itself (free reads only)
            return wait
        return None

    def preflight(context):
        wait = hold(context)
        show(spend_root, at=now().isoformat(), wait=wait, proceed=wait is None)
        if wait is not None:
            return {"action": "wait", "calm_wait": True, "reason": str(wait), "next_retry_at": wait.next_retry_at}
        return {"action": "collect"}

    def access_hint(context):
        wait = hold(context)
        return {"ready": wait is None, "local_saved": False, "bypass_public_pause": False,
                "reason": wait and str(wait), "next_retry_at": wait and wait.next_retry_at, "signal": {}}

    return {"handler": collect, "reconcile": lambda context: collect(context, reconcile=True),
            "source_access_preflight": preflight, "source_access_hint": access_hint}
