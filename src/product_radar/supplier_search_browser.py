"""Read one public Google Search page in a clean, short-lived browser.

Only explicit Google Search queries are navigated. No persistent profile,
personal cookies, login, proxies, contacts, CAPTCHA solving or paid fallback.
The returned rendered HTML receipt is interpreted by supplier_discovery; this
reader does not nominate suppliers or establish their commercial capabilities.
"""

from hashlib import sha256
from pathlib import Path
import time
from uuid import uuid4
from urllib.parse import parse_qs, urlsplit

from .models import SafetyError, identifier, utc_now
from .public_pages import MAX_HTML, MAX_TEXT, _public_url
from .supplier_discovery import _BLOCKED, _GOOGLE_HOSTS, _search_url

MAX_SECONDS = 45
_RESOURCE_HOSTS = _GOOGLE_HOSTS | {"www.gstatic.com", "ssl.gstatic.com", "fonts.gstatic.com", "fonts.googleapis.com"}
_READY = """() => {
  if (location.pathname === '/sorry' || location.pathname.startsWith('/sorry/')) return true;
  const text = (document.body?.innerText || '').toLowerCase();
  if (['our systems have detected unusual traffic', 'before you continue to google',
       'verify you are human', 'did not match any documents', 'no results found for'].some(s => text.includes(s))) return true;
  return Array.from(document.querySelectorAll('a[href] h3')).some(h => {
    const a = h.closest('a[href]');
    return a && h.innerText.trim() && (a.href.startsWith('http') || a.getAttribute('href')?.startsWith('/url?'));
  });
}"""
_SNAPSHOT = """() => {
  const html = document.documentElement.outerHTML;
  const text = document.body?.innerText || '';
  return {html: html.length > 1048576 ? null : html, html_characters: html.length,
          text: text.slice(0, 24000), text_truncated: text.length > 24000,
          title: document.title.slice(0, 2000)};
}"""


def _query_url(url):
    clean, host, _, scheme = _public_url(url)
    parsed = urlsplit(clean)
    params = parse_qs(parsed.query)
    query = params.get("q", [])
    if (scheme != "https" or host != "www.google.com" or parsed.path != "/search"
            or len(query) != 1 or not query[0].strip() or len(query[0]) > 1000
            or clean != _search_url(query[0])):
        raise SafetyError("SUPPLIER_BROWSER_QUERY_INVALID")
    return clean, query[0]


def _allow_request(url, method, resource_type, navigation, query):
    """Only read-only Google-owned resources, never destination supplier URLs."""
    try:
        clean, host, port, scheme = _public_url(url)
        parsed = urlsplit(clean)
        if (method != "GET" or scheme != "https" or port != 443 or host not in _RESOURCE_HOSTS
                or resource_type in ("image", "media", "font", "websocket", "ping")
                or parsed.path.startswith(("/accounts", "/signin", "/logout", "/sorry"))):
            return False
        if navigation:
            return host in _GOOGLE_HOSTS and parsed.path == "/search" and parse_qs(parsed.query).get("q") == [query]
        return True
    except (SafetyError, ValueError):
        return False


def _challenge_url(url):
    """Google's own anti-abuse destination is a challenge, not a query change."""
    try:
        parsed = urlsplit(url)
        return parsed.hostname in _GOOGLE_HOSTS and (parsed.path == "/sorry" or parsed.path.startswith("/sorry/"))
    except (TypeError, ValueError):
        return False


