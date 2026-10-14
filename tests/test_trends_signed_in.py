"""Google Trends read in the owner's signed-in research profile (Plan R10 W2).

The installed Trends route runs end to end against an offline double of the
native browser's signed-in read (it saves what the real one saves: the result,
the dispatch proof and the exported CSV). Nothing launches a browser or visits
a site; sign-ins, the desktop and pacing are local records in a temporary folder.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from product_radar import signed_in_pacing as pacing
from product_radar.indiamart_redaction import QUARANTINE as INDIAMART_QUARANTINE, protect_saved as indiamart_protect
from product_radar.models import SafetyError, digest, timestamp
from product_radar.research_memory import ResearchMemory
from product_radar.signed_in_attempts import SignedInWait
from product_radar.signed_in_browser import (note_sign_in_window, record_site_observation, records_path,
                                             redact_signed_in, sign_in_line, signed_in_wait, site_states)
from product_radar.signed_in_saved import QUARANTINE, protect_saved
from product_radar.source_acquisitions import ACQUISITION_KIND
from product_radar.source_coverage import (SIGNED_IN_ACCESS_IDENTITY, SIGNED_IN_TRANSPORT, acquisition_access_scope,
                                           execute_source_run, installed_source_handlers, prepare_source_tasks,
                                           signed_in_access_scope, source_access_wait)
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.trends_capture import explore_url
from product_radar.trends_controller import (EXPORT_JS, GOOGLE_LIMIT, MINIMISED_NOT_RENDERED, READY_JS, _shared_access_failure,
                                             build_trends_request, capture_direct_trends, create_trends_handlers,
                                             replay_direct_trends)
from product_radar import trends_proof_gate as gate
from product_radar.trends_rising import FILE as RISING_FILE, parse_rising_queries, rising_queries
from product_radar.trends_shared import build_shared_trends_request, release_replaced_trends_waits
from product_radar.trends_signed_in import (PENDING_BOOKKEEPING, begin_signed_in_read, capture_signed_in_trends,
                                            release_unsent_read, render_hold, signed_in_hold, signed_in_marker,
                                            signed_in_snapshot, google_account_marker)
from test_trends_controller import original, snapshot

RELATED = "\n".join(["Related queries", "help_outline", "Rising", "arrow_drop_down", "file_download", "code", "share",
                     "1", "chair cover stretchable", "Breakout", "more_vert",
                     "2", "chair cover for wedding", "+1,250%", "more_vert",
                     "3", "sofa chair cover", "+40%", "more_vert", "Showing 1-3 of 12 queries"])


def now():
    return datetime.now(timezone.utc).isoformat()


def page(term="chair cover", *, avatar=True, link=False, related=RELATED, **changes):
    """The rendered Trends page as the export expression returns it."""
    return snapshot(term, **{"account": {"avatar": avatar, "sign_in_link": link},
                             "related": None if related is None else {"text": related},
                             "html": "<html><a aria-label=\"Google Account: Research Owner (owner.research@example.com)\"></a></html>",
                             **changes})


class SignedInTrendsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.signed = self.root / "signed-in"
        self.captures = self.root / "captures"
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="captured")
        self.memory.put("concepts", "chair", {"status": "active", "description": {"label": "chair cover"}})
        self.calls, self.state = [], "captured"
        self.snap, self.status, self.visibility, self.frame = page(), 200, "visible", None
        browser = patch("product_radar.native_public_browser.native_snapshot", side_effect=self.native)
        browser.start(); self.addCleanup(browser.stop)

    def sign_in(self, at=None):
        at = at or (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        note_sign_in_window(self.signed, True, opened=True, at=at)
        note_sign_in_window(self.signed, False, at=at)
        return at

    def native(self, url, expression, folder, **options):
        """An offline double of ``native_snapshot(profile="signed-in")``."""
        self.calls.append(dict(options, url=url, expression=expression))
        folder.mkdir(parents=True, exist_ok=True)
        if self.state != "captured":
            output = {"state": "waiting", "reason": self.state, "navigation_started": False,
                      "stage": "browser_launch", "snapshot": None}
            atomic_json(folder / "native_result.json", output)
            return output
        started = self.tick()
        atomic_json(folder / "native_dispatch.json", {"url": url, "navigation_started": True, "navigation_started_at": started})
        output = {"state": "captured", "reason": "Synthetic signed-in read", "navigation_status": self.status,
                  "navigation_started": True, "navigation_started_at": started, "completed_at": self.tick(),
                  "owned_process_exited": True, "window_state": "minimized", "page_visibility": self.visibility,
                  "animation_frame": self.visibility == "visible" if self.frame is None else self.frame,
                  "snapshot": dict(self.snap, url=self.snap.get("redirect", url))}
        if self.snap.get("download_found"):
            term = self.snap["text"].split("\n", 1)[0].encode()
            (folder / "multiTimeline.csv").write_bytes(original()["csv_bytes"].replace(b"chair cover:", term + b":"))
        atomic_json(folder / "native_result.json", output)
        return output

    def round(self, shared=True):
        """One controller round, each later than the question's calm recheck time."""
        self.shift = getattr(self, "shift", -150) + 150
        at = (datetime.now(timezone.utc) + timedelta(seconds=self.shift)).isoformat()
        handlers = create_trends_handlers(self.memory, data_root=self.captures, shared_acquisitions=shared,
                                          signed_in_root=self.signed)
        # Pacing, the capture wrapper and the browser double all read the round's clock.
        self.clock = timestamp(at)
        with patch("product_radar.signed_in_pacing.utc_now", return_value=at), \
                patch("product_radar.trends_controller._wrapper_utc_now", side_effect=self.tick), \
                patch("product_radar.trends_shared.utc_now", side_effect=lambda **_: self.tick()):
            return execute_source_run(self.memory, "checks", handlers=handlers, as_of=at,
                bounded_call=lambda callback, context, timeout: callback(context), shared_marketplace=shared)

    def tick(self):
        """Successive instants of the current round (real time outside rounds)."""
        if getattr(self, "clock", None) is None:
            return now()
        self.clock += timedelta(milliseconds=1)
        return self.clock.isoformat()

    def question(self):
        prepare_source_tasks(self.memory, [{"route": "google-trends", "request": {"query": "chair cover", "country": "IN"},
                                            "concept_ids": ["chair"]}], run_id="checks", as_of=now())

    def capture(self, name="read-1", term="chair cover"):
        return capture_signed_in_trends(build_trends_request(term), name, data_root=self.captures, signed_in_root=self.signed)

    # The route's pacing, sign-in gate and refusals.

    def test_pacing_settings_and_one_read_every_ninety_seconds(self):
        self.assertEqual(pacing.pacing_rules("google-trends"), {"min_interval_seconds": 90, "daily_ceiling": 200})
        self.sign_in()
        at = "2026-10-14T10:00:00+00:00"
        with patch("product_radar.signed_in_pacing.utc_now", return_value=at):
            counted = begin_signed_in_read(self.signed, as_of=at)
        self.assertEqual(counted, at)
        # The hold and the count read the same clock (the reservation's own).
        with patch("product_radar.signed_in_pacing.utc_now", return_value="2026-10-14T10:01:00+00:00"), \
                self.assertRaises(SignedInWait) as held:
            begin_signed_in_read(self.signed, as_of="2026-10-14T09:00:00+00:00")
        self.assertEqual(str(held.exception), "SIGNED_IN_READ_SPACING")
        self.assertEqual(held.exception.next_retry_at, "2026-10-14T10:01:30+00:00")
        self.assertIsNone(signed_in_hold(self.signed, as_of="2026-10-14T10:01:30+00:00"))
        # IndiaMART keeps its own spacing: a Trends read never holds it.
        self.assertIsNone(pacing.signed_in_pace(self.signed, "indiamart", as_of="2026-10-14T10:00:10+00:00"))

    def test_daily_ceiling_of_two_hundred_reads(self):
        self.sign_in()
        start = datetime(2026, 10, 14, tzinfo=timezone.utc)
        reads = [(start + timedelta(seconds=100 * index)).isoformat() for index in range(200)]
        atomic_json(pacing._path(self.signed), {"schema_version": 1, "reads": {"google-trends": reads}})
        later = (start + timedelta(hours=10)).isoformat()
        hold = signed_in_hold(self.signed, as_of=later)
        self.assertEqual(hold, {"reason": "SIGNED_IN_DAILY_CEILING", "next_retry_at": (start + timedelta(hours=24)).isoformat()})
        with patch("product_radar.signed_in_pacing.utc_now", return_value=later), self.assertRaises(SignedInWait) as held:
            begin_signed_in_read(self.signed, as_of=later)
        self.assertEqual(held.exception.next_retry_at, (start + timedelta(hours=24)).isoformat())

    def test_without_sign_in_the_route_waits_calmly_and_never_reads(self):
        self.assertEqual(signed_in_hold(self.signed)["reason"], "OWNER_SIGN_IN_REQUIRED")
        self.question()
        for shared in (True, False):
            with self.subTest(shared=shared):
                for _ in range(2):
                    result = self.round(shared)
                    self.assertEqual(result["counts"], {"waiting": 1})
                    self.assertEqual(result["results"][0]["reason"], "OWNER_SIGN_IN_REQUIRED")
                    self.assertTrue(result["results"][0]["next_retry_at"])
        self.assertEqual(self.calls, []); self.assertFalse(self.captures.exists())

    def test_the_signed_in_identity_holds_its_own_access_waits(self):
        request = build_shared_trends_request("chair cover")
        self.assertEqual(request["transport"], SIGNED_IN_TRANSPORT)
        scope = acquisition_access_scope({"binding": {"route": "google-trends", "request": request}})
        self.assertEqual(scope, signed_in_access_scope(request["url"]))
        self.assertEqual(scope["access_identity"], SIGNED_IN_ACCESS_IDENTITY)

    def test_marker_reads_the_avatar_or_the_sign_in_link(self):
        url = explore_url("chair cover")
        self.assertTrue(signed_in_marker({"url": url, "account": {"avatar": True, "sign_in_link": False}}))
        self.assertFalse(signed_in_marker({"url": url, "account": {"avatar": False, "sign_in_link": True}}))
        for unclear in ({"url": url, "account": {"avatar": False, "sign_in_link": False}}, {"url": url},
                        {"url": "https://consent.google.com/x", "account": {"avatar": True}}, None, {"account": {}}):
            self.assertIsNone(signed_in_marker(unclear))
        self.assertIn("accounts.google.com/SignOutOptions", EXPORT_JS)
        self.assertIn("accounts.google.com/ServiceLogin", EXPORT_JS)

    def test_a_signed_in_read_saves_a_verified_export_its_rising_queries_and_the_marker(self):
        signed_at = self.sign_in()
        receipt = self.capture()
        self.assertEqual((receipt["state"], receipt["reason"]), ("usable", "DIRECT_TRENDS_CHART_EXPORT_VERIFIED"))
        self.assertEqual(replay_direct_trends(receipt["folder"]), receipt)
        intent = load_json(Path(receipt["folder"]) / "intent.json")
        self.assertEqual(intent["transport"], "signed-in")
        (call,) = self.calls
        self.assertEqual(call["profile"], "signed-in"); self.assertEqual(call["root"], self.signed)
        self.assertEqual(call["expression"], EXPORT_JS)
        self.assertEqual(site_states(self.signed)["google-trends"]["state"], "signed_in")
        transport = load_json(Path(receipt["folder"]) / "transport.json")
        self.assertIs(transport["signed_in"], True)
        rising = load_json(Path(receipt["folder"]) / RISING_FILE)
        self.assertEqual(rising["state"], "read"); self.assertEqual(rising["total"], 12)
        self.assertEqual([row["query"] for row in rising["queries"]],
                         ["chair cover stretchable", "chair cover for wedding", "sofa chair cover"])
        self.assertEqual(rising["queries"][0], {"rank": 1, "query": "chair cover stretchable", "raw_value": "Breakout",
                                                "growth_kind": "breakout", "growth_percent": None})
        self.assertEqual(rising["queries"][1]["growth_percent"], 1250.0)
        self.assertEqual((rising["term"], rising["acquisition_id"]), ("chair cover", "read-1"))
        self.assertEqual(rising_queries(receipt["folder"]), rising)
        record = gate.proof_gate(self.signed)
        self.assertEqual((record["state"], record["sign_in_at"], len(record["reads"])), ("in_progress", signed_at, 1))
        self.assertEqual(gate.proof_gate_line(self.signed), "Google Trends check: 1 of 3 signed-in reads saved complete results.")

    def test_a_page_showing_the_sign_in_link_makes_later_reads_wait_for_the_owner(self):
        self.sign_in(); self.snap = page(avatar=False, link=True)
        receipt = self.capture()
        self.assertEqual(receipt["state"], "usable")  # Google served the chart: it is still a verified chart
        self.assertEqual(site_states(self.signed)["google-trends"]["state"], "sign_in_needed")
        self.assertEqual(signed_in_wait(self.signed, "google-trends"), "OWNER_SIGN_IN_REQUIRED")
        self.assertEqual(gate.proof_gate(self.signed)["reads"], [])  # a signed-out read proves nothing
        with self.assertRaises(SignedInWait) as held:
            self.capture("read-2")
        self.assertEqual(str(held.exception), "OWNER_SIGN_IN_REQUIRED")
        self.assertEqual(len(self.calls), 1)

    def test_refusals_block_with_their_exact_reason_and_fail_the_gate(self):
        self.sign_in(); self.status = 429
        self.snap = page(text="429. That's an error. We're sorry, but you have sent too many requests.",
                         account={"avatar": False, "sign_in_link": False}, rows=[], download_found=False)
        receipt = self.capture()
        self.assertEqual((receipt["state"], receipt["reason"], receipt["navigation_status"]),
                         ("blocked", "DIRECT_TRENDS_ACCESS_CHALLENGE", 429))
        self.assertTrue(_shared_access_failure(receipt, Path(receipt["folder"])))
        record = gate.proof_gate(self.signed)
        self.assertEqual(record["state"], "failed"); self.assertTrue(record["reads"][0]["refused"])
        self.assertIn("refused the signed-in read on", gate.proof_gate_line(self.signed))
        self.assertIn("Google is refusing Trends reads for now. Research continues without Trends",
                      gate.proof_gate_line(self.signed))
        self.assertNotIn("DIRECT_TRENDS", gate.proof_gate_line(self.signed))  # plain words, never codes
        self.assertEqual(site_states(self.signed)["google-trends"]["state"], "not_checked")  # no marker on a refusal page

    def test_a_consent_page_is_a_refusal_never_answered(self):
        self.sign_in()
        self.snap = page(text="Before you continue to Google\nAccept all\nReject all", rows=[], download_found=False,
                         account={"avatar": False, "sign_in_link": True})
        receipt = self.capture()
        self.assertEqual((receipt["state"], receipt["reason"], receipt["navigation_status"]),
                         ("blocked", "DIRECT_TRENDS_CONSENT_REQUIRED", 200))
        self.assertTrue(_shared_access_failure(receipt, Path(receipt["folder"])))
        self.assertIn("Accept all", load_json(Path(receipt["folder"]) / "scope.json")["text"])  # kept, not clicked

    def test_a_minimised_page_that_never_drew_its_chart_is_a_rendering_failure(self):
        self.sign_in(); self.visibility = "hidden"
        self.snap = page(rows=[], download_found=False, download_clicked=False, time_widget_present=False, related=None)
        receipt = self.capture()
        self.assertEqual((receipt["state"], receipt["reason"]), ("failed", MINIMISED_NOT_RENDERED))
        self.assertEqual(replay_direct_trends(receipt["folder"]), receipt)
        self.assertTrue(_shared_access_failure(receipt, Path(receipt["folder"])))  # the whole route pauses
        row = gate.proof_gate(self.signed)["reads"][0]
        self.assertEqual((row["refused"], row["page_visibility"], row["reason"]), (False, "hidden", MINIMISED_NOT_RENDERED))
        hold = signed_in_hold(self.signed)
        self.assertEqual(hold["reason"], "TRENDS_RENDERING_PAUSED")
        self.assertIn("Google Trends is paused: the minimised Trends page did not draw its chart. Trying again at",
                      gate.proof_gate_line(self.signed))
        with self.assertRaises(SignedInWait) as held:
            begin_signed_in_read(self.signed)
        self.assertEqual((str(held.exception), held.exception.next_retry_at), ("TRENDS_RENDERING_PAUSED", hold["next_retry_at"]))
        self.sign_in(at=now())  # signing in again ends the pause at once
        self.assertIsNone(render_hold(self.signed))
        # A hidden page that still drew animation frames, without a chart, is not a rendering failure.
        self.frame = True; self.snap = page(rows=[], download_found=False, time_widget_present=False, related=None)
        self.assertEqual(self.capture("drawn")["reason"], "DIRECT_TRENDS_CHART_EXPORT_MISSING")
        self.frame = None
        # A minimised page that did draw its chart is a normal verified read.
        self.snap = page()
        self.assertEqual(self.capture("read-2")["state"], "usable")
        # A visible page without a chart stays an incomplete check, not a rendering failure.
        self.visibility = "visible"; self.snap = page(rows=[], download_found=False, time_widget_present=False)
        self.assertEqual(self.capture("read-3")["reason"], "DIRECT_TRENDS_CHART_EXPORT_MISSING")

    def test_proof_gate_passes_on_three_complete_reads_and_restarts_on_a_new_sign_in(self):
        self.assertEqual(gate.proof_gate_line(self.signed), "Google Trends: waiting for the owner to sign in.")
        self.sign_in()
        self.assertEqual(gate.proof_gate(self.signed)["state"], "waiting_for_reads")
        for index in range(1, 5):
            self.snap = page("chair cover %d" % index)
            self.capture("read-%d" % index, term="chair cover %d" % index)
        record = gate.proof_gate(self.signed)
        self.assertEqual(record["state"], "passed"); self.assertEqual(len(record["reads"]), 3)
        self.assertTrue(all(row["passed"] for row in record["reads"]))
        self.assertTrue(gate.proof_gate_line(self.signed).startswith("Google Trends check passed: the first signed-in reads"))
        self.assertEqual(gate.record_proof_read(self.signed, self.captures / "read-1",
                                                replay_direct_trends(self.captures / "read-1")), record)
        self.sign_in(at=now())  # the owner signs in again: a new gate
        self.assertEqual(gate.proof_gate(self.signed)["state"], "waiting_for_reads")
        early = replay_direct_trends(self.captures / "read-4")  # read before this sign-in: not counted
        self.assertEqual(gate.record_proof_read(self.signed, self.captures / "read-4", early)["reads"], [])
        atomic_json(gate._path(self.signed), dict(record, state="unknown"))
        with self.assertRaisesRegex(SafetyError, "TRENDS_PROOF_GATE_RECORD_INVALID"):
            gate.proof_gate(self.signed)

    def test_refusal_checks_need_the_saved_refusal_itself(self):
        self.sign_in()
        self.snap = page(text="Unusual traffic from your computer network", rows=[], download_found=False)
        receipt = self.capture()
        folder = Path(receipt["folder"])
        self.assertTrue(_shared_access_failure(receipt, folder))
        self.assertFalse(_shared_access_failure(dict(receipt, reason="DIRECT_TRENDS_SCOPE_UNVERIFIED"), folder))
        self.assertFalse(_shared_access_failure(dict(receipt, artifacts={}), folder))
        self.assertFalse(_shared_access_failure(dict(receipt, reason="DIRECT_TRENDS_CONSENT_REQUIRED"), folder))
        atomic_json(folder / "scope.json", {"text": None})
        self.assertFalse(_shared_access_failure(receipt, folder))

    def test_count_and_give_back_reads_around_busy_records(self):
        self.sign_in()
        with patch("product_radar.signed_in_pacing.reserve_signed_in_read", side_effect=SafetyError("SIGNED_IN_RECORD_BUSY")):
            with self.assertRaises(SignedInWait) as held:
                begin_signed_in_read(self.signed)
        self.assertEqual(str(held.exception), "SIGNED_IN_RECORD_BUSY"); self.assertTrue(held.exception.next_retry_at)
        paced = pacing.SignedInPaced("SIGNED_IN_READ_SPACING", "2026-10-14T10:01:30+00:00")
        with patch("product_radar.signed_in_pacing.reserve_signed_in_read", side_effect=paced):
            with self.assertRaises(SignedInWait) as held:
                begin_signed_in_read(self.signed)
        self.assertEqual(held.exception.next_retry_at, "2026-10-14T10:01:30+00:00")
        for code in ("SIGNED_IN_PACING_SETTINGS_INVALID", "SIGNED_IN_PACING_RECORD_INVALID"):  # failures, never calm
            with patch("product_radar.signed_in_pacing.reserve_signed_in_read", side_effect=SafetyError(code)):
                with self.assertRaisesRegex(SafetyError, code) as raised:
                    begin_signed_in_read(self.signed)
            self.assertNotIsInstance(raised.exception, SignedInWait)
        release_unsent_read(self.signed, None)
        with patch("product_radar.signed_in_pacing.release_signed_in_read", side_effect=SafetyError("SIGNED_IN_RECORD_BUSY")):
            release_unsent_read(self.signed, "2026-10-14T10:00:00+00:00")
        with patch("product_radar.signed_in_pacing.release_signed_in_read", side_effect=SafetyError("SIGNED_IN_RECORD_INVALID")):
            with self.assertRaisesRegex(SafetyError, "RECORD_INVALID"):
                release_unsent_read(self.signed, "2026-10-14T10:00:00+00:00")

    def test_rising_queries_are_derived_again_when_missing_and_never_for_unverified_pages(self):
        self.sign_in()
        receipt = self.capture()
        saved = Path(receipt["folder"]) / RISING_FILE
        first = load_json(saved); saved.unlink()
        self.assertEqual(rising_queries(receipt["folder"]), first)
        atomic_json(saved, dict(first, parser_version="old"))
        self.assertEqual(rising_queries(receipt["folder"]), first)
        self.status = 429; self.snap = page(text="Too many requests", rows=[], download_found=False)
        refused = self.capture("read-2")
        self.assertIsNone(rising_queries(refused["folder"]))
        self.assertFalse((Path(refused["folder"]) / RISING_FILE).exists())

    def test_a_refused_route_due_again_still_waits_for_the_owner_first(self):
        self.sign_in(); self.status = 429
        self.snap = page(text="Too many requests", rows=[], download_found=False, account={"avatar": False, "sign_in_link": False})
        self.question()
        self.assertEqual(self.round()["counts"], {"blocked": 1})
        record_site_observation(self.signed, "google-trends", False)  # the sign-in then expired
        self.shift = 400  # past the first five-minute refusal wait
        result = self.round()
        self.assertEqual(result["counts"], {"waiting": 1})
        self.assertEqual(result["results"][0]["reason"], "OWNER_SIGN_IN_REQUIRED")
        self.assertEqual(len(self.calls), 1)

    # A read that could not start after it was admitted.

    def test_a_busy_browser_reopens_the_shared_acquisition_and_the_next_round_reads(self):
        self.sign_in(); self.state = "SIGNED_IN_BROWSER_BUSY"
        self.question(); run = self.round
        first = run()
        self.assertEqual(first["counts"], {"waiting": 1})
        self.assertEqual(first["results"][0]["reason"], "SIGNED_IN_BROWSER_BUSY")
        (aid, owner), = ((rid, row) for rid, row in self.memory.snapshot()["batch_runs"].items() if row.get("kind") == ACQUISITION_KIND)
        self.assertEqual(owner["state"], "reserved")
        self.assertFalse((self.captures / aid).exists())
        (aside,) = self.captures.glob(aid + "-not-started-1-*")
        self.assertRegex(aside.name, r"-not-started-1-\d{4}-\d{2}-\d{2}T\d{6}\.\d{6}")  # one style, to the microsecond
        self.assertTrue((aside / "not-started" / "process-exit.json").is_file())
        self.assertFalse((aside / "not-started.json").exists())
        self.assertEqual(pacing._records(self.signed)["reads"]["google-trends"], [])  # the unsent read was given back
        self.state = "captured"
        second = run()
        self.assertEqual(second["counts"], {"acquired": 1})
        owner = self.memory.snapshot()["batch_runs"][aid]
        response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertTrue(response["complete"]); self.assertEqual(len(self.calls), 2)
        self.assertIsNone(source_access_wait(self.memory.snapshot(), signed_in_access_scope(explore_url("chair cover")), as_of=now()))

    def test_a_failed_reopening_is_retried_by_the_next_resolution(self):
        self.sign_in(); self.state = "OWNER_DESKTOP_UNAVAILABLE"
        self.question(); run = self.round
        with patch("product_radar.source_acquisitions.recover_source_prelaunch", side_effect=OSError("held")):
            self.assertEqual(run()["results"][0]["reason"], "SIGNED_IN_START_RECOVERY_PENDING")
            self.state = "captured"  # the next resolution retries the reopening first, still without a read
            self.assertEqual(run()["results"][0]["reason"], "SIGNED_IN_START_RECOVERY_PENDING")
        self.assertEqual(len(self.calls), 1)
        (aid, owner), = ((rid, row) for rid, row in self.memory.snapshot()["batch_runs"].items() if row.get("kind") == ACQUISITION_KIND)
        self.assertEqual(owner["state"], "dispatching")
        (note,) = self.captures.glob(aid + "-not-started-1-*/not-started.json")
        handler = create_trends_handlers(self.memory, data_root=self.captures, shared_acquisitions=True,
                                         signed_in_root=self.signed)["google-trends"]
        context = {"route": "google-trends", "request": {"query": "chair cover", "country": "IN"},
                   "as_of": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), "task_id": "x"}
        self.assertTrue(handler["source_due_hint"](context)["ready"])
        self.state = "captured"
        self.assertEqual(run()["counts"], {"acquired": 1})
        owner = self.memory.snapshot()["batch_runs"][aid]
        self.assertTrue(self.memory.snapshot()["source_checks"][owner["active_response_id"]]["complete"])

    def test_an_unshared_read_that_could_not_start_is_set_aside_unsent(self):
        self.sign_in(); self.state = "SIGNED_IN_BROWSER_BUSY"
        self.question(); run = lambda: self.round(False)
        self.assertEqual(run()["counts"], {"waiting": 1})
        self.assertEqual([path.name.split("-not-started-")[1] != "" for path in self.captures.iterdir()], [True])
        self.assertEqual(pacing._records(self.signed)["reads"]["google-trends"], [])
        self.state = "captured"
        self.assertEqual(run()["counts"], {"acquired": 1})
        self.assertEqual(len(self.memory.snapshot()["trends_evidence"]), 1)

    def test_browser_refusals_before_launch_wait_but_after_launch_stay_failures(self):
        folder = self.root / "direct"; folder.mkdir()
        self.sign_in()
        read = lambda: signed_in_snapshot(explore_url("chair cover"), EXPORT_JS, folder, root=self.signed, timeout=45,
                                          ready_expression=None, download_folder=folder)
        with patch("product_radar.trends_signed_in.signed_in_read", side_effect=SafetyError("SIGNED_IN_BROWSER_BUSY")):
            with self.assertRaises(SignedInWait) as held:
                read()
            self.assertTrue(held.exception.started_at and held.exception.finished_at)
        unavailable = {"state": "unavailable", "reason": "NATIVE_PUBLIC_BROWSER_NOT_INSTALLED", "navigation_started": False}
        with patch("product_radar.trends_signed_in.signed_in_read", return_value=unavailable):
            self.assertEqual(read(), unavailable)  # a failure to see, not a calm wait
        with patch("product_radar.trends_signed_in.signed_in_read", side_effect=SafetyError("NATIVE_PUBLIC_ARTIFACT_EXISTS")):
            with self.assertRaisesRegex(SafetyError, "ARTIFACT_EXISTS") as raised:
                read()
            self.assertNotIsInstance(raised.exception, SignedInWait)
            (folder / "owned_process.json").write_text("{}")
            with self.assertRaisesRegex(SafetyError, "ARTIFACT_EXISTS") as raised:
                signed_in_snapshot(explore_url("chair cover"), EXPORT_JS, folder, root=self.signed, timeout=45,
                                   ready_expression=None, download_folder=folder)
            self.assertNotIsInstance(raised.exception, SignedInWait)

    # Review probes (W2 review, 14 October): limits, rendering, the gate, sign-in pages.

    def test_googles_in_chart_error_is_a_refusal_whether_hidden_or_visible(self):
        self.sign_in()
        oops = page()["text"] + "\nOops! Something went wrong.\nPlease try again."
        for name, visibility in (("hidden", "hidden"), ("visible", "visible")):
            self.visibility = visibility
            self.snap = page(text=oops, rows=[], download_found=False, time_widget_present=False, related=None)
            receipt = self.capture(name)
            self.assertEqual((receipt["state"], receipt["reason"]), ("blocked", GOOGLE_LIMIT))
            self.assertTrue(_shared_access_failure(receipt, Path(receipt["folder"])))
        self.assertIn("Google Trends refused the signed-in read", gate.proof_gate_line(self.signed))
        self.assertIn("\"Something went wrong\" (its usage limit)", gate.proof_gate_line(self.signed))
        # The same words beside a drawn chart (another widget's error) are not a refusal.
        self.visibility = "visible"; self.snap = page(text=oops)
        self.assertEqual(self.capture("chart-drawn")["state"], "usable")

    def test_no_data_reads_alone_never_pass_the_gate(self):
        self.sign_in()
        for index in range(1, 4):
            self.snap = page("t %d" % index, rows=[], download_found=False, time_widget_present=True,
                             no_data_text="Hmm, your search doesn't have enough data to show here.")
            self.assertEqual(self.capture("nd-%d" % index, term="t %d" % index)["state"], "no_data")
        record = gate.proof_gate(self.signed)
        self.assertEqual((record["state"], len(record["reads"])), ("in_progress", 3))
        self.assertEqual(gate.proof_gate_line(self.signed), "Google Trends check: 3 of 3 signed-in reads saved complete results.")
        self.snap = page("t 4"); self.capture("chart", term="t 4")
        self.assertEqual(gate.proof_gate(self.signed)["state"], "passed")

    def test_ten_complete_reads_without_any_export_fail_the_gate(self):
        self.sign_in()
        for index in range(1, 11):
            self.snap = page("t %d" % index, rows=[], download_found=False, time_widget_present=True,
                             no_data_text="Your search doesn't have enough data to show here.")
            self.capture("nd-%d" % index, term="t %d" % index)
        record = gate.proof_gate(self.signed)
        self.assertEqual((record["state"], record["reads"][-1]["reason"]), ("failed", gate.NO_EXPORT))
        self.assertIn("ten Trends reads in a row had no chart to download", gate.proof_gate_line(self.signed))

    def test_a_rendering_failure_pauses_the_route_without_using_up_questions(self):
        self.sign_in(); self.visibility = "hidden"
        self.memory.put("concepts", "mat", {"status": "active", "description": {"label": "table mat"}})
        prepare_source_tasks(self.memory, [{"route": "google-trends", "request": {"query": term, "country": "IN"},
                                            "concept_ids": [concept]} for term, concept in (("chair cover", "chair"), ("table mat", "mat"))],
                             run_id="checks", as_of=now())
        self.snap = page(rows=[], download_found=False, time_widget_present=False, related=None)
        reasons = []
        for _ in range(4):
            reasons += [row.get("reason") for row in self.round()["results"]]
        self.assertEqual(len(self.calls), 1)  # one read met the problem; nothing more was spent
        self.assertIn("TRENDS_RENDERING_PAUSED", reasons)
        (owner,) = (row for row in self.memory.snapshot()["batch_runs"].values()
                    if row.get("kind") == ACQUISITION_KIND and row.get("active_response_id"))
        response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertEqual((response["source_state"], response["parse_reason"]), ("blocked", MINIMISED_NOT_RENDERED))
        # The owner signs in again: the pause ends, both questions are still asked and answered.
        self.sign_in(at=now()); self.visibility = "visible"
        for index in range(4):
            self.snap = page("chair cover" if index % 2 == 0 else "table mat")
            self.round()
        self.assertEqual(len(self.memory.snapshot()["trends_evidence"]), 2)

    def test_googles_sign_in_page_is_the_owners_wait_not_a_long_refusal(self):
        self.sign_in()
        self.snap = page(text="Sign in\nto continue to Google Trends", rows=[], download_found=False,
                         account={"avatar": False, "sign_in_link": False},
                         redirect="https://accounts.google.com/v3/signin/identifier?continue=x")
        self.assertFalse(signed_in_marker({"url": "https://accounts.google.com/ServiceLogin", "text": ""}))
        receipt = self.capture()
        self.assertEqual((receipt["state"], receipt["reason"]), ("blocked", "OWNER_SIGN_IN_REQUIRED"))
        self.assertFalse(_shared_access_failure(receipt, Path(receipt["folder"])))
        self.assertEqual(site_states(self.signed)["google-trends"]["state"], "sign_in_needed")
        self.assertEqual(site_states(self.signed)["google"]["state"], "sign_in_needed")  # the account itself
        self.assertEqual(gate.proof_gate(self.signed)["reads"], [])

    def test_trends_reads_confirm_the_google_account_sign_in(self):
        self.sign_in()
        for site in ("indiamart", "dropship-india", "dropdash"):
            record_site_observation(self.signed, site, True)
        self.assertEqual(site_states(self.signed)["google"]["state"], "not_checked")
        self.assertEqual(sign_in_line(self.signed), "Sign-ins will be confirmed on the next research reads.")
        self.capture()
        self.assertEqual((site_states(self.signed)["google"]["state"], site_states(self.signed)["google"]["route"]),
                         ("signed_in", "google-trends"))
        self.assertEqual(sign_in_line(self.signed), "Research sites are signed in.")
        # Trends' own sign-in link says only that Trends is signed out: the account record is unchanged.
        self.snap = page(avatar=False, link=True)
        self.capture("read-2")
        self.assertEqual(site_states(self.signed)["google"]["state"], "signed_in")
        self.assertEqual(site_states(self.signed)["google-trends"]["state"], "expired")
        # Google's own sign-in page means the account itself is signed out.
        self.sign_in(now())
        self.snap = page(text="Sign in\nto continue to Google Trends", rows=[], download_found=False,
                                         account={"avatar": False, "sign_in_link": False},
                                         redirect="https://accounts.google.com/v3/signin/identifier?continue=x")
        later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with patch("product_radar.signed_in_pacing.utc_now", return_value=later), \
                patch("product_radar.signed_in_browser.utc_now", return_value=later):
            self.capture("read-3")
        self.assertEqual(site_states(self.signed)["google"]["state"], "expired")
        for unclear in (None, {"url": explore_url("chair cover"), "account": {"avatar": False, "sign_in_link": True}}):
            self.assertIsNone(google_account_marker(unclear))

    def test_a_sign_in_page_holds_the_shared_route_briefly_and_clears_after_signing_in(self):
        self.sign_in()
        self.snap = page(text="Sign in to continue to Google Trends", rows=[], download_found=False,
                         account={"avatar": False, "sign_in_link": True})
        self.question()
        self.assertEqual(self.round()["counts"], {"blocked": 1})
        wait = source_access_wait(self.memory.snapshot(), signed_in_access_scope(explore_url("chair cover")), as_of=now())
        self.assertLessEqual(timestamp(wait["next_retry_at"]) - timestamp(now()), timedelta(minutes=5))  # never hours
        self.sign_in(at=now()); self.snap = page(); self.shift = 400
        self.assertEqual(self.round()["counts"], {"acquired": 1})

    def test_a_saved_read_of_a_replaced_transport_is_never_replayed_as_this_route(self):
        self.sign_in()
        capture_direct_trends(build_trends_request("chair cover"), "old", data_root=self.captures,
                              snapshot_reader=lambda _request: original())
        handler = create_trends_handlers(self.memory, data_root=self.captures, signed_in_root=self.signed)["google-trends"]
        context = {"route": "google-trends", "request": build_trends_request("chair cover"), "acquisition_id": "old",
                   "concept_ids": ["chair"], "description_hashes": {"chair": digest({"label": "chair cover"})}}
        with self.assertRaisesRegex(SafetyError, "DIRECT_TRENDS_TRANSPORT_REPLACED"):
            handler["reconcile"](context)
        self.assertEqual(self.memory.snapshot()["trends_evidence"], {})

    def test_cutover_releases_retry_times_set_by_the_replaced_route(self):
        self.question()
        task_id, task = next((rid, row) for rid, row in self.memory.snapshot()["query_tasks"].items()
                             if row.get("kind") == "all_source_task_v1")
        later = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
        old = dict(task, next_retry_at=later, source_physical_request={"query": "chair cover", "country": "IN"})
        self.memory.put("query_tasks", task_id, old, previous=task)
        self.assertEqual(release_replaced_trends_waits(self.memory, as_of=now()), [task_id])
        self.assertIsNone(self.memory.snapshot()["query_tasks"][task_id]["next_retry_at"])
        self.assertEqual(release_replaced_trends_waits(self.memory, as_of=now()), [])
        # The signed-in route's own waits stay.
        current = self.memory.snapshot()["query_tasks"][task_id]
        own = dict(current, next_retry_at=later, source_physical_request=build_shared_trends_request("chair cover"))
        self.memory.put("query_tasks", task_id, own, previous=current)
        self.assertEqual(release_replaced_trends_waits(self.memory, as_of=now()), [])

    def test_bookkeeping_that_cannot_be_saved_never_fails_the_read_and_is_retried(self):
        self.sign_in()
        with patch("product_radar.trends_proof_gate.records_lock", side_effect=SafetyError("SIGNED_IN_RECORD_BUSY")):
            receipt = self.capture()
        self.assertEqual(receipt["state"], "usable")
        self.assertTrue((self.captures / "read-1" / PENDING_BOOKKEEPING).is_file())
        self.assertEqual(gate.proof_gate(self.signed)["reads"], [])
        self.snap = page("table mat"); self.capture("read-2", term="table mat")
        self.assertFalse((self.captures / "read-1" / PENDING_BOOKKEEPING).exists())
        self.assertEqual([row["acquisition_id"] for row in gate.proof_gate(self.signed)["reads"]], ["read-1", "read-2"])
        atomic_json(gate._path(self.signed), {"broken": True})
        self.snap = page("sofa"); self.assertEqual(self.capture("read-3", term="sofa")["state"], "usable")
        self.assertTrue((self.captures / "read-3" / PENDING_BOOKKEEPING).is_file())

    def test_a_refused_shared_read_tells_the_owner_when_trends_is_tried_again(self):
        self.sign_in(); self.status = 429
        self.snap = page(text="Too many requests", rows=[], download_found=False, account={"avatar": False, "sign_in_link": False})
        self.question(); self.round()
        line = gate.proof_gate_line(self.signed)
        self.assertRegex(line, r"^Google Trends is waiting: Google is refusing Trends reads for now\. Trying again at "
                               r"\d{2} \w{3}, \d{2}:\d{2} UTC\. Research continues without Trends\.$")
        atomic_json(records_path(self.signed, "trends-render-hold.json"), {"schema_version": 2})
        with self.assertRaisesRegex(SafetyError, "TRENDS_RENDER_HOLD_INVALID"):
            render_hold(self.signed)

    def test_route_wait_records_expire_fail_closed_and_never_fail_a_read(self):
        gate.note_route_wait(self.signed, "DIRECT_TRENDS_ACCESS_CHALLENGE", "2026-01-01T00:00:00+00:00")
        self.assertIsNone(gate.route_wait(self.signed))  # already over
        atomic_json(records_path(self.signed, "trends-route-wait.json"), {"schema_version": 1})
        with self.assertRaisesRegex(SafetyError, "TRENDS_ROUTE_WAIT_INVALID"):
            gate.route_wait(self.signed)
        self.sign_in(); self.status = 429
        self.snap = page(text="Too many requests", rows=[], download_found=False)
        self.question()
        with patch("product_radar.trends_proof_gate.note_route_wait", side_effect=SafetyError("SIGNED_IN_RECORD_BUSY")):
            self.assertEqual(self.round()["counts"], {"blocked": 1})

    def test_a_pending_note_whose_read_cannot_be_replayed_is_left_for_its_reconciliation(self):
        self.sign_in()
        broken = self.captures / "broken"; broken.mkdir(parents=True)
        atomic_json(broken / PENDING_BOOKKEEPING, {"reason": "SIGNED_IN_RECORD_BUSY"})
        self.assertEqual(self.capture()["state"], "usable")
        self.assertTrue((broken / PENDING_BOOKKEEPING).is_file())
        atomic_json(self.captures / "read-1" / PENDING_BOOKKEEPING, {"reason": "SIGNED_IN_RECORD_BUSY"})
        with patch("product_radar.trends_proof_gate.records_lock", side_effect=SafetyError("SIGNED_IN_RECORD_BUSY")):
            self.snap = page("sofa"); self.capture("read-2", term="sofa")
        self.assertTrue((self.captures / "read-1" / PENDING_BOOKKEEPING).is_file())  # still busy: tried later

    def test_ready_waits_end_on_refusal_pages_and_related_reads_only_its_widget(self):
        for text in ("before you continue to google", "oops! something went wrong", "sign in to continue"):
            self.assertIn(text, READY_JS)
        self.assertIn("location.hostname!=='trends.google.com'", READY_JS)
        self.assertIn("createTreeWalker", EXPORT_JS)
        related = EXPORT_JS[EXPORT_JS.index("const related"):EXPORT_JS.index("const text=")]
        self.assertNotIn("querySelectorAll('*')", related)

    # The programme reads Trends only through the signed-in route.

    def test_the_programme_uses_only_the_signed_in_route(self):
        self.sign_in()
        with self.assertRaises(TypeError):
            create_trends_handlers(self.memory, transport="auto")
        with self.assertRaisesRegex(SafetyError, "CAPTURE_OPTIONS_INVALID"):
            capture_direct_trends(build_trends_request("chair cover"), "x", data_root=self.captures, transport="native",
                                  signed_in_root=self.signed)
        installed = installed_source_handlers(self.memory, trends_options={"shared_acquisitions": True, "data_root": self.captures,
                                                                           "signed_in_root": self.signed})
        prepare_source_tasks(self.memory, [{"route": "google-trends", "request": {"query": "chair cover", "country": "IN"},
                                            "concept_ids": ["chair"]}], run_id="checks", as_of=now())
        with patch("product_radar.trends_capture.capture", side_effect=AssertionError("No Playwright browser")):
            result = execute_source_run(self.memory, "checks", handlers={"google-trends": installed["google-trends"]},
                as_of=now(), bounded_call=lambda callback, context, timeout: callback(context), shared_marketplace=True)
        self.assertEqual(result["counts"], {"acquired": 1})
        (call,) = self.calls
        self.assertEqual((call["profile"], call["root"]), ("signed-in", self.signed))
        self.assertNotIn("headed", call)


