"""Free public attempts composed with the existing funded commerce reader.

The ordinary-page pilot found barriers on all three platforms. Consequently
this module does not claim a working free product parser: a readable page with
unverified location/rows stays pending. The funded adapter remains responsible
for actual location-bound catalogue capture/import. Public barriers are saved
once and cool down by platform, rather than being visited for every product.
"""

from copy import deepcopy
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .quickcommerce import HOSTS, PLATFORMS, adapter as funded_adapter, prepare_query
from .source_deadline import replay_native_deadline, source_phase
from .storage import Journal, atomic_json, load_json, safe_path

VERSION = "quickcommerce-free-probe-v1"
MAX_BYTES = 2 * 1024 * 1024
COOLDOWN_SECONDS = 1800
ACCESS_CONTRACT = "quickcommerce-public-wait-v1"
SNAPSHOT = """JSON.stringify({url:location.href,title:document.title.slice(0,2000),
 text:(document.body?.innerText||'').slice(0,12000),
 location_widgets:Array.from(document.querySelectorAll('[data-testid*=location],[data-testid*=address],header button'))
   .map(e=>(e.innerText||e.getAttribute('aria-label')||'').slice(0,500)).slice(0,15)})"""


def public_search_url(platform, query, location):
    request = prepare_query(platform, query, location)
    parameter = "q" if platform == "blinkit" else "query"
    base = {"blinkit": "https://blinkit.com/s/", "zepto": "https://www.zepto.com/search",
            "instamart": "https://www.swiggy.com/instamart/search"}[platform]
    return base + "?" + urlencode({parameter: request["query"]})


def prepare_single_source_query(platform, request, *, prefer_funded=False):
    """Freeze one actual reader, not a fictitious mixed public/provider read."""
    from .quickcommerce import ENDPOINT
    if not isinstance(request, dict): raise SafetyError("QUICKCOMMERCE_QUERY_INVALID")
    actual = prepare_query(platform, request.get("query"), request.get("location"))
    transport = request.get("transport", "funded-http" if prefer_funded else "public-browser")
    if transport not in {"funded-http", "public-browser"}: raise SafetyError("QUICKCOMMERCE_SOURCE_TRANSPORT_INVALID")
    url = (ENDPOINT + "?" + urlencode({"platform": actual["provider_platform"], "q": actual["query"],
            "lat": actual["location"]["lat"], "lon": actual["location"]["lon"]}) if transport == "funded-http" else
           public_search_url(platform, actual["query"], actual["location"]))
    prepared = dict(actual, url=url, transport=transport)
    if "url" in request or "transport" in request:
        if request != prepared: raise SafetyError("QUICKCOMMERCE_SOURCE_SCOPE_CHANGED")
    elif request != actual and (not set(request) <= {"query", "country", "limit", "location"}
            or request.get("country", "IN") != "IN" or request.get("limit", 50) != 50):
        raise SafetyError("QUICKCOMMERCE_SOURCE_SCOPE_CHANGED")
    return prepared


def _guard(root, platform):
    return safe_path(root, "quickcommerce_free", "route_guards", platform)


def _retry_header(value):
    return value if (isinstance(value, str) and len(value) <= 256
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)) else None


def _health_deadline(retry_after, observed, cooldown_seconds):
    # The existing source policy owns seconds/date parsing. This public-only
    # platform guard does not imply that the funded reader has been blocked.
    from .source_coverage import _source_retry_at
    return _source_retry_at(retry_after, observed, fallback_seconds=cooldown_seconds)


