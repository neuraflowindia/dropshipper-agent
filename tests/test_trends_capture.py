"""Owned Playwright capture fixtures. No network, model or provider calls."""

import asyncio
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import sys
import shutil
import subprocess
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from product_radar.models import SafetyError
from product_radar.trends_capture import capture, chart_comparison, chart_matches, explore_url, verified_scope


def snapshot(term="chair cover"):
    return {"url": explore_url(term), "title": "Synthetic Trends chart", "text": "\n".join([
        term, "Search term", "India", "Past 12 months", "All categories", "Web Search", "Interest over time"]),
        "rows": [["x", "y1"], ["\u202aSep 13, 2026\u202c", "12"], ["Sep 20, 2026", "<1"], ["Sep 27, 2026", "20"]],
        "download_found": True}


class Download:
    def __init__(self, path):
        self.file = path
    async def path(self):
        return self.file


class DownloadEvent:
    def __init__(self, download):
        self.download = download
    async def __aenter__(self):
        self.value = asyncio.sleep(0, result=self.download)
        return self
    async def __aexit__(self, *_):
        return False


class FixturePage:
    def __init__(self, folder, snap=None, raw=None, status=200):
        self.snapshot = snap or snapshot()
        self.status, self.actions = status, []
        self.download_path = Path(folder) / "browser-download.csv"
        self.download_path.write_bytes(raw if raw is not None else b"Week,chair cover: (India)\n2026-09-13,12\n2026-09-20,<1\n2026-09-27,20\n")
    async def goto(self, url, **options):
        self.actions.append(("goto", url, options))
        return SimpleNamespace(status=self.status)
    async def wait_for_function(self, expression, **options):
        self.actions.append(("wait", options))
    async def evaluate(self, expression):
        self.actions.append(("snapshot", expression))
        return self.snapshot
    async def screenshot(self, path, **options):
        Path(path).write_bytes(b"synthetic screenshot")
    def expect_download(self, **options):
        self.actions.append(("download", options))
        return DownloadEvent(Download(self.download_path))
    def locator(self, selector):
        self.actions.append(("locator", selector))
        return self
    async def click(self, **options):
        self.actions.append(("click", options))


class TrendsCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.page = FixturePage(self.root)

    def run_capture(self, name="capture", **options):
        return capture("chair cover", name, self.root / "captures", page=self.page, **options)

    def test_public_url_is_encoded_and_fixed_scope(self):
        url = urlsplit(explore_url("sink organiser & travel"))
        self.assertEqual(url.hostname, "trends.google.com")
        self.assertEqual(parse_qs(url.query), {"hl": ["en"], "geo": ["IN"], "date": ["today 12-m"],
                                              "q": ["sink organiser & travel"], "cat": ["0"]})
        for bad in ("", " ", "x" * 121, "bad\nterm", None):
            with self.subTest(term=bad), self.assertRaises(SafetyError):
                explore_url(bad)

    def test_scope_comes_from_rendered_page_not_requested_url(self):
        self.assertTrue(verified_scope(snapshot(), "chair cover"))
        for old, new in (("India", "Canada"), ("Search term", "Topic"), ("Web Search", "YouTube Search"),
                         ("Past 12 months", "Past 5 years"), ("chair cover", "table mat")):
            snap = snapshot()
            snap["text"] = snap["text"].replace(old, new)
            self.assertFalse(verified_scope(snap, "chair cover"))
        self.assertFalse(verified_scope({"url": explore_url("chair cover"), "text": ""}, "chair cover"))

    def test_success_preserves_original_csv_scope_and_every_pair(self):
        result = self.run_capture()
        self.assertEqual(result["state"], "Usable")
        self.assertEqual(result["acquisition_id"], "capture")
        self.assertTrue(result["chart_rows_match"])
        self.assertTrue(result["scope_verified"])
        self.assertEqual(result["provider_requests"], 0)
        self.assertEqual(result["provider_cost_usd"], "0")
        raw = self.page.download_path.read_bytes()
        self.assertEqual(Path(result["csv_path"]).read_bytes(), raw)
        self.assertEqual(result["csv_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(json.loads(Path(result["receipt_path"]).read_text()), result)
        self.assertIn(("locator", '[data-radar-trends-download="true"]'), self.page.actions)
        self.assertEqual(result["intervals"], 3)

    def test_google_http_and_rendered_blocks_do_not_download_or_bypass(self):
        for name, status, text in (("429", 429, "Too Many Requests"), ("403", 403, "Denied"),
                                    ("challenge", 200, "We detected unusual traffic")):
            self.page = FixturePage(self.root, status=status)
            self.page.snapshot["text"] = text
            result = self.run_capture(name)
            self.assertEqual(result["state"], "Blocked")
            self.assertIsNone(result["csv_path"])
            self.assertFalse(any(a[0] == "download" for a in self.page.actions))

    def test_redirect_is_not_followed_by_login(self):
        self.page.snapshot["url"] = "https://accounts.google.com/signin"
        self.assertEqual(self.run_capture()["state"], "Blocked")
        self.assertFalse(any(a[0] == "click" for a in self.page.actions))

    def test_missing_scope_or_chart_is_insufficient_not_zero(self):
        for name in ("scope", "chart"):
            self.page = FixturePage(self.root)
            if name == "scope":
                self.page.snapshot["text"] = self.page.snapshot["text"].replace("India", "Canada")
            else:
                self.page.snapshot["download_found"] = False
            result = self.run_capture(name)
            self.assertEqual(result["state"], "Insufficient")
            self.assertIsNone(result["csv_path"])
            self.assertFalse(any(a[0] == "download" for a in self.page.actions))

    def test_wrong_csv_or_chart_pair_does_not_leave_usable_parsed_file(self):
        self.page.snapshot["rows"][1][1] = "99"
        result = self.run_capture()
        self.assertEqual(result["state"], "Insufficient")
        self.assertFalse(result["chart_rows_match"])
        parsed = json.loads(Path(result["receipt_path"]).with_name("parsed.json").read_text())
        self.assertEqual(parsed["state"], "Insufficient")
        self.assertTrue(Path(result["csv_path"]).exists())

    def test_html_export_is_failed_and_original_retained(self):
        self.page = FixturePage(self.root, raw=b"<html>Denied</html>")
        result = self.run_capture()
        self.assertEqual(result["state"], "Failed")
        self.assertEqual(Path(result["csv_path"]).read_bytes(), b"<html>Denied</html>")

    def test_duplicate_acquisition_and_bad_deadline_do_not_navigate(self):
        original = self.run_capture()
        count = len(self.page.actions)
        self.assertEqual(self.run_capture(), original)
        self.assertEqual(count, len(self.page.actions))
        with self.assertRaisesRegex(SafetyError, "TIMEOUT_INVALID"):
            self.run_capture("invalid", timeout=91)
        self.assertEqual(count, len(self.page.actions))

    def test_saved_capture_rejects_changed_term_or_csv_without_relaunch(self):
        original = self.run_capture()
        count = len(self.page.actions)
        with self.assertRaisesRegex(SafetyError, "INVALID_OR_INCOMPLETE"):
            capture("table mat", "capture", self.root / "captures", page=self.page)
        Path(original["csv_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(SafetyError, "HASH_MISMATCH"):
            self.run_capture()
        self.assertEqual(count, len(self.page.actions))

    def test_saved_capture_completion_must_be_valid_and_ordered(self):
        original = self.run_capture()
        count = len(self.page.actions)
        path = Path(original["receipt_path"])
        from product_radar.storage import atomic_json
        for completed in (None, "bad", "2000-01-01T00:00:00+00:00"):
            bad = dict(original, completed_at=completed)
            atomic_json(path, bad)
            with self.subTest(completed=completed), self.assertRaisesRegex(SafetyError, "INVALID_OR_INCOMPLETE"):
                self.run_capture()
        self.assertEqual(count, len(self.page.actions))

    def test_incomplete_acquisition_is_not_overwritten_or_relaunched(self):
        folder = self.root / "captures" / "capture"
        folder.mkdir(parents=True)
        with self.assertRaisesRegex(SafetyError, "SAVED_FILE_UNAVAILABLE"):
            self.run_capture()
        self.assertEqual(self.page.actions, [])

    def test_navigation_failure_has_saved_receipt_without_private_error_text(self):
        async def failure(*args, **options):
            raise RuntimeError("private cookies must not leak")
        self.page.goto = failure
        result = self.run_capture()
        self.assertEqual(result["state"], "Failed")
        self.assertNotIn("private", result["reason"])
        self.assertTrue(Path(result["receipt_path"]).exists())

    def test_dns_failure_is_distinguished_from_google_block(self):
        async def failure(*args, **options):
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED with private response text")
        self.page.goto = failure
        result = self.run_capture()
        self.assertEqual(result["state"], "Failed")
        self.assertEqual(result["reason_code"], "browser_dns_failure")
        self.assertNotIn("private", result["reason"])

    def test_missing_selected_browser_does_not_contact_google(self):
        stopped = []
        async def stop():
            stopped.append(True)
        driver = SimpleNamespace(chromium=SimpleNamespace(executable_path=str(self.root / "not-installed")), stop=stop)
        async def start():
            return driver
        package, module = ModuleType("playwright"), ModuleType("playwright.async_api")
        module.async_playwright = lambda: SimpleNamespace(start=start)
        with patch.dict(sys.modules, {"playwright": package, "playwright.async_api": module}):
            result = capture("chair cover", "missing-browser", self.root / "captures")
        self.assertEqual(result["state"], "Unavailable")
        self.assertEqual(result["reason_code"], "browser_executable_missing")
        self.assertEqual(stopped, [True])
        self.assertIsNone(result.get("navigation_status"))

    def test_whole_capture_timeout_covers_stalled_download(self):
        async def stalled(*args, **options):
            await asyncio.sleep(60)
        self.page.goto = stalled
        real_wait = asyncio.wait_for
        async def short_wait(awaitable, timeout):
            return await real_wait(awaitable, .01 if timeout > 5 else timeout)
        with patch("product_radar.trends_capture.asyncio.wait_for", short_wait):
            result = self.run_capture()
        self.assertEqual(result["state"], "Failed")
        self.assertIn("deadline", result["reason"])

    def test_chart_comparison_checks_all_dates_values_and_dimensions(self):
        rows = [["x", "y1"]]
        series = []
        for i in range(53):
            day = date(2025, 10, 5) + timedelta(weeks=i)
            rows.append([day.strftime("%b %d, %Y"), str(i)])
            series.append({"start": day.isoformat(), "raw_value": str(i)})
        self.assertTrue(chart_matches(series, rows))
        rows[24][1] = "99"
        self.assertFalse(chart_matches(series, rows))
        self.assertFalse(chart_matches(series, rows[:-1]))
        self.assertFalse(chart_matches(series, [["x", "y1", "y2"], *rows[1:]]))

    def test_ongoing_week_difference_is_retained_but_completed_difference_fails(self):
        points = [{"start": "2026-09-27", "raw_value": "61", "ongoing_at_capture": False},
                  {"start": "2026-10-04", "raw_value": "64", "ongoing_at_capture": True}]
        rows = [["x", "y1"], ["Sep 27, 2026", "61"], ["Oct 4, 2026", "66"]]
        result = chart_comparison(points, rows)
        self.assertTrue(result["matches"])
        self.assertEqual(result["completed_values_matched"], 1)
        self.assertEqual(result["ongoing_differences"][0]["used_for_market_signal"], False)
        rows[1][1] = "60"
        self.assertFalse(chart_matches(points, rows))

    def test_widget_heading_uses_rendered_text_not_hidden_tooltip(self):
        from product_radar.trends_capture import SNAPSHOT_JS
        self.assertIn("norm(e.innerText)", SNAPSHOT_JS)
        self.assertNotIn("isHeading = e => /^Interest over time(?: help_outline)?$/.test(norm(e.textContent))", SNAPSHOT_JS)

    @unittest.skipUnless(shutil.which("node"), "Local JavaScript fixture requires Node; it never launches a browser.")
    def test_readiness_and_capture_ignore_a_related_table_until_time_chart_is_ready(self):
        from product_radar.trends_capture import READY_JS, SNAPSHOT_JS
        fixture = r"""
class Element {
  constructor(tag, text='', children=[]) {
    this.tagName=tag.toUpperCase(); this.text=text; this.children=children;
    for(const child of children) child.parentElement=this;
  }
  get innerText(){return [this.text,...this.children.map(c=>c.innerText)].filter(Boolean).join('\n');}
  get textContent(){return this.innerText;}
  querySelectorAll(selector){
    const all=this.children.flatMap(c=>[c,...c.querySelectorAll('*')]);
    if(selector==='*')return all;
    if(selector==='button,[role="button"]')return all.filter(e=>e.tagName==='BUTTON');
    if(selector==='th,td')return all.filter(e=>['TH','TD'].includes(e.tagName));
    return all.filter(e=>e.tagName===selector.toUpperCase());
  }
  getAttribute(){return null;}
  setAttribute(name,value){this[name]=value;}
}
const cell=t=>new Element('td',t);
const table=()=>new Element('table','',[new Element('tr','',[cell('x'),cell('y1')]),new Element('tr','',[cell('Oct 4, 2026'),cell('20')])]);
const timeHeading=new Element('div','Interest over time');
const timeButton=new Element('button','file_download');
const timeWidget=new Element('div','',[timeHeading,timeButton]);
const relatedTable=table();
const relatedWidget=new Element('div','',[new Element('div','Interest by subregion'),new Element('button','file_download'),relatedTable]);
const shared=new Element('div','',[timeWidget,relatedWidget]);
const body=new Element('body','',[shared]);
global.document={body,title:'Synthetic Trends',querySelectorAll:s=>body.querySelectorAll(s)};
global.location={href:'https://trends.google.com/trends/explore?geo=IN&q=chair+cover'};
"""
        code = fixture + "\nconst ready=" + READY_JS + ";const snapshot=" + SNAPSHOT_JS + r""";
const before={ready:ready(),snapshot:snapshot()};
const ownTable=table();ownTable.parentElement=timeWidget;timeWidget.children.push(ownTable);
const after={ready:ready(),snapshot:snapshot()};
process.stdout.write(JSON.stringify({before,after,relatedClicked:!!relatedWidget.children[1]['data-radar-trends-download']}));
"""
        result = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=5, check=True)
        value = json.loads(result.stdout)
        self.assertFalse(value["before"]["ready"])
        self.assertFalse(value["before"]["snapshot"]["download_found"])
        self.assertTrue(value["after"]["ready"])
        self.assertTrue(value["after"]["snapshot"]["download_found"])
        self.assertFalse(value["relatedClicked"])


if __name__ == "__main__":
    unittest.main()
