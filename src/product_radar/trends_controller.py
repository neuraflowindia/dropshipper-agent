"""Free, bounded Trends browser captures connected to the all-source controller.

Actual rendered India/year/Web Search scope, chart rows and original CSV must
agree before import. A readable no-data widget completes a check without
inventing poor demand. Acquisition replay never starts a replacement browser.
Old native/manual/Playwright packets retain their original acceptance paths.

The controller's handlers read Trends only in the owner's signed-in research
profile (``transport="signed-in"``, Plan R10 W2; ``trends_signed_in``): paced,
gated on the owner's sign-in, and waiting calmly (``SignedInWait``) whenever a
read cannot start. The hidden-browser and Playwright transports remain only
for the manual scripts until the signed-in route's live proof (W13).
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from .models import SafetyError, canonical, digest, identifier, timestamp
from .providers.trends_browser import MAX_BYTES, parse_trends_csv
from .source_deadline import (SourceActionDeadline, interrupted_transport, saved_completed_transport,
                              validate_interrupted_transport, source_phase, local_source_work)
from .runtime_storage import runtime_path
from .storage import load_json, safe_path
from .trends_capture import BLOCKED, WIDGET_JS, chart_comparison, explore_url, verified_scope

VERSION = "direct-trends-browser-v1"
DATA_ROOT = runtime_path(Path(__file__).resolve().parents[2], "data/research/direct-trends")
MAX_JSON = 2 * 1024 * 1024
MAX_HTML = 8 * 1024 * 1024
_WHOLE_SECOND_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})\Z")
_CONSENT_URL = re.compile(r"https://consent\.google\.[a-z.]+/", re.I)
_CONSENT_TEXT = re.compile(r"Before you continue to Google", re.I)
_LIMIT_TEXT = re.compile(r"Oops! Something went wrong|Something went wrong\.? Please try again", re.I)
_SIGN_IN_URL = re.compile(r"https://accounts\.google\.com/", re.I)
_SIGN_IN_TEXT = re.compile(r"sign in to continue", re.I)
GOOGLE_LIMIT = "DIRECT_TRENDS_GOOGLE_LIMIT"
TRANSPORTS = ("auto", "native", "playwright", "signed-in")
# A minimised signed-in page whose chart never appeared: a rendering problem.
MINIMISED_NOT_RENDERED = "TRENDS_MINIMISED_NOT_RENDERED"


def _wrapper_utc_now():
    """Keep wrapper precision comparable to the owned Windows native clock."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _time_within_wrapper(receipt, value, *, legacy_native=False):
    start, end, instant = map(timestamp, (receipt["started_at"], receipt["completed_at"], value))
    if end < start or instant < start:
        return False
    if legacy_native and _WHOLE_SECOND_TIME.fullmatch(receipt["completed_at"]):
        # Historical wrapper clocks truncated, rather than rounded, seconds.
        # This is their declared one-second interval, not a drift allowance.
        return instant < end + timedelta(seconds=1)
    return instant <= end

_NO_DATA_JS = r"""
 const noData = () => {
   const norm=s=>(s||'').replace(/\s+/g,' ').trim();
   const isTime=e=>/^Interest over time(?: help_outline)?$/.test(norm(e.innerText));
   const heading=Array.from(document.querySelectorAll('*')).find(e=>isTime(e)&&!Array.from(e.children).some(isTime));
   let node=heading;
   for(let n=0;node&&n<10&&!['BODY','HTML'].includes(node.tagName);n++,node=node.parentElement){
     if(Array.from(node.querySelectorAll('*')).some(e=>/^(Interest by subregion|Related topics|Related queries)(?: help_outline)?$/.test(norm(e.innerText)))) return null;
     const text=norm(node.innerText);
     const match=text.match(/(?:your search (?:doesn't|does not) have enough data to show here|not enough data for (?:this query|your search)|no data (?:available|for this query))/i);
     if(match) return {text:match[0],time_widget_present:true};
   }
   return null;
 };
"""

# The page's own account control: the signed-in avatar, or Google's sign-in
# link (``trends_signed_in.signed_in_marker``). Nothing is clicked.
_ACCOUNT_JS = r"""
 const account = () => ({
   avatar: !!document.querySelector('a[href*="accounts.google.com/SignOutOptions"],a[aria-label^="Google Account:"]'),
   sign_in_link: !!document.querySelector('a[href*="accounts.google.com/ServiceLogin"],a[href*="accounts.google.com/signin"],a[href*="accounts.google.com/v3/signin"],a[href*="accounts.google.com/AccountChooser"]')});
"""

# The rendered "Related queries" widget's own text (its selected Rising/Top
# list as shown; ``trends_rising`` reads it). The widget is the largest
# container holding that heading and no other widget's heading. Not clicked.
_RELATED_JS = r"""
 const related = () => {
   // Widget headings are found from visible text nodes only (no innerText of every element).
   const label=/^(Interest over time|Interest by subregion|Related topics|Related queries)(?: help_outline)?$/;
   const heads=[], walk=document.createTreeWalker(document.body||document.documentElement,NodeFilter.SHOW_TEXT);
   for(let n=walk.nextNode();n;n=walk.nextNode()){
     const e=n.parentElement;
     if(e&&label.test(norm(n.nodeValue))&&e.getClientRects().length&&!heads.includes(e)) heads.push(e);
   }
   const head=heads.find(e=>/^Related queries/.test(norm(e.textContent)));
   if(!head) return null;
   let node=head;
   while(node.parentElement&&!['BODY','HTML'].includes(node.parentElement.tagName)&&!heads.some(h=>h!==head&&node.parentElement.contains(h))) node=node.parentElement;
   return {text:(node.innerText||'').slice(0,8000)};
 };
"""

