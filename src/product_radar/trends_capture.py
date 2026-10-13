"""Bounded public Google Trends CSV capture through an owned Playwright browser.

This module does not log in, reuse personal profiles, bypass challenges, call
Apify, or interpret product relevance. The caller supplies the approved term.
Original exports and rendered chart rows are preserved before parsing.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlencode, urlsplit

from .models import SafetyError, identifier, timestamp
from .providers.trends_browser import MAX_BYTES, parse_trends_csv
from .storage import atomic_bytes, atomic_json, load_json, safe_path

CAPTURE_VERSION = "owned-playwright-trends-v1"
BLOCKED = re.compile(r"too many requests|too many queries|unusual traffic|captcha|not a robot|sign in to continue", re.I)


def _capture_utc_now():
    """Preserve observation precision locally, without redating old packets."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def explore_url(term):
    if not isinstance(term, str) or not term.strip() or len(term) > 120 or re.search(r"[\x00-\x1f]", term):
        raise SafetyError("TRENDS_TERM_INVALID")
    return "https://trends.google.com/trends/explore?" + urlencode(
        {"hl": "en", "geo": "IN", "date": "today 12-m", "q": term.strip(), "cat": "0"})


def verified_scope(snapshot, term):
    text = snapshot.get("text", "")
    if not isinstance(text, str):
        return False
    lines = {re.sub(r"\s+", " ", line).strip().casefold() for line in text.splitlines()}
    return all(label in lines for label in (term.strip().casefold(), "search term", "india",
                                            "past 12 months", "all categories", "web search"))


def chart_comparison(series, rows):
    """Check all dates/completed values and retain moving partial values.

    Google can refresh the ongoing week between displaying the chart and
    serving its CSV. That week is already excluded from demand arithmetic.
    """
    result = {"matches": False, "completed_values_matched": 0, "ongoing_differences": []}
    if not isinstance(rows, list) or len(rows) != len(series) + 1 or rows[0] != ["x", "y1"]:
        return result
    try:
        for point, row in zip(series, rows[1:]):
            if not isinstance(row, list) or len(row) != 2 or not all(isinstance(v, str) for v in row):
                return result
            label = "".join(c for c in row[0] if unicodedata.category(c) != "Cf").strip()
            if datetime.strptime(label, "%b %d, %Y").date().isoformat() != point["start"]:
                return result
            value = row[1].strip()
            if value != point["raw_value"]:
                if (point.get("ongoing_at_capture") is not True
                        or not re.fullmatch(r"\d{1,3}", value) or int(value) > 100):
                    return result
                result["ongoing_differences"].append({"start": point["start"], "chart": value,
                    "export": point["raw_value"], "used_for_market_signal": False})
            if point.get("ongoing_at_capture") is not True:
                result["completed_values_matched"] += 1
        result["matches"] = bool(series)
        return result
    except (ValueError, TypeError, KeyError):
        return result


def chart_matches(series, rows):
    return chart_comparison(series, rows)["matches"]


# Both readiness and capture use the same actual time-chart widget. Never
# climb into the page body and mistake a subregion/related-results table for it.
WIDGET_JS = r"""
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  // Use rendered text: the widget also contains an invisible Help tooltip.
  const isHeading = e => /^Interest over time(?: help_outline)?$/.test(norm(e.innerText));
  const heading = Array.from(document.querySelectorAll('*')).find(e =>
    isHeading(e) && !Array.from(e.children).some(isHeading));
  let widget = heading;
  const findChart = () => {
  for (let n = 0; widget && n < 10 && !['BODY','HTML'].includes(widget.tagName); n++, widget = widget.parentElement) {
    if (Array.from(widget.querySelectorAll('*')).some(e =>
      /^(Interest by subregion|Related topics|Related queries)(?: help_outline)?$/.test(norm(e.innerText)))) return null;
    const table = Array.from(widget.querySelectorAll('table')).find(t => {
      const rows = Array.from(t.querySelectorAll('tr'));
      if (rows.length < 2) return false;
      const headings = Array.from(rows[0].querySelectorAll('th,td')).map(c => norm(c.textContent));
      return headings.length === 2 && headings[0] === 'x' && headings[1] === 'y1';
    });
    const button = Array.from(widget.querySelectorAll('button,[role="button"]')).find(e =>
      /file_download|download.*csv|csv/i.test(norm(e.innerText)+' '+(e.getAttribute('aria-label')||'')+' '+(e.title||'')));
    if (table && button) return {table, button};
  }
  return null;
  };
"""