def _checked_health(health, platform):
    """Recheck the actual original refusal before suppressing/new navigation.

    Older pilot records remain readable. Their missing process-exit proof is
    not invented when the deadline passes. A modified/missing original guard
    is a local proof problem, not permission to try the website again.
    """
    try:
        if (not isinstance(health, dict) or health.get("version") != VERSION
                or health.get("platform") != platform or health.get("http_status") not in (401, 403, 429)
                or timestamp(health["blocked_until"]) < timestamp(health["observed_at"])):
            raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
        original = load_json(safe_path(health["reference"]), MAX_BYTES)
        if not isinstance(original, dict):
            raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
        record = original.get("record") if isinstance(original, dict) else None
        if record is not None:
            if (not isinstance(record, dict) or original.get("version") != VERSION
                    or original.get("record_hash") != digest(record) or record.get("dispatch_started") is not True):
                raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
            request = original.get("request")
            if (not isinstance(request, dict) or request.get("platform") != platform
                    or prepare_query(platform, request.get("query"), request.get("location")) != request
                    or record.get("requested_url") != public_search_url(platform, request["query"], request["location"])):
                raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
            identifier(original.get("acquisition_id"))
            native, requested = record.get("native"), record.get("requested_url")
        else:
            native, requested = original.get("native"), original.get("requested_url")
        if (digest(original) != health["reference_hash"] or not isinstance(native, dict)
                or native.get("navigation_started") is not True
                or native.get("navigation_status") != health["http_status"]
                or native.get("completed_at") != health["observed_at"]
                or urlsplit(requested or "").hostname not in HOSTS[platform]):
            raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
        header = _retry_header(native.get("retry_after"))
        if "cooldown_seconds" in health:
            cooldown = health["cooldown_seconds"]
            if (type(cooldown) is not int or cooldown < 1 or health.get("retry_after") != header
                    or health["blocked_until"] != _health_deadline(header, health["observed_at"], cooldown)):
                raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
        elif header is not None and timestamp(health["blocked_until"]) < timestamp(
                _health_deadline(header, health["observed_at"], 1)):
            raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
        return native
    except (KeyError, TypeError, ValueError, OSError, SafetyError):
        raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID") from None


def _save_health(path, record, platform):
    previous = load_json(path, MAX_BYTES) if path.exists() else None
    if previous is not None:
        _checked_health(previous, platform)
        # A later shorter or older delayed refusal cannot shorten an actual
        # longer Retry-After. Keep that original proof rather than combining
        # two receipts into an invented observation date/deadline.
        if timestamp(previous["blocked_until"]) > timestamp(record["blocked_until"]):
            return previous
    atomic_json(path, record)
    return record


def _pending_public_probe(health, platform):
    probe = health.get("recovery_probe") if isinstance(health, dict) else None
    if probe is None:
        return None
    if not isinstance(probe, dict) or set(probe) != {"acquisition_id", "intent_reference", "intent_hash"}:
        raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
    identifier(probe["acquisition_id"])
    intent = load_json(safe_path(probe["intent_reference"]), MAX_BYTES)
    if not isinstance(intent, dict):
        raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
    request = intent.get("request", {}) if isinstance(intent, dict) else {}
    if (intent.get("version") != VERSION or intent.get("acquisition_id") != probe["acquisition_id"]
            or digest(intent) != probe["intent_hash"]
            or prepare_query(platform, request.get("query"), request.get("location")) != request
            or intent.get("requested_url") != public_search_url(platform, request["query"], request["location"])):
        raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
    return probe


def _finish_public_probe(health_path, acquisition_id, record, platform):
    if not health_path.is_file():
        return
    health = load_json(health_path, MAX_BYTES)
    if not isinstance(health, dict) or health.get("recovery_probe") is None:
        return
    _checked_health(health, platform)
    probe = _pending_public_probe(health, platform)
    native = record.get("native") or {}
    if (probe and probe["acquisition_id"] == acquisition_id and native.get("owned_process_exited") is True
            and (record.get("dispatch_started") is False or
                 record.get("dispatch_started") is True and native.get("navigation_status") == 200)):
        health = dict(health)
        health.pop("recovery_probe")
        atomic_json(health_path, health)


def _public_original(reference, platform, acquisition_id=None):
    """A public-only receipt is not a failed funded-provider receipt."""
    value = load_json(safe_path(reference), MAX_BYTES)
    if not isinstance(value, dict):
        raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
    record = value.get("record", {}) if isinstance(value, dict) else {}
    request = value.get("request", {}) if isinstance(value, dict) else {}
    if (not isinstance(record, dict) or value.get("version") != VERSION or value.get("record_hash") != digest(record)
            or acquisition_id is not None and value.get("acquisition_id") != acquisition_id
            or prepare_query(platform, request.get("query"), request.get("location")) != request):
        raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
    return value