# Ready as soon as the page is a refusal, a consent or sign-in page, Google's
# in-chart error, a chart or an explicit no-data widget: never wait out the
# whole deadline on a page that will not change.
READY_JS = "(() => {" + WIDGET_JS + _NO_DATA_JS + r"""
 const text=document.body?.innerText||'';
 return location.hostname!=='trends.google.com'
   || /too many requests|too many queries|unusual traffic|captcha|not a robot|sign in to continue|before you continue to google|oops! something went wrong/i.test(text)
   || !!findChart() || !!noData();
})()"""

EXPORT_JS = "JSON.stringify((() => {" + WIDGET_JS + _NO_DATA_JS + _ACCOUNT_JS + _RELATED_JS + r"""
 const text=document.body?.innerText||'', chart=findChart(), empty=noData(), html=document.documentElement.outerHTML;
 const labels=new Set(text.split('\n').map(s=>s.replace(/\s+/g,' ').trim().toLowerCase()));
 const query=new URLSearchParams(location.search), term=query.get('q')||'';
 const scoped=location.origin==='https://trends.google.com'&&location.pathname==='/trends/explore'
   && query.get('geo')==='IN'&&(!query.get('date')||query.get('date')==='today 12-m')&&!query.get('gprop')
   && [term.toLowerCase(),'search term','india','past 12 months','all categories','web search'].every(s=>labels.has(s));
 const blocked=/too many requests|too many queries|unusual traffic|captcha|not a robot|sign in to continue/i.test(text);
 const result={url:location.href,title:document.title,text:text.slice(0,75000),
   rows:chart?Array.from(chart.table.querySelectorAll('tr')).map(r=>Array.from(r.querySelectorAll('th,td')).map(c=>c.textContent.trim())):[],
   download_found:!!chart,download_clicked:false,no_data_text:empty?.text||null,time_widget_present:!!chart||!!empty,
   html:html.length<=8388608?html:null,html_characters:html.length,account:account(),related:related()};
 // Click only the public time-chart CSV control that supplied these rows.
 // This does not log in, accept consent, or interact with an access challenge.
 if(chart&&scoped&&!blocked){chart.button.click();result.download_clicked=true;}
 return result;
})())"""


def sign_in_page(snapshot):
    """Google's sign-in page (a redirect to accounts.google.com, or "sign in to continue")."""
    return bool(isinstance(snapshot, dict) and (isinstance(snapshot.get("url"), str) and _SIGN_IN_URL.match(snapshot["url"])
                or isinstance(snapshot.get("text"), str) and _SIGN_IN_TEXT.search(snapshot["text"])))


def source_state(receipt):
    """The read's state as the controller sees it: Google's no-data widget is
    an empty search, and a minimised page that never drew its chart holds the
    route like a refusal (it is retried, never counted as an answer)."""
    if receipt["state"] == "no_data":
        return "empty"
    return "blocked" if receipt["reason"] == MINIMISED_NOT_RENDERED else receipt["state"]


def build_trends_request(query, *, country="IN"):
    explore_url(query)
    if country != "IN":
        raise SafetyError("DIRECT_TRENDS_REQUEST_INVALID")
    return {"query": query.strip(), "country": "IN"}


def _request(request):
    if not isinstance(request, dict) or set(request) != {"query", "country"}:
        raise SafetyError("DIRECT_TRENDS_REQUEST_INVALID")
    if build_trends_request(request["query"], country=request["country"]) != request:
        raise SafetyError("DIRECT_TRENDS_REQUEST_INVALID")
    return request


def _actual_scope(snapshot, request):
    try:
        actual = urlsplit(snapshot.get("url", ""))
        query = parse_qs(actual.query)
        return (actual.scheme == "https" and actual.netloc == "trends.google.com" and actual.path == "/trends/explore"
                and query.get("q") == [request["query"]] and query.get("geo") == ["IN"]
                and query.get("date", ["today 12-m"]) == ["today 12-m"] and not query.get("gprop")
                and verified_scope(snapshot, request["query"]))
    except (ValueError, TypeError):
        return False


