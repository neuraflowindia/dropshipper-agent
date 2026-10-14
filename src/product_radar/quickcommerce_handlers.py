"""Paid Blinkit, Zepto and Instamart product checks through the provider's API.

The provider's documented search (quickcommerceapi.com, one prepaid credit per
search) is the only quick-commerce route. The ordinary public pages blocked
every pilot read and never yielded verified local rows, so that free browser
route is deleted rather than kept as a fallback.

Before any paid call a question passes two gates, in this order:
* admission (``quickcommerce_plan.paid_question_hold``): its type still passes
  the checklist with Low/Medium crowding, and a place after the first is asked
  only after a relevant first-place result; a held question waits calmly with
  its reserved reason and is never sent;
* credits (``quickcommerce_credits.CreditGate``): one free balance check per
  batch; an unknown or used-up balance waits calmly.
An interrupted paid call is never repeated: its saved intent stays uncertain
until the original reply is found.
"""

from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from .models import SafetyError, digest, identifier, timestamp, utc_now
from .quickcommerce import ENDPOINT, PLATFORMS, adapter as funded_adapter, prepare_query
from .source_deadline import source_phase
from .storage import Journal, atomic_json, load_json, safe_path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 2 * 1024 * 1024
TRANSPORT = "funded-http"
HELD_RECHECK_SECONDS = 3600


def prepare_funded_query(platform, request):
    """Freeze the one paid provider read for a question's request."""
    if not isinstance(request, dict): raise SafetyError("QUICKCOMMERCE_QUERY_INVALID")
    actual = prepare_query(platform, request.get("query"), request.get("location"))
    url = ENDPOINT + "?" + urlencode({"platform": actual["provider_platform"], "q": actual["query"],
                                      "lat": actual["location"]["lat"], "lon": actual["location"]["lon"]})
    prepared = dict(actual, url=url, transport=TRANSPORT)
    if "url" in request or "transport" in request:
        if request != prepared: raise SafetyError("QUICKCOMMERCE_SOURCE_SCOPE_CHANGED")
    elif request != actual and (not set(request) <= {"query", "country", "limit", "location"}
            or request.get("country", "IN") != "IN" or request.get("limit", 50) != 50):
        raise SafetyError("QUICKCOMMERCE_SOURCE_SCOPE_CHANGED")
    return prepared


def _funded_response(client, request, response_path, intent_path, on_dispatch, *, saved_only=False, on_reply=None):
    """An interrupted canonical import cannot spend the same credit twice.

    ``on_reply`` sees each fresh provider reply once, after it is saved
    (credit accounting); a saved reply is never counted again."""
    with Journal(response_path.parent / "funded_response_lock").locked():
        if response_path.exists():
            saved = load_json(response_path, MAX_BYTES * 3)
            if saved.get("request") != request:
                raise SafetyError("QUICKCOMMERCE_CAPTURE_CONFLICT")
            return saved
        if intent_path.exists():
            intent = load_json(intent_path, MAX_BYTES)
            if intent.get("request_hash") != digest(request):
                raise SafetyError("QUICKCOMMERCE_CAPTURE_CONFLICT")
            if intent.get("state") == "dispatched":
                raise SafetyError("QUICKCOMMERCE_FUNDED_RESPONSE_UNCERTAIN")
        if saved_only:
            # A prepared intent cannot authorise a new credit-spending request
            # during reconciliation.
            raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
        atomic_json(intent_path, {"request_hash": digest(request), "state": "prepared", "at": utc_now()})
        def dispatched():
            atomic_json(intent_path, {"request_hash": digest(request), "state": "dispatched", "at": utc_now()})
            if on_dispatch:
                on_dispatch()
        result = client.search(request, on_dispatch=dispatched)
        atomic_json(response_path, result)
        atomic_json(intent_path, {"request_hash": digest(request), "state": "response_saved", "at": utc_now()})
        if on_reply is not None:
            on_reply(result)
        return result