def validate_public_access_wait(value, platform, acquisition_id=None):
    """Validate the exact original public refusal, including known-zero skips."""
    if not isinstance(value, dict) or value.get("contract") != ACCESS_CONTRACT:
        raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
    health = value.get("health")
    native = _checked_health(health, platform)
    pending = _pending_public_probe(health, platform)
    original = _public_original(value.get("probe_reference"), platform, acquisition_id)
    record = original["record"]
    if value.get("probe_hash") != digest(original) or type(value.get("guard_skipped")) is not bool:
        raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
    if value["guard_skipped"]:
        if (record.get("dispatch_started") is not False or record.get("state") != "not_attempted"
                or record.get("native") is not None or record.get("route_health_reference") != health["reference"]
                or record.get("observed_at") != health["observed_at"]
                or record.get("http_status") != health["http_status"]):
            raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
    elif (record.get("dispatch_started") is not True or record.get("state") != "blocked"
            or not isinstance(record.get("native"), dict)
            or record["native"].get("navigation_status") not in (401, 403, 429)
            or record["native"].get("navigation_started") is not True
            or record["native"].get("completed_at") != record.get("observed_at")
            or record.get("requested_url") != public_search_url(platform, original["request"]["query"], original["request"]["location"])):
        raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
    return {"next_retry_at": health["blocked_until"] if native.get("owned_process_exited") is True and not pending else None,
            "guard_skipped": value["guard_skipped"], "health": deepcopy(health)}


def _public_wait_projection(root, platform, public, cooldown_seconds):
    record = public["record"]
    if record.get("state") != "blocked" and not record.get("route_health_reference"):
        return None
    path = Path(public["reference"]).parent / "access_wait.json"
    original = _public_original(public["reference"], platform)
    if path.is_file():
        value = load_json(path, MAX_BYTES)
    else:
        try:
            health = load_json(_guard(root, platform) / "health.json", MAX_BYTES)
            _checked_health(health, platform)
        except (SafetyError, OSError):
            if record.get("state") != "blocked" or record.get("dispatch_started") is not True:
                raise
            # Recover the original blocked read itself before consulting an
            # unrelated damaged guard. Its date/header are not recollected.
            health = {"version": VERSION, "platform": platform, "observed_at": record["observed_at"],
                "http_status": record["http_status"], "reference": public["reference"],
                "reference_hash": digest(original), "retry_after": record.get("retry_after"),
                "cooldown_seconds": cooldown_seconds,
                "blocked_until": _health_deadline(record.get("retry_after"), record["observed_at"], cooldown_seconds)}
        value = {"contract": ACCESS_CONTRACT, "health": health,
                 "probe_reference": public["reference"], "probe_hash": digest(original),
                 "guard_skipped": record.get("dispatch_started") is False}
        validate_public_access_wait(value, platform, original["acquisition_id"])
        atomic_json(path, value)
    validate_public_access_wait(value, platform, original["acquisition_id"])
    return value


def _proved_public_only(root, envelope, platform):
    if not isinstance(envelope, dict) or envelope.get("route") != platform:
        return False
    try:
        value = _public_original(envelope.get("reference"), platform, envelope.get("acquisition_id"))
        projection = envelope.get("public_access_wait")
        if projection is not None:
            validate_public_access_wait(projection, platform, envelope.get("acquisition_id"))
        else:
            # A legacy wrapper has no new projection. Its still-retained
            # actual public guard must bind the same original bytes instead.
            health = load_json(_guard(root, platform) / "health.json", MAX_BYTES)
            _checked_health(health, platform)
            if health["reference"] != envelope["reference"] or health["reference_hash"] != digest(value):
                return False
        record = value["record"]
        return (record.get("state") == "blocked" and record.get("dispatch_started") is True
                and record.get("native", {}).get("navigation_status") in (401, 403, 429))
    except (SafetyError, OSError, TypeError, ValueError):
        return False


def note_route_failure(root, platform, reference, *, cooldown_seconds=COOLDOWN_SECONDS):
    """Adopt a real pilot barrier as route health, not another product's check."""
    if platform not in PLATFORMS or type(cooldown_seconds) is not int or cooldown_seconds < 1:
        raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
    path = safe_path(Path(reference).absolute())
    if not path.is_file():
        raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
    value = load_json(path, MAX_BYTES)
    native = value.get("native") if isinstance(value, dict) else None
    requested = value.get("requested_url") if isinstance(value, dict) else None
    if (not isinstance(native, dict) or native.get("navigation_started") is not True
            or native.get("navigation_status") not in (401, 403, 429)
            or urlsplit(requested or "").hostname not in HOSTS[platform]):
        raise SafetyError("QUICKCOMMERCE_FREE_GUARD_INVALID")
    at = native.get("completed_at"); timestamp(at)
    header = _retry_header(native.get("retry_after"))
    record = {"version": VERSION, "platform": platform, "observed_at": at,
              "http_status": native["navigation_status"], "reference": str(path), "reference_hash": digest(value),
              "blocked_until": _health_deadline(header, at, cooldown_seconds),
              "retry_after": header, "cooldown_seconds": cooldown_seconds}
    folder = _guard(root, platform)
    with Journal(folder).locked():
        previous_path = folder / "health.json"
        _checked_health(record, platform)
        return _save_health(previous_path, record, platform)