def _interpret(snapshot, request, navigation_status, raw, observed_at):
    result = {"state": "insufficient", "reason": "DIRECT_TRENDS_SCOPE_UNVERIFIED", "scope_verified": False,
              "parsed": None, "chart_comparison": None}
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("text"), str)
            or len(snapshot["text"]) > 75000 or not isinstance(snapshot.get("rows", []), list)
            or len(snapshot.get("rows", [])) > 1001):
        return dict(result, state="failed", reason="DIRECT_TRENDS_SNAPSHOT_INVALID")
    if navigation_status not in (403, 429, 503) and sign_in_page(snapshot):
        # Google asks the research account to sign in again: the owner's wait
        # (signed_in_browser), held only briefly as an access refusal.
        return dict(result, state="blocked", reason="OWNER_SIGN_IN_REQUIRED")
    if navigation_status in (403, 429, 503) or BLOCKED.search(snapshot["text"]):
        return dict(result, state="blocked", reason="DIRECT_TRENDS_ACCESS_CHALLENGE")
    if (isinstance(snapshot.get("url"), str) and _CONSENT_URL.match(snapshot["url"])) or _CONSENT_TEXT.search(snapshot["text"]):
        # Google's consent page stands between every read and Trends: a refusal, never accepted here.
        return dict(result, state="blocked", reason="DIRECT_TRENDS_CONSENT_REQUIRED")
    if (_LIMIT_TEXT.search(snapshot["text"]) and not snapshot.get("download_found")
            and snapshot.get("time_widget_present") is not True):
        # Google's own in-chart error in place of the chart (its usage limit): a refusal.
        return dict(result, state="blocked", reason=GOOGLE_LIMIT)
    if not _actual_scope(snapshot, request):
        return result
    result["scope_verified"] = True
    if type(navigation_status) is not int or navigation_status != 200:
        return dict(result, reason="DIRECT_TRENDS_HTTP_STATUS_UNVERIFIED")
    if (snapshot.get("time_widget_present") is True and isinstance(snapshot.get("no_data_text"), str)
            and re.search(r"(?:your search (?:doesn't|does not) have enough data to show here|not enough data for (?:this query|your search)|no data (?:available|for this query))", snapshot["no_data_text"], re.I)
            and not snapshot.get("rows")):
        return dict(result, state="no_data", reason="DIRECT_TRENDS_EXPLICIT_NO_DATA")
    if raw is None or not snapshot.get("download_found") or not snapshot.get("rows"):
        return dict(result, reason="DIRECT_TRENDS_CHART_EXPORT_MISSING")
    metadata = {"term": request["query"], "geo": "IN", "time_range": "today 12-m", "kind": "search_term",
                "property": "web", "scope_verified": True, "collected_at": observed_at, "source_reference": snapshot["url"]}
    parsed = parse_trends_csv(raw, metadata)
    comparison = chart_comparison(parsed["series"], snapshot["rows"])
    result.update(parsed=parsed, chart_comparison=comparison)
    if parsed["state"] != "Usable":
        return dict(result, state="failed" if parsed["state"] == "Failed" else "insufficient", reason="DIRECT_TRENDS_EXPORT_NOT_USABLE")
    if not comparison["matches"]:
        return dict(result, reason="DIRECT_TRENDS_CHART_EXPORT_MISMATCH")
    return dict(result, state="usable", reason="DIRECT_TRENDS_CHART_EXPORT_VERIFIED")