SNAPSHOT_JS = "() => {" + WIDGET_JS + r"""
  const chart = findChart();
  if (chart) {
    chart.button.setAttribute('data-radar-trends-download','true');
    return {url:location.href,title:document.title,text:document.body.innerText.slice(0,75000),
      rows:Array.from(chart.table.querySelectorAll('tr')).map(r => Array.from(r.querySelectorAll('th,td')).map(c => c.textContent.trim())),
      download_found:true};
  }
  return {url:location.href,title:document.title,text:document.body ? document.body.innerText.slice(0,75000) : '',rows:[],download_found:false};
}"""

READY_JS = "() => {" + WIDGET_JS + r"""
  const text = document.body ? document.body.innerText : '';
  return /too many requests|too many queries|unusual traffic|captcha|not a robot|sign in to continue/i.test(text)
    || !!findChart();
}"""


async def _collect(page, folder, receipt):
    receipt.update(navigation_started=True, navigation_started_at=_capture_utc_now())
    response = await page.goto(receipt["requested_url"], wait_until="domcontentloaded", timeout=30000)
    receipt["navigation_status"] = response.status if response else None
    try:
        await page.wait_for_function(READY_JS, timeout=25000)
    except Exception:
        # The snapshot explains an empty/no-data widget or an explicit block;
        # a wait timeout does not establish zero demand.
        pass
    snapshot = await page.evaluate(SNAPSHOT_JS)
    receipt["collected_at"] = _capture_utc_now()
    atomic_json(folder / "scope.json", snapshot)
    receipt.update(actual_url=snapshot.get("url"), scope_verified=verified_scope(snapshot, receipt["term"]))
    try:
        await page.screenshot(path=str(folder / "page.png"), timeout=5000)
    except Exception:
        receipt["screenshot_available"] = False
    else:
        receipt["screenshot_available"] = True
    actual = urlsplit(snapshot.get("url", ""))
    if receipt["navigation_status"] in (403, 429) or BLOCKED.search(snapshot.get("text", "")):
        receipt.update(state="Blocked", reason_code="google_access_challenge", reason="Google returned a rate limit or access challenge; no bypass or paid fallback was attempted.")
        return
    if actual.scheme != "https" or actual.hostname != "trends.google.com" or actual.path != "/trends/explore":
        receipt.update(state="Blocked", reason_code="redirect_outside_public_explore", reason="Redirected away from public Trends Explore; no login or consent action was attempted.")
        return
    if not receipt["scope_verified"]:
        receipt.update(state="Insufficient", reason_code="rendered_scope_unverified", reason="Rendered page did not establish the required term and India/twelve-month/Web Search scope.")
        return
    if not snapshot.get("download_found") or not snapshot.get("rows"):
        receipt.update(state="Insufficient", reason_code="chart_export_unavailable", reason="The actual interest-over-time chart table or its CSV control is unavailable.")
        return
    async with page.expect_download(timeout=15000) as event:
        await page.locator('[data-radar-trends-download="true"]').click(timeout=5000)
    download = await event.value
    temporary = await download.path()
    if temporary is None:
        receipt.update(state="Failed", reason="Browser did not complete the chart CSV download.")
        return
    source = Path(temporary)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_BYTES:
        receipt.update(state="Failed", reason="Chart CSV exceeds the capture size limit or is not a regular file.")
        return
    raw = source.read_bytes()
    receipt["collected_at"] = _capture_utc_now()
    csv_path = folder / "multiTimeline.csv"
    atomic_bytes(csv_path, raw)
    receipt.update(csv_path=str(csv_path), csv_sha256=hashlib.sha256(raw).hexdigest())
    parsed = parse_trends_csv(raw, receipt)
    atomic_json(folder / "parsed.json", parsed)
    comparison = chart_comparison(parsed["series"], snapshot["rows"])
    receipt.update(state=parsed["state"], reason=parsed["reason"], reason_code="chart_export_accepted" if parsed["state"] == "Usable" else "chart_export_not_usable", intervals=len(parsed["series"]),
                   chart_rows_match=comparison["matches"], chart_comparison=comparison)
    if parsed["state"] == "Usable" and not receipt["chart_rows_match"]:
        receipt.update(state="Insufficient", reason_code="chart_export_mismatch", reason="Downloaded CSV differs from the rendered dates or completed chart values.")
        # Persist the comparison in the parsed result so consumers cannot
        # accidentally import a mismatched chart via parsed.json alone.
        parsed.update(state="Insufficient", reason=receipt["reason"])
        atomic_json(folder / "parsed.json", parsed)