def capture_free_probe(root, acquisition_id, request, *, native_capture=None, on_dispatch=None,
                       cooldown_seconds=COOLDOWN_SECONDS, saved_only=False, respect_access_waits=False):
    """One ordinary navigation or truthful saved route-health skip."""
    identifier(acquisition_id)
    platform = request.get("platform") if isinstance(request, dict) else None
    if (prepare_query(platform, request.get("query"), request.get("location")) != request
            or on_dispatch is not None and not callable(on_dispatch)
            or type(cooldown_seconds) is not int or cooldown_seconds < 1
            or type(respect_access_waits) is not bool):
        raise SafetyError("QUICKCOMMERCE_FREE_REQUEST_INVALID")
    folder = safe_path(root, "quickcommerce_free", "acquisitions", acquisition_id)
    path = folder / "probe.json"
    with Journal(_guard(root, platform)).locked():
        native_folder = safe_path(folder, "native")
        native_path = native_folder / "native_result.json"
        native_deadline = native_folder / "native_outer_deadline.json"
        url = public_search_url(platform, request["query"], request["location"])
        expected_intent = {"version": VERSION, "acquisition_id": acquisition_id,
                           "request": deepcopy(request), "requested_url": url}
        intent_path = folder / "intent.json"
        if intent_path.exists() and load_json(intent_path, MAX_BYTES) != expected_intent:
            raise SafetyError("QUICKCOMMERCE_FREE_CAPTURE_CONFLICT")
        if path.exists():
            saved = load_json(path, MAX_BYTES)
            if (saved.get("version") != VERSION or saved.get("request") != request
                    or saved.get("acquisition_id") != acquisition_id or saved.get("record_hash") != digest(saved["record"])):
                raise SafetyError("QUICKCOMMERCE_FREE_CAPTURE_CONFLICT")
            if saved["record"].get("reason") == "SOURCE_ACTION_TIMEOUT":
                original = replay_native_deadline(native_folder, requested_url=url)
                if (not intent_path.exists() or saved["record"].get("native") != original
                        or saved["record"].get("dispatch_started") != original["navigation_started"]
                        or saved["record"].get("state") != "failed"):
                    raise SafetyError("QUICKCOMMERCE_FREE_CAPTURE_CONFLICT")
            return {"reference": str(path), "record": deepcopy(saved["record"]), "reused": True}
        if saved_only and not native_path.exists() and not native_deadline.exists():
            raise SafetyError("QUICKCOMMERCE_SAVED_CAPTURE_REQUIRED")
        health_path = _guard(root, platform) / "health.json"
        health = load_json(health_path, MAX_BYTES) if health_path.exists() else None
        now = utc_now()
        waiting = False
        if not native_path.exists() and not native_deadline.exists() and health:
            original = _checked_health(health, platform)
            pending = _pending_public_probe(health, platform) if respect_access_waits else None
            waiting = (timestamp(now) < timestamp(health["blocked_until"])
                       or original.get("owned_process_exited") is not True or pending is not None)
        if waiting:
            record = {"observed_at": health["observed_at"], "state": "not_attempted", "dispatch_started": False,
                      "reason": "The earlier public recovery read still needs its original saved result; the funded route can continue." if pending else
                          "The public platform recently blocked a read; the funded route can continue." if
                          timestamp(now) < timestamp(health["blocked_until"]) else
                          "The earlier public read has no confirmed process exit; the funded route can continue.",
                      "route_health_reference": health["reference"], "http_status": health["http_status"],
                      "native": None, "location_established": False}
        else:
            if native_capture is None:
                from .native_public_browser import native_snapshot
                native_capture = native_snapshot
            native_folder.mkdir(parents=True, exist_ok=True)
            # If a process stopped after navigation, use its actual saved
            # native result rather than launching another browser.
            if native_deadline.exists():
                if not intent_path.exists():
                    raise SafetyError("QUICKCOMMERCE_FREE_CAPTURE_CONFLICT")
                native = replay_native_deadline(native_folder, requested_url=url)
            elif native_path.exists():
                native = load_json(native_path, MAX_BYTES)
            else:
                if not intent_path.exists():
                    atomic_json(intent_path, expected_intent)
                if respect_access_waits and health is not None:
                    # The platform lock pins this one due recovery to its
                    # real original intent before entering the capture leaf.
                    # An uncertain outcome cannot be replaced by a keyword.
                    health = dict(health, recovery_probe={"acquisition_id": acquisition_id,
                        "intent_reference": str(intent_path), "intent_hash": digest(expected_intent)})
                    atomic_json(health_path, health)
                try:
                    with source_phase("transport"):
                        native = native_capture(url, SNAPSHOT, native_folder)
                except Exception as error:
                    native = {"state": "failed", "navigation_started": None, "completed_at": now,
                              "reason": str(error) if isinstance(error, SafetyError) else "QUICKCOMMERCE_FREE_STARTUP_FAILED"}
            actual = native.get("navigation_started")
            actual = actual if actual is True or actual is False else None
            status = native.get("navigation_status")
            observed = native.get("completed_at") or now; timestamp(observed)
            blocked = actual and status in (401, 403, 429)
            cancelled = native.get("outer_deadline") is True
            record = {"observed_at": observed, "state": "failed" if cancelled else "blocked" if blocked else "insufficient" if actual else
                        "failed" if actual is None else "not_attempted",
                      "dispatch_started": actual, "http_status": status, "requested_url": url,
                      "native": native, "location_established": False,
                      "reason": "SOURCE_ACTION_TIMEOUT" if cancelled else "The public platform blocked the browser read." if blocked else
                                "The public page did not provide verified local product results." if actual else
                                "The public browser's navigation outcome is uncertain; its saved execution record needs review." if actual is None else
                                "The public browser did not start a platform request."}
            header = _retry_header(native.get("retry_after"))
            if header is not None:
                record["retry_after"] = header
            # Location names appearing in footer city lists are not a verified
            # delivery location. No rows or empty verdicts are inferred here.
        saved = {"version": VERSION, "acquisition_id": acquisition_id, "request": deepcopy(request),
                 "record": record, "record_hash": digest(record)}
        if len(canonical(saved)) > MAX_BYTES:
            raise SafetyError("QUICKCOMMERCE_FREE_CAPTURE_TOO_LARGE")
        atomic_json(path, saved)
        if record["dispatch_started"] and record["http_status"] in (401, 403, 429):
            _save_health(health_path, {"version": VERSION, "platform": platform,
                "observed_at": record["observed_at"], "http_status": record["http_status"], "reference": str(path),
                "reference_hash": digest(saved), "retry_after": record.get("retry_after"), "cooldown_seconds": cooldown_seconds,
                "blocked_until": _health_deadline(record.get("retry_after"), record["observed_at"], cooldown_seconds)}, platform)
        elif respect_access_waits:
            _finish_public_probe(health_path, acquisition_id, record, platform)
        if record["dispatch_started"] and on_dispatch:
            on_dispatch()
        return {"reference": str(path), "record": deepcopy(record), "reused": False}