@local_source_work("response_save")
def _write(path, raw):
    if path.exists() or path.is_symlink():
        raise SafetyError("DIRECT_TRENDS_ARTIFACT_EXISTS")
    with path.open("xb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def _playwright(request, folder, timeout, browser_path):
    from .trends_capture import capture
    receipt = capture(request["query"], "playwright", folder / "fallback", timeout=timeout, browser_path=browser_path)
    child = folder / "fallback/playwright"
    snapshot = load_json(child / "scope.json", 262144) if (child / "scope.json").is_file() else None
    raw = None
    if receipt.get("csv_path"):
        path = safe_path(receipt["csv_path"])
        if path != child / "multiTimeline.csv" or path.stat().st_size > MAX_BYTES:
            raise SafetyError("DIRECT_TRENDS_EXPORT_PATH_INVALID")
        raw = path.read_bytes()
    states = {"Blocked": "blocked", "Unavailable": "unavailable", "Failed": "failed"}
    return {"state": states.get(receipt["state"], "captured"), "reason": receipt["reason"],
            "navigation_status": receipt.get("navigation_status"), "navigation_started": receipt.get("navigation_started", False),
            "navigation_started_at": receipt.get("navigation_started_at"), "owned_process_exited": receipt.get("owned_browser_closed"),
            "completed_at": receipt["collected_at"], "snapshot": snapshot, "csv_bytes": raw,
            "fallback_receipt": receipt}


def capture_direct_trends(request, acquisition_id, *, data_root=DATA_ROOT, timeout=45, transport="auto",
                          browser_path=None, snapshot_reader=None, signed_in_root=None):
    """Capture first; import later. No paid fallback, challenge solving or model.

    ``transport="signed-in"`` reads in the owner's signed-in research profile
    (records under ``signed_in_root``) and never falls back to another
    browser. A read that could not start raises ``SignedInWait`` after its
    receipt is saved (nothing was requested).
    """
    _request(request); identifier(acquisition_id)
    if (type(timeout) is not int or not 20 <= timeout <= 45 or transport not in TRANSPORTS
            or signed_in_root is not None and transport != "signed-in"):
        raise SafetyError("DIRECT_TRENDS_CAPTURE_OPTIONS_INVALID")
    folder = safe_path(data_root, acquisition_id)
    intent = {"version": VERSION, "acquisition_id": acquisition_id, "request": deepcopy(request),
              "timeout": timeout, "transport": transport, "evidence_type": "synthetic" if snapshot_reader else "captured"}
    if folder.exists():
        receipt = replay_direct_trends(folder)
        if receipt["intent_hash"] != digest(intent): raise SafetyError("DIRECT_TRENDS_ACQUISITION_CONFLICT")
        return receipt
    folder.mkdir(parents=True, exist_ok=False)
    _write(folder / "intent.json", canonical(intent))
    receipt = {"version": VERSION, "acquisition_id": acquisition_id, "request": deepcopy(request), "route": "google-trends",
               "intent_hash": digest(intent), "folder": str(folder), "evidence_type": intent["evidence_type"],
               "started_at": _wrapper_utc_now(), "observed_at": None, "completed_at": None,
               "state": "failed", "reason": "DIRECT_TRENDS_CAPTURE_INCOMPLETE", "scope_verified": False,
               "dispatch_started": False, "dispatch_started_at": None, "artifacts": {}, "transport_attempts": [],
               "apify_requests": 0, "model_calls": 0, "provider_cost_usd": "0"}
    started = time.monotonic()
    deadline_error = calm_wait = None
    actual = None
    def save(name, raw, limit=MAX_JSON):
        if len(raw) > limit: raise SafetyError("DIRECT_TRENDS_CAPTURE_SIZE_LIMIT")
        _write(folder / name, raw); receipt["artifacts"][name] = sha256(raw).hexdigest()
    try:
        if snapshot_reader:
            with source_phase("transport"):
                actual = snapshot_reader(request)
            receipt["transport_attempts"].append("synthetic_fixture")
        else:
            actual = None
            if transport == "signed-in":
                from .signed_in_browser import PROJECT_ROOT
                from .trends_signed_in import signed_in_snapshot
                receipt["transport_attempts"].append("owned_native_windows_chrome")
                with source_phase("transport"):
                    actual = signed_in_snapshot(explore_url(request["query"]), EXPORT_JS, folder, timeout=timeout,
                                                ready_expression=READY_JS, download_folder=folder,
                                                root=PROJECT_ROOT if signed_in_root is None else signed_in_root)
            if transport in ("auto", "native"):
                from .native_public_browser import native_snapshot
                receipt["transport_attempts"].append("owned_native_windows_chrome")
                with source_phase("transport"):
                    actual = native_snapshot(explore_url(request["query"]), EXPORT_JS, folder, timeout=timeout,
                                             ready_expression=READY_JS, download_folder=folder)
            remaining = timeout - (time.monotonic() - started)
            if transport == "playwright" or (transport == "auto" and remaining >= 20 and actual.get("navigation_started") is False and actual.get("state") in ("failed", "unavailable")):
                receipt["transport_attempts"].append("owned_playwright_browser")
                with source_phase("transport"):
                    actual = _playwright(request, folder, min(timeout, int(remaining)), browser_path)
        snapshot = deepcopy(actual.get("snapshot"))
        raw = actual.get("csv_bytes")
        if raw is None and receipt["transport_attempts"][-1] == "owned_native_windows_chrome":
            files = list(folder.glob("*.csv"))
            if len(files) == 1 and not files[0].is_symlink() and files[0].stat().st_size <= MAX_BYTES:
                raw = files[0].read_bytes()
                if files[0].name != "multiTimeline.csv":
                    save("multiTimeline.csv", raw, MAX_BYTES)
                else:
                    receipt["artifacts"]["multiTimeline.csv"] = sha256(raw).hexdigest()
        receipt.update(observed_at=actual.get("completed_at") or _wrapper_utc_now(),
                       dispatch_started=actual.get("navigation_started") is True,
                       dispatch_started_at=actual.get("navigation_started_at"), navigation_status=actual.get("navigation_status"),
                       owned_process_exited=actual.get("owned_process_exited"), state=actual.get("state", "failed"),
                       reason=actual.get("reason", "DIRECT_TRENDS_TRANSPORT_FAILED"))
        dispatch_path = folder / "native_dispatch.json"
        if dispatch_path.is_file():
            dispatch = load_json(dispatch_path, 16384)
            if dispatch.get("url") != explore_url(request["query"]) or dispatch.get("navigation_started") is not True:
                raise SafetyError("DIRECT_TRENDS_DISPATCH_INVALID")
            timestamp(dispatch.get("navigation_started_at"))
            # A process failure after the actual navigation boundary must not
            # be relabelled "not tried" merely because its final output is lost.
            if receipt["transport_attempts"][-1] == "owned_native_windows_chrome":
                receipt.update(dispatch_started=True, dispatch_started_at=dispatch["navigation_started_at"])
                actual.update(navigation_started=True, navigation_started_at=dispatch["navigation_started_at"])
        save("transport.json", canonical({k: deepcopy(v) for k, v in actual.items() if k not in ("snapshot", "csv_bytes")}))
        if isinstance(raw, bytes) and "multiTimeline.csv" not in receipt["artifacts"]: save("multiTimeline.csv", raw, MAX_BYTES)
        if snapshot is not None:
            html = snapshot.pop("html", None)
            if isinstance(html, str): save("rendered.html", html.encode(), MAX_HTML)
            save("scope.json", canonical(snapshot))
            with source_phase("parse"):
                interpreted = _interpret(snapshot, request, receipt["navigation_status"], raw, receipt["observed_at"])
            save("interpretation.json", canonical(interpreted))
            receipt.update(scope_verified=interpreted["scope_verified"], interpretation_hash=digest(interpreted))
            if actual.get("state") == "captured" or (actual.get("state") == "blocked" and interpreted["state"] == "blocked"):
                # Keep the child's original transport explanation, but make a
                # new wrapper's independently confirmed block match its proof.
                receipt.update(state=interpreted["state"], reason=interpreted["reason"])
            if (transport == "signed-in" and receipt["state"] == "insufficient" and actual.get("page_visibility") == "hidden"
                    and actual.get("animation_frame") is False
                    and not snapshot.get("download_found") and snapshot.get("time_widget_present") is not True):
                # A minimised page that never drew its chart is a rendering
                # problem (W13 proof), not a refusal and not an empty search.
                receipt.update(state="failed", reason=MINIMISED_NOT_RENDERED)
        elif receipt["state"] == "captured":
            receipt.update(state="failed", reason="DIRECT_TRENDS_SNAPSHOT_MISSING")
        if receipt["state"] in ("usable", "no_data") and (not receipt["dispatch_started"] or receipt["owned_process_exited"] is not True):
            receipt.update(state="failed", reason="DIRECT_TRENDS_EXECUTION_UNVERIFIED")
        for name, limit in (("native_result.json", 12 * 1024 * 1024), ("native_dispatch.json", 16384), ("owned_process.json", 16384),
                            ("native_expression.js", 80000), ("native_ready.js", 80000)):
            path = folder / name
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= limit:
                receipt["artifacts"][name] = sha256(path.read_bytes()).hexdigest()
    except SourceActionDeadline as error:
        deadline_error = error
        proof = interrupted_transport(error, folder=folder, requested_url=explore_url(request["query"]), completed_transport=actual)
        receipt.update(outer_deadline=True, state="failed", reason="SOURCE_ACTION_TIMEOUT",
                       dispatch_started=proof["navigation_started"], dispatch_started_at=proof.get("navigation_started_at"),
                       owned_process_exited=proof.get("owned_process_exited"))
        try:
            save("outer_deadline.json", canonical(proof), 16384)
            for name in ("native_dispatch.json", "native_outer_deadline.json", "native_result.json", "multiTimeline.csv"):
                path = folder / name
                limit = 12 * 1024 * 1024 if name == "native_result.json" else MAX_BYTES if name == "multiTimeline.csv" else 16384
                if path.is_file() and not path.is_symlink() and path.stat().st_size <= limit:
                    receipt["artifacts"][name] = sha256(path.read_bytes()).hexdigest()
        except (SafetyError, OSError):
            receipt["deadline_proof_save_error"] = "SOURCE_DEADLINE_PROOF_SAVE_FAILED"
        raise
    except Exception as error:
        receipt.update(state="failed", reason=str(error) if isinstance(error, SafetyError) else "DIRECT_TRENDS_CAPTURE_FAILED", exception_type=type(error).__name__)
        if getattr(error, "calm_wait", False) is True:
            calm_wait = error  # nothing was requested: raised once the receipt is saved
    finally:
        receipt.update(completed_at=_wrapper_utc_now(), elapsed_seconds=round(time.monotonic() - started, 3))
        if receipt["observed_at"] is None: receipt["observed_at"] = receipt["completed_at"]
        try:
            _write(folder / "receipt.json", canonical(receipt))
        except (SafetyError, OSError):
            if deadline_error is None:
                raise
            raise deadline_error
    if calm_wait is not None:
        raise calm_wait
    return receipt


def replay_direct_trends(folder):
    """Verify original bytes, execution and interpretation; do not navigate."""
    folder = safe_path(folder)
    intent, receipt = load_json(folder / "intent.json", 16384), load_json(folder / "receipt.json", MAX_JSON)
    request = _request(intent.get("request"))
    deadline = receipt.get("outer_deadline") is True
    if (intent.get("version") != VERSION or receipt.get("version") != VERSION or receipt.get("route") != "google-trends"
            or receipt.get("request") != request or receipt.get("intent_hash") != digest(intent)
            or receipt.get("acquisition_id") != intent.get("acquisition_id") or receipt.get("evidence_type") != intent.get("evidence_type")
            or receipt.get("state") not in ("usable", "no_data", "insufficient", "blocked", "failed", "unavailable")
            or (type(receipt.get("dispatch_started")) is not bool and not (deadline and receipt.get("dispatch_started") is None))
            or not isinstance(receipt.get("artifacts"), dict)
            or receipt.get("apify_requests") != 0
            or receipt.get("model_calls") != 0 or receipt.get("provider_cost_usd") != "0"):
        raise SafetyError("DIRECT_TRENDS_RECEIPT_INVALID")
    identifier(receipt["acquisition_id"])
    limits = {"scope.json": MAX_JSON, "interpretation.json": MAX_JSON, "multiTimeline.csv": MAX_BYTES,
              "transport.json": MAX_JSON, "rendered.html": MAX_HTML, "native_result.json": 12 * 1024 * 1024,
              "owned_process.json": 16384, "native_dispatch.json": 16384,
              "native_expression.js": 80000, "native_ready.js": 80000,
              "native_outer_deadline.json": 16384, "outer_deadline.json": 16384}
    for name, expected in receipt["artifacts"].items():
        path = safe_path(folder / name)
        if name not in limits or not path.is_file() or path.stat().st_size > limits[name] or sha256(path.read_bytes()).hexdigest() != expected:
            raise SafetyError("DIRECT_TRENDS_ARTIFACT_HASH_MISMATCH")
    transport = load_json(folder / "transport.json", MAX_JSON) if "transport.json" in receipt["artifacts"] else None
    if deadline:
        if (receipt["state"] != "failed" or receipt["reason"] != "SOURCE_ACTION_TIMEOUT"
                or "outer_deadline.json" not in receipt["artifacts"]):
            raise SafetyError("DIRECT_TRENDS_RECEIPT_INVALID")
        proof = validate_interrupted_transport(load_json(folder / "outer_deadline.json", 16384), requested_url=explore_url(request["query"]))
        if (receipt["dispatch_started"] != proof["navigation_started"]
                or receipt.get("dispatch_started_at") != proof.get("navigation_started_at")
                or receipt.get("owned_process_exited") != proof.get("owned_process_exited")):
            raise SafetyError("DIRECT_TRENDS_TRANSPORT_MISMATCH")
        if proof["navigation_started"] is True and "native_dispatch.json" not in receipt["artifacts"]:
            raise SafetyError("DIRECT_TRENDS_DISPATCH_MISMATCH")
        if proof["navigation_started"] is False:
            if "native_outer_deadline.json" not in receipt["artifacts"]:
                raise SafetyError("DIRECT_TRENDS_DISPATCH_MISMATCH")
            native_deadline = load_json(folder / "native_outer_deadline.json", 16384)
            if native_deadline.get("launch_invoked") is not False or native_deadline.get("requested_url") != explore_url(request["query"]):
                raise SafetyError("DIRECT_TRENDS_DISPATCH_MISMATCH")
    elif transport:
        if (receipt["dispatch_started"] != (transport.get("navigation_started") is True)
                or receipt["dispatch_started_at"] != transport.get("navigation_started_at")
                or receipt["navigation_status"] != transport.get("navigation_status")
                or receipt["owned_process_exited"] != transport.get("owned_process_exited")):
            raise SafetyError("DIRECT_TRENDS_TRANSPORT_MISMATCH")
    native = load_json(folder / "native_result.json", limits["native_result.json"]) if "native_result.json" in receipt["artifacts"] else None
    legacy_native = bool(isinstance(native, dict) and isinstance(transport, dict)
        and receipt["transport_attempts"] and receipt["transport_attempts"][-1] == "owned_native_windows_chrome"
        and native.get("completed_at") == transport.get("completed_at") == receipt["observed_at"])
    if not _time_within_wrapper(receipt, receipt["observed_at"], legacy_native=legacy_native):
        raise SafetyError("DIRECT_TRENDS_RECEIPT_TIME_INVALID")
    if receipt["dispatch_started"] and (not receipt.get("dispatch_started_at")
            or not _time_within_wrapper(receipt, receipt["dispatch_started_at"],
                legacy_native=legacy_native and "native_dispatch.json" in receipt["artifacts"])
            or timestamp(receipt["dispatch_started_at"]) > timestamp(receipt["observed_at"])):
        raise SafetyError("DIRECT_TRENDS_DISPATCH_TIME_INVALID")
    if "native_dispatch.json" in receipt["artifacts"] and receipt["transport_attempts"][-1] == "owned_native_windows_chrome":
        dispatch = load_json(folder / "native_dispatch.json", 16384)
        if (not receipt["dispatch_started"] or dispatch.get("url") != explore_url(request["query"])
                or dispatch.get("navigation_started") is not True or dispatch.get("navigation_started_at") != receipt["dispatch_started_at"]):
            raise SafetyError("DIRECT_TRENDS_DISPATCH_MISMATCH")
    if "interpretation.json" in receipt["artifacts"] and not deadline:
        snapshot = load_json(folder / "scope.json", MAX_JSON)
        raw = (folder / "multiTimeline.csv").read_bytes() if "multiTimeline.csv" in receipt["artifacts"] else None
        interpreted = _interpret(snapshot, request, receipt["navigation_status"], raw, receipt["observed_at"])
        if interpreted != load_json(folder / "interpretation.json", MAX_JSON) or digest(interpreted) != receipt.get("interpretation_hash"):
            raise SafetyError("DIRECT_TRENDS_INTERPRETATION_MISMATCH")
        if receipt["state"] not in ("failed", "unavailable") and any(receipt[k] != interpreted[k] for k in ("state", "reason", "scope_verified")):
            raise SafetyError("DIRECT_TRENDS_RECEIPT_INVALID")
        if "native_result.json" in receipt["artifacts"] and receipt["transport_attempts"][-1] == "owned_native_windows_chrome":
            original = deepcopy(native.get("snapshot"))
            if not isinstance(original, dict): raise SafetyError("DIRECT_TRENDS_ORIGINAL_SCOPE_INVALID")
            html = original.pop("html", None)
            if original != snapshot or native.get("navigation_status") != receipt["navigation_status"]:
                raise SafetyError("DIRECT_TRENDS_ORIGINAL_SCOPE_MISMATCH")
            if isinstance(html, str) and sha256(html.encode()).hexdigest() != receipt["artifacts"].get("rendered.html"):
                raise SafetyError("DIRECT_TRENDS_ORIGINAL_HTML_MISMATCH")
    if receipt["state"] in ("usable", "no_data") and (not transport or transport.get("state") != "captured"
            or not receipt["dispatch_started"] or receipt.get("owned_process_exited") is not True
            or "interpretation.json" not in receipt["artifacts"]):
        raise SafetyError("DIRECT_TRENDS_EXECUTION_UNVERIFIED")
    return receipt


def reconcile_direct_trends(folder):
    """Reinterpret only bound original chart/export bytes, never a new read."""
    from .storage import Journal
    folder = safe_path(folder)
    with Journal(folder / "deadline_reparse_lock").locked():
        original = replay_direct_trends(folder)
        actual = saved_completed_transport(folder, original, snapshot_file="scope.json",
            requested_url=explore_url(original["request"]["query"]), snapshot_limit=MAX_JSON)
        if actual is None:
            return original
        snapshot = deepcopy(actual["snapshot"]); snapshot.pop("html", None)
        raw = (folder / "multiTimeline.csv").read_bytes() if "multiTimeline.csv" in original["artifacts"] else None
        interpreted = _interpret(snapshot, original["request"], actual.get("navigation_status"), raw, actual["completed_at"])
        if interpreted["state"] != "blocked" and interpreted["scope_verified"] is not True:
            return original
        scope_raw = canonical(snapshot)
        interpretation_raw = canonical(interpreted)
        recovery = {"version": "source-deadline-reparse-v1",
            "original_receipt_sha256": sha256((folder / "receipt.json").read_bytes()).hexdigest(),
            "original_artifacts_hash": digest(original["artifacts"])}
        receipt = dict(original, outer_deadline=False, recovery=recovery,
            receipt_file="deadline_reparse.json", interpretation_file="deadline_interpretation.json",
            scope_file="deadline_scope.json",
            state=interpreted["state"], reason=interpreted["reason"], observed_at=actual["completed_at"],
            dispatch_started=True, dispatch_started_at=actual["navigation_started_at"],
            navigation_status=actual.get("navigation_status"), owned_process_exited=True,
            scope_verified=interpreted["scope_verified"], interpretation_hash=digest(interpreted),
            artifacts=dict(original["artifacts"], **{"deadline_interpretation.json": sha256(interpretation_raw).hexdigest(),
                                                     "deadline_scope.json": sha256(scope_raw).hexdigest()}))
        for name, contents in (("deadline_scope.json", scope_raw), ("deadline_interpretation.json", interpretation_raw), ("deadline_reparse.json", canonical(receipt))):
            path = safe_path(folder / name)
            if path.exists():
                if path.read_bytes() != contents:
                    raise SafetyError("DIRECT_TRENDS_DEADLINE_REPARSE_CONFLICT")
            else:
                _write(path, contents)
        return receipt


def _verified_saved_dispatch(folder, *, request, acquisition_id, evidence_type):
    """Attribute a proved attempt independently of chart/receipt acceptance.

    Only exact original intent and hash-bound transport/native navigation
    records can notify the controller. Neither a wrapper's optimistic flag
    nor the existence of a file is sufficient. This does not validate demand
    data, waive replay errors or launch any reader.
    """
    folder = safe_path(folder)
    intent, receipt = load_json(folder / "intent.json", 16384), load_json(folder / "receipt.json", MAX_JSON)
    if (not isinstance(intent, dict) or not isinstance(receipt, dict)
            or intent.get("version") != VERSION or receipt.get("version") != VERSION
            or intent.get("request") != request or receipt.get("request") != request
            or intent.get("acquisition_id") != acquisition_id or receipt.get("acquisition_id") != acquisition_id
            or receipt.get("intent_hash") != digest(intent) or receipt.get("route") != "google-trends"):
        raise SafetyError("DIRECT_TRENDS_RECEIPT_MISMATCH")
    if intent.get("evidence_type") != evidence_type or receipt.get("evidence_type") != evidence_type:
        raise SafetyError("RESEARCH_NAMESPACE_INVALID")
    if (type(receipt.get("dispatch_started")) is not bool and not
            (receipt.get("outer_deadline") is True and receipt.get("dispatch_started") is None)) or not isinstance(receipt.get("artifacts"), dict):
        raise SafetyError("DIRECT_TRENDS_RECEIPT_INVALID")
    def artifact(name, limit):
        expected = receipt["artifacts"].get(name)
        if expected is None:
            return None
        path = safe_path(folder / name)
        if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or not path.is_file() or path.stat().st_size > limit or sha256(path.read_bytes()).hexdigest() != expected):
            raise SafetyError("DIRECT_TRENDS_ARTIFACT_HASH_MISMATCH")
        return load_json(path, limit)
    if receipt.get("outer_deadline") is True:
        # Cancellation has no chart/no-data authority; exact hash-bound
        # native dispatch may still prove the original attempt.
        return replay_direct_trends(folder)["dispatch_started"] is True
    transport = artifact("transport.json", MAX_JSON)
    if not isinstance(transport, dict):
        if receipt["dispatch_started"]:
            raise SafetyError("DIRECT_TRENDS_EXECUTION_UNVERIFIED")
        return False
    if (receipt["dispatch_started"] != (transport.get("navigation_started") is True)
            or receipt.get("dispatch_started_at") != transport.get("navigation_started_at")):
        raise SafetyError("DIRECT_TRENDS_TRANSPORT_MISMATCH")
    if not receipt["dispatch_started"]:
        return False
    timestamp(receipt.get("dispatch_started_at"))
    attempts = receipt.get("transport_attempts")
    if not isinstance(attempts, list) or not attempts or any(not isinstance(value, str) for value in attempts):
        raise SafetyError("DIRECT_TRENDS_EXECUTION_UNVERIFIED")
    if attempts[-1] == "owned_native_windows_chrome":
        dispatch = artifact("native_dispatch.json", 16384)
        native = artifact("native_result.json", 12 * 1024 * 1024)
        if (not isinstance(dispatch, dict)
                or dispatch.get("url") != explore_url(request["query"]) or dispatch.get("navigation_started") is not True
                or dispatch.get("navigation_started_at") != receipt["dispatch_started_at"]
                or (native is not None and (not isinstance(native, dict) or native.get("navigation_started") is not True
                    or native.get("navigation_started_at") != receipt["dispatch_started_at"]))):
            raise SafetyError("DIRECT_TRENDS_DISPATCH_MISMATCH")
    elif attempts[-1] not in ("owned_playwright_browser", "synthetic_fixture") or (
            attempts[-1] == "synthetic_fixture" and evidence_type != "synthetic"):
        raise SafetyError("DIRECT_TRENDS_EXECUTION_UNVERIFIED")
    return True