class RisingQueriesTests(unittest.TestCase):
    def test_states_of_the_related_queries_widget(self):
        self.assertEqual(parse_rising_queries(None)["state"], "not_rendered")
        self.assertEqual(parse_rising_queries({"text": "Related queries\nTop\n1\nchair\n100"})["state"], "top_selected")
        self.assertEqual(parse_rising_queries({"text": "Related queries\nRising\nTop"})["state"], "unreadable")
        none = parse_rising_queries({"text": "Related queries\nRising\nYour search doesn't have enough data to show here."})
        self.assertEqual((none["state"], none["queries"]), ("none", []))
        self.assertEqual(parse_rising_queries({"text": "Related queries\nRising\n1\nchair\nsoon"})["state"], "unreadable")
        skipped = parse_rising_queries({"text": "Rising\n2\nnot first\n+5%\n1\nfirst\n+5%\n2\n+5%\n+5%"})
        self.assertEqual([row["query"] for row in skipped["queries"]], ["first"])
        self.assertIsNone(skipped["total"])

    def test_unverified_scope_saves_no_rising_queries(self):
        from product_radar.trends_rising import save_rising_queries
        self.assertIsNone(save_rising_queries(Path(tempfile.gettempdir()), {"scope_verified": False, "artifacts": {}}))