def _recover(folder, term, acquisition_id):
    """Resume the immutable result, never reissue a saved acquisition request."""
    receipt = load_json(folder / "receipt.json", limit=32768)
    expected = {"schema_version": 1, "route": "owned_playwright_browser", "acquisition_id": acquisition_id, "term": term.strip(), "geo": "IN", "time_range": "today 12-m",
                "kind": "search_term", "property": "web", "capture_version": CAPTURE_VERSION,
                "requested_url": explore_url(term), "receipt_path": str(folder / "receipt.json")}
    if (not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items())
            or receipt.get("state") not in ("Usable", "Blocked", "Insufficient", "Failed", "Unavailable")
            or not receipt.get("completed_at")):
        raise SafetyError("TRENDS_SAVED_CAPTURE_INVALID_OR_INCOMPLETE")
    try:
        started, collected, completed = (timestamp(receipt[k]) for k in ("started_at", "collected_at", "completed_at"))
    except (KeyError, SafetyError):
        raise SafetyError("TRENDS_SAVED_CAPTURE_INVALID_OR_INCOMPLETE") from None
    if not started <= collected <= completed:
        raise SafetyError("TRENDS_SAVED_CAPTURE_INVALID_OR_INCOMPLETE")
    if receipt.get("csv_path") is not None:
        csv_path = safe_path(folder) / "multiTimeline.csv"
        if (receipt["csv_path"] != str(csv_path) or csv_path.is_symlink() or not csv_path.is_file()
                or csv_path.stat().st_size > MAX_BYTES or hashlib.sha256(csv_path.read_bytes()).hexdigest() != receipt.get("csv_sha256")):
            raise SafetyError("TRENDS_SAVED_CAPTURE_HASH_MISMATCH")
        if receipt["state"] == "Usable":
            snapshot = load_json(folder / "scope.json", limit=262144)
            parsed = parse_trends_csv(csv_path.read_bytes(), receipt)
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("url"), str):
                raise SafetyError("TRENDS_SAVED_CAPTURE_SCOPE_OR_CHART_MISMATCH")
            actual = urlsplit(snapshot.get("url", ""))
            if (receipt.get("chart_rows_match") is not True
                    or not verified_scope(snapshot, term) or parsed["state"] != "Usable"
                    or actual.scheme != "https" or actual.hostname != "trends.google.com" or actual.path != "/trends/explore"
                    or not chart_matches(parsed["series"], snapshot.get("rows"))):
                raise SafetyError("TRENDS_SAVED_CAPTURE_SCOPE_OR_CHART_MISMATCH")
    elif receipt["state"] == "Usable":
        raise SafetyError("TRENDS_SAVED_CAPTURE_MISSING_CSV")
    return receipt


def validate_saved_capture(root, acquisition_id, term):
    """Validate original owned-capture artifacts without launching a browser.

    The regular dispatcher and capture replay use this same acceptance path.
    Manual connected-browser imports remain a separate research operation.
    """
    identifier(acquisition_id)
    explore_url(term)
    return _recover(safe_path(root, acquisition_id), term, acquisition_id)