def _shared_access_failure(receipt, folder):
    """A verified Google access refusal is shared; local proof errors are not.
    A minimised page that never drew its chart holds the whole route too."""
    if receipt["dispatch_started"] is True and receipt["reason"] == MINIMISED_NOT_RENDERED:
        return True
    if receipt["state"] != "blocked" or receipt["dispatch_started"] is not True:
        return False
    if receipt.get("navigation_status") in (403, 429):
        return True
    scope_file = receipt.get("scope_file", "scope.json")
    if (receipt["reason"] not in ("DIRECT_TRENDS_ACCESS_CHALLENGE", "DIRECT_TRENDS_CONSENT_REQUIRED", GOOGLE_LIMIT)
            or scope_file not in receipt["artifacts"]):
        return False
    snapshot = load_json(folder / scope_file, MAX_JSON)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("text"), str):
        return False
    if receipt["reason"] == "DIRECT_TRENDS_CONSENT_REQUIRED":
        return bool(isinstance(snapshot.get("url"), str) and _CONSENT_URL.match(snapshot["url"]) or _CONSENT_TEXT.search(snapshot["text"]))
    if receipt["reason"] == GOOGLE_LIMIT:
        return bool(_LIMIT_TEXT.search(snapshot["text"]))
    return bool(BLOCKED.search(snapshot["text"]))