def fetch_google_search_page(item, *, browser_path=None, headless=True, timeout=MAX_SECONDS, playwright_factory=None,
                             on_dispatch=None):
    """Return one original public-page-compatible receipt with strict bounds.

    ``playwright_factory`` is only a local fixture seam. Browser path is an
    explicit installed executable, never a user profile. All owned resources
    are closed even after navigation, parsing, timeout or permission failures.
    """
    receipt = {"schema_version": 2, "id": None, "requested_url": None, "final_url": None,
               "state": "rejected", "reason": "SUPPLIER_BROWSER_INPUT_INVALID", "checked_at": utc_now(),
               "http_status": None, "body_complete": False, "title": "", "text": "", "html": None,
               "discovered_links": [], "text_truncated": False, "html_bytes": 0,
               "received_bytes": None, "redirects": [], "wire_sha256": None,
               "html_sha256": None, "text_sha256": None, "html_basis": "rendered_browser_dom",
               "route": "google-browser-html", "execution_stage": "not_dispatched",
               "attempt_started_at": None, "execution_ref": None}
    try:
        if (not isinstance(item, dict) or set(item) != {"id", "url"} or type(headless) is not bool
                or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_SECONDS
                or (on_dispatch is not None and not callable(on_dispatch))):
            raise SafetyError("SUPPLIER_BROWSER_INPUT_INVALID")
        receipt["id"] = identifier(item["id"])
        requested_url, query = _query_url(item["url"])
        receipt["requested_url"] = requested_url
        if browser_path is not None:
            if not isinstance(browser_path, (str, Path)) or not Path(browser_path).is_absolute() or not Path(browser_path).is_file():
                raise SafetyError("SUPPLIER_BROWSER_EXECUTABLE_INVALID")
        if playwright_factory is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                return {**receipt, "state": "unavailable", "reason": "SUPPLIER_BROWSER_DEPENDENCY_UNAVAILABLE"}
            playwright_factory = sync_playwright
    except (KeyError, TypeError, SafetyError) as error:
        return {**receipt, "reason": str(error) if isinstance(error, SafetyError) else "SUPPLIER_BROWSER_INPUT_INVALID"}

    deadline = time.monotonic() + timeout
    browser = context = driver = None
    def remaining():
        value = int((deadline - time.monotonic()) * 1000)
        if value <= 0:
            raise SafetyError("SUPPLIER_BROWSER_DEADLINE")
        return value
    try:
        driver = playwright_factory().start()
        launch = {"headless": headless, "timeout": remaining()}
        if browser_path is not None:
            launch["executable_path"] = str(browser_path)
        browser = driver.chromium.launch(**launch)
        context = browser.new_context(locale="en-IN", accept_downloads=False, service_workers="block")
        def route_request(route):
            request = route.request
            allowed = _allow_request(request.url, request.method, request.resource_type, request.is_navigation_request(), query)
            route.continue_() if allowed else route.abort()
        context.route("**/*", route_request)
        page = context.new_page()
        page.set_default_timeout(remaining())
        # Launch/context creation cannot count as supplier research. Only an
        # admitted, prepared public query navigation crosses this boundary.
        navigation_timeout = remaining()
        if on_dispatch is not None:
            on_dispatch()
        receipt.update(execution_stage="attempt_started", attempt_started_at=utc_now(),
                       execution_ref="supplier-browser-" + uuid4().hex)
        response = page.goto(requested_url, wait_until="domcontentloaded", timeout=navigation_timeout)
        receipt["http_status"] = response.status if response is not None else None
        if response is not None:
            receipt["execution_stage"] = "response_received"
        receipt["final_url"] = page.url
        challenge = _challenge_url(page.url)
        if receipt["http_status"] != 200 and not challenge:
            return {**receipt, "state": "unavailable", "reason": "SUPPLIER_BROWSER_HTTP_UNAVAILABLE", "checked_at": utc_now()}
        if not challenge:
            page.wait_for_function(_READY, timeout=remaining())
        snapshot = page.evaluate(_SNAPSHOT)
        remaining()
        receipt["final_url"] = page.url
        final = urlsplit(page.url)
        challenge = _challenge_url(page.url)
        if not challenge and (final.hostname not in _GOOGLE_HOSTS or final.path != "/search" or parse_qs(final.query).get("q") != [query]):
            raise SafetyError("SUPPLIER_BROWSER_SCOPE_CHANGED")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("html"), str):
            raise SafetyError("SUPPLIER_BROWSER_HTML_TOO_LARGE")
        encoded = snapshot["html"].encode("utf-8")
        if len(encoded) > MAX_HTML:
            raise SafetyError("SUPPLIER_BROWSER_HTML_TOO_LARGE")
        if not isinstance(snapshot.get("text"), str) or not isinstance(snapshot.get("title"), str):
            raise SafetyError("SUPPLIER_BROWSER_SNAPSHOT_INVALID")
        receipt.update(title=snapshot["title"][:2000], text=snapshot["text"][:MAX_TEXT],
                       text_truncated=snapshot.get("text_truncated") is True,
                       html=snapshot["html"], html_bytes=len(encoded), html_sha256=sha256(encoded).hexdigest(),
                       text_sha256=sha256(snapshot["text"][:MAX_TEXT].encode("utf-8")).hexdigest(),
                       body_complete=True, checked_at=utc_now())
        if challenge or any(marker in receipt["text"].casefold() for marker in _BLOCKED):
            return {**receipt, "state": "unavailable", "reason": "SUPPLIER_BROWSER_CHALLENGE"}
        return {**receipt, "state": "usable", "reason": "Rendered public Google Search page; result relevance remains unreviewed."}
    except SafetyError as error:
        return {**receipt, "state": "unavailable", "reason": str(error), "checked_at": utc_now()}
    except Exception as error:
        # Do not retain arbitrary browser exception strings or machine paths.
        reason = "SUPPLIER_BROWSER_DEADLINE" if type(error).__name__ == "TimeoutError" else "SUPPLIER_BROWSER_UNAVAILABLE"
        return {**receipt, "state": "unavailable", "reason": reason, "checked_at": utc_now()}
    finally:
        for owned, method in ((context, "close"), (browser, "close"), (driver, "stop")):
            if owned is not None:
                try:
                    getattr(owned, method)()
                except Exception:
                    pass