async def capture_async(term, acquisition_id, root, *, browser_path=None, headed=False, timeout=90, page=None):
    """Save one immutable acquisition and return its receipt.

    ``root`` is the acquisition parent directory; ``page`` is an optional
    injected async Playwright-compatible fixture and is never closed here.
    Real calls create and close only a task-owned browser and fresh context.
    A whole-acquisition deadline includes stalled navigation and downloads.
    """
    identifier(acquisition_id)
    url = explore_url(term)
    if type(timeout) is not int or not 20 <= timeout <= 90:
        raise SafetyError("TRENDS_CAPTURE_TIMEOUT_INVALID")
    folder = safe_path(root, acquisition_id)
    try:
        folder.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _recover(folder, term, acquisition_id)
    receipt = {"schema_version": 1, "acquisition_id": acquisition_id, "capture_version": CAPTURE_VERSION, "route": "owned_playwright_browser",
               "term": term.strip(), "geo": "IN", "time_range": "today 12-m", "kind": "search_term", "property": "web",
               "source_reference": "browser-trends-" + acquisition_id, "requested_url": url, "started_at": _capture_utc_now(),
               "collected_at": None, "scope_verified": False, "state": "Failed", "reason": "Capture did not complete.",
               "reason_code": "capture_incomplete",
               "navigation_started": False, "navigation_started_at": None,
               "csv_path": None, "csv_sha256": None, "provider_requests": 0, "provider_cost_usd": "0",
               "receipt_path": str(folder / "receipt.json"), "owned_browser_closed": page is None}
    atomic_json(folder / "receipt.json", receipt)
    driver = browser = None

    async def run():
        nonlocal driver, browser, page
        if page is None:
            from playwright.async_api import async_playwright
            driver = await async_playwright().start()
            executable = str(browser_path) if browser_path is not None else driver.chromium.executable_path
            if not Path(executable).is_file():
                raise SafetyError("TRENDS_BROWSER_EXECUTABLE_MISSING")
            options = {"headless": not headed}
            if browser_path is not None:
                options["executable_path"] = str(browser_path)
            browser = await driver.chromium.launch(**options)
            receipt["owned_browser_closed"] = False
            context = await browser.new_context(accept_downloads=True, locale="en-US", viewport={"width": 1440, "height": 1100})
            page = await context.new_page()
        await _collect(page, folder, receipt)

    try:
        await asyncio.wait_for(run(), timeout=timeout - 5)
    except asyncio.TimeoutError:
        receipt.update(state="Failed", reason_code="capture_deadline", reason="The bounded browser capture deadline was reached; no automatic retry or paid fallback was made.")
    except ImportError:
        receipt.update(state="Unavailable", reason_code="browser_dependency_missing", reason="The optional Playwright browser dependency is unavailable.")
    except Exception as exc:
        # Never persist arbitrary exception text: it can contain browser
        # headers, filesystem names or remote response bodies.
        if isinstance(exc, SafetyError) and str(exc) == "TRENDS_BROWSER_EXECUTABLE_MISSING":
            receipt.update(state="Unavailable", reason_code="browser_executable_missing", reason="Playwright's selected browser executable is not installed; Google was not contacted.")
        elif "ERR_NAME_NOT_RESOLVED" in str(exc):
            receipt.update(state="Failed", reason_code="browser_dns_failure", reason="The browser could not resolve the website address; this is not a confirmed Google rate limit.")
        elif any(code in str(exc) for code in ("ERR_CONNECTION", "ERR_PROXY", "ERR_TUNNEL", "ERR_INTERNET", "ERR_NETWORK")):
            receipt.update(state="Failed", reason_code="browser_connection_failure", reason="The browser could not establish the website connection; this is not a confirmed Google rate limit.")
        else:
            receipt.update(state="Failed", reason_code="browser_capture_error", reason="Browser capture failed (" + type(exc).__name__ + "); saved evidence remains available.")
    finally:
        async def close_owned():
            if browser is not None:
                await browser.close()
                receipt["owned_browser_closed"] = True
            if driver is not None:
                await driver.stop()
        try:
            await asyncio.wait_for(close_owned(), timeout=5)
        except Exception:
            receipt["owned_browser_closed"] = False
            receipt.update(state="Failed", reason_code="owned_browser_cleanup_failure", reason="Owned browser cleanup did not complete; review this acquisition before retrying.")
        receipt["completed_at"] = _capture_utc_now()
        if receipt["collected_at"] is None:
            # No page/CSV was observed. Record when the failed or unavailable
            # attempt ended, not a stale pre-navigation observation or demand.
            receipt["collected_at"] = receipt["completed_at"]
        atomic_json(folder / "receipt.json", receipt)
    return receipt


def capture(term, acquisition_id, root, **options):
    """Synchronous application entry point; use capture_async inside an event loop."""
    return asyncio.run(capture_async(term, acquisition_id, root, **options))