def create_trends_handlers(memory, *, data_root=None, capture=None, replay=None, shared_acquisitions=False,
                           signed_in_root=None):
    """Return concrete controller handler and saved-only recovery functions.

    Every new read is a signed-in Trends read (``trends_signed_in``);
    ``signed_in_root`` holds the signed-in browser's records (the project by default).
    """
    if type(shared_acquisitions) is not bool:
        raise SafetyError("DIRECT_TRENDS_SHARED_OPTION_INVALID")
    if shared_acquisitions:
        from .trends_shared import create_shared_trends_handlers
        return create_shared_trends_handlers(memory, data_root=data_root, capture=capture, replay=replay,
                                             signed_in_root=signed_in_root)
    return plain_trends_handlers(memory, data_root=data_root, capture=capture, replay=replay,
                                 signed_in_root=signed_in_root, paced=True)


def plain_trends_handlers(memory, *, data_root=None, capture=None, replay=None, signed_in_root=None, paced=True):
    """The unshared handler. With ``paced`` it first asks whether a signed-in
    read may start and counts it; the shared route does both before its launch
    guard and passes ``paced=False``. A read that could not start is set aside
    unsent (``SignedInWait``), so the question's next try starts fresh."""
    from .research_operations import import_trends_evidence
    from .signed_in_attempts import SignedInWait
    from .signed_in_browser import PROJECT_ROOT
    from .trends_signed_in import begin_signed_in_read, capture_signed_in_trends, release_unsent_read, set_aside_unsent
    signed_in_root = PROJECT_ROOT if signed_in_root is None else signed_in_root
    capture, replay = capture or capture_signed_in_trends, replay or replay_direct_trends
    root = safe_path(data_root or (memory.root / "direct_trends"))
    def collect(context, *, reconcile=False):
        if context["route"] != "google-trends": raise SafetyError("DIRECT_TRENDS_ROUTE_INVALID")
        request = _request(context["request"])
        state = memory.snapshot()
        for cid in context["concept_ids"]:
            if cid not in state["concepts"] or digest(state["concepts"][cid]["description"]) != context["description_hashes"][cid]:
                raise SafetyError("DIRECT_TRENDS_PRODUCT_CHANGED")
        folder = safe_path(root, context["acquisition_id"])
        if folder.exists() and load_json(folder / "intent.json", 16384).get("transport") != "signed-in":
            # A saved read of a replaced transport is history, never this route's answer.
            raise SafetyError("DIRECT_TRENDS_TRANSPORT_REPLACED")
        if not reconcile and not folder.exists():
            counted = begin_signed_in_read(signed_in_root, as_of=context.get("as_of")) if paced else None
            try:
                capture(request, context["acquisition_id"], data_root=root,
                        timeout=min(45, max(20, int(context.get("timeout_seconds", 45)))), signed_in_root=signed_in_root)
            except SignedInWait:
                if paced:
                    release_unsent_read(signed_in_root, counted)
                    set_aside_unsent(folder)
                raise
        if _verified_saved_dispatch(folder, request=request, acquisition_id=context["acquisition_id"],
                evidence_type=memory.evidence_type) and context.get("record_dispatch"):
            context["record_dispatch"]()
        receipt = reconcile_direct_trends(folder) if reconcile and replay is replay_direct_trends else replay(folder)
        if receipt["request"] != request or receipt["acquisition_id"] != context["acquisition_id"]:
            raise SafetyError("DIRECT_TRENDS_RECEIPT_MISMATCH")
        if receipt["evidence_type"] != memory.evidence_type: raise SafetyError("RESEARCH_NAMESPACE_INVALID")
        targets, refs = [], []
        for cid in context["concept_ids"]:
            current = memory.snapshot()["concepts"].get(cid)
            if current is None or digest(current["description"]) != context["description_hashes"][cid]:
                raise SafetyError("DIRECT_TRENDS_PRODUCT_CHANGED")
            evidence_id = None
            if receipt["state"] == "usable":
                interpreted = load_json(folder / receipt.get("interpretation_file", "interpretation.json"), MAX_JSON)
                imported = import_trends_evidence(memory, concept_id=cid, acquisition_id=context["acquisition_id"],
                    csv_bytes=(folder / "multiTimeline.csv").read_bytes(), metadata=interpreted["parsed"]["metadata"],
                    expected_description_hash=context["description_hashes"][cid])
                evidence_id = imported["id"]; refs.append(evidence_id)
            target_state = ("completed_relevant" if evidence_id else "completed_no_results" if receipt["state"] == "no_data"
                            else "not_attempted" if receipt["state"] == "unavailable" and not receipt["dispatch_started"]
                            else "failed" if receipt["state"] == "unavailable" else receipt["state"])
            targets.append({"concept_id": cid, "description_hash": context["description_hashes"][cid],
                "state": target_state, "evidence_refs": [evidence_id] if evidence_id else [], "reason": receipt["reason"]})
        shared_failure = _shared_access_failure(receipt, folder)
        return {"schema_version": 1, "route": "google-trends", "acquisition_id": context["acquisition_id"],
            "observed_at": receipt["observed_at"], "state": source_state(receipt),
            "dispatch_started": receipt["dispatch_started"], "reference": str(folder / receipt.get("receipt_file", "receipt.json")),
            "capture_ids": [], "observation_ids": [], "evidence_refs": refs, "target_results": targets,
            "reason": receipt["reason"], "retryable": (receipt.get("navigation_status") in (429, 500, 502, 503, 504)
                                                       or receipt["reason"] in (GOOGLE_LIMIT, MINIMISED_NOT_RENDERED,
                                                                                "OWNER_SIGN_IN_REQUIRED")),
            "failure_scope": "route" if shared_failure else "question"}
    return {"google-trends": {"handler": collect, "reconcile": lambda context: collect(context, reconcile=True)}}