class SharedQuarantineTests(unittest.TestCase):
    """Every signed-in route's saved results: cleaned, or quarantined unread."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "direct_trends" / "acquisition" / "native_result.json"
        self.path.parent.mkdir(parents=True)

    def test_unreadable_results_are_quarantined_and_readable_ones_cleaned(self):
        self.assertEqual(protect_saved(self.path, redact_signed_in), "absent")
        atomic_json(self.path, {"snapshot": {"html": "owner.research@example.com"}})
        self.assertEqual(protect_saved(self.path, redact_signed_in), "redacted")
        self.assertNotIn("owner.research", self.path.read_text())
        self.path.write_text('{"snapshot": "owner.research@example.com"', encoding="utf-8")
        self.assertEqual(protect_saved(self.path, redact_signed_in), "quarantined")
        self.assertFalse(self.path.exists())
        (held,) = (self.path.parent.parent / QUARANTINE).iterdir()
        self.assertTrue(held.name.endswith("-acquisition-native_result.json.unread"))

    def test_a_cleaned_copy_that_cannot_be_saved_quarantines_the_raw_file(self):
        atomic_json(self.path, {"snapshot": {"html": "owner.research@example.com"}})
        with patch("product_radar.signed_in_saved.atomic_json", side_effect=OSError("disk full")):
            self.assertEqual(protect_saved(self.path, redact_signed_in), "quarantined")
        self.assertFalse(self.path.exists())

    def test_account_picture_and_account_ids_are_removed(self):
        html = ('<img src="https://lh3.googleusercontent.com/a/ACg8ocJ_x-Yz=s32-c"> '
                '<img src="//lh5.googleusercontent.com/ogw/AF2bZyi9=s64"> data-id="112233445566778899001" '
                'chart 1234567 file-112233445566778899001.png')
        clean = redact_signed_in({"html": html})["html"]
        self.assertNotIn("ACg8ocJ", clean); self.assertNotIn("AF2bZyi9", clean)
        self.assertIn("https://lh3.googleusercontent.com/a/redacted", clean)
        self.assertIn('data-id="[account id redacted]"', clean)
        self.assertIn("chart 1234567", clean); self.assertIn("file-112233445566778899001.png", clean)

    def test_a_failed_clean_up_never_replaces_an_interrupted_reads_deadline_proof(self):
        from product_radar.native_public_browser import native_snapshot
        from product_radar.source_deadline import SourceActionDeadline
        deadline = SourceActionDeadline(); deadline.native_result = {"state": "failed", "reason": "SOURCE_ACTION_TIMEOUT"}
        folder = Path(self.temp.name) / "capture"; folder.mkdir()
        with patch("product_radar.native_public_browser.CHROME") as chrome, \
                patch("product_radar.native_public_browser.POWERSHELL") as shell, \
                patch("product_radar.signed_in_browser.research_profile", return_value="C:\\p"), \
                patch("product_radar.native_public_browser._snapshot", side_effect=deadline), \
                patch("product_radar.signed_in_saved.protect_saved", side_effect=SafetyError("SIGNED_IN_REDACTION_FAILED")):
            chrome.is_file.return_value = shell.is_file.return_value = True
            with self.assertRaises(SourceActionDeadline) as raised:
                native_snapshot("https://trends.google.com/trends/explore?q=x", "'x'", folder, profile="signed-in",
                                root=Path(self.temp.name) / "store", timeout=30)
        self.assertIs(raised.exception, deadline)
        self.assertEqual(deadline.native_result["redaction_failure"], "SIGNED_IN_REDACTION_FAILED")

    def test_indiamart_uses_the_shared_quarantine(self):
        self.assertEqual(INDIAMART_QUARANTINE, QUARANTINE)
        self.path.write_text("[broken", encoding="utf-8")
        self.assertEqual(indiamart_protect(self.path), "quarantined")
        self.assertEqual(len(list((self.path.parent.parent / QUARANTINE).iterdir())), 1)

    def test_a_signed_in_browser_result_that_cannot_be_read_is_quarantined(self):
        from test_native_public_browser import SignedInNativeBrowserTests
        reader = SignedInNativeBrowserTests("test_signed_in_read_uses_the_lasting_profile_minimised_without_focus")
        reader.setUp(); self.addCleanup(reader.doCleanups)
        original_runner = reader.signed_runner

        def broken(result):
            run = original_runner(result)

            def wrapped(command, **kwargs):
                reply = run(command, **kwargs)
                if getattr(reader, "payload", None) and Path(reader.payload["output"]).is_file():
                    Path(reader.payload["output"]).write_text('{"snapshot": "owner.research@example.com"', encoding="utf-8")
                return reply
            return wrapped
        reader.signed_runner = broken
        actual = reader.read({"state": "captured", "snapshot": {}, "owned_process_exited": True})
        self.assertEqual(actual["state"], "failed")
        self.assertFalse((reader.capture / "native_result.json").exists())
        self.assertEqual(len(list((reader.capture.parent / QUARANTINE).iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