def create_quickcommerce_handlers(memory, funded_client, *, spend_root=None):
    """The three paid routes; nothing is read or charged until a question is admitted.

    The spend watch records live under ``spend_root`` (the project by default).
    """
    from .quickcommerce import QuickCommerceClient
    from .quickcommerce_credits import CreditGate
    from .quickcommerce_plan import load_settings, paid_question_hold
    from .source_acquisitions import record_quickcommerce_response
    from .source_coverage import direct_source_resolution_hooks, guard_shared_source_launch, _record_marketplace_access
    if not isinstance(funded_client, QuickCommerceClient):
        raise SafetyError("QUICKCOMMERCE_SHARED_INSTALLED_CLIENT_REQUIRED")
    gate = CreditGate(Path(spend_root) if spend_root is not None else PROJECT_ROOT, funded_client)

    batch = {"at": None}

    def holds(context, request):
        """Admission first (the type may have changed while it waited), then credits.

        One saved-state read and one admission answer per type serve a whole
        source pass (known by its pass time), like the credit check."""
        at = context.get("as_of") or utc_now()
        if batch["at"] != at:
            batch.clear(); batch.update(at=at, state=memory.snapshot(), settings=load_settings(), types={})
        reason = paid_question_hold(batch["state"], context["concept_ids"], request["location"], as_of=at,
                                    settings=batch["settings"], cache=batch["types"])
        if reason:
            return {"action": "wait", "calm_wait": True, "reason": reason,
                    "next_retry_at": (timestamp(at) + timedelta(seconds=HELD_RECHECK_SECONDS)).isoformat()}
        wait = gate.admit(at)
        return wait and {"action": "wait", "calm_wait": True, **wait}

    handlers = {}
    for platform in PLATFORMS:
        def prepare(context, route=platform):
            return prepare_funded_query(route, context["request"])

        def access_hint(context, prepare=prepare):
            prepare(context)
            return {"ready": True, "reason": None, "next_retry_at": None, "bypass_public_pause": False,
                    "local_saved": False, "signal": {"funded_connected": True}}

        def preflight(context, resolver=None, prepare=prepare):
            request = prepare(context)
            if context.get("reconciling"):
                resolution = resolver(context)
                if resolution["action"] in {"saved", "wait"}: return resolution
                # Reserved but proved unstarted: fresh caller, admission and credits still required.
            return holds(context, request) or {"action": "collect"}

        def collect(context, *, reconcile=False, route=platform, prepare=prepare):
            request = prepare(context); aid = identifier(context["acquisition_id"])
            if not reconcile: guard_shared_source_launch(memory, context, request)
            base = prepare_query(route, request["query"], request["location"])
            folder = safe_path(memory.root, "quickcommerce_captures", aid)
            response_path, intent_path = folder / "original_response.json", folder / "funded_intent.json"
            class OriginalClient:
                def search(self, prepared, *, on_dispatch=None):
                    with source_phase("transport"):
                        return funded_client.search(prepared, on_dispatch=on_dispatch, retain_transport=True)
            class CachedClient:
                def search(self, prepared, *, on_dispatch=None):
                    return _funded_response(OriginalClient(), prepared, response_path, intent_path,
                                            on_dispatch, saved_only=reconcile, on_reply=gate.record)
            result = funded_adapter(memory, CachedClient())(dict(context, request=base))
            commit_at = max(context["as_of"], utc_now(timespec="microseconds"), key=timestamp)
            response = record_quickcommerce_response(memory, aid, reference=result["reference"],
                capture_ids=result["capture_ids"], as_of=commit_at)
            _record_marketplace_access(memory, aid, response["response_id"], as_of=commit_at)
            return result

        hooks = direct_source_resolution_hooks(memory, route=platform, root=memory.root,
            prepare=prepare, saved=lambda context, collect=collect: collect(context, reconcile=True), access_hint=access_hint)
        handlers[platform] = {"handler": collect, "reconcile": lambda context, collect=collect: collect(context, reconcile=True),
            "source_access_preflight": lambda context, preflight=preflight, resolver=hooks["resolve_source"]: preflight(context, resolver=resolver),
            "source_access_hint": access_hint, **hooks}
    return handlers