def _funded_response(client, request, response_path, intent_path, on_dispatch, *, saved_only=False):
    """An interrupted canonical import cannot spend the same credit twice."""
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
            # A completed public probe or prepared funded intent cannot
            # authorise a new credit-spending request during reconciliation.
            raise SafetyError("SOURCE_ORIGINAL_RECEIPT_PENDING")
        atomic_json(intent_path, {"request_hash": digest(request), "state": "prepared", "at": utc_now()})
        def dispatched():
            atomic_json(intent_path, {"request_hash": digest(request), "state": "dispatched", "at": utc_now()})
            if on_dispatch:
                on_dispatch()
        result = client.search(request, on_dispatch=dispatched)
        atomic_json(response_path, result)
        atomic_json(intent_path, {"request_hash": digest(request), "state": "response_saved", "at": utc_now()})
        return result


def _combine_stages(result, public, request):
    """One real public attempt is not erased by a later prelaunch failure."""
    record = public["record"]
    stages = {"public": {"reference": public["reference"], "state": record["state"],
                          "dispatch_started": record["dispatch_started"], "observed_at": record["observed_at"]},
              "funded": {"reference": result["reference"], "state": result["state"],
                          "dispatch_started": result["dispatch_started"], "observed_at": result["observed_at"]}}
    flags = [stage["dispatch_started"] for stage in stages.values()]
    overall = True if any(value is True for value in flags) else None if any(value is None for value in flags) else False
    combined_path = Path(public["reference"]).parent / "stages.json"
    combined = {"version": VERSION, "acquisition_id": result["acquisition_id"], "request": request,
                "stages": stages, "dispatch_started": overall}
    if combined_path.exists():
        if load_json(combined_path, MAX_BYTES) != combined:
            raise SafetyError("QUICKCOMMERCE_STAGE_CAPTURE_CONFLICT")
    else:
        atomic_json(combined_path, combined)
    targets = deepcopy(result["target_results"])
    if result["dispatch_started"] is False and overall is not False:
        for target in targets:
            if target["state"] == "not_attempted":
                target.update(state="blocked" if result["state"] == "blocked" else "failed",
                              reason=record["reason"] + " " + target["reason"])
    return dict(result, dispatch_started=overall, target_results=targets, reference=str(combined_path),
                observed_at=max((stage["observed_at"] for stage in stages.values()), key=timestamp),
                free_attempt_reference=public["reference"], free_route_state=record["state"], stages=stages)


def _shared_quickcommerce_handlers(memory, funded_client, native_capture, cooldown_seconds):
    """Opt-in single-leaf ownership; the legacy fallback workflow is untouched."""
    from .quickcommerce import QuickCommerceClient
    from .source_coverage import (direct_source_resolution_hooks, guard_shared_source_launch,
                                  _record_marketplace_access)
    from .source_acquisitions import record_quickcommerce_response
    if funded_client is not None and not isinstance(funded_client, QuickCommerceClient):
        raise SafetyError("QUICKCOMMERCE_SHARED_INSTALLED_CLIENT_REQUIRED")
    handlers = {}
    for platform in PLATFORMS:
        def prepare(context, route=platform):
            return prepare_single_source_query(route, context["request"], prefer_funded=funded_client is not None)

        def access_hint(context, route=platform, prepare=prepare):
            request = prepare(context); at = context.get("as_of") or utc_now()
            if request["transport"] == "funded-http":
                latest, pause = context.get("task_receipt"), context.get("pause_receipt")
                bypass = bool(funded_client is not None and (latest is not None or pause is not None)
                    and (latest is None or _proved_public_only(memory.root, latest, route))
                    and (pause is None or _proved_public_only(memory.root, pause, route)))
                return {"ready": funded_client is not None, "reason": None if funded_client is not None else "QUICKCOMMERCE_FUNDED_CONNECTION_REQUIRED",
                        "next_retry_at": None, "bypass_public_pause": bypass, "local_saved": False,
                        "signal": {"funded_connected": funded_client is not None}}
            health_path = _guard(memory.root, route) / "health.json"
            health = load_json(health_path, MAX_BYTES) if health_path.is_file() else None
            native = _checked_health(health, route) if health else None
            pending = _pending_public_probe(health, route) if health else None
            waiting = bool(health and (timestamp(at) < timestamp(health["blocked_until"])
                or native.get("owned_process_exited") is not True or pending is not None))
            return {"ready": not waiting, "reason": "QUICKCOMMERCE_PUBLIC_ACCESS_WAIT" if waiting else None,
                "next_retry_at": health["blocked_until"] if waiting and not pending and native.get("owned_process_exited") is True else None,
                "signal": {"health": digest(health) if health else None, "funded_connected": funded_client is not None}}

        def preflight(context, hint=access_hint, resolver=None):
            advice = hint(context)
            if context.get("reconciling"):
                resolution = resolver(context)
                if resolution["action"] in {"saved", "wait"}: return resolution
                return {"action": "collect"}  # Reserved but proved unstarted: fresh caller admission still required.
            return ({"action": "collect", "bypass_public_pause": advice.get("bypass_public_pause", False)} if advice["ready"] else
                    {"action": "wait", "reason": advice["reason"], "next_retry_at": advice.get("next_retry_at")})

        def collect(context, *, reconcile=False, route=platform, prepare=prepare):
            request = prepare(context); aid = identifier(context["acquisition_id"])
            if not reconcile: guard_shared_source_launch(memory, context, request)
            base = prepare_query(route, request["query"], request["location"])
            if request["transport"] == "funded-http":
                folder = safe_path(memory.root, "quickcommerce_captures", aid)
                response_path, intent_path = folder / "original_response.json", folder / "funded_intent.json"
                class OriginalClient:
                    def search(self, prepared, *, on_dispatch=None):
                        if funded_client is None: raise SafetyError("QUICKCOMMERCE_FUNDED_CONNECTION_REQUIRED")
                        with source_phase("transport"):
                            return funded_client.search(prepared, on_dispatch=on_dispatch, retain_transport=True)
                class CachedClient:
                    def search(self, prepared, *, on_dispatch=None):
                        return _funded_response(OriginalClient(), prepared, response_path, intent_path,
                                                on_dispatch, saved_only=reconcile)
                result = funded_adapter(memory, CachedClient())(dict(context, request=base))
            else:
                public = capture_free_probe(memory.root, aid, base, native_capture=native_capture,
                    on_dispatch=context.get("record_dispatch"), cooldown_seconds=cooldown_seconds,
                    saved_only=reconcile, respect_access_waits=True)
                record = public["record"]
                result = {"schema_version": 1, "route": route, "acquisition_id": aid,
                    "observed_at": record["observed_at"], "state": record["state"] if record["state"] != "not_attempted" else "blocked",
                    "dispatch_started": record["dispatch_started"], "reference": public["reference"],
                    "capture_ids": [], "observation_ids": [], "reason": record["reason"],
                    "target_results": [{"concept_id": cid, "description_hash": context["description_hashes"][cid],
                        "state": "blocked" if record["state"] == "blocked" else "insufficient" if record["dispatch_started"] else "not_attempted",
                        "evidence_refs": [], "reason": record["reason"]} for cid in context["concept_ids"]],
                    "retryable": record.get("http_status") in (401, 403, 429),
                    "failure_scope": "route" if record["dispatch_started"] is True and record.get("http_status") in (401, 403, 429) else "question"}
                projection = _public_wait_projection(memory.root, route, public, cooldown_seconds)
                if projection is not None: result["public_access_wait"] = projection
            commit_at = max(context["as_of"], utc_now(), key=timestamp)
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


def create_quickcommerce_handlers(memory, funded_client=None, *, native_capture=None,
                                 cooldown_seconds=COOLDOWN_SECONDS, respect_access_waits=False, shared_acquisitions=False):
    """One truthful public attempt; funded capture is the working fallback."""
    if type(respect_access_waits) is not bool or type(shared_acquisitions) is not bool:
        raise SafetyError("QUICKCOMMERCE_ACCESS_OPTION_INVALID")
    if shared_acquisitions:
        return _shared_quickcommerce_handlers(memory, funded_client, native_capture, cooldown_seconds)
    handlers = {}
    for platform in PLATFORMS:
        def handle(context, route=platform):
            request = prepare_query(route, context["request"]["query"], context["request"]["location"])
            acquisition = identifier(context["acquisition_id"])
            funded_path = safe_path(memory.root, "quickcommerce_captures", acquisition) / "capture.json"
            if funded_path.exists():
                result = funded_adapter(memory, funded_client)(context)
                probe_path = safe_path(memory.root, "quickcommerce_free", "acquisitions", acquisition) / "probe.json"
                if probe_path.exists():
                    public = capture_free_probe(memory.root, acquisition, request, native_capture=native_capture, saved_only=True)
                    return _combine_stages(result, public, request)
                return result
            public = capture_free_probe(memory.root, acquisition, request, native_capture=native_capture,
                on_dispatch=context.get("record_dispatch"), cooldown_seconds=cooldown_seconds,
                saved_only=context.get("reconciling") is True, respect_access_waits=respect_access_waits)
            response_path = Path(public["reference"]).parent / "funded_response.json"
            if (funded_client is not None and public["record"].get("reason") != "SOURCE_ACTION_TIMEOUT"
                    and not (respect_access_waits and context.get("reconciling") is True and not response_path.exists())):
                # Retain successful public provider responses before canonical
                # import, and reuse them if that import was interrupted.
                intent_path = response_path.parent / "funded_intent.json"
                class CachedFundedClient:
                    def search(self, prepared, *, on_dispatch=None):
                        return _funded_response(funded_client, prepared, response_path, intent_path, on_dispatch,
                                                saved_only=context.get("reconciling") is True)
                result = funded_adapter(memory, CachedFundedClient())(context)
                return _combine_stages(result, public, request)
            record = public["record"]
            result = {"schema_version": 1, "route": route, "acquisition_id": acquisition,
                    "observed_at": record["observed_at"], "state": record["state"] if record["state"] != "not_attempted" else
                        "blocked" if record["http_status"] in (403, 429) else "insufficient",
                    "dispatch_started": record["dispatch_started"], "reference": public["reference"],
                    "capture_ids": [], "observation_ids": [],
                    "target_results": [{"concept_id": cid, "description_hash": context["description_hashes"][cid],
                        "state": "blocked" if record["state"] == "blocked" else
                                 "failed" if record["reason"] == "SOURCE_ACTION_TIMEOUT" else
                                 "failed" if record["dispatch_started"] is None else
                                 "insufficient" if record["dispatch_started"] else "not_attempted",
                        "evidence_refs": [], "reason": record["reason"]} for cid in context["concept_ids"]],
                    "reason": record["reason"], "retryable": record["http_status"] in ((401, 403, 429) if respect_access_waits else (403, 429)),
                    "failure_scope": "route" if record["dispatch_started"] is True and record["http_status"] in (401, 403, 429) else "question"}
            if respect_access_waits:
                projection = _public_wait_projection(memory.root, route, public, cooldown_seconds)
                if projection is not None:
                    result["public_access_wait"] = projection
            return result

        def access_hint(context, route=platform):
            prepare_query(route, context["request"]["query"], context["request"]["location"])
            at = context.get("as_of") or utc_now(); timestamp(at)
            acquisition = context.get("original_acquisition_id") or context.get("acquisition_id")
            files = []
            if acquisition:
                identifier(acquisition)
                public = safe_path(memory.root, "quickcommerce_free", "acquisitions", acquisition)
                for path in (safe_path(memory.root, "quickcommerce_captures", acquisition) / "capture.json",
                             public / "probe.json", public / "funded_response.json",
                             public / "native/native_result.json", public / "native/native_outer_deadline.json"):
                    if path.is_file():
                        info = path.stat()
                        files.append([str(path), info.st_size, info.st_mtime_ns, info.st_ctime_ns])
            local_saved = bool(context.get("reconciling") and files)
            health_path = _guard(memory.root, route) / "health.json"
            health = load_json(health_path, MAX_BYTES) if health_path.is_file() and not local_saved else None
            native = _checked_health(health, route) if health else None
            pending = _pending_public_probe(health, route) if health else None
            waiting = bool(health and (timestamp(at) < timestamp(health["blocked_until"])
                                      or native.get("owned_process_exited") is not True or pending is not None))
            ready = local_saved if context.get("reconciling") else funded_client is not None or not waiting
            latest, pause = context.get("task_receipt"), context.get("pause_receipt")
            bypass = bool(funded_client is not None and (latest is not None or pause is not None)
                          and (latest is None or _proved_public_only(memory.root, latest, route))
                          and (pause is None or _proved_public_only(memory.root, pause, route)))
            return {"ready": ready, "local_saved": local_saved,
                    "bypass_public_pause": bypass,
                    "next_retry_at": health["blocked_until"] if waiting and not pending and native.get("owned_process_exited") is True
                                     and funded_client is None else None,
                    "reason": "The public read is waiting for its original refusal deadline or process exit." if not ready else None,
                    "signal": {"files": files, "health": digest(health) if health else None,
                               "funded_connected": funded_client is not None}}

        def preflight(context, route=platform, callback=handle, hint=access_hint):
            advice = hint(context)
            if context.get("reconciling"):
                if advice["local_saved"]:
                    original = context.get("original_acquisition_id") or context["acquisition_id"]
                    public = safe_path(memory.root, "quickcommerce_free", "acquisitions", original)
                    intent_path = public / "funded_intent.json"
                    if intent_path.is_file() and not (public / "funded_response.json").is_file():
                        intent = load_json(intent_path, MAX_BYTES)
                        prepared = prepare_query(route, context["request"]["query"], context["request"]["location"])
                        if intent.get("request_hash") != digest(prepared):
                            raise SafetyError("QUICKCOMMERCE_CAPTURE_CONFLICT")
                        if intent.get("state") == "dispatched":
                            return {"action": "wait", "reason": "QUICKCOMMERCE_FUNDED_RESPONSE_UNCERTAIN", "next_retry_at": None}
                    receipt = callback(dict(context, acquisition_id=original, reconciling=True))
                    return {"action": "saved", "acquisition_id": original, "receipt": receipt}
                return {"action": "wait", "reason": "SOURCE_ORIGINAL_RECEIPT_PENDING", "next_retry_at": None}
            if not advice["ready"]:
                return {"action": "wait", "reason": advice["reason"], "next_retry_at": advice["next_retry_at"]}
            return {"action": "collect", "bypass_public_pause": advice["bypass_public_pause"]}

        handlers[platform] = ({"handler": handle, "reconcile": lambda context, callback=handle: callback(dict(context, reconciling=True)),
                               "source_access_preflight": preflight,
                               "source_access_hint": access_hint} if respect_access_waits else handle)
    return handlers
